#!/usr/bin/env bash
# guard_cuda_run.sh — обвязка CUDA-прогона: запрет сна, предполётная проверка
# устройства и расписка о том, что устройство выжило.
#
# **Зачем это появилось.** Прогон V3 дельты decode-diagnosis (21.09.2026, 05:03)
# умер с `cudaErrorLaunchFailure` на 24-й пробе из 104. Трейс показал обработчик
# запрета 4-грамм, и это выглядело дефектом прибора. Ядро сказало другое::
#
#     NVRM: Xid (PCI:0000:01:00): 31, pid=2340175, name=python3 —
#     MMU Fault: ENGINE HOST3 HUBCLIENT_ESC faulted @ 0x2_00224000,
#     Fault is of type FAULT_PDE ACCESS_TYPE_VIRT_READ
#
# и за семь секунд до этого — вход и выход из S3 («Preparing to enter system sleep
# state S3» → «Waking up from system sleep state S3», 05:03:37). То есть во время
# прогона машина ушла в сон: контекст CUDA этого не переживает, следующее ядро
# уходит по мёртвой таблице страниц, а исключение всплывает на ближайшей
# синхронизации — ею оказался `.tolist()` в обработчике. После этого устройство
# не поднялось вовсе (драйверный `cuInit` → 999, устройств 0).
#
# Отсюда два обязательства, которые этот страж и выполняет:
#
#   1. **Сон не случается во время прогона** — `systemd-inhibit --what=sleep
#      --mode=block` вокруг полезной нагрузки. Это единственное, что отделяет
#      многочасовой прогон от потери и прогона, и устройства.
#   2. **Отказ виден, а не выглядит замером** — расписка пишет состояние
#      устройства до и после, счётчик Xid до и после и **число дошедших проб**
#      по логу прибора. Прогон, убитый отказом устройства, обязан отличаться от
#      прогона, который просто кончился: иначе «нет отчёта» читается как «не
#      мерили», а не как «измерение оборвано отказом».
#
# **Почему обвязка, а не правка прибора.** Прибор пиннут хешем
# (`99dafa8d…` — он же в решающем отчёте S3aq), и его правка сломала бы
# сопоставимость чисел стадии (ADR-041). Страж не трогает прибор: он оборачивает
# вызов и ничего не меняет в замере — ни в RNG, ни в байтах ответов.
#
# **Почему не «просто перезапустить».** Перезапуск лечит следствие и оставляет
# причину: следующий прогон на той же машине умрёт так же, а флот дельт работает
# часами. Запрет сна снимает причину, не меняя ни одного измеренного числа.
#
# Вердикты расписки (и коды возврата)::
#
#     ok                      0  прогон дошёл, нового Xid нет, устройство цело
#     payload_failed          4  нагрузка вернула ненулевой код, отказа не было
#     device_faulted          5  новый Xid и/или устройство испортилось во время
#                                прогона — числа недействительны
#     device_unusable         2  отказ до старта: устройство непригодно (require)
#     device_unusable_no_evidence  5  --device-check warn/skip и устройство
#                                непригодно и до, и после: прогон не свидетельство
#     refused_no_inhibit      2  защита сна не действует и взять её не удалось
#                                (ни инхибита, ни маскировки) — прогон не запускался
#
# **Запрет сна обязателен, а не рекомендован** (дельта ``sleep-guard-in-launch``,
# 21.09.2026). Прежняя редакция позволяла флагом ``--no-inhibit`` объявить отказ от
# запрета и всё равно запустить нагрузку: защита держалась на памяти человека, а
# обход не оставлял следа. Теперь у флага другое значение — «запрет сна **уже
# взят** снаружи» (вложенный вызов, цепочка под общим ``systemd-inhibit``); если он
# не взят, прогон не стартует. Взят ли он — решает не самообъявление, а
# ``tools/check_gpu_sleep_guard.py --assert-held``: тот же механизм, которым
# пользуется правило C-029, поэтому «взяли» и «страж увидел» не могут разойтись.
#
# **Два способа защиты, оба доказываются фактом** (дельта ``sleep-guard-masked-stand``,
# 21.09.2026). Инхибит — не единственный способ удержать сон, и на стенде
# ``gb10-fast`` его взять неоткуда (``Failed to inhibit: Access denied`` — нет
# logind-сессии). Стенд защищён иначе и **сильнее**: цели сна замаскированы
# (``/etc/systemd/system/{sleep,suspend,hibernate,hybrid-sleep}.target → /dev/null``,
# переживает перезагрузку), и это доказано попыткой — ``systemctl start
# suspend.target`` проваливается с причиной о маскировке. Поэтому обвязка принимает
# **либо** действующий инхибит, **либо** доказанную структурную недостижимость сна;
# способ называется в расписке (``protection: inhibit | masked_targets``), а
# ``held_by`` — кто защиту держит (``guard`` | ``caller`` | ``platform``). «Зелено без
# причины» не бывает: расписка без имени способа доказательством не считается.
#
# Взят ли запрет — видно не только по расписке, но и по факту: полезная нагрузка
# получает метку ``CUDA_SLEEP_GUARD=1`` в окружении, и по ней живая нагрузка
# опознаётся стражем, даже если расписка потерялась.
#
# Запуск::
#
#     tools/guard_cuda_run.sh --out runs/<run>/guard.json --why "проба ADR-041" \
#         --probe-log runs/<run>/v3.log -- \
#         /usr/bin/python3 tools/probe_language_split.py --prompts wide ...
#
# Проверка только устройства (без полезной нагрузки)::
#
#     tools/guard_cuda_run.sh --check --out /tmp/guard-check.json
#
# --device-check — объявленный, а не подразумеваемый выбор: `require` (по
# умолчанию) отказывает до старта на непригодном устройстве; `warn` читает
# устройство и называет его состояние в расписке; `skip` не читает устройство
# вовсе, и расписка честно говорит, что не подтверждает его.

