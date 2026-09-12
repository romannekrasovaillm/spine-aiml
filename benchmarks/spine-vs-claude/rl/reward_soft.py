"""Плотный reward (soft-score) для H2.1 — против разреженности fitness-гейта.

Проблема первого прогона: reward = доля точных regex-паттернов, 0.5B-модель их
почти не выдаёт -> advantage≈0, градиент шумит.

Soft-score: частичный кредит за структуру markdown (заголовки, жирный, списки,
маркеры ADR/Approver/spec) ПЛЮС точные паттерны. Даёт плотный градиент.
"""
import json
import re


def _structural(text):
    s = 0.0
    if re.search(r"(?m)^#{1,3}\s", text):
        s += 0.2  # есть markdown-заголовок
    if re.search(r"\*\*[^*]+\*\*", text):
        s += 0.1  # жирный
    if re.search(r"(?m)^\s*[-*]\s", text):
        s += 0.1  # список
    if re.search(r"(?i)ADR", text):
        s += 0.2  # упомянул ADR
    if re.search(r"(?i)(approver|аппрувер)", text):
        s += 0.2  # упомянул аппрувера
    if re.search(r"(?i)sha256|spec\s*:", text):
        s += 0.2  # spec-binding
    return min(s, 1.0)


def compute_score(data_source, solution_str, ground_truth, extra_info=None):
    if isinstance(extra_info, str):
        extra_info = json.loads(extra_info)
    patterns = (extra_info or {}).get("patterns", [])
    if not patterns:
        return 0.0
    hit = 0
    for p in patterns:
        try:
            if re.search(p, solution_str or "", re.MULTILINE | re.IGNORECASE):
                hit += 1
        except re.error:
            continue
    exact = hit / len(patterns)
    soft = _structural(solution_str or "")
    return round(0.5 * exact + 0.5 * soft, 4)
