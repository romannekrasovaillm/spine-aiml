//! Парсер mermaid-подмножества в AST ([`super::model`]).
//!
//! Flowchart: `graph|flowchart TD|TB|BT|LR|RL`, узлы `A[label]`/`B(label)`/
//! `C{label}`/`D((label))`, рёбра `-->`, `---`, `-.->`, `-- метка -->`,
//! цепочки `A --> B --> C`. Sequence: `participant X as Label`, `->>`, `-->>`,
//! `Note left of|right of X: текст`. Комментарии `%%` и пустые строки
//! игнорируются; известные неподдерживаемые конструкции (`subgraph`, `style`,
//! `click`, `loop`, …) пропускаются с предупреждением.

use std::collections::HashMap;

use crate::error::{HarnessError, Result};

use super::model::{
    C4Ast, C4ElemKind, C4Element, C4Relation, Direction, ErAst, ErAttribute, ErCard, ErEntity,
    ErRelation, FlowAst, FlowEdge, FlowNode, NoteSide, Participant, SeqAst, SeqItem, SeqMessage,
    SeqNote, Shape, Skipped,
};

/// Формирует [`HarnessError::Mermaid`] с номером строки и фрагментом.
fn err_at(line: usize, text: &str, msg: &str) -> HarnessError {
    let snippet: String = text.chars().take(40).collect();
    HarnessError::Mermaid(format!("строка {line}: {msg}: «{snippet}»"))
}

/// Отрезает комментарий `%%` (вне двойных кавычек).
fn strip_comment(line: &str) -> &str {
    let mut in_quotes = false;
    let mut prev = '\0';
    for (i, c) in line.char_indices() {
        if c == '"' {
            in_quotes = !in_quotes;
        } else if c == '%' && prev == '%' && !in_quotes {
            // `%` — ASCII, поэтому `i - 1` — граница char.
            return &line[..i - 1];
        }
        prev = c;
    }
    line
}

/// Снимает обрамляющие двойные кавычки и обрезает пробелы.
fn unquote(raw: &str) -> String {
    let t = raw.trim();
    if t.len() >= 2 && t.starts_with('"') && t.ends_with('"') {
        t[1..t.len() - 1].to_owned()
    } else {
        t.to_owned()
    }
}

/// Ищет подстроку вне двойных кавычек (байтовый индекс).
fn find_outside_quotes(hay: &str, needle: &str) -> Option<usize> {
    let mut in_quotes = false;
    let mut i = 0;
    while i < hay.len() {
        let s = &hay[i..];
        let c = s.chars().next()?;
        if c == '"' {
            in_quotes = !in_quotes;
            i += 1;
        } else {
            if !in_quotes && s.starts_with(needle) {
                return Some(i);
            }
            i += c.len_utf8();
        }
    }
    None
}

/// Курсор по строке (байтовая позиция, двигается только по char-границам).
struct Cursor<'a> {
    text: &'a str,
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(text: &'a str) -> Self {
        Self { text, pos: 0 }
    }

    /// Остаток строки от текущей позиции.
    fn rest(&self) -> &'a str {
        &self.text[self.pos..]
    }

    /// Пропускает пробельные символы.
    fn skip_ws(&mut self) {
        while let Some(c) = self.rest().chars().next() {
            if c.is_whitespace() {
                self.pos += c.len_utf8();
            } else {
                break;
            }
        }
    }

    /// Съедает префикс, если он есть.
    fn eat(&mut self, prefix: &str) -> bool {
        if self.rest().starts_with(prefix) {
            self.pos += prefix.len();
            true
        } else {
            false
        }
    }
}

/// Сканирует идентификатор (буквы/цифры/`_`).
fn scan_id(cur: &mut Cursor<'_>, line_no: usize, full: &str) -> Result<String> {
    cur.skip_ws();
    let start = cur.pos;
    while let Some(c) = cur.rest().chars().next() {
        if c.is_alphanumeric() || c == '_' {
            cur.pos += c.len_utf8();
        } else {
            break;
        }
    }
    if cur.pos == start {
        return Err(err_at(line_no, full, "ожидался идентификатор"));
    }
    Ok(cur.text[start..cur.pos].to_owned())
}

/// Распознанная в потоке ссылка на узел flowchart.
struct NodeRef {
    id: String,
    shape: Option<Shape>,
    label: Option<String>,
}

/// Сканирует узел: `id`, `id[label]`, `id(label)`, `id{label}`, `id((label))`.
fn scan_node(cur: &mut Cursor<'_>, line_no: usize, full: &str) -> Result<NodeRef> {
    let id = scan_id(cur, line_no, full)?;
    let (shape, label) = if cur.rest().starts_with("((") {
        cur.pos += 2;
        (
            Some(Shape::Circle),
            Some(scan_label_until(cur, "))", line_no, full)?),
        )
    } else if cur.eat("[") {
        (
            Some(Shape::Rect),
            Some(scan_label_until(cur, "]", line_no, full)?),
        )
    } else if cur.eat("(") {
        (
            Some(Shape::Rounded),
            Some(scan_label_until(cur, ")", line_no, full)?),
        )
    } else if cur.eat("{") {
        (
            Some(Shape::Rhombus),
            Some(scan_label_until(cur, "}", line_no, full)?),
        )
    } else {
        (None, None)
    };
    Ok(NodeRef { id, shape, label })
}