set -uo pipefail

OUT=""
CHECK=0
DEVICE_CHECK="require"
XID_SOURCE=""
PROBE_LOG=""
INHIBIT=1
WHY="CUDA-прогон: S3 во время прогона уничтожает контекст CUDA (Xid 31)"
#: Объявлены до первой записи расписки: отказы случаются и до того, как механизм
#: запрета сна проверен, а `set -u` сделал бы такую расписку невозможной —
#: то есть отказ остался бы без свидетельства ровно там, где оно нужнее всего.
HAS_INHIBIT=no
INHIBIT_SELF=no
#: Кто держит запрет на время нагрузки: сам страж (`guard`), вызвавший его
#: контур (`caller`, флаг `--no-inhibit`) или площадка (`platform` — сон
#: структурно недостижим). Пусто — защиты нет, и прогона не будет.
HELD_BY=""
#: **Чем** защита держится: `inhibit` | `masked_targets`. Пусто — нечем: расписка
#: без имени способа доказательством не считается («зелено без причины» запрещено).
PROTECTION=""
PROT_WHY=""
#: Чем защита оказалась **после** нагрузки (пусто — нагрузки не было): сравнивается
#: со стартовой, потому что и инхибит, и маскировку можно потерять во время прогона.
PROT_AFTER=""
TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKER="$TOOLS_DIR/check_gpu_sleep_guard.py"

usage() { awk 'NR>1 && /^set -uo pipefail/{exit} NR>1{sub(/^# ?/,""); print}' "${BASH_SOURCE[0]}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="${2:-}"; shift 2 ;;
    --check) CHECK=1; shift ;;
    --device-check) DEVICE_CHECK="${2:-}"; shift 2 ;;
    --xid-source) XID_SOURCE="${2:-}"; shift 2 ;;
    --probe-log) PROBE_LOG="${2:-}"; shift 2 ;;
    --no-inhibit) INHIBIT=0; shift ;;
    --why) WHY="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    *) echo "неизвестный аргумент: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [ -z "$OUT" ]; then
  echo "отказ: не задан --out — расписка обязательна (иначе отказ неотличим от успеха)" >&2
  exit 1
fi
if [ "$CHECK" -eq 0 ] && [ $# -eq 0 ]; then
  echo "отказ: не задана полезная нагрузка (после --) и не задан --check" >&2
  exit 1
fi
case "$DEVICE_CHECK" in
  require|warn|skip) ;;
  *) echo "отказ: --device-check $DEVICE_CHECK; допустимо require|warn|skip" >&2; exit 1 ;;
esac
# --check и skip противоречат друг другу: проверка, которой запрещено читать
# устройство, назвала бы его непригодным не читая — то есть записала бы в расписку
# утверждение, которого не измеряла. Отказ, а не «пусть будет unusable».
if [ "$CHECK" -eq 1 ] && [ "$DEVICE_CHECK" = "skip" ]; then
  echo "отказ: --check и --device-check skip противоречат друг другу — проверять нечего" >&2
  exit 1
