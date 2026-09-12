# deploy-kit — развёртывание arch-ml с нуля

Комплект «под ключ» для развёртывания харнесса **Spine Banking Edition**
(бинарь `arch-ml`) на чистой машине и подключения его к китайским LLM
(DeepSeek, Z.AI GLM, Kimi) или к локальной/self-hosted модели.

## Состав комплекта

| Файл | Назначение |
|---|---|
| `arch-ml` | Бинарь харнесса (Linux x86-64; версия бинаря 0.1.3, комплект v0.1.3, сборка 2026-09-08 из ветки `spine-be`) |
| `ИНСТРУКЦИЯ_ЧЕЛОВЕК.md` | Пошаговая инструкция для человека: установка, ключи, модели, контекстные папки, грабли |
| `ИНСТРУКЦИЯ_АГЕНТ.md` | Сжатый контракт для ИИ-агента, выполняющего развёртывание |
| `config.starter.toml` | Готовый минимальный конфиг: DeepSeek + GLM + Kimi + локальная модель |
| `ПРОВЕРКА.md` | Протокол живого тестирования инструкции (волны 2026-09-07 и 2026-09-08) |

## Что уже зашито в бинарь (ничего докачивать не нужно)

Бинарь самодостаточен. Команда `arch-ml init` раскладывает встроенные
ассеты в `~/.arch-ml/`:

- **Промпты** (9): architect, adr, spine, review_adversarial, readiness_gate,
  handoff_compile, reverse_discovery, nfr_design, skill_distiller.
- **Рубрики качества** (6): adr_quality, solution_architecture, architecture_gates,
  handoff_quality, agents_md_quality, macedo_dimensions.
- **Бенчмарки** (6 + golden): payment_integration, event_driven_design,
  legacy_decomposition, gigachat_sla_resilience, pangolin_replication,
  meta_agent_realtime.
- **Плагины и скиллы**: 7 плагинов, 52 скилла (arch-core, arch-governance,
  arch-office, patterns-integration, patterns-resilience, aws-agentic-ai,
  spine-be-docs и др.) — разворачиваются в `~/.arch-ml/plugins/`.
- **Дефолтные модели**: deepseek, deepseek-pro, glm, glm-4.7, glm-air,
  glm-flash, glm-5.3-flash, kimi, gigachat* — работают сразу после
  установки ключа в окружение, конфиг для них править не нужно.
- **Конфиг по умолчанию**: `arch-ml init` пишет полный прокомментированный
  `~/.config/arch-ml/config.toml`.
- Примеры: `mcp.json`, `cron.toml`, `CONSTRAINTS.example.yaml`.

Единственное, что бинарь НЕ содержит: API-ключи (по инварианту AD-3 —
только через окружение/файлы ключей) и личные контекстные папки
пользователя (базы знаний, библиотеки плагинов — подключаются в конфиге).

## Быстрый старт (4 команды)

```bash
# из релиза GitHub файл называется arch-ml-linux-x86_64 — переименуйте:
cp arch-ml-linux-x86_64 ~/.local/bin/arch-ml && chmod +x ~/.local/bin/arch-ml
arch-ml init                         # конфиг + ассеты в ~/.arch-ml
export DEEPSEEK_API_KEY="sk-..."     # ключ DeepSeek (platform.deepseek.com)
arch-ml run -q "Привет! Кто ты?"     # проверка
```

Только ключ GLM/Kimi, без DeepSeek? Поставьте `default_model = "glm"`
(или `"kimi"`) в `~/.config/arch-ml/config.toml` — модель по
умолчанию DeepSeek. Подробности — в `ИНСТРУКЦИЯ_ЧЕЛОВЕК.md`, шаг 3.
