//! Стартовая заставка-интро: «живая сессия» ≈10 с — сплэш-логотип каскадом,
//! печатающийся запрос, тики вызовов инструментов, ответ архитектора и
//! mermaid-схема, собирающаяся по узлам (как на демо-кадрах README).
//! Пропуск — любая клавиша; отключается `--no-animation` / `[tui] animation`.
//! Реиграция из чата — `/intro`.

use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};

use super::app::App;
use super::theme::Theme;
use crate::assets;

/// Тик интро совпадает с тиком спиннера (120 мс, см. `SPINNER_INTERVAL` в tui.rs).
/// Длительность фазы сплэша — 15 тиков ≈ 1,8 с.
const SPLASH_TICKS: u32 = 15;
/// Тик появления метки «вы» и старта печати запроса.
const TYPE_START: u32 = 17;
/// Символов запроса, допечатываемых за тик (печать без ожидания модели).
const TYPE_PER_TICK: usize = 5;
/// Тик появления правой панели mermaid.
const PANEL_AT: u32 = 36;
/// Тик появления блока очереди.
const QUEUE_AT: u32 = 58;
/// Тик появления статус-бара.
const STATUS_AT: u32 = 62;
/// Полная длительность сценария: на этом тике интро завершается само.
const END_TICKS: u32 = 82;

/// Запрос демо-сессии (контент AI/ML-редакции — MoE-графтинг).
const USER_LINE: &str = "спроектируй MoE-графтинг из двух dense-моделей: эксперты и роутер";
/// Тип строки сценария — определяет стиль при рендере.
#[derive(Clone, Copy)]
enum StepKind {
    /// Вызов инструмента (зелёная галка + текст).
    Tool,
    /// Мелкая сноска-итог (результат инструмента/скоринга).
    Note,
    /// Заголовок ответа архитектора.
    AnswerHead,
    /// Строка ответа архитектора.
    Answer,
}
/// Шаг сценария: строка появляется на тике `at`.
struct Step {
    at: u32,
    kind: StepKind,
    text: &'static str,
}
/// Шаги диалога в порядке появления на экране.
const STEPS: &[Step] = &[
    Step {
        at: 31,
        kind: StepKind::Tool,
        text: "skill_load llm-grafting",
    },
    Step {
        at: 33,
        kind: StepKind::Tool,
        text: "kb_search «MoE роутер load balancing»",
    },
    Step {
        at: 35,
        kind: StepKind::Tool,
        text: "concept_search «GRPO»",
    },
    Step {
        at: 36,
        kind: StepKind::Note,
        text: "карточка group_relative_policy_optimization · 12 алиасов · 514 соседей",
    },
    Step {
        at: 39,
        kind: StepKind::AnswerHead,
        text: "Скелет MoE-графтинга",
    },
    Step {
        at: 41,
        kind: StepKind::Answer,
        text: "• Эксперты: FFN-блоки двух доноров → 8 экспертов",
    },
    Step {
        at: 43,
        kind: StepKind::Answer,
        text: "• Роутер: top-2 с aux-loss-free балансировкой",
    },
    Step {
        at: 45,
        kind: StepKind::Answer,
        text: "• Gate: инициализация из центроидов доноров",
    },
    Step {
        at: 47,
        kind: StepKind::Answer,
        text: "• Решения — в ADR-001…003 · SPINE-ML (ML-05, ML-06)",
    },
    Step {
        at: 50,
        kind: StepKind::Tool,
        text: "mermaid_render flowchart TD · 8 узлов",
    },
    Step {
        at: 53,
        kind: StepKind::Tool,
        text: "control_score --trigger pretrain+critical",
    },
    Step {
        at: 55,
        kind: StepKind::Note,
        text: "Score 12 → маршрут Critical · preflight обязателен до аренды GPU",
    },
];