fi

# ── чтение состояния устройства драйверным путём, а не через torch ────────────
# Через torch нельзя: при недоступном устройстве torch падает на инициализации
# контекста, и «проверка устройства» сама стала бы источником отказа. `cuInit` —
# нижняя точка входа драйвера: 0 = устройство есть, 999 = CUDA_ERROR_UNKNOWN, то
# самое состояние после Xid 31 (у нас 21.09: cuInit 999, устройств 0 — при живом
# nvidia-smi, который ходит другим путём).
read -r -d '' DEVICE_PY <<'PY' || true
import ctypes, json, sys
if sys.argv[1] == "skip":
    print(json.dumps({"checked": False,
                      "why": "устройство не читалось (--device-check skip)"}))
    raise SystemExit(0)
out = {"checked": True, "ok": False}
try:
    lib = ctypes.CDLL("libcuda.so.1")
except OSError as exc:
    out["why"] = f"libcuda.so.1 не открывается: {exc}"
    print(json.dumps(out)); raise SystemExit(0)
rc = int(lib.cuInit(0))
out["cu_init_rc"] = rc
if rc != 0:
    out["why"] = ("cuInit != 0 — драйвер не отдаёт устройство "
                  "(999 = CUDA_ERROR_UNKNOWN: состояние после отказа GPU)")
    print(json.dumps(out)); raise SystemExit(0)
n = ctypes.c_int(0)
lib.cuDeviceGetCount(ctypes.byref(n))
out["n_devices"] = int(n.value)
if n.value < 1:
    out["why"] = "драйвер инициализировался, но устройств 0"
    print(json.dumps(out)); raise SystemExit(0)
d = ctypes.c_int(0)
lib.cuDeviceGet(ctypes.byref(d), 0)
buf = ctypes.create_string_buffer(256)
lib.cuDeviceGetName(buf, 256, d)
out["name"] = buf.value.decode(errors="replace")
out["ok"] = True
print(json.dumps(out))
PY
device_state() { python3 -c "$DEVICE_PY" "$1" 2>/dev/null \
  || echo '{"checked": false, "ok": false, "why": "устройство прочитать не удалось: python3 или libcuda недоступны"}'; }

# ── Xid: сколько отказов устройства ядро записало с начала прогона ────────────
# Счётчик, а не факт: Xid'ы копятся в журнале, и «Xid есть» без вычитания назвало
# бы отказом прогон, стартовавший уже после чужого отказа. Источник вынесен в
# --xid-source, чтобы красный путь проверялся синтетическим журналом, а не
# ожиданием настоящего отказа GPU.
xid_lines() {
  if [ -n "$XID_SOURCE" ]; then eval "$XID_SOURCE" 2>/dev/null
  else journalctl -k --since "@${1:-0}" 2>/dev/null | grep -F "Xid" || true; fi
}
xid_count() { xid_lines "$1" | grep -c . || true; }

# ── число дошедших проб из лога прибора ───────────────────────────────────────
# Прибор печатает строку на пробу (`  [состояние/режим] тег: …`) и пишет отчёт
# только целиком, в конце состояния: при обрыве сделанная работа в отчёт не
# попадает. Число строк в логе — единственный след этой работы, и расписка
# обязана его назвать, иначе «24 пробы сделаны и потеряны» неотличимо от «не
# начинали».
probe_lines() {
  if [ -n "$PROBE_LOG" ] && [ -f "$PROBE_LOG" ]; then grep -c '^  \[' "$PROBE_LOG" || true
  else echo 0; fi
}

json_get() { printf '%s' "$1" | python3 -c "import json,sys;print(json.load(sys.stdin).get('$2'))" 2>/dev/null || echo "None"; }

