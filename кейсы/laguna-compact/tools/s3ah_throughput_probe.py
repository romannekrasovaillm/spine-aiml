#!/usr/bin/env python3
"""S3ah — замер пропускной способности учителя `qwen3.8-27b` на стенде.

Зачем. Решением владельца (ADR-038, обновление 17.09.2026) учитель один —
`qwen3.8-27b`; локальный `qwen3:8b` отпадает по качеству, а не по скорости.
При одиночном режиме 27B даёт ≈11 ток/с, и полный проход по рассуждениям
измеряется десятками суток. Значит, решение о схеме объёма упирается в два
вопроса, на которые нельзя отвечать ожиданием:

1. **сколько токенов в секунду даёт не один запрос, а загрузка целиком** —
   llama.cpp умеет непрерывный батчинг (`--parallel`), и на батче веса читаются
   один раз на несколько последовательностей, поэтому суммарная скорость может
   быть в разы выше одиночной;
2. **сколько стоит контекст**: чем глубже заполнено окно, тем дороже декод;
   для генерации трасс достаточно ctx 8–16k, а не 262k.

Прибор ничего не предполагает: он шлёт запросы и читает `timings` самого
сервера (`prompt_per_second`, `predicted_per_second`, `predicted_ms`), а не
меряет «на глазок». Одиночная скорость, батч ×N, префилл на разной глубине —
все три числа попадают в отчёт как замер с условиями (порт, ctx, слоты).

Почему суммарная скорость считается по журналу загрузки, а не как сумма
одиночных: при N > слотов часть запросов ждёт в очереди, и «сумма predicted_n /
стенные секунды» — единственная величина, которая честно отвечает на вопрос
«сколько трасс в час». Рядом кладётся `slots_busy_ratio` = Σ predicted_ms /
стенные секунды: он показывает, сколько слотов реально работало, то есть
случился ли батчинг или очередь.

Режимы::

    measure — снять замеры (одиночная, батч ×N, префилл на глубине) → raw JSON
    report  — собрать evidence/s3ah-throughput.json из замера и фактов стенда

Коды возврата::

    0 — замер снят (все уровни ответили)
    2 — NOT-VERIFIED: эндпоинт не отвечает или ни один уровень не дал токенов

Запуск (туннель к роутеру стенда поднимается снаружи — прибор его не открывает)::

    ssh -N -L 127.0.0.1:18081:127.0.0.1:8080 gb10-fast &
    python3 tools/s3ah_throughput_probe.py --mode measure --levels 1,2,4,8 \\
        --out runs/s3ah-throughput-<ts>/
    python3 tools/s3ah_throughput_probe.py --mode report \\
        --measure runs/s3ah-throughput-<ts>/measure.json \\
        --platform-facts runs/s3ah-throughput-<ts>/platform.json \\
        --out evidence/s3ah-throughput.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
EXIT_OK, EXIT_NOT_VERIFIED = 0, 2

DEFAULT_ENDPOINT = "http://127.0.0.1:18081"
DEFAULT_MODEL = "qwen3.8-27b@2026-08-18@llama.cpp"

#: Промпты разных запросов обязаны различаться. Иначе llama.cpp отдаёт попадание
#: в кэш промпта слота (`cache_n` > 0), префилл не выполняется, и «суммарная
#: скорость» меряет кэш, а не движок. Отсюда nonce в каждом запросе.
PROMPT_TMPL = (
    "Ты — эксперт по ML/AI. Кратко и по делу опиши, чем отличается GRPO от PPO "
    "в части оценки преимущества. Запрос номер {nonce}."
)
PREFILL_FILLER = (
    "Концепт описывает механизм обучения с подкреплением и его связь с соседними "
    "концептами библиотеки. "
)


def note(msg: str) -> None:
    print(msg, file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def http_post_json(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def chat_request(endpoint: str, model: str, prompt: str, tokens: int,
                 timeout: float, ignore_eos: bool = True) -> dict:
    """Один запрос. Возвращает замер самого сервера (`timings`) и стенные секунды.

    `ignore_eos` обязателен для замера: без него запрос может остановиться на
    первом же EOS, и «100 токенов» превратятся в три — мерялся бы не движок, а
    длина ответа.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": tokens,
        "ignore_eos": bool(ignore_eos),
        "chat_template_kwargs": {"enable_thinking": False},
    }
    started = time.time()
    try:
        d = http_post_json(endpoint.rstrip("/") + "/v1/chat/completions", payload, timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"error": f"{type(e).__name__}: {e}", "seconds": time.time() - started}
    wall = time.time() - started
    t = d.get("timings") or {}
    usage = d.get("usage") or {}
    return {
        "seconds": round(wall, 3),
        "predicted_n": t.get("predicted_n") or usage.get("completion_tokens") or 0,
        "predicted_ms": t.get("predicted_ms"),
        "prompt_n": t.get("prompt_n") or usage.get("prompt_tokens") or 0,
        "prompt_ms": t.get("prompt_ms"),
        "cache_n": t.get("cache_n"),
        "predicted_per_second": t.get("predicted_per_second"),
        "prompt_per_second": t.get("prompt_per_second"),
        "finish_reason": ((d.get("choices") or [{}])[0]).get("finish_reason"),
    }


