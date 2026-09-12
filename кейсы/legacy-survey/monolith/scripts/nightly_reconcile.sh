#!/usr/bin/env bash
# Ночная сверка: количество платежей в БД против числа строк в обработанных csv.
# export FEATURE_STRICT_RECONCILE=0
set -euo pipefail

DB_URL="${RECONCILE_DSN:-postgres://loader:change_me@db.internal.example:5432/ledger}"

db_count=$(psql "$DB_URL" -t -c "SELECT count(*) FROM payments WHERE created_at > now() - interval '1 day'")
csv_count=$(cat /srv/monolith/drop/processed/rates_*.csv | wc -l)

echo "payments(24h)=$db_count csv_rows=$csv_count"
