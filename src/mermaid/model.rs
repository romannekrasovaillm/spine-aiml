//! Внутренние структуры AST диаграмм (flowchart, sequence, ER, C4).
//!
//! ER (`erDiagram`) и C4 (`C4Context`/`C4Container`/`C4Component`) имеют свои
//! AST ([`ErAst`], [`C4Ast`]) и понижаются к [`FlowAst`] (ADR-009): рендер
//! переиспользует единственный layout/draw-пайплайн.

/// Направление flowchart-диаграммы.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Direction {
    /// Сверху вниз (`TD`/`TB`).
    TopDown,
    /// Снизу вверх (`BT`).
    BottomUp,
    /// Слева направо (`LR`).
    LeftRight,
    /// Справа налево (`RL`).
    RightLeft,
}

impl Direction {
    /// Горизонтальное ли направление (`LR`/`RL`).
    pub(crate) fn is_horizontal(self) -> bool {
        matches!(self, Self::LeftRight | Self::RightLeft)
    }
}

/// Форма узла flowchart.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Shape {
    /// `A[label]` — прямоугольник.
    Rect,
    /// `A(label)` — скруглённый прямоугольник.
    Rounded,
    /// `A{label}` — ромб (упрощённое обрамление `< … >`, `╱ ╲`).
    Rhombus,
    /// `A((label))` — круг (обрамление `(( … ))`).
    Circle,
}

/// Узел flowchart.
#[derive(Debug, Clone)]
pub(crate) struct FlowNode {
    /// Идентификатор узла.
    #[allow(dead_code)] // читается в тестах раскладки и нужен для диагностики графа
    pub id: String,
    /// Подпись (по умолчанию равна идентификатору).
    pub label: String,
    /// Форма рамки.
    pub shape: Shape,
}

/// Ребро flowchart (узлы — индексы в `FlowAst::nodes`).
#[derive(Debug, Clone)]
pub(crate) struct FlowEdge {
    /// Узел-источник.
    pub from: usize,
    /// Узел-назначение.
    pub to: usize,
    /// Метка ребра (`-- метка -->`).
    pub label: Option<String>,
    /// `---` — линия без стрелки.
    pub plain: bool,
}

/// Предупреждение о пропущенной (неподдерживаемой) конструкции.
#[derive(Debug, Clone)]
pub(crate) struct Skipped {
    /// Номер строки во входе (с 1).
    pub line: usize,
    /// Текст строки.
    pub text: String,
}

/// AST flowchart-диаграммы.
#[derive(Debug)]
pub(crate) struct FlowAst {
    /// Направление.
    pub dir: Direction,
    /// Узлы в порядке первого упоминания.
    pub nodes: Vec<FlowNode>,
    /// Рёбра в порядке объявления.
    pub edges: Vec<FlowEdge>,
    /// Пропущенные конструкции.
    pub skipped: Vec<Skipped>,
}

/// Участник sequence-диаграммы.
#[derive(Debug, Clone)]
pub(crate) struct Participant {
    /// Идентификатор (`participant X as Label` → `X`).
    #[allow(dead_code)] // идентификатор хранится для отладки/будущих проверок связей
    pub id: String,
    /// Отображаемая подпись (по умолчанию = идентификатор).
    pub label: String,
}

/// Сообщение между участниками.
#[derive(Debug, Clone)]
pub(crate) struct SeqMessage {
    /// Отправитель (индекс в `SeqAst::participants`).
    pub from: usize,
    /// Получатель (индекс; равен `from` при самовызове).
    pub to: usize,
    /// Текст сообщения (после `:`).
    pub label: String,
    /// `-->>` — пунктир (рисуется `┄`).
    pub dotted: bool,
}

/// Сторона заметки относительно линии жизни.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum NoteSide {
    /// `Note left of X`.
    Left,
    /// `Note right of X`.
    Right,
}

/// Заметка рядом с линией жизни участника.
#[derive(Debug, Clone)]
pub(crate) struct SeqNote {
    /// Участник (индекс).
    pub participant: usize,
    /// Сторона.
    pub side: NoteSide,
    /// Текст заметки.
    pub text: String,
}

