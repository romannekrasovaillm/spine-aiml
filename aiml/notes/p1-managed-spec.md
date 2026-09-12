# СПЕК: managed-блоки и якорные диффы (протокол перезаписи генерируемого фрагмента)

- Статус: проект (P1 моста библиотеки Ариадна, `aiml/notes/library-bridge.md`).
- Дата: 2026-09-11. Автор: волна P1 (managed).
- Грунт: `library/distillate/1_методология/концепт-html-комментарии-в-markdown.md`,
  `library/distillate/1_методология/Диффы для TUI-агентов — память, инструкции, хуки.md`.
- Новый модуль: `src/managed.rs` (библиотека, НЕ инструмент — см. §8).
- Зависимости: **новых крейтов нет.** SHA-256 берётся из уже существующего
  `crate::archunit::sha256_hex` (FIPS 180-4, safe Rust, покрыт эталонными векторами);
  `regex`/`serde`/`chrono` — в ядре уже есть.

---

## 0. Проблема (что закрываем)

Харнесс сегодня пишет генерируемые артефакты **целиком, без якорей и без обнаружения ручных правок**:

| # | Дефект | Где | Последствие |
|---|---|---|---|
| 1 | Вся запись — прямой `std::fs::write` / `tokio::fs::write` без tmp+rename | `src/distill.rs:185`, `src/tools/fs.rs:343` (`write_file`), `src/tools/fs.rs:439` (`edit_file`), `src/agentsmd.rs:440` | Обрыв/падение на середине = обрезанный файл. Атомарной записи в реестре нет нигде (единственный `fs::rename` — `delta.rs:211`, перенос каталога дельты) |
| 2 | Зона `<!-- ARCH:GENERATED … -->` → `<!-- ARCH:END -->` сращивается **слепо** | `src/agentsmd.rs:450 splice()` | Ручная правка внутри зоны затирается молча; хеш `inputs_hash` (`fnv1a`) пишется, но при splice **не сверяется** |
| 3 | `/distill` переписывает файл целиком | `src/distill.rs:139-185` | Идемпотентность случайная: любой дрейф модели меняет файл; ручной правки в managed-зоне никто не замечает |
| 4 | Unified diff для прозы | — | Номера строк нестабильны, hunk-заголовки для LLM — точка отказа (Aider ушёл на SEARCH/REPLACE). Методология: маркеры вместо строк |

Методология требует три инварианта, которых нет ни одного: **идемпотентность** (ретрай безопасен),
**атомарность** (частично применённого диффа не существует), **provenance** (`id` + `ver` + `source`).

---

## 1. Грунт: правила методологии (дословно)

- «Маркеры задают границы блока, который разрешено перезаписывать машине, — при этом файл остаётся
  валидным, читаемым markdown для человека.»
- «Поставка обновления = **замена целого блока по `id`**, со сверкой `hash` (обнаруживает, что
  человек правил блок руками — тогда merge-конфликт, а не молчаливая перезапись).»
- «Верифицируемость. `base_hash` позволяет отказаться применять дифф к неожиданному состоянию
  файла (fail-fast вместо тихой порчи памяти).»
- «Операции применяются к AST markdown в памяти, результат валидируется, и только потом —
  атомарная запись (tmp-файл + rename).»
- «Человеку — diff для ревью, машине — операции с якорями и хешами, агенту — понятный отказ,
  а не тихая порча файла.»
- Конвенция `<!-- -->` (невидимый служебный слой): «всё, что адресовано машине (имя файла, границы
  блоков, метки версий), живёт в `<!-- -->`; всё, что адресовано читателю, — в обычном markdown.»

Асимметрия артефактов (из таблицы методологии): **инструкции/кастомизация** → managed-блоки
(правила конфликтуют нелокально, патчить секциями опасно); **md-память** → якорные секционные
операции по пути заголовков. Контракт покрывает оба случая одним пакетом.

---

## 2. Формат маркера

### 2.1. Грамматика

