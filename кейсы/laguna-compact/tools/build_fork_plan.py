#!/usr/bin/env python3
"""build_fork_plan.py — планировщик серии форк-семантики сида (Н-7, ADR-057).

Предмет: собрать **план серии** — структуру «базовые стадии модели ОДИН раз + k RL-рук»
(`ADR-056` п.2: `CPT+SFT+eval + k × RL`, k по умолчанию 3) — и, по явному флагу, каталоги
рук. Стенд не нужен: ни `ssh`, ни запуска обучения здесь нет и быть не может
(спека `docs/specs/LADDER-FORK-CARRIER.md` §0).

Что пишется в дерево (при `--write`)::

    <series-dir>/fork_plan.json                # машиночитаемый план серии
    <series-dir>/<model>-base/                 # базовые стадии модели (один раз)
        chain_command.txt  stages.tsv  checkpoints/  logs/
    <series-dir>/<model>-rl-s<seed>/           # RL-рука, k штук
        chain_command.txt  stages.tsv  checkpoints/  logs/

Границы носителей (названы, чтобы два носителя не разошлись):

* **здесь** — план серии: какие каталоги есть, какой вход у них общий, какая строка записана
  на запуск, чем подтверждается идентичность;
* `tools/pilot_chain.sh` — **последовательность стадий** внутри каталога (шаги, стоп-условия,
  страж AD-9 перед каждой стадией, манифест AD-2). Планировщик его не правит и не исполняет;
* `tools/fork_series_chain.sh` — **исполнение серии**: читает `chain_command.txt` единицы серии
  из её каталога и запускает её.

Инварианты, за которые отвечает этот файл (спека §1):

* **I1** — форк виден структурой: один каталог базовых стадий на модель + `k` каталогов рук;
* **I2** — общий вход: все `k` рук объявляют один и тот же SFT-чекпойнт (путь + sha256)
  **полем** (`shared_sft` у каждой руки и `shared_input` на серии); рука без объявленного
  входа — отказ (код 2);
* **I3** — ни один артефакт не перезаписывается между руками: коллизия полных путей
  (две единицы в одном каталоге) — отказ с поимённым конфликтом;
* **I5** — страж AD-9 назван в записанной строке каждой единицы (обход стража ради
  серийности запрещён);
* **I6** — единица исполняет записанное: план несёт `chain_command` и его
  `chain_command_sha256`, драйвер сверяет запись с планом;
* **I8** — план до старта: `--print-plan` печатает состав серии и стенда не требует.

Две фазы (названы, потому что иначе план был бы неисполним). Серия собирается в порядке
«база → руки»: пока базовые стадии не посчитаны, SFT-чекпойнта ещё нет, значит его sha256
**неизвестен**, а рука обязана объявить вход (I2) и записать его в свою строку (I6). Отсюда
`--units base` (план только базовых стадий) и `--units arms` (руки — после того, как вход
измерен). `--units all` (по умолчанию) требует уже существующего/объявленного входа и потому
годится для повторного планирования рук готовой модели.

Идемпотентность (`--write`): повторный запуск с теми же входами **не переписывает**
`fork_plan.json` (сравнение байт; `created_at` переносится из существующего плана, поэтому
побайтовое равенство достижимо); расхождение без `--force` — отказ (код 2) с перечислением
разошедшихся полей. Прецедент — `tools/write_run_manifest.py`.

Что планировщик **не** делает: не изобретает числа рецепта (шаги, батчи) — они берутся из
`stages.tsv` пилота (`--recipe`); не проверяет наличие наборов данных и весов (объявляет их
путями; значения по умолчанию — умолчания `tools/pilot_chain.sh` и они печатаются, а не молчат).

Коды возврата::

    0 — план собран (при `--print-plan` — напечатан) или `--check-plan` подтвердил план
    2 — NOT-VERIFIED: отказ (нет объявленного SFT-входа, коллизия артефактов, неизвестная
        модель, k < 1, каталог серии вне общего каталога, расхождение с существующим планом
        без --force, нечитаемый план)

Запуск (примеры)::

    # сухой прогон: план на экран; стенда не касается. Каталог серии — внутри --shared:
    # иначе контейнерный путь единицы не выводится, а команда без него неисполнима
    python3 tools/build_fork_plan.py --model-short qwen25-05b \
        --series-dir /tmp/fork-plan-dry --shared /tmp --shared-ctr /workspace/shared

    # сборка серии: база (пока SFT-чекпойнта нет), затем руки (вход уже измерен)
    python3 tools/build_fork_plan.py --model-short qwen25-05b --seeds 42,1337,2024 \
        --series-dir /home/user/gb10-shared/ladder/<ts>-qwen25-05b --units base --write
    python3 tools/build_fork_plan.py --model-short qwen25-05b --seeds 42,1337,2024 \
        --series-dir /home/user/gb10-shared/ladder/<ts>-qwen25-05b --units arms --write --force

    # проверка плана перед серией (её же вызывает драйвер)
    python3 tools/build_fork_plan.py --check-plan <series-dir>/fork_plan.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent

#: Реестр моделей лесенки — short → HF id, семейство и тег претокен-кэша.
#: Источник состава: `docs/specs/LADDER-FULL-PLAN.md` §1.2 (short/HF id/семейство) и §1.2а
#: (токенизатор на семейство один → тег кэша). Здесь только имена; наличие весов — предмет
#: замера (`tools/inventory_stand_weights.py`), планировщик его не подменяет.
MODELS: dict[str, dict[str, str]] = {
    "qwen25-05b": {"hf_id": "Qwen/Qwen2.5-0.5B", "family": "Qwen2.5", "tok_tag": "qwen25"},
    "qwen25-15b": {"hf_id": "Qwen/Qwen2.5-1.5B", "family": "Qwen2.5", "tok_tag": "qwen25"},
    "qwen25-3b": {"hf_id": "Qwen/Qwen2.5-3B", "family": "Qwen2.5", "tok_tag": "qwen25"},
    "qwen25-7b": {"hf_id": "Qwen/Qwen2.5-7B", "family": "Qwen2.5", "tok_tag": "qwen25"},
    "qwen3-06b": {"hf_id": "Qwen/Qwen3-0.6B", "family": "Qwen3", "tok_tag": "qwen3"},
    "qwen3-17b": {"hf_id": "Qwen/Qwen3-1.7B", "family": "Qwen3", "tok_tag": "qwen3"},
    "qwen3-4b": {"hf_id": "Qwen/Qwen3-4B", "family": "Qwen3", "tok_tag": "qwen3"},
    "qwen3-8b": {"hf_id": "Qwen/Qwen3-8B", "family": "Qwen3", "tok_tag": "qwen3"},
    "qwen35-08b": {"hf_id": "Qwen/Qwen3.5-0.8B-Base", "family": "Qwen3.5", "tok_tag": "qwen35"},
    "qwen35-2b": {"hf_id": "Qwen/Qwen3.5-2B-Base", "family": "Qwen3.5", "tok_tag": "qwen35"},
    "qwen35-4b": {"hf_id": "Qwen/Qwen3.5-4B-Base", "family": "Qwen3.5", "tok_tag": "qwen35"},
    "qwen35-9b": {"hf_id": "Qwen/Qwen3.5-9B-Base", "family": "Qwen3.5", "tok_tag": "qwen35"},
}

#: Рецепт по умолчанию — `stages.tsv` пилота ревизии (8 полей, S3b). Числа шагов и батчей
#: берутся отсюда: у рецепта один носитель, а не пересказ.
DEFAULT_RECIPE = "runs/pilot-compact-s42-20260914-1626/stages.tsv"

#: k по умолчанию (ADR-056 п.2, наследует R3 14.09.2026) и сиды по умолчанию.
DEFAULT_K = 3
DEFAULT_SEEDS = "42,1337,2024"

#: Стадии базового каталога модели: CPT → SFT → eval(base) → eval(sft-база) — один раз на модель.
BASE_STAGE_NAMES = ("cpt", "sft", "eval_base", "eval_sft")
#: Стадии каталога RL-руки: RL и его собственный eval.
ARM_STAGE_NAMES = ("rl", "eval_rl")

#: Инструменты, которые называет записанная строка. Путь + sha256 в плане: серия исполняет
#: ровно эти носители, а драйвер сверяет хеш записанной строки с планом (I6).
CHAIN_TOOL_KEYS = ("chain", "guard", "manifest_tool", "sampler")


class Refusal(Exception):
    """Отказ планировщика: код 2, NOT-VERIFIED. Причина обязана быть названа."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha12_of(runner_name: str) -> str:
    """sha12 носителя серии — из файла, если он есть в дереве кейса (иначе пусто, а не выдумка)."""
    path = Path(runner_name)
    if not path.is_absolute():
        path = CASE_ROOT / runner_name
    if not path.is_file():
        return ""
    return sha256_file(path)[:12]


