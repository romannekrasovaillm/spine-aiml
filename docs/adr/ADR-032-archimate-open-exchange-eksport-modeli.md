# ADR-032. Экспорт модели в ArchiMate Open Exchange Format 3.2 (только экспорт)

- Date: 2026-09-04
- Status: Accepted
- Expiry review: при появлении потребности в импорте ArchiMate (обратный
  round-trip через `propertyDefinitions`) или в выгрузке Business/Technology-
  слоёв (процессы, узлы deploy) — пересмотреть границы подмножества и
  направление «только экспорт».

## Context

Внешнее ревью: обмен моделью (ADR-009) покрывает C4-форматы (Structurizr
DSL, PlantUML, drawio), но не связан с корпоративными EA-репозиториями
банка (Archi/ARIS-класс), работающими в нотации ArchiMate. Скилл
`ea-repository-bridge` (banking/plugins/ru-architecture) фиксирует маппинг
модели ADR-003 → ArchiMate и требует машиночитаемого моста вместо ручной
перерисовки на архитектурном комитете (инструментальная задача транша T,
ADR-015). Экспортируемое подмножество ADR-009 (SYS/CMP/INT) для EA-моста
недостаточно: в ArchiMate есть аналоги для возможностей, требований и
инвариантов (Capability, Requirement, Principle).

Ограничения площадки прежние: новых внешних зависимостей вводить нельзя —
XML пишется руками с экранированием (прецедент drawio в ADR-009).

## Decision

1. **Формат — ArchiMate Open Exchange Format 3.2 (OEF), только экспорт.**
   `arch-be model export <dir> --format archimate` → XML на stdout, как у
   остальных форматов (`ExportFormat::Archimate`, ветка в `export_model`,
   `src/model/exchange.rs`).
2. **Маппинг типов** (по скиллу `ea-repository-bridge`): `SYS`/`CMP` →
   `ApplicationComponent`; `INT` → `ApplicationInterface`; `CAP` →
   `Capability`; `REQ`/`NFR` → `Requirement`; `AD` → `Principle`.
   `ADR`/`RISK`/`OWNER` (и прочие типы) аналога в маппинге не имеют и
   не экспортируются — как в ADR-009 для C4-форматов; связи на них
   пропускаются.
3. **Маппинг связей** (оба конца — экспортируемые сущности):
   - `depends_on` A→B → `Serving` (source=B, target=A): B обслуживает A;
   - `implements` A→B → `Realization` (источник — реализатор);
   - `affects` A→B → `Influence`;
   - `verified_by` A→B → `Realization`, только если цель — сущность модели;
     ссылка на правило `C-NNN` из CONSTRAINTS.yaml пропускается как
     внемодельная (общее правило «битая ссылка не попадает в вывод»).
4. **Идентификаторы и тексты.** Идентификаторы элементов — spine-id как
   есть (детерминированные; XML-спецсимволы в атрибутах экранируются);
   идентификаторы связей — `rel-NNN` в порядке обхода. Заголовок →
   `name`, тело сущности → `documentation` (переводы строк — допустимый
   XML-текст, сохраняются; спецсимволы `& < > " '` экранируются тем же
   хелпером-подходом, что и в drawio-экспорте).
5. **Round-trip-носитель.** Секция `propertyDefinitions`
   (`pd-spine-id`/`pd-spine-type`/`pd-spine-status`/`pd-spine-date`,
   type `string`) + per-element `properties` со значениями
   `spine.id`/`spine.type`/`spine.status`/`spine.date` — по аналогии со
   `spine.*` properties в Structurizr-экспорте (ADR-009): при будущем
   импорте id/тип/статус не угадываются.
6. **Пустое экспортируемое подмножество — ошибка** «нечего
   экспортировать», как в ADR-009 (для ArchiMate подмножество —
   SYS/CMP/INT/CAP/REQ/NFR/AD).
7. **Импорт ArchiMate осознанно не реализуется** (export-only): сценарий —
   односторонняя поставка модели проекта в EA-репозиторий банка; обратный
   круг не заявлен, а толерантный XML-парсер — это объём и риск ради
   незаявленного сценария. Ограничение зафиксировано здесь и в
   модульном комментарии `exchange.rs`.

## Alternatives Considered

| Вариант | Плюсы | Минусы |
|---------|-------|--------|
| OEF 3.2, ручная печать XML с экранированием (выбрано) | ноль новых зависимостей (инвариант), прецедент drawio, детерминированный вывод | валидность гарантируется золотыми тестами, не библиотекой |
| XML-библиотека (serde/quick-xml) | схемная валидность | новая зависимость ради ~100 строк печати — запрещено инвариантом «ноль новых зависимостей» |
| Своё расширение C4-экспорта (ArchiMate-стереотипы в Structurizr) | переиспользование парсера импорта | EA-инструменты не читают Structurizr; мост не работает |
| Импорт ArchiMate тоже | симметрия с ADR-009 | XML-парсер на руках без зависимостей; сценарий обратного круга не заявлен |
| Только SYS/CMP/INT (подмножество ADR-009) | меньше кода | теряются Capability/Requirement/Principle — ради них мост и нужен |

## Consequences

### Positive

- Модель проекта втягивается в EA-репозиторий банка (Archi и совместимые
  инструменты читают OEF 3.2) без ручной перерисовки — закрыт gap ревью
  «нет связи с ArchiMate/Sparx».
- Маппинг задокументирован в `///`-комментариях `exchange.rs` и совпадает
  со скиллом `ea-repository-bridge`; направления Serving/Realization/
  Influence покрыты детерминированными золотыми тестами.
- Ноль новых зависимостей; экспорт — чистая функция над `Model`, как
  остальные форматы.
- `propertyDefinitions`/`properties` оставляют точку опоры для будущего
  точного round-trip (id/тип/статус/дата не угадываются).

### Negative

- Подмножество: `ADR`/`RISK`/`OWNER` и связи на них не покидают репозиторий
  (нет аналога в маппинге); `verified_by` на правила `C-NNN` теряется —
  надзорный контур остаётся внутренним форматом CONSTRAINTS.yaml.
- Иерархии (вложенность CMP в SYS, grouping) не выгружаются — модель
  ADR-003 плоская, как и в ADR-009.
- Валидность XML обеспечена тестами, а не схемной проверкой; экзотика OEF
  (views, organizations) не генерируется.

## Reversibility

reversible. Изменение аддитивно: новый вариант `ExportFormat`, одна ветка
диспетчера и чистая функция `export_archimate`; C4-форматы и импорт
Structurizr не затронуты (общие отбор сущностей/связей параметризован
предикатом, поведение C4-подмножества не изменено — контролируется
существующими тестами). Удаление варианта и ветки возвращает поведение к
ADR-009.

## References

- ADR-009 (обмен моделью с отраслевыми форматами, прецедент ручного XML
  для drawio), ADR-003 (модель архитектуры), ADR-015 (транш T —
  инструментальный экспорт в EA).
- Скилл `banking/plugins/ru-architecture/skills/ea-repository-bridge`
  (маппинг модели ADR-003 → ArchiMate).
- ArchiMate 3.2 / Model Exchange File Format, The Open Group
  (pubs.opengroup.org/architecture/archimate32-doc/).
- `src/model/exchange.rs` (`ExportFormat::Archimate`, `export_archimate`).
