//! Отрисовка AST на символьную сетку: box-drawing рамки, ортогональные
//! рёбра со стрелками (`▼▲◀▶`), метки вдоль линий. Линии на пересечениях
//! сливаются (`┼`, `┴`, …); узлы рисуются поверх — длинные рёбра визуально
//! «проходят за» промежуточными узлами.

use std::collections::HashMap;
use std::fmt::Write as _;

use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use super::layout;
use super::model::{Direction, FlowAst, NoteSide, SeqAst, SeqItem, Shape, Skipped};

// Биты направлений линий — для слияния пересечений.
const R: u8 = 1;
const L: u8 = 2;
const D: u8 = 4;
const U: u8 = 8;

/// Ширина строки в колонках терминала.
fn str_width(s: &str) -> i32 {
    UnicodeWidthStr::width(s) as i32
}

/// Направления линии по символу (`None` — не линия).
fn line_bits(c: char) -> Option<u8> {
    Some(match c {
        '─' | '┄' => L | R,
        '│' => U | D,
        '┌' => R | D,
        '┐' => L | D,
        '└' => R | U,
        '┘' => L | U,
        '├' => R | U | D,
        '┤' => L | U | D,
        '┬' => L | R | D,
        '┴' => L | R | U,
        '┼' => L | R | U | D,
        _ => return None,
    })
}

/// Символ линии по набору направлений.
fn bits_line(bits: u8) -> Option<char> {
    Some(match bits {
        b if b == L | R => '─',
        b if b == U | D => '│',
        b if b == R | D => '┌',
        b if b == L | D => '┐',
        b if b == R | U => '└',
        b if b == L | U => '┘',
        b if b == R | U | D => '├',
        b if b == L | U | D => '┤',
        b if b == L | R | D => '┬',
        b if b == L | R | U => '┴',
        b if b == L | R | U | D => '┼',
        _ => return None,
    })
}

/// Символьная сетка с разреженным хранением.
struct Canvas {
    cells: HashMap<(i32, i32), char>,
}

impl Canvas {
    fn new() -> Self {
        Self {
            cells: HashMap::new(),
        }
    }

    /// Рисует линию: сливается с другими линиями, уступает тексту/рамкам/стрелкам.
    fn line(&mut self, x: i32, y: i32, c: char) {
        let key = (x, y);
        match self.cells.get(&key).copied() {
            None => {
                self.cells.insert(key, c);
            }
            Some(old) => {
                if let (Some(a), Some(b)) = (line_bits(old), line_bits(c)) {
                    if let Some(m) = bits_line(a | b) {
                        self.cells.insert(key, m);
                    }
                }
                // иначе старое — текст/рамка/стрелка: линия уступает
            }
        }
    }

    /// Рисует «сильный» символ (текст, рамки, стрелки) поверх линий.
    fn strong(&mut self, x: i32, y: i32, c: char) {
        if c != ' ' {
            self.cells.insert((x, y), c);
        }
    }

    /// Рисует строку посимвольно (по ширине символов), пробелы прозрачны.
    fn text(&mut self, x: i32, y: i32, s: &str) {
        let mut dx = 0;
        for c in s.chars() {
            self.strong(x + dx, y, c);
            dx += UnicodeWidthChar::width(c).unwrap_or(1) as i32;
        }
    }

    /// Очищает прямоугольник (под узлом линий быть не должно).
    fn clear(&mut self, x: i32, y: i32, w: i32, h: i32) {
        for r in y..(y + h) {
            for c in x..(x + w) {
                self.cells.remove(&(c, r));
            }
        }
    }

    /// Свободны ли `w` колонок начиная с `(x, y)`.
    fn free_span(&self, x: i32, y: i32, w: i32) -> bool {
        (0..w).all(|i| !self.cells.contains_key(&(x + i, y)))
    }

    /// Ячейка свободна или занята горизонтальной линией (кандидат под метку).
    fn slot_cell(&self, x: i32, y: i32) -> bool {
        matches!(self.cells.get(&(x, y)), None | Some('─'))
    }

