# FFI: шаблоны интеграции Rust ⇄ C/C++/Python

## Архитектура: -sys крейт + safe-обёртка

```
foo-sys/            # сырой слой: только extern-декларации и линковка
├── build.rs        # поиск библиотеки (pkg-config) или сборка (cc), запуск bindgen
└── src/lib.rs      # include! сгенерированных биндингов
foo/                # safe API: RAII, Result, идиоматичные типы
```

`build.rs` для bindgen:

```rust
fn main() {
    println!("cargo:rustc-link-lib=foo");
    println!("cargo:rerun-if-changed=wrapper.h");
    let bindings = bindgen::Builder::default()
        .header("wrapper.h")
        .allowlist_function("foo_.*")        // только нужное — биндинги компактнее
        .generate().expect("bindgen failed");
    bindings.write_to_file(
        std::path::PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("bindings.rs")
    ).unwrap();
}
```

Сборка вендоренного C — крейт `cc`. Для C++ прямой FFI ограничен (только `extern "C"`-поверхность); для богатого C++-API используй `cxx` (https://cxx.rs) — безопасный мост с проверками на этапе компиляции.

## Вызов C из Rust: safe-обёртка с RAII

```rust
use foo_sys as ffi;

pub struct Database { raw: std::ptr::NonNull<ffi::foo_db> }

impl Database {
    pub fn open(path: &std::path::Path) -> Result<Self, Error> {
        let c_path = std::ffi::CString::new(path.as_os_str().as_encoded_bytes())
            .map_err(|_| Error::NulInPath)?;
        // SAFETY: c_path — валидный NUL-терминированный указатель на время вызова;
        // foo_db_open по контракту возвращает NULL при ошибке, иначе владеющий указатель.
        let raw = unsafe { ffi::foo_db_open(c_path.as_ptr()) };
        std::ptr::NonNull::new(raw).map(|raw| Self { raw }).ok_or_else(Error::last_ffi)
    }
}

impl Drop for Database {
    fn drop(&mut self) {
        // SAFETY: raw получен из foo_db_open и ещё не освобождался (владение единолично).
        unsafe { ffi::foo_db_close(self.raw.as_ptr()) }
    }
}
// Send/Sync НЕ выводятся из-за NonNull — реализуй unsafe impl только если
// документация C-библиотеки гарантирует потокобезопасность соответствующего уровня.
```

Ошибки C-стиля (`int` код + `errno`/`*_last_error()`): конвертируй в enum сразу на границе; никакие сырые коды в публичный API не протекают.

## Строки

| Направление | Как |
|---|---|
| Rust → C (временный) | `let c = CString::new(s)?; ffi(c.as_ptr());` — `c` должен жить весь вызов |
| C → Rust (заимствование) | `unsafe { CStr::from_ptr(p) }.to_str()?` (проверка UTF-8) / `.to_string_lossy()` |
| Литерал | `c"hello"` (тип `&CStr`) |

Классический UB-баг: `CString::new("x")?.as_ptr()` — временный `CString` уничтожается в конце выражения, указатель повисает. Держи `CString` в переменной.
`String` содержит внутренние NUL и не NUL-терминирован — прямой каст невозможен. Пути на Unix — `OsStr::as_encoded_bytes`, на Windows строки — UTF-16 (`encode_wide`).

## Экспорт Rust-API в C (и Python/ctypes)

```rust
/// Заголовок генерируется cbindgen'ом (cbindgen --lang c -o include/mylib.h)
#[unsafe(no_mangle)]
pub extern "C" fn mylib_parse(input: *const c_char, out_len: *mut usize) -> *mut Item {
    let result = std::panic::catch_unwind(|| {
        // SAFETY: контракт функции (документирован в заголовке): input — валидная
        // NUL-терминированная строка; out_len — валидный указатель.
        let s = unsafe { std::ffi::CStr::from_ptr(input) };
        parse_items(s.to_str().ok()?)
    });
    match result {
        Ok(Some(items)) => {
            let mut boxed = items.into_boxed_slice();
            // SAFETY: out_len валиден по контракту.
            unsafe { *out_len = boxed.len() };
            let ptr = boxed.as_mut_ptr();
            std::mem::forget(boxed);         // владение уходит вызывающему
            ptr
        }
        _ => std::ptr::null_mut(),           // и ошибка парсинга, и паника → NULL
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn mylib_items_free(ptr: *mut Item, len: usize) {
    if ptr.is_null() { return; }
    // SAFETY: ptr+len получены из mylib_parse и передаются сюда ровно один раз.
    drop(unsafe { Box::from_raw(std::slice::from_raw_parts_mut(ptr, len)) });
}
```

Правила:
- **cdylib** в `[lib] crate-type = ["cdylib"]` (+ `"rlib"`, если крейт нужен и Rust-пользователям);
- каждая экспортируемая функция: `catch_unwind` (паника через `extern "C"` = UB/abort) или сигнатура `extern "C-unwind"`, если вызывающая сторона умеет разматывание;
- на каждый тип, чьё владение передано наружу, — парная `*_free`-функция; освобождает та сторона, что аллоцировала;
- все структуры в сигнатурах — `#[repr(C)]`; `Option<extern "C" fn(...)>` — легальный способ nullable-колбэка (niche-оптимизация);
- версионируй ABI: префикс функций, `mylib_abi_version()`.

## Колбэки из C в Rust

```rust
pub extern "C" fn dispatch(cb: extern "C" fn(*mut c_void, u32), user_data: *mut c_void) { ... }
```
Замыкание Rust → C: передавай `Box::into_raw(Box::new(closure)) as *mut c_void` как user_data + трамплин-функцию, восстанавливающую тип; освобождение — в парном unregister. Колбэк не должен паниковать (оберни тело в `catch_unwind`).

## Python-специфика

Для полноценных модулей Python используй **PyO3** + maturin (https://pyo3.rs) — это стандарт, вручную через ctypes ходи только для простейших случаев. PyO3 сам решает вопросы GIL, исключений и владения.

## Чеклист FFI-ревью

- [ ] Все биндинги сгенерированы (bindgen/cbindgen), не написаны руками
- [ ] На каждый unsafe — SAFETY-комментарий со ссылкой на контракт C-API
- [ ] Владение: у каждого `into_raw`/`forget` есть парный free; double-free невозможен по конструкции (типы!)
- [ ] `catch_unwind` на всех `extern "C"`-экспортах
- [ ] Никакой Rust-enum не принимает значения напрямую из C
- [ ] Тесты обёртки гоняются под ASan/LeakSanitizer (Miri FFI не видит)
- [ ] Потокобезопасность C-библиотеки отражена в Send/Sync обёртки (с обоснованием)