/// Узел mermaid-панели: одиночная рамка или ряд из трёх рамок (эксперты).
enum Node {
    One(u32, &'static str),
    Row(u32, [&'static str; 3]),
}
/// Узлы правой панели в порядке сборки схемы.
const NODES: &[Node] = &[
    Node::One(40, "Задача: MoE-графтинг"),
    Node::One(44, "Роутер top-2 · aux-loss-free"),
    Node::Row(48, ["Эксперт 1", "Эксперт 2", "… 8"]),
    Node::One(53, "Gate из центроидов доноров"),
    Node::One(58, "ADR-001…003 · SPINE-ML"),
];
/// Тик появления сноски под схемой.
const MERMAID_NOTE_AT: u32 = 62;

/// Строки блока очереди (второй акт сценария — как в живом TUI).
const QUEUE_LINES: [&str; 2] = [
    "▶ 1. а теперь датасет: дедупликация и утечка теста",
    "• 2. потом /handoff claude-code ./moe-graft ↵",
];
/// Модель и индикатор контекста в демо-статусе.
const STATUS_MODEL: &str = "deepseek:v4-flash";
const STATUS_GAUGE: &str = "◆ 61.4k/1.0M ▰▰▱▱▱ 6%";

/// Состояние заставки: число тиков с момента старта.
#[derive(Default)]
pub(crate) struct Intro {
    ticks: u32,
}

impl Intro {
    /// Новая заставка с первого тика.
    pub(crate) fn new() -> Self {
        Self::default()
    }

    /// Продвигает сценарий на тик. `false` — сценарий доигран, App снимает интро.
    pub(crate) fn advance(&mut self) -> bool {
        self.ticks += 1;
        self.ticks < END_TICKS
    }

    /// Текущий тик (для рендера и тестов).
    pub(crate) fn ticks(&self) -> u32 {
        self.ticks
    }
}

/// Длина напечатанной части запроса на тике `ticks` (0 до старта печати).
fn typed_len(ticks: u32) -> usize {
    let full = USER_LINE.chars().count();
    if ticks <= TYPE_START {
        return 0;
    }
    ((ticks - TYPE_START) as usize * TYPE_PER_TICK).min(full)
}

/// Строки диалога, видимые на тике `ticks` (метка «вы», запрос, шаги сценария).
fn dialog_lines(ticks: u32, theme: &Theme, ok: &str) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    lines.push(Line::from(vec![
        Span::styled("● ", theme.heading()),
        Span::styled("вы", theme.heading()),
    ]));
    let typed: String = USER_LINE.chars().take(typed_len(ticks)).collect();
    let full = USER_LINE.chars().count();
    if typed_len(ticks) < full {
        lines.push(Line::from(vec![
            Span::styled(typed, theme.base()),
            Span::styled("▌", theme.heading()),
        ]));
    } else {
        lines.push(Line::from(Span::styled(typed, theme.base())));
    }
    for step in STEPS.iter().filter(|s| s.at <= ticks) {
        let line = match step.kind {
            StepKind::Tool => Line::from(vec![
                Span::styled(format!("{ok} "), theme.art()),
                Span::styled(step.text, theme.muted()),
            ]),
            StepKind::Note => Line::from(Span::styled(format!("  {}", step.text), theme.muted())),
            StepKind::AnswerHead => Line::from(Span::styled(step.text, theme.accent())),
            StepKind::Answer => Line::from(Span::styled(step.text, theme.base())),
        };
        lines.push(line);
    }
    lines
}

/// Полный кадр заставки (сплэш либо демо-сессия с панелью mermaid).
pub(super) fn draw(f: &mut Frame, app: &App) {
    let theme = &app.theme;
    let area = f.area();
    let ticks = app.intro.as_ref().map_or(0, Intro::ticks);
    // Подсказка о пропуске — всегда в правом верхнем углу.
    let hint = Paragraph::new("любая клавиша — пропустить · skip: any key")
        .style(theme.muted())
        .alignment(Alignment::Right);
    f.render_widget(hint, Rect { height: 1, ..area });
    if ticks < TYPE_START {
        draw_splash(f, area, theme, ticks);
    } else {
        draw_session(f, area, theme, ticks);
    }
}

/// Фаза сплэша: логотип открывается построчно, затем издание и подпись.
fn draw_splash(f: &mut Frame, area: Rect, theme: &Theme, ticks: u32) {
    let logo: Vec<&str> = assets::BANNER.lines().collect();
    // Первые две строки логотипа видны сразу (кадр не открывается пустым),
    // дальше — построчное раскрытие по тику.
    let shown = (ticks as usize + 2).min(logo.len());
    // Центрирование по вертикали: логотип + издание + подпись ≈ 16 строк.
    let top = area.height.saturating_sub(16) / 2;
    let mut lines: Vec<Line> = logo[..shown]
        .iter()
        .map(|l| Line::from(Span::styled((*l).to_string(), theme.heading())))
        .collect();
    if ticks >= SPLASH_TICKS.saturating_sub(2) {
        lines.push(Line::default());
        lines.push(Line::from(Span::styled(
            "A I / M L   E D I T I O N".to_string(),
            theme.accent(),
        )));
    }
    if ticks >= SPLASH_TICKS {
        lines.push(Line::from(Span::styled(
            "solution-архитектор AI/ML · ADR · spine · handoff".to_string(),
            theme.muted(),
        )));
    }
    let block = Paragraph::new(lines);
    f.render_widget(
        block,
        Rect {
            y: area.y + top,
            ..area
        },
    );
}

/// Фаза демо-сессии: диалог слева, собирающаяся mermaid-схема справа.
fn draw_session(f: &mut Frame, area: Rect, theme: &Theme, ticks: u32) {
    let (dialog, panel) = if area.width >= 100 {
        let chunks =
            Layout::horizontal([Constraint::Ratio(2, 3), Constraint::Ratio(1, 3)]).split(area);
        (chunks[0], Some(chunks[1]))
    } else {
        (area, None)
    };
    let ok = theme.glyphs.ok().to_string();
    let lines = dialog_lines(ticks, theme, &ok);

    let mut constraints = vec![Constraint::Min(3)];
    if ticks >= QUEUE_AT {
        constraints.push(Constraint::Length(4));
    }
    if ticks >= STATUS_AT {
        constraints.push(Constraint::Length(3));
    }
    let chunks = Layout::vertical(constraints).split(dialog);
    f.render_widget(Paragraph::new(lines), chunks[0]);

    let mut idx = 1;
    if ticks >= QUEUE_AT {
        let queue = Paragraph::new(vec![
            Line::from(Span::styled("▼ очередь · 2", theme.muted())),
            Line::from(Span::styled(QUEUE_LINES[0], theme.art())),
            Line::from(Span::styled(QUEUE_LINES[1], theme.muted())),
        ])
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(theme.purple()),
        );
        f.render_widget(queue, chunks[idx]);
        idx += 1;
    }
    if ticks >= STATUS_AT {
        let status = Paragraph::new(Line::from(vec![
            Span::styled(format!(" {STATUS_MODEL} "), theme.badge()),
            Span::styled(format!("  {STATUS_GAUGE}"), theme.muted()),
        ]))
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(theme.border()),
        );
        f.render_widget(status, chunks[idx]);
    }
    if let Some(panel) = panel.filter(|_| ticks >= PANEL_AT) {
        draw_mermaid(f, panel, theme, ticks);
    }
}

