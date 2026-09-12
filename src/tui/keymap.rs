//! Реестр клавиш — единый источник строки подсказок и оверлея `?`.
//!
//! Рассинхрон «код умеет одно, подсказки говорят другое» — самая частая
//! болезнь TUI. Поэтому таблица [`BINDINGS`] объявляет каждое действие один
//! раз, а строка подсказок ([`hints`]) и справка ([`sections`]) собираются из
//! неё. Новая клавиша без записи в реестре не попадёт ни в подсказку, ни в
//! справку — и это заметно сразу, а не через полгода.

use ratatui::style::{Modifier, Style};
use ratatui::text::Span;

use super::theme::Theme;

/// Контекст: на каком экране/в каком состоянии действует биндинг.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Ctx {
    /// Доступно всегда (`q`, `Ctrl+C`, `?`).
    Global,
    /// Чат в простое: поле ввода активно.
    ChatIdle,
    /// Чат во время хода модели: ввод не блокирован, но Enter — в очередь.
    ChatBusy,
    /// Модальная панель выбора (`propose_options`, пикер модели/сессии).
    Ask,
    /// Полноэкранный просмотрщик вкладки (F4).
    Viewer,
    /// Оверлей справки.
    Help,
    /// Активный поиск по диалогу (Ctrl+F).
    Search,
}

/// Раздел справки: группировка по задаче, а не по клавише.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Group {
    /// Навигация по диалогу.
    Navigation,
    /// Ввод и отправка.
    Typing,
    /// Вкладки, панели, просмотрщик.
    Panels,
    /// Сессия, модель, выход.
    Session,
    /// Поиск по диалогу.
    Search,
}

impl Group {
    /// Заголовок раздела в справке.
    pub(crate) fn title(self) -> &'static str {
        match self {
            Self::Navigation => "НАВИГАЦИЯ",
            Self::Typing => "ВВОД",
            Self::Panels => "ВКЛАДКИ И ПАНЕЛИ",
            Self::Session => "СЕССИЯ И ВЫХОД",
            Self::Search => "ПОИСК ПО ДИАЛОГУ",
        }
    }
}

/// Одно действие интерфейса.
pub(crate) struct Binding {
    /// Контекст действия.
    pub(crate) ctx: Ctx,
    /// Как клавиши называются в интерфейсе (`↑/↓`, `Ctrl+J`, `F1–F3,F6`).
    pub(crate) keys: &'static str,
    /// Нормализованные имена клавиш — для проверки конфликтов в тестах
    /// (`no_key_collisions_inside_a_context`); в рантайме не читается.
    #[cfg_attr(
        not(test),
        allow(dead_code, reason = "поле читает только тест конфликтов клавиш")
    )]
    pub(crate) codes: &'static [&'static str],
    /// Короткая подпись для строки подсказок.
    pub(crate) label: &'static str,
    /// Полное описание для справки.
    pub(crate) desc: &'static str,
    /// Раздел справки.
    pub(crate) group: Group,
    /// Показывать ли в строке подсказок.
    pub(crate) hint: bool,
    /// Приоритет в подсказке (меньше — левее). Уходят с конца.
    pub(crate) priority: u8,
    /// Не выкидывать из подсказки даже в узком окне.
    pub(crate) keep: bool,
}

