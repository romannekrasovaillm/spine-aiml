"""Валидация csv-файлов drop-каталога до импорта."""

REQUIRED_COLUMNS = {"date", "rate"}


def validate_header(fieldnames) -> bool:
    return fieldnames is not None and REQUIRED_COLUMNS.issubset(set(fieldnames))
