//! Генератор README-скриншотов: настоящие кадры ratatui → SVG.
//!
//! Идея: TUI рендерится в `TestBackend` (без терминала), буфер ячеек
//! превращается в SVG с «оконным» оформлением (хром с тремя точками,
//! скруглённая рамка, фон Tokyo Night). Шрифт — моноширинный, сетка
//! выровнена абсолютными координатами tspan'ов. Скриншоты — это СНИМКИ
//! РЕАЛЬНЫХ ЭКРАНОВ, а не макеты: сцены собираются тем же `App`, что и в
//! проде, поэтому рассинхрон дизайна и картинок невозможен.
//!
//! Регенерация (после изменений дизайна — обязательна):
//! `cargo test gen_readme_screenshots -- --ignored` → docs/screenshots/*.svg
//!
//! Конвертация в PNG для README — `scripts/svg2png.sh <каталог>` (headless
//! Chrome; ранее — `rsvg-convert -z 2`). SVG остаются источником истины в
//! репозитории.

use std::fmt::Write as _;

use ratatui::buffer::Buffer;
use ratatui::style::{Color, Modifier};

/// Шрифт кадра и метрики ячейки (px). `DejaVu` Sans Mono: advance 0.602em.
const FONT: &str = "DejaVu Sans Mono, Noto Color Emoji, monospace";
/// Размер шрифта, px.
const FONT_SIZE: f32 = 15.0;
/// Ширина ячейки (0.602 × `FONT_SIZE`).
const CELL_W: f32 = 9.03;
/// Высота строки.
const ROW_H: f32 = 19.0;
/// Боковой паддинг окна.
const PAD_X: f32 = 14.0;
/// Высота оконного хрома (точки + заголовок).
const CHROME_H: f32 = 26.0;
/// Нижний паддинг.
const PAD_BOTTOM: f32 = 12.0;

/// ANSI-16 в hex (стандартная xterm-палитра; тема Tokyo Night сама в Rgb,
/// это страховка для именованных цветов ratatui).
const ANSI16: [u32; 16] = [
    0x0000_0000,
    0x00cc_0000,
    0x004e_9a06,
    0x00c4_a000,
    0x0034_65a4,
    0x0075_507b,
    0x0006_989a,
    0x00d3_d7cf, //
    0x0055_5753,
    0x00ef_2929,
    0x008a_e234,
    0x00fc_e94f,
    0x0072_9fcf,
    0x00ad_7fa8,
    0x0034_e2e2,
    0x00ee_eeec,
];

/// Цвет ячейки в CSS; `Reset` — None (подставляется дефолт сцены).
fn css_color(c: Color) -> Option<String> {
    let hex = |v: u32| Some(format!("#{v:06x}"));
    match c {
        Color::Reset => None,
        Color::Rgb(r, g, b) => Some(format!("#{r:02x}{g:02x}{b:02x}")),
        Color::Black => hex(ANSI16[0]),
        Color::Red => hex(ANSI16[1]),
        Color::Green => hex(ANSI16[2]),
        Color::Yellow => hex(ANSI16[3]),
        Color::Blue => hex(ANSI16[4]),
        Color::Magenta => hex(ANSI16[5]),
        Color::Cyan => hex(ANSI16[6]),
        Color::Gray => hex(ANSI16[7]),
        Color::DarkGray => hex(ANSI16[8]),
        Color::LightRed => hex(ANSI16[9]),
        Color::LightGreen => hex(ANSI16[10]),
        Color::LightYellow => hex(ANSI16[11]),
        Color::LightBlue => hex(ANSI16[12]),
        Color::LightMagenta => hex(ANSI16[13]),
        Color::LightCyan => hex(ANSI16[14]),
        Color::White => hex(ANSI16[15]),
        Color::Indexed(i) => {
            let i = u32::from(i);
            let v = match i {
                0..=15 => ANSI16[i as usize],
                16..=231 => {
                    let n = i - 16;
                    let lv = |k: u32| [0, 95, 135, 175, 215, 255][((n / k) % 6) as usize];
                    (lv(36) << 16) | (lv(6) << 8) | lv(1)
                }
                _ => {
                    let g = 8 + (i - 232) * 10;
                    (g << 16) | (g << 8) | g
                }
            };
            hex(v)
        }
    }
}