    /// Собирает сетку в строку без хвостовых пробелов в строках.
    fn paint(&self) -> String {
        if self.cells.is_empty() {
            return String::new();
        }
        let min_x = self.cells.keys().map(|k| k.0).min().unwrap_or(0);
        let max_x = self.cells.keys().map(|k| k.0).max().unwrap_or(0);
        let min_y = self.cells.keys().map(|k| k.1).min().unwrap_or(0);
        let max_y = self.cells.keys().map(|k| k.1).max().unwrap_or(0);
        let mut out = String::new();
        for y in min_y..=max_y {
            let mut row = String::new();
            for x in min_x..=max_x {
                row.push(self.cells.get(&(x, y)).copied().unwrap_or(' '));
            }
            out.push_str(row.trim_end());
            if y < max_y {
                out.push('\n');
            }
        }
        out
    }
}

/// Горизонтальная линия между двумя колонками (концы не трогаем — там углы).
fn hline(cv: &mut Canvas, y: i32, x1: i32, x2: i32) {
    for c in (x1.min(x2) + 1)..x1.max(x2) {
        cv.line(c, y, '─');
    }
}

/// Вертикальная линия между двумя строками (концы не трогаем).
fn vline(cv: &mut Canvas, x: i32, y1: i32, y2: i32) {
    for r in (y1.min(y2) + 1)..y1.max(y2) {
        cv.line(x, r, '│');
    }
}

/// Ищет слот шириной `w` из свободных/`─` ячеек на строке `y` в `[x1, x2]`, слева.
fn slot_on_row(cv: &Canvas, y: i32, x1: i32, x2: i32, w: i32) -> Option<i32> {
    if w <= 0 || x2 - x1 + 1 < w {
        return None;
    }
    (x1..=(x2 - w + 1)).find(|&sx| (0..w).all(|i| cv.slot_cell(sx + i, y)))
}

/// То же, но поиск справа (метка ближе к стрелке).
fn slot_on_row_rev(cv: &Canvas, y: i32, x1: i32, x2: i32, w: i32) -> Option<i32> {
    if w <= 0 || x2 - x1 + 1 < w {
        return None;
    }
    (x1..=(x2 - w + 1))
        .rev()
        .find(|&sx| (0..w).all(|i| cv.slot_cell(sx + i, y)))
}

/// Ширина строки метки в колонках терминала (многострочная — максимум строк).
fn label_width(label: &str) -> i32 {
    label.lines().map(str_width).max().unwrap_or(0)
}

/// Ширина узла на сетке (с рамкой и отступами).
fn node_width(shape: Shape, label_w: i32) -> i32 {
    match shape {
        Shape::Rect | Shape::Rounded => label_w + 4,
        Shape::Rhombus | Shape::Circle => label_w + 6,
    }
}

/// Высота узла на сетке: многострочная метка (ER/C4, ADR-009) добавляет
/// строки; ромб/круг всегда однострочные (высота 3).
fn node_height(shape: Shape, label: &str) -> i32 {
    match shape {
        Shape::Rect | Shape::Rounded => 2 + label.lines().count().max(1) as i32,
        Shape::Rhombus | Shape::Circle => 3,
    }
}

