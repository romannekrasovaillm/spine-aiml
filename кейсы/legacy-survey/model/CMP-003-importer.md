---
id: CMP-003
type: cmp
title: "Importer (csv drop-обмен)"
status: adopted
code_roots: [monolith/importer, importer]
depends_on: []
---

Импорт csv-файлов из drop-каталога в общую таблицу `payments`
(importer/import_csv.py:29). Файловый обмен с внешней системой вместо API —
скрытая связь (карта, секция 5: каталог drop/, 3 файлов). Джобы планировщика:
crontab, systemd-таймеры (карта, секция 2).