/// XML-эскейп текста ячейки.
fn esc(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// Стиль одного прогона ячеек (одинаковые fg/bg/модификаторы).
struct Run {
    fg: Option<String>,
    bg: Option<String>,
    bold: bool,
    dim: bool,
    text: String,
}

/// Буфер ratatui → SVG с оконным хромом. `title` — заголовок окна.
pub(crate) fn buffer_to_svg(buf: &Buffer, title: &str) -> String {
    let area = buf.area;
    let cols = usize::from(area.width);
    let rows = usize::from(area.height);
    let w = PAD_X.mul_add(2.0, CELL_W * cols as f32);
    let h = (CHROME_H + PAD_BOTTOM).mul_add(1.0, ROW_H * rows as f32 + 6.0);
    let mut svg = String::with_capacity(64 * 1024);
    let _ = writeln!(
        svg,
        "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"{w:.0}\" height=\"{h:.0}\" \
         viewBox=\"0 0 {w:.0} {h:.0}\" font-family=\"{FONT}\" font-size=\"{FONT_SIZE}\">"
    );
    // Окно: фон + тонкая рамка + хром.
    svg.push_str("<rect width=\"100%\" height=\"100%\" rx=\"10\" fill=\"#1a1b26\"/>\n");
    let _ = writeln!(
        svg,
        "<rect x=\"0.5\" y=\"0.5\" width=\"{:.0}\" height=\"{:.0}\" rx=\"10\" fill=\"none\" stroke=\"#3b4261\"/>",
        w - 1.0,
        h - 1.0
    );
    for (i, c) in ["#f7768e", "#e0af68", "#9ece6a"].iter().enumerate() {
        let _ = writeln!(
            svg,
            "<circle cx=\"{:.0}\" cy=\"13\" r=\"5\" fill=\"{c}\"/>",
            20.0 + i as f32 * 16.0
        );
    }
    let _ = writeln!(
        svg,
        "<text x=\"{:.0}\" y=\"17\" fill=\"#565f89\" text-anchor=\"middle\">{}</text>",
        w / 2.0,
        esc(title)
    );
    let _ = writeln!(
        svg,
        "<line x1=\"0\" y1=\"{CHROME_H}\" x2=\"{w:.0}\" y2=\"{CHROME_H}\" stroke=\"#3b4261\"/>"
    );

    // Ячейки: прогоны одинакового стиля → rect (фон) + text (глифы).
    let mut y_px = CHROME_H + ROW_H * 0.78 + 3.0;
    for row in 0..rows {
        let mut runs: Vec<Run> = Vec::new();
        let mut col_of_run: Vec<usize> = Vec::new();
        for col in 0..cols {
            let cell = &buf[(col as u16, row as u16)];
            if cell.skip {
                continue; // вторая половина широкого глифа
            }
            let fg = css_color(cell.fg);
            let bg = css_color(cell.bg);
            let bold = cell.modifier.contains(Modifier::BOLD);
            let dim = cell.modifier.contains(Modifier::DIM);
            let sym = cell.symbol();
            let same = runs
                .last()
                .is_some_and(|r: &Run| r.fg == fg && r.bg == bg && r.bold == bold && r.dim == dim);
            if same {
                if let Some(r) = runs.last_mut() {
                    r.text.push_str(sym);
                }
            } else {
                runs.push(Run {
                    fg,
                    bg,
                    bold,
                    dim,
                    text: sym.to_string(),
                });
                col_of_run.push(col);
            }
        }
        let mut out_runs = String::new();
        for (run, &col) in runs.iter().zip(&col_of_run) {
            let text = run.text.trim_end_matches(' ');
            let x = PAD_X + CELL_W * col as f32;
            // Фон рисуем по ПОЛНОЙ ширине прогона (пробелы в конце — тоже фон).
            if let Some(bg) = &run.bg {
                let rw = CELL_W * run.text.chars().count() as f32;
                let _ = write!(
                    out_runs,
                    "<rect x=\"{x:.1}\" y=\"{:.1}\" width=\"{rw:.1}\" height=\"{ROW_H}\" fill=\"{bg}\"/>",
                    y_px - ROW_H * 0.78
                );
            }
            if !text.is_empty() {
                let mut style = String::new();
                if let Some(fg) = &run.fg {
                    let _ = write!(style, " fill=\"{fg}\"");
                }
                if run.bold {
                    style.push_str(" font-weight=\"700\"");
                }
                if run.dim {
                    style.push_str(" opacity=\"0.72\"");
                }
                let _ = write!(
                    out_runs,
                    "<text x=\"{x:.1}\" y=\"{y_px:.1}\" xml:space=\"preserve\"{style}>{}</text>",
                    esc(text)
                );
            }
        }
        // Дефолтный фон строки не рисуем — окно уже #1a1b26.
        svg.push_str(&out_runs);
        y_px += ROW_H;
    }
    svg.push_str("</svg>\n");
    svg
}

#[cfg(test)]
mod tests {
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;

    use crate::tui::app::{
        ChatBlock, RightTab, Screen, ToolState,
        testing::{set_context_usage, set_thinking, test_app},
    };

    use super::*;

    /// Снимок `App` в SVG: рендер в `TestBackend` заданного размера.
    fn snap(app: &mut crate::tui::app::App, w: u16, h: u16, title: &str) -> String {
        let mut terminal = Terminal::new(TestBackend::new(w, h)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        buffer_to_svg(terminal.backend().buffer(), title)
    }

    fn write(out_dir: &std::path::Path, name: &str, svg: &str) {
        let path = out_dir.join(name);
        std::fs::write(&path, svg).expect("запись svg");
        eprintln!("shot: {} ({} КБ)", path.display(), svg.len() / 1024);
    }

    #[test]
    fn gen_readme_screenshots() {
        if std::env::var("ARCH_GEN_SHOTS").is_err() {
            return; // генерация — только по явному запросу
        }
        let out = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("docs/screenshots");
        std::fs::create_dir_all(&out).expect("mkdir");

        // 1. Первый кадр: рабочий чат сразу, без заставки — логотип первым
        //    блоком диалога, пустое состояние, поле ввода без рамки,
        //    подсказки клавиш из реестра.
        let mut app = test_app();
        app.model_name = "deepseek:deepseek-v4-flash".into();
        app.tool_ctx.cwd = std::path::PathBuf::from("/home/user/sbp-gateway");
        set_context_usage(&mut app, 22_000, 1_000_000);
        write(&out, "01-first-frame.svg", &snap(&mut app, 116, 28, "arch"));

        // 1b. Справка `?`: клавиши текущего экрана по разделам.
        let mut app = test_app();
        app.model_name = "deepseek:deepseek-v4-flash".into();
        app.help = true;
        write(&out, "07-help.svg", &snap(&mut app, 116, 28, "arch"));

        // 2. Чат (hero): сквозной архитектурный ход — скиллы, KB, скоринг,
        //    mermaid-контейнеры на вкладке; статус-бар с живым индикатором
        //    контекста и фоновыми субагентами; скроллбар у рамки диалога.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "deepseek:deepseek-v4-flash".into();
        app.tool_ctx.cwd = std::path::PathBuf::from("/home/user/sbp-gateway");
        app.push_block(ChatBlock::User(
            "спроектируй платёжный шлюз СБП (C2B): контейнеры, паттерны, маршрут".into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "skill_load".into(),
            action: "transactional-outbox".into(),
            state: ToolState::Ok,
            summary: "transactional-outbox [patterns-integration] — скилл в контексте".into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "kb_search".into(),
            action: "«идемпотентный потребитель»".into(),
            state: ToolState::Ok,
            summary: "4 фрагмента: saga-transactions, idempotent-consumer, strangler-acl…".into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "web_search".into(),
            action: "«transactional outbox сверка»".into(),
            state: ToolState::Ok,
            summary: "AWS Builders' Library: 3 статьи по outbox и сверке".into(),
        });
        app.push_block(ChatBlock::Assistant(
            "## Контур шлюза (C4 — контейнеры)\n\
             **Ядро:** API ТСП → статусная машина платежа (единый источник истины) → outbox.\n\
             - **Паттерны:** сага, transactional outbox, идемпотентный потребитель\n\
             - **Адаптеры:** ОПКЦ (НСПК) и АБС — ядро от них контрактно независимо\n\
             - Решения фиксируем в ADR-001…003, инварианты — в ARCHITECTURE-SPINE.md"
                .into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "mermaid_render".into(),
            action: "flowchart TD · 6 узлов".into(),
            state: ToolState::Ok,
            summary: "flowchart: 6 узлов · рендер на вкладке ◇ Mermaid".into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "control_score".into(),
            action: "--trigger payments+critical".into(),
            state: ToolState::Ok,
            summary: "Score 13 → маршрут Critical · гейты A0–A5, рубрика обязательна".into(),
        });
        app.push_block(ChatBlock::Assistant(
            "Готово к передаче: `/handoff claude-code ./sbp-gateway` — пакет с инвариантами, \
             критериями приёмки и рубрикой."
                .into(),
        ));
        app.panels.mermaid = crate::mermaid::render(
            "flowchart TD\n  API[API ТСП] --> SM[Ядро: статусная машина]\n  \
             SM --> OB[(Outbox)]\n  SM --> OP[Адаптер ОПКЦ]\n  OB --> ABS[Адаптер АБС]\n  \
             OP --> DLQ[(DLQ)]",
        )
        .expect("рендер mermaid");
        // Живой индикатор контекста + двое фоновых субагентов в статус-баре.
        set_context_usage(&mut app, 61_440, 1_000_000);
        let registry = crate::subagent::SubagentRegistry::new();
        for (id, task) in [
            ("sa-01", "разведка репозитория ТСП"),
            ("sa-02", "черновик ADR-002: статусная машина"),
        ] {
            registry.insert(crate::subagent::SubagentTask {
                id: id.into(),
                agent: "explore".into(),
                task: task.into(),
                status: crate::subagent::TaskStatus::Running,
                report: String::new(),
                started_at: "2026-08-15".into(),
                finished_at: None,
            });
        }
        let ctx = app.tool_ctx.clone().with_subagents(registry);
        app.tool_ctx = ctx;
        // Модель ещё работает: в очереди два сообщения (карточка в окне логов),
        // в поле ввода — многострочный черновик.
        set_thinking(&mut app, true);
        app.queue
            .push_back("а теперь NFR: p95 шлюза и деградация НСПК".into());
        app.queue.push_back(
            "потом /handoff claude-code ./sbp-gateway\nс инвариантами и рубрикой".into(),
        );
        app.input
            .set_text("покажи fitness-функции для outbox и сверки\nи пороги для p95".into());
        write(
            &out,
            "02-chat-mermaid.svg",
            &snap(&mut app, 150, 40, "arch — чат + mermaid"),
        );

        // 3. Пикер моделей (модалка поверх чата).
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "deepseek:deepseek-v4-flash".into();
        app.tool_ctx.cwd = std::path::PathBuf::from("/home/user/sbp-gateway");
        app.push_block(ChatBlock::User(
            "перед ревью ADR переключись на тяжёлую модель с ризонингом".into(),
        ));
        app.push_block(ChatBlock::Assistant(
            "Открываю пикер: у моделей с меткой «ризонинг» доступен `/think on`.".into(),
        ));
        app.open_model_picker();
        // В сцене текущая модель — stub-провайдер тестов; для снимка показываем
        // реалистичное состояние: deepseek — текущая (★ и курсор).
        if let Some(ask) = app.ask.as_mut() {
            ask.question = "Модель для этой сессии (сейчас: deepseek):".into();
            ask.recommended = Some("deepseek".into());
        }
        set_context_usage(&mut app, 12_300, 1_000_000);
        write(
            &out,
            "03-model-picker.svg",
            &snap(&mut app, 122, 32, "arch — /model"),
        );

        // 4. Рубрика: отчёт LLM-судьи на правой вкладке; индикатор контекста
        //    за порогом L1 (оранжевый) — видна семантика цветов.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "deepseek-pro:deepseek-v4-pro 🧠".into();
        app.tool_ctx.cwd = std::path::PathBuf::from("/home/user/sbp-gateway");
        app.push_block(ChatBlock::User(
            "оцени solutioning СБП-шлюза по якорной рубрике и прогоняй бенчмарк".into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "rubric_evaluate".into(),
            action: String::new(),
            state: ToolState::Ok,
            summary: "solution_architecture: 4.2/5 — отчёт на вкладке ✓ Рубрика".into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "bench_run".into(),
            action: String::new(),
            state: ToolState::Ok,
            summary: "solution-bench: 8/10 сценариев PASS · 2 замечания по сверке".into(),
        });
        app.push_block(ChatBlock::Assistant(
            "**Итог: READY с замечаниями.** Обратимость (3/5) и критерии отката — \
             в доработку; остальное закрыто."
                .into(),
        ));
        app.push_block(ChatBlock::User(
            "зафиксируй замечания в ADR и подготовь вопросы к архкому".into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "adr_new".into(),
            action: String::new(),
            state: ToolState::Ok,
            summary: "ADR-002 создан: docs/adr/ADR-002-state-machine.md".into(),
        });
        app.push_block(ChatBlock::Assistant(
            "Замечания внесены в ADR-002. На A3 выносится выбор вендора транспорта — \
             RFP-пакет готов (`docs/rfp/vendor-rfp.md`)."
                .into(),
        ));
        app.panels.rubric = "Оценка: solution_architecture (якорная)\n\n\
             ✓ Контекст и драйверы      5/5\n✓ Альтернативы             4/5\n\
             ✓ Отрицательные следствия  4/5\n◌ Обратимость              3/5\n\
             ✓ Fitness-функции          5/5\n\n\
             Взвешенный итог: 4.2/5 — READY с замечаниями\nБенчмарк: 8/10 PASS"
            .into();
        app.right_tab = RightTab::Rubric;
        set_context_usage(&mut app, 812_000, 1_000_000);
        write(
            &out,
            "04-rubric.svg",
            &snap(&mut app, 134, 30, "arch — рубрика"),
        );

        // 5. Handoff: архитектор передаёт контекст кодовому харнессу —
        //    пакет .arch-handoff/, прогон harness_run с умными таймаутами,
        //    JSON-контракт результата в сводке.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "deepseek:deepseek-v4-flash".into();
        app.tool_ctx.cwd = std::path::PathBuf::from("/home/user/sbp-gateway");
        app.push_block(ChatBlock::User(
            "передай статусную машину на исполнение Claude Code и проконтролируй результат".into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "handoff_create".into(),
            action: String::new(),
            state: ToolState::Ok,
            summary: ".arch-handoff/: TASK.md · ARCHITECTURE.md · CONSTRAINTS.yaml · \
                 RUBRIC.yaml · MANIFEST.json · adr/ — epic-context ~1 247 токенов"
                .into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "harness_run".into(),
            action: "claude-code".into(),
            state: ToolState::Ok,
            summary: "claude-code: код 0 · 1 847 с · контракт status=complete · \
                 assumptions 3 · open_questions 1 · прерываний нет"
                .into(),
        });
        app.push_block(ChatBlock::Assistant(
            "**Исполнение принято.** Claude Code реализовал статусную машину и outbox-\n\
             ретраи: `pytest -q` — 14 passed, fitness `no-print-in-py` — PASS.\n\
             - Открытый вопрос: таймаут подтверждения НСПК — вынес в ADR-004\n\
             - Тишины не было: heartbeat по файлам репо, процессная группа жила весь прогон"
                .into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "control_check".into(),
            action: ". · 6 правил".into(),
            state: ToolState::Ok,
            summary: "fitness: 4/4 PASS · spine-инварианты AD-1…AD-3 не нарушены".into(),
        });
        app.push_block(ChatBlock::Assistant(
            "Контур целостен. Дальше: `/export docx` — протокол прогона для архкома.".into(),
        ));
        app.panels.mermaid = crate::mermaid::render(
            "flowchart LR\n  A[архитектор] -->|handoff-пакет| H(Claude Code)\n  \
             H -->|код + контракт| R[репозиторий]\n  A -->|control check| R",
        )
        .expect("рендер mermaid");
        set_context_usage(&mut app, 96_500, 1_000_000);
        write(
            &out,
            "05-handoff.svg",
            &snap(&mut app, 150, 36, "arch — handoff кодовому харнессу"),
        );
    }

    /// Кадры кейса 004 (кейсы/parallel-epics/screenshots): параллельный флот
    /// Claude Code по worktree + передача пакета через MCP.
    #[test]
    fn gen_case04_screenshots() {
        if std::env::var("ARCH_GEN_SHOTS").is_err() {
            return; // генерация — только по явному запросу
        }
        let out = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("кейсы/parallel-epics/screenshots");
        std::fs::create_dir_all(&out).expect("mkdir");

        // A. Параллельный флот: три Claude Code в трёх worktree, спайн
        //    AD-1…AD-3 — единственный канал согласования; гейт зелёный.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "deepseek:deepseek-v4-flash".into();
        app.tool_ctx.cwd = std::path::PathBuf::from("/home/user/spine-parallel");
        app.push_block(ChatBlock::User(
            "три модуля библиотеки spinecalc — тремя параллельными Claude Code: каждому \
             свой worktree и под-пакет; спайн AD-1…AD-3 — единственный канал согласования"
                .into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "handoff_create".into(),
            action: String::new(),
            state: ToolState::Ok,
            summary: "3 worktree: пакеты по AD-1/AD-2/AD-3 · baseline 42b5020 · \
                 epic-context ~416 токенов каждый"
                .into(),
        });
        for (wt, tests, conflicts) in [
            ("p1-amount", 5, 1),
            ("p2-logfmt", 6, 0),
            ("p3-report", 4, 0),
        ] {
            app.push_block(ChatBlock::Tool {
                name: "harness_run".into(),
                action: "claude-code".into(),
                state: ToolState::Ok,
                summary: format!(
                    "{wt}: claude-code · код 0 · pytest {tests} passed · \
                     контракт complete · conflicts {conflicts}"
                ),
            });
        }
        app.push_block(ChatBlock::Tool {
            name: "control_check".into(),
            action: ". · 6 правил".into(),
            state: ToolState::Ok,
            summary: "fitness 6/6 PASS по трём worktree · сигнатуры AD-1…AD-3 соблюдены".into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "spine_lint".into(),
            action: String::new(),
            state: ToolState::Ok,
            summary: "spine: нарушений нет".into(),
        });
        app.push_block(ChatBlock::Assistant(
            "**Ландшафт не разошёлся.** Три параллельных исполнителя сошлись на контрактах\n\
             без взаимной видимости: склейка `spinecalc` — `INTEGRATION OK` с первой сборки.\n\
             Конфликт p1 (аннотации в AD-1 против regex приёмки) эскалирован архитектору,\n\
             а не скрыт в коде — поле conflicts сработало по назначению."
                .into(),
        ));
        app.panels.mermaid = crate::mermaid::render(
            "flowchart LR\n  S[spine AD-1…3] --> P1(Claude Code №1)\n  \
             S --> P2(Claude Code №2)\n  S --> P3(Claude Code №3)\n  \
             P1 --> I[пакет spinecalc]\n  P2 --> I\n  P3 --> I\n  \
             I --> G[гейт: 15/15 PASS]",
        )
        .expect("рендер mermaid");
        set_context_usage(&mut app, 148_200, 1_000_000);
        write(
            &out,
            "run.svg",
            &snap(&mut app, 150, 38, "arch — параллельные эпики (кейс 004)"),
        );

        // B. Handoff как сервис: пакет читается через MCP-сервер arch-handoff;
        //    прогон p4 честно прерван стражем тишины (медленный прокси).
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "deepseek:deepseek-v4-flash".into();
        app.tool_ctx.cwd = std::path::PathBuf::from("/home/user/spine-parallel");
        app.push_block(ChatBlock::User(
            "тот же пакет отдай четвёртому исполнителю через MCP — сервер arch-handoff".into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "arch-handoff__list_packets".into(),
            action: "arch-handoff".into(),
            state: ToolState::Ok,
            summary: "4 пакета: p1-amount · p2-logfmt · p3-report · p4-mcp".into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "arch-handoff__read_packet".into(),
            action: "p4-mcp".into(),
            state: ToolState::Ok,
            summary: "read_packet(\"p4-mcp\") → TASK.md + ARCHITECTURE.md + \
                 CONSTRAINTS.yaml + spine (дословные Rule AD-1…3)"
                .into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "harness_run".into(),
            action: "claude-code".into(),
            state: ToolState::Error,
            summary: "p4-mcp: прерван по тишине 600 с (медленный прокси) — группа \
                 завершена, сирот нет, вывод частичный"
                .into(),
        });
        app.push_block(ChatBlock::Assistant(
            "**Пакет по MCP доставлен и прочитан** исполнителем; прогон остановил страж\n\
             тишины — heartbeat по mtime не увидел работы за 10 минут. Урок влит в\n\
             харнесс: heartbeat по файлам + таймауты по маршруту значимости."
                .into(),
        ));
        app.panels.mermaid = crate::mermaid::render(
            "flowchart TD\n  A[архитектор] -->|пакет по id| M[MCP arch-handoff]\n  \
             M -->|read_packet| P4(Claude Code №4)",
        )
        .expect("рендер mermaid");
        set_context_usage(&mut app, 122_700, 1_000_000);
        write(
            &out,
            "mcp.svg",
            &snap(&mut app, 150, 30, "arch — handoff через MCP (кейс 004)"),
        );
    }

    /// Кадры кейса 005 (кейсы/fleet-of-ten/screenshots): флот из десяти
    /// Claude Code и флот, закоммитивший работу сам (контракт «Финализация»).
    #[test]
    fn gen_case05_screenshots() {
        if std::env::var("ARCH_GEN_SHOTS").is_err() {
            return; // генерация — только по явному запросу
        }
        let out =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("кейсы/fleet-of-ten/screenshots");
        std::fs::create_dir_all(&out).expect("mkdir");

        // A. Флот из десяти: прогоны, гейты, интеграция с первой сборки.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "deepseek:deepseek-v4-flash".into();
        app.tool_ctx.cwd = std::path::PathBuf::from("/home/user/bankcalc-fleet");
        app.push_block(ChatBlock::User(
            "десять эпиков bankcalc — десятью параллельными Claude Code, каждому worktree \
             и под-пакет; спайн AD-1…AD-10 — единственный канал согласования"
                .into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "handoff_create".into(),
            action: String::new(),
            state: ToolState::Ok,
            summary: "10 worktree: пакеты по AD-1…AD-10 · baseline 82d2f9d · \
                 epic-context ~1 028 токенов · план отката в каждом TASK.md"
                .into(),
        });
        for (wt, tests, secs) in [
            ("p01-amount", 5, 104),
            ("p08-tax", 4, 190),
            ("p10-summary", 4, 191),
        ] {
            app.push_block(ChatBlock::Tool {
                name: "harness_run".into(),
                action: "claude-code".into(),
                state: ToolState::Ok,
                summary: format!(
                    "{wt}: claude-code · код 0 · {secs} с · pytest {tests} passed · \
                     контракт complete · conflicts 0 · коммит исполнителя"
                ),
            });
        }
        app.push_block(ChatBlock::Assistant(
            "p02–p07, p09 — аналогично: все complete, все закоммичены самими\n\
             исполнителями. Стена: 10 эпиков за ~3,2 минуты."
                .into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "control_check".into(),
            action: ". · 6 правил".into(),
            state: ToolState::Ok,
            summary: "fitness 60/60 PASS по десяти worktree · сигнатуры AD-1…AD-10 соблюдены"
                .into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "spine_lint".into(),
            action: String::new(),
            state: ToolState::Ok,
            summary: "spine: нарушений нет".into(),
        });
        app.push_block(ChatBlock::Assistant(
            "**Ландшафт из десяти не разошёлся.** Склейка `bankcalc` — «день процессинга»\n\
             прошёл с первой сборки: платёж прошёл все десять контрактов по цепочке.\n\
             Интеграция читается из git log — оркестратору нечего дособирать."
                .into(),
        ));
        app.panels.mermaid = crate::mermaid::render(
            "flowchart LR\n  S[спайн AD-1…10] --> F[10 × Claude Code]\n  \
             F --> B[bankcalc]\n  B --> G[гейт: всё зелёное]",
        )
        .expect("рендер mermaid");
        set_context_usage(&mut app, 341_800, 1_000_000);
        write(
            &out,
            "run.svg",
            &snap(&mut app, 150, 40, "arch — флот из десяти (кейс 005)"),
        );

        // B. Финализация: флот закоммитил сам (урок утреннего кейса 004).
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "deepseek:deepseek-v4-flash".into();
        app.tool_ctx.cwd = std::path::PathBuf::from("/home/user/bankcalc-fleet");
        app.push_block(ChatBlock::User(
            "проверь, что флот сам зафиксировал работу — как требует секция «Финализация»".into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "bash".into(),
            action: String::new(),
            state: ToolState::Ok,
            summary: "git log ×10: у каждого worktree коммит исполнителя поверх baseline \
                 82d2f9d («feat(bankcalc.fee): calc_fee по базисным пунктам (AD-3)»…)"
                .into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "bash".into(),
            action: String::new(),
            state: ToolState::Ok,
            summary: "⚑ авто-коммит харнесса: 0 срабатываний — страховка не понадобилась".into(),
        });
        app.push_block(ChatBlock::Assistant(
            "**Контракт «Финализация» сработал 10/10.** Утренний флот кейса 004 не закоммитил\n\
             ничего — интеграцию собирал оркестратор; вечерний флот зафиксировал всё сам.\n\
             Точка интеграции переехала в git log каждого worktree."
                .into(),
        ));
        app.panels.mermaid = crate::mermaid::render(
            "flowchart LR\n  B[baseline 82d2f9d] --> C[10/10 коммитов]\n  \
             C --> V[INTEGRATION OK]",
        )
        .expect("рендер mermaid");
        set_context_usage(&mut app, 208_400, 1_000_000);
        write(
            &out,
            "commits.svg",
            &snap(&mut app, 150, 30, "arch — флот коммитит сам (кейс 005)"),
        );
    }

    /// Кадры кейса 006 (кейсы/drift-control/screenshots): дрейф-эксперимент —
    /// одна задача двум рукам (голая vs спайн+CONSTRAINTS), один гейт судит обе.
    #[test]
    fn gen_case06_screenshots() {
        if std::env::var("ARCH_GEN_SHOTS").is_err() {
            return; // генерация — только по явному запросу
        }
        let out = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("кейсы/drift-control/screenshots");
        std::fs::create_dir_all(&out).expect("mkdir");

        // A. Две руки: одна и та же задача платёжного ядра — голая и с пакетом;
        //    механический гейт судит обе одними правилами.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "deepseek:deepseek-v4-flash".into();
        app.tool_ctx.cwd = std::path::PathBuf::from("/home/user/drift-lab");
        app.push_block(ChatBlock::User(
            "дрейф-эксперимент: одна задача «платёжное ядро» двум рукам — \
             A голая, B с handoff-пакетом (AD-1…3 + C-01…06); судит control check"
                .into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "handoff_create".into(),
            action: String::new(),
            state: ToolState::Ok,
            summary: "рука B: TASK.md · ARCHITECTURE-SPINE.md (AD-1…3) · \
                 CONSTRAINTS.yaml (6 правил critical)"
                .into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "harness_run".into(),
            action: "claude-code".into(),
            state: ToolState::Ok,
            summary: "рука A: claude-code · код 0 · 371.9 с · cargo test 32/32 · \
                 коммит c185a69"
                .into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "harness_run".into(),
            action: "claude-code".into(),
            state: ToolState::Ok,
            summary: "рука B: claude-code · код 0 · 360.4 с · cargo test 19/19 · \
                 коммит 6a0ee75"
                .into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "control_check".into(),
            action: ". · 6 правил".into(),
            state: ToolState::Error,
            summary: "рука A: FAIL · 2 error — C-02 thiserror не найден в Cargo.toml · \
                 C-05 идемпотентность не найдена · exit 1"
                .into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "control_check".into(),
            action: ". · 6 правил".into(),
            state: ToolState::Ok,
            summary: "рука B: PASS · 6/6 правил · нарушений нет · exit 0".into(),
        });
        app.push_block(ChatBlock::Assistant(
            "**Дрейф пойман механически.** Рука A сдала добротный код: 6 модулей,\n\
             32 зелёных теста, f64 не выбрала сама — но ошибки оформила рукописным\n\
             enum вместо thiserror и вообще без идемпотентности. Дрейф ровно по\n\
             орг-инвариантам: модель их не угадывает, они должны доезжать в пакете."
                .into(),
        ));
        app.panels.mermaid = crate::mermaid::render(
            "flowchart TD\n  T[задача: платёжное ядро] --> A[рука A: голая]\n  \
             T --> B[рука B: + спайн AD-1…3]\n  A --> GA[гейт: FAIL 2/6]\n  \
             B --> GB[гейт: PASS 6/6]",
        )
        .expect("рендер mermaid");
        set_context_usage(&mut app, 64_300, 1_000_000);
        write(
            &out,
            "run.svg",
            &snap(&mut app, 150, 38, "arch — дрейф-эксперимент (кейс 006)"),
        );

        // B. Спайн удержал: рука B прошла не формально — настоящий inbox
        //    идемпотентности, исполнитель сам сверился с критериями приёмки.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "deepseek:deepseek-v4-flash".into();
        app.tool_ctx.cwd = std::path::PathBuf::from("/home/user/drift-lab");
        app.push_block(ChatBlock::User(
            "рука B прошла 6/6 — проверь, что это не строки под линтер".into(),
        ));
        app.push_block(ChatBlock::Tool {
            name: "bash".into(),
            action: String::new(),
            state: ToolState::Ok,
            summary: "Cargo.lock: thiserror 2.0.20 — реальная зависимость; \
                 PaymentProcessor::authorize — inbox по ключу"
                .into(),
        });
        app.push_block(ChatBlock::Tool {
            name: "control_check".into(),
            action: ". · 6 правил".into(),
            state: ToolState::Ok,
            summary: "armB-solution: PASS · 6/6 · повторный authorize по ключу \
                 возвращает первый результат без повторного эффекта"
                .into(),
        });
        app.push_block(ChatBlock::Assistant(
            "**Не формальность.** Идемпотентность — настоящий inbox: повтор с тем же\n\
             ключом клонирует первый платёж, ошибка возвращается та же. Цена спайна\n\
             по стене — нулевая: 360 с против 372 с у голой руки. Гейт воспроизводим:\n\
             `arch-ml control check кейсы/drift-control/armB-solution -c …` → exit 0."
                .into(),
        ));
        app.panels.mermaid = crate::mermaid::render(
            "flowchart TD\n  S[спайн AD-1…3 + C-01…06] --> H[рука B]\n  \
             H --> I[inbox идемпотентности]\n  I --> P[PASS 6/6]",
        )
        .expect("рендер mermaid");
        set_context_usage(&mut app, 41_900, 1_000_000);
        write(
            &out,
            "gate.svg",
            &snap(&mut app, 150, 32, "arch — спайн удержал (кейс 006)"),
        );
    }
}