# Вердикт и расписка — одним вызовом, чтобы арифметика вердикта жила в одном
# месте и не расползалась по ветвям выхода.
write_receipt() {  # <verdict> <exit> <dev_before> <dev_after> <xid_before> <xid_after> <pb> <pa> <rc> <elapsed>
  python3 - "$OUT" "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9" "${10}" \
          "$DEVICE_CHECK" "$INHIBIT" "$INHIBIT_SELF" "$WHY" "$XID_SOURCE" "$CHECK" "$PROBE_LOG" \
          "$HELD_BY" "$PROTECTION" "$PROT_WHY" "$PROT_AFTER" <<'PY'
import json, sys, datetime
(o, verdict, code, db, da, xb, xa, pb, pa, rc, el, mode, inh, self_test, why,
 xsrc, check, plog, held_by, protection, prot_why, prot_after) = sys.argv[1:]
#: Название механизма — по способу, которым защита доказана. Строка одна на оба
#: способа не бывает: `inhibit` и `masked_targets` доказываются разными пробами, и
#: расписка, называющая не тот, вводила бы в заблуждение ровно там, где она —
#: свидетельство.
MECHANISMS = {
    "inhibit": "systemd-inhibit --what=sleep --mode=block",
    "masked_targets": ("маскировка целей сна (sleep/suspend/hibernate/hybrid-sleep → "
                       "/dev/null), доказана провалом попытки старта suspend.target"),
}
dev_b, dev_a = json.loads(db), (json.loads(da) if da != "null" else None)
checked = bool(dev_b.get("checked"))
if not checked:
    health = "unknown"
elif dev_a is None:
    # Прогона не было (отказ до старта или --check): состояние берётся из
    # предполётного чтения, иначе расписка об отказе не называла бы устройства.
    health = "unusable" if not dev_b.get("ok") else "ok"
elif dev_b.get("ok") and dev_a.get("ok"):
    health = "ok"
elif dev_b.get("ok") and not dev_a.get("ok"):
    health = "degraded"
elif not dev_b.get("ok") and dev_a.get("ok"):
    health = "recovered"
else:
    health = "unusable"
ran = rc != "None"
if not ran:
    # Прогона не было — сравнивать защиту «до и после» не с чем: расписка не
    # утверждает, что защита держалась во времени, она утверждает, что прогон
    # начался под ней.
    prot_after = None
warnings = []
if prot_after and protection and prot_after != protection:
    warnings.append(f"защита сна изменилась во время прогона: до старта — {protection}, "
                    f"после — {prot_after}. Числа прогона сняты **не под той защитой**, "
                    f"под какой он начат: проверять, покрывает ли {prot_after} весь "
                    f"прогон, а не только его конец")
elif ran and not prot_after and protection:
    warnings.append("защиту сна после прогона подтвердить не удалось (проба не ответила): "
                    "расписка подтверждает её на старте, но не на всём протяжении")
if not checked:
    warnings.append("устройство не читалось (--device-check skip): расписка не "
                    "подтверждает, что числа сняты на живом устройстве")
if health == "unusable":
    warnings.append("устройство непригодно и до, и после прогона: прогон не "
                    "может быть свидетельством о модели" if ran else
                    "устройство непригодно: прогон не запускался")
if health == "recovered":
    warnings.append("устройство было непригодно до прогона и стало пригодно "
                    "после: прогон начат на только что поднявшемся устройстве")
if ran and held_by == "":
    # Недостижимо по построению: без действующего запрета нагрузка не стартует
    # (отказ выше). Строка стоит как утверждение инварианта: если она появится,
    # значит в страж пролез путь запуска без запрета — и расписка это назовёт.
    warnings.append("нагрузка запускалась без действующего запрета сна: расписка "
                    "противоречит контракту стража — прогон считать недействительным")
rec = {"schema": "cuda-run-guard/2",
       "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
       "device_check": mode, "check_only": check == "1",
       "device": {"checked": checked, "before": dev_b, "after": dev_a,
                  "health": health},
       "xid": {"before": (int(xb) if xb != "None" else None),
               "after": (int(xa) if xa != "None" else None),
               "new": (int(xa) - int(xb)) if (xb != "None" and xa != "None") else None,
               "source": xsrc or "journalctl -k (строки с Xid)"},
       # `held` — «защита сна действовала на момент нагрузки»; `held_by` называет,
       # кто её держал (обвязка, внешний контур или площадка), а `protection` —
       # **чем** (инхибит или структурная недостижимость сна). Нагрузка без
       # действующей защиты не запускается вовсе, поэтому у расписки с
       # завершившейся нагрузкой `held` всегда true, а `payload = null` означает
       # отказ до старта — то есть GPU-работы не было. Способ назван всегда:
       # «зелено без причины» распиской не считается.
       "sleep_inhibit": {"requested": str(inh) == "1", "held": held_by != "",
                         "held_by": held_by or None,
                         "protection": protection or None,
                         "protection_why": prot_why or None,
                         "protection_after": (prot_after or None),
                         "self_test": self_test == "yes",
                         "mechanism": MECHANISMS.get(protection,
                                                     "защита сна не установлена"),
                         "why": why},
       "payload": (None if rc == "None" else {"rc": int(rc),
                                              "elapsed_s": (int(el) if el != "None" else None)}),
       "probes": {"log": (plog or None),
                  "completed_before": (int(pb) if pb != "None" else None),
                  "completed": (int(pa) if pa != "None" else None)},
       "verdict": verdict, "exit_code": int(code), "warnings": warnings,
       "why": {"ok": "прогон дошёл, нового Xid нет, устройство цело",
               "payload_failed": "нагрузка вернула ненулевой код; отказа устройства не было",
               "device_faulted": ("устройство отказало во время прогона (новый Xid и/или "
                                  "переход «цело → испорчено»): числа прогона недействительны"),
               "device_unusable": ("отказ до старта: драйвер не отдаёт устройство "
                                   "(--device-check require)"),
               "device_unusable_no_evidence": ("устройство непригодно и до, и после: "
                                               "прогон запускался на мёртвом устройстве и "
                                               "свидетельством быть не может"),
               "refused_no_inhibit": ("запрет сна не взят: прогон не запускался — S3 во "
                                      "время CUDA-прогона уничтожает контекст (Xid 31)")}[verdict]}
open(o, "w", encoding="utf-8").write(json.dumps(rec, ensure_ascii=False, indent=2))
PY
}