/// Сканирует метку до закрывающего токена (внутри кавычек токен не ищется).
fn scan_label_until(
    cur: &mut Cursor<'_>,
    close: &str,
    line_no: usize,
    full: &str,
) -> Result<String> {
    let start = cur.pos;
    let mut in_quotes = false;
    loop {
        let Some(c) = cur.rest().chars().next() else {
            return Err(err_at(line_no, full, "незакрытая метка узла"));
        };
        if !in_quotes && cur.rest().starts_with(close) {
            let raw = &cur.text[start..cur.pos];
            cur.pos += close.len();
            return Ok(unquote(raw));
        }
        if c == '"' {
            in_quotes = !in_quotes;
        }
        cur.pos += c.len_utf8();
    }
}

/// Сканирует оператор связи: `-->`, `-.->`, `---`, `-- метка -->`,
/// pipe-метки `-->|метка|` / `---|метка|` / `-.->|метка|`.
/// Возвращает `(метка, plain)`: `plain = true` для линии без стрелки.
fn scan_edge_op(
    cur: &mut Cursor<'_>,
    line_no: usize,
    full: &str,
) -> Result<(Option<String>, bool)> {
    let mut label = None;
    let plain = if cur.eat("-.->") || cur.eat("-->") {
        false
    } else if cur.eat("---") {
        true
    } else if cur.eat("--") {
        let Some(idx) = find_outside_quotes(cur.rest(), "--") else {
            return Err(err_at(line_no, full, "ожидалось '-->' после метки ребра"));
        };
        let raw = unquote(&cur.rest()[..idx]);
        cur.pos += idx;
        let plain = if cur.eat("-->") {
            false
        } else if cur.eat("---") {
            true
        } else {
            return Err(err_at(
                line_no,
                full,
                "ожидалось '-->' или '---' после метки ребра",
            ));
        };
        if !raw.is_empty() {
            label = Some(raw);
        }
        plain
    } else {
        return Err(err_at(
            line_no,
            full,
            "ожидалась связь '-->', '-.->', '---' или '-- метка -->'",
        ));
    };
    // Pipe-метка после оператора: `-->|Да|`, `---|путь|`, `-.->|x|`.
    if cur.eat("|") {
        let Some(idx) = find_outside_quotes(cur.rest(), "|") else {
            return Err(err_at(
                line_no,
                full,
                "незакрытая pipe-метка ребра (ожидалась '|')",
            ));
        };
        let raw = unquote(&cur.rest()[..idx]);
        cur.pos += idx + 1;
        if !raw.is_empty() {
            label = Some(raw);
        }
    }
    Ok((label, plain))
}

/// Регистрирует узел (первое упоминание) или обновляет метку/форму существующего.
fn register_node(
    nref: NodeRef,
    nodes: &mut Vec<FlowNode>,
    ids: &mut HashMap<String, usize>,
) -> usize {
    if let Some(&i) = ids.get(&nref.id) {
        if let Some(label) = nref.label {
            nodes[i].label = label;
        }
        if let Some(shape) = nref.shape {
            nodes[i].shape = shape;
        }
        return i;
    }
    let i = nodes.len();
    let label = nref.label.unwrap_or_else(|| nref.id.clone());
    nodes.push(FlowNode {
        id: nref.id.clone(),
        label,
        shape: nref.shape.unwrap_or(Shape::Rect),
    });
    ids.insert(nref.id, i);
    i
}

/// Разбирает оператор flowchart: объявление узла или цепочку связей `A --> B --> C`.
fn parse_flow_statement(
    text: &str,
    line_no: usize,
    nodes: &mut Vec<FlowNode>,
    ids: &mut HashMap<String, usize>,
    edges: &mut Vec<FlowEdge>,
) -> Result<()> {
    let mut cur = Cursor::new(text);
    let first = scan_node(&mut cur, line_no, text)?;
    let mut prev = register_node(first, nodes, ids);
    loop {
        cur.skip_ws();
        if cur.rest().is_empty() {
            return Ok(());
        }
        let (label, plain) = scan_edge_op(&mut cur, line_no, text)?;
        let next_ref = scan_node(&mut cur, line_no, text)?;
        let next = register_node(next_ref, nodes, ids);
        edges.push(FlowEdge {
            from: prev,
            to: next,
            label,
            plain,
        });
        prev = next;
    }
}

/// Первое слово — известная неподдерживаемая конструкция flowchart (пропускаем).
fn is_skippable_flow(text: &str) -> bool {
    let Some(first) = text.split_whitespace().next() else {
        return false;
    };
    matches!(
        first,
        "subgraph" | "end" | "classDef" | "class" | "click" | "style" | "linkStyle" | "direction"
    )
}

/// Разбирает заголовок `graph|flowchart TD|TB|BT|LR|RL`.
fn parse_flow_header(text: &str, line_no: usize) -> Result<Direction> {
    let mut parts = text.split_whitespace();
    let kw = parts.next().unwrap_or("");
    if kw != "graph" && kw != "flowchart" {
        return Err(err_at(
            line_no,
            text,
            "ожидался заголовок 'graph'/'flowchart'",
        ));
    }
    let dir_token = parts
        .next()
        .ok_or_else(|| err_at(line_no, text, "укажите направление: TD, TB, BT, LR или RL"))?;
    match dir_token.to_ascii_uppercase().as_str() {
        "TD" | "TB" => Ok(Direction::TopDown),
        "BT" => Ok(Direction::BottomUp),
        "LR" => Ok(Direction::LeftRight),
        "RL" => Ok(Direction::RightLeft),
        _ => Err(err_at(
            line_no,
            text,
            "неизвестное направление (ожидалось TD|TB|BT|LR|RL)",
        )),
    }
}

