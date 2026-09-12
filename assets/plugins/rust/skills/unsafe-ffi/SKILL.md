---
name: unsafe-ffi
description: Безопасная работа с unsafe Rust и FFI по канонам Rustonomicon, std docs и Unsafe Code Guidelines - когда unsafe оправдан, SAFETY-комментарии и инварианты, список undefined behavior, raw pointers, MaybeUninit, transmute, unsafe impl Send/Sync, проверка Miri и санитайзерами, интеграция с C/C++ (extern "C", repr(C), bindgen, cbindgen, CString, Box::into_raw), паники через FFI-границу. Используй этот навык ВСЕГДА, когда в задаче встречается ключевое слово unsafe, сырые указатели, FFI/биндинги к C/C++ библиотекам, no_mangle/extern, вопросы про undefined behavior, Miri, stacked/tree borrows, или просьбы обернуть C-библиотеку в Rust / отдать Rust-код в C/Python.
---

# Unsafe Rust и FFI

`unsafe` не отключает проверки — он **перекладывает** доказательство корректности с компилятора на автора. UB в Rust хуже, чем в C: оптимизатор агрессивно опирается на инварианты (noalias и др.), и «вроде работает» ничего не значит.

## Методология: пять правил

1. **Сначала докажи, что unsafe нужен.** Большинство «нужен unsafe» случаев решаются safe-средствами: самоссылки → индексы/арены; графы → `petgraph`/индексы; глобалы → `OnceLock`; каст байтов → `bytemuck`/`zerocopy` (safe-обёртки с проверками); производительность → сперва навык performance. Unsafe оправдан: FFI, реализация фундаментальных структур данных, доказанные горячие точки, платформенные API.
2. **Минимальный радиус.** Маленькие `unsafe {}`-блоки вокруг конкретных операций, а не unsafe-функции целиком. В 2024 edition тело `unsafe fn` требует явных `unsafe {}` внутри (`unsafe_op_in_unsafe_fn`) — это фича: каждая опасная операция видна.
3. **Каждый unsafe-блок — комментарий `// SAFETY:`**, объясняющий, почему инварианты соблюдены (не «что делаем», а «почему это корректно»). Каждая `unsafe fn` — rustdoc-секция `# Safety` с контрактом для вызывающего. Включи линты:
   ```toml
   [lints.clippy]
   undocumented_unsafe_blocks = "deny"
   missing_safety_doc = "deny"
   ```
4. **Инкапсулируй в safe-абстракцию.** Модуль/тип, чьи публичные методы safe, а инварианты поддерживаются приватностью полей. Помни: соседний safe-код модуля может нарушить инварианты unsafe-кода (например, изменив `len` у самодельного Vec) — граница доверия = модуль.
5. **Верифицируй инструментами, не глазами**: Miri обязателен, санитайзеры и фаззинг — по обстоятельствам (см. ниже).

## Что именно является UB (краткий список Reference/Nomicon)

Вызвать **нельзя ни при каких условиях**, даже «на мгновение»:

- разыменование висячего/невыровненного указателя; чтение неинициализированной памяти (используй `MaybeUninit<T>`, не `mem::zeroed` для типов без валидного нуля);
- нарушение aliasing-модели: одновременно живые `&mut` и любые другие ссылки на то же место; мутация через ссылку, полученную из `&T` (без `UnsafeCell`);
- data race (несинхронизированный доступ из потоков с записью);
- **создание невалидного значения**: `bool` не 0/1, невалидный `char`, нулевой `NonZero*`/ссылка/`Box`, невалидный дискриминант enum, невыровненная ссылка, `str` с не-UTF8 — UB возникает в момент создания значения, не использования;
- выход за границы аллокации при арифметике указателей (`ptr.add`), переполнение `isize::MAX`;
- неверный `transmute` (размер/валидность/лайфтаймы); каст функций с неверным ABI;
- разматывание паники через `extern "C"`-границу (используй `extern "C-unwind"` или лови `catch_unwind`).

Если сомневаешься в конкретном случае — Rustonomicon и The Rust Reference (Behavior considered undefined), а не интуиция из C.