/// Все действия интерфейса. Порядок — по разделам справки.
pub(crate) const BINDINGS: &[Binding] = &[
    // --- Навигация ---
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "PgUp/PgDn",
        codes: &["pageup", "pagedown"],
        label: "прокрутка",
        desc: "Прокрутка диалога (работает и во время хода модели)",
        group: Group::Navigation,
        hint: true,
        priority: 20,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "↑/↓",
        codes: &["up", "down"],
        label: "история",
        desc: "По строкам ввода; на крайней строке — история (до 100 записей)",
        group: Group::Navigation,
        hint: false,
        priority: 50,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "Home/End",
        codes: &["home", "end"],
        label: "строка",
        desc: "Начало и конец строки ввода",
        group: Group::Navigation,
        hint: false,
        priority: 51,
        keep: false,
    },
    Binding {
        ctx: Ctx::Viewer,
        keys: "↑/↓",
        codes: &["up", "down"],
        label: "строка",
        desc: "Пролистать содержимое вкладки на строку",
        group: Group::Navigation,
        hint: false,
        priority: 10,
        keep: false,
    },
    Binding {
        ctx: Ctx::Viewer,
        keys: "←/→",
        codes: &["left", "right"],
        label: "панорама",
        desc: "Горизонтальная панорама широкого арта (шаг 8 колонок)",
        group: Group::Navigation,
        hint: false,
        priority: 11,
        keep: false,
    },
    Binding {
        ctx: Ctx::Viewer,
        keys: "PgUp/PgDn",
        codes: &["pageup", "pagedown"],
        label: "экран",
        desc: "Пролистать содержимое вкладки на экран",
        group: Group::Navigation,
        hint: false,
        priority: 12,
        keep: false,
    },
    Binding {
        ctx: Ctx::Viewer,
        keys: "Home",
        codes: &["home"],
        label: "в начало",
        desc: "Вернуться в левый верхний угол содержимого",
        group: Group::Navigation,
        hint: false,
        priority: 13,
        keep: false,
    },
    // --- Ввод ---
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "Enter",
        codes: &["enter"],
        label: "отправить",
        desc: "Отправить набранное: слэш-команду — сразу, остальное — модели",
        group: Group::Typing,
        hint: true,
        priority: 10,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatBusy,
        keys: "Enter",
        codes: &["enter"],
        label: "в очередь",
        desc: "Поставить набранное в очередь (старт после текущего хода)",
        group: Group::Typing,
        hint: true,
        priority: 11,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatBusy,
        keys: "Alt+Enter",
        codes: &["alt+enter"],
        label: "срочно",
        desc: "Прервать ход и вклинить набранное первым (без Alt — префикс `!!`)",
        group: Group::Typing,
        hint: true,
        priority: 12,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "Alt+Enter",
        codes: &["alt+enter"],
        label: "перенос",
        desc: "Перевод строки (как `Shift+Enter`; `Ctrl+J` работает в любом терминале)",
        group: Group::Typing,
        hint: false,
        priority: 60,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "Ctrl+J",
        codes: &["ctrl+j"],
        label: "перенос",
        desc: "Перевод строки в поле ввода",
        group: Group::Typing,
        hint: false,
        priority: 61,
        keep: false,
    },
    // --- Панели ---
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "Tab/Shift+Tab",
        codes: &["tab", "shift+tab"],
        label: "вкладки",
        desc: "Следующая / предыдущая вкладка панели; Tab сначала дополняет слэш-команду",
        group: Group::Panels,
        hint: true,
        priority: 30,
        keep: false,
    },
    // --- Поиск по диалогу ---
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "Ctrl+F",
        codes: &["ctrl+f"],
        label: "поиск",
        desc: "Поиск по диалогу: строка запроса, совпадения подсвечиваются",
        group: Group::Navigation,
        hint: true,
        priority: 31,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatBusy,
        keys: "Ctrl+F",
        codes: &["ctrl+f"],
        label: "поиск",
        desc: "Поиск по диалогу (работает и во время хода модели)",
        group: Group::Navigation,
        hint: false,
        priority: 71,
        keep: false,
    },
    Binding {
        ctx: Ctx::Search,
        keys: "буквы",
        codes: &["char"],
        label: "запрос",
        desc: "Печатайте запрос: совпадения подсвечиваются и нумеруются",
        group: Group::Search,
        hint: false,
        priority: 10,
        keep: false,
    },
    Binding {
        ctx: Ctx::Search,
        keys: "Enter",
        codes: &["enter"],
        label: "далее",
        desc: "К следующему совпадению (по кругу)",
        group: Group::Search,
        hint: true,
        priority: 11,
        keep: true,
    },
    Binding {
        ctx: Ctx::Search,
        keys: "Alt+Enter",
        codes: &["alt+enter"],
        label: "назад",
        desc: "К предыдущему совпадению",
        group: Group::Search,
        hint: true,
        priority: 12,
        keep: false,
    },
    Binding {
        ctx: Ctx::Search,
        keys: "Esc",
        codes: &["esc"],
        label: "закрыть",
        desc: "Закрыть строку поиска (подсветка и позиция остаются)",
        group: Group::Search,
        hint: true,
        priority: 13,
        keep: true,
    },
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "F1–F3, F6",
        codes: &["f1", "f2", "f3", "f6"],
        label: "вкладка",
        desc: "Mermaid · Рубрика · Знания · Флот — прыжок в конкретную вкладку (показывает панель, если скрыта F5)",
        group: Group::Panels,
        hint: false,
        priority: 70,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "F4",
        codes: &["f4"],
        label: "весь экран",
        desc: "Активная вкладка на весь экран (Esc или F4 — назад)",
        group: Group::Panels,
        hint: true,
        priority: 40,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "F5",
        codes: &["f5"],
        label: "панель",
        desc: "Показать/скрыть правую панель (удобно в узком окне)",
        group: Group::Panels,
        hint: false,
        priority: 71,
        keep: false,
    },
    Binding {
        ctx: Ctx::Viewer,
        keys: "F1–F3",
        codes: &["f1", "f2", "f3"],
        label: "вкладка",
        desc: "Сменить вкладку, не выходя из просмотрщика",
        group: Group::Panels,
        hint: false,
        priority: 14,
        keep: false,
    },
    Binding {
        ctx: Ctx::Viewer,
        keys: "F4/Esc/q",
        codes: &["f4", "esc", "q"],
        label: "назад",
        desc: "Закрыть просмотрщик и вернуться в чат",
        group: Group::Panels,
        hint: false,
        priority: 15,
        keep: false,
    },
    // --- Сессия и выход ---
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "Esc",
        codes: &["esc"],
        label: "прервать",
        desc: "Прервать ход модели; в простое очищает ввод (не выходит — выход по `q`)",
        group: Group::Session,
        hint: true,
        priority: 25,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatBusy,
        keys: "Esc",
        codes: &["esc"],
        label: "прервать",
        desc: "Прервать текущий ход: обрывается запрос к модели или инструмент",
        group: Group::Session,
        hint: true,
        priority: 13,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "/help",
        codes: &["/help"],
        label: "команды",
        desc: "Каталог слэш-команд (в отличие от справки `?` — это команды агента)",
        group: Group::Session,
        hint: false,
        priority: 72,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "/model",
        codes: &["/model"],
        label: "модель",
        desc: "Выбрать модель: без аргумента — пикер, с именем — переключить сразу",
        group: Group::Session,
        hint: false,
        priority: 73,
        keep: false,
    },
    Binding {
        ctx: Ctx::ChatIdle,
        keys: "/new",
        codes: &["/new"],
        label: "новая сессия",
        desc: "Ротировать журнал и начать новый диалог",
        group: Group::Session,
        hint: false,
        priority: 74,
        keep: false,
    },
    Binding {
        ctx: Ctx::Global,
        keys: "q",
        codes: &["q"],
        label: "выход",
        desc: "Выход из приложения (в пустом вводе, вне модалок)",
        group: Group::Session,
        hint: true,
        priority: 90,
        keep: true,
    },
    Binding {
        ctx: Ctx::Global,
        keys: "Ctrl+C",
        codes: &["ctrl+c"],
        label: "выход",
        desc: "Выход из приложения в любой момент, даже во время хода",
        group: Group::Session,
        hint: false,
        priority: 95,
        keep: true,
    },
    Binding {
        ctx: Ctx::Global,
        keys: "?",
        codes: &["?"],
        label: "помощь",
        desc: "Эта справка: клавиши текущего экрана (при пустом вводе; иначе `?` — обычный текст)",
        group: Group::Session,
        hint: true,
        priority: 80,
        keep: true,
    },
    // --- Модалка выбора ---
    Binding {
        ctx: Ctx::Ask,
        keys: "↑/↓, j/k",
        codes: &["up", "down", "j", "k"],
        label: "выбор",
        desc: "Перемещение по вариантам",
        group: Group::Navigation,
        hint: true,
        priority: 10,
        keep: false,
    },
    Binding {
        ctx: Ctx::Ask,
        keys: "Enter",
        codes: &["enter"],
        label: "подтвердить",
        desc: "Выбрать текущий вариант",
        group: Group::Typing,
        hint: true,
        priority: 11,
        keep: false,
    },
    Binding {
        ctx: Ctx::Ask,
        keys: "1–9",
        codes: &["1", "2", "3", "4", "5", "6", "7", "8", "9"],
        label: "быстро",
        desc: "Быстрый выбор варианта по номеру",
        group: Group::Typing,
        hint: true,
        priority: 12,
        keep: false,
    },
    Binding {
        ctx: Ctx::Ask,
        keys: "Esc",
        codes: &["esc"],
        label: "отмена",
        desc: "Отказ от выбора (для вопроса инструмента — «реши сам»)",
        group: Group::Session,
        hint: true,
        priority: 13,
        keep: false,
    },
    // --- Оверлей справки ---
    Binding {
        ctx: Ctx::Help,
        keys: "↑/↓, PgUp/PgDn",
        codes: &["up", "down", "pageup", "pagedown"],
        label: "прокрутка",
        desc: "Пролистать справку, если она не поместилась",
        group: Group::Navigation,
        hint: false,
        priority: 10,
        keep: false,
    },
    Binding {
        ctx: Ctx::Help,
        keys: "?/Esc/q",
        codes: &["?", "esc", "q"],
        label: "закрыть",
        desc: "Закрыть справку и вернуться в чат",
        group: Group::Session,
        hint: true,
        priority: 5,
        keep: true,
    },
];

