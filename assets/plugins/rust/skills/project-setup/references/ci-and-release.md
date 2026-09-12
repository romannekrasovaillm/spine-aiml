# CI и релизный процесс для Rust

## GitHub Actions: базовый workflow

```yaml
# .github/workflows/ci.yml
name: CI
on:
  push: { branches: [main] }
  pull_request:

env:
  CARGO_TERM_COLOR: always
  RUSTFLAGS: "-D warnings"          # предупреждения = ошибки во всех джобах

jobs:
  fmt:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
        with: { components: rustfmt }
      - run: cargo fmt --all -- --check

  clippy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
        with: { components: clippy }
      - uses: Swatinem/rust-cache@v2          # кэш ~/.cargo и target/
      - run: cargo clippy --all-targets --all-features

  test:
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
      - uses: Swatinem/rust-cache@v2
      - run: cargo test --all-features
      - run: cargo doc --no-deps --all-features
        env: { RUSTDOCFLAGS: "-D warnings" }

  msrv:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@master
        with: { toolchain: "1.85" }           # = rust-version из Cargo.toml
      - run: cargo check --all-features
```

Замечания:
- `dtolnay/rust-toolchain` и `Swatinem/rust-cache` — де-факто стандарт экосистемы; проверь актуальные версии экшенов при генерации.
- Матрица ОС нужна библиотекам и CLI; для внутреннего сервиса под Linux достаточно ubuntu.
- Для скорости: `cargo nextest run` (параллельнее, лучше отчёты, retries для flaky) — установка через `taiki-e/install-action@nextest`.

## Джобы по мере зрелости проекта

```yaml
  deny:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: EmbarkStudios/cargo-deny-action@v2   # licenses, advisories, bans, sources

  semver:                                          # только для библиотек
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: obi1kenobi/cargo-semver-checks-action@v2

  coverage:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
        with: { components: llvm-tools-preview }
      - uses: taiki-e/install-action@cargo-llvm-cov
      - run: cargo llvm-cov --all-features --lcov --output-path lcov.info
      # дальше upload в codecov/coveralls по вкусу

  miri:                                            # если есть unsafe
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@nightly
        with: { components: miri }
      - run: cargo +nightly miri test
```

`deny.toml` генерируется `cargo deny init`; минимально настрой `[licenses] allow = [...]` под политику организации.

## Кросс-компиляция и сборка релизных артефактов

- `cross` (https://github.com/cross-rs/cross) — сборка под другие таргеты в контейнерах: `cross build --release --target aarch64-unknown-linux-gnu`.
- Статический Linux-бинарник: таргет `x86_64-unknown-linux-musl`.
- Автоматизация релизов бинарников по тегу: `taiki-e/upload-rust-binary-action` или `cargo-dist`.

## Чеклист публикации на crates.io

1. Метаданные заполнены: `description`, `license`, `repository`, `keywords` (≤5), `categories` (из списка crates.io).
2. `cargo publish --dry-run` и `cargo package --list` — проверь, что в пакет не попало лишнее (`exclude`/`include` в Cargo.toml).
3. `cargo semver-checks` — если это не первый релиз.
4. Версия поднята по semver; CHANGELOG обновлён; git-тег `vX.Y.Z`.
5. `cargo publish` (в workspace — публикуй в порядке зависимостей; `cargo publish --workspace` умеет это в новых версиях cargo, иначе — release-plz/cargo-release).
6. Автоматизация всего цикла: **release-plz** (PR с бампом версий и changelog по conventional commits) или **cargo-release**.

## Docker для Rust-сервисов

```dockerfile
FROM rust:1.87-slim AS builder
WORKDIR /app
COPY . .
# кэшируемая сборка зависимостей: cargo-chef, либо BuildKit cache mounts:
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/app/target \
    cargo build --release && cp target/release/myapp /usr/local/bin/

FROM debian:bookworm-slim
COPY --from=builder /usr/local/bin/myapp /usr/local/bin/myapp
USER 1000
ENTRYPOINT ["myapp"]
```

Для минимального образа — musl-таргет + `FROM scratch`/`gcr.io/distroless/static`.
