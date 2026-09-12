"""Модуль лимитов (AD-9).

Единственная публичная функция модуля — `daily_limit_key`; реализация — чистая
функция: без print и без доступа к сети/файлам.
"""


def daily_limit_key(client: str, day: str) -> str:
    """Ключ дневного лимита клиента: формат ``{client}:{day}``."""
    return f"{client}:{day}"
