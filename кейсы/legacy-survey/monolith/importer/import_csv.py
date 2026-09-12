"""Импортёр курсов: забирает csv из drop-каталога (файловый обмен с
внешней системой!) и дозаписывает платежи-корректировки в payments."""
import csv
import glob
import os
import shutil

import psycopg2

DROP_DIR = os.environ.get("DROP_DIR", "/srv/monolith/drop")
PROCESSED_DIR = os.path.join(DROP_DIR, "processed")
DSN = os.environ.get("IMPORTER_DSN", "postgres://loader:change_me@db.internal.example:5432/ledger")


def import_drop_files():
    # Файловый обмен: внешняя система кладёт csv в drop/, мы читаем и переносим
    # в processed/. Никакого API, контракт — «формат колонок договорились в 2019-м».
    for path in glob.glob(os.path.join(DROP_DIR, "rates_*.csv")):
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                apply_rate(row)
        shutil.move(path, os.path.join(PROCESSED_DIR, os.path.basename(path)))


def apply_rate(row):
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO payments (account_id, amount, currency, notified) VALUES (0, %s, %s, true)",
        (row["rate"], row.get("currency", "RUB")),
    )
    conn.commit()
