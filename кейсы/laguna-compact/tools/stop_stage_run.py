#!/usr/bin/env python3
"""S3ae: штатная остановка живого прогона стадии (ADR-016) и запись следа остановки.

Зачем отдельный инструмент, а не три команды руками. Остановка живой стадии —
это **вмешательство**, и оно обязано быть воспроизводимым и оставить след, иначе
остановленный прогон перестаёт быть доказательством: непонятно, чем именно он
остановлен, на каком шаге, целы ли артефакты и почему в манифесте `stopped`.

Протокол — ровно тот, что записан в `pilot_chain.sh` и ADR-016:

1. **Маркер `var/STOPPED` первым.** Наблюдатель стадии (`watch_stage`) крутится
   `while [ ! -f "$STOP_MARKER" ]`: маркер его останавливает, а `attempt_stage`
   после выхода контейнера читает маркер и записывает стадии статус `stopped`
   (не `failed`), `chain.status = stopped`, код возврата цепочки 3. Без маркера
   тот же выход контейнера был бы записан как падение — след был бы ложным.
2. **`docker stop -t 120` (SIGTERM, затем SIGKILL через 120 с).** Без `kill -9`:
   сигнал даёт Python шанс закрыть файлы, а `--rm`-контейнер убирается сам.
   Чекпойнты пишутся атомарно (`save_checkpoint_atomic`), поэтому обрыв записи
   не оставляет битого файла — но именно поэтому же «последний шаг» берётся из
   `logs/loss_trace.jsonl`, а не из имён чекпойнтов (они пишутся каждые 200 шагов).
3. **Ничего не удаляется.** Остановленный прогон — артефакт «SFT без маски think»
   (ADR-036): его чекпойнты, `loss_trace.jsonl`, логи и манифест сохраняются, а в
   манифест добавляется пометка `stopped_by`/`stopped_at_step`/`purpose`.

Пометка в манифест пишется **после** выхода цепочки: `write_run_manifest.py`
перезаписывает `run_manifest.json` по завершении стадии (R6), и правка под живым
писателем была бы потеряна (ADR-016 п.1 запрещает правки под живой цепочкой —
здесь это не запрет, а условие корректности).

Коды возврата::

    0 — остановлено (или уже остановлено) и след записан
    2 — NOT-VERIFIED: прогон/контейнер/манифест не найдены — останавливать нечего
    3 — маркер поставлен, но контейнер не остановился за отведённое время
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STAND = "gb10-fast"
SHARED = Path("/home/user/gb10-shared")
CASE_ROOT = Path(__file__).resolve().parent.parent


def ssh(cmd: str, timeout: int = 180) -> tuple[int, str, str]:
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", STAND, cmd],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def last_trace_step(run_dir: Path) -> int | None:
    """Последний записанный шаг — по траектории, а не по имени чекпойнта."""
    lt = run_dir / "logs" / "loss_trace.jsonl"
    if not lt.is_file():
        return None
    lines = lt.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    if not lines:
        return None
    try:
        return int(json.loads(lines[-1])["step"])
    except Exception:
        return None


def snapshot(run_dir: Path) -> dict:
    """Инвентарь артефактов остановленного прогона: что именно сохранено."""
    ck = run_dir / "checkpoints"
    ckpts = []
    if ck.is_dir():
        for p in sorted(ck.glob("*.pt")):
            st = p.stat()
            ckpts.append({"name": p.name, "size": st.st_size,
                          "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()})
    def tail_lines(p: Path, n: int = 3) -> list[str]:
        if not p.is_file():
            return []
        return p.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-n:]
    return {
        "checkpoints": ckpts,
        "checkpoints_count": len(ckpts),
        "checkpoints_bytes": sum(c["size"] for c in ckpts),
        "loss_trace_lines": (len((run_dir / "logs" / "loss_trace.jsonl")
                                 .read_text(encoding="utf-8", errors="replace").strip().splitlines())
                             if (run_dir / "logs" / "loss_trace.jsonl").is_file() else 0),
        "logs": sorted(p.name for p in (run_dir / "logs").glob("*")) if (run_dir / "logs").is_dir() else [],
        "sft_log_tail": tail_lines(run_dir / "logs" / "sft.log", 6),
        "chain_log_tail": tail_lines(run_dir / "chain.log", 6),
    }


def do_stop(run_dir: Path, reason: str, timeout_s: int) -> int:
    marker = run_dir / "var" / "STOPPED"
    ctr = f"laguna-{run_dir.name}-sft"
    running = ssh(f"docker ps --format '{{{{.Names}}}}' | grep -x {shlex.quote(ctr)} || true")[1]
    if not running:
        print(json.dumps({"verdict": "NOT-VERIFIED", "why": "контейнер стадии не запущен",
                          "container": ctr, "run_dir": str(run_dir)}, ensure_ascii=False, indent=2))
        return 2
    step_before = last_trace_step(run_dir)
    # 1 — маркер: без него остановка запишется как падение (failed), а не как stopped.
    rc, out, err = ssh(f"printf '%s\\n' {shlex.quote(reason)} > {marker}; head -1 {marker}")
    if rc != 0:
        print(f"маркер не поставлен: {err}", file=sys.stderr)
        return 2
    # 2 — штатная остановка: SIGTERM, затем SIGKILL через timeout_s.
    t0 = time.time()
    rc, out, err = ssh(f"docker stop -t {timeout_s} {shlex.quote(ctr)}", timeout=timeout_s + 120)
    wall = round(time.time() - t0, 1)
    stopped = out.strip() == ctr
    step_after = last_trace_step(run_dir)
    res = {"verdict": "STOPPED" if stopped else "NOT-STOPPED", "marker": str(marker),
           "marker_text": reason, "container": ctr, "container_rc": rc,
           "stop_stdout": out.strip(), "stop_wall_seconds": wall,
           "step_before": step_before, "step_after": step_after,
           "stopped_at_step": step_after if step_after is not None else step_before}
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if stopped else 3


def do_record(run_dir: Path, stopped_by: str, purpose: str, mask: str, extra: dict,
              note: str | None = None) -> int:
    """След остановки: инвентарь артефактов + пометка в манифесте (после выхода цепочки)."""
    chain_status = (run_dir / "var" / "chain.status")
    status = chain_status.read_text().strip() if chain_status.is_file() else None
    stage_status = None
    st = run_dir / "var" / "status" / "sft"
    if st.is_file():
        stage_status = st.read_text().strip().split("\t")[0]
    snap = snapshot(run_dir)
    record = {
        "run_dir": str(run_dir), "stopped_by": stopped_by, "purpose": purpose,
        "loss_mask": mask,
        "stopped_at_step": last_trace_step(run_dir),
        "chain_status": status, "stage_status": stage_status,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": snap, **extra,
    }
    # Пометка в манифест. Пишется после того, как цепочка вышла (иначе её перезапишет
    # write_run_manifest.py) — проверяется фактом: статус цепочки уже терминальный.
    man = run_dir / "run_manifest.json"
    if man.is_file():
        m = json.loads(man.read_text(encoding="utf-8"))
        m["stopped_by"] = stopped_by
        m["stopped_at_step"] = record["stopped_at_step"]
        m["purpose"] = purpose
        m["loss_mask"] = mask
        #: Формулировка пометки — параметр, а не константа: остановок у стадии
        #: бывает больше одной и по разным причинам (ADR-036 — маска; ADR-039 —
        #: достигнутый шаг эксперимента). Жёсткая строка приписала бы второй
        #: остановке смысл первой, то есть соврала бы в манифесте.
        m["stopped_note"] = note or (
            "прогон остановлен штатно по ADR-016 (маркер var/STOPPED → docker stop -t 120); "
            "артефакты сохранены полностью и являются артефактом «SFT без маски think»")
        for s in m.get("stages", []):
            if s.get("name") == "sft":
                s["status"] = "stopped"
        man.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record["manifest_note"] = {k: m[k] for k in
                                   ("stopped_by", "stopped_at_step", "purpose", "loss_mask")}
    else:
        record["manifest_note"] = None
    (run_dir / "STOP-RECORD.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def do_annotate(run_dir: Path, note: dict) -> int:
    """Дописать в манифест/запись остановки пометку о последующем решении.

    Остановка — факт, и он не переписывается: `stopped_by` остаётся тем решением,
    которое её санкционировало. Но если позже решение отменено (ADR-037), в
    манифесте обязана быть ссылка — иначе читатель через месяц решит, что прогон
    ждёт перезапуска по отменённому решению.
    """
    man = run_dir / "run_manifest.json"
    rec = run_dir / "STOP-RECORD.json"
    for path, kind in ((man, "manifest"), (rec, "stop_record")):
        if not path.is_file():
            continue
        d = json.loads(path.read_text(encoding="utf-8"))
        d.update(note)
        path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"обновлено ({kind}): {path}")
    for k, v in note.items():
        print(f"  {k}: {v}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="каталог прогона (на стенде, он же в NFS)")
    ap.add_argument("--do", required=True, choices=["stop", "record", "annotate"])
    ap.add_argument("--superseded-by", default=None,
                    help="решение, отменившее предмет дельты (annotate): в манифест "
                         "пишется stopped_confirmed_by/superseded_by")
    ap.add_argument("--superseded-note", default=None)
    ap.add_argument("--reason", default="ADR-036: маскирование <think> в лоссе — стадия перезапускается")
    ap.add_argument("--timeout", type=int, default=120, help="docker stop -t (ADR-016: 120)")
    ap.add_argument("--stopped-by", default="ADR-036")
    ap.add_argument("--purpose", default="артефакт «SFT без маски think»")
    ap.add_argument("--loss-mask", default="prefix_only")
    ap.add_argument("--stopped-note", default=None,
                    help="чем является остановленный прогон (пишется в манифест); "
                         "умолчание — формулировка ADR-036")
    a = ap.parse_args()
    run_dir = Path(a.run)
    if not run_dir.is_dir():
        print(f"каталога прогона нет: {run_dir}", file=sys.stderr)
        return 2
    if a.do == "stop":
        return do_stop(run_dir, a.reason, a.timeout)
    if a.do == "annotate":
        if not a.superseded_by:
            print("annotate требует --superseded-by", file=sys.stderr)
            return 2
        return do_annotate(run_dir, {
            "stopped_confirmed_by": a.superseded_by,
            "stopped_remains_valid": True,
            "superseded_note": a.superseded_note or
            f"{a.superseded_by}: предмет дельты изменён; остановка и артефакты остаются "
            "в силе и служат основанием нового решения",
        })
    return do_record(run_dir, a.stopped_by, a.purpose, a.loss_mask, {},
                     note=a.stopped_note)


if __name__ == "__main__":
    raise SystemExit(main())
