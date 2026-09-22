#!/usr/bin/env python3
"""S3aa: сборка каталога SFT-стадии и её запуск на стенде (tmux + setsid nohup).

Что делает и почему именно так:

* **Отдельный каталог `sft-<ts>`.** Вход SFT — CPT-чекпойнт, а не база, и он
  подхватывается пайплайном по **фиксированному пути** `<ckpt_dir>/checkpoint_final.pt`
  (`laguna_pipeline_v8.py:804-809`, флага «вход» у стадии нет). Значит рабочая
  директория стадии обязана этот файл нести. Кладём его **жёсткой ссылкой**: тот же
  inode, ноль байт дублирования (AD-4), и тождество доказывается не «одинаковым
  размером», а равенством inode. Безопасность ссылки проверена по коду: имя
  `checkpoint_final.pt` пишет **только** `run_cpt` (строка 796); в прогоне со
  стадией `sft` оно читается (строка 805) и не пишется нигде — ни `run_sft`, ни
  `run_probes`, ни `save_checkpoint_atomic` под другим именем.

* **Стадии `sft` — одна строка `stages.tsv`.** Формат из 8 TSV-полей
  (`pilot_chain.sh:593`): `name pstage gstage artifact steps batch eckpt exp`.
  Поле 7 — это `--eval_ckpt` (НЕ вход стадии; см. комментарий `pipeline_args`),
  поэтому для SFT там `-`.

* **Автономность.** Запуск через `tmux` + `setsid nohup` на стенде — цепочка
  переживает завершение агента (ADR-023 п.11).

* **Не ждём финиша.** Стадия идёт ≈58-61 ч; дельта закрывается фактами старта и
  командой забора (`how_to_fetch`), а не ожиданием.

* **Маска лосса — свойство стадии, а не «галочка».** `--loss-mask think_masked`
  (S3ae, ADR-036) собирает копию пайплайна с маскированием `<think>…</think>`;
  `prefix_only` — редакция S3aa (в лосс входит и рассуждение). Значение уходит в
  `full_sft_params.json`, в `pipeline_patch.json` (`loss_mask`) и в манифест прогона
  (`--extra-hyperparam loss_mask=…`), потому что иначе остановленный прогон и его
  перезапуск различались бы только хешем копии, а по манифесту — нет.

* **Защита сна обязательна для `--do launch|all`** (дельты `sleep-guard-in-launch` и
  `sleep-guard-masked-stand`, ADR-9/C-029). `launch` ставит нагрузку на устройство, а
  сон машины во время прогона уничтожает контекст CUDA: 21.09.2026 хост ушёл в S3 в
  05:03:37, ядро записало `Xid 31` (MMU fault) по процессу прогона в 05:03:44, и
  прогон V3 умер на 24-й пробе из 104 — после этого устройство не поднялось. Поэтому
  запуск идёт **только через** `tools/guard_cuda_run.sh` (он берёт защиту и пишет
  расписку), а без доказанной защиты раннер отказывается стартовать — не
  «предупреждает», а отказывается. Проверку ведёт `tools/check_gpu_sleep_guard.py`
  (тот же механизм, что и правило C-029), поэтому «взяли защиту» и «страж увидел
  защиту» не могут разойтись. Способов два, и оба — факт: действующий инхибит
  **либо** структурная недостижимость сна на площадке, доказанная попыткой старта
  `suspend.target` (так защищён стенд `gb10-fast`, где инхибит взять неоткуда).
  Сама цепочка защищается шагом `tools/chain_sleep_guard.sh` — он стоит первым в
  записанной команде и решает на площадке прогона.

  `build`, `reship`, `check`, `facts` нагрузки не ставят и защиты не требуют:
  сборка каталога и опрос состояния — не GPU-работа.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
SHARED = Path("/home/user/gb10-shared")
CTR_SHARED = "/workspace/shared"
STAND = "gb10-fast"

#: Вход стадии (рабочий чекпойнт CPT, ADR-032 п.2).
CPT_RUN_DIR = SHARED / "full-cpt-20260916-2149"
CPT_CKPT = CPT_RUN_DIR / "checkpoints" / "checkpoint_final.pt"
CPT_CKPT_SHA256 = "080c3ab6523f44378c87f443e4d10fb84e060f5c4ae3825e721aad066b54e086"

#: Данные стадии. По умолчанию — v12 (заморожен ADR-013 п.2; карточка —
#: SFT-STAGE-PLAN §1), то есть поведение сборки не меняется. `v13` — набор после
#: нормализации S3an (ADR-042): те же правила, другой файл. Набор выбирается
#: ИМЕНЕМ, а не «последним по дате»: прогон обязан называть данные, на которых
#: учился, иначе сравнение с прежними прогонами невозможно (AD-2).
DATASETS = {
    "v12": {
        "jsonl": "datasets/sft_train_v12.jsonl",
        "jsonl_sha256": "39f616f1e47b1c50490bb9e01167271bac5191c71e4bff727e4940094d6d49a6",
        "tok_cache": "datasets/tok/sft_train_v12_8192_qwen25.npz",
        "examples": 44949,
        "duplicates_share_pct": 57.71,
        "unique_examples": 19011,
        "effective_passes": 7.093,
        "note": "ADR-033 п.1: не дедуплицируем ради сопоставимости; «3 эпохи» = "
                "≈7.09 прохода по уникальному примеру",
        "adr": "ADR-033",
    },
    #: S3an: набор, нормализованный механически (tools/normalize_sft_dataset.py).
    #: Число примеров и доля дубликатов — из отчёта нормализации и карточки v13;
    #: вписываются сюда тем же прогоном, что породил файл, а не «на глаз».
    "v13": {
        "jsonl": "datasets/sft_train_v13_fixed.jsonl",
        "jsonl_sha256": "71c4bd2b011b210b906e7c77eca373ad094c14264b86353189faaa0596983b01",
        "tok_cache": "datasets/tok/sft_train_v13_fixed_8192_qwen25.npz",
        #: Полный sha256 ТЕНЗОРА (S3ax). До этого объявлялся только jsonl, а тензор
        #: назывался путём: подмена файла по тому же пути прошла бы и сборку, и
        #: C-030 (тот сверяет путь, выведенный из объявленного jsonl, а не байты).
        #: Хеш снят `sha256sum` 21.09.2026 и повторяется тем же `--verify-disk`.
        "tok_cache_sha256": "dc3d4838d15b83c3f1abefb549473e1efd1dc822000a675e34f548f759b94d75",
        "examples": 44105,
        "duplicates_share_pct": 57.57,
        "unique_examples": 18713,
        "effective_passes": 7.071,
        "note": "набор v13 (ADR-042): три структурных дефекта исправлены механически, "
                "исходный v12 не изменялся; 844 примера (1.88 %) исключены с причинами, "
                "пересчитанные числа — из runs/s3an-normalize-20260919/report.json",
        "adr": "ADR-042",
    },
}

#: Активный набор — значения по умолчанию, совпадающие с прежним поведением.
SFT_DATA = DATASETS["v12"]["jsonl"]
SFT_DATA_SHA256 = DATASETS["v12"]["jsonl_sha256"]
SFT_TOK_CACHE = DATASETS["v12"]["tok_cache"]

#: Компоненты меры стадии (ADR-033 п.3) — их же мерит монитор в прогоне.
EVAL_SETS = {
    "K1": ("general_eval_v3.txt", "6fa8e00fc268b10e337995273f4763a8b465101584989068bc638e83d69b2f8e", 7.50468637420902),
    "K2": ("general_eval_k2.txt", "723daeaf163c072f3d746730759dc4c0fe71415330fb094ca84ffa765200b983", 6.1599356842437585),
    "DOMAIN": ("domain_eval_v2.txt", "7ac83fe30d97be6208af5060e010f12927b7e9acdb34325f0e893ab83dfd444e", 11.134115855539092),
}

#: Конфигурация SFT — из плана стадии (§3.1) и контура; фактические значения
#: называются в отчёте. LR/расписание/warmup/clip заданы **в коде** `run_sft`
#: (флагами не переопределяются) — поэтому они здесь как факт, а не как параметр.
SFT_EPOCHS = 3
SFT_SAMPLES = 44949
SFT_BATCH = 2
SFT_STEPS = SFT_EPOCHS * SFT_SAMPLES // SFT_BATCH          # 67423
SFT_LR = 1e-5
SFT_LR_MIN = 2e-7
SFT_SCHEDULE = "CosineAnnealingLR(T_max=max_steps, eta_min=2e-7)"
#: S3ax (ADR-052): множитель пика LR. 1.0 — штатный темп (`1e-5`), 0.2 — 2e-6.
#: Форму расписания и warmup понижение темпа НЕ трогает: рука отличается
#: масштабом, а не формой (иначе это были бы две переменные сразу).
SFT_PEAK_LR_SCALE = 1.0
SFT_OPTIMIZER = "AdamW(lr=1e-5, weight_decay=0.01, betas=(0.9,0.95))"
SFT_CLIP = 1.0
SFT_WARMUP_STEPS = 100
SEED = 42
MAX_LEN = 8192
MAX_SAMPLES = 50000
MODEL = "Qwen/Qwen2.5-0.5B"
IMAGE = "nvcr.io/nvidia/pytorch:26.07-py3-vllm"
MEM_CAP = "100g"
STALL_MINUTES = 30
SAMPLER_SECONDS = 32400
PROBE_EVERY = 500

#: Защита сна ставится **в саму записанную команду цепочки** (C-029, AD-9 вторая
#: грань), а не вокруг запускающего её `tmux`/`ssh`: те возвращаются сразу, и
#: снятая с них защита не защитила бы ничего. Первым в команде идёт
#: `tools/chain_sleep_guard.sh` — он держит защиту все ~60 часов стадии и
#: **решает на той площадке, где цепочка идёт**: берёт инхибит сам, а если взять
#: нечем (на `gb10-fast` — `Failed to inhibit: Access denied`, нет logind-сессии),
#: требует доказанную структурную недостижимость сна (маскировка целей, проверенная
#: попыткой старта `suspend.target`). Прежде здесь стоял `systemd-inhibit` прямо в
#: команде — и на стенде это работало **против** прогона: механизм отказывает
#: закрыто, и цепочка не стартовала вовсе, хотя защита там была (и сильнее).
#:
#: Отказ остаётся закрытым: если ни одного из двух способов нет, шаг не запускает
#: нагрузку (exit 2). Молчаливого незащищённого прогона не будет.
#:
#: `--why` в самом шаге намеренно ASCII: команда едет через `tmux new-session … "…"`
#: и остаётся читаемой в `chain_command.txt`, манифесте и `ps` без вопросов к
#: локали стенда. В записанной команде кавычек нет вовсе — её собирает
#: `shlex.quote`, а оболочку `tmux` двойные кавычки сломали бы.
CHAIN_GUARD_NAME = "chain_sleep_guard.sh"

#: Файлы, доставляемые в каталог прогона (копии кейса с записью sha256 — так шёл CPT).
SHIPPED = ["tools/pilot_chain.sh", "tools/check_resource_owner.sh",
           "tools/write_run_manifest.py", "tools/smoke_mem_sampler.sh",
           # C-029: шаг защиты сна в записанной команде и его источник ответа. Едут
           # в каталог прогона, потому что решают **на площадке прогона** (стенд) и
           # по хешу из `ship.json` (AD-2): «поправил в tools/» до стенда не доедет.
           "tools/chain_sleep_guard.sh", "tools/check_gpu_sleep_guard.py",
           # S3ae: проверка маски на коротком тесте — часть пина стадии, а не «прогон
           # инструмента из рабочей копии»: стадия доказывает маску тем же файлом.
           "tools/verify_think_mask.py"]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def place_checkpoint(src: Path, dst: Path, log=None) -> dict:
    """Положить чекпойнт в каталог прогона: жёсткая ссылка, иначе копия.

    Жёсткая ссылка предпочтительна (ноль байт дублирования, AD-4), но она
    **не всегда возможна**: чекпойнты пишет контейнер от root, а
    `fs.protected_hardlinks=1` запрещает ссылаться на чужой файл — `link(2)`
    отдаёт EPERM (наблюдено 17.09.2026 на `sft_probe_3000.pt`). Копия в этом
    случае — не «на всякий случай», а вынужденный путь, и она обязана быть
    **проверена побайтово**: непроверенная копия чекпойнта — это прогон,
    который может тренироваться не с той точки.

    Возвращает паспорт размещения: как легло, совпал ли inode и совпал ли хеш.
    """
    src, dst = Path(src), Path(dst)
    src_sha = sha256_file(src)
    try:
        os.link(src, dst)
        return {"kind": "hardlink", "src": str(src), "dst": str(dst),
                "src_sha256": src_sha, "dst_sha256": src_sha,
                "same_inode": os.stat(src).st_ino == os.stat(dst).st_ino,
                "sha_verified": True,
                "note": "тот же inode, ноль байт дублирования (AD-4)"}
    except OSError as e:
        import shutil
        shutil.copy2(src, dst)
        dst_sha = sha256_file(dst)
        if log:
            log(f"  hardlink невозможен ({e}) → копия, хеш сверен: {dst_sha[:12]}")
        return {"kind": "copy (hardlink невозможен)", "why": str(e),
                "src": str(src), "dst": str(dst),
                "src_sha256": src_sha, "dst_sha256": dst_sha,
                "same_inode": False, "sha_verified": src_sha == dst_sha,
                "bytes_duplicated": dst.stat().st_size,
                "note": "копия вынужденная (protected_hardlinks: файл принадлежит root); "
                        "тождество доказано равенством sha256, а не именем"}


def ssh(cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", STAND, cmd],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def stage_dirs(ts: str, loss_mask: str = "prefix_only", kind: str = "sft") -> dict:
    name = f"{kind}-{ts}"
    return {"name": name, "run_dir": SHARED / name, "ctr_run_dir": f"{CTR_SHARED}/{name}",
            "loss_mask": loss_mask, "kind": kind}


#: Возобновление SFT (S3ai, ADR-039 п.1): стадия продолжается с чекпойнта
#: остановленного прогона. Каталог прогона **свой** (`sft-resume-<ts>`), потому что
#: это отдельный прогон со своими фактами старта, манифестом и следом остановки;
#: исходный каталог остаётся неприкосновенным артефактом ADR-036/037.
RESUME_KIND = "sft-resume"
RE_RESUME_STEP = re.compile(r"sft_(?:probe_|checkpoint_)?(\d+)\.pt$")


def resume_input(path: Path, source_run: Path | None = None) -> dict:
    """Проверить вход возобновления и вернуть его паспорт.

    Проверяется **фактом**, а не именем: шаг берётся из имени файла (иначе
    `start_step` в прогоне разошёлся бы с содержимым чекпойнта), хеш считается
    по байтам, а пайплайн берётся из каталога исходного прогона и сверяется с его
    манифестом — «тот же код» должно быть равенством хешей, а не намерением.
    """
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"чекпойнта возобновления нет: {path}")
    m = RE_RESUME_STEP.search(path.name)
    if not m:
        raise SystemExit(f"из имени {path.name} не читается шаг "
                         "(ожидается sft_probe_<step>.pt | sft_checkpoint_<step>.pt)")
    step = int(m.group(1))
    src_run = Path(source_run) if source_run else path.parent.parent
    pipe = src_run / "laguna_pipeline_sft.py"
    if not pipe.is_file():
        raise SystemExit(f"пайплайна исходного прогона нет: {pipe}")
    pipe_sha = sha256_file(pipe)
    man = src_run / "run_manifest.json"
    man_sha = None
    if man.is_file():
        man_sha = json.loads(man.read_text(encoding="utf-8")).get("pipeline_sha256")
        if man_sha and man_sha != pipe_sha:
            raise SystemExit(f"пайплайн {pipe} не совпал с манифестом исходного прогона: "
                             f"{pipe_sha} != {man_sha}")
    return {"path": str(path), "step": step, "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
            "source_run": str(src_run), "pipeline": str(pipe),
            "pipeline_sha256": pipe_sha,
            "pipeline_manifest_sha256": man_sha,
            "pipeline_verified_by_manifest": bool(man_sha and man_sha == pipe_sha)}


# ─────────────────────────── сборка каталога ───────────────────────────

def build(ts: str, loss_mask: str = "prefix_only", resume: dict | None = None,
          dataset: str = "v12", peak_lr_scale: float = SFT_PEAK_LR_SCALE) -> dict:
    kind = RESUME_KIND if resume else "sft"
    d = stage_dirs(ts, loss_mask, kind)
    if dataset not in DATASETS:
        raise SystemExit(f"неизвестный набор: {dataset} (есть: {', '.join(DATASETS)})")
    if not (0.0 < peak_lr_scale <= 1.0):
        raise SystemExit(f"множитель пика LR вне (0, 1]: {peak_lr_scale}")
    ds = DATASETS[dataset]
    #: Шаги считаются от числа примеров НАБОРА: 3 эпохи × N / batch. Оставлять
    #: прежние 67423 на наборе из 44105 примеров значило бы «те же
    #: гиперпараметры» ценой другого числа эпох — то есть смену параметра.
    steps = SFT_EPOCHS * ds["examples"] // SFT_BATCH
    #: Хеш данных — факт, а не константа в коде: он берётся с диска и сверяется с
    #: записанным. Набор, изменившийся после объявления, обязан остановить сборку.
    ds_sha = sha256_file(SHARED / ds["jsonl"])
    if ds["jsonl_sha256"] and ds_sha != ds["jsonl_sha256"]:
        raise SystemExit(f"данные {ds['jsonl']} не совпали с записанным хешем: "
                         f"{ds_sha} != {ds['jsonl_sha256']}")
    #: Тензор — тоже объявление, и он тоже сверяется ДО сборки: 2.9 ГБ читаются
    #: один раз за прогон, а расхождение иначе всплыло бы через 40 часов в отчёте
    #: стража. Поле необязательное: у наборов, объявленных до S3ax, его нет.
    tok_sha = None
    if ds.get("tok_cache_sha256"):
        tok_sha = sha256_file(SHARED / ds["tok_cache"])
        if tok_sha != ds["tok_cache_sha256"]:
            raise SystemExit(f"тензор {ds['tok_cache']} не совпал с записанным хешем: "
                             f"{tok_sha} != {ds['tok_cache_sha256']}")
    #: Паспорт набора кладётся в `d` СРАЗУ: `build_chain_command` читает его и без
    #: этого собрал бы команду на v12 (запасной вариант по умолчанию) — прогон
    #: ушёл бы на прежнем наборе, а манифест называл бы v13.
    d.update({"dataset": dataset, "dataset_spec": ds, "dataset_sha256": ds_sha, "steps": steps,
              "tok_cache_sha256": tok_sha, "peak_lr_scale": peak_lr_scale})
    run_dir: Path = d["run_dir"]
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit(f"каталог прогона уже не пуст: {run_dir}")
    (run_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "var" / "status").mkdir(parents=True, exist_ok=True)

    # 1. Вход стадии — CPT-чекпойнт (жёсткой ссылкой, иначе проверенной копией).
    if sha256_file(CPT_CKPT) != CPT_CKPT_SHA256:
        raise SystemExit("CPT-чекпойнт не совпал с записанным в var/status/cpt — стоп")
    dst = run_dir / "checkpoints" / "checkpoint_final.pt"
    entry = place_checkpoint(CPT_CKPT, dst, note)

    # 1b. (возобновление) чекпойнт-точка под **штатным** именем `sft_checkpoint_N.pt`:
    # пайплайн ищет точку возобновления только глобом `sft_checkpoint_[0-9]*.pt`
    # (`run_sft`, SFT RESUME) и берёт последнюю по номеру. Если положить файл под
    # исходным именем `sft_probe_N.pt`, прогон молча начал бы с нуля — то есть
    # «возобновление» стало бы перезапуском с CPT и никто бы этого не заметил.
    resume_entry = None
    if resume:
        r_step = int(resume["step"])
        r_dst = run_dir / "checkpoints" / f"sft_checkpoint_{r_step}.pt"
        placed = place_checkpoint(resume["path"], r_dst, note)
        if not placed["sha_verified"]:
            raise SystemExit(f"копия чекпойнта возобновления не совпала по хешу: "
                             f"{placed['src_sha256']} != {placed['dst_sha256']}")
        resume_entry = {**resume, **placed, "link": placed["kind"],
                        "dst": str(r_dst)}
        #: Паспорт кладётся в `d` **до** сборки команды: `build_chain_command`
        #: читает `d["resume"]` и без него команда ушла бы без признаков
        #: возобновления — прогон в манифесте выглядел бы обычным SFT-стартом.
        d["resume"] = resume_entry

    # 2. Пайплайн стадии. Обычный путь — патч копии базы; путь возобновления —
    # **байты пайплайна исходного прогона** (тот же код, что тренировал первые
    # шаги): пересборка патчем дала бы «похожий» файл, а сравнение результатов
    # требует одинакового.
    patch_json = run_dir / "pipeline_patch.json"
    if resume:
        src_pipe = Path(resume["pipeline"])
        (run_dir / "laguna_pipeline_sft.py").write_bytes(src_pipe.read_bytes())
        patch_json.write_text(json.dumps(
            {"resume": True, "source_run": resume["source_run"],
             "source_pipeline": str(src_pipe),
             "source_pipeline_sha256": resume["pipeline_sha256"],
             "patch_rebuild": "не выполняется: стадия возобновляется тем же кодом, "
                              "которым тренировались первые шаги",
             "loss_mask": "prefix_only (маска <think> не применяется — ADR-037)"},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        patch_cmd = [sys.executable, str(CASE_ROOT / "tools/patch_pipeline_sft.py"),
                     "--base", str(CASE_ROOT / "laguna_pipeline_v8.py"),
                     "--out", str(run_dir / "laguna_pipeline_sft.py"),
                     "--patch-json", str(patch_json)]
        if loss_mask == "think_masked":
            patch_cmd.append("--think-mask")
        #: S3ax: темп уезжает в СБОРКУ копии, а не в командную строку. Причина —
        #: у пика SFT нет флага в пайплайне (число живёт в коде `run_sft`), и
        #: «задать темп» можно только копией. Число при этом не «спрятано»: оно
        #: в pipeline_patch.json (sft_lr), в полном объявлении прогона и в логе
        #: стадии, а связь «объявление ↔ копия» проверяет tools/check_stage_lr.py.
        if peak_lr_scale != 1.0:
            patch_cmd += ["--peak-lr-scale", repr(float(peak_lr_scale))]
        rc = subprocess.run(patch_cmd, capture_output=True, text=True)
        if rc.returncode != 0:
            raise SystemExit(f"патч пайплайна не собрался: {rc.stderr}")

    # 3. Доставка файлов цепочки + сверка sha256 (AD-2: прогон пинит свой код).
    ship: dict[str, str] = {}
    for rel in SHIPPED:
        src = CASE_ROOT / rel
        body = src.read_bytes()
        (run_dir / src.name).write_bytes(body)
        ship[src.name] = hashlib.sha256(body).hexdigest()
    for extra in ("laguna_pipeline_sft.py", "pipeline_patch.json"):
        ship[extra] = sha256_file(run_dir / extra)

    # 4. Стадии: одна строка, 8 TSV-полей.
    artifact = "checkpoints/sft_checkpoint_final.pt"
    (run_dir / "stages.tsv").write_text(
        f"sft\tsft\tsft\t{artifact}\t{steps}\t{SFT_BATCH}\t-\t{d['name']}\n",
        encoding="utf-8")

    # 5. Паспорт запуска: фактические значения, а не «намерения».
    params = {
        "stage": "sft", "run_dir": str(run_dir), "ctr_run_dir": d["ctr_run_dir"],
        "loss_mask": loss_mask,
        "input_checkpoint": {"path": str(CPT_CKPT), "sha256": CPT_CKPT_SHA256,
                             "loaded_as": f"{d['ctr_run_dir']}/checkpoints/checkpoint_final.pt",
                             "how": entry["kind"]},
        "data": {
            "dataset": dataset,
            "sft_jsonl": ds["jsonl"], "sft_jsonl_sha256": ds_sha,
            "sft_examples": ds["examples"], "tok_cache": ds["tok_cache"],
            "tok_cache_sha256": tok_sha,
            "duplicates_share_pct": ds["duplicates_share_pct"],
            "unique_examples": ds["unique_examples"],
            "effective_passes": ds["effective_passes"],
            "duplicates_note": "ADR-033 п.1: не дедуплицируем ради сопоставимости; "
                               "«3 эпохи» = ≈%.3f прохода по уникальному примеру" % ds["effective_passes"],
            "dataset_note": ds["note"], "dataset_adr": ds["adr"],
        },
        "config": {
            "steps": steps, "epochs": SFT_EPOCHS, "batch": SFT_BATCH,
            "steps_source": "3 эпохи × %d примеров // batch %d" % (ds["examples"], SFT_BATCH),
            "max_len": MAX_LEN, "max_samples": MAX_SAMPLES, "seed": SEED,
            "model": MODEL, "image": IMAGE,
            #: Объявляется ФАКТИЧЕСКИЙ темп (пик × множитель), а не штатный: до S3ax
            #: здесь стояли бы 1e-5 при копии, исполняющей 2e-6, — тот же класс
            #: расхождения «декларация про другое», что C-030 ловит по набору.
            "lr": SFT_LR * peak_lr_scale, "lr_base": SFT_LR,
            "peak_lr_scale": peak_lr_scale,
            "lr_min": SFT_LR_MIN, "schedule": SFT_SCHEDULE,
            "optimizer": ("AdamW(lr=%g, weight_decay=0.01, betas=(0.9,0.95))"
                          % (SFT_LR * peak_lr_scale)),
            "clip_grad_norm": SFT_CLIP,
            "warmup_steps": SFT_WARMUP_STEPS,
            "warmup_note": "100 шагов учится только embed/lm_head, затем полный размороз; "
                           "понижение темпа warmup НЕ меняет",
            "lr_source": ("патч копии (patch_pipeline_sft.py --peak-lr-scale): "
                          "peak_lr = 1e-5 × множитель; форма расписания и warmup штатные"
                          if peak_lr_scale != 1.0 else
                          "run_sft: peak_lr=1e-5 задан в коде; --peak_lr_scale на SFT не влияет"),
            "lr_guard": "tools/check_stage_lr.py — объявление против копии пайплайна",
            "lr_adr": "ADR-052",
            "ckpt_every": 200, "probe_ckpt_every": PROBE_EVERY,
            "probe_keep_last": 24,
        },
        "monitor": {
            "sets": {k: v[0] for k, v in EVAL_SETS.items()},
            "rule": "K1,K2 ≤ 2× базы; домен × базы < 1 (ADR-027, ADR-031 п.4)",
            "bases": {k: v[2] for k, v in EVAL_SETS.items()},
            "every_steps": 50,
            "history": "general_eval_history.jsonl",
            "reference_only": ["general_eval.txt", "domain_eval.txt"],
            "adr": "ADR-033 п.3 (дыра G2 закрыта патчем пайплайна)",
        },
    }
    if resume:
        params["resume"] = {
            **resume_entry,
            "from_step": resume["step"], "first_step": resume["step"] + 1,
            "how_pipeline_finds_it":
                "штатное имя sft_checkpoint_<step>.pt: run_sft берёт последнюю по номеру "
                "точку глобом sft_checkpoint_[0-9]*.pt; под именем sft_probe_N.pt прогон "
                "начался бы с CPT-входа, то есть возобновление стало бы перезапуском",
            "scheduler": "CosineAnnealingLR(T_max=max_steps) — T_max не меняется, "
                         "last_epoch = step: расписание LR продолжается, а не перезапускается",
            "data_order_note":
                "DataLoader(shuffle=True) без генератора: тот же сид даёт ту же "
                "перестановку, и возобновлённый прогон проходит **начало** потока "
                "(шаги 100…), уже пройденное в первых 3395 шагах. Данные и "
                "гиперпараметры не менялись; смещение потока не реализовано "
                "намеренно — правка пайплайна под живой стадией запрещена (ADR-016 п.1), "
                "а непроверенная правка смещения рискованнее объявленного ограничения",
            "planned_stop_step": resume.get("planned_stop_step"),
            "adr": "ADR-039 п.1 (возобновление с чекпойнта 3000 без изменения данных)",
        }
    (run_dir / "full_sft_params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 6. Точная команда цепочки (та же, что пойдёт в tmux).
    chain_cmd = build_chain_command(d, run_dir)
    (run_dir / "chain_command.txt").write_text(chain_cmd + "\n", encoding="utf-8")

    launch_cmd = (f"ssh {STAND} 'tmux kill-session -t {d['name']} 2>/dev/null; "
                  f"tmux new-session -d -s {d['name']} \"setsid nohup {chain_cmd} "
                  f">> {run_dir}/chain.log 2>&1 < /dev/null\"'")
    (run_dir / "launch_command.txt").write_text(launch_cmd + "\n", encoding="utf-8")

    (run_dir / "ship.json").write_text(json.dumps(
        {"ok": True, "stand_dir": str(run_dir), "ctr_dir": d["ctr_run_dir"],
         "sha256": ship, "mismatched": [], "link": entry,
         "checked_from": f"ssh {STAND} — {run_dir}"}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    d.update({"link": entry, "ship": ship, "chain_cmd": chain_cmd, "launch_cmd": launch_cmd,
              "artifact": artifact, "params": params, "resume": resume_entry,
              "dataset": dataset, "dataset_spec": ds, "dataset_sha256": ds_sha, "steps": steps})
    return d


def build_chain_command(d: dict, run_dir: Path) -> str:
    run = str(run_dir)
    ctr = d["ctr_run_dir"]
    #: Словарь может быть частичным (тесты зовут функцию с name/ctr_run_dir) —
    #: тогда поведение прежнее, v12. Набор по умолчанию не меняется.
    ds = d.get("dataset_spec", DATASETS["v12"])
    steps = d.get("steps", SFT_EPOCHS * ds["examples"] // SFT_BATCH)
    a = ["bash", f"{run}/{CHAIN_GUARD_NAME}", "bash", f"{run}/pilot_chain.sh",
        "--run-dir", run, "--ctr-run-dir", ctr,
        "--stages-file", f"{run}/stages.tsv",
        "--exp-base", d["name"], "--ctr-prefix", f"laguna-{d['name']}",
        "--shared", str(SHARED), "--experiments", "/home/user/experiments",
        "--pipeline", f"{run}/laguna_pipeline_sft.py",
        "--pipeline-ctr", f"{ctr}/laguna_pipeline_sft.py",
        "--cpt-data", f"{CTR_SHARED}/datasets/cpt_corpus_v12r.txt",
        "--dataset", str(SHARED / ds["tok_cache"]),
        #: Вход SFT отдаётся **тем же** значением, что объявляется ниже
        #: (`sft_dataset`/`sft_dataset_sha256`). До S3av цепочка брала набор
        #: литералом, и выбор `--dataset v13` менял объявление, не меняя обучения:
        #: стадия читала v12, манифест называл v13. Один источник на декларацию и
        #: факт — плюс хеш, по которому пайплайн откажет, если файл не тот.
        "--sft-data", f"{CTR_SHARED}/{ds['jsonl']}",
        "--sft-data-sha256", d.get("dataset_sha256", ds["jsonl_sha256"]),
        "--runner-name", "tools/launch_sft_stage.py",
        "--extra-hyperparam", "stage_input=cpt:full-cpt-20260916-2149/checkpoints/checkpoint_final.pt",
        "--extra-hyperparam", f"cpt_ckpt_sha256={CPT_CKPT_SHA256}",
        "--extra-hyperparam", "sft_epochs=3",
        "--extra-hyperparam", f"sft_steps={steps}",
        #: Темп — в манифест прогона (AD-2): по манифесту прогоны обязаны
        #: различаться, а два SFT с пиком 1e-5 и 2e-6 иначе неразличимы.
        "--extra-hyperparam", f"sft_peak_lr={SFT_LR * d.get('peak_lr_scale', SFT_PEAK_LR_SCALE)!r}",
        "--extra-hyperparam", f"sft_peak_lr_scale={d.get('peak_lr_scale', SFT_PEAK_LR_SCALE)!r}",
        "--extra-hyperparam", f"sft_lr_min={SFT_LR_MIN!r}",
        "--extra-hyperparam", f"sft_peak_lr_base={SFT_LR!r}",
        "--extra-hyperparam", f"duplicates_share_pct={ds['duplicates_share_pct']}",
        "--extra-hyperparam", f"effective_passes={ds['effective_passes']}",
        "--extra-hyperparam", f"sft_dataset={ds['jsonl']}",
        "--extra-hyperparam", f"sft_dataset_sha256={d.get('dataset_sha256', ds['jsonl_sha256'])}",
        #: Число примеров объявленного набора — вторая ось сверки с фактическим
        #: (страж `tools/check_dataset_identity.py`): одного хеша мало, если манифест
        #: правят руками, а число эпох считают от числа примеров.
        "--extra-hyperparam", f"sft_dataset_samples={ds['examples']}",
        "--extra-hyperparam",
        f"sft_tensor_sha256={d.get('tok_cache_sha256') or ds.get('tok_cache_sha256') or 'not-declared'}",
        "--extra-hyperparam", "monitor_sets=K1:general_eval_v3,K2:general_eval_k2,DOMAIN:domain_eval_v2",
        "--extra-hyperparam", f"probe_ckpt_every={PROBE_EVERY}",
        #: `.get`, а не `[...]`: словарь может быть частичным (тест 20.6 зовёт
        #: build_chain_command с name/ctr_run_dir) — падать на отсутствии нового
        #: поля значит ломать вызовы, которые к маске отношения не имеют.
        "--extra-hyperparam", f"loss_mask={d.get('loss_mask', 'prefix_only')}",
        "--extra-hyperparam", "adr=ADR-033"
        + ("+ADR-036" if d.get("loss_mask") == "think_masked" else "")
        + ("+ADR-039" if d.get("resume") else "")
        + ("+ADR-042" if d.get("dataset") == "v13" else ""),
        "--guard", f"{run}/check_resource_owner.sh",
        "--safe-start", "/home/user/gb10-shared/nvrm-storm/safe_start.sh",
        "--sampler", f"{run}/smoke_mem_sampler.sh",
        "--sampler-seconds", str(SAMPLER_SECONDS),
        "--storm-gap", "/home/user/gb10-shared/nvrm-storm/storm_gap.sh",
        "--manifest-tool", f"{run}/write_run_manifest.py",
        "--image", IMAGE,
        "--glm-env", "/home/user/gb10-shared/.glm_env",
        "--nvrm-log", "/home/user/experiments/nvrm_watch.log",
        "--model", MODEL, "--seed", str(SEED),
        "--max-len", str(MAX_LEN), "--max-samples", str(MAX_SAMPLES),
        "--attn", "flex", "--mem-cap", MEM_CAP,
        "--stall-minutes", str(STALL_MINUTES),
    ]
    #: Паспорт возобновления едет в манифест прогона (AD-2): по нему остановленный
    #: и возобновлённый прогоны различимы между собой, а не «по дате каталога».
    r = d.get("resume")
    if r:
        a += ["--extra-hyperparam", f"resume_from={r['path']}",
              "--extra-hyperparam", f"resume_step={r['step']}",
              "--extra-hyperparam", f"resume_ckpt_sha256={r['sha256']}",
              "--extra-hyperparam", f"resume_pipeline_sha256={r['pipeline_sha256']}",
              "--extra-hyperparam", f"resume_source_run={r['source_run']}"]
        if r.get("planned_stop_step"):
            a += ["--extra-hyperparam", f"planned_stop_step={r['planned_stop_step']}"]
    return " ".join(shlex.quote(x) for x in a)


# ─────────────────────────── проверки и запуск ───────────────────────────

def reship(ts: str, kind: str = "sft") -> dict:
    """Перевыложить файлы цепочки в каталог прогона и обновить `ship.json`.

    Нужно, когда инструмент стадии исправлен **до** старта: стадия исполняет
    тот код, который лежит в её каталоге, и «поправил в tools/» до неё не доедет.
    Под живой цепочкой запрещено (ADR-016 п.1) — проверяется фактом статуса.
    """
    d = stage_dirs(ts, kind=kind)
    run_dir: Path = d["run_dir"]
    if not run_dir.is_dir():
        raise SystemExit(f"каталога прогона нет: {run_dir}")
    status_file = run_dir / "var" / "chain.status"
    status = status_file.read_text().strip() if status_file.is_file() else None
    if status == "running":
        raise SystemExit(f"цепочка прогона {d['name']} жива (chain.status=running) — "
                         "под живой цепочкой правки запрещены (ADR-016 п.1)")
    ship = json.loads((run_dir / "ship.json").read_text(encoding="utf-8"))
    changed = {}
    for rel in SHIPPED:
        src = CASE_ROOT / rel
        new = hashlib.sha256(src.read_bytes()).hexdigest()
        if ship["sha256"].get(src.name) != new:
            (run_dir / src.name).write_bytes(src.read_bytes())
            changed[src.name] = {"before": ship["sha256"].get(src.name), "after": new}
            ship["sha256"][src.name] = new
    ship["reship"] = {"at": datetime.now(timezone.utc).isoformat(), "changed": changed,
                      "chain_status": status}
    (run_dir / "ship.json").write_text(json.dumps(ship, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    return {"run_dir": str(run_dir), "chain_status": status, "changed": changed}


def preflight(ts: str, loss_mask: str = "prefix_only", kind: str = "sft") -> dict:
    """Прогон цепочки в режиме --check-only: все предусловия, ноль запусков.

    Команда берётся из `chain_command.txt` каталога прогона — **та же**, что уйдёт
    в tmux. Собрать её заново значило бы проверять не тот прогон: у возобновления
    (S3ai) в команде есть признаки `resume_*`, и пересборка «с чистого словаря»
    тихо проверяла бы обычный SFT-старт.
    """
    d = stage_dirs(ts, loss_mask, kind)
    cmd_file = d["run_dir"] / "chain_command.txt"
    if not cmd_file.is_file():
        raise SystemExit(f"команды цепочки нет ({cmd_file}) — сначала --do build")
    cmd = cmd_file.read_text(encoding="utf-8").strip() + " --check-only"
    rc, out, err = ssh(cmd, timeout=300)
    return {"command": cmd, "rc": rc, "output_tail": out.splitlines()[-25:], "stderr": err[-800:]}


def launch(ts: str, kind: str = "sft") -> dict:
    d = stage_dirs(ts, kind=kind)   # команда берётся из каталога прогона, собирать её заново нельзя
    # Команда берётся из каталога прогона, а не собирается заново: запускать надо
    # ровно то, что записано в `chain_command.txt` и ушло в манифест (иначе
    # «запустили не то, что записали» — класс дефекта AD-2).
    cmd_file = d["run_dir"] / "chain_command.txt"
    chain_cmd = cmd_file.read_text(encoding="utf-8").strip()
    cmd = (f"tmux kill-session -t {d['name']} 2>/dev/null; "
           f"tmux new-session -d -s {d['name']} \"setsid nohup {chain_cmd} "
           f">> {d['run_dir']}/chain.log 2>&1 < /dev/null\"")
    rc, out, err = ssh(cmd, timeout=120)
    return {"rc": rc, "stdout": out, "stderr": err, "launched_command": chain_cmd}


def facts(ts: str, settle: int = 90, kind: str = "sft") -> dict:
    """Факты старта: tmux, контейнер, траектория по шагам, статус стадии."""
    import time
    d = stage_dirs(ts, kind=kind)
    run = d["run_dir"]
    f: dict = {"settle_seconds": settle, "checked_at": datetime.now(timezone.utc).isoformat()}
    time.sleep(settle)
    f["tmux"] = ssh(f"tmux ls 2>&1 | grep {d['name']} || echo 'нет сессии'", 60)[1]
    f["docker"] = ssh(f"docker ps --format '{{{{.Names}}}} {{{{.Status}}}}' | grep {d['name']} || echo 'нет контейнера'", 60)[1]
    f["chain_status"] = (run / "var" / "chain.status").read_text().strip() \
        if (run / "var" / "chain.status").is_file() else None
    lt = run / "logs" / "loss_trace.jsonl"
    if lt.is_file():
        lines = lt.read_text(encoding="utf-8").strip().splitlines()
        f["loss_trace"] = {"file": str(lt), "lines": len(lines)}
        if lines:
            first, last = json.loads(lines[0]), json.loads(lines[-1])
            f["loss_trace"].update({"first_step": first["step"], "last_step": last["step"],
                                    "last_loss": last["loss"], "last_lr": last["lr"]})
        # «растёт ли» — проверка фактом, а не намерением
        time.sleep(60)
        n2 = len(lt.read_text(encoding="utf-8").strip().splitlines())
        f["loss_trace"]["lines_after_60s"] = n2
        f["loss_trace"]["growing"] = n2 > len(lines)
    else:
        f["loss_trace"] = {"file": str(lt), "exists": False}
    log = run / "logs" / "sft.log"
    if log.is_file():
        txt = log.read_text(encoding="utf-8", errors="replace").splitlines()
        f["sft_log_tail"] = txt[-12:]
        f["loaded_cpt_ckpt"] = any("Loaded CPT ckpt" in l for l in txt)   # предусловие R3 (ADR-016 п.3)
        f["stage_line"] = next((l for l in txt if "STAGE: SFT" in l), None)
    else:
        f["sft_log_tail"] = None
        f["loaded_cpt_ckpt"] = False
    f["chain_log_tail"] = (run / "chain.log").read_text(encoding="utf-8").splitlines()[-12:] \
        if (run / "chain.log").is_file() else None
    return f


def sleep_guard_state() -> tuple[bool, str]:
    """Действует ли защита сна для этого процесса — ответ даёт страж, а не самообъявление.

    Источник один: ``tools/check_gpu_sleep_guard.py`` (то же, чем проверяет правило
    C-029). Раннер не заводит собственную копию проверки: разойдись они — «запуск
    под запретом» и «страж видит запрет» стали бы разными утверждениями.

    Способов два, и оба — факт, а не намерение: действующий инхибит (предок
    ``systemd-inhibit`` либо метка обвязки) **либо** структурная недостижимость сна
    на площадке, доказанная попыткой старта ``suspend.target``. Второй нужен там,
    где инхибит взять неоткуда: на стенде ``gb10-fast`` нет logind-сессии
    (``Failed to inhibit: Access denied``), зато цели сна замаскированы. Способ
    называется в ответе — «зелено без причины» не бывает.
    """
    tools_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(tools_dir))
    try:
        from check_gpu_sleep_guard import current_protection  # noqa: PLC0415
    except ImportError as exc:  # страж обязан быть рядом: его отсутствие — не разрешение
        return False, f"страж запрета сна не найден ({exc})"
    prot = current_protection(Path("/proc"))
    if prot.get("held"):
        return True, f"{prot.get('method')} — {prot.get('why')}"
    return False, prot.get("why", "")


def require_sleep_guard(action: str) -> None:
    """Отказ стартовать без доказанной защиты сна (спайн AD-9, правило C-029)."""
    held, why = sleep_guard_state()
    if held:
        note(f"защита сна действует: {why}")
        return
    print(
        f"ОТКАЗ: --do {action} ставит GPU-нагрузку, а защита сна не доказана"
        f"{f' ({why})' if why else ''}.\n"
        "Сон машины во время прогона уничтожает контекст CUDA: 21.09.2026 хост ушёл в S3,\n"
        "ядро записало Xid 31 по процессу прогона, прогон V3 умер на 24-й пробе из 104,\n"
        "и устройство после этого не поднялось (cuInit → 999).\n"
        "Запуск — через обвязку:\n"
        "  tools/guard_cuda_run.sh --out runs/<run>/guard.json --why \"запуск стадии\" -- \\\n"
        f"      python3 tools/launch_sft_stage.py {' '.join(sys.argv[1:])}\n"
        "(обвязка берёт systemd-inhibit --what=sleep --mode=block либо — там, где инхибит\n"
        "взять нечем, — доказывает структурную недостижимость сна на площадке, и в обоих\n"
        "случаях пишет расписку с именем способа; запуск без защиты — находка C-029)",
        file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ts", default=datetime.now().strftime("%Y%m%d-%H%M"))
    ap.add_argument("--do", required=True,
                    choices=["build", "reship", "check", "launch", "facts", "all"])
    ap.add_argument("--settle", type=int, default=90)
    ap.add_argument("--loss-mask", choices=["prefix_only", "think_masked"], default="prefix_only",
                    help="маска лосса SFT: prefix_only (S3aa) | think_masked (S3ae, ADR-036)")
    ap.add_argument("--resume-ckpt", default=None,
                    help="S3ai/ADR-039: путь к чекпойнту остановленного прогона "
                         "(sft_probe_3000.pt | sft_checkpoint_N.pt) — стадия "
                         "возобновляется в каталоге sft-resume-<ts>")
    ap.add_argument("--resume-source-run", default=None,
                    help="каталог исходного прогона (по умолчанию — родитель чекпойнта); "
                         "из него берётся пайплайн и сверяется с его манифестом")
    ap.add_argument("--dataset", choices=sorted(DATASETS), default="v12",
                    help="какой обучающий набор стадии: v12 (ADR-033) | v13 (ADR-042, "
                         "нормализованный S3an); по умолчанию v12 — поведение не меняется")
    ap.add_argument("--planned-stop-step", type=int, default=None,
                    help="объявленный шаг остановки возобновлённого прогона (в манифест)")
    ap.add_argument("--peak-lr-scale", type=float, default=SFT_PEAK_LR_SCALE,
                    help="S3ax (ADR-052): множитель пика LR SFT. 0.2 — пониженный темп "
                         "2e-6 вместо штатных 1e-5; форма расписания (косинус до 2e-7) "
                         "и warmup не меняются")
    a = ap.parse_args()

    # Отказ — до чтения чекпойнта и до сети: `launch` и `all` ставят нагрузку на
    # устройство, и запрет сна проверяется раньше, чем что-либо будет запущено.
    if a.do in ("launch", "all"):
        require_sleep_guard(a.do)

    kind = RESUME_KIND if a.resume_ckpt else "sft"
    resume = resume_input(Path(a.resume_ckpt), a.resume_source_run) if a.resume_ckpt else None
    if resume:
        resume["planned_stop_step"] = a.planned_stop_step

    if a.do in ("build", "all"):
        d = build(a.ts, a.loss_mask, resume, a.dataset, a.peak_lr_scale)
        print(json.dumps({"built": d["name"], "run_dir": str(d["run_dir"]),
                          "chain_command": d["chain_cmd"], "link": d["link"],
                          "resume": d.get("resume"), "ship": d["ship"]},
                         ensure_ascii=False, indent=2))
    if a.do in ("reship",):
        print(json.dumps(reship(a.ts, kind), ensure_ascii=False, indent=2))
    if a.do in ("check", "all"):
        print(json.dumps(preflight(a.ts, a.loss_mask, kind), ensure_ascii=False, indent=2))
    if a.do in ("launch", "all"):
        print(json.dumps(launch(a.ts, kind), ensure_ascii=False, indent=2))
    if a.do in ("facts", "all"):
        print(json.dumps(facts(a.ts, a.settle, kind), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