/// Разбирает flowchart-диаграмму целиком.
///
/// # Ошибки
/// Некорректный заголовок, битый синтаксис связей/узлов (с номером строки),
/// диаграмма без единого узла.
pub(crate) fn parse_flowchart(input: &str) -> Result<FlowAst> {
    let mut dir: Option<Direction> = None;
    let mut nodes = Vec::new();
    let mut ids = HashMap::new();
    let mut edges = Vec::new();
    let mut skipped = Vec::new();
    for (idx, raw) in input.lines().enumerate() {
        let line_no = idx + 1;
        let text = strip_comment(raw).trim();
        if text.is_empty() {
            continue;
        }
        if dir.is_none() {
            dir = Some(parse_flow_header(text, line_no)?);
            continue;
        }
        if is_skippable_flow(text) {
            skipped.push(Skipped {
                line: line_no,
                text: text.to_owned(),
            });
            continue;
        }
        parse_flow_statement(text, line_no, &mut nodes, &mut ids, &mut edges)?;
    }
    let Some(dir) = dir else {
        return Err(HarnessError::Mermaid(
            "пустой ввод: ожидалась mermaid-диаграмма (graph/flowchart ...)".into(),
        ));
    };
    if nodes.is_empty() {
        return Err(HarnessError::Mermaid(
            "диаграмма не содержит ни одного узла".into(),
        ));
    }
    Ok(FlowAst {
        dir,
        nodes,
        edges,
        skipped,
    })
}

/// Разбирает `participant X` / `participant X as Метка` (после ключевого слова).
fn parse_participant(rest: &str, line_no: usize, full: &str) -> Result<(String, Option<String>)> {
    let (id, label) = if let Some(idx) = rest.find(" as ") {
        (rest[..idx].trim(), Some(rest[idx + 4..].trim()))
    } else {
        (rest.trim(), None)
    };
    if id.is_empty() || id.contains(char::is_whitespace) {
        return Err(err_at(
            line_no,
            full,
            "некорректный идентификатор участника",
        ));
    }
    Ok((
        id.to_owned(),
        label.filter(|l| !l.is_empty()).map(str::to_owned),
    ))
}

/// Регистрирует участника (первое упоминание) или обновляет его подпись.
fn register_participant(
    id: &str,
    label: Option<String>,
    participants: &mut Vec<Participant>,
    ids: &mut HashMap<String, usize>,
) -> usize {
    if let Some(&i) = ids.get(id) {
        if let Some(l) = label {
            participants[i].label = l;
        }
        return i;
    }
    let i = participants.len();
    participants.push(Participant {
        id: id.to_owned(),
        label: label.unwrap_or_else(|| id.to_owned()),
    });
    ids.insert(id.to_owned(), i);
    i
}

/// Разбирает сообщение `A->>B: текст` / `A-->>B: текст`.
fn parse_message(
    text: &str,
    line_no: usize,
    participants: &mut Vec<Participant>,
    ids: &mut HashMap<String, usize>,
) -> Result<SeqMessage> {
    let mut cur = Cursor::new(text);
    let from = scan_id(&mut cur, line_no, text)?;
    cur.skip_ws();
    let dotted = if cur.eat("-->>") {
        true
    } else if cur.eat("->>") {
        false
    } else {
        return Err(err_at(line_no, text, "ожидалась стрелка '->>' или '-->>'"));
    };
    let to = scan_id(&mut cur, line_no, text)?;
    cur.skip_ws();
    if !cur.eat(":") {
        return Err(err_at(line_no, text, "ожидалось ':' и текст сообщения"));
    }
    let label = unquote(cur.rest());
    let from = register_participant(&from, None, participants, ids);
    let to = register_participant(&to, None, participants, ids);
    Ok(SeqMessage {
        from,
        to,
        label,
        dotted,
    })
}

/// Разбирает `left of X: текст` / `right of X: текст` (после `Note `).
fn parse_note(
    rest: &str,
    line_no: usize,
    full: &str,
    participants: &mut Vec<Participant>,
    ids: &mut HashMap<String, usize>,
) -> Result<SeqNote> {
    let (side, tail) = if let Some(t) = rest.strip_prefix("left of ") {
        (NoteSide::Left, t)
    } else if let Some(t) = rest.strip_prefix("right of ") {
        (NoteSide::Right, t)
    } else {
        return Err(err_at(
            line_no,
            full,
            "ожидалось 'Note left of' или 'Note right of'",
        ));
    };
    let Some(colon) = tail.find(':') else {
        return Err(err_at(line_no, full, "ожидалось ':' в Note"));
    };
    let id = tail[..colon].trim();
    let text = tail[colon + 1..].trim();
    if id.is_empty() || text.is_empty() {
        return Err(err_at(line_no, full, "пустой участник или текст Note"));
    }
    let participant = register_participant(id, None, participants, ids);
    Ok(SeqNote {
        participant,
        side,
        text: text.to_owned(),
    })
}

/// Первое слово — известная неподдерживаемая конструкция sequence (пропускаем).
fn is_skippable_seq(text: &str) -> bool {
    let Some(first) = text.split_whitespace().next() else {
        return false;
    };
    matches!(
        first,
        "loop"
            | "alt"
            | "else"
            | "opt"
            | "par"
            | "and"
            | "critical"
            | "break"
            | "end"
            | "autonumber"
            | "activate"
            | "deactivate"
            | "create"
            | "destroy"
            | "rect"
            | "box"
            | "title"
            | "link"
            | "links"
    )
}

