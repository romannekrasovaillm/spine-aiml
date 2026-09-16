#!/usr/bin/env python3
"""S3f / ADR-021 — пул ревизии v2: верифицируемая часть сред E1–E8 + env_types.

Вопрос дельты: почему среды `~/library/rl_envs/` лежат готовыми и не подключены.
Ответ (ADR-021): **формат** (задачи сред — `{task_id, prompt, gold_card}`, пайплайн
ждёт `task_type`/`prompt`/gold для **своего** верификатора), **веса** (у E1/E4/E6
`judge_weight` 0.4–0.7 — это судья в контуре награды, запрещено AD-6/C-013) и
**заброшенный реестр** (`task_count` 412/75 против факта 6568/1839).

Инструмент делает ровно конверсию и отбор; ничего в источниках не меняет
(AD-7 — заморозка; `--update-registry` — отдельный явный режим, см. ниже).

**Проекция gold → проверяемый ответ.** Верификатор пайплайна заморожен
(`laguna_pipeline_v8.py:verify_task`, читается живым прогоном — править нельзя),
поэтому задача среды обязана принять форму, которую он умеет проверять:

* **slug-ветка** (``find_concept``/``chain_reasoning``/``multi_hop_search``/
  ``common_neighbor``/``formula_chain``/``ood_link``/``ood_pair``) — в ответе
  обязаны появиться все строки ``expected_slugs`` (подстрочно, без учёта
  регистра; текст ``<tool_response>`` из ответа вырезается). Карточка концепта
  несёт `slug` — это и есть проекция: задача «про концепт X» проверяется тем,
  что модель назвала X.
* **keyword-ветка** (``explain_relation``) — проверяются **не** slug-и (они лежат
  в промпте, лазейка 08.08), а термины определений обоих концептов (по индексу
  концептов). Проекция E5: ``gold_relation.{from,to}`` → ``keywords``.

Задача, у которой `task_type` вне словаря верификатора, получает `False` на
**любой** ответ — это не «слабая» задача, а ложный ноль на всю среду. Поэтому
каждая выпускаемая задача прогоняется через ``verifier_accepts`` (симуляция
диспетчера верификатора), а словарь типов сверяется с исходником пайплайна в
тестах (`tools/tests/run_tool_tests.sh`) — дрейф ловится, а не предполагается.

**Цепочки: проверяется содержание концептов-условий, а не их имена (S3f-fix-2).**
Промпт цепочки по построению называет начало и конец («Начало: 'a' … Конец: 'c'»,
«Концепт A: 'a' … Концепт B: 'b'») — это **условие задачи**. Прежняя проекция
брала эти же концы проверяемым набором, и лейк-фильтр снял все 500 задач: проверка
сводилась к «воспроизведи условие». Проекция исправлена: проверяемый набор —
**термины определений** концептов, которые промпт обязывает найти («Шаг 1: Найди
определение 'a'», «Найди оба определения»); имена концептов остаются условием и в
проверку не входят. Промежуточный концепт (подсказка «например, 'b' или другой»)
не проверяется: промпт разрешает другой выбор, и требование именно генераторского
звена давало бы ложный ноль. Правило и границы — в
`PROJECTION_RULES["chain_definition_terms"]`.

**Фильтры** (каждый с поимённым отчётом, порядок применения фиксирован):

1. веса реестра: `verifiable_weight >= 0.8` **и** `judge_weight <= 0.2`
   (ADR-021 п.1/п.2; судья в награде запрещён AD-6/C-013);
2. проекция gold: нет однозначной проекции → среда/задача исключается
   (ложные нули хуже отсутствия задач);
3. лейк-правило: проверяемый ответ встречается в промпте в проверяемой форме →
   награда копируется из промпта, а не добывается решением (S3f-fix, ADR-021
   п.3 — тот же класс, что гейт A «slug-ответ в промпте» в
   `check_eval_leakage.py`; нормализация и проверка — общая функция
   `checked_in_prompt`). Правило не ослаблено и на цепочках: их проверяемый
   набор тоже не должен быть в промпте — см. ниже;
4. достижимость gold: slug есть в индексе концептов (иначе модель не может его
   получить), для keyword-ветки — определения обоих концептов непусты;
5. дубли внутри пула (нормализованный промпт; для E5 ещё и пара slug-ов);
6. пересечение с `sft_train_v12.jsonl`, `eval_ood_clean.jsonl` и действующим
   пулом `rl_tasks_revpool_v1.jsonl` (нормализация — `norm` из
   `check_eval_leakage.py`, та же, что у стражей C-009/C-017).

**Что инструмент НЕ делает:** не трогает корпус, SFT, eval, существующие пулы и
файлы сред; не переключает пайплайн на v2 (фаза 2, отдельная дельта); не пишет
в `~/library/rl_envs/` — кроме явного `--update-registry` (реестр `envs.yaml`,
с бэкапом `envs.yaml.bak_20260916`).

Коды возврата::

    0 — пул записан (или уже актуален с тем же содержимым)
    1 — ОТКАЗ: пул существует и отличается (нужен --force)
    2 — NOT-VERIFIED: вход отсутствует/нечитаем

Запуск::

    python3 tools/build_rev_envs.py                 # симлинки кейса, запись пула
    python3 tools/build_rev_envs.py --json          # машинный отчёт в stdout
    python3 tools/build_rev_envs.py --dry-run       # отбор без записи пула
    python3 tools/build_rev_envs.py --update-registry   # реестр сред по факту
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_eval_leakage import (  # noqa: E402
    NotVerified, checked_in_prompt, eval_prompt_text, norm, read_jsonl)
from check_sft_rl_overlap import EXIT_NOT_VERIFIED, EXIT_OK, scan_sft  # noqa: E402

EXIT_REFUSE = 1

# ── Замороженный верификатор пайплайна: словарь типов задач ───────────────────
# Источник: laguna_pipeline_v8.py::verify_task (диспетчер `if tt in (...)`),
# продублирован здесь и сверяется с исходником в тестах: пул, тип которого
# верификатор не знает, получает 0 на любой ответ — это ложный ноль на всю среду.
PIPELINE = "laguna_pipeline_v8.py"
SLUG_TASK_TYPES = ("find_concept", "chain_reasoning", "multi_hop_search",
                   "common_neighbor", "formula_chain", "ood_link", "ood_pair")
KEYWORD_TASK_TYPES = ("explain_relation",)
ANSWER_KEYWORD_TASK_TYPES = ("compare_concepts",)
VERIFIER_TASK_TYPES = SLUG_TASK_TYPES + KEYWORD_TASK_TYPES + ANSWER_KEYWORD_TASK_TYPES

#: Служебные значения keyword-ветки: отбрасываются до проверки определений
#: (`k.lower() not in ("related","связаны","связан")` в пайплайне).
KEYWORD_DROP = ("related", "связаны", "связан")

#: Минимальная длина проверяемой строки в slug-ветке. Верификатор сверяет
#: подстрокой (`s.lower() in resp_lower`), поэтому 1–3 символа («and», «dpo»)
#: совпадают со случайными словами ответа и мерой решения не являются.
MIN_TOKEN_LEN = 4

#: Порог ADR-021 п.1/п.2.
MIN_VERIFIABLE_WEIGHT = 0.8
MAX_JUDGE_WEIGHT = 0.2

# ── Цепочки: роли концептов в промпте (S3f-fix-2) ────────────────────────────
#: Типы задач-цепочек: промпт по построению называет начало и конец цепочки —
#: это **условие** задачи, а не проверяемый результат.
CHAIN_TASK_TYPES = ("chain_reasoning", "formula_chain")

#: Якоря промпта, из которых видны концепты-условия: (начало, конец). Это
#: единственный источник ролей — текст промпта; имена концептов остаются
#: условием и в проверяемый набор не входят.
CHAIN_ANCHORS = {
    "chain_reasoning": (r"Начало:\s*'([^']+)'", r"Конец:\s*'([^']+)'"),
    "formula_chain": (r"Концепт\s*A:\s*'([^']+)'", r"Концепт\s*B:\s*'([^']+)'"),
}

#: Якорь подсказки-примера промежуточного концепта. Роль «промежуточное» **не
#: проверяется**: промпт предлагает концепт примером и разрешает другой
#: («например, 'b' или другой»), поэтому требование именно этого звена было бы
#: ложным нулём на верном ответе. Якорь нужен только для провенанса и диагностики.
CHAIN_HINT_ANCHORS = {
    "chain_reasoning": r"например,\s*'([^']+)'",
    "formula_chain": None,
}

#: Сколько терминов определения берётся на каждый концепт-условие. Ровно столько
#: требует keyword-ветка замороженного верификатора (`min(3, |терминов|)` на
#: концепт, `verify_task`): проверяемый набор цепочки меряется той же мерой, что
#: уже принята в контуре для задач о терминах определений, — своей меры дельта
#: не вводит.
CHAIN_TERMS_PER_CONCEPT = 3

#: Формы лейка: в какой именно «проверяемой форме» ответ оказался в промпте.
LEAK_FORMS = {
    "slug_in_prompt": (
        "проверяемая строка встречается в промпте буквально — та же форма, "
        "что у гейта A стража C-009; ответ копируется из промпта, а не добывается "
        "(у цепочек проверяемые строки — термины определений концептов-условий)"
    ),
    "keyword_prompt_satisfies_verifier": (
        "термины определений обоих концептов набираются из самого промпта: "
        "дословный пересказ промпта проходит keyword-ветку верификатора "
        "(порог min(3, |терминов|) на каждый концепт)"
    ),
}

#: Формулировка правила — в карточку и в отчёт, дословно одна и та же.
LEAK_RULE = (
    "Проверяемый ответ не должен присутствовать в промпте в проверяемой форме "
    "(S3f-fix, ADR-021 п.3 — тот же принцип, что C-009 для eval-набора). "
    "«Проверяемая форма» — та, в которой строку потребляет замороженный "
    "верификатор: у slug-ветки это буквальное вхождение строки (гейт A стража "
    "C-009), у keyword-ветки — порог min(3, |терминов|) терминов определений "
    "обоих концептов, набранный из промпта. Правило применено к цепочкам без "
    "исключений: набор цепочки — термины определений концептов-условий "
    "(S3f-fix-2), и ни один из них не должен встречаться в промпте. "
    "Ролевого исключения у цепочек больше нет: оно существовало, пока "
    "проверялись ИМЕНА концептов (чтобы промежуточное значение не путалось с "
    "концом); у содержания определения роли в цепочке нет, и исключение "
    "оправдывало бы любое совпадение, то есть было бы дырой, а не правилом."
)

#: Замер до правки: пул 9197 задач (карточка v2 от 35f933d) нёс проверяемый ответ
#: в промпте у 500 задач slug-ветки (`checks.gold_in_prompt_audit` прежней
#: карточки) и у 6 задач keyword-ветки (замер тем же правилом до фильтра).
BASELINE_LEAK = {"slug_in_prompt": 500, "keyword_prompt_satisfies_verifier": 6}

#: Пулы до/после правки — чтобы «что осталось» читалось против «что было».
BASELINE_POOL = {"lines": 9197,
                 "sha256": "a5d6496aa3427c59a1ef2bcfcd50981af95d9e8fc81e64f3a95c02695dd9de0e"}

#: Пул **до этой дельты** (состояние после лейк-фильтра S3f-fix): в нём цепочек
#: нет ни одной — все 500 сняты правилом, потому что проекция проверяла концы.
#: Это база отката этой дельты: прежняя ревизия конвертера воспроизводит её ровно.
S3F_FIX_POOL = {
    "lines": 8692,
    "sha256": "78c502121dd912d193e2bb57303ab3ebf0eca33140676587b73c134b1f5eb2fd",
    "by_task_type_chains": {"chain_reasoning": 0, "formula_chain": 0},
    "chains_excluded": {"chain_reasoning": 300, "formula_chain": 200},
    "why": ("проекция брала концы цепочки проверяемым набором, а концы названы в "
            "промпте у 500 из 500 — лейк-фильтр снял тип целиком"),
}

#: Числа дельты S3f-fix — закреплены (историческая часть карточки): сколько задач
#: снял лейк-фильтр тогда и на сколько изменился пул. Проверяются откатом
#: (`ROLLBACK_CMD` → 9197), а не текущей сборкой, которую изменила S3f-fix-2.
S3F_FIX_DELTA = {
    "excluded_by_this_filter": 506,
    "net_pool_change": 505,
    "why_they_differ": (
        "На единицу: одна из шести keyword-задач E5 была одновременно дублем по "
        "gold (`duplicate_gold`) — фильтр дублей снял бы её и без лейк-правила, "
        "поэтому пул теряет на одну задачу меньше, чем снимает фильтр. Проверяемо: "
        "`by_reason.duplicate_gold` базовой ревизии 16, после правки — 15."),
}

#: Часть лейк-исключений, снятая **до** S3f-fix: slug-ветка E2/E3 базовой ревизии
#: пула v2. Нужна, чтобы `excluded.total` (608) не читался как «столько снял этот
#: фильтр»: 608 всего = 102 база + 506 дельта. Воспроизводится прежней ревизией
#: конвертера — тем же откатом, что и `BASELINE_POOL` (см. `ROLLBACK_CMD`).
BASELINE_LEAK_EXCLUSIONS = {"total": 102,
                            "by_env": {"E2_formula": 21, "E3_classify": 81}}

#: Команда отката/воспроизведения базы S3f-fix: прежняя ревизия конвертера из git
#: даёт ровно `BASELINE_POOL` (9197, sha256 a5d6496a…). Ревизия закреплена хешем,
#: а не `HEAD`: после этой дельты `HEAD` — уже другой конвертер. Путь внутри
#: репозитория берётся из `git rev-parse --show-prefix`, потому что `git show`
#: разрешает путь от корня репозитория, а команда запускается из каталога кейса.
#: Каталог артефактов — с префиксом дельты: общий `/tmp` делят параллельные дельты
#: (названный ADR-021 инфраструктурный риск).
#: Независимый замер лейка и проекции **тем же замороженным верификатором**, что
#: судит награду (`laguna_pipeline_v8.verify_task`), а не повтором правила:
#: `leak_form` — наше правило, `verify_task` — тот, кто судит. Три пробы, каждая —
#: ответ на вопрос приёмки:
#:
#: * `gold_accepted` — эталонный ответ задачи принимается;
#: * `prompt_copy_rewarded` — ответ = сам промпт награду получает (должно быть 0);
#: * `ends_only_rejected` — ответ только концами цепочки отвергается (значит
#:   проверяются действительно промежуточные значения, а не условие).
#:
#: Числа сняты пробой `tools/probe_chain_projection.py` 16.09.2026 на пуле,
#: собранном этой ревизией; артефакт — `evidence/s3f-fix-2-validation.json`.
#: Сборкой не пересчитываются: проба импортирует пайплайн (torch), конвертер — нет.
CHAIN_VALIDATION = {
    "tool": "tools/probe_chain_projection.py",
    "evidence": "evidence/s3f-fix-2-validation.json",
    "verifier": "laguna_pipeline_v8.verify_task (заморожен, читается живым импортом)",
    "command": ("python3 tools/probe_chain_projection.py --baseline-projection "
                "--pool runs/rev-pool-v2/rl_pool_filtered.jsonl"),
    "gold_accepted": {"checked": 470, "accepted": 470},
    # Критерий приёмки сформулирован про пул целиком, а не про цепочки: копия
    # промпта не должна награждаться нигде.
    "prompt_copy_rewarded_pool_wide": {"checked": 9162, "rewarded": 0},
    "prompt_copy_rewarded": {"checked": 470, "after_fix": 0,
                             "before_fix": {"checked": 500, "rewarded": 500}},
    "ends_only_rejected": {"checked": 470, "rejected": 470},
    "intermediate_not_checked": {"checked": 278, "rejected": 278},
    "note": ("`before_fix` — та же проба на проекции «концы» (состояние S3f-fix) по "
             "всем 500 цепочкам источника: копирование промпта награждалось у всех. "
             "`intermediate_not_checked` — ответ, называющий начало, конец и "
             "промежуточный концепт (имена без определений), тоже отвергается: "
             "проверяется содержание концептов-условий, а не имена звеньев; счёт — по "
             "задачам, где промпт называет промежуточный концепт (chain_reasoning), у "
             "`formula_chain` подсказки в формате нет. Числа пересчитываются пробой, а "
             "не сборкой: проба импортирует пайплайн (torch), конвертер — нет. "
             "`prompt_copy_rewarded_pool_wide` — тот же замер по всему пулу: "
             "критерий приёмки про пул, а не про один тип."),
}


def _rollback_cmd(rev: str, tag: str) -> str:
    tmp = f"/tmp/{tag}"
    return (
        f"P={tmp}; mkdir -p \"$P\"; PFX=$(git rev-parse --show-prefix); "
        f"git show {rev}:\"${{PFX}}tools/build_rev_envs.py\" > \"$P/build_rev_envs.py\"; "
        f"git show {rev}:\"${{PFX}}tools/check_eval_leakage.py\" > \"$P/check_eval_leakage.py\"; "
        f"ln -sf \"$PWD/tools/check_sft_rl_overlap.py\" \"$P/\"; "
        f"python3 \"$P/build_rev_envs.py\" --out \"$P/pool.jsonl\" --card \"$P/card.json\" "
        f"--report \"$P/report.json\" --link \"$P/link.jsonl\" --force"
    )


#: Откат этой дельты: прежняя (S3f-fix) ревизия конвертера → `S3F_FIX_POOL` (8692).
ROLLBACK_CMD_CHAIN = _rollback_cmd("ae692aa", "s3f-fix-2-rollback")
#: Откат лейк-фикса (исторический, для чисел раздела `leak_fix`) → 9197.
ROLLBACK_CMD = _rollback_cmd("35f933d", "s3f-fix-rollback")

#: Независимый замер лейка **тем же замороженным верификатором**, что судит награду:
#: ответ = сам промпт → сколько задач получает награду за копирование. Это прямая
#: проверка меры («копирование промпта = награда без обучения»), а не повтор
#: правила: `leak_form` — наше правило, `verify_task` — тот, кто судит.
#: Измерено на артефактах 16.09.2026; команда — `INDEPENDENT_CHECK_CMD`.
BASELINE_COPY_REWARDED = 505
AFTER_COPY_REWARDED = 0
INDEPENDENT_CHECK_CMD = (
    "CONCEPTS_INDEX=datasets/concepts_search_index.jsonl python3 -c \""
    "import json,sys; sys.path.insert(0,'.'); import laguna_pipeline_v8 as P; "
    "tasks=[json.loads(l) for l in open(sys.argv[1],encoding='utf-8') if l.strip()]; "
    "print('prompt_copied_gets_reward:', "
    "sum(1 for t in tasks if P.verify_task(t['prompt'], t)))\" <пул.jsonl>"
)

# ── Пути по умолчанию (симлинки кейса, AD-4) ─────────────────────────────────
DEFAULT_ENVS_ROOT = "~/library/rl_envs"
DEFAULT_ENV_TYPES = "datasets/rl_tasks_env_types.jsonl"
DEFAULT_SFT = "datasets/sft_train_v12.jsonl"
DEFAULT_EVAL = "datasets/eval_ood_clean.jsonl"
DEFAULT_V1 = "datasets/rl_tasks_revpool_v1.jsonl"
DEFAULT_INDEX = "datasets/concepts_search_index.jsonl"
DEFAULT_OUT = "datasets/rl_tasks_revpool_v2.jsonl"
DEFAULT_CARD = "data/rev-envs-v2-card.json"
DEFAULT_REPORT = "runs/rev-pool-v2/exclusions.json"
REGISTRY_NAME = "envs.yaml"
REGISTRY_BACKUP = "envs.yaml.bak_20260916"
NOT_A_RUN_MARKER = "NOT_A_RUN"

#: Куда пишется симлинк пула в дереве кейса (AD-4: данные — только на gb10-shared).
POOL_LINK = "runs/rev-pool-v2/rl_pool_filtered.jsonl"

# ── Среды: проекция gold → проверяемый ответ ─────────────────────────────────
# `task_type` — тип задачи в пуле (словарь верификатора), `projection` — правило
# проекции gold. Имена сред сохраняются в `source_env`, потому что один тип
# пула может нести несколько сред (find_concept ← E2_formula и E3_classify).
ENV_SPECS = (
    {"id": "E1_define", "tasks": "define.jsonl", "projection": None,
     "why": "judge_weight 0.5 > 0.2 — судья в контуре награды (AD-6/C-013, ADR-021 п.2)"},
    {"id": "E2_formula", "tasks": "formula.jsonl", "projection": "card_slug",
     "task_type": "find_concept", "verifier": "multi_slug_match"},
    {"id": "E3_classify", "tasks": "classify.jsonl", "projection": "card_slug",
     "task_type": "find_concept", "verifier": "multi_slug_match"},
    {"id": "E4_extract", "tasks": "extract.jsonl", "projection": None,
     "why": "judge_weight 0.4 > 0.2 — судья в контуре награды (AD-6/C-013, ADR-021 п.2)"},
    {"id": "E5_relate", "tasks": "relate.jsonl", "projection": "gold_relation",
     "task_type": "explain_relation", "verifier": "keyword_match"},
    {"id": "E6_contrast", "tasks": "contrast.jsonl", "projection": None,
     "why": "judge_weight 0.7 > 0.2 — судья в контуре награды (AD-6/C-013, ADR-021 п.2)"},
    {"id": "E7_repair", "tasks": "repair.jsonl", "projection": "unprojectable",
     "why": ("нет однозначной проекции gold: исправляемые значения присутствуют в "
             "тексте промпта (золотой type — в 97 из 100 карточек, раздел «## Тип "
             "концепции»; slug — в 100 из 100), проверка сводится к копированию из "
             "промпта, а собственный верификатор среды (verifiers/common.py::"
             "verify_repair разбирает frontmatter ОТВЕТА) пайплайном не вызывается")},
    {"id": "E8_plan_experiment", "tasks": "plan_experiment.jsonl", "projection": None,
     "why": "среда не заполнена: 0 задач (ADR-021 п.2)"},
)

#: Правила проекции — в карточку и в отчёт по каждой выпущенной задаче.
PROJECTION_RULES = {
    "card_slug": (
        "gold_card → slug карточки концепта → expected_slugs=[slug]; проверяет "
        "slug-ветка verify_task (все строки expected_slugs присутствуют в ответе). "
        "Мера — «модель назвала концепт, о котором задача»; содержание ответа "
        "(формула в E2, тип/уровень/формальность в E3) этой веткой не проверяется."
    ),
    "gold_relation": (
        "gold_relation.{from,to} → keywords=[from,to]; проверяет keyword-ветка "
        "verify_task (термины ОПРЕДЕЛЕНИЙ обоих концептов по индексу концептов, "
        ">= min(3, |терминов|) на каждый). Slug-и в промпте для этой ветки "
        "безразличны — она ими не пользуется."
    ),
    "unprojectable": (
        "проекция невозможна: проверяемое значение gold присутствует в промпте "
        "либо проверка требует верификатора среды, который пайплайн не вызывает"
    ),
    "chain_definition_terms": (
        "Цепочки (chain_reasoning/formula_chain): проверяемый набор — термины "
        "ОПРЕДЕЛЕНИЙ концептов-условий, а не их имена. Промпт цепочки называет "
        "начало и конец по построению задачи («Начало: …/Конец: …», «Концепт A: …/"
        "Концепт B: …»), поэтому имена концептов — условие, и проверка сводилась бы "
        "к копированию условия (S3f-fix снял по этой причине все 500 цепочек — "
        "правильно снял, но проекция была неверной). Проверяется то, что промпт "
        "обязывает найти: определения концептов-условий («Шаг 1: Найди определение "
        "'a'», «Найди оба определения»). Мера — не больше 3 значимых слов "
        "определения на каждый концепт (min(3, |терминов|) — ровно порог keyword-"
        "ветки замороженного верификатора), из которых вычтены слова имени самого "
        "концепта: имя промпт даёт даром, проверять его значило бы проверять "
        "условие. Промежуточный концепт (подсказка «например, 'b' или другой») не "
        "проверяется: промпт разрешает другой выбор, и требование именно "
        "генераторского звена давало бы ложный ноль на верном ответе. Роли якорей "
        "не распознаны → проекция невозможна → задача исключается с причиной, а не "
        "угадывается. Копирование промпта верификатор при этом не проходит: слова "
        "имени концепта из набора вычтены (замер — "
        "`chain_projection.validation`). Буквальное совпадение термина с текстом "
        "промпта всё же возможно (термин одного концепта может совпасть со словом "
        "из имени другого) — такие задачи снимает лейк-правило, не ослабленное ради "
        "них: числа в `chain_projection.still_excluded`."
    ),
}


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def count_lines(path: Path) -> int:
    n = 0
    with path.open("rb") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


# ─────────────────────────────────────────────────────────────────────────────
# Реестр сред
# ─────────────────────────────────────────────────────────────────────────────
def read_registry(path: Path) -> dict:
    """Разбор `envs.yaml` — веса и заявленные `task_count` (только чтение)."""
    try:
        import yaml
    except ImportError as e:  # pragma: no cover — PyYAML есть в окружении кейса
        raise NotVerified(f"PyYAML недоступен, реестр не прочитать: {e}") from e
    if not path.is_file():
        raise NotVerified(f"реестр сред не найден: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise NotVerified(f"{path}: не разбирается как YAML ({e})") from e
    envs = (data or {}).get("environments")
    if not isinstance(envs, dict) or not envs:
        raise NotVerified(f"{path}: нет секции environments")
    return envs


def env_verdict(spec: dict, registry: dict) -> tuple[bool, str | None]:
    """Решение по среде до чтения задач: веса реестра (ADR-021 п.1/п.2)."""
    cfg = registry.get(spec["id"])
    if not isinstance(cfg, dict):
        return False, f"среды нет в реестре {REGISTRY_NAME}"
    jw = float(cfg.get("judge_weight", 0.0) or 0.0)
    vw = float(cfg.get("verifiable_weight", 0.0) or 0.0)
    if jw > MAX_JUDGE_WEIGHT:
        return False, (f"judge_weight {jw:g} > {MAX_JUDGE_WEIGHT:g} — судья в контуре "
                       f"награды (AD-6/C-013, ADR-021 п.2)")
    if vw < MIN_VERIFIABLE_WEIGHT:
        return False, f"verifiable_weight {vw:g} < {MIN_VERIFIABLE_WEIGHT:g} (ADR-021 п.1)"
    return True, None


# ─────────────────────────────────────────────────────────────────────────────
# Проекция gold → проверяемый ответ
# ─────────────────────────────────────────────────────────────────────────────
def card_frontmatter(card: str) -> str | None:
    """Текст YAML-frontmatter карточки (без ограничителей) или None."""
    if not card or not card.startswith("---"):
        return None
    end = card.find("\n---", 3)
    return card[3:end] if end > 0 else None


def card_slug(card: str) -> tuple[str | None, str]:
    """`slug` карточки концепта — проверяемое поле проекции.

    Порядок: YAML-frontmatter → строка `slug:` регуляркой → ничего.
    Регулярка нужна не «на всякий случай»: 56 карточек E-сред (29 classify,
    24 formula, 3 repair) не парсятся YAML — в `title:` попадает двоеточие
    («A1: Tool Execution Signaled ...»), и незакавыченный скаляр ломает разбор.
    Строка `slug` при этом цела, а её значение совпадает с `meta.slug` во всех
    56 случаях (проверено конвертером) — проекция остаётся однозначной.
    """
    raw = card_frontmatter(card)
    if raw is None:
        return None, "gold_card без frontmatter — проекция невозможна"
    try:
        import yaml
        data = yaml.safe_load(raw)
        if isinstance(data, dict) and data.get("slug"):
            return str(data["slug"]).strip(), "frontmatter(yaml)"
    except Exception:  # noqa: BLE001 — любой сбой разбора уходит в регулярку ниже
        pass
    m = re.search(r"^\s*slug:\s*(\S+)", raw, re.M)
    if m:
        return m.group(1).strip(), "frontmatter(regex: yaml не разобрался)"
    return None, "gold_card без строки slug — проекция невозможна"


def gold_tokens(spec: dict, rec: dict) -> tuple[list[str] | None, str, str]:
    """Проверяемые строки задачи + описание применённой проекции.

    Возвращает ``(tokens | None, reason_если_None, как_получено)``.
    """
    kind = spec["projection"]
    if kind == "card_slug":
        slug, how = card_slug(str(rec.get("gold_card") or ""))
        if not slug:
            return None, how, how
        meta_slug = str((rec.get("meta") or {}).get("slug") or "")
        if meta_slug and meta_slug != slug:
            return None, (f"frontmatter slug '{slug}' != meta.slug '{meta_slug}' — "
                          f"проекция неоднозначна"), how
        return [slug], "", how
    if kind == "gold_relation":
        rel = rec.get("gold_relation") or {}
        frm, to = str(rel.get("from") or ""), str(rel.get("to") or "")
        if not frm or not to:
            return None, "gold_relation без from/to — проекция невозможна", "gold_relation"
        return [frm, to], "", "gold_relation.{from,to}"
    return None, PROJECTION_RULES.get("unprojectable", "проекция не задана"), "-"


# ─────────────────────────────────────────────────────────────────────────────
# Симуляция замороженного верификатора
# ─────────────────────────────────────────────────────────────────────────────
def strip_tool_responses(text: str) -> str:
    """Ответ без текста инструмента — ровно как в `verify_task`."""
    return re.sub(r"<tool_response>.*?</tool_response>", "", text, flags=re.S)


def verifier_accepts(task: dict, answer: str) -> bool:
    """Симуляция диспетчера `verify_task` (slug- и keyword-ветки).

    Нужна, чтобы пул не нёс задач, которые верификатор не примет ни на каком
    ответе (`task_type` вне словаря → `return False`), и чтобы проверяемые
    строки задачи действительно были проверяемыми. Определения концептов
    передаются в ``task["_defs"]`` (их кладёт конвертер из индекса) — в
    пайплайне это `_ensure_def_index()`.
    """
    resp = strip_tool_responses(answer).lower()
    tt = task.get("task_type", "")
    if tt in SLUG_TASK_TYPES:
        slugs = task.get("expected_slugs", task.get("expected_slug", []))
        if isinstance(slugs, str):
            slugs = [slugs]
        if not slugs:
            return False
        return all(str(s).lower() in resp for s in slugs)
    if tt in KEYWORD_TASK_TYPES:
        kws = [k for k in task.get("keywords", []) if k.lower() not in KEYWORD_DROP]
        if not kws:
            return False
        t1 = sig_words(task.get("_defs", {}).get(kws[0], ""))
        t2 = sig_words(task.get("_defs", {}).get(kws[1], "")) if len(kws) > 1 else []
        if not t1 and not t2:
            return False
        h1 = sum(1 for w in t1 if w in resp)
        h2 = sum(1 for w in t2 if w in resp)
        return h1 >= min(3, len(t1)) and h2 >= min(3, len(t2))
    return False


#: Значимые слова определения — копия `_sig_words`/`_COMPARE_STOP` пайплайна
#: (`laguna_pipeline_v8.py:540-560`). Копия, а не импорт: пайплайн тянет torch
#: на импорте (и читается живым прогоном). Дрейф ловится тестом
#: `tests:verify_task constants match pipeline source`.
_COMPARE_STOP = frozenset("""от в отличие и что отличается для на использует не которые тем а с или только например
позволяет который методов обеспечивает где более как используют это систем существующих является
стандартного может традиционных других стандартных моделей явно подходов по через предоставляет
требует к делает данных но статических простых простого генерации работает имеет между основе
которая их просто динамически структуру the and that et al a llm agent agents attention
этого этой этот этом при все его она они мы он так же во ни за об из до у о""".split())


def sig_words(text: str, top: int = 10) -> list[str]:
    """Значимые слова текста: len>=5, не стоп-слова, первые ``top`` уникальных."""
    words = re.findall(r"[а-яa-z0-9_\-]{5,}", (text or "").lower())
    out, seen = [], set()
    for w in words:
        if w not in _COMPARE_STOP and w not in seen:
            seen.add(w)
            out.append(w)
            if len(out) >= top:
                break
    return out


def keyword_branch_ok(defs: dict, keywords: list[str]) -> tuple[bool, str]:
    """Достижим ли gold в keyword-ветке: определения дают верификатору материал."""
    if len(keywords) < 2:
        return False, "keyword-ветке нужны два концепта (keywords), найдено меньше"
    mats = {k: sig_words(defs.get(k, "")) for k in keywords[:2]}
    empty = [k for k, t in mats.items() if not t]
    if len(empty) == 2:
        return False, ("определения обоих концептов пусты/коротки — верификатор вернёт "
                       "False на любой ответ (ложный ноль)")
    return True, ("термины определений: "
                  + ", ".join(f"{k}:{len(t)}" for k, t in mats.items()))


# ─────────────────────────────────────────────────────────────────────────────
# Лейк-правило: проверяемый ответ не должен лежать в промпте (S3f-fix)
# ─────────────────────────────────────────────────────────────────────────────
def keyword_terms(task: dict) -> list[list[str]]:
    """Термины определений обоих концептов — по списку на концепт.

    Пустой список — задача не той ветки. Именно эти термины (а не `keywords`)
    ищет в ответе keyword-ветка верификатора, поэтому «проверяемый ответ» здесь
    — они.
    """
    if task.get("task_type") not in KEYWORD_TASK_TYPES:
        return []
    out = []
    for k in [k for k in task.get("keywords", []) if k.lower() not in KEYWORD_DROP][:2]:
        out.append(sig_words(task.get("_defs", {}).get(k, "")))
    return out


def checked_strings(task: dict) -> list[str]:
    """Строки, которые замороженный верификатор ищет **в ответе** задачи.

    Для slug-ветки — `expected_slugs`; для keyword-ветки — термины определений
    обоих концептов (slug-и этой веткой не проверяются вовсе — см.
    `verifier_accepts`).
    """
    tt = task.get("task_type", "")
    if tt in SLUG_TASK_TYPES:
        slugs = task.get("expected_slugs", task.get("expected_slug", []))
        if isinstance(slugs, str):
            slugs = [slugs]
        return [str(s) for s in slugs or []]
    return [w for terms in keyword_terms(task) for w in terms]


def chain_concepts(prompt: str, task_type: str) -> tuple[str, str, str | None] | None:
    """Концепты промпта цепочки по ролям: ``(начало, конец, подсказка|None)``.

    ``None`` — якоря не нашлись: роли неизвестны, значит неизвестно и то, чьи
    определения проверять. Проекция в этом случае **невозможна** — задача
    исключается с поимённой причиной, а не догадкой (угаданная роль дала бы
    проверку не того концепта).
    """
    a, b = CHAIN_ANCHORS.get(task_type, (None, None))
    if not a:
        return None
    m1, m2 = re.search(a, prompt), re.search(b, prompt)
    if not (m1 and m2):
        return None
    hint_pat = CHAIN_HINT_ANCHORS.get(task_type)
    hint = re.search(hint_pat, prompt) if hint_pat else None
    return m1.group(1), m2.group(1), (hint.group(1) if hint else None)


def chain_content_terms(concept: str, defs: dict[str, str]) -> list[str]:
    """Термины определения концепта — проверяемый материал цепочки.

    Из значимых слов определения (`sig_words` — та же нарезка, что у keyword-ветки
    верификатора) вычитаются слова **имени самого концепта**: в промпте имя есть по
    построению (это условие задачи), поэтому слово, из которого оно собрано, модель
    получает даром — проверять его значило бы проверять копирование условия, то есть
    ровно дефект, который эта дельта исправляет. Берётся не больше
    `CHAIN_TERMS_PER_CONCEPT` слов — столько же требует keyword-ветка.
    """
    own = set(re.findall(r"[a-z0-9]+", concept.lower()))
    words = [w for w in sig_words(defs.get(concept, "")) if w not in own]
    return words[:CHAIN_TERMS_PER_CONCEPT]


def chain_checked_strings(prompt: str, task_type: str,
                          defs: dict[str, str]) -> tuple[list[str], dict] | None:
    """Проекция gold цепочки: ``(проверяемые строки, роли)`` или ``None``.

    Проверяемый набор формируется из **содержания** концептов, которые промпт
    обязывает найти (начало и конец), а не из их имён: имена — условие задачи и
    лежат в промпте. Промежуточный концепт (подсказка «например, 'b' или другой»)
    не проверяется — промпт разрешает другой выбор.

    Набор — конкатенация по концептам со снятием повторов (порядок сохраняется):
    повтор между концептами не ослабляет и не усиливает меру — верификатор
    проверяет конъюнкцию, а объединение содержит термины каждого концепта
    целиком, поэтому «не меньше ``CHAIN_TERMS_PER_CONCEPT`` на каждый концепт»
    сохраняется и в записи без дублей.
    """
    if task_type not in CHAIN_TASK_TYPES:
        return None
    found = chain_concepts(prompt, task_type)
    if found is None:
        return None
    start, end, hint = found
    from_start = chain_content_terms(start, defs)
    from_end = chain_content_terms(end, defs)
    if not from_start and not from_end:
        return None
    terms: list[str] = []
    for word in from_start + from_end:
        if word not in terms:
            terms.append(word)
    roles = {"start": start, "end": end, "hint": hint,
             "checked_from": {"start": from_start, "end": from_end},
             "terms_per_concept": CHAIN_TERMS_PER_CONCEPT}
    return terms, roles


def _chain_projection_stat(stats: dict, task: dict,
                           roles: tuple | None, projected: bool) -> None:
    """Диагностика проекции цепочек по факту промптов (в карточку, не фильтр).

    Отвечает числами на вопрос, который иначе читается догадкой: сколько цепочек
    получили проверяемый набор, у скольких распознаны роли, у скольких промпт
    называет промежуточный концепт (`hint_named`). Последнее число существует,
    чтобы «промежуточное не проверяется» было измерением: у всех этих задач
    промпт называет звено, а проверяемый набор его не требует — что именно
    отвергается на ответе с подсказкой, показывает проба
    (`tools/probe_chain_projection.py`, проба `intermediate_not_checked`).
    """
    tt = task.get("task_type", "")
    if tt not in CHAIN_TASK_TYPES:
        return
    d = stats.setdefault("chain_projection", {
        "checked": 0, "projected": 0, "roles_recognized": 0, "roles_unknown": 0,
        "hint_named": 0, "projection_impossible": 0, "by_task_type": {},
    })
    d["checked"] += 1
    d["by_task_type"][tt] = d["by_task_type"].get(tt, 0) + 1
    if roles is None:
        d["roles_unknown"] += 1
    else:
        d["roles_recognized"] += 1
        if roles[2]:
            d["hint_named"] += 1
    if projected:
        d["projected"] += 1
    else:
        # Два разных провала: роли неизвестны (угадывать нельзя) и определения
        # концептов пусты (проверяемый набор пуст). Оба видны числом.
        d["projection_impossible"] += 1


def leak_form(task: dict, prompt: str) -> tuple[str | None, str]:
    """Форма лейка задачи: ``(код, объяснение)`` или ``(None, "")`` — лейка нет.

    Правило одно на оба контура (гейт A стража C-009 и пул ревизии, ADR-021
    п.3): **проверяемый ответ не должен присутствовать в промпте в проверяемой
    форме**. «Проверяемая форма» — та, в которой строку потребляет замороженный
    верификатор, и она у веток разная:

    * **slug-ветка** — буквальное вхождение проверяемой строки
      (``checked_in_prompt``, нормализация как в `check_eval_leakage.py`);
    * **keyword-ветка** — термины определений с порогом ``min(3, |терминов|)``
      на каждый концепт: лейк не «одно слово встретилось в промпте» (терминов
      много, частичное совпадение обычно), а **порог, набранный из промпта** —
      дословный пересказ промпта уже принимается верификатором.

    Исключений по типу задачи нет, и у цепочек тоже: их проверяемый набор —
    термины определений концептов-условий (S3f-fix-2), а не имена концептов,
    поэтому ролевого исключения «промежуточное значение — не лейк» больше нет.
    Оно существовало ровно пока проверялись имена: у содержания определения роли
    в цепочке нет, и исключение оправдывало бы **любое** совпадение, то есть
    превращало бы правило в пустое место.
    """
    tt = task.get("task_type", "")
    if tt in SLUG_TASK_TYPES:
        hits = [s for s in checked_strings(task) if checked_in_prompt(s, prompt)]
        if not hits:
            return None, ""
        if tt in CHAIN_TASK_TYPES:
            return "slug_in_prompt", (
                f"проверяемые строки {sorted(hits)} (термины определений "
                f"концептов-условий цепочки) есть в промпте буквально: правило "
                f"«проверяемая строка не в промпте» исключений для цепочек не "
                f"делает, задача снята **консервативно**. Копирование промпта "
                f"верификатор при этом не проходит — он требует все термины обоих "
                f"концептов, — поэтому это «снято консервативно», а не измеренный "
                f"лейк (числа: chain_projection.still_excluded, "
                f"conservative_not_measured)")
        return "slug_in_prompt", (
            f"проверяемые строки {sorted(hits)} есть в промпте буквально — ответ "
            f"копируется из промпта, а не добывается (форма правила: "
            f"check_eval_leakage.checked_in_prompt, гейт A C-009)")
    terms = keyword_terms(task)
    if len(terms) < 2 or not any(terms):
        return None, ""  # верификатор и так вернёт False — это не лейк, а мёртвая задача
    if not verifier_accepts(task, prompt):
        return None, ""
    hits = [[w for w in t if checked_in_prompt(w, prompt)] for t in terms]
    kws = [k for k in task.get("keywords", []) if k.lower() not in KEYWORD_DROP][:2]
    detail = ", ".join(f"{k}: {h}" for k, h in zip(kws, hits))
    return "keyword_prompt_satisfies_verifier", (
        f"порог keyword-ветки набирается из самого промпта — {detail}")


# ─────────────────────────────────────────────────────────────────────────────
# Отбор задач
# ─────────────────────────────────────────────────────────────────────────────
def load_index(path: Path, wanted: set[str]) -> tuple[set[str], dict[str, str]]:
    """Индекс концептов: множество всех slug-ов и определения нужных.

    Читается потоком: файл ~130 МБ, а определений нужен минимум (`--index`
    подменяется в тестах). ``definition or summary`` — как `_ensure_def_index`.
    """
    all_slugs: set[str] = set()
    defs: dict[str, str] = {}
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError as e:
                raise NotVerified(f"{path}: не JSON ({e})") from e
            slug = str(o.get("slug") or "")
            if not slug:
                continue
            all_slugs.add(slug)
            if slug in wanted:
                defs[slug] = str(o.get("definition") or o.get("summary") or "")
    return all_slugs, defs


def task_record(spec: dict, rec: dict, tokens: list[str], line_no: int,
                defs: dict[str, str]) -> dict:
    """Задача пула в формате пайплайна (+ провенанс источника)."""
    task = {
        "task_type": spec["task_type"],
        "prompt": str(rec.get("prompt") or ""),
        "verifier": spec["verifier"],
        "reward": 1.0,
        "source_env": spec["id"],
        "source_task_id": str(rec.get("task_id") or f"line:{line_no}"),
    }
    if spec["projection"] == "gold_relation":
        task["expected_relation"] = str((rec.get("gold_relation") or {}).get("expected") or "")
        task["keywords"] = list(tokens)
    else:
        task["expected_slugs"] = list(tokens)
    if defs:
        task["_defs"] = {k: defs.get(k, "") for k in tokens[:2]}
    return task


def collect(spec: dict, rows: list[tuple[int, dict]], defs: dict[str, str],
            all_slugs: set[str]) -> tuple[list[dict], list[dict], dict]:
    """Отбор задач одной среды: проекция, лейк, достижимость, верифицируемость."""
    kept: list[dict] = []
    excluded: list[dict] = []
    stats: dict = {"actual": len(rows), "by_reason": {}, "projection_how": {},
                   "leak_form": {}}

    def drop(line_no: int, rec: dict, code: str, reason: str,
             leak_form: str | None = None, task_type: str | None = None) -> None:
        entry = {
            "env": spec["id"], "line": line_no,
            "task_id": str(rec.get("task_id") or f"line:{line_no}"),
            "reason_code": code, "reason": reason,
        }
        if leak_form:
            entry["leak_form"] = leak_form
        if task_type:
            entry["task_type"] = task_type
        excluded.append(entry)
        stats["by_reason"][code] = stats["by_reason"].get(code, 0) + 1

    for line_no, rec in rows:
        prompt = str(rec.get("prompt") or "")
        if not prompt.strip():
            drop(line_no, rec, "empty_prompt", "пустой промпт")
            continue
        tokens, reason, how = gold_tokens(spec, rec)
        if not tokens:
            drop(line_no, rec, "gold_unprojectable", reason)
            continue
        stats["projection_how"][how] = stats["projection_how"].get(how, 0) + 1

        if spec["projection"] == "card_slug":
            short = [t for t in tokens if len(t) < MIN_TOKEN_LEN]
            if short:
                drop(line_no, rec, "token_too_short",
                     f"проверяемая строка короче {MIN_TOKEN_LEN} символов ({short[0]!r}): "
                     f"подстрочная сверка совпадёт со случайным словом ответа")
                continue
        task = task_record(spec, rec, tokens, line_no,
                           {} if spec["projection"] == "card_slug" else defs)

        # Лейк-правило (S3f-fix, ADR-021 п.3) — до проверки достижимости: задача,
        # чей ответ лежит в промпте, не «трудная», а бессмысленная — её снимает
        # не отсутствие концепта в индексе, а копирование.
        form, detail = leak_form(task, prompt)
        if form:
            stats["leak_form"][form] = stats["leak_form"].get(form, 0) + 1
            drop(line_no, rec, "prompt_leak", detail, leak_form=form,
                 task_type=str(task.get("task_type")))
            continue

        if spec["projection"] == "card_slug":
            missing = [t for t in tokens if t not in all_slugs]
            if missing:
                drop(line_no, rec, "gold_unreachable",
                     f"концепта {missing[0]!r} нет в индексе концептов — модель не может "
                     f"получить slug поиском (ложный ноль)")
                continue
        else:  # keyword-ветка: голден не slug, а определения обоих концептов
            ok, detail = keyword_branch_ok(defs, tokens)
            if not ok:
                drop(line_no, rec, "gold_unreachable", detail)
                continue

        if not verifier_accepts(task, gold_answer(task)):
            drop(line_no, rec, "verifier_rejects_gold",
                 "верификатор пайплайна не принимает эталонный ответ этой задачи "
                 "(формат/ветка не совпадают) — гарантированный ложный ноль")
            continue
        task.pop("_defs", None)
        kept.append(task)
    return kept, excluded, stats


def gold_answer(task: dict) -> str:
    """Эталонный ответ по gold — из чего должен состоять правильный ответ.

    Для slug-ветки достаточно упомянуть slug-и; для keyword-ветки — термины
    определений. Используется как само-проверка пула (задача, которую нельзя
    решить «по gold», не решается вообще).
    """
    if task["task_type"] in SLUG_TASK_TYPES:
        slugs = task.get("expected_slugs", [])
        return "Ответ: " + ", ".join(str(s) for s in slugs)
    terms: list[str] = []
    for k in task.get("keywords", [])[:2]:
        terms.extend(sig_words(task.get("_defs", {}).get(k, "")))
    return "Ответ: " + ", ".join(terms)


def gold_of(task: dict) -> tuple:
    """Проверяемый набор строк задачи — то, что верификатор ищет в ответе."""
    return tuple(sorted(task.get("expected_slugs") or task.get("keywords") or []))


def dedup(tasks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Дубли внутри пула: промпт, неоднозначный промпт, пары relation-задач.

    * **Промпт, повторённый дословно**, — не «больше данных», а задвоенный вес
      задачи в отборе (и лишний шум в метриках): остаётся первая.
    * **Неоднозначный промпт** — один и тот же вопрос с *разными* проверяемыми
      ответами (у E2 два концепта с одинаковым `title` дают один промпт «Запиши
      формулу X»). Это не дубль, а дефект: награда становится монеткой, поэтому
      снимаются **обе** задачи, а не первая попавшаяся.
    * **Совпадение проверяемого набора** отбрасывается только у relation-задач
      (``keywords``): там пара концептов и есть задача («как связаны A и B»), и та
      же пара в другой формулировке (в том числе обратным порядком — ``B_to_A``)
      — та же задача. Для slug-ветки правило неприменимо: один концепт законно
      несёт несколько разных задач (E2_formula «запиши формулу X» и E3_classify
      «определи тип X» проверяются одним slug-ом, но спрашивают разное), и 1818
      задач E2 почти целиком лежат в E3.
    """
    out, dropped = [], []
    by_prompt: dict[str, set] = {}
    for t in tasks:
        by_prompt.setdefault(norm(t["prompt"]), set()).add(gold_of(t))
    ambiguous = {p for p, golds in by_prompt.items() if len({g for g in golds if g}) > 1}

    seen_prompt: dict[str, dict] = {}
    seen_gold: dict[tuple, dict] = {}
    for t in tasks:
        key = norm(t["prompt"])
        relation = t["task_type"] in KEYWORD_TASK_TYPES and bool(t.get("keywords"))
        gold = gold_of(t) if relation else ()
        why = None
        if key in ambiguous:
            why = "ambiguous_prompt"
            reason = ("промпт дословно совпадает с другой задачей, но проверяемый ответ "
                      "иной (концепты с одинаковым названием) — ответ на такой промпт "
                      "не определён, награда была бы монеткой")
        elif key in seen_prompt:
            why = "duplicate_prompt"
            reason = (f"промпт дословно повторяет задачу {seen_prompt[key]['source_task_id']} "
                      f"({seen_prompt[key]['source_env']}) — задвоенный вес в отборе")
        elif gold and gold in seen_gold:
            why = "duplicate_gold"
            reason = (f"та же проверяемая пара {list(gold)} уже есть в задаче "
                      f"{seen_gold[gold]['source_task_id']} ({seen_gold[gold]['source_env']})")
        if why:
            dropped.append({"env": t["source_env"], "task_id": t["source_task_id"],
                            "reason_code": why, "reason": reason})
            continue
        seen_prompt[key] = t
        if gold:
            seen_gold.setdefault(gold, t)
        out.append(t)
    return out, dropped