def parse_seeds(raw: str) -> list[int]:
    seeds: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            seeds.append(int(part))
        except ValueError:
            raise Refusal(f"сид не целое число: {part!r} (--seeds 42,1337,2024)")
    if not seeds:
        raise Refusal("--seeds пуст: серия без сидов не определена")
    if len(set(seeds)) != len(seeds):
        dup = sorted({s for s in seeds if seeds.count(s) > 1})
        raise Refusal(
            "I3: сид повторяется "
            + ", ".join(str(s) for s in dup)
            + " — две руки получили бы один каталог и одни пути артефактов (коллизия вместо сравнения)"
        )
    return seeds


def read_recipe(path: Path) -> dict[str, list[str]]:
    """stages.tsv рецепта: имя стадии → 8 полей. Носитель чисел, а не пересказ."""
    if not path.is_file():
        raise Refusal(f"нет файла рецепта стадий: {path} (--recipe)")
    rows: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 8:
            raise Refusal(
                f"рецепт {path}: строка стадии {fields[0]!r} несёт {len(fields)} полей, "
                "ожидается 8 (name, pstage, gstage, artifact, steps, batch, eckpt, exp)"
            )
        rows[fields[0]] = fields
    missing = [n for n in BASE_STAGE_NAMES + ARM_STAGE_NAMES if n not in rows]
    if missing:
        raise Refusal(
            f"рецепт {path}: нет стадий {', '.join(missing)} — форк-серия собирается из базовых "
            "стадий (cpt, sft, eval_base, eval_sft) и стадий руки (rl, eval_rl)"
        )
    return rows


def recipe_rows(rows: dict[str, list[str]], names: tuple[str, ...], exp_base: str) -> list[dict]:
    out = []
    for name in names:
        f = rows[name]
        out.append(
            {
                "name": name,
                "pstage": f[1],
                "gstage": f[2],
                "artifact": f[3],
                "steps": int(f[4]),
                "batch": int(f[5]) if f[5].lstrip("-").isdigit() else f[5],
                "eval_ckpt": f[6],
                "exp": f"{exp_base}-{name}",
            }
        )
    return out


def stages_tsv_text(stages: list[dict]) -> str:
    """8 полей, как в носителе рецепта (`runs/pilot-compact-s42-20260914-1626/stages.tsv`)."""
    lines = []
    for st in stages:
        lines.append(
            "\t".join(
                str(x)
                for x in (
                    st["name"],
                    st["pstage"],
                    st["gstage"],
                    st["artifact"],
                    st["steps"],
                    st["batch"],
                    st["eval_ckpt"],
                    st["exp"],
                )
            )
        )
    return "\n".join(lines) + "\n"


def ctr_path(host_path: Path, shared: Path, ctr_shared: str) -> str | None:
    """Хостовый путь → контейнерный той же подстановкой, что и монтирование `-v shared:ctr_shared`."""
    try:
        rel = host_path.resolve().relative_to(shared.resolve())
    except ValueError:
        return None
    return f"{ctr_shared.rstrip('/')}/{rel}"


