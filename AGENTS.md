# AGENTS.md — Spine AI/ML Edition

> **Форк:** локальный форк банковской линии Spine (`~/spine-bank`),
> 2026-09-11, чистая копия без истории (см. `NOTICE.md`). Продуктовая
> идентичность — AI/ML: доменная зона `aiml/` (пресет `ml-researcher`,
> инварианты ML-01…ML-14), бинарь `arch-ml`. Адаптация документации
> завершена 2026-09-22 (README, ROADMAP, getting_started, CI-гвард
> терминов).

Guidance for AI agents (and humans) working on this repository.
**Reading/exploring the repo instead? See `AGENTS-READERS.md`.**

**Spine AI/ML Edition** is a *domain meta-harness for AI/ML researchers*:
a thin, Rust-built terminal agent with architecture-specific tooling — ADRs,
architecture-spine invariants, rubrics with an evidence-bound LLM judge,
fitness functions, handoff packages for coding harnesses, a skills/plugins
library, background sub-agents, and governance. One binary, `arch-ml`:
TUI + CLI + library. See `README.md` (RU) / `README.en.md` (EN) for the
feature tour.

> **Product fork.** This repository is a product fork of Spine
> (`NOTICE.md`, ADR-010/ADR-040). Core is MIT. Product invariants:
> `ARCHITECTURE-SPINE-BE.md` (license boundary, model matrix, upstream
> patch discipline, product positioning).

## Install & run (one-minute setup)

```bash
cargo build --release          # binary: target/release/arch-ml
ln -sf "$PWD/target/release/arch-ml" ~/.local/bin/arch-ml   # one-word launch: `arch-ml`
arch-ml init                      # config + assets into ~/.arch-ml and
                               # ~/.config/arch-ml/config.toml

arch-ml                           # interactive TUI (default command)
arch-ml run -q "draft an ADR for saga adoption" > adr.md   # strict headless
arch-ml doctor                    # environment check (keys, dirs, plugins, MCP)
```

API keys come from the environment or key files — never from the config
(values are never stored there):

```bash
export DEEPSEEK_API_KEY=...    # deepseek (v4-flash, default), deepseek-pro (v4-pro)
export ZHIPU_API_KEY=...       # glm (glm-5.2 + budget 4.7/air/flash)
export KIMI_API_KEY=...        # kimi (k3, coding surface) or file ~/.kimi_api_key
```

No-LLM smoke: `arch-ml mermaid examples/mermaid/flow.mmd`,
`arch-ml control score --trigger new_component=true`, `arch-ml doctor`.

## Commands for development

- Build: `cargo build` / fast check: `cargo check`
- Tests: `cargo test` (live-LLM tests are `#[ignore]`d — they need keys and network)
- Живые тесты (`#[ignore]`): матрица запуска и требования — `docs/live-tests.md`
- Lint: `cargo clippy --all-targets` (pedantic warnings are tolerated for now)
- Release: `cargo build --release`
- README screenshots regenerate from code: `ARCH_GEN_SHOTS=1 cargo test gen_readme_screenshots`
- PNG из SVG для README/кейсов: `scripts/svg2png.sh docs/screenshots` (headless Chrome)

## Architecture map

| Area | Files | Role |
|---|---|---|
| Agent loop | `src/agent.rs`, `src/agent/{slash,prompts}.rs` | turn loop, tool dispatch, compaction (L1/prune/L3), session journal (append-only JSONL), failure memory (`src/failure_memory.rs`: повторные сбои → уроки), slash commands |
| LLM | `src/llm.rs`, `src/llm/openai_compat.rs`, `{deepseek,kimi,glm}.rs` | OpenAI-compatible client (SSE streaming, retries, stream-break recovery, `reasoning_content` echo, thinking maps) |
| Tools | `src/tool.rs`, `src/tools/{bash,fs,ask}.rs`, `src/tools.rs` | registry, policy gate (R0–R5), bash with env-scrub + orthogonal outcome markers, file ops with fuzzy edit |
| Domain tools | `src/{rubric,bench,control,model,trace,agentsmd,evidence,metrics,delta,worktree,subagent,ralph,distill,harness,kb,web,mcp,mermaid,plugin,eval}.rs` | architect-specific tooling (see README) |
| TUI | `src/tui.rs`, `src/tui/{app,render,text,theme}.rs` | ratatui Tokyo Night; ask-modal, model picker, tabs, fullscreen viewer |
| Config | `src/config.rs` | `~/.config/arch-ml/config.toml` (all personal paths live HERE, never in code) |
| Assets | `assets/`, `src/assets.rs` | embedded prompts/rubrics/benchmarks/plugins, deployed by `arch-ml init` |
| Entry | `src/main.rs` | CLI (clap) + wiring |

## Conventions (enforced)

- **No `unsafe`**, no `unwrap`/`expect` outside tests. Doc comments are in
  Russian (`///`); user-facing text is Russian; code/identifiers English.
- Errors: `HarnessError`/`Result` (thiserror) in the library; `anyhow` with
  `.context()` at the CLI edge. A tool failure is `ToolOutput::err`, never a panic.
- Tests are deterministic and self-contained: `tempfile` + `Config::default()`
  with overridden `paths.*`; no network, no real home dir, no real plugin
  libraries (fixtures set `plugins.include_hooks = false`).
- **Secrets**: never print, log, or commit key material; keys resolve lazily
  via `api_key_env`/`api_key_file`; tool output and journals pass through the
  redactor (`src/secrets.rs`); spawned commands get a scrubbed environment
  (`[bash] env_scrub`).
