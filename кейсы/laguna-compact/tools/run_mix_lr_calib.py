#!/usr/bin/env python3
"""S3m — раннер калибровки микса и пика LR: сетка 2×2, по 2000 шагов CPT.

Что запускается. ADR-022 п.2 после диагноза S3k (flex **не причина**: штатное
внимание разрушает язык сильнее) оставил два фактора — доля replay и пик LR.
Сетка `replay {25 %, 50 %}` × `peak_lr_scale {0.7, 0.35}`; всё остальное
одинаково: `Qwen/Qwen2.5-0.5B`, батч 1, `max_len 8192`, сид 42, flex-режим
(`LAGUNA_ATTN=flex`, он оставлен по решению владельца).

Почему четыре цепочки, а не одна с четырьмя стадиями. `--ctr-run-dir` у цепочки
один на всю цепочку: стадии писали бы чекпойнты в общий каталог, и `CPT RESUME`
внутри пайплайна подхватил бы состояние предыдущей руки (в пайплайне resume ищет
`checkpoint_[0-9]*.pt` в `--ckpt_dir`). Поэтому рука = цепочка = свой каталог;
цепочки идут **последовательно** (AD-5: на GB10 одна тренировочная нагрузка).

Где лежат артефакты. Каталог прогона на стенде — `/home/user/gb10-shared/calib/<run_id>`
(он же `/workspace/shared/calib/<run_id>` в контейнере): чекпойнты и логи пишутся
**сразу на сетевой диск** (AD-4), а не в `/home/user/experiments`. В кейс
приезжают малые артефакты (логи, траектория, манифесты), а `runs/<run_id>/checkpoints`
— симлинк на сетевой диск (C-011: копий весов в кейсе нет).

Что правится в копии пайплайна (рабочий `laguna_pipeline_v8.py` не трогается;
`pipeline_base_sha256` базовой ревизии пишется и в `pipeline_patch.json`, и в
манифест AD-2):

* **номерной чекпойнт каждые `CKPT_EVERY` (500) шагов, под именем, которое
  ретенция пайплайна не трогает.** Штатный `step % 200` и «держим 2 последних»
  съели бы ровно те состояния, ради которых прогон идёт (шаги 500/1000/1500
  штатным шагом не сохраняются вовсе);
* **траектория лосса по шагам** в `logs/loss_trace.jsonl` — ADR-022 п.4 требует
  судить по траектории, а пайплайн печатает лосс раз в 50 шагов.

Обе вставки привязаны к якорям, и якорь, встречающийся не ровно один раз, —
отказ: «применилось не туда» дало бы траекторию чужого шага.

**Почему точки замера пишутся именем `calib_checkpoint_<N>.pt` (правка 16.09 после
первой руки).** `keep_last=0` в `save_checkpoint_atomic` выключает ретенцию
**внутри** функции, но сразу за вставкой в цикле CPT стоит собственная ретенция
пайплайна — `glob("checkpoint_[0-9]*.pt")` и «оставить два последних по mtime».
На шаге 1500 она удалила `checkpoint_500.pt`, то есть ровно то, ради чего вставка
и делалась (наблюдалось на руке 25-0.7). Имя `calib_checkpoint_` под этот glob не
попадает, поэтому точки замера выживают без правки самой ретенции: она продолжает
работать как прежде для номерных чекпойнтов контура. Лишний довод за отдельное
имя — резюм CPT ищет `checkpoint_[0-9]*.pt` и не должен подхватывать точки замера.

**Точка «шаг 2000».** Цикл CPT идёт по шагам 0…1999 и завершается записью
`checkpoint_final.pt`, то есть состояния после 2000 шагов оптимизатора;
отдельного номерного файла шага 2000 не существует, и `checkpoint_final.pt` — это
он. Разрешение имён (в том числе для руки 1, уже отработавшей со старым именем) —
в `ckpt_path_for_step`.

Запуск по шагам (каждая команда самостоятельна — прогон длиной ~6 ч нельзя
делать одним вызовом):

    python3 tools/run_mix_lr_calib.py --plan
    python3 tools/run_mix_lr_calib.py --prepare
    python3 tools/run_mix_lr_calib.py --launch 25-0.7 --ts <ts>
    python3 tools/run_mix_lr_calib.py --wait   25-0.7 --ts <ts>
    python3 tools/run_mix_lr_calib.py --fetch  25-0.7 --ts <ts>
    python3 tools/run_mix_lr_calib.py --chain --arms 25-0.35 50-0.7 50-0.35 --ts <ts>
    python3 tools/run_mix_lr_calib.py --grid-wait  --ts <ts> --timeout 540
    python3 tools/run_mix_lr_calib.py --grid-fetch --arms 25-0.35 50-0.7 50-0.35 --ts <ts>
    python3 tools/run_mix_lr_calib.py --probe          --ts <ts>
    python3 tools/run_mix_lr_calib.py --analyze        --ts <ts>

`--launch` поднимает **одну** руку и требует, чтобы раннер дожил до `--launch`
следующей; смерть раннера между руками оставляет сетку недоделанной — так и вышло
16.09 (рука 1 из 4). Для сетки целиком есть `--chain`: он поднимает на стенде
одну tmux-сессию, которая идёт по рукам **последовательно** (AD-5: одна
тренировочная нагрузка на GB10) и переживает раннер.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE_ROOT / "tools"))

import run_smoke as S  # noqa: E402 — транспорт стенда (ssh/ssh_put/ssh_get/sha256)

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

RUN_PREFIX = "calib"

#: Стенд. Каталоги прогонов — на сетевом диске (AD-4): он же виден контейнеру
#: как /workspace/shared, поэтому «хост-путь» и «путь в контейнере» — один файл.
STAND_SHARED = "/home/user/gb10-shared"
STAND_RUNS = f"{STAND_SHARED}/calib"
CTR_RUNS = "/workspace/shared/calib"
SAFE_START = f"{STAND_SHARED}/nvrm-storm/safe_start.sh"
STORM_GAP = f"{STAND_SHARED}/nvrm-storm/storm_gap.sh"
GLM_ENV = f"{STAND_SHARED}/.glm_env"
NVRN_LOG = "/home/user/experiments/nvrm_watch.log"
DEFAULT_IMAGE = "nvcr.io/nvidia/pytorch:26.07-py3-vllm"

#: Копия пайплайна в каталоге прогона. Рабочий файл контура — только чтение.
PIPELINE_SRC = CASE_ROOT / "laguna_pipeline_v8.py"
PIPELINE_COPY_NAME = "laguna_pipeline_calib.py"

#: Что доставляется в каталог прогона: инструменты кейса, чья копия в прогоне
#: делает прогон воспроизводимым (цепочка, страж, сэмплер, генератор манифеста).
SHIPPED = [
    ("pilot_chain.sh", "tools/pilot_chain.sh"),
    ("check_resource_owner.sh", "tools/check_resource_owner.sh"),
    ("smoke_mem_sampler.sh", "tools/smoke_mem_sampler.sh"),
    ("write_run_manifest.py", "tools/write_run_manifest.py"),
]

MODEL = "Qwen/Qwen2.5-0.5B"
SEED = 42
MAX_LEN = 8192
MAX_SAMPLES = 50000
STEPS = 2000
#: flex eager (FLEX_COMPILE=0) держит скоры [B,H,L,L]; батч 1 — не выбор раннера,
#: а условие влезания (run_cpt пайплайна сам ставит cpt_bs=1 в этом режиме).
BATCH = 1
CKPT_EVERY = 500
#: Точки замера PPL — ровно те, что требует дельта (шаг 500/1000/1500/2000).
CKPT_POINTS = [500, 1000, 1500, 2000]
#: Имя точки замера. Отдельное от `checkpoint_<N>.pt` намеренно: под штатное имя
#: в цикле CPT стоит ретенция «два последних по mtime» (см. шапку), и на шаге 1500
#: она удалила точку шага 500 у уже отработавшей руки 25-0.7.
CALIB_CKPT_STEM = "calib_checkpoint"
CALIB_CKPT_FMT = CALIB_CKPT_STEM + "_{step}.pt"
#: Имя, которым писались точки у руки, отработавшей до этой правки (16.09).
LEGACY_CKPT_FMT = "checkpoint_{step}.pt"
#: Финальный чекпойнт стадии — состояние после `STEPS` шагов оптимизатора.
FINAL_CKPT_NAME = "checkpoint_final.pt"

#: Корпуса. Путь в контейнере задаёт пайплайну stem кэша; путь на хосте — то, что
#: хеширует манифест AD-2 (полный файл, не первые 1 МиБ).
CORPORA = {
    25: {
        "txt_ctr": "/workspace/shared/datasets/cpt_corpus_v12r.txt",
        "cache_host": f"{STAND_SHARED}/datasets/tok/cpt_corpus_v12r_8192_qwen25.npy",
        "label": "v12r (75/25, существующий кэш контура — не пересобирался)",
        "replay_chunks": 2444,
    },
    50: {
        "txt_ctr": "/workspace/shared/datasets/cpt_corpus_v12r50.txt",
        "cache_host": f"{STAND_SHARED}/datasets/tok/cpt_corpus_v12r50_8192_qwen25.npy",
        "label": "v12r50 (50/50, собран S3m: tools/build_mix_v12r50.py)",
        "replay_chunks": 3493,
    },
}

#: Реплей-источник целиком (`general_replay_ru.txt`) — 3493 чанка по 8192 токена:
#: это потолок источника, он же ограничил долю 50 % в S3m равными долями.
REPLAY_SOURCE_CHUNKS = 3493
#: Префикс реплей-источника, который видел пилот (ADR-018 строил набор v2 с
#: исключением документов именно этого префикса, а не всего источника).
ADR018_PILOT_REPLAY_PREFIX = 2444

ARMS: dict[str, dict] = {
    "25-0.7": {"replay": 25, "lr_scale": 0.7},
    "25-0.35": {"replay": 25, "lr_scale": 0.35},
    "50-0.7": {"replay": 50, "lr_scale": 0.7},
    "50-0.35": {"replay": 50, "lr_scale": 0.35},
}
ARM_ORDER = list(ARMS)

# ── S3o: контрольные руки (ADR-022 п.1–2: сначала причина форгеттинга) ────────
#: Тот же протокол, что у сетки S3m (2000 шагов, батч 1, сид 42, тот же образ и та
#: же копия пайплайна), и **ровно один** отличающийся фактор на руку:
#:
#:   * **C1 «чистый протокол»** — корпус только из общего языка (`general_replay_ru.txt`,
#:     домена нет вовсе), пик LR как у лучшей руки сетки (0.35). Если язык
#:     разрушается и здесь, причина — протокол, а не доменный корпус.
#:   * **C2 «низкий LR»** — корпус v12r (как у лучшей руки сетки), пик LR в 10 раз
#:     ниже (0.035). Если язык держится, причина в LR и это рабочий конфиг для CPT.
#:
#: Каталоги рук — `ctrl-*`, а не `calib-*`: сетка S3m остаётся адресуемой по своим
#: именам, а проба PPL и манифесты не смешивают два разных вопроса (калибровка
#: долей и диагностика причины).
CTRL_PREFIX = "ctrl"

#: Корпус C1 — 100 % общий язык. Кэш собран `tools/build_ctrl_c1_corpus.py` тем же
#: чанкером, что v12r50, с побайтовой сверкой против реплей-блоков v12r/v12r50.
CTRL_CORPUS_100 = {
    "txt_ctr": "/workspace/shared/datasets/cpt_corpus_ctrl100.txt",
    "cache_host": f"{STAND_SHARED}/datasets/tok/cpt_corpus_ctrl100_8192_qwen25.npy",
    "label": "ctrl100 (0/100 — общий язык, домена нет; tools/build_ctrl_c1_corpus.py)",
    "replay_chunks": 3493,
}

CTRL_ARMS: dict[str, dict] = {
    # replay=100 назван честно: у корпуса C1 домена нет, «100 % replay» здесь
    # означает «весь реплей-источник», а не выбранную долю (потолок источника —
    # 3493 чанка, ровно столько и берётся).
    "C1-100-0.35": {"replay": 100, "lr_scale": 0.35, "corpus": CTRL_CORPUS_100},
    "C2-25-0.035": {"replay": 25, "lr_scale": 0.035, "corpus": CORPORA[25]},
}
CTRL_ORDER = list(CTRL_ARMS)
ALL_ARMS = ARM_ORDER + CTRL_ORDER


def is_control(arms) -> bool:
    """Контрольные ли руки. Смешанный список — ошибка адресации, а не «почти контроль».

    Отвечает `False` и на пустом списке: «нет рук» — не контроль.
    """
    return bool(arms) and all(a in CTRL_ARMS for a in arms)


def arm_spec(arm: str) -> dict:
    """Спецификация руки вместе с её корпусом — сетка S3m и контроль S3o одним видом.

    Раньше корпус доставался как `CORPORA[ARMS[arm]["replay"]]` по месту; для C1
    такой ключ не существует (доля 100 — не доля микса, а отсутствие домена),
    поэтому связь «рука → корпус» теперь названа один раз здесь.
    """
    if arm in CTRL_ARMS:
        spec = dict(CTRL_ARMS[arm])
        #: `setdefault` здесь не годится: второй аргумент вычисляется всегда, а
        #: `CORPORA[100]` для C1 не существует (доля 100 — не доля микса: домена нет).
        if "corpus" not in spec:
            spec["corpus"] = CORPORA[spec["replay"]]
        return spec
    return {**ARMS[arm], "corpus": CORPORA[ARMS[arm]["replay"]]}


def run_prefix(arm: str) -> str:
    """Префикс каталогов руки: `ctrl-` у контрольных, `calib-` у сетки."""
    return CTRL_PREFIX if arm in CTRL_ARMS else RUN_PREFIX


def general_metric_validity(arm: str) -> dict:
    """Какие наборы общего языка годны для вердикта по этой руке (AD-7, утечка).

    Набор `v2_general` собирался (ADR-018 п.3) с исключением документов, попавших в
    **обученный префикс реплей-источника** — 2444 чанка, столько видел пилот. Рука,
    берущая реплей-источник целиком (3493 чанка), обучается и на документах, которые
    остались за тем префиксом, — то есть и на документах набора v2.

    Замер S3o (tools/audit_ctrl_corpus_leak.py, единица счёта — документ набора, как
    его режет `_ppl_eval`): из 200 документов v2_general **все 200** лежат в
    реплей-источнике дословно, у 183 есть общее непрерывное окно ≥20 слов, максимум —
    429 слов подряд. У `v1_general` — 0 общих 12-граммовых окон из 10.1M слов и 0
    дословных попаданий из 24 документов: он чист для всех рук, включая C1.

    Отсюда правило: решающая метрика общего языка у «полных» рук — **v1**, а v2
    публикуется как заражённый, а не как второй независимый голос.
    """
    spec = arm_spec(arm)
    chunks = spec["corpus"].get("replay_chunks")
    contaminated = chunks is not None and chunks >= REPLAY_SOURCE_CHUNKS
    return {
        "replay_chunks": chunks,
        "replay_source_chunks": REPLAY_SOURCE_CHUNKS,
        "v1_general": "годен (0 общих 12-граммовых окон с источниками корпуса)",
        "v2_general": ("ЗАРАЖЁН: рука обучается на всём реплей-источнике, а набор v2 "
                       "исключался только против префикса "
                       f"{ADR018_PILOT_REPLAY_PREFIX} чанков" if contaminated
                       else "годен: обученный префикс реплей-источника "
                            f"({chunks} чанков) не длиннее префикса ADR-018 "
                            f"({ADR018_PILOT_REPLAY_PREFIX})"),
        "decisive_general_set": "v1_general" if contaminated else "v1_general и v2_general",
    }


def probe_prefix(arms) -> str:
    """Префикс каталога пробы PPL — по рукам, которые в ней мерятся."""
    return CTRL_PREFIX if is_control(arms) else RUN_PREFIX


def ppl_run_name(ts: str, arms=None) -> str:
    return f"{probe_prefix(arms)}-ppl-{ts}"

# ─────────────────────────── патч копии пайплайна ────────────────────────────

ANCHOR_LOSS = """            loss = out.loss
            optimizer.zero_grad(); loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
