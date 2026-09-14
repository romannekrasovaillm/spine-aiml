//! Состояние TUI-приложения: блоки чата, строка ввода, вкладки, обработка
//! клавиш и сообщений фоновых задач. App владеет состоянием в event loop —
//! никаких `Arc<Mutex>`; ход агента и слэш-команды выполняются в `tokio::spawn`
//! и возвращают сессию сообщением.

use std::collections::{HashMap, VecDeque};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::Frame;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::agent::{AgentEvent, AgentSession, prompts, slash};
use crate::config::Config;
use crate::error::Result;
use crate::llm::{ChatMessage, LlmRegistry};
use crate::mcp::{self, McpManager};
use crate::subagent::BackgroundNotice;
use crate::tool::{AskRequest, ToolContext, ToolProgress};
use crate::tools;

use super::caps::Caps;
use super::text;
use super::theme::Theme;

/// Максимум блоков в истории чата (сверху отбрасываются самые старые).
const MAX_BLOCKS: usize = 500;
/// Ёмкость канала событий агента (bounded — backpressure до модели).
const AGENT_EVENTS_CAP: usize = 64;
/// Максимум записей в истории ввода.
const MAX_HISTORY: usize = 100;
/// Максимум сообщений в очереди ожидания (пока агент занят).
const MAX_QUEUE: usize = 32;
/// Встроенный системный промпт (fallback, если шаблона `architect` нет).
const FALLBACK_SYSTEM_PROMPT: &str = "Ты — solution-архитектор в контуре AI/ML-исследователя. \
     Помогаешь проектировать решения, ведёшь ADR и architecture-spine, оцениваешь архитектуру \
     по рубрикам, готовишь handoff-пакеты кодовым агентам. Отвечай по-русски, точно и по делу.";

/// Сколько тиков (по 120 мс) живёт успешный тост — около 4 секунд.
const TOAST_TICKS: usize = 33;

/// Уровень тоста: успех гаснет сам, ошибка ждёт действия пользователя.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ToastLevel {
    /// Действие удалось.
    Ok,
    /// Действие не удалось — сообщение не исчезает само.
    Err,
}

/// Тост: короткий результат действия в статус-баре. Модальное «ОК» на каждый
/// чих — антипаттерн (AP8): успех подтверждается строкой, которая гаснет,
/// ошибка — строкой, которая ждёт следующего действия.
#[derive(Debug, Clone)]
pub(crate) struct Toast {
    /// Текст без символа: символ добавит рендер по уровню и глифам.
    pub(crate) text: String,
    /// Уровень (цвет и символ).
    pub(crate) level: ToastLevel,
    /// Тик появления.
    born: usize,
    /// Время жизни в тиках; `None` — до следующего действия (ошибки).
    ttl: Option<usize>,
}

impl Toast {
    /// Успех: гаснет сам через [`TOAST_TICKS`].
    fn ok(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            level: ToastLevel::Ok,
            born: 0,
            ttl: Some(TOAST_TICKS),
        }
    }

    /// Ошибка: живёт до следующего действия пользователя.
    fn err(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            level: ToastLevel::Err,
            born: 0,
            ttl: None,
        }
    }

    /// Жив ли тост на тике `now`.
    fn alive(&self, now: usize) -> bool {
        self.ttl
            .is_none_or(|ttl| now.saturating_sub(self.born) < ttl)
    }
}

/// Активный экран приложения.
#[derive(Debug)]
pub(crate) enum Screen {
    /// Основной чат — единственный рабочий экран: старт сразу здесь, логотип
    /// уезжает вверх как первый блок диалога. Отдельной заставки нет: она
    /// стоила лишнего нажатия и отдавала первый экран декору (A1, A5, AP10).
    Chat,
    /// Фатальная ошибка инициализации: показать текст и выйти по `q`.
    Fatal(String),
}

/// Состояние вызова инструмента.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ToolState {
    /// Выполняется.
    Running,
    /// Завершился успешно.
    Ok,
    /// Завершился ошибкой.
    Error,
}

/// Живое состояние выполняющегося tool-вызова: когда стартовал и что
/// сейчас пишет в вывод (снапшоты [`ToolProgress`]). Хранится отдельно от
/// [`ChatBlock`] по индексу блока: живые поля не входят в экспорт и не
/// раздувают конструкторы блока (сцены снимков, тесты).
#[derive(Debug)]
pub(crate) struct ToolLive {
    /// Момент старта вызова (таймер «выполняется N:SS»).
    pub(crate) started: Instant,
    /// Хвост живого вывода (последний снапшот, уже усечён инструментом).
    pub(crate) tail: String,
}

/// Блок чата (центральная колонка).
#[derive(Debug)]
pub(crate) enum ChatBlock {
    /// Логотип `ArchSpine` — первый блок диалога (уезжает вверх по мере
    /// переписки, как любой контент лога).
    Logo,
    /// Сообщение пользователя.
    User(String),
    /// Ответ ассистента (стримится дельтами).
    Assistant(String),
    /// «Мысли» модели (`reasoning_content`): компактный приглушённый блок,
    /// стримится дельтами до видимого ответа.
    Thinking(String),
    /// Вызов инструмента.
    Tool {
        /// Имя инструмента.
        name: String,
        /// Состояние выполнения.
        state: ToolState,
        /// Краткое действие из аргументов (путь, команда, запрос) — чтобы
        /// пользователь видел, ЧТО делает агент, а не только имя инструмента.
        action: String,
        /// Краткий итог (первая строка вывода).
        summary: String,
    },
    /// Результат слэш-команды.
    System {
        /// Введённая команда.
        command: String,
        /// Текст результата.
        text: String,
    },
    /// Ошибка (красный блок).
    Error(String),
}

/// Вкладка правой панели.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RightTab {
    /// Последний рендер mermaid-диаграммы.
    Mermaid,
    /// Последний отчёт рубрики.
    Rubric,
    /// Последний результат поиска по знаниям/вебу.
    Knowledge,
    /// Последний dashboard флота (`fleet_run` / `fleet status`).
    Fleet,
    /// Фоновые задачи субагентов текущей сессии (`subagent_run`).
    Subagents,
}

impl RightTab {
    /// Все вкладки по порядку.
    ///
    /// Порядок выбран по смыслу: первые три — «артефакты разговора» (что
    /// агент вернул: диаграмма, рубрика, знания), последние две — «ход
    /// исполнения» (кто и что сейчас работает). Флот и Субагенты стоят
    /// рядом: оба про запущенные задачи, но Флот читает журнал прогонов с
    /// диска (batch, переживает сессию), а Субагенты — живой реестр памяти
    /// текущей сессии. Новая вкладка добавлена в конец, а не в середину,
    /// чтобы не сдвинуть уже выученные F1–F6 и нумерацию читателей `ALL`.
    pub(crate) const ALL: [Self; 5] = [
        Self::Mermaid,
        Self::Rubric,
        Self::Knowledge,
        Self::Fleet,
        Self::Subagents,
    ];

    /// Следующая вкладка (цикл по Tab).
    fn next(self) -> Self {
        match self {
            Self::Mermaid => Self::Rubric,
            Self::Rubric => Self::Knowledge,
            Self::Knowledge => Self::Fleet,
            Self::Fleet => Self::Subagents,
            Self::Subagents => Self::Mermaid,
        }
    }

    /// Предыдущая вкладка (цикл по Shift+Tab — обратный ход к `next`).
    fn prev(self) -> Self {
        match self {
            Self::Mermaid => Self::Subagents,
            Self::Rubric => Self::Mermaid,
            Self::Knowledge => Self::Rubric,
            Self::Fleet => Self::Knowledge,
            Self::Subagents => Self::Fleet,
        }
    }

    /// Заголовок вкладки.
    pub(crate) fn title(self) -> &'static str {
        match self {
            Self::Mermaid => "Mermaid",
            Self::Rubric => "Рубрика",
            Self::Knowledge => "Знания",
            Self::Fleet => "Флот",
            Self::Subagents => "Субагенты",
        }
    }
}

/// Содержимое вкладок правой панели.
#[derive(Debug, Default)]
pub(crate) struct Panels {
    /// Вкладка Mermaid: последний рендер диаграммы.
    pub(crate) mermaid: String,
    /// Вкладка «Рубрика»: последний отчёт.
    pub(crate) rubric: String,
    /// Вкладка «Знания»: последний результат kb/web.
    pub(crate) knowledge: String,
    /// Вкладка «Флот»: последний dashboard прогона.
    pub(crate) fleet: String,
    /// Вкладка «Субагенты»: фоновые задачи текущей сессии.
    pub(crate) subagents: String,
}

impl Panels {
    /// Текст активной вкладки.
    pub(crate) fn content(&self, tab: RightTab) -> &str {
        match tab {
            RightTab::Mermaid => &self.mermaid,
            RightTab::Rubric => &self.rubric,
            RightTab::Knowledge => &self.knowledge,
            RightTab::Fleet => &self.fleet,
            RightTab::Subagents => &self.subagents,
        }
    }

    /// Подсказка-заглушка пустой вкладки.
    pub(crate) fn placeholder(tab: RightTab) -> &'static str {
        match tab {
            RightTab::Mermaid => {
                "Пока пусто. Здесь появится последний рендер диаграммы \
                 (инструмент mermaid_render или /mermaid)."
            }
            RightTab::Rubric => {
                "Пока пусто. Здесь появится последний отчёт рубрики (/rubric run …)."
            }
            RightTab::Knowledge => {
                "Пока пусто. Здесь появится последний результат поиска (/kb, /web, /fetch)."
            }
            RightTab::Fleet => {
                "Пока пусто. Здесь появится последний dashboard флота \
                 (инструмент fleet_run или /fleet)."
            }
            RightTab::Subagents => {
                "Пока пусто. Здесь появится список субагентов текущей сессии \
                 (инструмент subagent_run или /agents)."
            }
        }
    }
}

/// Сообщение в event loop от фоновых задач.
pub(crate) enum AppMessage {
    /// Событие агентного цикла (дельта, инструмент, конец хода).
    AgentEvent(AgentEvent),
    /// Инструмент `propose_options` просит пользователя выбрать вариант.
    AskUser(AskRequest),
    /// Живой прогресс инструмента (хвост вывода долгой команды).
    ToolProgress(ToolProgress),
    /// Ход агента завершён: сессия возвращается владельцу (App).
    TurnFinished {
        /// Сессия после хода.
        session: AgentSession,
        /// Итог хода (финальный текст или ошибка).
        result: Result<String>,
    },
    /// Слэш-команда завершена.
    SlashFinished {
        /// Сессия после команды.
        session: AgentSession,
        /// Исход команды.
        result: Result<slash::SlashOutcome>,
    },
    /// Фоновая задача (субагент, `harness_run`, ralph) завершилась.
    BackgroundFinished(BackgroundNotice),
}

/// Состояние строки ввода: текст, курсор, история, автодополнение.
#[derive(Debug, Default)]
pub(crate) struct InputState {
    /// Текст ввода.
    text: String,
    /// Позиция курсора (байтовый индекс, всегда на границе char).
    cursor: usize,
    /// История отправленных строк (старые — в начале).
    history: VecDeque<String>,
    /// Индекс навигации по истории (None — редактируется черновик).
    hist_idx: Option<usize>,
    /// Черновик, сохранённый при уходе в историю.
    draft: String,
    /// Активные кандидаты автодополнения и текущий индекс (цикл по Tab).
    completion: Option<(Vec<&'static str>, usize)>,
}

impl InputState {
    /// Текущий текст.
    pub(crate) fn text(&self) -> &str {
        &self.text
    }

    /// Позиция курсора (байтовый индекс).
    pub(crate) fn cursor(&self) -> usize {
        self.cursor
    }

    /// Устанавливает текст, курсор — в конец; сбрасывает автодополнение.
    pub(crate) fn set_text(&mut self, text: String) {
        self.text = text;
        self.cursor = self.text.len();
        self.completion = None;
    }

    /// Вставляет символ в позицию курсора.
    fn insert_char(&mut self, c: char) {
        self.completion = None;
        self.text.insert(self.cursor, c);
        self.cursor += c.len_utf8();
    }

    /// Вставляет перевод строки (многострочный ввод: Ctrl+J / Shift+Enter).
    fn insert_newline(&mut self) {
        self.completion = None;
        self.text.insert(self.cursor, '\n');
        self.cursor += 1;
    }

    /// Логическая строка (по '\n') и колонка в символах под курсором.
    fn line_col(&self) -> (usize, usize) {
        let before = &self.text[..self.cursor];
        let line = before.matches('\n').count();
        let col = before.rsplit('\n').next().map_or(0, |s| s.chars().count());
        (line, col)
    }

    /// Байтовый индекс колонки `col` логической строки `line`
    /// (col клампится по длине строки).
    fn byte_at_line_col(&self, line: usize, col: usize) -> usize {
        let mut start = 0;
        for (n, part) in self.text.split('\n').enumerate() {
            if n == line {
                return part
                    .char_indices()
                    .nth(col)
                    .map_or(start + part.len(), |(i, _)| start + i);
            }
            start += part.len() + 1; // + '\n'
        }
        self.text.len()
    }

    /// Up внутри многострочного ввода: true, если курсор ушёл на строку выше
    /// (false — курсор на первой строке, Up свободен для истории).
    fn move_up_line(&mut self) -> bool {
        let (line, col) = self.line_col();
        if line == 0 {
            return false;
        }
        self.cursor = self.byte_at_line_col(line - 1, col);
        true
    }

    /// Down внутри многострочного ввода: true, если курсор ушёл на строку
    /// ниже (false — курсор на последней строке, Down свободен для истории).
    fn move_down_line(&mut self) -> bool {
        let (line, col) = self.line_col();
        if line >= self.text.matches('\n').count() {
            return false;
        }
        self.cursor = self.byte_at_line_col(line + 1, col);
        true
    }

    /// Удаляет символ перед курсором.
    fn backspace(&mut self) {
        self.completion = None;
        if self.cursor == 0 {
            return;
        }
        let prev = self.text[..self.cursor]
            .chars()
            .next_back()
            .map_or(1, char::len_utf8);
        self.text.replace_range(self.cursor - prev..self.cursor, "");
        self.cursor -= prev;
    }

    /// Удаляет символ под курсором.
    fn delete(&mut self) {
        self.completion = None;
        if let Some(c) = self.text[self.cursor..].chars().next() {
            self.text
                .replace_range(self.cursor..self.cursor + c.len_utf8(), "");
        }
    }

    /// Курсор на символ влево.
    fn move_left(&mut self) {
        if self.cursor > 0 {
            self.cursor = self.text[..self.cursor]
                .chars()
                .next_back()
                .map_or(0, |c| self.cursor - c.len_utf8());
        }
    }

    /// Курсор на символ вправо.
    fn move_right(&mut self) {
        if let Some(c) = self.text[self.cursor..].chars().next() {
            self.cursor += c.len_utf8();
        }
    }

    /// Курсор в начало текущей логической строки (по '\n').
    fn move_home(&mut self) {
        let (line, _) = self.line_col();
        self.cursor = self.byte_at_line_col(line, 0);
    }

    /// Курсор в конец текущей логической строки (по '\n').
    fn move_end(&mut self) {
        let (line, _) = self.line_col();
        let len = self
            .text
            .split('\n')
            .nth(line)
            .map_or(0, |s| s.chars().count());
        self.cursor = self.byte_at_line_col(line, len);
    }

    /// Забирает введённую строку, сохраняя непустую в истории.
    fn submit(&mut self) -> String {
        let text = self.text.trim().to_string();
        if !text.is_empty() && self.history.back() != Some(&text) {
            self.history.push_back(text.clone());
            while self.history.len() > MAX_HISTORY {
                self.history.pop_front();
            }
        }
        self.text.clear();
        self.cursor = 0;
        self.hist_idx = None;
        self.draft.clear();
        self.completion = None;
        text
    }

    /// Up: шаг назад по истории (черновик сохраняется).
    fn history_up(&mut self) {
        if self.history.is_empty() {
            return;
        }
        let idx = match self.hist_idx {
            None => {
                self.draft.clone_from(&self.text);
                self.history.len() - 1
            }
            Some(0) => 0,
            Some(i) => i - 1,
        };
        self.hist_idx = Some(idx);
        if let Some(entry) = self.history.get(idx) {
            self.set_text(entry.clone());
        }
    }

    /// Down: шаг вперёд по истории; за последней записью — черновик.
    fn history_down(&mut self) {
        let Some(idx) = self.hist_idx else {
            return;
        };
        if idx + 1 < self.history.len() {
            self.hist_idx = Some(idx + 1);
            if let Some(entry) = self.history.get(idx + 1) {
                self.set_text(entry.clone());
            }
        } else {
            self.hist_idx = None;
            let draft = std::mem::take(&mut self.draft);
            self.set_text(draft);
        }
    }

    /// Tab: дополняет слэш-команду; повторные Tab циклят кандидатов.
    /// Возвращает false, если кандидатов нет (Tab свободен для вкладок).
    fn complete_tab(&mut self) -> bool {
        // Уже в цикле дополнения и текст не редактировали — следующий кандидат.
        if let Some((cands, idx)) = &mut self.completion {
            if self.text == cands[*idx] {
                *idx = (*idx + 1) % cands.len();
                let candidate = cands[*idx];
                // Напрямую, не через set_text: цикл кандидатов сохраняем.
                self.text = candidate.to_string();
                self.cursor = self.text.len();
                return true;
            }
        }
        self.completion = None;
        let cands = text::completion_candidates(&self.text);
        if cands.is_empty() {
            return false;
        }
        self.set_text(cands[0].to_string());
        self.completion = Some((cands, 0));
        true
    }

    /// Приглушённая подсказка-дополнение справа от ввода (суффикс кандидата).
    pub(crate) fn ghost_hint(&self) -> Option<String> {
        let first = text::completion_candidates(&self.text).into_iter().next()?;
        Some(first[self.text.len()..].to_string())
    }
}

/// Приложение TUI: владеет всем состоянием, события приходят по каналу.
/// Назначение активной модальной панели выбора.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum AskKind {
    /// Ответ уходит инструменту `propose_options` (oneshot-канал, агент ждёт).
    Tool,
    /// Пикер модели (`/model` без аргумента): выбор сводится к `/model <name>`.
    ModelPicker,
    /// Пикер сессии (`/resume` без аргумента): выбор сводится к `/resume <file>`.
    SessionPicker,
}

/// Активная модальная панель выбора (инструмент `propose_options` ждёт ответа).
#[derive(Debug)]
pub(crate) struct AskState {
    /// Вопрос агента.
    pub(crate) question: String,
    /// Варианты (2–4; у пикера моделей — по числу `[models]`).
    pub(crate) options: Vec<crate::tool::AskOption>,
    /// `label` рекомендуемого варианта (если есть).
    pub(crate) recommended: Option<String>,
    /// Индекс подсвеченного варианта.
    pub(crate) selected: usize,
    /// Канал ответа инструменту (None — пикер или ответ уже отправлен).
    pub(crate) reply: Option<tokio::sync::oneshot::Sender<String>>,
    /// Назначение модалки.
    pub(crate) kind: AskKind,
}

/// Полноэкранный просмотрщик активной вкладки (F4): вертикальный и
/// ГОРИЗОНТАЛЬНЫЙ скролл широкого mermaid-арта. Основной экран не
/// перестраивается — viewer перекрывает его модальным слоем.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ViewerState {
    /// Вертикальный сдвиг (строки сверху).
    pub(crate) scroll_y: usize,
    /// Горизонтальный сдвиг (display-колонки слева).
    pub(crate) scroll_x: usize,
}

/// Состояние поиска по диалогу (Ctrl+F).
#[derive(Debug, Default)]
pub(crate) struct DialogSearch {
    /// Текст запроса (редактируется посимвольно).
    pub(crate) query: String,
    /// Индексы строк диалога с совпадениями (пересчёт каждый кадр рендера —
    /// дёшево и всегда актуально, даже пока модель стримит дельты).
    pub(crate) matches: Vec<usize>,
    /// Текущее совпадение (индекс в `matches`).
    pub(crate) current: usize,
    /// Перескочить к `current` при ближайшем кадре (после ввода/шага).
    pub(crate) dirty: bool,
}