def build_command(
    *,
    run_dir: Path,
    ctr_run_dir: str,
    stages_file: Path,
    exp_base: str,
    ctr_prefix: str,
    params: dict,
    seed: int,
    extra_hyperparams: list[str],
) -> str:
    """Записанная строка запуска единицы серии (I6: исполняется записанное, а не помнимое)."""
    parts = [
        "bash",
        str(params["chain"]),
        "--run-dir",
        str(run_dir),
        "--ctr-run-dir",
        ctr_run_dir,
        "--stages-file",
        str(stages_file),
        "--exp-base",
        exp_base,
        "--ctr-prefix",
        ctr_prefix,
        "--shared",
        str(params["shared"]),
        "--experiments",
        str(params["experiments"]),
        "--pipeline",
        str(params["pipeline"]),
        "--pipeline-ctr",
        params["pipeline_ctr"],
        "--cpt-data",
        params["cpt_data"],
        "--dataset",
        params["dataset"],
        "--sft-data",
        params["sft_data"],
        "--rl-data",
        params["rl_data"],
        "--model",
        params["hf_id"],
        "--seed",
        str(seed),
        "--guard",
        str(params["guard"]),
        "--manifest-tool",
        str(params["manifest_tool"]),
        "--safe-start",
        str(params["safe_start"]),
        "--storm-gap",
        str(params["storm_gap"]),
        "--sampler",
        str(params["sampler"]),
        "--image",
        params["image"],
        "--glm-env",
        str(params["glm_env"]),
        "--runner-name",
        params["runner_name"],
    ]
    if params.get("runner_sha12"):
        parts += ["--runner-sha12", params["runner_sha12"]]
    if params.get("sft_data_sha256"):
        parts += ["--sft-data-sha256", params["sft_data_sha256"]]
    if params.get("rl_data_sha256"):
        parts += ["--rl-data-sha256", params["rl_data_sha256"]]
    if params.get("arch_base"):
        parts += ["--arch-base", params["arch_base"]]
    for flag in params.get("extra_flags", []):
        parts += [flag]
    for kv in extra_hyperparams:
        parts += ["--extra-hyperparam", kv]
    return " ".join(parts)


