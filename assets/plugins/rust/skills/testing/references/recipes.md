# Рецепты: proptest, insta, mockall, criterion, trybuild, assert_cmd

## proptest: инварианты вместо примеров

```toml
[dev-dependencies]
proptest = "1"
```

```rust
use proptest::prelude::*;

proptest! {
    // roundtrip-инвариант
    #[test]
    fn encode_decode_roundtrip(input in any::<Vec<u8>>()) {
        let encoded = encode(&input);
        prop_assert_eq!(decode(&encoded)?, input);
    }

    // парсер не должен паниковать НИ на каком вводе
    #[test]
    fn parse_never_panics(s in "\\PC*") {          // произвольные строки (regex-стратегия)
        let _ = parse_config(&s);                   // Err — ок, паника — провал
    }

    // составная стратегия
    #[test]
    fn order_total_non_negative(
        items in prop::collection::vec((1u32..1000, 0.01f64..100.0), 0..50)
    ) {
        let order = Order::from_pairs(&items);
        prop_assert!(order.total() >= 0.0);
    }
}
```

Ключевое:
- при провале proptest **минимизирует** контрпример (shrinking) и пишет его в `proptest-regressions/` — **коммить этот каталог**: упавший кейс будет прогоняться всегда;
- типовые инварианты: roundtrip (serialize/deserialize), идемпотентность (`normalize(normalize(x)) == normalize(x)`), эквивалентность наивной реализации (`fast_sort(v) == { v.sort(); v }`), «не паникует», сохранение суммы/длины;
- свои стратегии: `(0..100u8).prop_map(Age)`, `prop_oneof![Just(A), Just(B)]`, `#[derive(Arbitrary)]` из крейта `proptest-derive`.

## insta: снапшоты

```rust
#[test]
fn renders_report() {
    let report = build_report(&sample_data());
    insta::assert_yaml_snapshot!(report);          // также assert_snapshot!/assert_json_snapshot!
}
```

Первый запуск создаёт `snapshots/*.snap`; изменения ревьюишь `cargo insta review` (accept/reject). Нестабильные поля (id, время) — редакция: `assert_yaml_snapshot!(report, { ".created_at" => "[ts]" })`. Снапшоты коммить; большие бинарные выводы снапшотам не давать.

## mockall: моки портов

```rust
#[cfg_attr(test, mockall::automock)]
trait Clock {
    fn now(&self) -> DateTime<Utc>;
}

#[test]
fn expires_after_ttl() {
    let mut clock = MockClock::new();
    clock.expect_now()
        .times(2)
        .returning(|| Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap());
    let cache = Cache::new(Box::new(clock), Duration::minutes(5));
    ...
}
```

Методология: абстрагируй недетерминизм (время, rand, сеть, БД) за трейтом-портом на границе архитектуры — тогда моков нужно мало. Если expectations в тестах становятся сложными сценариями — это запах: замени мок фейком (простая in-memory реализация трейта), он не завязан на порядок вызовов.

## criterion: бенчмарки

```toml
[dev-dependencies]
criterion = "0.5"

[[bench]]
name = "parsing"
harness = false
```

```rust
// benches/parsing.rs
use criterion::{criterion_group, criterion_main, Criterion};
use std::hint::black_box;

fn bench_parse(c: &mut Criterion) {
    let input = std::fs::read_to_string("benches/data/large.toml").unwrap();
    c.bench_function("parse_large_toml", |b| {
        b.iter(|| parse_config(black_box(&input)))
    });

    // сравнение реализаций / параметризация
    let mut g = c.benchmark_group("sort");
    for size in [100, 10_000] {
        g.bench_with_input(format!("n={size}"), &size, |b, &n| {
            let data = gen_data(n);
            b.iter(|| my_sort(black_box(data.clone())));
        });
    }
    g.finish();
}

criterion_group!(benches, bench_parse);
criterion_main!(benches);
```

- `black_box` на входах и/или результатах — иначе LLVM выкинет вычисление;
- setup вне `b.iter`; если в итерации нужна свежая копия — `b.iter_batched(setup, work, BatchSize::SmallInput)`;
- сравнение с базой: `cargo bench -- --save-baseline main`, после изменений `cargo bench -- --baseline main`;
- HTML-отчёты в `target/criterion/`. Для микробенчей с точностью до инструкций — альтернативы `divan`, `iai-callgrind`.

## trybuild: негативные компайл-тесты

```rust
#[test]
fn ui() {
    let t = trybuild::TestCases::new();
    t.compile_fail("tests/ui/*.rs");     // .stderr-файлы фиксируют текст ошибки
    t.pass("tests/ui/ok_*.rs");
}
```
Применение: проверка, что typestate/sealed-трейты действительно запрещают неверное использование; диагностики макросов.

## assert_cmd: CLI end-to-end

```rust
use assert_cmd::Command;
use predicates::prelude::*;

#[test]
fn fails_on_missing_file() {
    Command::cargo_bin("myapp").unwrap()
        .args(["--config", "nope.toml"])
        .assert()
        .failure()
        .code(2)
        .stderr(predicate::str::contains("nope.toml"));
}
```
Файловые фикстуры — `tempfile::tempdir()`; golden-файлы вывода — сочетание с insta.

## nextest: полезное в CI

```toml
# .config/nextest.toml
[profile.ci]
retries = 2                       # авто-ретрай flaky (и репорт о них!)
fail-fast = false
slow-timeout = { period = "60s", terminate-after = 2 }
```
`cargo nextest run --profile ci`. Doctests запускай отдельно: `cargo test --doc`.