/// Рисует узел (очищая место под ним — линии «проходят за» узлом).
///
/// Метка может быть многострочной (только Rect/Rounded): каждая строка —
/// отдельный ряд рамки, пустая строка рисуется разделителем `├─┤`.
fn draw_node(cv: &mut Canvas, x: i32, y: i32, shape: Shape, label: &str) {
    let lw = label_width(label);
    let w = node_width(shape, lw);
    let h = node_height(shape, label);
    cv.clear(x, y, w, h);
    let dash = "─".repeat((lw + 2) as usize);
    match shape {
        Shape::Rect | Shape::Rounded => {
            let (tl, tr, bl, br) = if shape == Shape::Rounded {
                ('╭', '╮', '╰', '╯')
            } else {
                ('┌', '┐', '└', '┘')
            };
            cv.text(x, y, &format!("{tl}{dash}{tr}"));
            for (row, line) in label.lines().enumerate() {
                let ry = y + 1 + row as i32;
                if line.is_empty() {
                    cv.text(x, ry, &format!("├{dash}┤"));
                } else {
                    let pad = (lw - str_width(line)).max(0) as usize;
                    cv.text(x, ry, &format!("│ {line}{} │", " ".repeat(pad)));
                }
            }
            cv.text(x, y + h - 1, &format!("{bl}{dash}{br}"));
        }
        Shape::Rhombus => {
            cv.text(x, y, &format!(" ╱{dash}╲ "));
            cv.text(x, y + 1, &format!("<  {label}  >"));
            cv.text(x, y + 2, &format!(" ╲{dash}╱ "));
        }
        Shape::Circle => {
            cv.text(x, y, &format!(" ╭{dash}╮ "));
            cv.text(x, y + 1, &format!("(( {label} ))"));
            cv.text(x, y + 2, &format!(" ╰{dash}╯ "));
        }
    }
}

/// Геометрия узла на канвасе.
#[derive(Clone, Copy)]
struct Geom {
    x: i32,
    y: i32,
    w: i32,
    h: i32,
}

/// Стрелка (или конец линии для `---`) у границы узла.
fn draw_head(cv: &mut Canvas, x: i32, y: i32, plain: bool, arrow: char, line: char) {
    if plain {
        cv.line(x, y, line);
    } else {
        cv.strong(x, y, arrow);
    }
}

/// Метка справа от стрелки (если место свободно), иначе метка опускается.
fn place_label_right_of(cv: &mut Canvas, x: i32, y: i32, label: &str) {
    let lw = str_width(label);
    if cv.free_span(x + 2, y, lw) {
        cv.text(x + 2, y, label);
    }
}

/// Рисует вертикальное ребро (`forward`: сверху вниз; иначе снизу вверх — BT).
///
/// Ортогональная трасса: из `from` в горизонтальную «шину» канала, по ней —
/// в колонку `to`, затем к границе `to` со стрелкой. Длинные рёбра (через
/// несколько слоёв) идут по той же схеме: вертикаль проходит за узлами.
fn route_vertical(
    cv: &mut Canvas,
    from: Geom,
    to: Geom,
    label: Option<&str>,
    plain: bool,
    forward: bool,
    gap: i32,
) {
    let fcx = from.x + from.w / 2;
    let tcx = to.x + to.w / 2;
    let arrow = if forward { '▼' } else { '▲' };
    let (head_row, bus) = if forward {
        (to.y - 1, to.y - gap)
    } else {
        (to.y + to.h, to.y + to.h - 1 + gap)
    };
    if fcx == tcx {
        if forward {
            for r in (from.y + from.h)..head_row {
                cv.line(fcx, r, '│');
            }
        } else {
            for r in (head_row + 1)..from.y {
                cv.line(fcx, r, '│');
            }
        }
        draw_head(cv, tcx, head_row, plain, arrow, '│');
        if let Some(l) = label {
            place_label_right_of(cv, tcx, head_row, l);
        }
        return;
    }
    let (c1, c2) = if forward {
        (
            if tcx > fcx { '└' } else { '┘' },
            if tcx > fcx { '┐' } else { '┌' },
        )
    } else {
        (
            if tcx > fcx { '┌' } else { '┐' },
            if tcx > fcx { '┘' } else { '└' },
        )
    };
    if forward {
        for r in (from.y + from.h)..bus {
            cv.line(fcx, r, '│');
        }
    } else {
        for r in (bus + 1)..from.y {
            cv.line(fcx, r, '│');
        }
    }
    cv.line(fcx, bus, c1);
    hline(cv, bus, fcx, tcx);
    cv.line(tcx, bus, c2);
    if forward {
        for r in (bus + 1)..head_row {
            cv.line(tcx, r, '│');
        }
    } else {
        for r in (head_row + 1)..bus {
            cv.line(tcx, r, '│');
        }
    }
    draw_head(cv, tcx, head_row, plain, arrow, '│');
    if let Some(l) = label {
        let lw = str_width(l);
        match slot_on_row(cv, bus, fcx.min(tcx) + 1, fcx.max(tcx) - 1, lw) {
            Some(sx) => cv.text(sx, bus, l),
            None => place_label_right_of(cv, tcx, head_row, l),
        }
    }
}

