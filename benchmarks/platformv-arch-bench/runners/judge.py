#!/usr/bin/env python3
"""judge.py — LLM-судья glm-5.3 для ячеек с answer.md.

ОДИН вызов на ячейку на ВСЕ критерии: вход = TASK.md + CONTEXT.md (целиком,
контекст glm-5.3 = 1M) + RUBRICS.md + answer.md (обезличенный) + инструкция
вернуть СТРОГО JSON:
  {"hard_fails": {"HF-01": {"triggered": bool, "evidence": "цитата"}, ...},
   "criteria": {"C1": {"score": 0-4, "evidence": "дословная цитата",
                        "comment": "..."}, ...},
   "word_count_ok": bool}
k=2 независимых прогона -> judge_1.json, judge_2.json; итог judge.json:
per-criterion mean, total = 100*sum(w*s)/(4*sum(w)); если любой HF triggered
(хотя бы в одном из 2 прогонов) -> total = min(total, 39).
Верификация цитат: fuzzy-проверка (нормализация пробелов/регистра), что
evidence >= 40 символов реально встречается в answer.md; доля
неверифицированных — поле evidence_unverified в judge.json.
Эндпоинт: http://127.0.0.1:8787/v1/chat/completions, модель glm-5.3,
temperature 0, max_tokens 32768 (ОТКЛОНЕНИЕ от ТЗ=8000: у glm-5.3 thinking
не отключается, reasoning съедает весь бюджет 8000 -> finish=length и
пустой content; замерено 3/3 пустых ответа при 8000, ~28-30К символов
reasoning; 32768 покрывает reasoning + JSON-вердикт).
Параллелизм <= 4, ретраи при невалидном JSON (до 3). Ключи не печатаются.
Только stdlib.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

BASE = pvlib.BASE
CELLS = pvlib.CELLS_DIR
JUDGE_URL = os.environ.get("PVBENCH_JUDGE_URL",
                           "http://127.0.0.1:8787/v1/chat/completions")
JUDGE_MODEL = os.environ.get("PVBENCH_JUDGE_MODEL", "glm-5.3")
JUDGE_MAX_TOKENS = 32768  # см. docstring: 8000 съедается reasoning целиком
K_RUNS = int(os.environ.get("PVBENCH_JUDGE_K", "2"))
JUDGE_KEY_ENV = os.environ.get("PVBENCH_JUDGE_KEY_ENV", "ZHIPU_API_KEY")
MAX_JSON_TRIES = 3
_sem = threading.Semaphore(4)

PROMPT_TMPL = """Ты — строгий, но справедливый эксперт-архитектор, оценивающий архитектурную записку. Единственный источник фактов о продукте — CONTEXT.md §1. Оценивай только по RUBRICS.md.

Порядок:
1. Проверь правила hard-fail {hf_ids}. Для каждого укажи triggered (true/false) и evidence (дословная цитата из ответа; пустая строка, если не сработал).
2. Для каждого критерия {c_ids} поставь score 0–4 по якорям RUBRICS.md, приведи evidence — ДОСЛОВНУЮ цитату из ответа (<= 25 слов), и comment — чего не хватает до следующего уровня.
3. word_count_ok: true, если ответ укладывается в лимит слов из mechanical_checks.

Правила: шкала 0–4; «упомянул != применил»; не додумывать за автора; хедж-формулировки без решения не засчитывать; длина не признак качества.

Верни СТРОГО один JSON-объект без пояснений и без markdown-обрамления, схема:
{{"hard_fails": {{"HF-01": {{"triggered": false, "evidence": ""}}}}, "criteria": {{"C1": {{"score": 0, "evidence": "", "comment": ""}}}}, "word_count_ok": true}}