def overlap_with(tasks: list[dict], ref: Path, kind: str) -> tuple[list[dict], list[dict]]:
    """Пересечение с чужим пулом по нормализованному промпту и по gold-набору.

    ``kind`` ∈ {``eval``, ``v1``}: eval — набор, на котором считается ось AD-1
    (задача, дословно совпавшая с eval, «мерит» уже измеренное); v1 — действующий
    пул ревизии (дубль между пулами — задвоенный вес в отборе).
    """
    ref_prompts: dict[str, str] = {}
    ref_gold: dict[tuple, str] = {}
    for i, o in read_jsonl(ref):
        rid = str(o.get("task_id") or o.get("id") or f"line:{i + 1}")
        prompt = eval_prompt_text(o) if kind == "eval" else str(o.get("prompt") or "")
        if prompt:
            ref_prompts.setdefault(norm(prompt), rid)
        gold = gold_of(o)
        if gold:
            ref_gold.setdefault(gold, rid)

    kept, dropped = [], []
    for t in tasks:
        key = norm(t["prompt"])
        gold = gold_of(t)
        if key in ref_prompts:
            dropped.append({"env": t["source_env"], "task_id": t["source_task_id"],
                            "reason_code": f"{kind}_overlap",
                            "reason": (f"промпт дословно совпадает с {kind}-задачей "
                                       f"{ref_prompts[key]} (нормализация как в C-009)")})
            continue
        if gold and gold in ref_gold:
            dropped.append({"env": t["source_env"], "task_id": t["source_task_id"],
                            "reason_code": f"{kind}_gold_overlap",
                            "reason": (f"та же проверяемая пара {list(gold)}, что у "
                                       f"{kind}-задачи {ref_gold[gold]}")})
            continue
        kept.append(t)
    return kept, dropped


