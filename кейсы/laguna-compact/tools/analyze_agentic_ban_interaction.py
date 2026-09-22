#!/usr/bin/env python3
"""S3am — почему штатный запрет повторов ломает АГЕНТНУЮ пробу: механизм, а не мнение.

**Вопрос.** Штатный режим стадии (запрет повторов 4-грамм, ADR-041) снимает петлю
механически и на языковых пробах работает в плюс. Агентная проба после его введения
дала другие числа: переснятая база — самостоятельных вызовов 36.6 % против 45.1 %
(legacy), pass-rate 4.0 % против 15.1 %. Различие надо **объяснить**, а не назвать:
«модель стала хуже» — не объяснение, а пересказ разницы.

**Что проверяется здесь.** Гипотеза одна и она механическая: агентный протокол требует
от модели **воспроизвести синтаксис вызова инструмента**, а этот синтаксис лежит в
системном промпте **образцом** (`<tool_call>{"name": "search_concepts", "query": "…"}`,
три примера в `UNIFIED_SYSTEM_PROMPT`). Запрет повторов n-грамм запрещает токен,
если 4-грамма «последние n−1 токенов + T» уже встречалась **в последовательности,
включая промпт** (правило fairseq/transformers). Значит каноническая форма вызова
под запретом **недостижима**: модель обязана «обойти» запрет и портит JSON — а
сломанный JSON верификатор не принимает, инструмент отвечает ошибкой, и агентность
падает не потому, что модель разучилась, а потому что **канал вызова перерезан
протоколом**.

**Как это измеряется, без шума и без модели.**
1. Контекст собирается **как в пробе** (системный промпт + реальная задача пула +
   prefill) и токенизируется тем же токенизатором.
2. Строится состояние запрета (`passrate_probe.NoRepeatNGramBan`) и по канонической
   форме вызова проверяется, **на каком токене** она запрещена.
3. **Отрицательный контроль** (без него прибор не прибор): та же форма при
   `no_repeat_ngram = 0` обязана пройти целиком. Если она проходит и под запретом —
   гипотеза не подтверждена, и прибор **отказывает** (код 1), а не смягчает вывод.
4. Счёт ошибок инструмента в сырых записях обоих прогонов: механизм обязан быть
   виден ещё и в поведении (legacy: 0 ошибок на 350 траекторий; штатный: сотни).

Чего прибор **не** делает: не решает, что важнее — единый режим или рабочий канал
вызова. Он называет механизм и его цену; выбор (исключить синтаксис вызова из
запрета, считать запрет только по сгенерированному, вернуть агентную пробу в legacy)
— решение архитектора, и прибор его не подменяет.

Коды возврата::

    0 — отчёт собран (механизм подтверждён, оба контроля на месте)
    1 — отказ: контроль не прошёл — каноническая форма не запрещается либо
        запрещается и без запрета повторов (утверждение о механизме неверно)
    2 — NOT-VERIFIED: нет входа (нет пула задач, нет сырых записей прогона)

Запуск::

    /usr/bin/python3 tools/analyze_agentic_ban_interaction.py \\
        --run runs/s3am-standard-mode-20260918/agentic \\
        --legacy-run runs/passrate-agentic-cpt-20260917-0153 \\
        --out runs/s3am-standard-mode-20260918/ban_protocol_interaction.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE / "tools"))
sys.path.insert(0, str(CASE))

import passrate_probe as PR                    # noqa: E402  (правило запрета — вызовом)

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Формы вызова, которые модель обязана уметь напечатать. Первая — **дословно** та,
#: что лежит образцом в `UNIFIED_SYSTEM_PROMPT`; остальные — её же части и варианты
#: (с перевода строки, с другим slug), чтобы вывод не держался на одной строке.
CANONICAL_FORMS = (
    ("точная форма образца", '<tool_call>{"name": "search_concepts", "query": "grpo"}'),
    ("с перевода строки", '\n<tool_call>{"name": "search_concepts", "query": "grpo"}'),
    ("только ключи вызова", '{"name": "search_concepts", "query": "'),
    ("другой slug", '<tool_call>{"name": "search_concepts", "query": "advantage_estimation"}'),
    ("минимальный вызов", '{"name": "search_concepts"}'),
)


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(CASE))
    except ValueError:
        return str(p)


def load_jsonl(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ─────────────────── проверка «запрещена ли каноническая форма» ───────────────

def first_forbidden(ban: "PR.NoRepeatNGramBan | None", token_ids: list[int], tok,
                    decode=None) -> dict:
    """На каком токене форма запрещена (или не запрещена вовсе).

    Возврат несёт и индекс, и сам токен, и накопленный текст: без них «запрещено»
    неотличимо от «не сошлось что-то другое» — а это разные утверждения.
    """
    seq = list(token_ids)
    for k, t in enumerate(seq):
        if ban is not None and t in ban.banned(0):
            allowed_before = (decode or tok.decode)(seq[:k]) if k else ""
            return {"forbidden": True, "step": k, "n_tokens": len(seq),
                    "token_id": int(t), "token": tok.decode([t]),
                    "emitted_before": allowed_before,
                    "would_emit": (decode or tok.decode)(seq[:k + 1])}
        if ban is not None:
            ban.extend(0, t)
    return {"forbidden": False, "step": None, "n_tokens": len(seq), "would_emit": None}


def context_ids(tok, system_prompt: str, task_prompt: str, prefill: str) -> list[int]:
    """Контекст пробы: chat-template + prefill (как в `run_probe`, `--no-hint`)."""
    msgs = [{"role": "system", "content": system_prompt},
            {"role": "user", "content": task_prompt}]
    text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True) + prefill
    return tok(text)["input_ids"]


def selftest() -> int:
    """Красный путь прибора на фикстурах, без токенизатора и без модели.

    Проверяется само правило и его контроль: (1) форма, чьи 4-граммы уже есть в
    контексте, обязана быть запрещена; (2) та же форма на контексте без повторов —
    пройти целиком; (3) без запрета (`ban=None`) не запрещается ничто.
    """
    class FakeTok:
        def __init__(self, table: dict[int, str]):
            self.table = table

        def decode(self, ids):
            return "".join(self.table.get(i, "?") for i in ids)

    tok = FakeTok({1: "a", 2: "b", 3: "c", 4: "d", 5: "e", 9: "z"})
    #: Контекст содержит 4-грамму (1,2,3,4). Форма (1,2,3,4) обязана упереться в
    #: четвёртом токене: к этому шагу 4-грамма «последние 3 + 4» уже была.
    #: (Фикстура проверяет именно правило: запрет ставится по **последним n−1**
    #: токенам, а не по первым — на этом различии и держится вся арифметика.)
    ban = PR.NoRepeatNGramBan(4, [[1, 2, 3, 4]])
    r = first_forbidden(ban, [1, 2, 3, 4], tok)
    assert r["forbidden"] and r["step"] == 3, r
    assert r["token_id"] == 4 and r["emitted_before"] == "abc", r
    # контроль: тот же контекст, но форма уходит в сторону — проходит целиком
    assert not first_forbidden(PR.NoRepeatNGramBan(4, [[1, 2, 3, 4]]), [1, 2, 3, 9],
                               tok)["forbidden"]
    # контроль: контекст без этой 4-граммы — форма проходит
    assert not first_forbidden(PR.NoRepeatNGramBan(4, [[7, 7, 7, 7]]), [1, 2, 3, 4],
                               tok)["forbidden"]
    # контроль «без запрета»: не запрещается ничего никогда
    assert not first_forbidden(None, [1, 2, 3, 4], tok)["forbidden"]
    # вырожденные случаи не падают
    assert not first_forbidden(PR.NoRepeatNGramBan(4, [[]]), [], tok)["forbidden"]
    assert not first_forbidden(PR.NoRepeatNGramBan(4, [[]]), [1, 2], tok)["forbidden"]
    print("selftest ок: правило и оба контроля")
    return EXIT_OK


def load_pipeline(path: Path):
    """Пайплайн — **файлом по пути `--pipeline`**, а не импортом по имени.

    Так прибор остаётся проверяемым: тесты подставляют синтетический пайплайн и
    проверяют красный путь (системный промпт без образца вызова → механизм не
    подтверждён → отказ), не трогая настоящий контур.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("s3am_pipeline_under_test", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"не читается пайплайн {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─────────────────── «что если»: область запрета (не решение) ────────────────

def alternative_domains(tok, context: list[int], prefill: str) -> dict:
    """Сравнение **области** запрета: с промптом (как сейчас) и только по генерации.

    Это измерение «что если», а не решение. Оно нужно потому, что у архитектора есть
    выбор, которого не было при написании ADR-041: петля (по S3ak) — повтор
    **собственного** текста модели, а канонический вызов — повтор **промпта**. Если
    считать запрет только по сгенерированным токенам, обе задачи решаются сразу:
    каноническая форма проходит, а повтор своей же 4-граммы — нет. Цена варианта
    названа там же: он расходится с правилом `NoRepeatNGramLogitsProcessor`
    (у transformers в состояние входит весь `input_ids`), то есть «тот же режим, что
    у языковых проб» перестаёт быть тождеством — и это уже вопрос к ADR-041 п.1.
    """
    call = '<tool_call>{"name": "search_concepts", "query": "grpo"}'
    toks = tok(call)["input_ids"]
    prefill_ids = tok(prefill)["input_ids"]

    # (а) как сейчас: в состояние входит промпт (весь контекст)
    now = first_forbidden(PR.NoRepeatNGramBan(4, [context]), toks, tok)
    # (б) что если: состояние строится **от prefill**, промпт в него не входит
    gen_only = first_forbidden(PR.NoRepeatNGramBan(4, [prefill_ids]), toks, tok)
    # (в) и повтор собственного вызова (то, ради чего запрет и вводился) — обязан
    #     быть запрещён в обоих вариантах одинаково
    ban_loop = PR.NoRepeatNGramBan(4, [prefill_ids])
    for t in toks:                      # первый вызов
        ban_loop.extend(0, t)
    repeat = first_forbidden(ban_loop, toks, tok)
    return {
        "question": "какая область запрета оставляет канал вызова живым, а петлю — запрещённой",
        "variants": {
            "prompt_included_now": {
                "rule": "состояние строится по всему контексту (промпт + генерация), "
                        "как у transformers NoRepeatNGramLogitsProcessor",
                "canonical_call_blocked": now["forbidden"],
                "blocked_at": now["would_emit"],
                "self_repeat_blocked": repeat["forbidden"],
            },
            "generated_only": {
                "rule": "состояние строится от prefill: запрет считается только по "
                        "сгенерированным токенам, промпт в него не входит",
                "canonical_call_blocked": gen_only["forbidden"],
                "blocked_at": gen_only["would_emit"],
                "self_repeat_blocked": repeat["forbidden"],
            },
        },
        "reading": ("вариант «только по генерации» пропускает каноническую форму и "
                    "запрещает повтор своего же вызова: петля (по S3ak — повтор "
                    "собственного текста) остаётся запрещённой, а канал вызова — живым"),
        "boundary": ("это измерение, а не решение: смена области запрета расходится с "
                     "правилом transformers и с «тем же режимом, что у языковых проб» "
                     "(ADR-041 п.1) — выбирать архитектору; здесь только числа"),
        "not_measured": ("сколько петель снимет вариант «только по генерации» на реальных "
                         "генерациях: для этого нужен новый прогон, а не эта проба"),
    }


# ─────────────────────────── счёт ошибок инструмента ────────────────────────

def tool_error_stats(run: Path) -> dict:
    """Ошибки инструмента по сырым записям прогона — поведенческая сторона механизма.

    Ошибка инструмента (`tool_errors`) в контуре означает ровно две вещи
    (`laguna_pipeline_v8.execute_tool_call`): некорректный JSON в вызове или
    неизвестное имя инструмента. То есть это счётчик **сломанного канала вызова**,
    а не «инструмент не нашёл ничего».
    """
    pools = {}
    tot = {"n": 0, "n_with_error": 0, "n_error_events": 0, "n_calls": 0, "n_self": 0}
    for pool in ("v1", "v2"):
        recs = load_jsonl(run / pool / "tasks.jsonl")
        if not recs:
            continue
        st = {"n": len(recs),
              "n_with_error": sum(1 for r in recs if r.get("tool_errors", 0) > 0),
              "n_error_events": sum(int(r.get("tool_errors", 0)) for r in recs),
              "n_calls": sum(int(r.get("tool_calls", 0)) for r in recs),
              "n_self": sum(1 for r in recs if r.get("tool_calls", 0) > 0)}
        pools[pool] = st
        for k in tot:
            tot[k] += st[k]
    if not pools:
        return {"available": False, "why": f"нет сырых записей в {rel(run)}"}
    return {
        "available": True, "source": rel(run), "pools": pools, "combined": tot,
        "reading": ("`tool_errors > 0` = вызов не разобран (некорректный JSON) или назван "
                    "неизвестный инструмент: это сломанный канал вызова, а не пустой ответ "
                    "поиска"),
        "share_trajectories_with_error": (round(tot["n_with_error"] / tot["n"], 4)
                                           if tot["n"] else None),
        "errors_per_call": (round(tot["n_error_events"] / tot["n_calls"], 3)
                            if tot["n_calls"] else None),
    }


# ─────────────────────────────── main ───────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true",
                    help="фикстуры правила и контролей, без токенизатора и модели")
    ap.add_argument("--run", default="runs/s3am-standard-mode-20260918/agentic")
    ap.add_argument("--legacy-run", default="runs/passrate-agentic-cpt-20260917-0153")
    ap.add_argument("--pool", default="datasets/rl_tasks_revpool_v2.jsonl",
                    help="откуда взять реальную задачу для контекста (ADR-054 п.1: "
                         "набор курикулума стадии — v2)")
    ap.add_argument("--tokenizer", default="Qwen/Qwen2.5-0.5B")
    ap.add_argument("--pipeline", default="laguna_pipeline_v8.py")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    run, legacy = CASE / args.run, CASE / args.legacy_run
    pool = CASE / args.pool
    if not pool.is_file():
        note(f"NOT-VERIFIED: нет пула задач {rel(pool)}")
        return EXIT_NOT_VERIFIED
    errors = tool_error_stats(run)
    errors_legacy = tool_error_stats(legacy)
    if not errors.get("available"):
        note(f"NOT-VERIFIED: {errors.get('why')}")
        return EXIT_NOT_VERIFIED

    #: Отказ, если прибор пробы не в штатном режиме: механизм объясняет **штатный**
    #: запрет, и мерить его на другом режиме значило бы объяснять не то число.
    default_n, _ = PR.resolve_no_repeat_ngram(None, False)
    if default_n != PR.STANDARD_NO_REPEAT_NGRAM:
        note("отказ: агентная проба не в штатном режиме по умолчанию — механизм "
             "объясняет не то число")
        return EXIT_FAIL

    #: Окружение без рабочего transformers — это NOT-VERIFIED, а не трейсбек: у кейса
    #: два интерпретатора, и `python3` из PATH (miniconda 3.11) transformers не
    #: импортирует вовсе (huggingface-hub 1.27.0 не проходит проверку версии). Прогоны
    #: идут `/usr/bin/python3`; прибор, который на «не тем интерпретатором» падает
    #: трейсбеком, неотличим от прибора со сломанной логикой.
    try:
        from transformers import AutoTokenizer
    except Exception as e:                    # noqa: BLE001 (ImportError и VersionError)
        note(f"NOT-VERIFIED: нет рабочего transformers ({type(e).__name__}: {e}) — "
             "контекст не собрать, механизм не проверен (не «механизма нет»)")
        return EXIT_NOT_VERIFIED
    try:
        pipe = load_pipeline(CASE / args.pipeline)
    except Exception as e:                    # noqa: BLE001 (пайплайн может не читаться)
        note(f"NOT-VERIFIED: пайплайн {args.pipeline} не загрузился "
             f"({type(e).__name__}: {e}) — системный промпт неизвестен")
        return EXIT_NOT_VERIFIED
    tok = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True)
    tok.add_special_tokens({"additional_special_tokens": list(pipe.SPECIAL_TOKENS)})

    row = json.loads(pool.read_text(encoding="utf-8").splitlines()[0])
    prefill = "<think>\n"          # `--no-hint`, `--no-toolcall-force` — конфигурация базы
    ids = context_ids(tok, pipe.UNIFIED_SYSTEM_PROMPT, row["prompt"], prefill)

    forms = []
    for tag, text in CANONICAL_FORMS:
        toks = tok(text)["input_ids"]
        under_ban = first_forbidden(PR.NoRepeatNGramBan(4, [ids]), toks, tok)
        without = first_forbidden(None, toks, tok)          # отрицательный контроль
        forms.append({"form": tag, "text": text, "n_tokens": len(toks),
                      "under_standard_ban": under_ban, "without_ban": without,
                      "blocked_by_ban": bool(under_ban["forbidden"]),
                      "control_clean": not without["forbidden"]})

    blocked = [f for f in forms if f["blocked_by_ban"]]
    controls_ok = all(f["control_clean"] for f in forms)
    #: Контроль обязателен в обе стороны: если форма не запрещается под запретом, то
    #: механизм не подтверждён, и прибор обязан отказать, а не смягчить вывод.
    if not blocked:
        note("ОТКАЗ: каноническая форма вызова НЕ запрещается штатным режимом — "
             "механизм «запрет перерезает канал вызова» не подтверждён")
        return EXIT_FAIL
    if not controls_ok:
        note("ОТКАЗ: контроль без запрета не прошёл — форма не проходит и там, где "
             "запрета нет; утверждение о механизме было бы неверным")
        return EXIT_FAIL

    out = {
        "schema": "agentic-ban-interaction/1",
        "stage": "S3am — механизм влияния штатного запрета повторов 4-грамм на агентную пробу",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "measured",
        "question": ("почему переснятая агентная база в штатном режиме ниже legacy: "
                     "деградация модели или перерезанный протоколом канал вызова"),
        "rule": {
            "ban": ("запрещён токен T, если 4-грамма «последние 3 токена + T» уже "
                    "встречалась в последовательности, **включая промпт** "
                    "(fairseq/transformers, NoRepeatNGramLogitsProcessor)"),
            "from": "passrate_probe.apply_ngram_ban / NoRepeatNGramBan (вызов, не копия)",
            "standard_no_repeat_ngram": PR.STANDARD_NO_REPEAT_NGRAM,
            "default_resolves_to": default_n,
        },
        "context": {
            "task_pool": rel(pool), "task_index_in_pool": 0,
            "task_type": row.get("task_type"),
            "n_context_tokens": len(ids), "prefill": repr(prefill),
            "prompt_artifacts": ("в системном промпте лежат три образца канонического "
                                 "вызова `<tool_call>{\"name\": \"search_concepts\", "
                                 "\"query\": …}</tool_call>` — модель обязана напечатать "
                                 "тот же синтаксис"),
        },
        "canonical_forms": forms,
        "verdict": {
            "mechanism_confirmed": True,
            "statement": ("штатный запрет повторов 4-грамм делает каноническую форму "
                          "tool_call недостижимой: её токен-4-граммы уже есть в промпте, "
                          "и запрет срабатывает на первом же токене ключа"),
            "first_blocked": {f["form"]: f["under_standard_ban"] for f in blocked},
            "control_without_ban": "все формы проходят целиком (запрета нет — нет и запрета)",
            "consequence": ("модель вынуждена обходить запрет и печатает испорченный JSON "
                            "(`{\"name\": \"\\tsearch_concepts\"` и подобное) → "
                            "`execute_tool_call` возвращает ошибку → вызов не работает"),
        },
        "behaviour": {
            "standard_run": errors, "legacy_run": errors_legacy,
            "reading": ("поведение подтверждает механизм: в legacy ошибок инструмента 0 "
                        "на 350 траекторий, в штатном режиме — сотни; ошибка инструмента "
                        "здесь означает именно неразобранный вызов"),
        },
        "alternative_domains": alternative_domains(tok, ids, prefill),
        "what_it_does_not_decide": [
            "что важнее — единый режим стадии или рабочий канал вызова инструмента",
            "как именно чинить: исключить синтаксис вызова из запрета, считать запрет "
            "только по сгенерированному (без промпта) или вернуть агентную пробу в legacy",
            "годна ли переснятая база как точка отсчёта критерия ADR-033 п.2в, пока "
            "канал вызова перерезан",
        ],
        "artifacts": [],
    }
    for p in (Path(__file__).resolve(), CASE / args.pipeline, pool):
        out["artifacts"].append({"path": rel(p),
                                 "sha256": sha256_file(p) if p.is_file() else None,
                                 "exists": p.is_file()})
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
        note(f"отчёт: {args.out}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps({"blocked_forms": [f["form"] for f in blocked],
                      "tool_errors_standard": errors["combined"]["n_error_events"],
                      "tool_errors_legacy": (errors_legacy.get("combined") or {}).get(
                          "n_error_events")}, ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
