#!/usr/bin/env python3
"""SFT-POINT-CHAIN §3#2 — каталог-обёртка приборной цепочки завершённой SFT-стадии.

Зачем отдельный сборщик, если есть `tools/launch_sft_stage.py`. Тот собирает каталог
**обучающей** стадии: вход — CPT-чекпойнт, три стадии подряд (cpt→sft→rl), маска
лосса, гиперпараметры обучения. Здесь предмет другой — **замеры по завершённому
чекпойнту** (`docs/specs/SFT-POINT-CHAIN.md`): стадии только `eval_*`, весов не
обучаем, и первое действие — не запуск, а **бронь предмета** (§2: копия в `ckpt/`,
хеш снят с копии, запись до первого замера). Смешать это с обучающим сборщиком
значило бы получить каталог, в котором «стадия» неотличима от «обучающей», — а
именно от этого различия зависит, читает ли прибор живой прогон или бронь.

Что делает (по шагам, каждый — идемпотентен):

1. `build`  — каталог на сетевом диске + **бронь** предмета + доставка файлов
   цепочки (по хешу в `ship.json`, AD-2) + `stages.tsv` + `chain_command.txt`.
2. `check`  — `pilot_chain.sh --check-only`: право стартовать по всем стадиям
   (страж AD-9 + предусловие GLM-ключа + наличие чекпойнта в брони). Нагрузки нет.
3. `launch` — отсоединённая цепочка в `tmux` на стенде (`setsid nohup`, лог в
   `chain.log`). Первым в записанной команде стоит `chain_sleep_guard.sh` (C-029).
4. `facts`  — факты старта: tmux, контейнер, статус стадий, хвост лога.
5. `mirror` — малые артефакты каталога в дерево кейса (`runs/<имя>/`), **кроме
   весов** (AD-4: в кейсе копий весов нет; симлинков на бронь тоже — `ckpt/`
   остаётся на сетевом диске и в кейс не переезжает).

Границы (названы, чтобы не выглядели обещанием):

* не запускает два GPU-замера разом (AD-5): цепочка идёт последовательно сама, а
  сборщик не умеет запускать «второй параллельно» — такого флага нет;
* не правит живой прогон `sft-v13-2e6-20260921-2054` (SFT-POINT-CHAIN §4.2): все
  пути сборщика ведут в **новый** каталог, а предмет только читается (link);
* не решает за архитектора пороги: `--sft-data-sha256`, `--eval-items` и прочие
  числа приходят из спеки/ADR, а не подбираются здесь;
* не снимает замеры: их снимают приборы (`passrate_probe.py`,
  `full_cpt_probe.py`, `probe_language_split.py`), у каждого свой каталог.

Коды возврата::

    0 — шаг выполнен
    1 — отказ: предусловие сборки не выполнено (нет предмета, хеш не сошёлся)
    2 — NOT-VERIFIED: не прочитан вход (нет каталога, нет стенда, нет tmux)

Запуск::

    python3 tools/build_sft_point_chain.py --ts 20260925-1000 --do build
    python3 tools/build_sft_point_chain.py --ts 20260925-1000 --do check
    python3 tools/build_sft_point_chain.py --ts 20260925-1000 --do all
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
STAND = "gb10-fast"
SHARED = Path("/home/user/gb10-shared")
CTR_SHARED = "/workspace/shared"
EXPERIMENTS = "/home/user/experiments"

#: Предмет замера (SFT-POINT-CHAIN §2). Путь — на сетевом диске; чтение его
#: сборщиком ограничено метаданными и хешем, запись — никогда.
CKPT_SRC = SHARED / "sft-v13-2e6-20260921-2054" / "checkpoints" / "sft_checkpoint_final.pt"
SOURCE_RUN = "sft-v13-2e6-20260921-2054"

#: Второй предмет — **входной CPT-чекпойнт**, т.е. прежний вход SFT-стадии. Он нужен
#: не «для полноты»: и форматная проба (ADR-042 п.5: покрытие ответа и усечение — к
#: прежнему входу), и агентная (ADR-041 п.3: база переснимается в штатном режиме)
#: сравнивают SFT-финал именно с ним. Мерить живой каталог CPT-прогона — тот же
#: риск ретенции, от которого бронь и заводилась (ADR-058: «все приборы читают
#: копию»), поэтому ссылка ставится и на него.
AUX_SUBJECTS = [
    {"name": "cpt_checkpoint_final.pt", "role": "входной CPT-чекпойнт (прежний вход SFT)",
     "run": "full-cpt-20260916-2149",
     "src": SHARED / "full-cpt-20260916-2149" / "checkpoints" / "checkpoint_final.pt"},
]

#: Объявленный набор SFT-стадии (ADR-051: объявленное обязано совпасть с прочитанным).
#: Число — из манифеста прогона (`checkpoints/run_manifest.json`), не подбирается.
SFT_DATA = f"{CTR_SHARED}/datasets/sft_train_v13_fixed.jsonl"
SFT_DATA_SHA256 = "71c4bd2b011b210b906e7c77eca373ad094c14264b86353189faaa0596983b01"
RL_DATA = f"{CTR_SHARED}/datasets/rl_tasks_revpool_v2.jsonl"

MODEL = "Qwen/Qwen2.5-0.5B"
IMAGE = "nvcr.io/nvidia/pytorch:26.07-py3-vllm"
SEED = 42
MAX_LEN = 8192
MAX_SAMPLES = 50000
#: 192 — объём набора судьи (S4-PROTOCOL §2: SE ≈ 3.6 % при p≈0.5, n=192). Не «сколько
#: успеем»: число стоит в протоколе до замера, и сборщик его не понижает.
EVAL_ITEMS = 192
STALL_MINUTES = 120
SAMPLER_SECONDS = 60

#: Файлы цепочки, доставляемые в каталог прогона. Состав — как у обучающего
#: сборщика, минус то, что относится к обучению (маска, патч пайплайна), плюс копия
#: **базового** пайплайна: eval-стадия исполняет тот же прибор, что дал базы K1/K2,
#: а не SFT-копию с монитором форгеттинга (монитор в eval не участвует).
SHIPPED = [
    "tools/pilot_chain.sh",
    "tools/check_resource_owner.sh",
    "tools/write_run_manifest.py",
    "tools/chain_sleep_guard.sh",
    "tools/check_gpu_sleep_guard.py",
    "tools/smoke_mem_sampler.sh",
    "tools/rl_degeneracy.py",
]
#: Копия пайплайна едет отдельно: источник — симлинк кейса на сетевой диск.
PIPELINE_SRC = Path("/home/user/gb10-shared/laguna_pipeline_v8.py")
PIPELINE_NAME = "laguna_pipeline_v8.py"
#: Имя маркера «каталог не является каталогом прогона» (то же, что у гейта AD-2).
NOT_A_RUN_NAME = "NOT_A_RUN"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def ssh(script: str, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    """Исполнить скрипт на стенде через `bash -s`.

    stdin, а не `ssh '…'`: строка команды цепочки и так собирается из десятков
    кавычек, и второй слой экранирования — это ровно тот механизм, на котором
    «записанное» расходится с «исполненным» (класс ADR-057 I6).
    """
    p = subprocess.run(["ssh", STAND, "bash -s"], input=script, text=True,
                       capture_output=True, timeout=timeout)
    if check and p.returncode != 0:
        note(f"ssh rc={p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
    return p


def run_name(ts: str) -> str:
    return f"sft-point-chain-{ts}"


def run_dir(ts: str) -> Path:
    return SHARED / run_name(ts)


def ctr_run_dir(ts: str) -> str:
    return f"{CTR_SHARED}/{run_name(ts)}"


# ── шаг 1: каталог, бронь, доставка ──────────────────────────────────────────

def book_checkpoint(rd: Path, ts: str) -> dict:
    """Бронь предмета: ссылка в `ckpt/` **до** первого замера, хеш — с неё.

    Почему ссылка, а не копия. SFT-POINT-CHAIN §2 разрешает «копию или хардлинк», и
    выбор между ними решает не удобство, а AD-4: копии весов запрещены (страж
    C-011 — glob по рабочим каталогам и `git ls-files` по размеру). Хардлинк не
    дублирует байты, но переживает `unlink` исходного имени — то есть ровно тот
    сценарий, ради которого бронь вводилась (ретенция трейнера удалила предмет под
    пробой). Отличие от копии называем прямо: **перезапись** исходного inode
    ссылку не спасёт; от ретенции-удаления — спасает.

    Вторая ссылка (`checkpoints/sft_checkpoint_final.pt`) — предусловие
    `pilot_chain.sh` для стадии `eval_sft`: цепочка ищет чекпойнт по этому пути.
    Тот же inode, не второй предмет.
    """
    src = str(CKPT_SRC)
    ckpt_link = f"{rd}/ckpt/sft_checkpoint_final.pt"
    stage_link = f"{rd}/checkpoints/sft_checkpoint_final.pt"
    aux = ""
    for a in AUX_SUBJECTS:
        aux += (f"ln_sudo {shlex.quote(str(a['src']))} "
                f"{shlex.quote(str(rd))}/ckpt/{a['name']}\n")
    sc = f"""
