#!/usr/bin/env python3
"""S3ae: проверка маски `<think>` на коротком тесте — ДО длинного прогона (ADR-036 п.3).

> **Статус инструмента: готов, к длинному прогону НЕ применялся.** ADR-037 отменил
> маскирование `<think>` (46.1 % сигнала, 86.1 % примеров; корень — язык входа в
> рассуждение — маска не лечит). Проверка сохранена как прибор: она же даёт
> **независимую цену маски в токенах** (снято 39.6 % целевых токенов на 1 500
> сэмплах, вызовы инструмента и ответы сохранены полностью) — второе измерение
> того же вопроса, что ADR-037 измерил в символах.

Зачем отдельный инструмент, если отчёт печатает сам патч. Отчёт патча
(`_sft_mask_report`) меряет **число целевых токенов** до и после маски, но не
проверяет, что замаскировано именно задуманное. Разница существенна: маска,
которая «снимает 25 % целей», может снимать их из ответной части, из вызовов
инструмента или из чужих ходов — по одной сводке это неотличимо. Здесь
проверяются **инварианты правила**, каждый числом:

* **(а) покрытие.** Каждая позиция строго внутри парного спана `<think>…</think>`
  обнулена — кроме позиций внутри `<tool_call>…</tool_call>` (действие, не
  рассуждение) и кроме самих делимитеров.
* **(б) доля снятых целей.** Целевых токенов после маски меньше примерно на долю
  рассуждений; число приводится, а не оценивается.
* **(в) формат цел.** Делимитеры `<think>`/`</think>` там, где были целями,
  остаются целями; ответная часть (цели вне рассуждений и вне вызовов) сохранена
  полностью; **ни одна позиция, не бывшая целью, целью не стала** (маска не
  размывает базовую редакцию меток); сэмплов без единой цели — ноль.
* **(г) что именно снято.** Доля кириллицы в снятом и в оставшемся — прямая мера
  цели ADR-036 (англоязычные рассуждения не должны закрепляться обучением).

Проверка идёт **тем же кодом, который учится**: класс `SFTDataset` и функция
маски берутся из пропатченной копии пайплайна (`--pipeline`), а не переписываются
здесь. Копия импортируется как модуль (`transformers` при импорте не нужен —
в пайплайне он импортируется внутри функций); токенайзер подключается для (г) и
для сверки id-контракта AD-3 и, если его нет, проверка не отменяется: работают
замороженные id контракта (умолчания `PINNED`).

Коды возврата::

    0 — все инварианты выполнены
    1 — инвариант нарушен (числа печатаются)
    2 — NOT-VERIFIED: нет копии пайплайна/кэша токенов, копия не импортируется
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import torch

CASE_ROOT = Path(__file__).resolve().parent.parent

#: Контракт AD-3: id токенов формата. Числа заморожены (проверяются
#: tools/check_special_tokens.py); здесь — умолчание для запуска без токенайзера.
PINNED = {
    "think": (151665, 151666),
    "tool_call": (151657, 151658),
    "tool_response": (151667, 151668),
    "role": (151644, 77091, 872),      # im_start, assistant, user
}


def load_pipeline(path: Path):
    spec = importlib.util.spec_from_file_location("laguna_pipeline_under_test", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"копия пайплайна не читается: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tokenizer_ids(model: str, special_tokens: list[str]) -> dict:
    """id контракта из живого токенайзера — если `transformers` доступен.

    Токены формата (`<think>` и прочие) в базовой Qwen2.5 **отсутствуют**:
    их добавляет пайплайн (`SPECIAL_TOKENS` → `add_special_tokens`, строка 1831).
    Без этого шага `convert_tokens_to_ids` возвращает `None`, маска сравнивает
    `int == None` и молча не срабатывает — проверка «всё обнулено» проходит на
    пустом множестве. Поэтому токены добавляются здесь тем же списком контракта,
    а `None` в id считается провалом, а не «нет данных».
    """
    try:
        from transformers import AutoTokenizer
    except Exception as e:                                     # noqa: BLE001
        return {"available": False, "why": f"{type(e).__name__}: {e}"}
    tok = AutoTokenizer.from_pretrained(model)
    tok.add_special_tokens({"additional_special_tokens": list(special_tokens)})
    names = {"think": ("<think>", "</think>"), "tool_call": ("<tool_call>", "</tool_call>"),
             "tool_response": ("<tool_response>", "</tool_response>")}
    out = {"available": True, "model": model, "ids": {}, "matches_pinned": True}
    for key, (a, b) in names.items():
        got = tuple(tok.convert_tokens_to_ids(x) for x in (a, b))
        out["ids"][key] = list(got)
        if got != PINNED[key]:
            out["matches_pinned"] = False
    role = tuple(tok.convert_tokens_to_ids(x) for x in ("<|im_start|>", "assistant", "user"))
    out["ids"]["role"] = list(role)
    if role != PINNED["role"]:
        out["matches_pinned"] = False
    #: None в любом id — отказ: сравнение `int == None` всегда ложно, и маска
    #: «проходит» проверки, ничего не замаскировав (наблюдалось на первом прогоне).
    out["null_ids"] = [k for k, v in out["ids"].items() if any(x is None for x in v)]
    if out["null_ids"]:
        out["matches_pinned"] = False
    return out


def cyr_share(tok, ids) -> tuple[float, int]:
    """Доля кириллицы среди букв в декодированном тексте + число символов."""
    if tok is None or len(ids) == 0:
        return float("nan"), 0
    text = tok.decode([int(x) for x in ids], skip_special_tokens=False)
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return float("nan"), len(text)
    cyr = sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё")
    return cyr / len(letters), len(text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pipeline", required=True, help="пропатченная копия пайплайна")
    ap.add_argument("--baseline-pipeline",
                    help="копия БЕЗ маски (артефакт «SFT без маски think»): с выключенным "
                         "переключателем метки обязаны совпасть с ней побитово")
    ap.add_argument("--sft-jsonl", default=str(CASE_ROOT / "datasets/sft_train_v12.jsonl"),
                    help="набор SFT; кэш токенов ищется конструктором датасета, как в стадии")
    ap.add_argument("--samples", type=int, default=2000)
    ap.add_argument("--out", help="куда записать отчёт (json)")
    ap.add_argument("--tokenizer", default="Qwen/Qwen2.5-0.5B")
    a = ap.parse_args()

    pipe_path = Path(a.pipeline)
    if not pipe_path.is_file():
        print(json.dumps({"verdict": "NOT-VERIFIED", "why": "нет копии пайплайна",
                          "pipeline": str(pipe_path)}, ensure_ascii=False, indent=2))
        return 2
    mod = load_pipeline(pipe_path)
    if not (hasattr(mod, "SFTDataset") and hasattr(mod, "_sft_think_mask")
            and hasattr(mod, "_sft_apply_think_mask")):
        print(json.dumps({"verdict": "NOT-VERIFIED",
                          "why": "в копии нет SFTDataset/_sft_think_mask — патч не применён"},
                         ensure_ascii=False, indent=2))
        return 2

    base_mod = None
    if a.baseline_pipeline:
        base_mod = load_pipeline(Path(a.baseline_pipeline))

    ids_info = tokenizer_ids(a.tokenizer, getattr(mod, "SPECIAL_TOKENS", []))
    tok = None
    if ids_info.get("available"):
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(a.tokenizer)

    def ids_for(key: str):
        return tuple(ids_info["ids"][key]) if ids_info.get("available") else PINNED[key]

    ds_cls = mod.SFTDataset
    ds_cls.ROLE_IDS = ids_for("role")
    ds_cls.TOOL_RESP_IDS = ids_for("tool_response")
    ds_cls.THINK_IDS = ids_for("think")
    ds_cls.TOOL_CALL_IDS = ids_for("tool_call")

    try:      # конструктор тот же, что у стадии: имя кэша он ищет сам
        ds = ds_cls(a.sft_jsonl, 8192, a.samples, tok_tag="qwen25")
    except FileNotFoundError as e:
        print(json.dumps({"verdict": "NOT-VERIFIED", "why": f"кэш токенов не найден: {e}"},
                         ensure_ascii=False, indent=2))
        return 2

    th_o, th_c = ds_cls.THINK_IDS
    tc_o, tc_c = ds_cls.TOOL_CALL_IDS
    im_start = ds_cls.ROLE_IDS[0]

    stat = {"samples": int(len(ds)), "targets_before": 0, "targets_after": 0,
            "reason_content_positions": 0, "reason_content_unmasked": 0,
            "tool_call_positions": 0, "tool_call_unmasked": 0,
            "delims_targets_before": 0, "delims_unmasked": 0,
            "answer_before": 0, "answer_after": 0, "empty_before": 0, "empty_after": 0,
            "leaked_positions": 0, "unclosed_region_positions": 0,
            "unclosed_region_unmasked": 0, "base_mismatch": 0,
            "must_mask_positions": 0, "samples_compared_to_baseline": 0}
    removed_ids, kept_ids = [], []

    for r in range(len(ds)):
        ids = ds.input_ids[r].astype(np.int64)
        mask = ds.masks[r].astype(np.int64)
        t_ids, t_mask = torch.from_numpy(ids), torch.from_numpy(mask)

        mod.SFT_THINK_MASK = False                       # переключатель выключен
        lab_before = ds[r]["labels"].numpy().copy()
        mod.SFT_THINK_MASK = True                        # пропатченная редакция
        lab_after = ds[r]["labels"].numpy().copy()
        # Выключенный переключатель обязан давать ровно то, что даёт копия без
        # маски (немаскирующий артефакт): иначе патч тронул бы и базовую редакцию.
        if base_mod is not None:
            b_ds = base_mod.SFTDataset.__new__(base_mod.SFTDataset)
            b_ds.ROLE_IDS, b_ds.TOOL_RESP_IDS = ds_cls.ROLE_IDS, ds_cls.TOOL_RESP_IDS
            b_ds.input_ids, b_ds.masks = ds.input_ids[r:r + 1], ds.masks[r:r + 1]
            lab_ref = base_mod.SFTDataset.__getitem__(b_ds, 0)["labels"].numpy()
            stat["base_mismatch"] += int(not np.array_equal(lab_ref, lab_before))
            stat["samples_compared_to_baseline"] += 1

        tgt0 = lab_before != -100
        n = len(ids)
        tc = np.zeros(n, bool)
        in_tc = False
        for i in range(n):
            if ids[i] == tc_o:
                in_tc = True
            if in_tc:
                tc[i] = True
            if ids[i] == tc_c:
                in_tc = False
        reason = np.zeros(n, bool)
        opens = []
        for i in range(n):
            if ids[i] == th_o:
                opens.append(i)
            elif ids[i] == th_c and opens:
                reason[opens.pop() + 1:i] = True
        unclosed = np.zeros(n, bool)
        for o in opens:
            if not tgt0[o]:
                continue
            j = o + 1
            while j < n and ids[j] != im_start:
                unclosed[j] = True
                j += 1
        unclosed &= ~tc
        answer = tgt0 & ~reason & ~tc & ~unclosed
        delims = (ids == th_o) | (ids == th_c)
        kept = lab_after != -100

        stat["targets_before"] += int(tgt0.sum())
        stat["targets_after"] += int(kept.sum())
        must_mask = (reason | unclosed) & tgt0 & ~tc & ~delims
        stat["must_mask_positions"] += int(must_mask.sum())
        stat["reason_content_positions"] += int((reason & tgt0).sum())
        stat["reason_content_unmasked"] += int((must_mask & kept).sum())
        stat["tool_call_positions"] += int((tc & tgt0).sum())
        stat["tool_call_unmasked"] += int((tc & tgt0 & kept).sum())
        stat["delims_targets_before"] += int((delims & tgt0).sum())
        stat["delims_unmasked"] += int((delims & tgt0 & kept).sum())
        stat["answer_before"] += int(answer.sum())
        stat["answer_after"] += int((answer & kept).sum())
        stat["empty_before"] += int(tgt0.sum() == 0)
        stat["empty_after"] += int(kept.sum() == 0)
        stat["leaked_positions"] += int((~tgt0 & kept).sum())
        stat["unclosed_region_positions"] += int((unclosed & tgt0).sum())
        stat["unclosed_region_unmasked"] += int((unclosed & tgt0 & kept).sum())
        if tok is not None:
            removed_ids.append(ids[tgt0 & ~kept])
            kept_ids.append(ids[kept])

    checks = [
        ("а: рассуждения (<think>-содержимое и незакрытые регионы) обнулены — "
         "кроме вызовов инструмента и делимитеров", stat["reason_content_unmasked"] == 0),
        ("а2: выключенный переключатель даёт ровно немаскирующую копию",
         stat["base_mismatch"] == 0),
        ("а3: id токенайзера получены (не None) — маска сравнивает числа",
         not ids_info.get("null_ids") if ids_info.get("available") else True),
        ("а4: маска действительно сработала (что-то обнулено)",
         stat["must_mask_positions"] > 0),
        ("б: целевых токенов стало меньше", stat["targets_after"] < stat["targets_before"]),
        ("в1: делимитеры <think>/</think> остались целями",
         stat["delims_unmasked"] == stat["delims_targets_before"]),
        ("в2: ответная часть сохранена полностью", stat["answer_after"] == stat["answer_before"]),
        ("в3: новых целей не появилось (нет утечки)", stat["leaked_positions"] == 0),
        ("в4: сэмплов без целей не осталось", stat["empty_after"] == 0),
        ("г: вызовы инструмента сохранены",
         stat["tool_call_unmasked"] == stat["tool_call_positions"]),
    ]
    rep = {
        "tool": "tools/verify_think_mask.py",
        "pipeline": str(pipe_path),
        "pipeline_sha256": hashlib.sha256(pipe_path.read_bytes()).hexdigest(),
        "sft_jsonl": a.sft_jsonl,
        "tokenizer_ids": ids_info,
        "stats": stat,
        "masked_share": (1 - stat["targets_after"] / stat["targets_before"])
        if stat["targets_before"] else None,
        "checks": [{"name": n, "ok": bool(ok)} for n, ok in checks],
        "verdict": "PASS" if all(ok for _, ok in checks) else "FAIL",
    }
    if tok is not None and removed_ids:
        rem, kp = np.concatenate(removed_ids), np.concatenate(kept_ids)
        c_rem, ch_rem = cyr_share(tok, rem)
        c_keep, ch_keep = cyr_share(tok, kp)
        rep["language"] = {
            "cyrillic_share_of_masked_targets": round(c_rem, 4),
            "cyrillic_share_of_kept_targets": round(c_keep, 4),
            "masked_tokens": int(len(rem)), "kept_tokens": int(len(kp)),
            "masked_chars": ch_rem, "kept_chars": ch_keep,
            "note": "чем ниже доля кириллицы в снятом, тем точнее маска бьёт по "
                    "англоязычному рассуждению (ADR-036)",
        }
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if a.out:
        Path(a.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
