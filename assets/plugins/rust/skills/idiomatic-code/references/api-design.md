# API-дизайн: рабочая выжимка Rust API Guidelines

Источник: https://rust-lang.github.io/api-guidelines/ (официальный чеклист: /checklist.html). Коды правил (C-CONV, C-GETTER…) — оттуда; используй их в ревью-комментариях, их знает всё сообщество.

## Именование (naming)

- **C-CASE**: типы/трейты/enum-варианты `UpperCamelCase`; функции, методы, модули, поля `snake_case`; константы и статики `SCREAMING_SNAKE_CASE`; акронимы как слово: `Uuid`, `HttpClient`, не `UUID`.
- **C-CONV** — префиксы конверсий:
  | Префикс | Стоимость | Владение |
  |---|---|---|
  | `as_` | бесплатно | ссылка → ссылка (`as_str`, `as_bytes`) |
  | `to_` | дорого (копия/аллокация) | ссылка → новое значение (`to_string`, `to_vec`) |
  | `into_` | переменная | забирает self (`into_inner`, `into_iter`) |
- **C-GETTER**: геттер называется как поле — `fn len(&self)`, не `get_len`. Префикс `get_` — только когда имя без него конфликтует (`Cell::get`) или есть пара с сеттером в одном стиле.
- **C-ITER**: методы, дающие итераторы: `iter()` → `&T`, `iter_mut()` → `&mut T`, `into_iter()` → `T`. Тип итератора называется по методу: `Iter`, `IterMut`, `IntoIter` (C-ITER-TY).
- Предикаты: `is_empty`, `has_headers`, `contains`. Fallible-вариант паникующей операции — префикс `try_` (`try_into`, `try_reserve`).

## Interoperability: какие трейты реализовать

- **C-COMMON-TRAITS**: жадно derive'ай там, где семантика позволяет: `Debug` (практически обязателен, C-DEBUG), `Clone`, `PartialEq`/`Eq`, `PartialOrd`/`Ord`, `Hash`, `Default`. Отсутствие реализации у публичного типа — заноза для пользователей (orphan rule не даст им добавить её самим).
- **C-CONV-TRAITS**: реализуй `From`/`TryFrom` (получишь `Into` бесплатно), `FromStr` для парсинга, `AsRef`/`AsMut` для дешёвых ссылочных конверсий. Не реализуй `Into` напрямую — только `From`.
- **C-COLLECT**: коллекции реализуют `FromIterator` и `Extend`; типы, которые можно перебирать, — `IntoIterator` (в т.ч. для `&T` и `&mut T`).
- **C-SEND-SYNC**: следи, чтобы типы оставались `Send`/`Sync`, где это возможно; потеря — breaking change. Тесты: `fn assert_send<T: Send>() {}`.
- **C-SERDE**: типы данных общего назначения — поддержка serde за feature-флагом `serde`.
- `Display` — для типов, у которых есть каноничное человекочитаемое представление; `Display` ⇒ реализуй и `ToString` не вручную (он blanket).

## Гибкость на входе, конкретность на выходе

- Принимай обобщения, когда это дёшево для эргономики: `impl AsRef<Path>` для путей, `impl Into<String>` когда всё равно сохраняешь владение, `impl IntoIterator<Item = T>` вместо `&Vec<T>` / `Vec<T>`.
- В аргументах предпочитай срезы и слайсы: `&str` вместо `&String`, `&[T]` вместо `&Vec<T>` (clippy: `ptr_arg`).
- Возвращай конкретные типы или `impl Trait` (`impl Iterator<Item = ...>`); не заставляй пользователя иметь дело с `Box<dyn ...>` без необходимости.
- **C-CALLER-CONTROL**: если функции нужно владение — принимай по значению, а не `&T` + внутренний clone: пусть о клонировании решает вызывающий.
- **C-INTERMEDIATE**: операции возвращают полезные данные, а не `()` — `insert` возвращает старое значение, `parse` — распарсенное + позицию и т.п.

## Конструкторы и билдеры

- **C-CTOR**: `new()` — основной конструктор без аргументов или с очевидными; согласуй с `Default` (`new()` без аргументов ⇒ реализуй `Default`, clippy: `new_without_default`).
- **C-BUILDER**: три и более опциональных параметра / конфигурация → builder:

```rust
let server = Server::builder()
    .port(8080)
    .timeout(Duration::from_secs(30))
    .build()?;          // build возвращает Result, если валидация нетривиальна
```

Методы билдера принимают `self` (consuming, удобно чейнить и возвращать из функций) или `&mut self` (удобно в циклах/условиях) — выбери один стиль на весь API.

## Эволюция без breaking changes (future proofing)

- **C-STRUCT-PRIVATE**: поля структур приватны, доступ через методы — иначе добавление поля станет breaking change.
- `#[non_exhaustive]` на публичных enum (пользователи обязаны писать `_ =>`) и структурах-конфигах, которые будут расти.
- **C-NEWTYPE-HIDE**: сложные внутренние типы прячь за newtype: `pub struct Matches<'a>(inner::MatchesImpl<'a>);` — сможешь менять реализацию.
- **C-SEALED**: трейты, которые пользователь не должен реализовывать, — запечатывай (приватный супертрейт `mod private { pub trait Sealed {} }`).
- **C-STABLE**: всё, что достижимо из публичного API, — часть контракта semver, включая auto-traits (`Send`) и bounds.

## Документация (C-CRATE-DOC, C-EXAMPLE, C-FAILURE)

- У крейта — вводный `//!` doc с примером «hello world» уровня.
- У каждого публичного элемента — пример использования, который компилируется (doctest). Используй `?` в doctests через скрытую обёртку:

```rust
/// ```
/// # fn main() -> Result<(), Box<dyn std::error::Error>> {
/// let cfg = mycrate::Config::load("app.toml")?;
/// # Ok(()) }
/// ```
```

- Секции по необходимости: `# Errors` (когда возвращается Err), `# Panics` (когда паникует), `# Safety` (для unsafe fn — обязательна), `# Examples`.
- Ссылки на типы — intra-doc: `` [`Config`] ``, а не URL.

## Мини-чеклист ревью публичного API

- [ ] Имена соответствуют C-CASE/C-CONV/C-GETTER/C-ITER
- [ ] Debug/Clone/PartialEq/Default реализованы, где семантика позволяет
- [ ] Аргументы: `&str`/`&[T]`/`impl AsRef<...>`, не `&String`/`&Vec<T>`
- [ ] Ошибки: `Result` с типом, реализующим `Error` (не `String`, не паники)
- [ ] Поля приватны; растущие enum'ы — `#[non_exhaustive]`
- [ ] Доктесты на каждом публичном элементе, секции Errors/Panics/Safety
- [ ] Типы остаются `Send + Sync` (или это осознанно и задокументировано)