set -euo pipefail
mkdir -p {shlex.quote(str(rd))}/ckpt {shlex.quote(str(rd))}/checkpoints \\
         {shlex.quote(str(rd))}/logs {shlex.quote(str(rd))}/var/status
# protected_hardlinks=1 + файл под root: без sudo ссылку не поставить. Функция
# идемпотентна — повторный прогон под живой цепочкой не трогает уже стоящие
# ссылки (пересоздание инода было бы подменой предмета под замером).
ln_sudo() {{
  [ -e "$2" ] && return 0
  sudo ln "$1" "$2"
}}
[ -f {shlex.quote(src)} ] || {{ echo "НЕТ ПРЕДМЕТА: {src}" >&2; exit 1; }}
ln_sudo {shlex.quote(src)} {shlex.quote(ckpt_link)}
ln_sudo {shlex.quote(src)} {shlex.quote(stage_link)}
{aux}
sha256sum {shlex.quote(ckpt_link)}
stat -c '%s %i %h %Y' {shlex.quote(ckpt_link)}
stat -c '%s %i %h %Y' {shlex.quote(src)}
python3 - <<'PY'
import hashlib, json, os
for name in {json.dumps([a['name'] for a in AUX_SUBJECTS])}:
    p = os.path.join({json.dumps(str(rd))}, "ckpt", name)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    st = os.stat(p)
    print(f"AUX {{name}} {{h.hexdigest()}} {{st.st_size}} {{st.st_ino}} {{st.st_nlink}}")
