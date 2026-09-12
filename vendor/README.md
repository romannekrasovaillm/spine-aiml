# vendor/archify — вендоренный движок диаграмм Archify (монорепозиторий Spine BE)

**Что это.** Копия upstream-пакета Archify (github.com/tt-a1i/archify,
MIT) — Node.js CLI детерминистичного рендера диаграмм JSON IR → HTML/SVG,
используемый интеграцией `src/archify.rs` (ADR-027): инструменты
`archify_validate` / `archify_deliver` / `archify_compare` и
`arch-be archify …` запускают `node <cli>` — по умолчанию путь
`vendor/archify/bin/archify.mjs` (см. `[archify].cli_path` в
`config.example.toml`).

**Зачем вендоринг (политика монорепозитория).** Продукт самодостаточен:
клон репозитория + Node ≥18 = работающий контур диаграмм без
`npx skills add` и внешних скачиваний (ИБ-контур банка). Внешний
зависимостей-процессов это не отменяет: Node нужен на машине.

**Граница лицензий.** Archify — MIT (© 2026 tt-a1i, © 2025 Cocoon AI):
`vendor/archify/LICENSE` и `THIRD_PARTY_NOTICES.md` сохранены, код не
модифицируется точечно — только целостные обновления версии. Ядро — MIT,
`banking/` — proprietary; вендоренный код в `vendor/` НЕ попадает под
proprietary-стража BE-01 (он для `banking/**`).

**Состав.** Пакет без `node_modules`: рантайм zero-dependency (схемные
валидаторы прекомпилированы в standalone ESM, visual-check — свой
CDP-клиент). `node_modules` нужен только для перегенерации валидаторов/
бренд-марок и прогона тест-сьюта upstream (`npm install && npm test`
внутри vendor/archify при обновлении версии).

**Обновление версии.**
1. `git clone --depth 1 https://github.com/tt-a1i/archify /tmp/archify`
2. `rsync -a --delete --exclude node_modules --exclude '.git*' /tmp/archify/archify/ vendor/archify/`
3. Прогон гейтов: `node vendor/archify/bin/archify.mjs doctor`,
   `arch-be archify validate architecture banking/plugins/ru-archify/skills/archify-diagrams/references/bank-target-landscape.architecture.json`,
   `cargo test archify`, `arch-be control check . --constraints CONSTRAINTS.yaml`.
4. Коммит с тегом версии в сообщении (см. `vendor/archify/package.json` → `version`).

Текущая версия: **2.17.0-dev.1** (вендорена 2026-09-03).