/// Разбирает sequence-диаграмму целиком.
///
/// # Ошибки
/// Некорректный заголовок, битое сообщение/заметка (с номером строки),
/// диаграмма без участников.
pub(crate) fn parse_sequence(input: &str) -> Result<SeqAst> {
    let mut header_seen = false;
    let mut participants = Vec::new();
    let mut ids = HashMap::new();
    let mut items = Vec::new();
    let mut skipped = Vec::new();
    for (idx, raw) in input.lines().enumerate() {
        let line_no = idx + 1;
        let text = strip_comment(raw).trim();
        if text.is_empty() {
            continue;
        }
        if !header_seen {
            if text == "sequenceDiagram" {
                header_seen = true;
                continue;
            }
            return Err(err_at(
                line_no,
                text,
                "ожидался заголовок 'sequenceDiagram'",
            ));
        }
        if let Some(rest) = text
            .strip_prefix("participant ")
            .or_else(|| text.strip_prefix("actor "))
        {
            let (id, label) = parse_participant(rest, line_no, text)?;
            register_participant(&id, label, &mut participants, &mut ids);
            continue;
        }
        if text.starts_with("Note over") || is_skippable_seq(text) {
            skipped.push(Skipped {
                line: line_no,
                text: text.to_owned(),
            });
            continue;
        }
        if let Some(rest) = text.strip_prefix("Note ") {
            let note = parse_note(rest, line_no, text, &mut participants, &mut ids)?;
            items.push(SeqItem::Note(note));
            continue;
        }
        let msg = parse_message(text, line_no, &mut participants, &mut ids)?;
        items.push(SeqItem::Message(msg));
    }
    if !header_seen {
        return Err(HarnessError::Mermaid(
            "пустой ввод: ожидался 'sequenceDiagram'".into(),
        ));
    }
    if participants.is_empty() {
        return Err(HarnessError::Mermaid(
            "sequenceDiagram не содержит участников".into(),
        ));
    }
    Ok(SeqAst {
        participants,
        items,
        skipped,
    })
}

// ===== erDiagram =====

/// Сканирует идентификатор сущности ER (буквы/цифры/`_`/`-`).
fn scan_er_id(cur: &mut Cursor<'_>, line_no: usize, full: &str) -> Result<String> {
    cur.skip_ws();
    let start = cur.pos;
    while let Some(c) = cur.rest().chars().next() {
        if c.is_alphanumeric() || c == '_' || c == '-' {
            cur.pos += c.len_utf8();
        } else {
            break;
        }
    }
    if cur.pos == start {
        return Err(err_at(line_no, full, "ожидался идентификатор сущности"));
    }
    Ok(cur.text[start..cur.pos].to_owned())
}

/// Разбирает двухсимвольную кардинальность crow's foot. `left` — левая
/// сторона связи (маркеры `|`/`}`/`o` + `|`/`o`), иначе правая (`|`/`o` +
/// `|`/`{`).
fn scan_er_card(cur: &mut Cursor<'_>, left: bool, line_no: usize, full: &str) -> Result<ErCard> {
    let pair = cur.rest().get(..2);
    let card = if left {
        match pair {
            Some("||") => ErCard::One,
            Some("|o") => ErCard::ZeroOne,
            Some("}|") => ErCard::OneMany,
            Some("}o") => ErCard::ZeroMany,
            _ => {
                return Err(err_at(
                    line_no,
                    full,
                    "ожидалась кардинальность '||', '|o', '}|' или '}o'",
                ));
            }
        }
    } else {
        match pair {
            Some("||") => ErCard::One,
            Some("o|") => ErCard::ZeroOne,
            Some("|{") => ErCard::OneMany,
            Some("o{") => ErCard::ZeroMany,
            _ => {
                return Err(err_at(
                    line_no,
                    full,
                    "ожидалась кардинальность '||', 'o|', '|{' или 'o{'",
                ));
            }
        }
    };
    cur.pos += 2;
    Ok(card)
}

/// Регистрирует сущность ER (первое упоминание), возвращает её индекс.
fn register_er_entity(
    id: &str,
    entities: &mut Vec<ErEntity>,
    ids: &mut HashMap<String, usize>,
) -> usize {
    if let Some(&i) = ids.get(id) {
        return i;
    }
    let i = entities.len();
    entities.push(ErEntity {
        id: id.to_owned(),
        attributes: Vec::new(),
    });
    ids.insert(id.to_owned(), i);
    i
}

/// Разбирает строку атрибута: `тип имя [PK|FK|UK|комментарий …]`.
fn parse_er_attribute(text: &str, line_no: usize) -> Result<ErAttribute> {
    let mut parts = text.split_whitespace();
    let typ = parts.next().unwrap_or("");
    let Some(name) = parts.next() else {
        return Err(err_at(
            line_no,
            text,
            "ожидались тип и имя атрибута (например, 'string name')",
        ));
    };
    let rest: Vec<&str> = parts.collect();
    let extra = if rest.is_empty() {
        None
    } else {
        // Кавычки комментариев в рамке не нужны.
        Some(rest.join(" ").replace('"', ""))
    };
    Ok(ErAttribute {
        typ: typ.to_owned(),
        name: name.to_owned(),
        extra,
    })
}