pub(crate) struct App {
    /// Активный экран.
    pub(crate) screen: Screen,
    /// Активный вопрос выбора вариантов (модалка поверх чата).
    pub(crate) ask: Option<AskState>,
    /// Полноэкранный просмотрщик вкладки (F4); None — обычный лейаут.
    pub(crate) viewer: Option<ViewerState>,
    /// Правая панель (Mermaid/Рубрика/Знания) видна; F5 — скрыть/показать.
    pub(crate) right_visible: bool,
    /// Открыт оверлей справки (`?`); Esc/`?`/`q` закрывают его.
    pub(crate) help: bool,
    /// Сдвиг прокрутки справки (строки сверху).
    pub(crate) help_scroll: usize,
    /// Приёмник запросов выбора от инструментов (форвардится в attach).
    ask_rx: Option<mpsc::Receiver<AskRequest>>,
    /// Приёмник живого прогресса инструментов (форвардится в attach).
    progress_rx: Option<mpsc::UnboundedReceiver<ToolProgress>>,
    /// Сессия агента (None — пока ход выполняется в фоновой задаче).
    session: Option<AgentSession>,
    /// Контекст инструментов (для слэш-команд; cwd — в статус-баре).
    pub(crate) tool_ctx: ToolContext,
    /// MCP-менеджер (нужен для shutdown); None, если не подключён.
    mcp: Option<Arc<McpManager>>,
    /// Блоки чата.
    pub(crate) blocks: Vec<ChatBlock>,
    /// Открыт ли сейчас assistant-блок для дельт.
    assistant_open: bool,
    /// Была ли хотя бы одна дельта за текущий ход.
    turn_got_output: bool,
    /// Строка ввода.
    pub(crate) input: InputState,
    /// Сдвиг прокрутки чата от низа (0 — прилип к низу).
    pub(crate) scroll: usize,
    /// Поиск по диалогу (Ctrl+F): активная строка запроса с подсветкой
    /// совпадений и переходом Enter/Alt+Enter (E4 — `/` занят командами).
    pub(crate) search: Option<DialogSearch>,
    /// Авто-прилипание к низу при новых событиях.
    stick: bool,
    /// Высота вьюпорта чата (заполняется при рендере).
    pub(crate) viewport: usize,
    /// Активная вкладка правой панели.
    pub(crate) right_tab: RightTab,
    /// Сдвиг прокрутки правой панели (строки сверху; 0 — начало).
    ///
    /// Панель уже, чем диалог, и содержимое вкладки (список из 6+ задач, лог
    /// флота) легко превышает её высоту. Без прокрутки хвост молча срезался и
    /// был недостижим. Сбрасывается в 0 при каждой смене вкладки — иначе
    /// прокрутка «залипает» на старом смещении и новая вкладка открывается
    /// не сначала.
    pub(crate) right_scroll: usize,
    /// Высота вьюпорта правой панели (заполняется при рендере) — шаг
    /// страничной прокрутки `PgUp`/`PgDn`.
    pub(crate) right_viewport: usize,
    /// Верхняя граница [`Self::right_scroll`], которую знает только рендер
    /// (длина содержимого минус вьюпорт). Записывается при рендере, чтобы
    /// `G` (прыжок к концу) ставил последнюю достижимую позицию, а не
    /// произвольное «очень большое» число, которое потом всё равно клампится.
    pub(crate) right_max: usize,
    /// Высота тела ask-модалки в строках (заполняется при рендере).
    ///
    /// Раньше шаг `PgUp`/`PgDn` был константой 10, не связанной
    /// с реальным окном: на высоком терминале страница была меньше экрана, на
    /// низком — больше, и «страница» перескакивала через весь список. Модалка
    /// не знала своей высоты (её считает рендер по размеру терминала), поэтому
    /// рендер записывает сюда число видимых строк, а [`Self::handle_ask_key`]
    /// берёт шаг от него. 0 — модалка ещё не рисовалась; шаг не ниже 1.
    pub(crate) ask_viewport: usize,
    /// Содержимое вкладок правой панели.
    pub(crate) panels: Panels,
    /// Постоянная заметка статус-бара (например, режим MCP) — в отличие от
    /// [`Toast`], не гаснет: это состояние, а не результат действия.
    status_extra: Option<String>,
    /// Транзиентный результат последнего действия (копирование, черновик).
    toast: Option<Toast>,
    /// Один слот спрятанного черновика: Esc в непустом вводе не выходит из
    /// приложения и не теряет набранное — текст ждёт здесь (H3/H5).
    draft: Option<String>,
    /// Строка теплицы гипотез от последней постановки цели: первая строка
    /// доменного хука `intent` (плагин hypothesis-router) — гипотезы,
    /// пересекающиеся с намерением и фактами проекта (`docs/hypotheses.md`).
    ///
    /// Считается ровно один раз — при постановке/замене цели в исполнителе
    /// слэш-команды, — а здесь только хранится: рендер обязан оставаться
    /// чистым и не запускать процессы на каждый кадр. `None` — плагина нет,
    /// хук промолчал или цель снята/сменилась подкомандой без намерения.
    intent_hint: Option<String>,
    /// Идёт фоновый ход (модель/команда) — ввод складывается в очередь.
    thinking: bool,
    /// Момент начала текущего хода (таймер «модель думает · N:SS» в строке
    /// состояния): длинный разгон модели перестаёт быть «офлайном».
    thinking_since: Option<Instant>,
    /// Момент первой дельты текущего хода (старт стрима — для скорости).
    stream_started: Option<Instant>,
    /// Байты видимого ответа текущего хода (сумма Delta).
    stream_answer_bytes: usize,
    /// Байты «мыслей» текущего хода (сумма ReasoningDelta).
    stream_think_bytes: usize,
    /// Живое состояние выполняющихся tool-вызовов: индекс блока → старт и
    /// хвост вывода. Запись умирает вместе с завершением вызова.
    pub(crate) tool_live: HashMap<usize, ToolLive>,
    /// Токен отмены текущего хода (Esc — прервать, Alt+Enter — прервать и
    /// вклинить набранное). None, пока ход не запущен или идёт слэш-команда.
    turn_cancel: Option<CancellationToken>,
    /// Очередь сообщений, набранных во время хода агента (FIFO;
    /// срочные — в начало через Alt+Enter или префикс «!!»; Alt+Enter также
    /// прерывает текущий ход, чтобы срочное стартовало немедленно).
    pub(crate) queue: VecDeque<String>,
    /// Кадр спиннера.
    spinner: usize,
    /// Счётчик тиков для live-обновления вкладки «Флот» (троттлинг).
    fleet_ticks: usize,
    /// Счётчик тиков для live-обновления вкладки «Субагенты» (троттлинг).
    subagent_ticks: usize,
    /// Команда, ожидающая результата (для привязки вывода к вкладкам).
    pending_slash: Option<String>,
    /// Флаг выхода из event loop.
    pub(crate) should_quit: bool,
    /// Имя модели для статус-бара.
    pub(crate) model_name: String,
    /// Грубая оценка токенов истории.
    history_tokens: usize,
    /// Эффективный бюджет контекста активной модели (0 — неизвестен).
    context_budget: usize,
    /// Область кнопки «▼ — к свежему ответу» (ставит рендер; None — у дна).
    pub(crate) jump_btn: Option<ratatui::layout::Rect>,
    /// Выделение мышью в окне диалога: якорь и текущий конец (экранные
    /// координаты). Снимается скроллом, новым контентом и кликом без драга.
    pub(crate) selection: Option<((u16, u16), (u16, u16))>,
    /// Внутренняя область диалога без рамки (ставит рендер) — маппинг
    /// экранных координат выделения в строки контента.
    pub(crate) dialog_inner: Option<ratatui::layout::Rect>,
    /// Plain-текст всех строк контента диалога после переноса по ширине
    /// (ставит рендер) — источник текста для копирования выделения.
    pub(crate) dialog_lines: Vec<String>,
    /// Индекс первой видимой строки контента (ставит рендер).
    pub(crate) dialog_skip: usize,
    /// Держатель системного буфера обмена (живёт всю сессию TUI: на X11
    /// данные буфера привязаны к процессу-владельцу селекции).
    clipboard: crate::clipboard::Clipboard,
    /// Тема оформления (палитра + глифы) — построена по [`Caps`].
    pub(crate) theme: Theme,
    /// Возможности терминала: ярус цвета, Unicode, мышь, минимальный размер.
    pub(crate) caps: Caps,
    /// Канал событий в event loop.
    msg_tx: Option<mpsc::Sender<AppMessage>>,
}

impl App {
    /// Собирает приложение: LLM-реестр, инструменты, MCP (мягко), сессию.
    /// Ошибка инициализации модели → экран [`Screen::Fatal`] (выход по `q`).
    pub(crate) async fn build(cfg: Arc<Config>, caps: Caps) -> Self {
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let registry = match LlmRegistry::from_config(&cfg) {
            Ok(r) => Arc::new(r),
            Err(e) => {
                let tool_ctx = ToolContext::new(cwd, cfg);
                return Self::fatal(tool_ctx, format!("{e}"), caps);
            }
        };
        let provider = registry.default();
        let model_name = format!("{}:{}", registry.default_name(), provider.model());

        let mut tools = tools::full_registry(&cfg);
        let mut status_extra = None;
        let mut mcp_manager = None;
        // MCP по умолчанию ленивый (connect_on_start=false): старт TUI не ждёт
        // npx/uvx-загрузок серверов. Инструменты MCP доступны модели только при
        // connect_on_start=true; слэш `/mcp` работает в любом режиме.
        if cfg.mcp.connect_on_start && cfg.mcp.servers_file.is_file() {
            match mcp::load_servers(&cfg.mcp.servers_file) {
                Ok(servers) if !servers.is_empty() => {
                    match McpManager::connect(&servers, cfg.mcp.timeout_secs).await {
                        Ok(manager) => {
                            let manager = Arc::new(manager);
                            mcp::register_mcp_tools(&mut tools, &manager).await;
                            mcp_manager = Some(manager);
                        }
                        Err(e) => status_extra = Some(format!("MCP недоступен: {e}")),
                    }
                }
                Ok(_) => {}
                Err(e) => status_extra = Some(format!("MCP: {e}")),
            }
        } else if cfg.mcp.servers_file.is_file() {
            status_extra = Some("MCP: ленивый режим (/mcp list — подключить)".into());
        }

        let tool_ctx = ToolContext::new(cwd, cfg.clone())
            .with_llm(registry)
            .with_provider(provider.clone())
            .with_subagents(crate::subagent::SubagentRegistry::new());
        // Мост интерактивных вопросов: инструмент propose_options → модалка.
        let (ask_tx, ask_rx) = mpsc::channel::<AskRequest>(8);
        // Канал живого прогресса инструментов: bash шлёт снапшоты хвоста
        // вывода долгой команды — TUI показывает, что происходит прямо сейчас.
        let (progress_tx, progress_rx) = mpsc::unbounded_channel::<ToolProgress>();
        let tool_ctx = tool_ctx.with_ask(ask_tx).with_progress(progress_tx);
        let system = system_prompt(&cfg);
        let session = AgentSession::new(cfg, provider, tools, tool_ctx.clone(), system);
        let mut app = Self::new(Some(session), tool_ctx, mcp_manager, status_extra, caps);
        app.ask_rx = Some(ask_rx);
        app.progress_rx = Some(progress_rx);
        app.model_name = model_name;
        app
    }

    /// Приложение в состоянии фатальной ошибки (показать экран и выйти).
    fn fatal(tool_ctx: ToolContext, error: String, caps: Caps) -> Self {
        let mut app = Self::new(None, tool_ctx, None, None, caps);
        app.screen = Screen::Fatal(error);
        app
    }

    /// Конструктор состояния по умолчанию (сразу чат, логотип — первый блок).
    fn new(
        session: Option<AgentSession>,
        tool_ctx: ToolContext,
        mcp: Option<Arc<McpManager>>,
        status_extra: Option<String>,
        caps: Caps,
    ) -> Self {
        let context_budget = session
            .as_ref()
            .map_or(0, AgentSession::effective_context_budget);
        let mut app = Self {
            screen: Screen::Chat,
            ask: None,
            viewer: None,
            right_visible: true,
            help: false,
            help_scroll: 0,
            ask_rx: None,
            progress_rx: None,
            session,
            tool_ctx,
            mcp,
            blocks: Vec::new(),
            assistant_open: false,
            turn_got_output: false,
            input: InputState::default(),
            scroll: 0,
            search: None,
            stick: true,
            viewport: 1,
            right_tab: RightTab::Mermaid,
            right_scroll: 0,
            right_viewport: 1,
            right_max: 0,
            ask_viewport: 0,
            panels: Panels::default(),
            status_extra,
            toast: None,
            draft: None,
            intent_hint: None,
            thinking: false,
            thinking_since: None,
            stream_started: None,
            stream_answer_bytes: 0,
            stream_think_bytes: 0,
            tool_live: HashMap::new(),
            turn_cancel: None,
            queue: VecDeque::new(),
            spinner: 0,
            fleet_ticks: 0,
            subagent_ticks: 0,
            pending_slash: None,
            should_quit: false,
            model_name: "—".into(),
            history_tokens: 0,
            context_budget,
            jump_btn: None,
            selection: None,
            dialog_inner: None,
            dialog_lines: Vec::new(),
            dialog_skip: 0,
            clipboard: crate::clipboard::Clipboard::new(),
            theme: Theme::for_caps(&caps),
            caps,
            msg_tx: None,
        };
        // Логотип — первый блок диалога: он уезжает вверх по мере переписки.
        // На экране Fatal блоки не рендерятся, так что класть его безопасно.
        app.push_block(ChatBlock::Logo);
        app
    }