"""
PATCH_LOSS = ANCHOR_LOSS + "            _calib_trace(step, loss.item(), lr, args)\n"

ANCHOR_CKPT = """            if step % 200 == 0 and step > 0:
                save_checkpoint_atomic(model, optimizer, Path(args.ckpt_dir)/f"checkpoint_{step}.pt")
"""
#: `{stem}` — единственная подстановка шаблона (имя точки замера из
#: `CALIB_CKPT_STEM`); `{step}` остаётся в тексте как есть — это часть f-строки
#: вставляемого кода, а не место подстановки.
PATCH_CKPT = ("""            _calib_every = int(os.environ.get("CALIB_CKPT_EVERY", "500") or 0)
            if _calib_every > 0 and step > 0 and step % _calib_every == 0:
                # S3m: точки замера PPL. keep_last=0 и ОТДЕЛЬНОЕ имя: ретенция,
                # стоящая ниже в этом же блоке, глобит `checkpoint_[0-9]*.pt` и
                # держит два последних по mtime — под штатным именем она удалила
                # точку шага 500 на шаге 1500 (факт руки 25-0.7, 16.09).
                save_checkpoint_atomic(model, optimizer,
                                       Path(args.ckpt_dir)/f"{stem}_{step}.pt",
                                       keep_last=0)