/// Разбирает связь ER: `A ||--o{ B : метка` (`--` identifying, `..` — нет).
fn parse_er_relation(
    text: &str,
    line_no: usize,
    entities: &mut Vec<ErEntity>,
    ids: &mut HashMap<String, usize>,
    relations: &mut Vec<ErRelation>,
) -> Result<()> {
    let mut cur = Cursor::new(text);
    let from_id = scan_er_id(&mut cur, line_no, text)?;
    cur.skip_ws();
    let from_card = scan_er_card(&mut cur, true, line_no, text)?;
    let identifying = if cur.eat("--") {
        true
    } else if cur.eat("..") {
        false
    } else {
        return Err(err_at(
            line_no,
            text,
            "ожидалась связь '--' (identifying) или '..' (non-identifying)",
        ));
    };
    let to_card = scan_er_card(&mut cur, false, line_no, text)?;
    let to_id = scan_er_id(&mut cur, line_no, text)?;
    cur.skip_ws();
    if !cur.eat(":") {
        return Err(err_at(line_no, text, "ожидалось ':' и метка связи"));
    }
    let label = unquote(cur.rest());
    if label.is_empty() {
        return Err(err_at(line_no, text, "пустая метка связи"));
    }
    let from = register_er_entity(&from_id, entities, ids);
    let to = register_er_entity(&to_id, entities, ids);
    relations.push(ErRelation {
        from,
        to,
        from_card,
        to_card,
        label,
        identifying,
    });
    Ok(())
}

/// Разбирает `erDiagram` целиком (ADR-009): сущности с блоками атрибутов
/// `{ … }` и связи `A ||--o{ B : метка`.
///
/// # Ошибки
/// Некорректный заголовок, битая связь/атрибут (с номером строки),
/// незакрытый блок, диаграмма без сущностей.
pub(crate) fn parse_er(input: &str) -> Result<ErAst> {
    let mut header_seen = false;
    let mut entities: Vec<ErEntity> = Vec::new();
    let mut ids = HashMap::new();
    let mut relations = Vec::new();
    let skipped = Vec::new();
    // Индекс сущности с открытым блоком атрибутов.
    let mut open_block: Option<usize> = None;
    for (idx, raw) in input.lines().enumerate() {
        let line_no = idx + 1;
        let text = strip_comment(raw).trim();
        if text.is_empty() {
            continue;
        }
        if !header_seen {
            if text == "erDiagram" {
                header_seen = true;
                continue;
            }
            return Err(err_at(line_no, text, "ожидался заголовок 'erDiagram'"));
        }
        if let Some(ei) = open_block {
            if text == "}" {
                open_block = None;
                continue;
            }
            if text.ends_with('{') {
                return Err(err_at(
                    line_no,
                    text,
                    "вложенные блоки в erDiagram не поддерживаются",
                ));
            }
            let attr = parse_er_attribute(text, line_no)?;
            entities[ei].attributes.push(attr);
            continue;
        }
        if text == "}" {
            return Err(err_at(line_no, text, "закрывающая '}' вне блока сущности"));
        }
        if let Some(head) = text.strip_suffix('{') {
            let head = head.trim();
            if head.contains('[') {
                return Err(err_at(
                    line_no,
                    text,
                    "алиасы сущностей (`id[\"метка\"]`) не поддерживаются",
                ));
            }
            let mut cur = Cursor::new(head);
            let id = scan_er_id(&mut cur, line_no, text)?;
            cur.skip_ws();
            if !cur.rest().is_empty() {
                return Err(err_at(
                    line_no,
                    text,
                    "ожидался идентификатор сущности перед '{'",
                ));
            }
            open_block = Some(register_er_entity(&id, &mut entities, &mut ids));
            continue;
        }
        parse_er_relation(text, line_no, &mut entities, &mut ids, &mut relations)?;
    }
    if !header_seen {
        return Err(HarnessError::Mermaid(
            "пустой ввод: ожидался 'erDiagram'".into(),
        ));
    }
    if open_block.is_some() {
        return Err(HarnessError::Mermaid(
            "незакрытый блок атрибутов сущности (нет '}')".into(),
        ));
    }
    if entities.is_empty() {
        return Err(HarnessError::Mermaid(
            "erDiagram не содержит ни одной сущности".into(),
        ));
    }
    Ok(ErAst {
        entities,
        relations,
        skipped,
    })
}

// ===== C4 (C4Context/C4Container/C4Component) =====

/// Разбирает вызов `Keyword(arg, "…")` с необязательным `{` в конце.
/// Возвращает `(keyword, содержимое скобок)`; `None` — строка не похожа
/// на вызов.
fn parse_c4_call(text: &str) -> Option<(&str, &str)> {
    let open = text.find('(')?;
    let kw = text[..open].trim();
    if kw.is_empty() || kw.contains(char::is_whitespace) {
        return None;
    }
    let close = text.rfind(')')?;
    if close < open {
        return None;
    }
    let tail = text[close + 1..].trim();
    if !tail.is_empty() && tail != "{" {
        return None;
    }
    Some((kw, &text[open + 1..close]))
}

/// Разбивает аргументы вызова C4 по запятым вне кавычек; кавычки снимает.
fn split_c4_args(inner: &str) -> Vec<String> {
    let mut args = Vec::new();
    let mut cur = String::new();
    let mut in_quotes = false;
    for c in inner.chars() {
        match c {
            '"' => {
                in_quotes = !in_quotes;
                cur.push(c);
            }
            ',' if !in_quotes => {
                args.push(unquote(&cur));
                cur.clear();
            }
            _ => cur.push(c),
        }
    }
    if !cur.trim().is_empty() {
        args.push(unquote(&cur));
    }
    args
}

