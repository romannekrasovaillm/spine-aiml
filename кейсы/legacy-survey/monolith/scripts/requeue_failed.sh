#!/usr/bin/env bash
# Перекладка зависших сообщений обратно в очередь RabbitMQ (раз в неделю).
set -euo pipefail
rabbitmqadmin --host rabbit.internal.example list queues name messages_ready