def build_plan(args, seeds: list[int], recipe_path: Path, rows: dict[str, list[str]]) -> dict:
    """Собрать план серии как словарь (он же — содержимое fork_plan.json)."""
    series = Path(args.series_dir)
    series = series.resolve() if not series.is_absolute() else series
    previous = read_existing(series)
    shared = Path(args.shared)
    ctr_shared = args.shared_ctr
    ctr_series = args.ctr_series_dir or ctr_path(series, shared, ctr_shared)
    if not ctr_series:
        raise Refusal(
            f"каталог серии {series} вне общего каталога {shared}: контейнерный путь единицы "
            "серии не выводится, а без него записанная строка неисполнима (I6). Держите серию "
            "внутри --shared или объявите путь явно (--ctr-series-dir)"
        )

    notes: list[str] = []
    if any(m != "qwen25-05b" for m in args.models):
        notes.append(
            "рецепт шагов взят с прогона 0,5B и для другого семейства не калиброван "
            "(LADDER-FORK-CARRIER.md §4): числа шагов и батчи — из рецепта, а не из замера "
            "этого семейства"
        )
    if args.units == "base":
        notes.append(
            "план несёт только базовые стадии (units=base): руки планируются после того, как "
            "SFT-чекпойнт базы появится и будет измерен (units=arms)"
        )

    params = {
        "shared": shared,
        "ctr_shared": ctr_shared,
        "experiments": Path(args.experiments),
        "chain": Path(args.chain),
        "guard": Path(args.guard),
        "manifest_tool": Path(args.manifest_tool),
        "sampler": Path(args.sampler),
        "safe_start": Path(args.safe_start),
        "storm_gap": Path(args.storm_gap),
        "glm_env": Path(args.glm_env),
        "image": args.image,
        "pipeline": Path(args.pipeline),
        "pipeline_ctr": args.pipeline_ctr,
        "runner_name": args.runner_name,
        "runner_sha12": sha12_of(args.runner_name),
        "cpt_data": args.cpt_data,
        "dataset_tmpl": args.dataset_npy,
        "sft_data": args.sft_data,
        "sft_data_sha256": args.sft_data_sha256,
        "rl_data": args.rl_data,
        "rl_data_sha256": args.rl_data_sha256,
        "arch_base": args.arch_base,
        "extra_flags": args.extra_flags,
    }

    #: ── общий вход рук КАЖДОЙ модели (I2) ───────────────────────────────────
    #: Форк считает CPT+SFT один раз **на модель**, поэтому «общий» вход общий для рук
    #: одной модели. У серии из двух моделей входов два — и это не побочность, а предмет:
    #: руки модели A сравниваются со SFT-базой модели A (ADR-055 п.1).
    shared_inputs: list[dict] = []
    for short in args.models:
        if args.sft_checkpoint:
            declared_path = Path(args.sft_checkpoint)
            if not declared_path.is_absolute():
                declared_path = series / declared_path
            declared_by = "--sft-checkpoint (объявление серии)"
        else:
            declared_path = series / f"{short}-base" / "checkpoints" / "sft_checkpoint_final.pt"
            declared_by = "умолчание: SFT-чекпойнт базового каталога модели"
        measured_at = None
        if args.sft_sha256:
            sha, sha_state = args.sft_sha256.strip().lower(), "declared"
        elif declared_path.is_file():
            sha, sha_state, measured_at = sha256_file(declared_path), "measured", now_iso()
        else:
            sha, sha_state = None, "unmeasured"
        #: Замер переносится из прежнего плана, если файл тот же: иначе повторная сборка тех же
        #: входов всегда расходилась бы по времени замера и «идемпотентность» стала бы отказом.
        for old in previous.get("shared_inputs") or []:
            if (
                measured_at
                and old.get("model") == short
                and old.get("sha256") == sha
                and old.get("measured_at")
            ):
                measured_at = old["measured_at"]
        shared_inputs.append(
            {
                "model": short,
                "path": str(declared_path),
                "sha256": sha,
                "sha256_state": sha_state,
                "measured_at": measured_at,
                "declared_by": declared_by,
                "base_artifact": f"{short}-base/checkpoints/sft_checkpoint_final.pt",
                "role": "вход RL-рук модели: SFT-чекпойнт её базовых стадий (ADR-056 п.2)",
                "consumed_as": "checkpoints/sft_checkpoint_final.pt",
            }
        )
    if len(shared_inputs) > 1 and args.sft_checkpoint:
        notes.append(
            "объявленный --sft-checkpoint применён ко всем моделям серии: форк-семантика "
            "предполагает свой SFT-чекпойнт у каждой модели — проверьте объявление"
        )
    shared_input = shared_inputs[0] if len(shared_inputs) == 1 else None

    plan_models = []
    for short in args.models:
        info = MODELS[short]
        base_name = f"{short}-base"
        base_dir = series / base_name
        dataset = str(params["dataset_tmpl"]).format(
                shared=params["shared"], tok_tag=info["tok_tag"]
            )
        p = dict(params, hf_id=info["hf_id"], dataset=dataset)
        stages = recipe_rows(rows, BASE_STAGE_NAMES, base_name)
        cmd = build_command(
            run_dir=base_dir,
            ctr_run_dir=f"{ctr_series.rstrip('/')}/{base_name}",
            stages_file=base_dir / "stages.tsv",
            exp_base=base_name,
            ctr_prefix=f"{args.ctr_prefix}-{base_name}",
            params=p,
            seed=seeds[0],
            extra_hyperparams=[],
        )
        plan_models.append(
            {
                "short": short,
                "hf_id": info["hf_id"],
                "family": info["family"],
                "tok_tag": info["tok_tag"],
                #: Сид базовых стадий — первый объявленный: CPT и SFT считаются один раз
                #: на модель, поэтому у базы сид один, и он назван, а не угадан.
                "base_seed": seeds[0],
                "base_dir": base_name,
                "base_exp_base": base_name,
                "base_ctr_prefix": f"{args.ctr_prefix}-{base_name}",
                "ctr_dir": f"{ctr_series.rstrip('/')}/{base_name}",
                "stages": stages,
                "expected_artifacts": [f"{base_name}/{st['artifact']}" for st in stages],
                "chain_command": cmd,
                "chain_command_sha256": sha256_text(cmd + chr(10)),
            }
        )

    arms = []
    if args.units != "base":
        unmeasured = [si["model"] for si in shared_inputs if not si["sha256"]]
        if unmeasured and not args.write:
            #: Сухой прогон показывает структуру серии и ДОЛЖЕН назвать, чего не хватает
            #: для записи (I2), а не молчать и не выдумывать число.
            notes.append(
                "общий SFT-вход не объявлен у модели "
                + ", ".join(unmeasured)
                + " (ни файла, ни --sft-sha256): план печатается, чтобы был виден состав "
                "серии; --write такой план не соберёт, серия рук на неизмеренном входе не "
                "стартует (I2)"
            )
        if unmeasured and args.write:
            si = next(x for x in shared_inputs if not x["sha256"])
            raise Refusal(
                "I2: нет объявленного SFT-входа — все k рук обязаны объявить один и тот же "
                f"SFT-чекпойнт (путь + sha256), а у модели {si['model']} он не объявлен: файла "
                f"{si['path']} нет и --sft-sha256 не задан. Порядок: сначала базовые стадии "
                "(--units base), затем руки (--units arms) — серия не стартует на неизмеренном входе"
            )
        si_by_model = {si["model"]: si for si in shared_inputs}
        for short in args.models:
            info = MODELS[short]
            base_name = f"{short}-base"
            si = si_by_model[short]
            dataset = str(params["dataset_tmpl"]).format(
                shared=params["shared"], tok_tag=info["tok_tag"]
            )
            p = dict(params, hf_id=info["hf_id"], dataset=dataset)
            for seed in seeds:
                name = f"{short}-rl-s{seed}"
                arm_dir = series / name
                stages = recipe_rows(rows, ARM_STAGE_NAMES, name)
                cmd = build_command(
                    run_dir=arm_dir,
                    ctr_run_dir=f"{ctr_series.rstrip('/')}/{name}",
                    stages_file=arm_dir / "stages.tsv",
                    exp_base=name,
                    ctr_prefix=f"{args.ctr_prefix}-{name}",
                    params=p,
                    seed=seed,
                    extra_hyperparams=(
                        [f"sft_shared_checkpoint={si['path']}"]
                        + ([f"sft_shared_checkpoint_sha256={si['sha256']}"] if si["sha256"] else [])
                        + list(args.extra_hyperparams)
                    ),
                )
                arms.append(
                    {
                        "model": short,
                        "seed": seed,
                        "name": name,
                        "dir": name,
                        "ctr_dir": f"{ctr_series.rstrip('/')}/{name}",
                        "exp_base": name,
                        "ctr_prefix": f"{args.ctr_prefix}-{name}",
                        "stages": stages,
                        "expected_artifacts": [f"{name}/{st['artifact']}" for st in stages],
                        "shared_sft": {
                            "model": short,
                            "path": si["path"],
                            "sha256": si["sha256"],
                            "sha256_state": si["sha256_state"],
                            "materialized_as": "checkpoints/sft_checkpoint_final.pt",
                            "link_target": f"../../{base_name}/checkpoints/sft_checkpoint_final.pt",
                        },
                        "chain_command": cmd,
                        "chain_command_sha256": sha256_text(cmd + chr(10)),
                    }
                )

    tools = []
    for key in CHAIN_TOOL_KEYS:
        tool = params.get(key)
        if tool is None:
            continue
        tool_path = Path(tool)
        tools.append(
            {
                "role": key,
                "path": str(tool_path),
                "sha256": sha256_file(tool_path) if tool_path.is_file() else None,
            }
        )

    if args.units == "base" and not arms:
        notes.append(
            "общий вход объявлен путём; sha256 появится при планировании рук (units=arms) — "
            "до измерения входа серия рук не стартует (I2)"
        )

    return {
        "schema": "fork-plan/1",
        "created_at": (previous.get("created_at") or now_iso()),
        "units": args.units,
        "no_stand": True,
        "series_dir": str(series),
        "ctr_series_dir": ctr_series,
        "shared": str(shared),
        "ctr_shared": ctr_shared,
        "k": len(seeds),
        "seeds": seeds,
        #: §2.1 спеки знает поле `model` (одна модель); полный состав всегда лежит в `models[]`.
        "model": plan_models[0]["hf_id"] if len(plan_models) == 1 else None,
        "models": plan_models,
        #: Стадии как записи рецепта (без имён прогонов): по ним видно, что именно считается
        #: один раз на модель и что множит сид (I1), ещё до чтения каталогов.
        "base_stages": stage_records(rows, BASE_STAGE_NAMES),
        "arm_stages": stage_records(rows, ARM_STAGE_NAMES),
        "shared_input": shared_input,
        "shared_inputs": shared_inputs,
        "arms": arms,
        "recipe": {
            "path": str(recipe_path),
            "sha256": sha256_file(recipe_path),
            "source": "stages.tsv пилота ревизии (8 полей) — носитель чисел рецепта",
            "base_rows": {
                st["name"]: {"steps": st["steps"], "batch": st["batch"]}
                for st in recipe_rows(rows, BASE_STAGE_NAMES, "base")
            },
            "arm_rows": {
                st["name"]: {"steps": st["steps"], "batch": st["batch"]}
                for st in recipe_rows(rows, ARM_STAGE_NAMES, "arm")
            },
        },
        "chain_tools": tools,
        "params": {
            "image": args.image,
            "pipeline": str(params["pipeline"]),
            "pipeline_ctr": args.pipeline_ctr,
            "glm_env": str(params["glm_env"]),
            "safe_start": str(params["safe_start"]),
            "storm_gap": str(params["storm_gap"]),
            "sampler": str(params["sampler"]),
            "runner_name": args.runner_name,
            "runner_sha12": params["runner_sha12"] or None,
            "experiments": str(params["experiments"]),
            "cpt_data": args.cpt_data,
            "dataset_npy_template": args.dataset_npy,
            "sft_data": args.sft_data,
            "sft_data_sha256": args.sft_data_sha256 or None,
            "rl_data": args.rl_data,
            "rl_data_sha256": args.rl_data_sha256 or None,
            "extra_flags": list(args.extra_flags),
        },
        "notes": notes,
    }


