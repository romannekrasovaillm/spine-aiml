# Theseus-закалка arch-харнесса (шестая волна, 2026-08-14)

Перенос промышленных паттернов агентного ядра из Theseus
(`~/experiments/harness-review/theseus`, ~57k строк Rust, 1367 тестов) —
самого зрелого харнесса пользователя. Theseus, в свою очередь, собран по
код-ревью тройки лидеров (Claude Code / OpenAI Codex / xAI Grok Build —
`docs/LEADERS_NOTES.md` в репозитории Тесея). Здесь — маппинг: что взято,
где живёт в arch-ml, чем отличается.

## Маппинг паттернов

| Паттерн (Theseus → лидер) | Модуль arch-ml | Особенности переноса |
|---|---|---|
| doom-loop guard (OpenDev шаг 13 / Grok doom_loop) | `src/detectors.rs` | fingerprint(tool,args), окно 20, ≥3 идентичных → предупреждение вместо исполнения; рецидив → DENIED. Контракт tool-пар не нарушается: каждый `tool_call_id` получает ровно один ответ |
| exploration spiral (OpenDev #6) | `src/detectors.rs` | 5+ read-only вызовов подряд → напоминание; сброс на мутирующем вызове; бюджет 2 напоминания на серию |
| doom-text | `src/detectors.rs` | идентичный текст модели два хода подряд → REMINDER в контекст (вклейка после tool-результатов, пары не рвутся) |
| retry-матрица (Codex retry) | `src/retry.rs` + `llm/openai_compat.rs` | ErrorKind {RateLimit 8×/2с/120с, Server5xx 5×/500мс/30с, Network 5×/250мс/10с, Unknown 3×} с джиттером SplitMix64; Auth/BadRequest/ContextOverflow — никогда; смена класса ошибки пересоздаёт итератор задержек |
| on-error compact & resubmit (Grok) | `src/agent.rs` | HTTP 413 / «context length» → принудительная L3 + ровно один повтор хода |
| трёхуровневая компактификация | `src/agent.rs` | L1 (70% бюджета) маскирование старых tool-результатов → прунинг только при >100% → L3 (95%) LLM-саммари до последнего user-сообщения; `l3_futile` гасит бесполезную L3 (иначе жгла бы API каждый ход). Пороги — `[agent] compact_l1_pct/compact_l3_pct` |
| редакция секретов (Codex secrets) | `src/secrets.rs` | 7 встроенных правил (PEM, `*_API_KEY=`, user:pass@URL, Bearer, sk-, AKIA, hex-32+) + точные значения env-ключей провайдеров; применяется к выводу инструментов И к аргументам tool_calls в журнале |
| fuzzy-каскад правок (Claude ~9 матчеров) | `src/matchers.rs` → `tools/fs.rs::edit_file` | Exact → TrimEnd → TrimBoth → WhitespaceCollapsed; построчные блоки, перевод последней строки сохраняется; неоднозначность — ошибка с уровнем совпадения |
| хуки жизненного цикла (Claude Code) | `src/hooks.rs` + `[hooks]` | 7 событий (PreToolUse/PostToolUse/PreCompact/PostCompact/SessionStart/SessionEnd/UserPromptSubmit); exit 2 = блок; stdout PostToolUse дописывается к результату; env `ARCH_HOOK_*`; таймаут 5с |
| /doctor (Claude Code, Kimi) | `src/doctor.rs` | 10 проверок: default_model, api-keys (без вывода значений!), sessions на запись, плагины/скиллы счётчиком, база знаний, кодовые харнессы в PATH, MCP + бинари серверов, крон, веб-сайты, git. Exit code 1 при Fail |

## Осознанно НЕ перенесено

- **Sandbox (Landlock/bwrap)** — у arch-ml своя политика R0–R5 (`policy.rs`);
  ядерный confinement — отдельный проект, не смешивать с этой волной.
- **Субагенты с бюджетами / peer-стриминг stream-json** — у arch-ml роль пиров
  играют кодовые харнессы через `harness.rs` (handoff-пакеты); нативный
  стриминг клоды/кими — кандидат на седьмую волну.
- **PersistentShell, filewatcher, memory_v2, prompt_cache, темы/keymap
  из TOML** — полезно, но не критично для домена архитектора; YAGNI до
  запроса.
- **Тестовая инфраструктура mock_sse** — у arch-ml свои fake-провайдеры
  в тестах агента (FakeLlm/LoopLlm/SumLlm/OverflowLlm), мок-сервер
  добавить при первой необходимости e2e по HTTP.

## Конфигурация

```toml
[agent]
compact_l1_pct = 70   # маскирование старых tool-результатов
compact_l3_pct = 95   # LLM-саммари истории (>100 — выключено)

[[hooks.specs]]
event = "PreToolUse"
tool = "bash"                    # подстрока имени; опционально
command = "case \"$ARCH_HOOK_CONTEXT\" in *'rm -rf /'*) echo 'запрещено'; exit 2;; esac"
timeout_secs = 5

[[hooks.specs]]
event = "PostToolUse"
tool = "edit_file"
command = "git diff --stat >> ~/.arch-ml/audit.log"
```

## Проверка

- 279 unit-тестов зелёные (`cargo test`), из них новых ~40: детекторы
  (doom/spiral/doom-text/reset), retry (classify/матрица/джиттер/413),
  компактификация (L3-свёртка, resubmit после 413, 413 без свёртки),
  секреты (7 правил + env-литералы), хуки (блок/фильтр/таймаут/env),
  матчеры (4 уровня, неоднозначность, сохранение \n), doctor (3 сценария),
  агент (doom-guard, редакция в истории и журнале, блок хуком).
- Живой смоук: `arch-ml doctor` на реальном окружении — 155 плагинов,
  1205 скиллов, 5/6 харнессов в PATH (qwen отсутствует — известно),
  предупреждение по MOONSHOT_API_KEY (DPI) — корректно.