PY
"""
    p = ssh(sc, timeout=3600)
    if p.returncode != 0:
        note("бронь предмета не встала")
        raise SystemExit(1)
    lines = [l for l in p.stdout.strip().splitlines() if l.strip()]
    sha_copy = lines[0].split()[0]
    size, inode, links, mtime = lines[1].split()
    src_size, src_inode, _src_links, _ = lines[2].split()
    if (size, inode) != (src_size, src_inode):
        note(f"ОТКАЗ: бронь не тот inode (копия {inode}, источник {src_inode})")
        raise SystemExit(1)
    aux_booked = []
    for line in lines[3:]:
        if not line.startswith("AUX "):
            continue
        _, name, sha, asize, ainode, alinks = line.split()
        a = next(x for x in AUX_SUBJECTS if x["name"] == name)
        aux_booked.append({
            "name": name, "role": a["role"], "run": a["run"], "source": str(a["src"]),
            "copy": f"{rd}/ckpt/{name}", "sha256": sha, "size_bytes": int(asize),
            "inode": int(ainode), "link_count": int(alinks), "method": "hardlink"})
    receipt = {
        "artifact": "sft_checkpoint_final.pt",
        "booking": "SFT-POINT-CHAIN §2/§3#1 — предмет бронируется до первого замера",
        "method": "hardlink",
        "method_note": (
            "SFT-POINT-CHAIN §2 допускает «копию или хардлинк»; выбран хардлинк: AD-4 "
            "запрещает копии весов (C-011), хардлинк не дублирует байты и переживает "
            "unlink исходного имени (ретенция трейнера). Перезапись исходного inode "
            "ссылку не спасёт — отличие от копии названо, а не умолчано. reflink "
            "недоступен (ФС без reflink), ln под protected_hardlinks=1 требует прав "
            "на файл — ссылка поставлена sudo (на стенде sudo -n)."),
        "source": src,
        "copy": ckpt_link,
        "secondary_link": stage_link,
        "secondary_link_note": (
            "предусловие pilot_chain.sh для стадии eval_sft "
            "(RUN_DIR/checkpoints/sft_checkpoint_final.pt); тот же inode"),
        "sha256": sha_copy,
        "sha256_taken_from": "copy",
        "size_bytes": int(size),
        "inode": int(inode),
        "link_count": int(links),
        "mtime_epoch": int(mtime),
        "booked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "booked_from_host": "local (case tools/build_sft_point_chain.py) via ssh " + STAND,
        "run": SOURCE_RUN,
        "model": MODEL,
        "verdict": "ok",
        "aux_subjects": aux_booked,
        "aux_subjects_note": (
            "ссылки на входной CPT-чекпойнт — для сравнительных состояний приборов "
            "(формат: прежний вход; агентность: переснятие базы ADR-041 п.3). Не "
            "второй предмет стадии: вердикты ADR-033 п.2 выносятся по SFT-финалу"),
        "stop_condition_check": (
            "SFT-POINT-CHAIN §6: бронь указывает на тот же inode, что источник — "
            "предмет брендирован"),
    }
    Path("/tmp/sftpc_RECEIPT.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["scp", "-q", "/tmp/sftpc_RECEIPT.json",
                    f"{STAND}:{rd}/ckpt/RECEIPT.json"], check=True)
    return receipt


def ship_files(rd: Path, ts: str) -> dict:
    """Доставить файлы цепочки и записать `ship.json` (AD-2: доехало — по хешу)."""
    ship: dict[str, str] = {}
    for rel in SHIPPED:
        src = CASE_ROOT / rel
        body = src.read_bytes()
        name = src.name
        subprocess.run(["ssh", STAND, f"cat > {shlex.quote(str(rd / name))}"],
                       input=body, check=True)
        ship[name] = hashlib.sha256(body).hexdigest()
    # Копия пайплайна: источник — файл на сетевом диске (симлинк кейса ведёт туда же).
    pipe_bytes = PIPELINE_SRC.read_bytes()
    subprocess.run(["ssh", STAND, f"cat > {shlex.quote(str(rd / PIPELINE_NAME))}"],
                   input=pipe_bytes, check=True)
    ship[PIPELINE_NAME] = hashlib.sha256(pipe_bytes).hexdigest()
    subprocess.run(["ssh", STAND, f"chmod +x {shlex.quote(str(rd))}/*.sh"],
                   check=False)
    # Проверка фактом: хеши на стенде — те же, что записаны (иначе ship.json — декларация).
    check = "set -e\n" + "".join(
        f"echo \"{h}  {rd / n}\" | sha256sum -c -\n" for n, h in ship.items())
    p = ssh(check)
    mismatched = []
    for line in p.stdout.splitlines():
        if ": FAILED" in line:
            mismatched.append(line.split(":")[0])
    payload = {"ok": not mismatched, "stand_dir": str(rd), "ctr_dir": ctr_run_dir(ts),
               "sha256": ship, "mismatched": mismatched,
               "checked_from": f"ssh {STAND} — {rd}"}
    (Path("/tmp") / f"sftpc_ship_{ts}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["scp", "-q", f"/tmp/sftpc_ship_{ts}.json",
                    f"{STAND}:{rd}/ship.json"], check=True)
    if mismatched:
        note(f"ОТКАЗ: хеши не сошлись на стенде: {mismatched}")
        raise SystemExit(1)
    return payload


def write_stages(rd: Path, ts: str) -> str:
    """Файл стадий: только `eval_*` (формат колонок — как у `pilot_chain.sh:593`).

    Порядок — **SFT первым, база второй**. Обе стадии длинные (судья GLM: 192
    элемента × десятки секунд), поэтому «сначала быстрые» внутри пары не работает;
    работает другое правило — **ось AD-1 нужна раньше ориентира**. `eval_sft` даёт
    число, которым закрывается ADR-033 п.2; `eval_base` даёт точку сравнения. Если
    прогон оборвётся, вопрос «выучен ли домен» уже имеет ответ, а «относительно
    чего» — ещё нет, и это названо, а не скрыто порядком файла.
    """
    rows = [
        ("eval_sft", "eval", "sft", "logs/eval_results_sft.json", "1", "1", "sft",
         f"{run_name(ts)}-eval_sft"),
        ("eval_base", "eval", "sft", "logs/eval_results_base.json", "1", "1", "base",
         f"{run_name(ts)}-eval_base"),
    ]
    body = "".join("\t".join(r) + "\n" for r in rows)
    subprocess.run(["ssh", STAND, f"cat > {shlex.quote(str(rd / 'stages.tsv'))}"],
                   input=body, text=True, check=True)
    return body


def write_chain_command(rd: Path, ts: str) -> str:
    """Записать **ту самую** строку, которой пойдёт цепочка (ADR-057 I6).

    Строка собирается здесь, кладётся в `chain_command.txt` и исполняется запуском
    без правок: «ручной путь и автоматический — один код». Первый шаг —
    `chain_sleep_guard.sh` (C-029): на `gb10-fast` инхибит не берётся, и шаг решает
    на площадке прогона, доказана ли недостижимость сна.
    """
    runner_sha12 = sha256_file(Path(__file__))[:12]
    rd = rd
    a = ["bash", f"{rd}/chain_sleep_guard.sh", "bash", f"{rd}/pilot_chain.sh",
         "--run-dir", str(rd), "--ctr-run-dir", ctr_run_dir(ts),
         "--stages-file", f"{rd}/stages.tsv",
         "--exp-base", run_name(ts), "--ctr-prefix", f"laguna-{run_name(ts)}",
         "--shared", str(SHARED), "--experiments", EXPERIMENTS,
         "--pipeline", f"{rd}/{PIPELINE_NAME}",
         "--pipeline-ctr", f"{ctr_run_dir(ts)}/{PIPELINE_NAME}",
         "--cpt-data", f"{CTR_SHARED}/datasets/cpt_corpus_v12r.txt",
         "--dataset", str(SHARED / "datasets/tok/cpt_corpus_v12r_8192_qwen25.npy"),
         # ADR-051: объявление набора SFT и его полный хеш. Стадия `eval_sft` несёт
         # поле `sft` в файле стадий, поэтому цепочка сверяет объявленное с файлом
         # на стенде — и это же объявление уходит в манифест AD-2.
         "--sft-data", SFT_DATA, "--sft-data-sha256", SFT_DATA_SHA256,
         "--rl-data", RL_DATA,
         "--runner-name", "tools/build_sft_point_chain.py",
         "--runner-sha12", runner_sha12,
         # Предмет замера назван в манифесте прогона: «проба указывает на другой
         # чекпойнт, чем бронь» — стоп-условие §6, и оно проверяется сверкой этих
         # полей с RECEIPT.json, а не памятью исполнителя.
         "--extra-hyperparam", f"point_ckpt={rd}/ckpt/sft_checkpoint_final.pt",
         "--extra-hyperparam", f"point_ckpt_sha256={_booked_sha(rd)}",
         "--extra-hyperparam", f"point_source_run={SOURCE_RUN}",
         "--extra-hyperparam", "point_adr=SFT-POINT-CHAIN",
         "--extra-hyperparam", "instrument=laguna_pipeline_v8.py:eval_stage+GLM-judge",
         "--extra-hyperparam", f"eval_judge=GLM-API:eval_items={EVAL_ITEMS}",
         "--guard", f"{rd}/check_resource_owner.sh",
         "--safe-start", "/home/user/gb10-shared/nvrm-storm/safe_start.sh",
         "--sampler", f"{rd}/smoke_mem_sampler.sh",
         "--sampler-seconds", str(SAMPLER_SECONDS),
         "--storm-gap", "/home/user/gb10-shared/nvrm-storm/storm_gap.sh",
         "--manifest-tool", f"{rd}/write_run_manifest.py",
         "--degeneracy-tool", f"{rd}/rl_degeneracy.py",
         "--image", IMAGE,
         "--glm-env", "/home/user/gb10-shared/.glm_env",
         "--nvrm-log", "/home/user/experiments/nvrm_watch.log",
         "--model", MODEL, "--seed", str(SEED),
         "--max-len", str(MAX_LEN), "--max-samples", str(MAX_SAMPLES),
         "--eval-items", str(EVAL_ITEMS),
         "--attn", "flex", "--mem-cap", "100g",
         "--stall-minutes", str(STALL_MINUTES),
         ]
    cmd = " ".join(shlex.quote(x) for x in a)
    subprocess.run(["ssh", STAND, f"cat > {shlex.quote(str(rd / 'chain_command.txt'))}"],
                   input=cmd + "\n", text=True, check=True)
    launch = (f"ssh {STAND} 'tmux kill-session -t {run_name(ts)} 2>/dev/null; "
              f"tmux new-session -d -s {run_name(ts)} \"setsid nohup {cmd} "
              f">> {rd}/chain.log 2>&1 < /dev/null\"'")
    subprocess.run(["ssh", STAND, f"cat > {shlex.quote(str(rd / 'launch_command.txt'))}"],
                   input=launch + "\n", text=True, check=True)
    return cmd


def _booked_sha(rd: Path) -> str:
    p = Path("/tmp/sftpc_RECEIPT.json")
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))["sha256"]
    r = ssh(f"cat {shlex.quote(str(rd / 'ckpt/RECEIPT.json'))}")
    return json.loads(r.stdout)["sha256"]


# ── шаги 2–5 ─────────────────────────────────────────────────────────────────

def do_check(rd: Path, ts: str) -> int:
    cmd = (rd / "chain_command.txt").read_text(encoding="utf-8").strip()
    p = ssh(f"cd {shlex.quote(str(rd))} && {cmd} --check-only\n", timeout=600)
    print(p.stdout)
    if p.stderr.strip():
        note(p.stderr)
    return 0 if p.returncode == 0 else 2


def do_launch(rd: Path, ts: str) -> int:
    launch = (rd / "launch_command.txt").read_text(encoding="utf-8").strip()
    p = subprocess.run(launch, shell=True, text=True, capture_output=True, timeout=120)
    if p.returncode != 0:
        note(f"launch rc={p.returncode}\n{p.stdout}\n{p.stderr}")
        return 2
    return 0


def do_facts(rd: Path, ts: str) -> int:
    name = run_name(ts)
    sc = f"""echo "== tmux =="; tmux ls 2>&1 | grep {shlex.quote(name)} || echo "нет сессии"
