"""Reward для H2.1: RL-через-харнесс на задачах spine-vs-claude.

Reward = fitness-гейт: доля must_contain-паттернов задачи, найденных в ответе
модели. Детерминированный (без LLM-судьи) — идеален для проверки RL-петли.
"""
import json
import re


def _patterns(extra_info):
    """Извлечь список regex-паттернов из extra_info (передаёт датасет)."""
    if isinstance(extra_info, str):
        extra_info = json.loads(extra_info)
    return extra_info.get("patterns", [])


def compute_score(data_source, solution_str, ground_truth, extra_info=None):
    patterns = _patterns(extra_info)
    if not patterns:
        return 0.0
    hit = 0
    for p in patterns:
        try:
            if re.search(p, solution_str or "", re.MULTILINE | re.IGNORECASE):
                hit += 1
        except re.error:
            continue
    return round(hit / len(patterns), 4)