def sft_overlap(tasks: list[dict], sft_path: Path) -> tuple[list[dict], list[dict], dict]:
    """Пересечение с SFT v12 — той же машинерией, что страж C-017.

    Задача, чей промпт дословно есть в SFT, даёт награду «по памяти» (ADR-007
    п.1) — на пуле ревизии это уже решено exclusion'ом, здесь то же правило
    применено к новым средам.
    """
    index = {norm(t["prompt"]): [{"rl_line": i, "rl_id": t["source_task_id"],
                                  "rl_task_type": t["task_type"]}]
             for i, t in enumerate(tasks, start=1)}
    res = scan_sft(sft_path, index, None, 0)
    matched = set(res["matched_rl_lines"])
    kept, dropped = [], []
    for i, t in enumerate(tasks, start=1):
        if i in matched:
            dropped.append({"env": t["source_env"], "task_id": t["source_task_id"],
                            "reason_code": "sft_overlap",
                            "reason": ("промпт дословно присутствует в sft_train_v12 — "
                                       "награда достижима по памяти, не решением (ADR-007 п.1)")})
            continue
        kept.append(t)
    return kept, dropped, {"sft_examples": res["sft_examples"],
                           "sft_matched_prompts": res["sft_matched_prompts"]}


def env_types_tasks(path: Path, all_slugs: set[str], defs: dict[str, str],
                    stats: dict) -> tuple[list[dict], list[dict]]:
    """env_types (700): формат пайплайна уже есть — маршрутизация и проверка ветки.

    Одна правка по существу: у 200 задач `ood_pair` поля `expected_slugs` нет
    (только `keywords`), а диспетчер `verify_task` отправляет `ood_pair` в
    **slug-ветку** → `slugs = []` → `return False` на любой ответ. Это не
    «слабая» задача, а мёртвая: 200 задач давали бы нулевую награду всегда.
    Их форма (два slug-а + `expected_relation`) — ровно форма `explain_relation`,
    поэтому они перемаршрутизируются туда, а не выбрасываются.

    Вторая правка — проекция цепочек (S3f-fix-2, ADR-021 уточнение 16.09.2026):
    у `chain_reasoning` (300) и `formula_chain` (200) проверяемый набор собирается
    из **терминов определений** концептов-условий, а не из их имён. Прежняя
    проекция брала проверяемым набором концы цепочки — те самые, что названы в
    промпте, — и лейк-фильтр (S3f-fix) снял все 500 задач: проверка сводилась к
    «воспроизведи условие». Правило при этом было верным, неверной была проекция.
    Подробности и границы — в `PROJECTION_RULES["chain_definition_terms"]`.
    """
    kept, excluded = [], []

    def drop(task: dict, code: str, reason: str, leak: str | None = None,
             task_type: str | None = None) -> None:
        entry = {"env": "env_types", "task_id": task["source_task_id"],
                 "reason_code": code, "reason": reason}
        if leak:
            entry["leak_form"] = leak
        if task_type:
            entry["task_type"] = task_type
        excluded.append(entry)

    for line_no, o in read_jsonl(path):
        task = dict(o)
        task["source_env"] = "env_types"
        task["source_task_id"] = str(o.get("id") or o.get("task_id") or f"line:{line_no + 1}")
        tt = task.get("task_type", "")
        if tt not in VERIFIER_TASK_TYPES:
            drop(task, "unknown_task_type", f"task_type {tt!r} вне словаря верификатора")
            continue
        if tt == "ood_pair" and not task.get("expected_slugs"):
            kws = [k for k in task.get("keywords", []) if k not in KEYWORD_DROP]
            if len(kws) < 2:
                drop(task, "verifier_rejects_gold",
                     "ood_pair без expected_slugs и без двух keywords — верификатор "
                     "вернёт False всегда")
                continue
            ok, detail = keyword_branch_ok(defs, kws)
            if not ok:
                drop(task, "gold_unreachable", detail)
                continue
            task["task_type"] = "explain_relation"
            task["_defs"] = {k: defs.get(k, "") for k in kws[:2]}
            task["rerouted_from"] = "ood_pair"
            stats["rerouted_ood_pair"] = stats.get("rerouted_ood_pair", 0) + 1
        elif tt == "explain_relation":
            kws = [k for k in task.get("keywords", []) if k not in KEYWORD_DROP]
            ok, detail = keyword_branch_ok(defs, kws)
            if not ok:
                drop(task, "gold_unreachable", detail)
                continue
            task["_defs"] = {k: defs.get(k, "") for k in kws[:2]}
        elif tt in CHAIN_TASK_TYPES:
            # S3f-fix-2: проверяемый набор — содержание концептов-условий.
            prompt = str(task.get("prompt") or "")
            anchor_roles = chain_concepts(prompt, tt)
            projection = chain_checked_strings(prompt, tt, defs)
            _chain_projection_stat(stats, task, anchor_roles, projection is not None)
            if projection is None:
                code, why = _chain_projection_failure(prompt, tt)
                stats.setdefault("chain_projection_failure", {})
                stats["chain_projection_failure"][code] = \
                    stats["chain_projection_failure"].get(code, 0) + 1
                drop(task, code, why, task_type=tt)
                continue
            terms, roles = projection
            task["expected_slugs"] = terms
            task["chain_roles"] = roles
            missing = [c for c in (roles["start"], roles["end"]) if c not in all_slugs]
            if missing:
                stats["slug_not_in_index"] = stats.get("slug_not_in_index", 0) + 1
        else:  # прочие slug-ветки: проверяемой строкой служит ожидаемый slug генератора
            slugs = task.get("expected_slugs") or []
            if not slugs:
                drop(task, "verifier_rejects_gold",
                     "slug-ветка без expected_slugs — ложный ноль")
                continue
            missing = [s for s in slugs if s not in all_slugs]
            if missing:
                stats["slug_not_in_index"] = stats.get("slug_not_in_index", 0) + 1

        # Лейк-правило (S3f-fix, ADR-021 п.3): проверяемый ответ в промпте.
        # Диагностика проекции считается у **всех** цепочек (и снятых, и
        # оставленных), а не только у прошедших фильтр: иначе «проекция удалась
        # N раз» не отличить от «проекция не проверялась».
        form, detail = leak_form(task, str(task.get("prompt") or ""))
        if form:
            stats.setdefault("leak_form", {})
            stats["leak_form"][form] = stats["leak_form"].get(form, 0) + 1
            if form == "slug_in_prompt":
                stats["slug_in_prompt"] = stats.get("slug_in_prompt", 0) + 1
            if tt in CHAIN_TASK_TYPES:
                stats["chain_leak_excluded"] = stats.get("chain_leak_excluded", 0) + 1
            drop(task, "prompt_leak", detail, leak=form,
                 task_type=str(task.get("task_type")))
            continue

        if not verifier_accepts(task, gold_answer(task)):
            drop(task, "verifier_rejects_gold",
                 "верификатор не принимает эталонный ответ задачи")
            continue
        task.pop("_defs", None)
        kept.append(task)
    return kept, excluded


