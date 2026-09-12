"""Нотификатор: читает НЕОТПРАВЛЕННЫЕ платежи из общей таблицы payments
(записывает её биллинг!) и рассылает уведомления через RabbitMQ."""
import os

import pika
import psycopg2

DSN = os.environ.get("NOTIFIER_DSN", "postgres://loader:change_me@db.internal.example:5432/ledger")
RABBIT_URL = os.environ.get("RABBIT_URL", "amqp://guest:guest@rabbit.internal.example:5672/")


def fetch_unnotified(cur):
    # Скрытая связь: таблица payments принадлежит биллингу, но читается здесь напрямую,
    # без API и без контракта — изменение схемы бьёт по двум сервисам сразу.
    cur.execute("SELECT id, account_id, amount FROM payments WHERE notified = false")
    return cur.fetchall()


def run():
    conn = psycopg2.connect(DSN)
    params = pika.URLParameters(RABBIT_URL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.basic_consume(queue="payments.notify", on_message_callback=on_event)


def on_event(ch, method, properties, body):
    ch.basic_ack(delivery_tag=method.delivery_tag)