verdict_exit() {
  case "$1" in
    ok) echo 0 ;;
    payload_failed) echo 4 ;;
    device_faulted) echo 5 ;;
    device_unusable) echo 2 ;;
    device_unusable_no_evidence) echo 5 ;;
    refused_no_inhibit) echo 2 ;;
    *) echo 1 ;;
  esac
}

# ── предполётная проверка устройства ─────────────────────────────────────────
DEV_BEFORE="$(device_state "$( [ "$DEVICE_CHECK" = skip ] && echo skip || echo read )")"
echo "== устройство до прогона =="
echo "$DEV_BEFORE"
DEV_BEFORE_OK="$(json_get "$DEV_BEFORE" ok)"
DEV_BEFORE_CHECKED="$(json_get "$DEV_BEFORE" checked)"

# --check идёт **до** отказа `require`: это и есть предполётная проверка, и её
# ответ — состояние устройства, а не сообщение о том, что прогон не состоялся бы.
if [ "$CHECK" -eq 1 ]; then
  if [ "$DEV_BEFORE_OK" = "True" ]; then
    write_receipt ok 0 "$DEV_BEFORE" "$DEV_BEFORE" None None None None None None
    echo "расписка: $OUT (verdict=ok, --check)"
    exit 0
  fi
  write_receipt device_unusable 2 "$DEV_BEFORE" "$DEV_BEFORE" None None None None None None
  echo "расписка: $OUT (verdict=device_unusable, --check)"
  exit 2
fi

if [ "$DEVICE_CHECK" = "require" ] && [ "$DEV_BEFORE_OK" != "True" ]; then
  echo "ОТКАЗ до старта: устройство непригодно (--device-check require)." >&2
  echo "Это не результат о модели: прогон не состоялся бы вовсе." >&2
  write_receipt device_unusable 2 "$DEV_BEFORE" null None None None None None None
  echo "расписка: $OUT (verdict=device_unusable)"
  exit 2
fi
[ "$DEVICE_CHECK" = "warn" ] && [ "$DEV_BEFORE_OK" != "True" ] \
  && echo "ВНИМАНИЕ: устройство непригодно до прогона, но объявлен --device-check warn" >&2

# ── запрет сна ────────────────────────────────────────────────────────────────
# Самопроверка механизма, а не обещание: systemd-inhibit запускается на пустой
# нагрузке и обязан быть виден в списке держателей. Без этого «взяли запрет»
# было бы утверждением о намерении.
if command -v systemd-inhibit >/dev/null 2>&1; then
  if systemd-inhibit --what=sleep --mode=block --why="guard self-test" \
       sh -c 'systemd-inhibit --list 2>/dev/null | grep -qF "guard self-test"' 2>/dev/null; then
    HAS_INHIBIT=yes; INHIBIT_SELF=yes
  fi