def _chain_projection_failure(prompt: str, task_type: str) -> tuple[str, str]:
    """Почему проекция цепочки невозможна — поимённо, а не «не вышло»."""
    found = chain_concepts(prompt, task_type)
    if found is None:
        return "gold_unprojectable", (
            "якоря начала/конца в промпте не распознаны — роли концептов "
            "неизвестны, а значит неизвестно, чьи определения проверять "
            "(угадывать роль вместо проверки — ложный ноль)")
    start, end, _hint = found
    return "gold_unreachable", (
        f"у концептов-условий ({start}, {end}) нет значимых слов определений в "
        f"индексе концептов — проверяемый набор пуст, модель не может его добыть")


# ─────────────────────────────────────────────────────────────────────────────
# Запись пула
# ─────────────────────────────────────────────────────────────────────────────
def pool_text(tasks: list[dict]) -> str:
    """Текст пула: по строке JSON на задачу (порядок источников сохраняется)."""
    return "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks)


def write_pool(path: Path, tasks: list[dict], force: bool) -> dict:
    """Пишет пул атомарно; существующий пул не перезаписывается без --force."""
    text = pool_text(tasks)
    if path.is_file() and not force:
        current = path.read_text(encoding="utf-8")
        if current != text:
            raise FileExistsError(
                f"{path} существует и отличается от нового пула — не перезаписываю; "
                f"сверь пул или передай --force")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return {"path": str(path), "lines": len(tasks), "sha256": sha256_file(path)}


