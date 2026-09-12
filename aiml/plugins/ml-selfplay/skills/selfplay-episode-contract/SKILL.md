---
name: selfplay-episode-contract
description: >-
  Контракт selfplay-эпизода: как устроена генерация обучающих диалогов между
  агентом-средой (ENV-AGENT) и обучаемой моделью (SOLVER) и как награда
  остаётся внешней по отношению к обоим. Разбирает роли, инвариант «награду
  назначает только внешний verifier, ни один агент», формат эпизода JSONL
  (episode_id / turn / actor / type / content / grounding), машину состояний
  INIT→TASK→SUBMIT→VERIFY→REWARD→LOG с ABORT-веткой, commit-reveal золотого
  эталона по sha256, обязанности и запреты ENV-AGENT, режимы M1–M5, сэмплер
  по леджеру frontier и детекторы вырождения (коллапс слагов, дрейф сложности,
  сговор env↔solver, length-bias, шаблонный коллапс, нарушение персоны).
  Отдельно — связь с fitness/rubric-дисциплиной харнесса: внешний verifier =
  рубрика харнесса (взвешенный итог по критериям, изоляция судьи, цитаты-
  свидетельства, k-сэмпловая медиана, метки unstable/evidence_not_found), а
  веса judge_weight / verifiable_weight — аналог весов критериев рубрики.
  ВСЕГДА используй этот навык, когда пользователь: «selfplay-эпизод»,
  «контракт эпизода», «ENV-AGENT», «SOLVER», «роли в selfplay», «формат
  JSONL эпизода», «кто назначает reward», «внешний verifier», «commit-reveal»,
  «сгенерировать selfplay-данные», «режимы M1–M5», «леджер frontier», «сговор
  env и solver», «детекторы вырождения selfplay», «агент сам себе ставит
  оценку». Грундится в источнике `library/selfplay/env_agent/ENV_CONTRACT.md`
  (S0, 2026-07-10) и реестре сред `library/rl_envs/envs.yaml` — не выдумывай
  поля и роли, цитируй контракт.
---

# Контракт selfplay-эпизода