```
begin_line := '<!--' SP 'agent:managed:begin' SP attr (SP attr)* SP '-->'
end_line   := '<!--' SP 'agent:managed:end'   SP 'id=' id        SP '-->'
attr       := 'id=' id | 'ver=' uint | 'hash=sha256:' hex64 | 'source=' token | 'ts=' quoted
id         := [a-z0-9][a-z0-9-]{0,63}          ; kebab-case
uint       := [1-9][0-9]{0,9}                  ; ver >= 1
hex64      := [0-9a-f]{64}                     ; lowercase sha256
token      := [A-Za-z0-9._-]+                  ; без пробелов
```

Пример тела файла:

```markdown
# Правила проекта

(человеческая зона — агент не трогает)

<!-- agent:managed:begin id=concept-cards source=article-lib ver=3 hash=sha256:9be1… -->
## Формат карточек концепций
- один md-файл — одно извлечение из одной статьи
<!-- agent:managed:end id=concept-cards -->
```

### 2.2. Правила разбора

1. Маркер обязан занимать **строку целиком** (после `trim`). Маркер внутри строки текста не
   распознаётся — иначе markdown-документ, документирующий протокол (как этот), был бы распарсен.
2. `begin` без `end` / `end` без `begin` / `begin` внутри блока → ошибка `UnclosedBlock` /
   `OrphanEnd` / `NestedBlock`. Вложенность запрещена.
3. `id` в `end` обязан совпадать с `id` в парном `begin` → иначе `MarkerIdMismatch`.
4. Дубликат `id` среди блоков → `DuplicateId`.
5. `hash` и `ver` — обязательны в `begin`. `source`/`ts` — опциональны.
6. Неизвестный атрибут → `UnknownAttr` (fail-fast: опечатка в `hashh=` не должна молча
   отключать защиту).

### 2.3. Что покрывает `hash` и почему

`hash` = `sha256_hex(canonical_body)`, где `canonical_body`:

```
canonical_body(s) := s.replace("\r\n", "\n").trim_end_matches('\n')
```

Хешируется **тело, не маркеры**: маркер содержит сам хеш — включение маркеров дало бы
самореференцию. Именно так устроен пример методологии («замена тела блока, маркеры обновлены:
`ver=3 hash=sha256:c447…`»).

Канонизация (CRLF→LF, срез хвостовых `\n`) делает разбор и рендер **неподвижной точкой**:
`parse(render(b)) == b` и `render(parse(text)) == text` для корректного `text`.

### 2.4. Рендер блока

Канонический вид (ровно один `\n` после `begin` и перед `end`):

```
begin_line "\n" canonical_body "\n" end_line "\n"
```

Пустое тело → `begin_line "\n" end_line "\n"` (без пустой строки между).

---

## 3. API модуля `src/managed.rs`

### 3.1. Хеш

```rust
/// SHA-256 в hex (lowercase). Обёртка над `crate::archunit::sha256_hex`:
/// даёт типу право быть ключом сверки и печататься как `sha256:<hex>`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Sha256(String);

impl Sha256 {
    /// Хеш сырых байт.
    #[must_use]
    pub fn of_bytes(bytes: &[u8]) -> Self;

    /// Хеш текста после канонизации (`\r\n`→`\n`, срез хвостовых `\n`).
    #[must_use]
    pub fn of_text(text: &str) -> Self;

    /// Разбор `sha256:<hex>` либо голого `<hex>` (64 символа, lowercase).
    ///
    /// # Errors
    /// [`ManagedError::BadHash`] — не 64 hex-символа.
    pub fn parse(raw: &str) -> Result<Self>;

    /// Голый hex без префикса.
    #[must_use]
    pub fn as_hex(&self) -> &str;

    /// Значение в виде `sha256:<hex>` (для маркера и отчётов).
    #[must_use]
    pub fn as_marker(&self) -> String;
}

impl std::fmt::Display for Sha256; // → "sha256:<hex>"
```

### 3.2. Разобранный блок

```rust
/// Managed-блок с байтовыми границами в исходном тексте.
#[derive(Debug, Clone)]
pub struct Block {
    /// Идентификатор блока (kebab-case).
    pub id: String,
    /// Версия содержимого (монотонна, >= 1).
    pub ver: u32,
    /// Хеш ТЕЛА блока (см. §2.3).
    pub hash: Sha256,
    /// Источник (provenance), напр. `article-lib` или `arXiv:2402.03300`.
    pub source: Option<String>,
    /// Канонизированное тело (без маркеров).
    pub body: String,
    /// 1-based номер строки `begin`-маркера.
    pub begin_line: usize,
    /// 1-based номер строки `end`-маркера.
    pub end_line: usize,
    /// Байтовый диапазон `begin..end` включительно с хвостовым `\n` end-строки.
    pub span: std::ops::Range<usize>,
}
```