def run_level(endpoint: str, model: str, concurrency: int, tokens: int,
              reps: int, timeout: float, tag: str) -> dict:
    """Уровень загрузки: `reps` раундов по `concurrency` одновременных запросов."""
    rounds = []
    for rep in range(reps):
        results: list[dict | None] = [None] * concurrency

        def worker(i: int) -> None:
            nonce = f"{tag}-{concurrency}-{rep}-{i}"
            results[i] = chat_request(endpoint, model, PROMPT_TMPL.format(nonce=nonce),
                                      tokens, timeout)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.time() - t0
        ok = [r for r in results if r and not r.get("error")]
        errors = [r for r in results if r and r.get("error")]
        pred = sum(r["predicted_n"] for r in ok)
        prompt = sum(r["prompt_n"] for r in ok)
        busy_ms = sum((r.get("predicted_ms") or 0.0) for r in ok)
        per_req = [round(r["predicted_n"] / r["seconds"], 2)
                   for r in ok if r["seconds"] > 0]
        rounds.append({
            "rep": rep,
            "concurrency": concurrency,
            "wall_seconds": round(wall, 2),
            "requests_ok": len(ok),
            "requests_failed": len(errors),
            "completion_tokens_total": pred,
            "prompt_tokens_total": prompt,
            # Суммарная пропускная способность — то, ради чего замер: сколько
            # токенов в секунду даёт загрузка целиком, а не один её запрос.
            "tok_per_s_total": round(pred / wall, 2) if wall else None,
            "tok_per_s_total_incl_prompt": round((pred + prompt) / wall, 2) if wall else None,
            "tok_per_s_each": per_req,
            "slots_busy_ratio": round(busy_ms / 1000.0 / wall, 2) if wall else None,
            "errors": [e["error"] for e in errors][:3],
        })
        note(f"  уровень ×{concurrency} раунд {rep + 1}/{reps}: "
             f"{pred} ток за {wall:.1f} с → {round(pred / wall, 2) if wall else 0} ток/с суммарно, "
             f"слотов занято ≈{round(busy_ms / 1000.0 / wall, 2) if wall else 0}")
    best = max((r["tok_per_s_total"] or 0.0) for r in rounds)
    med = statistics.median([r["tok_per_s_total"] or 0.0 for r in rounds])
    return {
        "concurrency": concurrency,
        "rounds": rounds,
        "tok_per_s_total_median": round(med, 2),
        "tok_per_s_total_best": round(best, 2),
    }


