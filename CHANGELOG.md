# Changelog
## [0.2.1] — 2026-09-22

### Исправлено

- `control check` на несуществующем пути репозитория снова отвечает
  «репозиторий недоступен» (контракт SDK), а не «ruleset не найден» —
  поймано свежим CI-набором sdk/python.
- Workflow sdk.yml: установка pytest на раннере (первый прогон упал на
  отсутствующем модуле).


Формат — по [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [SemVer](https://semver.org/lang/ru/). История до 0.2.0
ведётся суммарно (публичная редакция синхронизировалась снапшотами).

## [0.2.0] — 2026-09-22

Волна по внешнему ревью репозитория (13 пунктов: идентичность, порог входа,
CI-покрытие, вес репозитория).

### Добавлено

- `README.en.md` — полная английская версия; README.md сокращён до
  быстрого старта и витрины (< 300 строк; обзор возможностей переехал в
  `docs/features.md`).
- `examples/ml-experiment/` — офлайн-демо fitness-гейта (ML-06/ML-09):
  красное состояние → фикс → PASS, без API-ключей и GPU.
- `examples/archify/ml-pipeline-v1.architecture.json` — ML-фикстура контура
  диаграмм (9/9 checks, composition pass).
- `examples/mermaid/ml-pipeline.mmd` — ML-пайплайн для примера
  mermaid → ASCII в README.
- CI: workflow `sdk.yml` — тесты Python SDK (3.10/3.12), Rust SDK, Java SDK
  (JDK 21) и плагина hypothesis-router; запуск по изменениям `sdk/`, `aiml/`.
- CI: identity guard — термины банковской редакции запрещены вне allowlist;
  personal-paths scan ловит также `~/Загрузки`, `~/Downloads`, `~/Desktop`,
  `/Users/`.
- `CONTRIBUTING.md`, `CHANGELOG.md`, шаблоны issue и PR в `.github/`.
- ROADMAP.md — дорожная карта AI/ML Edition (заменила унаследованную).

### Изменено

- Идентичность документации доведена до AI/ML Edition: README, ROADMAP,
  `docs/getting_started.md` (примеры переведены на `examples/`), гайд
  `docs/SPINE-BE-GUIDE.md` переименован в `docs/SPINE-ML-GUIDE.md`,
  `docs/README.md`, `SUPPORT.md` (каналы — GitHub Issues), `AGENTS.md`,
  `AGENTS-READERS.md`, `deploy-kit/`.
- Ссылки на локальные файлы автора (`~/Загрузки/…`) убраны из `aiml/`,
  `benchmarks/`, кейсовых ADR.
- Манифесты плагинов `aiml/plugins/` приведены к единому формату:
  `$schema` (битый внешний URL) убран, описания переменных окружения
  переехали из `env` в `env_doc` (hypothesis-router), событие `check`
  объявлено в манифесте.
- `evalio.py` плагина laguna-gb10-skills — единая копия в `lib/` (было две
  побайтно-identичные).
- Тестовые фикстуры и комментарии в `src/` очищены от банковского домена.

### Исправлено

- `control.rs`: константные regex вынесены в `LazyLock`
  (`regex_creation_in_loops` — реальная горячая точка).
- Clippy-долг форка (84 срабатывания на stable 1.98): механические классы
  зачищены, allow-список в `Cargo.toml` сокращён до обоснованных позиций.

### Удалено

- `кейсы/kimi-killer/evidence/a4-run-wire/` (≈36 МБ, ~3300 файлов сырых
  evidence прогонов) — вынесены из дерева в архив релиза
  (`kimi-killer-a4-run-wire-evidence.tar.zst`, см. SHA256 в примечаниях
  релиза); сводки и вердикты остаются в кейсе.

## [0.1.x] — 2026-09-08 … 2026-09-21 (суммарно)

Форк от банковской линии Spine (2026-09-11, `NOTICE.md`), первая публичная
редакция: бинарь `arch-ml`, пресет `ml-researcher` (инварианты
ML-01…ML-14), 8 доменных плагинов `aiml/`, кейсы (12), движок флотов по
паттернам (ADR-042), дельта-протокол и SSOT-аудит, ArchUnit-мост,
OpenSpec-адаптер, реестр артефактов ML (ADR-044), независимость исполнителей
флота (ADR-045), доверие к приёмке (ADR-046), роутинг гипотез (ADR-047),
post-merge гейт (ADR-048).