### 3.3. Проверка владения

```rust
/// Разрешение на перезапись. `Force` конструируется только явно — молчаливой
/// перезаписи чужой правки в API нет.
#[derive(Debug, Clone)]
pub enum HashGuard {
    /// Сверка: текущее значение обязано совпасть, иначе [`ManagedError::HashMismatch`].
    Expect(Sha256),
    /// Явный форс (человек/`--force`): пишем поверх, факт фиксируется в отчёте.
    Force,
}
```

### 3.4. Операции

```rust
/// Куда вставлять новый блок.
#[derive(Debug, Clone)]
pub enum Anchor {
    /// После блока с указанным id.
    AfterBlock(String),
    /// Перед блоком с указанным id.
    BeforeBlock(String),
    /// В конец файла (отдельной секцией через пустую строку).
    Eof,
    /// В начало файла.
    Bof,
}

/// Операция над managed-блоком (адресация по `id`).
#[derive(Debug, Clone)]
pub enum BlockOp {
    /// Заменить тело блока; `ver` → `ver+1`.
    /// `upsert = true` — нет блока с таким id: создать на `anchor`.
    Replace { id: String, guard: HashGuard, content: String,
              source: Option<String>, upsert: bool, anchor: Anchor },
    /// Вставить НОВЫЙ блок. Idempotent: id уже есть с тем же телом → no-op.
    Insert { anchor: Anchor, id: String, content: String, source: Option<String> },
    /// Удалить блок вместе с маркерами. Отсутствие → ошибка `BlockNotFound`.
    Remove { id: String, guard: HashGuard },
}

/// Куда вставлять секцию относительно якорного заголовка.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Position { Before, After, ChildFirst, ChildLast }

/// Операция над секцией (адресация путём заголовков — «память»).
#[derive(Debug, Clone)]
pub enum SectionOp {
    /// Заменить тело секции, сохранив её заголовок.
    Replace { anchor: Vec<String>, content: String },
    /// Вставить секцию. Если дочерний заголовок с тем же текстом уже есть —
    /// деградирует в `Replace` (идемпотентность ретрая, пример 1 методологии).
    Insert { anchor: Vec<String>, position: Position,
             heading: String, level: u8, content: String },
    /// Дописать в конец тела секции.
    Append { anchor: Vec<String>, content: String },
}
```

### 3.5. Пакет и отчёт

```rust
/// Пакет изменений одного файла: база + операции.
#[derive(Debug, Clone)]
pub struct Patch {
    /// Целевой файл.
    pub target: PathBuf,
    /// Сверка состояния ВСЕГО файла до применения (fail-fast на устаревшем диффе).
    pub base_hash: HashGuard,
    /// Операции над managed-блоками.
    pub block_ops: Vec<BlockOp>,
    /// Якорные операции над секциями.
    pub section_ops: Vec<SectionOp>,
}

/// Итог применения.
#[derive(Debug, Clone)]
pub struct ApplyReport {
    /// Путь файла.
    pub path: PathBuf,
    /// Хеш файла до применения.
    pub old_hash: Sha256,
    /// Хеш после применения (или равен `old_hash` при `changed == false`).
    pub new_hash: Sha256,
    /// false → идемпотентный no-op; файл побайтово не изменился.
    pub changed: bool,
    /// Выполнена ли атомарная запись (false при no-op).
    pub written: bool,
    /// Затронутые id блоков (в порядке применения).
    pub blocks_touched: Vec<String>,
    /// Блоки, снятые/перезаписанные принудительно (`HashGuard::Force`).
    pub forced: Vec<String>,
}
```

### 3.6. Функции