""").replace("{stem}", CALIB_CKPT_STEM)

ANCHOR_MAIN = 'if __name__ == "__main__":\n    main()\n'

TRACE_HELPER = '''

# ── S3m (калибровка микса и пика LR) ─────────────────────────────────────────
# Вставлено раннером tools/run_mix_lr_calib.py. В рабочем пайплайне контура этого
# блока нет и быть не должно: он существует затем, чтобы вердикт по ADR-022 п.2
# опирался на **траекторию по шагам**, а не на сводку раз в 50 шагов.
def _calib_trace(step, loss_value, lr, args):
    """Построчная запись (step, loss, lr, t) в <log_dir>/loss_trace.jsonl.

    `flush` на каждой строке обязателен: траекторию читают раннер и страж по
    живому прогону, а не после него.
    """
    log_dir = getattr(args, "log_dir", None)
    if not log_dir:
        return
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        with open(Path(log_dir) / "loss_trace.jsonl", "a") as f:
            f.write(json.dumps({"step": int(step), "loss": float(loss_value),
                                "lr": float(lr), "t": time.time()}) + "\\n")
            f.flush()
    except Exception as e:  # трассировка не имеет права уронить стадию
        log.warning(f"S3m: траектория не записана на шаге {step}: {e}")
'''


def sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def patched_pipeline(src: str) -> tuple[str, dict]:
    """Копия пайплайна с двумя вставками; якорь обязан встречаться ровно один раз."""
    for name, anchor in (("loss", ANCHOR_LOSS), ("ckpt", ANCHOR_CKPT), ("main", ANCHOR_MAIN)):
        n = src.count(anchor)
        if n != 1:
            raise SystemExit(
                f"патч не применён: якорь «{name}» встречается {n} раз (ожидался ровно 1) — "
                f"контурный пайплайн изменился, сверь ANCHOR_* в tools/run_mix_lr_calib.py")
    out = src.replace(ANCHOR_LOSS, PATCH_LOSS, 1).replace(ANCHOR_CKPT, PATCH_CKPT, 1)
    out = out.replace(ANCHOR_MAIN, TRACE_HELPER.strip("\n") + "\n\n" + ANCHOR_MAIN, 1)
    return out, {
        "runner": "tools/run_mix_lr_calib.py",
        "applied": ["loss_trace (per-step)",
                    f"calib_ckpt_every={CKPT_EVERY} (keep_last=0; имя {CALIB_CKPT_FMT} — "
                    f"вне ретенции пайплайна, которая глобит {LEGACY_CKPT_FMT.format(step='[0-9]*')})"],
        "anchors": {"loss": ANCHOR_LOSS.splitlines()[0].strip(),
                    "ckpt": ANCHOR_CKPT.splitlines()[0].strip(),
                    "main": ANCHOR_MAIN.splitlines()[0].strip()},
        "pipeline_base_file": PIPELINE_SRC.name,
        "pipeline_base_sha256": sha256_text(src),
        "patched_sha256": sha256_text(out),
        "size_bytes": {"base": len(src.encode()), "patched": len(out.encode())},
    }


# ─────────────────────────── каталоги прогонов ───────────────────────────────

def run_id(arm: str, ts: str) -> str:
    return f"{run_prefix(arm)}-{arm}-{ts}"


def stages_tsv(arm: str, ts: str) -> str:
    """Одна стадия: CPT `STEPS` шагов. Формат — тот, что читает `pilot_chain.sh`."""
    return "\t".join(["cpt", "cpt", "cpt", "checkpoints/checkpoint_final.pt",
                      str(STEPS), str(BATCH), "-", run_id(arm, ts)]) + "\n"


def runs_root_default() -> Path:
    return CASE_ROOT / "runs"


def arm_dir(arm: str, ts: str, runs_root: Path | None = None) -> Path:
    """Каталог руки. Корень — параметр: разбор обязан быть проверяем на фикстуре."""
    return (runs_root or runs_root_default()) / run_id(arm, ts)


def prepare_arm(arm: str, ts: str, force: bool = False) -> dict:
    """Собрать каталог прогона в кейсе: копия пайплайна, цепочка, stages.tsv, параметры."""
    d = arm_dir(arm, ts)
    if d.exists() and not force:
        raise SystemExit(f"каталог прогона уже есть: {d} (--force перезапишет)")
    (d / "logs").mkdir(parents=True, exist_ok=True)
    (d / "var" / "status").mkdir(parents=True, exist_ok=True)

    src = PIPELINE_SRC.read_text(encoding="utf-8")
    patched, patch_record = patched_pipeline(src)
    (d / PIPELINE_COPY_NAME).write_text(patched, encoding="utf-8")
    (d / "pipeline_patch.json").write_text(
        json.dumps(patch_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    shipped = {}
    for local_name, case_rel in SHIPPED:
        data = (CASE_ROOT / case_rel).read_bytes()
        (d / local_name).write_bytes(data)
        shipped[local_name] = {"source": case_rel, "sha256": S.sha256_bytes(data)}
        if local_name.endswith(".sh"):
            os.chmod(d / local_name, 0o755)
    (d / "stages.tsv").write_text(stages_tsv(arm, ts), encoding="utf-8")

    spec = arm_spec(arm)
    corpus = spec["corpus"]
    params = {
        "task": ("S3o — контрольные руки CPT: чистый протокол и низкий LR "
                 "(ADR-022 п.1–2, диагностика причины форгеттинга)" if arm in CTRL_ARMS
                 else "S3m — калибровка микса и пика LR (ADR-022 п.2)"),
        "arm": arm, "replay_share_pct": spec["replay"], "peak_lr_scale": spec["lr_scale"],
        "model": MODEL, "seed": SEED, "max_len": MAX_LEN, "max_samples": MAX_SAMPLES,
        "cpt_steps": STEPS, "cpt_batch": BATCH, "ckpt_every": CKPT_EVERY,
        "ckpt_points": CKPT_POINTS,
        "attn": "flex (LAGUNA_ATTN=flex, FLEX_COMPILE=0) — режим оставлен владельцем",
        "cpt_data_ctr": corpus["txt_ctr"], "cpt_dataset_host": corpus["cache_host"],
        "corpus_label": corpus["label"],
        "pipeline_patch": patch_record, "shipped": shipped,
        "pipeline_base_sha256": patch_record["pipeline_base_sha256"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (d / "calib_params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"run_dir": d, "params": params, "patch": patch_record}


def write_launch_manifest(d: Path, arm: str, args, case_root: Path | None = None) -> dict:
    """Манифест AD-2 в каталоге руки — **сразу при старте**, а не только на финише.

    Зачем. Правило AD-2 говорит «каждый каталог прогона несёт run_manifest.json», а
    каталог прогона существует с момента запуска: без манифеста старта гейт C-012
    красный всё время, пока рука идёт (часы), и «прогон» неотличим от «каталога с
    файлами». Цепочка на стенде перезаписывает этот манифест своим — по завершении
    стадии (`pilot_chain.sh` → тот же `write_run_manifest.py` с фактическими
    статусами), и `--fetch` привозит его в кейс.

    Стадия помечается `running`, а не `pending`: работа идёт, и это факт, а не
    план. Манифест с `pipeline_complete: true` не перезаписывается никогда —
    повторный `--chain` по законченному прогону не имеет права стереть его
    фактический манифест.
    """
    spec = arm_spec(arm)
    root = case_root or CASE_ROOT
    manifest = d / "run_manifest.json"
    if manifest.is_file():
        try:
            if json.loads(manifest.read_text(encoding="utf-8")).get("pipeline_complete"):
                return {"path": str(manifest), "written": False,
                        "why": "манифест прогона уже полный (pipeline_complete=true)"}
        except (json.JSONDecodeError, OSError):
            pass
    tool = CASE_ROOT / "tools" / "write_run_manifest.py"
    #: Датасет передаётся **через симлинк кейса** (`datasets/...`), а не абсолютным
    #: путём сетевого диска: `rel()` считает abspath (не resolve), поэтому путь
    #: остаётся в словаре кейса и в манифесте выходит относительным — как у цепочки.
    dataset_via_case = root / "datasets" / "tok" / Path(spec["corpus"]["cache_host"]).name
    cmd = [sys.executable, str(tool), "--run-dir", str(d),
           "--dataset", str(dataset_via_case), "--base-model", MODEL,
           "--pipeline", str(d / PIPELINE_COPY_NAME), "--seed", str(SEED),
           "--image", args.image, "--stages", "cpt=running",
           #: Пути — относительно сетевого диска, как в манифесте цепочки: AD-2/C-012
           #: требуют относительных путей, а абсолютный путь на стенде в кейсе не
           #: разрешается ни во что.
           "--relative-to", str(root),
           "--run-version", f"tools/run_mix_lr_calib.py@{_runner_sha12()}",
           "--hyperparams", f"replay_share_pct={spec['replay']}",
           "--hyperparams", f"peak_lr_scale={spec['lr_scale']}",
           "--hyperparams", f"corpus={spec['corpus']['label'].split(' ')[0]}",
           "--force"]
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        return {"path": str(manifest), "written": False,
                "why": f"write_run_manifest.py вернул {p.returncode}: {p.stderr.strip()[:200]}"}
    return {"path": str(manifest), "written": True, "status": "cpt=running"}


def ship(host: str, d: Path, stand_dir: str) -> dict:
    """Доставка файлов прогона на стенд со сверкой sha256 (что исполнялось — то и лежит)."""
    rc, _, err = S.ssh(host, f"mkdir -p {stand_dir}/logs {stand_dir}/var/status "
                             f"{stand_dir}/checkpoints", timeout=60)
    if rc != 0:
        return {"ok": False, "detail": f"каталог не создан: {err.strip()}"}
    shipped, mismatched = {}, []
    for path in sorted(d.iterdir()):
        if path.is_dir():
            continue
        data = path.read_bytes()
        rc, err = S.ssh_put(host, f"{stand_dir}/{path.name}", data)
        if rc != 0:
            return {"ok": False, "detail": f"{path.name}: доставка не удалась: {err.strip()}"}
        rc2, out2, _ = S.ssh(host, f"sha256sum {stand_dir}/{path.name} | cut -d' ' -f1")
        local = S.sha256_bytes(data)
        if rc2 != 0 or out2.strip() != local:
            mismatched.append(path.name)
        shipped[path.name] = local
    return {"ok": not mismatched, "sha256": shipped, "mismatched": mismatched,
            "stand_dir": stand_dir,
            "detail": "все файлы доставлены, sha256 совпал" if not mismatched
                      else f"расхождение sha256: {', '.join(mismatched)}"}


def chain_command(arm: str, ts: str, stand_dir: str, args) -> str:
    """Строка запуска цепочки — одна и та же для ручного повтора и для tmux."""
    name = run_id(arm, ts)
    spec = arm_spec(arm)
    corpus = spec["corpus"]
    return " ".join([
        f"bash {stand_dir}/pilot_chain.sh",
        f"--run-dir {stand_dir}",
        f"--ctr-run-dir {CTR_RUNS}/{name}",
        f"--stages-file {stand_dir}/stages.tsv",
        f"--exp-base {name}",
        f"--ctr-prefix laguna-{run_prefix(arm)}-{ts}-{arm}",
        f"--shared {STAND_SHARED}",
        f"--experiments /home/user/experiments",
        f"--pipeline {stand_dir}/{PIPELINE_COPY_NAME}",
        f"--pipeline-ctr {CTR_RUNS}/{name}/{PIPELINE_COPY_NAME}",
        f"--cpt-data {corpus['txt_ctr']}",
        f"--dataset {corpus['cache_host']}",
        f"--runner-name tools/run_mix_lr_calib.py",
        f"--extra-hyperparam replay_share_pct={spec['replay']}",
        f"--extra-hyperparam peak_lr_scale={spec['lr_scale']}",
        f"--extra-hyperparam pipeline_base_sha256={_pipeline_base_sha(arm, ts)}",
        f"--extra-hyperparam corpus={corpus['label'].split(' ')[0]}",
        f"--guard {stand_dir}/check_resource_owner.sh",
        f"--safe-start {SAFE_START}",
        f"--sampler {stand_dir}/smoke_mem_sampler.sh",
        f"--sampler-seconds {args.sampler_seconds}",
        f"--storm-gap {STORM_GAP}",
        f"--manifest-tool {stand_dir}/write_run_manifest.py",
        f"--image {args.image}",
        f"--glm-env {GLM_ENV}",
        f"--nvrm-log {NVRN_LOG}",
        f"--runner-sha12 {_runner_sha12()}",
        f"--model {MODEL}",
        f"--seed {SEED}",
        f"--max-len {MAX_LEN}",
        f"--max-samples {MAX_SAMPLES}",
        f"--peak-lr-scale {spec['lr_scale']}",
        f"--attn flex",
        # GEN-EVAL пайплайна выключен намеренно: он берёт за «base» шаг 50 (дефект
        # подтверждён в S3k), а меряет только v1. Замер — внешняя проба
        # tools/calib_ppl_probe.py на чекпойнтах.
        f"--cpt-gen-eval-every 0",
        f"--mem-cap 100g",
        f"--stall-minutes {args.stall_minutes}",
    ])


def _pipeline_base_sha(arm: str, ts: str) -> str:
    p = arm_dir(arm, ts) / "pipeline_patch.json"
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))["pipeline_base_sha256"]
    return "?"


def _runner_sha12() -> str:
    return S.sha256_bytes(Path(__file__).read_bytes())[:12]


def launch_tmux(host: str, session: str, cmd: str, log: str) -> dict:
    tmux_cmd = (f"tmux kill-session -t {session} 2>/dev/null; "
                f"tmux new-session -d -s {session} \"{cmd} >> {log} 2>&1\"")
    rc, out, err = S.ssh(host, tmux_cmd, timeout=120)
    if rc != 0:
        return {"ok": False, "detail": f"tmux не поднял сессию: {err.strip() or out.strip()}"}
    return {"ok": True, "tmux_command": tmux_cmd, "log": log, "session": session}


def chain_status(host: str, stand_dir: str) -> str:
    rc, out, _ = S.ssh(host, f"cat {stand_dir}/var/chain.status 2>/dev/null || echo none")
    return out.strip() or "none"


def wait_chain(host: str, stand_dir: str, session: str, timeout_sec: int,
               poll: int = 30) -> dict:
    """Ждать завершения цепочки по файлу `var/chain.status`, а не по tmux-сессии.

    Сессия закрывается раньше, чем цепочка пишет статус (и наоборот — при wedge
    сессия живёт, а статуса нет), поэтому источник истины — файл, а `tmux
    has-session` только ускоряет выход из ожидания.
    """
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout_sec:
        st = chain_status(host, stand_dir)
        if st != last:
            print(f"  [{int(time.time() - t0)} с] chain.status = {st}", flush=True)
            last = st
        if st in ("done", "failed", "precondition_failed", "paused"):
            return {"status": st, "seconds": round(time.time() - t0, 1)}
        rc, _, _ = S.ssh(host, f"tmux has-session -t {session} 2>/dev/null")
        if rc != 0 and st == "none":
            return {"status": "session_gone", "seconds": round(time.time() - t0, 1)}
        time.sleep(poll)
    return {"status": "timeout", "seconds": round(time.time() - t0, 1)}


def fetch(host: str, stand_dir: str, d: Path) -> dict:
    """Забрать в кейс малые артефакты прогона; чекпойнты остаются на сетевом диске."""
    got, missed = [], []
    #: `var/STOPPED` попадёт в копию, только если цепочка действительно встала на
    #: стоп-условии; его отсутствие — не пропажа, а «стопа не было».
    files = ["run_manifest.json", "pipeline_run_manifest.json", "calib_params.json",
             "stages.tsv", "var/chain.status", "var/STOPPED"]
    for name in files:
        rc, data, err = S.ssh_get(host, f"{stand_dir}/{name}", timeout=180)
        if rc != 0:
            missed.append(name)
            continue
        out = d / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        got.append({"path": str(out.relative_to(CASE_ROOT)), "bytes": len(data),
                    "sha256": S.sha256_bytes(data)})
    for sub in ("logs",):
        rc, out, _ = S.ssh(host, f"ls {stand_dir}/{sub} 2>/dev/null")
        for name in [x for x in out.split() if x]:
            rc, data, err = S.ssh_get(host, f"{stand_dir}/{sub}/{name}", timeout=300)
            if rc != 0:
                missed.append(f"{sub}/{name}")
                continue
            p = d / sub / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            got.append({"path": str(p.relative_to(CASE_ROOT)), "bytes": len(data),
                        "sha256": S.sha256_bytes(data)})
    for name in ("mem_cpt.jsonl",):
        rc, data, err = S.ssh_get(host, f"{stand_dir}/{name}", timeout=300)
        if rc == 0:
            (d / name).write_bytes(data)
            got.append({"path": str(d / name), "bytes": len(data),
                        "sha256": S.sha256_bytes(data)})
        else:
            missed.append(name)
    return {"fetched": got, "missing": missed}


def link_checkpoints(d: Path, stand_dir: str) -> dict:
    """`runs/<id>/checkpoints` — симлинк на сетевой диск (C-011: копий весов нет)."""
    link = d / "checkpoints"
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        shutil.rmtree(link)
    link.symlink_to(stand_dir + "/checkpoints")
    return {"symlink": str(link), "target": stand_dir + "/checkpoints",
            "resolves": str(link.resolve())}


# ─────────────────────────── анализ траектории ───────────────────────────────

def read_trace(path: Path) -> list[dict]:
    out = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.sort(key=lambda r: r["step"])
    #: Повтор стадии цепочкой (0x51 → storm_gap → повтор) резюмится **внутри**
    #: пайплайна и дописывает траекторию с шага возобновления. Для анализа нужен
    #: один ряд: на каждый шаг — последняя запись, а не сумма дублей.
    dedup: dict[int, dict] = {}
    for row in out:
        dedup[int(row["step"])] = row
    return [dedup[k] for k in sorted(dedup)]


def thirds(values: list[float]) -> dict:
    """Тренд по третям — вспомогательный сигнал (ADR-022 п.4), не основание."""
    if len(values) < 3:
        return {"n": len(values), "means": None, "delta": None}
    k = len(values) // 3
    parts = [values[:k], values[k:2 * k], values[2 * k:]]
    means = [statistics.fmean(p) for p in parts if p]
    return {"n": len(values), "third_means": [round(m, 6) for m in means],
            "delta_first_last": round(means[-1] - means[0], 6),
            "monotone_down": all(means[i] > means[i + 1] for i in range(len(means) - 1)),
            "boundaries": [len(parts[0]), len(parts[0]) + len(parts[1])]}


def step_seconds(trace: list[dict]) -> dict:
    """Время шага из таймстемпов траектории (свой замер, а не разбор лога)."""
    ts = [r.get("t") for r in trace if isinstance(r.get("t"), (int, float))]
    if len(ts) < 2:
        return {"n": len(ts), "mean": None, "max": None}
    d = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    return {"n": len(d), "mean": round(statistics.fmean(d), 4),
            "median": round(statistics.median(d), 4), "max": round(max(d), 4)}


def parse_peak_mem(path: Path) -> dict:
    """Пик занятой unified-памяти и минимум доступной — из сэмплера стенда."""
    if not path.is_file():
        return {"samples": 0}
    used_kb, avail_kb = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "mem_total_kb" in r and "mem_available_kb" in r:
            used_kb.append(r["mem_total_kb"] - r["mem_available_kb"])
            avail_kb.append(r["mem_available_kb"])
    if not used_kb:
        return {"samples": 0}
    #: «Занято» на GB10 включает платформенные контейнеры llm-platform-* (они не
    #: трогаются), поэтому рядом с абсолютным пиком даётся прирост над первым
    #: сэмплом: порог AD-5 считается по **свободной** памяти, а не по этому числу.
    return {"samples": len(used_kb),
            "peak_used_gb": round(max(used_kb) / 1024 / 1024, 2),
            "baseline_used_gb": round(used_kb[0] / 1024 / 1024, 2),
            "peak_delta_gb": round((max(used_kb) - used_kb[0]) / 1024 / 1024, 2),
            "min_available_gb": round(min(avail_kb) / 1024 / 1024, 2)}


def parse_peak_lr(path: Path) -> float | None:
    """Пиковый LR и число параметров — из строки старта CPT в логе стадии."""
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "peak_lr=" in line:
            try:
                return float(line.split("peak_lr=")[1].split()[0])
            except (IndexError, ValueError):
                return None
    return None


# ─────────────────────────── шаги раннера ────────────────────────────────────

def do_prepare(args) -> int:
    ts = args.ts or datetime.now().strftime("%Y%m%d-%H%M")
    for arm in (args.arms or ARM_ORDER):
        info = prepare_arm(arm, ts, force=args.force)
        print(f"подготовлен {info['run_dir'].name}")
    print(f"ts={ts}")
    return EXIT_OK


def do_launch(args) -> int:
    arm, ts = args.arm, args.ts
    d = arm_dir(arm, ts)
    if not d.is_dir():
        print(f"NOT-VERIFIED: нет каталога прогона {d}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    name = run_id(arm, ts)
    stand_dir = f"{STAND_RUNS}/{name}"
    sh = ship(args.host, d, stand_dir)
    if not sh["ok"]:
        print(f"ОТКАЗ доставки: {sh['detail']}", file=sys.stderr)
        return EXIT_FAIL
    (d / "ship.json").write_text(json.dumps(sh, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
    write_launch_manifest(d, arm, args)
    cmd = chain_command(arm, ts, stand_dir, args)
    session = f"{run_prefix(arm)}-{ts}-{arm}"
    res = launch_tmux(args.host, session, cmd, f"{stand_dir}/logs/chain.log")
    if not res["ok"]:
        print(f"ОТКАЗ запуска: {res['detail']}", file=sys.stderr)
        return EXIT_FAIL
    (d / "chain_command.txt").write_text(cmd + "\n", encoding="utf-8")
    print(f"tmux-сессия: {session}")
    print(f"контейнер:   laguna-{run_prefix(arm)}-{ts}-{arm}-cpt")
    print(f"лог цепочки: {stand_dir}/logs/chain.log")
    return EXIT_OK


def do_wait(args) -> int:
    name = run_id(args.arm, args.ts)
    stand_dir = f"{STAND_RUNS}/{name}"
    session = f"{run_prefix(args.arm)}-{args.ts}-{args.arm}"
    res = wait_chain(args.host, stand_dir, session, args.timeout, args.poll)
    print(json.dumps(res, ensure_ascii=False))
    return EXIT_OK if res["status"] == "done" else EXIT_FAIL


def do_fetch(args) -> int:
    name = run_id(args.arm, args.ts)
    d = arm_dir(args.arm, args.ts)
    stand_dir = f"{STAND_RUNS}/{name}"
    res = fetch(args.host, stand_dir, d)
    res["checkpoints"] = link_checkpoints(d, stand_dir)
    (d / "fetch.json").write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
    print(f"забрано файлов: {len(res['fetched'])}; нет: {res['missing']}")
    print(f"чекпойнты: {res['checkpoints']['symlink']} → {res['checkpoints']['target']}")
    return EXIT_OK


def ckpt_path_for_step(run: str, step: int, exists=None, runs_root: str | None = None) -> str | None:
    """Файл состояния шага `step` руки `run` или None, если его нет.

    Два имени — не вкусовщина. Руки 2–4 пишут точки замера именем
    `calib_checkpoint_<N>.pt` (его не трогает ретенция пайплайна), а рука 25-0.7
    отработала 16.09 до этой правки со штатным `checkpoint_<N>.pt` — и её точку
    шага 500 ретенция удалила. Разрешение имён живёт здесь, а не в плане пробы,
    чтобы «нет файла» был одним и тем же ответом и у плана, и у разбора.

    Шаг `STEPS` (2000) — это `checkpoint_final.pt`: цикл CPT идёт по шагам 0…1999
    и завершается его записью, то есть состоянием после 2000 шагов оптимизатора.
    """
    exists = exists or os.path.isfile
    base = f"{runs_root or STAND_RUNS}/{run}/checkpoints"
    if step >= STEPS:
        names = [FINAL_CKPT_NAME, CALIB_CKPT_FMT.format(step=step)]
    else:
        names = [CALIB_CKPT_FMT.format(step=step), LEGACY_CKPT_FMT.format(step=step)]
    for name in names:
        if exists(f"{base}/{name}"):
            return f"{base}/{name}"
    return None


def probe_plan(ts: str, arms=None, exists=None, runs_root: str | None = None) -> dict:
    """Состояния пробы PPL: база, 4 чекпойнта каждой руки, снова база (контроль).

    Пропущенная точка замера не исчезает молча: она попадает в `gaps` с причиной
    и уходит в отчёт пробы (`probe_plan.json`) и в evidence. Отказ пробы остаётся
    за **решающей** точкой (шаг `STEPS`): без неё вердикт по потолку считать не на
    чем, и проба не запускается вовсе.
    """
    states = [("base", "base")]
    gaps: list[dict] = []
    for arm in (arms or ARM_ORDER):
        name = run_id(arm, ts)
        for step in CKPT_POINTS:
            path = ckpt_path_for_step(name, step, exists=exists, runs_root=runs_root)
            if path is None:
                gaps.append({
                    "arm": arm, "step": step, "run": name, "decisive": step >= STEPS,
                    "reason": "нет файла состояния: ни "
                              + CALIB_CKPT_FMT.format(step=step) + ", ни "
                              + (FINAL_CKPT_NAME if step >= STEPS
                                 else LEGACY_CKPT_FMT.format(step=step))
                              + f" в {runs_root or STAND_RUNS}/{name}/checkpoints",
                })
                continue
            states.append((f"{name}-c{step}", f"ckpt:{path}"))
    #: Вакуумная точка: те же базовые веса, снятые ПОСЛЕ всех чекпойнтов. Если
    #: загрузка чекпойнта оставляет след в модели, эта точка разойдётся с `base`,
    #: и проба откажет, а не выдаст чужие числа как «базу».
    states.append(("base_untouched", "base"))
    return {"states": states, "gaps": gaps,
            "pipeline": f"runs/{run_id((arms or ARM_ORDER)[0], ts)}/{PIPELINE_COPY_NAME}"}


#: Проба PPL гоняется **в контейнере стенда**, а не на локальной машине: логиты
#: слоя `lm_head` — это [batch, 1024, 151671] в fp32 (≈2.5 ГБ на батч), и на
#: 16-гигабайтной карте замер падает в OOM, если рядом идёт чужая GPU-нагрузка
#: (факт 16.09: `logits.float()` не получил 2.31 ГиБ при чужих 4.18 ГиБ).
#: На стенде 121 ГБ unified-памяти, чекпойнты лежат локально, а нагрузка — только
#: эта. Методика та же: та же функция `measure`, та же загрузка состояния.
PROBE_SHIPPED = ["tools/calib_ppl_probe.py", "tools/ppl_probe.py",
                 "evidence/ppl-baseline-v1v2.json"]


def do_probe(args) -> int:
    ts = args.ts
    arms = args.arms or ARM_ORDER
    plan = probe_plan(ts, arms=arms)
    decisive = [g for g in plan["gaps"] if g["decisive"]]
    if decisive:
        print("NOT-VERIFIED: нет решающих точек замера (шаг "
              f"{CKPT_POINTS[-1]}): " + ", ".join(f"{g['arm']}:{g['reason']}" for g in decisive),
              file=sys.stderr)
        return EXIT_NOT_VERIFIED
    if plan["gaps"]:
        #: Пропуск не молчит: он печатается здесь, ложится в probe_plan.json и
        #: уходит в evidence (`ppl_gaps`). Числа по этому шагу не «добираются»
        #: ничем — сетка остаётся с дырой, названной вслух.
        print("ВНИМАНИЕ: точек замера нет — " + "; ".join(
            f"{g['arm']} шаг {g['step']}" for g in plan["gaps"]), file=sys.stderr)
    pipe = CASE_ROOT / plan["pipeline"]
    if not pipe.is_file():
        print(f"NOT-VERIFIED: нет копии пайплайна прогона: {pipe}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    ppl_name = ppl_run_name(ts, arms)
    d = CASE_ROOT / "runs" / ppl_name
    d.mkdir(parents=True, exist_ok=True)
    #: План пробы фиксируется до запуска: по нему потом видно, какие состояния
    #: просили и какие из них существовали, а не только какие получились.
    (d / "probe_plan.json").write_text(
        json.dumps({"ts": ts, "arms": arms,
                    "states": [{"name": n, "spec": s} for n, s in plan["states"]],
                    "gaps": plan["gaps"]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    stand_dir = f"{STAND_RUNS}/{ppl_name}"
    shipped = {}
    for rel in PROBE_SHIPPED:
        data = (CASE_ROOT / rel).read_bytes()
        rc, err = S.ssh_put(args.host, f"{stand_dir}/{Path(rel).name}", data)
        if rc != 0:
            print(f"ОТКАЗ доставки {rel}: {err.strip()}", file=sys.stderr)
            return EXIT_FAIL
        shipped[Path(rel).name] = S.sha256_bytes(data)

    #: Команда сохраняется рядом с отчётом: замер воспроизводится той же строкой,
    #: а не «как-то иначе» (практика каталогов прогонов кейса).
    inner = ["python3", "/workspace/shared/calib/" + ppl_name + "/calib_ppl_probe.py",
             "--case-root", "/workspace/shared",
             #: Копия пайплайна берётся у ПЕРВОЙ меряемой руки: у контрольных рук она
             #: в `ctrl-*`, у сетки — в `calib-*`, и путь не должен угадываться.
             "--pipeline", f"/workspace/shared/calib/{run_id(arms[0], ts)}/{PIPELINE_COPY_NAME}",
             #: Эталон S3h доставлен рядом с пробой (в контейнере каталога кейса нет):
             #: без явного пути проверка «base воспроизводит S3h» стала бы «не
             #: проверено», а это отказ пробы — то есть тишина вместо ответа.
             "--s3h", f"/workspace/shared/calib/{ppl_name}/ppl-baseline-v1v2.json"]
    for name, spec in plan["states"]:
        inner += ["--state", f"{name}={spec.replace(STAND_SHARED, '/workspace/shared')}"]
    inner += ["--out", f"/workspace/shared/calib/{ppl_name}/ppl.json"]
    #: Два монтирования одного сетевого диска — не избыточность. `/workspace/shared`
    #: — то, как стенд видит диск; `/home/user/gb10-shared` — путь, которым веса и
    #: кэш заданы **внутри пробы** (`ppl_probe.py`: `BASE_WEIGHTS_CANDIDATES`,
    #: «веса — рядом с токенизатором», ADR-004). Без второго монтирования проба в
    #: контейнере не находит базовые веса и возвращает NOT-VERIFIED — то есть
    #: отказывает не из-за состояния модели, а из-за раскладки путей. Правильнее
    #: показать контейнеру те же пути, а не переписывать список кандидатов в
    #: инструменте S3h (он — часть эталона прибора).
    docker = (f"docker run --rm --name laguna-{probe_prefix(arms)}-ppl "
              "-v /home/user/gb10-shared:/workspace/shared "
              "-v /home/user/gb10-shared:/home/user/gb10-shared "
              "-v /home/user/.cache/huggingface:/root/.cache/huggingface "
              "-e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 "
              "-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True "
              f"--gpus all --memory=100g --memory-swap=100g --ipc=host "
              f"{args.image} " + " ".join(inner))
    (d / "run_probe.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"# {('S3o: PPL чекпойнтов контрольных рук' if is_control(arms) else 'S3m: PPL чекпойнтов сетки')}"
        " — в контейнере стенда (память под логиты).\n"
        f"# Состояний: {len(plan['states'])}. Доставлено: "
        + ", ".join(f"{k}={v[:12]}" for k, v in shipped.items()) + "\n"
        f"ssh {args.host} '{docker}'\n", encoding="utf-8")
    print(f"проба на стенде: {stand_dir}, состояний {len(plan['states'])}", flush=True)
    rc, out, err = S.ssh(args.host, docker, timeout=args.probe_timeout)
    print(out.strip()[-4000:] if out else "", flush=True)
    if err.strip():
        print(err.strip()[-2000:], file=sys.stderr)
    #: Отчёт забирается в кейс **и при отказе**: проба пишет его инкрементально
    #: (состояние за состоянием), и отказ на пятнадцатом не имеет права стереть
    #: четырнадцать сделанных замеров — вместе с отчётом теряется и то, докуда
    #: проба дошла. Поэтому: сначала забрать, потом судить о коде возврата.
    rc2, data, err2 = S.ssh_get(args.host, f"{stand_dir}/ppl.json", timeout=180)
    if rc2 != 0:
        print(f"ОТКАЗ: отчёт пробы не забран: {err2.strip()}", file=sys.stderr)
        return EXIT_FAIL if rc == 0 else EXIT_FAIL
    (d / "ppl.json").write_bytes(data)
    print(f"отчёт пробы: runs/{ppl_name}/ppl.json ({len(data)} Б)")
    if rc != 0:
        print(f"ОТКАЗ пробы: ssh вернул {rc}; в кейсе — та часть отчёта, что успела "
              f"записаться (см. pending_states)", file=sys.stderr)
        man = write_probe_manifest(d, ts, arms, pipe, args, plan)
        print(f"манифест AD-2: {man['path']} (complete={man['complete']})")
        return EXIT_FAIL
    man = write_probe_manifest(d, ts, arms, pipe, args, plan)
    print(f"манифест AD-2: {man['path']} (stages={man['stages']}, complete={man['complete']})")
    return EXIT_OK


def sets_digest(set_paths: list[Path]) -> str:
    """sha256 по содержимому наборов пробы, склеенному в порядке имён наборов.

    Один `dataset_sha256` в манифесте AD-2, а меряется проба на четырёх наборах:
    сводный хеш — это хеш **того же материала**, а не выдуманное число, и способ
    его счёта назван здесь, чтобы его можно было пересчитать. Хеши наборов
    поимённо лежат рядом, в `datasets_extra`.
    """
    import hashlib
    h = hashlib.sha256()
    for path in set_paths:
        h.update(path.read_bytes())
    return h.hexdigest()


def write_probe_manifest(d: Path, ts: str, arms: list[str], pipe: Path, args,
                         plan: dict) -> dict:
    """Манифест AD-2 для каталога пробы: он тоже доказательство, а не «не прогон».

    Собирается **из отчёта пробы**, а не пересчётом по второму разу: хеши пайплайна
    и наборов уже посчитаны там, и второй счёт — это второе место, которое может
    разойтись с первым. Стадия у пробы одна (`ppl-probe`), `pipeline_complete` —
    «все запланированные состояния измерены», по собственному полю отчёта.
    """
    report = json.loads((d / "ppl.json").read_text(encoding="utf-8"))
    inst = report.get("instrument", {})
    sets = report.get("sets", {})
    set_paths = []
    extra = {}
    for name in sorted(sets):
        rel = sets[name]["path"]
        full = Path(STAND_SHARED) / rel if not rel.startswith("/") else Path(rel)
        if full.is_file():
            set_paths.append(full)
        extra[name] = {"path": rel, "sha256": sets[name]["sha256"],
                       "docs_kept": sets[name].get("docs_kept")}
    manifest = {
        "dataset_path": "datasets/",
        "dataset_sha256": (sets_digest(set_paths) if set_paths else None),
        "dataset_note": "проба меряет не корпус, а состояния чекпойнтов на четырёх "
                        "eval-наборах v1/v2; dataset_sha256 — sha256 по их содержимому, "
                        "склеенному в порядке имён наборов (datasets_extra — поимённо)",
        "base_model_id": MODEL,
        "pipeline_path": f"calib/{ppl_run_name(ts, arms)}/{Path(inst.get('pipeline', '')).name}",
        "pipeline_version": f"{Path(inst.get('pipeline', '?')).name}@{inst.get('pipeline_sha256', '?')[:12]}",
        "pipeline_sha256": inst.get("pipeline_sha256"),
        "image": args.image,
        "seed": SEED,
        "stages": [{"name": "ppl-probe", "status": "done"}],
        "pipeline_complete": bool(report.get("pipeline_complete", report.get("complete"))),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_version": f"tools/run_mix_lr_calib.py@{_runner_sha12()}",
        "datasets_extra": extra,
        "hyperparameters": {
            "measurement": ("PPL чекпойнтов контрольных рук S3o (C1 — чистый протокол, "
                            "C2 — низкий LR)" if is_control(arms)
                            else "PPL чекпойнтов сетки replay × peak_lr_scale (S3m)"),
            "device": inst.get("device"), "dtype": inst.get("dtype"),
            "batch": inst.get("batch"), "max_len": inst.get("max_len"),
            "tokenizer_setup": inst.get("tokenizer_setup"),
            "base_weights": inst.get("base_weights"),
            "arms": ",".join(arms),
            "states_measured": ",".join(report.get("measured_states", [])),
            "states_missing": ",".join(f"{g['arm']}:c{g['step']}"
                                       for g in plan.get("gaps", [])),
            "seed_note": "проба не сэмплирует: PPL детерминирован, сид наследуется "
                         "от прогонов сетки",
        },
        "hyperparameters_source": f"отчёт пробы runs/{ppl_run_name(ts, arms)}/ppl.json "
                                  f"(instrument, sets)",
    }
    #: В каталоге лежит не только этот прогон: смоук пробы снят раньше и на другой
    #: машине, и читатель не должен принять его за часть сетки — это разные прогоны.
    if (d / SMOKE_NAME).is_file():
        manifest["files_note"] = {
            SMOKE_NAME: "смоук пробы от 16.09 08:51 (локальная машина, только наборы v1) — "
                        "не относится к этому прогону пробы; замер по нему назван в "
                        "evidence.side_observations и в вердикт не входит",
        }
    (d / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"path": f"runs/{ppl_run_name(ts, arms)}/run_manifest.json",
            "stages": [s["name"] for s in manifest["stages"]],
            "complete": manifest["pipeline_complete"]}


# ─────────────────────────── сетка одной tmux-сессией ────────────────────────

#: Драйвер сетки — инструмент кейса, а не сгенерированный на стенде текст: он
#: версионируется, тестируется и исполняется байт-в-байт тем, что лежит в кейсе.
GRID_TOOL = "tools/mix_lr_grid_chain.sh"


def grid_dir_name(ts: str, arms=None) -> str:
    """Имя каталога сетки/серии рук. `ctrl-` — у контрольных рук S3o.

    Каталоги сетки S3m остаются `calib-grid-<ts>`: переименование задним числом
    сделало бы прежние прогоны неадресуемыми по документации.
    """
    return f"{CTRL_PREFIX + '-' if is_control(arms) else ''}grid-{ts}"


def grid_stand_dir(ts: str, arms=None) -> str:
    return f"{STAND_RUNS}/{grid_dir_name(ts, arms)}"


def grid_session(ts: str, arms=None) -> str:
    return f"{probe_prefix(arms)}-grid-{ts}"


def grid_status_file(ts: str, arms=None) -> str:
    return f"{grid_stand_dir(ts, arms)}/status"


def do_chain(args) -> int:
    """Поднять **всю сетку** одной отсоединённой tmux-сессией на стенде.

    Смысл: между руками не должен стоять живой раннер. `--launch` поднимает одну
    руку и полагается на то, что раннер доживёт до следующего вызова; 16.09 он не
    дожил, и сетка осталась на 1 из 4 (инцидент hr-40). Здесь раннер делает свою
    часть — подготовить каталоги, доставить их, поднять драйвер — и возвращает
    управление, назвав идентификаторы: tmux-сессию, каталог сетки, руки, шаги,
    путь манифеста. Дальше руки идут сами, на стенде.
    """
    ts = args.ts
    arms = args.arms or ARM_ORDER
    if not arms:
        print("NOT-VERIFIED: --chain без рук", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    #: Смешанный список — отказ, а не «поехали»: у сетки и контроля разные каталоги
    #: рук, разные каталоги серии и разные вопросы, на которые они отвечают. Молча
    #: запустить их одной серией значило бы смешать калибровку долей с диагностикой
    #: причины в одном манифесте.
    if not is_control(arms) and any(a in CTRL_ARMS for a in arms):
        print("NOT-VERIFIED: смешаны руки сетки S3m и контроля S3o — запускай их "
              "разными сериями", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    grid = grid_stand_dir(ts, arms)
    arms_joined = " ".join(arms)

    # 1. Каждая рука: каталог прогона в кейсе готов, строка запуска записана в
    #    него (её же читает драйвер) и каталог доставлен со сверкой sha256.
    shipped: dict[str, dict] = {}
    for arm in arms:
        d = arm_dir(arm, ts)
        if not d.is_dir():
            print(f"NOT-VERIFIED: нет каталога прогона руки {arm}: {d}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        need = [PIPELINE_COPY_NAME, "pipeline_patch.json", "stages.tsv", "calib_params.json"]
        missing = [n for n in need if not (d / n).is_file()]
        if missing:
            print(f"NOT-VERIFIED: каталог руки {arm} не подготовлен: нет {missing}",
                  file=sys.stderr)
            return EXIT_NOT_VERIFIED
        cmd = chain_command(arm, ts, f"{STAND_RUNS}/{run_id(arm, ts)}", args)
        (d / "chain_command.txt").write_text(cmd + "\n", encoding="utf-8")
        sh = ship(args.host, d, f"{STAND_RUNS}/{run_id(arm, ts)}")
        if not sh["ok"]:
            print(f"ОТКАЗ доставки руки {arm}: {sh['detail']}", file=sys.stderr)
            return EXIT_FAIL
        (d / "ship.json").write_text(json.dumps(sh, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
        lm = write_launch_manifest(d, arm, args)
        shipped[arm] = {"ok": True, "files": len(sh["sha256"]), "mismatched": [],
                        "launch_manifest": lm}

    # 2. Драйвер — на стенд, рядом с каталогом сетки.
    tool = (CASE_ROOT / GRID_TOOL).read_bytes()
    rc, _, err = S.ssh(args.host, f"mkdir -p {grid}", timeout=60)
    if rc != 0:
        print(f"ОТКАЗ: каталог сетки не создан: {err.strip()}", file=sys.stderr)
        return EXIT_FAIL
    rc, err = S.ssh_put(args.host, f"{grid}/mix_lr_grid_chain.sh", tool)
    if rc != 0:
        print(f"ОТКАЗ доставки драйвера: {err.strip()}", file=sys.stderr)
        return EXIT_FAIL
    rc2, out2, _ = S.ssh(args.host, f"sha256sum {grid}/mix_lr_grid_chain.sh | cut -d' ' -f1")
    if rc2 != 0 or out2.strip() != S.sha256_bytes(tool):
        print("ОТКАЗ: драйвер на стенде разошёлся с кейсом (sha256)", file=sys.stderr)
        return EXIT_FAIL

    # 3. Отсоединённый запуск: tmux-сервер живёт на стенде, поэтому смерть
    #    раннера (или обрыв ssh) прогон не задевает. `setsid` — вторая страховка:
    #    драйвер уходит в свою сессию и не получает сигналов панели.
    session = grid_session(ts, arms)
    #: Список рук — в кавычках: без них драйвер получил бы первую руку в --arms,
    #: а остальные — как «неизвестный флаг» (проверено: ранний запуск так и упал,
    #: сетка не стартовала, и это правильный отказ, а не тихая потеря рук).
    #: Префикс каталогов рук и имя каталога серии драйвер получает явно: он не
    #: угадывает их по именам рук, а исполняет то, что назвал раннер.
    inner = (f"setsid nohup bash {grid}/mix_lr_grid_chain.sh --ts {ts} "
             f"--arms '{arms_joined}' --shared {STAND_SHARED} "
             f"--run-prefix {probe_prefix(arms)} --grid-name {grid_dir_name(ts, arms)} "
             f">> {grid}/grid.log 2>&1 < /dev/null")
    rc, out, err = S.ssh(args.host,
                         f"tmux kill-session -t {session} 2>/dev/null; "
                         f"tmux new-session -d -s {session} \"{inner}\"", timeout=120)
    if rc != 0:
        print(f"ОТКАЗ запуска сетки: {err.strip() or out.strip()}", file=sys.stderr)
        return EXIT_FAIL

    # 4. Подтверждение старта — по факту, а не по коду возврата ssh. Признак
    #    старта: в `status.tsv` появилась строка первой руки. Заголовок в grid.log
    #    признаком **не** является: он печатается и тогда, когда драйвер тут же
    #    отказывается (так и вышло на первом запуске — заголовок был, сетки не
    #    было). Поэтому подтверждение — только строка стадии.
    first = arms[0]
    t0 = time.time()
    start: dict = {}
    while time.time() - t0 < 180:
        rc, rows, _ = S.ssh(args.host, f"cat {grid}/status.tsv 2>/dev/null || true")
        rc2, st, _ = S.ssh(args.host, f"cat {grid}/status 2>/dev/null || echo running")
        if any(l.split("\t")[1:2] == [first] for l in rows.splitlines() if "\t" in l):
            start = {"row_first_arm": [l for l in rows.splitlines() if first in l][:2],
                     "seconds": round(time.time() - t0, 1)}
            break
        if st.strip() in ("failed", "not_verified"):
            _, tail, _ = S.ssh(args.host, f"tail -5 {grid}/grid.log 2>/dev/null")
            print(f"ОТКАЗ: драйвер сетки завершился со статусом {st.strip()} до старта руки "
                  f"{first}; хвост grid.log:\n{tail}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        time.sleep(5)
    _, out, _ = S.ssh(args.host, f"tmux ls 2>/dev/null | grep {session} || true")
    _, cont, _ = S.ssh(args.host,
                       f"docker ps --format '{{{{.Names}}}}' | grep '^laguna-{probe_prefix(arms)}-' || true")
    _, head, _ = S.ssh(args.host, f"head -20 {grid}/grid.log 2>/dev/null || true")

    report = {
        "ts": ts, "arms": arms, "grid_dir": grid, "session": session,
        "launched_command": inner, "driver": {"path": GRID_TOOL,
                                              "sha256": S.sha256_bytes(tool)},
        "shipped": shipped,
        "start": start if start else {"row_first_arm": None,
                                      "reason": f"строка руки {first} не появилась в "
                                                f"status.tsv за 180 с"},
        "tmux": out.strip(), "containers": cont.strip().split(),
        "grid_log_head": head.strip().splitlines(),
        "steps_per_arm": STEPS, "ckpt_points": CKPT_POINTS,
        "manifest_hint": f"в каталоге каждой руки — run_manifest.json (AD-2), "
                         f"плюс манифест серии в runs/{grid_dir_name(ts, arms)}/",
    }
    d = CASE_ROOT / "runs" / grid_dir_name(ts, arms)
    d.mkdir(parents=True, exist_ok=True)
    (d / "grid_launch.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not start:
        return EXIT_NOT_VERIFIED
    return EXIT_OK


def grid_progress(host: str, ts: str, arms=None) -> dict:
    """Состояние серии: строки status.tsv, статус текущей руки и её прогресс."""
    grid = grid_stand_dir(ts, arms)
    _, rows, _ = S.ssh(host, f"cat {grid}/status.tsv 2>/dev/null || true")
    _, done, _ = S.ssh(host, f"cat {grid}/status 2>/dev/null || echo running")
    _, tmx, _ = S.ssh(host, f"tmux ls 2>/dev/null | grep {grid_session(ts, arms)} || true")
    arms = []
    for line in rows.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            arms.append({"at": parts[0], "arm": parts[1], "status": parts[2], "run": parts[3]})
    #: Текущая рука — последняя **незавершённая** строка, а не последняя вообще:
    #: после строки `start` следующая строка появляется только по итогу руки, и
    #: между ними прогресс надо брать у неё, а не у закончившейся.
    pend = [a for a in arms if a["status"] == "start"]
    cur = (pend or arms or [{}])[-1].get("arm")
    prog = None
    if cur:
        run = run_id(cur, ts)
        _, out, _ = S.ssh(host,
                          f"cat {STAND_RUNS}/{run}/var/chain.status 2>/dev/null || echo none; "
                          f"tail -1 {STAND_RUNS}/{run}/logs/loss_trace.jsonl 2>/dev/null | "
                          f"sed -n 's/.*\"step\": *\\([0-9]*\\).*/\\1/p'; "
                          f"ls {STAND_RUNS}/{run}/checkpoints 2>/dev/null | tr '\\n' ' '")
        lines = out.splitlines()
        prog = {"arm": cur, "chain_status": (lines[0] if lines else "none"),
                "last_step": (lines[1] if len(lines) > 1 and lines[1].strip() else None),
                "checkpoints": (lines[2].split() if len(lines) > 2 else [])}
    return {"grid": done.strip() or "running", "tmux": tmx.strip(),
            "rows": arms, "current": prog}


def do_grid_wait(args) -> int:
    """Ждать сетку, печатая **изменения** состояния (не поток одинаковых строк).

    Ожидание ограничено `--timeout`: сетка идёт часами, и вызов, который «просто
    ждёт», не отличим от зависшего. Прогресс печатается инкрементально — по нему
    видно, что прогон жив, а не молчит (требование дельты).
    """
    ts = args.ts
    arms = args.arms or ARM_ORDER
    t0 = time.time()
    last = None
    while time.time() - t0 < args.timeout:
        st = grid_progress(args.host, ts, arms)
        key = json.dumps(st, ensure_ascii=False, sort_keys=True)
        if key != last:
            cur = st.get("current") or {}
            print(f"[{int(time.time() - t0)} с] сетка={st['grid']} "
                  f"рука={cur.get('arm')} цепочка={cur.get('chain_status')} "
                  f"шаг={cur.get('last_step')} чекпойнтов={len(cur.get('checkpoints') or [])}",
                  flush=True)
            last = key
        if st["grid"] in ("done", "failed", "not_verified"):
            print(json.dumps(st, ensure_ascii=False, indent=2))
            return EXIT_OK if st["grid"] == "done" else EXIT_FAIL
        time.sleep(args.poll)
    print(f"таймаут ожидания сетки ({args.timeout} с); последнее состояние:")
    print(json.dumps(grid_progress(args.host, ts, arms), ensure_ascii=False, indent=2))
    return EXIT_NOT_VERIFIED


def do_grid_fetch(args) -> int:
    """Забрать в кейс логи сетки, дописать манифест AD-2 и разобрать статусы рук."""
    ts = args.ts
    arms = args.arms or ARM_ORDER
    grid = grid_stand_dir(ts, arms)
    d = CASE_ROOT / "runs" / grid_dir_name(ts, arms)
    d.mkdir(parents=True, exist_ok=True)
    got, missed = [], []
    for name in ("grid.log", "status.tsv", "status"):
        rc, data, err = S.ssh_get(args.host, f"{grid}/{name}", timeout=300)
        if rc != 0:
            missed.append(name)
            continue
        (d / name).write_bytes(data)
        got.append({"path": f"runs/{grid_dir_name(ts, arms)}/{name}", "bytes": len(data),
                    "sha256": S.sha256_bytes(data)})
    rows = (d / "status.tsv").read_text(encoding="utf-8") if (d / "status.tsv").is_file() else ""
    statuses = {}
    for line in rows.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and parts[2] in ("done", "failed", "timeout", "paused",
                                            "precondition_failed", "missing"):
            statuses[parts[1]] = parts[2]
    man = write_grid_manifest(d, ts, arms, args, statuses)
    print(f"забрано: {[g['path'] for g in got]}; нет: {missed}")
    print(f"манифест сетки: {man['path']} (стадии={man['stages']}, complete={man['complete']})")
    return EXIT_OK


def write_grid_manifest(d: Path, ts: str, arms: list[str], args, statuses: dict) -> dict:
    """Манифест AD-2 для каталога сетки: он тоже доказательство, а не «не прогон».

    «Данные» сетки — кэши корпуса её рук (25 % replay и 50 %): сводный sha256 по
    их содержимому, поимённо — в `datasets_extra`. Стадии — руки: их статусы
    берутся из `status.tsv`, который пишет драйвер, а не пересказываются.
    """
    caches = sorted({arm_spec(a)["corpus"]["cache_host"] for a in arms})
    paths = [Path(c) for c in caches]
    present = [p for p in paths if p.is_file()]
    stages = [{"name": f"cpt:{a}", "status": statuses.get(a, "pending")} for a in arms]
    control = is_control(arms)
    manifest = {
        "dataset_path": "datasets/tok",
        "dataset_sha256": sets_digest(present) if present else None,
        "dataset_note": ("контрольные руки обучаются по разным корпусам: C1 — корпус "
                         "общего языка без домена, C2 — v12r; dataset_sha256 — sha256 по "
                         "их содержимому в порядке имён (datasets_extra — поимённо)"
                         if control else
                         "сетка обучает не на одном корпусе: руки 25 % идут по кэшу v12r, "
                         "руки 50 % — по кэшу v12r50; dataset_sha256 — sha256 по их "
                         "содержимому в порядке имён (datasets_extra — поимённо)"),
        "base_model_id": MODEL,
        "pipeline_path": GRID_TOOL,
        "pipeline_version": f"{GRID_TOOL}@{_runner_sha12()}",
        "pipeline_sha256": S.sha256_bytes((CASE_ROOT / GRID_TOOL).read_bytes()),
        "image": args.image,
        "seed": SEED,
        "stages": stages,
        "pipeline_complete": all(s["status"] == "done" for s in stages),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_version": f"tools/run_mix_lr_calib.py@{_runner_sha12()}",
        "datasets_extra": {Path(c).name: {"path": f"datasets/tok/{Path(c).name}",
                                          "sha256": (sha256_file(Path(c)) if Path(c).is_file() else None)}
                           for c in caches},
        "hyperparameters": {
            "grid": ("S3o, контроль: C1 (replay 100, только общий язык, lr 0.35) и "
                     "C2 (replay 25, v12r, lr 0.035)" if control
                     else "replay {25,50} × peak_lr_scale {0.7,0.35}"),
            "arms": ",".join(arms), "steps_per_arm": STEPS, "ckpt_points": ",".join(map(str, CKPT_POINTS)),
            "mode": "последовательно, одна tmux-сессия (AD-5: одна нагрузка на GB10)",
            "arm_statuses": ",".join(f"{a}={statuses.get(a, 'pending')}" for a in arms),
        },
        "hyperparameters_source": f"runs/{d.name}/status.tsv (драйвер серии)",
    }
    (d / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"path": f"runs/{d.name}/run_manifest.json",
            "stages": [s["name"] for s in stages], "complete": manifest["pipeline_complete"]}


def do_status(args) -> int:
    name = run_id(args.arm, args.ts)
    stand_dir = f"{STAND_RUNS}/{name}"
    st = chain_status(args.host, stand_dir)
    rc, out, _ = S.ssh(args.host,
                       f"tmux ls 2>/dev/null | grep {run_prefix(args.arm)} || true")
    rc2, mem, _ = S.ssh(args.host, "free -g | awk 'NR==2{print $4\" GB свободно, \"$7\" доступно\"}'")
    rc3, ck, _ = S.ssh(args.host, f"ls {stand_dir}/checkpoints 2>/dev/null | tr '\\n' ' '")
    print(json.dumps({"arm": args.arm, "chain_status": st, "tmux": out.strip(),
                      "memory": mem.strip(), "checkpoints": ck.strip()},
                     ensure_ascii=False, indent=2))
    return EXIT_OK


def do_stop_containers(args) -> int:
    """Закрыть контейнеры прогона (критерий приёмки: после работы — пусто)."""
    prefix = "laguna-(calib|ctrl)-"
    rc, out, _ = S.ssh(args.host, f"docker ps -a --format '{{{{.Names}}}} {{{{.Status}}}}' "
                                   f"| grep -E '^{prefix}' || true")
    names = [l.split()[0] for l in out.splitlines() if l.strip()]
    removed = []
    for n in names:
        S.ssh(args.host, f"docker rm -f {n} >/dev/null 2>&1 || true")
        removed.append(n)
    return {"found": names, "removed": removed}


def artifact(d: Path, arm: str, ts: str, rel: str, runs_root: Path | None = None) -> Path:
    """Файл прогона: сначала копия в кейсе, иначе — каталог прогона на сетевом диске.

    Сетевой диск виден локально, поэтому разбор не обязан ждать `--fetch`: если
    прогон ещё идёт или копия не доехала, числа берутся из первоисточника, а не
    подменяются пустыми.
    """
    local = d / rel
    if local.exists():
        return local
    if runs_root is not None:
        return arm_dir(arm, ts, runs_root) / rel
    return Path(f"{STAND_RUNS}/{run_id(arm, ts)}") / rel


def arm_report(arm: str, ts: str, ppl: dict | None, runs_root: Path | None = None) -> dict:
    """Одна рука: траектория лосса, время шага, память, PPL по чекпойтам.

    `runs_root` — корень каталогов прогонов: тесту он нужен, чтобы разобрать
    фикстуру, а не живой стенд (прогон сетки идёт часами и тестом не является).
    """
    d = arm_dir(arm, ts, runs_root)
    params = json.loads((d / "calib_params.json").read_text(encoding="utf-8"))
    trace = read_trace(artifact(d, arm, ts, "logs/loss_trace.jsonl", runs_root))
    losses = [r["loss"] for r in trace]
    lrs = [r.get("lr") for r in trace]
    rep = {
        "arm": arm, "replay": params["replay_share_pct"],
        "lr_scale": params["peak_lr_scale"],
        "corpus": params["corpus_label"],
        #: Хеш кэша корпуса — по полному файлу (AD-2; сетевой диск виден локально).
        "cpt_dataset": params["cpt_dataset_host"],
        "cpt_dataset_sha256": (sha256_file(Path(params["cpt_dataset_host"]))
                               if Path(params["cpt_dataset_host"]).is_file() else None),
        "steps_logged": len(trace),
        "loss": {"first": losses[0] if losses else None,
                 "last": losses[-1] if losses else None,
                 "min": min(losses) if losses else None,
                 "max": max(losses) if losses else None,
                 "mean": round(statistics.fmean(losses), 6) if losses else None},
        "loss_trend": thirds(losses),
        "loss_trace": {"steps": [r["step"] for r in trace],
                       "loss": [round(x, 6) for x in losses],
                       "lr": [None if x is None else round(x, 10) for x in lrs]},
        "step_sec": step_seconds(trace),
        "peak_mem": parse_peak_mem(artifact(d, arm, ts, "mem_cpt.jsonl", runs_root)),
        "peak_lr": parse_peak_lr(artifact(d, arm, ts, "logs/cpt.log", runs_root)),
        "chain_status": (artifact(d, arm, ts, "var/chain.status", runs_root).read_text().strip()
                         if artifact(d, arm, ts, "var/chain.status", runs_root).is_file() else None),
    }
    if ppl:
        states = ppl.get("states", {})
        base = states.get("base", {}).get("sets", {})
        #: Поля PPL лежат на уровне руки — ровно как в контракте отчёта
        #: (replay, lr_scale, loss_trend, ppl_v1_2000, ppl_v2_2000, ppl_trace,
        #: step_sec, peak_mem), а не вложены в подобъект.
        rep["ppl_base"] = {k: base.get(k, {}).get("ppl")
                           for k in ("v1_general", "v1_domain", "v2_general", "v2_domain")}
        #: Имя состояния — то же, что построил `probe_plan`: «<прогон>-c<шаг>».
        #: Раньше разбор искал «c<шаг>» и не находил ничего: числа пробы лежали в
        #: отчёте, а evidence выходил без них (дефект, правка 16.09).
        run = run_id(arm, ts)
        ck_root = str(runs_root) if runs_root is not None else None
        rep["ppl_trace"] = {}
        rep["ppl_gaps"] = []
        for step in CKPT_POINTS:
            st = states.get(f"{run}-c{step}", {}).get("sets")
            if st is None:
                #: Нет состояния — нет и числа: ни пустой словарь, ни ноль, ни
                #: «пропустим». Причина называется (файла нет либо проба его не
                #: застала), и это единственное место, где она видна.
                rep["ppl_gaps"].append({
                    "step": step,
                    "reason": ("нет файла состояния на сетевом диске"
                               if ckpt_path_for_step(run, step, runs_root=ck_root) is None
                               else "состояние есть, но в отчёте пробы его нет"),
                })
                continue
            rep["ppl_trace"][step] = {k: st.get(k, {}).get("ppl")
                                      for k in ("v1_general", "v1_domain",
                                                "v2_general", "v2_domain")}
        rep["ppl_state_names"] = [f"{run}-c{s}" for s in CKPT_POINTS]
        rep["ppl_ckpt_files"] = {str(s): ckpt_path_for_step(run, s, runs_root=ck_root)
                                 for s in CKPT_POINTS}
        last = rep["ppl_trace"].get(CKPT_POINTS[-1], {})
        rep["ppl_v1_2000"] = last.get("v1_general")
        rep["ppl_v2_2000"] = last.get("v2_general")
        rep["ppl_instrument"] = ppl.get("instrument", {}).get("pipeline_sha256")
        rep["ppl_checks"] = ppl.get("checks")
    return rep


def verdict(arms: list[dict]) -> dict:
    """Вердикт по факторам. Механический: отношения к базе и потолок 2× (ADR-022 п.3б).

    Числа описываются, а не проверяются на значимость: один сид статистики не даёт
    (AD-1/S4-PROTOCOL §2), поэтому здесь главные эффекты и потолок, а не выводы.
    """
    base = next((a for a in arms if a.get("ppl_base", {}).get("v1_general")), None)
    out: dict = {"ceiling_rule": "ppl_general(шаг 2000) ≤ 2× базы (ADR-022 п.3б)"}
    if base is None:
        #: Поле названо `verdict_status`, а не `status`: у отчёта есть свой
        #: обязательный `status` (контракт дельты), и два разных `status` в одном
        #: JSON читались бы как противоречие.
        out["verdict_status"] = "NOT-VERIFIED"
        out["reason"] = "нет базового замера PPL — сравнивать не с чем"
        return out
    out["verdict_status"] = "OK"
    b1 = base["ppl_base"]["v1_general"]
    b2 = base["ppl_base"]["v2_general"]
    out["base"] = {"v1_general": b1, "v2_general": b2,
                   "ceiling_v1": round(2 * b1, 4), "ceiling_v2": round(2 * b2, 4),
                   "source": "состояние base пробы calib_ppl_probe (веса ADR-002 + настройка AD-3)"}

    rows, table = [], []
    for a in arms:
        r1 = a.get("ppl_v1_2000")
        r2 = a.get("ppl_v2_2000")
        row = {"arm": a["arm"], "replay": a["replay"], "lr_scale": a["lr_scale"],
               "ppl_v1_2000": r1, "ppl_v2_2000": r2,
               "ratio_v1": None if not r1 else round(r1 / b1, 3),
               "ratio_v2": None if not r2 else round(r2 / b2, 3),
               "under_ceiling": None if not r1 else bool(r1 <= 2 * b1 and r2 <= 2 * b2)}
        rows.append(row)
        table.append(row)

    def mean_ratio(key: str, sel_replay=None, sel_lr=None) -> float | None:
        xs = [r[key] for r in rows
              if r[key] is not None
              and (sel_replay is None or r["replay"] == sel_replay)
              and (sel_lr is None or r["lr_scale"] == sel_lr)]
        return round(statistics.fmean(xs), 3) if xs else None

    out["table"] = table
    #: Главные эффекты — разность средних отношений к базе по двум уровням фактора
    #: (усреднение по второму фактору). Знак: **минус = фактор снижает деградацию**.
    for set_name, key in (("v1", "ratio_v1"), ("v2", "ratio_v2")):
        eff = {
            "replay_25": mean_ratio(key, sel_replay=25),
            "replay_50": mean_ratio(key, sel_replay=50),
            "lr_0.7": mean_ratio(key, sel_lr=0.7),
            "lr_0.35": mean_ratio(key, sel_lr=0.35),
            "note": f"отношение ppl_general({set_name}) к базе на шаге 2000; "
                    f"меньше — меньше деградация",
        }
        if eff["replay_25"] is not None and eff["replay_50"] is not None:
            eff["replay_effect"] = round(eff["replay_50"] - eff["replay_25"], 3)
        if eff["lr_0.7"] is not None and eff["lr_0.35"] is not None:
            eff["lr_effect"] = round(eff["lr_0.35"] - eff["lr_0.7"], 3)
        out[f"effects_{set_name}_ratio"] = eff
    out["effects_reading"] = (
        "replay_effect = (доля 50 %) − (доля 25 %); lr_effect = (lr 0.35) − (lr 0.7). "
        "Отрицательное значение означает, что уровень фактора снижает деградацию "
        "общего языка; ноль — что вклад в пределах разброса одного сида не виден.")
    #: Вклад факторов «в какой мере»: доля модуля главного эффекта в сумме модулей.
    #: Это описание 2×2 без повторов — не дисперсионный анализ; при одном сиде
    #: большего из него не следует, и это сказано в `limits`.
    contrib = {}
    for set_name in ("v1", "v2"):
        eff = out.get(f"effects_{set_name}_ratio", {})
        r_e, l_e = eff.get("replay_effect"), eff.get("lr_effect")
        if r_e is None or l_e is None:
            contrib[set_name] = {"note": "нужны обе руки по обоим факторам"}
            continue
        total = abs(r_e) + abs(l_e)
        contrib[set_name] = {
            "replay_effect": r_e, "lr_effect": l_e,
            "replay_share_pct": round(100 * abs(r_e) / total, 1) if total else None,
            "lr_share_pct": round(100 * abs(l_e) / total, 1) if total else None,
            "stronger": ("replay" if abs(r_e) > abs(l_e) else "lr") if total else "ничья",
        }
    out["factor_contribution"] = contrib
    out["under_ceiling"] = [r["arm"] for r in rows if r["under_ceiling"]]
    best = min((r for r in rows if r["ratio_v1"] is not None), key=lambda r: r["ratio_v1"],
               default=None)
    out["best_arm_step2000"] = None if best is None else best["arm"]
    if best is not None:
        head = (f"наименьшая деградация общего языка к шагу 2000: "
                f"v1 ×{best['ratio_v1']} базы, v2 ×{best['ratio_v2']}")
        if out["under_ceiling"]:
            out["recommendation"] = (
                f"полный CPT вести конфигом руки {best['arm']} (replay {best['replay']} %, "
                f"peak_lr_scale {best['lr_scale']}): {head}; потолок 2× она проходит")
        else:
            #: Ни одна рука потолок не проходит. Рекомендовать «лучшую из плохих»
            #: как готовый конфиг полной стадии было бы подменой: у руки 25 %/0.7
            #: известен конец полной стадии (пилот, ×8–16), то есть порядок величин
            #: сохраняется и на 9776 шагах. Поэтому — направление плюс условие.
            out["recommendation"] = (
                f"конфиг полного CPT этой сеткой НЕ выбран: {head}, но потолок "
                f"2× базы (v1 ≤ {out['base']['ceiling_v1']}, v2 ≤ {out['base']['ceiling_v2']}) "
                f"к шагу 2000 не проходит ни одна рука — минимальное отношение ×{best['ratio_v1']}. "
                f"Направление указывает на руку {best['arm']} (replay {best['replay']} %, "
                f"peak_lr_scale {best['lr_scale']}); запуск полной стадии на ней требует "
                f"либо названного послабления критерия, либо третьего фактора — решение "
                f"владельца, а не следствие сетки")
    out["limits"] = [
        "2000 шагов ≠ 9776: вывод — направление, а не итог полной стадии (ADR-022 п.5)",
        "деградация немонотонна (в S3k пик на шаге 100, затем спад) — сравнивать шаг 2000 "
        "с шагом 2000 корректно, читать траекторию как минимум нельзя",
        "один сид: главные эффекты описательные, значимость не заявляется (AD-1, S4-PROTOCOL §2)",
    ]
    return out


#: Лог CPT пилота — единственный прогон с **тем же** корпусом и конфигом, что рука
#: 25-0.7 (v12r, 25 % replay, lr×0.7, сид 42, батч 1, 8192, flex). Лежит в кейсе
#: (доложен в S3m из каталога прогона), чтобы сравнение опиралось на файл, а не
#: на пересказ.
PILOT_LOG = CASE_ROOT / "runs" / "pilot-compact-s42-20260914-1626" / "logs" / "cpt.log"
PILOT_STEPS_RE = r"CPT step (\d+)/9776 \| loss=([\d.]+)"


#: Лосс шага 0 стенда при `lr=0` — веса нетронуты, то есть это замер базовой модели
#: на первом чанке корпуса. Якорь независимый: он снят S2-смоуком (`evidence/s2-smoke.json`,
#: `runs/smoke-20260914-0930/logs/cpt.log`) и повторён пилотом.
#: Логи печатают лосс с четырьмя знаками (`loss={:.4f}`), поэтому сверка идёт по
#: четырём знакам: побитовое равенство здесь недостижимо не из-за расхождения, а
#: из-за округления в источнике.
STAND_STEP0_LOSS = 2.0989
STEP0_DECIMALS = 4


def step0_check(arms: list[dict]) -> dict:
    """Шаг 0: у рук на v12r обязан совпасть с якорем стенда, у руки на v12r50 — нет."""
    out = {"anchor": STAND_STEP0_LOSS,
           "anchor_source": "runs/smoke-20260914-0930/logs/cpt.log + evidence/s2-smoke.json",
           "per_arm": {}}
    for a in arms:
        first = a.get("loss", {}).get("first")
        same_corpus = a.get("replay") == 25
        out["per_arm"][a["arm"]] = {
            "step0_loss": first, "corpus_is_v12r": same_corpus,
            "expected": STAND_STEP0_LOSS if same_corpus else "другой корпус — другой первый чанк",
            "matches_anchor": (round(first, STEP0_DECIMALS) == STAND_STEP0_LOSS
                               if (first is not None and same_corpus) else None),
        }
    out["note"] = ("руки на v12r (25 %) начинают с того же чанка, что смоук и пилот: "
                   "совпадение шага 0 подтверждает, что корпус, токенизация и порядок "
                   "шафла те же; рука на v12r50 обязана отличаться — это другой микс")
    return out


def pilot_comparison(trace: list[dict]) -> dict:
    """Сверка руки 25-0.7 с пилотом по шагам, которые пилот печатал (каждые 50).

    Это **не** проверка воспроизводимости: обычный режим бит-в-бит не воспроизводим
    (ADR-009/ADR-014), и лосс шага при батче 1 — лосс одного чанка, то есть величина
    с большим разбросом. Смысл сверки в другом: если бы порядок данных, корпус или
    рецепт разошлись, per-step лоссы были бы несовместны, а не «похожи».
    """
    import re as _re
    if not PILOT_LOG.is_file():
        return {"status": "NOT-VERIFIED", "reason": f"нет лога пилота: {PILOT_LOG}"}
    pilot = {}
    for line in PILOT_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _re.search(PILOT_STEPS_RE, line)
        if m:
            pilot[int(m.group(1))] = float(m.group(2))
    ours = {r["step"]: r["loss"] for r in trace}
    common = sorted(set(ours) & set(pilot))
    if len(common) < 5:
        return {"status": "NOT-VERIFIED", "reason": "мало общих шагов",
                "common": len(common)}
    diffs = [ours[s] - pilot[s] for s in common]
    absd = [abs(x) for x in diffs]
    return {
        "status": "OK",
        "what": "лосс руки 25-0.7 против лога пилота на общих шагах (пилот печатает каждые 50)",
        "pilot_log": str(PILOT_LOG.relative_to(CASE_ROOT)),
        "n_common": len(common),
        "step0_ours": ours.get(0), "step0_pilot": pilot.get(0),
        "step0_equal": (round(ours[0], STEP0_DECIMALS) == round(pilot[0], STEP0_DECIMALS)
                        if 0 in common else None),
        "mean_diff": round(statistics.fmean(diffs), 4),
        "median_diff": round(statistics.median(diffs), 4),
        "median_abs_diff": round(statistics.median(absd), 4),
        "max_abs_diff": round(max(absd), 4),
        "points": [{"step": s, "ours": round(ours[s], 4), "pilot": round(pilot[s], 4)}
                   for s in common],
        "note": "совпадение шага 0 — до четырёх знаков (логи печатают `loss={:.4f}`, "
                "траектория несёт полную точность), то есть веса и первый чанк те же; "
                "дальше траектория расходится, и это ожидаемо: обычный режим не "
                "воспроизводим бит-в-бит (ADR-009), а первые десятки шагов — область, "
                "где расхождение растёт лавинообразно",
    }


#: История GEN-EVAL пилота (стадия CPT + хвост стадии SFT в том же файле).
#: Это единственная запись того, что делает **полная** стадия на той же связке
#: (v12r, 25 % replay, lr×0.7, сид 42) — то есть ровно та рука, которую сетка
#: проверяет на 2000 шагах. Нужна затем, чтобы ограничение «2000 ≠ 9776» было
#: числом, а не оговоркой: видно, куда траектория пришла к 9776.
PILOT_HISTORY = (CASE_ROOT / "runs" / "pilot-compact-s42-20260914-1626"
                 / "general_eval_history.jsonl")
PILOT_CPT_STEPS = 9776


def pilot_long_run() -> dict:
    """Точки GEN-EVAL пилота внутри CPT — как выглядит конец полной стадии."""
    if not PILOT_HISTORY.is_file():
        return {"status": "NOT-VERIFIED", "reason": f"нет истории пилота: {PILOT_HISTORY}"}
    rows = []
    for line in PILOT_HISTORY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append(r)
    #: В файле **две** серии с своей нумерацией шагов: сначала CPT (50…9750), потом
    #: SFT, который пишет в тот же файл и начинает счёт заново (50…36500). Отбор
    #: «step ≤ 9776» смешал бы их и вернул бы значения SFT на шагах 50–9750.
    #: Поэтому берётся **первая неубывающая серия**: конец серии — там, где шаг
    #: перестал расти.
    cpt: list[dict] = []
    for r in rows:
        if cpt and r["step"] <= cpt[-1]["step"]:
            break
        if r["step"] > PILOT_CPT_STEPS:
            break
        cpt.append(r)
    if not cpt:
        return {"status": "NOT-VERIFIED", "reason": "нет точек внутри CPT"}
    wanted = [50, 100, 200, 500, 1000, 2000, 5000, 9000, 9750]
    by_step = {r["step"]: r for r in cpt}
    points = [{"step": s, "ppl_general": round(by_step[s]["ppl_general"], 3),
               "ppl_domain": round(by_step[s]["ppl_domain"], 3)}
              for s in wanted if s in by_step]
    gens = [r["ppl_general"] for r in cpt]
    return {
        "status": "OK",
        "source": str(PILOT_HISTORY.relative_to(CASE_ROOT)),
        "what": "GEN-EVAL пилота внутри его CPT (v12r, 25 % replay, lr×0.7, сид 42) — "
                "то, что делает полная стадия на связке, которую сетка проверяет на 2000 шагах",
        "cpt_steps": PILOT_CPT_STEPS, "points_total": len(cpt),
        "points": points,
        "general_min": round(min(gens), 3), "general_max": round(max(gens), 3),
        "note": "«base» в GEN-EVAL пилота — состояние шага 50, а не модель (дефект, "
                "подтверждённый в S3k), поэтому абсолютные значения сравнимы с пробой "
                "как порядок величины, а не как отношение к базе; точки после 9776 "
                "в файле относятся к стадии SFT и в разбор не берутся",
    }


#: Смоук пробы PPL (16.09, 08:51) — единственный замер точки шага 500 руки 25-0.7.
#: Файл состояния к моменту разбора уже удалён ретенцией пайплайна, но замер по
#: нему успел состояться и лежит в кейсе: терять факт вместе с файлом незачем.
SMOKE_NAME = "smoke.json"


def smoke_observation(ts: str) -> dict:
    """Контекст, а не число сетки: замер точки, которой в сетке больше нет.

    Почему это не «частичная метрика» (AD-11). Конфиг по нему не выбирается и в
    вердикт он не входит — иначе он подменял бы сетку собой. Различий с сеткой
    три, и они названы: другая машина (локальная, а не контейнер стенда), другой
    протокол (смоук до правки имён состояний), только наборы v1. Замер идёт от
    той же методики и того же базового состояния (проверка `base_vs_s3h` в самом
    смоуке — расхождение 0), поэтому он говорит о порядке величины на шаге 500 и
    ни о чём больше.
    """
    p = CASE_ROOT / "runs" / f"calib-ppl-{ts}" / SMOKE_NAME
    if not p.is_file():
        return {"status": "нет смоука пробы", "path": f"runs/calib-ppl-{ts}/{SMOKE_NAME}"}
    rep = json.loads(p.read_text(encoding="utf-8"))
    inst = rep.get("instrument", {})
    base = rep.get("states", {}).get("base", {}).get("sets", {})
    points = []
    for name, st in rep.get("states", {}).items():
        if name == "base":
            continue
        sets = st.get("sets", {})
        points.append({
            "state": name, "checkpoint": st.get("load", {}).get("checkpoint"),
            "ppl": {k: sets[k]["ppl"] for k in sorted(sets)},
            "ratio_v1_general_to_base": (
                round(sets["v1_general"]["ppl"] / base["v1_general"]["ppl"], 3)
                if "v1_general" in sets and "v1_general" in base else None),
        })
    #: Кросс-проверка независимым прибором: тот же шаг 500 руки 25-0.7 мерил
    #: GEN-EVAL **самого пилота** (внутри его CPT, «base» = шаг 50). Совпадение
    #: говорит, что число смоука — свойство состояния, а не машины, на которой его
    #: сняли. Это не делает его числом сетки: прибор другой, и в вердикт он не идёт.
    cross = {"what": "та же точка (шаг 500 руки 25-0.7), снятая GEN-EVAL пилота "
                      "в его CPT — другим прибором",
             "pilot_source": str(PILOT_HISTORY.relative_to(CASE_ROOT))}
    long_run = pilot_long_run()
    pilot500 = next((p for p in long_run.get("points", []) if p["step"] == 500), None)
    smoke_general = next((p["ppl"]["v1_general"] for p in points if "v1_general" in p["ppl"]),
                         None)
    if pilot500 and smoke_general:
        cross.update({"pilot_ppl_general_step500": pilot500["ppl_general"],
                      "smoke_ppl_general_step500": round(smoke_general, 3),
                      "rel_diff": round(abs(smoke_general - pilot500["ppl_general"])
                                        / pilot500["ppl_general"], 5),
                      "reading": "приборы сходятся — число описывает состояние, а не машину"})
    else:
        cross["reading"] = "нет точки шага 500 у пилота — сверить не с чем"
    return {
        "status": "контекст, НЕ число сетки",
        "path": f"runs/calib-ppl-{ts}/{SMOKE_NAME}",
        "what": "замер точки шага 500 руки 25-0.7, файл которой удалён ретенцией пайплайна",
        "date": rep.get("date"), "device": inst.get("device"), "dtype": inst.get("dtype"),
        "sets_measured": sorted(rep.get("sets", {})),
        "base_ppl": {k: base[k]["ppl"] for k in sorted(base)},
        "points": points,
        "cross_check_pilot_gen_eval": cross,
        "checks": rep.get("checks"),
        "why_not_grid": [
            "другая машина: локальная (RTX 4080), а сетка мерится в контейнере стенда",
            "другой протокол: смоук до правки имён состояний и до разбора сетки",
            "только наборы v1: числа v2 по этой точке не существует ни в каком виде",
            "в вердикт не входит (AD-11: частичная метрика не выбирает конфиг)",
        ],
    }


#: Отчёт сборки микса — источник хешей «до»: он снят до сетки (16.09, 08:46) и
#: лежит в кейсе. Сверка «до/после» идёт против него, а не против памяти раннера.
MIX_BUILD_REPORT = CASE_ROOT / "data" / "mix-v12r50-build.json"


def cache_integrity() -> dict:
    """Кэши корпуса до/после сетки: существующие не перезаписаны (AD-4, C-011).

    «До» — хеши из отчёта сборки микса (снят до запуска рук), «после» — файлы на
    сетевом диске сейчас. Расхождение здесь — не «дрейф», а порча чужого кэша,
    которым пользуется контур: тогда вердикт сетки недействителен.
    """
    if not MIX_BUILD_REPORT.is_file():
        return {"status": "NOT-VERIFIED", "reason": f"нет отчёта сборки {MIX_BUILD_REPORT}"}
    rep = json.loads(MIX_BUILD_REPORT.read_text(encoding="utf-8"))
    fc = rep.get("fidelity_check", {})
    items: list[dict] = []
    for name, node in (("v12r_domain_check", fc.get("reference_domain")),
                       ("v12r_tokens", fc.get("reference")),
                       ("v12r_pos", fc.get("reference_pos"))):
        if isinstance(node, dict):
            items.append({"what": name, **node})
    for w in rep.get("written", []):
        items.append({"what": "v12r50_written", **w})
    out = []
    for it in items:
        path = str(it.get("path", ""))
        host = path.replace("/workspace/shared", STAND_SHARED)
        p = Path(host)
        now = sha256_file(p) if p.is_file() else None
        out.append({"what": it["what"], "path": path, "sha256_before": it.get("sha256"),
                    "sha256_after": now,
                    "unchanged": (now is not None and now == it.get("sha256")),
                    "exists": p.is_file()})
    return {
        "status": "OK" if all(x["unchanged"] for x in out) else "РАСХОЖДЕНИЕ",
        "source": str(MIX_BUILD_REPORT.relative_to(CASE_ROOT)),
        "note": "хеши «до» сняты отчётом сборки микса до запуска рук; «после» — по файлам "
                "сейчас. Руки сетки корпус не пишут: они его только читают",
        "files": out,
        "caches_used_by_arms": {a: arm_spec(a)["corpus"]["cache_host"] for a in ARMS},
    }


def ctrl_verdict(arms: list[dict]) -> dict:
    """Вердикт по контрольным рукам S3o: потолок 2× базы и какая причина подтверждена.

    Правило то же, что у сетки (ADR-022 п.3б): `ppl_general` на шаге 2000 не выше
    **2× базы**. Отвечает вердикт не на «какая доля лучше», а на вопрос, ради
    которого руки поставлены:

    * **C1** (общий язык, домена нет, LR 0.35) — провал потолка при отсутствии
      домена означает, что язык разрушает **сам протокол**, а не доменный корпус;
      проход — что на общем тексте протокол безвреден, и причина в домене/его
      обработке.
    * **C2** (v12r, LR ×0.035) — проход означает, что причина в **LR**, и даёт
      конфиг для полного CPT; провал — что снижение LR в 10 раз не спасает и
      причина не в нём.

    Числа описываются, а не проверяются на значимость: один сид статистики не даёт
    (AD-1), поэтому здесь потолок и названные следствия, а не «доказано».
    """
    base = next((a for a in arms if a.get("ppl_base", {}).get("v1_general")), None)
    out: dict = {"ceiling_rule": "ppl_general(шаг 2000) ≤ 2× базы (ADR-022 п.3б)",
                 "question": "причина форгеттинга: протокол CPT или LR (домен исключён C1)"}
    if base is None:
        out["verdict_status"] = "NOT-VERIFIED"
        out["reason"] = "нет базового замера PPL — сравнивать не с чем"
        return out
    b1 = base["ppl_base"]["v1_general"]
    b2 = base["ppl_base"]["v2_general"]
    out["verdict_status"] = "OK"
    out["base"] = {"v1_general": b1, "v2_general": b2,
                   "ceiling_v1": round(2 * b1, 4), "ceiling_v2": round(2 * b2, 4),
                   "source": "состояние base пробы (веса ADR-002 + настройка AD-3)"}
    table = []
    for a in arms:
        r1, r2 = a.get("ppl_v1_2000"), a.get("ppl_v2_2000")
        valid = general_metric_validity(a["arm"])
        #: Потолок считается по ГОДНЫМ наборам: у руки на всём реплей-источнике
        #: набор v2 заражён её же обучением (ADR-018 исключал документы только
        #: против префикса пилота), и «прошла по v2» означало бы «прошла по тому,
        #: что учила». Заражённый набор остаётся в таблице — но не в вердикте.
        contaminated = valid["decisive_general_set"] == "v1_general"
        ok_v1 = None if r1 is None else bool(r1 <= 2 * b1)
        ok_v2 = None if (r2 is None or contaminated) else bool(r2 <= 2 * b2)
        table.append({"arm": a["arm"], "replay": a["replay"], "lr_scale": a["lr_scale"],
                      "corpus": a.get("corpus"),
                      "ppl_v1_2000": r1, "ppl_v2_2000": r2,
                      "ratio_v1": None if not r1 else round(r1 / b1, 3),
                      "ratio_v2": None if not r2 else round(r2 / b2, 3),
                      "v2_contaminated": contaminated,
                      "metrics_validity": valid,
                      "under_ceiling_v1": ok_v1,
                      "under_ceiling_v2": ok_v2,
                      "under_ceiling": (None if ok_v1 is None
                                        else bool(ok_v1 and (ok_v2 is None or ok_v2)))})
    out["table"] = table
    out["decisive_set"] = ("v1_general — у рук на всём реплей-источнике набор v2 заражён "
                           "их же обучением (см. metrics_validity)")
    by_arm = {r["arm"]: r for r in table}
    c1 = by_arm.get("C1-100-0.35")
    c2 = by_arm.get("C2-25-0.035")
    findings: list[str] = []
    if c1 and c1["under_ceiling"] is not None:
        if c1["under_ceiling"]:
            findings.append(
                "C1 (общий язык, домена нет) прошла потолок: протокол CPT язык не "
                "разрушает — причина форгеттинга в доменной части или её обработке")
            out["protocol_blamed"] = False
        else:
            findings.append(
                "C1 (общий язык, домена нет) НЕ прошла потолок: язык разрушается без "
                "всякого домена — причина в самом протоколе CPT (режим, шаги, батч, "
                "max_len), а не в доменном корпусе")
            out["protocol_blamed"] = True
    if c2 and c2["under_ceiling"] is not None:
        if c2["under_ceiling"]:
            findings.append(
                "C2 (v12r, пик LR ×0.035) прошла потолок: LR — рабочая ось, конфиг C2 "
                "годится как кандидат для полного CPT")
            out["lr_blamed"] = True
        else:
            findings.append(
                "C2 (v12r, пик LR ×0.035) НЕ прошла потолок: снижение пика LR в 10 раз "
                "не спасает — причина не в LR (или не только в нём)")
            out["lr_blamed"] = False
    out["findings"] = findings
    out["under_ceiling"] = [r["arm"] for r in table if r["under_ceiling"]]
    out["recommendation"] = (
        "составной вывод: " + "; ".join(findings) if findings
        else "чисел на шаге 2000 нет — вердикт не выносится (проверь --probe)")
    out["caveats"] = [
        "Потолок 2× базы — критерий ADR-022 п.3б, сформулированный для полной стадии; "
        "здесь он применён к шагу 2000 как промежуточной точке.",
        "C1 и C2 отличаются от рук сетки не одним фактором каждая: у C1 корпус без "
        "домена (и, как следствие, короче — 3493 чанка против 6986/9776), у C2 только "
        "пик LR. Сравнивать C1 с C2 «в лоб» нельзя — они отвечают на разные вопросы.",
        "Один сид на руку: главные эффекты описательные (AD-1, S4-PROTOCOL §2).",
        "У руки C1 (и у рук 50 % сетки S3m — их корпус v12r50 тоже берёт весь "
        "реплей-источник) набор `v2_general` заражён обучением: ADR-018 брал документы "
        "набора ИЗ ЭТОГО ЖЕ файла и исключал их только против префикса пилота (2444 "
        "чанка). Замер S3o: 200 документов из 200 лежат в реплей-источнике дословно, "
        "у 183 — непрерывное общее окно ≥20 слов, максимум 429 слов. Решающий набор "
        "для них — v1_general (0 попаданий из 24), и потолок считается по нему.",
    ]
    return out


def ctrl_corpus_integrity() -> dict:
    """Кэши контрольных рук: записанное сборщиком сверяется с тем, что на диске (AD-4).

    «До» — хеши из отчёта сборки корпуса C1 (`data/mix-ctrl100-build.json`, снят до
    запуска рук), «после» — файлы на сетевом диске сейчас. Порча кэша корпуса —
    не «дрейф», а недействительность замера руки C1.
    """
    report_path = CASE_ROOT / "data" / "mix-ctrl100-build.json"
    if not report_path.is_file():
        return {"status": "NOT-VERIFIED", "reason": f"нет отчёта сборки {report_path}"}
    rep = json.loads(report_path.read_text(encoding="utf-8"))
    out = []
    for w in rep.get("written", []):
        host = str(w["path"]).replace("/workspace/shared", STAND_SHARED)
        p = Path(host)
        now = sha256_file(p) if p.is_file() else None
        out.append({"what": p.name, "path": w["path"], "sha256_before": w["sha256"],
                    "sha256_after": now, "unchanged": now == w["sha256"], "exists": p.is_file()})
    return {
        "status": "OK" if out and all(x["unchanged"] for x in out) else "РАСХОЖДЕНИЕ",
        "source": str(report_path.relative_to(CASE_ROOT)),
        "note": "руки корпус не пишут — только читают; контрольные кэши v12r и ctrl100 "
                "лежат на сетевом диске и в контур не копируются (AD-4, C-011)",
        "files": out,
        "caches_used_by_arms": {a: arm_spec(a)["corpus"]["cache_host"] for a in CTRL_ORDER},
    }


def do_ctrl_analyze(args) -> int:
    """Evidence по контрольным рукам: числа рук, вердикт по потолку и следующее решение.

    Отдельная схема (`ctrl-arms/1`), а не `mix-lr-calibration/1`: у контроля другой
    вопрос и другой набор рук, и один файл evidence не должен отвечать сразу на два.
    """
    ts = args.ts
    arms_wanted = args.arms or CTRL_ORDER
    not_ready = _arms_ready(arms_wanted, ts)
    if not_ready:
        print(f"NOT-VERIFIED: нет каталогов контрольных рук {', '.join(not_ready)} "
              f"для ts={ts}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    ppl_name = ppl_run_name(ts, arms_wanted)
    ppl_path = CASE_ROOT / "runs" / ppl_name / "ppl.json"
    ppl = json.loads(ppl_path.read_text(encoding="utf-8")) if ppl_path.is_file() else None
    arms = [arm_report(a, ts, ppl) for a in arms_wanted]
    incomplete = [a["arm"] for a in arms
                  if a["steps_logged"] < STEPS or a.get("ppl_v1_2000") is None]
    measured = [a for a in arms if a["steps_logged"] > 0]
    status = ("complete" if not incomplete else "partial") if measured else "blocked"
    evidence = {
        "schema": "ctrl-arms/1",
        "status": status,
        "status_basis": {
            "rule": f"complete = все контрольные руки прошли {STEPS} шагов и имеют PPL на "
                    f"шаге {CKPT_POINTS[-1]}; partial = измерено меньше; blocked = не "
                    f"измерено ничего",
            "incomplete_arms": incomplete,
            "arms_measured": [a["arm"] for a in measured],
        },
        "stage": "S3o",
        "date": datetime.now(timezone.utc).isoformat(),
        "task": "ADR-022 п.1–2 — контрольные руки: чистый протокол (C1) и низкий LR (C2)",
        "design": {
            "arms": {a: {"replay_pct": arm_spec(a)["replay"],
                         "peak_lr_scale": arm_spec(a)["lr_scale"],
                         "corpus": arm_spec(a)["corpus"]["label"]} for a in CTRL_ORDER},
            "fixed": {"model": MODEL, "seed": SEED, "batch": BATCH, "max_len": MAX_LEN,
                      "attn": "flex (LAGUNA_ATTN=flex, FLEX_COMPILE=0)", "steps": STEPS,
                      "checkpoints": CKPT_POINTS},
            "instrument_ppl": "tools/calib_ppl_probe.py — методика _ppl_eval из "
                              "tools/ppl_probe.py на чекпойнтах",
            "runner": "tools/run_mix_lr_calib.py",
        },
        "runs": arms,
        "instrument_checks": {"step0_vs_known_base": step0_check(arms)},
        "verdict": ctrl_verdict(arms),
        "measurement_gaps": [{"arm": a["arm"], "step": g["step"], "reason": g["reason"]}
                             for a in arms for g in a.get("ppl_gaps", [])],
        "corpus_integrity": ctrl_corpus_integrity(),
        "assumptions": [
            "Корпус C1 (100 % общий язык) собран тем же чанкером, что v12r50, и сверен "
            "побайтово: наши первые 2444 чанка равны реплей-блоку v12r, первые 3493 — "
            "реплей-блоку v12r50 (data/mix-ctrl100-build.json, fidelity_check).",
            "Протокол рук идентичен протоколу сетки S3m: та же копия пайплайна (патч "
            "применяется тем же раннером), тот же образ, сид, батч, max_len и режим "
            "внимания; отличаются только названные факторы.",
            "C1 обучается на всём реплей-источнике (3493 чанка) — утечка общего eval в "
            "него проверена отдельным инструментом (tools/audit_ctrl_corpus_leak.py).",
            "Один сид: эффекты описательные, статистическая значимость не заявляется "
            "(AD-1, S4-PROTOCOL §2).",
        ],
        "artifacts": [
            {"path": "tools/build_ctrl_c1_corpus.py",
             "what": "сборка корпуса C1 (100 % общий язык) с побайтовой самопроверкой"},
            {"path": "data/mix-ctrl100-build.json",
             "what": "отчёт сборки корпуса C1: состав, хеши, сверка с реплей-блоками"},
            {"path": "tools/run_mix_lr_calib.py",
             "what": "раннер: контрольные руки C1/C2 тем же протоколом, что сетка S3m"},
            {"path": "evidence/ctrl-arms-analysis.json", "what": "этот файл"},
        ],
    }
    out = CASE_ROOT / "evidence" / "ctrl-arms-analysis.json"
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = CASE_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"evidence: {out}")
    v = evidence["verdict"]
    print(json.dumps({k: v[k] for k in v if k in
                      ("verdict_status", "base", "under_ceiling", "findings",
                       "recommendation", "reason")}, ensure_ascii=False, indent=2))
    return EXIT_OK


def _arms_ready(arms, ts: str) -> list[str]:
    """Руки, у которых нет каталога прогона с параметрами: разбор по ним невозможен.

    Проверка нужна потому, что `--analyze` адресуется руками: без `--arms` берутся
    руки сетки, и у контрольной дельты это дало бы разбор чужих каталогов (или
    падение на отсутствующем `calib_params.json`) вместо ответа «не туда».
    """
    return [a for a in arms
            if not (arm_dir(a, ts) / "calib_params.json").is_file()]


def do_analyze(args) -> int:
    if is_control(args.arms or []):
        return do_ctrl_analyze(args)
    ts = args.ts
    not_ready = _arms_ready(args.arms or ARM_ORDER, ts)
    if not_ready:
        print(f"NOT-VERIFIED: нет каталогов рук {', '.join(not_ready)} для ts={ts} — "
              f"укажи руки явно (--arms) или проверь метку прогона", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    ppl_path = CASE_ROOT / "runs" / f"calib-ppl-{ts}" / "ppl.json"
    ppl = json.loads(ppl_path.read_text(encoding="utf-8")) if ppl_path.is_file() else None
    arms = [arm_report(a, ts, ppl) for a in (args.arms or ARM_ORDER)]
    incomplete = [a["arm"] for a in arms
                  if a["steps_logged"] < STEPS or a.get("ppl_v1_2000") is None]
    measured = [a for a in arms if a["steps_logged"] > 0]
    status = ("complete" if not incomplete else "partial") if measured else "blocked"
    evidence = {
        "schema": "mix-lr-calibration/1",
        "status": status,
        "status_basis": {
            "rule": f"complete = все четыре руки прошли {STEPS} шагов и имеют PPL на шаге "
                    f"{CKPT_POINTS[-1]}; partial = измерено меньше; blocked = не измерено ничего",
            "incomplete_arms": incomplete,
            "arms_measured": [a["arm"] for a in measured],
        },
        "stage": "S3m",
        "date": datetime.now(timezone.utc).isoformat(),
        "task": "ADR-022 п.2 — калибровка доли replay и пика LR (сетка 2×2, 2000 шагов)",
        "design": {
            "grid": {"replay_pct": [25, 50], "peak_lr_scale": [0.7, 0.35]},
            "fixed": {"model": MODEL, "seed": SEED, "batch": BATCH, "max_len": MAX_LEN,
                      "attn": "flex (LAGUNA_ATTN=flex, FLEX_COMPILE=0)",
                      "steps": STEPS, "checkpoints": CKPT_POINTS},
            "instrument_ppl": "tools/calib_ppl_probe.py — методика _ppl_eval из tools/ppl_probe.py "
                              "на чекпойнтах; GEN-EVAL пайплайна выключен (его «base» — шаг 50, дефект S3k)",
            "runner": "tools/run_mix_lr_calib.py",
        },
        "runs": arms,
        #: Проверки прибора — то, что должно сойтись независимо от исхода сетки.
        #: Не сошлось → числа сетки не с чем сравнивать, и это видно сразу.
        "instrument_checks": {
            "step0_vs_known_base": step0_check(arms),
            "arm_25_0.7_vs_pilot": pilot_comparison(
                read_trace(artifact(arm_dir("25-0.7", ts), "25-0.7", ts,
                                    "logs/loss_trace.jsonl"))),
            "pilot_full_stage": pilot_long_run(),
        },
        "verdict": verdict(arms),
        #: Пропущенные точки замера — на уровне evidence, а не только внутри рук:
        #: сетка читается сводкой, и дыра в ней обязана быть видна сводке.
        "measurement_gaps": [{"arm": a["arm"], "step": g["step"], "reason": g["reason"]}
                             for a in arms for g in a.get("ppl_gaps", [])],
        "side_observations": [smoke_observation(ts)],
        "cache_integrity": cache_integrity(),
        "assumptions": [
            "Доля replay 50 % достигнута равными долями (3493 домена + 3493 реплея): "
            "источник реплея general_replay_ru.txt даёт ровно 3493 чанка по 8192 токена — "
            "это потолок источника, а не выбор раннера. 50 % из полного доменного пула "
            "v12r (7332) требовали бы 7332 реплей-чанков.",
            "Обе части микса v12r50 — префиксы соответствующих частей v12r: домен — первые "
            "3493 чанка того же потока (у v12r 7332), реплей — первые 3493 (у v12r 2444). "
            "Материал нового микса не «другой корпус», а подмножество того же.",
            "Рука 25 % идёт по существующему кэшу v12r — тому же, что у пилота: кэш не "
            "пересобирался и не изменялся (сверка sha256 до/после в artifacts).",
            "PPL чекпойнтов снят **в контейнере стенда** (bfloat16, batch 4, max_len 1024) "
            "той же методикой `_ppl_eval`, что S3h: на локальной машине логиты слоя "
            "`lm_head` не влезали в 16 ГБ рядом с чужой нагрузкой. Согласие прибора "
            "проверяется на состоянии `base` против S3h и на вакуумной точке "
            "`base_untouched` (instrument_checks пробы, поле checks).",
            "«Шаг 2000» — это `checkpoint_final.pt`: цикл CPT идёт по шагам 0…1999 и "
            "завершается записью состояния после 2000 шагов оптимизатора, отдельного "
            "номерного файла шага 2000 не существует (ppl_ckpt_files показывает, какой "
            "файл мерился в каждой точке).",
            "Копии пайплайна рук 2–4 отличаются от копии руки 1 одной строкой — именем "
            "файла точки замера (`calib_checkpoint_<N>.pt` вместо `checkpoint_<N>.pt`): "
            "штатная ретенция пайплайна глобит `checkpoint_[0-9]*.pt` и держит два "
            "последних по mtime, из-за чего у руки 1 точка шага 500 была удалена на шаге "
            "1500. На обучение отличие не влияет (шаг сохранения тот же), на замер — тоже: "
            "мерится состояние, а не имя файла. Патч руки 1 остаётся тем, что реально "
            "исполнялось.",
            "Все четыре руки идут в flex-режиме (LAGUNA_ATTN=flex) — решение владельца. "
            "Уровень лосса несёт систематический сдвиг маски (S3k: +0.184 ± 0.019), "
            "поэтому лосс сравнивается между руками этой сетки, а не с не-flex прогонами.",
            "Один сид: главные эффекты описательные, статистическая значимость не "
            "заявляется (AD-1, S4-PROTOCOL §2).",
        ],
        "open_questions": [
            "Точка шага 500 руки 25-0.7 в сетке отсутствует (файл удалён ретенцией "
            "пайплайна до того, как дефект был найден; у рук 2–4 он исправлен). "
            "Перезапуск руки заданием запрещён, поэтому дыра названа, а не залечена: "
            "если архитектору нужна симметрия траекторий по четырём точкам, это отдельная "
            "дельта ценой ~90 мин стенда — и у неё есть цена: у конфигурации 25 %/0.7 "
            "появились бы два неидентичных прогона (обычный режим бит-в-бит не "
            "воспроизводим, ADR-009), то есть два разных числа на одну клетку сетки.",
            "Перенос рекомендации на полную стадию: 2000 шагов ≠ 9776, и деградация "
            "немонотонна (в S3k пик на шаге 100, затем спад). Порядок рук на шаге 9776 "
            "может отличаться от порядка на шаге 2000 — проверять на промежуточных "
            "чекпойнтах полного прогона, а не считать доказанным.",
            "Доменный пул руки 50 % (3493) вдвое меньше доменного пула руки 25 % (7332) — "
            "это содвинутый фактор, названный явно (см. assumptions). Если архитектор "
            "считает удешевление домена недопустимым для вердикта, нужен либо расширенный "
            "источник реплея, либо третья рука с уравненным доменным пулом.",
            "Критерий «ppl_general ≤ 2× базы» (ADR-022 п.3б) сформулирован для полной "
            "стадии; здесь он применён к шагу 2000 как промежуточной точке — трактовка "
            "названа в verdict.ceiling_rule.",
            "Прибор GEN-EVAL v1 шумит на 5 доменных документах (`ppl_domain` v1) — "
            "основанием вердикта взяты `ppl_general` (v1: 24 документа, v2: набор ADR-018) "
            "и оба отношения считаются к базе, снятой тем же прибором в том же запуске.",
        ],
        "artifacts": [
            {"path": "tools/run_mix_lr_calib.py", "what": "раннер сетки (патч копии пайплайна, доставка, ожидание, разбор)"},
            {"path": "tools/calib_ppl_probe.py", "what": "проба PPL чекпойнтов (методика _ppl_eval наборами v1/v2)"},
            {"path": "tools/build_mix_v12r50.py", "what": "сборка микса 50 % replay с самопроверкой против v12r"},
            {"path": "data/corpus-card-v12r50.json", "what": "карточка нового микса (доли, seed, sha256) — AD-7/C-010"},
            {"path": "evidence/mix-lr-calibration.json", "what": "этот файл: числа по четырём конфигам и вердикт"},
        ],
    }
    out = CASE_ROOT / "evidence" / "mix-lr-calibration.json"
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = CASE_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"evidence: {out}")
    v = evidence["verdict"]
    print(json.dumps({k: v[k] for k in v if k in
                      ("verdict_status", "base", "effects_v1_ratio", "replay_effect_v1",
                       "lr_effect_v1", "under_ceiling", "best_arm_step2000",
                       "recommendation", "reason")},
                     ensure_ascii=False, indent=2))
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S3m: раннер сетки replay × peak_lr_scale")
    ap.add_argument("--host", default="gb10-fast")
    ap.add_argument("--image", default=os.environ.get("LAGUNA_IMAGE", DEFAULT_IMAGE))
    ap.add_argument("--ts", default=None)
    ap.add_argument("--arm", default=None, choices=ALL_ARMS,
                    help="одна рука: четыре руки сетки S3m или две контрольные S3o")
    ap.add_argument("--arms", nargs="*", default=None, choices=ALL_ARMS)
    ap.add_argument("--stall-minutes", type=int, default=20)
    ap.add_argument("--sampler-seconds", type=int, default=10800)
    ap.add_argument("--timeout", type=int, default=14400)
    ap.add_argument("--poll", type=int, default=30)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--out", default=None, help="куда писать evidence (--analyze)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true")
    g.add_argument("--prepare", action="store_true")
    g.add_argument("--launch", action="store_true")
    g.add_argument("--wait", action="store_true")
    g.add_argument("--fetch", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--chain", action="store_true",
                   help="поднять сетку целиком: одна отсоединённая tmux-сессия на стенде, "
                        "руки последовательно (--arms по умолчанию все четыре)")
    g.add_argument("--grid-wait", action="store_true",
                   help="ждать сетку, печатая изменения состояния (--ts; --timeout)")
    g.add_argument("--grid-fetch", action="store_true",
                   help="забрать логи сетки в кейс и записать её манифест AD-2")
    g.add_argument("--stop-containers", action="store_true")
    g.add_argument("--probe", action="store_true",
                   help="проба PPL чекпойнтов всех рук (база + 4 шага × 4 руки + контроль) — "
                        "в контейнере стенда")
    ap.add_argument("--probe-timeout", type=int, default=3600,
                    help="потолок ожидания пробы PPL")
    g.add_argument("--analyze", action="store_true",
                   help="собрать evidence/mix-lr-calibration.json из прогонов и пробы PPL")
    args = ap.parse_args(argv)

    if args.plan:
        print(render_plan(args))
        return EXIT_OK
    if args.prepare:
        return do_prepare(args)
    if args.launch:
        need(args, "--launch")
        return do_launch(args)
    if args.wait:
        need(args, "--wait")
        return do_wait(args)
    if args.fetch:
        need(args, "--fetch")
        return do_fetch(args)
    if args.status:
        need(args, "--status")
        return do_status(args)
    if args.analyze:
        if not args.ts:
            print("--analyze требует --ts", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        return do_analyze(args)
    if args.probe:
        if not args.ts:
            print("--probe требует --ts", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        return do_probe(args)
    if args.chain:
        if not args.ts:
            print("--chain требует --ts", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        return do_chain(args)
    if args.grid_wait:
        if not args.ts:
            print("--grid-wait требует --ts", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        return do_grid_wait(args)
    if args.grid_fetch:
        if not args.ts:
            print("--grid-fetch требует --ts", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        return do_grid_fetch(args)
    if args.stop_containers:
        print(json.dumps(do_stop_containers(args), ensure_ascii=False))
        return EXIT_OK
    return EXIT_NOT_VERIFIED


def need(args, flag: str) -> None:
    """Команды одной руки требуют и `--arm`, и `--ts` — иначе адресуются не туда.

    Код возврата 2 — как у argparse и у NOT-VERIFIED: не хватает входа, а не
    «прогон упал» (1).
    """
    if not args.arm:
        print(f"{flag} требует --arm", file=sys.stderr)
        raise SystemExit(EXIT_NOT_VERIFIED)
    if not args.ts:
        print(f"{flag} требует --ts (метка прогона)", file=sys.stderr)
        raise SystemExit(EXIT_NOT_VERIFIED)


def render_plan(args) -> str:
    """План — тот, что просят: сетка S3m по умолчанию, контроль S3o по `--arms`.

    Режим не «дописывается» к плану сетки: у контроля другой корпус, другие каталоги
    и другой вопрос, и план обязан показывать именно то, что будет запущено.
    """
    arms = args.arms or ARM_ORDER
    control = is_control(arms)
    head = ("план контрольных рук S3o (ADR-022 п.1–2, диагностика причины форгеттинга):"
            if control else "план калибровки S3m (ADR-022 п.2):")
    lines = [head,
             f"  модель/сид/длина: {MODEL} / {SEED} / {MAX_LEN}",
             f"  шагов: {STEPS}, батч {BATCH}, чекпойнт каждые {CKPT_EVERY} → {CKPT_POINTS}",
             "  руки:"]
    for arm in arms:
        spec = arm_spec(arm)
        lines.append(f"    {arm:12s} replay={spec['replay']}% lr×{spec['lr_scale']}  "
                     f"корпус: {spec['corpus']['label']}")
    lines.append(f"  каталог прогонов на стенде: {STAND_RUNS}/"
                 f"{'ctrl' if control else 'calib'}-<arm>-<ts>")
    lines.append(f"  последовательно (AD-5): {len(arms)} × {STEPS} шагов")
    lines.append("  запуск серии целиком (отсоединённо, переживает раннер):")
    lines.append(f"    python3 tools/run_mix_lr_calib.py --chain --arms "
                 f"{' '.join(arms)} --ts <ts>")
    lines.append(f"    затем: --grid-wait / --grid-fetch, --probe, --analyze --ts <ts>")
    if not control:
        lines.append("  контрольные руки S3o (другой вопрос — причина, а не калибровка):")
        for arm in CTRL_ORDER:
            spec = arm_spec(arm)
            lines.append(f"    {arm:12s} replay={spec['replay']}% lr×{spec['lr_scale']}  "
                         f"корпус: {spec['corpus']['label']}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