fi
echo "запрет сна: механизм доступен=$HAS_INHIBIT, самопроверка=$INHIBIT_SELF"

# ── защита сна: взять самому, убедиться, что уже взята, или положиться на площадку ──
# Спрашивается у стража, а не у себя: `--prove-protection` — тот же ответ, что даёт
# предполётному вопросу раннер (`--assert-held`), только с **именем способа**
# (`inhibit` | `masked_targets`) и его доказательством. Разойдись эти два ответа —
# «запуск под защитой» и «страж видит защиту» стали бы разными утверждениями.
protection_json() {  # JSON стража о защите ЭТОГО процесса; пусто — страж недоступен
  [ -f "$CHECKER" ] || return 2
  python3 "$CHECKER" --prove-protection 2>/dev/null
}
probe_protection() {  # <переменная-приёмник метода> — «чем защищена площадка сейчас»
  local json method why
  json="$(protection_json || true)"
  method="$(printf '%s' "$json" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("method") or "")' 2>/dev/null || echo "")"
  why="$(printf '%s' "$json" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("why") or "")' 2>/dev/null || echo "")"
  echo "$method|$why"
}

IFS='|' read -r PROT_METHOD PROT_WHY <<<"$(probe_protection)"

if [ "$INHIBIT" -eq 1 ]; then
  if [ "$HAS_INHIBIT" = "yes" ]; then
    HELD_BY="guard"; PROTECTION="inhibit"
  elif [ "$PROT_METHOD" = "masked_targets" ]; then
    # Инхибит взять нечем, но сон площадки структурно недостижим, и это доказано
    # попыткой. Отказать здесь значило бы потребовать ритуал вместо защиты: на
    # gb10-fast маскировка держит сон крепче инхибита (переживает перезагрузку).
    HELD_BY="platform"; PROTECTION="masked_targets"
  fi
else
  # `--no-inhibit` — «защита уже взята снаружи»: проверяется механически, тем же
  # стражем, которым пользуется правило C-029. Объявление без механизма не
  # принимается: иначе флаг снова стал бы способом обойти защиту.
  case "$PROT_METHOD" in
    masked_targets) HELD_BY="platform"; PROTECTION="masked_targets" ;;
    inhibit)        HELD_BY="caller";   PROTECTION="inhibit" ;;
    *)
      if [ "$HAS_INHIBIT" = "yes" ]; then
        # Защиты не взято снаружи, но механизм в порядке — берём её сами: отказать
        # здесь значило бы наказать за лишний флаг, а не за отсутствие защиты.
        HELD_BY="guard"; INHIBIT=1; PROTECTION="inhibit"
        echo "запрет сна: --no-inhibit объявлен, но снаружи защиты нет — беру сам" >&2
      fi ;;
  esac
fi

if [ -z "$HELD_BY" ]; then
  echo "ОТКАЗ до старта: ни запрет сна не действует, ни сон площадки не исключён." >&2
  if [ "$INHIBIT" -eq 0 ]; then
    echo "Флаг --no-inhibit означает «защита сна уже взята снаружи»: проверено стражем" >&2
    echo "($CHECKER --prove-protection) — он не увидел ни инхибита, ни маскировки." >&2
  else
    echo "systemd-inhibit недоступен или не проходит самопроверку, а маскировка целей" >&2
    echo "сна не доказана: $PROT_WHY" >&2
  fi
  echo "Прогон на машине, которая может уйти в S3, — это потеря прогона и устройства (Xid 31, 21.09.2026)." >&2
  echo "Отказаться от защиты нельзя: запуск без неё — находка правила C-029 (AD-9)." >&2
  write_receipt refused_no_inhibit 2 "$DEV_BEFORE" null None None None None None None
  echo "расписка: $OUT (verdict=refused_no_inhibit, защиты нет: ни инхибита, ни маскировки)"
  exit 2
fi
echo "защита сна: способ=$PROTECTION, держит=$HELD_BY — $PROT_WHY"

START_TS="$(date +%s)"
PROBES_BEFORE="$(probe_lines)"
XID_BEFORE="$(xid_count "$START_TS")"
echo "Xid с начала прогона: $XID_BEFORE; проб в логе до прогона: $PROBES_BEFORE"