```rust
// --- Чистые (без I/O): тестируются без tempfile ---

/// Разобрать все managed-блоки текста. Порядок — по позиции в файле.
///
/// # Errors
/// `UnclosedBlock` / `OrphanEnd` / `NestedBlock` / `MarkerIdMismatch` /
/// `DuplicateId` / `BadMarker` / `BadHash` / `UnknownAttr`.
pub fn parse_blocks(text: &str) -> Result<Vec<Block>>;

/// Найти блок по id.
///
/// # Errors
/// [`ManagedError::BlockNotFound`].
pub fn find_block<'a>(blocks: &'a [Block], id: &str) -> Result<&'a Block>;

/// Канонизировать тело (CRLF→LF, срез хвостовых `\n`). Идемпотентна.
#[must_use]
pub fn canonical_body(body: &str) -> String;

/// Отрендерить блок целиком (маркеры + тело), `hash` считается вызывающим.
#[must_use]
pub fn render_block(id: &str, ver: u32, hash: &Sha256,
                    source: Option<&str>, body: &str) -> String;

/// Применить пакет к тексту в памяти. НИЧЕГО не пишет на диск.
/// Проверки `base_hash`/`guard` выполняются здесь — до любой записи.
///
/// # Errors
/// `BaseHashMismatch` / `HashMismatch` / `BlockNotFound` / `DuplicateId` /
/// `BadAnchor` / `AmbiguousHeading` / `HeadingNotFound`.
pub fn apply(text: &str, patch: &Patch) -> Result<(String, ApplyReport)>;

/// Линтер: структурные проблемы блоков (без I/O над файлом).
#[must_use]
pub fn lint_text(text: &str) -> Vec<ManagedIssue>;

// --- I/O ---

/// Прочитать файл и применить пакет, с атомарной записью результата.
/// Конвейер §4; при любой ошибке файл НЕ изменён.
///
/// # Errors
/// См. [`apply`] + ошибки чтения/записи/rename.
pub fn apply_file(patch: &Patch) -> Result<ApplyReport>;

/// Асинхронная обёртка (для `Tool::call`): `apply_file` в `spawn_blocking`.
///
/// # Errors
/// См. [`apply_file`]; ошибка join-а — `HarnessError::Managed`.
pub async fn apply_file_async(patch: Patch) -> Result<ApplyReport>;

/// Линтер файла: структура + сверка заявленного хеша с телом.
///
/// # Errors
/// Файл не читается.
pub fn lint_file(path: &Path) -> Result<Vec<ManagedIssue>>;

/// Атомарная запись: tmp в ТОМ ЖЕ каталоге → flush → sync_all → rename.
///
/// # Errors
/// Ошибка создания каталога/записи/синхронизации/rename.
pub fn atomic_write(path: &Path, contents: &str) -> Result<()>;
```

### 3.7. Замечание линтера

```rust
/// Находка линтера managed-блоков.
#[derive(Debug, Clone)]
pub struct ManagedIssue {
    /// Путь файла (или `<text>` для `lint_text`).
    pub file: PathBuf,
    /// 1-based строка (0 — файл целиком).
    pub line: usize,
    /// Правило: `unclosed_block` | `orphan_end` | `duplicate_id` |
    /// `bad_marker` | `unknown_attr` | `hash_mismatch` | `no_blocks`.
    pub rule: String,
    /// Человекочитаемое сообщение (русский).
    pub message: String,
    /// `error` | `warn`.
    pub severity: String,
}
```

`hash_mismatch` — главная находка: заявленный в маркере `hash` не равен
`Sha256::of_text(body)`, т.е. **блок правили руками после генерации**. Это
обнаружение ручного дрейфа, которого сегодня нет нигде.

---

## 4. Конвейер применения (три инварианта методологии)

```
┌──────────┐   ┌──────────────────┐   ┌──────────────┐   ┌────────────────┐
│ Patch    │ → │ 1. parse+base_   │ → │ 2. apply к   │ → │ 3. verify +    │
│ (ops)    │   │    hash сверка   │   │ тексту в     │   │ атомарная      │
│          │   │    (§4.1)        │   │ памяти (§4.2)│   │ запись (§4.3)  │
└──────────┘   └──────────────────┘   └──────────────┘   └────────────────┘
     любой шаг упал → НИЧЕГО НЕ ЗАПИСАНО, отчёт об ошибке
```

### 4.1. Шаг 1 — проверка базы (fail-fast)

1. Прочитать файл. Отсутствие:
   - `Remove`/`Replace(upsert=false)`/любая `section_op` → `BaseHashMismatch { expected, actual: None }`;
   - все операции — только `Insert(..., Eof|Bof)` и `base_hash = Force` → базой служит пустой текст.