def run_prefill(endpoint: str, model: str, lens: list, tokens: int,
                timeout: float) -> list:
    """Префилл на разной глубине контекста: одна и та же работа, разный вход."""
    rows = []
    for n_chars in lens:
        filler = (PREFILL_FILLER * (n_chars // len(PREFILL_FILLER) + 1))[:n_chars]
        prompt = (f"Ниже — фрагмент технической документации.\n\n{filler}\n\n"
                  f"Опиши в одном предложении, о чём этот фрагмент.")
        rows.append(chat_request(endpoint, model, prompt, tokens, timeout))
        r = rows[-1]
        if r.get("error"):
            note(f"  префилл ~{n_chars} симв.: ОШИБКА {r['error']}")
            continue
        note(f"  префилл ~{n_chars} симв. ({r['prompt_n']} ток.): "
             f"prefill {r['prompt_per_second'] and round(r['prompt_per_second'], 1)} ток/с, "
             f"decode {r['predicted_per_second'] and round(r['predicted_per_second'], 2)} ток/с")
    return rows


def do_measure(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    endpoint = args.endpoint
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    note(f"эндпоинт {endpoint}, модель {args.model}, слотов {args.slots}, ctx {args.ctx}")
    warm = chat_request(endpoint, args.model, PROMPT_TMPL.format(nonce="warmup"),
                        max(1, args.tokens // 4), args.timeout)
    if warm.get("error"):
        note(f"NOT-VERIFIED: эндпоинт не ответил: {warm['error']}")
        return EXIT_NOT_VERIFIED
    note(f"прогрев: {warm['predicted_n']} ток за {warm['seconds']} с, "
         f"tok/s запроса {warm['predicted_per_second']}")

    note("уровни загрузки:")
    measured = [run_level(endpoint, args.model, n, args.tokens, args.reps,
                          args.timeout, "load") for n in levels]

    prefill = []
    if args.prefill_lens.strip():
        note("префилл на глубине:")
        prefill = run_prefill(endpoint, args.model,
                              [int(x) for x in args.prefill_lens.split(",") if x.strip()],
                              args.prefill_tokens, args.timeout)

    single = next((m for m in measured if m["concurrency"] == 1), None)
    total_ok = sum(r["completion_tokens_total"]
                   for m in measured for r in m["rounds"])
    if total_ok == 0:
        note("NOT-VERIFIED: ни один уровень не дал токенов")
        return EXIT_NOT_VERIFIED

    doc = {
        "schema": "s3ah-throughput-measure/1",
        "stage": "S3ah",
        "date": now_iso(),
        "endpoint": endpoint,
        "model": args.model,
        "env": None,
    }
    if args.tunnel and Path(args.tunnel).exists():
        doc["env"] = json.loads(Path(args.tunnel).read_text(encoding="utf-8"))
    doc.update({
        "conditions": {
            "server_slots": args.slots,
            "server_ctx": args.ctx,
            "kv_cache": args.kv_cache,
            "tokens_per_request": args.tokens,
            "reps": args.reps,
            "prompt_unique": True,
            "ignore_eos": True,
            "thinking": False,
            "temperature": 0.0,
            "note": ("промпт каждого запроса уникален (nonce): иначе попадание в кэш "
                     "промпта слота мерило бы кэш, а не движок"),
        },
        "warmup": warm,
        "levels": measured,
        "single_request_tok_per_s": (
            round(statistics.median([r["tok_per_s_each"][0] for r in single["rounds"]
                                     if r["tok_per_s_each"]]), 2) if single else None),
        "prefill": prefill,
    })
    path = out / "measure.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    note(f"записано: {path}")
    return EXIT_OK


def do_report(args) -> int:
    """Сборка evidence: замер + факты стенда + таблица объёма."""
    measure = json.loads(Path(args.measure).read_text(encoding="utf-8"))
    facts = (json.loads(Path(args.platform_facts).read_text(encoding="utf-8"))
             if args.platform_facts and Path(args.platform_facts).exists() else None)
    volume = json.loads(Path(args.volume).read_text(encoding="utf-8"))
    tunnel = (json.loads(Path(args.tunnel).read_text(encoding="utf-8"))
              if args.tunnel and Path(args.tunnel).exists() else measure.get("env"))

    unique = volume["volume"]
    # Токены полного прохода считаются по ЗАМЕРЕННОМУ отношению символов к токену
    # у самого учителя (chars_out_per_tok из журнала пилота S3ag), а не по
    # «4 символа на токен»: у русского текста это число другое, и подстановка
    # общего правила занизила бы объём.
    cpt = volume["price"]["chars_per_tok"][volume["price"]["compared_teacher"]]
    tokens_unique = round(unique["think_chars_unique"] / cpt)
    tokens_all = round(unique["think_chars"] / cpt)

    throughput_rows = []
    for lvl in measure["levels"]:
        n = lvl["concurrency"]
        throughput_rows.append({
            "mode": "single" if n == 1 else f"batch{n}" + ("" if n <= measure["conditions"]["server_slots"] else "_queued"),
            "slots": measure["conditions"]["server_slots"],
            "ctx": measure["conditions"]["server_ctx"],
            "tokens_total": sum(r["completion_tokens_total"] for r in lvl["rounds"]),
            "seconds": round(sum(r["wall_seconds"] for r in lvl["rounds"]), 2),
            "tok_per_s_total": lvl["tok_per_s_total_median"],
            "note": (f"одновременных запросов {n}, раундов {len(lvl['rounds'])}, "
                     f"по {measure['conditions']['tokens_per_request']} токенов, "
                     f"промпты уникальны; слотов занято ≈"
                     f"{lvl['rounds'][0]['slots_busy_ratio']}"),
        })
    for row in measure.get("prefill") or []:
        if row.get("error"):
            continue
        throughput_rows.append({
            "mode": f"prefill_ctx{row['prompt_n']}tok",
            "slots": measure["conditions"]["server_slots"],
            "ctx": measure["conditions"]["server_ctx"],
            "tokens_total": row["predicted_n"],
            "seconds": row["seconds"],
            "tok_per_s_total": round(row["predicted_n"] / row["seconds"], 2) if row["seconds"] else None,
            "note": (f"глубина входа {row['prompt_n']} токенов: префилл "
                     f"{row['prompt_per_second'] and round(row['prompt_per_second'], 1)} ток/с, "
                     f"декод {row['predicted_per_second'] and round(row['predicted_per_second'], 2)} ток/с"),
        })

    # Таблица объёма: часы стенда на каждую схему и долю. Берётся МЕДИАНА раундов,
    # а не лучший раунд: лучший — это выбор удачного замера, и он занижал бы
    # календарь. Рядом в `levels_raw` лежат оба числа, если нужен размах.
    schemes = {"single": measure["single_request_tok_per_s"]}
    for lvl in measure["levels"]:
        schemes[f"batch{lvl['concurrency']}"] = lvl["tok_per_s_total_median"]
    if facts and facts.get("vllm_measured_tok_per_s"):
        schemes["vllm"] = facts["vllm_measured_tok_per_s"]

    shares = [("100%", tokens_all, "все вхождения рассуждений"),
              ("100%_unique", tokens_unique, "уникальные фрагменты (вхождения получают тот же текст)"),
              ("5%_unique", round(tokens_unique * 0.05), "5 % уникальных фрагментов"),
              ("1%_unique", round(tokens_unique * 0.01), "1 % уникальных фрагментов"),
              ("heads_100tok_unique", round(volume["volume"]["think_fragments_unique"] * 100),
               "«только начала»: 100 токенов на каждый уникальный фрагмент")]
    volume_table = []
    for scheme, speed in schemes.items():
        if not speed:
            continue
        for share, toks, why in shares:
            volume_table.append({
                "scheme": scheme, "share": share,
                "tokens": toks,
                "hours": round(toks / speed / 3600.0, 2),
                "note": f"{why}; скорость {speed} ток/с",
            })

    vllm = {"available": False, "why_not": "факты стенда не собраны"}
    if facts:
        vllm = {
            "available": bool(facts.get("vllm_available")),
            "why_not": facts.get("vllm_why_not"),
            "weights_hf_bf16_gib": facts.get("weights_hf_bf16_gib"),
            "cuda_free_gib_at_probe": facts.get("cuda_free_gib"),
            "registry_engines_for_27b": facts.get("registry_engines_for_27b"),
            "checked": facts.get("checks"),
        }

    doc = {
        "schema": "s3ah-throughput/1",
        "stage": "S3ah",
        "date": now_iso(),
        "status": "complete",
        "purpose": ("замеренная пропускная способность 27B (одиночная / батч ×N / префилл "
                    "на глубине) и проверка доступности vLLM — основание для схемы объёма "
                    "русскоязычных трасс (ADR-038)"),
        "tunnel": tunnel,
        "conditions": measure["conditions"],
        # Параметры движка берутся из САМОГО сервера (`/props` прямого порта и
        # аргументы процессов), а не из задания: задание называет 4 слота, и
        # подтвердить это можно только у сервера.
        "server_verified": (facts or {}).get("loaded_server"),
        "server_processes": (facts or {}).get("llama_servers"),
        "stand_memory": (facts or {}).get("meminfo"),
        "throughput": throughput_rows,
        "levels_raw": measure["levels"],
        "single_request_tok_per_s": measure["single_request_tok_per_s"],
        "prefill": measure.get("prefill"),
        "vllm": vllm,
        "volume": {
            "records": volume["volume"]["records"],
            "think_fragments_unique": volume["volume"]["think_fragments_unique"],
            "think_chars_unique": volume["volume"]["think_chars_unique"],
            "think_chars_all": volume["volume"]["think_chars"],
            "chars_per_tok_teacher": cpt,
            "tokens_unique": tokens_unique,
            "tokens_all": tokens_all,
            "tokens_heads": volume["volume"]["think_fragments_unique"] * 100,
            "source": args.volume,
            "note": ("объём рассуждений взят из замера S3ag (он мерил полный проход по "
                     "набору); токены — по замеренному отношению символов к токену у самого "
                     "учителя, а не по правилу «4 символа на токен»"),
        },
        "volume_table": volume_table,
        "conclusion": None,
        "limits": [
            "квантование НЕ мерялось: смена кванта требует перезагрузки весов, а на стенде "
            "по ADR-013 одновременно работает один тяжёлый движок (инцидент NVRM OOM 02.08) — "
            "поднимать второй сервер рядом с платформенным запрещено. Оценка по механизму "
            "названа оценкой, а не замером (см. `quant_estimate`)",
            "vLLM для 27B не поднимался: см. `vllm.why_not` — причина измерена, а не предположена",
            "замер снят на роутере (порт 8080 через туннель); прямой порт 53581 живёт внутри "
            "контейнера и снаружи не слушается — это тот же процесс-движок, что отдаёт роутер",
        ],
        "quant_estimate": facts.get("quant_estimate") if facts else None,
    }
    if args.conclusion and Path(args.conclusion).exists():
        doc["conclusion"] = json.loads(Path(args.conclusion).read_text(encoding="utf-8"))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        note(f"записано: {args.out}")
    else:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="S3ah: пропускная способность учителя 27B")
    ap.add_argument("--mode", choices=["measure", "report"], required=True)
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--slots", type=int, default=4, help="слотов у сервера (--parallel)")
    ap.add_argument("--ctx", type=int, default=262144, help="ctx-size сервера")
    ap.add_argument("--kv-cache", default="q8_0/q8_0 (cache-type-k/v)")
    ap.add_argument("--levels", default="1,2,4,8", help="уровни одновременности")
    ap.add_argument("--tokens", type=int, default=100, help="токенов на запрос")
    ap.add_argument("--reps", type=int, default=2, help="раундов на уровень")
    ap.add_argument("--prefill-lens", default="800,8000,32000",
                    help="длины входа в символах (0 — не мерить)")
    ap.add_argument("--prefill-tokens", type=int, default=32)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--tunnel", default="", help="JSON с описанием туннеля (провенанс)")
    ap.add_argument("--out", default="")
    # report
    ap.add_argument("--measure", default="")
    ap.add_argument("--platform-facts", default="")
    ap.add_argument("--volume", default="evidence/s3ag-teacher-comparison.json")
    ap.add_argument("--conclusion", default="")
    args = ap.parse_args()
    if args.mode == "measure":
        if not args.out:
            note("--out обязателен в режиме measure")
            return EXIT_NOT_VERIFIED
        return do_measure(args)
    if not args.measure or not Path(args.measure).exists():
        note("NOT-VERIFIED: нет файла замера (--measure)")
        return EXIT_NOT_VERIFIED
    if not Path(args.volume).exists():
        note(f"NOT-VERIFIED: нет файла объёма ({args.volume})")
        return EXIT_NOT_VERIFIED
    return do_report(args)


if __name__ == "__main__":
    sys.exit(main())