- **No personal paths in the repo** — machine-specific directories
  (knowledge bases, plugin libraries) belong to the user config only
  (see README “Configuring personal paths”). This is checked before every push.
- Orthogonal outcomes are reported independently (exit code, signal, timeout,
  truncation — separate markers, never nested).
- **Долговременная запись файлов в async**: `write_all` на `tokio::fs::File`
  НЕ гарантирует, что байты уже в ядре (`poll_write` у tokio возвращает Ready
  сразу после постановки blocking-задачи — deferred syscall). Перед ответом
  «успех» обязателен `flush().await` (дожидается inflight-записи и отдаёт её
  ошибку) — образец: `WriteFileTool` (`src/tools/fs.rs`). Синхронный `std::fs`
  и `tokio::fs::write` (целиком в `asyncify`) безопасны без дополнительных мер.
- Swallowed errors (`let _ = …`) carry a comment naming what is ignored and
  why it is safe. Numeric limits are named `MAX_*` constants with docs.

## How to extend

- **New agent tool**: implement `Tool` (`spec` + `call`), register in
  `tools::domain_tools`, add a doc row in `docs/tools.md`, add tests.
- **New model**: add `[models.<name>]` to the config (base_url, model,
  api_key_env/api_key_file, `thinking_on/off` maps, `context_limit`,
  optional `proxy` — per-provider egress proxy, loopback gateways are
  auto-started via `src/net.rs`).
  Any OpenAI-compatible endpoint works out of the box.
- **New skill/plugin**: a directory under a `[plugins] dirs` entry —
  `plugin.json` + `skills/<name>/SKILL.md` (+ optional `mcp.json`,
  `agents/*.md`, `hooks/hooks.json`). The plugin is the only install unit;
  skills never install separately.
- **New slash command**: `src/agent/slash.rs` — `execute()` arm + `catalog()`
  entry + a test; update `docs/slash_commands.md`.

## Definition of done for a change

1. `cargo test` green (incl. new tests for the change) and
   `cargo build --release` clean.
2. Docs touched by the change updated (`docs/*.md`, README when user-facing).
3. No secrets or personal paths added (run a grep gate before pushing).
4. Session journal facts: user-visible behavior changes are reflected in
   `docs/architecture.md` when the loop contract moves.

## Бенчмарки

- `benchmarks/spine-vs-claude/` — A/B-бенчмарк Spine vs Claude Code (H0.1):
  пререгистрация, 4 задачи, один детерминированный гейт `arch-ml`, раннеры
  `run_spine.sh`/`run_claude.sh`. Документация: `benchmarks/spine-vs-claude/SPEC.md`.

- `benchmarks/platformv-arch-bench/` — бенчмарк из 24 архитектурных задач по
  документации Platform V (СберТех): выбор модели, регрессионный гейт фич,
  сравнение кодовых харнессов для handoff. Документация:
  `docs/platformv-benchmark.md`. Тяжёлые прогоны (`runs/`) в git не входят.

<!-- ARCH:GENERATED hash=7663710e594c057f ts="2026-09-02 16:27" -->
> Сгенерировано харнессом `arch` (`arch agents-md refresh`). Не редактируйте
> внутри маркеров — правьте источники (spine, CONSTRAINTS.yaml) или зону снаружи.

## Команды

- Сборка: `cargo build`
- Тесты: `cargo test`
- Линт: `cargo clippy --all-targets`
- CI: GitHub Actions

## Инварианты архитектуры (нарушать нельзя)

- **AD-1 Тонкое ядро — механика в коде, знания в плагинах**
- **AD-2 Детерминированный слой контроля — без LLM**
- **AD-3 Секреты — только через окружение**
- **AD-4 Единый OpenAI-совместимый провайдерный слой**
- **AD-5 Журнал — единственный источник аудита**
- **AD-6 Безопасный Rust — без unsafe, с пином MSRV**
- **AD-7 Тесты и git-операции — изолированы от машины**
- **AD-8 Handoff кодовым харнессам — механический контракт**
- **AD-9 Spine и трассировка — гейт, а не документация задним числом**
- **AD-10 Плагин — единица распространения знаний**

Полный текст: `ARCHITECTURE-SPINE.md`

## Запреты и fitness-правила

- `module-exists` (must_contain, error) 
- `tool-registered` (must_contain, error) 
- `registry-test-updated` (must_contain, error) 
- `ruleset-o001-o005` (must_contain, error) 
- `ruleset-o003-idempotency` (must_contain, error) 
- `ruleset-rfc7807` (must_contain, error) 
- `no-banking-deps-in-core` (must_not_contain, error) 
- `no-new-dependencies` (must_not_contain, error) 
- `docs-tools-row` (must_contain, error) 
- `tests-present` (must_contain, error) 
- `cargo-fmt-clean` (command_succeeds, error) 
- `cargo-clippy-deny-warnings` (command_succeeds, error) 
- `cargo-test-all-targets` (command_succeeds, error) 

Проверка: `arch control check .` — источник `.arch-handoff/CONSTRAINTS.yaml`

## Карта репозитория

- Стек: rust
- Каталоги: assets, banking, benches, docs, examples, src, tests, кейсы
- ADR: `docs/adr` (24 шт.) — решения читаем ДО изменения затронутых мест

## Стоп-условия: когда остановиться и эскалировать архитектору

Прекратите работу и запросите решение архитектора (A3), если изменение затрагивает:
- API/data-контракт, схему данных, security boundary / trust zone;
- новый компонент/хранилище/вендора, cross-domain интеграцию;
- необратимую миграцию, RTO/RPO, финансово значимые потоки.
Маршрут значимости: `arch control score --trigger …` (Fast/Standard/Critical).
<!-- ARCH:END -->