2. `Sha256::of_text(&text)` против `base_hash`:
   - `Expect(h)` и `h != actual` → `ManagedError::BaseHashMismatch { expected: h, actual }`, файл не тронут;
   - `Force` → продолжаем, `ApplyReport.forced` пополняется id.
3. `parse_blocks(&text)` — структурная валидность до любых правок.

### 4.2. Шаг 2 — применение к памяти

Операции применяются **к строке**, не к AST-типам-структурам (минимум кода, байтовая
точность сохраняется), но с адресацией по `id`/пути заголовков — не по номерам строк.
Это удовлетворяет правилу методологии «к AST, не к строкам» по существу: сдвиг строк
не влияет на адрес.

Порядок: сначала все `block_ops` (в порядке перечисления), затем `section_ops`.
Байтовые диапазоны пересчитываются после каждой операции (оперируем смещениями в
текущей строке, а не в исходной).

**Семантика операций:**

| Операция | Адрес | Сверка | Эффект | Повторный прогон |
|---|---|---|---|---|
| `BlockOp::Replace` | `id` | `guard` по хешу тела | тело заменено, `ver+1`, `hash` пересчитан | То же тело → побайтово тот же файл (`changed=false`) |
| `BlockOp::Replace{upsert:true}` | `id`, нет → `anchor` | `guard`; при создании — нет сверки | новый блок на `anchor` | Блок уже есть и тело совпало → no-op |
| `BlockOp::Insert` | `anchor` | нет (id не должен существовать) | новый блок | id + то же тело → no-op; id + другое тело → `DuplicateId` |
| `BlockOp::Remove` | `id` | `guard` | блок с маркерами удалён | Отсутствует → `BlockNotFound` (не no-op) |
| `SectionOp::Replace` | путь заголовков | корневой `base_hash` | тело секции заменено, заголовок сохранён | То же тело → no-op |
| `SectionOp::Insert` | путь + `position` | корневой `base_hash` | секция вставлена | Заголовок уже есть → деградирует в `Replace` |
| `SectionOp::Append` | путь заголовков | корневой `base_hash` | строки дописаны в конец тела | Строки уже в хвосте → no-op |

**Идемпотентность (формально).** Для `Replace`/`Insert`/`SectionOp`:
`apply(apply(t, p).text, p).text == apply(t, p).text`. Достигается тем, что после
первого применения `guard`/`base_hash`-сверка в явном виде не требуется (замена на то
же тело детектируется сравнением результата), а финальный текст сравнивается с исходным
и при равенстве запись **не выполняется**.

### 4.3. Шаг 3 — верификация и запись

1. `parse_blocks(&new_text)` — результат структурно валиден (нет битых маркеров).
2. Каждый затронутый id присутствует (кроме `Remove`); его `hash` == `Sha256::of_text(body)`.
3. `new_text == text` → `ApplyReport { changed: false, written: false, … }`,
   **выход без записи** (mtime не трогаем — ретрай дешёв и безопасен).
4. Иначе — `atomic_write(&path, &new_text)` (§5), отчёт с `changed: true, written: true`.

### 4.4. Ошибка → отчёт об отказе

При `HashMismatch` в отчёте (не только в тексте ошибки) даётся: `id`, ожидаемый и
фактический хеш, подсказка «блок модифицирован вручную после ver=N». Методология
требует именно этого («merge-конфликт, а не молчаливая перезапись»):

```
✗ expected_hash mismatch в блоке concept-cards
  ожидалось: sha256:9be1…   фактически: sha256:d812…
  блок модифицирован вручную после ver=2
  → обновление НЕ применено; варианты: guard=Force или merge вручную
```

---

## 5. Атомарная запись tmp + rename

`atomic_write(path, contents)`:

1. `create_dir_all(parent)` (если родителя нет).
2. Имя tmp — **в том же каталоге** (rename атомарен только на одном файловом томе):
   `<dir>/.<file>.<pid>.<counter>.tmp`, где `counter` — статический `AtomicU64`
   (уникальность без `rand`; pid отсекает параллельные процессы).
