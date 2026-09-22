#!/usr/bin/env python3
#
# ── Провенанс (S3ab, перенос без переписывания логики) ───────────────────────
# Источник: ветка arch/laguna-replay50, коммит c6e93741422b994f3689472b3c927c052c68aec6,
# путь кейсы/laguna-compact/runs/replay50-collapse-20260916/probe_control.py
# blob 6516b9ef0dc9cc138882bdf746737c70bd6c9ceb, sha256 файла
# 882b4e05670eacc80273ccb1597825d9f6c9ce173aacb3e08ad384435118aaa0 (сверено при переносе).
# Точно так же файл лежит в ветке arch/laguna-canon (blob совпадает побайтово).
# Отличия переноса — ровно два, оба вынужденные, методику не трогают:
#   (а) CASE: инструмент переехал из runs/<прогон>/ в tools/, поэтому корень кейса —
#       parent.parent, а не parent.parent.parent (в исходнике файл лежал на уровень глубже);
#   (б) добавлен аргумент --ckpt TAG=PATH (повторяемый): проба произвольного чекпойнта
#       по точному пути. Нужен потому, что исходный обход знает только руки калибровки
#       (`--arms`/`--steps`), а проба финала полного CPT живёт вне их сетки. Ни протокол
#       проб (PROBE_PROMPTS, chat template, greedy, max_new_tokens=384), ни метрики
#       (degenerate_metrics) не изменены — иначе числа не были бы сопоставимы с
#       probes_cpt.json рук, а именно это и требуется от пробы.
"""A2 (replay50-collapse): проба поведения — база и промежуточные чекпойнты рук.

Зачем. Вердикт узла опирался на чтение проб четырёх рук «на глаз»: руки 25 %
описаны как «пробы связны и язык цел», руки 50 % — как распад. Проверить это
можно только с опорой: на чём измерена «связность»? Инструмент даёт две опоры,
которых в исходных данных нет.

1. **База** — та же проба на нетронутой Qwen2.5-0.5B (с той же процедурой
   спецтокенов, что в пайплайне). Без неё «распад» неотличим от «модель всегда
   так отвечала»: пробы короткие, а 0.5B слабая.
2. **Траектория** — те же пробы на чекпойнтах 500/1000/1500/2000. Конечная точка
   говорит «плохо», траектория говорит «когда началось». Разница существенна:
   деградация к шагу 500 (≈4.1M токенов) и деградация к шагу 2000 — разные
   диагнозы, и первый несовместим с объяснением «мало/много replay в миксе».

Протокол пробы повторяет `run_probes` пайплайна дословно (те же 5 промптов, тот же
`UNIFIED_SYSTEM_PROMPT` для доменных, chat template, greedy, `max_new_tokens=384`),
иначе сравнение с `probes_cpt.json` рук было бы несравнимым.

Чтение: чекпойнты и кэши — с сетевого диска, только чтение (AD-4). Ничего не
обучается; генерация — inference-only на локальной 4080 (стенд GB10 не задействован).

Коды возврата::

    0 — отчёт собран
    2 — NOT-VERIFIED: нет весов базы или чекпойнтов
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

#: Корень кейса. В исходнике (runs/replay50-collapse-*/probe_control.py) это
#: parent.parent.parent, здесь файл лежит в tools/ — на уровень выше, поэтому parent.parent.
CASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE / "tools"))
import ppl_probe as P  # noqa: E402

CALIB = Path("/home/user/gb10-shared/calib")
ARMS = ["25-0.35", "25-0.7", "50-0.35", "50-0.7"]
STEPS = [500, 1000, 1500, 2000]
#: Разные руки писали чекпойнты под разными именами (патч нумерации менялся):
#: 25-0.7 — старым `checkpoint_N.pt`, остальные — `calib_checkpoint_N.pt`.
CKPT_NAMES = ["calib_checkpoint_{step}.pt", "checkpoint_{step}.pt"]

UNIFIED_SYSTEM_PROMPT = (
    "Ты — эксперт по ML/AI с доступом к библиотеке концептов через инструмент поиска.\n\n"
    "Чтобы найти концепт, вызови инструмент:\n"
    '<tool_call>{"name": "search_concepts", "query": "название_концепта"}</tool_call>\n\n'
    "Инструмент вернёт определения в блоке <tool_response>. Затем продолжай рассуждение "
    "или вызови инструмент ещё раз. Когда соберёшь достаточно информации — дай финальный "
    "ответ БЕЗ <tool_call>.\n\n"
    "Правила:\n- Используй <think> для рассуждений\n- Один <tool_call> за ход\n"
    "- В финальном ответе упоминай slug-и концептов и объясняй точно по найденным "
    "определениям\n\n"
    "Пример диалога:\nВопрос: Объясни связь между 'grpo' и 'advantage_estimation'.\n"
    "Ответ:\n<think>Мне нужно найти определение grpo.</think>\n"
    '<tool_call>{"name": "search_concepts", "query": "grpo"}</tool_call>\n'
    "<tool_response>\nslug: grpo\ntype: algorithmic_primitive\n"
    "Определение: Алгоритм reinforcement learning, который генерирует группы выходов "
    "для каждого входа и оптимизирует политику через групповую оценку преимущества.\n"
    "</tool_response>\n<think>Теперь найду advantage_estimation.</think>\n"
    '<tool_call>{"name": "search_concepts", "query": "advantage_estimation"}</tool_call>\n'
    "<tool_response>\nslug: advantage_estimation\ntype: behavioral_mechanism\n"
    "Определение: Оценка преимущества действия относительно базовой линии для уменьшения "
    "дисперсии градиента политики.\n</tool_response>\n"
    "<think>Оба определения найдены. grpo использует advantage_estimation как ключевой "
    "механизм.</think>\n## Связь между grpo и advantage_estimation\n\n"
    "**grpo** (algorithmic_primitive): Алгоритм reinforcement learning, который "
    "генерирует группы выходов для каждого входа и оптимизирует политику через "
    "групповую оценку преимущества.\n\n"
    "**advantage_estimation** (behavioral_mechanism): Оценка преимущества действия "
    "относительно базовой линии для уменьшения дисперсии градиента политики.\n\n"
    "grpo строится на advantage_estimation: вместо критика он оценивает преимущество "
    "как отклонение reward от среднего по группе."
)

PROBE_PROMPTS = [
    {"tag": "domain_tool", "system": True,
     "text": "Найди концепты про обучение языковых моделей агентов с подкреплением."},
    {"tag": "domain_knowledge", "system": True,
     "text": "Объясни, что такое WSD-расписание learning rate и почему его используют."},
    {"tag": "general_language", "system": False,
     "text": "Расскажи, как приготовить домашний хлеб на закваске."},
    {"tag": "general_reasoning", "system": False,
     "text": "Если у Маши было 3 яблока и она отдала одно Пете, а потом нашла ещё 2, "
             "сколько яблок у Маши? Ответь одним предложением."},
    {"tag": "instruction", "system": False,
     "text": "Напиши одно предложение о пользе чтения."},
]

RE_CYR = re.compile(r"[а-яА-ЯёЁ]")
RE_LAT = re.compile(r"[a-zA-Z]")
RE_DIGIT = re.compile(r"\d")


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def degenerate_metrics(text: str) -> dict:
    """Метрики вырождения ответа.

    ``uniq4`` — доля уникальных 4-грамм слов: у здорового ответа близка к 1, у
    зацикленного падает к 0. ``max_ngram_rep`` — сколько раз повторяется самая
    частая 4-грамма. Обе смотрят на **слова**, поэтому отдельно считается
    ``max_char_run``: зацикливание бывает и внутри одного «слова»
    (`hypothesis_1_hypothesis_2_…`), и словесная метрика его не видит.
    """
    words = text.split()
    n = len(words)
    out: dict = {"chars": len(text), "words": n}
    if n >= 8:
        grams: dict[str, int] = {}
        for i in range(n - 3):
            k = " ".join(words[i:i + 4])
            grams[k] = grams.get(k, 0) + 1
        reps = sum(v for v in grams.values() if v > 1)
        top = max(grams.values())
        out["uniq4"] = round(len(grams) / max(n - 3, 1), 3)
        out["max4gram_rep"] = top
        out["top4gram"] = max(grams, key=grams.get)[:50]
        out["repeated_4gram_share"] = round(reps / max(n - 3, 1), 3)
    else:
        out["uniq4"] = None
        out["max4gram_rep"] = None
        out["top4gram"] = ""
        out["repeated_4gram_share"] = None
    #: самое длинное повторение одной и той же подстроки символов
    best = 1
    for size in (4, 8, 16, 24):
        if len(text) < 2 * size:
            continue
        chunks = [text[i:i + size] for i in range(0, len(text) - size, size)]
        cnt: dict[str, int] = {}
        for c in chunks:
            cnt[c] = cnt.get(c, 0) + 1
        if cnt:
            best = max(best, max(cnt.values()))
    out["max_repeat_of_char_block"] = best
    cyr, lat = len(RE_CYR.findall(text)), len(RE_LAT.findall(text))
    out["cyrillic_chars"] = cyr
    out["latin_chars"] = lat
    out["cyrillic_share"] = round(cyr / max(cyr + lat, 1), 3)
    out["has_think"] = "<think>" in text
    out["has_tool_call"] = "<tool_call>" in text
    return out


def generate_all(model, tokenizer, torch, tag: str, device: str) -> list[dict]:
    results = []
    for p in PROBE_PROMPTS:
        msgs = ([{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}] if p["system"] else []) + \
               [{"role": "user", "content": p["text"]}]
        prompt = tokenizer.apply_chat_template(msgs, tokenize=False,
                                               add_generation_prompt=True)
        enc = tokenizer(prompt, return_tensors="pt").to(device)
        with torch.no_grad():
            out = model.generate(**enc, max_new_tokens=384, do_sample=False,
                                 pad_token_id=tokenizer.pad_token_id
                                 or tokenizer.eos_token_id)
        resp = tokenizer.decode(out[0][enc["input_ids"].shape[1]:],
                                skip_special_tokens=False).strip()
        results.append({"tag": p["tag"], "system": p["system"], "prompt": p["text"],
                        "response": resp, "metrics": degenerate_metrics(resp)})
        note(f"  [{tag}] {p['tag']}: {resp[:90]!r}")
    return results


def load_ckpt_into(model, path: Path, torch) -> dict:
    """Перенос state_dict в модель с переносом embeddings при смене числа токенов.

    Копия `_load_ckpt_with_resize` из пайплайна: без неё строки спецтокенов
    (151665..151670) не совпадут по форме, и `load_state_dict` либо откажет,
    либо — при `strict=False` — молча оставит случайную инициализацию, то есть
    проба мерила бы не чекпойнт.
    """
    ck = torch.load(str(path), map_location="cpu", mmap=True, weights_only=False)
    sd = ck["model"] if isinstance(ck, dict) and "model" in ck else ck
    cur = model.state_dict()
    moved = []
    for key in list(sd.keys()):
        if key in cur and tuple(sd[key].shape) != tuple(cur[key].shape):
            old = sd.pop(key)
            if "embed_tokens" in key or key in ("lm_head.weight",):
                moved.append({"key": key, "from": list(old.shape),
                              "to": list(cur[key].shape)})
                new = cur[key].clone()
                k = min(old.shape[0], new.shape[0])
                new[:k] = old[:k]
                sd[key] = new
                if key == "lm_head.weight" and hasattr(model, "lm_head"):
                    model.lm_head.weight = torch.nn.Parameter(new)
            else:
                moved.append({"key": key, "from": list(old.shape),
                              "to": list(cur[key].shape), "dropped": True})
    missing, unexpected = model.load_state_dict(sd, strict=False)
    del ck, sd
    torch.cuda.empty_cache()
    return {"moved_for_resize": moved,
            "missing_keys": len(missing), "unexpected_keys": len(unexpected)}


def normalize(path: Path) -> int:
    """Привести отчёт к канонической форме: `runs[state] = {"probes": [...]}` + сводка.

    Нужен потому, что первая редакция инструмента писала состояния базовой модели
    **списком**. Отчёт от этого не переставал быть верным (ответы на месте), но
    сводка и сборщик evidence искали ключ `probes`, не находили его и молча
    пропускали состояние — то есть «нет строки» читалось как «нет замера».
    Функция чинит форму, не трогая ни одного ответа.
    """
    d = json.loads(path.read_text(encoding="utf-8"))
    fixed = []
    for tag, val in d.get("runs", {}).items():
        if isinstance(val, list):
            d["runs"][tag] = {"probes": val}
            fixed.append(tag)
    summary = {}
    for tag, val in d.get("runs", {}).items():
        if not isinstance(val, dict) or "probes" not in val:
            continue
        summary[tag] = {p["tag"]: {k: p["metrics"][k] for k in
                                  ("uniq4", "max4gram_rep", "max_repeat_of_char_block",
                                   "cyrillic_share", "words", "has_think")}
                        for p in val["probes"]}
    d["summary"] = summary
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    note(f"нормализовано: состояния-списки {fixed or '—'}; сводка по {len(summary)} состояниям")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--steps", default=",".join(str(s) for s in STEPS))
    ap.add_argument("--calib-dir", default=str(CALIB))
    ap.add_argument("--skip-base", action="store_true")
    ap.add_argument("--device", default="cuda",
                    help="cuda или cpu; cpu нужен, когда 4080 занята соседним узлом — "
                         "ждать её нельзя (запрет фоновых ожиданий), а конкурировать "
                         "за неё нельзя тем более (AD-5: одна нагрузка на устройство)")
    ap.add_argument("--only", default=None,
                    help="список состояний через запятую: base, 50-0.35@500, … — "
                         "для выборочного прогона (полная сетка на CPU дорога)")
    ap.add_argument("--instruct", action="store_true",
                    help="дополнительно снять те же пробы с Qwen2.5-0.5B-Instruct — "
                         "здоровая опора прибора. Нужна потому, что база (не instruct) "
                         "на эти промпты продолжает текст, а не отвечает: без опоры "
                         "«проба выглядит плохо» неотличимо от «модель не инструктируема»")
    ap.add_argument("--normalize", default=None,
                    help="привести готовый отчёт к канонической форме и выйти "
                         "(чинит состояния, записанные списком, и пересчитывает сводку)")
    ap.add_argument("--out", default=None,
                    help="файл для инкрементальной записи: состояние пишется сразу "
                         "после генерации, а не в конце — прогон, снятый по таймауту, "
                         "иначе не оставляет ничего")
    ap.add_argument("--ckpt", action="append", default=[],
                    metavar="TAG=PATH",
                    help="проба чекпойнта по точному пути (повторяемый): --ckpt "
                         "cfinal=/path/checkpoint_final.pt. Добавлено S3ab: обход "
                         "--arms/--steps знает только руки калибровки, а финал полного "
                         "CPT лежит вне их сетки. Протокол и метрики те же")
    args = ap.parse_args()

    if args.normalize:
        return normalize(Path(args.normalize))

    P.repair_broken_pyopenssl()
    P.import_transformers()
    import torch
    if args.device == "cpu":
        torch.set_num_threads(max(1, (__import__("os").cpu_count() or 4) - 2))

    model, tokenizer, prov, errs = P.load_model("base", "float32", args.device,
                                                pipeline_tokenizer=True)
    if model is None:
        note("NOT-VERIFIED: база не загрузилась: " + "; ".join(errs))
        return 2
    note(f"база загружена ({args.device}): {prov}")

    out: dict = {"schema": "probe-control/1", "stage": "A2 replay50-collapse",
                 "device": args.device,
                 "model_provenance": prov,
                 "protocol": {"prompts": [p["tag"] for p in PROBE_PROMPTS],
                              "decoding": "greedy", "max_new_tokens": 384,
                              "system_prompt": "копия run_probes из laguna_pipeline_calib.py"},
                 "runs": {}}

    def flush() -> None:
        if not args.out:
            return
        summary = {}
        for tag_, val in out["runs"].items():
            if "probes" not in val:
                continue
            summary[tag_] = {p["tag"]: {k: p["metrics"][k] for k in
                                        ("uniq4", "max4gram_rep",
                                         "max_repeat_of_char_block",
                                         "cyrillic_share", "words")}
                             for p in val["probes"]}
        out["summary"] = summary
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

    only = set(args.only.split(",")) if args.only else None

    if not args.skip_base and (only is None or "base" in only):
        note("проба: base (нетронутая Qwen2.5-0.5B)")
        #: Форма записи одна для всех состояний — `{"probes": [...]}`. Список
        #: вместо словаря ломает и сводку, и сборщик evidence: они ищут ключ
        #: `probes`, а у списка его нет, и состояние молча выпадает из итога.
        out["runs"]["base"] = {"probes": generate_all(model, tokenizer, torch, "base",
                                                      args.device)}
        flush()

    if args.instruct and (only is None or "instruct" in only):
        #: Отдельная загрузка: instruct — другая ревизия весов, и подменять ею
        #: состояния чекпойнтов нельзя. Здесь она нужна только как опора прибора.
        note("проба: instruct (Qwen2.5-0.5B-Instruct — здоровая опора)")
        imodel, itok, iprov, ierrs = P.load_model("instruct", "float32", args.device,
                                                  pipeline_tokenizer=True)
        if imodel is None:
            out["runs"]["instruct"] = {"skipped": "; ".join(ierrs)}
            note("  instruct не загрузился: " + "; ".join(ierrs))
        else:
            out["instruct_provenance"] = iprov
            out["runs"]["instruct"] = {"probes": generate_all(imodel, itok, torch,
                                                              "instruct", args.device)}
            del imodel
        flush()

    #: Проба чекпойнтов по точному пути (S3ab). Идёт до обхода рук: у прогона
    #: финала полного CPT своя сетка имён, и `--arms` для него пуст.
    for spec in args.ckpt:
        tag, _, path_str = spec.partition("=")
        if not tag or not path_str:
            note(f"  --ckpt '{spec}': ожидается TAG=PATH — пропуск")
            out["runs"][spec or "?"] = {"skipped": "формат аргумента не TAG=PATH"}
            flush()
            continue
        path = Path(path_str).expanduser()
        if only is not None and tag not in only:
            continue
        if not path.is_file():
            out["runs"][tag] = {"skipped": f"чекпойнт не найден: {path}"}
            note(f"  {tag}: чекпойнт не найден ({path})")
            flush()
            continue
        note(f"проба: {tag} ({path})")
        info = load_ckpt_into(model, path, torch)
        res = generate_all(model, tokenizer, torch, tag, args.device)
        out["runs"][tag] = {"checkpoint": str(path),
                            "checkpoint_bytes": path.stat().st_size,
                            "load": info, "probes": res}
        flush()

    calib = Path(args.calib_dir)
    for arm in args.arms.split(","):
        if not arm:
            continue
        d = calib / f"calib-{arm}-20260916-0820" / "checkpoints"
        for step in [int(s) for s in args.steps.split(",") if s]:
            tag = f"{arm}@{step}"
            if only is not None and tag not in only:
                continue
            path = None
            for pat in CKPT_NAMES:
                cand = d / pat.format(step=step)
                if cand.is_file():
                    path = cand
                    break
            if path is None and step == 2000:
                cand = d / "checkpoint_final.pt"
                path = cand if cand.is_file() else None
            if path is None:
                out["runs"][tag] = {"skipped": "чекпойнт не найден"}
                note(f"  {tag}: чекпойнт не найден")
                flush()
                continue
            note(f"проба: {tag} ({path.name})")
            info = load_ckpt_into(model, path, torch)
            res = generate_all(model, tokenizer, torch, tag, args.device)
            out["runs"][tag] = {"checkpoint": str(path),
                                "checkpoint_bytes": path.stat().st_size,
                                "load": info, "probes": res}
            flush()

    if not args.out:
        flush()
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
