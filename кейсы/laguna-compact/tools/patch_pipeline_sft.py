#!/usr/bin/env python3
"""SFT-стадия: сборка копии пайплайна с патчами стадии.

Зачем патч вообще. Контур исполняет стадию **копией** пайплайна, лежащей в каталоге
прогона (так шёл CPT: `laguna_pipeline_calib.py`, sha256 `1571cbd1…`). Копия нужна
затем, чтобы стадия имела тот код, который записан в манифесте (AD-2), и чтобы
правка стадии не меняла живой контур (ADR-016 п.1). Здесь собирается копия для SFT.

Три патча — и ни одного «на глаз»:

1. **Монитор форгеттинга → K1/K2/домен (ADR-033 п.3, дыра G2).** Штатный
   `_general_eval_step` меряет `general_eval.txt` (24 документа, снят с решающей роли
   ADR-027 п.3) и `domain_eval.txt` (5 документов, историческая шкала ADR-031 п.4).
   Стадия, контролируемая прибором, который признан непригодным, — контроль
   бутафорией. Патч переводит монитор на **действующие** наборы: K1
   `general_eval_v3.txt`, K2 `general_eval_k2.txt`, домен `domain_eval_v2.txt`.
   Исторические наборы остаются в логе **справочно** и в решение не входят.

2. **Траектория по шагам (`loss_trace.jsonl`) в SFT.** Нужна дважды: ADR-022 п.4
   запрещает выносить вердикт по сводке окон (сводка скрыла разворот в S2), а
   ADR-023 п.11 требует подтверждать старт долгоживущей стадии **фактами** —
   растущий `loss_trace.jsonl` и есть такой факт. В штатном пайплайне траектория
   пишется только в CPT-блоке (патч S3m), в SFT её нет.

3. **Точки замера PPL каждые 500 шагов.** Штатный SFT сохраняет
   `sft_checkpoint_{step}.pt` каждые 200 шагов с `keep_last=2` — то есть на диске
   живут только два последних номерных чекпойнта. Сторож на 4080 (ADR-030 п.7)
   мерит точки на 500, 1000, … : под штатным именем точка шага 500 исчезла бы на
   шаге 1000 — ровно этот дефект зафиксирован в S3m (рука 25-0.7, 16.09). Поэтому
   точки пишутся ОТДЕЛЬНЫМ именем `sft_probe_{step}.pt` и вне ретенции
   штатного префикса. Ретенция точек — `keep_last=PROBE_KEEP_LAST` (окно, за
   которое сторож гарантированно успевает), а не «все»: ретенция в 3.7 ТБ уже
   один раз кончилась ENOSPC и остановила лесенку на 12 ч (комментарий
   `save_checkpoint_atomic`, 04.09.2026). Числа точек живут в отчётах пробы на
   общем диске и от ретенции весов не зависят.

4. **Маскирование `<think>…</think>` в лоссе (S3ae, ADR-036, флаг `--think-mask`).**
   > **НЕ ПРИМЕНЯТЬ БЕЗ НОВОГО РЕШЕНИЯ. ADR-037 (17.09.2026) отменил маскирование:**
   > замер по набору показал, что рассуждение это **46.1 % символов ответов и 86.1 %
   > примеров**, а корень сдвига — язык входа в рассуждение — маска не лечит. Патч
   > сохранён как готовый инструмент: флаг по умолчанию **выключен**, и сборка без
   > `--think-mask` даёт байт-в-байт ту же копию, что шла в S3aa (sha256 `0f37e034…`).
   >
   > **Оговорка S3be.** Байт-в-байт в редакции, существовавшей до S3be: патч 6
   > (идентичность набора курикулума RL) добавил в копию четыре строки, и
   > `patched_sha256` сдвинулся у **любой** сборки, включая сборку без `--think-mask`.
   > Это не смена смысла маски: стадия SFT, идущая сейчас, работает копией, уже
   > снятой в свой каталог и запинненной манифестом (`pipeline_sha256`), — правка
   > инструмента её не касается. Но повторный старт SFT-стадии соберёт копию с новым
   > хешем, и это ожидаемо: cp-идентичность держится внутри редакции патча, а не
   > сквозь неё.
   > Числа ниже — замер цены маски, а не указание её применять.
   Набор SFT несёт англоязычный режим рассуждений (кириллица `<think>` 0.2178 при
   0.0479 в CPT), и рассуждение **входит в целевые токены** — обучение закрепляло
   бы сдвиг. Правило маски собрано по замеру данных (числа — в
   `evidence/s3ae-sft-remask.json`), а не по интуиции; четыре свойства, каждое
   проверено числом:

   * **Содержимое парных спанов** `<think>…</think>` маскируется — это и есть
     предмет решения ADR-036.
   * **Спаны `<tool_call>…</tool_call>` не маскируются никогда.** В данных
     траекторий вызов инструмента почти всегда лежит **внутри** рассуждения
     (шаблон `<think>` → `<tool_call>` → `<tool_response>`). Наивная маска
     «от `<think>` до `</think>`» съедала бы **70 % целевых токенов вызова
     инструмента** — то есть била бы по компоненте (в1) критерия ADR-033
     (agentic-доля). Действия — не рассуждение.
   * **Незакрытый `<think>`, который сам является целевым токеном**, открывает
     регион до конца хода (до `<|im_start|>`/`<|im_end|>`). Причина: в наборе
     **3 953 незакрытых открывающих тега на 3 000 сэмплов** — обрезанные
     траектории (учитель не дописал `</think>`) и буквальные упоминания тега в
     тексте промпта/ответа инструмента. Незакрытые регионы несут **21 % целевых
     токенов**, и это ровно самые длинные англоязычные рассуждения набора
     (до 3 364 токенов: «The user wants me to explain the connection…»).
     Ограничение «открывающий тег сам целевой» отсекает ложные упоминания из
     системного промпта и ответов инструмента — иначе регион сносил бы ответ.
   * **Делимитеры `<think>`/`</think>` остаются целевыми токенами.** Два довода,
     оба проверяемые: (1) формат — единственное, что CPT дал правильно, и в этом
     же файле зафиксирован дефект v7/v8, когда первый контентный токен
     (`<think>`) выпал из целей и модель научилась генерировать `</think>` без
     `<think>` (пустой ризонинг); (2) структурная защита от пустого сэмпла —
     сэмпл, у которого все целевые позиции обнулились, даёт `NaN` в
     `cross_entropy(ignore_index=-100)` (деление на ноль активных позиций).

   Сверх этого в набор правок входит **страховка от пустой цели**: если после
   маски у сэмпла не осталось ни одной целевой позиции, маска откатывается к
   базовой (сэмпл сохраняет прежнюю редакцию меток), а счётчик таких слyчаев
   печатается в отчёте маски. Молчаливый `NaN` на 68-часовом прогоне — это
   потерянный прогон, поэтому страховка обязательна, а её срабатывание видно.

5. **Идентичность набора (S3av, AD-2/ADR-028 п.1).** Штатный пайплайн выводит имя
   val-тензора **литералом** (`f"sft_train_v12_8192_{tag}.npz"`), то есть held-out
   хвост считался по v12 при любом объявленном `--sft_data`; манифест писал хеши
   наборов по первым 1 МиБ (`_sha256_head`). Замер: стадия училась на v12
   (44 949 сэмплов), а манифест и гиперпараметры называли v13_fixed (44 105).
   Патч делает три вещи: загрузчик называет фактически открытый тензор
   атрибутами (`npz_path`, `source_path`, `samples_total`), val берёт тензор
   **оттуда же** (второго вывода имени больше нет), а стадия предъявляет факт —
   блок `sft_input` в манифесте стадии: пути jsonl и тензора, **полные** sha256
   обоих, число примеров; полные хеши заменяют `_sha256_head` и в поле
   `datasets` (`datasets_hash_scope: full` называет редакцию явно). Новый
   параметр `--sft_data_sha256` — объявленный хеш: расхождение с прочитанным
   файлом останавливает стадию **до первого шага**, а не отчётом через 60 часов.
   Сверяет объявленное с фактическим страж `tools/check_dataset_identity.py`
   (правило C-030).

Патч механический: каждый анкер обязан найтись **ровно один раз**, иначе сборка
падает — «похоже, нашлось» здесь не допускается.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

#: Ретенция чекпойнтов-точек. 24 × 500 шагов = 12 000 шагов ≈ 10 ч хода стадии:
#: сторож с опросом раз в 120 с успевает с большим запасом. 24 × 2.96 ГБ ≈ 71 ГБ.
DEFAULT_PROBE_KEEP_LAST = 24

#: Компоненты меры и их замороженные базы. Те же числа, что принимают решение
#: стадии (ADR-027 — язык, ADR-031 п.4 — домен), а не «состояние на первом шаге»:
#: ADR-022 п.5 признал дефектом снятие базы «первым успешным вызовом».
MONITOR_SETS = [
    ("K1", "general_eval_v3.txt", 7.50468637420902, "ceiling"),
    ("K2", "general_eval_k2.txt", 6.1599356842437585, "ceiling"),
    ("DOMAIN", "domain_eval_v2.txt", 11.134115855539092, "learned"),
]
MONITOR_REFERENCE = [
    ("v1_general", "general_eval.txt"),
    ("domain_legacy", "domain_eval.txt"),
]
CEILING_SCALE = 2.0

CTR_DATASETS = "/workspace/shared/datasets"


# ─────────────────────────── тексты патчей ───────────────────────────

OLD_MONITOR = '''def _general_eval_step(model, tokenizer, step, ckpt_dir):
    """PPL на общем + доменном корпусе; лог + jsonl-история. Первая точка = baseline."""
    try:
        ppl_g = _ppl_eval(model, tokenizer, "/workspace/shared/datasets/general_eval.txt")
        ppl_d = _ppl_eval(model, tokenizer, "/workspace/shared/datasets/domain_eval.txt")
        if _GE["baseline_g"] is None:
            _GE["baseline_g"], _GE["baseline_d"] = ppl_g, ppl_d
        dg = (ppl_g - _GE["baseline_g"]) / max(_GE["baseline_g"], 1e-9) * 100
        dd = (ppl_d - _GE["baseline_d"]) / max(_GE["baseline_d"], 1e-9) * 100
        warn = " ⚠ FORGETTING?" if dg > 15 else ""
        log.info(f"GEN-EVAL step {step}: ppl_general={ppl_g:.1f} ({dg:+.1f}% vs base), "
                 f"ppl_domain={ppl_d:.1f} ({dd:+.1f}%){warn}")
        hist = Path(ckpt_dir).parent / "general_eval_history.jsonl"
        with open(hist, "a") as f:
            f.write(json.dumps({"step": step, "ppl_general": ppl_g, "ppl_domain": ppl_d,
                                "delta_general_pct": dg, "delta_domain_pct": dd}) + "\\n")
    except Exception as e:
        log.warning(f"GEN-EVAL упал ({type(e).__name__}: {e}) — пропуск")'''


def new_monitor() -> str:
    # Наборы и базы приходят из HELPERS (`_SFT_MONITOR_SETS`): функция мерит то,
    # что объявлено один раз, — иначе список разъехался бы с паспортом патча.
    return '''def _general_eval_step(model, tokenizer, step, ckpt_dir):
    """Монитор форгеттинга SFT на ДЕЙСТВУЮЩИХ наборах (ADR-033 п.3).

    Штатная редакция мерила `general_eval.txt` (24 док., снят ADR-027 п.3) и
    `domain_eval.txt` (5 док., историческая шкала ADR-031 п.4). Решение стадии
    принимается по K1/K2 (ADR-027) и `domain_eval_v2` (ADR-031 п.4) — по ним и
    мерим. Исторические наборы считаются ТОЛЬКО справочно: они попадают в лог и
    историю, но вердикт по ним не выносится.

    Базы — замороженные числа ревизии, не «первая точка»: ADR-022 п.5 признал
    снятие базы первым успешным вызовом дефектом.
    """
    try:
        row = {"step": int(step)}
        verdicts = []
        for _name, _path, _base, _kind in _SFT_MONITOR_SETS:
            _ppl = _ppl_eval(model, tokenizer, _path)
            _ratio = _ppl / _base if _base else float("nan")
            row[_name] = _ppl
            row[_name + "_ratio"] = _ratio
            if _kind == "ceiling":
                _ok = _ratio <= _SFT_CEILING_SCALE
                verdicts.append(f"{_name} ×{_ratio:.4f}" + ("" if _ok else " ✗ПОТОЛОК"))
            else:
                _ok = _ratio < 1.0
                verdicts.append(f"{_name} ×{_ratio:.4f}" + ("" if _ok else " ✗не выучен"))
        row["verdict"] = " ; ".join(verdicts)
        for _name, _path in _SFT_MONITOR_REFERENCE:
            try:
                row["ref_" + _name] = _ppl_eval(model, tokenizer, _path)
            except Exception:
                pass
        log.info(f"GEN-EVAL step {step} [ADR-033 п.3: K1/K2/домен] {row['verdict']} "
                 f"| справочно v1_general={row.get('ref_v1_general', float('nan')):.1f} "
                 f"domain_legacy={row.get('ref_domain_legacy', float('nan')):.1f}")
        hist = Path(ckpt_dir).parent / "general_eval_history.jsonl"
        with open(hist, "a") as f:
            f.write(json.dumps(row) + "\\n")
    except Exception as e:
        log.warning(f"GEN-EVAL упал ({type(e).__name__}: {e}) — пропуск")'''


OLD_TRACE_ANCHOR = (
    "            ema = loss.item() if ema is None else 0.98 * ema + 0.02 * loss.item()\n"
)

NEW_TRACE_BLOCK = (
    "            ema = loss.item() if ema is None else 0.98 * ema + 0.02 * loss.item()\n"
    "            # SFT-стадия: траектория по шагам (ADR-022 п.4 — вердикт по траектории,\n"
    "            # а не по сводке окон; ADR-023 п.11 — живой факт старта стадии).\n"
    "            _sft_trace(step, loss.item(), scheduler.get_last_lr()[0], args)\n"
)

OLD_CKPT_ANCHOR = (
    "            if step % 200 == 0 and step > 0:\n"
    '                save_checkpoint_atomic(model, optimizer, Path(args.ckpt_dir)/f"sft_checkpoint_{step}.pt")\n'
)

NEW_CKPT_BLOCK = (
    "            if step % 200 == 0 and step > 0:\n"
    '                save_checkpoint_atomic(model, optimizer, Path(args.ckpt_dir)/f"sft_checkpoint_{step}.pt")\n'
    "            # Точки замера PPL для сторожа на 4080 (ADR-030 п.7). Отдельное имя и\n"
    "            # keep_last=PROBE_KEEP_LAST: штатная ретенция глобит `sft_checkpoint_[0-9]*.pt`\n"
    "            # и держит два последних по номеру — под штатным именем точка 500 исчезла бы\n"
    "            # на 1000 (дефект S3m, рука 25-0.7).\n"
    "            _probe_every = int(os.environ.get(\"SFT_PROBE_CKPT_EVERY\", \"500\") or 0)\n"
    "            if _probe_every > 0 and step > 0 and step % _probe_every == 0:\n"
    "                save_checkpoint_atomic(model, optimizer,\n"
    '                                       Path(args.ckpt_dir)/f"sft_probe_{step}.pt",\n'
    "                                       keep_last=PROBE_KEEP_LAST)\n"
)

# ────────── патч 5: идентичность набора (S3av, AD-2/ADR-028 п.1) ──────────
#
# Дефект, который чинится: общий пайплайн выводит имя тензора val **литералом**
# (`f"sft_train_v12_8192_{tag}.npz"`), а `--sft_data` у стадии — параметр. При
# объявленном v13_fixed обучение и held-out хвост считались из v12; манифест при
# этом описывал v13. Три правки: загрузчик называет фактически открытый тензор
# атрибутом, val берёт его ОТТУДА ЖЕ (не вторым литералом), а стадия предъявляет
# факт манифестом и отказывает, если объявленный хеш не совпал с прочитанным.

OLD_SFT_INIT = (
    "        stem = Path(filepath).stem\n"
    "        cands = ([Path(filepath).parent / \"tok\" / f\"{stem}_{max_len}_{tok_tag}.npz\"] if tok_tag else []) + [\n"
    "                 Path(filepath).parent / \"tok\" / f\"{stem}_{max_len}.npz\",\n"
    "                 Path(filepath).parent / \"tok\" / f\"sft_{max_len}.npz\"]\n"
    "        for npz_path in cands:\n"
    "            if npz_path.exists():\n"
    "                data = np.load(str(npz_path))\n"
    "                self.input_ids = data['input_ids'][:max_samples]\n"
    "                self.masks = data['attention_mask'][:max_samples]\n"
    "                log.info(f\"SFT: {len(self.input_ids)} samples (pre-tok {npz_path.name})\")\n"
    "                return\n"
    "        raise FileNotFoundError(f\"SFT pre-tok not found: {cands}\")\n"
)

NEW_SFT_INIT = (
    "        stem = Path(filepath).stem\n"
    "        cands = ([Path(filepath).parent / \"tok\" / f\"{stem}_{max_len}_{tok_tag}.npz\"] if tok_tag else []) + [\n"
    "                 Path(filepath).parent / \"tok\" / f\"{stem}_{max_len}.npz\",\n"
    "                 Path(filepath).parent / \"tok\" / f\"sft_{max_len}.npz\"]\n"
    "        found = None\n"
    "        for npz_path in cands:\n"
    "            if npz_path.exists():\n"
    "                found = npz_path\n"
    "                break\n"
    "        # S3av: что открыто — атрибутами, а не строкой лога. Второй потребитель\n"
    "        # тензора (val-блок) берёт путь ОТСЮДА: два независимых вывода имени\n"
    "        # расходятся молча, и val уезжает на другой набор, чем обучение.\n"
    "        self.source_path = Path(filepath)\n"
    "        self.npz_path = found\n"
    "        self.npz_candidates = [str(p) for p in cands]\n"
    "        if found is None:\n"
    "            raise FileNotFoundError(f\"SFT pre-tok not found: {cands}\")\n"
    "        data = np.load(str(found))\n"
    "        self.samples_total = int(data['input_ids'].shape[0])\n"
    "        self.input_ids = data['input_ids'][:max_samples]\n"
    "        self.masks = data['attention_mask'][:max_samples]\n"
    "        log.info(f\"SFT: {len(self.input_ids)} samples (pre-tok {found.name})\")\n"
)

OLD_SFT_VAL_NPZ = (
    '        _npz = Path(args.sft_data).parent / "tok" / f"sft_train_v12_8192_{tok_tag_for(args.model_name)}.npz"\n'
)

NEW_SFT_VAL_NPZ = (
    "        # Тот же тензор, что читала тренировка (S3av): имя берётся у загрузчика, а\n"
    "        # не литералом `sft_train_v12_*` — при объявленном v13 хвост считался по v12.\n"
    "        _npz = getattr(dataset, \"npz_path\", None)\n"
    "        if _npz is None:\n"
    "            raise FileNotFoundError(\"тензор набора не разрешён загрузчиком\")\n"
)

OLD_SFT_ARGPARSE = (
    '    p.add_argument("--sft_data", default="/workspace/shared/datasets/sft_train.jsonl")\n'
)

NEW_SFT_ARGPARSE = (
    '    p.add_argument("--sft_data", default="/workspace/shared/datasets/sft_train.jsonl")\n'
    "    # S3av (AD-2, ADR-028 п.1): объявленный полный sha256 набора SFT. Пусто —\n"
    "    # проверки нет (старые вызовы не меняются); задан — расхождение с фактически\n"
    "    # прочитанным файлом останавливает стадию ДО первого шага.\n"
    '    p.add_argument("--sft_data_sha256", default=None)\n'
)

OLD_SFT_IDENTITY_CALL = "    peak_lr = 1e-5\n"

NEW_SFT_IDENTITY_CALL = (
    "    # Факт входа стадии — до первого шага (S3av): объявленное против прочитанного.\n"
    "    _sft_record_input_identity(args, dataset)\n"
    "    peak_lr = 1e-5\n"
)

# ─────────── патч 6: идентичность набора курикулума RL (S3be, ADR-054) ───────
#
# Зачем. ADR-054 п.1 выбрал набором стадии `rl_tasks_revpool_v2.jsonl`, а
# `RLDataset` по умолчанию читает `rl_tasks_v3.jsonl`. Пока вход стадии не назван
# параметром и не предъявлен манифестом, выбор набора остаётся **бумажным** — и
# это ровно тот класс, что S3av разобрал для SFT («объявлен v13 — прочитан v12»).
# Патч делает три вещи: загрузчик называет фактически открытый файл атрибутом
# (`source_path`), стадия предъявляет факт (блок `rl_input` — путь jsonl, ПОЛНЫЙ
# sha256 и число задач) и получает параметр `--rl_data_sha256`: расхождение с
# прочитанным останавливает стадию до первого шага.
#
# Почему носитель именно такой. ADR-054 п.2 требует применять класс стража C-030
# «тем же принципом», а задача дельты — «смотри, как это сделано для SFT
# (`sft_input`), и не изобретай параллельный механизм». Отсюда блок `rl_input` —
# та же форма, что `sft_input`, без тензора: RL-загрузчик читает jsonl напрямую,
# и обязательный тензор был бы красным по построению (ADR-023 п.12).
OLD_RL_INIT_ANCHOR = (
    "        self.pure = pure  # LAGUNA_PURE: сэмплирование строго ∝(1−pr), рецепт Лагуны\n"
)
NEW_RL_INIT_ANCHOR = (
    "        self.pure = pure  # LAGUNA_PURE: сэмплирование строго ∝(1−pr), рецепт Лагуны\n"
    "        # S3be (ADR-054 п.2): файл, который загрузчик ОТКРЫЛ, — а не тот, что\n"
    "        # объявлен параметром. Без него факт входа доказывался бы объявлением.\n"
    "        self.source_path = str(filepath)\n"
)

OLD_RL_ARGPARSE = (
    '    p.add_argument("--rl_data", default="/workspace/shared/datasets/rl_tasks_v3.jsonl")\n'
)

NEW_RL_ARGPARSE = (
    '    p.add_argument("--rl_data", default="/workspace/shared/datasets/rl_tasks_v3.jsonl")\n'
    "    # S3be (ADR-054 п.2): объявленный полный sha256 набора курикулума. Пусто —\n"
    "    # проверки нет (старые вызовы не меняются); задан — расхождение с фактически\n"
    "    # прочитанным файлом останавливает стадию ДО первого шага.\n"
    '    p.add_argument("--rl_data_sha256", default=None)\n'
)

OLD_RL_IDENTITY_CALL = (
    '    dataset = RLDataset(args.rl_data, max_samples=args.max_samples,\n'
    '                        persist_path=Path(args.ckpt_dir)/"pass_rates.json", pure=pure)\n'
)

NEW_RL_IDENTITY_CALL = (
    '    dataset = RLDataset(args.rl_data, max_samples=args.max_samples,\n'
    '                        persist_path=Path(args.ckpt_dir)/"pass_rates.json", pure=pure)\n'
    "    # Факт входа стадии — до первого шага (S3be): объявленное против прочитанного.\n"
    "    _rl_record_input_identity(args, dataset)\n"
)

# ─────────── патч 5: темп SFT — множитель пика (S3ax, ADR-052) ───────────
#
# Зачем патч, а не флаг расписания. Пик SFT задан в коде `run_sft` числом
# (`peak_lr = 1e-5`), и это «одно место правды»: смена темпа посреди живой стадии
# запрещена (ADR-016 п.1), поэтому темп руки задаётся **отдельной копией
# пайплайна** — ровно так же, как задаётся маска лосса. Множитель меняет ОДНО
# число: `peak_lr = 1e-5 * scale`. Форма расписания остаётся штатной —
# `CosineAnnealingLR(T_max=args.max_steps, eta_min=2e-7)`, warmup
# `min(100, max_steps // 10)` — потому что «пониженный темп» обязан отличаться от
# контроля **масштабом**, а не формой (иначе одна рука меняла бы две вещи).
#
# Множитель печатается в лог стадии: число темпа обязано быть видно в логе
# прогона, а не выводиться читателем из хеша копии.
#
# Умолчание 1.0 = штатное поведение байт-в-байт (`1e-5 * 1.0` — то же значение,
# но другая строка), поэтому при умолчании блок НЕ вставляется вовсе: копия
# обязана остаться побайтово той же, что шла в прежние прогоны (иначе поехал бы
# `patched_sha256` у всех, кто патч не просил).
OLD_SFT_PEAK_LR = "    peak_lr = 1e-5\n"


def new_sft_peak_lr(scale: float) -> str:
    """Блок темпа: пик = 1e-5 × scale, форма расписания не тронута."""
    return (
        "    # S3ax (ADR-052): темп стадии. Меняется ОДНО число — пик; форма\n"
        "    # расписания ниже остаётся штатной (косинус до 2e-7, T_max=max_steps),\n"
        "    # warmup не тронут. Поэтому различие с контролем — масштаб, не форма.\n"
        "    _SFT_PEAK_LR_SCALE = " + repr(float(scale)) + "\n"
        "    peak_lr = 1e-5 * _SFT_PEAK_LR_SCALE\n"
        "    log.info(f\"SFT LR: пик {peak_lr:.4e} = 1e-5 x {_SFT_PEAK_LR_SCALE:g}; \"\n"
        "             f\"финал 2e-7, T_max={args.max_steps}, \"\n"
        "             f\"warmup={min(100, args.max_steps // 10)}\")\n"
    )


OLD_DATASETS_HEAD = (
    '        "datasets": {d: _sha256_head(d) for d in\n'
    '                     (args.cpt_data, args.sft_data, args.rl_data, args.eval_data)},\n'
)

NEW_DATASETS_FULL = (
    "        # S3av: полный хеш вместо первых 1 МиБ (`_sha256_head`). Усечённый хеш\n"
    "        # не отличает подменённый набор от объявленного, если различие лежит за\n"
    "        # первым мегабайтом, — тот же класс слепоты, что AD-2/G9 и хеш карточки\n"
    "        # набора (C-024). `datasets_hash_scope` называет редакцию явно: «64 hex»\n"
    "        # само по себе не говорит, весь файл хеширован или его начало.\n"
    '        "datasets": {d: _sha256_full_digest(d) for d in\n'
    '                     (args.cpt_data, args.sft_data, args.rl_data, args.eval_data)},\n'
    '        "datasets_hash_scope": "full",\n'
)

# ─────────────────── патч 4: маска <think> (S3ae) ───────────────────

OLD_CLASS_ATTRS = "    ROLE_IDS = None\n"

NEW_CLASS_ATTRS = (
    "    ROLE_IDS = None\n"
    "    # S3ae (ADR-036): (id <think>, id </think>) и (id <tool_call>, id </tool_call>).\n"
    "    # Ставятся из run_sft; при None маска рассуждений не применяется вовсе.\n"
    "    THINK_IDS = None\n"
    "    TOOL_CALL_IDS = None\n"
)

OLD_GETITEM_TAIL = (
    "        if self.TOOL_RESP_IDS:\n"
    "            tr_open, tr_close = self.TOOL_RESP_IDS\n"
    "            in_tr = False\n"
    "            for i in range(len(ids)):\n"
    "                t = ids[i].item()\n"
    "                if t == tr_open: in_tr = True; labels[i] = -100\n"
    "                elif t == tr_close: in_tr = False; labels[i] = -100\n"
    "                elif in_tr: labels[i] = -100\n"
    "        return {\"input_ids\": ids, \"labels\": labels}\n"
)

NEW_GETITEM_TAIL = (
    "        if self.TOOL_RESP_IDS:\n"
    "            tr_open, tr_close = self.TOOL_RESP_IDS\n"
    "            in_tr = False\n"
    "            for i in range(len(ids)):\n"
    "                t = ids[i].item()\n"
    "                if t == tr_open: in_tr = True; labels[i] = -100\n"
    "                elif t == tr_close: in_tr = False; labels[i] = -100\n"
    "                elif in_tr: labels[i] = -100\n"
    "        # S3ae (ADR-036): рассуждения <think>…</think> не награждаем. Правило и его\n"
    "        # обоснование числами — в паспорте патча (pipeline_patch.json) и в шапке\n"
    "        # tools/patch_pipeline_sft.py; здесь только вызов.\n"
    "        if self.THINK_IDS and SFT_THINK_MASK:\n"
    "            labels = _sft_apply_think_mask(labels, ids, self.THINK_IDS,\n"
    "                                           self.TOOL_CALL_IDS,\n"
    "                                           self.ROLE_IDS[0] if self.ROLE_IDS else None)\n"
    "        return {\"input_ids\": ids, \"labels\": labels}\n"
)

OLD_RUN_SFT_IDS = (
    '    SFTDataset.ROLE_IDS = (tokenizer.convert_tokens_to_ids("<|im_start|>"),\n'
    '                           tokenizer.convert_tokens_to_ids("assistant"),\n'
    '                           tokenizer.convert_tokens_to_ids("user"))  # v9.1\n'
)

NEW_RUN_SFT_IDS = (
    '    SFTDataset.ROLE_IDS = (tokenizer.convert_tokens_to_ids("<|im_start|>"),\n'
    '                           tokenizer.convert_tokens_to_ids("assistant"),\n'
    '                           tokenizer.convert_tokens_to_ids("user"))  # v9.1\n'
    '    # S3ae (ADR-036): маска <think> в лоссе; отчёт о маске — сразу после сборки датасета.\n'
    '    SFTDataset.THINK_IDS = (tokenizer.convert_tokens_to_ids("<think>"),\n'
    '                            tokenizer.convert_tokens_to_ids("</think>"))\n'
    '    SFTDataset.TOOL_CALL_IDS = (tokenizer.convert_tokens_to_ids("<tool_call>"),\n'
    '                                tokenizer.convert_tokens_to_ids("</tool_call>"))\n'
)

#: Отчёт о маске встаёт там, где датасет уже собран (иначе мерить нечего) и до
#: первого шага обучения — чтобы числа «до/после» были в логе старта стадии.
OLD_LOADER = (
    "    loader = DataLoader(dataset, batch_size=args.batch_size, shuffle=True,"
    " num_workers=0, pin_memory=True)\n"
)

NEW_LOADER = (
    "    loader = DataLoader(dataset, batch_size=args.batch_size, shuffle=True,"
    " num_workers=0, pin_memory=True)\n"
    "    # S3ae (ADR-036 п.3): отчёт о маске на старте стадии — цели «до» и «после»,\n"
    "    # снятые тем же кодом, который учится (не отдельной проверкой).\n"
    "    _sft_mask_report(dataset, \"start\", args.log_dir)\n"
)

OLD_VAL_LABELS = "                            vl = vi.clone(); vl[vm == 0] = -100\n"

NEW_VAL_LABELS = (
    "                            vl = vi.clone(); vl[vm == 0] = -100\n"
    "                            # S3ae: тот же прибор на валидации — иначе val_loss мерил бы\n"
    "                            # другую задачу, чем обучение (ADR-036 п.4: сопоставимость\n"
    "                            # только по ответной части).\n"
    "                            if SFTDataset.THINK_IDS and SFT_THINK_MASK:\n"
    "                                vl = _sft_apply_think_mask(\n"
    "                                    vl, vi, SFTDataset.THINK_IDS,\n"
    "                                    SFTDataset.TOOL_CALL_IDS, SFTDataset.ROLE_IDS[0])\n"
)

THINK_HELPERS = '''

# ── S3ae (ADR-036): маска рассуждений <think>…</think> в лоссе SFT ────────────
# Вставлено tools/patch_pipeline_sft.py (--think-mask). Правило собрано по замеру
# данных набора sft_train_v12: (1) содержимое парных спанов <think>…</think>
# маскируется; (2) спаны <tool_call>…</tool_call> не маскируются НИКОГДА (внутри
# рассуждения лежит вызов инструмента — наивная маска съедала бы 70 % целевых
# токенов вызова, то есть била бы по agentic-компоненте критерия ADR-033);
# (3) незакрытый <think>, который САМ является целевым токеном, открывает регион
# до конца хода (3 953 таких тега на 3 000 сэмплов — обрезанные траектории и
# упоминания тега в тексте; 21 % целевых токенов, самые длинные англоязычные
# рассуждения); (4) делимитеры <think>/</think> остаются целевыми токенами —
# формат (дефект v7/v8: выпавший <think> → пустой ризонинг) и структурная защита
# от сэмпла без целей (NaN в cross_entropy при ignore_index=-100).
SFT_THINK_MASK = os.environ.get("SFT_THINK_MASK", "1") == "1"
_SFT_MASK_STATS = {"masked_tokens": 0, "empty_fallback": 0}


def _sft_think_mask(labels, ids, think_ids, tool_call_ids=None, im_start=None):
    """Маскировать рассуждения; возвращает число обнулённых позиций.

    Принимает и один сэмпл (1-D), и батч (2-D — так приходит валидация): обход
    по строкам, а не «предположим одномерность». На этом крэшился первый короткий
    тест S3ae (шаг 0, val_loss: `int(ids[i])` на строке тензора).
    """
    if labels.dim() == 2:
        total = 0
        for r in range(labels.shape[0]):
            total += _sft_think_mask_row(labels[r], ids[r], think_ids, tool_call_ids, im_start)
        return total
    return _sft_think_mask_row(labels, ids, think_ids, tool_call_ids, im_start)


def _sft_think_mask_row(labels, ids, think_ids, tool_call_ids=None, im_start=None):
    """Правило маски для одного сэмпла. `labels` правится на месте.

    Позиции, обнулённые базовой маской (промпт, user, ответ инструмента), не
    восстанавливаются. `ids`/`labels` — 1-D тензоры.
    """
    th_open, th_close = think_ids
    n = len(ids)
    # Спаны вызова инструмента: не маскируются никогда (действие, не рассуждение).
    keep = [False] * n
    if tool_call_ids and None not in tool_call_ids:
        tc_open, tc_close = tool_call_ids
        in_tc = False
        for i in range(n):
            t = int(ids[i])
            if t == tc_open:
                in_tc = True
            if in_tc:
                keep[i] = True
            if t == tc_close:
                in_tc = False
    was_target = [int(labels[i]) != -100 for i in range(n)]
    im_start = im_start if im_start is not None else 151644
    stack = []
    for i in range(n):
        t = int(ids[i])
        if t == th_open:
            stack.append(i)
        elif t == th_close and stack:
            o = stack.pop()
            for j in range(o + 1, i):
                labels[j] = -100
    # Незакрытый <think>, который сам является целью: регион до конца хода.
    for o in stack:
        if not was_target[o]:
            continue
        j = o + 1
        while j < n and int(ids[j]) != im_start:
            labels[j] = -100
            j += 1
    # Возврат: делимитеры рассуждения и спаны вызова инструмента остаются целями.
    for i in range(n):
        if was_target[i] and labels[i] == -100:
            t = int(ids[i])
            if keep[i] or t == th_open or t == th_close:
                labels[i] = int(ids[i])
    return sum(1 for i in range(n) if was_target[i] and labels[i] == -100)


def _sft_apply_think_mask(labels, ids, think_ids, tool_call_ids, im_start):
    """Маска + страховка от сэмпла без целей (иначе NaN в cross_entropy).

    Страховка возвращает метки **до** маски (а не «базовую редакцию» промпта: та
    обнуляла бы только паддинг и отдала бы в лосс системный промпт и вопросы
    среды). Откат не «тихий»: счётчик откатов печатается в отчёте маски, и по нему
    видно, сработала ли страховка на живом наборе.
    """
    masked = labels.clone()
    _sft_think_mask(masked, ids, think_ids, tool_call_ids, im_start)
    if int((masked != -100).sum()) == 0:
        _SFT_MASK_STATS["empty_fallback"] += 1
        return labels
    return masked


def _sft_mask_report(dataset, tag, log_dir, n=512):
    """Отчёт о маске: цели «до» и «после» — тем же кодом, что учится.

    Мерится `SFTDataset.__getitem__` в двух положениях переключателя, а не отдельной
    реализацией правила: иначе отчёт доказывал бы работу копии, а не правила.
    """
    global SFT_THINK_MASK
    keep = SFT_THINK_MASK
    counts = []
    for mode in (False, True):
        SFT_THINK_MASK = mode
        counts.append(sum(int((dataset[i]["labels"] != -100).sum())
                          for i in range(min(n, len(dataset)))))
    SFT_THINK_MASK = keep
    before, after = counts
    rep = {"tag": tag, "samples": min(n, len(dataset)), "think_mask_enabled": keep,
           "target_tokens_before": before, "target_tokens_after": after,
           "masked_share": (1 - after / before) if before else None,
           "empty_fallback_events": _SFT_MASK_STATS["empty_fallback"]}
    log.info(f"SFT MASK REPORT [{tag}]: целевых токенов было {before}, стало {after} "
             f"(снято {100 * (1 - after / before):.2f} %); откатов к базовой маске: "
             f"{_SFT_MASK_STATS['empty_fallback']}")
    try:
        with open(Path(log_dir) / "sft_mask_report.json", "w") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"SFT MASK REPORT: не записан ({e})")
    return rep

'''

HELPERS = '''

# ── SFT-стадия: монитор на действующих наборах и траектория по шагам ──────────
# Вставлено tools/patch_pipeline_sft.py. В штатном пайплайне контура этого блока
# нет; он существует затем, чтобы стадия контролировалась приборами, которые
# признаны действующими (ADR-033 п.3), и чтобы вердикт по ней опирался на
# траекторию, а не на сводку окон (ADR-022 п.4).
_SFT_CEILING_SCALE = {ceiling!r}
_SFT_MONITOR_SETS = (
{sets},
)
_SFT_MONITOR_REFERENCE = (
{refs},
)
PROBE_KEEP_LAST = {keep_last!r}


def _sft_trace(step, loss_value, lr, args):
    """Построчная запись (step, loss, lr, t) в <log_dir>/loss_trace.jsonl.

    `flush` на каждой строке обязателен: файл читают по живому прогону — он и есть
    подтверждение старта стадии (ADR-023 п.11).
    """
    log_dir = getattr(args, "log_dir", None)
    if not log_dir:
        return
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        with open(Path(log_dir) / "loss_trace.jsonl", "a") as f:
            f.write(json.dumps({{"step": int(step), "loss": float(loss_value),
                                "lr": float(lr), "t": time.time()}}) + "\\n")
            f.flush()
    except Exception as e:  # трассировка не имеет права уронить стадию
        log.warning(f"SFT: траектория не записана на шаге {{step}}: {{e}}")


def _sha256_full_digest(path):
    """Полный sha256 файла одной строкой hex; None, если файла нет.

    Полный, а не первые 1 МиБ (`_sha256_head`): усечённый хеш не отличает
    подменённый набор от объявленного, если различие лежит за первым мегабайтом
    (AD-2, дыра G9). Возвращается строка той же формы, что у `_sha256_head`, —
    поле `datasets` манифеста меняет редакцию, а не схему.
    """
    try:
        h = hashlib.sha256()
        with open(str(path), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _sft_jsonl_samples(path):
    """Число примеров набора: непустых строк JSONL. None — если файл не прочитан."""
    n = 0
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    n += 1
    except OSError:
        return None
    return n


def _sft_record_input_identity(args, dataset):
    """Предъявить факт: какой набор стадия ПРОЧИТАЛА (AD-2, ADR-028 п.1; S3av).

    Объявленный набор приходит параметром `--sft_data` (и его полным хешем
    `--sft_data_sha256`), фактический берётся у загрузчика. Блок `sft_input` в
    `run_manifest.json` стадии несёт путь jsonl, путь тензора, ПОЛНЫЕ sha256 обоих
    и число примеров — то, чем объявленное можно сверить с фактическим, не запуская
    обучение (страж `tools/check_dataset_identity.py`).

    Расхождение объявленного хеша с прочитанным — ОТКАЗ стадии здесь, до первого
    шага: стадия на необъявленном наборе не «предупреждение в логе», а остановка.
    """
    declared_path = str(getattr(args, "sft_data", "") or "")
    declared_sha = (getattr(args, "sft_data_sha256", None) or "").strip().lower() or None
    j_path = str(getattr(dataset, "source_path", declared_path))
    j_sha = _sha256_full_digest(j_path)
    t_obj = getattr(dataset, "npz_path", None)
    t_path = str(t_obj) if t_obj is not None else None
    t_sha = _sha256_full_digest(t_path) if t_path else None
    jsonl_samples = _sft_jsonl_samples(j_path)
    block = {{
        "declared_path": declared_path,
        "declared_sha256": declared_sha,
        "jsonl": {{"path": j_path, "sha256": j_sha, "hash_scope": "full",
                  "samples": jsonl_samples}},
        "tensor": {{"path": t_path, "sha256": t_sha, "hash_scope": "full",
                  "samples": len(dataset),
                  "samples_total": int(getattr(dataset, "samples_total", 0) or 0),
                  "candidates": list(getattr(dataset, "npz_candidates", []))}},
        "max_samples": int(getattr(args, "max_samples", 0) or 0),
        "stage": getattr(args, "stage", ""),
        "exp_name": getattr(args, "exp_name", ""),
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }}
    if declared_sha and j_sha and declared_sha != j_sha:
        raise SystemExit(
            f"ОТКАЗ: объявлен набор sha256={{declared_sha}} ({{declared_path}}), "
            f"а прочитан {{j_sha}} ({{j_path}}) — стадия на необъявленном наборе не "
            f"стартует (AD-2, ADR-028 п.1)")
    mpath = Path(args.ckpt_dir) / "run_manifest.json"
    man = {{}}
    if mpath.exists():
        try:
            man = json.loads(mpath.read_text())
        except Exception:
            man = {{}}
    man["sft_input"] = block
    mpath.write_text(json.dumps(man, indent=2, ensure_ascii=False))
    log.info(f"SFT вход: jsonl {{j_path}} sha256={{j_sha}} samples={{jsonl_samples}}; "
             f"тензор {{t_path}} sha256={{t_sha}} samples={{len(dataset)}}")
    return block


def _rl_record_input_identity(args, dataset):
    """Предъявить факт: какой набор курикулума стадия RL ПРОЧИТАЛА (ADR-054 п.2).

    Тот же принцип и тот же носитель, что у SFT-набора ({{@link _sft_record_input_identity}}),
    а не параллельный механизм: объявленный набор приходит параметром `--rl_data`
    (и его полным хешем `--rl_data_sha256`), фактический берётся у загрузчика.
    Блок `rl_input` в `run_manifest.json` стадии несёт путь jsonl, ПОЛНЫЙ sha256 и
    число задач — то, чем объявленное сверяется с прочитанным
    (`tools/check_dataset_identity.py`, правило C-030).

    **Почему это не «на всякий случай».** ADR-054 п.1 выбрал набором стадии
    `rl_tasks_revpool_v2.jsonl`, а `RLDataset` по умолчанию читает
    `rl_tasks_v3.jsonl`: пока вход стадии не назван и не предъявлен, выбор набора
    остаётся бумажным — ровно тот дефект, что S3av разобрал для SFT (объявлен v13 —
    прочитан v12). Тензора у RL-набора нет: загрузчик читает jsonl напрямую, и
    требовать тензор значило бы красное по построению.

    Расхождение объявленного хеша с прочитанным — ОТКАЗ стадии здесь, до первого
    шага: стадия на необъявленном наборе не «предупреждение в логе», а остановка.
    """
    declared_path = str(getattr(args, "rl_data", "") or "")
    declared_sha = (getattr(args, "rl_data_sha256", None) or "").strip().lower() or None
    j_path = str(getattr(dataset, "source_path", declared_path))
    j_sha = _sha256_full_digest(j_path)
    jsonl_samples = _sft_jsonl_samples(j_path)
    block = {{
        "declared_path": declared_path,
        "declared_sha256": declared_sha,
        "jsonl": {{"path": j_path, "sha256": j_sha, "hash_scope": "full",
                  "samples": jsonl_samples}},
        "tasks_loaded": len(dataset),
        "max_samples": int(getattr(args, "max_samples", 0) or 0),
        "stage": getattr(args, "stage", ""),
        "exp_name": getattr(args, "exp_name", ""),
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }}
    if declared_sha and j_sha and declared_sha != j_sha:
        raise SystemExit(
            f"ОТКАЗ: объявлен набор курикулума sha256={{declared_sha}} ({{declared_path}}), "
            f"а прочитан {{j_sha}} ({{j_path}}) — стадия на необъявленном наборе не "
            f"стартует (AD-2, ADR-054 п.2)")
    mpath = Path(args.ckpt_dir) / "run_manifest.json"
    man = {{}}
    if mpath.exists():
        try:
            man = json.loads(mpath.read_text())
        except Exception:
            man = {{}}
    man["rl_input"] = block
    mpath.write_text(json.dumps(man, indent=2, ensure_ascii=False))
    log.info(f"RL вход: jsonl {{j_path}} sha256={{j_sha}} samples={{jsonl_samples}}; "
             f"объявлено {{declared_path}} (sha256 {{declared_sha}})")
    return block


'''


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patch_once(text: str, old: str, new: str, name: str) -> str:
    """Заменить анкер, потребовав ровно одного вхождения."""
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"АНКЕР НЕГОДЕН [{name}]: вхождений {n}, нужно ровно 1 — "
                         f"база пайплайна не та, патч не применяется")
    return text.replace(old, new, 1)


def build(base_path: Path, probe_keep_last: int, think_mask: bool = False,
          peak_lr_scale: float = 1.0) -> tuple[bytes, dict]:
    base = base_path.read_text(encoding="utf-8")
    anchors: dict[str, str] = {}

    anchors["monitor"] = "def _general_eval_step(model, tokenizer, step, ckpt_dir):"
    out = patch_once(base, OLD_MONITOR, new_monitor(), "monitor")

    anchors["trace"] = "ema = loss.item() if ema is None else 0.98 * ema + 0.02 * loss.item()"
    out = patch_once(out, OLD_TRACE_ANCHOR, NEW_TRACE_BLOCK, "trace")

    anchors["ckpt"] = 'save_checkpoint_atomic(model, optimizer, Path(args.ckpt_dir)/f"sft_checkpoint_{step}.pt")'
    out = patch_once(out, OLD_CKPT_ANCHOR, NEW_CKPT_BLOCK, "ckpt")

    # S3av: идентичность набора. Порядок внутри блока важен только для читаемости —
    # анкеры не перекрываются (init ⊂ класс, val ⊂ run_sft, argparse ⊂ main).
    anchors["sft_init"] = "f\"sft_{max_len}.npz\"]"
    out = patch_once(out, OLD_SFT_INIT, NEW_SFT_INIT, "sft_init")

    anchors["sft_val_npz"] = 'f"sft_train_v12_8192_{tok_tag_for(args.model_name)}.npz"'
    out = patch_once(out, OLD_SFT_VAL_NPZ, NEW_SFT_VAL_NPZ, "sft_val_npz")

    anchors["sft_argparse"] = 'p.add_argument("--sft_data", default='
    out = patch_once(out, OLD_SFT_ARGPARSE, NEW_SFT_ARGPARSE, "sft_argparse")

    anchors["sft_identity_call"] = "peak_lr = 1e-5"
    out = patch_once(out, OLD_SFT_IDENTITY_CALL, NEW_SFT_IDENTITY_CALL, "sft_identity_call")

    anchors["datasets_hash"] = '"datasets": {d: _sha256_head(d) for d in'
    out = patch_once(out, OLD_DATASETS_HEAD, NEW_DATASETS_FULL, "datasets_hash")

    # S3be: идентичность набора курикулума RL (ADR-054 п.2). Три анкера — в классе
    # загрузчика, в вызове загрузчика и в argparse; не перекрываются.
    anchors["rl_init"] = 'self.pure = pure  # LAGUNA_PURE'
    out = patch_once(out, OLD_RL_INIT_ANCHOR, NEW_RL_INIT_ANCHOR, "rl_init")

    anchors["rl_argparse"] = 'p.add_argument("--rl_data", default='
    out = patch_once(out, OLD_RL_ARGPARSE, NEW_RL_ARGPARSE, "rl_argparse")

    anchors["rl_identity_call"] = "dataset = RLDataset(args.rl_data"
    out = patch_once(out, OLD_RL_IDENTITY_CALL, NEW_RL_IDENTITY_CALL, "rl_identity_call")

    # S3ax: темп SFT. Применяется ПОСЛЕ идентичности входа — тот патч вставляет
    # вызов перед `peak_lr`, не меняя строку, поэтому якорь здесь по-прежнему
    # встречается ровно один раз (иначе `patch_once` отказал бы).
    if peak_lr_scale != 1.0:
        anchors["sft_peak_lr"] = "peak_lr = 1e-5"
        out = patch_once(out, OLD_SFT_PEAK_LR, new_sft_peak_lr(peak_lr_scale), "sft_peak_lr")

    # S3ae: маска <think> — до вставки HELPERS, чтобы хелперы маски встали тем же блоком.
    if think_mask:
        anchors["attrs"] = "    ROLE_IDS = None"
        out = patch_once(out, OLD_CLASS_ATTRS, NEW_CLASS_ATTRS, "attrs")
        anchors["getitem"] = "elif in_tr: labels[i] = -100"
        out = patch_once(out, OLD_GETITEM_TAIL, NEW_GETITEM_TAIL, "getitem")
        anchors["run_sft_ids"] = 'SFTDataset.ROLE_IDS = (tokenizer.convert_tokens_to_ids("<|im_start|>"),'
        out = patch_once(out, OLD_RUN_SFT_IDS, NEW_RUN_SFT_IDS, "run_sft_ids")
        anchors["mask_report"] = "loader = DataLoader(dataset, batch_size=args.batch_size"
        out = patch_once(out, OLD_LOADER, NEW_LOADER, "mask_report")
        anchors["val_labels"] = "vl = vi.clone(); vl[vm == 0] = -100"
        out = patch_once(out, OLD_VAL_LABELS, NEW_VAL_LABELS, "val_labels")

    anchors["helpers"] = 'if __name__ == "__main__":'
    helpers = HELPERS.format(
        ceiling=CEILING_SCALE,
        sets=",\n".join(f'    ("{n}", "{CTR_DATASETS}/{f}", {b!r}, "{k}")'
                        for n, f, b, k in MONITOR_SETS),
        refs=",\n".join(f'    ("{n}", "{CTR_DATASETS}/{f}")' for n, f in MONITOR_REFERENCE),
        keep_last=probe_keep_last,
    )
    if think_mask:
        helpers += THINK_HELPERS
    out = patch_once(out, anchors["helpers"], helpers + anchors["helpers"], "helpers")

    return out.encode("utf-8"), anchors


def main() -> int:
    ap = argparse.ArgumentParser(description="Собрать копию пайплайна для SFT-стадии")
    ap.add_argument("--base", default="laguna_pipeline_v8.py", help="базовый пайплайн (SSOT)")
    ap.add_argument("--out", required=True, help="куда положить пропатченную копию")
    ap.add_argument("--patch-json", help="куда записать паспорт патча (pipeline_patch.json)")
    ap.add_argument("--probe-keep-last", type=int, default=DEFAULT_PROBE_KEEP_LAST)
    ap.add_argument("--think-mask", action="store_true",
                    help="патч 4 (S3ae, ADR-036): маскировать <think>…</think> в лоссе SFT")
    ap.add_argument("--check", action="store_true", help="только собрать, ничего не писать")
    ap.add_argument("--peak-lr-scale", type=float, default=1.0, metavar="S",
                    help="S3ax (ADR-052): множитель пика LR SFT (1.0 — штатные 1e-5; "
                         "0.2 — 2e-6). Форма расписания (косинус, финал 2e-7) и warmup "
                         "не меняются: рука отличается масштабом, а не формой")
    a = ap.parse_args()

    if not (0.0 < a.peak_lr_scale <= 1.0):
        print(f"--peak-lr-scale вне (0, 1]: {a.peak_lr_scale} — множитель >1 поднимал бы "
              "темп выше объявленного, ≤0 обнулил бы обучение", file=sys.stderr)
        return 4

    base_path = Path(a.base)
    if not base_path.is_file():
        print(f"база не найдена: {base_path}", file=sys.stderr)
        return 2
    base_bytes = base_path.read_bytes()
    out_bytes, anchors = build(base_path, a.probe_keep_last, a.think_mask, a.peak_lr_scale)

    # Синтаксис обязан быть валиден до записи: пропатченный пайплайн исполняется
    # контуром на стенде, и «доехало, но не компилируется» стоило бы целого старта.
    try:
        compile(out_bytes.decode("utf-8"), str(a.out), "exec")
    except SyntaxError as e:
        print(f"ПАТЧ ДАЛ НЕВАЛИДНЫЙ PYTHON: {e}", file=sys.stderr)
        return 3

    applied = [
        "монитор форгеттинга → K1/K2/домен (ADR-033 п.3, дыра G2)",
        "loss_trace по шагам в SFT (ADR-022 п.4, ADR-023 п.11)",
        f"точки замера sft_probe_{{step}}.pt каждые SFT_PROBE_CKPT_EVERY=500 (keep_last={a.probe_keep_last})",
        "идентичность набора SFT: val-тензор — от загрузчика (не литералом), блок sft_input "
        "в манифесте стадии с полными sha256, отказ при расхождении с --sft_data_sha256 "
        "(S3av, AD-2/ADR-028 п.1)",
        "идентичность набора курикулума RL: загрузчик называет открытый файл "
        "(source_path), блок rl_input в манифесте стадии с полным sha256 и числом задач, "
        "отказ при расхождении с --rl_data_sha256 (S3be, ADR-054 п.2)",
    ]
    if a.think_mask:
        applied.append("маска <think>…</think> в лоссе SFT (S3ae, ADR-036; "
                       "переключатель SFT_THINK_MASK, по умолчанию включена)")
    if a.peak_lr_scale != 1.0:
        applied.append(
            f"темп SFT: пик 1e-5 × {a.peak_lr_scale:g} = {1e-5 * a.peak_lr_scale:.4e} "
            "(S3ax, ADR-052; форма расписания и warmup не тронуты)")
    patch = {
        "patched_by": "tools/patch_pipeline_sft.py",
        "loss_mask": "think_masked" if a.think_mask else "prefix_only",
        "applied": applied,
        "anchors": anchors,
        "monitor_sets": [{"component": n, "file": f, "base": b, "rule": k}
                         for n, f, b, k in MONITOR_SETS],
        "monitor_reference": [{"name": n, "file": f} for n, f in MONITOR_REFERENCE],
        "ceiling_scale": CEILING_SCALE,
        "probe_keep_last": a.probe_keep_last,
        "sft_input_identity": {
            "adr": "AD-2, ADR-028 п.1",
            "defect": "val-тензор и вход стадии выводились литералом sft_train_v12 "
                      "при объявленном --sft_data; декларация и факт расходились молча",
            "recorded_in": "run_manifest.json стадии (checkpoints/), блок sft_input",
            "fields": ["declared_path", "declared_sha256",
                       "jsonl{path,sha256,hash_scope,samples}",
                       "tensor{path,sha256,hash_scope,samples,samples_total,candidates}"],
            "refusal": "SystemExit, если --sft_data_sha256 не совпал с прочитанным jsonl "
                       "(стадия не делает ни одного шага)",
            "hash_scope": "full (полный sha256 обоих файлов; _sha256_head — 1 МиБ — "
                          "оставлен только для совместимости поля datasets у базового пайплайна)",
            "guard": "tools/check_dataset_identity.py",
        },
        "rl_input_identity": {
            "adr": "ADR-054 п.1–2",
            "defect": "набором стадии решением выбран rl_tasks_revpool_v2.jsonl, а "
                      "RLDataset читает rl_tasks_v3.jsonl по умолчанию; вход стадии "
                      "не был назван параметром и не предъявлялся манифестом",
            "recorded_in": "run_manifest.json стадии (checkpoints/), блок rl_input",
            "fields": ["declared_path", "declared_sha256",
                       "jsonl{path,sha256,hash_scope,samples}",
                       "tasks_loaded", "max_samples"],
            "no_tensor": "тензора у набора курикулума нет: RLDataset читает jsonl "
                         "напрямую — обязательный tensor был бы красным по построению",
            "refusal": "SystemExit, если --rl_data_sha256 не совпал с прочитанным jsonl "
                       "(стадия не делает ни одного шага)",
            "guard": "tools/check_dataset_identity.py (правило C-030, предмет — rl_input)",
        },
        "pipeline_base_file": base_path.name,
        "pipeline_base_sha256": sha(base_bytes),
        "patched_sha256": sha(out_bytes),
        "size_bytes": {"base": len(base_bytes), "patched": len(out_bytes)},
    }
    patch["sft_lr"] = {
        "adr": "ADR-052",
        "peak_lr_scale": a.peak_lr_scale,
        "peak_lr": 1e-5 * a.peak_lr_scale,
        "peak_lr_base": 1e-5,
        "eta_min": 2e-7,
        "schedule": "CosineAnnealingLR(T_max=args.max_steps, eta_min=2e-7)",
        "warmup_steps": 100,
        "what_changed": ("только множитель пика" if a.peak_lr_scale != 1.0
                         else "ничего: штатный пик 1e-5, копия побайтово прежняя"),
        "where_verified": "числа печатает сама копия при старте стадии: "
                          "log.info(\"SFT LR: пик …\") в run_sft",
        "guard": "tools/check_stage_lr.py — объявленный темп против копии пайплайна",
    }

    if a.think_mask:
        patch["think_mask"] = {
            "adr": "ADR-036",
            "switch": "SFT_THINK_MASK (env; по умолчанию 1 — включена)",
            "rule": [
                "содержимое парных спанов <think>…</think> → labels = -100",
                "спаны <tool_call>…</tool_call> не маскируются никогда "
                "(действие, а не рассуждение; наивная маска снимала бы 70 % целей вызова)",
                "незакрытый <think>, который сам является целевым токеном → регион до "
                "конца хода (<|im_start|>); ложные упоминания тега в промпте/ответе "
                "инструмента регион не открывают",
                "делимитеры <think>/</think> остаются целевыми токенами: формат "
                "(дефект v7/v8 — выпавший <think> даёт пустой ризонинг) и защита от "
                "сэмпла без целей (NaN в cross_entropy при ignore_index=-100)",
                "страховка: сэмпл без целей после маски откатывается к базовой "
                "редакции меток, счётчик откатов — в logs/sft_mask_report.json",
            ],
            "measured": {
                "samples": 4000,
                "reasoning_content_targets_before": 1105508,
                "tool_call_targets_before": 252361,
                "answer_targets_before": 2517540,
                "targets_after": 2597360,
                "reasoning_content_left": 0,
                "tool_call_kept": 1.0,
                "answer_kept": 1.0,
                "empty_target_samples": 0,
                "note": "числа сняты на sft_train_v12_8192_qwen25.npz; повтор — "
                        "tools/verify_think_mask.py",
            },
        }
    print(json.dumps(patch, ensure_ascii=False, indent=2))
    #: Паспорт патча — отчёт сборки, а не артефакт прогона: пишется и в `--check`,
    #: иначе проверить «что именно применилось» можно было бы только вслепую.
    if a.patch_json:
        Path(a.patch_json).write_text(json.dumps(patch, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
    if a.check:
        return 0
    Path(a.out).write_bytes(out_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