    /// Подключает канал сообщений event loop и форвардеры: вопросы выбора
    /// (`propose_options` → модалка) и уведомления реестра фоновых задач
    /// (завершение субагента/`harness_run`/ralph → авто-ход с отчётом).
    /// Вызывается один раз из `tui::run`.
    pub(crate) fn attach(&mut self, tx: mpsc::Sender<AppMessage>) {
        self.msg_tx = Some(tx.clone());
        if let Some(mut rx) = self.ask_rx.take() {
            let fwd_tx = tx.clone();
            tokio::spawn(async move {
                while let Some(req) = rx.recv().await {
                    if fwd_tx.send(AppMessage::AskUser(req)).await.is_err() {
                        break;
                    }
                }
            });
        }
        if let Some(mut rx) = self.progress_rx.take() {
            let fwd_tx = tx.clone();
            tokio::spawn(async move {
                while let Some(p) = rx.recv().await {
                    if fwd_tx.send(AppMessage::ToolProgress(p)).await.is_err() {
                        break;
                    }
                }
            });
        }
        if let Some(registry) = self.tool_ctx.subagents.clone() {
            let (notice_tx, mut notice_rx) = mpsc::unbounded_channel::<BackgroundNotice>();
            registry.set_notifier(notice_tx);
            tokio::spawn(async move {
                while let Some(notice) = notice_rx.recv().await {
                    if tx
                        .send(AppMessage::BackgroundFinished(notice))
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
            });
        }
    }

    /// Отрисовка текущего экрана.
    pub(crate) fn render(&mut self, f: &mut Frame<'_>) {
        super::render::draw(f, self);
    }

    /// Нужны ли периодические тики (спиннер ожидания).
    /// Тики нужны, пока идёт ход ИЛИ работают фоновые субагенты
    /// (индикатор в статус-баре должен крутиться и обновляться).
    pub(crate) fn needs_tick(&self) -> bool {
        self.thinking
            || self.subagents_running() > 0
            || self.right_tab == RightTab::Fleet
            || self.right_tab == RightTab::Subagents
            || self.toast.is_some()
    }

    /// Число работающих фоновых задач (субагенты и ralph-циклы делят слоты).
    pub(crate) fn subagents_running(&self) -> usize {
        self.tool_ctx
            .subagents
            .as_ref()
            .map_or(0, super::super::subagent::SubagentRegistry::running)
    }

    /// Всего записей в реестре фоновых задач (бегущие + завершённые + упавшие).
    ///
    /// Нужно счётчикам вкладки и статус-бара: панель с одними завершёнными
    /// задачами — это содержимое, и она не должна выглядеть пустой только
    /// потому, что прямо сейчас ничего не бежит (tui-design-principles #5:
    /// состояние обязано быть видимым; «счётчик» у списка).
    pub(crate) fn subagents_total(&self) -> usize {
        self.tool_ctx
            .subagents
            .as_ref()
            .map_or(0, |r| r.list().len())
    }

    /// Сбрасывает статистику стрима хода (старт нового хода / завершение).
    fn reset_stream_stats(&mut self) {
        self.stream_started = None;
        self.stream_answer_bytes = 0;
        self.stream_think_bytes = 0;
    }

    /// Статистика стрима текущего хода: (байты ответа, байты мыслей, секунды
    /// стрима). None — стрим ещё не начался (разгон до первой дельты).
    pub(crate) fn stream_stats(&self) -> Option<(usize, usize, f64)> {
        self.stream_started.map(|t| {
            (
                self.stream_answer_bytes,
                self.stream_think_bytes,
                t.elapsed().as_secs_f64(),
            )
        })
    }

    /// Идёт ли фоновый ход (модель/команда).
    pub(crate) fn thinking(&self) -> bool {
        self.thinking
    }

    /// Секунды с начала текущего хода (None — ход не идёт).
    pub(crate) fn thinking_elapsed(&self) -> Option<u64> {
        self.thinking_since.map(|t| t.elapsed().as_secs())
    }

    /// Выполняющийся tool-вызов (последний Running-блок): имя, краткое
    /// действие и секунды работы — для строки состояния «что происходит».
    /// None — инструменты сейчас не выполняются.
    pub(crate) fn running_tool(&self) -> Option<(&str, &str, u64)> {
        self.blocks
            .iter()
            .enumerate()
            .rev()
            .find_map(|(i, b)| match b {
                ChatBlock::Tool {
                    name,
                    action,
                    state: ToolState::Running,
                    ..
                } => {
                    let secs = self
                        .tool_live
                        .get(&i)
                        .map_or(0, |l| l.started.elapsed().as_secs());
                    Some((name.as_str(), action.as_str(), secs))
                }
                _ => None,
            })
    }

    /// Есть ли что показать в строке состояния над вводом (ход, очередь,
    /// активный поиск или строка теплицы гипотез): если нет — строка не
    /// занимает место и раскладка не дёргается.
    ///
    /// Строка теплицы не считается при открытой ask-модалке: модалка — верхний
    /// слой и перекрывает строку, а её текст («… — поднять?») читался бы как
    /// живой вопрос, которого на экране нет (инвариант [`Self::ask`]).
    pub(crate) fn has_input_state(&self) -> bool {
        self.thinking
            || !self.queue.is_empty()
            || self.search.is_some()
            || (self.intent_hint.is_some() && self.ask.is_none())
    }

    /// Текущий кадр спиннера ожидания.
    pub(crate) fn spinner_frame(&self) -> usize {
        self.spinner
    }

    /// Кадр анимации длиной `len`: при `--no-animation` (и по ssh, где
    /// перерисовка каждого кадра стоит задержки) индикатор статичен —
    /// показываем первый кадр вместо бегущего.
    pub(crate) fn anim_frame(&self, len: usize) -> usize {
        if self.caps.animation && len > 0 {
            self.spinner_frame() % len
        } else {
            0
        }
    }

    /// Активная вкладка правой панели.
    pub(crate) fn right_tab(&self) -> RightTab {
        self.right_tab
    }

    /// Грубая оценка токенов истории.
    pub(crate) fn history_tokens(&self) -> usize {
        self.history_tokens
    }

    /// Эффективный бюджет контекста активной модели (0 — неизвестен).
    pub(crate) fn context_budget(&self) -> usize {
        self.context_budget
    }

    /// Доп. сообщение статус-бара (ошибки MCP и т.п.).
    pub(crate) fn status_extra(&self) -> Option<&str> {
        self.status_extra.as_deref()
    }

    /// Живой тост (результат последнего действия) для статус-бара.
    pub(crate) fn toast(&self) -> Option<&Toast> {
        self.toast.as_ref()
    }

    /// Спрятанный черновик ввода (Esc в непустом вводе).
    pub(crate) fn draft(&self) -> Option<&str> {
        self.draft.as_deref()
    }

    /// Строка теплицы гипотез от последней постановки цели — для строки
    /// состояния над вводом. Значение посчитано при постановке цели, не в
    /// рендере (см. поле [`Self::intent_hint`]).
    pub(crate) fn intent_hint(&self) -> Option<&str> {
        self.intent_hint.as_deref()
    }

    /// Показать тост: новый вытесняет старый (истории тостов не держим).
    fn set_toast(&mut self, toast: Toast) {
        let born = self.spinner;
        self.toast = Some(Toast { born, ..toast });
    }

    /// Тик таймера: кадр спиннера. Счётчик не ограничен — анимации берут
    /// модуль по своей длине на месте отрисовки (`PULSE`, `SPINNER`).
    pub(crate) fn tick(&mut self) {
        self.spinner += 1;
        if self.toast.as_ref().is_some_and(|t| !t.alive(self.spinner)) {
            self.toast = None;
        }
        // Live-обновление вкладки «Флот»: раз в ~2 c (16 тиков × 120 мс)
        // перечитываем последний журнал прогона (дёшево — один JSONL-файл).
        if self.right_tab == RightTab::Fleet {
            self.fleet_ticks += 1;
            if self.fleet_ticks >= 16 {
                self.fleet_ticks = 0;
                self.refresh_fleet();
            }
        } else {
            self.fleet_ticks = 0;
        }
        // Live-обновление вкладки «Субагенты»: раз в ~0.5 c (4 тика × 120 мс).
        // Интервал короче флотовского намеренно: реестр задач живёт в памяти
        // (Arc<Mutex<…>>), чтение — лок и клон вектора без диска, и список
        // должен отзываться на старт/финиш задачи почти сразу, чтобы вкладка
        // ощущалась живой, а не «через две секунды».
        if self.right_tab == RightTab::Subagents {
            self.subagent_ticks += 1;
            if self.subagent_ticks >= 4 {
                self.subagent_ticks = 0;
                self.refresh_subagents();
            }
        } else {
            self.subagent_ticks = 0;
        }
    }

    /// Перечитывает реестр фоновых задач сессии во вкладку «Субагенты».
    /// Реестр в памяти, поэтому состояния два: задачи есть — рендер
    /// [`crate::subagent::render_tasks`], задач нет — честная заглушка с
    /// подсказкой запуска (пустая строка запрещена: пустеющая вкладка не
    /// объясняет, как её наполнить).
    fn refresh_subagents(&mut self) {
        self.panels.subagents = match &self.tool_ctx.subagents {
            Some(registry) => {
                let tasks = registry.list();
                if tasks.is_empty() {
                    "Фоновых задач нет.\n\
                     запуск: инструмент subagent_run или /agents"
                        .to_string()
                } else {
                    crate::subagent::render_tasks(&tasks)
                }
            }
            None => "Реестр фоновых задач недоступен в этой сессии.\n\
                     запуск: инструмент subagent_run или /agents"
                .to_string(),
        };
    }

    /// Перечитывает последний журнал флота во вкладку «Флот»: панель всегда
    /// перезаписывается одним из трёх состояний — прочитанный журнал, «журналов
    /// нет» или «журнал не читается». Прежний кадр не сохраняется: иначе
    /// удаление `state_dir/fleet/*` оставляло бы на экране устаревший прогон
    /// вместе с пульсом heartbeat по mtime исчезнувшего файла.
    ///
    /// Сверху прочитанного журнала — живой заголовок прогресса (шкала узлов,
    /// таймер, пульс heartbeat по mtime журнала).
    fn refresh_fleet(&mut self) {
        let fleet_dir = self.tool_ctx.config.paths.state_dir.join("fleet");
        let path = match crate::fleet_run::latest_log(&fleet_dir) {
            Ok(path) => path,
            Err(_) => {
                self.panels.fleet = format!(
                    "Журналов прогонов нет: {}\n\
                     запуск: arch-ml fleet run --plan <файл-плана>",
                    fleet_dir.display()
                );
                return;
            }
        };
        let dashboard = match crate::fleet_run::render_log(&path) {
            Ok(s) => s,
            Err(e) => {
                self.panels.fleet = format!("Журнал {} не читается: {e}", path.display());
                return;
            }
        };
        let header = crate::fleet_run::read_progress(&path).ok().map(|p| {
            let age = std::fs::metadata(&path)
                .and_then(|m| m.modified())
                .ok()
                .and_then(|t| t.elapsed().ok())
                .map(|d| d.as_secs());
            crate::fleet_run::progress_header(
                &p,
                age,
                chrono::Local::now().naive_local(),
                (
                    self.theme.glyphs.gauge_full(),
                    self.theme.glyphs.gauge_empty(),
                ),
            )
        });
        self.panels.fleet = match header {
            Some(h) => format!("{h}\n\n{dashboard}"),
            None => dashboard,
        };
    }

    /// Graceful shutdown фоновых ресурсов (MCP-серверы).
    pub(crate) async fn shutdown(&self) {
        if let Some(manager) = &self.mcp {
            manager.shutdown().await;
        }
    }

    /// Обработка клавиши.
    pub(crate) fn handle_key(&mut self, key: KeyEvent) {
        // Ctrl-C — выход всегда.
        if key.modifiers.contains(KeyModifiers::CONTROL) && matches!(key.code, KeyCode::Char('c')) {
            self.should_quit = true;
            return;
        }
        // Модалка выбора (propose_options) перехватывает клавиши: агент
        // заблокирован в ожидании ответа, обычный ввод недоступен.
        if matches!(self.screen, Screen::Chat) && self.ask.is_some() {
            self.handle_ask_key(key);
            return;
        }
        // Просмотрщик вкладки (F4) перехватывает клавиши: навигация по арту,
        // Esc здесь — «назад», а не выход из приложения.
        if matches!(self.screen, Screen::Chat) && self.viewer.is_some() {
            self.handle_viewer_key(key);
            return;
        }
        // Оверлей справки (`?`) — верхний слой: фокус заперт внутри него,
        // `q`/`?`/Esc закрывают справку, а не приложение.
        if matches!(self.screen, Screen::Chat) && self.help {
            self.handle_help_key(key);
            return;
        }
        match &self.screen {
            Screen::Fatal(error) => match key.code {
                KeyCode::Char('q') | KeyCode::Esc | KeyCode::Enter => {
                    self.should_quit = true;
                }
                // Причину сбоя обычно несут в тикет — даём её скопировать.
                KeyCode::Char('c') => {
                    let error = error.clone();
                    self.status_extra = Some(match self.clipboard.copy(&error) {
                        Ok(_) => "скопировано в буфер".into(),
                        Err(e) => format!("не скопировалось: {e}"),
                    });
                }
                _ => {}
            },
            Screen::Chat => self.handle_chat_key(key),
        }
    }

    /// Событие мыши: колесо прокручивает диалог (3 строки за тик);
    /// в просмотрщике — его вертикаль, а с Shift — горизонтальная панорама.
    /// Клик левой кнопкой по «▼» в правом нижнем углу диалога — прыжок
    /// к свежему ответу. Работает и во время хода модели (как PgUp/PgDn).
    /// Драг левой кнопкой по диалогу — выделение текста; на отпускании
    /// выделенное копируется в буфер обмена (см. [`crate::clipboard`]).
    pub(crate) fn handle_mouse(&mut self, mouse: crossterm::event::MouseEvent) {
        const WHEEL_LINES: usize = 3;
        use crossterm::event::MouseButton as B;
        use crossterm::event::MouseEventKind as K;
        if !matches!(self.screen, Screen::Chat) {
            return;
        }
        if let Some(mut v) = self.viewer {
            let shift = mouse.modifiers.contains(KeyModifiers::SHIFT);
            match (mouse.kind, shift) {
                (K::ScrollUp, true) => v.scroll_x = v.scroll_x.saturating_sub(8),
                (K::ScrollDown, true) => v.scroll_x = v.scroll_x.saturating_add(8),
                (K::ScrollUp, false) => v.scroll_y = v.scroll_y.saturating_sub(WHEEL_LINES),
                (K::ScrollDown, false) => v.scroll_y = v.scroll_y.saturating_add(WHEEL_LINES),
                _ => {}
            }
            self.viewer = Some(v);
            return;
        }
        let pos = ratatui::layout::Position::new(mouse.column, mouse.row);
        match mouse.kind {
            // Клик по кнопке «▼» (справа внизу диалога) — к свежему ответу.
            K::Down(B::Left) if self.jump_btn.is_some_and(|r| r.contains(pos)) => {
                self.scroll_to_bottom();
            }
            // Начало выделения — только внутри окна диалога; клик вне его
            // снимает текущее выделение.
            K::Down(B::Left) if self.dialog_inner.is_some_and(|r| r.contains(pos)) => {
                self.selection = Some(((mouse.column, mouse.row), (mouse.column, mouse.row)));
            }
            K::Down(B::Left) => self.selection = None,
            K::Drag(B::Left) => {
                if let Some((_, end)) = &mut self.selection {
                    *end = (mouse.column, mouse.row);
                }
            }
            K::Up(B::Left) => self.finish_selection(),
            K::ScrollUp => self.scroll_by(WHEEL_LINES),
            K::ScrollDown => self.scroll_back(WHEEL_LINES),
            _ => {}
        }
    }

    /// Отпускание кнопки: точечный клик снимает выделение, драг —
    /// копирует выделенный текст в буфер обмена.
    fn finish_selection(&mut self) {
        let Some((anchor, end)) = self.selection else {
            return;
        };
        if anchor == end {
            self.selection = None;
            return;
        }
        let text = self.selected_text();
        if text.is_empty() {
            self.selection = None;
            return;
        }
        let n = text.chars().count();
        let toast = match self.clipboard.copy(&text) {
            Ok(mech) => Toast::ok(format!("скопировано {n} симв. ({mech})")),
            Err(e) => Toast::err(format!("{e}")),
        };
        self.set_toast(toast);
    }

    /// Строки текущего выделения в координатах контента диалога:
    /// (индекс строки в `dialog_lines`, начальная колонка, конечная колонка
    /// exclusive) — в display-колонках. Порядок — чтение (сверху вниз).
    pub(crate) fn selection_rows(&self) -> Vec<(usize, usize, usize)> {
        let mut rows = Vec::new();
        let Some(((ax, ay), (bx, by))) = self.selection else {
            return rows;
        };
        let Some(inner) = self.dialog_inner else {
            return rows;
        };
        // Нормализация в порядок чтения: драг мог идти снизу вверх/справа налево.
        let ((sx, sy), (ex, ey)) = if (ay, ax) <= (by, bx) {
            ((ax, ay), (bx, by))
        } else {
            ((bx, by), (ax, ay))
        };
        for row in sy..=ey {
            if row < inner.y || row >= inner.y + inner.height {
                continue;
            }
            let idx = self.dialog_skip + usize::from(row - inner.y);
            let line_w = self
                .dialog_lines
                .get(idx)
                .map_or(0, |l| UnicodeWidthStr::width(l.as_str()));
            let sc = usize::from(sx.saturating_sub(inner.x));
            let ec = usize::from(ex.saturating_sub(inner.x));
            let (c0, c1) = if sy == ey {
                (sc, ec + 1)
            } else if row == sy {
                (sc, line_w)
            } else if row == ey {
                (0, ec + 1)
            } else {
                (0, line_w)
            };
            rows.push((idx, c0, c1.min(line_w)));
        }
        rows
    }

    /// Текст текущего выделения (построчно, с переносами строк).
    fn selected_text(&self) -> String {
        let mut lines = Vec::new();
        for (idx, c0, c1) in self.selection_rows() {
            let Some(line) = self.dialog_lines.get(idx) else {
                continue;
            };
            lines.push(slice_by_cols(line, c0, c1));
        }
        lines.join("\n").trim().to_string()
    }

    fn handle_chat_key(&mut self, key: KeyEvent) {
        // Поиск по диалогу (Ctrl+F) перехватывает ввод, пока активен: буквы
        // идут в запрос, Enter/Alt+Enter — по совпадениям, Esc — закрыть.
        if self.search.is_some() {
            self.handle_search_key(key);
            return;
        }
        if key.code == KeyCode::Char('f') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.search = Some(DialogSearch {
                dirty: true,
                ..DialogSearch::default()
            });
            return;
        }
        if key.code == KeyCode::Esc {
            // Три роли Esc, в порядке приоритета: прервать ход → спрятать или
            // вернуть черновик → выйти. Выход по Esc возможен только при
            // пустом поле и пустом слоте черновика, иначе набранный текст
            // пропадал бы молча (H3 «контроль и свобода», H5, AP15).
            if self.thinking {
                self.interrupt_turn();
                return;
            }
            if !self.input.text().is_empty() {
                let text = self.input.submit();
                self.draft = Some(text);
                let hint = self.theme.glyphs.ok();
                self.set_toast(Toast::ok(format!("{hint} ввод отложен · Esc — вернуть")));
                return;
            }
            if let Some(text) = self.draft.take() {
                self.input.set_text(text);
                let hint = self.theme.glyphs.ok();
                self.set_toast(Toast::ok(format!("{hint} черновик возвращён")));
                return;
            }
            self.should_quit = true;
            return;
        }
        match key.code {
            // Прокрутка АКТИВНОЙ правой панели. Первичны конвенционные клавиши
            // (tui-keyboard-interaction: `PgUp`/`PgDn` — страница, `g`/`G` —
            // начало/конец); `Ctrl+U`/`Ctrl+D` — синонимы полстраницы, а
            // `Alt+PgUp`/`Alt+PgDn` ловятся теми же ветками `PageUp`/`PageDown`
            // (Alt+… доставляется ненадёжно — только как дубль). Панельные
            // ветки обязаны стоять ДО общих веток ниже, иначе клавиша уедет
            // в диалог или в текст.
            //
            // Когда панель скрыта (F5), те же PgUp/PgDn/Ctrl+U/Ctrl+D
            // прокручивают диалог: фокус не «теряется» при исчезновении
            // панели (tui-design-principles, Focus).
            KeyCode::PageUp if self.right_visible => self.right_scroll_up(self.right_page()),
            KeyCode::PageDown if self.right_visible => self.right_scroll_down(self.right_page()),
            KeyCode::Char('u')
                if self.right_visible && key.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                self.right_scroll_up(self.right_half_page());
            }
            KeyCode::Char('d')
                if self.right_visible && key.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                self.right_scroll_down(self.right_half_page());
            }
            // `g`/`G` — начало/конец списка панели. Гард пустого ввода тот же,
            // что у `q` и `?`: в режиме набора буква — это текст, а не команда
            // (tui-design-principles, Input modes). Цена — сообщение, которое
            // начинается с 'g' при пустом поле, отдаст первую букву навигации;
            // тот же компромисс уже принят для `q`/`?`. Строчная/заглавная
            // различаются по `SHIFT` (в части терминалов `G` приходит без
            // модификатора).
            KeyCode::Char('g')
                if self.right_visible && key.modifiers.is_empty() && self.input.text.is_empty() =>
            {
                self.right_scroll_to_start();
            }
            KeyCode::Char('G')
                if self.right_visible
                    && matches!(key.modifiers, KeyModifiers::SHIFT | KeyModifiers::NONE)
                    && self.input.text.is_empty() =>
            {
                self.right_scroll_to_end();
            }
            // Прокрутка диалога: панель скрыта или клавиша не имеет панельной
            // роли. `Ctrl+U`/`Ctrl+D` — те же синонимы полстраницы.
            KeyCode::Char('u') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.scroll_by(self.page());
            }
            KeyCode::Char('d') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.scroll_back(self.page());
            }
            KeyCode::PageUp => self.scroll_by(self.page()),
            KeyCode::PageDown => self.scroll_back(self.page()),
            // Прыжок на вкладку подразумевает желание её видеть: панель
            // показываем, даже если была скрыта F5 (иначе F1–F3/F6–F7 при
            // скрытой панели — «мёртвые» клавиши без видимого эффекта).
            KeyCode::F(1) => {
                self.right_visible = true;
                self.set_right_tab(RightTab::Mermaid);
            }
            KeyCode::F(2) => {
                self.right_visible = true;
                self.set_right_tab(RightTab::Rubric);
            }
            KeyCode::F(3) => {
                self.right_visible = true;
                self.set_right_tab(RightTab::Knowledge);
            }
            KeyCode::F(6) => {
                self.right_visible = true;
                // Дашборд — сразу, без ожидания ~2 с тикового опроса.
                self.set_right_tab(RightTab::Fleet);
            }
            KeyCode::F(7) => {
                self.right_visible = true;
                // Список — сразу, без ожидания ~0.5 с тикового опроса.
                self.set_right_tab(RightTab::Subagents);
            }
            KeyCode::F(4) => self.toggle_viewer(),
            // F5: скрыть/показать правую панель целиком (узкие терминалы).
            KeyCode::F(5) => self.right_visible = !self.right_visible,
            // Во время хода ввод НЕ блокируется: Enter — сообщение в очередь
            // (FIFO), Alt+Enter — срочно: прерывает текущий ход и вклинивает
            // набранное первым (иначе, пока агент ждёт harness_run/модель,
            // срочное лежало бы в очереди до конца хода).
            KeyCode::Enter if self.thinking && key.modifiers.contains(KeyModifiers::ALT) => {
                self.interrupt_and_inject();
            }
            // Перевод строки в поле ввода: Shift+Enter (kitty-протокол),
            // Alt+Enter (вне хода) или Ctrl+J (работает в любом терминале).
            KeyCode::Enter
                if key
                    .modifiers
                    .intersects(KeyModifiers::SHIFT | KeyModifiers::ALT) =>
            {
                self.input.insert_newline();
            }
            KeyCode::Enter if self.thinking => self.enqueue_typed(false),
            KeyCode::Enter => self.submit(),
            // Shift+Tab — предыдущая вкладка. Без kitty-протокола
            // (`KeyboardEnhancementFlags`, main.rs их не включает) crossterm
            // отдаёт Shift+Tab как `BackTab`, а не `Tab`+SHIFT, поэтому ветка
            // на Tab+SHIFT ниже — только совместимость; рабочая — эта.
            KeyCode::BackTab => {
                let tab = self.right_tab.prev();
                self.set_right_tab(tab);
            }
            // Tab сначала дополняет слэш-команду, дальше — следующая вкладка;
            // Shift+Tab — предыдущая (обе дороги к вкладкам, F1–F3/F6 — третья).
            KeyCode::Tab if key.modifiers.contains(KeyModifiers::SHIFT) => {
                let tab = self.right_tab.prev();
                self.set_right_tab(tab);
            }
            KeyCode::Tab => {
                if !self.input.complete_tab() {
                    let tab = self.right_tab.next();
                    self.set_right_tab(tab);
                }
            }
            // `?` при ПУСТОМ вводе — справка по клавишам (клавиши команд
            // агента — `/help`); при непустом — обычный вопросительный знак
            // в текст: вопросы («что такое GRPO?») вводятся свободно —
            // тот же гард, что у `q` (выход только при пустом вводе).
            KeyCode::Char('?') if self.input.text.is_empty() => {
                self.help = true;
                self.help_scroll = 0;
            }
            // Up/Down — по строкам многострочного ввода; на крайней строке —
            // навигация по истории.
            KeyCode::Up => {
                if !self.input.move_up_line() {
                    self.input.history_up();
                }
            }
            KeyCode::Down => {
                if !self.input.move_down_line() {
                    self.input.history_down();
                }
            }
            KeyCode::Left => self.input.move_left(),
            KeyCode::Right => self.input.move_right(),
            KeyCode::Home => self.input.move_home(),
            KeyCode::End => self.input.move_end(),
            KeyCode::Backspace => self.input.backspace(),
            KeyCode::Delete => self.input.delete(),
            KeyCode::Char('j') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.input.insert_newline();
            }
            KeyCode::Char('q') if self.input.text.is_empty() && key.modifiers.is_empty() => {
                self.should_quit = true;
            }
            KeyCode::Char(c)
                if key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT =>
            {
                self.input.insert_char(c);
            }
            _ => {}
        }
    }

    /// Enter во время хода: набранный текст — в очередь (`front` — в начало,
    /// срочное). Префикс «!!» — срочно даже без Alt (терминали без Alt+Enter).
    fn enqueue_typed(&mut self, front: bool) {
        let input = self.input.submit();
        if input.is_empty() {
            return;
        }
        let (front, input) = match input.strip_prefix("!!") {
            Some(rest) => (true, rest.trim_start().to_string()),
            None => (front, input),
        };
        if input.is_empty() {
            return;
        }
        if self.queue.len() >= MAX_QUEUE {
            self.push_block(ChatBlock::Error(format!(
                "очередь полна ({MAX_QUEUE}) — дождитесь завершения хода или прервите его (Esc)"
            )));
            return;
        }
        if front {
            self.queue.push_front(input);
        } else {
            self.queue.push_back(input);
        }
    }

    /// Alt+Enter во время хода — «срочно»: набранное (если есть) в начало
    /// очереди и немедленное прерывание текущего хода. По возврате сессии
    /// очередь стартует сама (см. [`App::maybe_start_queued`]).
    fn interrupt_and_inject(&mut self) {
        self.enqueue_typed(true);
        self.interrupt_turn();
    }

    /// Прерывает текущий ход (Esc/Alt+Enter во время хода): агентный цикл
    /// обрывает LLM-запрос или вызов инструмента (включая ожидание
    /// `harness_run`), история сессии остаётся консистентной — висячие
    /// tool-вызовы получают результат «прервано» (см.
    /// [`AgentSession::set_cancel_token`]). Без активного хода — тихий no-op.
    fn interrupt_turn(&mut self) {
        if let Some(token) = &self.turn_cancel {
            token.cancel();
        }
    }

    /// Открывает модалку выбора по запросу инструмента `propose_options`.
    /// Если предыдущий вопрос ещё висит (нештатно), он отклоняется — инструмент
    /// не должен ждать вечно.
    fn open_ask(&mut self, req: AskRequest) {
        if let Some(prev) = self.ask.take() {
            answer(prev.reply, String::new());
        }
        let selected = req
            .recommended
            .as_deref()
            .and_then(|rec| req.options.iter().position(|o| o.label == rec))
            .unwrap_or(0);
        self.ask = Some(AskState {
            question: req.question,
            options: req.options,
            recommended: req.recommended,
            selected,
            reply: Some(req.reply),
            kind: AskKind::Tool,
        });
        self.scroll_to_bottom();
    }

    /// Открывает пикер моделей по `/model` без аргумента: варианты — ключи
    /// `[models]` из конфига (описание — id модели и доступность `/think`),
    /// ★ и курсор — текущая модель. Выбор сводится к обычному `/model <name>`.
    pub(crate) fn open_model_picker(&mut self) {
        let current = self
            .session
            .as_ref()
            .map(|s| s.provider().name().to_string())
            .unwrap_or_default();
        let options: Vec<crate::tool::AskOption> = self
            .tool_ctx
            .config
            .models
            .iter()
            .map(|(name, mc)| {
                let think = if mc.thinking_on.is_some() {
                    " · ризонинг: /think"
                } else {
                    ""
                };
                crate::tool::AskOption {
                    label: name.clone(),
                    description: format!("{}{think}", mc.model),
                }
            })
            .collect();
        if options.is_empty() {
            return;
        }
        let selected = options.iter().position(|o| o.label == current).unwrap_or(0);
        self.ask = Some(AskState {
            question: format!("Модель для этой сессии (сейчас: {current}):"),
            options,
            recommended: (!current.is_empty()).then_some(current),
            selected,
            reply: None,
            kind: AskKind::ModelPicker,
        });
        self.scroll_to_bottom();
    }

    /// Открывает пикер сессий по `/resume` без аргумента: варианты — журналы
    /// из `paths.sessions_dir` (новые первыми, журнал текущей сессии скрыт;
    /// описание — дата, число сообщений, первая реплика). Выбор сводится
    /// к обычному `/resume <имя-файла>`.
    pub(crate) fn open_session_picker(&mut self) {
        let current = self
            .session
            .as_ref()
            .and_then(|s| s.log_path().map(std::path::Path::to_path_buf));
        let logs = crate::agent::list_session_logs(&self.tool_ctx.config.paths.sessions_dir);
        let options: Vec<crate::tool::AskOption> = logs
            .into_iter()
            .filter(|l| Some(&l.path) != current.as_ref())
            .take(12)
            .map(|l| {
                let name = l
                    .path
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default();
                crate::tool::AskOption {
                    label: name,
                    description: format!(
                        "{} · сообщений: {} · {}",
                        l.modified, l.messages, l.first_user_line
                    ),
                }
            })
            .collect();
        if options.is_empty() {
            self.push_block(ChatBlock::System {
                command: "resume".into(),
                text: "прошлых сессий нет (журналы — в paths.sessions_dir)".into(),
            });
            return;
        }
        self.ask = Some(AskState {
            question: "Сессия для восстановления:".into(),
            options,
            recommended: None,
            selected: 0,
            reply: None,
            kind: AskKind::SessionPicker,
        });
        self.scroll_to_bottom();
    }

    /// Клавиши модалки выбора: навигация, подтверждение, отказ.
    ///
    /// Цифровой быстрый выбор — только `1..=9` и ОСОЗНАННО без клавиш для
    /// 10+: многоразрядный номер («10») конкурировал бы и с текстовым вводом
    /// (мышление уже блокирует печать — но пришлось бы вводить режим набора
    /// номера), и с одиночной цифрой, выбирающей мгновенно (непонятно, ждать
    /// ли вторую цифру). Хвост длинного списка (пикер `/resume` отдаёт до 12
    /// пунктов) достижим `↓`/`PgDn`/`End`, обратно — `↑`/`PgUp`/`Home`.
    ///
    /// Цифра вне списка (напр. «7» при трёх пунктах) — не «мёртвая» клавиша:
    /// ответ не отправляется и модалка не закрывается, но пользователь
    /// получает видимый отклик в статус-баре (тост). `0` — не номер пункта
    /// (нумерация с единицы), это обычный no-op, как любая буква: печать в
    /// модалке заблокирована.
    fn handle_ask_key(&mut self, key: KeyEvent) {
        // Шаг «страницы» — от реальной высоты окна, которую рендер записал в
        // `ask_viewport`: вариант занимает минимум две строки (метка +
        // описание), поэтому половина видимого тела ≈ один экран вариантов.
        // Константа здесь врала: на 40-строчном терминале страница в 10
        // пунктов была меньше экрана и листала «по чуть-чуть», а на низком —
        // перескакивала весь список. Ниже 1 шаг не опускаем: `PgDn` обязан
        // сдвинуть курсор даже в терминале в три строки, иначе клавиша молчит.
        let page = (self.ask_viewport / 2).max(1);
        let Some(ask) = self.ask.as_mut() else {
            return;
        };
        let count = ask.options.len();
        match key.code {
            KeyCode::Up | KeyCode::Char('k') => {
                ask.selected = ask.selected.saturating_sub(1);
            }
            KeyCode::Down | KeyCode::Char('j') => {
                ask.selected = (ask.selected + 1).min(count.saturating_sub(1));
            }
            KeyCode::PageUp => {
                ask.selected = ask.selected.saturating_sub(page);
            }
            KeyCode::PageDown => {
                ask.selected = (ask.selected + page).min(count.saturating_sub(1));
            }
            KeyCode::Home => ask.selected = 0,
            KeyCode::End => ask.selected = count.saturating_sub(1),
            KeyCode::Enter => {
                let label = ask.options.get(ask.selected).map(|o| o.label.clone());
                self.answer_ask(label);
            }
            KeyCode::Char(c) if ('1'..='9').contains(&c) => {
                let idx = (c.to_digit(10).unwrap_or(1) - 1) as usize;
                if idx < count {
                    let label = ask.options[idx].label.clone();
                    self.answer_ask(Some(label));
                } else {
                    // Пункта нет: ответ не уходит, модалка и курсор не меняются,
                    // но клавиша не молчит — сообщаем, сколько пунктов доступно.
                    // Уровень `err` (✗/красный), а не `ok` (✓/зелёный): текст —
                    // отрицание, а знак обязан дублировать смысл (правило
                    // «цвет не в одиночку»). Ошибка ждёт следующего действия —
                    // пользователь, занятый модалкой, не пропустит отклик.
                    self.set_toast(Toast::err(format!(
                        "пункта {} нет — доступно {count}",
                        idx + 1
                    )));
                }
            }
            // «0» — не номер пункта (нумерация с 1). Раньше клавиша молчала,
            // хотя соседние «1»..«9» вне диапазона честно отвечают тостом:
            // одинаковые по смыслу нажатия вели себя по-разному. Отвечаем тем
            // же текстом и уровнем, что и для отсутствующего пункта.
            KeyCode::Char('0') => {
                self.set_toast(Toast::err(format!("пункта 0 нет — доступно {count}")));
            }
            // Отказ — пустой ответ: инструмент превратит его в «реши сам».
            KeyCode::Esc => self.answer_ask(None),
            _ => {}
        }
    }

    /// Отправляет ответ и закрывает модалку. `None` — отказ (Esc): инструменту
    /// уходит пустой ответ («реши сам»), пикер просто закрывается.
    fn answer_ask(&mut self, label: Option<String>) {
        if let Some(ask) = self.ask.take() {
            match ask.kind {
                AskKind::Tool => answer(ask.reply, label.unwrap_or_default()),
                AskKind::ModelPicker => {
                    if let Some(label) = label {
                        self.start_slash(format!("/model {label}"));
                    }
                }
                AskKind::SessionPicker => {
                    if let Some(label) = label {
                        self.start_slash(format!("/resume {label}"));
                    }
                }
            }
        }
        // Модалка могла задержать очередь — запускаем, если свободны.
        self.maybe_start_queued();
    }

    /// F4: открыть/закрыть полноэкранный просмотр активной вкладки.
    /// При открытии скролл сбрасывается (начинаем с верхнего левого угла).
    fn toggle_viewer(&mut self) {
        self.viewer = if self.viewer.is_some() {
            None
        } else {
            Some(ViewerState::default())
        };
    }

    /// Клавиши просмотрщика вкладки: прокрутка (в т.ч. горизонтальная
    /// панорама широкого арта), смена вкладки, закрытие.
    fn handle_viewer_key(&mut self, key: KeyEvent) {
        let Some(mut v) = self.viewer else {
            return;
        };
        match key.code {
            KeyCode::Esc | KeyCode::Char('q') | KeyCode::F(4) => {
                self.viewer = None;
                return;
            }
            KeyCode::Up => v.scroll_y = v.scroll_y.saturating_sub(1),
            KeyCode::Down => v.scroll_y = v.scroll_y.saturating_add(1),
            KeyCode::Left => v.scroll_x = v.scroll_x.saturating_sub(8),
            KeyCode::Right => v.scroll_x = v.scroll_x.saturating_add(8),
            KeyCode::PageUp => v.scroll_y = v.scroll_y.saturating_sub(12),
            KeyCode::PageDown => v.scroll_y = v.scroll_y.saturating_add(12),
            KeyCode::Home => {
                v.scroll_x = 0;
                v.scroll_y = 0;
            }
            // Вкладки переключаются и в просмотрщике (скролл — новой вкладки).
            KeyCode::F(1) => {
                self.set_right_tab(RightTab::Mermaid);
                v = ViewerState::default();
            }
            KeyCode::F(2) => {
                self.set_right_tab(RightTab::Rubric);
                v = ViewerState::default();
            }
            KeyCode::F(3) => {
                self.set_right_tab(RightTab::Knowledge);
                v = ViewerState::default();
            }
            _ => {}
        }
        self.viewer = Some(v);
    }

    /// Клавиши оверлея справки (`?`): прокрутка содержимого и закрытие.
    /// Фокус заперт — остальные клавиши не доходят до чата (конвенция E12).
    fn handle_help_key(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Char('?' | 'q') | KeyCode::Esc => {
                self.help = false;
                self.help_scroll = 0;
            }
            KeyCode::Up => self.help_scroll = self.help_scroll.saturating_sub(1),
            KeyCode::Down => self.help_scroll = self.help_scroll.saturating_add(1),
            KeyCode::PageUp => self.help_scroll = self.help_scroll.saturating_sub(10),
            KeyCode::PageDown => self.help_scroll = self.help_scroll.saturating_add(10),
            KeyCode::Home => self.help_scroll = 0,
            KeyCode::End => self.help_scroll = usize::MAX,
            _ => {}
        }
    }

    /// Сдвиг прокрутки справки (рендер клампит его по высоте окна).
    pub(crate) fn help_scroll(&self) -> usize {
        self.help_scroll
    }

    /// Сброс прокрутки справки по фактической высоте содержимого.
    pub(crate) fn set_help_scroll(&mut self, v: usize) {
        self.help_scroll = v;
    }

    /// Контекст клавиш для строки подсказок: его печатает статус-бар.
    ///
    /// Модалка выбора — верхний слой независимо от того, занят чат или нет:
    /// при открытой модалке статус-бар обязан обещать клавиши МОДАЛКИ
    /// (`Enter` — выбрать пункт, `Esc` — отклонить), а не чата. Иначе на
    /// занятом чате подсказка врала бы: «Enter — в очередь» и «Esc — прервать»
    /// обещали бы то, чего `handle_ask_key` не делает (модалка перехватывает
    /// клавиши раньше чата — см. `handle_key`).
    pub(crate) fn hint_ctx(&self) -> super::keymap::Ctx {
        if self.ask.is_some() && matches!(self.screen, Screen::Chat) {
            super::keymap::Ctx::Ask
        } else if self.help {
            super::keymap::Ctx::Help
        } else {
            self.chat_ctx()
        }
    }

    /// Контекст, который документирует оверлей справки `?`.
    ///
    /// Справка рассказывает про нижний относительно неё слой: при открытой
    /// ask-модалке — про клавиши модалки (она блокирующая и перехватывает
    /// ввод), иначе про сам чат. Подменять это на [`Self::chat_ctx`] нельзя:
    /// оверлей обещал бы клавиши экрана, который фокус не получает.
    pub(crate) fn help_ctx(&self) -> super::keymap::Ctx {
        if self.ask.is_some() && matches!(self.screen, Screen::Chat) {
            super::keymap::Ctx::Ask
        } else {
            self.chat_ctx()
        }
    }

    /// Контекст самого чата: справка `?` документирует именно его, а не себя
    /// (иначе оверлей рассказывал бы, как закрыть оверлей). Активный поиск —
    /// собственный контекст (Enter/Alt+Enter иначе значили бы другое).
    pub(crate) fn chat_ctx(&self) -> super::keymap::Ctx {
        if self.search.is_some() {
            super::keymap::Ctx::Search
        } else if self.thinking {
            super::keymap::Ctx::ChatBusy
        } else {
            super::keymap::Ctx::ChatIdle
        }
    }

    /// Enter: отправка ввода — слэш-команде или модели; во время хода —
    /// постановка в очередь (см. [`App::enqueue_typed`]).
    fn submit(&mut self) {
        let input = self.input.submit();
        if input.is_empty() {
            return;
        }
        if self.thinking {
            // Гонка отрисовки: ход уже начался — в конец очереди.
            if self.queue.len() < MAX_QUEUE {
                self.queue.push_back(input);
            }
            return;
        }
        self.submit_text(input);
    }

    /// Немедленный запуск сообщения: блок пользователя + ход/слэш/export.
    fn submit_text(&mut self, input: String) {
        self.scroll_to_bottom();
        self.push_block(ChatBlock::User(input.clone()));
        // `/export` перехватывается здесь: блоки диалога видны только TUI,
        // слэш-слой работает с сессией и про экран не знает.
        if input.trim_start().starts_with("/export") {
            self.do_export(&input);
        } else if slash::is_slash(&input) {
            self.start_slash(input);
        } else {
            self.start_turn(input);
        }
    }

    /// Запуск первого сообщения из очереди (после завершения хода/команды).
    /// Ждёт, если открыта модалка выбора (ответ пользователя важнее).
    fn maybe_start_queued(&mut self) {
        if self.thinking || self.ask.is_some() || self.viewer.is_some() {
            return;
        }
        if let Some(next) = self.queue.pop_front() {
            self.submit_text(next);
        }
    }

    /// Завершение фоновой задачи: системный блок в диалог + ход с отчётом
    /// через общую очередь. Без этого доставка была pull-only: агент спал
    /// до следующего сообщения пользователя и вердикты «отчитавшихся»
    /// контрагентов до модели не доходили (индикатор статус-бара просто
    /// гас). Если очередь полна — ход не ставится, но системный блок
    /// остаётся, а отчёт доступен pull-путём (`subagent_result`).
    fn on_background_finished(&mut self, notice: &BackgroundNotice) {
        let report = self
            .tool_ctx
            .subagents
            .as_ref()
            .and_then(|r| r.get(&notice.id))
            .map(|t| t.report)
            .unwrap_or_default();
        self.push_block(ChatBlock::System {
            command: "фон".into(),
            text: format!(
                "фоновая задача {} завершена ({}) — отчёт передан агенту",
                notice.id,
                notice.status.as_str()
            ),
        });
        let input = if report.trim().is_empty() {
            format!(
                "[фон] Задача {} завершилась со статусом {}. Отчёт пуст.",
                notice.id,
                notice.status.as_str()
            )
        } else {
            format!(
                "[фон] Задача {} завершилась со статусом {}.\n\nОтчёт задачи:\n{report}",
                notice.id,
                notice.status.as_str()
            )
        };
        if self.queue.len() < MAX_QUEUE {
            self.queue.push_back(input);
            self.maybe_start_queued();
        }
    }

    /// `/export <word|excel> [path]`: экран диалога в .docx/.xlsx.
    fn do_export(&mut self, input: &str) {
        let mut parts = input.split_whitespace();
        let _ = parts.next(); // сама команда
        let usage = "использование: /export <word|excel> [путь] — экран в .docx/.xlsx";
        let Some(fmt_arg) = parts.next() else {
            self.push_block(ChatBlock::System {
                command: "export".into(),
                text: usage.into(),
            });
            return;
        };
        let Some(format) = crate::export::ExportFormat::parse(fmt_arg) else {
            self.push_block(ChatBlock::System {
                command: "export".into(),
                text: format!("неизвестный формат «{fmt_arg}»; {usage}"),
            });
            return;
        };
        let path = parts.next().map_or_else(
            || {
                let ts = chrono::Local::now().format("%Y%m%d-%H%M%S");
                self.tool_ctx
                    .cwd
                    .join(format!("arch-screen-{ts}.{}", format.extension()))
            },
            |p| self.tool_ctx.resolve(p),
        );
        match crate::export::export_blocks(&self.blocks, format, &path) {
            Ok(n) => {
                self.push_block(ChatBlock::System {
                    command: "export".into(),
                    text: format!("экран экспортирован ({n} строк) → {}", path.display()),
                });
            }
            Err(e) => {
                self.push_block(ChatBlock::Error(format!("экспорт не удался: {e}")));
            }
        }
    }

    /// Запускает ход агента в фоновой задаче (сессия переезжает в задачу).
    fn start_turn(&mut self, input: String) {
        let Some(tx) = self.msg_tx.clone() else {
            self.push_block(ChatBlock::Error(
                "внутренняя ошибка: канал событий не подключён".into(),
            ));
            return;
        };
        let Some(session) = self.session.take() else {
            self.push_block(ChatBlock::Error("сессия занята — дождитесь ответа".into()));
            return;
        };
        self.thinking = true;
        self.thinking_since = Some(Instant::now());
        self.reset_stream_stats();
        self.turn_got_output = false;
        // Токен отмены хода: Esc/Alt+Enter прерывают LLM-запрос или вызов
        // инструмента (см. AgentSession::set_cancel_token).
        let cancel = CancellationToken::new();
        let mut session = session;
        session.set_cancel_token(Some(cancel.clone()));
        self.turn_cancel = Some(cancel);
        // Fire-and-forget: JoinHandle не храним — результат и ошибки приходят
        // сообщением TurnFinished; при выходе runtime отменит задачу.
        tokio::spawn(async move {
            let (ev_tx, mut ev_rx) = mpsc::channel::<AgentEvent>(AGENT_EVENTS_CAP);
            let fwd_tx = tx.clone();
            let forwarder = tokio::spawn(async move {
                while let Some(ev) = ev_rx.recv().await {
                    if fwd_tx.send(AppMessage::AgentEvent(ev)).await.is_err() {
                        break;
                    }
                }
            });
            let mut session = session;
            let result = session.send(&input, Some(ev_tx)).await;
            // ev_tx дропнут вместе с future send — forwarder дочитает буфер
            // и завершится сам.
            let _ = forwarder.await;
            let _ = tx.send(AppMessage::TurnFinished { session, result }).await;
        });
    }

    /// Запускает слэш-команду в фоновой задаче (сессия переезжает в задачу).
    fn start_slash(&mut self, input: String) {
        let Some(tx) = self.msg_tx.clone() else {
            self.push_block(ChatBlock::Error(
                "внутренняя ошибка: канал событий не подключён".into(),
            ));
            return;
        };
        let Some(session) = self.session.take() else {
            self.push_block(ChatBlock::Error("сессия занята — дождитесь ответа".into()));
            return;
        };
        self.thinking = true;
        self.thinking_since = Some(Instant::now());
        self.reset_stream_stats();
        self.pending_slash = Some(input.clone());
        let ctx = self.tool_ctx.clone();
        // Fire-and-forget: исход приходит сообщением SlashFinished.
        tokio::spawn(async move {
            let mut session = session;
            let result = slash::execute(&input, &mut session, &ctx).await;
            let _ = tx.send(AppMessage::SlashFinished { session, result }).await;
        });
    }

    /// Обработка сообщения от фоновых задач.
    pub(crate) fn handle_message(&mut self, msg: AppMessage) {
        match msg {
            AppMessage::AgentEvent(ev) => self.handle_agent_event(ev),
            AppMessage::AskUser(req) => self.open_ask(req),
            AppMessage::ToolProgress(p) => self.handle_tool_progress(p),
            AppMessage::TurnFinished { session, result } => {
                self.thinking = false;
                self.thinking_since = None;
                self.reset_stream_stats();
                self.turn_cancel = None;
                self.assistant_open = false;
                self.on_session_back(session);
                match result {
                    Ok(text) => {
                        // Без стриминга дельт не было — показываем финальный текст.
                        if !self.turn_got_output && !text.trim().is_empty() {
                            self.push_block(ChatBlock::Assistant(text));
                        }
                    }
                    Err(e) => {
                        self.push_block(ChatBlock::Error(format!("ход завершился ошибкой: {e}")));
                    }
                }
                // Очередь: следующее набранное во время хода сообщение.
                self.maybe_start_queued();
            }
            AppMessage::SlashFinished { session, result } => {
                self.thinking = false;
                self.thinking_since = None;
                self.reset_stream_stats();
                self.on_session_back(session);
                let command = self.pending_slash.take().unwrap_or_default();
                match result {
                    Ok(slash::SlashOutcome::Handled(text)) => {
                        // `/goal status|pause|cancel|next` не несёт нового
                        // намерения: строка теплицы от прошлой постановки
                        // снимается, иначе она висела бы как обещание про
                        // уже снятую цель. `replace` идёт веткой GoalStarted и
                        // ставит свежую строку.
                        if command.trim_start().starts_with("/goal") {
                            self.intent_hint = None;
                        }
                        self.route_slash_output(&command, &text);
                        self.push_block(ChatBlock::System { command, text });
                    }
                    Ok(slash::SlashOutcome::PickModel) => self.open_model_picker(),
                    Ok(slash::SlashOutcome::PickSession) => self.open_session_picker(),
                    Ok(slash::SlashOutcome::NewSession) => {
                        // /new: чистый лист — блоки, вкладки и скролл сброшены;
                        // сессия уже ротирована исполнителем (новый журнал).
                        // Свежий лог, как и при входе в чат, открывает логотип.
                        self.blocks.clear();
                        self.tool_live.clear();
                        self.panels = Panels::default();
                        // Строка теплицы про прежнее намерение: в новой сессии
                        // она уже не про эту задачу.
                        self.intent_hint = None;
                        self.scroll = 0;
                        self.stick = true;
                        self.push_block(ChatBlock::Logo);
                        self.push_block(ChatBlock::System {
                            command,
                            text: "новая сессия: история и панели очищены, \
                                   журнал начат заново; прошлые сессии — /sessions"
                                .into(),
                        });
                    }
                    Ok(slash::SlashOutcome::GoalStarted { text, intent_hint }) => {
                        // Строка теплицы приходит из исполнителя команды (там
                        // доменной хук вызван ровно один раз), а не считается
                        // в рендере: рендер не должен запускать процессы.
                        self.intent_hint = intent_hint;
                        self.push_block(ChatBlock::System { command, text });
                        // Первый терн goal-петли: цель уходит в контекст
                        // синтетическим user-сообщением (continuation), дальше
                        // петля продолжает сама до критерия/блокера/паузы
                        // (`aiml/notes/goal-mode.md` §2).
                        if let Some(kickoff) = self
                            .session
                            .as_ref()
                            .and_then(AgentSession::goal_kickoff_message)
                        {
                            self.start_turn(kickoff);
                        }
                    }
                    Ok(slash::SlashOutcome::Quit) => self.should_quit = true,
                    Ok(slash::SlashOutcome::Unknown(cmd)) => {
                        self.push_block(ChatBlock::Error(format!(
                            "неизвестная команда: {cmd} (/help — список)"
                        )));
                    }
                    Ok(slash::SlashOutcome::NotSlash) => {
                        self.push_block(ChatBlock::Error(
                            "внутренняя ошибка: NotSlash дошёл до обработчика слэш-команд".into(),
                        ));
                    }
                    Err(e) => self.push_block(ChatBlock::Error(format!("команда не удалась: {e}"))),
                }
                // Очередь: следующее набранное, пока шла команда.
                self.maybe_start_queued();
            }
            AppMessage::BackgroundFinished(notice) => self.on_background_finished(&notice),
        }
    }

    /// Обработка события агентного цикла.
    fn handle_agent_event(&mut self, ev: AgentEvent) {
        match ev {
            AgentEvent::Delta(delta) => {
                self.turn_got_output = true;
                self.stream_started.get_or_insert_with(Instant::now);
                self.stream_answer_bytes += delta.len();
                if !self.assistant_open {
                    self.push_block(ChatBlock::Assistant(String::new()));
                    self.assistant_open = true;
                }
                if let Some(ChatBlock::Assistant(text)) = self.blocks.last_mut() {
                    text.push_str(&delta);
                }
            }
            AgentEvent::ReasoningDelta(delta) => {
                self.turn_got_output = true;
                self.stream_started.get_or_insert_with(Instant::now);
                self.stream_think_bytes += delta.len();
                // «Мысли» идут до видимого ответа: копим в отдельном
                // приглушённом блоке; новый блок — если последний уже не
                // «мысли» (между витками инструментов мысли новые).
                if !matches!(self.blocks.last(), Some(ChatBlock::Thinking(_))) {
                    self.push_block(ChatBlock::Thinking(String::new()));
                }
                if let Some(ChatBlock::Thinking(text)) = self.blocks.last_mut() {
                    text.push_str(&delta);
                }
            }
            AgentEvent::ToolStart { name, args } => {
                self.assistant_open = false;
                self.push_block(ChatBlock::Tool {
                    action: action_desc(&name, &args),
                    name,
                    state: ToolState::Running,
                    summary: String::new(),
                });
                self.tool_live.insert(
                    self.blocks.len() - 1,
                    ToolLive {
                        started: Instant::now(),
                        tail: String::new(),
                    },
                );
            }
            AgentEvent::ToolEnd {
                name,
                is_error,
                summary,
                content,
            } => {
                self.finish_tool(&name, is_error, &summary);
                self.route_tool_output(&name, &content);
            }
            AgentEvent::Note(text) => {
                self.assistant_open = false;
                self.push_block(ChatBlock::System {
                    command: "детектор".into(),
                    text,
                });
            }
            AgentEvent::TurnDone => self.assistant_open = false,
            // Живое обновление индикатора контекста по ходу длинного хода.
            AgentEvent::ContextUsage(used) => self.history_tokens = used,
        }
        if self.stick {
            self.scroll = 0;
        }
    }

    /// Завершает последний активный tool-блок (по имени, иначе любой Running).
    fn finish_tool(&mut self, name: &str, is_error: bool, summary: &str) {
        let state = if is_error {
            ToolState::Error
        } else {
            ToolState::Ok
        };
        let idx = self
            .blocks
            .iter()
            .rposition(|b| {
                matches!(
                    b,
                    ChatBlock::Tool {
                        name: n,
                        state: ToolState::Running,
                        ..
                    } if n == name
                )
            })
            .or_else(|| {
                self.blocks.iter().rposition(|b| {
                    matches!(
                        b,
                        ChatBlock::Tool {
                            state: ToolState::Running,
                            ..
                        }
                    )
                })
            });
        match idx {
            Some(i) => {
                self.tool_live.remove(&i);
                if let Some(ChatBlock::Tool {
                    state: s,
                    summary: sum,
                    ..
                }) = self.blocks.get_mut(i)
                {
                    *s = state;
                    *sum = summary.to_string();
                }
            }
            None => self.push_block(ChatBlock::Tool {
                name: name.to_string(),
                action: String::new(),
                state,
                summary: summary.to_string(),
            }),
        }
    }

    /// Привязывает вывод инструмента к вкладке правой панели.
    fn route_tool_output(&mut self, name: &str, summary: &str) {
        if name.contains("mermaid") {
            self.panels.mermaid = summary.to_string();
        } else if name.contains("rubric") {
            self.panels.rubric = summary.to_string();
        } else if name.starts_with("kb") || name.starts_with("web") {
            self.panels.knowledge = summary.to_string();
        }
    }

    /// Живой прогресс инструмента: обновляет хвост вывода последнего
    /// выполняющегося блока с тем же именем. Нет подходящего блока — снапшот
    /// пропадает без последствий (это декорация, а не данные для модели).
    fn handle_tool_progress(&mut self, p: ToolProgress) {
        let idx = self.blocks.iter().rposition(|b| {
            matches!(
                b,
                ChatBlock::Tool {
                    name,
                    state: ToolState::Running,
                    ..
                } if *name == p.name
            )
        });
        if let Some(i) = idx {
            self.tool_live
                .entry(i)
                .or_insert_with(|| ToolLive {
                    started: Instant::now(),
                    tail: String::new(),
                })
                .tail = p.tail;
        }
        if self.stick {
            self.scroll = 0;
        }
    }

    /// Привязывает результат слэш-команды к вкладке правой панели.
    fn route_slash_output(&mut self, command: &str, text: &str) {
        let head = command.split_whitespace().next().unwrap_or("");
        match head {
            "/mermaid" => self.panels.mermaid = text.to_string(),
            "/rubric" => self.panels.rubric = text.to_string(),
            "/fleet" => self.panels.fleet = text.to_string(),
            "/kb" | "/web" | "/fetch" | "/sites" => {
                self.panels.knowledge = text.to_string();
            }
            _ => {}
        }
    }

    /// Возвращает сессию из фоновой задачи; обновляет модель и токены.
    fn on_session_back(&mut self, session: AgentSession) {
        let mut session = session;
        // Токен отмены одноразовый (ставится на каждый ход в start_turn) —
        // не таскаем отработанный/отменённый в следующий ход.
        session.set_cancel_token(None);
        let mut name = session.model_name();
        // Индикатор ризонинга в бейдже модели: ● — включён явно, ○off — выключен.
        // Глифы BMP, а не эмодзи: ширина эмодзи зависит от терминала и шрифта,
        // и бейдж разъезжается (см. tui-accessibility-compat, «Unicode»).
        let g = self.theme.glyphs;
        match session.thinking() {
            Some(true) => {
                name.push(' ');
                name.push_str(g.think_on());
            }
            Some(false) => {
                name.push(' ');
                name.push_str(g.think_off());
                name.push_str("off");
            }
            None => {}
        }
        if !name.is_empty() {
            self.model_name = name;
        }
        self.history_tokens = session
            .messages()
            .iter()
            .map(ChatMessage::rough_tokens)
            .sum();
        // Бюджет перечитываем: `/model` мог сменить провайдера и окно.
        self.context_budget = session.effective_context_budget();
        self.session = Some(session);
    }

    /// Добавляет блок в чат, удерживая историю в пределах [`MAX_BLOCKS`].
    pub(crate) fn push_block(&mut self, block: ChatBlock) {
        self.blocks.push(block);
        if self.blocks.len() > MAX_BLOCKS {
            let excess = self.blocks.len() - MAX_BLOCKS;
            self.blocks.drain(..excess);
        }
        if self.stick {
            self.scroll = 0;
        }
        // Контент сдвинулся — выделение указывало бы на чужой текст.
        self.selection = None;
    }

    /// Размер «страницы» прокрутки.
    fn page(&self) -> usize {
        self.viewport.max(1)
    }

    /// Прокрутка вверх на `n` строк.
    pub(crate) fn scroll_by(&mut self, n: usize) {
        self.scroll = self.scroll.saturating_add(n);
        self.stick = false;
        self.selection = None;
    }

    /// Прокрутка вниз на `n` строк; на дне — снова прилипнуть.
    pub(crate) fn scroll_back(&mut self, n: usize) {
        self.scroll = self.scroll.saturating_sub(n);
        if self.scroll == 0 {
            self.stick = true;
        }
        self.selection = None;
    }

    /// Прилипнуть к низу чата.
    fn scroll_to_bottom(&mut self) {
        self.scroll = 0;
        self.stick = true;
    }

    /// Переключает активную вкладку правой панели.
    ///
    /// Единая точка переключения (F1–F3/F6/F7, Tab/Shift+Tab, просмотрщик):
    ///  * сброс прокрутки на 0 при смене вкладки — иначе новая вкладка
    ///    открывалась бы со старым смещением («залипание»: список задач
    ///    показывался бы с середины, будто первые строки пропали);
    ///  * «живые» вкладки (Флот, Субагенты) обновляются немедленно. Раньше
    ///    это делали только F6/F7, а Tab/Shift+Tab ждали тикового опроса и до
    ///    ~0.5 с показывали заглушку, хотя реестр читается мгновенно.
    ///
    /// Повторное нажатие той же F-клавиши по-прежнему обновляет содержимое.
    fn set_right_tab(&mut self, tab: RightTab) {
        if self.right_tab != tab {
            self.right_tab = tab;
            self.right_scroll = 0;
        }
        match tab {
            RightTab::Fleet => self.refresh_fleet(),
            RightTab::Subagents => self.refresh_subagents(),
            _ => {}
        }
    }

    /// Шаг страничной прокрутки правой панели — её фактическая высота
    /// (ставит рендер; [`.max(1)`] — до первого кадра вьюпорт неизвестен).
    fn right_page(&self) -> usize {
        self.right_viewport.max(1)
    }

    /// Полстраницы правой панели для синонимов `Ctrl+U`/`Ctrl+D`
    /// (не меньше строки — на вьюпорте в 1 строку полстраницы не существует).
    fn right_half_page(&self) -> usize {
        (self.right_page() / 2).max(1)
    }

    /// Сдвиг прокрутки правой панели (рендер клампит его по содержимому).
    pub(crate) fn right_scroll(&self) -> usize {
        self.right_scroll
    }

    /// Записывает клампнутый рендером сдвиг прокрутки правой панели —
    /// тот же приём, что у [`Self::set_help_scroll`]: колесо/страницы не
    /// уезжают за пределы содержимого и не накапливают «мёртвый» запас.
    pub(crate) fn set_right_scroll(&mut self, v: usize) {
        self.right_scroll = v;
    }

    /// Высота вьюпорта правой панели в строках (ставит рендер).
    pub(crate) fn set_right_viewport(&mut self, v: usize) {
        self.right_viewport = v.max(1);
    }

    /// Записывает верхнюю границу прокрутки (ставит рендер).
    pub(crate) fn set_right_max_scroll(&mut self, v: usize) {
        self.right_max = v;
    }

    /// Прокрутка правой панели к НАЧАЛУ на `n` строк.
    ///
    /// `right_scroll` — смещение от начала содержимого (его отдаёт
    /// `Paragraph::scroll`), поэтому «вверх» — это уменьшение, а не
    /// увеличение сдвига. Раньше здесь стоял `saturating_add`, и клавиша
    /// `PgUp`/`Ctrl+U`, подписанная «вверх», уезжала в конец.
    pub(crate) fn right_scroll_up(&mut self, n: usize) {
        self.right_scroll = self.right_scroll.saturating_sub(n);
    }

    /// Прокрутка правой панели к КОНЦУ на `n` строк (за нижнюю границу не
    /// пускает кламп рендера — содержимое может быть короче страницы).
    pub(crate) fn right_scroll_down(&mut self, n: usize) {
        self.right_scroll = self.right_scroll.saturating_add(n);
    }

    /// Прыжок к началу содержимого (`g`).
    pub(crate) fn right_scroll_to_start(&mut self) {
        self.right_scroll = 0;
    }

    /// Прыжок к концу содержимого (`G`) — по границе, записанной рендером.
    pub(crate) fn right_scroll_to_end(&mut self) {
        self.right_scroll = self.right_max;
    }

    /// Клавиши активного поиска (Ctrl+F): запрос редактируется посимвольно,
    /// навигация — Enter (к следующему) / Alt+Enter (к предыдущему), Esc —
    /// закрыть строку (подсветка и позиция прокрутки сохраняются).
    fn handle_search_key(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Esc => self.search = None,
            KeyCode::Enter => {
                let prev = key.modifiers.contains(KeyModifiers::ALT);
                if let Some(s) = self.search.as_mut() {
                    if !s.matches.is_empty() {
                        if prev {
                            s.current = s.current.checked_sub(1).unwrap_or(s.matches.len() - 1);
                        } else {
                            s.current = (s.current + 1) % s.matches.len();
                        }
                        s.dirty = true;
                    }
                }
            }
            KeyCode::Backspace => {
                if let Some(s) = self.search.as_mut() {
                    s.query.pop();
                    s.current = 0;
                    s.dirty = true;
                }
            }
            KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                let ch = if key.modifiers.contains(KeyModifiers::SHIFT) {
                    c.to_uppercase().next().unwrap_or(c)
                } else {
                    c
                };
                if let Some(s) = self.search.as_mut() {
                    s.query.push(ch);
                    s.current = 0;
                    s.dirty = true;
                }
            }
            _ => {}
        }
    }
}