/// Рисует горизонтальное ребро (`forward`: слева направо; иначе справа — RL).
fn route_horizontal(
    cv: &mut Canvas,
    from: Geom,
    to: Geom,
    label: Option<&str>,
    plain: bool,
    forward: bool,
    gap: i32,
) {
    let fry = from.y + from.h / 2;
    let try_ = to.y + to.h / 2;
    let arrow = if forward { '▶' } else { '◀' };
    let (head_col, bus) = if forward {
        (to.x - 1, to.x - gap)
    } else {
        (to.x + to.w, to.x + to.w - 1 + gap)
    };
    // Линия: прямая или с вертикальной «шиной» в канале между слоями.
    if fry == try_ {
        if forward {
            for c in (from.x + from.w)..head_col {
                cv.line(c, fry, '─');
            }
        } else {
            for c in (head_col + 1)..from.x {
                cv.line(c, fry, '─');
            }
        }
    } else {
        let (c1, c2) = if forward {
            (
                if try_ > fry { '┐' } else { '┘' },
                if try_ > fry { '└' } else { '┌' },
            )
        } else {
            (
                if try_ > fry { '┌' } else { '└' },
                if try_ > fry { '┘' } else { '┐' },
            )
        };
        if forward {
            for c in (from.x + from.w)..bus {
                cv.line(c, fry, '─');
            }
        } else {
            for c in (bus + 1)..from.x {
                cv.line(c, fry, '─');
            }
        }
        cv.line(bus, fry, c1);
        vline(cv, bus, fry, try_);
        cv.line(bus, try_, c2);
        if forward {
            for c in (bus + 1)..head_col {
                cv.line(c, try_, '─');
            }
        } else {
            for c in (head_col + 1)..bus {
                cv.line(c, try_, '─');
            }
        }
    }
    draw_head(cv, head_col, try_, plain, arrow, '─');
    if let Some(l) = label {
        let lw = str_width(l);
        // сегмент у стрелки: на прямой — весь прогон, на ломаной — после шины
        let (x1, x2) = if fry == try_ {
            if forward {
                (from.x + from.w, head_col - 1)
            } else {
                (head_col + 1, from.x - 1)
            }
        } else if forward {
            (bus + 1, head_col - 1)
        } else {
            (head_col + 1, bus - 1)
        };
        // предпочитаем центр прогона, иначе — ближе к стрелке, иначе — над стрелкой
        let centered = x1 + (x2 - x1 + 1 - lw) / 2;
        let slot = Some(centered).filter(|&sx| {
            sx >= x1 && sx + lw - 1 <= x2 && (0..lw).all(|i| cv.slot_cell(sx + i, try_))
        });
        let slot = slot.or_else(|| slot_on_row_rev(cv, try_, x1, x2, lw));
        match slot {
            Some(sx) => cv.text(sx, try_, l),
            None if cv.free_span(head_col - lw + 1, try_ - 1, lw) => {
                cv.text(head_col - lw + 1, try_ - 1, l);
            }
            None => {} // некуда — метку опускаем
        }
    }
}

