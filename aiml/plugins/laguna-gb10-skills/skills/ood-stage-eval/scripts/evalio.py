"""Загрузка eval_results_*.json с угадыванием полей.

Поддерживаемые формы файла:
  - список записей [{...}, {...}]
  - {"results": [...]} / {"tasks": [...]} / {"items": [...]}
  - {"<task_id>": {...}, ...}
Каждая запись приводится к: id, expected (list[str]), answer (str),
verdict (str|None), score (float|None), hit (bool).
"""
import json
import re

EXPECTED_KEYS = ("expected_slugs", "expected", "slugs", "target_slugs", "gold", "labels")
ANSWER_KEYS = ("answer", "response", "output", "completion", "model_answer", "text")
VERDICT_KEYS = ("verdict", "judge_verdict", "label", "judgement")
SCORE_KEYS = ("score", "judge_score", "judge", "reward")
HIT_KEYS = ("slug_hit", "hit", "slug_ok", "slug_match", "correct_slugs")
ID_KEYS = ("id", "task_id", "idx", "index", "qid")

INVALID_VERDICTS = {"API_DOWN", "JUDGE_ERROR", "TIMEOUT", "N/A", "NA", "NONE", ""}


def _first(rec, keys, default=None):
    for k in keys:
        if k in rec and rec[k] is not None:
            return rec[k]
    return default


def _as_list(x):
    if x is None:
        return []
    if isinstance(x, str):
        return [s for s in re.split(r"[,\s;]+", x) if s]
    if isinstance(x, dict):
        return [str(v) for v in x.values()]
    return [str(v) for v in x]


def _as_text(x):
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        # список сообщений/чанков
        parts = []
        for p in x:
            if isinstance(p, dict):
                parts.append(str(p.get("content") or p.get("text") or ""))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    if isinstance(x, dict):
        return str(x.get("content") or x.get("text") or json.dumps(x, ensure_ascii=False))
    return str(x)


def load(path, fields=None):
    """fields: dict с переопределениями {'expected': 'gold_slugs', 'answer': 'out', ...}"""
    fields = fields or {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        for k in ("results", "tasks", "items", "records", "data"):
            if k in raw and isinstance(raw[k], list):
                raw = raw[k]
                break
        else:
            raw = [dict(v, _key=k) if isinstance(v, dict) else {"_key": k, "value": v}
                   for k, v in raw.items()]
    recs = []
    for i, rec in enumerate(raw):
        if not isinstance(rec, dict):
            continue
        exp = _as_list(rec.get(fields.get("expected")) if "expected" in fields
                       else _first(rec, EXPECTED_KEYS))
        ans = _as_text(rec.get(fields.get("answer")) if "answer" in fields
                       else _first(rec, ANSWER_KEYS))
        verdict = rec.get(fields.get("verdict")) if "verdict" in fields else _first(rec, VERDICT_KEYS)
        score = rec.get(fields.get("score")) if "score" in fields else _first(rec, SCORE_KEYS)
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        hit = rec.get(fields.get("hit")) if "hit" in fields else _first(rec, HIT_KEYS)
        if hit is None:
            hit = bool(exp) and all(s.lower() in ans.lower() for s in exp)
        rid = _first(rec, ID_KEYS, rec.get("_key", i))
        recs.append({"id": rid, "expected": exp, "answer": ans,
                     "verdict": str(verdict).upper() if verdict is not None else None,
                     "score": score, "hit": bool(hit)})
    return recs


def judge_valid(rec):
    return rec["verdict"] is not None and rec["verdict"] not in INVALID_VERDICTS


def inspect(path, n=1):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        print("top-level keys:", list(raw.keys())[:20])
        for k in ("results", "tasks", "items", "records", "data"):
            if k in raw and isinstance(raw[k], list):
                raw = raw[k]
                break
        else:
            raw = list(raw.values())
    print("records:", len(raw))
    for rec in raw[:n]:
        print(json.dumps(rec, ensure_ascii=False, indent=1)[:1500])