/// Срез строки по display-колонкам [c0, c1) — unicode-width-безопасно.
fn slice_by_cols(line: &str, c0: usize, c1: usize) -> String {
    let mut col = 0usize;
    let mut out = String::new();
    for ch in line.chars() {
        let w = UnicodeWidthChar::width(ch).unwrap_or(0);
        if col + w > c1 {
            break;
        }
        if col >= c0 {
            out.push(ch);
        }
        col += w;
    }
    out
}

/// Отправляет ответ ожидающему инструменту (oneshot; приёмник мог уйти —
/// тогда вопрос мёртв и отвечать некому, это не ошибка).
fn answer(reply: Option<tokio::sync::oneshot::Sender<String>>, text: String) {
    if let Some(tx) = reply {
        let _ = tx.send(text);
    }
}

/// Системный промпт: шаблон `architect` из библиотеки или встроенный,
/// дополненный глобальной md-памятью (`paths.memory_file`, см. `memory`).
fn system_prompt(cfg: &Config) -> String {
    let dir = cfg.paths.prompts_dir();
    let base = match prompts::load_library(&dir) {
        Ok(lib) => match lib.iter().find(|t| t.name == "architect") {
            Some(tpl) => tpl.body.clone(),
            None => FALLBACK_SYSTEM_PROMPT.into(),
        },
        Err(_) => FALLBACK_SYSTEM_PROMPT.into(),
    };
    // Ошибка чтения памяти не фатальна: сессия работает без неё.
    let memory = crate::memory::load(&cfg.paths.memory_file).ok().flatten();
    crate::memory::augment_system_prompt(&base, memory.as_deref(), &cfg.paths.memory_file)
}

