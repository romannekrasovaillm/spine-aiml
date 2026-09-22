#!/usr/bin/env python3
"""Страж «во время GPU-нагрузки кейса запрет сна действует» (спайн AD-9, правило C-029).

**Инцидент, ради которого страж заведён (21.09.2026).** Прогон V3 дельты
``decode-diagnosis`` умер на 24-й пробе из 104 с ``cudaErrorLaunchFailure``.
Трейс указывал на обработчик запрета 4-грамм и выглядел дефектом прибора; ядро
записало другое::

    NVRM: Xid (PCI:0000:01:00): 31, pid=2340175, name=python3 — MMU Fault ...

причём за семь секунд до этого машина вошла и вышла из S3 (05:03:37). Контекст
CUDA сна не переживает: следующее ядро уходит по мёртвой таблице страниц, а
исключение всплывает на ближайшей синхронизации. После этого устройство не
поднялось вовсе (``cuInit`` → 999, устройств 0). То есть отказ был **средой**, а
не прибором и не моделью, — и повторялся бы автоматически при каждом следующем
многочасовом прогоне.

Обвязка, снимающая причину, — ``tools/guard_cuda_run.sh``: ``systemd-inhibit
--what=sleep --mode=block`` вокруг полезной нагрузки плюс расписка о устройстве,
счётчике Xid и числе дошедших проб. Чего обвязка не делала сама, — не была
**обязательной**: защита держалась на памяти человека, а запуск в обход не
оставлял следа. Этот страж превращает «не рекомендовано» в находку.

Проверяются два источника, оба механические:

  (а) **Живые нагрузки.** По таблице процессов (локально — ``/proc``, на стенде —
      ``ps`` через ssh) находятся процессы объявленного набора GPU-нагрузок
      (``GPU_LOADS``) и для каждого ищется действующий запрет сна: предок
      ``systemd-inhibit --what=…sleep… --mode=block`` в цепочке PPid либо метка
      ``CUDA_SLEEP_GUARD=1`` в окружении (её ставит обвязка полезной нагрузке).
      Нагрузка без запрета — находка с pid и командной строкой: «раннер запущен
      без запрета сна» видно не по логу, а по факту. Доказательство площадки
      (``masked_targets``) этой находки **не снимает**: оно отвечает про площадку, а
      здесь спрашивается про нагрузку — пришла ли она санкционированным путём (метка
      обвязки несёт с собой ещё и расписку с проверкой устройства). Разные вопросы —
      разные ответы, и подменять один другим значило бы ослабить проверку; поэтому у
      такой находки причина называет ещё и состояние площадки.

  (б) **Расписки и записанные команды начатых прогонов.** У каждого каталога
      ``runs/<id>/``, **начатого после вступления правила в силу**, обязано быть
      доказательство защиты, и страж называет, какое именно: расписка обвязки
      ``guard*.json`` с ``sleep_inhibit.held = true`` **и названным способом**
      (``protection: inhibit | masked_targets`` — расписка без имени способа
      находка, а не доказательство) либо защита в **записанной
      команде прогона** (``chain_command.txt``/``launch_command.txt``) — так
      стартует отсоединённая стадия SFT (``tmux`` + ``setsid nohup``): первым в
      команде идёт ``tools/chain_sleep_guard.sh``, потому что вокруг запускающего её
      ``ssh`` защита не жила бы ни секунды. Прогоны, начатые раньше, находкой не
      являются: они стартовали до
      появления механизма и называются историческими (ADR-028 п.4 — история не
      переписывается), но перечисляются в отчёте поимённо как ``legacy-not-checked``
      со словами «старше правила, защита сна НЕ доказана», чтобы «историческое» не
      читалось как «проверенное». Порог применимости — **момент** вступления правила
      в силу (``ENACTED_AT``), а не начало суток: разбор — ADR-053 п.1–2. Отказ
      обвязки до старта (``payload = null``) — тоже
      не находка: GPU-работы не было, и это названо распиской, а не умолчанием.

  (в) **Механизм на каждой площадке — измеряется, а не предполагается.** Страж
      сам проверяет, берётся ли ``systemd-inhibit --what=sleep --mode=block``
      (локально — вызовом, на стенде — тем же вызовом через ssh). Это не
      формальность: на ``gb10-fast`` 21.09.2026 механизм **недоступен** —
      ``Failed to inhibit: Access denied`` (у пользователя нет logind-сессии).
      Площадка, где защитить нагрузку нечем, называется **блокером**, а не
      «зелено»: иначе «нагрузок не найдено» читалось бы как «площадка в порядке».
      При этом нагрузка, найденная там без запрета, — по-прежнему находка.

**Два способа доказать защиту, и оба — фактом (дельта ``sleep-guard-masked-stand``,
21.09.2026).** Защита площадки считается доказанной, если выполнено **любое** из:

  ``inhibit``         — ``systemd-inhibit --what=sleep --mode=block`` берётся:
                        механизм отвечает успехом на пустой нагрузке. Это ответ на
                        вопрос «сон можно попросить не наступать»;
  ``masked_targets``  — сон **структурно недостижим**: все четыре цели
                        (``sleep``/``suspend``/``hibernate``/``hybrid-sleep``)
                        отдают ``is-enabled = masked``, и **попытка старта
                        ``suspend.target`` проваливается**. Это ответ на другой
                        вопрос — «сон можно наступить»: не «попросили не спать», а
                        «спать нечем». Так устроен стенд ``gb10-fast``, где
                        инхибит взять неоткуда (нет logind-сессии), а маскировка
                        переживает перезагрузку.

Второй способ **доказывается попыткой, а не чтением флага**: ``is-enabled =
masked`` — это то, что мы просим systemd сделать, а провал ``systemctl start
suspend.target`` с причиной о маскировке — то, что он делает. Флаг без провала
попытки доказательством не считается (тот же принцип, что у стража egress:
пытаемся выйти и требуем отказа). Попытка при этом не может усыпить площадку **по
построению**: она делается только после того, как все четыре цели прочитаны
``masked`` — то есть старт заведомо будет отвергнут. Обратное тоже верно и
проверяется мутантом: маскировка прочитана, а попытка **не** провалилась или
провалилась не из-за маскировки — доказательства нет, площадка называется
блокером.

**Площадка без обоих способов — блокер, а не предупреждение** (``verdict:
blocked``, находка с кодом 1, когда площадка **измерена**): читаемая таблица
процессов при недостижимом ни одним способом запрете сна означает, что нагрузка
здесь может быть уничтожена сном — и обвязка старт на такой площадке не пропустит
(``refused_no_inhibit``, код 2). Недоступный стенд (не прочитан вовсе) остаётся
NOT-VERIFIED: «не измерено» и «измерено и не защищено» — разные утверждения, и
сливать их значило бы красить гейт чужой занятостью.

**Граница стража (названа, а не подразумевается).** Прибор ``probe_language_split.py``
пиннут хешем (``99dafa8d…``, ADR-041): правка его тела сломала бы сопоставимость
чисел стадии, поэтому отказать в старте изнутри он не может. Механическая защита
прибора — обвязка (единственный разрешённый путь запуска) и **этот страж**:
обход обнаруживается на живой нагрузке и по отсутствию доказательства. Раннер
``launch_sft_stage.py`` дополнительно отказывается стартовать без запрета сна
(``--assert-held`` ниже) — то есть для него обход невозможен, а не только виден.
Предел доказательства «записанной командой» назван честно: это документ (AD-2), и
исполнение по нему подтверждает живая проверка, пока прогон идёт, — а не сам факт
записи.

**Остаточный шум назван числом, а не умолчанием.** Проверка (а) опознаёт нагрузку по
имени инструмента и режиму, а не по физическому признаку работы на устройстве: fd
устройства хозяину не видны для чужих процессов **внутри контейнера** (на ``gb10-fast``
``ls -l /proc/<pid>/fd | grep nvidia`` возвращает 0 даже у ``llama-server`` с
``--n-gpu-layers 999``: процесс контейнера принадлежит root), поэтому физический
признак на стенде не работает, а имя и режим работают на обеих площадках. Отсюда
следствие, которое надо знать: **вызов инструмента объявленного набора на подставном
входе** (так делает ``tools/tests/run_tool_tests.sh`` — например
``run_det_probe.py --steps 100`` в проверке отказа) тоже читается находкой. Это
выбрано сознательно: находка видна поимённо (pid и командная строка — видно, что это
тест), а пропущенная настоящая нагрузка невидима. Режимы ``NON_LOAD_MODES`` снимают
основную часть шума, оставшуюся часть придётся назвать в отчёте глазами.

**Стенд.** ``gb10-fast`` — отдельная площадка, и запрет сна нужен на ней, а не на
машине наблюдателя: нагрузка идёт там. Поэтому ``--host`` (можно несколько)
читает таблицу процессов стенда **только на чтение** (``ps`` и проверка механизма
через ssh, без записи и без запуска нагрузки) и ищет в ней те же нагрузки без
запрета. Недоступный стенд — NOT-VERIFIED, а не «зелено»: по умолчанию он не красит
гейт (нужен явный ``--strict``), но и не выдаётся за проверенный.

Коды возврата::

    0 — нарушений нет (в т.ч. недоступная площадка стенда — NOT-VERIFIED без --strict)
    1 — нарушение: поимённый список в stdout (включая площадку, которая **измерена**
        и не защищена ни одним из двух способов — ``verdict: blocked``)
    2 — NOT-VERIFIED: не прочитан вход (своя таблица процессов / каталог прогонов) —
        проверять было нечего; либо площадка, у которой защита **не измерена** (ssh не
        ответил, механизм не спрошен), при --strict

Различие между двумя NOT-VERIFIED существенное. **Своя** таблица процессов — не
«площадка», а вход: не увидев её, страж не проверил ничего, и зелёный вердикт был бы
утверждением без основания (те же коды у ``check_instrument_versions.py``). Площадка
стенда — наоборот: она может быть занята или недоступна, и объявлять это красным по
умолчанию значило бы красить гейт чужой занятостью; поэтому отказ площадки по
умолчанию возвращает 0 с явной строкой NOT-VERIFIED, а ``--strict`` делает его
красным (``--unreachable-not-verified`` отменяет и это — профиль гейта кейса).

Запуск::

    python3 tools/check_gpu_sleep_guard.py                       # локальная площадка
    python3 tools/check_gpu_sleep_guard.py --host gb10-fast --json
    python3 tools/check_gpu_sleep_guard.py --assert-held         # «защита сна взята?»
    python3 tools/check_gpu_sleep_guard.py --prove-protection    # чем защищена площадка (JSON)
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Набор GPU-нагрузок кейса — объявлен, а не выводится из имени каталога. Приборы
#: (считают на устройстве) и раннеры стадий (ставят нагрузку на устройство —
#: локально или на стенде). Список — часть контракта правила: он печатается в
#: отчёте, поэтому «страж не нашёл нагрузку» отличимо от «страж искал не то».
GPU_LOADS: tuple[str, ...] = (
    # приборы
    "probe_language_split.py",
    "passrate_probe.py",
    "ppl_probe.py",
    "ppl_probe_k2.py",
    "ppl_probe_v3.py",
    "calib_ppl_probe.py",
    "flex_ppl_probe.py",
    "full_cpt_probe.py",
    "probe_control.py",
    "run_det_probe.py",
    "run_flex_check.py",
    "smoke_probe.py",
    "run_smoke.py",
    "run_full_cpt.py",
    "run_pilot.py",
    "run_rl_probe.py",
    "run_mix_lr_calib.py",
    "run_mask_smoke.py",
    "rl_probe_hook.py",
    # раннеры стадий
    "launch_sft_stage.py",
    "pilot_chain.sh",
)

#: Режимы, которые нагрузки на устройство не создают: планирование, разбор готового,
#: самопроверка, остановка, проверка предусловий. Объявлены, потому что «запущен файл
#: прибора» и «идёт работа на устройстве» — не одно и то же: тесты кейса зовут приборы
#: и раннеры на подставных входах (`--selftest`, `--plan`, `--analyze-only`,
#: `--dry-run`), и читать это нагрузкой значило бы объявить находкой работу тестов.
#: Список — часть контракта правила и печатается в отчёте: «страж не нашёл нагрузку»
#: отличимо от «страж искал не то».
#:
#: Чего здесь намеренно **нет**: `--once` (`full_cpt_probe.py` — одна попытка
#: полного CPT, то есть работа на устройстве), `--watch`/`--trend`-подобные режимы,
#: если они считают на устройстве. Пропустить настоящую нагрузку дороже, чем назвать
#: находкой вызов инструмента, который до устройства не дошёл: у находки видно pid и
#: командную строку, у пропуска — ничего.
NON_LOAD_MODES: tuple[str, ...] = (
    "--plan", "--analyze-only", "--dry-run", "--summarize", "--trend",
    "--stop", "--stop-only",
    "--selftest", "--check-only", "--write-manifest-only",
    "--reaudit-report", "--recut-report",
    "--help", "-h", "--version",
    #: `--do`-режимы раннера стадии: сборка каталога, доставка файлов, проверка
    #: предусловий и опрос фактов нагрузки не ставят (см. tools/launch_sft_stage.py).
    "--do build", "--do reship", "--do check", "--do facts",
)


def is_non_load_mode(args: list[str]) -> str | None:
    """Режим, объявленный «без нагрузки» (для отчёта), или None."""
    cmd = " ".join(args)
    for mode in NON_LOAD_MODES:
        if mode in cmd or mode.replace(" ", "=") in cmd:
            return mode
    return None


#: Метка, которую обвязка ставит полезной нагрузке. Её наличие — свидетельство
#: того, что нагрузка порождена обвязкой, а не поднята вручную.
GUARD_ENV_MARKER = "CUDA_SLEEP_GUARD"

#: Цели, через которые машина уходит в сон. Маскировка **всех четырёх** делает сон
#: структурно недостижимым — не «попросили не спать», а «спать нечем». Это второй
#: способ доказать защиту площадки (``masked_targets``) там, где инхибит взять
#: неоткуда: на ``gb10-fast`` нет logind-сессии, зато маскировка переживает перезагрузку.
SLEEP_TARGETS: tuple[str, ...] = ("sleep.target", "suspend.target",
                                  "hibernate.target", "hybrid-sleep.target")

#: Проба маскировки — **попыткой, а не чтением флага**. Одна и та же строка идёт и
#: на локальную площадку (``sh -c``), и на стенд (внутри единственного ssh-снимка),
#: поэтому обе площадки измеряются одним способом. Кавычек в ней нет намеренно: она
#: едет через ssh одной строкой.
#:
#: Попытка старта делается **только** после того, как все четыре цели прочитаны
#: ``masked``: тогда старт заведомо отвергнут, и усыпить площадку проба не может по
#: построению. Если маскировка неполна, проба честно говорит, что не пробовала.
#:
#: ``LC_ALL=C`` — не украшение: и ``is-enabled``, и причина отказа приходят на языке
#: локали площадки, а доказательство читается по слову ``masked``. Локализованный
#: ответ («замаскирован», «maskiert») страж доказательством не признал бы — и
#: площадка, которая **защищена**, была бы названа незащищённой. Ошибка была бы в
#: сторону отказа, то есть тихой: прогон просто не стартовал бы.
MASK_PROBE = r'''
m_all=yes; m_list=""
for t in sleep.target suspend.target hibernate.target hybrid-sleep.target; do
  s=$(LC_ALL=C systemctl is-enabled "$t" 2>&1 | head -1)
  [ "$s" = masked ] || m_all=no
  m_list="$m_list $t=$s"
done
echo "MASK-TARGETS:$m_list"
if [ "$m_all" = yes ]; then
  if sudo -n true 2>/dev/null; then SUDO="sudo -n"; else SUDO=""; fi
  a=$(LC_ALL=C $SUDO systemctl --no-ask-password start suspend.target 2>&1); rc=$?
  echo "MASK-ATTEMPT-RC:$rc"
  echo "MASK-ATTEMPT-OUT:$(printf "%s" "$a" | head -2 | tr "\n" " ")"
else
  echo "MASK-ATTEMPT-RC:skipped"
  echo "MASK-ATTEMPT-OUT:маскировка неполна - попытка не делалась (усыпила бы площадку)"
fi
'''

#: Признак того, что отказ в попытке — **о маскировке**, а не о правах. Без этого
#: «попытка не удалась» читалось бы доказательством на машине, где старт отвергнут
#: одним лишь polkit'ом (`Interactive authentication required`), — а такая машина
#: уходит в сон и сама, по idle-таймауту: запрет там не структурный.
MASK_REFUSAL_MARK = "masked"

#: **Момент** вступления правила в силу (дельта ``sleep-guard-in-launch``), а не
#: начало суток (ADR-053 п.1). Прогоны, начатые раньше, — исторические: механизма
#: не существовало, и находка на них была бы обвинением задним числом (ADR-028 п.4).
#: Граница названа здесь, а не выведена из mtime: mtime каталога прогона меняется
#: при каждой правке отчёта — то есть «свежесть» файлов не датирует старт прогона.
#:
#: Почему дробно и с таймзоной. Правило ``C-029`` вошло в ветвь коммитом ``ed87e8b``
#: 21.09.2026 в **14:22:35 MSK**, а прогон ``sft-loop-trend-20260921`` начат в
#: 14:10:13 MSK того же дня — за 12 минут **до** рождения правила. Порог «начало
#: суток» делал такой прогон находкой, которую невозможно закрыть иначе как
#: дописав расписку задним числом: краснота by construction, запрещённая ADR-023
#: п.12. Суточная гранулярность и была источником дефекта (разбор — ADR-053);
#: поэтому порог — момент с точностью до секунды и с явной таймзоной.
#: Сдвиг этой константы — чувствительная операция: только решением с ADR и с
#: записью редакции прибора в ``docs/specs/INSTRUMENT-VERSIONS.md`` (ADR-023 п.10).
ENACTED_AT = "2026-09-21T11:22:35+00:00"

#: Артефакты, по которым датируется старт прогона, если манифеста нет.
LAUNCH_ARTIFACTS = ("chain.sh", "chain.log", "pilot_chain.sh", "launch_command.txt",
                    "resume_probe.sh")


class Proc:
    """Процесс в таблице: pid, предок, командная строка, метка обвязки."""

    __slots__ = ("pid", "ppid", "args", "marker")

    def __init__(self, pid: int, ppid: int, args: list[str], marker: bool = False):
        self.pid = pid
        self.ppid = ppid
        self.args = args
        self.marker = marker

    @property
    def cmdline(self) -> str:
        return " ".join(self.args)

    @property
    def basename(self) -> str:
        if not self.args:
            return ""
        return Path(self.args[0]).name


def _read_cmdline(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _read_ppid(status: Path) -> int | None:
    try:
        for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _has_marker(environ: Path) -> bool:
    try:
        raw = environ.read_bytes()
    except OSError:
        return False
    for part in raw.split(b"\0"):
        if part.startswith(GUARD_ENV_MARKER.encode() + b"="):
            value = part.split(b"=", 1)[1].decode("utf-8", "replace")
            return value.strip() not in ("", "0", "no", "false")
    return False


def local_processes(proc_root: Path) -> tuple[list[Proc], str | None]:
    """Таблица процессов локальной площадки. Второй элемент — причина отказа."""
    if not proc_root.is_dir():
        return [], f"таблица процессов не читается: нет каталога {proc_root}"
    procs: list[Proc] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        args = _read_cmdline(entry / "cmdline")
        ppid = _read_ppid(entry / "status")
        if ppid is None:
            continue
        procs.append(Proc(pid, ppid, args, _has_marker(entry / "environ")))
    if not procs:
        return [], f"таблица процессов пуста или недоступна: {proc_root}"
    return procs, None


def parse_ps(text: str) -> list[Proc]:
    """Таблица процессов из вывода ``ps -eo pid=,ppid=,args=`` (площадка стенда)."""
    procs: list[Proc] = []
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        procs.append(Proc(pid, ppid, parts[2].split()))
    return procs


#: Разметка одного ssh-вызова: сначала таблица процессов, потом проверка механизма.
#: Один вызов, а не два, — чтобы площадка читалась одним снимком: два вызова
#: оставили бы между собой окно, в котором «нагрузок нет» и «механизм отказал»
#: относятся к разным моментам.
REMOTE_SENTINEL = "---SLEEP-GUARD-MECHANISM---"


def remote_probe(host: str, timeout: int,
                 mechanism: bool = True) -> tuple[list[Proc], dict | None, dict | None, str | None]:
    """Снимок площадки стенда: таблица процессов, механизм запрета и проба маскировки.

    Проверка механизма — не догадка о площадке, а её измеренное состояние:
    ``systemd-inhibit`` впереди полезной нагрузки отказывает **закрыто** (на
    ``gb10-fast`` 21.09.2026: ``Failed to inhibit: Access denied`` — у пользователя
    нет logind-сессии, — и нагрузка не запускается вовсе). Поэтому «механизма на
    площадке нет» — не зелёный вердикт, а названный блокер.

    Тем же снимком идёт **проба маскировки**: у площадки, где инхибит взять
    неоткуда, защита может держаться на структурной недостижимости сна, и её надо
    измерить здесь же — иначе «механизма нет» читалось бы как «защиты нет».
    """
    probe = (f"{{ systemd-inhibit --what=sleep --mode=block --why=check_gpu_sleep_guard true "
             f"&& echo MECHANISM-OK || echo MECHANISM-FAIL; }} 2>&1" if mechanism else "true")
    mask_probe = MASK_PROBE if mechanism else ""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", host,
           f"ps -eo pid=,ppid=,args=; echo {REMOTE_SENTINEL}; {probe}; {mask_probe}"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], None, None, f"стенд {host} недоступен: {exc}"
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        return [], None, None, f"стенд {host} недоступен (ssh rc={done.returncode}): " \
                               f"{detail[-1] if detail else 'без объяснения'}"
    # Разделитель берётся **с конца**: та же строка есть в аргументах самого
    # ssh-шелла, и `ps` печатает его первым — деление по первому вхождению
    # отдало бы таблицу процессов в «ответ механизма».
    head, _, tail = done.stdout.rpartition(REMOTE_SENTINEL)
    procs = parse_ps(head)
    if not procs:
        return [], None, None, f"стенд {host}: таблица процессов пуста"
    if not mechanism:
        return procs, None, None, None
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    ok = "MECHANISM-OK" in lines
    mask_lines = [ln for ln in lines if ln.startswith("MASK-")]
    detail = " ".join(ln for ln in lines
                      if ln not in ("MECHANISM-OK", "MECHANISM-FAIL")
                      and not ln.startswith("MASK-"))
    mechanism_state = {"available": ok,
                       "why": ("запрет сна берётся" if ok else
                               detail.strip() or "systemd-inhibit отказал без объяснения")}
    return procs, mechanism_state, parse_mask_probe(mask_lines), None


def local_mechanism(timeout: int = 10) -> dict:
    """Состояние механизма запрета на локальной площадке — тем же способом, что на стенде."""
    cmd = ["systemd-inhibit", "--what=sleep", "--mode=block",
           "--why=check_gpu_sleep_guard", "true"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              check=False)
    except FileNotFoundError:
        return {"available": False, "why": "systemd-inhibit не найден"}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "why": f"systemd-inhibit не отвечает: {exc}"}
    if done.returncode == 0:
        return {"available": True, "why": "запрет сна берётся"}
    detail = " ".join((done.stdout + done.stderr).split())
    return {"available": False,
            "why": detail or f"systemd-inhibit вернул {done.returncode}"}


def mask_proof(targets: dict, attempt_rc: str | None, attempt_out: str) -> dict:
    """Доказана ли структурная недостижимость сна — прочитанным **и** попыткой.

    Доказательство держится на двух половинах, и ни одна не достаточна:

      * все четыре цели прочитаны ``masked`` (что мы попросили systemd сделать);
      * попытка старта ``suspend.target`` **провалилась**, и причина отказа — та
        самая маскировка (что systemd делает на самом деле).

    Флаг без попытки не годится: чтение ``is-enabled`` доказывает намерение, а не
    поведение. Попытка без причины тоже: старт, отвергнутый одним лишь polkit'ом
    (``Interactive authentication required``), не отличает замаскированную площадку
    от незамаскированной — такую же картину даст любая машина, где у пользователя
    нет прав на ``systemctl start``. И обратное — не меньшая находка: маскировка
    прочитана, а старт **прошёл** — значит сон достижим.
    """
    measured = bool(targets)
    unmasked = [f"{t}={targets.get(t, 'не прочитано')}" for t in SLEEP_TARGETS
                if targets.get(t) != "masked"]
    attempted = attempt_rc not in (None, "skipped")
    rc_value = int(attempt_rc) if (attempted and (attempt_rc or "").lstrip("-").isdigit()) else None
    rc_failed = rc_value is not None and rc_value != 0
    names_mask = MASK_REFUSAL_MARK in attempt_out.lower()
    proved = measured and not unmasked and rc_failed and names_mask
    if not measured:
        why = ("маскировка не измерена: площадка не назвала состояние целей сна "
               "(измерение невозможно, а не «сон недостижим»)")
    elif unmasked:
        why = ("не замаскированы: " + ", ".join(unmasked) +
               " — сон достижим; флаг без провала попытки доказательством не считается")
    elif not attempted:
        why = "попытка старта suspend.target не делалась"
    elif not rc_failed:
        why = (f"маскировка прочитана, но попытка старта suspend.target НЕ провалилась "
               f"(rc={attempt_rc}) — сон достижим, маскировка не действует")
    elif not names_mask:
        why = (f"попытка провалилась, но отказ не о маскировке ({attempt_out or 'без объяснения'}) — "
               f"он не доказывает, что сон недостижим структурно")
    else:
        why = (f"все четыре цели сна masked, и попытка старта suspend.target "
               f"провалилась: {attempt_out}")
    return {"proved": proved, "measured": measured, "targets": targets,
            "attempt": {"made": attempted, "rc": rc_value, "reason": attempt_out},
            "why": why}


def parse_mask_probe(lines: list[str]) -> dict:
    """Разбор ответа пробы маскировки — из тех же строк, что печатает площадка."""
    targets: dict[str, str] = {}
    attempt_rc: str | None = None
    attempt_out = ""
    for line in lines:
        line = line.strip()
        if line.startswith("MASK-TARGETS:"):
            for item in line.split(":", 1)[1].split():
                name, _, state = item.partition("=")
                if name:
                    targets[name] = state
        elif line.startswith("MASK-ATTEMPT-RC:"):
            attempt_rc = line.split(":", 1)[1].strip()
        elif line.startswith("MASK-ATTEMPT-OUT:"):
            attempt_out = line.split(":", 1)[1].strip()
    return mask_proof(targets, attempt_rc, attempt_out)


def local_mask_probe(timeout: int = 30) -> dict:
    """Проба маскировки на локальной площадке — той же строкой, что уходит на стенд."""
    try:
        done = subprocess.run(["sh", "-c", MASK_PROBE], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return mask_proof({}, None, f"проба маскировки не выполнена: {exc}")
    return parse_mask_probe(done.stdout.splitlines())


def resolve_protection(mechanism: dict, mask: dict | None) -> dict:
    """Чем защищена площадка: ``inhibit``, ``masked_targets``, ничем — или не измерено.

    Три исхода различимы, и путать их нельзя:

      ``method`` есть      — защита доказана; способ назван поимённо;
      ``measured`` истинно — площадка **измерена и не защищена** ни одним способом:
                             нагрузка здесь может быть уничтожена сном, и это
                             находка, а не предупреждение;
      ``measured`` ложно   — защита **не измерена** (механизм не спрошен, ssh не
                             ответил): NOT-VERIFIED, потому что «не измерено» и
                             «измерено и не защищено» — разные утверждения.
    """
    avail = mechanism.get("available")
    if avail:
        return {"method": "inhibit", "measured": True,
                "why": f"запрет сна берётся: {mechanism.get('why')}",
                "inhibit": mechanism, "masked_targets": mask}
    if mask is None:
        return {"method": None, "measured": False,
                "why": f"защита не измерена: механизм запрета не спрошен ({mechanism.get('why')})",
                "inhibit": mechanism, "masked_targets": None}
    if mask.get("proved"):
        return {"method": "masked_targets", "measured": True, "why": mask["why"],
                "inhibit": mechanism, "masked_targets": mask}
    if avail is False and mask.get("measured"):
        return {"method": None, "measured": True,
                "why": (f"ни инхибита ({mechanism.get('why')}), ни маскировки ({mask['why']})"),
                "inhibit": mechanism, "masked_targets": mask}
    return {"method": None, "measured": False,
            "why": (f"защита не измерена: механизм запрета не подтверждён "
                    f"({mechanism.get('why')}), маскировка — {mask['why']}"),
            "inhibit": mechanism, "masked_targets": mask}


def current_protection(proc_root: Path | None = None) -> dict:
    """Чем защищена нагрузка, стартующая из ЭТОГО процесса: свой запрет или площадка.

    Один вопрос — один ответ, и он нужен двум потребителям: ``--assert-held``
    (предполётный вопрос раннера и цепочки) и обвязке, которая пишет расписку.
    Обе половины — про одно и то же: «сон не наступит, пока идёт нагрузка».
    """
    procs, err = local_processes(proc_root or Path("/proc"))
    if err:
        return {"method": None, "held": False, "why": err, "masked_targets": None}
    me = next((p for p in procs if p.pid == os.getpid()), None)
    if me is None:
        return {"method": None, "held": False,
                "why": "свой процесс не найден в таблице процессов", "masked_targets": None}
    held, own_why = inhibition(me, {p.pid: p for p in procs})
    if held:
        return {"method": "inhibit", "held": True, "why": own_why, "masked_targets": None}
    mask = local_mask_probe()
    if mask["proved"]:
        return {"method": "masked_targets", "held": True, "why": mask["why"],
                "masked_targets": mask}
    return {"method": None, "held": False,
            "why": (f"запрет сна не взят ({own_why or 'ни предка systemd-inhibit, ни метки'}), "
                    f"и сон площадки структурно не исключён ({mask['why']})"),
            "masked_targets": mask}


def _inhibit_of(args: list[str]) -> tuple[bool, str]:
    """Взят ли этим процессом запрет сна. Второй элемент — почему (для отчёта)."""
    if not args or Path(args[0]).name != "systemd-inhibit":
        return False, ""
    what = ""
    mode = ""
    for i, arg in enumerate(args):
        if arg.startswith("--what="):
            what = arg.split("=", 1)[1]
        elif arg == "--what" and i + 1 < len(args):
            what = args[i + 1]
        elif arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
        elif arg == "--mode" and i + 1 < len(args):
            mode = args[i + 1]
    #: Умолчание systemd-inhibit — `block`; `delay`/`fail` сон НЕ удерживают, и
    #: читать их как защиту значило бы выдать намерение за механизм. На машине
    #: такие держатели есть всегда (GNOME держит `sleep` в режиме `delay`), поэтому
    #: «в списке держателей что-то про sleep есть» защитой не является — проверка
    #: идёт по цепочке предков конкретной нагрузки, а не по глобальному списку.
    blocks = mode in ("", "block")
    #: Пустой `--what` — умолчание `shutdown:sleep:idle`, то есть сон входит.
    holds_sleep = (not what) or "sleep" in [w.strip() for w in what.split(":")]
    return (blocks and holds_sleep), " ".join(args)


def inhibition(proc: Proc, by_pid: dict[int, Proc], depth: int = 64) -> tuple[bool, str]:
    """Ищет действующий запрет сна: предок-``systemd-inhibit`` либо метка обвязки."""
    seen: set[int] = set()
    cur: Proc | None = proc
    while cur is not None and len(seen) < depth:
        if cur.pid in seen:
            break
        seen.add(cur.pid)
        here, why = _inhibit_of(cur.args)
        if here:
            return True, f"предок pid {cur.pid}: {why}"
        cur = by_pid.get(cur.ppid)
    if proc.marker:
        return True, f"метка {GUARD_ENV_MARKER}=1 в окружении (нагрузку подняла обвязка)"
    return False, ""


#: Интерпретаторы, через которые кейс запускает нагрузки. Нагрузка опознаётся
#: парой «интерпретатор + скрипт», а не «любое упоминание имени в аргументах»:
#: иначе `grep probe_language_split.py` или открытый в редакторе файл читались бы
#: как GPU-нагрузка, и находка обесценилась бы шумом.
INTERPRETERS: tuple[str, ...] = ("bash", "sh", "dash", "zsh", "env")


def load_name(proc: "Proc", loads: tuple[str, ...]) -> str | None:
    """Имя GPU-нагрузки в этом процессе или None (режимы без нагрузки названы отдельно)."""
    if not proc.args:
        return None
    names = [Path(arg).name for arg in proc.args]
    found: str | None = None
    if names[0] in loads:
        found = names[0]
    else:
        is_interpreter = names[0].startswith("python") or names[0] in INTERPRETERS
        if is_interpreter:
            for name in names[1:3]:
                if name in loads:
                    found = name
                    break
    if found is None:
        return None
    if is_non_load_mode(proc.args):
        return None
    return found


def find_loads(procs: list[Proc], loads: tuple[str, ...]) -> list[dict]:
    by_pid = {p.pid: p for p in procs}
    out: list[dict] = []
    for proc in procs:
        name = load_name(proc, loads)
        if name is None:
            continue
        held, why = inhibition(proc, by_pid)
        out.append({"pid": proc.pid, "ppid": proc.ppid, "load": name,
                    "cmdline": proc.cmdline, "sleep_inhibit": held, "why": why})
    return sorted(out, key=lambda r: r["pid"])


def _parse_iso(text: str) -> datetime.datetime | None:
    try:
        stamp = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp


def run_start(run_dir: Path) -> tuple[datetime.datetime | None, str]:
    """Когда прогон начат: манифест (AD-2) — носитель, mtime артефактов — запасной путь."""
    manifest = run_dir / "run_manifest.json"
    if manifest.is_file():
        try:
            stamp = _parse_iso(json.loads(manifest.read_text(encoding="utf-8")).get("created_at", ""))
        except (OSError, ValueError, AttributeError):
            stamp = None
        if stamp is not None:
            return stamp, "run_manifest.json:created_at"
    stamps: list[float] = []
    for name in LAUNCH_ARTIFACTS:
        path = run_dir / name
        if path.exists():
            stamps.append(path.stat().st_mtime)
    if not stamps:
        return None, "не датируется"
    return (datetime.datetime.fromtimestamp(min(stamps), datetime.timezone.utc),
            "mtime артефактов запуска")


#: Признаки защиты сна в записанной команде прогона. Стадия SFT запускается
#: отсоединённо (`tmux` + `setsid nohup`), поэтому защита ставится **в саму
#: записанную команду** (`tools/launch_sft_stage.py`), а не вокруг запускающего её
#: `ssh`: тот возвращается сразу, и снятая с него защита не защитила бы ничего. Для
#: таких прогонов носитель доказательства — `chain_command.txt` (он же в манифесте,
#: AD-2), а живое исполнение подтверждает проверка нагрузок.
#:
#: Признаков два, и оба названы своими именами: шаг `tools/chain_sleep_guard.sh`
#: (нынешняя форма — он решает **на площадке прогона**, каким из двух способов
#: защищаться) и `systemd-inhibit` прямо в команде (форма до 21.09.2026: она годилась
#: только там, где инхибит берётся, и на стенде отказывала закрыто — то есть
#: останавливала исправный прогон). Обе формы читаются доказательством, потому что
#: обе стоят **перед** цепочкой и обе отказывают закрыто: без защиты цепочка не
#: стартует. Предел доказательства назван честно: это документ (AD-2), и исполнение
#: по нему подтверждает живая проверка, пока прогон идёт, — а не сам факт записи.
CHAIN_COMMAND_ARTIFACTS = ("chain_command.txt", "launch_command.txt")
CHAIN_GUARD_MARK = "chain_sleep_guard.sh"
CHAIN_INHIBIT_MARK = "systemd-inhibit --what=sleep --mode=block"

#: Имена способов защиты, которые расписка обязана назвать. `held = true` без имени
#: способа доказательством не считается: «зелено без причины» неотличимо от
#: «зелено потому, что забыли проверить».
PROTECTION_METHODS = ("inhibit", "masked_targets")


def guard_receipt(run_dir: Path) -> tuple[str, str]:
    """Чем подтверждён запрет сна в этом прогоне — распиской или записанной командой.

    Три исхода, и они различимы не по словам, а по носителю:

      ``held``     — расписка обвязки (`guard*.json`) с `held = true` **и названным
                     способом** (`protection: inhibit | masked_targets`): защиту
                     держала обвязка, внешний контур или площадка, нагрузка шла под
                     ней. Расписка, которая говорит `held = true`, но способа не
                     называет, доказательством не считается (``unnamed``);
      ``chain``    — защита стоит в **записанной команде** прогона
                     (`chain_command.txt`/`launch_command.txt`): первым в ней идёт
                     `tools/chain_sleep_guard.sh`, который решает на площадке
                     прогона, каким из двух способов защищаться, и отказывает
                     закрыто, если ни один не доказан (прежняя форма —
                     `systemd-inhibit` прямо в команде — читается так же);
                     это доказательство документа (AD-2), и предел его честно назван:
                     исполнение подтверждает живая проверка, пока прогон идёт, а не
                     сам факт записи;
      ``no_payload`` — расписка есть, а нагрузки не было (`payload = null`): обвязка
                     отказала до старта либо шла только проверка устройства —
                     GPU-работы не произошло, и это названный отказ, а не находка;
      ``bypass``   — расписка сама говорит, что нагрузка шла **без** запрета
                     (`held` не true, а `payload` есть): это признание в обходе, и оно
                     находка, а не «расписка есть — значит всё в порядке»;
      ``unnamed``  — расписка утверждает `held = true`, но не называет способ: «зелено
                     без причины» — находка, а не доказательство;
      ``none``     — ни расписки, ни защиты в записанной команде.
    """
    found: list[str] = []
    for path in sorted(run_dir.glob("guard*.json")):
        found.append(path.name)
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        block = rec.get("sleep_inhibit") or {}
        if block.get("held") is True:
            method = block.get("protection")
            if method in PROTECTION_METHODS:
                return "held", (f"{path.name}: защита сна — {method}"
                                f" ({block.get('held_by') or 'держатель не назван'})")
            return "unnamed", (f"{path.name}: расписка говорит held=true, но способ защиты "
                               f"не назван (protection={method!r}) — «зелено без причины» "
                               f"доказательством не считается")
        if rec.get("payload") is not None:
            return "bypass", (f"{path.name}: расписка говорит, что нагрузка шла без "
                              f"запрета сна (sleep_inhibit.held не true, а нагрузка есть)")
    for name in CHAIN_COMMAND_ARTIFACTS:
        path = run_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if CHAIN_GUARD_MARK in text:
            return "chain", (f"{name}: защита сна стоит в записанной команде прогона "
                             f"(шаг {CHAIN_GUARD_MARK} перед цепочкой — способ решается "
                             f"на площадке прогона)")
        if CHAIN_INHIBIT_MARK in text:
            return "chain", f"{name}: запрет сна стоит в записанной команде прогона"
    if found:
        return "no_payload", (f"расписка есть ({', '.join(found)}), нагрузки не было — "
                              f"GPU-работы не произошло")
    return "none", "ни расписки обвязки, ни запрета сна в записанной команде прогона"


def check_receipts(runs_root: Path, since: datetime.datetime) -> dict:
    """Прогоны с момента вступления правила обязаны нести расписку обвязки.

    Три состояния, а не два (ADR-023 п.12, ADR-053 п.2): ``held``/``chain`` —
    защита доказана; находка — прогон после порога без доказательства, расписка без
    имени способа или признание в обходе; ``historical_without_guard`` — прогон
    **старше правила** (``legacy-not-checked``, защита сна НЕ доказана) либо
    названный отказ обвязки до старта. Порог ``since`` — момент, а не дата.
    """
    findings: list[str] = []
    historical: list[str] = []
    fresh: list[dict] = []
    if not runs_root.is_dir():
        return {"runs_root": str(runs_root), "checked": 0, "findings": findings,
                "historical_without_guard": historical, "runs": fresh,
                "not_verified": f"каталога прогонов нет: {runs_root}"}
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        if not any((run_dir / name).exists() for name in
                   ("run_manifest.json", "chain.sh", "pilot_chain.sh", "chain.log")):
            continue
        start, source = run_start(run_dir)
        state, why = guard_receipt(run_dir)
        in_scope = start is not None and start >= since
        row = {"run": run_dir.name, "started_at": start.isoformat() if start else None,
               "started_from": source, "in_scope": in_scope,
               "guard": state in ("held", "chain"), "guard_state": state,
               "guard_note": why}
        fresh.append(row)
        if state in ("held", "chain"):
            continue
        if state == "no_payload":
            historical.append(f"{run_dir.name}: {why}")
            continue
        if state == "bypass":
            findings.append(f"{run_dir.name}: прогон под запретом не шёл — {why}")
            continue
        if state == "unnamed":
            findings.append(f"{run_dir.name}: расписка не называет способ защиты сна — {why}")
            continue
        if in_scope:
            findings.append(f"{run_dir.name}: прогон начат {start.isoformat()} — "
                            f"после вступления правила, расписки о запрете сна нет ({why})")
        else:
            # Не находка, но и не «проверено»: третье состояние, названное словом
            # (ADR-023 п.12, ADR-053 п.2 — та же симметрия, что у C-030/C-031).
            when = start.isoformat() if start else "старт не датируется"
            historical.append(
                f"{run_dir.name}: legacy-not-checked — старше правила "
                f"(начат {when}, {source}), защита сна НЕ доказана")
    return {"runs_root": str(runs_root), "checked": len(fresh), "since": since.isoformat(),
            "findings": findings, "historical_without_guard": historical, "runs": fresh,
            "not_verified": None}


def load_finding(label: str, row: dict, protection: dict) -> str:
    """Находка «нагрузка без запрета» — с состоянием площадки, если оно измерено.

    Формулировка зависит от того, защищена ли площадка: на площадке со структурно
    недостижимым сном нагрузка не в опасности, но она **пришла мимо обвязки** (нет
    метки ``CUDA_SLEEP_GUARD=1``) — а вместе с обвязкой не пришли ни расписка, ни
    предполётная проверка устройства. Поэтому находка остаётся находкой, но
    называется тем, чем является, а не «сон наступит» там, где он недостижим.
    """
    who = f"на площадке {label}" if label != "local" else "локально"
    if protection.get("method") == "masked_targets":
        return (f"{who} GPU-нагрузка мимо обвязки: pid {row['pid']} — {row['cmdline']} "
                f"(нет метки {GUARD_ENV_MARKER}=1 и нет предка systemd-inhibit; сон "
                f"площадки при этом структурно недостижим — {protection['why']})")
    return f"{who} GPU-нагрузка без запрета сна: pid {row['pid']} — {row['cmdline']}"


def collect(args: argparse.Namespace) -> dict:
    """Обе площадки и оба источника: живые нагрузки и расписки прогонов."""
    since = _parse_iso(args.since) or _parse_iso(ENACTED_AT)
    report: dict = {"schema": "gpu-sleep-guard/2",
                    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "rule": "C-029 (AD-9): во время GPU-нагрузки кейса запрет сна действует",
                    "loads_declared": list(args.loads),
                    "since": since.isoformat() if since else None,
                    "platforms": [], "not_verified": [], "not_verified_input": [],
                    "findings": []}

    local_procs, local_err = local_processes(Path(args.proc_root))
    local_loads = find_loads(local_procs, args.loads) if local_err is None else []
    if args.mechanism_probe and local_err is None:
        local_mech = local_mechanism()
        local_mask = local_mask_probe()
    else:
        local_mech = {"available": None, "why": "не проверялся"}
        local_mask = None
    local_prot = resolve_protection(local_mech, local_mask)
    report["platforms"].append({
        "platform": "local", "processes": len(local_procs),
        "loads": local_loads,
        "uninhibited": [r for r in local_loads if not r["sleep_inhibit"]],
        "mechanism": local_mech, "masked_targets": local_mask, "protection": local_prot,
        "verdict": platform_verdict(local_err, local_prot),
        "not_verified": local_err})
    if local_err:
        # Своя таблица процессов — не «площадка», а вход: не увидев её, страж не
        # проверил ничего, и зелёный вердикт был бы утверждением без основания.
        report["not_verified_input"].append(f"локальная площадка: {local_err}")
    elif not local_prot["method"]:
        if local_prot["measured"]:
            report["findings"].append(
                f"локальная площадка не защищена ни одним из двух способов — "
                f"{local_prot['why']}. GPU-нагрузка здесь может быть уничтожена сном "
                f"(Xid 31, 21.09.2026): её числа не будут свидетельством")
        elif not local_loads:
            report["not_verified"].append(
                f"локальная площадка: защита от сна не измерена ({local_prot['why']})")
    for row in local_loads:
        if not row["sleep_inhibit"]:
            report["findings"].append(load_finding("local", row, local_prot))

    for host in args.host:
        procs, mech, mask, err = remote_probe(host, args.timeout,
                                              mechanism=args.mechanism_probe)
        # Проверка механизма не спрошена — не спрошена и проба маскировки: у
        # площадки тогда нет измеренной защиты, а не «её нет».
        prot = resolve_protection(mech or {"available": None, "why": "не проверялся"},
                                  mask)
        loads = find_loads(procs, args.loads) if err is None else []
        report["platforms"].append({
            "platform": host, "processes": len(procs), "loads": loads,
            "uninhibited": [r for r in loads if not r["sleep_inhibit"]],
            "mechanism": mech, "masked_targets": mask, "protection": prot,
            "verdict": platform_verdict(err, prot), "not_verified": err})
        if err:
            report["not_verified"].append(f"площадка {host}: {err}")
            continue
        if not prot["method"]:
            if prot["measured"]:
                report["findings"].append(
                    f"площадка {host} не защищена ни одним из двух способов — "
                    f"{prot['why']}; старт стадии на этой площадке обвязка не пропустит, "
                    f"а нагрузка, пущенная мимо неё, будет уничтожена сном (Xid 31)")
            elif not loads:
                report["not_verified"].append(
                    f"площадка {host}: механизм запрета сна недоступен "
                    f"({(mech or {}).get('why')}) и маскировка целей сна не доказана "
                    f"({(mask or {}).get('why', 'не измерена')}) — защитить нагрузку "
                    f"здесь нечем; старт стадии на этой площадке обвязка не пропустит")
        for row in loads:
            if not row["sleep_inhibit"]:
                report["findings"].append(load_finding(host, row, prot))

    receipts = check_receipts(Path(args.runs_root), since)
    report["receipts"] = receipts
    if receipts["not_verified"]:
        report["not_verified_input"].append(receipts["not_verified"])
    report["findings"].extend(receipts["findings"])
    return report


def platform_verdict(err: str | None, protection: dict) -> str:
    """Вердикт площадки: protected | blocked | not_verified | unknown.

    ``blocked`` — площадка **измерена** и защиты нет: это находка. ``not_verified``
    — измерение не состоялось (площадка не прочитана либо защита не спрошена):
    сказать «не защищена» здесь было бы утверждением без основания.
    """
    if err:
        return "not_verified"
    if protection.get("method"):
        return "protected"
    return "blocked" if protection.get("measured") else "not_verified"


def print_report(report: dict) -> None:
    for platform in report["platforms"]:
        head = f"== площадка {platform['platform']} (процессов: {platform['processes']}) =="
        print(head)
        mech = platform.get("mechanism") or {}
        if mech:
            state = ("берётся" if mech.get("available") else
                     "недоступен" if mech.get("available") is False else "не проверялся")
            print(f"  механизм запрета сна: {state} ({mech.get('why', '—')})")
        prot = platform.get("protection") or {}
        if prot:
            method = prot.get("method")
            if method:
                print(f"  защита сна: {method} — {prot.get('why')}")
            elif prot.get("measured"):
                print(f"  защита сна: БЛОКЕР — {prot.get('why')}")
            else:
                print(f"  защита сна: не измерена — {prot.get('why')}")
        if platform["not_verified"]:
            print(f"  NOT-VERIFIED: {platform['not_verified']}")
            continue
        if not platform["loads"]:
            print("  GPU-нагрузок объявленного набора не найдено")
        for row in platform["loads"]:
            mark = "ok  " if row["sleep_inhibit"] else "FAIL"
            why = row["why"] or "запрета сна нет ни в предках, ни в окружении"
            print(f"  [{mark}] pid {row['pid']}: {row['cmdline']}")
            print(f"         запрет сна: {why}")
    receipts = report["receipts"]
    print(f"== расписки прогонов ({receipts['runs_root']}, "
          f"начатых после {report['since']}) ==")
    if receipts["not_verified"]:
        print(f"  NOT-VERIFIED: {receipts['not_verified']}")
    else:
        held = sum(1 for r in receipts["runs"] if r["guard_state"] == "held")
        chained = sum(1 for r in receipts["runs"] if r["guard_state"] == "chain")
        print(f"  прогонов учтено: {receipts['checked']}, под запретом: {held + chained} "
              f"(расписка обвязки: {held}, записанная команда: {chained})")
        for name in receipts["historical_without_guard"]:
            print(f"  не находка: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Страж C-029 (AD-9): во время GPU-нагрузки кейса запрет сна действует")
    parser.add_argument("--case-root", default=".",
                        help="корень кейса (по умолчанию текущий каталог)")
    parser.add_argument("--proc-root", default="/proc",
                        help="корень таблицы процессов (для синтетики — свой)")
    parser.add_argument("--runs-root", default=None,
                        help="каталог прогонов (по умолчанию <кейс>/runs)")
    parser.add_argument("--host", action="append", default=[],
                        help="площадка стенда (повторяемый); читается только на чтение")
    parser.add_argument("--timeout", type=int, default=10, help="таймаут ssh, секунды")
    parser.add_argument("--since", default=ENACTED_AT,
                        help="с какого момента прогон обязан нести расписку — ISO-8601 "
                             f"с таймзоной (по умолчанию {ENACTED_AT} — момент вступления "
                             "правила в силу, ADR-053)")
    parser.add_argument("--loads", default=None,
                        help="переопределить набор GPU-нагрузок (через запятую)")
    parser.add_argument("--mechanism-probe", dest="mechanism_probe", action="store_true",
                        default=True,
                        help="проверять, берётся ли запрет сна на площадке (по умолчанию да)")
    parser.add_argument("--no-mechanism-probe", dest="mechanism_probe", action="store_false",
                        help="не трогать механизм запрета (для синтетики и офлайн-прогонов)")
    parser.add_argument("--unreachable-not-verified", action="store_true",
                        help="непроверенная площадка (недоступна либо без механизма "
                             "запрета) — NOT-VERIFIED, а не отказ: профиль гейта кейса")
    parser.add_argument("--strict", action="store_true",
                        help="NOT-VERIFIED красит гейт (exit 2)")
    parser.add_argument("--json", action="store_true", help="отчёт одним объектом JSON")
    parser.add_argument("--assert-held", action="store_true",
                        help="вопрос «запрет сна действует для ЭТОГО процесса?»: 0 — да "
                             "(свой инхибит или структурная недостижимость сна на "
                             "площадке), 2 — нет")
    parser.add_argument("--prove-protection", action="store_true",
                        help="доказательство защиты одним объектом JSON (для обвязки и "
                             "раннера): способ, попытка, причина; 0 — доказано, 2 — нет")
    args = parser.parse_args(argv)

    case_root = Path(args.case_root).resolve()
    if args.loads:
        args.loads = tuple(x.strip() for x in args.loads.split(",") if x.strip())
    else:
        args.loads = GPU_LOADS

    if args.prove_protection:
        # Тот же вопрос, что у `--assert-held`, но ответ — объектом: обвязке нужен не
        # только факт («защита есть»), но и **способ** (`inhibit` | `masked_targets`)
        # и его доказательство — их она кладёт в расписку. Спрашивается один раз и в
        # одном месте, поэтому «взяли запрет» и «страж увидел» разойтись не могут.
        prot = current_protection(Path(args.proc_root))
        print(json.dumps(prot, ensure_ascii=False, indent=2))
        return EXIT_OK if prot["held"] else EXIT_NOT_VERIFIED

    if args.assert_held:
        # Предполётный вопрос раннера и цепочки: «мне разрешено стартовать?» Ответ
        # — по своей же таблице процессов, без запуска полезной нагрузки: страж,
        # который для ответа поднимает GPU-работу, сам стал бы нагрузкой.
        prot = current_protection(Path(args.proc_root))
        if prot["held"]:
            print(f"запрет сна действует ({prot['method']}): {prot['why']}")
            return EXIT_OK
        print(f"ОТКАЗ: {prot['why']}. "
              "GPU-нагрузка не стартует (S3 во время прогона уничтожает контекст "
              "CUDA: Xid 31, 21.09.2026). Запуск — через tools/guard_cuda_run.sh",
              file=sys.stderr)
        return EXIT_NOT_VERIFIED

    if not case_root.is_dir():
        print(f"NOT-VERIFIED: корень кейса не найден: {case_root}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    if args.runs_root is None:
        args.runs_root = str(case_root / "runs")

    report = collect(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)

    if report["findings"]:
        if not args.json:
            print(f"НАРУШЕНИЙ: {len(report['findings'])}")
        for finding in report["findings"]:
            print(f"  - {finding}")
        return EXIT_FAIL
    if report["not_verified_input"]:
        # Вход не прочитан — проверять было нечего. Зелёный вердикт здесь означал бы
        # «проверено», хотя не проверено ничего; это красное и без --strict.
        # Печатается в stderr: при `--json` stdout обязан остаться **одним объектом
        # JSON** (иначе отчёт неразбираем машиной — ровно то, ради чего он и нужен).
        for note in report["not_verified_input"]:
            print(f"NOT-VERIFIED: {note}", file=sys.stderr)
        print("NOT-VERIFIED: вход не прочитан — вердикта нет", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    if report["not_verified"]:
        if not args.json:
            print("NOT-VERIFIED: " + "; ".join(report["not_verified"]))
        if args.strict and not args.unreachable_not_verified:
            print("--strict: непроверенная площадка считается красной", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        return EXIT_OK
    loads = sum(len(p["loads"]) for p in report["platforms"])
    platforms = ", ".join(p["platform"] for p in report["platforms"])
    guarded = sum(1 for r in report["receipts"]["runs"] if r["guard"])
    scoped = sum(1 for r in report["receipts"]["runs"] if r["in_scope"])
    # Вакуумный зелёный называется вакуумным: «нагрузок не найдено» — это не то же
    # самое, что «нагрузки под запретом», и сливать их в одну строку нельзя.
    what = (f"живых GPU-нагрузок объявленного набора не найдено"
            if loads == 0 else
            f"все живые GPU-нагрузки ({loads}) идут под запретом сна")
    # Способ защиты называется в итоговой строке: «зелено» без причины не выдаётся —
    # `inhibit` и `masked_targets` доказываются разными пробами и взаимозаменяемы
    # только там, где доказаны.
    how = ", ".join(f"{p['platform']}: {(p.get('protection') or {}).get('method') or '—'}"
                    for p in report["platforms"])
    # При `--json` итог уходит в stderr: stdout — один объект JSON и ничего больше.
    print(f"SLEEP GUARD OK: {what} на площадках [{platforms}] "
          f"(защита — {how}); прогонов после "
          f"{report['since']} в области правила: {scoped}, под запретом: {guarded}",
          file=(sys.stderr if args.json else sys.stdout))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