/// Элемент sequence-диаграммы в порядке объявления.
#[derive(Debug, Clone)]
pub(crate) enum SeqItem {
    /// Сообщение.
    Message(SeqMessage),
    /// Заметка.
    Note(SeqNote),
}

/// AST sequence-диаграммы.
#[derive(Debug)]
pub(crate) struct SeqAst {
    /// Участники в порядке объявления/первого упоминания.
    pub participants: Vec<Participant>,
    /// Сообщения и заметки сверху вниз.
    pub items: Vec<SeqItem>,
    /// Пропущенные конструкции.
    pub skipped: Vec<Skipped>,
}

/// Кардинальность стороны ER-связи (crow's foot).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ErCard {
    /// `||` — ровно один.
    One,
    /// `|o` слева / `o|` справа — ноль или один.
    ZeroOne,
    /// `}|` слева / `|{` справа — один или более.
    OneMany,
    /// `}o` слева / `o{` справа — ноль или более.
    ZeroMany,
}

impl ErCard {
    /// Текстовая форма множественности (UML-стиль: `1`, `0..1`, `1..*`, `0..*`).
    pub(crate) fn multiplicity(self) -> &'static str {
        match self {
            Self::One => "1",
            Self::ZeroOne => "0..1",
            Self::OneMany => "1..*",
            Self::ZeroMany => "0..*",
        }
    }
}

/// Атрибут ER-сущности: `тип имя [ключи/комментарий]`.
#[derive(Debug, Clone)]
pub(crate) struct ErAttribute {
    /// Тип атрибута (`string`, `int`, …).
    pub typ: String,
    /// Имя атрибута.
    pub name: String,
    /// Хвост строки (`PK`, `FK`, комментарий) без кавычек; `None` — не задан.
    pub extra: Option<String>,
}

/// Сущность ER-диаграммы.
#[derive(Debug, Clone)]
pub(crate) struct ErEntity {
    /// Имя сущности.
    pub id: String,
    /// Атрибуты из блока `{ … }` (может быть пустым).
    pub attributes: Vec<ErAttribute>,
}

/// Связь ER-диаграммы (сущности — индексы в `ErAst::entities`).
#[derive(Debug, Clone)]
pub(crate) struct ErRelation {
    /// Сущность-источник (левая сторона).
    pub from: usize,
    /// Сущность-назначение (правая сторона).
    pub to: usize,
    /// Кардинальность левой стороны.
    pub from_card: ErCard,
    /// Кардинальность правой стороны.
    pub to_card: ErCard,
    /// Метка связи (после `:`).
    pub label: String,
    /// `--` — identifying (стрелка); `..` — non-identifying (линия).
    pub identifying: bool,
}

/// AST `erDiagram`.
#[derive(Debug)]
pub(crate) struct ErAst {
    /// Сущности в порядке первого упоминания.
    pub entities: Vec<ErEntity>,
    /// Связи в порядке объявления.
    pub relations: Vec<ErRelation>,
    /// Пропущенные конструкции.
    pub skipped: Vec<Skipped>,
}

impl ErAst {
    /// Понижение к flowchart TD (ADR-009): сущность → узел с многострочной
    /// меткой (имя, пустая строка-разделитель, атрибуты `тип имя хвост`),
    /// связь → ребро с меткой `label (from:to)` в UML-множественности.
    pub(crate) fn to_flow(&self) -> FlowAst {
        let nodes = self
            .entities
            .iter()
            .map(|e| {
                let mut label = e.id.clone();
                if !e.attributes.is_empty() {
                    label.push('\n'); // пустая строка — разделитель рамки в draw
                    for a in &e.attributes {
                        label.push('\n');
                        label.push_str(&a.typ);
                        label.push(' ');
                        label.push_str(&a.name);
                        if let Some(extra) = &a.extra {
                            label.push(' ');
                            label.push_str(extra);
                        }
                    }
                }
                FlowNode {
                    id: e.id.clone(),
                    label,
                    shape: Shape::Rect,
                }
            })
            .collect();
        let edges = self
            .relations
            .iter()
            .map(|r| FlowEdge {
                from: r.from,
                to: r.to,
                label: Some(format!(
                    "{} ({}:{})",
                    r.label,
                    r.from_card.multiplicity(),
                    r.to_card.multiplicity()
                )),
                plain: !r.identifying,
            })
            .collect();
        FlowAst {
            dir: Direction::TopDown,
            nodes,
            edges,
            skipped: self.skipped.clone(),
        }
    }
}