## Инструменты верификации

```bash
rustup +nightly component add miri
cargo +nightly miri test                 # интерпретатор: ловит UB, точные aliasing-нарушения
MIRIFLAGS="-Zmiri-strict-provenance" cargo +nightly miri test
```

- **Miri** — главный инструмент; гоняй все тесты, задевающие unsafe. Ограничения: не видит FFI и очень медленный — пиши маленькие целевые тесты для unsafe-ядра.
- Санитайзеры (nightly): `RUSTFLAGS="-Zsanitizer=address" cargo test --target x86_64-unknown-linux-gnu` (ASan, TSan для гонок LockFree-кода).
- `cargo careful` — тесты с включёнными debug-ассертами std.
- Фаззинг unsafe-парсеров — cargo-fuzz (см. навык testing).
- `unsafe impl Send`/`Sync` — пиши только с письменным обоснованием в SAFETY: почему тип реально можно перемещать/шарить (правило: `Send`, если все «владения» переносимы; `Sync`, если `&T` не даёт несинхронизированной мутации).

## Ключевые примитивы вместо «сырых» приёмов

| Задача | Правильный инструмент |
|---|---|
| Неинициализированная память | `MaybeUninit<T>`, `assume_init` только после полной инициализации |
| Ненулевой указатель с ковариантностью | `NonNull<T>` |
| Мутация под `&` | `UnsafeCell<T>` (единственный легальный путь) |
| Копирование/чтение по указателю | `ptr::read/write/copy(_nonoverlapping)`, `read_unaligned` для packed |
| Реинтерпретация байтов POD-типов | `bytemuck::cast`/`zerocopy` (safe) вместо `transmute` |
| Срез из указателя+длины | `slice::from_raw_parts` — документируй все 6 условий из его docs |

## FFI

Полный справочник с шаблонами (bindgen/cbindgen, строки, владение через границу, колбэки, паники, Drop) — `references/ffi.md`; читай его при любой интеграции с C/C++/Python. Краткая карта:

- Слоёная архитектура: `foo-sys`-крейт (сырые биндинги, `build.rs` + линковка) → safe-обёртка `foo` (RAII, Result, типы). Биндинги генерируй **bindgen**, не пиши руками.
- Все FFI-структуры — `#[repr(C)]`; enum'ы через границу — не Rust-enum, а `#[repr(C)]`/константы (входящее невалидное значение Rust-enum = UB).
- Строки: `CString`/`CStr`; литералы — `c"hello"`. Указатель от `CString` живёт, пока жив `CString` (классический баг: `CString::new(s)?.as_ptr()` — временный умирает сразу).
- Владение через границу: наружу `Box::into_raw`, обратно `Box::from_raw` ровно один раз; освобождение — той же стороной, что аллоцировала (экспортируй `foo_free`).
- Экспорт в C: `#[unsafe(no_mangle)] pub extern "C" fn ...` (2024 требует `unsafe(...)`); заголовки генерируй **cbindgen**; паники лови `catch_unwind` на каждой экспортируемой функции либо объявляй `extern "C-unwind"`.

## Первоисточники

| Источник | Что искать |
|---|---|
| https://doc.rust-lang.org/nomicon/ | Rustonomicon — библия unsafe |
| https://doc.rust-lang.org/reference/behavior-considered-undefined.html | официальный список UB |
| https://doc.rust-lang.org/std/ptr/index.html | правила provenance и валидности указателей |
| https://github.com/rust-lang/unsafe-code-guidelines | UCG WG — открытые вопросы модели |
| https://github.com/rust-lang/miri | флаги и возможности Miri |
| https://rust-lang.github.io/rust-bindgen/ / https://github.com/mozilla/cbindgen | генерация биндингов |
| https://doc.rust-lang.org/nomicon/ffi.html | канонический FFI-гайд |
| https://anssi-fr.github.io/rust-guide/ | ANSSI Secure Rust Guidelines (регуляторный взгляд) |

## Смежные навыки пакета

Miri/фаззинг в CI → **project-setup**, **testing**; когда unsafe «ради скорости» не нужен → **performance**.
