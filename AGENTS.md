# AGENTS.md — Spine AI/ML Edition

> **Форк:** локальный форк банковской линии Spine (`~/spine-private`),
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
| Entry | `src/main.rs`, `src/cli.rs`, `src/cli/*` | CLI (clap) + wiring; `main.rs` — тонкая точка входа (DEF-3, 2026-10-05), командный слой (дерево `Cmd`, диспетчер, тела команд) — тематические модули `src/cli/` |

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

<!-- agent:managed:begin id=arch-generated ver=2 hash=sha256:966c7c37687d6f85cf4be8cab40fcfd99aa5ca33731cd86007bd548ff317b938 source=agents-md -->
<!-- inputs: 88a8838c217ced77 -->
> Сгенерировано харнессом `arch-ml` (`arch-ml agents-md refresh`). Не редактируйте
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
- **AD-11 Один предмет — один поток**
- **AD-12 Канон исполнителя — один, и он проверяем**

Полный текст: `ARCHITECTURE-SPINE.md`

## Запреты и fitness-правила

- `no_unsafe_code` (must_not_contain, critical) 
- `unsafe_forbidden_in_lints` (must_contain, critical) 
- `msrv_pinned` (must_contain, critical) 
- `ci_audit_job` (must_contain, critical) 
- `no_secret_literals_code` (must_not_contain, critical) 
- `no_secret_literals_configs` (must_not_contain, critical) 
- `policy_enforced_in_dispatch` (must_contain, critical) 
- `session_journal_jsonl` (must_contain, high) 
- `plugin_library_present` (file_exists, high) 
- `single_provider_trait` (must_contain, high) 
- `handoff_json_contract` (must_contain, high) 
- `dogfood_ci_job` (must_contain, critical) 
- `readme_bilingual` (must_contain, medium) 
- `license_present` (file_exists, critical) 
- `agents_md_present` (file_exists, critical) 
- `architecture_doc_present` (file_exists, high) 
- `adr_practice` (must_contain, high) 
- `rustfmt_gate` (command_succeeds, critical) 
- `tests_pass` (command_succeeds, critical) 
- `ci_clippy_deny_warnings` (must_contain, high) 
- `eval_suite_present` (file_exists, high) 
- `eval_ci_job` (must_contain, high) 
- `banking_zone_license_header_md` (must_contain, critical) 
- `banking_zone_license_header_yaml` (must_contain, critical) 
- `no_yandexgpt_in_code` (must_not_contain, critical) 
- `no_yandexgpt_in_configs` (must_not_contain, critical) 
- `no_yandexgpt_in_presets` (must_not_contain, critical) 
- `no_external_llm_urls_in_banking_presets` (must_not_contain, critical) 
- `no_research_framing_readme` (must_not_contain, critical) 
- `no_research_framing_agents` (must_not_contain, critical) 
- `no_research_framing_banking` (must_not_contain, critical) 
- `product_spine_present` (file_exists, critical) 
- `notice_present` (file_exists, critical) 
- `bank_profile_no_external_egress` (must_not_contain, critical) 
- `no_public_registries_in_presets` (must_not_contain, critical) 
- `bank_profile_present` (file_exists, critical) 
- `reg_map_present` (file_exists, high) 
- `cbr_719p_map_present` (file_exists, critical) 
- `pay27_supervision_block_in_preset` (must_contain, critical) 
- `cbr_683p_map_present` (file_exists, critical) 
- `no_telemetry_beacons_in_src` (must_not_contain, critical) 
- `evidence_bundle_present` (file_exists, high) 
- `rules_library_readme_present` (file_exists, critical) 
- `wave1_rules_present` (file_exists, critical) 
- `wave1_cards_have_expiry` (must_contain, critical) 
- `waves_cards_have_check` (must_contain, critical) 
- `sources_catalog_present` (file_exists, high) 
- `binary_named_arch_be` (must_contain, critical) 
- `archify_module_exists` (must_contain, error) 
- `archify_tool_registered` (must_contain, error) 
- `archify_registry_test_updated` (must_contain, error) 
- `archify_docs_tools_row` (must_contain, error) 
- `archify_tests_present` (must_contain, error) 
- `archify_plugin_present` (file_exists, high) 
- `archify_vendor_present` (file_exists, high) 
- `llm_provider_isolation` (dependency_direction, critical) 
- `no_tui_below_ui` (dependency_direction, critical) 
- `tools_layer_no_agent_tui` (dependency_direction, critical) 
- `leaf_modules_stay_leaf` (dependency_direction, critical) 
- `no_banking_in_core_structural` (dependency_direction, critical) 
- `deps_check_script` (command_succeeds, critical) 
- `fleet_engine_modules_present` (file_exists, high) 
- `fleet_gate_module_present` (file_exists, high) 
- `fleet_exec_module_present` (file_exists, high) 
- `fleet_patterns_doc_present` (file_exists, high) 
- `fleet_journal_single_write_append` (must_contain, critical) 
- `no_weight_copies_in_working_tree` (command_succeeds, critical) 
- `no_large_dataset_copies_in_working_tree` (command_succeeds, critical) 
- `adf_domain_coverage` (command_succeeds, critical) 
- `archify_vendor_integrity` (command_succeeds, critical) 
- `supply_chain_ci_job` (must_contain, critical) 
- `skill_has_trigger_and_procedure` (skill_contract, medium) 
- `playbook_graduates_into_existing_skill` (playbook_graduation, medium) 
- `managed_hash_current` (managed_hash_current, medium) 
- `address-trace` (address_trace, medium) 
- `computer_module_exists` (must_contain, error) 
- `computer_tool_registered` (must_contain, error) 
- `computer_registry_test_updated` (must_contain, error) 
- `computer_docs_tools_row` (must_contain, error) 
- `vision_tools_documented` (must_contain, error) 
- `computer_tests_present` (must_contain, error) 
- `coordinate_contract_tested` (must_contain, error) 
- `cdp_helper_present` (must_contain, high) 
- `input_tools_stay_destructive` (must_contain, error) 
- `desktop_binaries_classified` (must_contain, error) 
- `fleet_plans_pass_independence_gate` (command_succeeds, critical) 
- `no_mid_wildcard_permission_rules` (command_succeeds, critical) 
- `contract_required_in_task_template` (must_contain, critical) 
- `harness_contract_drift_below_threshold` (command_succeeds, medium) 

Проверка: `arch-ml control check .` — источник `CONSTRAINTS.yaml`

## Карта репозитория

- Стек: rust
- Каталоги: aiml, assets, banking, benches, benchmarks, deploy-kit, docs, examples, experiments, scripts, sdk, src
- ADR: `docs/adr` (48 шт.) — решения читаем ДО изменения затронутых мест

## Стоп-условия: когда остановиться и эскалировать архитектору

Прекратите работу и запросите решение архитектора (A3), если изменение затрагивает:
- API/data-контракт, схему данных, security boundary / trust zone;
- новый компонент/хранилище/вендора, cross-domain интеграцию;
- необратимую миграцию, RTO/RPO, финансово значимые потоки.
Маршрут значимости: `arch-ml control score --trigger …` (Fast/Standard/Critical).
<!-- agent:managed:end id=arch-generated -->