echo "== прогон =="
# Метка ставится **нагрузке**, а не обвязке: по ней живой процесс опознаётся как
# порождённый обвязкой (tools/check_gpu_sleep_guard.py), даже если расписка
# потерялась или прогон убит до её записи. Через `env`, а не `export`: экспорт
# пометил бы и саму обвязку, а её метка — уже не свидетельство о нагрузке, а шум
# (расписка спрашивает «чем защищена площадка **сейчас**» и получила бы «метка
# обвязки» вместо состояния площадки).
# Обвязка оборачивает нагрузку инхибитом **только тогда, когда держит его сама**:
# под уже взятым снаружи инхибитом обёртка не нужна (и `--no-inhibit` её прямо
# запрещает), а на площадке со структурно недостижимым сном `systemd-inhibit`
# отказывает закрыто и завернул бы исправный прогон в отказ.
if [ "$HELD_BY" = "guard" ]; then
  systemd-inhibit --what=sleep --mode=block --why="$WHY" env CUDA_SLEEP_GUARD=1 "$@"; RC=$?
else
  env CUDA_SLEEP_GUARD=1 "$@"; RC=$?
fi
ELAPSED=$(( $(date +%s) - START_TS ))
echo "код возврата полезной нагрузки: $RC за ${ELAPSED} с"

DEV_AFTER="$(device_state "$( [ "$DEVICE_CHECK" = skip ] && echo skip || echo read )")"
# Защиту после прогона перепроверяют **только у площадочной**: инхибит живёт ровно
# столько, сколько живёт его полезная нагрузка, поэтому «инхибита после прогона
# нет» — норма, а не потеря. Маскировка же живёт в системе, и её снятие во время
# прогона (его делает человек) — событие, которое проба видит: тогда прогон
# закончился не под той защитой, под какой начался. Проба та же, что до старта,
# поэтому «стало хуже» видно сравнением, а не догадкой; вердикт она не меняет —
# вердикт считают устройство и Xid, а расхождение называется предупреждением.
if [ "$PROTECTION" = "masked_targets" ]; then
  IFS='|' read -r PROT_AFTER_METHOD _ <<<"$(probe_protection)"
  PROT_AFTER="${PROT_AFTER_METHOD:-не доказана}"
fi
PROBES_AFTER="$(probe_lines)"
XID_AFTER="$(xid_count "$START_TS")"
NEW_XID="$(( XID_AFTER - XID_BEFORE ))"
[ "$NEW_XID" -lt 0 ] && NEW_XID=0
DEV_AFTER_OK="$(json_get "$DEV_AFTER" ok)"

# Вердикт — по **изменению**, а не по состоянию: устройство, мёртвое до прогона,
# не становится «отказом во время прогона» только потому, что после прогона оно
# всё ещё мертво. Ложное обвинение здесь стоило бы доверия ко всей расписке.
# И обратное: если устройство объявлено непроверяемым (skip), вердикт считает
# нагрузка, а расписка несёт предупреждение — приписать отказу нечего.
if [ "$NEW_XID" -gt 0 ] || { [ "$DEV_BEFORE_OK" = "True" ] && [ "$DEV_AFTER_OK" != "True" ]; }; then
  VERDICT="device_faulted"
elif [ "$DEV_BEFORE_CHECKED" != "True" ]; then
  if [ "$RC" -ne 0 ]; then VERDICT="payload_failed"; else VERDICT="ok"; fi
elif [ "$DEV_BEFORE_OK" != "True" ] && [ "$DEV_AFTER_OK" != "True" ]; then
  VERDICT="device_unusable_no_evidence"
elif [ "$RC" -ne 0 ]; then
  VERDICT="payload_failed"
else
  VERDICT="ok"
fi

write_receipt "$VERDICT" "$(verdict_exit "$VERDICT")" "$DEV_BEFORE" "$DEV_AFTER" \
              "$XID_BEFORE" "$XID_AFTER" "$PROBES_BEFORE" "$PROBES_AFTER" "$RC" "$ELAPSED"

echo "расписка: $OUT (verdict=$VERDICT, новых Xid: $NEW_XID, проб дошло: $PROBES_AFTER)"
if [ "$VERDICT" = "device_faulted" ]; then
  echo "КРАСНЫЙ КОНТУР: устройство отказало во время прогона." >&2
  echo "Новые строки Xid:" >&2
  xid_lines "$START_TS" | sed 's/^/  /' >&2
fi
exit "$(verdict_exit "$VERDICT")"