/// Максимум символов дескриптора действия в строке tool-блока.
const ACTION_DESC_MAX: usize = 48;

/// Усекает строку для дескриптора (с многоточием, по символам).
fn action_clip(text: &str) -> String {
    let mut chars = text.chars();
    let taken: String = chars.by_ref().take(ACTION_DESC_MAX).collect();
    if chars.next().is_some() {
        format!("{taken}…")
    } else {
        taken
    }
}

/// Краткое описание действия из аргументов tool-вызова: пользователь видит,
/// ЧТО делает агент (какой файл читает, что грепает, какую команду гоняет) —
/// одной строкой, без перегруза деталями.
fn action_desc(name: &str, args: &serde_json::Value) -> String {
    let str_of = |key: &str| args.get(key).and_then(|v| v.as_str());
    match name {
        "read_file" | "write_file" | "edit_file" => {
            str_of("path").map(action_clip).unwrap_or_default()
        }
        "glob" => str_of("pattern").map(action_clip).unwrap_or_default(),
        "grep" => str_of("pattern")
            .map(|p| action_clip(&format!("'{p}'")))
            .unwrap_or_default(),
        "bash" => str_of("command")
            .map(|c| action_clip(&format!(": {c}")))
            .unwrap_or_default(),
        "kb_search" | "skill_search" | "web_search" => str_of("query")
            .map(|q| action_clip(&format!("«{q}»")))
            .unwrap_or_default(),
        "web_fetch" => str_of("url")
            .and_then(|u| {
                u.split("://")
                    .nth(1)
                    .and_then(|rest| rest.split('/').next())
                    .map(str::to_string)
            })
            .unwrap_or_default(),
        "fitness_check" | "spine_lint" => str_of("repo")
            .or_else(|| str_of("path"))
            .map(action_clip)
            .unwrap_or_default(),
        "model_query" | "trace_check" => str_of("dir").map(action_clip).unwrap_or_default(),
        "archify_validate" | "archify_deliver" | "archify_compare" => {
            str_of("input").map(action_clip).unwrap_or_default()
        }
        "harness_run" => str_of("harness")
            .map(str::to_string)
            .or_else(|| str_of("repo").map(action_clip))
            .unwrap_or_default(),
        "subagent" | "subagent_run" => str_of("task").map(action_clip).unwrap_or_default(),
        _ => String::new(),
    }
}

/// Заглушки для headless-тестов (без терминала, сети и LLM).
#[cfg(test)]
pub(crate) mod testing {
    use std::path::PathBuf;
    use std::sync::Arc;

    use async_trait::async_trait;

    use crate::agent::AgentSession;
    use crate::config::Config;
    use crate::error::Result;
    use crate::llm::{ChatMessage, ChatRequest, LlmProvider};
    use crate::tool::{ToolContext, ToolRegistry};

    use super::super::caps::Caps;
    use super::App;

    /// LLM-провайдер-заглушка: отвечает фиксированным текстом, сеть не нужна.
    #[derive(Debug)]
    struct StubProvider;

    #[async_trait]
    impl LlmProvider for StubProvider {
        fn name(&self) -> &'static str {
            "stub"
        }

        fn model(&self) -> &'static str {
            "stub-model"
        }

        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            Ok(ChatMessage::assistant("ответ-заглушка", Vec::new()))
        }
    }

    /// Сессия на провайдере-заглушке (конструктор реальный, без сети).
    /// Журнал — во временный каталог теста: дефолтный `paths.sessions_dir`
    /// указывает в настоящий ~/.arch-ml, тесты не должны туда писать.
    pub(crate) fn stub_session(cfg: &Arc<Config>) -> AgentSession {
        let mut test_cfg = cfg.as_ref().clone();
        test_cfg.paths.sessions_dir =
            std::env::temp_dir().join(format!("arch-test-sessions-{}", std::process::id()));
        let cfg = Arc::new(test_cfg);
        let ctx = ToolContext::new(PathBuf::from("/tmp"), cfg.clone());
        AgentSession::new(
            cfg,
            Arc::new(StubProvider),
            ToolRegistry::new(),
            ctx,
            "системный промпт".into(),
        )
    }

    /// Приложение для тестов: сессия-заглушка, без MCP и канала событий.
    /// Возможности терминала — дефолтные (truecolor, Unicode, 60×16).
    pub(crate) fn test_app() -> App {
        let cfg = Arc::new(Config::default());
        let session = stub_session(&cfg);
        let ctx = ToolContext::new(PathBuf::from("/tmp"), cfg);
        App::new(Some(session), ctx, None, None, Caps::default())
    }

    /// Подменяет показания контекста для тестов статус-бара.
    pub(crate) fn set_context_usage(app: &mut App, used: usize, budget: usize) {
        app.history_tokens = used;
        app.context_budget = budget;
    }

    /// Подменяет флаг «модель думает» для тестов очереди ввода.
    pub(crate) fn set_thinking(app: &mut App, thinking: bool) {
        app.thinking = thinking;
        app.thinking_since = thinking.then(std::time::Instant::now);
        app.reset_stream_stats();
    }

    /// Кладёт строку теплицы гипотез (поле приватное: в бою его ставит только
    /// ветка `SlashOutcome::GoalStarted`; тесты рендера берут готовое состояние).
    pub(crate) fn set_intent_hint(app: &mut App, hint: Option<&str>) {
        app.intent_hint = hint.map(str::to_string);
    }
}

#[cfg(test)]
mod tests {
    use super::testing;
    use super::testing::{stub_session, test_app};
    use super::*;

    /// Число реплик пользователя в логе (логотип — не реплика).
    fn user_blocks(app: &App) -> usize {
        app.blocks
            .iter()
            .filter(|b| matches!(b, ChatBlock::User(_)))
            .count()
    }
    use crate::agent::slash::SlashOutcome;
    use crate::error::HarnessError;

    #[test]
    fn model_picker_lists_config_models_with_think_marks() {
        let mut app = test_app();
        app.open_model_picker();
        let ask = app.ask.as_ref().expect("пикер открыт");
        assert_eq!(ask.kind, super::AskKind::ModelPicker);
        // Дефолтный конфиг: deepseek*, kimi и ряд GLM (BTreeMap, ≥4 моделей).
        assert!(ask.options.len() >= 4, "options: {:?}", ask.options.len());
        let ds = ask
            .options
            .iter()
            .find(|o| o.label == "deepseek")
            .expect("deepseek в списке");
        assert!(
            ds.description.contains("deepseek-flash"),
            "id модели: {}",
            ds.description
        );
        assert!(
            ds.description.contains("/think"),
            "метка ризонинга: {}",
            ds.description
        );
        assert!(ask.options.iter().any(|o| o.label == "deepseek-pro"));
        assert!(ask.reply.is_none(), "пикер без канала инструмента");
    }

    #[tokio::test]
    async fn model_picker_answer_dispatches_model_switch_and_esc_cancels() {
        let mut app = test_app();
        let (tx, _rx) = mpsc::channel(8);
        app.attach(tx);
        app.open_model_picker();
        app.answer_ask(Some("kimi".into()));
        assert!(app.ask.is_none(), "модалка закрыта");
        assert_eq!(app.pending_slash.as_deref(), Some("/model kimi"));
        assert!(app.thinking, "переключение пошло в фон");
        // Esc — тихая отмена без диспатча.
        app.thinking = false;
        app.pending_slash = None;
        app.open_model_picker();
        app.answer_ask(None);
        assert!(app.pending_slash.is_none(), "Esc не диспатчит");
        assert!(app.ask.is_none());
    }

    #[test]
    fn history_navigates_up_and_down_with_draft() {
        let mut input = InputState::default();
        input.set_text("первая".into());
        let _ = input.submit();
        input.set_text("вторая".into());
        let _ = input.submit();
        input.set_text("черновик".into());
        input.history_up();
        assert_eq!(input.text(), "вторая");
        input.history_up();
        assert_eq!(input.text(), "первая");
        input.history_up();
        assert_eq!(input.text(), "первая", "дальше начала истории не идём");
        input.history_down();
        assert_eq!(input.text(), "вторая");
        input.history_down();
        assert_eq!(input.text(), "черновик", "за концом истории — черновик");
    }

    #[test]
    fn history_ignores_empty_and_duplicates() {
        let mut input = InputState::default();
        let _ = input.submit();
        assert!(input.history.is_empty());
        input.set_text("команда".into());
        let _ = input.submit();
        input.set_text("команда".into());
        let _ = input.submit();
        assert_eq!(input.history.len(), 1, "дубликат подряд не сохраняется");
    }

    #[test]
    fn tab_completes_and_stays_on_single_candidate() {
        let mut input = InputState::default();
        // Префикс уникален: "/me" матчит также "/memory", цикл по двум.
        input.set_text("/merm".into());
        assert!(input.complete_tab());
        assert_eq!(input.text(), "/mermaid");
        assert!(
            input.complete_tab(),
            "Tab на полной команде остаётся в цикле"
        );
        assert_eq!(input.text(), "/mermaid");
    }

    #[test]
    fn tab_without_candidates_returns_false() {
        let mut input = InputState::default();
        input.set_text("/zzz".into());
        assert!(!input.complete_tab());
        input.set_text("привет".into());
        assert!(!input.complete_tab());
    }

    #[test]
    fn ghost_hint_shows_candidate_suffix() {
        let mut input = InputState::default();
        input.set_text("/mer".into());
        assert_eq!(input.ghost_hint().as_deref(), Some("maid"));
        input.set_text("текст".into());
        assert_eq!(input.ghost_hint(), None);
    }

    #[test]
    fn editing_handles_unicode_boundaries() {
        let mut input = InputState::default();
        for c in "при".chars() {
            input.insert_char(c);
        }
        assert_eq!(input.text(), "при");
        input.move_left();
        input.backspace();
        assert_eq!(input.text(), "пи", "backspace удалил «р» перед курсором");
        input.move_home();
        input.delete();
        assert_eq!(input.text(), "и", "delete удалил «п» под курсором");
        input.move_end();
        input.insert_char('!');
        assert_eq!(input.text(), "и!");
    }

    #[test]
    fn mouse_wheel_scrolls_dialog() {
        use crossterm::event::{MouseEvent, MouseEventKind};
        let mut app = test_app();
        app.screen = Screen::Chat;
        let mouse = |kind| MouseEvent {
            kind,
            column: 5,
            row: 5,
            modifiers: crossterm::event::KeyModifiers::empty(),
        };
        app.handle_mouse(mouse(MouseEventKind::ScrollUp));
        assert_eq!(app.scroll, 3, "колесо вверх — три строки");
        assert!(!app.stick, "скролл отлипает от дна");
        app.handle_mouse(mouse(MouseEventKind::ScrollUp));
        assert_eq!(app.scroll, 6);
        app.handle_mouse(mouse(MouseEventKind::ScrollDown));
        app.handle_mouse(mouse(MouseEventKind::ScrollDown));
        assert_eq!(app.scroll, 0, "колесо вниз — назад к дну");
        assert!(app.stick, "на дне — прилипание");
        // Вне чата колесо игнорируется.
        app.screen = Screen::Fatal("нет модели".into());
        app.handle_mouse(mouse(MouseEventKind::ScrollUp));
        assert_eq!(app.scroll, 0);
    }