3. `File::create(tmp)` → `write_all(contents.as_bytes())` → `flush()` → `sync_all()`
   (durability: запись переживает падение сразу после записи).
4. `std::fs::rename(tmp, path)` — атомарная подмена.
5. **При любой ошибке** — best-effort `remove_file(tmp)` (ошибку удаления глотаем:
   первичная ошибка важнее), возврат `HarnessError::io(&path, e)`.

Не делаем: `fsync` каталога (платформенная экзотика, ценности в нашем контуре нет),
копирование прав (файл, как правило, создаётся харнессом; при `rename` поверх
существующего права наследуются от tmp — отметить в doc-комментарии как известное
ограничение; при необходимости — `set_permissions` из метаданных цели до rename).

Функция синхронная (`std::fs`): критическая секция короткая, вызов из async-слоя —
через `apply_file_async` → `tokio::task::spawn_blocking`.

---

## 6. Обработка ошибок

Ошибки — `HarnessError`/`Result` (конвенция ядра). Новый вариант в `src/error.rs`
(энум `#[non_exhaustive]` — расширение безопасно), рядом с `Kb`/`Rubric`/`Bench`:

```rust
/// Ошибка managed-блоков и якорных диффов (P1).
#[error("managed: {0}")]
Managed(#[from] crate::managed::ManagedError),
```

Структурный источник правды — `ManagedError` в `managed.rs` (thiserror, русские
сообщения; `#[from]` в `HarnessError` сохраняет программную различаемость):

```rust
/// Ошибка протокола managed-блоков.
#[derive(Debug, thiserror::Error)]
#[non_exhaustive]
pub enum ManagedError {
    /// Файл изменился с момента сборки диффа (fail-fast).
    #[error("base_hash mismatch: ожидалось {expected}, в файле {actual} — файл не изменён; перечитайте файл и пересоберите дифф")]
    BaseHashMismatch { expected: Sha256, actual: Sha256 },

    /// Блок правили вручную: заявленный hash ≠ hash тела.
    #[error("hash mismatch в блоке '{id}' (ver={ver}): ожидалось {expected}, фактически {actual} — блок модифицирован вручную; guard=Force затрёт правку")]
    HashMismatch { id: String, ver: u32, expected: Sha256, actual: Sha256 },

    /// Блока нет.
    #[error("блок '{id}' не найден в {path}")]
    BlockNotFound { id: String, path: PathBuf },

    /// Блок с таким id уже есть.
    #[error("блок '{id}' уже существует (ver={ver}) — для обновления используйте Replace")]
    DuplicateId { id: String, ver: u32 },

    /// Незакрытый begin-маркер.
    #[error("строка {line}: begin-маркер '{id}' без парного end")]
    UnclosedBlock { id: String, line: usize },

    /// end без begin.
    #[error("строка {line}: end-маркер '{id}' без парного begin")]
    OrphanEnd { id: String, line: usize },

    /// begin внутри блока.
    #[error("строка {line}: begin-маркер '{id}' внутри блока '{outer}' — вложенность запрещена")]
    NestedBlock { id: String, outer: String, line: usize },

    /// id в end ≠ id в begin.
    #[error("строка {line}: end-маркер '{got}' не совпадает с begin '{want}'")]
    MarkerIdMismatch { want: String, got: String, line: usize },

    /// Синтаксис маркера.
    #[error("строка {line}: невалидный маркер — {reason}")]
    BadMarker { line: usize, reason: String },

    /// `hash=` не sha256.
    #[error("невалидный hash '{raw}': ожидается sha256:<64 hex>")]
    BadHash { raw: String },

    /// Неизвестный атрибут маркера.
    #[error("строка {line}: неизвестный атрибут '{attr}' в маркере")]
    UnknownAttr { attr: String, line: usize },

    /// Путь заголовков не найден.
    #[error("якорь {anchor} не найден в {path}")]
    HeadingNotFound { anchor: String, path: PathBuf },

    /// Путь заголовков неоднозначен (дубликат заголовка).
    #[error("якорь {anchor} неоднозначен: {count} совпадений")]
    AmbiguousHeading { anchor: String, count: usize },

    /// Плохой уровень заголовка.
    #[error("уровень заголовка {level} вне диапазона 1..=6")]
    BadLevel { level: u8 },

    /// Некорректный id блока.
    #[error("невалидный id '{id}': нужен kebab-case [a-z0-9][a-z0-9-]{{0,63}}")]
    BadId { id: String },
}
```