/// Действия контекста `ctx` в порядке объявления.
///
/// Глобальные клавиши (`q`, `?`, `Ctrl+C`) наследует только чат. Модальные
/// слои (`Ask`, `Viewer`, `Help`) запирают фокус: там `q`/`Esc`/`?` — «закрыть
/// слой», а не «выйти из приложения», и такой слой объявляет их сам.
fn bindings_for(ctx: Ctx) -> impl Iterator<Item = &'static Binding> {
    let inherits_globals = matches!(ctx, Ctx::ChatIdle | Ctx::ChatBusy);
    BINDINGS
        .iter()
        .filter(move |b| b.ctx == ctx || (b.ctx == Ctx::Global && inherits_globals))
}

/// Строка подсказок клавиш для статус-бара: приоритетные действия текущего
/// контекста, вписанные в `width`. Уходят с конца; `q` и `?` остаются всегда
/// (правило «минимум — выход и помощь», даже если окно 40 колонок).
pub(crate) fn hints(ctx: Ctx, width: usize, theme: &Theme) -> Vec<Span<'static>> {
    let mut picked: Vec<&Binding> = bindings_for(ctx).filter(|b| b.hint).collect();
    picked.sort_by_key(|b| b.priority);

    let pair = |b: &Binding| -> Vec<Span<'static>> {
        vec![
            Span::styled(
                b.keys.to_string(),
                Style::default()
                    .fg(theme.cyan)
                    .bg(theme.bg)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(format!(" {}", b.label), theme.muted()),
        ]
    };
    let joined = |items: &[&Binding]| -> usize {
        items
            .iter()
            .map(|b| b.keys.chars().count() + 1 + b.label.chars().count())
            .sum::<usize>()
            + items.len().saturating_sub(1) * 2
    };

    // Сначала обязательные (keep), затем остальные по приоритету — пока влезают.
    let keeps: Vec<&Binding> = picked.iter().copied().filter(|b| b.keep).collect();
    let mut chosen: Vec<&Binding> = keeps.clone();
    for b in picked.iter().copied().filter(|b| !b.keep) {
        let mut candidate = chosen.clone();
        // Необязательные идут перед обязательными (обязательные — у правого края).
        candidate.insert(candidate.len() - keeps.len(), b);
        if joined(&candidate) <= width {
            chosen = candidate;
        }
    }
    if joined(&chosen) > width {
        chosen.clone_from(&keeps);
    }

    let mut spans = Vec::new();
    for (i, b) in chosen.iter().enumerate() {
        if i > 0 {
            spans.push(Span::styled("  ".to_string(), theme.muted()));
        }
        spans.extend(pair(b));
    }
    spans
}