def stage_records(rows: dict[str, list[str]], names: tuple[str, ...]) -> list[dict]:
    """Записи стадий рецепта без имён прогонов — «что именно считается» в плане."""
    return [
        {k: v for k, v in st.items() if k != "exp"}
        for st in recipe_rows(rows, names, "plan")
    ]


def read_existing(series: Path) -> dict:
    """Существующий план серии — чтобы повторная сборка тех же входов была байт-в-байт.

    Из него переносятся `created_at` и `measured_at` общего входа: иначе «тот же вход»
    никогда не дал бы тех же байт, и идемпотентность выродилась бы в отказ по времени.
    """
    plan_file = series / "fork_plan.json"
    if not plan_file.is_file():
        return {}
    try:
        data = json.loads(plan_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def validate_units(plan: dict) -> list[tuple[str, str, list[str]]]:
    """Единицы серии: (метка, каталог, ожидаемые артефакты). Метка уникальна и поимённа."""
    raw: list[tuple[str, str, list[str]]] = []
    for i, model in enumerate(plan.get("models") or []):
        name = model.get("base_dir") or model.get("short") or f"#модель{i}"
        raw.append((f"база {name}", name, list(model.get("expected_artifacts") or [])))
    for i, arm in enumerate(plan.get("arms") or []):
        name = arm.get("dir") or arm.get("name") or f"#рука{i}"
        raw.append(
            (f"рука {name} (сид {arm.get('seed')})", name, list(arm.get("expected_artifacts") or []))
        )
    counts: dict[str, int] = {}
    for _, name, _ in raw:
        counts[name] = counts.get(name, 0) + 1
    seen: dict[str, int] = {}
    out: list[tuple[str, str, list[str]]] = []
    for label, name, artifacts in raw:
        seen[name] = seen.get(name, 0) + 1
        tag = f"{label} [{seen[name]}]" if counts[name] > 1 else label
        out.append((tag, name, artifacts))
    return out


def validate_plan(plan: dict, *, require_input: bool = True) -> list[str]:
    """Проверки, которые обязан пройти любой исполнимый план (I1, I2, I3, I5, I6).

    `require_input=False` — режим печати сухого прогона: структура проверяется,
    а отсутствие объявленного общего входа называется примечанием, а не отказом
    (запись и драйвер требуют его наличия).
    """
    problems: list[str] = []
    if not isinstance(plan, dict):
        return ["план не объект JSON"]

    k = plan.get("k")
    seeds = plan.get("seeds")
    if not isinstance(k, int) or k < 1:
        problems.append(f"I1: k={k!r} — число рук обязано быть ≥ 1")
    if not isinstance(seeds, list) or len(seeds) != k:
        problems.append(f"I1: seeds={seeds!r} не согласован с k={k!r}")
    if not plan.get("series_dir"):
        problems.append("нет поля series_dir — каталог серии не назван")
    if not plan.get("ctr_series_dir"):
        problems.append(
            "нет контейнерного пути серии — записанная строка неисполнима (I6)"
        )
    if plan.get("no_stand") is not True:
        problems.append("нет no_stand: true — планировщик стенда не требует и обязан это объявить")

    arms = plan.get("arms") or []
    if not isinstance(arms, list):
        problems.append("arms: не список")
        arms = []
    #: ── I2: у каждой модели свой общий вход, руки модели объявляют ЕГО ───────
    inputs = plan.get("shared_inputs")
    if not isinstance(inputs, list) or not inputs:
        problems.append("I2: нет shared_inputs — общий SFT-вход рук не назван ни у одной модели")
        inputs = []
    by_model: dict[str, dict] = {}
    for si in inputs:
        model = si.get("model")
        if model in by_model:
            problems.append(f"I2: модель {model} объявлена дважды — какой вход у её рук, неясно")
        if not si.get("path"):
            problems.append(f"I2: модель {model}: общий SFT-вход не называет путь")
        if arms and require_input and not si.get("sha256"):
            problems.append(
                f"I2: модель {model}: общий SFT-вход без sha256 — объявление неполно "
                "(путь + sha256), а серия рук запускается только на измеренном входе"
            )
        by_model[model] = si
    if len(inputs) == 1:
        if plan.get("shared_input") != inputs[0]:
            problems.append("I2: поле shared_input разошлось с shared_inputs[0] (два носителя одного входа)")
    elif plan.get("shared_input") is not None:
        problems.append("I2: при нескольких моделях поле shared_input обязано быть null (входы — в shared_inputs)")

    if arms:
        if len(arms) != k * len(plan.get("models") or []):
            problems.append(
                f"I1: рук {len(arms)}, а ожидается k × моделей = "
                f"{k} × {len(plan.get('models') or [])}"
            )
    elif plan.get("units") != "base":
        problems.append(
            f"I1: план без рук обязан называть себя план-базы (units=base), а не {plan.get('units')!r}"
        )

    declared_by_model: dict[str, str] = {}
    for arm in arms:
        name = arm.get("name") or arm.get("dir") or "?"
        sf = arm.get("shared_sft") or {}
        si = by_model.get(sf.get("model") or arm.get("model"))
        if not sf.get("path") or (not sf.get("sha256") and require_input):
            problems.append(f"I2: рука {name}: нет объявленного SFT-входа (путь + sha256)")
        elif si is None:
            problems.append(f"I2: рука {name}: модель {sf.get('model')!r} не объявлена в shared_inputs")
        else:
            if sf.get("path") != si.get("path"):
                problems.append(
                    f"I2: рука {name}: путь входа {sf.get('path')} не совпадает с входом её "
                    f"модели {si.get('path')} — руки одной модели обязаны читать ОДИН чекпойнт"
                )
            if si.get("sha256") and sf.get("sha256") != si.get("sha256"):
                problems.append(
                    f"I2: рука {name}: объявленный SFT-вход {str(sf.get('sha256'))[:12]}… "
                    f"не совпадает с общим входом модели {str(si.get('sha256'))[:12]}…"
                )
            if arm.get("model") and sf.get("model") != arm.get("model"):
                problems.append(f"I2: рука {name}: модель руки и модель её входа расходятся")
        prev = declared_by_model.get(str(sf.get("model")))
        if prev is not None and prev != sf.get("sha256"):
            problems.append(
                f"I2: руки одной модели объявили РАЗНЫЕ входы ({str(prev)[:12]}… и "
                f"{str(sf.get('sha256'))[:12]}…) — форк сломан: сид множит RL, а не CPT/SFT"
            )
        declared_by_model[str(sf.get("model"))] = sf.get("sha256")
        check_unit(name, arm, problems)
        if not arm.get("expected_artifacts"):
            problems.append(f"I1: рука {name}: не объявлены ожидаемые артефакты")

    models = plan.get("models") or []
    if not models:
        problems.append("models: пусто — базовые стадии не названы")
    for model in models:
        name = model.get("base_dir") or model.get("short") or "?"
        check_unit(name, model, problems)
        if not model.get("expected_artifacts"):
            problems.append(f"I1: база {name}: не объявлены ожидаемые артефакты")

    #: ── I3: ни один артефакт не перезаписывается между единицами серии ──────
    seen_paths: dict[str, str] = {}
    for label, name, artifacts in validate_units(plan):
        for rel in artifacts:
            if rel in seen_paths and seen_paths[rel] != label:
                problems.append(
                    f"I3: коллизия артефактов: {seen_paths[rel]} и {label} пишут один путь "
                    f"{rel} — руки перезапишут друг друга"
                )
            else:
                seen_paths[rel] = label
    seen_dirs: dict[str, str] = {}
    for label, name, _ in validate_units(plan):
        if name in seen_dirs and seen_dirs[name] != label:
            problems.append(f"I3: единицы {seen_dirs[name]} и {label} делят один каталог {name}")
        else:
            seen_dirs[name] = label
    return problems


def check_unit(name: str, unit: dict, problems: list[str]) -> None:
    """I5/I6 у единицы серии: записанная строка есть, её хеш сходится, страж AD-9 назван."""
    cmd = unit.get("chain_command") or ""
    if not cmd:
        problems.append(f"I6: {name}: нет записанной команды (chain_command)")
        return
    #: Хеш считается по БАЙТАМ записи (`chain_command.txt` = строка + перевод строки):
    #: драйвер сверяет именно байты файла (`sha256sum`), и расхождение хотя бы в переводе
    #: строки обязано быть видно, а не «потому что почти совпало».
    if sha256_text(cmd + "\n") != unit.get("chain_command_sha256"):
        problems.append(f"I6: {name}: хеш записанной команды не совпадает с планом")
    if "check_resource_owner.sh" not in cmd:
        problems.append(
            f"I5: {name}: страж AD-9 не назван в записанной команде — обход стража ради "
            "серийности запрещён (ADR-057 «Решение» п.4)"
        )


def print_plan(plan: dict) -> None:
    print(f"план серии форк-семантики сида (Н-7): units={plan['units']}, стенд не нужен")
    print(f"  каталог серии: {plan['series_dir']}")
    print(f"  контейнер:     {plan['ctr_series_dir']}")
    print(f"  k = {plan['k']} (сиды: {', '.join(str(s) for s in plan['seeds'])})")
    print(f"  моделей: {len(plan['models'])}")
    for model in plan["models"]:
        stages = " → ".join(st["name"] for st in model["stages"])
        print(
            f"  · {model['short']} ({model['hf_id']}, семейство {model['family']}): "
            f"базовые стадии {stages}, каталог {model['base_dir']}, сид базы {model['base_seed']}"
        )
        print(f"      ожидаемые артефакты: {', '.join(model['expected_artifacts'])}")
    for si in plan["shared_inputs"]:
        sha = si["sha256"] or "НЕ ОБЪЯВЛЕН (серию рук с --write собрать нельзя)"
        print(f"  общий SFT-вход рук {si['model']}: {si['path']} (sha256: {sha}, {si['sha256_state']})")
    print(f"  рук: {len(plan['arms'])}")
    for arm in plan["arms"]:
        stages = " → ".join(st["name"] for st in arm["stages"])
        print(
            f"  · рука {arm['name']}: сид {arm['seed']}, каталог {arm['dir']}, стадии {stages}"
        )
        print(f"      ожидаемые артефакты: {', '.join(arm['expected_artifacts'])}")
        print(f"      вход: {arm['shared_sft']['path']} → {arm['shared_sft']['materialized_as']}")
    print(f"  рецепт шагов: {plan['recipe']['path']} (sha256 {plan['recipe']['sha256'][:12]}…)")
    print(f"  инструменты цепочки: {', '.join(t['path'] for t in plan['chain_tools'])}")
    print("  записанные строки запуска (исполняется записанное, а не помнимое):")
    for model in plan["models"]:
        print(f"    [база {model['base_dir']}] {model['chain_command']}")
    for arm in plan["arms"]:
        print(f"    [рука {arm['name']}] {arm['chain_command']}")
    if plan["units"] == "base":
        print("  руки не планируются (--units base): после базовых стадий — --units arms --write")
    for note in plan.get("notes") or []:
        print(f"  примечание: {note}")


def write_series(plan: dict, *, force: bool) -> list[str]:
    """Создать каталоги и файлы серии. Возвращает список сообщений о том, что произошло."""
    series = Path(plan["series_dir"])
    messages: list[str] = []
    plan_file = series / "fork_plan.json"
    existing = plan_file.read_text(encoding="utf-8") if plan_file.is_file() else None
    new_text = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"

    if existing is not None and not force and existing != new_text:
        raise Refusal(
            "план в каталоге серии расходится с собранным (сравнение байт) — пересборка только "
            "осознанно (--force). Разошлось: " + "; ".join(diff_plans(existing, plan))
        )

    units: list[tuple[str, dict]] = [(m["base_dir"], m) for m in plan["models"]]
    units += [(a["dir"], a) for a in plan["arms"]]
    for name, unit in units:
        unit_dir = series / name
        (unit_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
        (unit_dir / "logs").mkdir(parents=True, exist_ok=True)
        for fname, content in (
            ("stages.tsv", stages_tsv_text(unit["stages"])),
            ("chain_command.txt", unit["chain_command"] + "\n"),
        ):
            f = unit_dir / fname
            if f.is_file():
                if f.read_text(encoding="utf-8") == content:
                    continue
                if not force:
                    raise Refusal(
                        f"{name}/{fname} уже существует и расходится с планом — перезапись "
                        "только с --force (иначе серия исполнит не то, что записано)"
                    )
            f.write_text(content, encoding="utf-8")
            messages.append(f"записан {f}")

    if existing == new_text:
        messages.append(f"план уже собран и не переписан: {plan_file}")
        return messages
    plan_file.write_text(new_text, encoding="utf-8")
    messages.append(f"записан план серии: {plan_file}")
    return messages


def diff_plans(existing_text: str, plan: dict) -> list[str]:
    try:
        existing = json.loads(existing_text)
    except json.JSONDecodeError:
        return ["существующий fork_plan.json не разбирается как JSON"]
    diff = [key for key in sorted(set(existing) | set(plan)) if existing.get(key) != plan.get(key)]
    return diff or ["содержимое отличается, но поля совпали (форматирование)"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Планировщик серии форк-семантики сида (Н-7): база + k RL-рук",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--model-short", action="append", dest="models", default=None,
                   help=f"краткое имя модели лесенки (повторяемый): {', '.join(sorted(MODELS))}")
    p.add_argument("--seeds", default=DEFAULT_SEEDS,
                   help=f"сиды через запятую (по умолчанию {DEFAULT_SEEDS})")
    p.add_argument("--k", type=int, default=None,
                   help=f"число RL-рук; по умолчанию — число сидов (у сидов по умолчанию "
                        f"{DEFAULT_SEEDS} это {DEFAULT_K}, ADR-056 п.2); заданный --k обязан "
                        "совпасть с числом сидов")
    p.add_argument("--series-dir", default=None, help="каталог серии (обязателен)")
    p.add_argument("--shared", default="/home/user/gb10-shared", help="корень общих данных (хостовый)")
    p.add_argument("--shared-ctr", default="/workspace/shared", help="тот же корень в контейнере")
    p.add_argument("--ctr-series-dir", default=None,
                   help="контейнерный путь каталога серии (по умолчанию выводится из --shared)")
    p.add_argument("--sft-checkpoint", default=None,
                   help="объявленный общий SFT-чекпойнт (вход всех рук); по умолчанию — "
                        "<series>/<model>-base/checkpoints/sft_checkpoint_final.pt")
    p.add_argument("--sft-sha256", default=None,
                   help="объявленный sha256 общего входа (когда файла ещё нет — объявление вместо замера)")
    p.add_argument("--recipe", default=str(CASE_ROOT / DEFAULT_RECIPE),
                   help="stages.tsv пилота — носитель чисел рецепта")
    p.add_argument("--units", choices=("base", "arms", "all"), default="all",
                   help="что планировать: base (до появления SFT-чекпойнта), arms|all (после)")
    p.add_argument("--image", default="nvcr.io/nvidia/pytorch:26.07-py3-vllm")
    p.add_argument("--pipeline", default=str(CASE_ROOT / "laguna_pipeline_v8.py"),
                   help="пайплайн стадий (хостовый путь)")
    p.add_argument("--pipeline-ctr", default="/workspace/shared/laguna_pipeline_v8.py")
    p.add_argument("--experiments", default="/home/user/experiments",
                   help="каталог экспериментов (хостовый)")
    p.add_argument("--chain", default=str(CASE_ROOT / "tools" / "pilot_chain.sh"),
                   help="носитель стадий (цепочка кейса)")
    p.add_argument("--guard", default=str(CASE_ROOT / "tools" / "check_resource_owner.sh"),
                   help="страж AD-9, вызываемый цепочкой перед каждой стадией")
    p.add_argument("--manifest-tool", default=str(CASE_ROOT / "tools" / "write_run_manifest.py"))
    p.add_argument("--sampler", default=str(CASE_ROOT / "tools" / "smoke_mem_sampler.sh"))
    p.add_argument("--safe-start", default="/home/user/gb10-shared/nvrm-storm/safe_start.sh")
    p.add_argument("--storm-gap", default="/home/user/gb10-shared/nvrm-storm/storm_gap.sh")
    p.add_argument("--glm-env", default="/home/user/gb10-shared/.glm_env")
    p.add_argument("--runner-name", default="tools/fork_series_chain.sh",
                   help="чем исполняется серия (попадает в run_version манифеста AD-2)")
    p.add_argument("--cpt-data", default="/workspace/shared/datasets/cpt_corpus_v12r.txt")
    p.add_argument("--dataset-npy",
                   default="{shared}/datasets/tok/cpt_corpus_v12r_8192_{tok_tag}.npy",
                   help="претокенизированный CPT-набор для манифеста AD-2; {tok_tag} — тег семейства")
    p.add_argument("--sft-data", default="/workspace/shared/datasets/sft_train_v12.jsonl")
    p.add_argument("--sft-data-sha256", default="")
    p.add_argument("--rl-data", default="/workspace/shared/datasets/rl_tasks_revpool_v2.jsonl")
    p.add_argument("--rl-data-sha256", default="")
    p.add_argument("--arch-base", default="")
    p.add_argument("--extra-flag", action="append", dest="extra_flags", default=[],
                   help="дополнительный флаг цепочки (повторяемый): значения без умолчаний у "
                        "pilot_chain.sh объявляются явно, а не подразумеваются")
    p.add_argument("--extra-hyperparam", action="append", dest="extra_hyperparams", default=[],
                   help="--extra-hyperparam для манифеста AD-2 (повторяемый)")
    p.add_argument("--ctr-prefix", default="laguna-fork", help="префикс имён контейнеров стадий")
    p.add_argument("--print-plan", action="store_true", help="напечатать план (по умолчанию — только печать)")
    p.add_argument("--write", action="store_true", help="создать каталоги и файлы серии")
    p.add_argument("--check-plan", default=None,
                   help="проверить существующий fork_plan.json (I2/I3/I5/I6); драйвер вызывает это же")
    p.add_argument("--json", action="store_true", help="машинный план в stdout")
    p.add_argument("--force", action="store_true", help="перезаписать расходящийся план и файлы серии")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.check_plan:
            return check_plan(Path(args.check_plan))

        args.models = args.models or ["qwen25-05b"]
        dup = sorted({m for m in args.models if args.models.count(m) > 1})
        if dup:
            raise Refusal(
                f"I3: модель повторяется в --model-short ({', '.join(dup)}) — две базовые единицы "
                "получили бы один каталог и одни пути артефактов"
            )
        for short in args.models:
            if short not in MODELS:
                raise Refusal(f"неизвестная модель: {short!r}; известны: {', '.join(sorted(MODELS))}")
        seeds = parse_seeds(args.seeds)
        if args.k is None:
            args.k = len(seeds)
        elif args.k != len(seeds):
            raise Refusal(
                f"k={args.k} не согласован с числом сидов {len(seeds)} ({args.seeds}): у каждой руки "
                "свой сид — число рук задаётся составом сидов"
            )
        if args.k < 1:
            raise Refusal(f"k={args.k} — число рук обязано быть ≥ 1")
        if not args.series_dir:
            raise Refusal("--series-dir обязателен: каталогу серии нужен адрес")

        recipe_path = Path(args.recipe)
        rows = read_recipe(recipe_path)
        plan = build_plan(args, seeds, recipe_path, rows)
        problems = validate_plan(plan, require_input=args.write)
        if problems:
            for problem in problems:
                print(f"ОТКАЗ: {problem}", file=sys.stderr)
            print("NOT-VERIFIED: собранный план не прошёл проверку", file=sys.stderr)
            return 2

        if args.write:
            for message in write_series(plan, force=args.force):
                print(message)
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            print_plan(plan)
        return 0
    except Refusal as refusal:
        print(f"ОТКАЗ: {refusal}", file=sys.stderr)
        print("NOT-VERIFIED: план серии не собран", file=sys.stderr)
        return 2


def check_plan(path: Path) -> int:
    """Проверка существующего плана (её же вызывает драйвер перед серией)."""
    if not path.is_file():
        print(f"ОТКАЗ: нет плана: {path}", file=sys.stderr)
        print("NOT-VERIFIED: план серии не прочитан", file=sys.stderr)
        return 2
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ОТКАЗ: план {path} не разбирается как JSON: {exc}", file=sys.stderr)
        print("NOT-VERIFIED: план серии не прочитан", file=sys.stderr)
        return 2
    problems = validate_plan(plan)
    if problems:
        for problem in problems:
            print(f"ОТКАЗ: {problem}", file=sys.stderr)
        print(f"NOT-VERIFIED: план {path} не годен ({len(problems)} находок)", file=sys.stderr)
        return 2
    units = validate_units(plan)
    inputs = plan.get("shared_inputs") or []
    inputs_txt = ", ".join(
        f"{si.get('model')}={str(si.get('sha256'))[:12]}…" if si.get("sha256") else f"{si.get('model')}=не объявлен"
        for si in inputs
    )
    print(
        f"план годен: {path}; единиц {len(units)} "
        f"(баз {len(plan.get('models') or [])}, рук {len(plan.get('arms') or [])}), "
        f"k={plan.get('k')}, входы: {inputs_txt}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
