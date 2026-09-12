---
name: testing
description: Тестирование Rust-кода по канонам The Book (гл. 11), rustc/cargo docs и экосистемных инструментов - юнит- и интеграционные тесты, doctests,#[tokio::test], property-based тестирование (proptest), снапшоты (insta), моки (mockall), бенчмарки (criterion), cargo-nextest, покрытие (cargo-llvm-cov), фаззинг (cargo-fuzz), Miri. Используй этот навык ВСЕГДА, когда нужно написать тесты для Rust-кода, покрыть тестами модуль/крейт, поднять тестовую инфраструктуру, тестировать async-код, чинить flaky-тесты, мерить покрытие или писать бенчмарки, а также при просьбах "добавь тесты", "проверь этот код", "как протестировать X в Rust".
---

# Тестирование в Rust

## Методология

1. **Тестируй поведение через публичное API**, а не внутренности: тесты не должны ломаться при рефакторинге. Приватные функции тестируй только если в них нетривиальный алгоритм.
2. **Пирамида по-растовски**: значительную часть работы делает компилятор + типы; тесты фокусируй на логике, граничных случаях, ошибочных путях и инвариантах. Каждый баг → сначала воспроизводящий тест, потом фикс.
3. **Детерминизм**: без реального времени, сети и порядка-зависимости. Flaky-тест — это баг.
4. Тесты — тоже код: без copy-paste (таблицы кейсов, хелперы), но с приоритетом читаемости над DRY.

## Где какие тесты живут

```
src/lib.rs        → юнит-тесты рядом с кодом: #[cfg(test)] mod tests (видят приватное)
src/…             → doctests в /// примерах (это и документация, и тест)
tests/*.rs        → интеграционные: каждый файл — отдельный крейт, только публичное API
tests/common/mod.rs → общие хелперы интеграционных тестов (не common.rs — иначе станет тестом)
benches/          → criterion-бенчмарки
fuzz/             → cargo-fuzz таргеты
```

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_valid_header() {
        let got = parse_header(b"GET / HTTP/1.1").expect("valid header must parse");
        assert_eq!(got.method, Method::Get);
    }

    #[test]
    fn rejects_empty_input() {
        assert!(matches!(parse_header(b""), Err(ParseError::Empty)));
    }
}
```

Правила оформления:
- Имя теста — утверждение о поведении: `returns_error_on_empty_input`, не `test1`.
- Один смысловой аспект на тест; в `assert_eq!(got, want, "context: {x}")` добавляй контекст для табличных случаев.
- Тесты могут возвращать `Result` — используй `?` вместо каскада unwrap: `fn t() -> anyhow::Result<()>`.
- `#[should_panic(expected = "substring")]` — всегда с `expected`; для проверки ошибок предпочитай `matches!` на `Err`.
- Дорогие/внешние тесты — `#[ignore = "reason"]`, запускаются `cargo test -- --ignored`.

## Запуск

```bash
cargo test                        # всё: unit + integration + doctests
cargo test parse                  # фильтр по имени
cargo test -- --nocapture         # видеть println (в 2024/nextest вывод и так показан у упавших)
cargo nextest run                 # быстрее, лучше отчёты, retries; НЕ гоняет doctests —
cargo test --doc                  #   поэтому doctests отдельно
```

Тесты в одном бинаре идут параллельно в потоках → общее состояние (env vars, cwd, файлы, порты) изолируй: `tempfile::tempdir()`, свободный порт `TcpListener::bind("127.0.0.1:0")`, для env — крейт `temp-env` или serial-запуск (`#[serial]` из serial_test).

## Async-тесты (Tokio)

```rust
#[tokio::test]
async fn worker_times_out() { ... }

#[tokio::test(start_paused = true)]        // виртуальное время: sleep'ы мгновенны
async fn retries_three_times_with_backoff() {
    tokio::time::timeout(Duration::from_secs(60), run_with_retries()).await.unwrap();
    // 60 «виртуальных» секунд пройдут мгновенно и детерминированно
}
```

Каждый `#[tokio::test]` — свой однопоточный runtime. Не забывай доводить graceful shutdown, иначе фоновые задачи утекут между тестами (см. навык async-concurrency).

## Инструменты за пределами assert_eq

Рецепты с кодом — `references/recipes.md` (читай, когда нужен конкретный инструмент):

- **proptest** — property-based: формулируешь инвариант («decode(encode(x)) == x», «parser не паникует ни на каком вводе»), библиотека генерирует и *минимизирует* контрпримеры; regression-файлы коммить в git.
- **insta** — снапшот-тесты для больших выводов (JSON, рендеры, error messages): `assert_snapshot!`, ревью изменений через `cargo insta review`.
- **mockall** — моки трейтов; методологическое правило: мокай **свои** трейты-порты (границы: БД, HTTP, время), а не чужие типы. Часто вместо мока достаточно фейка (in-memory реализация) — предпочитай его.
- **criterion** — статистически честные бенчмарки с отчётами и сравнением с baseline (`cargo bench`); `std::hint::black_box` против выкидывания кода оптимизатором.
- **cargo-llvm-cov** — покрытие: `cargo llvm-cov --html`. Покрытие — диагностика непокрытых веток, а не KPI.
- **cargo-fuzz** — фаззинг парсеров/декодеров недоверенного ввода (nightly, libFuzzer).
- **Miri** — `cargo +nightly miri test`: обнаруживает UB в unsafe-коде (см. навык unsafe-ffi).
- **trybuild** — тесты «этот код НЕ должен компилироваться» (для макросов и API с типовыми гарантиями).
- **assert_cmd + predicates** — тестирование CLI end-to-end.

## Что покрывать в первую очередь (чеклист)

- [ ] Happy path каждого публичного метода (часто закрыт doctest'ом)
- [ ] Каждый вариант ошибки конструируем и возвращается когда должен
- [ ] Границы: пустой вход, максимум, unicode, нулевые длительности
- [ ] Паникующие пути задокументированы и проверены `#[should_panic(expected)]`
- [ ] Инварианты roundtrip/идемпотентности — property-тестом
- [ ] Конкурентный код: shutdown, таймауты, отсутствие дедлока (тест с `timeout`)
- [ ] Regression-тест на каждый пофикшенный баг (имя с номером issue)

## Первоисточники

| Источник | Что искать |
|---|---|
| https://doc.rust-lang.org/book/ch11-00-testing.html | базовая механика тестов |
| https://doc.rust-lang.org/rustdoc/write-documentation/documentation-tests.html | doctests: скрытые строки, no_run, compile_fail |
| https://proptest-rs.github.io/proptest/ | Proptest Book: стратегии, shrinking |
| https://insta.rs/ | снапшоты |
| https://bheisler.github.io/criterion.rs/book/ | Criterion Book |
| https://nexte.st/ | nextest: конфиг, retries, партиционирование в CI |
| https://rust-fuzz.github.io/book/ | Rust Fuzz Book |
| https://docs.rs/tokio/latest/tokio/attr.test.html | опции #[tokio::test] |

## Смежные навыки пакета

CI-джобы для тестов/покрытия → **project-setup**; Miri и unsafe → **unsafe-ffi**; интерпретация бенчмарков → **performance**.