/// Правая панель: схема mermaid, собирающаяся по узлам со стрелками.
fn draw_mermaid(f: &mut Frame, area: Rect, theme: &Theme, ticks: u32) {
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(theme.border())
        .title(Span::styled(" ◇ Mermaid · живой рендер ", theme.accent()));
    let inner = block.inner(area);
    f.render_widget(block, area);

    let visible: Vec<&Node> = NODES
        .iter()
        .filter(|n| match n {
            Node::One(at, _) | Node::Row(at, _) => *at <= ticks,
        })
        .collect();
    // Каждому узлу — 3 строки (рамка с текстом), между узлами — строка стрелки.
    let mut constraints: Vec<Constraint> = Vec::new();
    for (i, _) in visible.iter().enumerate() {
        if i > 0 {
            constraints.push(Constraint::Length(1));
        }
        constraints.push(Constraint::Length(3));
    }
    constraints.push(Constraint::Min(1));
    let chunks = Layout::vertical(constraints).split(inner);

    for (i, node) in visible.iter().enumerate() {
        let chunk = chunks[i * 2];
        match node {
            Node::One(_, text) => {
                let w = node_box((*text).to_string(), theme);
                f.render_widget(w, chunk);
            }
            Node::Row(_, cells) => {
                let cols = Layout::horizontal([
                    Constraint::Ratio(1, 3),
                    Constraint::Ratio(1, 3),
                    Constraint::Ratio(1, 3),
                ])
                .split(chunk);
                for (j, cell) in cells.iter().enumerate() {
                    f.render_widget(node_box((*cell).to_string(), theme), cols[j]);
                }
            }
        }
        if i + 1 < visible.len() {
            let arrow = Paragraph::new("▼")
                .style(theme.art())
                .alignment(Alignment::Center);
            f.render_widget(arrow, chunks[i * 2 + 1]);
        }
    }
    if ticks >= MERMAID_NOTE_AT {
        let note = Paragraph::new(format!("{} узлов · mermaid_render", NODES.len() + 3))
            .style(theme.muted())
            .alignment(Alignment::Center);
        let bottom = chunks[chunks.len() - 1];
        f.render_widget(
            note,
            Rect {
                height: 1,
                ..bottom
            },
        );
    }
}