/// Разделы справки для контекста `ctx`: (раздел, [(клавиши, описание)]).
/// Разделы — в порядке [`Group`]; пустые не возвращаются.
pub(crate) fn sections(ctx: Ctx) -> Vec<(Group, Vec<(&'static str, &'static str)>)> {
    let order = [
        Group::Typing,
        Group::Navigation,
        Group::Panels,
        Group::Session,
    ];
    order
        .into_iter()
        .filter_map(|g| {
            let rows: Vec<(&'static str, &'static str)> = bindings_for(ctx)
                .filter(|b| b.group == g)
                .map(|b| (b.keys, b.desc))
                .collect();
            (!rows.is_empty()).then_some((g, rows))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tui::caps::ColorLevel;
    use crate::tui::theme::Theme;

    /// Все контексты, в которых пользователь может нажать клавишу.
    const CONTEXTS: [Ctx; 5] = [
        Ctx::ChatIdle,
        Ctx::ChatBusy,
        Ctx::Ask,
        Ctx::Viewer,
        Ctx::Help,
    ];

    /// Плоский текст подсказок (для проверок вписывания).
    fn hint_text(ctx: Ctx, width: usize) -> String {
        let theme = Theme::dark(ColorLevel::TrueColor);
        hints(ctx, width, &theme)
            .iter()
            .map(|s| s.content.as_ref())
            .collect()
    }

    #[test]
    fn no_key_collisions_inside_a_context() {
        // Одна клавиша — одно значение в пределах того, что видит пользователь
        // (эвристика H4): контекст вместе с унаследованными глобальными.
        for ctx in CONTEXTS {
            let mut seen: Vec<(&str, &str)> = Vec::new();
            for b in bindings_for(ctx) {
                for code in b.codes {
                    if let Some((_, prev)) = seen.iter().find(|(k, _)| k == code) {
                        panic!("конфликт в {ctx:?}: `{code}` — «{prev}» и «{}»", b.desc);
                    }
                    seen.push((code, b.desc));
                }
            }
        }
    }

    /// Модальный слой вправе переопределить глобальную клавишу — но только
    /// осознанно. Тест фиксирует полный список таких переопределений: новая
    /// случайная тень на `q`/`?`/`Ctrl+C` уронит сборку.
    #[test]
    fn overriding_a_global_key_is_an_explicit_decision() {
        let globals: Vec<&str> = BINDINGS
            .iter()
            .filter(|b| b.ctx == Ctx::Global)
            .flat_map(|b| b.codes.iter().copied())
            .collect();
        let mut overrides: Vec<(Ctx, &str)> = Vec::new();
        for ctx in CONTEXTS {
            for b in BINDINGS.iter().filter(|b| b.ctx == ctx) {
                for code in b.codes {
                    if globals.contains(code) {
                        overrides.push((ctx, code));
                    }
                }
            }
        }
        overrides.sort_unstable_by(|a, b| (a.0 as u8, a.1).cmp(&(b.0 as u8, b.1)));
        overrides.dedup();
        assert_eq!(
            overrides,
            [(Ctx::Viewer, "q"), (Ctx::Help, "?"), (Ctx::Help, "q")],
            "изменился список переопределений глобальных клавиш"
        );
    }

    /// Чат — единственный экран, который глобальные клавиши наследует.
    #[test]
    fn chat_inherits_globals_and_modals_do_not() {
        for ctx in [Ctx::ChatIdle, Ctx::ChatBusy] {
            let codes: Vec<&str> = bindings_for(ctx)
                .flat_map(|b| b.codes.iter().copied())
                .collect();
            for g in ["q", "?", "ctrl+c"] {
                assert!(codes.contains(&g), "{ctx:?} потерял глобальную `{g}`");
            }
        }
        for ctx in [Ctx::Ask, Ctx::Viewer, Ctx::Help] {
            let native: Vec<&str> = BINDINGS
                .iter()
                .filter(|b| b.ctx == Ctx::Global)
                .flat_map(|b| b.codes.iter().copied())
                .filter(|c| !bindings_for(ctx).any(|b| b.codes.contains(c)))
                .collect();
            assert!(!native.is_empty(), "{ctx:?} наследует необъявленное");
        }
    }

    #[test]
    fn global_keys_are_the_conventional_three() {
        let globals: Vec<&str> = BINDINGS
            .iter()
            .filter(|b| b.ctx == Ctx::Global)
            .flat_map(|b| b.codes.iter().copied())
            .collect();
        assert_eq!(globals, ["q", "ctrl+c", "?"]);
    }

    #[test]
    fn every_context_has_help_and_rendered_hints() {
        // Подсказки рисуют статус-бар (чат) и сам оверлей справки; у пикера и
        // просмотрщика своя строка подсказок в рамке, но справка нужна всем.
        for ctx in CONTEXTS {
            assert!(
                !sections(ctx).is_empty(),
                "нет разделов справки для {ctx:?}"
            );
        }
        for ctx in [Ctx::ChatIdle, Ctx::ChatBusy, Ctx::Help] {
            assert!(!hint_text(ctx, 200).is_empty(), "нет подсказок для {ctx:?}");
        }
    }

    #[test]
    fn narrow_hint_keeps_help_and_quit() {
        // Даже в 40 колонок пользователь видит, как выйти и где помощь.
        for width in [40, 24, 16] {
            let text = hint_text(Ctx::ChatIdle, width);
            assert!(text.contains('?'), "нет помощи при ширине {width}: {text}");
            assert!(text.contains('q'), "нет выхода при ширине {width}: {text}");
        }
    }

    #[test]
    fn hint_fits_width_when_possible() {
        for ctx in [Ctx::ChatIdle, Ctx::ChatBusy, Ctx::Ask] {
            let text = hint_text(ctx, 120);
            assert!(
                text.chars().count() <= 120,
                "{ctx:?}: подсказка длиннее окна: {} симв.",
                text.chars().count()
            );
        }
    }

    #[test]
    fn idle_hint_names_the_frequent_actions() {
        let text = hint_text(Ctx::ChatIdle, 120);
        for needle in ["Enter", "Tab", "?", "q"] {
            assert!(text.contains(needle), "нет «{needle}» в подсказке: {text}");
        }
    }

    #[test]
    fn busy_hint_shows_interrupt_and_queue() {
        let text = hint_text(Ctx::ChatBusy, 120);
        assert!(text.contains("в очередь"), "{text}");
        assert!(text.contains("прервать"), "{text}");
    }

    #[test]
    fn help_covers_every_binding_of_its_context() {
        for ctx in CONTEXTS {
            let listed: Vec<&str> = sections(ctx)
                .into_iter()
                .flat_map(|(_, rows)| rows.into_iter().map(|(k, _)| k))
                .collect();
            for b in bindings_for(ctx) {
                assert!(
                    listed.contains(&b.keys),
                    "{ctx:?}: «{}» ({}) не попал в справку",
                    b.keys,
                    b.desc
                );
            }
        }
    }

    #[test]
    fn hint_keys_are_accent_and_labels_dim() {
        let theme = Theme::dark(ColorLevel::TrueColor);
        let spans = hints(Ctx::ChatIdle, 120, &theme);
        assert!(spans.iter().all(|s| s.style.bg == Some(theme.bg)));
        assert!(spans.iter().any(|s| s.style.fg == Some(theme.cyan)));
        assert!(spans.iter().any(|s| s.style.fg == Some(theme.muted)));
        // Подсказка не начинается с разделителя.
        assert_eq!(spans.first().and_then(|s| s.style.fg), Some(theme.cyan));
    }

    #[test]
    fn bindings_are_well_formed() {
        for b in BINDINGS {
            assert!(!b.keys.is_empty() && !b.label.is_empty() && !b.desc.is_empty());
            assert!(
                !b.codes.is_empty(),
                "у «{}» нет нормализованных клавиш",
                b.keys
            );
        }
    }
}