/// Рендерит flowchart в строку с артом (+ предупреждения о пропущенном).
pub(crate) fn render_flowchart(ast: &FlowAst) -> String {
    /// Зазор между узлами внутри слоя.
    const NODE_GAP: i32 = 3;
    let lay = layout::layout(ast);
    let n = ast.nodes.len();
    let widths: Vec<i32> = ast
        .nodes
        .iter()
        .map(|nd| node_width(nd.shape, label_width(&nd.label)))
        .collect();
    // Высота узла переменная: многострочные метки ER/C4 (ADR-009).
    let node_hs: Vec<i32> = ast
        .nodes
        .iter()
        .map(|nd| node_height(nd.shape, &nd.label))
        .collect();
    let horizontal = ast.dir.is_horizontal();
    let max_label = ast
        .edges
        .iter()
        .filter_map(|e| e.label.as_deref())
        .map(str_width)
        .max()
        .unwrap_or(0);
    // Зазор между слоями: горизонтальному рендеру нужно место под метки рёбер.
    let layer_gap = if horizontal && max_label > 0 {
        max_label + 6
    } else {
        2
    };

    // Координаты узлов: слои равномерно, слой центрируется относительно самого широкого/высокого.
    let mut geoms = vec![
        Geom {
            x: 0,
            y: 0,
            w: 0,
            h: 0,
        };
        n
    ];
    if horizontal {
        let heights: Vec<i32> = lay
            .layers
            .iter()
            .map(|l| {
                l.iter().map(|&i| node_hs[i]).sum::<i32>() + NODE_GAP * (l.len() as i32 - 1).max(0)
            })
            .collect();
        let total = heights.iter().copied().max().unwrap_or(0);
        let mut x = 0;
        for (li, layer) in lay.layers.iter().enumerate() {
            let lw = layer.iter().map(|&i| widths[i]).max().unwrap_or(0);
            let mut y = (total - heights[li]) / 2;
            for &i in layer {
                geoms[i] = Geom {
                    x,
                    y,
                    w: widths[i],
                    h: node_hs[i],
                };
                y += node_hs[i] + NODE_GAP;
            }
            x += lw + layer_gap;
        }
    } else {
        let ws: Vec<i32> = lay
            .layers
            .iter()
            .map(|l| {
                l.iter().map(|&i| widths[i]).sum::<i32>() + NODE_GAP * (l.len() as i32 - 1).max(0)
            })
            .collect();
        let total = ws.iter().copied().max().unwrap_or(0);
        let mut y = 0;
        for (li, layer) in lay.layers.iter().enumerate() {
            let layer_h = layer.iter().map(|&i| node_hs[i]).max().unwrap_or(0);
            let mut x = (total - ws[li]) / 2;
            for &i in layer {
                geoms[i] = Geom {
                    x,
                    y,
                    w: widths[i],
                    h: node_hs[i],
                };
                x += widths[i] + NODE_GAP;
            }
            y += layer_h + layer_gap;
        }
    }

    let mut cv = Canvas::new();
    let forward = matches!(ast.dir, Direction::TopDown | Direction::LeftRight);
    for e in &ast.edges {
        let label = e.label.as_deref();
        if horizontal {
            route_horizontal(
                &mut cv,
                geoms[e.from],
                geoms[e.to],
                label,
                e.plain,
                forward,
                layer_gap,
            );
        } else {
            route_vertical(
                &mut cv,
                geoms[e.from],
                geoms[e.to],
                label,
                e.plain,
                forward,
                layer_gap,
            );
        }
    }
    for (i, nd) in ast.nodes.iter().enumerate() {
        let g = geoms[i];
        draw_node(&mut cv, g.x, g.y, nd.shape, &nd.label);
    }
    finish(&cv, &ast.skipped)
}

