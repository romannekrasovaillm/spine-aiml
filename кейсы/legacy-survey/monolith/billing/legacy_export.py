"""LEGACY: выгрузка платежей в старый формат АБС.

Не вызывается с 2024-го (переезд на CDC), файл оставлен «на всякий случай».
Кандидат на удаление — сначала подтвердить у владельца домена.
"""

def export_legacy_format(rows):
    raise RuntimeError("legacy exporter disabled")