/// Ключевые слова границ C4 (содержимое разбирается, рамки не рисуются).
fn is_c4_boundary(kw: &str) -> bool {
    matches!(
        kw,
        "Enterprise_Boundary" | "System_Boundary" | "Container_Boundary" | "Boundary"
    )
}

/// Ключевое слово элемента C4 → (стереотип, external, хранилище).
fn c4_element_kind(kw: &str) -> Option<(C4ElemKind, bool, Option<&'static str>)> {
    let (stem, external) = match kw.strip_suffix("_Ext") {
        Some(s) => (s, true),
        None => (kw, false),
    };
    let (stem, store) = if let Some(s) = stem.strip_suffix("Db") {
        (s, Some("db"))
    } else if let Some(s) = stem.strip_suffix("Queue") {
        (s, Some("queue"))
    } else {
        (stem, None)
    };
    let kind = match stem {
        "Person" => C4ElemKind::Person,
        "System" => C4ElemKind::System,
        "Container" => C4ElemKind::Container,
        "Component" => C4ElemKind::Component,
        _ => return None,
    };
    Some((kind, external, store))
}

/// Ключевое слово связи C4: `Rel`, `Rel_U/D/L/R/Back`, `BiRel`.
fn c4_rel_bidir(kw: &str) -> Option<bool> {
    if kw == "BiRel" {
        Some(true)
    } else if kw == "Rel" || kw.starts_with("Rel_") {
        Some(false)
    } else {
        None
    }
}

/// Неразрешённая связь C4 (алиасы разрешаются после разбора всех строк).
struct C4PendingRel {
    from: String,
    to: String,
    label: String,
    bidir: bool,
    line: usize,
}

