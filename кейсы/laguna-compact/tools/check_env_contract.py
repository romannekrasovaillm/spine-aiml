#!/usr/bin/env python3
"""Страж среды заземлённой задачи (ADR-049 п.1 G4): egress, лимит шагов, супервизор.

**Что проверяется — и почему именно так.**

1. **Выход наружу закрыт — ПОПЫТКОЙ выхода, а не флагом запуска.** Проба 20.09.2026
   показала: на дефолтной `bridge`-сети контейнер достаёт до `http://1.1.1.1/`, то
   есть награду можно получить внешним вызовом, а не состоянием, — это подрывает
   саму ось ADR-049. Проверка «в конфигурации выставлен `--internal`» стражем НЕ
   считается: она не поймает возврат дыры. Поэтому страж поднимает контейнер и
   пробует выйти тремя независимыми способами (публичный IP, DNS, канарейка-
   контейнер на дефолтной bridge) и требует, чтобы **каждая** попытка провалилась,
   а мок был достижим. Флаг сети при этом не читается вообще.

1b. **Мок — sidecar в сети прогона, и это проверяется по следу механизма**
   (поправка ADR-049 п.10 от 20.09.2026). Слушатель на шлюзе сети требовал, чтобы
   раннер занимал адрес ХОСТА: из контейнера стадии это `Errno 99`, то есть проба
   изоляции была исполнима только там, где живёт демон. Страж поэтому требует от
   доказательства следа именно sidecar'а — имя, образ, адрес В СЕТИ ПРОГОНА,
   `in_run_network: true`, — и живости канарейки (`self_check`). Доказательство,
   снятое прежним механизмом (мок на шлюзе), краснеет: «мок достижим» без
   названного механизма не отличает sidecar от слушателя на адресе хоста.

2. **Лимит шагов — часть схемы задачи.** `task.json` обязан нести `max_steps`
   (`grounded-task/2`); задача без поля — отказ набора, а не «без лимита»: иначе
   G4 выполняется лишь тогда, когда лимит реализует вызывающая сторона.

3. **Шаг под супервизором.** Измерено: клиентский таймаут `docker exec` процесс
   внутри контейнера не убивает и оставляет частичный след. Среда обязана снимать
   группу процессов сама и откатывать след. Доказательство (`step_limit`,
   `supervision`) обязано это подтверждать, а не заявлять.

**Почему страж берёт механику у раннера, а не повторяет её.** Второй проверкой
свойств среды страж обязан проверять ТУ ЖЕ среду: копия построения сети доказывала
бы корректность копии, а не раннера. Поэтому `create_internal_network`,
`egress_attempts`, `Sandbox`, `MockSidecar`, `Canary` импортируются из
`tools/grounded_runner.py` — правило `CONSTRAINTS.yaml` при этом ссылается только
на страж (AD-10), а раннер гейтом не вызывается: импортируется библиотека, не CLI.
Обратное направление зависимости (раннер → страж) AD-10 называет правильным; здесь
взято то же по смыслу — страж зависит от предмета проверки, а не наоборот.

**Три состояния, а не два.** Нет docker → NOT-VERIFIED (exit 2), а не «зелёный»:
молчание доказательством не является. Набор отсутствует → тоже NOT-VERIFIED.
Доказательство отсутствует → живые пробы и схема задач проверяются, а части,
которые видны только из доказательства, честно помечаются `pending` (не красное):
правило не должно краснеть на пустом месте (ADR-023 п.12).

Коды возврата::

    0 — среда держит контракт (egress закрыт, мок достижим, задачи несут лимит)
    1 — красное: выход прошёл, либо мок недостижим, либо лимит шагов не часть схемы,
        либо доказательство этому противоречит
    2 — NOT-VERIFIED: нет docker или нет набора задач

**Проба из контейнера стадии.** Страж исполним и оттуда (`--work-root` обязателен):
каталог пробы монтируется в контейнеры демоном ХОСТА, поэтому он обязан быть виден
демону по ТОМУ ЖЕ пути, что у стража, — иначе `--mount` отказывает. Это не
придирка: раннер из контейнера стадии работает от root, а его `$HOME` (`/root`) на
хосте не смонтирован, поэтому каталог по умолчанию там не годится.

Примеры::

    python3 tools/check_env_contract.py                     # правило C-028
    python3 tools/check_env_contract.py --probe-network bridge   # зубы: выход откроется → красное
    python3 tools/check_env_contract.py --json
    python3 tools/check_env_contract.py --work-root /общий/путь  # из контейнера стадии
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import shutil
import sys

CASE_ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE_SCHEMA = "grounded-skeleton-evidence/1"
TASK_SCHEMA = "grounded-task/2"
RUN_TAG = "gs"


def load_runner(path: pathlib.Path):
    """Механика среды — из раннера: страж проверяет ТУ ЖЕ среду, что исполняет задачи."""
    spec = importlib.util.spec_from_file_location("grounded_runner_under_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_task_schema(set_dir: pathlib.Path) -> tuple[list[dict], dict]:
    """Каждая задача несёт `max_steps`: лимит шагов — часть схемы, а не харнесса.

    Там же считается число задач с сетью: механизм мока проверяется только там,
    где мок вообще есть, — иначе набор без мока краснел бы за отсутствие того,
    чего в нём не должно быть (ложный красный запрещён, ADR-023 п.12).
    """
    findings: list[dict] = []
    detail: dict = {"tasks": {}, "schema_expected": TASK_SCHEMA, "n_tasks_with_network": 0}
    for d in sorted((set_dir / "tasks").iterdir()):
        tj = d / "task.json"
        if not tj.is_file():
            continue
        spec = json.loads(tj.read_text(encoding="utf-8"))
        tid = spec.get("id", d.name)
        entry = {"schema": spec.get("schema"), "max_steps": spec.get("max_steps"),
                 "network": spec.get("network", "none")}
        if entry["network"] != "none":
            detail["n_tasks_with_network"] += 1
        detail["tasks"][tid] = entry
        if not isinstance(spec.get("max_steps"), int) or isinstance(spec.get("max_steps"), bool):
            findings.append({
                "severity": "error", "rule": "max-steps-missing",
                "message": f"{tid}: task.json не несёт целочисленного max_steps — лимит шагов держит "
                           "вызывающая сторона, и G4 выполняется лишь если харнесс сам реализует лимит",
            })
        elif spec["max_steps"] < 0:
            findings.append({
                "severity": "error", "rule": "max-steps-negative",
                "message": f"{tid}: max_steps отрицательный ({spec['max_steps']})",
            })
    detail["n_tasks"] = len(detail["tasks"])
    return findings, detail


def check_evidence(evidence: pathlib.Path, set_dir: pathlib.Path) -> tuple[list[dict], dict]:
    """Доказательство обязано подтверждать контракт среды фактом, а не заголовком."""
    findings: list[dict] = []
    if not evidence.is_file():
        return [], {"pending": "доказательства нет — контракт среды проверен живыми пробами и схемой задач"}
    doc = json.loads(evidence.read_text(encoding="utf-8"))
    if doc.get("schema") != EVIDENCE_SCHEMA:
        return [], {"pending": f"чужая схема доказательства {doc.get('schema')!r} — части из доказательства не читаются"}

    detail: dict = {"generated_at": doc.get("generated_at"), "status": doc.get("status")}

    def fail(rule: str, message: str) -> None:
        findings.append({"severity": "error", "rule": rule, "message": message})

    # 1. Лимит шагов: доказательство подтверждает, что бюджет связывает НАГРАДУ.
    step = doc.get("step_limit")
    if not step:
        fail("step-limit-unproven",
             "в доказательстве нет раздела step_limit: лимит шагов не подтверждён пробой "
             "(прогон без --step-probe контракт среды не доказывает)")
    else:
        detail["step_limit"] = {k: step.get(k) for k in ("binds_on_state",)}
        if step.get("binds_on_state") is not True:
            fail("step-limit-unproven",
                 f"лимит шагов не связывает награду: binds_on_state={step.get('binds_on_state')!r} — "
                 "бюджет 0 обязан давать 0, бюджет 1 → 1 (иначе лимит держит не среда)")
        for item in step.get("per_task", []):
            tid = item.get("task")
            probe = item.get("budget_probe", {})
            if probe.get("0", {}).get("score") != 0 or probe.get("1", {}).get("score") != 1:
                fail("step-limit-unproven",
                     f"{tid}: бюджет 0 → {probe.get('0', {}).get('score')!r}, бюджет 1 → {probe.get('1', {}).get('score')!r}")

    # 2. Супервизор шага: убитый по таймауту шаг не влияет на следующий rollout.
    sup = doc.get("supervision")
    if not sup:
        fail("step-supervision-unproven",
             "в доказательстве нет раздела supervision: добивание процесса и откат частичного следа "
             "не подтверждены пробой (прогон без --timeout-probe контракт среды не доказывает)")
    else:
        detail["supervision"] = {
            "next_rollout_unaffected": sup.get("next_rollout_unaffected"),
            "process_killed_by_environment": sup.get("process_killed_by_environment"),
        }
        if sup.get("process_killed_by_environment") is not True:
            fail("step-supervision-unproven",
                 "среда не сняла группу процессов убитого шага: процесс внутри контейнера переживает "
                 "клиентский таймаут `docker exec` (измерено 20.09.2026), и шаги копились бы")
        if sup.get("next_rollout_unaffected") is not True:
            fail("step-supervision-unproven",
                 "убитый по таймауту шаг повлиял на следующий rollout: частичный след не откачен")

    # 3. Лимит из доказательства не разошёлся с самим набором.
    for item in doc.get("tasks", []):
        tid = item.get("id")
        spec = item.get("spec", {})
        tj = set_dir / "tasks" / str(tid) / "task.json"
        if not tj.is_file():
            continue
        real = json.loads(tj.read_text(encoding="utf-8")).get("max_steps")
        if spec.get("max_steps") != real:
            fail("max-steps-drift",
                 f"{tid}: в доказательстве max_steps={spec.get('max_steps')!r}, в задаче {real!r} — "
                 "доказательство описывает другую редакцию задачи")

    # Мок и канарейка существуют в наборе только там, где есть задача с сетью:
    # проверки механизма условны, иначе набор без мока краснел бы за отсутствие
    # того, чего в нём не должно быть (ложный красный запрещён, ADR-023 п.12).
    has_mock = any(
        json.loads((d / "task.json").read_text(encoding="utf-8")).get("network", "none") != "none"
        for d in sorted((set_dir / "tasks").iterdir()) if (d / "task.json").is_file()
    )

    # 4. Сеть: доказательство обязано нести след попыток выхода, а не «профиль».
    for name, probes in (doc.get("isolation") or {}).items():
        if not isinstance(probes, dict):
            continue
        if probes.get("external_unreachable") is not True or probes.get("dns_unresolvable") is not True:
            fail("egress-open-in-evidence",
                 f"{name}: проба изоляции не подтвердила закрытый выход "
                 f"(external_unreachable={probes.get('external_unreachable')!r}, "
                 f"dns_unresolvable={probes.get('dns_unresolvable')!r})")
        # Канарейка: «недостижима» без попытки — молчание, а не доказательство.
        if has_mock and probes.get("canary_attempted") is not True:
            fail("canary-not-attempted",
                 f"{name}: попытки дойти до канарейки не было — «выход закрыт» на хосте без интернета "
                 "держалось именно канарейкой, и без неё доказательство вакуумно")
        elif has_mock and probes.get("canary_unreachable") is not True:
            fail("egress-open-in-evidence",
                 f"{name}: канарейка ДОСТИЖИМА из сети прогона — адрес вне сети прогона виден, "
                 "то есть выход не закрыт")
        if probes.get("network_profile", "").startswith("internal") and probes.get("mock_reachable") is not True:
            fail("mock-unreachable", f"{name}: мок не достижим из сети прогона — среда не пропускает действия (п.6)")

    # 4b. МЕХАНИЗМ мока: sidecar в сети прогона, а не слушатель на её шлюзе
    #     (поправка ADR-049 п.10 от 20.09.2026). Без этого следа «мок достижим»
    #     не отличает контейнер в сети прогона от процесса, занявшего адрес хоста.
    net = doc.get("network") or {}
    sidecars = net.get("sidecars") or {}
    mock_sc = sidecars.get("mock")
    if not has_mock:
        detail["mock_sidecar"] = {"skipped": "в наборе нет задач с сетью — мок не поднимается"}
    elif not isinstance(mock_sc, dict):
        fail("mock-mechanism-unnamed",
             "доказательство не называет мок как sidecar-контейнер (network.sidecars.mock): "
             "«мок достижим» без названного механизма не отличает контейнер в сети прогона "
             "от слушателя на её шлюзе, а слушатель из контейнера стадии неисполним (Errno 99)")
    else:
        detail["mock_sidecar"] = {k: mock_sc.get(k) for k in
                                  ("kind", "name", "image", "network", "ip", "url", "in_run_network")}
        if mock_sc.get("kind") != "container":
            fail("mock-mechanism-unnamed",
                 f"мок объявлен как {mock_sc.get('kind')!r}, а не контейнер: механизм пробы изоляции "
                 "обязан быть исполнимым и внутри контейнера стадии")
        if mock_sc.get("in_run_network") is not True or not mock_sc.get("ip"):
            fail("mock-not-in-run-network",
                 "мок не в сети прогона (in_run_network/ip): достижимость мока тогда зависит от топологии "
                 "раннера, и проба изоляции из контейнера стадии неисполнима")
        elif mock_sc.get("network") not in set((net.get("networks") or {}).values()):
            fail("mock-not-in-run-network",
                 f"сеть мока {mock_sc.get('network')!r} не совпадает с сетью песочницы "
                 f"({sorted(set((net.get('networks') or {}).values()))})")

    canary_sc = sidecars.get("canary")
    if not has_mock:
        detail["canary"] = {"skipped": "в наборе нет задач с сетью — канарейка не поднимается"}
    elif not isinstance(canary_sc, dict):
        fail("canary-not-named",
             "доказательство не называет канарейку (network.sidecars.canary): без неё «недостижима» "
             "не отличима от мёртвого слушателя")
    else:
        detail["canary"] = {k: canary_sc.get(k) for k in ("kind", "name", "ip", "url", "self_check")}
        if canary_sc.get("self_check") is not True:
            fail("canary-dead",
                 "канарейка не подтвердила собственной живости (self_check): недостижимость мёртвого "
                 "слушателя ничего не доказывает (ADR-023 п.12)")

    # 5. За прогоном не осталось объектов (снимается по точному префиксу, не шаблоном).
    clean = doc.get("cleanup")
    if clean is None:
        fail("cleanup-not-named", "доказательство не называет, что осталось после прогона (cleanup)")
    else:
        detail["cleanup"] = clean
        left = list(clean.get("containers") or []) + list(clean.get("networks") or [])
        if left:
            fail("leftover-objects", f"за прогоном остались объекты: {', '.join(left)}")
    return findings, detail


def live_probe(runner, args, set_dir: pathlib.Path, work_root: pathlib.Path) -> tuple[list[dict], dict]:
    """Живая проба: выход наружу обязан провалиться, мок — быть достижимым."""
    findings: list[dict] = []
    detail: dict = {}
    network, own_network = args.probe_network, False
    if not network:
        network = f"{RUN_TAG}-envcheck-{work_root.name}"
        ok, gw = runner.create_internal_network(network)
        if not ok:
            return [], {"pending": f"сеть не создана: {gw}"}
        own_network = True
    else:
        gw = runner.network_gateway(network)

    mock = None
    canary = None
    sandbox = None
    sb_name = f"{RUN_TAG}-envcheck-{work_root.name}"
    try:
        mock = runner.MockSidecar(set_dir, work_root, runner.MOCK_SEED_DEFAULT, network,
                                  args.sidecar_image, f"{sb_name}-mock")
        mock.start()
        canary = runner.Canary(args.sidecar_image, f"{sb_name}-canary")
        canary.start()
        sandbox = runner.Sandbox(args.image, work_root / "work", network,
                                 sb_name, mock_url=mock.url)
        sandbox.start()
        probes = sandbox.probe(canary_url=canary.url)
        detail["network"] = network
        detail["gateway"] = probes.get("gateway")
        detail["canary"] = canary.record()
        detail["sidecar"] = mock.record()
        detail["egress_attempts"] = probes.get("egress_attempts")
        detail["mock_reachable"] = probes.get("mock_reachable")
        for key, probe in (probes.get("egress_attempts") or {}).items():
            if probe.get("refused") is False:
                findings.append({
                    "severity": "error", "rule": "egress-open",
                    "message": f"выход наружу ПРОШЁЛ ({key}: {probe.get('target')!r}, rc={probe.get('rc')}) — "
                               "награду можно получить внешним вызовом, а не состоянием (ось ADR-049)",
                })
            elif probe.get("refused") is None:
                detail.setdefault("not_verified", []).append(
                    f"{key}: попытка не выполнена ({probe.get('note') or 'нет данных'}) — молчание не доказательство")
        if network != "none" and probes.get("mock_reachable") is not True:
            findings.append({
                "severity": "error", "rule": "mock-unreachable",
                "message": f"мок {mock.url} (sidecar в сети {network}, ip {mock.ip}) не достижим из контейнера — "
                           "среда не пропускает действия (ADR-049 п.6)",
            })
        # Живость канарейки: мёртвый слушатель давал бы «недостижима» даром, и
        # зелёный страж не отличался бы от вакуумного.
        if canary.self_check is not True:
            findings.append({
                "severity": "error", "rule": "canary-dead",
                "message": f"канарейка {canary.url} не отвечает сама себе — проба «выход закрыт» вакуумна "
                           "(недостижимость мёртвого слушателя ничего не доказывает)",
            })
        detail["other_host_services_reachable"] = probes.get("other_host_services_reachable")
    finally:
        if sandbox is not None:
            sandbox.stop()
        if canary is not None:
            canary.stop()
        if mock is not None:
            mock.stop()
        if own_network:
            runner.remove_network(network)
    return findings, detail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Страж контракта среды заземлённой задачи (ADR-049 п.1, G4)")
    ap.add_argument("--root", default=".", help="корень кейса")
    ap.add_argument("--set-dir", default="", help="каталог набора (по умолчанию <root>/data/grounded-skeleton)")
    ap.add_argument("--evidence", default="", help="файл доказательства (по умолчанию <root>/evidence/grounded-skeleton.json)")
    ap.add_argument("--image", default="alpine:3.20")
    # Образ sidecar'ов берётся у раннера, а не дублируется здесь: страж обязан
    # проверять ТУ ЖЕ среду, и второй список значений разошёлся бы с первым.
    ap.add_argument("--sidecar-image", default="",
                    help="образ sidecar'ов (мок и канарейка); по умолчанию — как у раннера")
    ap.add_argument("--work-root", default="", help="каталог пробы (обязан лежать в $HOME: docker не видит /tmp)")
    ap.add_argument("--probe-network", default="",
                    help="ТЕСТ-КРЮЧОК: проверять готовую сеть вместо создания внутренней "
                         "(зубы стража: на дефолтной bridge выход обязан пройти → красное)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    set_dir = pathlib.Path(args.set_dir).resolve() if args.set_dir else root / "data" / "grounded-skeleton"
    evidence = pathlib.Path(args.evidence).resolve() if args.evidence else root / "evidence" / "grounded-skeleton.json"
    runner = load_runner(root / "tools" / "grounded_runner.py")
    args.sidecar_image = args.sidecar_image or runner.DEFAULT_SIDECAR_IMAGE

    out: dict = {"mode": "env-contract", "artifacts": {
        "skeleton_set": set_dir.is_dir(), "skeleton_evidence": evidence.is_file(),
        "probe_network": args.probe_network or "(internal, создаётся стражем)",
        "sandbox_image": args.image, "sidecar_image": args.sidecar_image,
        "execution": runner.execution_context(),
    }}
    findings: list[dict] = []
    detail: dict = {}

    if not set_dir.is_dir():
        out.update({"rc": 2, "findings": [],
                    "message": "NOT-VERIFIED: набора задач нет — проверять контракт среды не на чем"})
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else out["message"])
        return 2
    ok, info = runner.docker_ok()
    if not ok:
        out.update({"rc": 2, "findings": [],
                    "message": f"NOT-VERIFIED: {info} — выход наружу проверяется попыткой, без docker её нет"})
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else out["message"])
        return 2
    ok, digest = runner.ensure_image(args.image)
    if not ok:
        out.update({"rc": 2, "findings": [], "message": f"NOT-VERIFIED: {digest}"})
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else out["message"])
        return 2
    detail["docker"] = info
    detail["image_digest"] = digest

    work_root = pathlib.Path(args.work_root).expanduser() if args.work_root else \
        pathlib.Path.home() / ".cache" / "arch-ml" / "env-contract-check" / f"{RUN_TAG}-envcheck-{pathlib.Path.cwd().name}"
    shutil.rmtree(work_root, ignore_errors=True)
    work_root.mkdir(parents=True, exist_ok=True)

    f1, d1 = check_task_schema(set_dir)
    f2, d2 = live_probe(runner, args, set_dir, work_root)
    f3, d3 = check_evidence(evidence, set_dir)
    findings = f1 + f2 + f3
    detail.update({"task_schema": d1, "live_probe": d2, "evidence": d3})

    left = runner.leftovers(f"{RUN_TAG}-envcheck-{work_root.name}")
    detail["leftovers"] = left
    if left["containers"] or left["networks"]:
        findings.append({"severity": "error", "rule": "leftover-objects",
                         "message": f"страж оставил за собой объекты: {left}"})
    shutil.rmtree(work_root, ignore_errors=True)

    rc = 1 if findings else 0
    pending = [v for v in (d2.get("not_verified") or [])] + ([d3.get("pending")] if d3.get("pending") else [])
    out.update({"rc": rc, "findings": findings, "detail": detail, "pending": pending})
    out["message"] = (
        "контракт среды держится: выход наружу не проходит ни одной попыткой, мок достижим, "
        "задачи несут max_steps"
        if rc == 0 else f"КРАСНОЕ: {len(findings)} нарушений контракта среды (ADR-049 п.1, G4)"
    )
    if rc == 0 and pending:
        out["message"] += "; не проверено здесь: " + "; ".join(pending)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(out["message"])
        for f in findings:
            print(f"  - [{f['severity']}] {f['rule']}: {f['message']}")
        for p in pending:
            print(f"  ~ не проверено: {p}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
