#!/usr/bin/env python3
"""Страж идентичности набора: объявленное против фактически прочитанного.

**Класс дефекта.** Манифест прогона (AD-2) отвечает на вопрос «на чём учились».
Пока стадия берёт вход **литералом в коде**, а объявление живёт **отдельным полем
манифеста**, эти два ответа расходятся молча — и прогон, формально имеющий
манифест, описывает **другую сущность**. Факт S3av: `pilot_chain.sh` несла
`--sft_data …/sft_train_v12.jsonl` строкой, `--dataset` (куда передали v13_fixed)
попадала только в `dataset_sha256`, и стадия училась на v12 (44 949 сэмплов), тогда
как манифест называл v13_fixed (44 105). Это ровно то, от чего защищает AD-2, и
ровно то, чего не видит C-012: манифест **есть** и он **конформный** — он просто
про другое.

**Что проверяется.** По каждому каталогу прогона сверяются две записи, сделанные
независимо:

* **объявление** — путь и ПОЛНЫЙ sha256 набора, названные ДО чтения;
* **факт** — блок идентичности манифеста **стадии** (`checkpoints/run_manifest.json`),
  который пишет сам пайплайн: путь фактически открытого jsonl (и тензора), их полные
  sha256 и число примеров (`tools/patch_pipeline_sft.py`, S3av).

**Носителей объявления два, и порядок чтения — часть правила** (S3be). Первичный —
блок идентичности того же манифеста стадии: `sft_input.declared_path` /
`declared_sha256` (`rl_input` — для набора RL). Он введён разбором S3av и лежит
рядом с фактом **другой записью**: объявление приходит параметром стадии
(`--sft_data`), факт берётся у загрузчика. Исторический — поля манифеста прогона
(AD-2): `datasets_extra` семейства `sft*`/`rl*` и
`hyperparameters.sft_dataset` (+ `_sha256`, `_samples`); им писали прогоны до S3av,
и он **не выбрасывается**: история не переписывается (ADR-023 п.9).

**Третье состояние, ради которого носитель назван явно.** Пока прибор искал
объявление только в старых полях, он краснел на прогоне, который объявляет набор
**корректно**, но в новом носителе: `sft-v13-2e6-20260921-2054` писал
`sft_input.declared_*` и не писал `datasets_extra`/`hyperparameters` — и получал
`no-declaration` при живом, сходящемся объявлении. Это не «объявления нет», а
«объявление в другом носителе»: ложное красное на правильно объявленном прогоне,
ровно тот класс, против которого ADR-023 п.12. Отсюда порядок: `sft_input`/`rl_input`
если есть → иначе старые поля → иначе названное состояние «объявления нет»
(и только тогда `no-declaration`).

Красное — если объявленный путь/хеш/число примеров не сходятся с фактическими, если
хеш записан **не целиком** (`hash_scope != "full"`: `_sha256_head` — первые 1 МиБ —
класс слепоты AD-2/G9, усечённый хеш не отличает подменённый набор от объявленного),
если в первичном носителе нет объявленного хеша (сверять нечем — «объявлено» без
числа не объявление), если тензор не выводится из объявленного jsonl, или если
объявления нет ни в одном носителе.

**Почему сравнение записей, а не пересчёт файлов.** Файлы наборов — сотни мегабайт
и живут на сетевом диске; гейт, читающий их на каждом прогоне, платил бы минутами
за то, что уже измерено стадией. Записи же сделаны **разными** участниками
(цепочка объявляет, пайплайн читает) — поэтому расхождение между ними и есть
предмет проверки. Пересчёт с диска — отдельный, явный режим `--verify-disk`
(сверяет записанный хеш с файлом), он же ловит правку манифеста «под факт».

**История не переписывается** (ADR-023 п.9, ADR-028 п.4). Прогоны до `--since`
(по умолчанию — дата, с которой правило действует) печатаются как
`legacy-not-checked` со своими именами: молчаливого исключения нет, но и находки
по ним не выставляются — иначе повторяется класс «тест, красный by construction»
(ADR-023 п.12). Их расхождение фиксируется отдельным артефактом, а не правкой.

Коды возврата::

    0 — все прогоны после --since предъявили сходящуюся идентичность (или таких нет)
    1 — нарушение: поимённый список в stdout
    2 — NOT-VERIFIED: нет входа (`--runs` — файл), либо прогон объявил исполненную
        стадию sft, а её манифест недостижим (с `--unreachable-not-verified` —
        печатается как пропуск, гейт не краснеет: профиль CI)

Запуск::

    python3 tools/check_dataset_identity.py                  # гейт кейса
    python3 tools/check_dataset_identity.py --json           # машинный вердикт
    python3 tools/check_dataset_identity.py --verify-disk    # + сверка с файлами
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

MANIFEST_NAME = "run_manifest.json"
NOT_A_RUN_MARKER = "NOT_A_RUN"
#: Каталог манифеста стадии внутри каталога прогона (`--ckpt_dir` цепочки).
STAGE_MANIFEST_REL = "checkpoints/run_manifest.json"

#: Имена набора SFT в объявлении. Семейство, а не одно имя: контур уже писал
#: `sft`, `sft_train`, `sft_corpus`, `sft_tok_cache` — правило, знающее одно имя,
#: пропускало бы объявления под остальными.
SFT_EXTRA_RE = re.compile(r"^sft(?:[_-].*)?$")
#: Стадии, при которых стадия SFT **исполнялась** (объявление о ней — обязательство
#: предъявить факт). `pending`/`skipped` факта не обещают.
SFT_EXECUTED_STATUS = ("done", "running", "partial", "failed", "stopped")

#: Предметы правила: набор SFT (объявление в носителе S3av) и набор RL-курикулума
#: (ADR-054 п.1 — тем же принципом, тем же механизмом, не параллельным: блок
#: `rl_input` манифеста стадии и его же `declared_*`).
#:
#: * `block`  — имя блока идентичности в манифесте стадии;
#: * `stage`  — имя стадии в манифесте прогона (`stages[].name`);
#: * `extra_re`— семейство имён набора в `datasets_extra` (исторический носитель);
#: * `hp_field`— поле старого носителя в `hyperparameters` (`datasets_extra` старше
#:               его и оба читаются: у пилота объявление шло через `sft_dataset`);
#: * `parts`  — обязательные части блока: у RL-набора тензора нет (загрузчик
#:               `RLDataset` читает jsonl напрямую), и требовать его значило бы
#:               красное по построению (ADR-023 п.12);
#: * `marker` — признак того, что копия пайплайна стадии УМЕЕТ писать блок.
IDENTITY_KINDS = (
    {"block": "sft_input", "stage": "sft", "label": "SFT",
     "extra_re": SFT_EXTRA_RE, "hp_field": "sft_dataset", "parts": ("jsonl", "tensor"),
     "marker": "_sft_record_input_identity"},
    {"block": "rl_input", "stage": "rl", "label": "RL-курикулума",
     "extra_re": re.compile(r"^rl(?:[_-].*)?$"), "hp_field": "rl_dataset",
     "parts": ("jsonl",), "marker": "_rl_record_input_identity"},
)
SFT_KIND, RL_KIND = IDENTITY_KINDS

FULL_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 1 << 22


class Finding:
    def __init__(self, run: str, rule: str, message: str, severity: str = "error"):
        self.run, self.rule, self.message, self.severity = run, rule, message, severity

    def as_dict(self) -> dict:
        return {"run": self.run, "rule": self.rule, "severity": self.severity,
                "message": self.message}


def sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def read_json(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return d if isinstance(d, dict) else None


def run_date(manifest: dict, run_dir: Path) -> date | None:
    """Дата прогона: `created_at` манифеста, иначе время правки каталога.

    Берётся из манифеста, а не из имени каталога: имя — не доказательство, а
    манифест несёт измеренное время (ADR-028 п.2).
    """
    raw = str(manifest.get("created_at") or "")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        try:
            return date(*(int(x) for x in m.groups()))
        except ValueError:
            return None
    try:
        return date.fromtimestamp(run_dir.stat().st_mtime)
    except OSError:
        return None


def sft_declarations(manifest: dict, kind: dict = SFT_KIND) -> list[dict]:
    """Объявления набора из манифеста прогона (ИСТОРИЧЕСКИЙ носитель).

    Два места: `datasets_extra` (путь + полный sha) и `hyperparameters.<hp_field>`
    (+ `_sha256`, `_samples`). Источники называются поимённо — расхождение
    объявлений между собой это тоже находка, а не «одно из двух верно».

    Читается, когда первичного носителя (`sft_input`/`rl_input`) в манифесте стадии
    нет: им объявляли прогоны до S3av, и выбрасывать его значило бы переписывать
    историю (ADR-023 п.9).
    """
    out: list[dict] = []
    extras = manifest.get("datasets_extra")
    if isinstance(extras, dict):
        for name, spec in extras.items():
            if kind["extra_re"].match(str(name)) and isinstance(spec, dict):
                out.append({"source": f"datasets_extra.{name}",
                            "path": str(spec.get("path") or ""),
                            "sha256": (str(spec.get("sha256") or "").lower() or None),
                            "samples": None})
    hp = manifest.get("hyperparameters")
    field = kind["hp_field"]
    if isinstance(hp, dict) and hp.get(field):
        out.append({"source": f"hyperparameters.{field}",
                    "path": str(hp.get(field)),
                    "sha256": (str(hp.get(f"{field}_sha256") or "").lower() or None),
                    "samples": (int(hp[f"{field}_samples"])
                                if str(hp.get(f"{field}_samples", "")).lstrip("-").isdigit()
                                else None)})
    return out


def identity_block_declarations(name: str, block: dict, parts: tuple[str, ...],
                                findings: list[Finding]) -> list[dict]:
    """Объявление внутри блока идентичности (ПЕРВИЧНЫЙ носитель, S3av/S3be).

    Блок несёт две записи о наборе, сделанные разными участниками: `declared_*` —
    то, что стадии **объявили** параметром (`--sft_data`/`--rl_data` и его хеш);
    `<part>.path/sha256` — то, что стадия **прочитала**. Сверка их между собой и
    есть предмет правила; без неё блок был бы фактом без объявления, и расхождение
    «объявлен v13 — прочитан v12» осталось бы невидимым.

    Пустой список — объявления в носителе нет: тогда слово берёт исторический
    носитель, и только если и его нет, состояние называется «объявления нет».
    """
    d_path = str(block.get("declared_path") or "")
    raw_sha = str(block.get("declared_sha256") or "").strip().lower()
    if not d_path and not raw_sha:
        return []
    if not raw_sha:
        # «Объявлено» без хеша — не объявление: сверять объявленное с прочитанным
        # нечем, и зелёный здесь был бы молчанием вместо доказательства.
        findings.append(Finding(
            name, "declaration-incomplete",
            f"объявленный набор назван путём ({Path(d_path).name or d_path!r}), но его "
            f"полный sha256 не записан — сверять объявленное с прочитанным нечем"))
    elif not FULL_SHA_RE.match(raw_sha):
        findings.append(Finding(
            name, "declaration-hash-not-full",
            f"объявленный sha256 записан не целиком ({raw_sha[:16]}…, длина "
            f"{len(raw_sha)}) — усечённый хеш не отличает подменённый набор от "
            f"объявленного (класс слепоты AD-2/G9)"))
    #: Число примеров объявление не несёт (его пишет загрузчик, а не параметр
    #: стадии): `samples=None` здесь — не «не проверяем», а «в этом носителе числа
    #: нет», и сверка числа идёт по историческому носителю, если он есть.
    return [{"source": "объявление стадии (declared_*)", "path": d_path,
             "sha256": raw_sha or None, "samples": None}]


def pipeline_supports_identity(run_dir: Path, marker: str) -> bool | None:
    """Умеет ли копия пайплайна стадии писать блок идентичности (`marker`).

    Возвращает True/False, либо None — если копии в каталоге нет (тогда судить не по
    чему и решает дата). Проверка **по коду, а не по дате**: прогон, запущенный до
    введения правила, физически не мог оставить блок, которого в контуре не было, и
    требовать его — значит красить гейт по построению (ADR-023 п.12). Такой прогон
    называется поимённо как «старше правила», а не выдаётся за проверенный.
    """
    copies = sorted(p for p in run_dir.glob("*.py") if p.name.startswith("laguna_pipeline"))
    if not copies:
        return None
    for c in copies:
        try:
            if marker in c.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            return None
    return False


def stage_expected(manifest: dict, kind: dict = SFT_KIND) -> bool:
    """Объявил ли прогон исполненную стадию (значит, факт обязан быть предъявлен)."""
    stages = manifest.get("stages")
    if not isinstance(stages, list):
        return False
    for st in stages:
        if isinstance(st, dict) and st.get("name") == kind["stage"] \
                and str(st.get("status")) in SFT_EXECUTED_STATUS:
            return True
    return False


def check_identity_block(name: str, block: dict, kind: dict,
                         findings: list[Finding]) -> tuple[dict, dict]:
    """Полнота факта: пути, ПОЛНЫЕ хеши, число сэмплов, выводимость тензора из jsonl.

    Отделено от сверки с объявлением затем, что факт проверяется и там, где
    объявления нет (остановленный прогон: манифест стадии есть, корневого
    манифеста AD-2 нет) — иначе такой прогон пропускался бы молча.

    Обязательные части — из `kind["parts"]`: у набора RL тензора нет, и требовать
    его значило бы красное по построению (ADR-023 п.12).
    """
    blk = kind["block"]
    for key in kind["parts"]:
        part = block.get(key)
        if not isinstance(part, dict):
            findings.append(Finding(name, "path-missing", f"{blk}.{key}: раздела нет"))
            continue
        if not str(part.get("path") or ""):
            findings.append(Finding(
                name, "path-missing",
                f"{blk}.{key}: путь фактически загруженного файла не записан"))
        if not FULL_SHA_RE.match(str(part.get("sha256") or "").lower()):
            findings.append(Finding(
                name, "hash-missing",
                f"{blk}.{key}: полный sha256 не записан (получено {part.get('sha256')!r}) — "
                f"сверять объявленное с фактическим нечем"))
        if str(part.get("hash_scope") or "") != "full":
            findings.append(Finding(
                name, "hash-not-full",
                f"{blk}.{key}: hash_scope={part.get('hash_scope')!r}, ожидается 'full' "
                f"(усечённый хеш — класс слепоты AD-2/G9)"))
        samples = part.get("samples")
        if not isinstance(samples, int) or samples <= 0:
            findings.append(Finding(name, "samples-missing",
                                    f"{blk}.{key}: число сэмплов не записано ({samples!r})"))

    # Тензор обязан выводиться из объявленного jsonl (иначе читали другой набор).
    j_part = block.get("jsonl") if isinstance(block.get("jsonl"), dict) else {}
    t_part = block.get("tensor") if isinstance(block.get("tensor"), dict) else {}
    j_path, t_path = str(j_part.get("path") or ""), str(t_part.get("path") or "")
    if j_path and t_path:
        cands = [str(c) for c in (t_part.get("candidates") or [])]
        if not Path(t_path).name.startswith(Path(j_path).stem):
            findings.append(Finding(
                name, "tensor-not-from-jsonl",
                f"тензор {Path(t_path).name} не выведен из jsonl {Path(j_path).name} — "
                f"обучение и val читают разные наборы"))
        if cands and t_path not in cands:
            findings.append(Finding(
                name, "tensor-not-from-jsonl",
                f"загруженный тензор {t_path} не входит в список кандидатов набора"))
    return j_part, t_part


def compare_declared_to_actual(name: str, kind: dict, decls: list[dict], j_part: dict,
                               t_part: dict, findings: list[Finding]) -> None:
    """Объявление против факта: путь по имени файла, полный sha256, число примеров.

    Одна функция на оба носителя объявления: первичный (`declared_*` блока
    идентичности) и исторический (`datasets_extra`/`hyperparameters`). Разные
    реализации одного сравнения разошлись бы на первой правке.
    """
    blk = kind["block"]
    if not decls:
        findings.append(Finding(
            name, "no-declaration",
            f"набор {kind['label']} не объявлен ни в блоке {blk} манифеста стадии "
            f"(поля declared_path/declared_sha256), ни в манифесте прогона AD-2 "
            f"(datasets_extra семейства {kind['extra_re'].pattern}, "
            f"hyperparameters.{kind['hp_field']}) — сверять нечего"))
        return
    actual_sha = str(j_part.get("sha256") or "").lower()
    j_path = str(j_part.get("path") or "")
    actual_base = Path(j_path).name if j_path else ""
    for dec in decls:
        d_base = Path(dec["path"]).name
        if d_base and actual_base and d_base != actual_base:
            findings.append(Finding(
                name, "declared-actual-mismatch",
                f"{dec['source']} объявляет {d_base}, а прочитан {actual_base}"))
        if dec["sha256"] and actual_sha and dec["sha256"] != actual_sha:
            findings.append(Finding(
                name, "declared-actual-mismatch",
                f"{dec['source']} объявляет sha256={dec['sha256'][:12]}…, а прочитан "
                f"{actual_sha[:12]}… ({dec['path']})"))
        if dec["samples"] is not None and isinstance(j_part.get("samples"), int) \
                and dec["samples"] != j_part["samples"]:
            findings.append(Finding(
                name, "samples-mismatch",
                f"{dec['source']} объявляет {dec['samples']} примеров, а прочитано "
                f"{j_part['samples']}"))

    # Объявления между собой обязаны сходиться: два поля с одним смыслом — тот же
    # класс «одно имя, разные значения», что ADR-028 п.1.
    shas = {d["sha256"] for d in decls if d["sha256"]}
    bases = {Path(d["path"]).name for d in decls if d["path"]}
    if len(shas) > 1 or len(bases) > 1:
        findings.append(Finding(
            name, "declaration-conflict",
            f"объявления набора {kind['label']} противоречат друг другу: {sorted(bases)} / "
            f"{sorted(s[:12] + '…' for s in shas)}"))

    #: Объявленный прогоном тензор (`dataset_path`) сверяется отдельно, в
    #: `check_run`: поле живёт в манифесте прогона, а не в блоке идентичности.
    _ = t_part


def check_rl_identity(name: str, run_dir: Path, host: dict, stage: dict,
                      findings: list[Finding], notes: list[str]) -> None:
    """Набор RL-курикулума: тот же принцип, тот же носитель, не параллельный.

    ADR-054 п.1–2: курикулум стадии — `rl_tasks_revpool_v2.jsonl`, и «объявленное
    совпадает с прочитанным» проверяется **тем же механизмом**, что для SFT-набора:
    блок `rl_input` манифеста стадии (`declared_path`/`declared_sha256` против
    `jsonl.path`/`jsonl.sha256`) плюс исторический носитель
    `datasets_extra.rl*`/`hyperparameters.rl_dataset` в манифесте прогона.

    Границы (чтобы не красить гейт по построению, ADR-023 п.12): набор RL
    проверяется, только если блок `rl_input` есть ИЛИ стадия `rl` объявлена
    исполненной в прогоне, чья копия пайплайна умеет писать блок. Стадия RL ещё не
    шла — значит сегодня правило молчит, а не «красное за будущее».
    """
    block = stage.get("rl_input")
    if isinstance(block, dict):
        j_part, _ = check_identity_block(name, block, RL_KIND, findings)
        decls = identity_block_declarations(name, block, RL_KIND["parts"], findings)
        if not decls:
            decls = sft_declarations(host, RL_KIND)
        compare_declared_to_actual(name, RL_KIND, decls, j_part, {}, findings)
        return
    if not stage_expected(host, RL_KIND):
        notes.append(f"{name}: стадия RL не объявлена исполненной — набор курикулума "
                     f"не проверяется (блока rl_input нет)")
        return
    if pipeline_supports_identity(run_dir, RL_KIND["marker"]) is False:
        notes.append(f"{name}: копия пайплайна стадии не умеет писать rl_input — "
                     f"прогон старше правила, идентичность набора курикулума НЕ "
                     f"доказана (не проверено)")
        return
    findings.append(Finding(
        name, "no-input-identity",
        f"стадия RL исполнена, но манифест стадии не несёт блок rl_input (путь, "
        f"полный sha256 и число задач прочитанного набора курикулума) — идентичность "
        f"входа не предъявлена (ADR-054 п.2)"))


def check_run(run_dir: Path, since: date, shared: Path, verify_disk: bool,
              findings: list[Finding], notes: list[str]) -> str:
    """Проверить один каталог прогона. Возвращает статус для сводки."""
    name = run_dir.name
    if (run_dir / NOT_A_RUN_MARKER).exists():
        notes.append(f"{name}: не прогон (маркер {NOT_A_RUN_MARKER}) — пропущен")
        return "not-a-run"
    host = read_json(run_dir / MANIFEST_NAME)
    if host is None:
        # Объявления нет — но факт может быть (манифест стадии пишется раньше, и у
        # остановленного прогона корневого манифеста нет вовсе). Молча пропустить
        # такой прогон значило бы «молчание доказательством»: полнота блока
        # проверяется, а несделанная сверка называется явно.
        stage = read_json(run_dir / STAGE_MANIFEST_REL)
        blocks = [k for k in IDENTITY_KINDS
                  if isinstance(stage, dict) and isinstance(stage.get(k["block"]), dict)]
        if blocks:
            for k in blocks:
                check_identity_block(name, stage[k["block"]], k, findings)
                decls = identity_block_declarations(name, stage[k["block"]], k["parts"],
                                                    findings)
                j_part = stage[k["block"]].get("jsonl")
                compare_declared_to_actual(name, k, decls,
                                           j_part if isinstance(j_part, dict) else {},
                                           {}, findings)
            notes.append(f"{name}: блок(и) "
                         f"{', '.join(k['block'] for k in blocks)} объявление не "
                         f"сопровождает только манифест прогона AD-2 (это предмет C-012) — "
                         f"сверка по ПЕРВИЧНОМУ носителю (declared_* блока) выполнена, "
                         f"полнота факта проверена")
            return "checked"
        notes.append(f"{name}: манифеста AD-2 нет — не предмет этого правила (C-012)")
        return "no-manifest"
    d = run_date(host, run_dir)
    if d is not None and d < since:
        notes.append(f"{name}: прогон {d.isoformat()} до {since.isoformat()} — "
                     f"legacy-not-checked (история не переписывается, ADR-023 п.9)")
        return "legacy"
    if pipeline_supports_identity(run_dir, SFT_KIND["marker"]) is False \
            and stage_expected(host, SFT_KIND):
        # Старше правила по существу: копия пайплайна стадии не умеет писать sft_input.
        # Это НЕ «зелёный»: идентичность такого прогона остаётся недоказанной, и он
        # называется здесь поимённо, чтобы пропуск был видимым (ADR-023 п.12).
        notes.append(f"{name}: копия пайплайна стадии не умеет писать sft_input — "
                     f"прогон старше правила, идентичность НЕ доказана (не проверено)")
        return "legacy"

    stage_path = run_dir / STAGE_MANIFEST_REL
    stage = read_json(stage_path)
    if stage is None:
        if stage_expected(host, SFT_KIND):
            findings.append(Finding(
                name, "stage-manifest-unreachable",
                f"манифест стадии объявлен исполненным SFT, но не прочитан: {stage_path} — "
                f"факт входа недоступен (молчание доказательством не является)"))
        else:
            notes.append(f"{name}: стадия SFT не объявлена исполненной — проверять нечего")
        return "checked"

    block = stage.get("sft_input")
    if not isinstance(block, dict):
        if stage_expected(host, SFT_KIND):
            findings.append(Finding(
                name, "no-input-identity",
                f"стадия SFT исполнена, но манифест стадии не несёт блок sft_input "
                f"(путь, sha256 и число примеров прочитанного набора) — идентичность "
                f"входа не предъявлена"))
        else:
            notes.append(f"{name}: блока sft_input нет и стадия SFT не объявлена — "
                         f"проверять нечего")
        return "checked"

    # ── 1. Факт: полнота и редакция хешей ───────────────────────────────────
    j_part, t_part = check_identity_block(name, block, SFT_KIND, findings)

    # Редакция хешей остального lineage в том же манифесте стадии.
    if "datasets" in stage and stage.get("datasets_hash_scope") != "full":
        findings.append(Finding(
            name, "datasets-scope-not-full",
            f"манифест стадии несёт 'datasets' без datasets_hash_scope='full' — "
            f"редакция хешей не названа (усечённый хеш неотличим от полного)"))

    # ── 2. Объявление против факта (порядок носителей, S3be) ───────────────
    #: Первичный носитель — `declared_*` того же блока, что несёт факт. Он старше
    #: исторического и читается первым: прогон, объявивший набор в новом носителе,
    #: объявил его, и искать это объявление в старых полях — значит красить живой
    #: прогон в `no-declaration` (третье состояние, разобранное в шапке модуля).
    decls = identity_block_declarations(name, block, SFT_KIND["parts"], findings)
    if not decls:
        decls = sft_declarations(host, SFT_KIND)
    compare_declared_to_actual(name, SFT_KIND, decls, j_part, t_part, findings)

    # Объявленный прогоном тензор (--dataset) — против фактического. Проверяется
    # только если объявленный файл относится к семейству SFT: у пилота в этом поле
    # лежит тензор CPT-корпуса, и требовать от него совпадения с SFT-тензором
    # значило бы красное по построению.
    t_path = str(t_part.get("path") or "")
    ds_base = Path(str(host.get("dataset_path") or "")).name
    if ds_base.startswith("sft") and ds_base != Path(t_path).name:
        findings.append(Finding(
            name, "tensor-mismatch",
            f"прогон объявил тензор {ds_base}, а стадия прочитала {Path(t_path).name}"))

    # ── 2б. Набор RL-курикулума тем же принципом (ADR-054 п.1–2) ────────────
    check_rl_identity(name, run_dir, host, stage, findings, notes)

    # ── 3. Необязательный пересчёт с диска ──────────────────────────────────
    if verify_disk:
        for part, label in ((j_part, "jsonl"), (t_part, "tensor")):
            p = str(part.get("path") or "")
            recorded = str(part.get("sha256") or "").lower()
            if not p or not recorded:
                continue
            fp = Path(p)
            if not fp.is_file():
                rel = p
                if p.startswith("/workspace/shared/"):
                    rel = p[len("/workspace/shared/"):]
                fp = shared / rel
            got = sha256_file(fp) if fp.is_file() else None
            if got is None:
                notes.append(f"{name}: {label} {p} недоступен для пересчёта — "
                             f"записанный хеш не сверен с файлом")
            elif got != recorded:
                findings.append(Finding(
                    name, "disk-sha-mismatch",
                    f"{label}: записан sha256={recorded[:12]}…, а на диске {got[:12]}… "
                    f"({fp})"))
    return "checked"


def main() -> int:
    ap = argparse.ArgumentParser(description="Страж идентичности набора (AD-2, S3av)")
    ap.add_argument("--runs", default="runs/", help="каталог каталогов прогонов кейса")
    ap.add_argument("--stand-runs", default="/home/user/gb10-shared",
                    help="каталог прогонов стенда (дополнительный источник)")
    ap.add_argument("--since", default="2026-09-21",
                    help="дата, с которой правило действует (прогоны раньше — legacy)")
    ap.add_argument("--shared", default="/home/user/gb10-shared",
                    help="корень сетевого диска для --verify-disk")
    ap.add_argument("--verify-disk", action="store_true",
                    help="дополнительно пересчитать полный sha256 файлов с диска")
    ap.add_argument("--unreachable-not-verified", action="store_true",
                    help="профиль CI: недостижимый манифест стадии — пропуск, не красное")
    ap.add_argument("--json", action="store_true", help="машинный вердикт")
    a = ap.parse_args()

    try:
        since = date.fromisoformat(a.since)
    except ValueError:
        print(f"--since не дата: {a.since}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    runs = Path(a.runs)
    if not runs.exists():
        # Вакуумная истина: прогонов нет — правилу нечего нарушать (и это напечатано).
        out = {"tool": "check_dataset_identity", "passed": True, "runs_checked": 0,
               "findings": [], "notes": [f"нет каталога {runs} — прогонов нет"],
               "summary": "прогонов нет: проверять нечего"}
        print(json.dumps(out, ensure_ascii=False, indent=2) if a.json
              else out["summary"])
        return EXIT_OK
    if not runs.is_dir():
        print(f"NOT-VERIFIED: --runs не каталог: {runs}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    findings: list[Finding] = []
    notes: list[str] = []
    checked = legacy = 0
    for run_dir in sorted(p for p in runs.iterdir() if p.is_dir()):
        status = check_run(run_dir, since, Path(a.shared), a.verify_disk, findings, notes)
        checked += status == "checked"
        legacy += status == "legacy"

    stand = Path(a.stand_runs)
    if stand.is_dir():
        for run_dir in sorted(p for p in stand.iterdir()
                              if p.is_dir() and "sft" in p.name):
            # Берутся и прогоны без корневого манифеста: у остановленной стадии
            # манифест стадии есть, а корневого (пишется по завершении) нет —
            # пропустить её значило бы пропустить ровно тот случай, ради которого
            # правило и заведено.
            if (run_dir / MANIFEST_NAME).is_file() \
                    or (run_dir / STAGE_MANIFEST_REL).is_file():
                status = check_run(run_dir, since, Path(a.shared), a.verify_disk,
                                   findings, notes)
                checked += status == "checked"
                legacy += status == "legacy"
    else:
        notes.append(f"каталог стенда {stand} недостижим — его прогоны не проверены")

    unreachable = [f for f in findings if f.rule == "stage-manifest-unreachable"]
    if a.unreachable_not_verified and unreachable:
        notes.extend(f"пропущено (профиль CI): {f.run} — {f.message}" for f in unreachable)
        findings = [f for f in findings if f.rule != "stage-manifest-unreachable"]

    passed = not findings
    out = {
        "tool": "check_dataset_identity",
        "passed": passed,
        "since": since.isoformat(),
        "verify_disk": a.verify_disk,
        "runs_checked": checked,
        "runs_legacy": legacy,
        "findings": [f.as_dict() for f in findings],
        "notes": notes,
        "summary": ("идентичность набора сходится: объявленное и прочитанное совпадают"
                    if passed else
                    f"нарушений: {len(findings)} (красное при расхождении объявленного "
                    f"и фактического)"),
    }
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for n in notes:
            print(f"  · {n}")
        for f in findings:
            print(f"  [{f.severity}] {f.run}: {f.rule} — {f.message}")
        print(out["summary"])
    if findings:
        return EXIT_FAIL
    if unreachable and not a.unreachable_not_verified:
        return EXIT_NOT_VERIFIED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