<TASK>
{task}
</TASK>
<CONTEXT>
{context}
</CONTEXT>
<RUBRICS>
{rubrics}
</RUBRICS>
<ANSWER>
{answer}
</ANSWER>"""


def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_evidence(judge_obj, answer):
    """Доля evidence-строк >= 40 символов, НЕ найденных в answer (fuzzy)."""
    ans_n = norm(answer)
    total, bad = 0, 0
    for section in ("hard_fails", "criteria"):
        for item in (judge_obj.get(section) or {}).values():
            ev = (item or {}).get("evidence") or ""
            if isinstance(ev, list):
                ev = " ".join(str(x) for x in ev)
            if len(norm(ev)) >= 40:
                total += 1
                if norm(ev) not in ans_n:
                    bad += 1
    return (bad / total) if total else 0.0


def call_judge(prompt):
    key = os.environ.get(JUDGE_KEY_ENV)
    if not key:
        return None, f"нет env {JUDGE_KEY_ENV}"
    payload = {"model": JUDGE_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "temperature": 0, "max_tokens": JUDGE_MAX_TOKENS}
    last = None
    for attempt in range(MAX_JSON_TRIES):
        try:
            req = urllib.request.Request(
                JUDGE_URL, data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=900) as r:
                body = json.loads(r.read())
            choice = body["choices"][0]
            content = choice["message"].get("content") or ""
            if not content.strip():
                raise ValueError(
                    f"пустой content (finish={choice.get('finish_reason')}, "
                    f"completion={body.get('usage', {}).get('completion_tokens')})")
            text = re.sub(r"^```(json)?|```$", "", content.strip(),
                          flags=re.MULTILINE).strip()
            start, end = text.find("{"), text.rfind("}")
            obj = json.loads(text[start:end + 1])
            if "criteria" not in obj:
                raise ValueError("нет ключа criteria")
            return obj, None
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:200]}"
            time.sleep(30 * (attempt + 1))  # апстрим Z.AI деградирует окнами в минуты
    return None, last


def judge_cell(cell):
    name = cell.name
    task = name.split("__")[0]
    answer_p = cell / "answer.md"
    if not answer_p.is_file():
        return
    done_runs = [k for k in range(1, K_RUNS + 1)
                 if (cell / f"judge_{k}.json").is_file()]
    if len(done_runs) >= K_RUNS and (cell / "judge.json").is_file():
        return  # идемпотентность: оба прогона есть
    files = pvlib.load_task_files(task)
    weights = pvlib.extract_criteria_weights(files["RUBRICS"])
    hf_rows = pvlib.extract_hard_fails(files["RUBRICS"])
    hf_ids = [h[0] for h in hf_rows]
    answer = answer_p.read_text(encoding="utf-8")
    prompt = PROMPT_TMPL.format(
        hf_ids=", ".join(hf_ids), c_ids=", ".join(sorted(weights)),
        task=files["TASK"], context=files["CONTEXT"],
        rubrics=files["RUBRICS"], answer=answer)

    runs, errors = [], []
    for k in range(1, K_RUNS + 1):
        kp = cell / f"judge_{k}.json"
        if kp.is_file():
            obj = json.loads(kp.read_text(encoding="utf-8"))
            if "_evidence_unverified" not in obj:
                obj["_evidence_unverified"] = round(verify_evidence(obj, answer), 4)
            runs.append(obj)
            continue
        with _sem:
            obj, err = call_judge(prompt)
        if err:
            errors.append({"run": k, "error": err})
            continue
        obj["_evidence_unverified"] = round(verify_evidence(obj, answer), 4)
        kp.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                      encoding="utf-8")
        runs.append(obj)
    if not runs:
        print(f"{name}: JUDGE FAIL {errors}", flush=True)
        return

    # агрегация: per-criterion mean по k прогонам
    per_crit, ev_unv = {}, []
    for cid, w in weights.items():
        scores = []
        for obj in runs:
            try:
                s = float(obj["criteria"][cid]["score"])
                scores.append(max(0.0, min(4.0, s)))
            except (KeyError, TypeError, ValueError):
                pass
        if scores:
            per_crit[cid] = {"mean": sum(scores) / len(scores), "weight": w,
                             "scores": scores}
    ev_unv = [obj["_evidence_unverified"] for obj in runs]
    sw = sum(w for cid, w in weights.items() if cid in per_crit)
    total = 0.0
    if sw:
        total = 100.0 * sum(c["mean"] * c["weight"] for c in per_crit.values()) / (4.0 * sw)
    hf_trig = sorted({f"{hid}"
                      for obj in runs for hid, v in (obj.get("hard_fails") or {}).items()
                      if isinstance(v, dict) and v.get("triggered")})
    total_capped = min(total, 39.0) if hf_trig else total
    out = {"cell": name, "task": task, "judge_model": JUDGE_MODEL, "k": len(runs),
           "per_criterion": per_crit, "total_raw": round(total, 2),
           "hf_triggered": hf_trig, "total": round(total_capped, 2),
           "evidence_unverified": round(sum(ev_unv) / len(ev_unv), 4),
           "errors": errors,
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (cell / "judge.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{name}: total={out['total']} hf={hf_trig} k={len(runs)}", flush=True)


def main():
    cells = sorted(p for p in CELLS.iterdir() if p.is_dir())
    todo = []
    for c in cells:
        if not (c / "answer.md").is_file():
            continue
        if not (c / "judge.json").is_file() or \
                not (c / f"judge_{K_RUNS}.json").is_file():
            todo.append(c)
    print(f"ячеек к судейству: {len(todo)}", flush=True)
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(judge_cell, todo))


if __name__ == "__main__":
    main()