/// Рамка узла схемы (стиль зелёного арта, как у настоящих mermaid-рендеров).
fn node_box(text: String, theme: &Theme) -> Paragraph<'static> {
    Paragraph::new(text)
        .style(theme.art())
        .alignment(Alignment::Center)
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(theme.art()),
        )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tui::app::testing::test_app;
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;

    #[test]
    fn typewriter_grows_and_caps_at_full_line() {
        assert_eq!(typed_len(0), 0);
        assert_eq!(typed_len(TYPE_START), 0);
        let full = USER_LINE.chars().count();
        assert!(typed_len(TYPE_START + 3) > 0);
        assert_eq!(typed_len(END_TICKS), full);
    }

    #[test]
    fn steps_appear_in_scenario_order() {
        let theme = Theme::for_caps(&crate::tui::caps::Caps::default());
        let early = dialog_lines(31, &theme, "✓");
        let text31 = format!("{early:?}");
        assert!(
            text31.contains("skill_load"),
            "на 31 тике ждём skill_load: {text31}"
        );
        assert!(
            !text31.contains("concept_search"),
            "на 31 тике concept_search рано"
        );
        let later = dialog_lines(50, &theme, "✓");
        let text50 = format!("{later:?}");
        assert!(text50.contains("concept_search"));
        assert!(
            text50.contains("mermaid_render"),
            "на 50 тике ждём mermaid_render"
        );
    }

    #[test]
    fn advance_finishes_at_end_ticks() {
        let mut intro = Intro::new();
        for _ in 0..END_TICKS - 1 {
            assert!(intro.advance());
        }
        assert!(!intro.advance(), "на END_TICKS сценарий обязан завершиться");
    }

    #[test]
    fn any_key_skips_intro() {
        let mut app = test_app();
        app.start_intro();
        assert!(app.intro.is_some());
        app.handle_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
        assert!(app.intro.is_none(), "любая клавиша снимает интро");
    }

    #[test]
    fn intro_forces_ticks_and_render_smoke() {
        let mut app = test_app();
        app.start_intro();
        assert!(app.needs_tick(), "интро держит цикл тиков");
        let mut terminal = Terminal::new(TestBackend::new(140, 44)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw splash");
        let splash = buffer_text(&terminal);
        assert!(splash.contains("█████╗"), "сплэш без логотипа:\n{splash}");

        for _ in 0..40 {
            app.tick();
        }
        terminal.draw(|f| app.render(f)).expect("draw mid");
        let mid = buffer_text(&terminal);
        assert!(
            mid.contains("skill_load"),
            "к 40 тику ждём tool-строку:\n{mid}"
        );
        assert!(mid.contains("Mermaid"), "к 40 тику ждём панель:\n{mid}");

        for _ in 0..26 {
            app.tick();
        }
        terminal.draw(|f| app.render(f)).expect("draw late");
        let late = buffer_text(&terminal);
        assert!(late.contains("очередь"), "к финалу ждём очередь:\n{late}");
        assert!(late.contains(STATUS_MODEL), "к финалу ждём статус:\n{late}");
    }

    #[test]
    fn scenario_ends_into_plain_chat() {
        let mut app = test_app();
        app.start_intro();
        for _ in 0..END_TICKS + 2 {
            app.tick();
        }
        assert!(app.intro.is_none(), "после сценария интро снято");
    }

    /// Текст буфера TestBackend одной строкой (как в render.rs::tests).
    fn buffer_text(term: &Terminal<TestBackend>) -> String {
        term.backend()
            .buffer()
            .content()
            .iter()
            .map(|c| c.symbol())
            .collect()
    }
}