/// Разбирает C4-диаграмму (`C4Context`/`C4Container`/`C4Component`) целиком
/// (ADR-009). Границы `*_Boundary`, стили, раскладка и легенды пропускаются
/// с предупреждением; `title` — тоже.
///
/// # Ошибки
/// Некорректный заголовок, битый вызов элемента/связи (с номером строки),
/// связь на неизвестный алиас, диаграмма без элементов.
pub(crate) fn parse_c4(input: &str) -> Result<C4Ast> {
    let mut header_seen = false;
    let mut elements = Vec::new();
    let mut ids: HashMap<String, usize> = HashMap::new();
    let mut pending: Vec<C4PendingRel> = Vec::new();
    let mut skipped = Vec::new();
    for (idx, raw) in input.lines().enumerate() {
        let line_no = idx + 1;
        let text = strip_comment(raw).trim();
        if text.is_empty() {
            continue;
        }
        if !header_seen {
            if matches!(text, "C4Context" | "C4Container" | "C4Component") {
                header_seen = true;
                continue;
            }
            return Err(err_at(
                line_no,
                text,
                "ожидался заголовок 'C4Context', 'C4Container' или 'C4Component'",
            ));
        }
        // Закрытие boundary-блока.
        if text == "}" {
            skipped.push(Skipped {
                line: line_no,
                text: text.to_owned(),
            });
            continue;
        }
        // `title …` — строка без скобок.
        if text.split_whitespace().next() == Some("title") {
            skipped.push(Skipped {
                line: line_no,
                text: text.to_owned(),
            });
            continue;
        }
        let Some((kw, inner)) = parse_c4_call(text) else {
            return Err(err_at(
                line_no,
                text,
                "ожидалась C4-инструкция вида Keyword(аргументы)",
            ));
        };
        if is_c4_boundary(kw) {
            skipped.push(Skipped {
                line: line_no,
                text: text.to_owned(),
            });
            continue;
        }
        if let Some((kind, external, store)) = c4_element_kind(kw) {
            let args = split_c4_args(inner);
            if args.len() < 2 {
                return Err(err_at(
                    line_no,
                    text,
                    "ожидались аргументы (alias, \"label\"[, \"tech\"[, \"desc\"]])",
                ));
            }
            let alias = args[0].clone();
            if alias.is_empty() || alias.contains(char::is_whitespace) {
                return Err(err_at(line_no, text, "некорректный алиас элемента"));
            }
            let label = args[1].clone();
            let tech = if matches!(kind, C4ElemKind::Container | C4ElemKind::Component) {
                args.get(2).filter(|t| !t.is_empty()).cloned()
            } else {
                None
            };
            if let Some(&i) = ids.get(&alias) {
                // Повторное объявление — обновляем (как узлы flowchart).
                elements[i] = C4Element {
                    alias,
                    kind,
                    external,
                    store: store.map(str::to_owned),
                    label,
                    tech,
                };
            } else {
                ids.insert(alias.clone(), elements.len());
                elements.push(C4Element {
                    alias,
                    kind,
                    external,
                    store: store.map(str::to_owned),
                    label,
                    tech,
                });
            }
            continue;
        }
        if let Some(bidir) = c4_rel_bidir(kw) {
            let args = split_c4_args(inner);
            if args.len() < 2 {
                return Err(err_at(
                    line_no,
                    text,
                    "ожидались аргументы связи (from, to[, \"label\"])",
                ));
            }
            pending.push(C4PendingRel {
                from: args[0].clone(),
                to: args[1].clone(),
                label: args.get(2).cloned().unwrap_or_default(),
                bidir,
                line: line_no,
            });
            continue;
        }
        // Стили, раскладка, легенды и прочее известное/неизвестное — пропускаем.
        skipped.push(Skipped {
            line: line_no,
            text: text.to_owned(),
        });
    }
    if !header_seen {
        return Err(HarnessError::Mermaid(
            "пустой ввод: ожидался 'C4Context'/'C4Container'/'C4Component'".into(),
        ));
    }
    let mut relations = Vec::with_capacity(pending.len());
    for rel in pending {
        let Some(&from) = ids.get(&rel.from) else {
            return Err(err_at(
                rel.line,
                &rel.from,
                "связь ссылается на неизвестный алиас",
            ));
        };
        let Some(&to) = ids.get(&rel.to) else {
            return Err(err_at(
                rel.line,
                &rel.to,
                "связь ссылается на неизвестный алиас",
            ));
        };
        relations.push(C4Relation {
            from,
            to,
            label: rel.label,
            bidir: rel.bidir,
        });
    }
    if elements.is_empty() {
        return Err(HarnessError::Mermaid(
            "C4-диаграмма не содержит ни одного элемента".into(),
        ));
    }
    Ok(C4Ast {
        elements,
        relations,
        skipped,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_chain_and_updates_labels() {
        let ast = parse_flowchart("graph TD\nA --> B[Bee] --> C\nA[Ай]").unwrap();
        assert_eq!(ast.nodes.len(), 3);
        assert_eq!(ast.edges.len(), 2);
        assert_eq!(ast.nodes[0].label, "Ай");
        assert_eq!(ast.nodes[1].label, "Bee");
        assert_eq!(ast.nodes[2].label, "C");
    }

    #[test]
    fn parses_pipe_edge_labels() {
        // Самый частый синтаксис меток у моделей: -->|Да|.
        let ast = parse_flowchart(
            "graph TD\nA[Клиент] --> B{Авторизован?}\nB -->|Да| C[Кабинет]\nB -->|Нет| D[Логин]\nC -.->|сессия| D",
        )
        .unwrap();
        assert_eq!(ast.edges.len(), 4);
        assert_eq!(ast.edges[1].label.as_deref(), Some("Да"));
        assert_eq!(ast.edges[2].label.as_deref(), Some("Нет"));
        assert_eq!(ast.edges[2].to, 3, "D — четвёртый узел");
        assert_eq!(ast.nodes[1].shape, Shape::Rhombus);
    }

    #[test]
    fn pipe_label_needs_closing_bar() {
        let err = parse_flowchart("graph TD\nA -->|нет закрывающей B\n").unwrap_err();
        assert!(err.to_string().contains("pipe-метка"), "{}", err);
    }

    #[test]
    fn parses_all_shapes_and_edge_kinds() {
        let ast = parse_flowchart(
            "flowchart LR\nA[rect] --> B(rounded)\nB -.-> C{rhombus}\nC --- D((circle))\nA -- метка --> D",
        )
        .unwrap();
        assert_eq!(ast.nodes.len(), 4);
        assert_eq!(ast.edges.len(), 4);
        assert_eq!(ast.nodes[2].shape, Shape::Rhombus);
        assert_eq!(ast.nodes[3].shape, Shape::Circle);
        assert!(ast.edges[2].plain);
        assert_eq!(ast.edges[3].label.as_deref(), Some("метка"));
    }

    #[test]
    fn quoted_label_protects_arrow_inside() {
        let ast = parse_flowchart("graph LR\nA[\"текст с --> внутри\"] --> B").unwrap();
        assert_eq!(ast.nodes[0].label, "текст с --> внутри");
        assert_eq!(ast.edges.len(), 1);
    }

    #[test]
    fn skips_unknown_constructs_with_warning() {
        let ast = parse_flowchart(
            "flowchart LR\nsubgraph x\nA --> B\nend\nclick A cb\nstyle A fill:#f9f",
        )
        .unwrap();
        assert_eq!(ast.skipped.len(), 4);
        assert_eq!(ast.edges.len(), 1);
    }

    #[test]
    fn errors_have_line_numbers() {
        let err = parse_flowchart("graph TD\nA --> B\nC -->\n").unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("строка 3"), "нет номера строки: {msg}");
    }

    #[test]
    fn parses_sequence_parts() {
        let ast = parse_sequence(
            "sequenceDiagram\nparticipant A as Alpha\nparticipant B\nA->>B: hi\nNote left of B: hmm\nB-->>A: ok",
        )
        .unwrap();
        assert_eq!(ast.participants.len(), 2);
        assert_eq!(ast.participants[0].label, "Alpha");
        assert_eq!(ast.items.len(), 3);
        assert!(matches!(&ast.items[2], SeqItem::Message(m) if m.dotted));
    }

    #[test]
    fn parses_er_entities_attributes_and_relations() {
        let ast = parse_er(
            "erDiagram\n\
             CUSTOMER ||--o{ ORDER : places\n\
             ORDER ||--|{ LINE-ITEM : contains\n\
             CUSTOMER {\n\
             \x20   string name\n\
             \x20   int id PK\n\
             }\n",
        )
        .unwrap();
        assert_eq!(ast.entities.len(), 3);
        assert_eq!(ast.relations.len(), 2);
        let customer = &ast.entities[0];
        assert_eq!(customer.id, "CUSTOMER");
        assert_eq!(customer.attributes.len(), 2);
        assert_eq!(customer.attributes[0].typ, "string");
        assert_eq!(customer.attributes[0].name, "name");
        assert_eq!(customer.attributes[1].extra.as_deref(), Some("PK"));
        let rel = &ast.relations[0];
        assert_eq!(rel.from_card, ErCard::One);
        assert_eq!(rel.to_card, ErCard::ZeroMany);
        assert!(rel.identifying);
        assert_eq!(rel.label, "places");
    }

    #[test]
    fn parses_er_all_cardinalities_and_dotted_relation() {
        let ast = parse_er(
            "erDiagram\n\
             A |o..o| B : maybe\n\
             B }|--|{ C : many\n",
        )
        .unwrap();
        assert_eq!(ast.relations.len(), 2);
        assert_eq!(ast.relations[0].from_card, ErCard::ZeroOne);
        assert_eq!(ast.relations[0].to_card, ErCard::ZeroOne);
        assert!(!ast.relations[0].identifying, "`..` — non-identifying");
        assert_eq!(ast.relations[1].from_card, ErCard::OneMany);
        assert_eq!(ast.relations[1].to_card, ErCard::OneMany);
    }

    #[test]
    fn er_lowering_places_cardinality_in_edge_label() {
        let ast = parse_er("erDiagram\nCUSTOMER ||--o{ ORDER : places\n").unwrap();
        let flow = ast.to_flow();
        assert_eq!(flow.edges.len(), 1);
        assert_eq!(
            flow.edges[0].label.as_deref(),
            Some("places (1:0..*)"),
            "метка ребра с множественностью"
        );
        assert!(!flow.edges[0].plain, "identifying — со стрелкой");
        assert_eq!(flow.nodes[0].label, "CUSTOMER");
    }

    #[test]
    fn er_lowering_multiline_label_with_separator() {
        let ast = parse_er("erDiagram\nT {\n  string a\n  int b PK\n}\n").unwrap();
        let flow = ast.to_flow();
        assert_eq!(
            flow.nodes[0].label, "T\n\nstring a\nint b PK",
            "имя, пустая строка-разделитель, атрибуты"
        );
    }

    #[test]
    fn er_errors_have_line_numbers() {
        let err = parse_er("erDiagram\nA ||--o{ B : ok\nA ~~ B : bad\n").unwrap_err();
        assert!(err.to_string().contains("строка 3"), "{}", err);
        // Незакрытый блок атрибутов.
        assert!(parse_er("erDiagram\nA {\n  string x\n").is_err());
        // Пустая метка связи.
        assert!(parse_er("erDiagram\nA ||--o{ B :\n").is_err());
        // Диаграмма без сущностей.
        assert!(parse_er("erDiagram\n%% только комментарий\n").is_err());
        // Алиасы сущностей не поддерживаются.
        assert!(parse_er("erDiagram\nA[\"Ай\"] {\n  string x\n}\n").is_err());
    }

    #[test]
    fn parses_c4_context_elements_and_relations() {
        let ast = parse_c4(
            "C4Context\n\
             Person(user, \"Пользователь\", \"Клиент банка\")\n\
             System_Ext(mail, \"E-mail\", \"Exchange\")\n\
             System(banking, \"Internet Banking\")\n\
             Rel(user, banking, \"Использует\")\n\
             BiRel(banking, mail, \"Шлёт письма\")\n",
        )
        .unwrap();
        assert_eq!(ast.elements.len(), 3);
        assert_eq!(ast.elements[0].kind, C4ElemKind::Person);
        assert_eq!(ast.elements[1].kind, C4ElemKind::System);
        assert!(ast.elements[1].external, "_Ext детектируется");
        assert_eq!(ast.relations.len(), 2);
        assert!(!ast.relations[0].bidir);
        assert!(ast.relations[1].bidir);
        let flow = ast.to_flow();
        assert_eq!(flow.nodes[0].label, "«person»\nПользователь");
        assert_eq!(flow.nodes[1].label, "«system, external»\nE-mail");
        assert!(flow.edges[1].plain, "BiRel — линия без стрелки");
    }

    #[test]
    fn parses_c4_container_with_tech_and_boundary() {
        let ast = parse_c4(
            "C4Container\n\
             title Контейнеры\n\
             System_Boundary(sys, \"Платформа\") {\n\
             Container(api, \"API\", \"Rust\", \"HTTP-точка входа\")\n\
             ContainerDb(db, \"БД\", \"PostgreSQL\")\n\
             }\n\
             UpdateElementStyle(api, $bgColor=\"red\")\n\
             Rel(api, db, \"SQL\", \"JDBC\")\n",
        )
        .unwrap();
        assert_eq!(ast.elements.len(), 2, "boundary не элемент");
        assert_eq!(ast.elements[1].store.as_deref(), Some("db"));
        assert_eq!(
            ast.elements[0].tech.as_deref(),
            Some("Rust"),
            "технология третьим аргументом"
        );
        // title, boundary-открытие, '}' и UpdateElementStyle — пропущены.
        assert_eq!(ast.skipped.len(), 4, "{:?}", ast.skipped);
        assert_eq!(ast.relations.len(), 1);
        let flow = ast.to_flow();
        assert_eq!(
            flow.nodes[0].label, "«container»\nAPI\n[Rust]",
            "стереотип + имя + технология"
        );
    }

    #[test]
    fn c4_errors_on_unknown_alias_and_garbage() {
        let err = parse_c4("C4Context\nSystem(a, \"A\")\nRel(a, ghost, \"x\")\n").unwrap_err();
        assert!(err.to_string().contains("неизвестный алиас"), "{}", err);
        assert!(
            parse_c4("C4Context\nSystem(a)\n").is_err(),
            "без label — ошибка"
        );
        assert!(parse_c4("C4Context\nэто не вызов\n").is_err());
        assert!(
            parse_c4("C4Deployment\nSystem(a, \"A\")\n").is_err(),
            "заголовок не C4"
        );
        assert!(
            parse_c4("C4Context\n%% пусто\n").is_err(),
            "без элементов — ошибка"
        );
    }
}