Граница инструмента (если появится Tool, §8): ошибка превращается в
`ToolOutput::err(format!("managed: {e}"))` — агент видит отказ и корректирует план,
сессия не рвётся (как в `ToolRegistry::dispatch`).

---

## 7. Точки интеграции

| Поверхность | Сейчас | После |
|---|---|---|
| `src/agentsmd.rs:450 splice()` | слепая замена зоны `ARCH:GENERATED`→`ARCH:END` | `managed::apply` над блоком `id=arch-generated`: ручная правка внутри зоны → `HashMismatch` вместо тихой потери |
| `src/agentsmd.rs:440` запись | `std::fs::write` | `managed::atomic_write` |
| `src/distill.rs:139-185` | `std::fs::write` целого файла | playbook/`arch-distilled` пишутся как managed-блок в общий файл зоны; идемпотентный ретрай + provenance (`source` = имя источника) |
| `src/tools/fs.rs:343,439` | `tokio::fs::write` | опционально через `atomic_write` (отдельная задача; протокол к ней не привязан) |
| `src/control.rs` | нет правила | fitness-правило `managed_hash_current` (находки `hash_mismatch` из `lint_file`) — механика, не суждение (грунт «рубрики…»: суждение в хуках не живёт) |

`src/lib.rs`: строка `pub mod managed;` в алфавитном порядке (после `mcp_server`).

---

## 8. Нужен ли новый `Tool`

**По умолчанию — нет.**

- Обновление инструкций/памяти идёт через существующие поверхности: `/distill`,
  `arch-ml agents-md refresh`, `edit_file` (для человека), ручной
  `apply` в CLI-подкоманде при необходимости.
- Новый инструмент = новый инструмент шума в реестре (позиция §6 спека
  `mdvs-skill-spec.md`).
- Протокол — библиотечный слой; его потребители — `distill`/`agentsmd`/cron, а не
  LLM в первом лице.

Если понадобится агентский доступ (P2): единый инструмент `managed_apply` с
JSON-аргументом `{ target, base_hash, ops[] }` → `managed::apply_file_async`;
спека одноимённой операции уже полна (§3.5), добавление Tool — механическая
обёртка. Решение отложено до появления реального сценария (YAGNI).

---

## 9. Тесты

Встроенный `#[cfg(test)] mod tests` (стиль ядра: имена fn — латиница, doc/ассерты —
русский; `tempfile` уже в зависимостях).

**Чистые (без I/O):**

1. `parse_roundtrip_is_fixed_point` — `render(parse(t)) == t` для корректного файла с
   двумя блоками и человеческим текстом вокруг.
2. `canonical_body_idempotent_and_crlf` — CRLF→LF, срез хвостовых `\n`, повторная
   канонизация не меняет строку.
3. `parse_rejects_unclosed_orphan_nested_duplicate` — по одному ассерту на каждый
   вариант `ManagedError::{UnclosedBlock, OrphanEnd, NestedBlock, DuplicateId, MarkerIdMismatch}`.
4. `parse_ignores_markers_inside_text_lines` — строка с `agent:managed:begin` в середине
   (документация протокола, как этот спек) не парсится как блок; защита проверяется на
   самом этом файле.
5. `replace_block_bumps_ver_and_hash` — `ver` 2→3, `hash` = `sha256` нового тела.
6. `replace_with_expected_hash_mismatch_fails_and_writes_nothing` — `HashMismatch`,
   результат `apply` — `Err`, исходный текст не изменён (invariant: нет частичных записей).
7. `replace_force_overwrites_and_reports_forced` — `HashGuard::Force` проходит,
   `ApplyReport.forced` содержит id.
8. `base_hash_mismatch_fails_fast` — устаревший `base_hash` → `BaseHashMismatch`.
9. `replace_same_content_is_noop` — `changed == false`, `new_text == text`.
10. `insert_block_after_anchor_and_eof` — позиция вставки для `AfterBlock`/`Eof`.
11. `insert_existing_id_same_body_is_noop_other_body_is_duplicate`.
12. `remove_block_drops_markers_and_reports_not_found`.
13. `section_replace_keeps_heading_replaces_body`.
14. `section_insert_child_last_then_idempotent` — повторный прогон деградирует в replace.
15. `section_append_is_idempotent_on_second_run`.
16. `ambiguous_heading_is_error`, `heading_not_found_is_error`.