/// Рендерит sequence-диаграмму в строку с артом (+ предупреждения).
pub(crate) fn render_sequence(ast: &SeqAst) -> String {
    let n = ast.participants.len();
    let widths: Vec<i32> = ast
        .participants
        .iter()
        .map(|p| str_width(&p.label) + 4)
        .collect();
    // Требования к промежуткам между центрами соседних линий жизни:
    // метки сообщений и заметки должны помещаться.
    let mut gap_req = vec![10i32; n.saturating_sub(1)];
    for item in &ast.items {
        match item {
            SeqItem::Message(m) if m.from != m.to => {
                let a = m.from.min(m.to);
                if m.from.max(m.to) == a + 1 {
                    gap_req[a] = gap_req[a].max(str_width(&m.label) + 4);
                }
            }
            SeqItem::Note(note) => {
                let need = str_width(&note.text) + 8;
                match note.side {
                    NoteSide::Right if note.participant + 1 < n => {
                        gap_req[note.participant] = gap_req[note.participant].max(need);
                    }
                    NoteSide::Left if note.participant > 0 => {
                        gap_req[note.participant - 1] = gap_req[note.participant - 1].max(need);
                    }
                    _ => {} // с краю — заметка вылезает наружу, канвас расширится
                }
            }
            SeqItem::Message(_) => {}
        }
    }
    // Координаты боксов участников.
    let mut xs = vec![0i32; n];
    for i in 1..n {
        let prev_cx = xs[i - 1] + widths[i - 1] / 2;
        let by_gap = prev_cx + gap_req[i - 1] - widths[i] / 2;
        let by_pack = xs[i - 1] + widths[i - 1] + 2;
        xs[i] = by_gap.max(by_pack);
    }
    let cxs: Vec<i32> = xs.iter().zip(&widths).map(|(&x, &w)| x + w / 2).collect();

    let mut cv = Canvas::new();
    for (i, p) in ast.participants.iter().enumerate() {
        draw_node(&mut cv, xs[i], 0, Shape::Rect, &p.label);
    }
    let mut y = 4;
    for item in &ast.items {
        match item {
            SeqItem::Message(m) => {
                let (ca, cb) = (cxs[m.from], cxs[m.to]);
                if m.from == m.to {
                    // Самовызов: петля справа от линии жизни.
                    cv.text(ca + 2, y, &m.label);
                    for c in (ca + 1)..(ca + 4) {
                        cv.line(c, y + 1, '─');
                    }
                    cv.line(ca + 4, y + 1, '┐');
                    cv.line(ca + 4, y + 2, '│');
                    cv.line(ca + 4, y + 3, '┘');
                    for c in (ca + 2)..(ca + 4) {
                        cv.line(c, y + 3, '─');
                    }
                    cv.strong(ca + 1, y + 3, '◀');
                    y += 5;
                } else {
                    let line_ch = if m.dotted { '┄' } else { '─' };
                    // Метка по центру прогона между линиями жизни.
                    let (lo, hi) = (ca.min(cb), ca.max(cb));
                    let span = hi - lo - 1;
                    let start = lo + 1 + (span - str_width(&m.label)).max(0) / 2;
                    cv.text(start, y, &m.label);
                    if ca < cb {
                        for c in (ca + 1)..(cb - 1) {
                            cv.line(c, y + 1, line_ch);
                        }
                        cv.strong(cb - 1, y + 1, '▶');
                    } else {
                        for c in (cb + 2)..ca {
                            cv.line(c, y + 1, line_ch);
                        }
                        cv.strong(cb + 1, y + 1, '◀');
                    }
                    y += 3;
                }
            }
            SeqItem::Note(note) => {
                let w = str_width(&note.text) + 4;
                let lx = match note.side {
                    NoteSide::Right => cxs[note.participant] + 2,
                    NoteSide::Left => cxs[note.participant] - 2 - w,
                };
                draw_node(&mut cv, lx, y, Shape::Rect, &note.text);
                y += 4;
            }
        }
    }
    // Линии жизни — после элементов: сливаются со стрелками, уступают рамкам.
    let life_end = if ast.items.is_empty() { 4 } else { y - 2 };
    for &cx in &cxs {
        for r in 3..=life_end {
            cv.line(cx, r, '│');
        }
    }
    finish(&cv, &ast.skipped)
}

/// Собирает канвас в строку и дописывает предупреждения о пропущенных конструкциях.
fn finish(cv: &Canvas, skipped: &[Skipped]) -> String {
    let mut out = cv.paint();
    for s in skipped {
        if !out.is_empty() {
            out.push('\n');
        }
        let _ = write!(out, "%% пропущено [строка {}]: {}", s.line, s.text);
    }
    out
}