def write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return str(path)


def ensure_pool_link(link: Path, target: str) -> None:
    """Симлинк пула в дереве кейса (AD-4: данные — только на gb10-shared)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.readlink() == Path(target):
        return
    if link.exists() and not link.is_symlink():
        raise FileExistsError(f"{link} — настоящий файл, симлинк не подменяю")
    if link.is_symlink():
        link.unlink()
    link.symlink_to(target)


def write_not_a_run(directory: Path) -> None:
    """Маркер C-012: каталог пула — не каталог прогона (манифест AD-2 был бы фикцией)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / NOT_A_RUN_MARKER).write_text(
        "Каталог пула ревизии v2, не прогон: здесь лежит отобранный пул данных\n"
        "(симлинк rl_pool_filtered.jsonl, ADR-021), а не результаты стадии.\n"
        "Манифест AD-2 (run_manifest.json) к нему неприменим — прогона нет.\n"
        "Читается стражем C-012 (tools/check_run_manifest.py).\n",
        encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Реестр сред — пересборка по факту (явный режим)
# ─────────────────────────────────────────────────────────────────────────────
def update_registry(path: Path, facts: dict[str, int], backup: Path) -> list[str]:
    """Правит `task_count` по факту и помечает незаполненные среды.

    Правка **текстовая**, а не round-trip через YAML: в файле есть комментарии
    (шапка, пояснения), `yaml.safe_load` → `dump` их теряет, и «пересобрали
    реестр» превратилось бы в «переписали реестр». Реестр читает `arena/arena.py`
    (`envs.yaml` → список сред со `status: ready` и `task_count > 0`), формат
    ключей для него значим — поэтому меняются только значения.
    """
    text = path.read_text(encoding="utf-8")
    if not backup.is_file():
        backup.write_text(text, encoding="utf-8")
    changes: list[str] = []
    for env_id, actual in facts.items():
        m = re.search(rf"^(\s*{re.escape(env_id)}:\n(?:.*?\n)*?\s*task_count:\s*)(\d+)",
                      text, re.M)
        if not m:
            changes.append(f"{env_id}: task_count не найден — правка пропущена")
            continue
        if int(m.group(2)) != actual:
            changes.append(f"{env_id}.task_count: {m.group(2)} → {actual} (факт)")
            text = text[:m.start(2)] + str(actual) + text[m.end(2):]
    # E8: 0 задач — «ready» в реестре не должно читаться как «среда есть».
    m = re.search(r"^(\s*E8_plan_experiment:\n\s*status:\s*)(\S+)", text, re.M)
    if m and m.group(2) != "empty":
        changes.append(f"E8_plan_experiment.status: {m.group(2)} → empty (0 задач, ADR-021 п.2)")
        text = text[:m.start(2)] + "empty" + text[m.end(2):]
    if "note: " not in text.split("E8_plan_experiment:")[-1][:400]:
        anchor = re.search(r"^(\s*E8_plan_experiment:\n\s*status:\s*\S+\n)", text, re.M)
        if anchor:
            note = ('    note: "не заполнена: 0 задач в tasks/plan_experiment.jsonl; '
                    'в пул ревизии v2 не подключена (judge_weight 0.7 > 0.2, ADR-021 п.2)"\n')
            text = text[:anchor.end(1)] + note + text[anchor.end(1):]
            changes.append("E8_plan_experiment.note: причина незаполненности записана")
    if changes:
        path.write_text(text, encoding="utf-8")
    return changes


# ─────────────────────────────────────────────────────────────────────────────
def build(args) -> tuple[dict, list[dict], dict]:
    """Полный проход: отбор сред → задачи → фильтры → пул. Возвращает (карточка, задачи, отчёт)."""
    envs_root = Path(args.envs_root).expanduser()
    registry_path = envs_root / REGISTRY_NAME
    registry = read_registry(registry_path)

    all_slugs, needed = set(), set()
    for spec in ENV_SPECS:
        if spec["tasks"]:
            path = envs_root / "tasks" / spec["tasks"]
            if spec["projection"] == "card_slug" and path.is_file():
                for _i, rec in read_jsonl(path):
                    slug, _how = card_slug(str(rec.get("gold_card") or ""))
                    if slug:
                        needed.add(slug)
            elif spec["projection"] == "gold_relation" and path.is_file():
                for _i, rec in read_jsonl(path):
                    rel = rec.get("gold_relation") or {}
                    for k in ("from", "to"):
                        if rel.get(k):
                            needed.add(str(rel[k]))
    for _i, o in read_jsonl(Path(args.env_types)):
        for s in (o.get("expected_slugs") or o.get("keywords") or []):
            needed.add(str(s))

    index_path = Path(args.index)
    if not index_path.is_file():
        raise NotVerified(f"индекс концептов не найден: {index_path}")
    all_slugs, defs = load_index(index_path, needed)

    envs: dict[str, dict] = {}
    exclusions: list[dict] = []
    tasks: list[dict] = []
    filters: dict[str, dict] = {}

    for spec in ENV_SPECS:
        path = envs_root / "tasks" / spec["tasks"]
        cfg = registry.get(spec["id"]) or {}
        actual = count_lines(path) if path.is_file() else 0
        ok, why = env_verdict(spec, registry)
        entry = {
            "source": str(path), "registry_count": cfg.get("task_count"),
            "actual": actual, "judge_weight": cfg.get("judge_weight"),
            "verifiable_weight": cfg.get("verifiable_weight"),
            "kept": 0, "excluded": 0, "excluded_reason": why,
            "projection": spec["projection"],
        }
        if actual == 0:
            # Пустая среда исключается не по весам, а по факту: подключать нечего
            # (E8_plan_experiment: файл 0 байт, status: planned — ADR-021 п.2).
            entry["excluded"] = 0
            entry["excluded_reason"] = spec.get("why") or (
                "среда не заполнена: 0 задач (ADR-021 п.2)")
            envs[spec["id"]] = entry
            exclusions.append({"env": spec["id"], "task_id": "-",
                               "reason_code": "env_empty",
                               "reason": entry["excluded_reason"]})
            continue
        if not ok:
            entry["excluded"] = actual
            envs[spec["id"]] = entry
            exclusions.append({"env": spec["id"], "task_id": "-", "reason_code":
                               "judge_weight" if cfg.get("judge_weight", 0) and
                               float(cfg.get("judge_weight") or 0) > MAX_JUDGE_WEIGHT
                               else "not_verifiable", "reason": why})
            continue
        if spec["projection"] == "unprojectable":
            entry["excluded"] = actual
            entry["excluded_reason"] = spec["why"]
            envs[spec["id"]] = entry
            exclusions.append({"env": spec["id"], "task_id": "-",
                               "reason_code": "gold_unprojectable", "reason": spec["why"]})
            continue
        if not path.is_file():
            raise NotVerified(f"{spec['id']}: файл задач не найден: {path}")
        rows = [(i + 1, o) for i, o in read_jsonl(path)]
        kept, dropped, stats = collect(spec, rows, defs, all_slugs)
        entry.update({"kept": len(kept), "excluded": len(dropped),
                      "excluded_by_reason": stats["by_reason"],
                      "projection_how": stats["projection_how"]})
        exclusions.extend(dropped)
        tasks.extend(kept)
        envs[spec["id"]] = entry

    # ── env_types ────────────────────────────────────────────────────────────
    et_path = Path(args.env_types)
    et_stats: dict = {}
    et_kept, et_excluded = env_types_tasks(et_path, all_slugs, defs, et_stats)
    for t in et_kept:
        t.setdefault("reward", 1.0)
    envs["env_types"] = {
        "source": str(et_path), "registry_count": None, "actual": count_lines(et_path),
        "judge_weight": None, "verifiable_weight": None, "kept": len(et_kept),
        "excluded": len(et_excluded), "excluded_reason": None,
        "projection": "as_is",
        "excluded_by_reason": _by_reason(et_excluded),
        "rerouted_ood_pair": et_stats.get("rerouted_ood_pair", 0),
        "prompt_leak": {"total": sum(et_stats.get("leak_form", {}).values()),
                        "by_form": et_stats.get("leak_form", {})},
        "slug_not_in_index": et_stats.get("slug_not_in_index", 0),
        "chain_projection": et_stats.get("chain_projection", {}),
        "note": ("формат пайплайна уже есть; 200 ood_pair перемаршрутизированы в "
                 "explain_relation (у них keywords, а не expected_slugs — slug-ветка "
                 "верификатора вернула бы False на любой ответ); 500 цепочек "
                 "перепроецированы: проверяемый набор — термины определений "
                 "концептов-условий, а не их имена, названные в промпте (S3f-fix-2; "
                 "прежняя проекция проверяла концы — лейк-фильтр S3f-fix снимал тип "
                 "целиком)"),
    }
    exclusions.extend(et_excluded)
    tasks.extend(et_kept)

    # ── фильтры пула ─────────────────────────────────────────────────────────
    before = len(tasks)
    tasks, dup = dedup(tasks)
    filters["duplicates"] = {"excluded": len(dup), "remaining": len(tasks)}
    exclusions.extend(dup)

    tasks, eval_drop = overlap_with(tasks, Path(args.eval), "eval")
    filters["eval_overlap"] = {"excluded": len(eval_drop), "remaining": len(tasks)}
    exclusions.extend(eval_drop)

    tasks, v1_drop = overlap_with(tasks, Path(args.v1), "v1")
    filters["v1_overlap"] = {"excluded": len(v1_drop), "remaining": len(tasks)}
    exclusions.extend(v1_drop)

    tasks, sft_drop, sft_stats = sft_overlap(tasks, Path(args.sft))
    filters["sft_overlap"] = {"excluded": len(sft_drop), "remaining": len(tasks), **sft_stats}
    exclusions.extend(sft_drop)

    # Проверка «в пуле нет задач вне словаря верификатора» — последний барьер
    # перед записью: если сюда что-то просочилось, пул не выпускается.
    bad = [t for t in tasks if t.get("task_type") not in VERIFIER_TASK_TYPES]
    if bad:
        raise NotVerified(f"в пуле {len(bad)} задач с task_type вне словаря верификатора "
                          f"(первая: {bad[0].get('task_type')!r}) — пул не выпускается")

    # `kept` у среды — сколько прошло её собственный отбор; `kept_in_pool` —
    # сколько осталось после фильтров пула (дубли, пересечения). Разница честно
    # видна в карточке: среда не «потеряла» задачи, их снял фильтр пула.
    in_pool = _by_key(tasks, "source_env")
    for env_id, entry in envs.items():
        if entry["kept"]:
            entry["kept_in_pool"] = in_pool.get(env_id, 0)

    # Остаточный замер лейка — тем же правилом, что и отбор (S3f-fix).
    audit = leak_audit(tasks, defs)

    card = {
        "card": "laguna-compact / rev-envs-v2",
        "decision": ("ADR-021: подключаем верифицируемую часть сред E1–E8 "
                     "(verifiable_weight >= 0.8, judge_weight <= 0.2) + env_types"),
        "sources_unchanged": True,
        "inputs": {
            "registry": {"path": str(registry_path), "sha256": sha256_file(registry_path)},
            "index": {"path": str(index_path), "sha256": sha256_file(index_path),
                      "entries": len(all_slugs)},
        },
        "envs": envs,
        "gold_projection": {
            **{s["id"]: PROJECTION_RULES.get(s["projection"], "не задана")
               for s in ENV_SPECS if s["projection"]},
            "env_types": ("формат пайплайна как есть (проекция не нужна — gold уже в "
                          "форме верификатора); 200 ood_pair перемаршрутизированы в "
                          "explain_relation; 500 цепочек перепроецированы на содержание "
                          "концептов-условий — S3f-fix-2)"),
            "chain_definition_terms": PROJECTION_RULES["chain_definition_terms"],
        },
        "filters": filters,
        "checks": {
            "sft_overlap": {
                "excluded": filters["sft_overlap"]["excluded"],
                "guard_profile": ("python3 tools/check_sft_rl_overlap.py --sft "
                                  "datasets/sft_train_v12.jsonl --rl "
                                  "runs/rev-pool-v2/rl_pool_filtered.jsonl --max-matched 0"),
            },
            "eval_overlap": {"excluded": filters["eval_overlap"]["excluded"],
                             "criterion": "нормализованный промпт + проверяемый набор строк"},
            "v1_overlap": {"excluded": filters["v1_overlap"]["excluded"],
                           "criterion": "нормализованный промпт + проверяемый набор строк"},
            "gold_in_prompt_audit": audit,
            "judge_in_reward": {
                "envs_with_judge_weight_above_threshold": [
                    k for k, v in envs.items()
                    if (v.get("judge_weight") or 0) > MAX_JUDGE_WEIGHT],
                "rule": "AD-6/C-013: LLM-судья не участвует в награде (ADR-021 п.2)",
            },
        },
        "leak_fix": _leak_fix(exclusions, audit, before, len(tasks),
                               et_stats.get("chain_projection", {})),
        "chain_projection": _chain_projection_card(
            tasks, exclusions, et_stats.get("chain_projection", {})),
        "counts": {
            "candidates": before,
            "kept": len(tasks),
            "excluded": before - len(tasks),
            "by_reason": _by_reason(exclusions),
            "by_reason_note": ("`by_reason` считает записи отчёта: код уровня задачи — по "
                               "задаче, исключение среды целиком (веса, пустота, "
                               "непроецируемый gold) — одной записью на среду, поэтому "
                               "сумма по кодам больше `excluded` — сколько задач снято "
                               "у среды, видно в envs.<id>.excluded"),
            "by_env_in_pool": in_pool,
            "by_task_type": _by_key(tasks, "task_type"),
            "excluded_envs": [k for k, v in envs.items() if not v["kept"]],
        },
        "limits": [
            "Смешение двух генераций задач: E-среды (июль 2026, «Ариадна», "
            "tasks/*.jsonl) и env_types (август 2026, «oxalpha») — разные стили "
            "промптов; стилевая неоднородность пула не измерена и не сглажена.",
            "Проекция E2/E3 (slug карточки) проверяет, что модель назвала концепт, "
            "о котором задача, — но не содержание ответа (формулу E2, тип/уровень/"
            "формальность E3). Это следствие замороженного бинарного верификатора, "
            "а не свойство задач.",
            "E7_repair (100 задач, verifiable_weight 1.0) не подключена: "
            "проверяемые значения gold присутствуют в тексте промпта (type — в 97/100, "
            "slug — в 100/100), проверка сводилась бы к копированию из промпта.",
            "Класс «проверяемый slug назван в промпте» цепочек больше не описывает: "
            "их проверяемый набор перепроецирован на содержание концептов-условий "
            "(S3f-fix-2), и тип возвращён в пул — числа в `chain_projection`. У E2/E3 "
            "(`find_concept`) проекция прежняя и это не дефект: там проверяемый slug — "
            "сам предмет задачи («назови концепт»), а не условие, и промпт его не "
            "называет; 102 задачи E2/E3 сняты лейк-фильтром ещё базовой ревизией "
            "(`leak_fix`).",
            "Один тип пула может нести несколько сред (find_concept ← E2_formula и "
            "E3_classify): поле source_env сохраняет провенанс, но штатные метрики "
            "пайплайна считаются по task_type — разбивка по средам потребует правки "
            "пайплайна (фаза 2).",
            "Пул не подключён к пайплайну: пилот идёт на rl_tasks_revpool_v1 "
            "(ADR-007/ADR-016), переключение — отдельная дельта.",
            "Цепочки проверяются по содержанию, а не по форме задачи (S3f-fix-2): "
            "проверяемый набор — термины определений концептов-условий, поэтому сама "
            "связь a→b→c (то, ради чего тип существует) этой веткой не проверяется. "
            "Это тот же класс ограничения, что у E2/E3 (проверяется «назвал концепт», "
            "а не содержание ответа): следствие замороженного бинарного верификатора, "
            "а не свойство задач. Проверка содержания связи — правка пайплайна (фаза 2).",
            "Промежуточный концепт цепочки не входит в проверяемый набор "
            "сознательно: промпт предлагает его примером и разрешает другой "
            "(«например, 'b' или другой»), поэтому требование именно генераторского "
            "звена давало бы ложный ноль на верном ответе. Цена решения: модель может "
            "не найти связующее звено и всё равно получить награду, если назвала "
            "термины определений; число промптов с подсказкой — в "
            "`chain_projection` (`hint_named`).",
            "Проверяемый набор цепочки — слова определения, поэтому ответ, "
            "пересказывающий определение **своими** словами, может их не набрать и "
            "получить ноль. Это тот же класс риска, что у keyword-ветки E5 (и там "
            "верификатор требует три значимых слова определения буквально), и та же "
            "мера: дословное совпадение — цена бинарной проверки без судьи (ADR-011 "
            "не вводит частичный кредит). Числа пробы (`gold_accepted 470/470`) "
            "показывают, что эталон конвертера меру проходит; на «пересказ другими "
            "словами» проба ответить не может — это свойство меры, а не пула.",
            "Часть цепочек снята консервативно, а не как измеренный лейк: если один "
            "термин определения буквально встречается в промпте (чаще всего — со "
            "словом из имени соседнего концепта-условия), правило «любая проверяемая "
            "строка в промпте — лейк» снимает задачу, хотя копирование промпта "
            "верификатор не проходит (он требует конъюнкцию всех терминов обоих "
            "концептов). Числа — в `chain_projection.still_excluded` и "
            "`conservative_not_measured`; правило не ослаблено сознательно.",
            "Поле `n_min_steps`, которое env_types несёт в каждой задаче (3 у "
            "chain_reasoning), пайплайном не читается: минимум вызовов выводится из "
            "`task_type` (`MULTI_STEP_TASKS` → 2, остальные → 1). Расхождение "
            "замысла генератора и контура — не дефект пула, но при фазе 2 его стоит "
            "свести (правка пайплайна).",
        ],
    }
    report = {
        "stage": "S3f-fix-2", "decision": "ADR-021 (уточнение 16.09.2026: цепочки)",
        "criterion": ("нормализованный промпт совпадает с промптом чужого пула; "
                      "для gold — совпадает проверяемый набор строк"),
        "normalization": "lower + схлопнутые пробелы (norm из tools/check_eval_leakage.py)",
        "pool": {"path": str(Path(args.out)), "baseline": S3F_FIX_POOL,
                 "after": {"lines": len(tasks)}},
        "leak_fix": card["leak_fix"],
        "chain_projection": card["chain_projection"],
        "counts": card["counts"],
        "filters": filters,
        "excluded": exclusions,
    }
    return card, tasks, report


def _by_reason(exclusions: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in exclusions:
        out[e["reason_code"]] = out.get(e["reason_code"], 0) + 1
    return out


def _leak_fix(exclusions: list[dict], audit: dict, before: int, after: int,
              chain_stats: dict | None = None) -> dict:
    """Сводка лейк-фикса: что было, что снято, что осталось (S3f-fix, ADR-021 п.3).

    Отдельный раздел карточки, а не строка в `by_reason`: решение архитектора —
    «проверяемый ответ не должен присутствовать в промпте», и по нему нужно
    видеть и **сколько снято** (по формам лейка и по типам задач), и **сколько
    осталось** (остаточный замер тем же правилом), и **сколько сохранено**
    исключением о промежуточном значении.
    """
    leaks = [e for e in exclusions if e["reason_code"] == "prompt_leak"]
    forms: dict[str, int] = {}
    for e in leaks:
        form = e.get("leak_form", "unknown")
        forms[form] = forms.get(form, 0) + 1
    baseline_leaks = BASELINE_LEAK_EXCLUSIONS["total"]
    return {
        "rule": LEAK_RULE,
        "forms": LEAK_FORMS,
        "normalization": ("буквальное вхождение без учёта регистра "
                          "(checked_in_prompt из tools/check_eval_leakage.py — "
                          "одно правило с гейтом A стража C-009)"),
        "baseline_pool": BASELINE_POOL,
        # `answer_in_prompt` — замер **до** правки (базовая ревизия пула v2, 9197):
        # столько задач снимает то же правило. Не путать с `after_leak_filter_candidates`
        # — это счёт уже **после** лейк-фильтра, на входе остальных фильтров пула.
        "before_fix": {"answer_in_prompt": dict(BASELINE_LEAK)},
        "after_leak_filter_candidates": before,
        # «Сколько снято» без развилки «всего / этим фильтром» читается неверно:
        # 608 — это все лейк-исключения сборки, а снял **этот** фильтр 506.
        # Развилка названа числами, а не сноской: 102 задачи slug-ветки E2/E3
        # снимались и в базовой ревизии пула (9197), к этой дельте не относятся.
        "excluded": {"total": len(leaks), "by_form": forms,
                     "by_task_type": _by_key(leaks, "task_type"),
                     "by_env": _by_key(leaks, "env"),
                     "already_in_baseline": dict(BASELINE_LEAK_EXCLUSIONS)},
        # Числа этой дельты (S3f-fix) **закреплены**, а не пересчитываются текущей
        # сборкой: её состояние изменила следующая дельта (S3f-fix-2 вернула
        # цепочки), и «сколько снял этот фильтр» от неё не зависит. Проверяемость —
        # в `rollback`: прежняя ревизия конвертера воспроизводит базу 9197.
        "delta": {
            **S3F_FIX_DELTA,
            "current_build_leak_exclusions": len(leaks),
            "current_build_beyond_baseline": len(leaks) - baseline_leaks,
            "why_current_differs": (
                "Сборка после S3f-fix-2 снимает по этому правилу 108 задач "
                "базовой ревизии (102 slug-ветки E2/E3 + 6 keyword E5) и 30 цепочек "
                "— тех, у которых один термин определения совпал с промптом "
                "(консервативно; числа в `chain_projection`). Цепочки, снятые "
                "S3f-fix (500), в текущей проекции либо вернулись (470), либо "
                "сняты этими 30 — развитие смотрится по `chain_projection`, а не "
                "по этому разделу."),
        },
        "independent_check": {
            "what": ("тем же замороженным верификатором, что судит награду: "
                     "ответ = сам промпт → сколько задач награждается за "
                     "копирование (прямая проверка меры, а не повтор правила)"),
            "command": INDEPENDENT_CHECK_CMD,
            "baseline_pool": BASELINE_COPY_REWARDED,
            "after_fix": AFTER_COPY_REWARDED,
            "note": ("Числа `baseline_pool`/`after_fix` — замер на артефактах "
                     "16.09.2026, сборкой не пересчитываются (требуют импорта "
                     "пайплайна). Инвариант, который они подтверждают: "
                     "`net_pool_change` = числу задач, награждавших копирование "
                     "промпта в базовом пуле (505 = 505)."),
        },
        "chain_projection": {
            "what": ("как отработала проекция цепочек на реальных задачах "
                     "(chain_reasoning/formula_chain): проверяемый набор — термины "
                     "определений концептов-условий, роли берутся из якорей промпта, "
                     "промежуточный концепт не проверяется"),
            **(chain_stats or {}),
            "note": ("`hint_named` — сколько промптов называют промежуточный концепт "
                     "примером («например, 'b' или другой»), то есть «промежуточное» — "
                     "не мёртвая роль, а измеренная; в проверяемый набор оно при этом "
                     "не входит (промпт разрешает другой выбор), и это проверяется "
                     "ответом-пробой, а не обещанием — см. "
                     "`chain_projection.validation.intermediate_not_checked`. "
                     "`projection_impossible` — сколько цепочек снято до фильтров: без "
                     "ролей или без определений проверяемый набор не построить, и "
                     "угадывать его нельзя."),
        },
        # `after_fix` — состояние **после лейк-фильтра** (S3f-fix): 8692 задачи.
        # Текущее состояние пула — `pool` и `chain_projection`; смешивать их в
        # одном числе нельзя: между ними стоит правка проекции цепочек.
        "after_fix": {"kept": S3F_FIX_POOL["lines"],
                      "residual_answer_in_prompt": audit["tasks_with_gold_in_prompt"],
                      "residual_by_form": audit["by_leak_form"],
                      "residual_measured_on": "текущий пул (см. `pool`)"},
        "rollback": {
            "command": ROLLBACK_CMD,
            "result": ("воспроизводит базу ровно: 9197 задач, sha256 "
                       f"{BASELINE_POOL['sha256'][:12]}… (проверено 16.09.2026)"),
        },
        "not_leak_but_measured": {
            "keyword_slugs_in_prompt": audit["keyword_slugs_in_prompt"],
            "why": ("keyword-ветка ищет в ответе термины определений, а не `keywords`: "
                    "slug-и в промпте для неё безразличны (см. verifier_accepts). "
                    "Число приведено, чтобы выбор формы правила был проверяем."),
        },
    }


def _by_key(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[str(r.get(key))] = out.get(str(r.get(key)), 0) + 1
    return dict(sorted(out.items()))


def _chain_projection_card(tasks: list[dict], exclusions: list[dict],
                           chain_stats: dict | None = None) -> dict:
    """Раздел карточки о правке проекции цепочек (S3f-fix-2).

    Отвечает на три вопроса числами, а не текстом: **сколько вернулось** (по типам,
    против базы, где не было ни одной цепочки), **сколько всё ещё отсеивается и
    почему** (поимённо по кодам, с примерами), и **чем подтверждено** (замер
    замороженным верификатором из `tools/probe_chain_projection.py`, артефакт —
    `evidence/s3f-fix-2-validation.json`).
    """
    by_type = _by_key([t for t in tasks if t.get("task_type") in CHAIN_TASK_TYPES],
                      "task_type")
    # Отсев цепочек — только записи с типом цепочки: код `prompt_leak` общий с
    # остальными типами, поэтому «сколько снято цепочек» иначе не отделить.
    chain_drops = [e for e in exclusions if str(e.get("task_type")) in CHAIN_TASK_TYPES]
    reasons: dict[str, int] = {}
    for e in chain_drops:
        reasons[e["reason_code"]] = reasons.get(e["reason_code"], 0) + 1
    leak_hits = [e for e in chain_drops if e["reason_code"] == "prompt_leak"]
    return {
        "rule": PROJECTION_RULES["chain_definition_terms"],
        "before": dict(S3F_FIX_POOL),
        "after": {"pool_lines": len(tasks), "sha256": None, "by_type_chains": by_type},
        "chains_returned": {
            "total": sum(by_type.values()), "by_type": by_type,
            "source": (chain_stats or {}).get("by_task_type", {}),
        },
        # Сверка: каждая цепочка источника либо в пуле, либо снята с названной
        # причиной. `unaccounted` — дыра между этими числами (цепочка, потерянная
        # фильтром пула без записи): ненулевое значение означало бы, что счёт
        # «вернулось / отсеивается» неполон. Считается от факта источника, а не от
        # константы: карточка остаётся верной и на тестовой фикстуре.
        "reconciliation": {
            "source_total": sum((chain_stats or {}).get("by_task_type", {}).values()),
            "returned": sum(by_type.values()),
            "excluded_named": sum(reasons.values()),
            "unaccounted": (sum((chain_stats or {}).get("by_task_type", {}).values())
                            - sum(by_type.values()) - sum(reasons.values())),
        },
        "still_excluded": {
            "total": sum(reasons.values()), "by_reason": dict(sorted(reasons.items())),
            "examples": [{"task_id": e["task_id"], "reason_code": e["reason_code"],
                          "reason": e["reason"]} for e in chain_drops[:5]],
            "why": ("`prompt_leak` здесь — совпадение **одного** термина определения "
                    "с текстом промпта (чаще всего со словом из имени соседнего "
                    "концепта-условия): правило «любая проверяемая строка в промпте — "
                    "лейк» не ослаблено, поэтому задача снимается консервативно. "
                    "Копирование промпта при этом верификатор не проходит — он "
                    "требует конъюнкцию всех терминов обоих концептов "
                    "(`conservative_not_measured`): «снято консервативно» и «награда "
                    "копируется» — разные вещи, и разница названа числом."),
        },
        "conservative_not_measured": {
            "dropped_by_single_term_coincidence": len(leak_hits),
            "prompt_copy_satisfies_verifier_in_pool":
                CHAIN_VALIDATION["prompt_copy_rewarded"]["after_fix"],
            "why": ("второе число — прямой замер (ответ = промпт, тот же замороженный "
                    "верификатор, что судит награду), снят пробой "
                    "`tools/probe_chain_projection.py`; в базе этой дельты "
                    "копирование награждалось у 500 цепочек, потому что проекция "
                    "проверяла концы (`S3F_FIX_POOL`)"),
        },
        # Ссылка на артефакт замера — байтами: карточка и проба должны говорить об
        # одном пуле и одном файле, иначе «подтверждено числами» — слово.
        "validation": {**CHAIN_VALIDATION,
                       "evidence_sha256": (sha256_file(Path(CHAIN_VALIDATION["evidence"]))
                                           if Path(CHAIN_VALIDATION["evidence"]).is_file()
                                           else None)},
        "rollback": {
            "command": ROLLBACK_CMD_CHAIN,
            "result": (f"воспроизводит базу этой дельты ровно: "
                       f"{S3F_FIX_POOL['lines']} задач, sha256 "
                       f"{S3F_FIX_POOL['sha256'][:12]}…, цепочек 0 (проверено "
                       f"прогоном 16.09.2026)"),
        },
    }


def leak_audit(tasks: list[dict], defs: dict[str, str]) -> dict:
    """Остаточный замер: у скольких задач пула проверяемый ответ лежит в промпте.

    Тем же правилом, что отбирает (`leak_form`), а не «похожим»: замер обязан
    отвечать на вопрос «пул чист?» ровно в той форме, в какой его чистили, иначе
    зелёный замер ничего не подтверждает. Ожидаемое значение — ноль; ненулевое
    означает, что фильтр и замер разошлись (и это видно в карточке, а не
    выясняется на фазе 2).

    Keyword-ветке для замера нужны определения концептов, а в пуле их нет
    (`_defs` — служебное поле конвертера, пайплайн берёт определения из индекса):
    ``defs`` — тот же индекс, что читал отбор, поэтому замер воспроизводит
    проверку, а не «доверяет» ей.

    Отдельно считаются цепочки, у которых проверяемый набор (термины определений
    концептов-условий) всё же встретился в промпте. Это не «пропущенный лейк»:
    правило применено, задача снята — счётчик нужен, чтобы число снятых цепочек
    читалось по причинам, а не одним итогом.

    Второй замер (`keyword_slugs_in_prompt`) — честная альтернатива, которую
    правило **не** использует: сколько keyword-задач называют свои `keywords`
    (slug-и) в промпте. Для keyword-ветки это не лейк: верификатор эти строки в
    ответе не ищет (он ищет термины определений) — см. `verifier_accepts`.
    Число приводится, чтобы выбор формы правила был проверяем, а не декларативен.
    """
    out: dict = {"tasks_with_gold_in_prompt": 0, "by_task_type": {}, "examples": [],
                 "by_leak_form": {}, "chains_checked_in_prompt": 0,
                 "keyword_slugs_in_prompt": 0}
    for t in tasks:
        prompt = str(t.get("prompt") or "")
        probe = t
        if t.get("task_type") in KEYWORD_TASK_TYPES:
            kws = [k for k in t.get("keywords", []) if k.lower() not in KEYWORD_DROP][:2]
            probe = {**t, "_defs": {k: defs.get(k, "") for k in kws}}
        form, detail = leak_form(probe, prompt)
        if form:
            out["tasks_with_gold_in_prompt"] += 1
            tt = str(t.get("task_type"))
            out["by_task_type"][tt] = out["by_task_type"].get(tt, 0) + 1
            out["by_leak_form"][form] = out["by_leak_form"].get(form, 0) + 1
            if len(out["examples"]) < 5:
                out["examples"].append({"task_type": tt, "source_env": t["source_env"],
                                        "source_task_id": t["source_task_id"],
                                        "leak_form": form, "detail": detail})
            continue
        if t.get("task_type") in CHAIN_TASK_TYPES:
            hits = [s for s in checked_strings(t) if checked_in_prompt(s, prompt)]
            if hits:
                out["chains_checked_in_prompt"] += 1
        if t.get("task_type") in KEYWORD_TASK_TYPES:
            kws = [k for k in t.get("keywords", []) if k.lower() not in KEYWORD_DROP]
            if any(checked_in_prompt(k, prompt) for k in kws[:2]):
                out["keyword_slugs_in_prompt"] += 1
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="S3f/ADR-021: пул ревизии v2 — верифицируемая часть сред E1–E8 "
                    "+ env_types, конверсия в формат пайплайна (источники не меняются).")
    ap.add_argument("--envs-root", default=DEFAULT_ENVS_ROOT,
                    help=f"каталог сред (~/library/rl_envs), по умолчанию {DEFAULT_ENVS_ROOT}")
    ap.add_argument("--env-types", default=DEFAULT_ENV_TYPES,
                    help=f"пул env_types, по умолчанию {DEFAULT_ENV_TYPES}")
    ap.add_argument("--sft", default=DEFAULT_SFT, help=f"SFT v12, по умолчанию {DEFAULT_SFT}")
    ap.add_argument("--eval", default=DEFAULT_EVAL, help=f"eval-набор, по умолчанию {DEFAULT_EVAL}")
    ap.add_argument("--v1", default=DEFAULT_V1,
                    help=f"действующий пул v1, по умолчанию {DEFAULT_V1}")
    ap.add_argument("--index", default=DEFAULT_INDEX,
                    help=f"индекс концептов, по умолчанию {DEFAULT_INDEX}")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"пул v2, по умолчанию {DEFAULT_OUT}")
    ap.add_argument("--card", default=DEFAULT_CARD, help=f"карточка, по умолчанию {DEFAULT_CARD}")
    ap.add_argument("--report", default=DEFAULT_REPORT,
                    help=f"отчёт об исключениях, по умолчанию {DEFAULT_REPORT}")
    ap.add_argument("--link", default=POOL_LINK,
                    help=f"симлинк пула в дереве кейса (AD-4), по умолчанию {POOL_LINK}")
    ap.add_argument("--no-report", action="store_true", help="не писать отчёт и симлинк")
    ap.add_argument("--dry-run", action="store_true", help="отбор без записи пула")
    ap.add_argument("--force", action="store_true", help="перезаписать существующий пул v2")
    ap.add_argument("--update-registry", action="store_true",
                    help="пересобрать task_count в envs.yaml по факту (бэкап "
                         f"{REGISTRY_BACKUP}); без флага реестр не меняется")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)

    for p, what in ((Path(args.sft), "--sft"), (Path(args.eval), "--eval"),
                    (Path(args.v1), "--v1"), (Path(args.env_types), "--env-types")):
        if not p.is_file():
            print(f"NOT-VERIFIED: {what}: файл не найден: {p}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
    if args.dry_run and args.update_registry:
        print("NOT-VERIFIED: --dry-run не пишет ни пул, ни реестр — "
              "--update-registry с ним несовместим", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    try:
        card, tasks, report = build(args)
    except NotVerified as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    except OSError as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    written = None
    if not args.dry_run:
        try:
            written = write_pool(Path(args.out), tasks, args.force)
        except FileExistsError as e:
            print(f"ОТКАЗ: {e}", file=sys.stderr)
            return EXIT_REFUSE
        card["pool"] = written
        card["pool"]["link"] = args.link if not args.no_report else None
    else:
        card["pool"] = {"path": args.out, "lines": len(tasks),
                        "sha256": hashlib.sha256(pool_text(tasks).encode()).hexdigest(),
                        "written": False}
    # Раздел о правке цепочек несёт тот же хеш, что и пул: «после» в нём — тот
    # самый артефакт, который записан, а не отдельный счёт.
    if card.get("chain_projection"):
        card["chain_projection"]["after"]["sha256"] = card["pool"]["sha256"]

    if not args.no_report and not args.dry_run:
        card["report"] = write_json(Path(args.report), report)
        write_not_a_run(Path(args.report).parent)
        # Цель симлинка — абсолютный путь на сетевом диске (AD-4): относительная
        # цель разрешалась бы от каталога пула и вела в никуда.
        ensure_pool_link(Path(args.link), str(Path(args.out).resolve()))

    registry_changes: list[str] = []
    if args.update_registry:
        facts = {s["id"]: card["envs"][s["id"]]["actual"] for s in ENV_SPECS}
        registry_changes = update_registry(
            Path(args.envs_root).expanduser() / REGISTRY_NAME, facts,
            Path(args.envs_root).expanduser() / REGISTRY_BACKUP)
        card["registry_rebuild"] = {"backup": str(
            Path(args.envs_root).expanduser() / REGISTRY_BACKUP),
            "changes": registry_changes}

    # Карточка — артефакт прогона (числа по средам, правило проекции, хеш пула),
    # а не служебный отчёт: пишется и при --no-report, кроме явного --dry-run.
    if not args.dry_run:
        write_json(Path(args.card), card)
    if args.json:
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return EXIT_OK

    c = card["counts"]
    print("== S3f-fix-2 / ADR-021 (16.09.2026): пул v2, проекция цепочек через "
          "содержание концептов-условий ==")
    print(f"источники: {Path(args.envs_root).expanduser()} + {args.env_types}")
    print()
    for env_id, e in card["envs"].items():
        mark = "✓" if e["kept"] else "·"
        tail = ""
        if e.get("kept_in_pool") is not None and e["kept_in_pool"] != e["kept"]:
            tail = f" (в пуле {e['kept_in_pool']})"
        print(f"  {mark} {env_id:<20} реестр {str(e['registry_count']):>6}  "
              f"факт {e['actual']:>6}  в пул {e['kept']:>6}{tail}  "
              f"исключено {e['excluded']:>5}"
              + (f"  — {e['excluded_reason']}" if e["excluded_reason"] else ""))
    print()
    for name, f in card["filters"].items():
        print(f"  фильтр {name:<16} исключено {f['excluded']:>4}  осталось {f['remaining']}")
    print()
    cp = card["chain_projection"]
    print(f"пул: {c['kept']} задач из {c['candidates']} кандидатов "
          f"(база этой дельты {cp['before']['lines']}, sha256 "
          f"{cp['before']['sha256'][:12]}…); "
          f"типы: {', '.join(f'{k} {v}' for k, v in c['by_task_type'].items())}")
    print(f"цепочки: вернулось {cp['chains_returned']['total']} "
          f"({', '.join(f'{k} {v}' for k, v in cp['chains_returned']['by_type'].items())} "
          f"из {', '.join(f'{k} {v}' for k, v in cp['chains_returned']['source'].items())}); "
          f"снято {cp['still_excluded']['total']} — "
          f"{', '.join(f'{k} {v}' for k, v in cp['still_excluded']['by_reason'].items())} "
          f"(консервативно: один термин совпал с промптом, а не «награда копируется»)")
    lf = card["leak_fix"]
    by_type = ", ".join(f"{k} {v}" for k, v in lf["excluded"]["by_task_type"].items())
    print(f"лейк-фильтр (то же правило, весь пул): снято {lf['excluded']['total']} задач "
          f"({', '.join(f'{k} {v}' for k, v in lf['excluded']['by_form'].items())}); "
          f"по типам: {by_type or 'нет'}")
    print(f"             S3f-fix снял тогда {lf['delta']['excluded_by_this_filter']} "
          f"(пул 9197 → {lf['after_fix']['kept']}); в базовой ревизии уже было "
          f"{lf['excluded']['already_in_baseline']['total']}")
    print(f"             осталось с ответом в промпте: "
          f"{lf['after_fix']['residual_answer_in_prompt']}")
    audit = card["checks"]["gold_in_prompt_audit"]
    print(f"замер (тем же правилом): ответ в промпте у "
          f"{audit['tasks_with_gold_in_prompt']} задач пула; "
          f"keywords (не проверяемые строки) в промпте — у "
          f"{audit['keyword_slugs_in_prompt']} keyword-задач")
    if written:
        print(f"записан: {written['path']} — строк {written['lines']}, "
              f"sha256 {written['sha256'][:12]}…")
        print(f"карточка: {args.card}")
        print(f"отчёт об исключениях: {args.report}")
        print(f"симлинк (AD-4): {args.link}")
    if registry_changes:
        print()
        print(f"реестр: {Path(args.envs_root).expanduser() / REGISTRY_NAME} "
              f"(бэкап {REGISTRY_BACKUP})")
        for ch in registry_changes:
            print(f"  - {ch}")
    print()
    print(f"BUILD_REV_ENVS OK: {c['kept']} задач, "
          f"исключённые среды — {', '.join(c['excluded_envs']) or 'нет'}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