**I/O (tempfile):**

17. `atomic_write_creates_and_replaces_file` — файл создан, повторная запись перезаписала.
18. `atomic_write_leaves_no_tmp_on_success` — в каталоге ровно один файл.
19. `atomic_write_error_leaves_original_intact` — запись в путь, где родитель — файл
    (или readonly-каталог) → `Err`, исходный файл не повреждён.
20. `apply_file_end_to_end_and_idempotent` — двойной прогон `apply_file`; второй —
    `changed=false`, байты файла и mtime не «дёргаются».
21. `apply_file_hash_mismatch_does_not_touch_disk` — до/после совпадают побайтово.
22. `lint_file_detects_manual_edit` — правка тела без обновления `hash` → находка
    `hash_mismatch`, severity `error`.
23. `lint_text_clean_file_has_no_issues`.

**Совместимость:**

24. `managed_marker_grammar_matches_methodology_example` — маркеры из примеров
    методологии (`id=concept-cards`, `id=format-rules`) парсятся без правок текста.

---

## 10. Какие файлы править (сводно)

| # | Файл | Действие |
|---|---|---|
| 1 | `src/managed.rs` | **новый**: типы §3, `apply`/`atomic_write`/`lint_*`, тесты §9 |
| 2 | `src/error.rs` | вариант `Managed(#[from] crate::managed::ManagedError)` |
| 3 | `src/lib.rs` | `pub mod managed;` (алфавитно) |
| 4 | `src/agentsmd.rs` | `splice()` → `managed::apply` (блок `id=arch-generated`, сверка хеша); запись → `atomic_write`; тест на ручную правку |
| 5 | `src/distill.rs` | запись playbook/скилла через managed-блок + `atomic_write`; `source` в provenance |
| 6 | `src/control.rs` | `RuleKind` / линтер-обёртка над `managed::lint_file` (правило `managed_hash_current`) |
| 7 | `docs/control.md` | строка про новое правило-механику |
| 8 | `aiml/notes/p1-managed-spec.md` | этот спек |
| 9 | `docs/tools.md` | **не трогаем** — новых инструментов нет (§8) |

---

## 11. Порядок реализации и приёмка

1. **Ядро** (файлы 1–3): типы + `parse_blocks`/`canonical_body`/`render_block` +
   `apply` (чистые) — `cargo test managed`, зелёные тесты 1–16.
2. **I/O** (файл 1): `atomic_write` + `apply_file`/`apply_file_async` +
   `lint_text`/`lint_file` — тесты 17–23.
3. **Интеграция `agentsmd`** (файл 4): `cargo test agentsmd` зелёный; существующие
   тесты `splice_*` не ломаются (старые маркеры `ARCH:GENERATED` переводятся в
   managed-блок тем же разбором; при расхождении — миграционная ветка «зона есть,
   managed-маркера нет» = один `Insert`).
4. **Интеграция `distill`** (файл 5): `cargo test distill` зелёный; поведение
   `distill_writes_skill_with_model_frontmatter` не меняется (скилл пишется как и
   раньше, если контракт скилла требует целый файл; managed-путь — для общей зоны).
5. **Fitness** (файл 6): dogfood — `arch-ml fitness check` на репо не даёт находок
   `managed_hash_current` на корректных файлах и **даёт** на синтетически
   испорченном блоке (тест 22).
6. **Приёмка:** двойной прогон любого пакета → `changed=false`, файл побайтово тот же;
   ручная правка тела блока → второй прогон падает `HashMismatch` и файл не тронут —
   это воспроизводимое доказательство трёх инвариантов.

**Зависимости:** никаких новых крейтов. `sha256_hex` — переиспользование
`crate::archunit::sha256_hex` (не дублировать реализацию).

**Соблюдение конвенций:** без `unsafe`; без `unwrap`/`expect` вне тестов; ошибки —
`HarnessError::Managed`/`Result`; doc-комментарии и пользовательский текст — русский,
идентификаторы — английский.