/// Базовый стереотип C4-элемента.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum C4ElemKind {
    /// `Person[_Ext]`.
    Person,
    /// `System[_Ext|Db|Queue|…]`.
    System,
    /// `Container[Db|Queue|…]`.
    Container,
    /// `Component[Db|…]`.
    Component,
}

/// Элемент C4-диаграммы.
#[derive(Debug, Clone)]
pub(crate) struct C4Element {
    /// Алиас (первый позиционный аргумент).
    pub alias: String,
    /// Базовый стереотип.
    pub kind: C4ElemKind,
    /// Суффикс `_Ext` — внешний элемент.
    pub external: bool,
    /// Хранилище/очередь (`Db`/`Queue`), если задано суффиксом.
    pub store: Option<String>,
    /// Отображаемое имя (второй аргумент).
    pub label: String,
    /// Технология (третий аргумент `Container`/`Component`).
    pub tech: Option<String>,
}

/// Связь C4 (алиасы разрешаются в индексы после разбора всех строк).
#[derive(Debug, Clone)]
pub(crate) struct C4Relation {
    /// Алиас источника.
    pub from: usize,
    /// Алиас назначения.
    pub to: usize,
    /// Метка связи.
    pub label: String,
    /// `BiRel` — двунаправленная (рисуется линией без стрелки).
    pub bidir: bool,
}

/// AST C4-диаграммы (`C4Context`/`C4Container`/`C4Component`).
#[derive(Debug)]
pub(crate) struct C4Ast {
    /// Элементы в порядке объявления.
    pub elements: Vec<C4Element>,
    /// Связи в порядке объявления.
    pub relations: Vec<C4Relation>,
    /// Пропущенные конструкции (boundaries, стили, легенды).
    pub skipped: Vec<Skipped>,
}

impl C4Ast {
    /// Понижение к flowchart TD (ADR-009): элемент → узел с меткой
    /// `«стереотип»\nlabel[\n[tech]]`, `Rel*` → ребро со стрелкой,
    /// `BiRel` → линия без стрелки. Суффиксы направления `Rel_U/D/L/R`
    /// игнорируются — раскладка всегда сверху вниз.
    pub(crate) fn to_flow(&self) -> FlowAst {
        let nodes = self
            .elements
            .iter()
            .map(|e| {
                let mut stereo = match e.kind {
                    C4ElemKind::Person => "person".to_owned(),
                    C4ElemKind::System => "system".to_owned(),
                    C4ElemKind::Container => "container".to_owned(),
                    C4ElemKind::Component => "component".to_owned(),
                };
                if let Some(store) = &e.store {
                    stereo.push_str(", ");
                    stereo.push_str(store);
                }
                if e.external {
                    stereo.push_str(", external");
                }
                let mut label = format!("«{stereo}»\n{}", e.label);
                if let Some(tech) = &e.tech {
                    label.push_str("\n[");
                    label.push_str(tech);
                    label.push(']');
                }
                FlowNode {
                    id: e.alias.clone(),
                    label,
                    shape: Shape::Rect,
                }
            })
            .collect();
        let edges = self
            .relations
            .iter()
            .map(|r| FlowEdge {
                from: r.from,
                to: r.to,
                label: if r.label.is_empty() {
                    None
                } else {
                    Some(r.label.clone())
                },
                plain: r.bidir,
            })
            .collect();
        FlowAst {
            dir: Direction::TopDown,
            nodes,
            edges,
            skipped: self.skipped.clone(),
        }
    }
}
