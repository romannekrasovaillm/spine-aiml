#!/usr/bin/env python3
"""S3ay: полная SFT-стадия на исправленном входе с пониженным темпом — сборка каталога.

**Вопрос дельты.** Деградация длины имела две независимые причины, и обе починены
по отдельности: (1) **идентичность входа** — стадия читала v12 (44 949 сэмплов) при
объявленном v13_fixed (44 105), S3av/ADR-051, страж C-030; (2) **темп** — пик 1e-5
срывал поведение к 500-му шагу, а постоянный LR 2e-6 снимал усечения с
31/104 = 29.8 % до 10/104 = 9.6 % (S3aw). Комбинация «v13_fixed + пониженный темп»
измерена короткой рукой (`sft-v13lr2e6-*`, 500 шагов). Здесь собирается **полная**
стадия — 66 157 шагов, та же пара исправлений.

**Чем полная стадия отличается от короткой руки, и почему это названо.** Рука шла
ПОСТОЯННЫМ темпом (`--sft-lr-fixed`: `eta_min == peak_lr`, косинус вырождается в
константу). Полная стадия идёт **штатной формой** — косинус, финал 2e-7, warmup
100 шагов, — и понижен **только пик** (множитель 0.2: 1e-5 → 2e-6). Так требует
решение владельца, и это же правило «одна переменная»: форма расписания остаётся
той, которой учились прежние прогоны, поэтому различие с контролем — масштаб.
На шаге 500 два режима почти совпадают численно (косинус от пика 2e-6 к 2e-7 на
T_max=66 157 даёт 1.998e-6 против постоянных 2e-6 — расхождение 0.06 %), поэтому
ранний гейт сравнивает стадию с рукой **на той же точке** осмысленно.

**Почему инструмент, а не флаги сборщика.** Каталог стадии собирает
`tools/launch_sft_stage.py` (S3aa/S3av) и здесь **вызывается как есть**: два
сборщика одного каталога — это дрейф. Инструмент добавляет ровно то, чего в S3av
нет и что обязано быть у 40-часового прогона:

1. **Обвязка сна** (`guard/guard_cuda_run.sh` + `check_gpu_sleep_guard.py`) —
   копиями с записью sha256 из прогона сравнения (`sft-lr2e6-20260921-1453/guard`),
   чтобы у всех состояний серии была ОДНА обвязка, а не три похожих. Сон площадки
   во время прогона уничтожает контекст CUDA (Xid 31, S3 decode-diagnosis); на
   стенде цели сна замаскированы, `systemd-inhibit` не берётся — запрет держит
   именно эта обвязка (ADR-049 п.10).
2. **Декларация раннего гейта** (`early_gate.json`) — критерий остановки пишется
   ДО запуска, а не подбирается после чтения чисел (ADR-022 п.4, конвенция
   S3ap/S3aq). Критерий, объявленный задним числом, ничего не стоит.
3. **Доставка орудий гейта** в каталог прогона: `early_gate_watch.py` (снимает
   пробу, сравнивает, при провале останавливает цепочку) и `stop_stage_run.py`
   (штатная остановка, ADR-016). Прогон, который сторожит инструмент из ЧУЖОЙ
   рабочей копии, сторожится не тем кодом, что записан в его манифесте.

Чего инструмент НЕ делает: не меняет данные, тензоры, карточки (AD-7) и вход
стадии; не трогает `batch`/`seed`/`max_len`/`max_steps`/`loss_mask`; не правит
живой прогон (ADR-016 п.1); не занимает локальную 4080.

Запуск::

    python3 tools/launch_full_stage.py --ts v13-2e6-20260921-1805 --do build
    python3 tools/launch_full_stage.py --ts v13-2e6-20260921-1805 --do verify
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE_ROOT / "tools"))

import launch_sft_stage as L  # noqa: E402 — сборщик каталога стадии (S3aa/S3av)

#: Общий диск стенда: по нему видно каталог прогона и пакеты опор раннего гейта.
SHARED = "/home/user/gb10-shared"

#: Пик штатной стадии и множитель понижения. Числа объявлены здесь, а не выводятся
#: из копии: объявление обязано существовать ДО сборки, иначе «проверили тем, что
#: получилось» перестаёт быть проверкой.
PEAK_LR_BASE = 1e-5
PEAK_LR_SCALE = 0.2                    # → пик 2e-6
PEAK_LR = PEAK_LR_BASE * PEAK_LR_SCALE  # 2e-6

#: Обвязка сна — копиями того же стража, что у прогона сравнения.
GUARD_SRC = Path("/home/user/gb10-shared/sft-lr2e6-20260921-1453/guard")
GUARD_FILES = ("guard_cuda_run.sh", "check_gpu_sleep_guard.py")

#: Прибор, пиннутый хешем: тот же, что в решающих отчётах S3ap/S3aq/S3aw/S3ax.
PROBE_TOOL = "tools/probe_language_split.py"
PROBE_TOOL_SHA = "99dafa8d9551caaa57b770d4cd321da6f0c09b28deb1885500034c9d62eeb276"

#: Орудия раннего гейта — доставляются в каталог прогона. Прибор здесь потому, что
#: стадия, которую сторожит прибор из ЧУЖОГО каталога, сторожится не тем кодом,
#: который записан в её манифесте: проба обязана сниматься копией из каталога
#: прогона, а её хеш — быть в `ship.json` рядом с остальными.
#: Сторож и штатная остановка ложатся в КОРЕНЬ каталога прогона: стартер запускает
#: сторожа ровно по этому пути (`start_full_stage.py`, `Watch`), и перенос его в
#: подкаталог сломал бы запуск, не сказав об этом.
GATE_ROOT_TOOLS = ("tools/early_gate_watch.py", "tools/stop_stage_run.py")
#: Модули прибора: он импортирует их на уровне файла (`probe_control` — промпты и
#: метрики вырождения, `ppl_probe` — из него же). Цепочка пробы руки S3ax положила
#: в каталог ОДИН прибор, и обе пробы упали за две секунды с
#: `ModuleNotFoundError: No module named 'probe_control'` — при том что `CHAIN_DONE`
#: появился и выглядел готовностью опоры. Класс дефекта тот же, что у памяти: тихий
#: отказ предохранителя. Доставляются В ТУ ЖЕ раскладку, что и прибор (`tools/`),
#: потому что `probe_control` ищет `ppl_probe` рядом с собой.
GATE_PROBE_MODULES = ("tools/probe_control.py", "tools/ppl_probe.py")
#: Раскладка доставки прибора и его модулей внутри каталога прогона. Подкаталог, а
#: не корень: `CASE = Path(__file__).resolve().parent.parent` у прибора и его модулей
#: указывает на каталог прогона, и относительные пути в отчёте считаются от него —
#: как в пакете сравнения (`lr-sens-*/tools/`), где прибор работает.
GATE_PROBE_SUBDIR = "tools"
#: Прибор и его модули — ОДНОЙ раскладкой (`tools/`). Прибор ищет модули РЯДОМ с
#: собой (`probe_tool_path`: `cand.parent`), поэтому «прибор в корень, модули в
#: `tools/`» не находится НИ ОДНОЙ из двух раскладок, которые пробует сторож: гейт
#: на 500-м шаге остался бы без замера, назвав отказ `probe_tool_missing` (а сам
#: вердикт — «не проверено», то есть предохранителя нет). Так и было в первой
#: редакции этой правки (21.09.2026): предикат раскладки проверял попадание в список
#: МОДУЛЕЙ, и сам прибор в него не попал. Тест этого не видел, потому что строил
#: раскладку руками, а не сборкой, — теперь видит (`run_tool_tests.sh`, 34д).
GATE_PROBE_FILES = (PROBE_TOOL,) + GATE_PROBE_MODULES
#: Всё, что доставляется сборкой: корень (сторож и остановка) + подкаталог прибора.
GATE_TOOLS = GATE_ROOT_TOOLS + GATE_PROBE_FILES

#: Площадка сравнения. Проба НЕ инвариантна к устройству (измерено: 57/104
#: совпавших ответов на общем чекпойнте 4080 против GB10), поэтому гейт обязан
#: мерить там же, где мерилась рука.
SITE = "gb10-fast"

#: Каталог пакета пробы короткой руки. Первый пакет (`v13-arm-probe-20260921-1905`)
#: опоры НЕ дал: в его `tools/` лёг один прибор без `probe_control`/`ppl_probe`, обе
#: пробы упали за две секунды, а `CHAIN_DONE` при этом появился — файл «готово» без
#: готовности. Пакет сохранён как запись отказа и не переписывается; опора снимается
#: заново исправленной цепочкой, и её отчёты читаются отсюда.
ARM_PROBE_DIR = f"{SHARED}/v13-arm-probe2-20260921-1710"

#: Состояния-опоры раннего гейта, в порядке предпочтения. Первое доступное
#: используется; какое именно — записывается в вердикт. Рука «v13 + LR 2e-6» —
#: прямое сравнение (тот же набор и тот же темп); повтор руки «v12 + LR 2e-6» в
#: той же сессии — опора переносимости; CPT-база на этой площадке — «уровень
#: пола», ниже которого срыв длины читается как срыв, а не как шум.
#:
#: `states_key` — ключ состояния ВНУТРИ отчёта прибора, и он не всегда равен
#: метке опоры: отчёт называет состояние по метке `--ckpt TAG=PATH`, которой его
#: снимали. Сравнивать по метке значило бы не найти состояние у опоры, снятой
#: другим прогоном (`arm_lr2e6_500` против метки `…_prev_session`) — и сторож
#: молча счёл бы опору непригодной.
REFERENCE_CANDIDATES = [
    {"tag": "arm_v13_lr2e6_500", "states_key": "arm_v13_lr2e6_500",
     "path": f"{ARM_PROBE_DIR}/report_arm_v13_lr2e6_500.json",
     "why": "короткая рука: набор v13_fixed + LR 2e-6, шаг 500, та же площадка — "
            "прямое сравнение полной стадии (тот же вход, тот же темп на точке)"},
    {"tag": "arm_v12_lr2e6_500", "states_key": "arm_v12_lr2e6_500",
     "path": f"{ARM_PROBE_DIR}/report_arm_v12_lr2e6_500.json",
     "why": "повтор руки сравнения в той же сессии: отвечает, переносится ли число "
            "через границу сессий на одной площадке"},
    {"tag": "arm_lr2e6_500_prev_session", "states_key": "arm_lr2e6_500",
     "path": "/home/user/gb10-shared/lr-sens-20260921-1500/report_arm_lr2e6_500.json",
     "why": "прежний замер той же руки (v12 + LR 2e-6 @500) на этой площадке, "
            "измеренный 21.09.2026; опора, если повтор в новой сессии не состоялся"},
    {"tag": "cfinal", "states_key": "cfinal",
     "path": "/home/user/gb10-shared/lr-sens-20260921-1500/report_cfinal.json",
     "why": "CPT-финал на этой площадке — уровень «до SFT»: без него нельзя "
            "отличить срыв обучения от дефекта входа"},
]

#: Метрики гейта. `path` — путь в отчёте прибора; `name` — человеческое имя.
METRICS = [
    {"key": "truncated_share", "path": ["aggregate", "truncated_share"],
     "name": "доля усечений (не кончила ход на бюджете 4096)",
     "role": "главный критерий: срыв длины — это ровно он"},
    {"key": "natural_unclosed_think_share",
     "path": ["aggregate", "stop", "natural", "unclosed_think_share"],
     "name": "незакрытый <think> среди естественно завершённых",
     "role": "формат: ответ без размышления или размышление без ответа"},
    {"key": "natural_looped_share", "path": ["aggregate", "stop", "natural", "looped_share"],
     "name": "петли по сегментам среди естественно завершённых",
     "role": "вырождение: повтор 4-грамм в прозе ответа"},
]

#: Допуск (MDD), объявленный владельцем ДО прогона. Разница меньше допуска
#: различием не читается: при n = 104 доля с точностью ±0.03 не отличает 0.10 от
#: 0.12, и «строго не больше» было бы шумом, а не критерием.
MDD = 0.15
#: Порог «предупреждения»: ухудшение, которое ещё не остановка, но уже сигнал.
WARN_DELTA = 0.05


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def gate_delivery(run_dir: Path, rel: str) -> Path:
    """Куда в каталоге прогона ложится орудие гейта: раскладка — одна на сборку и
    на догрузку (`reship`), иначе догруженный прибор лёг бы туда, где его не ищут."""
    name = Path(rel).name
    return (run_dir / GATE_PROBE_SUBDIR / name if rel in GATE_PROBE_FILES
            else run_dir / name)


def build(ts: str, why: str | None = None) -> dict:
    """Собрать каталог полной стадии. Набор — v13 (S3av), темп — пик 2e-6 (S3ax)."""
    if sha256_file(CASE_ROOT / PROBE_TOOL) != PROBE_TOOL_SHA:
        raise SystemExit(
            f"ОТКАЗ: прибор {PROBE_TOOL} не совпал с пиннутым хешем "
            f"{PROBE_TOOL_SHA[:12]}… — гейт сравнивал бы числа разных приборов")
    # 1. Каталог стадии — штатной сборкой S3av: вход выбирается ИМЕНЕМ (`--dataset v13`),
    #    темп уезжает в сборку копии множителем (у пика SFT нет флага в пайплайне).
    d = L.build(ts, "prefix_only", None, "v13", PEAK_LR_SCALE)
    run_dir: Path = d["run_dir"]
    rec: dict = {
        "tool": "tools/launch_full_stage.py",
        "node": "S3ay",
        "date": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir),
        "why": why or ("полная SFT-стадия на исправленном входе (v13_fixed) с пониженным "
                       f"пиком LR {PEAK_LR:.0e} вместо штатного {PEAK_LR_BASE:.0e}"),
        "base_builder": {"tool": "tools/launch_sft_stage.py",
                         "sha256": sha256_file(CASE_ROOT / "tools/launch_sft_stage.py"),
                         "dataset": d["dataset"], "steps": d["steps"]},
        "tempo": {
            "peak_lr": PEAK_LR, "peak_lr_base": PEAK_LR_BASE,
            "peak_lr_scale": PEAK_LR_SCALE,
            "eta_min": 2e-7,
            "schedule": "CosineAnnealingLR(T_max=args.max_steps, eta_min=2e-7)",
            "warmup_steps": 100,
            "form": "штатная форма расписания сохранена; понижен только пик — "
                    "поэтому различие с контролем это масштаб, а не форма",
            "short_arm_note": "короткая рука шла ПОСТОЯННЫМ LR 2e-6 (eta_min == peak_lr); "
                              "на шаге 500 косинус от пика 2e-6 равен 1.998e-6 против 2e-6 "
                              "у руки — расхождение 0.06 %, поэтому сравнение на точке 500 "
                              "осмысленно, хотя формы за точкой расходятся",
            "guard": "tools/check_stage_lr.py",
        },
        "not_touched": ["набор, тензор, карточки (AD-7)",
                        "вход стадии (CPT-финал, sha256 080c3ab6…)",
                        "batch/seed/max_len/max_steps/loss_mask",
                        "warmup и порядок данных",
                        f"прибор {PROBE_TOOL} (пиннут хешем {PROBE_TOOL_SHA[:12]}…)"],
    }

    # 2. Обвязка сна — копиями с записью источника и хеша.
    guard_dir = run_dir / "guard"
    guard_dir.mkdir(exist_ok=True)
    guard_ship = {}
    for name in GUARD_FILES:
        src = GUARD_SRC / name
        if not src.is_file():
            raise SystemExit(f"ОТКАЗ: нет стража сна {src} — обвязка без него не ставится")
        body = src.read_bytes()
        (guard_dir / name).write_bytes(body)
        guard_ship[name] = {"source": str(src),
                            "sha256": hashlib.sha256(body).hexdigest()}
    (guard_dir / "guard_cuda_run.sh").chmod(0o755)
    rec["guard"] = guard_ship

    # 3. Орудия раннего гейта — в каталог прогона (стадия сторожится своим кодом).
    #    Раскладка: сторож и штатная остановка — в корень, прибор и его модули — в
    #    `tools/` (там же, где они лежат в пакете сравнения и где `probe_control`
    #    находит `ppl_probe`). Доставка проверяется на месте: файл, о котором не
    #    сказано, что он доехал, — это файл, которого может не быть.
    gate_ship = {}
    probes = GATE_TOOLS
    for rel in probes:
        src = CASE_ROOT / rel
        if not src.is_file():
            raise SystemExit(f"ОТКАЗ: нет орудия гейта {src}")
        dst = gate_delivery(run_dir, rel)
        dst.parent.mkdir(exist_ok=True)
        body = src.read_bytes()
        dst.write_bytes(body)
        if dst.read_bytes() != body:
            raise SystemExit(f"ОТКАЗ: доставка не сошлась байт-в-байт: {dst}")
        if src.suffix == ".py":
            dst.chmod(0o755)
        gate_ship[src.name] = {"source": str(src), "delivered": str(dst),
                               "sha256": hashlib.sha256(body).hexdigest()}
    rec["gate_tools"] = gate_ship
    rec["gate_probe_layout"] = {
        "subdir": GATE_PROBE_SUBDIR,
        "why": "прибор импортирует probe_control и ppl_probe на уровне файла; врозь "
               "они дают ModuleNotFoundError за секунды, а не замер",
        "files": [Path(r).name for r in GATE_PROBE_FILES],
        "check": "раскладка сверяется сторожем: probe_tool_path() обязан найти прибор "
                 "С модулями — это и есть предполётная проверка (verify)",
    }
    if gate_ship[Path(PROBE_TOOL).name]["sha256"] != PROBE_TOOL_SHA:
        raise SystemExit("ОТКАЗ: доставленный прибор не совпал с пиннутым хешем")

    # 4. Команда запуска: обвязка стражем сна вокруг штатной команды цепочки.
    #    Дополнительных флагов темпа НЕТ: темп вшит в копию пайплайна множителем,
    #    и второй путь его задания сделал бы «чем задан темп» неоднозначным.
    orig_chain = d["chain_cmd"]
    full_chain = (f"bash {guard_dir}/guard_cuda_run.sh "
                  f"--out {run_dir}/guard.json --why {shlex.quote(rec['why'])} "
                  f"-- {orig_chain}")
    (run_dir / "chain_command.txt").write_text(full_chain + "\n", encoding="utf-8")
    launch_cmd = (f"ssh {L.STAND} 'tmux kill-session -t {d['name']} 2>/dev/null; "
                  f"tmux new-session -d -s {d['name']} \"setsid nohup {full_chain} "
                  f">> {run_dir}/chain.log 2>&1 < /dev/null\"'")
    (run_dir / "launch_command.txt").write_text(launch_cmd + "\n", encoding="utf-8")
    rec["chain_command"] = {"path": str(run_dir / "chain_command.txt"),
                            "builder_command": orig_chain, "wrapped_command": full_chain,
                            "tempo_flags": []}

    # 5. Декларация раннего гейта — ДО запуска.
    refs = [dict(r, exists=Path(r["path"]).is_file()) for r in REFERENCE_CANDIDATES]
    gate = {
        "declared_at": datetime.now(timezone.utc).isoformat(),
        "declared_before_run": True,
        "why": ("ранний гейт отвечает на вопрос «продолжать ли 40 часов» за ~35 минут: "
                "проба точки 500 сравнивается с короткой рукой на той же площадке"),
        "when": {"step": 500, "artifact": "checkpoints/sft_probe_500.pt",
                 "how_detected": "logs/loss_trace.jsonl: последний шаг ≥ 500, и файл точки на месте"},
        "probe": {"tool": PROBE_TOOL, "tool_sha256": PROBE_TOOL_SHA, "site": SITE,
                  "prompts": "wide", "n_probes": 104, "max_new_tokens": 4096,
                  "batch_size": 8, "stop_at_turn_end": True,
                  "decoding": "greedy + штатный запрет повторов 4-грамм (ADR-041)",
                  "compare_rule": "по тому же протоколу снята и рука — числа сравнимы "
                                  "только при совпадении прибора, набора, бюджета и площадки",
                  "concurrency_note": (
                      "проба снимается ПРИ ЖИВОЙ стадии (остановить её на 35 минут значило бы "
                      "потерять больше, чем стоит замер). Рука мерилась на свободном "
                      "устройстве. Это различие условий названо, а не скрыто; грубое "
                      "искажение видно по опоре cfinal, снятой на свободном устройстве")},
        "reference": refs,
        "reference_rule": "берётся первая доступная опора списка; какая взята — в вердикте",
        "metrics": METRICS,
        "mdd": MDD, "warn_delta": WARN_DELTA,
        "rule": (f"ОСТАНОВИТЬ стадию, если хотя бы одна метрика хуже опоры на {MDD} или "
                 f"больше (объявленный допуск MDD при n = 104); ПРЕДУПРЕДИТЬ (не "
                 f"останавливая) при ухудшении от {WARN_DELTA} до {MDD}; иначе — продолжать. "
                 "Провал САМОГО замера (проба не снята) остановкой не является: «не "
                 "проверено» и «хуже значимо» — разные вещи"),
        "on_fail": {"tool": "stop_stage_run.py",
                    "invocation": "python3 <run>/stop_stage_run.py --run-dir <run> "
                                  "--reason 'early-gate: <метрика> хуже руки на <Δ>'",
                    "effect": "маркер var/STOPPED + docker stop -t 120; стадия "
                              "записывается как stopped, а не failed (ADR-016)"},
        "on_probe_failure": ("вердикт not_verified, стадия НЕ останавливается, факт "
                             "докладывается: отсутствие числа не есть плохое число"),
    }
    (run_dir / "early_gate.json").write_text(
        json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rec["early_gate"] = {"path": str(run_dir / "early_gate.json"),
                         "mdd": MDD, "reference_candidates": [r["tag"] for r in refs],
                         "reference_present_now": [r["tag"] for r in refs if r["exists"]]}

    # 6. `ship.json` пересчитывается ПОСЛЕ правок: иначе он называл бы хеши файлов,
    #    которых в каталоге уже нет (цепочка обёрнута стражем, орудия добавлены).
    ship_path = run_dir / "ship.json"
    ship = json.loads(ship_path.read_text(encoding="utf-8"))
    ship["sha256"]["guard/guard_cuda_run.sh"] = guard_ship["guard_cuda_run.sh"]["sha256"]
    ship["sha256"]["guard/check_gpu_sleep_guard.py"] = guard_ship["check_gpu_sleep_guard.py"]["sha256"]
    for name, meta in gate_ship.items():
        ship["sha256"][name] = meta["sha256"]
    ship["full_stage_patch"] = {
        "tool": rec["tool"], "peak_lr": PEAK_LR, "peak_lr_scale": PEAK_LR_SCALE,
        "why": "хеши пересчитаны после обвязки стражем и доставки орудий гейта",
        "early_gate": "early_gate.json",
    }
    ship_path.write_text(json.dumps(ship, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    rec["ship"] = ship["sha256"]

    (run_dir / "full_stage_patch.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return rec


def reship(ts: str) -> dict:
    """Догрузить орудия гейта в УЖЕ собранный каталог и пересчитать `ship.json`.

    Нужно, когда состав орудий выяснился после сборки: стадия исполняет тот код,
    что лежит в её каталоге, и «дописал в tools/» до неё не доедет. Под живой
    цепочкой запрещено (ADR-016 п.1) — проверяется фактом статуса, а не намерением.
    """
    d = L.stage_dirs(ts)
    run_dir: Path = d["run_dir"]
    if not run_dir.is_dir():
        raise SystemExit(f"каталога прогона нет: {run_dir}")
    status_file = run_dir / "var" / "chain.status"
    status = status_file.read_text().strip() if status_file.is_file() else None
    if status == "running":
        raise SystemExit(f"цепочка прогона {d['name']} жива (chain.status=running) — "
                         "под живой цепочкой правки запрещены (ADR-016 п.1)")
    ship_path = run_dir / "ship.json"
    ship = json.loads(ship_path.read_text(encoding="utf-8"))
    changed = {}
    for rel in GATE_TOOLS:
        src = CASE_ROOT / rel
        new = sha256_file(src)
        dst = gate_delivery(run_dir, rel)
        if ship["sha256"].get(src.name) != new or not dst.is_file():
            dst.parent.mkdir(exist_ok=True)
            dst.write_bytes(src.read_bytes())
            if src.suffix == ".py":
                dst.chmod(0o755)
            changed[src.name] = {"before": ship["sha256"].get(src.name), "after": new,
                                 "delivered": str(dst)}
            ship["sha256"][src.name] = new
    ship["reship"] = {"at": datetime.now(timezone.utc).isoformat(), "changed": changed,
                      "chain_status": status}
    ship_path.write_text(json.dumps(ship, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"run_dir": str(run_dir), "chain_status": status, "changed": changed}


def _run_check(args: list[str]) -> dict:
    p = subprocess.run(args, capture_output=True, text=True)
    try:
        return {"rc": p.returncode, **json.loads(p.stdout)}
    except Exception:
        return {"rc": p.returncode, "stdout": p.stdout[-2000:], "stderr": p.stderr[-2000:]}


def verify(ts: str) -> dict:
    """Сверить собранный каталог: темп (страж), вход (C-030) и объявление гейта."""
    d = L.stage_dirs(ts)
    run_dir: Path = d["run_dir"]
    if not run_dir.is_dir():
        raise SystemExit(f"каталога прогона нет: {run_dir}")
    return {
        "run_dir": str(run_dir),
        "stage_lr": _run_check([sys.executable, str(CASE_ROOT / "tools/check_stage_lr.py"),
                                "--run-dir", str(run_dir), "--expect-peak", repr(PEAK_LR),
                                "--json"]),
        "dataset_identity": _run_check([sys.executable,
                                        str(CASE_ROOT / "tools/check_dataset_identity.py"),
                                        "--json"]),
        #: Предполётная проверка САМОГО сторожа его же кодом: прибор с модулями,
        #: объявленные опоры и ЧТЕНИЕ ПАМЯТИ на площадке. Гейт, у которого не
        #: вооружён предохранитель, обязан быть виден до запуска, а не на 500-м шаге.
        "early_gate_preflight": _run_check(
            [sys.executable, str(run_dir / "early_gate_watch.py"),
             "--run-dir", str(run_dir), "--dry-run"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ts", required=True, help="метка прогона; каталог — sft-<ts>")
    ap.add_argument("--do", required=True, choices=["build", "verify", "reship"])
    ap.add_argument("--why", default=None)
    a = ap.parse_args()

    if a.do == "reship":
        print(json.dumps(reship(a.ts), ensure_ascii=False, indent=2))
    elif a.do == "build":
        rec = build(a.ts, a.why)
        print(json.dumps({"run_dir": rec["run_dir"], "tempo": rec["tempo"],
                          "steps": rec["base_builder"]["steps"],
                          "guard": {k: v["sha256"][:12] for k, v in rec["guard"].items()},
                          "gate_tools": {k: v["sha256"][:12] for k, v in rec["gate_tools"].items()},
                          "early_gate": rec["early_gate"]},
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(verify(a.ts), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