echo "== контейнер =="; docker ps --format '{{{{.Names}}}}|{{{{.Status}}}}' 2>&1 | grep {shlex.quote(name)} || echo "нет контейнера"
echo "== статусы стадий =="; cat {shlex.quote(str(rd))}/var/status/* 2>/dev/null || echo "нет статусов"
echo "== chain.status =="; cat {shlex.quote(str(rd))}/var/chain.status 2>/dev/null || echo "нет"
echo "== хвост chain.log =="; tail -25 {shlex.quote(str(rd))}/chain.log 2>/dev/null || echo "нет лога"
"""
    p = ssh(sc, timeout=120)
    print(p.stdout)
    return 0 if p.returncode == 0 else 2


def do_mirror(rd: Path, ts: str) -> int:
    """Малые артефакты каталога — в дерево кейса (`runs/<имя>/`), веса — нет.

    Копий весов в кейсе не бывает (AD-4/C-011): `ckpt/` и `checkpoints/` не
    зеркалятся **никогда** — ни файлом, ни симлинком. Манифест AD-2 зеркалится
    всегда: гейт читает прогоны под `runs/`, и прогон без манифеста стал бы
    находкой C-012 (класс S3ao).
    """
    dest = CASE_ROOT / "runs" / run_name(ts)
    dest.mkdir(parents=True, exist_ok=True)
    #: Гейт AD-2 читает прогоны под `runs/` и требует манифест от **каждого**
    #: каталога. Пока цепочка не закрыла первую стадию, её манифеста нет (цепочка
    #: зовёт `write_run_manifest.py` по завершении стадии, а не на старте) — и
    #: зеркало без манифеста красило бы C-012 находкой на пустом месте. До него
    #: каталог объявляется тем, чем он в этот момент и является: **каталогом свода**
    #: (прецедент — `runs/sft-v13-arm-20260921`, «каталог свода S3ax»), а сам прогон
    #: со `stages.tsv`, логами и манифестом живёт на сетевом диске. Как только
    #: манифест приезжает зеркалом, маркер снимается — иначе получилось бы
    #: запрещённое «и маркер, и манифест» (манифест нельзя прятать за маркером).
    marker = dest / NOT_A_RUN_NAME
    if (dest / "run_manifest.json").is_file():
        marker.unlink(missing_ok=True)
    else:
        marker.write_text(
            f"NOT_A_RUN — каталог свода цепочки {run_name(ts)}, а не каталог прогона.\n"
            "Прогон со stages.tsv, логами и манифестом AD-2 живёт на сетевом диске:\n"
            f"  {rd}\n"
            "Сюда зеркалятся только малые артефакты (отчёт, JSON приборов, расписки) —\n"
            "веса и чекпойнты не зеркалятся никогда (AD-4). Манифест ещё не записан:\n"
            "цепочка пишет его по завершении стадии. Как только он приедет зеркалом,\n"
            "этот маркер снимается автоматически (tools/build_sft_point_chain.py --do mirror).\n",
            encoding="utf-8")
    sc = (f"cd {shlex.quote(str(rd))} && "
          "tar -cf - --exclude=ckpt --exclude=checkpoints --exclude='*.pt' "
          "--exclude=var/chain.pid --exclude=var/STOPPED --exclude=__pycache__ --exclude='*.pyc' . 2>/dev/null | base64 -w0")
    p = ssh(sc, timeout=600)
    if p.returncode != 0 or not p.stdout.strip():
        note("зеркало не собрано (нет каталога прогона?)")
        return 2
    import base64
    import io
    import tarfile
    blob = base64.b64decode(p.stdout)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(dest)
    print(f"зеркало: {dest}")
    for f in sorted(dest.rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(dest)}  {f.stat().st_size}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ts", required=True, help="метка цепочки (YYYYMMDD-HHMM)")
    ap.add_argument("--do", default="build",
                    choices=["build", "book", "check", "launch", "facts", "mirror", "all"])
    args = ap.parse_args()
    rd = run_dir(args.ts)

    #: `book` отдельно от `build` — потому что бронь нужна и тогда, когда каталог
    #: уже живёт: доставка файлов под **живой** цепочкой запрещена (ADR-016 п.1:
    #: цепочка исполняет тот код, что лежит в её каталоге), а поставить ссылку на
    #: второй предмет — действие, не трогающее ни одного исполняемого файла.
    if args.do == "book":
        rec = book_checkpoint(rd, args.ts)
        print(f"бронь: {rec['method']} sha256={rec['sha256'][:16]}… inode={rec['inode']}")
        for a in rec.get("aux_subjects", []):
            print(f"  вспомогательный предмет: {a['name']} ({a['role']}) "
                  f"sha256={a['sha256'][:16]}… inode={a['inode']}")
        return 0
    if args.do in ("build", "all"):
        print(f"== каталог цепочки: {rd} (контейнер: {ctr_run_dir(args.ts)})")
        rec = book_checkpoint(rd, args.ts)
        print(f"бронь: {rec['method']} sha256={rec['sha256'][:16]}… "
              f"inode={rec['inode']} links={rec['link_count']}")
        ship = ship_files(rd, args.ts)
        print(f"доставлено файлов: {len(ship['sha256'])}, хеши сошлись на стенде: {ship['ok']}")
        write_stages(rd, args.ts)
        cmd = write_chain_command(rd, args.ts)
        print("записанная команда цепочки:")
        print("  " + cmd)
    if args.do in ("check", "all"):
        rc = do_check(rd, args.ts)
        if rc != 0:
            note("предусловие не выполнено — цепочка не стартует (это правило)")
            return rc
    if args.do == "launch" or args.do == "all":
        rc = do_launch(rd, args.ts)
        if rc != 0:
            return rc
        print("цепочка запущена отсоединённо (tmux)")
        do_facts(rd, args.ts)
    if args.do == "facts":
        return do_facts(rd, args.ts)
    if args.do == "mirror":
        return do_mirror(rd, args.ts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