    #[test]
    fn click_on_jump_button_scrolls_to_bottom() {
        use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.scroll_by(9);
        assert!(app.scroll > 0 && !app.stick);
        // Кнопка « ▼ » 3×1 (координаты ставит рендер — здесь эмулируем).
        app.jump_btn = Some(ratatui::layout::Rect::new(10, 20, 3, 1));
        let click = |col, row| MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: col,
            row,
            modifiers: crossterm::event::KeyModifiers::empty(),
        };
        // Клик мимо кнопки — скролл не сбрасывается.
        app.handle_mouse(click(0, 0));
        assert_eq!(app.scroll, 9, "клик мимо кнопки — без эффекта");
        // Клик по кнопке (в т.ч. по крайней ячейке) — прыжок к дну.
        app.handle_mouse(click(12, 20));
        assert_eq!(app.scroll, 0, "клик по ▼ — к свежему ответу");
        assert!(app.stick, "снова прилипли к дну");
    }

    #[test]
    fn agent_events_build_blocks_and_update_panels() {
        let mut app = test_app();
        app.handle_message(AppMessage::AgentEvent(AgentEvent::Delta("При".into())));
        app.handle_message(AppMessage::AgentEvent(AgentEvent::Delta("вет".into())));
        app.handle_message(AppMessage::AgentEvent(AgentEvent::ToolStart {
            name: "mermaid_render".into(),
            args: serde_json::json!({}),
        }));
        app.handle_message(AppMessage::AgentEvent(AgentEvent::ToolEnd {
            name: "mermaid_render".into(),
            is_error: false,
            summary: "┌───┐".into(),
            content: "┌───┐\n│ A │\n└───┘".into(),
        }));
        app.handle_message(AppMessage::AgentEvent(AgentEvent::TurnDone));
        // Первый блок — логотип (стартовый экран), дальше идут события хода.
        assert!(matches!(app.blocks.first(), Some(ChatBlock::Logo)));
        match app.blocks.get(1) {
            Some(ChatBlock::Assistant(text)) => assert_eq!(text, "Привет"),
            other => panic!("ожидался assistant-блок, получено: {other:?}"),
        }
        match app.blocks.get(2) {
            Some(ChatBlock::Tool {
                state: ToolState::Ok,
                summary,
                ..
            }) => assert_eq!(summary, "┌───┐"),
            other => panic!("ожидался завершённый tool-блок, получено: {other:?}"),
        }
        assert_eq!(
            app.panels.mermaid, "┌───┐\n│ A │\n└───┘",
            "на вкладку Mermaid уходит ПОЛНЫЙ рендер, не summary"
        );
    }

    #[test]
    fn tool_progress_updates_live_tail_of_running_block() {
        let mut app = test_app();
        app.handle_message(AppMessage::AgentEvent(AgentEvent::ToolStart {
            name: "bash".into(),
            args: serde_json::json!({"command": "make all"}),
        }));
        let idx = app.blocks.len() - 1;
        assert!(
            app.tool_live.contains_key(&idx),
            "старт вызова завёл live-запись"
        );
        assert_eq!(
            app.running_tool().map(|(n, a, _)| (n, a)),
            Some(("bash", ": make all"))
        );

        app.handle_message(AppMessage::ToolProgress(ToolProgress {
            name: "bash".into(),
            tail: "компилирую crate-a\nкомпилирую crate-b".into(),
            elapsed_secs: 3,
        }));
        assert_eq!(
            app.tool_live.get(&idx).map(|l| l.tail.as_str()),
            Some("компилирую crate-a\nкомпилирую crate-b"),
            "хвост обновился снапшотом"
        );
        // Снапшот чужого инструмента — мимо (нет Running-блока с таким именем).
        app.handle_message(AppMessage::ToolProgress(ToolProgress {
            name: "web_fetch".into(),
            tail: "мимо".into(),
            elapsed_secs: 1,
        }));
        assert!(
            app.tool_live
                .get(&idx)
                .is_some_and(|l| l.tail.contains("crate-b")),
            "чужой прогресс не тронул хвост"
        );
        // Завершение вызова — live-запись умирает вместе с ним.
        app.handle_message(AppMessage::AgentEvent(AgentEvent::ToolEnd {
            name: "bash".into(),
            is_error: false,
            summary: "готово".into(),
            content: "готово".into(),
        }));
        assert!(app.tool_live.is_empty(), "live-запись убрана по ToolEnd");
        assert!(app.running_tool().is_none());
    }

    #[test]
    fn thinking_timer_set_on_turn_and_cleared_on_finish() {
        let mut app = test_app();
        assert!(app.thinking_elapsed().is_none());
        testing::set_thinking(&mut app, true);
        assert_eq!(app.thinking_elapsed(), Some(0));
        testing::set_thinking(&mut app, false);
        assert!(app.thinking_elapsed().is_none());
    }

    #[test]
    fn stream_stats_accumulate_on_deltas_and_reset_on_finish() {
        let mut app = test_app();
        assert!(app.stream_stats().is_none(), "до хода статистики нет");
        app.handle_message(AppMessage::AgentEvent(AgentEvent::ReasoningDelta(
            "мысль ".into(),
        )));
        app.handle_message(AppMessage::AgentEvent(AgentEvent::Delta("отв".into())));
        app.handle_message(AppMessage::AgentEvent(AgentEvent::Delta("ет".into())));
        let (answer, think, secs) = app.stream_stats().expect("стрим идёт");
        assert_eq!(answer, "ответ".len(), "байты видимого ответа накоплены");
        assert_eq!(think, "мысль ".len(), "байты мыслей накоплены");
        assert!(secs < 5.0, "секунды стрима реалистичны: {secs}");
        // Завершение хода — статистика стрима сброшена.
        let session = app.session.take().expect("сессия есть");
        app.handle_message(AppMessage::TurnFinished {
            session,
            result: Ok("готово".into()),
        });
        assert!(
            app.stream_stats().is_none(),
            "по концу хода статистика сброшена"
        );
    }

    #[test]
    fn turn_error_pushes_error_block_and_restores_session() {
        let mut app = test_app();
        let session = app.session.take().expect("сессия есть");
        app.thinking = true;
        app.handle_message(AppMessage::TurnFinished {
            session,
            result: Err(HarnessError::Agent("llm down".into())),
        });
        assert!(!app.thinking);
        assert!(app.session.is_some(), "сессия вернулась в App");
        assert!(matches!(app.blocks.last(), Some(ChatBlock::Error(_))));
    }

    #[test]
    fn turn_without_deltas_shows_final_text() {
        let mut app = test_app();
        let session = app.session.take().expect("сессия есть");
        app.thinking = true;
        app.handle_message(AppMessage::TurnFinished {
            session,
            result: Ok("финальный ответ".into()),
        });
        assert!(matches!(
            app.blocks.last(),
            Some(ChatBlock::Assistant(t)) if t == "финальный ответ"
        ));
    }

    #[test]
    fn slash_handled_pushes_system_block_and_updates_panel() {
        let mut app = test_app();
        let session = app.session.take().expect("сессия есть");
        app.pending_slash = Some("/mermaid graph TD; A-->B".into());
        app.thinking = true;
        app.handle_message(AppMessage::SlashFinished {
            session,
            result: Ok(SlashOutcome::Handled("ASCII-art".into())),
        });
        assert!(!app.thinking);
        assert!(app.session.is_some());
        assert_eq!(app.panels.mermaid, "ASCII-art");
        match app.blocks.last() {
            Some(ChatBlock::System { command, text }) => {
                assert!(command.starts_with("/mermaid"));
                assert_eq!(text, "ASCII-art");
            }
            other => panic!("ожидался system-блок, получено: {other:?}"),
        }
    }

    #[test]
    fn slash_quit_sets_should_quit() {
        let mut app = test_app();
        let session = app.session.take().expect("сессия есть");
        app.handle_message(AppMessage::SlashFinished {
            session,
            result: Ok(SlashOutcome::Quit),
        });
        assert!(app.should_quit);
    }

    #[test]
    fn app_starts_in_chat_with_logo_as_first_block() {
        let mut app = test_app();
        assert!(
            matches!(app.screen, Screen::Chat),
            "заставки нет: старт сразу в рабочем экране"
        );
        assert!(
            matches!(app.blocks.first(), Some(ChatBlock::Logo)),
            "первый блок диалога — логотип: {:?}",
            app.blocks
        );
        // Логотип — обычный контент лога: уезжает вверх по мере переписки.
        app.push_block(ChatBlock::User("привет".into()));
        assert!(
            matches!(app.blocks.first(), Some(ChatBlock::Logo)),
            "логотип остаётся первой строкой лога"
        );
        assert_eq!(app.blocks.len(), 2);
        // Enter без набранного текста не отправляет пустое сообщение.
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.blocks.len(), 2);
    }

    #[test]
    fn slash_new_session_clears_blocks_panels_and_scroll() {
        let mut app = test_app();
        app.push_block(ChatBlock::User("старое сообщение".into()));
        app.panels.mermaid = "старый арт".into();
        app.scroll_by(3);
        let session = app.session.take().expect("сессия есть");
        app.handle_message(AppMessage::SlashFinished {
            session,
            result: Ok(SlashOutcome::NewSession),
        });
        assert_eq!(
            app.blocks.len(),
            2,
            "логотип + системная заметка: {:?}",
            app.blocks
        );
        assert!(
            matches!(app.blocks.first(), Some(ChatBlock::Logo)),
            "свежий лог открывает логотип"
        );
        match app.blocks.last() {
            Some(ChatBlock::System { text, .. }) => {
                assert!(text.contains("новая сессия"), "{text}");
            }
            other => panic!("ожидался system-блок, получено: {other:?}"),
        }
        assert!(app.panels.mermaid.is_empty(), "вкладки очищены");
        assert_eq!(app.scroll, 0);
        assert!(app.stick, "прилипание к дну восстановлено");
        assert_eq!(app.history_tokens(), 0, "индикатор контекста сброшен");
    }

    #[test]
    fn intent_hint_takes_status_row_but_not_under_modal() {
        let mut app = test_app();
        assert!(!app.has_input_state(), "пустая строка не занимает ряд");
        testing::set_intent_hint(&mut app, Some("гипотезы: stub — поднять?"));
        assert_eq!(app.intent_hint(), Some("гипотезы: stub — поднять?"));
        assert!(app.has_input_state(), "строка теплицы занимает ряд");

        // Ask-модалка — верхний слой: строка под ней не обещает действий
        // («… — поднять?» читалось бы как живой вопрос).
        app.open_model_picker();
        assert!(app.ask.is_some(), "модалка открыта");
        assert!(
            !app.has_input_state(),
            "под модалкой ряд теплицы не выделяется"
        );

        // Ход забирает ряд у теплицы, после хода строка возвращается.
        app.ask = None;
        testing::set_thinking(&mut app, true);
        assert!(app.has_input_state(), "ход держит ряд сам");
        testing::set_thinking(&mut app, false);
        assert!(
            app.has_input_state(),
            "после хода строка теплицы снова видна"
        );

        testing::set_intent_hint(&mut app, None);
        assert!(!app.has_input_state(), "снятая подсказка ряд не держит");
    }

    #[test]
    fn goal_started_carries_hint_and_new_session_clears_it() {
        let mut app = test_app();
        let session = app.session.take().expect("сессия есть");
        app.pending_slash = Some("/goal обучить 3B || loss падает || true".into());
        app.handle_message(AppMessage::SlashFinished {
            session,
            result: Ok(SlashOutcome::GoalStarted {
                text: "цель принята".into(),
                intent_hint: Some("гипотезы: stub — поднять?".into()),
            }),
        });
        assert_eq!(
            app.intent_hint(),
            Some("гипотезы: stub — поднять?"),
            "строка хука доехала из слэш-исполнителя в UI"
        );
        assert!(!app.thinking, "цель без kickoff-сообщения не запускает ход");

        let session = app.session.take().expect("сессия есть");
        app.handle_message(AppMessage::SlashFinished {
            session,
            result: Ok(SlashOutcome::NewSession),
        });
        assert_eq!(app.intent_hint(), None, "новая сессия снимает подсказку");
    }

    #[test]
    fn goal_subcommand_clears_stale_hint() {
        let mut app = test_app();
        testing::set_intent_hint(&mut app, Some("старая строка теплицы"));
        let session = app.session.take().expect("сессия есть");
        app.pending_slash = Some("/goal status".into());
        app.handle_message(AppMessage::SlashFinished {
            session,
            result: Ok(SlashOutcome::Handled("цель не поставлена".into())),
        });
        // `/goal status|pause|cancel|next` нового намерения не несёт — старая
        // строка висела бы как обещание про уже снятую цель.
        assert_eq!(app.intent_hint(), None);
    }

    #[tokio::test]
    async fn finished_turn_updates_context_gauge() {
        let mut app = test_app();
        let mut session = app.session.take().expect("сессия есть");
        // Полный ход через реальный AgentSession::send (провайдер-заглушка).
        session.send("расскажи про сагу", None).await.expect("ход");
        assert!(!session.messages().is_empty());
        app.handle_message(AppMessage::TurnFinished {
            session,
            result: Ok("ответ-заглушка".into()),
        });
        assert!(
            app.history_tokens() > 0,
            "после хода счётчик контекста обязан вырасти"
        );
    }

    #[test]
    fn context_usage_event_updates_gauge_mid_turn() {
        let mut app = test_app();
        app.handle_message(AppMessage::AgentEvent(AgentEvent::ContextUsage(12_345)));
        assert_eq!(
            app.history_tokens(),
            12_345,
            "индикатор обновился по ходу хода"
        );
        app.handle_message(AppMessage::AgentEvent(AgentEvent::ContextUsage(20_000)));
        assert_eq!(app.history_tokens(), 20_000);
    }

    #[test]
    fn typing_and_enter_enqueue_while_agent_thinks() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        testing::set_thinking(&mut app, true);
        for c in "добавь NFR".chars() {
            app.handle_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        assert_eq!(
            app.input.text(),
            "добавь NFR",
            "ввод во время хода работает"
        );
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.queue.len(), 1, "сообщение в очереди");
        assert_eq!(app.queue[0], "добавь NFR");
        assert!(app.input.text().is_empty(), "строка ввода очищена");
        assert!(app.thinking, "новый ход не начался — сообщение ждёт");
        assert!(app.session.is_some(), "сессия не уехала в фоновую задачу");
    }

    #[test]
    fn alt_enter_and_bang_prefix_jump_to_queue_front() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        testing::set_thinking(&mut app, true);
        app.input.set_text("обычное".into());
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        app.input.set_text("срочное".into());
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT));
        app.input.set_text("!!ещё срочнее".into());
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let order: Vec<&str> = app.queue.iter().map(String::as_str).collect();
        assert_eq!(
            order,
            vec!["ещё срочнее", "срочное", "обычное"],
            "срочные — в начало очереди (Alt+Enter и «!!»)"
        );
    }

    #[test]
    fn alt_enter_during_turn_cancels_turn_and_injects_urgent() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        testing::set_thinking(&mut app, true);
        let token = CancellationToken::new();
        app.turn_cancel = Some(token.clone());
        app.input.set_text("готово".into());
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT));
        assert!(token.is_cancelled(), "текущий ход получил отмену");
        assert_eq!(
            app.queue.front().map(String::as_str),
            Some("готово"),
            "набранное — первым в очереди (старт сразу после возврата сессии)"
        );
        assert!(app.input.text().is_empty(), "строка ввода очищена");
        assert!(app.thinking, "ждём возврата сессии из прерванного хода");
    }

    #[test]
    fn esc_during_turn_interrupts_instead_of_quitting() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        testing::set_thinking(&mut app, true);
        let token = CancellationToken::new();
        app.turn_cancel = Some(token.clone());
        app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(token.is_cancelled(), "ход прерван");
        assert!(
            !app.should_quit,
            "Esc во время хода прерывает ход, а не приложение"
        );
        // В простое — выход, как раньше.
        testing::set_thinking(&mut app, false);
        app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.should_quit, "Esc в простое — выход");
    }

    #[test]
    fn turn_finished_clears_cancel_token() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        testing::set_thinking(&mut app, true);
        app.turn_cancel = Some(CancellationToken::new());
        let session = app.session.take().expect("сессия в тестовом app");
        app.handle_message(AppMessage::TurnFinished {
            session,
            result: Ok(String::new()),
        });
        assert!(app.turn_cancel.is_none(), "токен сброшен вместе с ходом");
        assert!(!app.thinking);
    }

    #[test]
    fn newline_keys_insert_line_break() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        // Ctrl+J — перевод строки в любом терминале.
        app.handle_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::CONTROL));
        app.handle_key(KeyEvent::new(KeyCode::Char('b'), KeyModifiers::NONE));
        assert_eq!(app.input.text(), "a\nb");
        // Shift+Enter (kitty-протокол).
        app.input.set_text("x".into());
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT));
        assert_eq!(app.input.text(), "x\n");
        // Alt+Enter вне хода — перевод строки, а не submit.
        app.input.set_text("y".into());
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT));
        assert_eq!(app.input.text(), "y\n");
        assert_eq!(user_blocks(&app), 0, "сообщение не ушло модели");
    }

    #[test]
    fn queued_message_keeps_newlines() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        testing::set_thinking(&mut app, true);
        app.input.set_text("строка 1\nстрока 2".into());
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.queue[0], "строка 1\nстрока 2", "переносы сохраняются");
    }

    #[test]
    fn up_down_navigate_lines_then_history() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.input.set_text("старая".into());
        let _ = app.input.submit();
        app.input.set_text("ab\ncde".into()); // курсор в конце: строка 1, кол 3
        app.handle_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        assert_eq!(app.input.cursor(), 2, "строка 0, колонка клампится до 2");
        app.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        assert_eq!(app.input.cursor(), 5, "строка 1, колонка 2 (памяти нет)");
        // На первой строке Up уходит в историю.
        app.handle_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        assert_eq!(app.input.text(), "старая", "Up на первой строке — история");
    }

    #[test]
    fn home_end_are_line_aware() {
        let mut input = InputState::default();
        input.set_text("ab\ncde".into());
        input.move_home();
        assert_eq!(input.cursor(), 3, "начало второй строки");
        input.move_end();
        assert_eq!(input.cursor(), 6, "конец второй строки");
        input.move_up_line();
        input.move_home();
        assert_eq!(input.cursor(), 0, "начало первой строки");
    }

    /// Приложение с фиктивным окном диалога для тестов выделения мышью.
    fn app_with_dialog() -> App {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.dialog_inner = Some(ratatui::layout::Rect {
            x: 1,
            y: 1,
            width: 40,
            height: 5,
        });
        app.dialog_lines = vec![
            "первая строка".into(),
            "вторая строка".into(),
            "третья строка".into(),
        ];
        app.dialog_skip = 0;
        app
    }

    /// Событие мыши без модификаторов.
    fn mouse(
        kind: crossterm::event::MouseEventKind,
        x: u16,
        y: u16,
    ) -> crossterm::event::MouseEvent {
        crossterm::event::MouseEvent {
            kind,
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        }
    }

    #[test]
    fn slice_by_cols_unicode_safe() {
        assert_eq!(slice_by_cols("hello", 1, 4), "ell");
        assert_eq!(slice_by_cols("привет", 0, 3), "при");
        assert_eq!(slice_by_cols("abc", 5, 9), "");
    }

    #[test]
    fn mouse_drag_selects_text_and_reports_copy() {
        use crossterm::event::MouseButton as B;
        use crossterm::event::MouseEventKind as K;
        let mut app = app_with_dialog();
        // Драг с (1,1) по (5,2): первая строка целиком + «втора» второй.
        app.handle_mouse(mouse(K::Down(B::Left), 1, 1));
        app.handle_mouse(mouse(K::Drag(B::Left), 5, 2));
        assert_eq!(app.selected_text(), "первая строка\nвтора");
        app.handle_mouse(mouse(K::Up(B::Left), 5, 2));
        assert!(
            app.selection.is_some(),
            "выделение держится до следующего клика"
        );
        assert!(
            app.toast().is_some(),
            "статус-бар сообщает о копировании тостом (успех или отказ механизма)"
        );
    }

    #[test]
    fn mouse_drag_bottom_up_normalizes_reading_order() {
        use crossterm::event::MouseButton as B;
        use crossterm::event::MouseEventKind as K;
        let mut app = app_with_dialog();
        // Драг снизу вверх: (3,3) → (1,2) — порядок чтения всё равно сверху вниз.
        app.handle_mouse(mouse(K::Down(B::Left), 3, 3));
        app.handle_mouse(mouse(K::Drag(B::Left), 1, 2));
        assert_eq!(app.selected_text(), "вторая строка\nтре");
    }

    #[test]
    fn plain_click_and_scroll_clear_selection() {
        use crossterm::event::MouseButton as B;
        use crossterm::event::MouseEventKind as K;
        let mut app = app_with_dialog();
        app.handle_mouse(mouse(K::Down(B::Left), 1, 1));
        app.handle_mouse(mouse(K::Drag(B::Left), 8, 2));
        assert!(app.selection.is_some());
        // Точечный клик (без драга) снимает выделение без копирования.
        app.handle_mouse(mouse(K::Down(B::Left), 4, 2));
        app.handle_mouse(mouse(K::Up(B::Left), 4, 2));
        assert!(app.selection.is_none(), "клик без драга снял выделение");
        // Скролл колесом сдвигает контент — выделение снимается.
        app.handle_mouse(mouse(K::Down(B::Left), 1, 1));
        app.handle_mouse(mouse(K::Drag(B::Left), 8, 2));
        app.handle_mouse(mouse(K::ScrollUp, 1, 1));
        assert!(app.selection.is_none(), "скролл снял выделение");
    }

    #[tokio::test]
    async fn queued_message_starts_when_turn_finishes() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let (tx, _rx) = mpsc::channel(8);
        app.msg_tx = Some(tx);
        testing::set_thinking(&mut app, true);
        app.queue.push_back("второй вопрос".into());
        let session = app.session.take().expect("сессия есть");
        app.handle_message(AppMessage::TurnFinished {
            session,
            result: Ok("первый ответ".into()),
        });
        assert!(app.thinking, "сразу стартовал ход из очереди");
        assert!(app.queue.is_empty(), "очередь опустела");
        assert!(
            app.blocks
                .iter()
                .any(|b| matches!(b, ChatBlock::User(t) if t == "второй вопрос")),
            "блок пользователя для очередного сообщения"
        );
    }

    /// Реестр с одной завершённой задачей (отчёт «вердикт: PASS»).
    fn finished_registry() -> crate::subagent::SubagentRegistry {
        let registry = crate::subagent::SubagentRegistry::new();
        registry.insert(crate::subagent::SubagentTask {
            id: "hr-1".into(),
            agent: "claude-code".into(),
            task: "аудит".into(),
            status: crate::subagent::TaskStatus::Done,
            report: "вердикт: PASS".into(),
            started_at: "t0".into(),
            finished_at: Some("t1".into()),
        });
        registry
    }

    #[test]
    fn background_finished_queues_report_while_agent_thinks() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.tool_ctx.subagents = Some(finished_registry());
        testing::set_thinking(&mut app, true);
        app.handle_message(AppMessage::BackgroundFinished(BackgroundNotice {
            id: "hr-1".into(),
            status: crate::subagent::TaskStatus::Done,
        }));
        assert_eq!(app.queue.len(), 1, "отчёт встал в очередь");
        assert!(
            app.queue[0].contains("[фон]"),
            "метка фона: {}",
            app.queue[0]
        );
        assert!(
            app.queue[0].contains("вердикт: PASS"),
            "отчёт в сообщении: {}",
            app.queue[0]
        );
        assert!(app.thinking, "новый ход не начался — сообщение ждёт");
        assert!(
            app.blocks
                .iter()
                .any(|b| matches!(b, ChatBlock::System { command, .. } if command == "фон")),
            "системный блок о завершении"
        );
    }

    #[tokio::test]
    async fn background_finished_starts_turn_when_idle() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let (tx, _rx) = mpsc::channel(8);
        app.msg_tx = Some(tx);
        app.tool_ctx.subagents = Some(finished_registry());
        app.handle_message(AppMessage::BackgroundFinished(BackgroundNotice {
            id: "hr-1".into(),
            status: crate::subagent::TaskStatus::Done,
        }));
        assert!(app.thinking, "стартовал ход с отчётом фоновой задачи");
        assert!(app.queue.is_empty(), "очередь опустела");
        assert!(
            app.blocks
                .iter()
                .any(|b| matches!(b, ChatBlock::User(t) if t.contains("[фон]") && t.contains("вердикт: PASS"))),
            "блок пользователя с отчётом: {:?}", app.blocks
        );
    }

    #[tokio::test]
    async fn queued_slash_runs_after_turn() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let (tx, _rx) = mpsc::channel(8);
        app.msg_tx = Some(tx);
        testing::set_thinking(&mut app, true);
        app.queue.push_back("/tools".into());
        let session = app.session.take().expect("сессия есть");
        app.handle_message(AppMessage::TurnFinished {
            session,
            result: Ok("ответ".into()),
        });
        assert!(app.thinking);
        assert_eq!(
            app.pending_slash.as_deref(),
            Some("/tools"),
            "очередная слэш-команда запущена"
        );
    }

    #[tokio::test]
    async fn session_picker_lists_journals_and_confirms_to_resume() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let dir = tmp.path().join("sessions");
        std::fs::create_dir_all(&dir).expect("mkdir");
        for (name, first) in [
            (
                "session-20260815-100000-111.jsonl",
                "первый вопрос старой сессии",
            ),
            ("session-20260815-110000-111.jsonl", "второй вопрос"),
        ] {
            std::fs::write(
                dir.join(name),
                format!(
                    "{{\"kind\":\"system\",\"content\":\"sys\"}}\n\
                     {{\"kind\":\"user\",\"content\":\"{first}\"}}\n\
                     {{\"kind\":\"assistant\",\"content\":\"ответ\"}}\n"
                ),
            )
            .expect("write journal");
        }
        let mut app = test_app();
        app.screen = Screen::Chat;
        let mut cfg = Config::default();
        cfg.paths.sessions_dir = dir;
        app.tool_ctx = ToolContext::new(std::path::PathBuf::from("/tmp"), Arc::new(cfg));
        let (tx, _rx) = mpsc::channel(8);
        app.msg_tx = Some(tx);

        app.open_session_picker();
        let ask = app.ask.as_ref().expect("пикер открыт");
        assert_eq!(ask.kind, super::AskKind::SessionPicker);
        assert_eq!(ask.options.len(), 2, "оба журнала в вариантах");
        assert!(
            ask.options
                .iter()
                .any(|o| o.description.contains("второй вопрос")),
            "превью первой реплики в описании: {:?}",
            ask.options[0].description
        );
        // Навигация вниз + Enter → слэш /resume <имя-файла>.
        app.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(
            app.pending_slash
                .as_deref()
                .is_some_and(|c| c.starts_with("/resume session-")),
            "выбор свёлся к /resume <имя>: {:?}",
            app.pending_slash
        );
        assert!(app.ask.is_none(), "модалка закрыта после выбора");
    }

    #[test]
    fn session_picker_without_journals_shows_note() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut app = test_app();
        app.screen = Screen::Chat;
        let mut cfg = Config::default();
        cfg.paths.sessions_dir = tmp.path().join("empty-sessions");
        app.tool_ctx = ToolContext::new(std::path::PathBuf::from("/tmp"), Arc::new(cfg));
        app.open_session_picker();
        assert!(app.ask.is_none(), "пикер не открывается без журналов");
        match app.blocks.last() {
            Some(ChatBlock::System { text, .. }) => {
                assert!(text.contains("прошлых сессий нет"), "{text}");
            }
            other => panic!("ожидалась заметка, получено: {other:?}"),
        }
    }

    #[test]
    fn slash_unknown_pushes_error_block() {
        let mut app = test_app();
        let session = app.session.take().expect("сессия есть");
        app.handle_message(AppMessage::SlashFinished {
            session,
            result: Ok(SlashOutcome::Unknown("/zzz".into())),
        });
        match app.blocks.last() {
            Some(ChatBlock::Error(text)) => assert!(text.contains("/zzz")),
            other => panic!("ожидался error-блок, получено: {other:?}"),
        }
    }

    #[test]
    fn slash_not_slash_is_internal_error() {
        let mut app = test_app();
        let session = app.session.take().expect("сессия есть");
        app.handle_message(AppMessage::SlashFinished {
            session,
            result: Ok(SlashOutcome::NotSlash),
        });
        match app.blocks.last() {
            Some(ChatBlock::Error(text)) => assert!(text.contains("NotSlash")),
            other => panic!("ожидался error-блок, получено: {other:?}"),
        }
    }

    #[test]
    fn slash_execution_error_pushes_error_block() {
        let mut app = test_app();
        let session = app.session.take().expect("сессия есть");
        app.handle_message(AppMessage::SlashFinished {
            session,
            result: Err(HarnessError::Agent("boom".into())),
        });
        assert!(matches!(app.blocks.last(), Some(ChatBlock::Error(_))));
    }

    #[test]
    fn scroll_sticks_to_bottom_until_scrolled_up() {
        let mut app = test_app();
        app.viewport = 10;
        app.scroll_by(5);
        assert!(!app.stick);
        app.push_block(ChatBlock::User("x".into()));
        assert_eq!(
            app.scroll, 5,
            "без прилипания новые блоки скролл не сбрасывают"
        );
        app.scroll_back(100);
        assert_eq!(app.scroll, 0);
        assert!(app.stick, "на дне снова прилипаем");
    }

    #[test]
    fn key_q_quits_only_on_empty_input() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.handle_key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::NONE));
        assert!(app.should_quit, "q на пустом вводе — выход");

        let mut app = test_app();
        app.screen = Screen::Chat;
        app.input.insert_char('п');
        app.handle_key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::NONE));
        assert!(!app.should_quit, "q в непустом вводе — обычный символ");
        assert_eq!(app.input.text(), "пq");
    }

    #[test]
    fn first_frame_is_ready_to_type() {
        // Никакого «нажмите любую клавишу»: приложение сразу принимает ввод.
        let mut app = test_app();
        assert!(matches!(app.screen, Screen::Chat));
        app.handle_key(KeyEvent::new(KeyCode::Char('п'), KeyModifiers::NONE));
        assert_eq!(app.input.text(), "п");

        let mut app = test_app();
        app.handle_key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::NONE));
        assert!(app.should_quit, "q в пустом вводе — выход");
    }

    #[test]
    fn session_constructor_keeps_stub_provider() {
        let cfg = Arc::new(Config::default());
        let session = stub_session(&cfg);
        assert!(session.messages().is_empty());
    }

    /// Модалка `propose_options` с двумя вариантами (без рекомендации).
    fn open_test_ask(app: &mut App) -> tokio::sync::oneshot::Receiver<String> {
        let (reply_tx, reply_rx) = tokio::sync::oneshot::channel();
        app.handle_message(AppMessage::AskUser(AskRequest {
            question: "Какой брокер для событийной шины?".into(),
            options: vec![
                crate::tool::AskOption {
                    label: "Kafka".into(),
                    description: "масштаб, но тяжёлый".into(),
                },
                crate::tool::AskOption {
                    label: "NATS".into(),
                    description: "лёгкий, без хранения".into(),
                },
            ],
            recommended: None,
            reply: reply_tx,
        }));
        reply_rx
    }

    /// Модалка `propose_options` с `n` вариантами (без рекомендации).
    /// Нужна для проверки границы `1..=9` и навигации по хвосту длинного
    /// списка (у пикера `/resume` бывает до 12 пунктов).
    fn open_test_ask_n(app: &mut App, n: usize) -> tokio::sync::oneshot::Receiver<String> {
        let (reply_tx, reply_rx) = tokio::sync::oneshot::channel();
        let options = (0..n)
            .map(|i| crate::tool::AskOption {
                label: format!("opt-{i}"),
                description: String::new(),
            })
            .collect();
        app.handle_message(AppMessage::AskUser(AskRequest {
            question: "Выбор:".into(),
            options,
            recommended: None,
            reply: reply_tx,
        }));
        reply_rx
    }

    #[test]
    fn ask_modal_opens_navigates_and_enter_confirms() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let mut rx = open_test_ask(&mut app);
        assert!(app.ask.is_some(), "модалка открыта");
        assert_eq!(app.ask.as_ref().map(|a| a.selected), Some(0));
        app.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        assert_eq!(
            app.ask.as_ref().map(|a| a.selected),
            Some(1),
            "↓ двигает курсор"
        );
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.ask.is_none(), "после Enter модалка закрыта");
        let chosen = rx.try_recv().expect("ответ ушёл инструменту");
        assert_eq!(chosen, "NATS");
    }

    #[test]
    fn ask_modal_esc_declines_with_empty_answer() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let mut rx = open_test_ask(&mut app);
        app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.ask.is_none());
        assert!(!app.should_quit, "Esc в модалке — отказ, а не выход из TUI");
        let chosen = rx.try_recv().expect("ответ ушёл");
        assert_eq!(chosen, "", "отказ — пустая строка");
    }

    #[test]
    fn ask_modal_digit_quick_selects_and_recommended_preselects() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let (reply_tx, mut rx) = tokio::sync::oneshot::channel();
        app.handle_message(AppMessage::AskUser(AskRequest {
            question: "q".into(),
            options: vec![
                crate::tool::AskOption {
                    label: "A".into(),
                    description: String::new(),
                },
                crate::tool::AskOption {
                    label: "B".into(),
                    description: String::new(),
                },
            ],
            recommended: Some("B".into()),
            reply: reply_tx,
        }));
        assert_eq!(
            app.ask.as_ref().map(|a| a.selected),
            Some(1),
            "курсор на рекомендации"
        );
        app.handle_key(KeyEvent::new(KeyCode::Char('1'), KeyModifiers::NONE));
        assert!(app.ask.is_none());
        assert_eq!(
            rx.try_recv().expect("ответ"),
            "A",
            "цифра выбирает мгновенно"
        );
    }

    #[test]
    fn ask_modal_blocks_plain_typing_while_open() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let _rx = open_test_ask(&mut app);
        app.handle_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
        assert!(
            app.input.text().is_empty(),
            "обычный ввод заблокирован модалкой"
        );
        assert!(app.ask.is_some(), "нецифровая клавиша модалку не закрывает");
    }

    #[test]
    fn ask_modal_digit_out_of_range_does_not_answer_but_gives_feedback() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let mut rx = open_test_ask_n(&mut app, 3);
        app.handle_key(KeyEvent::new(KeyCode::Char('7'), KeyModifiers::NONE));
        assert!(app.ask.is_some(), "модалка не закрывается");
        assert_eq!(
            app.ask.as_ref().map(|a| a.selected),
            Some(0),
            "курсор остаётся консистентным"
        );
        assert!(rx.try_recv().is_err(), "ответ инструменту не отправлен");
        let toast = app
            .toast()
            .expect("клавиша не молчит — отклик в статус-баре");
        assert!(toast.text.contains('7'), "текст отклика: {}", toast.text);
        assert!(toast.text.contains('3'), "текст отклика: {}", toast.text);
    }

    #[test]
    fn ask_modal_zero_selects_nothing() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let mut rx = open_test_ask_n(&mut app, 3);
        app.handle_key(KeyEvent::new(KeyCode::Char('0'), KeyModifiers::NONE));
        assert!(
            app.ask.is_some(),
            "0 — не номер пункта, модалку не закрывает"
        );
        assert_eq!(app.ask.as_ref().map(|a| a.selected), Some(0));
        assert!(rx.try_recv().is_err(), "0 не отправляет ответ");
        assert!(
            app.input.text().is_empty(),
            "0 не печатается в строку ввода"
        );
    }

    #[test]
    fn ask_modal_zero_gives_feedback_like_other_dead_digits() {
        // «0» — не номер пункта (нумерация с 1), но раньше клавиша молчала,
        // хотя соседние «1»..«9» вне диапазона честно отвечают тостом.
        // Одинаковые по смыслу нажатия не имеют права вести себя по-разному.
        let mut app = test_app();
        app.screen = Screen::Chat;
        let mut rx = open_test_ask_n(&mut app, 3);
        app.handle_key(KeyEvent::new(KeyCode::Char('0'), KeyModifiers::NONE));
        assert!(app.ask.is_some(), "0 не закрывает модалку");
        assert_eq!(app.ask.as_ref().map(|a| a.selected), Some(0));
        assert!(rx.try_recv().is_err(), "0 не отправляет ответ");
        let toast = app.toast().expect("0 обязан дать отклик, как и «7»");
        assert!(toast.text.contains('0'), "текст отклика: {}", toast.text);
        assert!(toast.text.contains('3'), "текст отклика: {}", toast.text);
    }

    #[test]
    fn ask_modal_ninth_digit_selects_punkt_nine_of_twelve() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let mut rx = open_test_ask_n(&mut app, 12);
        app.handle_key(KeyEvent::new(KeyCode::Char('9'), KeyModifiers::NONE));
        assert!(app.ask.is_none(), "цифра выбирает мгновенно");
        assert_eq!(rx.try_recv().expect("ответ"), "opt-8", "девятый пункт");
    }

    #[test]
    fn ask_modal_page_keys_and_arrows_reach_tail_beyond_nine() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let _rx = open_test_ask_n(&mut app, 12);
        // Десятый пункт (индекс 9) — за пределами цифровых клавиш (их нет для
        // 10+ осознанно): достижим только навигацией. Девять «вниз» с начала.
        for _ in 0..9 {
            app.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        }
        assert_eq!(
            app.ask.as_ref().map(|a| a.selected),
            Some(9),
            "десятый пункт доступен стрелками"
        );
        // Шаг страницы — половина высоты тела модалки, которую записывает
        // рендер (`draw_ask` → `ask_viewport`); здесь кадра нет, поэтому задаём
        // окно явно. Константа 10 из хендлера убрана: она не знала терминала.
        app.ask_viewport = 20;
        let page = 20 / 2;
        // PgDn — страница вниз: 9 + 10 зажимается последним пунктом (11).
        app.handle_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        assert_eq!(app.ask.as_ref().map(|a| a.selected), Some(11));
        // PgUp — страница вверх: 11 − 10 = 1.
        app.handle_key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
        assert_eq!(app.ask.as_ref().map(|a| a.selected), Some(11 - page));
        app.handle_key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
        assert_eq!(app.ask.as_ref().map(|a| a.selected), Some(0));
        app.handle_key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
        assert_eq!(
            app.ask.as_ref().map(|a| a.selected),
            Some(0),
            "PgUp не уходит за верх списка"
        );
        app.handle_key(KeyEvent::new(KeyCode::End, KeyModifiers::NONE));
        assert_eq!(app.ask.as_ref().map(|a| a.selected), Some(11));
        assert!(app.ask.is_some(), "навигация не закрывает модалку");
    }

    #[test]
    fn viewer_toggles_and_pans_with_keys() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.handle_key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE));
        assert!(app.viewer.is_some(), "F4 открывает просмотрщик");
        app.handle_key(KeyEvent::new(KeyCode::Right, KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        let v = app.viewer.expect("открыт");
        assert_eq!((v.scroll_x, v.scroll_y), (8, 1), "→/↓ панорамируют");
        // Печать в просмотрщике не уходит в строку ввода.
        app.handle_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
        assert!(app.input.text().is_empty());
        // Смена вкладки внутри просмотрщика сбрасывает скролл.
        app.handle_key(KeyEvent::new(KeyCode::F(2), KeyModifiers::NONE));
        let v = app.viewer.expect("открыт");
        assert_eq!(
            (v.scroll_x, v.scroll_y),
            (0, 0),
            "скролл сброшен на новой вкладке"
        );
        assert!(matches!(app.right_tab(), RightTab::Rubric));
        app.handle_key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE));
        assert!(app.viewer.is_none(), "F4 закрывает");
    }

    #[test]
    fn f6_unhides_panel_and_loads_fleet_dashboard() {
        // Приложение с изолированным state_dir: дефолтный test_app смотрит в
        // настоящий ~/.arch-ml — в журналы пользователя тестами не пишем.
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut cfg0 = Config::default();
        cfg0.paths.state_dir = tmp.path().join("state");
        cfg0.paths.sessions_dir = tmp.path().join("sessions");
        let cfg = std::sync::Arc::new(cfg0);
        let session = stub_session(&cfg);
        let ctx = ToolContext::new(tmp.path().to_path_buf(), cfg);
        let mut app = App::new(Some(session), ctx, None, None, Caps::default());
        app.screen = Screen::Chat;
        app.right_visible = false;
        // Журнал прогона в изолированном state_dir.
        let fleet_dir = app.tool_ctx.config.paths.state_dir.join("fleet");
        std::fs::create_dir_all(&fleet_dir).expect("fleet dir");
        std::fs::write(
            fleet_dir.join("fpl-test.jsonl"),
            concat!(
                "{\"type\":\"run_started\",\"run_id\":\"fpl-test\",\"package\":\"demo\",\"n_items\":1,\"n_agents\":1,\"at\":\"t0\"}\n",
                "{\"type\":\"agent_started\",\"run_id\":\"fpl-test\",\"agent_id\":\"n0\",\"item_id\":\"node-1\",\"kind\":\"kimi-code\",\"at\":\"t1\"}\n",
            ),
        )
        .expect("write fleet log");
        app.handle_key(KeyEvent::new(KeyCode::F(6), KeyModifiers::NONE));
        assert!(app.right_visible, "F6 показывает скрытую панель");
        assert!(
            matches!(app.right_tab(), RightTab::Fleet),
            "F6 — вкладка Флот"
        );
        assert!(
            app.panels.fleet.contains("n0") && app.panels.fleet.contains("running"),
            "дашборд заполняется сразу (агент прогона со статусом): {}",
            app.panels.fleet
        );
        // F5 скрывает, F1 возвращает панель с Mermaid.
        app.handle_key(KeyEvent::new(KeyCode::F(5), KeyModifiers::NONE));
        assert!(!app.right_visible, "F5 скрывает");
        app.handle_key(KeyEvent::new(KeyCode::F(1), KeyModifiers::NONE));
        assert!(app.right_visible, "F1 тоже показывает панель");
        assert!(matches!(app.right_tab(), RightTab::Mermaid));
    }

    /// Приложение с изолированным `state_dir`: в отличие от `test_app` не
    /// смотрит в настоящий `~/.arch-ml` — журналы пользователя не трогаем.
    fn isolated_app(tmp: &tempfile::TempDir) -> App {
        let mut cfg0 = Config::default();
        cfg0.paths.state_dir = tmp.path().join("state");
        cfg0.paths.sessions_dir = tmp.path().join("sessions");
        let cfg = std::sync::Arc::new(cfg0);
        let session = stub_session(&cfg);
        let ctx = ToolContext::new(tmp.path().to_path_buf(), cfg);
        let mut app = App::new(Some(session), ctx, None, None, Caps::default());
        app.screen = Screen::Chat;
        app.right_visible = false;
        app
    }

    #[test]
    fn fleet_panel_reports_empty_state_when_no_journals() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut app = isolated_app(&tmp);
        let fleet_dir = app.tool_ctx.config.paths.state_dir.join("fleet");
        std::fs::create_dir_all(&fleet_dir).expect("fleet dir");
        app.handle_key(KeyEvent::new(KeyCode::F(6), KeyModifiers::NONE));
        assert!(
            app.panels.fleet.contains("Журналов прогонов нет"),
            "пустой каталог — явное сообщение вместо тишины: {}",
            app.panels.fleet
        );
        assert!(
            app.panels.fleet.contains(&fleet_dir.display().to_string()),
            "в сообщении — путь каталога флота: {}",
            app.panels.fleet
        );
    }

    #[test]
    fn fleet_panel_clears_when_journal_disappears() {
        // Регресс исходного дефекта: после удаления журналов панель сохраняла
        // прежний кадр (вместе с пульсом heartbeat по mtime удалённого файла).
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut app = isolated_app(&tmp);
        let fleet_dir = app.tool_ctx.config.paths.state_dir.join("fleet");
        std::fs::create_dir_all(&fleet_dir).expect("fleet dir");
        let log = fleet_dir.join("fpl-test.jsonl");
        std::fs::write(
            &log,
            concat!(
                "{\"type\":\"run_started\",\"run_id\":\"fpl-test\",\"package\":\"demo\",\"n_items\":1,\"n_agents\":1,\"at\":\"t0\"}\n",
                "{\"type\":\"plan_started\",\"run_id\":\"fpl-test\",\"plan_id\":\"fpl-test\",\"pattern\":\"pipeline\",\"plan_path\":\"plan.yaml\",\"plan_sha256\":\"deadbeef\",\"n_nodes\":1,\"n_waves\":1,\"at\":\"t1\"}\n",
                "{\"type\":\"agent_started\",\"run_id\":\"fpl-test\",\"agent_id\":\"n0\",\"item_id\":\"node-1\",\"kind\":\"kimi-code\",\"at\":\"t2\"}\n",
            ),
        )
        .expect("write fleet log");
        app.handle_key(KeyEvent::new(KeyCode::F(6), KeyModifiers::NONE));
        assert!(
            app.panels.fleet.contains("fpl-test") && app.panels.fleet.contains("n0"),
            "панель заполнена журналом: {}",
            app.panels.fleet
        );
        std::fs::remove_file(&log).expect("remove fleet log");
        // Живой путь обновления — тик активной вкладки «Флот» (16 × 120 мс).
        for _ in 0..16 {
            app.tick();
        }
        assert!(
            !app.panels.fleet.contains("fpl-test"),
            "прежний прогон не сохраняется: {}",
            app.panels.fleet
        );
        assert!(
            !app.panels.fleet.contains("n0"),
            "узлы прежнего прогона не сохраняются: {}",
            app.panels.fleet
        );
        assert!(
            app.panels.fleet.contains("Журналов прогонов нет"),
            "панель сообщает об отсутствии журналов: {}",
            app.panels.fleet
        );
    }

    #[test]
    fn fleet_panel_reports_unreadable_journal() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut app = isolated_app(&tmp);
        let fleet_dir = app.tool_ctx.config.paths.state_dir.join("fleet");
        std::fs::create_dir_all(&fleet_dir).expect("fleet dir");
        std::fs::write(fleet_dir.join("fpl-broken.jsonl"), "{ это не JSON\n")
            .expect("write broken log");
        app.handle_key(KeyEvent::new(KeyCode::F(6), KeyModifiers::NONE));
        assert!(
            app.panels.fleet.contains("не читается"),
            "битый журнал — явная ошибка: {}",
            app.panels.fleet
        );
        assert!(
            app.panels.fleet.contains("fpl-broken"),
            "в сообщении — имя файла: {}",
            app.panels.fleet
        );
    }

    #[test]
    fn f5_hides_and_shows_right_panel() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        assert!(app.right_visible);
        app.handle_key(KeyEvent::new(KeyCode::F(5), KeyModifiers::NONE));
        assert!(!app.right_visible, "F5 скрывает панель");
        app.handle_key(KeyEvent::new(KeyCode::F(5), KeyModifiers::NONE));
        assert!(app.right_visible, "F5 возвращает панель");
    }

    #[test]
    fn viewer_esc_closes_without_quitting_app() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.handle_key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.viewer.is_none(), "Esc — назад из просмотрщика");
        assert!(!app.should_quit, "приложение не выходит");
    }

    #[test]
    fn viewer_mouse_wheel_scrolls_viewer_not_chat() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.handle_key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE));
        let wheel = |kind, modifiers| crossterm::event::MouseEvent {
            kind,
            column: 5,
            row: 5,
            modifiers,
        };
        app.handle_mouse(wheel(
            crossterm::event::MouseEventKind::ScrollDown,
            KeyModifiers::NONE,
        ));
        app.handle_mouse(wheel(
            crossterm::event::MouseEventKind::ScrollDown,
            KeyModifiers::SHIFT,
        ));
        let v = app.viewer.expect("открыт");
        assert_eq!(v.scroll_y, 3, "колесо — вертикаль просмотрщика");
        assert_eq!(v.scroll_x, 8, "Shift+колесо — горизонталь");
        assert_eq!(app.scroll, 0, "скролл чата не тронут");
    }

    #[test]
    fn ctrl_f_opens_search_and_esc_closes() {
        let mut app = testing::test_app();
        assert!(app.search.is_none());
        assert_eq!(app.hint_ctx(), super::super::keymap::Ctx::ChatIdle);
        app.handle_key(KeyEvent::new(KeyCode::Char('f'), KeyModifiers::CONTROL));
        assert!(app.search.is_some(), "Ctrl+F открывает поиск");
        assert_eq!(app.hint_ctx(), super::super::keymap::Ctx::Search);
        app.handle_key(KeyEvent::new(KeyCode::Char('g'), KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Char('R'), KeyModifiers::SHIFT));
        assert_eq!(app.search.as_ref().unwrap().query, "gR");
        app.handle_key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        assert_eq!(app.search.as_ref().unwrap().query, "g");
        app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.search.is_none(), "Esc закрывает поиск");
        assert_eq!(app.hint_ctx(), super::super::keymap::Ctx::ChatIdle);
    }

    #[test]
    fn search_enter_cycles_matches_and_alt_enter_goes_back() {
        let mut app = testing::test_app();
        app.search = Some(DialogSearch {
            query: "x".into(),
            matches: vec![2, 5, 9],
            current: 0,
            dirty: false,
        });
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.search.as_ref().unwrap().current, 1);
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.search.as_ref().unwrap().current, 2);
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.search.as_ref().unwrap().current, 0, "цикл по кругу");
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT));
        assert_eq!(app.search.as_ref().unwrap().current, 2, "Alt+Enter — назад");
        assert!(
            app.search.as_ref().unwrap().dirty,
            "шаг требует перескока кадра"
        );
        // Пустой список совпадений — шаги безопасны.
        app.search = Some(DialogSearch {
            query: "z".into(),
            matches: Vec::new(),
            current: 0,
            dirty: false,
        });
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.search.as_ref().unwrap().current, 0);
    }

    #[test]
    fn question_mark_opens_help_and_esc_closes_it() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.handle_key(KeyEvent::new(KeyCode::Char('?'), KeyModifiers::NONE));
        assert!(app.help, "`?` открывает справку");
        app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(!app.help, "Esc закрывает справку");
        assert!(!app.should_quit, "Esc в справке не выходит из приложения");
    }

    #[test]
    fn question_mark_types_into_nonempty_input() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.input.set_text("что такое GRPO".into());
        app.handle_key(KeyEvent::new(KeyCode::Char('?'), KeyModifiers::NONE));
        assert!(!app.help, "при непустом вводе справка НЕ открывается");
        assert_eq!(
            app.input.text(),
            "что такое GRPO?",
            "`?` печатается как обычный символ — вопросы вводятся свободно"
        );
        // Ввод очистили — `?` снова справка.
        app.input.set_text(String::new());
        app.handle_key(KeyEvent::new(KeyCode::Char('?'), KeyModifiers::NONE));
        assert!(app.help, "при пустом вводе `?` — справка");
    }

    #[test]
    fn help_is_a_modal_layer_not_a_quit() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.handle_key(KeyEvent::new(KeyCode::Char('?'), KeyModifiers::NONE));
        // `q` внутри справки закрывает справку, а не приложение (конвенция
        // «в модале q — закрыть модал»).
        app.handle_key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::NONE));
        assert!(!app.help);
        assert!(!app.should_quit, "q в справке не выходит из приложения");
    }

    #[test]
    fn help_traps_typing_away_from_the_input() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.handle_key(KeyEvent::new(KeyCode::Char('?'), KeyModifiers::NONE));
        for c in ['п', 'р', 'и'] {
            app.handle_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        assert!(
            app.input.text().is_empty(),
            "фокус в модале заперт: ввод не должен набираться"
        );
    }

    #[test]
    fn help_scrolls_and_resets_on_reopen() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.handle_key(KeyEvent::new(KeyCode::Char('?'), KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        assert!(app.help_scroll() > 0, "PgDn листает справку");
        app.handle_key(KeyEvent::new(KeyCode::Char('?'), KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Char('?'), KeyModifiers::NONE));
        assert_eq!(app.help_scroll(), 0, "повторное открытие — с начала");
    }

    #[test]
    fn shift_tab_walks_tabs_backwards() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        assert_eq!(app.right_tab(), RightTab::Mermaid);
        app.handle_key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT));
        assert_eq!(
            app.right_tab(),
            RightTab::Subagents,
            "Shift+Tab — назад по циклу (Mermaid → последняя вкладка)"
        );
        app.handle_key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        assert_eq!(app.right_tab(), RightTab::Mermaid, "Tab — вперёд");
    }

    /// Дефект №3: рабочая ветка Shift+Tab была `KeyCode::Tab + SHIFT`, но
    /// crossterm без kitty-протокола отдаёт Shift+Tab как `BackTab` (своё
    /// значение `KeyCode`, модификаторов нет). Клавиша была мёртвой. Теперь
    /// `BackTab` — основной путь назад, а старая ветка остаётся совместимостью
    /// для терминалов с расширенными модификаторами.
    #[test]
    fn backtab_switches_to_previous_tab() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        assert_eq!(app.right_tab(), RightTab::Mermaid, "старт — первая вкладка");
        app.handle_key(KeyEvent::new(KeyCode::BackTab, KeyModifiers::NONE));
        assert_eq!(
            app.right_tab(),
            RightTab::Subagents,
            "BackTab — назад по циклу (Mermaid → последняя вкладка)"
        );
        // Обратно вперёд — тем же Tab.
        app.handle_key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        assert_eq!(app.right_tab(), RightTab::Mermaid);
    }

    /// `BackTab`, как и `Tab`, замыкает цикл из пяти вкладок и возвращает к
    /// исходной: ни одна вкладка не «застревает» на обратном ходу.
    #[test]
    fn backtab_cycles_through_all_five_tabs() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let start = app.right_tab();
        let mut seen = vec![start];
        for _ in 0..RightTab::ALL.len() {
            app.handle_key(KeyEvent::new(KeyCode::BackTab, KeyModifiers::NONE));
            seen.push(app.right_tab());
        }
        assert_eq!(seen.len(), 6, "пять шагов плюс возврат");
        assert_eq!(
            seen[5], start,
            "пятый BackTab возвращает к исходной вкладке"
        );
        // Ни одна вкладка не пропущена и не задета дважды: пять шагов назад
        // дают ровно пять разных вкладок (попарная проверка — `RightTab` без Ord).
        for i in 0..5 {
            for j in (i + 1)..5 {
                assert_ne!(seen[i], seen[j], "вкладка повторена: {seen:?}");
            }
        }
    }

    /// Дефект №2/#6: клавиши правой панели обязаны прокручивать в сторону,
    /// которую называют подписи. `right_scroll` — смещение ОТ НАЧАЛА (его
    /// отдаёт `Paragraph::scroll`), поэтому «вверх» — уменьшение. Раньше
    /// стоял `saturating_add` и PgUp/Ctrl+U уезжали в конец.
    #[test]
    fn right_panel_keys_scroll_in_labelled_direction() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.right_visible = true;
        app.set_right_viewport(10);
        app.set_right_max_scroll(100);
        assert_eq!(app.right_scroll(), 0, "старт — начало содержимого");

        app.handle_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        assert_eq!(app.right_scroll(), 10, "PgDn — страница К КОНЦУ");
        app.handle_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        assert_eq!(app.right_scroll(), 20);
        app.handle_key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
        assert_eq!(app.right_scroll(), 10, "PgUp — страница К НАЧАЛУ");
        app.handle_key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
        assert_eq!(app.right_scroll(), 0, "PgUp не уходит в минус");

        // Синонимы полстраницы (вьюпорт 10 → шаг 5): то же направление.
        app.handle_key(KeyEvent::new(KeyCode::Char('d'), KeyModifiers::CONTROL));
        assert_eq!(app.right_scroll(), 5, "Ctrl+D — полстраницы к концу");
        app.handle_key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL));
        assert_eq!(app.right_scroll(), 0, "Ctrl+U — полстраницы к началу");

        // g/G — конвенция: к началу/концу содержимого. Границу конца знает
        // рендер (right_max); здесь она выставлена явно, кадра нет.
        app.handle_key(KeyEvent::new(KeyCode::Char('G'), KeyModifiers::SHIFT));
        assert_eq!(app.right_scroll(), 100, "G — к концу содержимого");
        app.handle_key(KeyEvent::new(KeyCode::Char('g'), KeyModifiers::NONE));
        assert_eq!(app.right_scroll(), 0, "g — к началу содержимого");
    }

    /// Гард `g`/`G`: пока в поле есть текст, буква — это буква, а не команда
    /// (tui-design-principles, Input modes). Осознанный компромисс: сообщение,
    /// начинающееся с `g` при ПУСТОМ поле, отдаст первую букву навигации —
    /// тот же контракт, что у `q`/`?`.
    #[test]
    fn g_is_navigation_only_on_empty_input() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.right_visible = true;
        app.set_right_viewport(10);
        app.set_right_max_scroll(50);
        app.handle_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        let before = app.right_scroll();
        assert!(before > 0);

        // Непустой ввод: `g` печатается, прокрутка не трогается.
        app.handle_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Char('g'), KeyModifiers::NONE));
        assert_eq!(app.input.text(), "ag", "в режиме набора `g` — текст");
        assert_eq!(app.right_scroll(), before, "и прокрутки не касается");

        // Поле снова пусто — `g` снова навигация.
        app.handle_key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Char('g'), KeyModifiers::NONE));
        assert_eq!(app.right_scroll(), 0, "на пустом вводе `g` — к началу");
    }

    /// Focus-правило: панель скрыта (F5) — те же PgUp/PgDn не «умирают», а
    /// прокручивают диалог. Клавиша не должна терять фокус при исчезновении
    /// панели (tui-design-principles, Focus).
    #[test]
    fn page_keys_fall_back_to_dialog_when_panel_hidden() {
        let mut app = app_with_dialog();
        app.screen = Screen::Chat;
        app.right_visible = false;
        app.set_right_viewport(6);
        app.scroll = 0;
        let dialog_before = app.scroll;
        // У диалога `scroll` — смещение от ДНА, поэтому «вверх» (старые
        // строки) — это `PageUp` → `scroll_by`. Прокрутка не «умирает» вместе
        // с панелью: клавиша переадресуется диалогу.
        app.handle_key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
        assert!(
            app.scroll > dialog_before,
            "PageUp прокручивает диалог, когда панель скрыта"
        );
        assert_eq!(app.right_scroll(), 0, "правая панель не тронута");
    }

    /// Tab обходит все пять вкладок и возвращается к исходной — цикл замкнут.
    #[test]
    fn tab_cycles_through_all_five_tabs() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let start = app.right_tab();
        let mut seen = vec![start];
        for _ in 0..RightTab::ALL.len() {
            app.handle_key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
            seen.push(app.right_tab());
        }
        assert_eq!(seen.len(), 6, "пять шагов плюс возврат");
        assert_eq!(
            &seen[..5],
            &RightTab::ALL,
            "Tab идёт строго по RightTab::ALL"
        );
        assert_eq!(seen[5], start, "пятый шаг возвращает к исходной вкладке");
        // Обратный ход симметричен.
        for _ in 0..RightTab::ALL.len() {
            app.handle_key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT));
        }
        assert_eq!(app.right_tab(), start, "Shift+Tab возвращает туда же");
    }

    #[test]
    fn right_tab_all_matches_icon_count() {
        assert_eq!(RightTab::ALL.len(), 5, "пять вкладок — Mermaid…Субагенты");
        for g in [
            crate::tui::theme::Glyphs { unicode: true },
            crate::tui::theme::Glyphs { unicode: false },
        ] {
            assert_eq!(
                RightTab::ALL.len(),
                g.tab_icons().len(),
                "иконок столько же, сколько вкладок"
            );
        }
    }

    #[test]
    fn panels_cover_the_subagents_tab() {
        let mut p = Panels::default();
        assert!(
            p.content(RightTab::Subagents).is_empty(),
            "пустая вкладка — пустая строка (до refresh)"
        );
        p.subagents = "hr-1 [claude-code] done".into();
        assert_eq!(p.content(RightTab::Subagents), "hr-1 [claude-code] done");
        let ph = Panels::placeholder(RightTab::Subagents);
        assert!(
            ph.contains("subagent_run"),
            "заглушка зовёт инструмент: {ph}"
        );
        assert!(ph.contains("/agents"), "заглушка зовёт команду: {ph}");
    }

    #[test]
    fn f7_unhides_panel_and_loads_subagents_registry() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.right_visible = false;
        app.tool_ctx.subagents = Some(finished_registry());
        app.handle_key(KeyEvent::new(KeyCode::F(7), KeyModifiers::NONE));
        assert!(app.right_visible, "F7 показывает скрытую панель");
        assert!(
            matches!(app.right_tab(), RightTab::Subagents),
            "F7 — вкладка Субагенты"
        );
        assert!(
            app.panels.subagents.contains("hr-1"),
            "список заполняется сразу, без ожидания тика: {}",
            app.panels.subagents
        );
        assert!(
            app.panels.subagents.contains("done"),
            "статус задачи виден: {}",
            app.panels.subagents
        );
    }

    #[test]
    fn subagents_panel_reports_empty_registry_with_hint() {
        // Реестр есть, но задач нет — не пустая строка, а подсказка запуска.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.tool_ctx.subagents = Some(crate::subagent::SubagentRegistry::new());
        app.handle_key(KeyEvent::new(KeyCode::F(7), KeyModifiers::NONE));
        let text = app.panels.subagents.clone();
        assert!(!text.trim().is_empty(), "пустая вкладка запрещена");
        assert!(
            text.contains("subagent_run") && text.contains("/agents"),
            "подсказка запуска: {text}"
        );
        // Реестра нет вовсе — тоже осмысленный текст, не паника и не пусто.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.tool_ctx.subagents = None;
        app.handle_key(KeyEvent::new(KeyCode::F(7), KeyModifiers::NONE));
        assert!(
            !app.panels.subagents.trim().is_empty(),
            "нет реестра — тоже подсказка, а не пусто"
        );
    }

    #[test]
    fn subagents_tab_refreshes_on_tick() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.right_tab = RightTab::Subagents;
        app.tool_ctx.subagents = Some(crate::subagent::SubagentRegistry::new());
        app.panels.subagents.clear();
        for _ in 0..4 {
            app.tick();
        }
        assert!(
            !app.panels.subagents.trim().is_empty(),
            "на 4-м тике вкладка перечитала реестр"
        );
        assert!(
            app.needs_tick(),
            "активная вкладка субагентов требует тиков"
        );
    }

    #[test]
    fn help_ctx_switches_with_turn_state() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        assert_eq!(app.hint_ctx(), super::super::keymap::Ctx::ChatIdle);
        testing::set_thinking(&mut app, true);
        assert_eq!(app.hint_ctx(), super::super::keymap::Ctx::ChatBusy);
    }

    /// Свидетель состояния ask-модалки: открыта ли, где курсор, что сказано
    /// тостом. Сравнением «до/после» тест реестра видит любой отклик, включая
    /// закрытие модалки и ошибочный тост вместо движения курсора.
    fn ask_witness(app: &App) -> (bool, usize, Option<String>) {
        (
            app.ask.is_some(),
            app.ask.as_ref().map_or(usize::MAX, |a| a.selected),
            app.toast().map(|t| t.text.clone()),
        )
    }

    /// При открытой ask-модалке и строка состояния, и оверлей справки обязаны
    /// показывать клавиши МОДАЛКИ — независимо от того, занят чат или нет.
    ///
    /// Раньше `hint_ctx`/`help_ctx` шли через состояние чата: на занятом чате
    /// статус-бар обещал «Enter — в очередь» и «Esc — прервать», хотя те же
    /// клавиши `handle_ask_key` отправлял на выбор пункта и отказ. Справка
    /// `?` рассказывала про экран, который фокус не получает.
    #[test]
    fn ask_modal_is_the_top_layer_for_hint_and_help_contexts() {
        use crate::tui::keymap::Ctx;
        let mut app = test_app();
        app.screen = Screen::Chat;
        assert_eq!(app.hint_ctx(), Ctx::ChatIdle, "пустой чат — клавиши чата");
        assert_eq!(app.help_ctx(), Ctx::ChatIdle);

        let _rx = open_test_ask_n(&mut app, 2);
        assert_eq!(app.hint_ctx(), Ctx::Ask, "модалка перехватывает клавиши");
        assert_eq!(app.help_ctx(), Ctx::Ask, "справка описывает верхний слой");

        // Чат занят — модалка всё равно верхний слой (её клавиши уходят ей).
        testing::set_thinking(&mut app, true);
        assert_eq!(app.hint_ctx(), Ctx::Ask);
        assert_eq!(app.help_ctx(), Ctx::Ask);

        app.ask = None;
        assert_eq!(app.hint_ctx(), Ctx::ChatBusy, "без модалки — снова чат");
        assert_eq!(app.help_ctx(), Ctx::ChatBusy);
    }

    /// Реестр `Ctx::Ask` — обещание клавиш модалки, `handle_ask_key` — их
    /// реализация. Тест гоняет по свежему приложению КАЖДЫЙ код реестра и
    /// требует, чтобы нажатие что-то сделало, а клавиша вне реестра (и вне
    /// единственного задокументированного исключения) — не трогала модалку.
    ///
    /// Ловит обе поломки разом: «подсказка обещает клавишу без обработчика» и
    /// «обработчик умеет клавишу, о которой нигде не сказано».
    #[test]
    fn ask_registry_agrees_with_handle_ask_key() {
        use crate::tui::keymap::{self, Ctx};

        let event = |code: &str| -> KeyEvent {
            let key = match code {
                "up" => KeyCode::Up,
                "down" => KeyCode::Down,
                "pageup" => KeyCode::PageUp,
                "pagedown" => KeyCode::PageDown,
                "home" => KeyCode::Home,
                "end" => KeyCode::End,
                "enter" => KeyCode::Enter,
                "esc" => KeyCode::Esc,
                one if one.chars().count() == 1 => KeyCode::Char(one.chars().next().unwrap()),
                other => panic!("тест не знает клавиши реестра {other:?}"),
            };
            KeyEvent::new(key, KeyModifiers::NONE)
        };

        let mut advertised = keymap::codes(Ctx::Ask);
        // Единственная обрабатываемая, но не обещанная клавиша: «0» — не номер
        // пункта (нумерация с 1), отвечает тостом об ошибке. Обещать в
        // подсказке нечего: действия за ней нет, только отрицание.
        advertised.push("0");

        for code in &advertised {
            let mut app = test_app();
            app.screen = Screen::Chat;
            let _rx = open_test_ask_n(&mut app, 12);
            app.ask.as_mut().expect("модалка открыта").selected = 6;
            let before = ask_witness(&app);
            app.handle_key(event(code));
            assert_ne!(
                before,
                ask_witness(&app),
                "клавиша {code:?} объявлена в реестре (или в исключении), но \
                 `handle_ask_key` на неё не реагирует"
            );
        }

        // Обратная сторона: клавиша, которой нет ни в реестре, ни в исключении,
        // обязана молчать. Иначе модалка «в тихую» умеет недокументированное.
        for key in [
            KeyCode::Char('x'),
            KeyCode::Tab,
            KeyCode::Backspace,
            KeyCode::Left,
            KeyCode::Right,
        ] {
            let mut app = test_app();
            app.screen = Screen::Chat;
            let _rx = open_test_ask_n(&mut app, 12);
            app.ask.as_mut().expect("модалка открыта").selected = 6;
            let before = ask_witness(&app);
            app.handle_key(KeyEvent::new(key, KeyModifiers::NONE));
            assert_eq!(
                before,
                ask_witness(&app),
                "клавиша {key:?} меняет модалку, но её нет в реестре"
            );
        }
    }

    #[test]
    fn esc_keeps_the_draft_instead_of_quitting() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        for c in ['п', 'л', 'а', 'н'] {
            app.handle_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(!app.should_quit, "Esc не выходит с неотправленным текстом");
        assert!(app.input.text().is_empty(), "поле очищено");
        assert_eq!(app.draft(), Some("план"), "черновик сохранён");
        assert!(app.toast().is_some(), "пользователю сказали, что произошло");
    }

    #[test]
    fn esc_brings_the_draft_back() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        for c in ['а', 'д', 'р'] {
            app.handle_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert_eq!(app.input.text(), "адр", "черновик вернулся в поле");
        assert!(app.draft().is_none());
        assert!(!app.should_quit, "возврат черновика — не выход");
    }

    #[test]
    fn esc_quits_only_when_nothing_to_lose() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.should_quit, "пустое поле и пустой слот — выход по Esc");
    }

    #[test]
    fn esc_still_interrupts_a_running_turn_first() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        testing::set_thinking(&mut app, true);
        app.handle_key(KeyEvent::new(KeyCode::Char('а'), KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(!app.should_quit, "Esc во время хода — прерывание, не выход");
        assert_eq!(app.input.text(), "а", "текст при прерывании не трогаем");
        assert!(app.draft().is_none(), "прерывание хода не трогает черновик");
    }

    #[test]
    fn toast_expires_but_errors_stay() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.set_toast(Toast::ok("готово"));
        assert!(app.toast().is_some());
        assert!(app.needs_tick(), "живой тост требует тиков для затухания");
        for _ in 0..40 {
            app.tick();
        }
        assert!(app.toast().is_none(), "успех гаснет сам");

        app.set_toast(Toast::err("не вышло"));
        for _ in 0..200 {
            app.tick();
        }
        assert!(app.toast().is_some(), "ошибка ждёт действия пользователя");
    }

    #[test]
    fn copy_result_becomes_a_toast_not_a_permanent_note() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.blocks
            .push(ChatBlock::Assistant("строка для копирования".into()));
        let mut terminal =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(100, 24)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        // Выделяем первую строку диалога и завершаем выделение.
        app.dialog_inner = Some(ratatui::layout::Rect {
            x: 0,
            y: 0,
            width: 50,
            height: 10,
        });
        app.selection = Some(((1, 1), (1, 10)));
        app.handle_mouse(crossterm::event::MouseEvent {
            kind: crossterm::event::MouseEventKind::Up(crossterm::event::MouseButton::Left),
            column: 10,
            row: 1,
            modifiers: KeyModifiers::NONE,
        });
        assert!(app.toast().is_some(), "результат копирования — тост");
    }
}