Один инвариант держит всю конструкцию: **награду назначает внешний verifier,
а не участники эпизода.** ENV-AGENT генерирует задачи и ведёт диалог, SOLVER
отвечает — но ни один из них не ставит финальный балл и даже не намекает на
него. Если этот инвариант нарушен, selfplay вырождается в самопохвалу
(env подыгрывает solver'у, solver учится на собственных оценках), и данные
становятся непригодны для RL. Всё остальное — формат, машина состояний,
запреты — обслуживает именно это.

Источник: `~/library/selfplay/env_agent/ENV_CONTRACT.md`
(Version S0, 2026-07-10, Zone 8 selfplay; статус «ready for dry-run»).
Реестр сред и веса: `~/library/rl_envs/envs.yaml`.

## 0. Роли

| Роль | Кто это | Что делает |
|---|---|---|
| **ENV-AGENT** | агент-среда (внешняя batch-джоба, ~30 параллельных агентов) | генерирует задачи, ведёт состояние диалога, даёт промежуточный отклик, **грундует каждый факт** в базе концептов Ариадны |
| **SOLVER** | обучаемая модель | получает задачи, отвечает, **оценивается** |
| **VERIFIER** | внешний конвейер (зона 6) | **единственный** источник финальной награды |

Формулировка контракта дословно: *«NEITHER agent assigns final reward. Reward is
computed by the external verifier pipeline (zone 6).»*

## 1. Формат эпизода (JSONL)

Одна строка — один ход. Путь: `episodes/<date>/<mode>/<episode_id>.jsonl`.

Поля строки: `episode_id`, `turn` (монотонный), `actor` (`env` | `solver` |
`verifier`), `type`, `content`, `grounding`. Пример полного эпизода:

```json
{"episode_id": "uuid", "turn": 0, "actor": "env", "type": "commit",
 "content": {"sha256_gold": "abc123..."},
 "grounding": {"slugs": ["verifier_gated_reward"], "paper_ids": ["arxiv.2501.12345"]}}

{"episode_id": "uuid", "turn": 1, "actor": "env", "type": "task",
 "content": "Дай определение Verifier-Gated Reward...",
 "grounding": {"slugs": ["verifier_gated_reward"], "paper_ids": ["arxiv.2501.12345"]}}

{"episode_id": "uuid", "turn": 2, "actor": "solver", "type": "answer",
 "content": "Verifier-Gated Reward это..."}

{"episode_id": "uuid", "turn": 3, "actor": "env", "type": "feedback",
 "content": "Уточни: что на входе?"}

{"episode_id": "uuid", "turn": 4, "actor": "solver", "type": "answer",
 "content": "На вход подаётся..."}

{"episode_id": "uuid", "turn": 5, "actor": "verifier", "type": "final_reward",
 "content": {"gate": true, "components": {"latex": 0.9, "symbols": 0.8}, "total": 0.85}}
```

Наблюдения по типам ходов:
- `type` для `actor=env`: `commit`, `task`, `feedback`.
- `type` для `actor=solver`: `answer` (контракт допускает и `question` —
  цикл `(solver: answer | question)*`).
- `type=final_reward` пишет **только** `actor=verifier`, и только он несёт
  итоговый балл (`gate`, `components`, `total`).

`grounding` — обязательное поле у ходов ENV-AGENT: `{slugs, paper_ids}` из
базы Ариадны. Это то, что делает задачу проверяемой, а не выдуманной.

## 2. Машина состояний

```
INIT → [env: select mode + grounding + commit SHA256(gold)] → TASK
TASK → (solver: answer | question)* → SUBMIT
SUBMIT → VERIFY (external) → REWARD → LOG → END

Violations → ABORT (logged to degeneracy_flags.jsonl)
```

Ключевое: переход `SUBMIT → VERIFY → REWARD` вынесен за пределы эпизода —
это внешний конвейер. Любое нарушение контракта не молчаливо проходит, а
пишется в `degeneracy_flags.jsonl` и завершает эпизод через `ABORT`.

## 3. Commit-reveal (защита эталона от подгонки)

До первого хода solver'а ENV-AGENT пишет в лог `sha256(golden_reference)`.
Сам золотой эталон лежит в **скрытом поле**, открывается только на стадии
VERIFY; verifier сверяет хеш. Смысл: эталон нельзя переписать после того, как
увидел ответ solver'а (иначе env «подгоняет» gold под ответ). Хеш в открытом
логе одновременно фиксирует момент, до которого эталон был неизменен.

## 4. Обязанности ENV-AGENT (contract §4)

1. **Грундить каждый факт** — каждое `grounding` ссылается на конкретные
   `slugs`/`paper_ids` базы Ариадны.
2. **Be stateless между ходами** — вход = полный лог эпизода, выход = следующий
   ход. Это позволяет перезапускать/распараллеливать эпизоды без скрытого
   состояния.
3. **Соблюдать бюджеты** — максимум ходов и токенов на ход заданы в конфиге
   режима.
4. **Использовать персону** — стиль, строгость, язык из `personas/<persona>.md`.

## 5. Запреты ENV-AGENT (contract §5)

- Назначать или **намекать** на финальную награду.
- Видеть другие сэмплы solver'а из той же GRPO-группы.
- Генерировать задачи вне базы Ариадны.
- Менять золотой эталон после commit.
- Раскрывать эталон solver'у до стадии VERIFY.

Запрет «не видеть другие сэмплы группы» — это изоляция от утечки: если env
видит соседние попытки, он неосознанно выравнивает задачи под них, и
group-relative advantage в GRPO теряет смысл.

## 6. Реестр режимов (contract §6)

| Mode | Файл | Описание |
|------|------|----------|
| M1 | `modes/proposer_solver.md` | Task proposer + solver (основной асимметричный режим) |
| M2 | `modes/paper_persona.md` | Env играет статью, solver берёт интервью |
| M3 | `modes/examiner.md` | Адаптивный экзамен по теме |
| M4 | `modes/reviewer_author.md` | Reviewer критикует артефакт solver'а |
| M5 | `modes/socratic_tutor.md` | Сократический диалог для обучения |

## 7. Сэмплер (contract §7, `scripts/_sample_episodes.py` — строится в S2)

1. Читать `ledger/frontier.json` — success rate по паре slug×mode.
2. Выбор режима: **M1 50%, M2 15%, M3 15%, M4 15%, M5 5%**.
3. Выбор концептов: **70% frontier** (успех 20–80%), **15% weak** (<20%),
   **15% random**.
4. Назначить персону, seed, бюджеты.
5. Запустить эпизод; после VERIFY обновить леджер.

Логика «70% frontier» — это curriculum: основной поток задач идёт из зоны, где
модель учится (ещё не решает, но уже близко), а не из уже решённого или
безнадёжного.

## 8. Анти-дегенерация (contract §8, детекторы S3+)

| Детектор | Что ловит |
|---|---|
| Collapse detector | env всегда выбирает одни и те же слаги |
| Difficulty drift | задачи становятся легче со временем |
| Collusion detector | корреляция reward'ов env и solver |
| Length bias | длинные ответы получают выше балл |
| Template collapse | solver выдаёт шаблонный текст |
| Persona violation | env игнорирует назначенную персону |

Нарушения логируются в `degeneracy_flags.jsonl` (см. ABORT-ветку §2).

## 9. Связь с fitness/rubric-дисциплиной харнесса

Это главный мост «контракт → харнесс». Инвариант «награду назначает внешний
verifier» — это ровно тот же принцип, что уже зашит в `src/rubric.rs` и
`src/control.rs` (`fitness functions`): **оценку выносит независимый судья по
заранее зафиксированному критерию, а не тот, кого оценивают.**

Соответствия (грунт — `docs/rubrics_and_benchmarks.md`, `src/rubric.rs`,
`src/control.rs`, `library/rl_envs/envs.yaml`):

| Selfplay-контракт | Харнесс |
|---|---|
| Внешний verifier (зона 6), `type=final_reward` | LLM-судья / рубрика (`rubric::evaluate_with_options`) — судья «независимый рецензент», не проектировал оцениваемое |
| `content.components{a,b}` + `total` | `Criterion` с `weight` → `weighted_total` (взвешенный итог по критериям) |
| Анти-намекающий запрет: агент не ставит балл | Судья изолирован от prompt injection (маркеры `=== НАЧАЛО/КОНЕЦ ===`), каждая оценка ≥2 обязана нести дословную цитату-свидетельство |
| `grounding{slugs,paper_ids}` у каждого хода env | evidence-подход: балл без опоры на текст запрещён; `evidence_not_found` исключает критерий из итога |
| k-сэмплов групповая изоляция, стабильность reward | `judge.samples` (по умолчанию 3), итог — медиана, σ > `judge.unstable_stdev` → метка `unstable` |
| `judge_weight` / `verifiable_weight` (envs.yaml) | веса критериев рубрики: смешивание судейской и механически проверяемой части (E3 classify: 0.0/1.0 — чистый verifiable; E6 contrast: 0.7/0.3 — судейская) |
| `k_samples` среды (envs.yaml: 4–12) | `[judge].samples` рубрики — число независимых прогонов судьи |
| ABORT → `degeneracy_flags.jsonl` | fitness-находки `control.rs` (`must_contain` / `must_not_contain` / `broken_ad_ref` / `max_age`) |

Практический вывод для ML-исследователя: **строя selfplay-эпизод, бери
verifier из rubric-дисциплины харнесса, а не пиши свой оценщик внутри
env-агента.** Веса — из `envs.yaml` (E1_define 0.5/0.5, E3_classify 0.0/1.0,
E6_contrast 0.7/0.3 и т.д.), критерии — из YAML-рубрик харнесса
(`assets/rubrics/*.yaml`, схема в `docs/rubrics_and_benchmarks.md`). Числа
весов и k-сэмплов не выдумывай — читай `envs.yaml` и конфиг `[judge]`.

Смежная дисциплина записи артефактов: скилл не попадает в
`plugins/<plugin>/skills/`, если у него нет триггера и стабильной процедуры
(fitness-правило харнесса, см. `aiml/notes/library-bridge.md`, P0 «гейт md vs
skill»). Это тот же принцип внешней проверки, применённый к знанию: агент не
объявляет собственный вывод скиллом — решает гейт.

## Чек-лист (когда проектируешь или ревьюишь selfplay-эпизод)

- [ ] Финальный балл пишет **только** `actor=verifier`; ни ENV-AGENT, ни SOLVER балла не ставят и не намекают на него
- [ ] `commit` со `sha256(gold)` есть **до** первого хода solver'а; эталон скрыт до VERIFY
- [ ] У каждого хода ENV-AGENT непустое `grounding{slugs,paper_ids}` из базы Ариадны
- [ ] ENV-AGENT stateless между ходами (вход — полный лог, выход — следующий ход)
- [ ] Заданы бюджеты ходов/токенов и персона (из конфига режима)
- [ ] Режим выбран по распределению сэмплера; концепты — 70% frontier / 15% weak / 15% random
- [ ] ENV-AGENT не видит соседние сэмплы GRPO-группы
- [ ] Нарушения уходят в `degeneracy_flags.jsonl` (ABORT), а не молча
- [ ] Verifier переиспользует rubric-дисциплину харнесса; веса/k — из `envs.yaml`, не выдуманы

## Источники (грундовка)

- `~/library/selfplay/env_agent/ENV_CONTRACT.md` — контракт целиком (S0, 2026-07-10)
- `~/library/rl_envs/envs.yaml` — среды E1_define…E8, веса `judge_weight`/`verifiable_weight`, `k_samples`
- `~/library/rl_envs/verifiers/` (`v_define.py`, `v_extract.py`, `common.py::verify_*`) — verifier-паттерны
- Харнесс: `docs/rubrics_and_benchmarks.md` (схема YAML рубрики, LLM-судья, k-сэмплы, evidence), `src/rubric.rs` (`Criterion`/`Rubric`/`CriterionScore`, `weighted_total`, метки `unstable`/`evidence_not_found`), `src/control.rs` (fitness functions), `assets/rubrics/*.yaml`
- Мост: `~/spine-aiml/aiml/notes/library-bridge.md` (P2 «Контракт selfplay-эпизода»; P0 «гейт md vs skill»)
