def summarize(amounts: list) -> str:
    """Сводка набора сумм: ``count={n}; total={sum с 2 знаками}``."""
    return f"count={len(amounts)}; total={sum(amounts):.2f}"
