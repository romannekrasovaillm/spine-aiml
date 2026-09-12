"""Биллинг: приём платежей по HTTP и запись в общую таблицу payments."""
import os

import psycopg2
from flask import Flask, jsonify, request

from billing import tariffs

app = Flask(__name__)

# FEATURE_DYNAMIC_TARIFF=false
# Флаг выключен с 2024-го, сам код динамических тарифов удалён — кандидат на зачистку.

DSN = os.environ.get("BILLING_DSN", "postgres://loader:change_me@db.internal.example:5432/ledger")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/charge")
def charge():
    payload = request.get_json(force=True)
    amount = tariffs.with_commission(float(payload["amount"]))
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO payments (account_id, amount, currency, notified) VALUES (%s, %s, %s, false)",
        (payload["account_id"], amount, payload.get("currency", "RUB")),
    )
    conn.commit()
    return jsonify({"status": "accepted", "amount": amount})
