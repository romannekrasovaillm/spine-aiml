#!/usr/bin/env bash
# Тесты стражей S0/S1: у каждого инструмента проверяются и зелёный, и красный путь.
#
# Принцип: гейт, который не падал ни на одном негативном сценарии, не проверен.
# Поэтому на каждый инструмент есть (а) положительный случай, (б) синтетическое
# нарушение, (в) отсутствие входа → NOT-VERIFIED (exit 2) или вакуумный exit 0
# там, где он объявлен в контракте.
#
# Фикстуры живут в каталоге с **уникальным префиксом дельты** и удаляются на выходе:
# в дерево кейса ничего не пишется, данные не копируются (AD-4). Префикс, а не
# безымянный `mktemp -d`, потому что общий `/tmp` делят параллельные дельты: назван
# случай (ADR-021, 16.09.2026), когда файлы приватного mktemp-каталога исчезли из-под
# чужого прогона и секция тестов дала ложные падения.
#
# Запуск: bash tools/tests/run_tool_tests.sh

set -uo pipefail

CASE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$CASE_ROOT"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/laguna-s3f-fix-2-XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
failures=()

# expect_exit <ожидаемый код> <описание> <команда...>
expect_exit() {
  local want="$1" what="$2"; shift 2
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [ "$rc" -eq "$want" ]; then
    PASS=$((PASS + 1)); printf '  ok   %-58s (exit %s)\n' "$what" "$rc"
  else
    FAIL=$((FAIL + 1)); failures+=("$what: ожидался exit $want, получен $rc")
    printf '  FAIL %-58s (exit %s, ожидался %s)\n' "$what" "$rc" "$want"
    printf '%s\n' "$out" | sed 's/^/       | /' | head -6
  fi
}

expect_contains() {
  local needle="$1" what="$2"; shift 2
  local out
  out="$("$@" 2>&1)"
  if printf '%s' "$out" | grep -qF -- "$needle"; then
    PASS=$((PASS + 1)); printf '  ok   %-58s (содержит «%s»)\n' "$what" "$needle"
  else
    FAIL=$((FAIL + 1)); failures+=("$what: нет «$needle» в выводе")
    printf '  FAIL %-58s (нет «%s»)\n' "$what" "$needle"
    printf '%s\n' "$out" | sed 's/^/       | /' | head -6
  fi
}

echo "== фикстуры =="
mkdir -p "$TMP/empty-contour" "$TMP/static-contour" "$TMP/hyg" "$TMP/runs" "$TMP/runsgen" "$TMP/fakebin"

# eval/train для детектора пересечения SFT↔RL: пул RL повторяется в SFT дословно
cat > "$TMP/sft_ov.jsonl" <<'JSONL'
{"messages": [{"role": "user", "content": "Объясни связь между концептами 'alpha_x' и 'beta_y'. Как они соотносятся?"}, {"role": "assistant", "content": "<think>…</think>"}]}
{"messages": [{"role": "user", "content": "Найди концепт о регуляризации."}, {"role": "assistant", "content": "-"}]}
JSONL
cat > "$TMP/rl_ov.jsonl" <<'JSONL'
{"task_type": "explain_relation", "prompt": "Объясни   связь между концептами 'alpha_x' и 'beta_y'. Как они соотносятся?", "expected_relation": "related"}
JSONL
cat > "$TMP/rl_clean.jsonl" <<'JSONL'
{"task_type": "explain_relation", "prompt": "Объясни связь между концептами 'gamma_z' и 'delta_w'. Как они соотносятся?", "expected_relation": "related"}
JSONL
# Пул из трёх задач для сборки пула ревизии: строка 1 дословно (с точностью до
# пробелов) повторяет промпт SFT, строки 2-3 — нет.
cat > "$TMP/rl_pool.jsonl" <<'JSONL'
{"task_type": "explain_relation", "prompt": "Объясни   связь между концептами 'alpha_x' и 'beta_y'. Как они соотносятся?", "reward": 1.0}
{"task_type": "explain_relation", "prompt": "Объясни связь между концептами 'gamma_z' и 'delta_w'. Как они соотносятся?", "reward": 1.0}
{"task_type": "find_concept", "prompt": "Найди концепт о батч-нормализации.", "reward": 1.0}
JSONL

# contour без кэшей → NOT-VERIFIED
# contour со скриптом претокенизации без add_special_tokens → класс дефекта 17.08
cat > "$TMP/static-contour/pretokenize_bad.py" <<'PY'
SPECIAL_TOKENS = ["<think>", "</think>"]
def build(tok, docs):
    return [tok.encode(d, add_special_tokens=False) for d in docs]
PY
cat > "$TMP/static-contour/pretokenize_good.py" <<'PY'
SPECIAL_TOKENS = ["<think>", "</think>"]
def build(tok, docs):
    tok.add_special_tokens({"additional_special_tokens": SPECIAL_TOKENS})
    return [tok.encode(d, add_special_tokens=False) for d in docs]
PY
ln -sfn /home/user/gb10-shared "$TMP/shared"
ln -sfn /home/user/gb10-shared/datasets "$TMP/hyg/good_link"
ln -sfn /etc/hostname "$TMP/hyg/outside_link"
ln -sfn "$TMP/nowhere" "$TMP/hyg/broken_link"
head -c 55000000 /dev/zero > "$TMP/hyg/big_copy.bin"

# eval/train: синтетическая утечка обоих классов
cat > "$TMP/ev_leak.jsonl" <<'JSONL'
{"task_type": "ood_pair", "prompt": "Назови slug концепта leaky_slug_alpha и объясни", "expected_slugs": ["leaky_slug_alpha"]}
{"task_type": "find_concept", "prompt": "Что такое Трансформер?  \n Опиши.", "expected_slugs": ["transformer_arch"]}
JSONL
cat > "$TMP/tr_leak.jsonl" <<'JSONL'
{"messages": [{"role": "user", "content": "Что такое   трансформер? Опиши."}, {"role": "assistant", "content": "-"}]}
JSONL
cat > "$TMP/ev_clean.jsonl" <<'JSONL'
{"task_type": "find_concept", "prompt": "Что такое batch normalization?", "expected_slugs": ["batch_normalization"]}
JSONL
cat > "$TMP/tr_clean.jsonl" <<'JSONL'
{"messages": [{"role": "user", "content": "Объясни градиентный бустинг."}, {"role": "assistant", "content": "-"}]}
JSONL

# пайплайн с судьёй внутри награды
python3 - "$TMP/pipe_bad.py" <<'PY'
import sys, pathlib
src = pathlib.Path("/home/user/gb10-shared/laguna_pipeline_v8.py").read_text(encoding="utf-8")
src = src.replace("def compute_reward(rollout, n_min_steps=None):",
                  "def compute_reward(rollout, n_min_steps=None):\n    _x = glm_judge('a', 'b', '')")
pathlib.Path(sys.argv[1]).write_text(src, encoding="utf-8")
PY

echo
echo "== 1. check_special_tokens.py (C-008) =="
expect_exit 0 "реальный кэш v12r: дефолт — политика chat-format (ADR-004)" \
  python3 tools/check_special_tokens.py
expect_exit 0 "реальный кэш v12r: политика chat-format явным флагом" \
  python3 tools/check_special_tokens.py --occurrences chat-format
expect_exit 1 "реальный кэш v12r: буквальная политика all8 — разовая проверка" \
  python3 tools/check_special_tokens.py --occurrences all8
expect_contains "policy:    occurrences=chat-format" "политика печатается в отчёте" \
  python3 tools/check_special_tokens.py
expect_exit 0 "статика пути претокенизации (контур v12r)" \
  python3 tools/check_special_tokens.py --contour /home/user/gb10-shared --contour-only
expect_exit 1 "статика: скрипт без add_special_tokens (дефект 17.08)" \
  python3 tools/check_special_tokens.py --contour "$TMP/static-contour" --contour-only
expect_exit 2 "кэшей нет → NOT-VERIFIED" \
  python3 tools/check_special_tokens.py --contour "$TMP/empty-contour"
expect_exit 2 "--dataset указывает на несуществующий файл" \
  python3 tools/check_special_tokens.py --dataset "$TMP/nope.npy"
expect_exit 0 "литерал \${LAGUNA_TOK_CACHE} → автоопределение, не падение" \
  python3 tools/check_special_tokens.py --dataset '${LAGUNA_TOK_CACHE}'
expect_exit 1 "литерал \${LAGUNA_TOK_CACHE} + all8 → автоопределение и красный" \
  python3 tools/check_special_tokens.py --dataset '${LAGUNA_TOK_CACHE}' --occurrences all8
expect_contains "SPECIAL_TOKENS OK" "вывод «SPECIAL_TOKENS OK» при chat-format" \
  python3 tools/check_special_tokens.py --occurrences chat-format

echo
echo "== 2. check_eval_leakage.py (C-009) =="
expect_exit 0 "реальный eval_ood_clean ↔ sft_train_v12" \
  python3 tools/check_eval_leakage.py --eval datasets/eval_ood_clean.jsonl --train datasets/sft_train_v12.jsonl
expect_exit 1 "синтетика: slug в промпте + пересечение с train" \
  python3 tools/check_eval_leakage.py --eval "$TMP/ev_leak.jsonl" --train "$TMP/tr_leak.jsonl"
expect_contains "гейт A" "нарушение гейта A названо полем" \
  python3 tools/check_eval_leakage.py --eval "$TMP/ev_leak.jsonl" --train "$TMP/tr_leak.jsonl"
expect_exit 0 "синтетика без утечек" \
  python3 tools/check_eval_leakage.py --eval "$TMP/ev_clean.jsonl" --train "$TMP/tr_clean.jsonl"
expect_exit 2 "eval-файла нет → NOT-VERIFIED" \
  python3 tools/check_eval_leakage.py --eval "$TMP/nope.jsonl" --train "$TMP/tr_clean.jsonl"
expect_exit 2 "train-файла нет → NOT-VERIFIED" \
  python3 tools/check_eval_leakage.py --eval "$TMP/ev_clean.jsonl" --train "$TMP/nope.jsonl"

echo
echo "== 3. check_sft_rl_overlap.py (S1 / AD-7) =="
expect_exit 1 "реальные пулы v12: 39 совпадений из 44 949 (замер дельты)" \
  python3 tools/check_sft_rl_overlap.py --no-evidence
expect_contains "39 из 44949" "число совпадений печатается" \
  python3 tools/check_sft_rl_overlap.py --no-evidence
expect_exit 1 "синтетика: промпт SFT дословно повторён в RL" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_ov.jsonl" --no-evidence
expect_contains "line:1" "совпадение названо идентификатором задачи" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_ov.jsonl" --no-evidence
expect_exit 0 "синтетика: пересечения нет" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_clean.jsonl" --no-evidence
expect_exit 1 "кросс-проверка известного замера сошлась (39)" \
  python3 tools/check_sft_rl_overlap.py --no-evidence --expect-matched 39
expect_contains "известно 39, получено 39 — совпало" "кросс-проверка печатается" \
  python3 tools/check_sft_rl_overlap.py --no-evidence --expect-matched 39
expect_exit 1 "расхождение с известным замером → сигнал, не тихий «OK»" \
  python3 tools/check_sft_rl_overlap.py --no-evidence --expect-matched 7
expect_contains "РАСХОЖДЕНИЕ с известным замером" "расхождение названо явно" \
  python3 tools/check_sft_rl_overlap.py --no-evidence --expect-matched 7
expect_exit 0 "кросс-проверка чистого пула: ожидалось 0, получено 0" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_clean.jsonl" \
    --no-evidence --expect-matched 0
expect_exit 2 "SFT-файла нет → NOT-VERIFIED" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/nope.jsonl" --rl "$TMP/rl_ov.jsonl" --no-evidence
expect_exit 2 "RL-файла нет → NOT-VERIFIED" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/nope.jsonl" --no-evidence
echo '{"stage": "S1", "eval_set": {"tasks": 192}}' > "$TMP/ev_merge.json"
expect_exit 1 "запись evidence с сохранением чужих полей" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_ov.jsonl" \
    --evidence "$TMP/ev_merge.json"
expect_exit 0 "поле sft_rl_overlap добавлено, stage не затёрт" \
  python3 - "$TMP/ev_merge.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["stage"] == "S1", "чужое поле затёрто"
ov = d["sft_rl_overlap"]
assert ov["sft_matched_prompts"] == 1 and ov["sft_examples"] == 2, ov
assert ov["rl_matched_tasks"] == 1, ov
sys.exit(0)
PY
printf '{oops' > "$TMP/ev_broken.json"
expect_exit 2 "невалидный evidence-файл не перезаписывается" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_ov.jsonl" \
    --evidence "$TMP/ev_broken.json"
expect_exit 1 "допуск 0 (по умолчанию) на пуле с совпадением — сигнал" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_ov.jsonl" \
    --no-evidence --max-matched 0
expect_exit 0 "допуск 1: известный замер как порог, не регресс (ADR-007 п.2)" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_ov.jsonl" \
    --no-evidence --max-matched 1
expect_contains "в пределах допуска" "вердикт допуска не читается как «пул чист»" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_ov.jsonl" \
    --no-evidence --max-matched 1
expect_exit 1 "рост пересечения выше допуска — красный, а не «в пределах»" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_ov.jsonl" \
    --no-evidence --max-matched 0
expect_contains "строки RL-пула под исключение" "задачи под исключение названы номерами строк" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_ov.jsonl" \
    --no-evidence
expect_exit 2 "отрицательный допуск — NOT-VERIFIED, а не молчаливый проход" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_ov.jsonl" \
    --no-evidence --max-matched -1

# История замеров: перемер на другом пуле не затирает предыдущий (провенанс
# evidence/s1-data-audit.json — общий аудит данных кейса, а не файл одной дельты).
echo '{"sft_rl_overlap": {"rl": "pool-A.jsonl", "sft_matched_prompts": 39}, "stage": "S1"}' \
  > "$TMP/ev_hist.json"
expect_exit 0 "первый замер в истории (пул без совпадений)" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_clean.jsonl" \
    --evidence "$TMP/ev_hist.json"
expect_exit 0 "предыдущий замер сохранён в sft_rl_overlap_history" \
  python3 - "$TMP/ev_hist.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["sft_rl_overlap"]["sft_matched_prompts"] == 0, d["sft_rl_overlap"]
h = d.get("sft_rl_overlap_history", [])
assert len(h) == 1 and h[0]["sft_matched_prompts"] == 39, h
sys.exit(0)
PY
cp "$TMP/ev_hist.json" "$TMP/ev_hist_before.json"
expect_exit 0 "повторный тот же замер историю не растит (идемпотентность)" \
  python3 tools/check_sft_rl_overlap.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_clean.jsonl" \
    --evidence "$TMP/ev_hist.json"
expect_exit 0 "history не изменилась при повторном прогоне" \
  diff -q "$TMP/ev_hist_before.json" "$TMP/ev_hist.json"

echo
echo "== 4. check_symlink_hygiene.sh (C-011) =="
expect_exit 0 "дерево кейса без копий данных" \
  bash tools/check_symlink_hygiene.sh .
expect_contains "SYMLINK HYGIENE OK" "печатается вердикт" \
  bash tools/check_symlink_hygiene.sh .
expect_exit 1 "синтетика: копия >50 МБ, симлинки наружу и битый" \
  bash tools/check_symlink_hygiene.sh "$TMP/hyg" --shared "$TMP/shared"
expect_exit 2 "корня нет → NOT-VERIFIED" \
  bash tools/check_symlink_hygiene.sh "$TMP/nowhere"

echo
echo "== 5. check_judge_isolation.py (C-013) =="
expect_exit 0 "реальный пайплайн v8: судья только на eval" \
  python3 tools/check_judge_isolation.py --pipeline laguna_pipeline_v8.py
expect_contains "judge isolated: reward path clean" "вердикт изоляции" \
  python3 tools/check_judge_isolation.py --pipeline laguna_pipeline_v8.py
expect_exit 1 "синтетика: glm_judge внутри compute_reward" \
  python3 tools/check_judge_isolation.py --pipeline "$TMP/pipe_bad.py"
expect_exit 2 "пайплайна нет → NOT-VERIFIED" \
  python3 tools/check_judge_isolation.py --pipeline "$TMP/nope.py"
expect_exit 1 "eval-функция не найдена → изоляция недоказуема" \
  python3 tools/check_judge_isolation.py --eval-func no_such_eval_func

echo
echo "== 6. check_run_manifest.py (C-012) =="
expect_exit 0 "прогонов нет → вакуумная истина" \
  python3 tools/check_run_manifest.py --runs "$TMP/runs"
expect_contains "no runs yet" "вакуумный случай назван явно" \
  python3 tools/check_run_manifest.py --runs "$TMP/runs"
mkdir -p "$TMP/runs/r1" "$TMP/runs/r2"
echo '{"seed": 42}' > "$TMP/runs/r1/run_manifest.json"
expect_exit 1 "манифест без обязательных полей" \
  python3 tools/check_run_manifest.py --runs "$TMP/runs"
expect_contains "нет обязательного поля 'dataset_sha256'" "назван недостающий пиннинг" \
  python3 tools/check_run_manifest.py --runs "$TMP/runs"
expect_exit 1 "каталог прогона без манифеста" \
  python3 tools/check_run_manifest.py --runs "$TMP/runs"
expect_exit 2 "--runs указывает на файл → NOT-VERIFIED" \
  python3 tools/check_run_manifest.py --runs "$TMP/tr_clean.jsonl"
# не-прогон: каталог объявляет себя маркером NOT_A_RUN (пул ревизии, C-017)
rm -rf "$TMP/runs/r1" "$TMP/runs/r2"
mkdir -p "$TMP/runs/rev-pool"
printf 'Каталог пула, не прогон\n' > "$TMP/runs/rev-pool/NOT_A_RUN"
expect_exit 0 "каталог с маркером NOT_A_RUN не считается прогоном" \
  python3 tools/check_run_manifest.py --runs "$TMP/runs"
expect_contains "пропущен (не прогон)" "пропуск назван в отчёте, а не молча" \
  python3 tools/check_run_manifest.py --runs "$TMP/runs"
expect_contains "Каталог пула, не прогон" "причина из маркера печатается" \
  python3 tools/check_run_manifest.py --runs "$TMP/runs"
echo '{"seed": 42}' > "$TMP/runs/rev-pool/run_manifest.json"
expect_exit 1 "маркер + манифест — противоречие: красное, а не «пропустим»" \
  python3 tools/check_run_manifest.py --runs "$TMP/runs"
expect_contains "за маркером" "противоречие маркер/манифест названо" \
  python3 tools/check_run_manifest.py --runs "$TMP/runs"
rm -f "$TMP/runs/rev-pool/run_manifest.json"
expect_exit 0 "остались только не-прогоны: прогонов нет, нарушений нет" \
  python3 tools/check_run_manifest.py --runs "$TMP/runs"

echo
echo "== 7. write_run_manifest.py (C-012) =="
export LAGUNA_IMAGE="test-image:cu126"
expect_exit 0 "генерация манифеста" \
  python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/smoke-1" \
    --dataset datasets/eval_ood_clean.jsonl --base-model Qwen/Qwen2.5-0.5B \
    --pipeline laguna_pipeline_v8.py --seed 42 --stages cpt=done,eval=pending \
    --relative-to "$CASE_ROOT"
expect_exit 0 "идемпотентность: те же входы не перезатирают" \
  python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/smoke-1" \
    --dataset datasets/eval_ood_clean.jsonl --base-model Qwen/Qwen2.5-0.5B \
    --pipeline laguna_pipeline_v8.py --seed 42 --stages cpt=done,eval=pending \
    --relative-to "$CASE_ROOT"
expect_exit 1 "расхождение без --force → отказ" \
  python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/smoke-1" \
    --dataset datasets/eval_ood_clean.jsonl --base-model Qwen/Qwen2.5-0.5B \
    --pipeline laguna_pipeline_v8.py --seed 7 --stages cpt=done \
    --relative-to "$CASE_ROOT"
expect_exit 0 "сгенерированный манифест проходит стража" \
  python3 tools/check_run_manifest.py --runs "$TMP/runsgen"
expect_exit 2 "образ не задан и \$LAGUNA_IMAGE пуст → NOT-VERIFIED" \
  env -u LAGUNA_IMAGE python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/x" \
    --dataset "$TMP/ev_clean.jsonl" --base-model m --pipeline laguna_pipeline_v8.py \
    --seed 1 --stages cpt
expect_exit 2 "датасета нет → NOT-VERIFIED" \
  python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/x" \
    --dataset "$TMP/nope.jsonl" --base-model m --pipeline laguna_pipeline_v8.py \
    --seed 1 --stages cpt
expect_exit 0 "манифест с run_version и дополнительным датасетом" \
  python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/smoke-2" \
    --dataset datasets/eval_ood_clean.jsonl --base-model Qwen/Qwen2.5-0.5B \
    --pipeline laguna_pipeline_v8.py --seed 42 --stages cpt=done,sft=done --complete \
    --run-version "tools/run_smoke.py@deadbeef" \
    --dataset-extra corpus-card=data/corpus-card.json \
    --relative-to "$CASE_ROOT"
expect_exit 0 "обе версии (pipeline_version и run_version) в манифесте (SPEC §1a)" \
  python3 - "$TMP/runsgen/smoke-2/run_manifest.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["run_version"] == "tools/run_smoke.py@deadbeef", d.get("run_version")
assert d["pipeline_version"].startswith("laguna_pipeline_v8.py@"), d["pipeline_version"]
assert len(d["pipeline_sha256"]) == 64
extra = d["datasets_extra"]["corpus-card"]
assert len(extra["sha256"]) == 64 and extra["path"] == "data/corpus-card.json", extra
assert d["pipeline_complete"] is True
sys.exit(0)
PY
expect_exit 0 "манифест с доп. датасетом проходит стража C-012" \
  python3 tools/check_run_manifest.py --runs "$TMP/runsgen"
expect_exit 2 "--dataset-extra без NAME=PATH → NOT-VERIFIED" \
  python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/x" \
    --dataset "$TMP/ev_clean.jsonl" --base-model m --pipeline laguna_pipeline_v8.py \
    --seed 1 --stages cpt --dataset-extra "$TMP/tr_clean.jsonl"
expect_exit 2 "--dataset-extra с несуществующим файлом → NOT-VERIFIED" \
  python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/x" \
    --dataset "$TMP/ev_clean.jsonl" --base-model m --pipeline laguna_pipeline_v8.py \
    --seed 1 --stages cpt --dataset-extra sft="$TMP/nope.jsonl"
expect_exit 0 "манифест с фактическими гиперпараметрами (AD-2 п. Rule)" \
  python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/smoke-3" \
    --dataset datasets/eval_ood_clean.jsonl --base-model Qwen/Qwen2.5-0.5B \
    --pipeline laguna_pipeline_v8.py --seed 42 --stages rl=done --complete \
    --hyperparams kl_coef=0.01 --hyperparams resync_every_runtime=10 \
    --hyperparams resync_every_manifest_default=25 \
    --hyperparams-source "код пайплайна, регулярки" \
    --relative-to "$CASE_ROOT"
expect_exit 0 "гиперпараметры доехали до манифеста и не мешают стражу C-012" \
  python3 - "$TMP/runsgen/smoke-3/run_manifest.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
hp = d["hyperparameters"]
# Значения — типизированные, не строки: «10» и 10 в отчёте о расхождении 25/10
# выглядят одинаково, а сравниваются по-разному.
assert hp["kl_coef"] == 0.01 and isinstance(hp["kl_coef"], float), hp
assert hp["resync_every_runtime"] == 10 and hp["resync_every_manifest_default"] == 25, hp
assert d["hyperparameters_source"] == "код пайплайна, регулярки", d.get("hyperparameters_source")
assert "hyperparameters" not in json.load(
    open(sys.argv[1].replace("smoke-3", "smoke-2"), encoding="utf-8")), \
    "без флага манифест не должен обзаводиться полем"
sys.exit(0)
PY
expect_exit 0 "манифест с гиперпараметрами проходит стража" \
  python3 tools/check_run_manifest.py --runs "$TMP/runsgen"
expect_exit 2 "--hyperparams без NAME=VALUE → NOT-VERIFIED" \
  python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/x" \
    --dataset "$TMP/ev_clean.jsonl" --base-model m --pipeline laguna_pipeline_v8.py \
    --seed 1 --stages cpt --hyperparams kl_coef
# Манифест разведки несёт и env запуска: конфигурация стадии — часть факта
expect_exit 0 "раннер разведки передаёт в манифест гиперпараметры из кода" \
  python3 - <<'PY'
import sys, tempfile, pathlib, subprocess, json
sys.path.insert(0, "tools")
import run_rl_probe as R
d = pathlib.Path(tempfile.mkdtemp()) / "rl-probe-20260101-0000"
args = R.parse_args(["--ts", "20260101-0000"])
mf = R.write_manifest(d, args, {"stage": "rl"}, "done")
assert mf is not None and mf.is_file(), mf
m = json.loads(mf.read_text(encoding="utf-8"))
hp = m["hyperparameters"]
assert hp["kl_coef"] == 0.01, hp
assert hp["resync_every_runtime"] == 10, hp        # из кода RL-петли
assert hp["resync_every_env"] == "10", hp          # из env запуска
assert hp["group_size"] == 2 and hp["max_turns"] == 15, hp
assert hp["vllm_gpu_util"] == "0.32" and hp["vllm_kv_cache_bytes"] == "8589934592", hp
assert m["base_model_id"] == "Qwen/Qwen2.5-0.5B" and m["seed"] == 42, m
assert m["pipeline_complete"] is True and m["stages"][0]["name"] == "rl", m
# Прогон разведки — стадия `rl`, а не «все стадии»: манифест это называет
assert m["dataset_path"].endswith("datasets/rl_tasks_revpool_v1.jsonl"), m["dataset_path"]
assert "pool_symlink" in m["datasets_extra"], m.get("datasets_extra")
PY

echo
echo "== 8. check_gb10_serialization.sh (AD-5) =="
# Стенд подменяется целиком: nvidia-smi объявляет GB10 (локальный режим, ssh не
# нужен — тест не зависит от сети), ps/tmux отдают подготовленные раскладки.
# Раскладки задаются файлами через переменные, поэтому один fakebin обслуживает
# все сценарии сенсоров.
echo "1234, python3, 4096 MiB" > "$TMP/fakebin/smi_default.txt"
cat > "$TMP/fakebin/nvidia-smi" <<SH
#!/bin/bash
case "\$*" in
  *"--query-gpu=name"*)     echo "NVIDIA GB10" ;;
  *"--query-compute-apps"*) cat "\${FAKE_SMI_FILE:-$TMP/fakebin/smi_default.txt}" ;;
esac
SH
cat > "$TMP/fakebin/ps" <<'SH'
#!/bin/bash
cat "${FAKE_PS_FILE:-/dev/null}"
SH
cat > "$TMP/fakebin/tmux" <<SH
#!/bin/bash
cat "\${FAKE_TMUX_FILE:-/dev/null}"
SH
chmod +x "$TMP/fakebin/nvidia-smi" "$TMP/fakebin/ps" "$TMP/fakebin/tmux"

# ОДНА нагрузка = дерево из 6 процессов (tmux → runner → safe_start → docker →
# python): подсчёт «по процессам» объявил бы её нарушением AD-5.
cat > "$TMP/ps_one_load.txt" <<'PS'
 3521765       1 1-13:00:00 tmux new -d -s v12-ladder bash /home/user/gb10-shared/run_v12_ladder.sh
 3521766 3521765 1-13:00:00 bash -c bash /home/user/gb10-shared/run_v12_ladder.sh
 3521767 3521766 1-13:00:00 bash /home/user/gb10-shared/run_v12_ladder.sh
 3707779 3521767 1-01:00:00 bash /home/user/gb10-shared/nvrm-storm/safe_start.sh -d 60 -i 5 -- docker run --rm --gpus all nvcr.io/nvidia/pytorch:26.07-py3-vllm python3 /workspace/shared/laguna_pipeline_v8.py --stage sft --exp_name run_a
 3707814 3707779 1-01:00:00 docker run --rm --gpus all nvcr.io/nvidia/pytorch:26.07-py3-vllm python3 /workspace/shared/laguna_pipeline_v8.py --stage sft --exp_name run_a
 3707866 3707843 1-01:00:00 python3 /workspace/shared/laguna_pipeline_v8.py --stage sft --exp_name run_a
 3525880       1 00:00:10 bash tools/check_gb10_serialization.sh
PS
# Две нагрузки: разные --exp_name (разные прогоны стадий).
cat > "$TMP/ps_two_loads.txt" <<'PS'
  101       1 01:20:00 python3 /workspace/shared/laguna_pipeline_v8.py --stage cpt --exp_name run_a
  102       1 01:10:00 python3 /workspace/shared/laguna_pipeline_v8.py --stage rl --exp_name run_b
PS
# Без --exp_name: нагрузкой считается корень совпавшего дерева (два дерева → 2).
cat > "$TMP/ps_noexp_two.txt" <<'PS'
   30       1 02:00:00 bash -c python3 train_sft.py
   31      30 02:00:00 python3 train_sft.py
   32       1 02:00:00 bash -c python3 train_rl.py
   33      32 02:00:00 python3 train_rl.py
PS
# Одно дерево без --exp_name: лаунчер и нагрузка — одна сущность.
cat > "$TMP/ps_noexp_one.txt" <<'PS'
   40       1 02:00:00 bash -c python3 train_sft.py
   41      40 02:00:00 python3 train_sft.py
PS
expect_exit 0 "одна нагрузка (дерево из 6 процессов) — не нарушение" \
  env PATH="$TMP/fakebin:$PATH" FAKE_PS_FILE="$TMP/ps_one_load.txt" \
  bash tools/check_gb10_serialization.sh
expect_contains "тренировочных нагрузок: 1" "нагрузка названа счётом, а не процессами" \
  env PATH="$TMP/fakebin:$PATH" FAKE_PS_FILE="$TMP/ps_one_load.txt" \
  bash tools/check_gb10_serialization.sh
expect_exit 1 "две нагрузки с разными --exp_name → НАРУШЕНИЕ" \
  env PATH="$TMP/fakebin:$PATH" FAKE_PS_FILE="$TMP/ps_two_loads.txt" \
  bash tools/check_gb10_serialization.sh
expect_exit 1 "без --exp_name: два совпавших дерева → НАРУШЕНИЕ" \
  env PATH="$TMP/fakebin:$PATH" FAKE_PS_FILE="$TMP/ps_noexp_two.txt" \
  bash tools/check_gb10_serialization.sh
expect_exit 0 "без --exp_name: одно дерево → OK" \
  env PATH="$TMP/fakebin:$PATH" FAKE_PS_FILE="$TMP/ps_noexp_one.txt" \
  bash tools/check_gb10_serialization.sh
cat > "$TMP/tmux_two_stages.txt" <<'TMUX'
v12-ladder:0.0 bash bash /home/user/gb10-shared/run_v12_ladder.sh
v12-ladder:0.1 bash bash /home/user/gb10-shared/run_v12_ladder.sh
TMUX
expect_exit 1 "tmux: две панели стадий одновременно → НАРУШЕНИЕ" \
  env PATH="$TMP/fakebin:$PATH" FAKE_PS_FILE=/dev/null FAKE_SMI_FILE=/dev/null \
    FAKE_TMUX_FILE="$TMP/tmux_two_stages.txt" \
  bash tools/check_gb10_serialization.sh
expect_exit 2 "стенд доступен, но сенсоры пусты → NOT-VERIFIED" \
  env PATH="$TMP/fakebin:$PATH" FAKE_PS_FILE=/dev/null FAKE_SMI_FILE=/dev/null \
    FAKE_TMUX_FILE=/dev/null \
  bash tools/check_gb10_serialization.sh
expect_exit 0 "GB10 недоступен → check skipped" \
  bash tools/check_gb10_serialization.sh --host 203.0.113.1 --timeout 2
expect_contains "gb10 not reachable — check skipped" "недоступность названа явно" \
  bash tools/check_gb10_serialization.sh --host 203.0.113.1 --timeout 2
expect_exit 0 "фолбэк: список кандидатов недоступен → skip, exit 0" \
  bash tools/check_gb10_serialization.sh --hosts "no-such-host.invalid other.invalid" --timeout 2
expect_contains "no-such-host.invalid:" "причина недоступности каждого кандидата названа" \
  bash tools/check_gb10_serialization.sh --hosts "no-such-host.invalid other.invalid" --timeout 2
expect_contains "other.invalid:" "перебор кандидатов не останавливается на первом" \
  bash tools/check_gb10_serialization.sh --hosts "no-such-host.invalid other.invalid" --timeout 2
expect_contains "gb10-fast" "кандидат по умолчанию назван в справке" \
  bash tools/check_gb10_serialization.sh --help
expect_contains "--strict" "строгий режим назван в справке" \
  bash tools/check_gb10_serialization.sh --help
expect_contains "NOT-VERIFIED" "недоступность в обычном режиме не читается как OK" \
  bash tools/check_gb10_serialization.sh --host 203.0.113.1 --timeout 2
expect_exit 2 "--strict: недоступность стенда — красный вердикт, а не «пропущено»" \
  bash tools/check_gb10_serialization.sh --host 203.0.113.1 --timeout 2 --strict
expect_contains "FAIL(strict)" "строгий отказ назван явно (ADR-007 п.5)" \
  bash tools/check_gb10_serialization.sh --host 203.0.113.1 --timeout 2 --strict
expect_exit 2 "--strict не ослабляет сенсорный NOT-VERIFIED (стенд есть, данных нет)" \
  env PATH="$TMP/fakebin:$PATH" FAKE_PS_FILE=/dev/null FAKE_SMI_FILE=/dev/null \
    FAKE_TMUX_FILE=/dev/null GB10_STRICT=1 \
  bash tools/check_gb10_serialization.sh
expect_exit 0 "--strict на здоровом раскладе (одна нагрузка) остаётся зелёным" \
  env PATH="$TMP/fakebin:$PATH" FAKE_PS_FILE="$TMP/ps_one_load.txt" \
  bash tools/check_gb10_serialization.sh --strict

echo
echo "== 9. build_rev_pool.py (S2 / ADR-007 п.1) =="
POOL="$TMP/rev-pool/rl_pool_filtered.jsonl"
REPORT="$TMP/rev-pool/exclusions.json"
expect_exit 0 "синтетика: 1 задача из 3 исключена, пул собран" \
  python3 tools/build_rev_pool.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_pool.jsonl" \
    --out "$POOL" --report "$REPORT" --expect-excluded 1
expect_exit 0 "оставшиеся строки перенесены дословно (2 из 3), порядок сохранён" \
  python3 - "$POOL" "$TMP/rl_pool.jsonl" <<'PY'
import json, sys
kept = [l for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
src = [l for l in open(sys.argv[2], encoding="utf-8") if l.strip()]
assert len(kept) == 2, f"ожидалось 2 строки, получено {len(kept)}"
assert kept == src[1:], "строки пула не совпадают с исходными дословно"
assert all(json.loads(l) for l in kept), "строка пула не JSON"
sys.exit(0)
PY
expect_exit 0 "отчёт об исключениях называет задачу, причину и примеры SFT" \
  python3 - "$REPORT" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["counts"]["excluded"] == 1 and d["counts"]["retained"] == 2, d["counts"]
exc = d["excluded"][0]
assert exc["rl_line"] == 1 and exc["rl_id"] == "line:1", exc
assert "prompt_verbatim_in_sft_train_v12" in exc["reason"], exc["reason"]
assert exc["sft_examples_matched"] == 1 and exc["sft_lines"] == [1], exc
assert d["data_sources_unchanged"] is True
sys.exit(0)
PY
expect_exit 0 "каталог пула помечен NOT_A_RUN (C-012 не ищет в нём манифест)" \
  python3 - "$TMP/rev-pool" <<'PY'
import pathlib, sys
m = pathlib.Path(sys.argv[1]) / "NOT_A_RUN"
assert m.is_file() and m.read_text(encoding="utf-8").strip(), "маркера нет"
sys.exit(0)
PY
expect_exit 0 "повторный прогон: пул актуален, не перезаписывается" \
  python3 tools/build_rev_pool.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_pool.jsonl" \
    --out "$POOL" --report "$REPORT" --expect-excluded 1
expect_contains "не перезаписываю" "идемпотентность названа, а не молчаливая перезапись" \
  python3 tools/build_rev_pool.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_pool.jsonl" \
    --out "$POOL" --report "$REPORT" --expect-excluded 1
printf '{"task_type": "x", "prompt": "чужой пул"}\n' > "$POOL"
expect_exit 1 "чужой пул на месте выхода → отказ без --force" \
  python3 tools/build_rev_pool.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_pool.jsonl" \
    --out "$POOL" --report "$REPORT" --expect-excluded 1
expect_exit 1 "расхождение с ADR-007 (8 вместо 1) — сигнал, пул не собирается" \
  python3 tools/build_rev_pool.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_pool.jsonl" \
    --out "$TMP/rev-pool/other.jsonl" --report "$TMP/rev-pool/other.json" --force
expect_exit 0 "при расхождении пул не записан вовсе (нет half-built каталога)" \
  python3 - "$TMP/rev-pool/other.jsonl" <<'PY'
import pathlib, sys
sys.exit(1 if pathlib.Path(sys.argv[1]).exists() else 0)
PY
expect_exit 0 "--no-expect: сверка числа отключена осознанно" \
  python3 tools/build_rev_pool.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/rl_pool.jsonl" \
    --out "$TMP/rev-pool/other.jsonl" --report "$TMP/rev-pool/other.json" --no-expect
expect_exit 2 "RL-пула нет → NOT-VERIFIED" \
  python3 tools/build_rev_pool.py --sft "$TMP/sft_ov.jsonl" --rl "$TMP/nope.jsonl" \
    --out "$TMP/rev-pool/x.jsonl" --report "$TMP/rev-pool/x.json"
expect_exit 2 "SFT-пула нет → NOT-VERIFIED" \
  python3 tools/build_rev_pool.py --sft "$TMP/nope.jsonl" --rl "$TMP/rl_pool.jsonl" \
    --out "$TMP/rev-pool/x.jsonl" --report "$TMP/rev-pool/x.json"
# Реальные данные (пул 28 000 задач, SFT 44 949 примеров) — но запись только в TMP:
# фикстуры в дерево кейса не пишутся, данные-источники не меняются (AD-7).
expect_exit 0 "реальный пул: 8 задач исключено из 28 000 (AD-007 п.1)" \
  python3 tools/build_rev_pool.py --rl datasets/rl_tasks_oxalpha.jsonl \
    --sft datasets/sft_train_v12.jsonl \
    --out "$TMP/real-pool/rl_pool_filtered.jsonl" --report "$TMP/real-pool/exclusions.json"
expect_exit 0 "реальный пул ревизии: пересечение с SFT — 0 (гейт C-017)" \
  python3 tools/check_sft_rl_overlap.py --sft datasets/sft_train_v12.jsonl \
    --rl "$TMP/real-pool/rl_pool_filtered.jsonl" --no-evidence --max-matched 0
# Каталог пула в дереве кейса не должен считаться прогоном — но вердикт по
# остальным каталогам runs/ зависит от незавершённых прогонов, поэтому смотрим
# машинный отчёт по конкретному каталогу, а не код возврата целиком.
expect_exit 0 "пул ревизии в runs/ пропущен как не-прогон, нарушений по нему нет" \
  python3 - <<'PY'
import json, subprocess, sys
p = subprocess.run([sys.executable, "tools/check_run_manifest.py", "--runs", "runs/", "--json"],
                   capture_output=True, text=True)
rep = json.loads(p.stdout)
skipped = {e["run"]: e for e in rep["skipped"]}
assert "rev-pool" in skipped, rep["skipped"]
assert skipped["rev-pool"]["not_a_run"], skipped["rev-pool"]
assert not [v for v in rep["violations"] if "rev-pool" in v], rep["violations"]
assert not [w for w in rep["warnings"] if "rev-pool" in w], rep["warnings"]
content = open("runs/rev-pool/NOT_A_RUN", encoding="utf-8").read()
assert "пул" in content and "не прогон" in content, content
sys.exit(0)
PY

echo
echo "== 10. run_smoke.py / smoke_probe.py (S2: смоук CPT+SFT) =="
expect_exit 0 "план прогона печатается без обращения к стенду" \
  python3 tools/run_smoke.py --plan --ts 20260101-0000
expect_contains "RL:  НЕ запускается" "в плане назван отказ от RL-стадии" \
  python3 tools/run_smoke.py --plan --ts 20260101-0000
expect_contains "safe_start.sh" "в плане назван обязательный путь запуска (AD-5)" \
  python3 tools/run_smoke.py --plan --ts 20260101-0000
expect_contains "≥ 30" "в плане назван бюджет памяти (ADR-007)" \
  python3 tools/run_smoke.py --plan --ts 20260101-0000
expect_exit 2 "--plan и --analyze-only несовместимы" \
  python3 tools/run_smoke.py --plan --analyze-only
expect_exit 2 "--analyze-only без каталога прогона → NOT-VERIFIED" \
  python3 tools/run_smoke.py --analyze-only --ts 19990101-0000
expect_exit 1 "--stop-only на недоступном стенде → отказ, а не «закрыто»" \
  python3 tools/run_smoke.py --stop-only --ts 20260101-0000 --host no-such-host.invalid
expect_exit 2 "smoke_probe: нет пайплайна → NOT-VERIFIED" \
  python3 tools/smoke_probe.py --pipeline "$TMP/nope.py" --metrics "$TMP/m.jsonl"
expect_exit 0 "smoke_probe: справка (модуль импортируется без torch/transformers)" \
  python3 tools/smoke_probe.py --help
expect_exit 0 "smoke_mem_sampler.sh синтаксически корректен" \
  bash -n tools/smoke_mem_sampler.sh

# Разбор замеров и сценарий стадии — чистые функции: проверяются на синтетике,
# без стенда и без контейнера. Именно здесь ловится «скрипт стадии передал
# хост-путь внутрь контейнера» — ошибка, которая на стенде выглядит как
# «python3: can't open file».
expect_exit 0 "разбор замеров и сценарий стадии (синтетика)" \
  python3 - <<'PY'
import argparse, json, sys, pathlib, tempfile
sys.path.insert(0, "tools")
import run_smoke as R

# --- probe JSONL → сводка
d = pathlib.Path(tempfile.mkdtemp())
probe = d / "probe_cpt.jsonl"
rows = [{"event": "start", "stage": "cpt"}]
for i, loss in enumerate([3.0, 2.8, 2.6, 2.4, 2.2]):
    # i=2 идёт сразу после eval-прохода (skipped_before=1): его интервал в tok/s
    # не идёт — иначе GEN-EVAL раздул бы время шага в разы.
    rows.append({"event": "step", "i": i, "ts": 1.0 + i, "dt": 0.05,
                 "dt_step": None if i == 0 else 2.0, "tokens": 8192, "loss": loss,
                 "entropy": 1.5 + 0.01 * i, "skipped_before": 1 if i == 2 else 0,
                 "eval_seconds_before": 26.0 if i == 2 else 0.0})
rows.append({"event": "end", "train_steps": 5, "skipped_forwards": 3,
             "skipped_seconds": 78.0, "probe_seconds": 90.0, "error": None,
             "torch_peak_allocated_bytes": 4 * 2**30, "torch_peak_reserved_bytes": 6 * 2**30})
probe.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
parsed = R.parse_probe(probe)
assert len(parsed["steps"]) == 5 and parsed["end"]["skipped_forwards"] == 3, parsed["end"]
s = R.summarize_steps(parsed["steps"], parsed["end"], 11.0)
assert s["tokens_total"] == 5 * 8192, s
assert s["step_intervals_used"] == 3, s                   # 4 интервала минус один с eval
assert s["step_intervals_skipped_eval"] == 1, s
assert s["train_seconds"] == 6.0, s["train_seconds"]
assert abs(s["tok_per_s"] - 4096.0) < 1e-6, s["tok_per_s"] # 3×8192 / 6.0 с
assert s["step_seconds_mean"] == 2.0, s["step_seconds_mean"]
assert s["forward_seconds_mean"] == 0.05, s["forward_seconds_mean"]
assert s["loss_first"] == 3.0 and s["loss_last"] == 2.2, s
assert s["loss_decreasing_head_to_tail"] is True, s
assert s["loss_slope_per_step"] < 0, s["loss_slope_per_step"]
assert s["torch_peak_allocated_gb"] == 4.0 and s["torch_peak_reserved_gb"] == 6.0, s
assert s["eval_seconds"] == 78.0 and s["probe_seconds"] == 90.0, s
assert s["entropy"]["points"] == 5 and s["entropy"]["in_corridor"] is True, s["entropy"]

# --- сэмплы памяти → пик занятой и пик контейнера
mem = d / "mem_cpt.jsonl"
mem.write_text("\n".join(json.dumps(r) for r in [
    {"ts": 1, "mem_total_kb": 128000000, "mem_available_kb": 84000000, "mem_free_kb": 1,
     "cached_kb": 1, "containers": "llm-platform-runtime=28119MiB / 121GiB;laguna-smoke-t-cpt=12.5GiB / 121GiB"},
    {"ts": 2, "mem_total_kb": 128000000, "mem_available_kb": 62000000, "mem_free_kb": 1,
     "cached_kb": 1, "containers": "laguna-smoke-t-cpt=40GiB / 121GiB"},
]) + "\n", encoding="utf-8")
m = R.parse_mem_samples(mem, container_hint="laguna-smoke-t-cpt")
assert abs(m["peak_used_gb"] - 62.94) < 0.1, m          # (128000000-62000000)/1048576
assert abs(m["min_available_gb"] - 59.13) < 0.1, m      # 62000000/1048576
assert m["used_first_gb"] == 41.96 and m["container_peak_gb"] == 40.0, m
assert "container_peak_note" not in m, m               # 40 ГБ против ~21 ГБ прироста — норма
# Единицы docker stats: MiB ≠ MB, и разница в 1024× даёт «терабайты» в отчёте.
assert abs(R.parse_docker_mem("28119MiB / 121GiB") - 27.46) < 0.01, R.parse_docker_mem("28119MiB / 121GiB")
assert abs(R.parse_docker_mem("675.4MiB / 121GiB") - 0.66) < 0.01, R.parse_docker_mem("675.4MiB / 121GiB")
assert abs(R.parse_docker_mem("12.3GiB / 121GiB") - 12.3) < 1e-9
assert R.parse_docker_mem("") == 0.0 and R.parse_docker_mem("0B / 121GiB") == 0.0
# cgroup-память контейнера меньше прироста по хосту → честная пометка, а не тихое число
mem2 = d / "mem2.jsonl"
mem2.write_text("\n".join(json.dumps(r) for r in [
    {"ts": 1, "mem_total_kb": 128000000, "mem_available_kb": 84000000, "mem_free_kb": 1,
     "cached_kb": 1, "containers": "laguna-smoke-t-cpt=0.5GiB / 121GiB"},
    {"ts": 2, "mem_total_kb": 128000000, "mem_available_kb": 60000000, "mem_free_kb": 1,
     "cached_kb": 1, "containers": "laguna-smoke-t-cpt=0.7GiB / 121GiB"},
]) + "\n", encoding="utf-8")
m2 = R.parse_mem_samples(mem2, container_hint="laguna-smoke-t-cpt")
assert "cgroup не учитываются" in m2.get("container_peak_note", ""), m2

# --- инциденты: поимённо, а не счётчиком; здоровые строки не ловятся
log = "\n".join([
    "16:36:35 [INFO] CPT step 0/50 | loss=2.10 | tok/s=718",
    "torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB",
    "NVRM: Xid (PCI:0000:01:00): 51, pid=1234",
    "16:40:10 [INFO] CPT complete",
])
hits = R.scan_incidents(log)
assert len(hits) == 2, hits
assert "OutOfMemoryError" in hits[0] and "NVRM" in hits[1], hits
assert R.scan_incidents("CPT step 1 | loss=2.0 | tok/s=3000") == []

# --- сценарий стадии: хостовые и контейнерные пути не перепутаны
args = R.parse_args(["--ts", "20260101-0000"])
script = R.build_stage_script(ts=args.ts, run_id=args.run_id, stage="cpt",
                              exp_name="smoke-compact-cpt-20260101-0000", args=args)
assert 'RUN_DIR="/home/user/experiments/smoke-compact-20260101-0000"' in script, "нет хост-пути"
assert 'CTR_RUN_DIR="/workspace/experiments/smoke-compact-20260101-0000"' in script, "нет пути контейнера"
assert 'python3 "$CTR_RUN_DIR/smoke_probe.py"' in script, "обвязка зовётся не по пути контейнера"
assert "bash /home/user/gb10-shared/nvrm-storm/safe_start.sh -d 60 -i 5 -- docker run" in script
assert "smoke_mem_sampler.sh" in script and "SMOKE_STAGE_EXIT=$RC" in script
assert "--pipeline /workspace/shared/laguna_pipeline_v8.py" in script
assert "--stage cpt" in script and "--max_steps 50" in script
assert "llm-platform" not in script, "смоук не должен упоминать платформенные контейнеры"
sft = R.build_stage_script(ts=args.ts, run_id=args.run_id, stage="sft",
                           exp_name="smoke-compact-sft-20260101-0000", args=args)
assert "--stage sft" in sft and "--max_steps 30" in sft, "шаги SFT вне коридора 20–30"
assert "/checkpoints" in sft, "SFT без общего ckpt_dir не подхватит чекпойнт CPT"

# --- план: RL не запускается, стадии с одной моделью и сидом
plan = R.render_plan(args, args.run_id, "gb10-fast")
assert "RL:  НЕ запускается" in plan and "seed=42" in plan, plan
assert str(args.cpt_steps) in plan and str(args.sft_steps) in plan
assert 20 <= args.cpt_steps <= 50 and 20 <= args.sft_steps <= 30, (args.cpt_steps, args.sft_steps)
sys.exit(0)
PY

# Разбор энтропии/записи обвязки — с настоящим torch, но без transformers:
# проверяется арифметика (равномерное распределение → ln V, one-hot → 0) и то,
# что eval/no_grad-проходы в метрики не попадают.
if python3 -c "import torch" 2>/dev/null; then
expect_exit 0 "smoke_probe: энтропия и запись шагов (torch)" \
  python3 - <<'PY'
import json, math, pathlib, sys, tempfile
sys.path.insert(0, "tools")
import torch
import smoke_probe as P

V = 8
uni = torch.zeros(1, 4, V)
labels = torch.tensor([[1, 2, 3, 4]])
assert abs(P.entropy_of_logits(uni, labels, 1, 2) - math.log(V)) < 1e-5
onehot = torch.full((1, 4, V), -50.0); onehot[0, :, 0] = 50.0
assert P.entropy_of_logits(onehot, labels, 1, 2) < 1e-6
masked = torch.tensor([[1, -100, 3, -100]])
assert abs(P.entropy_of_logits(uni, masked, 1, 2) - math.log(V)) < 1e-5
assert P.entropy_of_logits(None, labels, 1, 2) is None

class Out:
    def __init__(self, loss): self.loss, self.logits = loss, None
class FakeModel:
    def __init__(self): self.training = True
    def forward(self, input_ids=None, labels=None): return Out(torch.tensor(1.5))

tmp = pathlib.Path(tempfile.mkdtemp()) / "m.jsonl"
rec = P.Recorder(tmp, 1)
rec.start({"stage": "cpt"})     # режим подставляет сам Recorder, а не вызывающий
model = FakeModel()
P.wrap_model(model, rec, {"entropy": True, "pos_stride": 1, "block": 2})
ids = torch.zeros(2, 4, dtype=torch.long)
model.forward(input_ids=ids, labels=ids)                 # обучающий → запись
model.training = False
model.forward(input_ids=ids, labels=ids)                 # eval → пропуск
model.training = True
with torch.no_grad():
    model.forward(input_ids=ids, labels=ids)             # no_grad → пропуск
rec.finish(None)
rows = [json.loads(l) for l in tmp.read_text(encoding="utf-8").splitlines()]
steps = [r for r in rows if r["event"] == "step"]
end = next(r for r in rows if r["event"] == "end")
assert len(steps) == 1 and steps[0]["tokens"] == 8, steps
assert end["train_steps"] == 1 and end["skipped_forwards"] == 2, end
assert end["error"] is None
sys.exit(0)
PY
else
  echo "  SKIP smoke_probe: torch недоступен — проверка энтропии не выполнена"
fi

# Сводка и evidence из готовых артефактов (--analyze-only): путь, которым
# пользуются после ручного прогона или падения раннера.
FIX="$TMP/smoke-20260101-0000"
mkdir -p "$FIX/logs"
cat > "$FIX/probe_cpt.jsonl" <<'JSONL'
{"event": "start", "stage": "cpt"}
{"event": "step", "i": 0, "ts": 1.0, "dt": 0.05, "dt_step": null, "tokens": 8192, "loss": 3.0, "entropy": 1.4, "skipped_before": 0, "eval_seconds_before": 0.0}
{"event": "step", "i": 1, "ts": 3.0, "dt": 0.05, "dt_step": 2.0, "tokens": 8192, "loss": 2.5, "entropy": 1.5, "skipped_before": 0, "eval_seconds_before": 0.0}
{"event": "end", "train_steps": 2, "skipped_forwards": 0, "skipped_seconds": 0.0, "probe_seconds": 4.0, "error": null}
JSONL
cat > "$FIX/mem_cpt.jsonl" <<'JSONL'
{"ts": 1, "mem_total_kb": 128000000, "mem_available_kb": 84000000, "mem_free_kb": 1, "cached_kb": 1, "containers": "laguna-smoke-20260101-0000-cpt=12GiB / 121GiB"}
{"ts": 2, "mem_total_kb": 128000000, "mem_available_kb": 70000000, "mem_free_kb": 1, "cached_kb": 1, "containers": "laguna-smoke-20260101-0000-cpt=38GiB / 121GiB"}
JSONL
printf '16:36:35 [INFO] CPT step 0/50 | loss=3.0\n16:36:40 [INFO] CPT complete\n' \
  > "$FIX/logs/cpt.log"
expect_exit 0 "--analyze-only пересобирает evidence из артефактов" \
  python3 tools/run_smoke.py --analyze-only --ts 20260101-0000 --runs-dir "$TMP" \
    --host no-such-host.invalid --evidence "$TMP/s2-smoke.json"
expect_exit 0 "evidence несёт замеры, предусловия и список непроверенного" \
  python3 - "$TMP/s2-smoke.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["stage"] == "S2"
m = d["measurements"]["cpt"]
assert m["steps"] == 2 and m["tok_per_s"] == 4096.0, m
assert m["loss_first"] == 3.0 and m["loss_last"] == 2.5, m
assert m["memory"]["peak_used_gb"] > 0 and m["memory"]["container_peak_gb"] == 38.0, m["memory"]
assert d["incidents"]["per_stage_from_logs"]["cpt"] == 0
assert any("RL-стадия" in x for x in d["not_verified"]), d["not_verified"]
assert any("eval" in x for x in d["not_verified"]), d["not_verified"]
assert d["config"]["rl"] == "не запускалась"
assert d["config"]["sft"]["tok_cache"].endswith("sft_train_v12_8192_qwen25.npz")
assert isinstance(d["forgetting_ppl"], list)
assert d["config"]["cpt"]["steps"] == 50 and d["config"]["sft"]["steps"] == 30
a = d["acceptance"]["per_stage"]["cpt"]
assert a["loss_decreasing_head_to_tail"] is True and a["entropy_in_corridor"] is True, a
assert a["entropy_mean"] == 1.45 and a["steps"] == 2, a
assert "SPEC" in d["acceptance"]["criteria"]
assert any("--analyze-only" in x for x in d["not_verified"]), d["not_verified"]
sys.exit(0)
PY
# Повторная пересборка: обновляет выводимое из артефактов, но не переписывает
# историю прогона (состояние стенда до/после повторным замером не восстановить).
expect_exit 0 "повторная пересборка помечает сводку как rebuilt" \
  python3 tools/run_smoke.py --analyze-only --ts 20260101-0000 --runs-dir "$TMP" \
    --host no-such-host.invalid --evidence "$TMP/s2-smoke.json"
expect_exit 0 "rebuild не теряет предусловия и результаты стадий прежнего прогона" \
  python3 - "$TMP/s2-smoke.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert "at" in d.get("rebuilt", {}), d.keys()
assert "preconditions" in d and "stage_results" in d, d.keys()
assert d["measurements"]["cpt"]["steps"] == 2
assert d["acceptance"]["per_stage"]["cpt"]["entropy_mean"] == 1.45
sys.exit(0)
PY

echo
echo "== 11. data/corpus-card.json (C-010, C-016) =="
expect_exit 0 "карточка корпуса разбирается и арифметика сходится" \
  python3 - <<'PY'
import json, sys
d = json.load(open("data/corpus-card.json", encoding="utf-8"))
m = d["mix"]
assert m["chunks"] == m["domain_chunks"] + m["replay_chunks"], "чанки не сходятся"
assert m["tokens"] == m["chunks"] * m["chunk_len"], "токены не сходятся"
assert abs(m["mix_domain_ratio"] - m["domain_chunks"] / m["chunks"]) < 1e-9
for f in ("source", "license", "sha256", "seed"):
    assert f in d or f in m, f"нет обязательного поля C-010: {f}"
for name in ("cpt_corpus_v12r_8192_qwen25.npy",):
    assert name in d["sha256"], f"нет хеша {name}"
sys.exit(0)
PY
expect_exit 0 "карточка несёт лицензионный статус (C-016, ADR-005 Accepted)" \
  python3 - <<'PY'
import json, sys
lic = json.load(open("data/corpus-card.json", encoding="utf-8"))["license"]
# ADR-005 Accepted (cc773c3): статус — internal_only, а не unresolved; лицензия
# доменной части не устанавливается, публикация запрещена. Прежнее ожидание
# "unresolved" здесь устарело и краснило тест на здоровой карточке.
assert lic.get("license_status") == "internal_only", lic.get("license_status")
assert lic.get("domain_license_unresolved") is True
assert "публикация" in lic.get("license_note", ""), "нет границы «внутреннее/публикация»"
assert "ADR-005" in lic.get("license_adr", ""), "не назван источник решения"
sys.exit(0)
PY
expect_exit 0 "все хеши карточки совпадают с файлами" \
  python3 - <<'PY'
import hashlib, json, sys
d = json.load(open("data/corpus-card.json", encoding="utf-8"))
bad = []
for a in d["artifacts"]:
    h = hashlib.sha256()
    try:
        with open(a["resolved"], "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
    except OSError as e:
        bad.append(f"{a['path']}: {e}"); continue
    if h.hexdigest() != a["sha256"]:
        bad.append(f"{a['path']}: хеш разошёлся")
print("РАСХОЖДЕНИЯ:", bad)
sys.exit(1 if bad else 0)
PY

echo
echo "== 12. run_rl_probe.py / rl_probe_hook.py (S3-pre: цена шага RL, ADR-010) =="
expect_exit 0 "план разведки печатается без обращения к стенду" \
  python3 tools/run_rl_probe.py --plan --ts 20260101-0000
expect_contains "ADR-010" "в плане назван ADR-010 (основание разведки)" \
  python3 tools/run_rl_probe.py --plan --ts 20260101-0000
expect_contains "CPT/SFT не запускаются" "в плане назван отказ от новых CPT/SFT" \
  python3 tools/run_rl_probe.py --plan --ts 20260101-0000
expect_contains "safe_start.sh" "в плане назван обязательный путь запуска (AD-5)" \
  python3 tools/run_rl_probe.py --plan --ts 20260101-0000
expect_contains "≥ 40.0 ГБ" "в плане назван порог памяти ADR-010 п.4" \
  python3 tools/run_rl_probe.py --plan --ts 20260101-0000
expect_contains "в манифесте пайплайна 25" "в плане названо расхождение resync_every 25/10" \
  python3 tools/run_rl_probe.py --plan --ts 20260101-0000
expect_contains "rl_tasks_revpool_v1.jsonl" "в плане назван пул ревизии с версионным именем" \
  python3 tools/run_rl_probe.py --plan --ts 20260101-0000
# Коридор шагов — из ADR-010 (50–100): выход за него отсекается, а не «предупреждается».
expect_exit 2 "шагов меньше 50 → отказ (коридор ADR-010)" \
  python3 tools/run_rl_probe.py --plan --rl-steps 20
expect_exit 2 "шагов больше 100 → отказ (коридор ADR-010)" \
  python3 tools/run_rl_probe.py --plan --rl-steps 500
expect_exit 2 "--plan и --analyze-only несовместимы" \
  python3 tools/run_rl_probe.py --plan --analyze-only
expect_exit 2 "--analyze-only без каталога прогона → NOT-VERIFIED" \
  python3 tools/run_rl_probe.py --analyze-only --ts 19990101-0000
expect_exit 1 "--stop-only на недоступном стенде → отказ, а не «закрыто»" \
  python3 tools/run_rl_probe.py --stop-only --ts 20260101-0000 --host no-such-host.invalid
expect_exit 2 "--write-manifest без --analyze-only → отказ" \
  python3 tools/run_rl_probe.py --plan --write-manifest

# Пересборка сводки из артефактов (--analyze-only) — путь, которым чинится отчёт
# после прогона, чей раннер остановился или был заменён. Стенд недоступен:
# обе деградации (нет сверки со стендом, каталог прогона вне кейса) обязаны быть
# названы причиной, а не падением и не нулём.
expect_exit 0 "--analyze-only пересобирает evidence и (по флагу) манифест" \
  python3 - <<'PY'
import json, pathlib, sys, tempfile
sys.path.insert(0, "tools")
import run_rl_probe as R

tmp = pathlib.Path(tempfile.mkdtemp())
d = tmp / "runs" / "rl-probe-20260101-0000"
(d / "logs").mkdir(parents=True)
(d / "rl_probe.jsonl").write_text("\n".join(json.dumps(r) for r in [
    {"event": "start", "ts": 1000.0, "tasks_per_step": 4, "group_size": 2,
     "patched": ["RLDataset.sample"], "forward_instrumented": True},
    {"event": "engine_init", "i": 1, "seconds": 150.0, "ts": 1150.0, "kind": "initial"},
    {"event": "step_start", "i": 0, "ts": 1160.0},
    {"event": "step", "i": 0, "dt": 124.0, "partial": False, "t_generate": 88.0,
     "t_policy_fwd": 8.0, "t_other_fwd": 4.0, "t_gen_eval": 0.0, "t_resync": 0.0,
     "t_ckpt": 0.0, "t_probe": 0.0, "n_generate_calls": 8, "gen_tokens": 4312,
     "prompt_tokens": 900, "n_hotsync": 0, "n_engine_init": 0},
    {"event": "step_start", "i": 1, "ts": 1284.0},
    {"event": "end", "ts": 1300.0, "wall_seconds": 300.0, "steps_closed": 1,
     "steps_started": 2, "samples_total": 5, "tasks_per_step": 4, "group_size": 2,
     "engine_inits_total": 1, "error": None},
]) + "\n", encoding="utf-8")
(d / "mem_rl.jsonl").write_text(json.dumps(
    {"ts": 1, "mem_total_kb": 128000000, "mem_available_kb": 60000000, "mem_free_kb": 1,
     "cached_kb": 1, "containers": "laguna-rlprobe-20260101-0000=50GiB / 121GiB"}) + "\n",
    encoding="utf-8")
(d / "rollouts_log.jsonl").write_text(json.dumps(
    {"step": 0, "reward": 0.5, "verifier_passed": False, "n_turns": 2,
     "n_assistant_tokens": 400, "n_tool_calls": 1, "hit_timeout": False,
     "text": "<think>частично</think>"}) + "\n", encoding="utf-8")
(d / "logs" / "rl.log").write_text("\n".join([
    "06:58:34 [INFO] RL: группы 4×2, baseline=spr (pure=False), max_steps=100",
    "07:00:37 [INFO] RL step 0/100 | reward=0.500 | pass=50% | turns=2.0 | len=4312t | "
    "kl=0.0000 | entropy=2.170 | clip=0.000 | adv=0.000 | adv≠0=100% | active=27992/27992 | mem=6.5GB",
]) + "\n", encoding="utf-8")
rc = R.main(["--analyze-only", "--ts", "20260101-0000", "--runs-dir", str(tmp / "runs"),
             "--evidence", str(tmp / "ev.json"), "--write-manifest",
             "--host", "no-such-host.invalid"])
assert rc == 0, rc
m = json.loads((d / "run_manifest.json").read_text(encoding="utf-8"))
assert m["hyperparameters"]["kl_coef"] == 0.01, m["hyperparameters"]
assert m["hyperparameters"]["resync_every_manifest_default"] == 25, m["hyperparameters"]
assert m["stages"][0]["name"] == "rl", m["stages"]
ev = json.loads((tmp / "ev.json").read_text(encoding="utf-8"))
assert ev["rebuilt"]["manifest_regenerated"], ev.get("rebuilt")
assert ev["measurements"]["steps_completed"] == 2, ev["measurements"]
comp = ev["measurements"]["completeness_of_ADR010_p3"]["_summary"]
assert comp["obtained"] == 9 and comp["not_obtained"] == [], comp
cc = ev["extrapolation"]["cross_check_historical_500_steps"]
assert cc["available"] is False and "недоступен" in cc["reason"], cc
assert ev["measurements"]["entropy"]["from_log"]["mean"] == 2.17, ev["measurements"]["entropy"]
sys.exit(0)
PY


# Чистые функции раннера: гиперпараметры из кода, разбор лога, фазы, фон,
# полнота замеров п.3, экстраполяция, сценарий стадии. Всё на синтетике, без стенда.
expect_exit 0 "разбор замеров и сценарий разведки (синтетика)" \
  python3 - <<'PY'
import argparse, json, pathlib, sys, tempfile
sys.path.insert(0, "tools")
import run_rl_probe as R

# --- гиперпараметры: из кода пайплайна, а не из манифеста
src = '''
    TASKS_PER_STEP, R_PER_TASK = 4, 2
    kl_coef = 0.01
    RESYNC_EVERY = int(os.environ.get("RESYNC_EVERY", "10"))
    max_turns = 15
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-6, weight_decay=0.01)
    generated = vllm_gen.generate_batch(prompts_to_gen, max_new_tokens=4096, temperature=1.0, top_k=20,
                    stop=["</tool_call>", "<|im_end|>"])
    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
    log.info(f"RL: группы {TASKS_PER_STEP}×{R_PER_TASK}, baseline={'LOO' if pure else 'spr'} (pure={pure}), max_steps={max_steps}")
    manifest.update({"resync_every": int(os.environ.get("RESYNC_EVERY", "25"))})
'''
hp = R.parse_pipeline_hyperparams(src)
assert hp["kl_coef"] == 0.01, hp
assert hp["resync_every_runtime"] == 10, hp          # код RL-петли
assert hp["resync_every_manifest_default"] == 25, hp # манифест пайплайна — расхождение ADR-010
assert hp["tasks_per_step"] == 4 and hp["group_size"] == 2, hp
assert hp["max_turns"] == 15 and hp["gen_max_new_tokens"] == 4096, hp
assert hp["gen_top_k"] == 20 and hp["baseline"] == "spr", hp
assert hp["cispo_c_low"] is None, "несуществующий ключ не выдумывается"
# Гиперпараметры исполняемого пайплайна кейса читаются (файл — симлинк на gb10-shared)
real = R.pipeline_hyperparams()
assert real["kl_coef"] == 0.01 and real["tasks_per_step"] == 4 and real["group_size"] == 2, real
assert real["resync_every_runtime"] == 10, real
assert real["max_turns"] == 15 and real["gen_max_new_tokens"] == 4096, real

# --- разбор лога: шаги, ресники, GEN-EVAL, инциденты
log = "\n".join([
    "09:27:02 [INFO] RL: группы 4×2, baseline=spr (pure=False), max_steps=100",
    "09:30:00 [INFO] RL step 0/100 | reward=0.500 | pass=50% | turns=1.8 | len=1583t | "
    "kl=0.0000 | entropy=1.144 | clip=0.000 | adv=0.000 | adv≠0=100% | active=27992/27992 | mem=5.6GB",
    "09:40:00 [INFO] HOT-SYNC: веса подменены за 12s (step 10)",
    "09:40:01 [INFO] RESYNC-wait: free=34GB/121GB за 21s",
    "09:40:02 [INFO] RESYNC: веса роллаута обновлены (step 10)",
    "09:45:00 [INFO] RL step 10/100 | reward=0.475 | pass=50% | turns=3.0 | len=859t | "
    "kl=0.0010 | entropy=0.420 | clip=0.000 | adv=-0.025 | adv≠0=100% | active=27992/27992 | mem=5.5GB",
    "09:45:01 [INFO] ENTROPY-BOOST: entropy=0.420 ×3 → kl_coef=0.0150",
    "09:50:00 [INFO] GEN-EVAL step 10: ppl_general=126.9 (+0.0% vs base), ppl_domain=24.9 (+0.0%)",
    "09:51:00 [INFO] CUDA out of memory. Tried to allocate 2.00 GiB",
])
m = R.parse_log_metrics(log)
assert len(m["step_lines"]) == 2, m["step_lines"]
assert m["step_lines"][0]["entropy"] == 1.144 and m["step_lines"][0]["pass_rate"] == 0.5
assert m["step_lines"][1]["entropy"] == 0.42 and m["step_lines"][1]["kl"] == 0.001
assert m["hotsyncs"] == [{"seconds": 12, "step": 10}], m["hotsyncs"]
assert m["resync_steps"] == [10] and m["resync_waits"][0]["seconds"] == 21, m
assert m["entropy_boosts"][0]["kl_coef"] == 0.015, m["entropy_boosts"]
assert m["gen_eval"][0]["ppl_domain"] == 24.9, m["gen_eval"]
assert m["groups"] == {"tasks_per_step": 4, "group_size": 2, "baseline": "spr",
                       "pure": False, "max_steps": 100}, m["groups"]
assert m["incidents_count"] == 1 and "out of memory" in m["incidents"][0], m["incidents"]

# --- разбивка по фазам: доли, tok/s, ресники (только закрытые блоки)
steps = [
    {"i": 0, "dt": 100.0, "partial": False, "t_generate": 60.0, "t_gen_eval": 10.0,
     "t_resync": 5.0, "t_ckpt": 5.0, "t_probe": 0.0, "t_policy_fwd": 8.0, "t_other_fwd": 2.0,
     "gen_tokens": 6000, "prompt_tokens": 4000, "n_generate_calls": 20,
     "n_hotsync": 1, "n_engine_init": 0},
    {"i": 1, "dt": 80.0, "partial": False, "t_generate": 40.0, "t_gen_eval": 0.0,
     "t_resync": 0.0, "t_ckpt": 0.0, "t_probe": 0.0, "t_policy_fwd": 6.0, "t_other_fwd": 1.0,
     "gen_tokens": 4000, "prompt_tokens": 3000, "n_generate_calls": 15,
     "n_hotsync": 0, "n_engine_init": 1},
    {"i": 2, "dt": 7.0, "partial": True, "t_generate": 5.0, "gen_tokens": 500},
]
ph = R.phase_summary(steps)
assert ph["steps_closed"] == 2, ph                     # незакрытый блок в средние не идёт
assert ph["step_seconds"]["mean"] == 90.0 and ph["step_seconds"]["min"] == 80.0, ph["step_seconds"]
assert ph["phases_seconds"]["t_generate"] == 100.0, ph["phases_seconds"]
# остаток обучения = dt − (генерация + GEN-EVAL + ресник + чекпоинт + пробы) = 180 − 120
assert ph["phases_seconds"]["t_train_residual"] == 60.0, ph["phases_seconds"]
assert ph["shares_pct"]["t_generate"] == 55.6, ph["shares_pct"]
assert ph["generation"]["tok_per_s"] == 100.0, ph["generation"]   # 10000 токенов / 100 с
assert ph["resync"] == {"hotsync_count": 1, "engine_reinit_count": 1, "total": 2}, ph["resync"]
empty = R.phase_summary([{"i": 0, "dt": 5.0, "partial": True}])
assert empty["steps_closed"] == 0 and "разбивка не получена" in empty["note"], empty

# --- «время шага» имеет несколько законных оснований: не усреднять
bases = R.step_seconds_bases(steps, ph, {"wall_seconds": 300.0, "steps_closed": 2,
                                        "steps_started": 3})
assert bases["hook_mean_closed_steps"] == 90.0, bases        # шкала обвязки
assert bases["wall_over_closed_steps"] == 150.0, bases        # 300 / 2 — со стартом и хвостом
assert bases["wall_over_started_steps"] == 100.0, bases       # 300 / 3 — и незакрытый тоже
assert bases["mean_including_partial"] == round((100 + 80 + 7) / 3, 2), bases
assert bases["basis_used_for_extrapolation"] == "hook_mean_closed_steps", bases
assert bases["wall_vs_hook_delta_seconds"] == 60.0, bases     # не ошибка, а другой вопрос
assert "не усредняются" in bases["note"], bases

# --- накладные расходы обвязки: t_probe пайплайна — однократный хвост, не шаг
probe_steps = [dict(steps[0]), dict(steps[1]),
               {"i": 2, "dt": 40.0, "partial": True, "t_probe": 15.0,
                "n_generate_calls": 4, "n_policy_fwd": 8, "n_other_fwd": 8}]
ovh = R.overhead_report(probe_steps, R.phase_summary(probe_steps),
                        {"wall_seconds": 300.0, "steps_closed": 2, "steps_started": 3})
assert ovh["t_probe_seconds_total"] == 15.0, ovh
assert ovh["t_probe_seconds_in_closed_steps"] == 0.0, ovh   # в закрытые шаги пробы не попадают
assert ovh["runs_per_stage"] == 1 and "1547" in ovh["call_site"], ovh
assert ovh["amortized_seconds_per_closed_step"] == 7.5, ovh  # 15 / 2
assert ovh["share_of_stage_wall_pct"] == 5.0, ovh            # 15 / 300
assert ovh["per_partial_block_reading"]["t_probe_share_of_partial_block_pct"] == 37.5, ovh
# Деление на хвостовой блок — не «цена шага»: у блока нет следующего старта
assert "хвост после цикла" in ovh["reading"], ovh["reading"]
assert "за владельцем" in ovh["decision_on_cheapening"], ovh
inst = ovh["hook_instrumentation"]
assert inst["wrapped_calls_counted"] == sum(inst["calls_by_kind"].values()), inst
assert inst["estimated_seconds"] == round(inst["wrapped_calls_counted"] * 10 / 1e6, 6), inst
assert inst["estimated_share_of_stage_pct"] < 0.01, inst      # инструментация — не 10% шага
no_probe = R.overhead_report(steps, ph, {"wall_seconds": 180.0, "steps_closed": 2})
assert no_probe["t_probe_seconds_total"] == 0, no_probe
assert no_probe["per_partial_block_reading"]["t_probe_share_of_partial_block_pct"] == 0.0, no_probe

# --- фон: reward/pass и деградировавшие траектории
d = pathlib.Path(tempfile.mkdtemp())
roll = d / "rollouts_log.jsonl"
rows = [
    {"step": 0, "reward": 1.0, "verifier_passed": True, "n_turns": 2, "n_assistant_tokens": 300,
     "n_tool_calls": 1, "hit_timeout": False, "text": "<think>ок</think>"},
    {"step": 0, "reward": 0.0, "verifier_passed": False, "n_turns": 15, "n_assistant_tokens": 900,
     "n_tool_calls": 0, "hit_timeout": True, "text": "длинный, но не завершившийся ответ"},
    {"step": 1, "reward": 0.0, "verifier_passed": False, "n_turns": 1, "n_assistant_tokens": 2,
     "n_tool_calls": 0, "hit_timeout": False, "text": "  "},
]
roll.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
rl = R.parse_rollouts(roll, cap=4096)
assert rl["trajectories"] == 3 and rl["steps"] == 2, rl
assert rl["degraded"]["truncated_at_cap"] == 0, rl["degraded"]      # все короче потолка
cap_hit = R.parse_rollouts(roll, cap=800)                            # потолок ниже — 1 упор
assert cap_hit["degraded"]["truncated_at_cap"] == 1, cap_hit["degraded"]
assert cap_hit["degraded"]["truncated_at_cap_rate"] == round(1 / 3, 4), cap_hit["degraded"]
assert R.parse_rollouts(roll)["degraded"]["truncated_at_cap"] == 0, "без потолка упор не считается"
assert rl["reward_mean"] == round(1.0 / 3, 4) and rl["pass_rate"] == round(1 / 3, 4), rl
assert rl["degraded"]["hit_timeout"] == 1 and rl["degraded"]["empty_text"] == 1, rl["degraded"]
assert rl["degraded"]["combined_hit_timeout_or_empty"] == 2, rl["degraded"]
assert rl["turns_mean"] == 6.0, rl["turns_mean"]

# --- энтропия: коридор 1.0–2.0 применим к политике (ADR-008 п.3), источник — лог
summary = {"log": {"step_lines": [{"step": 0, "entropy": 1.144}, {"step": 10, "entropy": 1.28}]},
           "rl_metrics": {"entropy": [1.1, 1.2, 0.4]}}
ent = R.entropy_report(summary, {"entropy_observations": []})
assert ent["from_log"]["in_corridor"] is True and ent["from_log"]["cadence_steps"] == 10, ent
assert ent["from_log"]["under_floor"] == 0, ent["from_log"]
assert ent["from_rl_metrics"]["under_floor"] == 1, ent["from_rl_metrics"]
assert ent["from_rl_metrics"]["cadence_steps"] == 1, ent["from_rl_metrics"]
# Сторона выхода из коридора важна: снизу — угроза коллапса, сверху — нет
assert ent["from_log"]["corridor_side"] == "inside", ent["from_log"]
assert ent["from_rl_metrics"]["corridor_side"] == "below", ent["from_rl_metrics"]  # mean 0.9
assert ent["from_rl_metrics"]["above_corridor"] == 0, ent["from_rl_metrics"]
assert ent["from_rl_metrics"]["below_corridor"] == 1, ent["from_rl_metrics"]
no_ent = R.entropy_report({"log": {"step_lines": []}}, None)
assert no_ent["from_log"]["points"] == [] and "не получена" in no_ent["from_log"]["note"], no_ent

# --- полнота замеров п.3: каждая величина либо значение, либо причина
full = {"step_seconds": {"mean": 91.0}, "phase_split": {"shares_pct": {"t_generate": 55.0},
        "generation": {"tok_per_s": 120.0}}, "memory": {"peak_used_gb": 70.0},
        "resync": {"total": 9}, "entropy": {"from_log": {"mean": 1.2}},
        "background": {"reward_mean": 0.5, "pass_rate": 0.5, "degraded": {"hit_timeout": 1}},
        "steps_completed": 100}
c = R.measurements_completeness(full)
assert c["_summary"] == {"items": 9, "obtained": 9, "not_obtained": []}, c["_summary"]
c2 = R.measurements_completeness({})
assert c2["_summary"]["obtained"] == 0 and len(c2["_summary"]["not_obtained"]) == 9, c2["_summary"]
assert all(v["not_obtained_reason"] for k, v in c2.items() if isinstance(v, dict)
           and k != "_summary"), c2

# --- экстраполяция: 500 шагов и три сида, с допущениями и оговоркой о чекпойнте
args = R.parse_args(["--ts", "20260101-0000"])
args.startup_seconds = 120.0
ex = R.extrapolate({"step_seconds": {"mean": 90.0}, "steps_closed": 100}, args)
assert ex["rl_500_steps"]["hours"] == round((120.0 + 500 * 90.0) / 3600, 2), ex["rl_500_steps"]
assert ex["three_seeds"]["hours"] == round((120.0 + 1500 * 90.0) / 3600, 2), ex["three_seeds"]
assert any("историческом" in c for c in ex["caveats"]), ex["caveats"]
assert any("линейно" in a for a in ex["assumptions"]), ex["assumptions"]
assert "не от чего" in R.extrapolate({"steps_closed": 0}, args)["error"]

# --- две границы 500 шагов: прямой замер внизу, экстраполяция вверху, без среднего
hist = {"available": True, "path": "p", "steps_covered": 480, "step_seconds_mean": 88.1,
        "stage_wall_hours_measured": 12.27,
        "trend_first_vs_second_half": {"change_pct": -2.9}}
cap_ctx = {"cap_tokens": 4096, "our_truncated_at_cap": 7, "our_trajectories": 8,
           "our_truncated_at_cap_rate": 0.875, "historical_truncated_at_cap_rate": 0.0862,
           "historical_trajectories": 800}
ex2 = R.extrapolate({"step_seconds": {"mean": 124.0}, "steps_closed": 99}, args, hist, cap_ctx)
upper = round((120.0 + 500 * 124.0) / 3600, 2)
assert ex2["bounds"]["upper"]["rl_500_steps_hours"] == upper, ex2["bounds"]["upper"]
assert ex2["bounds"]["lower"]["rl_500_steps_hours"] == 12.27, ex2["bounds"]["lower"]
assert ex2["bounds"]["upper"]["three_seeds_hours"] == round((120.0 + 1500 * 124.0) / 3600, 2), \
    ex2["bounds"]["upper"]
assert ex2["bounds"]["three_seeds_hours_range"] == [36.8, 51.7], ex2["bounds"]
# Три сида считаются одним способом: 3 × округлённая цена 500 шагов не подменяет
# прямо посчитанное значение (иначе два разных числа под одним именем)
assert ex2["bounds"]["upper"]["three_seeds_hours"] == ex2["three_seeds"]["hours"], ex2["bounds"]
assert ex2["range_summary"]["three_seeds_hours_range"] == ex2["bounds"]["three_seeds_hours_range"], \
    ex2["range_summary"]
assert "не считается" in ex2["bounds"]["no_averaging"], ex2["bounds"]["no_averaging"]
# Оговорка про «упор в потолок хода» обязательна: без неё верхняя граница читается
# как ожидаемая цена ревизионного прогона, а она измерена на другой политике.
assert any("упор в потолок хода" in c for c in ex2["caveats"]), ex2["caveats"]
tc = ex2["turn_ceiling_caveat"]
assert tc["our_rate"] == 0.875 and tc["historical_truncated_at_cap_rate"] == 0.0862, tc
assert "верхняя граница" in tc["reading"], tc
# Без контекста упора оговорки нет — и это видно, а не подменяется нулём
ex3 = R.extrapolate({"step_seconds": {"mean": 124.0}, "steps_closed": 99}, args, hist)
assert "turn_ceiling_caveat" not in ex3, ex3.get("turn_ceiling_caveat")

# --- сценарий стадии: хост-пути и пути контейнера не перепутаны
script = R.build_stage_script(ts=args.ts, args=args)
assert 'RUN_DIR="/home/user/experiments/rl-probe-compact-20260101-0000"' in script, "нет хост-пути"
assert 'CTR_RUN_DIR="/workspace/experiments/rl-probe-compact-20260101-0000"' in script, "нет пути контейнера"
assert 'python3 "$CTR_RUN_DIR/rl_probe_hook.py"' in script, "обвязка зовётся не по пути контейнера"
assert "bash /home/user/gb10-shared/nvrm-storm/safe_start.sh -d 60 -i 5 -- docker run" in script
assert "smoke_mem_sampler.sh" in script and "RL_PROBE_STAGE_EXIT=$RC" in script
assert "--pipeline /workspace/shared/laguna_pipeline_v8.py" in script
assert "--stage rl" in script and "--rl_steps 100" in script and "--seed 42" in script
assert R.RL_DATA_CTR in script, "пул ревизии не передан стадии"
assert "-e VLLM_GPU_UTIL=0.32" in script and "-e VLLM_KV_CACHE_BYTES=8589934592" in script
assert "-e RESYNC_EVERY=10" in script, "resync_every не зафиксирован явно (=10, а не манифестные 25)"
assert "llm-platform" not in script, "разведка не должна упоминать платформенные контейнеры"
# Стартовый чекпойнт подключается относительной ссылкой: абсолютный хост-путь не
# существует в контейнере, абсолютный путь контейнера — на хосте.
assert '../../laguna_qwen25-05b/checkpoints/sft_checkpoint_final.pt' in script, script[-800:]
assert "/home/user/experiments/laguna_qwen25-05b" not in script.replace(R.DEFAULT_SFT_CKPT, ""), \
    "абсолютный хост-путь чекпойнта внутри контейнера не разрешится"

# --- страж: энтропия ниже порога два наблюдения подряд → стоп (без стенда)
class FakeGuard(R.Guard):
    def __init__(self, a):
        super().__init__("no-such-host.invalid", "c", a, deadline=1e18)
    def trip(self, reason):
        self.reasons.append(reason); self._stopped = True
g = FakeGuard(args)
g.note_entropy(0, 1.1, "")
assert not g.tripped, "здоровая энтропия не должна останавливать стадию"
g.note_entropy(10, 0.42, "")
assert not g.tripped, "одно низкое наблюдение — ещё не коллапс (нужно два подряд)"
g.note_entropy(20, 0.30, "")
assert g.tripped and "mode collapse" in g.reasons[0], g.reasons
g2 = FakeGuard(args)
g2.note_entropy(10, 0.30, ""); g2.note_entropy(20, 1.30, ""); g2.note_entropy(30, 0.20, "")
assert not g2.tripped, "серия прервана здоровым наблюдением — счётчик сбрасывается"
g3 = FakeGuard(args)
g3.note_line("NVRM: Xid (PCI:0000:01:00): 51, pid=1234")
g3.note_line("NVRM: Xid (PCI:0000:01:00): 51, pid=1234")
assert g3.tripped and "пауза" in g3.reasons[0], g3.reasons
sys.exit(0)
PY

# Сборка evidence из артефактов — сквозная проверка на синтетическом каталоге
# прогона. Здесь ловится то, что иначе стоило бы трёх часов: падение сборщика
# отчёта после завершённого прогона (данные на стенде есть, доказательной базы нет).
expect_exit 0 "evidence разведки собирается из артефактов (синтетика)" \
  python3 - <<'PY'
import json, pathlib, sys, tempfile
sys.path.insert(0, "tools")
import run_rl_probe as R

d = pathlib.Path(tempfile.mkdtemp()) / "rl-probe-20260101-0000"
(d / "logs").mkdir(parents=True)
hook = [
    {"event": "start", "ts": 1000.0, "pipeline": "p", "pipeline_args": ["--stage", "rl"],
     "patched": ["RLDataset.sample"], "pid": 1, "forward_instrumented": True,
     "tasks_per_step": 4, "group_size": 2},
    {"event": "engine_init", "i": 1, "seconds": 159.7, "ts": 1159.0, "kind": "initial"},
    {"event": "step_start", "i": 0, "ts": 1160.0},
    {"event": "step", "i": 0, "dt": 123.236, "partial": False, "ts": 1283.0,
     "t_generate": 87.923, "t_policy_fwd": 8.0, "t_other_fwd": 4.0, "t_gen_eval": 0.0,
     "t_resync": 0.0, "t_ckpt": 0.0, "t_probe": 0.0, "n_generate_calls": 8,
     "gen_tokens": 4312, "prompt_tokens": 900, "n_hotsync": 0, "n_engine_init": 0,
     "n_policy_fwd": 8, "n_other_fwd": 8},
    {"event": "step_start", "i": 1, "ts": 1283.5},
    {"event": "step", "i": 1, "dt": 101.0, "partial": False, "ts": 1384.5,
     "t_generate": 70.0, "t_policy_fwd": 7.0, "t_other_fwd": 3.0, "t_gen_eval": 9.0,
     "t_resync": 12.0, "t_ckpt": 4.0, "t_probe": 0.0, "n_generate_calls": 7,
     "gen_tokens": 3600, "prompt_tokens": 800, "n_hotsync": 1, "n_engine_init": 0,
     "n_policy_fwd": 7, "n_other_fwd": 7},
    {"event": "step_start", "i": 2, "ts": 1385.0},
    {"event": "step", "i": 2, "dt": 30.0, "partial": True, "ts": 1415.0, "t_generate": 20.0,
     "t_probe": 15.0, "n_generate_calls": 4, "n_policy_fwd": 8, "n_other_fwd": 8},
    {"event": "end", "ts": 1415.5, "wall_seconds": 415.5, "steps_closed": 2,
     "steps_started": 3, "samples_total": 12, "tasks_per_step": 4, "group_size": 2,
     "engine_inits_total": 1, "error": None, "torch_peak_allocated_bytes": 6 * 2**30,
     "torch_peak_reserved_bytes": 9 * 2**30},
]
(d / "rl_probe.jsonl").write_text("\n".join(json.dumps(r) for r in hook) + "\n", encoding="utf-8")
(d / "mem_rl.jsonl").write_text("\n".join(json.dumps(r) for r in [
    {"ts": 1, "mem_total_kb": 128000000, "mem_available_kb": 84000000, "mem_free_kb": 1,
     "cached_kb": 1, "containers": "laguna-rlprobe-20260101-0000=12.5GiB / 121GiB"},
    {"ts": 2, "mem_total_kb": 128000000, "mem_available_kb": 56000000, "mem_free_kb": 1,
     "cached_kb": 1, "containers": "laguna-rlprobe-20260101-0000=44GiB / 121GiB"},
]) + "\n", encoding="utf-8")
(d / "logs" / "rl.log").write_text("\n".join([
    "06:55:39 [INFO] STAGE: RL (multi-turn ≤15, vLLM) | Qwen/Qwen2.5-0.5B",
    "06:58:34 [INFO] vLLM engine готов (enforce_eager=False, weight_transfer=True:ipc)",
    "06:58:34 [INFO] RL: группы 4×2, baseline=spr (pure=False), max_steps=100",
    "07:00:37 [INFO] RL step 0/100 | reward=0.500 | pass=50% | turns=1.0 | len=4312t | "
    "kl=0.0000 | entropy=2.170 | clip=0.000 | adv=0.000 | adv≠0=100% | active=27992/27992 | mem=6.5GB",
    "07:02:18 [INFO] RL step 10/100 | reward=0.475 | pass=50% | turns=3.0 | len=859t | "
    "kl=0.0010 | entropy=1.121 | clip=0.000 | adv=-0.025 | adv≠0=100% | active=27992/27992 | mem=5.5GB",
    "07:02:19 [INFO] HOT-SYNC: веса подменены за 3s (step 10)",
    "07:02:30 [INFO] RESYNC: веса роллаута обновлены (step 10)",
    "07:02:40 [INFO] GEN-EVAL step 10: ppl_general=126.9 (+0.0% vs base), ppl_domain=24.9 (-0.0%)",
]) + "\n", encoding="utf-8")
(d / "rollouts_log.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
    {"step": 0, "reward": 1.0, "verifier_passed": True, "n_turns": 1, "n_assistant_tokens": 4200,
     "n_tool_calls": 2, "hit_timeout": False, "text": "<think>ответ</think>"},
    {"step": 0, "reward": 0.0, "verifier_passed": False, "n_turns": 15, "n_assistant_tokens": 4050,
     "n_tool_calls": 1, "hit_timeout": True, "text": "не завершился"},
]) + "\n", encoding="utf-8")
(d / "rl_metrics.json").write_text(json.dumps({"rewards": [0.5], "pass_rates": [0.5],
                                               "lengths": [4000], "entropy": [2.17, 1.9],
                                               "clip_frac": [0.0]}), encoding="utf-8")

args = R.parse_args(["--ts", "20260101-0000"])
args.startup_seconds = 160.0
summary = R.analyze(d, args)
assert summary["hook"]["startup_seconds"] == 160.0, summary["hook"]
assert summary["phases"]["steps_closed"] == 2, summary["phases"]
assert summary["phases"]["step_seconds"]["mean"] == 112.12, summary["phases"]["step_seconds"]
assert summary["memory"]["peak_used_gb"] == 68.66, summary["memory"]   # (128000000-56000000)/1048576
assert summary["memory"]["torch_peak_allocated_gb"] == 6.0, summary["memory"]
assert summary["log"]["groups"]["tasks_per_step"] == 4, summary["log"]["groups"]
assert summary["rollouts"]["degraded"]["combined_hit_timeout_or_empty"] == 1, summary["rollouts"]

stage = {"stage": "rl", "exit": 0, "ok": True, "guard": {"tripped": False, "reasons": [],
         "entropy_observations": [{"step": 0, "entropy": 2.17}], "nvrm_incidents_in_log": 0}}
pre = {"host": "gb10-fast", "ok": True, "checks": [{"name": "memory", "ok": True, "detail": "ok"}],
       "dmesg_before": {"nvrm_xid": 3, "oom": 0}}
post = {"containers": ["llm-platform-runtime"], "probe_containers_running": [],
        "container_closed": True, "platform_alive": ["llm-platform-runtime"],
        "platform_lost": [], "free_g": "121 30 90", "mem_free_gb": 88.0,
        "dmesg_after": {"nvrm_xid": 3, "oom": 0}}
ev = R.build_evidence(d, args, pre, stage, summary, None, post)
ev["status"] = "done"
assert ev["stage"] == "S3-pre" and ev["status"] == "done", ev["stage"]
assert ev["incidents"]["dmesg_nvrm_xid_delta"] == 0, ev["incidents"]
assert ev["config"]["hyperparameters"]["kl_coef"] == 0.01, ev["config"]["hyperparameters"]
assert ev["config"]["hyperparameters"]["resync_every_manifest_default"] == 25, ev["config"]
assert "не чекпойнт ревизии" in ev["config"]["start_checkpoint_note"], ev["config"]
# Число шагов — из обвязки (3 старта), а не из строк лога (последняя на шаге 10):
# строки печатаются каждые 10 шагов, и по ним вышло бы 11 — грубее и неверно.
assert ev["measurements"]["steps_completed"] == 3, ev["measurements"]["steps_completed"]
assert ev["measurements"]["steps"]["steps_from_log_lines"] == 11, ev["measurements"]["steps"]
assert "обвязка" in ev["measurements"]["steps"]["source"], ev["measurements"]["steps"]
comp = ev["measurements"]["completeness_of_ADR010_p3"]
assert comp["_summary"] == {"items": 9, "obtained": 9, "not_obtained": []}, comp["_summary"]
share = round((87.923 + 70.0) / (123.236 + 101.0) * 100, 1)
assert comp["rollout_vs_train_share"]["value"]["t_generate"] == share, comp["rollout_vs_train_share"]
assert comp["policy_entropy"]["value"] == round((2.17 + 1.121) / 2, 4), comp["policy_entropy"]
# (2.170 + 1.121)/2 = 1.646 — в коридоре 1.0–2.0: стоп-условие не должно было сработать
assert ev["measurements"]["entropy"]["from_log"]["in_corridor"] is True, ev["measurements"]["entropy"]
assert ev["measurements"]["entropy"]["from_log"]["under_floor"] == 0, ev["measurements"]["entropy"]
assert ev["measurements"]["entropy"]["from_rl_metrics"]["cadence_steps"] == 1, ev["measurements"]
assert ev["measurements"]["resync"]["total"] == 1, ev["measurements"]["resync"]
# Два независимых источника ресников обязаны сойтись: обвязка и лог пайплайна
cross = ev["measurements"]["resync_cross_check"]
assert cross["from_hook"] == 1 and cross["from_log_hotsync_lines"] == 1 and cross["agree"], cross
assert ev["measurements"]["background"]["reward_mean"] == 0.5, ev["measurements"]["background"]
assert ev["extrapolation"]["rl_500_steps"]["hours"] == round((160.0 + 500 * 112.12) / 3600, 2), \
    ev["extrapolation"]
assert any("историческом" in c for c in ev["extrapolation"]["caveats"]), ev["extrapolation"]
# Накладные расходы обвязки: пробы пайплайна осели в незакрытом блоке и в средние
# закрытых шагов не попали — значит, «цена шага» из них не выводится.
ovh = ev["measurements"]["measurement_overhead"]
assert ovh["t_probe_seconds_total"] == 15.0, ovh
assert ovh["t_probe_seconds_in_closed_steps"] == 0.0, ovh
assert ovh["runs_per_stage"] == 1 and "1547" in ovh["call_site"], ovh
assert ovh["amortized_seconds_per_closed_step"] == 7.5, ovh          # 15 / 2 закрытых шага
assert ovh["share_of_stage_wall_pct"] == round(100 * 15.0 / 415.5, 3), ovh
assert ovh["hook_instrumentation"]["wrapped_calls_counted"] == 66, ovh["hook_instrumentation"]
# Три основания времени шага — рядом; экстраполяция идёт по шкале обвязки
b = ev["measurements"]["step_seconds_bases"]
assert b["hook_mean_closed_steps"] == 112.12 and b["wall_over_closed_steps"] == 207.75, b
assert b["wall_over_started_steps"] == round(415.5 / 3, 2), b
assert b["basis_used_for_extrapolation"] == "hook_mean_closed_steps", b
# Состояние стадии: стартов/закрытых, ресников и инициализаций, ошибки
lc = ev["measurements"]["hook_lifecycle"]
assert lc["steps_started"] == 3 and lc["steps_closed"] == 2 and lc["partial_blocks"] == 1, lc
assert lc["engine_inits_total"] == 1 and lc["error"] is None, lc
assert lc["group_size"] == 2 and lc["tasks_per_step"] == 4, lc
# Точная проверка «упор в потолок хода» — по gen_tokens, а не по косвенному признаку
cp = ev["measurements"]["cost_interpretation"]["generation_cap_pressure"]
assert cp["cap_tokens_per_call"] == 4096 and cp["trajectories_closed_steps"] == 2, cp
assert cp["exact_generated_tokens_closed_steps"] == 7912, cp          # 4312 + 3600
assert cp["exact_tokens_per_trajectory"] == 3956.0, cp
assert cp["cap_utilisation_pct"] == 96.6, cp
assert cp["proxy_over_exact_ratio"] == round((4200 + 4050) / 7912, 4), cp
assert "не токены ассистента" in cp["proxy_field_defect"], cp
# Провенанс обвязки обязан доехать до отчёта: evidence_limits ссылается на
# instrument, и поле, которое считается, но не пишется, — это ложная ссылка
ins = ev["instrument"]
assert ins["case_runner_sha256"] and ins["case_hook_sha256"], ins
assert "расхождение" in ins["note"], ins
# Отчёт обязан быть сериализуемым и не содержать NaN/Inf (json.dump с ними даёт не-JSON)
json.dumps(ev, ensure_ascii=False, allow_nan=False)
PY

# Независимая сверка с историческим 500-шаговым прогоном: разбор лога ресников.
expect_exit 0 "сверка с историческим 500-шаговым прогоном (синтетический лог)" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import run_rl_probe as R

# Строки ресника несут время: интервал между соседними = время RESYNC_EVERY шагов.
log = "\n".join([
    "09:27:02 [INFO] RL: группы 4×2, baseline=spr (pure=False), max_steps=500",
    "09:46:43 [INFO] HOT-SYNC: веса подменены за 0s (step 10)",
    "10:01:42 [INFO] HOT-SYNC: веса подменены за 0s (step 20)",
    "10:20:58 [INFO] HOT-SYNC: веса подменены за 3s (step 30)",
    "10:36:00 [INFO] HOT-SYNC: веса подменены за 0s (step 40)",
    "10:52:00 [INFO] HOT-SYNC: веса подменены за 0s (step 50)",
])
cc = R.parse_historical_rl_log(log)
assert cc["available"] is True, cc
assert cc["intervals"] == 4 and cc["steps_covered"] == 40, cc
# интервалы: 899/10=89.9, 1156/10=115.6, 902/10=90.2, 960/10=96.0
assert cc["step_seconds_mean"] == 97.9, cc                          # (89.9+115.6+90.2+96.0)/4
assert cc["step_seconds_min"] == 89.9 and cc["step_seconds_max"] == 115.6, cc
assert cc["first_leg_with_warmup"]["seconds_per_step"] == 118.1, cc  # (09:46:43−09:27:02)/10
assert cc["groups_from_log"]["tasks_per_step"] == 4, cc["groups_from_log"]
assert cc["trend_first_vs_second_half"]["first_half_mean"] == 102.8, cc["trend_first_vs_second_half"]
assert cc["trend_first_vs_second_half"]["change_pct"] == -9.4, cc["trend_first_vs_second_half"]
assert "не равенство" in cc["caveat"], cc["caveat"]
# Одна строка ресника — интервалов нет: сверка недоступна, а не «ноль секунд»
one = R.parse_historical_rl_log("10:01:42 [INFO] HOT-SYNC: веса подменены за 0s (step 10)")
assert one["available"] is False and "не построить" in one["reason"], one
empty = R.parse_historical_rl_log("")
assert empty["available"] is False, empty

# Сверка входит в экстраполяцию и не подменяет измеренное число
args = R.parse_args(["--ts", "20260101-0000"])
ex = R.extrapolate({"step_seconds": {"mean": 124.6}, "steps_closed": 100}, args, cc)
v = ex["cross_check_verdict"]
assert v["measured_step_seconds"] == 124.6 and v["historical_step_seconds"] == 97.9, v
assert v["consistent_order_of_magnitude"] is True, v
assert any("независимо" in a for a in ex["assumptions"]), ex["assumptions"]
# Полное время стадии из отметок лога и двусторонняя оценка 500 шагов
assert cc["stage_wall_seconds_measured"] == 5098, cc      # 09:27:02 → 10:52:00
assert cc["stage_wall_hours_measured"] == 1.42, cc
assert ex["rl_500_steps_measured_historically"]["hours"] == 1.42, ex
rng = ex["range_summary"]
assert rng["rl_500_steps_hours"] == sorted([ex["rl_500_steps"]["hours"], 1.42]), rng
# Три сида: нижняя граница — 3 × цена исторического прогона (своего замера трёх
# сидов у него нет), верхняя — прямо посчитанное startup + 1500 шагов, а не
# 3 × округлённая цена 500 шагов: двойное округление давало два разных числа
# под одним именем (51.93 против 51.92) в одном и том же отчёте.
seeds_lower = round(3 * min(rng["rl_500_steps_hours"]), 1)
assert rng["three_seeds_hours_range"] == [seeds_lower, ex["three_seeds"]["hours"]], rng
assert ex["bounds"]["upper"]["three_seeds_hours"] == ex["three_seeds"]["hours"], ex["bounds"]
assert ex["bounds"]["lower"]["three_seeds_hours"] == seeds_lower, ex["bounds"]["lower"]
assert ex["bounds"]["no_averaging"], ex["bounds"]
assert "обе границы" in rng["reading"], rng
ex_no = R.extrapolate({"step_seconds": {"mean": 124.6}, "steps_closed": 100}, args, None)
assert ex_no["cross_check_historical_500_steps"]["available"] is False, ex_no
assert "cross_check_verdict" not in ex_no, ex_no
sys.exit(0)
PY

# Обвязка RL: границы шага, время фаз, ресники — на синтетическом «пайплайне»,
# который повторяет структуру вызовов настоящего (sample → generate → train →
# resync → ckpt), но без torch/vLLM. Здесь ловится то, что на стенде стоило бы
# часов: перепутанные границы шага или непропатченный generate.
expect_exit 0 "rl_probe_hook: границы шага и фазы на синтетическом пайплайне" \
  python3 - <<'PY'
import json, os, pathlib, sys, tempfile
sys.path.insert(0, "tools")
import rl_probe_hook as H

tmp = pathlib.Path(tempfile.mkdtemp())
fake = tmp / "fake_pipeline.py"
fake.write_text('''
import os, time

TASKS_PER_STEP, R_PER_TASK = 4, 2
RESYNC_EVERY = int(os.environ.get("RESYNC_EVERY", "10"))

class _Out:
    class _O:
        token_ids = [1, 2, 3]
    outputs = [_O()]
    prompt_token_ids = [1, 2]

class _LLM:
    def generate(self, prompts, params, *a, **kw):
        time.sleep(0.01)
        return [_Out() for _ in prompts]

class RLDataset:
    def sample(self):
        return 0

class VLLMGenerator:
    def __init__(self, *a, **kw):
        time.sleep(0.05)
        self.llm = _LLM()
    def hot_sync_weights(self, model):
        time.sleep(0.02)

def save_checkpoint_atomic(*a, **kw):
    time.sleep(0.03)

def _general_eval_step(*a, **kw):
    time.sleep(0.01)

def run_probes(*a, **kw):
    time.sleep(0.01)

def main():
    ds = RLDataset()
    gen = VLLMGenerator("x")
    for step in range(3):
        for _ in range(TASKS_PER_STEP):
            ds.sample()
        for _ in range(4):
            gen.llm.generate(["p"] * 2, None)
        if step > 0:
            gen.hot_sync_weights(None)
            save_checkpoint_atomic()
            _general_eval_step()
    run_probes()
''', encoding="utf-8")
metrics = tmp / "m.jsonl"
# Инструментирование и запуск «стадии» — как это делает обвязка, но без CLI:
# CLI-путь импортирует transformers (обёртка forward'ов), а тест не должен
# зависеть от ML-стека — проверяется сама разводка патчей и границ шага.
module = H.load_pipeline(fake)
rec = H.StepRecorder(metrics, tasks_per_step=4, group_size=2)
patched = H.instrument_pipeline(module, rec)
assert sorted(patched) == ["RLDataset.sample", "VLLMGenerator.__init__/generate",
                           "VLLMGenerator.hot_sync_weights", "_general_eval_step",
                           "run_probes", "save_checkpoint_atomic"], sorted(patched)
rec.start({"pipeline": str(fake), "pipeline_args": [], "patched": sorted(patched)})
module.main()
rec.finish(None)
rows = [json.loads(l) for l in metrics.read_text(encoding="utf-8").splitlines() if l.strip()]
steps = [r for r in rows if r["event"] == "step"]
closed = [s for s in steps if not s["partial"]]
partial = [s for s in steps if s["partial"]]
assert len(closed) == 2 and len(partial) == 1, steps          # 3 старта → 2 закрытых + хвост
assert [s["i"] for s in closed] == [0, 1], steps
# 4 волны генерации × 2 промпта × 3 токена = 24 токена на шаг, время ≈ 4×0.01
assert all(s["n_generate_calls"] == 4 for s in closed), closed
assert all(s["gen_tokens"] == 24 and s["prompt_tokens"] == 16 for s in closed), closed
assert all(s["t_generate"] >= 0.04 for s in closed), closed
# resync/чекпоинт/GEN-EVAL — только у шага 1 (step>0), и время ресника учтено
assert closed[0]["n_hotsync"] == 0 and closed[0]["t_ckpt"] == 0.0, closed[0]
assert closed[1]["n_hotsync"] == 1 and closed[1]["t_resync"] >= 0.02, closed[1]
assert closed[1]["t_ckpt"] >= 0.03 and closed[1]["t_gen_eval"] >= 0.01, closed[1]
# dt закрытого блока не меньше суммы его внешних фаз
assert closed[1]["dt"] >= (closed[1]["t_generate"] + closed[1]["t_ckpt"]
                           + closed[1]["t_resync"] + closed[1]["t_gen_eval"]), closed[1]
inits = [r for r in rows if r["event"] == "engine_init"]
assert len(inits) == 1 and inits[0]["kind"] == "initial", inits
end = [r for r in rows if r["event"] == "end"][0]
assert end["steps_started"] == 3 and end["samples_total"] == 12, end
assert end["error"] is None, end
# старт шага помечен явно (по нему считается startup_seconds)
starts = [r for r in rows if r["event"] == "step_start"]
assert [s["i"] for s in starts] == [0, 1, 2], starts
# разбор обвязки раннером: startup_seconds = первый старт − старт обвязки
sys.path.insert(0, "tools")
import run_rl_probe as R
parsed = R.parse_hook_jsonl(metrics)
assert parsed["startup_seconds"] is not None and parsed["startup_seconds"] >= 0, parsed
assert len(parsed["steps"]) == 3 and len(parsed["engine_inits"]) == 1, parsed
ph = R.phase_summary(parsed["steps"])
assert ph["steps_closed"] == 2 and ph["resync"]["hotsync_count"] == 1, ph
PY

# Обёртка forward'а политики: grad-режим отделяет обучение от прочих проходов.
if python3 -c "import torch" 2>/dev/null; then
expect_exit 0 "rl_probe_hook: forward политики против прочих проходов (torch)" \
  python3 - <<'PY'
import pathlib, sys, tempfile
sys.path.insert(0, "tools")
import torch
import rl_probe_hook as H

path = pathlib.Path(tempfile.mkdtemp()) / "m.jsonl"
rec = H.StepRecorder(path, tasks_per_step=4, group_size=2)

class M:
    # nn.Module зовётся через __call__ → self.forward: обёртка ставится на forward,
    # и двойник обязан повторять это, иначе тест проверял бы не тот механизм.
    def __call__(self, *a, **kw):
        return self.forward(*a, **kw)

    def forward(self, input_ids=None, **kw):
        return None

m = H.wrap_model_forward(M(), rec)
ids = torch.zeros(2, 5, dtype=torch.long)
m(input_ids=ids)                                  # как обучающий forward RL: grad включён
with torch.no_grad():
    m(input_ids=ids)                              # как снапшот ref_model / PPL
assert rec.b["n_policy_fwd"] == 1 and rec.b["n_other_fwd"] == 1, rec.b
assert rec.b["t_policy_fwd"] > 0 and rec.b["t_other_fwd"] > 0, rec.b
# позиционные аргументы тоже считаются токенами (пайплайн зовёт модель по имени,
# но обёртка не должна зависеть от этого)
m(ids)
assert rec.b["n_policy_fwd"] == 2, rec.b
rec.on_sample(); rec.on_sample(); rec.on_sample(); rec.on_sample()
rec.on_sample()                                   # начало второго шага → закрытие первого
rec.finish(None)
import json
rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
closed = [r for r in rows if r.get("event") == "step" and not r["partial"]]
assert len(closed) == 1 and closed[0]["i"] == 0 and closed[0]["partial"] is False, rows
assert closed[0]["n_policy_fwd"] == 2, closed[0]
end = [r for r in rows if r.get("event") == "end"][0]
assert end["steps_started"] == 2 and end["samples_total"] == 5, end
PY
fi

echo
echo "== 13. check_resource_owner.sh (AD-9) + детерминизм-проба S3a (ADR-009) =="
# Стенд подменяется целиком, как в §8: nvidia-smi объявляет GB10 (локальный режим,
# ssh не нужен), а crontab и /proc/meminfo — шимы. Страж хозяина читает ровно эти
# три источника, и подменять надо все три: иначе тест проверял бы машину разработчика,
# а не правило.
cat > "$TMP/fakebin/crontab" <<'SH'
#!/bin/bash
# FAKE_CRONTAB_FILE — содержимое `crontab -l`; FAKE_CRONTAB_MODE:
#   ok — строки есть; empty — «нет crontab у пользователя» (это НЕ отказ чтения);
#   broken — spool не читается (вердикт обязан быть «недоказуемо»).
if [ "$1" = "-l" ]; then
  case "${FAKE_CRONTAB_MODE:-ok}" in
    ok)     cat "${FAKE_CRONTAB_FILE:-/dev/null}"; exit 0 ;;
    empty)  echo "no crontab for roman" >&2; exit 1 ;;
    broken) echo "crontab: cannot open spool" >&2; exit 3 ;;
  esac
fi
exit 0
SH
cat > "$TMP/fakebin/cat" <<'SH'
#!/bin/bash
# Подменяется только /proc/meminfo; всё прочее — настоящий cat (им пользуются шимы).
if [ "$1" = "/proc/meminfo" ]; then exec /bin/cat "${FAKE_MEMINFO_FILE:-/proc/meminfo}"; fi
exec /bin/cat "$@"
SH
chmod +x "$TMP/fakebin/crontab" "$TMP/fakebin/cat"

# fixtуры: память, права сторожа, флаг паузы, три раскладки крона
awk 'BEGIN{printf "MemTotal:      127650000 kB\nMemFree: 100000 kB\nMemAvailable:  %d kB\nCached: 100000 kB\n", 78*1048576}' > "$TMP/mem_78.txt"
awk 'BEGIN{printf "MemTotal:      127650000 kB\nMemFree: 100000 kB\nMemAvailable:  %d kB\nCached: 100000 kB\n", 40*1048576}' > "$TMP/mem_40.txt"
printf '#!/bin/bash\ntrue\n' > "$TMP/watchdog_noexec.sh"; chmod 644 "$TMP/watchdog_noexec.sh"
#: Крон, где и сторож, и @reboot — в закомментированных (DISABLED) строках: пауза
#: выполнена полностью, и ни один из двух блокирующих критериев не срабатывает.
printf '#DISABLED-20260914 @reboot sleep 300 && nohup bash /home/user/gb10-shared/restart_v12.sh >/dev/null 2>&1 &\n*/30 * * * * /bin/bash /home/user/gb10-shared/laguna_status_cron.sh\n' > "$TMP/cron_paused_only.txt"
printf '#!/bin/bash\ntrue\n' > "$TMP/watchdog_exec.sh";   chmod 755 "$TMP/watchdog_exec.sh"
printf 'lock\n' > "$TMP/REVISION-WINDOW.lock"
printf '#DISABLED-20260914 */15 * * * * /bin/bash /home/user/gb10-shared/laguna_watchdog_v12.sh\n*/30 * * * * /bin/bash /home/user/gb10-shared/laguna_status_cron.sh\n' > "$TMP/cron_paused.txt"
printf '*/15 * * * * /bin/bash /home/user/gb10-shared/laguna_watchdog_v12.sh\n' > "$TMP/cron_watchdog.txt"
printf '@reboot sleep 300 && nohup bash /home/user/gb10-shared/restart_v12.sh > /home/user/experiments/reboot_autostart.log 2>&1 &\n' > "$TMP/cron_autostart.txt"
#: Две фикстуры контура лесенки: флаг паузы читающий (как restart_v12.sh после S3c)
#: и доревизионный, который флага не знает. Страж обязан различать их вердиктом:
#: флаг без поддержки — декларация, а не защита (AD-9, уточнение 14.09.2026).
printf '#!/bin/bash\nif [ -f "$SHARED/REVISION-WINDOW.lock" ]; then echo "[REVISION-WINDOW] пауза"; exit 0; fi\ntrue\n' > "$TMP/restart_supports.sh"
printf '#!/bin/bash\n# доревизионный: docker kill + подъём лесенки, флага не знает\ntrue\n' > "$TMP/restart_nosupport.sh"

# owner_guard — вызов стража на подменённом стенде. Обёртка лежит файлом, а не
# функцией: expect_exit передаёт команду как "$@", и префиксное присваивание
# (FAKE_MEMINFO_FILE=... owner_guard ...) в этом случае осталось бы аргументом, а
# не переменной окружения — тест молча проверял бы не тот стенд.
OWNER_GUARD="$TMP/owner_guard.sh"
cat > "$OWNER_GUARD" <<SH
#!/usr/bin/env bash
exec env PATH="$TMP/fakebin:\$PATH" \
  FAKE_PS_FILE="\${FAKE_PS_FILE:-$TMP/ps_one_load.txt}" \
  FAKE_MEMINFO_FILE="\${FAKE_MEMINFO_FILE:-$TMP/mem_78.txt}" \
  FAKE_CRONTAB_FILE="\${FAKE_CRONTAB_FILE:-$TMP/cron_paused.txt}" \
  FAKE_CRONTAB_MODE="\${FAKE_CRONTAB_MODE:-ok}" \
  bash "$CASE_ROOT/tools/check_resource_owner.sh" \
    --watchdog "\${WATCHDOG:-$TMP/watchdog_noexec.sh}" \
    --lock "\${LOCK:-$TMP/no-such.lock}" \
    --restart-script "\${RESTART_SCRIPT:-$TMP/restart_nosupport.sh}" "\$@"
SH
chmod +x "$OWNER_GUARD"

# owner_guard <аргументы стража> — вызов на стенде по умолчанию (без паузы).
owner_guard() { "$OWNER_GUARD" "$@"; }

expect_exit 0 "пауза соблюдена: сторож неисполняем, строки крона нет" \
  owner_guard --stage cpt
expect_contains "МОЖНО СТАРТОВАТЬ" "вердикт назван словом, а не только кодом возврата" \
  owner_guard --stage cpt
expect_contains "порога 35" "порог CPT назван явно (ADR-008)" \
  owner_guard --stage cpt
expect_contains "порога 49" "порог RL назван явно (ADR-011)" \
  owner_guard --stage rl
# Переопределение путей — префиксным присваиванием: функция читает их через
# ${VAR:-default}, поэтому тест не подменяет стенд, а называет другой его файл.
expect_exit 0 "флаг паузы перебивает состояние сторожа" \
  env WATCHDOG="$TMP/watchdog_exec.sh" LOCK="$TMP/REVISION-WINDOW.lock" \
      RESTART_SCRIPT="$TMP/restart_supports.sh" "$OWNER_GUARD" --stage cpt
expect_contains "флаг паузы" "причина допуска названа флагом, а не связкой" \
  env LOCK="$TMP/REVISION-WINDOW.lock" RESTART_SCRIPT="$TMP/restart_supports.sh" \
      "$OWNER_GUARD" --stage cpt
#: Флаг, который контур лесенки не читает, — не пауза: у владельца остаётся
#: объявленный хозяин, но ребут и сторож возвращают прогон тем же путём.
expect_exit 2 "флаг паузы есть, но скрипт его не читает → стартовать нельзя (AD-9)" \
  env LOCK="$TMP/REVISION-WINDOW.lock" RESTART_SCRIPT="$TMP/restart_nosupport.sh" \
      "$OWNER_GUARD" --stage cpt
expect_contains "флаг без поддержки" "отказ назван причиной, а не молчанием" \
  env LOCK="$TMP/REVISION-WINDOW.lock" RESTART_SCRIPT="$TMP/restart_nosupport.sh" \
      "$OWNER_GUARD" --stage cpt
expect_exit 2 "поддержка флага недоказуема (текст скрипта не прочитан) → нельзя" \
  env LOCK="$TMP/REVISION-WINDOW.lock" RESTART_SCRIPT="$TMP/no-such-restart.sh" \
      "$OWNER_GUARD" --stage cpt
expect_exit 2 "сторож исполняем и флага паузы нет → стартовать нельзя" \
  env WATCHDOG="$TMP/watchdog_exec.sh" "$OWNER_GUARD" --stage cpt
expect_exit 2 "активная строка сторожа в кроне → стартовать нельзя" \
  env FAKE_CRONTAB_FILE="$TMP/cron_watchdog.txt" "$OWNER_GUARD" --stage cpt
expect_exit 0 "закомментированная строка (DISABLED) паузой не отменяется" \
  env FAKE_CRONTAB_FILE="$TMP/cron_paused.txt" "$OWNER_GUARD" --stage cpt
expect_exit 2 "crontab не прочитан → недоказуемо, значит нельзя (ADR-007 п.5)" \
  env FAKE_CRONTAB_MODE="broken" "$OWNER_GUARD" --stage cpt
expect_exit 0 "«нет crontab у пользователя» — это отсутствие строк, а не отказ чтения" \
  env FAKE_CRONTAB_MODE="empty" "$OWNER_GUARD" --stage cpt
expect_exit 2 "памяти 40 ГБ меньше порога SFT (50) → нельзя" \
  env FAKE_MEMINFO_FILE="$TMP/mem_40.txt" "$OWNER_GUARD" --stage sft
expect_exit 0 "те же 40 ГБ больше порога CPT (35) → можно" \
  env FAKE_MEMINFO_FILE="$TMP/mem_40.txt" "$OWNER_GUARD" --stage cpt
expect_exit 2 "две тренировочные нагрузки на стенде → нельзя" \
  env FAKE_PS_FILE="$TMP/ps_two_loads.txt" "$OWNER_GUARD" --stage cpt
expect_exit 2 "одна нагрузка, но чужая (--exp-name не совпал) → нельзя" \
  env FAKE_PS_FILE="$TMP/ps_one_load.txt" "$OWNER_GUARD" --stage cpt --exp-name det-probe-a-20260914-1450
expect_exit 0 "одна нагрузка и это наша стадия (резюм) → можно" \
  env FAKE_PS_FILE="$TMP/ps_one_load.txt" "$OWNER_GUARD" --stage cpt --exp-name run_a
# AD-9 (уточнение по факту пробы S3a): активный @reboot-автозапуск лесенки —
# блокирующий критерий, а не предупреждение: restart_v12.sh зовётся напрямую, и
# скрипт, не знающий флага, всё равно вернёт конкурирующий прогон.
expect_exit 2 "активная строка @reboot-автозапуска → стартовать нельзя (AD-9)" \
  env FAKE_CRONTAB_FILE="$TMP/cron_autostart.txt" "$OWNER_GUARD" --stage cpt
expect_contains "активный @reboot-автозапуск лесенки" "причина отказа — сама строка автозапуска" \
  env FAKE_CRONTAB_FILE="$TMP/cron_autostart.txt" "$OWNER_GUARD" --stage cpt
expect_contains "restart_v12.sh" "@reboot-строка названа дословно, а не «что-то активное»" \
  env FAKE_CRONTAB_FILE="$TMP/cron_autostart.txt" "$OWNER_GUARD" --stage cpt
expect_exit 2 "@reboot блокирует и при флаге, которого скрипт не читает: объявление, а не защита" \
  env FAKE_CRONTAB_FILE="$TMP/cron_autostart.txt" LOCK="$TMP/REVISION-WINDOW.lock" \
      RESTART_SCRIPT="$TMP/restart_nosupport.sh" "$OWNER_GUARD" --stage cpt
# AD-9 (S3c): тот же @reboot при флаге, **поддержанном** скриптом, старт не блокирует —
# ребут зовёт restart_v12.sh, а тот останавливается на флаге, не трогая контейнеры.
expect_exit 0 "@reboot обезврежен: флаг поддержан контуром лесенки (S3c)" \
  env FAKE_CRONTAB_FILE="$TMP/cron_autostart.txt" LOCK="$TMP/REVISION-WINDOW.lock" \
      RESTART_SCRIPT="$TMP/restart_supports.sh" "$OWNER_GUARD" --stage cpt
expect_contains "обезврежен" "допуск назван механизмом, а не «@reboot не заметили»" \
  env FAKE_CRONTAB_FILE="$TMP/cron_autostart.txt" LOCK="$TMP/REVISION-WINDOW.lock" \
      RESTART_SCRIPT="$TMP/restart_supports.sh" "$OWNER_GUARD" --stage cpt
expect_exit 0 "закомментированный @reboot (DISABLED) паузу не отменяет и не блокирует" \
  env FAKE_CRONTAB_FILE="$TMP/cron_paused_only.txt" "$OWNER_GUARD" --stage cpt
expect_exit 2 "стадия не названа → отказ: порог не вывести" \
  owner_guard
expect_exit 2 "неизвестная стадия → отказ" \
  owner_guard --stage eval
expect_exit 2 "стенд недоступен → нельзя стартовать (недоказуемое не равно чистому)" \
  bash tools/check_resource_owner.sh --stage cpt --host 203.0.113.1 --timeout 2
expect_contains "NOT-VERIFIED" "недоступность названа явно, а не «пропущено»" \
  bash tools/check_resource_owner.sh --stage cpt --host 203.0.113.1 --timeout 2
#: Профиль fitness-правила C-018: недоступность стенда — «не смогли проверить»,
#: а не дефект инварианта. Сенсоры при этом не «зелёные», а не измерены (null).
expect_exit 0 "--unreachable-not-verified: недоступность стенда → exit 0 + NOT-VERIFIED" \
  bash tools/check_resource_owner.sh --stage cpt --host 203.0.113.1 --timeout 2 \
       --unreachable-not-verified
expect_contains "NOT-VERIFIED (exit 0" "вердикт назван словом, а не только кодом возврата" \
  bash tools/check_resource_owner.sh --stage cpt --host 203.0.113.1 --timeout 2 \
       --unreachable-not-verified
expect_exit 0 "--unreachable-not-verified --json: сенсоры не измерены (null), а не «false»" \
  python3 - "$TMP" <<'PY'
import json, os, subprocess, sys
tmp = sys.argv[1]
env = dict(os.environ, PATH=f"{tmp}/fakebin:" + os.environ["PATH"])
p = subprocess.run(["bash", "tools/check_resource_owner.sh", "--stage", "rl",
                    "--host", "203.0.113.1", "--timeout", "2",
                    "--unreachable-not-verified", "--json"],
                   capture_output=True, text=True, env=env)
line = [l for l in p.stdout.splitlines() if l.startswith("{")][-1]
d = json.loads(line)
assert p.returncode == 0 and d["verdict"] == "NOT-VERIFIED" and d["exit"] == 0, d
assert d["owner_ok"] is None and d["memory_ok"] is None and d["loads_ok"] is None, d
assert d["threshold_gb"] == 49, d          # порог стадии назван даже без стенда
assert d["unreachable_not_verified"] is True, d
assert d["hosts_tried"] and d["hosts_tried"][0]["host"] == "203.0.113.1", d
sys.exit(0)
PY
expect_exit 0 "--json на живом стенде: несёт ветвь хозяина и блокирующий @reboot" \
  python3 - "$TMP" <<'PY'
import json, os, subprocess, sys
tmp = sys.argv[1]
base = dict(os.environ, PATH=f"{tmp}/fakebin:" + os.environ["PATH"],
            FAKE_PS_FILE=f"{tmp}/ps_one_load.txt", FAKE_MEMINFO_FILE=f"{tmp}/mem_78.txt")

def run(cron, lock=f"{tmp}/no-such.lock", restart=None):
    env = dict(base, FAKE_CRONTAB_FILE=cron)
    cmd = ["bash", "tools/check_resource_owner.sh", "--stage", "cpt", "--json",
           "--watchdog", f"{tmp}/watchdog_noexec.sh", "--lock", lock]
    if restart:
        cmd += ["--restart-script", restart]
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return p.returncode, json.loads([l for l in p.stdout.splitlines() if l.startswith("{")][-1])

rc, d = run(f"{tmp}/cron_paused.txt")
assert rc == 0 and d["verdict"] == "CAN-START", d
assert d["owner_branch"] == "watchdog-noexec", d
assert d["cron_autostart_lines"] == 0 and d["cron_autostart_blocking"] is False, d
rc, d = run(f"{tmp}/cron_autostart.txt", restart=f"{tmp}/restart_nosupport.sh")
assert rc == 2 and d["verdict"] == "CANNOT-START", d
assert d["owner_branch"] == "autostart-blocked", d
assert d["cron_autostart_lines"] == 1 and d["cron_autostart_blocking"] is True, d
assert "restart_v12.sh" in d["cron_autostart"], d
# S3c: тот же @reboot + флаг, поддержанный скриптом → ветвь допуска и снятый блок.
rc, d = run(f"{tmp}/cron_autostart.txt", lock=f"{tmp}/REVISION-WINDOW.lock",
            restart=f"{tmp}/restart_supports.sh")
assert rc == 0 and d["verdict"] == "CAN-START", d
assert d["owner_branch"] == "lock-supported", d
assert d["lock_present"] is True and d["flag_supported"] is True, d
assert d["flag_support_error"] == "" and d["restart_script"].endswith("restart_supports.sh"), d
assert d["cron_autostart_lines"] == 1, d
assert d["cron_autostart_blocking"] is False and d["cron_autostart_defused"] is True, d
# Флаг без поддержки: та же раскладка крона, но блок остаётся, и причина названа.
rc, d = run(f"{tmp}/cron_autostart.txt", lock=f"{tmp}/REVISION-WINDOW.lock",
            restart=f"{tmp}/restart_nosupport.sh")
assert rc == 2 and d["owner_branch"] == "autostart-blocked", d
assert d["lock_present"] is True and d["flag_supported"] is False, d
assert d["cron_autostart_blocking"] is True and d["cron_autostart_defused"] is False, d
assert "не читает REVISION-WINDOW.lock" in d["flag_support_error"], d
sys.exit(0)
PY
expect_exit 0 "страж хозяина только читает: флаг паузы не создаёт и крон не правит" \
  python3 - <<'PY'
import sys
import re
src = open("tools/check_resource_owner.sh", encoding="utf-8").read()
# Разрушающая операция в стражe — это инцидент, а не починка: флаг паузы и крон —
# решения владельца (AD-9, ADR-012 п.5), страж их только читает. Проверяются
# операции в командной позиции: слово «chmod» в тексте предупреждения — не запись.
for i, line in enumerate(src.splitlines(), 1):
    code = line.split("#", 1)[0]
    assert not re.match(r"\s*(touch|chmod|pkill|kill|tee|dd|truncate)\b", code), (i, line)
    for bad in ("crontab -r", "crontab -e", "crontab -l >", "docker stop",
                "docker rm", ">>"):
        assert bad not in code, (i, bad)
# rm допустим только для собственного временного файла (stderr крона)
for line in src.splitlines():
    if re.search(r"\brm\b", line):
        assert "CRON_ERR_FILE" in line, line
sys.exit(0)
PY
expect_exit 0 "--json: машинный вердикт разбирается как JSON" \
  python3 - "$TMP" <<'PY'
import json, os, subprocess, sys
tmp = sys.argv[1]
env = dict(os.environ, PATH=f"{tmp}/fakebin:" + os.environ["PATH"],
           FAKE_PS_FILE=f"{tmp}/ps_one_load.txt", FAKE_MEMINFO_FILE=f"{tmp}/mem_78.txt",
           FAKE_CRONTAB_FILE=f"{tmp}/cron_paused.txt")
p = subprocess.run(["bash", "tools/check_resource_owner.sh", "--stage", "cpt", "--json",
                    "--watchdog", f"{tmp}/watchdog_noexec.sh", "--lock", f"{tmp}/no-such.lock",
                    "--restart-script", f"{tmp}/restart_nosupport.sh"],
                   capture_output=True, text=True, env=env)
line = [l for l in p.stdout.splitlines() if l.startswith("{")][-1]
d = json.loads(line)
assert d["verdict"] == "CAN-START" and d["exit"] == 0 and d["owner_ok"] is True, d
assert d["threshold_gb"] == 35 and d["mem_available_gb"] == 78.0, d   # не «78,0»: LC_ALL=C
assert d["n_loads"] == 1 and d["watchdog_executable"] is False, d
assert d["cron_watchdog_lines"] == 0 and d["lock_present"] is False, d
sys.exit(0)
PY

# Сверка с AD-5: два стража считают нагрузки по одному правилу — не комментарием,
# а одним и тем же вердиктом на одной раскладке процессов.
expect_exit 0 "сверка с AD-5: оба стража считают нагрузки одинаково" \
  python3 - "$TMP" <<'PY'
import os, re, subprocess, sys
tmp = sys.argv[1]
base = dict(os.environ, PATH=f"{tmp}/fakebin:" + os.environ["PATH"],
            FAKE_SMI_FILE="/dev/null", FAKE_TMUX_FILE="/dev/null",
            FAKE_MEMINFO_FILE=f"{tmp}/mem_78.txt", FAKE_CRONTAB_FILE=f"{tmp}/cron_paused.txt")

def run(tool, ps_file):
    env = dict(base, FAKE_PS_FILE=ps_file)
    cmd = (["bash", "tools/check_gb10_serialization.sh"] if tool == "ad5" else
           ["bash", "tools/check_resource_owner.sh", "--stage", "cpt",
            "--watchdog", f"{tmp}/watchdog_noexec.sh", "--lock", f"{tmp}/no-such.lock",
            "--restart-script", f"{tmp}/restart_nosupport.sh"])
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    m = re.search(r"тренировочных нагрузок: (\d+)", p.stdout)
    return (int(m.group(1)) if m else None), p.returncode

for ps, want in ((f"{tmp}/ps_one_load.txt", 1), (f"{tmp}/ps_two_loads.txt", 2)):
    a5, owner = run("ad5", ps), run("owner", ps)
    assert a5[0] == want and owner[0] == want, (ps, want, a5, owner)
# одна нагрузка — оба зелёные; две — AD-5 краснеет нарушением (1), AD-9 запретом старта (2)
assert run("ad5", f"{tmp}/ps_one_load.txt")[1] == 0
assert run("owner", f"{tmp}/ps_one_load.txt")[1] == 0
assert run("ad5", f"{tmp}/ps_two_loads.txt")[1] == 1
assert run("owner", f"{tmp}/ps_two_loads.txt")[1] == 2
sys.exit(0)
PY

expect_exit 0 "план пробы печатается без обращения к стенду" \
  python3 tools/run_det_probe.py --plan --ts 20260101-0000
expect_contains "ADR-009" "в плане названо основание пробы" \
  python3 tools/run_det_probe.py --plan --ts 20260101-0000
expect_contains "детерминированный (use_deterministic_algorithms" "режим прогонов A/B назван" \
  python3 tools/run_det_probe.py --plan --ts 20260101-0000
expect_contains "safe_start.sh" "в плане назван обязательный путь запуска (AD-5)" \
  python3 tools/run_det_probe.py --plan --ts 20260101-0000
expect_contains "воспроизводится ли прогон по манифесту" "в плане назван обязательный вывод" \
  python3 tools/run_det_probe.py --plan --ts 20260101-0000
expect_contains "не восстанавливает сторож лесенки" "в плане названы границы (ADR-012)" \
  python3 tools/run_det_probe.py --plan --ts 20260101-0000
expect_exit 2 "--plan и --analyze-only несовместимы" \
  python3 tools/run_det_probe.py --plan --analyze-only
expect_exit 2 "шагов вне коридора пробы (10–50) → отказ" \
  python3 tools/run_det_probe.py --steps 100
expect_exit 2 "--analyze-only без каталога прогона → NOT-VERIFIED" \
  python3 tools/run_det_probe.py --analyze-only --ts 19990101-0000
expect_exit 1 "--stop-only на недоступном стенде → отказ, а не «закрыто»" \
  python3 tools/run_det_probe.py --stop-only --ts 20260101-0000 --host no-such-host.invalid

# Сценарий прогона — чистая функция: проверяется на синтетике, без стенда. Здесь
# ловится «режим объявлен в отчёте, но не включён в запуске» — ошибка, которая
# выглядела бы как успешная проба.
expect_exit 0 "сценарий прогона: режим виден в самом скрипте, а не только в отчёте" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import run_det_probe as D
args = D.parse_args(["--ts", "20260101-0000"])
s = {l: D.build_stage_script(ts=args.ts, letter=l, args=args) for l in "abc"}
for l in "ab":
    assert "-e CUBLAS_WORKSPACE_CONFIG=:4096:8" in s[l], l
    assert "--deterministic on" in s[l], l
assert "-e CUBLAS_WORKSPACE_CONFIG" not in s["c"], \
    "у прогона C режим контура: переменной cuBLAS быть не должно"
assert "--deterministic off" in s["c"], s["c"]
for l, txt in s.items():
    assert 'python3 "$CTR_RUN_DIR/smoke_probe.py"' in txt, l
    assert "nvrm-storm/safe_start.sh" in txt, l
    assert "--max_steps 20" in txt and "--seed 42" in txt and "--max_len 8192" in txt, l
    assert f"--exp_name det-probe-{l}-20260101-0000" in txt, l
    assert f"laguna-det-20260101-0000-{l}" in txt, l
    assert f"DET_PROBE_STAGE_EXIT={l}" in txt, l
    assert f"DET_PROBE_CKPT_SHA256={l}" in txt, l
    # sha256 чекпойнта считается на стенде: файл ~3 ГБ в кейс не копируется (AD-4)
    assert "sha256sum" in txt and "checkpoint_final.pt" in txt, l
# Конфиг прогонов A и B один, а C отличается ровно одной строкой env: сравнение
# «цена режима» было бы бессмысленным, если бы прогоны шли с разными настройками.
import re
def probe_args(txt):
    m = re.search(r"-- (--model_name[^\\]*)", txt)
    return m.group(1).split()
def norm(tokens, letter):
    return [t.replace(f"/det-probe-compact-20260101-0000/{letter}/", "/RUN/")
             .replace(f"det-probe-{letter}-20260101-0000", "EXP") for t in tokens]
def env_tokens(txt):
    return sorted(re.findall(r"-e ([A-Z_]+=\S+)", txt))
assert norm(probe_args(s["a"]), "a") == norm(probe_args(s["b"]), "b")
assert norm(probe_args(s["a"]), "a") == norm(probe_args(s["c"]), "c")
assert env_tokens(s["a"]) == env_tokens(s["b"]), "A и B идут с разным env"
assert set(env_tokens(s["a"])) - set(env_tokens(s["c"])) == {"CUBLAS_WORKSPACE_CONFIG=:4096:8"}
assert set(env_tokens(s["c"])) - set(env_tokens(s["a"])) == set()
sys.exit(0)
PY

# Разбор пробы на синтетических артефактах: расхождение, цена и обе ветви вывода.
expect_exit 0 "разбор пробы: расхождение, цена детерминизма и вывод (синтетика)" \
  python3 - "$TMP" <<'PY'
import json, pathlib, sys
sys.path.insert(0, "tools")
import run_det_probe as D

tmp = pathlib.Path(sys.argv[1]) / "det-probe-fix"
args = D.parse_args(["--ts", "20260101-0000", "--runs-dir", str(tmp.parent)])
base = [2.0989, 2.8013, 2.5, 2.4, 2.3, 2.2, 2.1, 2.0, 3.68, 2.1] + [2.0] * 10
nondet = [2.0989, 2.8020, 2.5, 2.4, 2.3, 2.2, 2.1, 2.0, 5.40, 2.1] + [2.0] * 10
series = {"a": base, "b": list(base), "c": nondet}
for letter, ls in series.items():
    sub = tmp / letter
    (sub / "logs").mkdir(parents=True)
    rows = [{"event": "start", "stage": "cpt", "determinism": {"requested": letter != "c"}}]
    for i, loss in enumerate(ls):
        rows.append({"event": "step", "i": i, "ts": 1.0 + i, "dt": 0.1,
                     "dt_step": None if i == 0 else 2.5, "tokens": 8192, "loss": loss,
                     "entropy": 3.0, "skipped_before": 0, "eval_seconds_before": 0.0})
    rows.append({"event": "end", "train_steps": len(ls), "skipped_forwards": 2,
                 "skipped_seconds": 50.0, "probe_seconds": 120.0, "error": None,
                 "torch_peak_allocated_bytes": 4 * 2**30,
                 "torch_peak_reserved_bytes": 5 * 2**30,
                 "determinism": {"requested": letter != "c"}})
    (sub / "probe_cpt.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                                         encoding="utf-8")
ok = {"pairs": {"a_vs_b": {"report": {"identical": True, "verdict": "IDENTICAL"}},
                "a_vs_c": {"report": {"identical": False, "verdict": "DIFFERS"}},
                "b_vs_c": {"report": {"identical": False, "verdict": "DIFFERS"}}}}
(tmp / "checkpoint_compare.json").write_text(json.dumps(ok), encoding="utf-8")

s = D.analyze(tmp, args)
ab, ac = s["divergence"]["a_vs_b"], s["divergence"]["a_vs_c"]
assert ab["identical"] is True and ab["first_divergent_step"] is None, ab
assert ac["identical"] is False and ac["first_divergent_step"] == 1, ac
assert ac["first_divergence"]["abs"] == 0.0007, ac["first_divergence"]
assert ac["max_abs_diff"] == 1.72, ac["max_abs_diff"]           # 5.40 против 3.68
assert ab["steps_compared"] == 20, ab
# цена: 2.5 с против S2 2.569 с → −2.69%; против прогона C того же дня → 0%
assert s["determinism_price"]["vs_s2"]["percent"] == -2.69, s["determinism_price"]["vs_s2"]
assert s["determinism_price"]["vs_run_c"]["percent"] == 0.0, s["determinism_price"]["vs_run_c"]
assert s["determinism_price"]["deterministic_step_seconds_mean"] == 2.5, s["determinism_price"]

v = D.verdict_of(s["runs"], s["divergence"], s["checkpoint_compare"],
                 s["determinism_price"]["vs_s2"], 20)
assert v["answer"] == "да", v
assert v["det_pair_identical"] is True and v["nondet_pair_differs"] is True, v
assert "только в детерминированном режиме" in v["consequence_for_a5_drift"], v
# вывод «да» не отменяет запрет ADR-009 п.2 на выводы по одиночным прогонам
assert "k сидов" in v["consequence_for_a5_drift"], v

# Негативная ветвь: та же проба, но чекпойнты A/B разошлись → вывод не «да».
bad = {"pairs": {k: {"report": {"identical": False}} for k in ("a_vs_b", "a_vs_c", "b_vs_c")}}
v2 = D.verdict_of(s["runs"], s["divergence"], bad, s["determinism_price"]["vs_s2"], 20)
assert v2["answer"] == "частично", v2
assert "распределений по k сидам" in v2["consequence_for_a5_drift"], v2

# Полностью расходящаяся пара A/B: вывод «нет» и переформулировка A5-drift.
bad_ab = D.pair_divergence("a", base, "b", nondet)
v3 = D.verdict_of(s["runs"], {"a_vs_b": bad_ab, "a_vs_c": ac, "b_vs_c": ac},
                  bad, s["determinism_price"]["vs_s2"], 20)
assert v3["answer"] == "нет", v3
assert "A5-drift" in v3["consequence_for_a5_drift"], v3
assert v3["basis"][1].startswith("A/C и B/C"), v3["basis"]

# Нет данных — вывод не выдумывается, а называется неопределённым.
v4 = D.verdict_of({}, {}, {}, {}, 20)
assert v4["answer"] == "не определён", v4
sys.exit(0)
PY

if python3 -c "import torch" 2>/dev/null; then
expect_exit 0 "smoke_probe: --deterministic on включает режим и пишет его в метрики (torch)" \
  python3 - <<'PY'
import json, pathlib, sys, tempfile
sys.path.insert(0, "tools")
import smoke_probe as P

d = P.enable_determinism()
assert d["use_deterministic_algorithms"] is True, d
assert d["cublas_workspace_config"] == ":4096:8", d
assert d["cudnn_deterministic"] is True and d["cudnn_benchmark"] is False, d
assert "сид" in d["seeding"], d          # посев остаётся делом пайплайна, не обвязки
p = pathlib.Path(tempfile.mkdtemp()) / "m.jsonl"
rec = P.Recorder(p, 1, determinism=d)
rec.start({"stage": "cpt"})     # режим подставляет сам Recorder, а не вызывающий
rec.step(1.0, 0.1, 8192, 2.5, None)
rec.finish(None)
rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
assert rows[0]["determinism"]["requested"] is True, rows[0]
assert rows[-1]["determinism"]["cublas_workspace_config"] == ":4096:8", rows[-1]
# по умолчанию режим выключен — прогоны контура не меняются
assert P.parse_args(["--pipeline", "x", "--metrics", "y"]).deterministic == "off"
sys.exit(0)
PY
expect_exit 2 "smoke_probe: --deterministic принимается и не ломает разбор аргументов" \
  python3 tools/smoke_probe.py --pipeline "$TMP/nope.py" --metrics "$TMP/m.jsonl" --deterministic on

expect_exit 0 "compare_checkpoints: идентичные тензоры при разных байтах файла (torch)" \
  python3 - "$TMP" <<'PY'
import copy, json, pathlib, subprocess, sys, tempfile
import torch
tmp = pathlib.Path(tempfile.mkdtemp())
torch.manual_seed(0)
ck = {"model": {"w": torch.randn(4, 4), "i": torch.arange(3, dtype=torch.int64)},
      "optimizer": {"state": {"0": {"step": torch.tensor(5.0), "exp_avg": torch.randn(2)}}}}
torch.save(ck, tmp / "a.pt")
same = copy.deepcopy(ck)
same["model"]["w"] = ck["model"]["w"].clone()
torch.save(same, tmp / "b.pt")
diff = copy.deepcopy(ck)
diff["model"]["w"][0, 0] += 1e-7
torch.save(diff, tmp / "c.pt")

def run(a, b):
    p = subprocess.run([sys.executable, "tools/compare_checkpoints.py", "--a", str(a),
                        "--b", str(b), "--json"], capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout.strip().splitlines()[-1])

rc, rep = run(tmp / "a.pt", tmp / "b.pt")
assert rc == 0 and rep["identical"] is True, rep
assert rep["tensors_compared"] == 4, rep
# Ключевой факт: sha256 файлов РАЗНЫЕ (zip-контейнер), а значения тензоров совпали —
# поэтому вердикт о «бит-в-бит» выносится по тензорам, а не по хешу файла.
assert rep["file_sha256_equal"] is False, rep
rc, rep = run(tmp / "a.pt", tmp / "c.pt")
assert rc == 1 and rep["identical"] is False, rep
assert rep["elements_differing"] == 1 and rep["max_abs_diff"] > 0, rep
assert rep["first_differing"][0]["key"] == "model.w", rep["first_differing"]
# 0-мерный тензор (`step` оптимизатора) сравнивается, а не роняет сравнение
assert rep["keys_only_in_a"] == [] and rep["keys_only_in_b"] == [], rep
rc, rep = run(tmp / "a.pt", tmp / "nope.pt")
assert rc == 2 and rep["verdict"] == "NOT-COMPARABLE", rep
sys.exit(0)
PY
else
  echo "  SKIP smoke_probe/compare_checkpoints: torch недоступен — проверки режима и сравнения не выполнены"
fi

echo
echo "== 14. pilot_chain.sh / run_pilot.py (S3b: пилот ревизии, ADR-013) =="
# Стенд подменяется фикстурами: фейковые docker/safe_start/страж/storm_gap, каталог
# прогона в mktemp. Проверяются не «успешные» пути, а правила: стадия не стартует
# без права стартовать; стоп-условия останавливают и ставят паузу; 0x51 проходит
# через storm_gap, а не через немедленный retry; наличие артефакта = стадия пройдена.
CHAIN_TMP="$TMP/chain"; mkdir -p "$CHAIN_TMP/fakebin"

cat > "$CHAIN_TMP/fakebin/sudo" <<'SH'
#!/bin/bash
exit 0
SH

#: Локаль C, которую ставит сама цепочка (`export LC_ALL=C`), на машинах, где в
#: site-packages лежит .pth с не-ASCII путём, ломает запуск python3 ещё в site.py.
#: К правилам цепочки это не относится (на стенде `LC_ALL=C python3` проверен и
#: работает), поэтому заглушка снимает локаль и зовёт настоящий python3: тест
#: проверяет разбор артефакта судьи и запись манифеста, а не локаль машины.
cat > "$CHAIN_TMP/fakebin/python3" <<'SH'
#!/bin/bash
self_dir="$(cd "$(dirname "$0")" && pwd)"
PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -vx -- "$self_dir" | paste -sd:)"
export PATH
exec env -u LC_ALL python3 "$@"
SH
chmod +x "$CHAIN_TMP/fakebin/python3"

#: Фейковый docker: ps/stop/rm — пустые, run — по сценарию FAKE_DOCKER_MODE.
#: Сценарий стадии задаётся файлом: test_chain <режим>.
cat > "$CHAIN_TMP/fakebin/docker" <<'SH'
#!/bin/bash
set -u
case "$1" in
  ps) exit 0 ;;
  stop|rm) echo "fake docker $1 $2"; mkdir -p "$FAKE_RUN_DIR/var"; touch "$FAKE_RUN_DIR/var/.docker-stopped"; exit 0 ;;
esac
#: FAKE_DOCKER_ARGS — файл, куда пишется аргумент-лист контейнера (`docker run …`)
#: одной строкой. Без него аргументы стадии не видны тесту, и перепутанные позиции
#: (`--exp_name` против `--eval_ckpt`) не ловятся ничем, кроме запуска на стенде.
if [ -n "${FAKE_DOCKER_ARGS:-}" ]; then printf '%s\n' "$*" > "$FAKE_DOCKER_ARGS"; fi
mkdir -p "$FAKE_RUN_DIR/checkpoints" "$FAKE_RUN_DIR/logs"
rm -f "$FAKE_RUN_DIR/var/.docker-stopped"

#: Спит короткими шагами и выходит, если пришёл `docker stop` — настоящий контейнер
#: умирает именно так, а тест не должен ждать минутами.
wait_or_stop() {
  local n="$1" i=0
  while [ "$i" -lt $((n * 2)) ]; do
    if [ -f "$FAKE_RUN_DIR/var/.docker-stopped" ]; then
      echo "(контейнер остановлен docker stop)"; exit 137
    fi
    sleep 0.5; i=$((i + 1))
  done
}

case "${FAKE_DOCKER_MODE:-ok}" in
  ok)
    echo "STAGE: fake | Qwen/Qwen2.5-0.5B"
    echo "CPT step 0/9776 | loss=2.8773"
    echo "ckpt" > "$FAKE_RUN_DIR/checkpoints/checkpoint_final.pt"
    #: FAKE_ARTIFACT — артефакт стадии, отличный от чекпойнта CPT (eval-стадии):
    #: судья обязан быть GLM-API, иначе цепочка справедливо объявит метрику подменённой.
    if [ -n "${FAKE_ARTIFACT:-}" ]; then
      mkdir -p "$FAKE_RUN_DIR/$(dirname "$FAKE_ARTIFACT")"
      printf '{"judge": "GLM-API", "judge_mean": 0.5, "total": 1}\n' > "$FAKE_RUN_DIR/$FAKE_ARTIFACT"
    fi
    exit 0 ;;
  entropy)
    echo "RL step 10/500 | reward=0.3 | pass=30% | turns=3 | len=100t | kl=0.01 | entropy=0.42 | clip=0.1 | adv=0.2 | adv≠0=50% | active=4/4 | mem=80.0GB"
    echo "RL step 20/500 | reward=0.3 | pass=30% | turns=3 | len=100t | kl=0.01 | entropy=0.38 | clip=0.1 | adv=0.2 | adv≠0=50% | active=4/4 | mem=80.0GB"
    sleep 60; exit 0 ;;
  entropy_high)
    echo "RL step 10/500 | reward=0.3 | pass=30% | turns=3 | len=100t | kl=0.01 | entropy=0.42 | clip=0.1 | adv=0.2 | adv≠0=50% | active=4/4 | mem=80.0GB"
    echo "RL step 20/500 | reward=0.3 | pass=30% | turns=3 | len=100t | kl=0.01 | entropy=1.80 | clip=0.1 | adv=0.2 | adv≠0=50% | active=4/4 | mem=80.0GB"
    echo "RL step 30/500 | reward=0.3 | pass=30% | turns=3 | len=100t | kl=0.01 | entropy=0.40 | clip=0.1 | adv=0.2 | adv≠0=50% | active=4/4 | mem=80.0GB"
    echo "ckpt" > "$FAKE_RUN_DIR/checkpoints/checkpoint_final.pt"
    exit 0 ;;
  storm)
    echo "NVRM: Xid (PCI:0000:01:00): 51, pid=123"
    if [ -f "$FAKE_STORM_DONE" ]; then
      echo "ckpt" > "$FAKE_RUN_DIR/checkpoints/checkpoint_final.pt"; exit 0
    fi
    wait_or_stop 2; exit 137 ;;
  nvrm2)
    echo "NVRM: Xid 79, pid=1"
    echo "NVRM: Xid 79, pid=2"
    wait_or_stop 60; exit 137 ;;
  silence)
    echo "STAGE: fake (дальше молчание — wedge)"
    wait_or_stop 300; exit 0 ;;
esac
exit 0
SH
chmod +x "$CHAIN_TMP/fakebin/docker" "$CHAIN_TMP/fakebin/sudo"

#: safe_start-заглушка разбирает свои флаги (`-d/-i/--`) — как настоящая обёртка.
cat > "$CHAIN_TMP/safe_start.sh" <<'SH'
#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in -d|-i) shift 2 ;; --) shift; break ;; *) break ;; esac; done
echo "safe_start: предстартовая гигиена (заглушка)"
exec "$@"
SH
chmod +x "$CHAIN_TMP/safe_start.sh"
printf '#!/bin/bash\nprintf "  | AD-9: МОЖНО СТАРТОВАТЬ (заглушка)\\n"\nexit 0\n' > "$CHAIN_TMP/guard_ok.sh"
printf '#!/bin/bash\nprintf "  | ВЕРДИКТ: СТАРТОВАТЬ НЕЛЬЗЯ (заглушка)\\n"\nexit 2\n' > "$CHAIN_TMP/guard_no.sh"
printf '#!/bin/bash\nexit 0\n' > "$CHAIN_TMP/manifest.py"
cat > "$CHAIN_TMP/storm_gap.sh" <<'SH'
#!/bin/bash
printf '%s storm_gap вызван\n' "$(date +%s)" >> "$FAKE_RUN_DIR/var/storm_gap.calls"
touch "$FAKE_STORM_DONE"
exit 0
SH
chmod +x "$CHAIN_TMP/guard_ok.sh" "$CHAIN_TMP/guard_no.sh" "$CHAIN_TMP/manifest.py" "$CHAIN_TMP/storm_gap.sh"

#: test_chain <каталог> <режим docker> <guard> [доп. флаги цепочки...]
test_chain() {
  local dir="$1" mode="$2" guard="$3"; shift 3
  PATH="$CHAIN_TMP/fakebin:$PATH" FAKE_RUN_DIR="$dir" FAKE_DOCKER_MODE="$mode" \
    FAKE_STORM_DONE="$CHAIN_TMP/storm.done" \
    bash tools/pilot_chain.sh \
      --run-dir "$dir" --ctr-run-dir /ctr --stages-file "$dir/stages.tsv" \
      --exp-base pilot-compact-s42-20260101-0000 \
      --ctr-prefix laguna-pilot-20260101-0000 \
      --guard "$guard" --safe-start "$CHAIN_TMP/safe_start.sh" \
      --manifest-tool "$CHAIN_TMP/manifest.py" --storm-gap "$CHAIN_TMP/storm_gap.sh" \
      --image test-image --poll-seconds 1 --mem-poll-seconds 1 --stall-minutes 0 \
      "$@" > "$dir/chain.out" 2>&1
}

mkstages() {  # <файл> — одна стадия cpt
  printf 'cpt\tcpt\tcpt\tcheckpoints/checkpoint_final.pt\t9776\t1\t-\tpilot-compact-s42-cpt-20260101-0000\n' > "$1"
}

#: test_chain_args <каталог> <режим> <guard> <файл аргументов> [доп. флаги...] —
#: тот же прогон, но с записью аргумент-листа контейнера. Отдельная обёртка, потому
#: что префиксное `env VAR=… test_chain` не работает: test_chain — функция, а не файл.
test_chain_args() {
  local dir="$1" mode="$2" guard="$3" argsf="$4"; shift 4
  FAKE_DOCKER_ARGS="$argsf" test_chain "$dir" "$mode" "$guard" "$@"
}

echo "  --- 14a. предусловие стадии ---"
CHAIN_DIR="$CHAIN_TMP/a"; mkdir -p "$CHAIN_DIR"; mkstages "$CHAIN_DIR/stages.tsv"
expect_exit 2 "отказ стража: стадия не стартует (exit 2)" \
  test_chain "$CHAIN_DIR" ok "$CHAIN_TMP/guard_no.sh"
expect_contains "ОТКАЗ СТРАЖА: стадия cpt не стартует" "причина отказа в логе (а не только код)" \
  cat "$CHAIN_DIR/chain.out"
if [ -f "$CHAIN_DIR/logs/cpt.log" ]; then
  FAIL=$((FAIL+1)); failures+=("стадия запустилась несмотря на отказ стража")
  echo "  FAIL стадия запустилась несмотря на отказ стража"
else
  PASS=$((PASS+1)); echo "  ok   стадия не запускалась: лога стадии нет"
fi

echo "  --- 14b. идемпотентность по артефактам ---"
CHAIN_DIR="$CHAIN_TMP/b"; mkdir -p "$CHAIN_DIR/checkpoints"; mkstages "$CHAIN_DIR/stages.tsv"
echo "готовый чекпойнт" > "$CHAIN_DIR/checkpoints/checkpoint_final.pt"
expect_exit 0 "артефакт на месте → стадия пропускается, цепочка завершена" \
  test_chain "$CHAIN_DIR" ok "$CHAIN_TMP/guard_ok.sh"
expect_contains "SKIP cpt: артефакт на месте" "пропуск назван в логе" cat "$CHAIN_DIR/chain.out"
expect_contains "ЦЕПОЧКА ЗАВЕРШЕНА" "полнота подтверждена по артефактам" cat "$CHAIN_DIR/chain.out"

echo "  --- 14c. стоп-условия ---"
CHAIN_DIR="$CHAIN_TMP/c"; mkdir -p "$CHAIN_DIR"; mkstages "$CHAIN_DIR/stages.tsv"
expect_exit 3 "энтропия < 0.5 два наблюдения подряд → стоп и пауза" \
  test_chain "$CHAIN_DIR" entropy "$CHAIN_TMP/guard_ok.sh"
expect_contains "энтропия политики < 0.5" "причина паузы — энтропия (маркер var/STOPPED)" \
  cat "$CHAIN_DIR/var/STOPPED"
expect_contains "СТОП ПО СТРАЖУ" "стоп записан в лог цепочки" cat "$CHAIN_DIR/chain.out"
expect_contains "остановка контейнера стадии" "контейнер стадии остановлен, а не оставлен" \
  cat "$CHAIN_DIR/chain.out"

CHAIN_DIR="$CHAIN_TMP/c2"; mkdir -p "$CHAIN_DIR"; mkstages "$CHAIN_DIR/stages.tsv"
expect_exit 0 "энтропия 0.42 → 1.80 → 0.40: серия разорвана, стопа нет" \
  test_chain "$CHAIN_DIR" entropy_high "$CHAIN_TMP/guard_ok.sh"
test ! -f "$CHAIN_DIR/var/STOPPED" \
  && { PASS=$((PASS+1)); echo "  ok   пауза не поставлена: порог не набран подряд"; } \
  || { FAIL=$((FAIL+1)); failures+=("пауза поставлена на разорванной серии"); echo "  FAIL пауза поставлена зря"; }

CHAIN_DIR="$CHAIN_TMP/e"; mkdir -p "$CHAIN_DIR"; mkstages "$CHAIN_DIR/stages.tsv"
expect_exit 3 "два NVRM/Xid-инцидента → стоп и пауза" \
  test_chain "$CHAIN_DIR" nvrm2 "$CHAIN_TMP/guard_ok.sh"
expect_contains "NVRM/Xid-инцидентов" "причина паузы — NVRM" cat "$CHAIN_DIR/var/STOPPED"

CHAIN_DIR="$CHAIN_TMP/f"; mkdir -p "$CHAIN_DIR"; mkstages "$CHAIN_DIR/stages.tsv"
expect_exit 3 "свободной памяти меньше предохранителя → стоп" \
  test_chain "$CHAIN_DIR" silence "$CHAIN_TMP/guard_ok.sh" --mem-floor-gb 999999
expect_contains "предохранителя" "причина — предохранитель памяти (llm-platform-*)" \
  cat "$CHAIN_DIR/var/STOPPED"

CHAIN_DIR="$CHAIN_TMP/g"; mkdir -p "$CHAIN_DIR"; mkstages "$CHAIN_DIR/stages.tsv"
expect_exit 3 "стадия молчит дольше порога → стоп как wedge" \
  test_chain "$CHAIN_DIR" silence "$CHAIN_TMP/guard_ok.sh" --stall-seconds 2 --mem-floor-gb 0
expect_contains "молчит" "причина — молчание стадии" cat "$CHAIN_DIR/var/STOPPED"

echo "  --- 14d. NVRM 0x51: storm_gap, а не немедленный retry ---"
CHAIN_DIR="$CHAIN_TMP/d"; rm -f "$CHAIN_TMP/storm.done"; mkdir -p "$CHAIN_DIR"
mkstages "$CHAIN_DIR/stages.tsv"
expect_exit 0 "0x51 → окно затишья (storm_gap) → повтор стадии → артефакт" \
  test_chain "$CHAIN_DIR" storm "$CHAIN_TMP/guard_ok.sh"
test -f "$CHAIN_DIR/var/storm_gap.calls" \
  && { PASS=$((PASS+1)); echo "  ok   storm_gap вызван до повтора"; } \
  || { FAIL=$((FAIL+1)); failures+=("storm_gap не вызван при 0x51"); echo "  FAIL storm_gap не вызван"; }
expect_contains "NVRM 0x51 в стадии cpt — жду окно затишья" "порядок назван: сначала окно, потом повтор" \
  cat "$CHAIN_DIR/chain.out"
expect_contains "повтор стадии cpt" "повтор состоялся" cat "$CHAIN_DIR/chain.out"

echo "  --- 14e. пауза снимается владельцем, а не цепочкой ---"
CHAIN_DIR="$CHAIN_TMP/i"; mkdir -p "$CHAIN_DIR/var"; mkstages "$CHAIN_DIR/stages.tsv"
printf 'два NVRM/Xid-инцидента — стоп и пауза\n' > "$CHAIN_DIR/var/STOPPED"
expect_exit 3 "маркер паузы блокирует цепочку" \
  test_chain "$CHAIN_DIR" ok "$CHAIN_TMP/guard_ok.sh"
expect_contains "маркер" "причина берётся из маркера, а не выдумывается" \
  cat "$CHAIN_DIR/chain.out"
expect_exit 0 "цепочка маркер не снимает: после прогона он на месте" \
  test -f "$CHAIN_DIR/var/STOPPED"

echo "  --- 14f. две копии цепочки в одном каталоге ---"
CHAIN_DIR="$CHAIN_TMP/j"; mkdir -p "$CHAIN_DIR/var"; mkstages "$CHAIN_DIR/stages.tsv"
printf '%s\n' "$$" > "$CHAIN_DIR/var/chain.pid"
expect_exit 2 "живая копия цепочки → вторая не запускается (AD-5)" \
  test_chain "$CHAIN_DIR" ok "$CHAIN_TMP/guard_ok.sh"
rm -f "$CHAIN_DIR/var/chain.pid"

echo "  --- 14g. --check-only: предусловие по каждой стадии без обучения ---"
CHAIN_DIR="$CHAIN_TMP/h"; mkdir -p "$CHAIN_DIR"
printf 'cpt\tcpt\tcpt\tcheckpoints/checkpoint_final.pt\t9776\t1\t-\tp-cpt\nsft\tsft\tsft\tcheckpoints/sft_checkpoint_final.pt\t67423\t2\t-\tp-sft\n' \
  > "$CHAIN_DIR/stages.tsv"
expect_exit 0 "--check-only зелёный: обе стадии пропущены стражем" \
  test_chain "$CHAIN_DIR" ok "$CHAIN_TMP/guard_ok.sh" --check-only
expect_contains "стадия не запускается: --check-only" "стадии не запускаются в режиме проверки" \
  cat "$CHAIN_DIR/chain.out"
if [ -f "$CHAIN_DIR/logs/cpt.log" ]; then
  FAIL=$((FAIL+1)); failures+=("--check-only запустил стадию")
  echo "  FAIL --check-only запустил стадию"
else
  PASS=$((PASS+1)); echo "  ok   лог стадии не создан: обучения не было"
fi
expect_exit 2 "--check-only красный на отказе стража" \
  test_chain "$CHAIN_DIR" ok "$CHAIN_TMP/guard_no.sh" --check-only

echo "  --- 14h. сценарий стадии: страж, порог и обёртка запуска ---"
expect_exit 0 "цепочка зовёт стража AD-9 перед стадией и идёт через safe_start" \
  python3 - <<'PY'
import pathlib, sys
src = pathlib.Path("tools/pilot_chain.sh").read_text(encoding="utf-8")
# предусловие — страж кейса с --stage и --exp-name (без профиля NOT-VERIFIED:
# стадия, стартующая на невидимом стенде, зелёного вердикта не получает)
assert 'bash "$GUARD" --stage "$1" --exp-name "$2" --json' in src
# локальное определение стенда: ssh к самому себе зависел бы от ключей и sshd
assert '--host "$HOST_LOCAL"' not in src
# профиль NOT-VERIFIED упомянут в шапке как «здесь не используется» — флага в
# разборе аргументов и в вызове стража быть не должно
code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
assert "unreachable" not in code, [l for l in code.splitlines() if "unreachable" in l]
assert 'bash "$SAFE_START" -d 60 -i 5 -- "$DOCKER" run' in src
assert "laguna_pipeline_v8.py" in src and "--ckpt_dir" in src
# пороги памяти в цепочке не дублируются: их источник — страж
for t in ("35", "50", "49"):
    assert f"MIN_FREE_GB={t}" not in src and f"THRESHOLD={t}" not in src, t
# eval без GLM-ключа не стартует: иначе judge=slug-verifier подменяет ось
assert "judge != GLM-API" in src and "GLM_API_KEY" in src
sys.exit(0)
PY

echo "  --- 14j. аргументы стадии: exp_name и eval_ckpt берутся из своих полей TSV ---"
# Факт S3c: позиции были перепутаны, и контейнер падал в argparse за 6 с
# («--eval_ckpt: invalid choice: 'pilot-…-cpt-…'») — стадия не стартовала вовсе.
# Тест ловит это по аргумент-листу, а не по факту запуска на стенде.
CHAIN_DIR="$CHAIN_TMP/k"; mkdir -p "$CHAIN_DIR"; mkstages "$CHAIN_DIR/stages.tsv"
expect_exit 0 "стадия cpt стартует: аргументы собраны из её TSV-ряда" \
  test_chain_args "$CHAIN_DIR" ok "$CHAIN_TMP/guard_ok.sh" "$CHAIN_TMP/k/args.txt"
expect_exit 0 "exp_name — из поля exp (8-е), а не из eval_ckpt" \
  grep -q -- "--exp_name pilot-compact-s42-cpt-20260101-0000" "$CHAIN_TMP/k/args.txt"
expect_exit 1 "eval_ckpt=- в контейнер не передаётся (подставлять нечего)" \
  grep -q -- "--eval_ckpt" "$CHAIN_TMP/k/args.txt"

CHAIN_DIR="$CHAIN_TMP/l"; mkdir -p "$CHAIN_DIR"
printf 'eval_base\teval\tsft\tlogs/eval_results_base.json\t1\t1\tbase\tpilot-compact-s42-eval_base-20260101-0000\n' \
  > "$CHAIN_DIR/stages.tsv"
printf 'GLM_API_KEY=fixture-key\n' > "$CHAIN_TMP/glm_env"
#: Артефакт eval-стадии подкладывается экспортом: `env VAR=… test_chain_args` не
#: работает (функцию нельзя запустить как файл), а без артефакта цепочка справедливо
#: объявит стадию упавшей. Снимается сразу после блока, чтобы не течь в другие тесты.
export FAKE_ARTIFACT="logs/eval_results_base.json"
expect_exit 0 "стадия eval стартует (ключ судьи из фикстуры)" \
  test_chain_args "$CHAIN_DIR" ok "$CHAIN_TMP/guard_ok.sh" "$CHAIN_TMP/l/args.txt" \
      --glm-env "$CHAIN_TMP/glm_env"
expect_exit 0 "exp_name у eval — имя прогона, а не имя чекпойнта" \
  grep -q -- "--exp_name pilot-compact-s42-eval_base-20260101-0000" "$CHAIN_TMP/l/args.txt"
expect_exit 0 "eval_ckpt у eval — base из поля eval_ckpt, а не имя прогона" \
  grep -q -- "--eval_ckpt base" "$CHAIN_TMP/l/args.txt"
unset FAKE_ARTIFACT

echo "  --- 14i. раннер: план, предусловия, разбор сценария ---"
expect_exit 0 "план пилота печатается без обращения к стенду" \
  python3 tools/run_pilot.py --plan --ts 20260101-0000
expect_contains "ADR-013" "в плане названо основание (фаза 1)" \
  python3 tools/run_pilot.py --plan --ts 20260101-0000
expect_contains "обычный режим" "режим назван: детерминизм только для проб (ADR-014)" \
  python3 tools/run_pilot.py --plan --ts 20260101-0000
expect_contains "энтропия политики < 0.5" "стоп-условия названы в плане" \
  python3 tools/run_pilot.py --plan --ts 20260101-0000
expect_contains "не снимает @reboot" "границы названы (R4+: крон и @reboot не трогаются)" \
  python3 tools/run_pilot.py --plan --ts 20260101-0000
expect_exit 2 "--plan и --analyze-only несовместимы" \
  python3 tools/run_pilot.py --plan --analyze-only
expect_exit 2 "--analyze-only без каталога прогона → NOT-VERIFIED" \
  python3 tools/run_pilot.py --analyze-only --ts 19990101-0000
expect_exit 1 "--stop-only на недоступном стенде → отказ, а не «закрыто»" \
  python3 tools/run_pilot.py --stop-only --ts 20260101-0000 --host no-such-host.invalid

expect_exit 0 "состав цепочки, шаги стадий и TSV считаются кодом, а не вписаны руками" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import run_pilot as P
args = P.parse_args(["--ts", "20260101-0000"])
stages = P.build_stages(args, P.EXPECT_SFT_SAMPLES)
by = {s.name: s for s in stages}
assert by["cpt"].steps == 9776 and by["cpt"].batch == 1, by["cpt"]
# 3 эпохи × 44 949 / 2 = 67 423 шага — та же арифметика, что в рабочем раннере лесенки
assert by["sft"].steps == 3 * 44949 // 2 == 67423, by["sft"]
assert by["rl"].steps == 500, by["rl"]
assert by["eval_sft"].eval_ckpt == "sft" and by["eval_rl"].eval_ckpt == "rl", stages
# порог берётся у стража: у eval-стадий он sft (eval не тренирует — меньший порог был бы выдумкой)
assert by["eval_base"].guard_stage == "sft", by["eval_base"]
assert [s.name for s in stages] == ["cpt", "sft", "rl", "eval_base", "eval_sft", "eval_rl"]
tsv = P.stages_tsv(stages, args.ts)
assert tsv.count("\n") == len(stages)
assert "checkpoint_final.pt" in tsv and "sft_checkpoint_final.pt" in tsv
assert "pilot-compact-s42-eval_rl-20260101-0000" in tsv, tsv
# команда запуска одна: её же печатает план, её же исполняет tmux
cmd = P.chain_command(args, "/home/user/experiments/pilot-x", stages)
assert "pilot_chain.sh" in cmd and "--stages-file" in cmd, cmd
assert "--safe-start /home/user/gb10-shared/nvrm-storm/safe_start.sh" in cmd, cmd
assert "--guard /home/user/experiments/pilot-x/check_resource_owner.sh" in cmd, cmd
# календарь — арифметика от измеренных времён шага (S2/ADR-011), а не замер
eta = P.eta_hours(stages)
assert 3.3 < eta["total_days_range"][0] < eta["total_days_range"][1] < 3.8, eta
assert eta["cpt_hours"] == 7.0 and eta["sft_hours"] == 60.9, eta
# предупреждение «указан --unreachable-not-verified» не должно появляться в команде
assert "unreachable" not in cmd, cmd
sys.exit(0)
PY

expect_exit 0 "отчёт о запуске не выдаёт несделанное за сделанное (манифест и evidence)" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import run_pilot as P
args = P.parse_args(["--ts", "20260101-0000"])
stages = P.build_stages(args, P.EXPECT_SFT_SAMPLES)
run_dir = P.CASE_ROOT / args.runs_dir / P.run_id_for(args.ts)
# `done` в манифесте AD-2 = артефакт стадии на месте (stage_done_strict в цепочке):
# запуск такого права не даёт, а файл коммитится в кейс и читается гейтом C-012.
assert set(P.launch_statuses(stages).values()) == {"pending"}, P.launch_statuses(stages)
launched = {"ok": True, "detail": "сессия жива", "run_dir": "/x"}
for launch, want_log, want_nv, forbid in (
        (launched, "logs/chain.log", "подтверждён tmux-сессией", "запуск не состоялся"),
        (None, "запуск не состоялся", "запуск в этом прогоне не состоялся",
         "подтверждён tmux-сессией")):
    ev = P.build_evidence(args, stages, {}, {}, {}, launch, {},
                          "launched" if launch else "blocked", "причина", run_dir)
    assert any(want_log in a for a in ev["next_actions"]), ev["next_actions"]
    assert not any(forbid in a for a in ev["next_actions"]), ev["next_actions"]
    nv = ev["not_verified"][-1]
    assert want_nv in nv, nv
    assert forbid not in nv, nv
sys.exit(0)
PY

expect_exit 0 "предусловие раннера зовёт страж строго (без профиля NOT-VERIFIED)" \
  python3 - <<'PY'
import io, sys, contextlib
sys.path.insert(0, "tools")
import run_pilot as P
seen = {}
def fake_run(cmd, **kw):
    seen["cmd"] = cmd
    class R: returncode, stdout, stderr = 2, "ВЕРДИКТ: СТАРТОВАТЬ НЕЛЬЗЯ\n  · причина X\n", ""
    return R()
real = P.subprocess.run
P.subprocess.run = fake_run
try:
    rep = P.owner_guard("gb10-fast", "sft", "pilot-x-sft-1")
finally:
    P.subprocess.run = real
assert "--unreachable-not-verified" not in seen["cmd"], seen["cmd"]
assert "--stage" in seen["cmd"] and "sft" in seen["cmd"] and "--exp-name" in seen["cmd"]
assert rep["ok"] is False and "причина X" in rep["detail"], rep
# и только явный запрос даёт профиль правила C-018
P.subprocess.run = fake_run
try:
    P.owner_guard("gb10-fast", "sft", "pilot-x-sft-1", lax_unreachable=True)
finally:
    P.subprocess.run = real
assert "--unreachable-not-verified" in seen["cmd"], seen["cmd"]
sys.exit(0)
PY

echo "  --- 15. конвертер сред E1–E8: пул ревизии v2 (S3f / ADR-021) ---"
# Фикстуры: свой каталог сред, пул env_types, SFT, eval, действующий пул, индекс
# концептов. Все выходы — в $TMP: тест не пишет ни в кейс, ни в gb10-shared (AD-4).
python3 - "$TMP" <<'PY'
import json, pathlib, sys
T = pathlib.Path(sys.argv[1]); REV = T / "rev"; (REV / "tasks").mkdir(parents=True)

def card(slug, title="Alpha X", broken_title=False):
    t = "A1: Tool Execution" if broken_title else title
    return (f"---\nslug: {slug}\ntype: algorithmic_primitive\nlevel: α\n"
            f"formality: A\ntitle: {t}\n---\n\n## Определение\nТекст определения.\n")

def write(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

(REV / "envs.yaml").write_text("""# RL Environments Registry (фикстура теста)
# комментарий, который обязан выжить при --update-registry
environments:
  E1_define:
    status: ready
    judge_weight: 0.5
    verifiable_weight: 0.5
    task_count: 414

  E2_formula:
    status: ready
    judge_weight: 0.1
    verifiable_weight: 0.9
    task_count: 75

  E3_classify:
    status: ready
    judge_weight: 0.0
    verifiable_weight: 1.0
    task_count: 412

  E4_extract:
    status: ready
    judge_weight: 0.4
    verifiable_weight: 0.6
    task_count: 45

  E5_relate:
    status: ready
    judge_weight: 0.2
    verifiable_weight: 0.8
    task_count: 216

  E6_contrast:
    status: ready
    judge_weight: 0.7
    verifiable_weight: 0.3
    task_count: 100

  E7_repair:
    status: ready
    judge_weight: 0.0
    verifiable_weight: 1.0
    task_count: 100

  E8_plan_experiment:
    status: planned
    judge_weight: 0.7
    verifiable_weight: 0.3
    task_count: 0
""", encoding="utf-8")

write(REV / "tasks" / "formula.jsonl", [
    # нормальная задача — в пул
    {"task_id": "E2_alpha_x", "meta": {"slug": "alpha_x"},
     "prompt": "Запиши формулу Alpha X в LaTeX и расшифруй каждое обозначение.",
     "gold_card": card("alpha_x")},
    # лейк: slug присутствует в промпте буквально
    {"task_id": "E2_beta_y", "meta": {"slug": "beta_y"},
     "prompt": "Запиши формулу beta_y в LaTeX.", "gold_card": card("beta_y")},
    # YAML не разбирается (двоеточие в title) — slug берётся регуляркой
    {"task_id": "E2_gamma_z", "meta": {"slug": "gamma_z"},
     "prompt": "Запиши формулу Gamma Z в LaTeX.", "gold_card": card("gamma_z", broken_title=True)},
    # нет slug — проекция невозможна
    {"task_id": "E2_noslug", "meta": {"slug": "noslug"},
     "prompt": "Запиши формулу No Slug в LaTeX.", "gold_card": "---\ntype: x\ntitle: No Slug\n---\n"},
    # два концепта с одинаковым title → один промпт, разные ответы → обе задачи снимаются
    {"task_id": "E2_dup_one", "meta": {"slug": "dup_one"},
     "prompt": "Запиши формулу Same Name в LaTeX.", "gold_card": card("dup_one", "Same Name")},
    {"task_id": "E2_dup_two", "meta": {"slug": "dup_two"},
     "prompt": "Запиши формулу Same Name в LaTeX.", "gold_card": card("dup_two", "Same Name")},
    # строка короче 4 символов не различает ответ
    {"task_id": "E2_short", "meta": {"slug": "ab"},
     "prompt": "Запиши формулу Short в LaTeX.", "gold_card": card("ab")},
    # концепта нет в индексе — slug недостижим
    {"task_id": "E2_unreachable", "meta": {"slug": "delta_w"},
     "prompt": "Запиши формулу Delta W в LaTeX.", "gold_card": card("delta_w")},
    # промпт дословно есть в SFT (с точностью до пробелов)
    {"task_id": "E2_sft_seen", "meta": {"slug": "sft_seen"},
     "prompt": "Запиши  формулу SFT Seen в LaTeX.", "gold_card": card("sft_seen")},
    # промпт дословно есть в eval
    {"task_id": "E2_eval_seen", "meta": {"slug": "eval_seen"},
     "prompt": "Запиши формулу Eval Seen в LaTeX.", "gold_card": card("eval_seen")},
    # промпт дословно есть в действующем пуле v1
    {"task_id": "E2_v1_seen", "meta": {"slug": "v1_seen"},
     "prompt": "Запиши формулу V1 Seen в LaTeX.", "gold_card": card("v1_seen")},
])
write(REV / "tasks" / "classify.jsonl", [
    {"task_id": "E3_eps_x", "meta": {"slug": "eps_x"},
     "prompt": "Дано определение концепции:\n\nEps X — это алгоритм.\n\nОпредели: тип, уровень, формальность.",
     "gold_card": card("eps_x")},
])
write(REV / "tasks" / "relate.jsonl", [
    {"task_id": "E5_alpha_x_to_eps_x", "meta": {"slug_from": "alpha_x", "slug_to": "eps_x"},
     "prompt": "Как связаны Alpha X и Eps X?",
     "gold_relation": {"from": "alpha_x", "to": "eps_x", "expected": "connected"}},
    # обратный порядок той же пары — та же задача (дубль по проверяемому набору)
    {"task_id": "E5_eps_x_to_alpha_x", "meta": {"slug_from": "eps_x", "slug_to": "alpha_x"},
     "prompt": "Как связаны Eps X и Alpha X?",
     "gold_relation": {"from": "eps_x", "to": "alpha_x", "expected": "connected"}},
    # определения обоих концептов пусты → keyword-ветка вернёт False всегда
    {"task_id": "E5_empty_pair", "meta": {"slug_from": "empty_one", "slug_to": "empty_two"},
     "prompt": "Как связаны Empty One и Empty Two?",
     "gold_relation": {"from": "empty_one", "to": "empty_two", "expected": "connected"}},
])
write(REV / "tasks" / "define.jsonl", [
    {"task_id": "E1_define_slug", "meta": {"slug": "define_slug"},
     "prompt": "Дай определение Define Slug.", "gold_card": card("define_slug")},
])
write(REV / "tasks" / "extract.jsonl", [
    {"task_id": "E4_extract_one", "meta": {"slug": "extract_slug"},
     "prompt": "Извлеки концепции.", "gold_concepts": ["extract_slug"]},
])
write(REV / "tasks" / "contrast.jsonl", [
    {"task_id": "E6_contrast_pair", "meta": {"slug_a": "contrast_slug"},
     "prompt": "Чем Contrast Slug отличается от Alpha X?", "gold_diff": "…"},
])
write(REV / "tasks" / "repair.jsonl", [
    {"task_id": "E7_repair_one", "meta": {"slug": "repair_slug"},
     "prompt": "Найди и исправь ошибки:\n\n---\nslug: repair_slug\ntype: wrong\n---\n",
     "corrupted_card": "---\nslug: repair_slug\ntype: wrong\n---\n",
     "corruptions_applied": ["swapped_type"], "gold_card": card("repair_slug")},
])
(REV / "tasks" / "plan_experiment.jsonl").write_text("", encoding="utf-8")

write(T / "rev-env-types.jsonl", [
    # Строка 1: цепочка с якорями ролей и подсказкой промежуточного концепта.
    # S3f-fix-2: проверяемый набор — термины определений концептов-условий
    # (alpha_x, eps_x), а не их имена; промежуточный ('dup_two') не проверяется.
    {"task_type": "chain_reasoning",
     "prompt": "Построй цепочку рассуждений.\nНачало: 'alpha_x'\nКонец: 'eps_x'\n"
               "Шаг 2: найди промежуточный концепт (например, 'dup_two' или другой).",
     "expected_slugs": ["alpha_x", "eps_x"], "verifier": "multi_slug_match",
     "max_steps": 15, "n_min_steps": 3, "reward": 1.0},
    # ood_pair идёт в slug-ветку верификатора, а expected_slugs у него нет →
    # перемаршрутизация в explain_relation (иначе задача мёртвая).
    # Пара концептов — своя, не совпадающая с парой E5: иначе её снимет дедуп.
    {"task_type": "ood_pair", "prompt": "Объясни связь 'gamma_z' и 'dup_one'.",
     "expected_relation": "related", "verifier": "keyword_match",
     "keywords": ["gamma_z", "dup_one"], "reward": 1.0},
    {"task_type": "ood_pair", "prompt": "Объясни связь 'empty_one' и 'empty_two'.",
     "expected_relation": "related", "verifier": "keyword_match",
     "keywords": ["empty_one", "empty_two"], "reward": 1.0},
    {"task_type": "make_tea", "prompt": "Завари чай.", "expected_slugs": ["alpha_x"]},
    # Строки 5–8 — проекция и лейк-правило цепочек (S3f-fix-2).
    # Строка 5: промпт цитирует определение концепта-условия → проверяемые термины
    # в промпте → снять (правило не ослаблено и на цепочках).
    {"task_type": "chain_reasoning",
     "prompt": "Построй цепочку рассуждений.\nНачало: 'alpha_x'\nКонец: 'eps_x'\n"
               "Шаг 1: найди определение 'alpha_x': «Алгоритм оптимизации политики, "
               "который генерирует группы выходов».",
     "expected_slugs": ["alpha_x", "eps_x"], "verifier": "multi_slug_match",
     "max_steps": 15, "n_min_steps": 3, "reward": 1.0},
    # Строка 6: якорей ролей нет → неизвестно, чьи определения проверять →
    # проекция невозможна (исключение с причиной, а не догадка).
    {"task_type": "chain_reasoning", "prompt": "Цепочка от 'beta_y' к 'gamma_z'.",
     "expected_slugs": ["beta_y", "gamma_z"], "verifier": "multi_slug_match",
     "max_steps": 15, "n_min_steps": 3, "reward": 1.0},
    # Строка 7: роли есть, но определений концептов-условий в индексе нет →
    # проверяемый набор пуст (модель не может его добыть).
    {"task_type": "chain_reasoning",
     "prompt": "Построй цепочку.\nНачало: 'empty_one'\nКонец: 'empty_two'.",
     "expected_slugs": ["empty_one", "empty_two"], "verifier": "multi_slug_match",
     "max_steps": 15, "n_min_steps": 3, "reward": 1.0},
    # Строка 8: keyword-ветка — промпт сам набирает порог терминов определений → снять.
    {"task_type": "explain_relation",
     "prompt": "Объясни связь: как «алгоритм оптимизации политики» соотносится с "
               "«группы выходов»?",
     "expected_relation": "related", "verifier": "keyword_match",
     "keywords": ["define_slug", "extract_slug"], "reward": 1.0},
])
write(T / "rev-sft.jsonl", [
    {"messages": [{"role": "user", "content": "Запиши формулу SFT Seen в LaTeX."},
                  {"role": "assistant", "content": "<think>…</think>"}]},
])
write(T / "rev-eval.jsonl", [
    {"task_type": "ood_pair", "prompt": "Запиши формулу Eval Seen в LaTeX.",
     "expected_slugs": ["eval_seen"], "verifier": "multi_slug_match"},
])
write(T / "rev-v1.jsonl", [
    {"task_type": "explain_relation", "prompt": "Запиши формулу V1 Seen в LaTeX.",
     "expected_relation": "related", "keywords": ["v1_seen", "alpha_x"], "verifier": "keyword_match"},
])
DEF = "Алгоритм оптимизации политики, который генерирует группы выходов для каждого входа"
write(T / "rev-index.jsonl", [
    {"slug": s, "title": s, "type": "algorithmic_primitive", "definition": DEF}
    for s in ("alpha_x", "beta_y", "gamma_z", "dup_one", "dup_two", "sft_seen", "eval_seen",
              "v1_seen", "eps_x", "short", "define_slug", "extract_slug", "contrast_slug",
              "repair_slug", "noslug", "make_tea")
] + [{"slug": s, "title": s, "type": "x", "definition": ""}
     for s in ("empty_one", "empty_two")])
print("фикстуры конвертера готовы")
PY

REV_ARGS=(--envs-root "$TMP/rev" --env-types "$TMP/rev-env-types.jsonl"
          --sft "$TMP/rev-sft.jsonl" --eval "$TMP/rev-eval.jsonl" --v1 "$TMP/rev-v1.jsonl"
          --index "$TMP/rev-index.jsonl" --out "$TMP/rev-pool-v2.jsonl"
          --card "$TMP/rev-card.json" --report "$TMP/rev-report.json" --link "$TMP/rev-link.jsonl")

expect_exit 0 "пул v2 собирается из сред (только верифицируемая часть)" \
  python3 tools/build_rev_envs.py "${REV_ARGS[@]}"
expect_contains "BUILD_REV_ENVS OK" "отчёт конвертера называет результат" \
  python3 tools/build_rev_envs.py "${REV_ARGS[@]}"

expect_exit 0 "формат совместим с пайплайном, проекция gold не пуста, фильтры сработали" \
  python3 - "$TMP" <<'PY'
import json, pathlib, sys
sys.path.insert(0, "tools")
import build_rev_envs as B
T = pathlib.Path(sys.argv[1])
pool = [json.loads(l) for l in (T / "rev-pool-v2.jsonl").open(encoding="utf-8")]
card = json.loads((T / "rev-card.json").read_text(encoding="utf-8"))
rep = json.loads((T / "rev-report.json").read_text(encoding="utf-8"))
by_id = {t["source_task_id"]: t for t in pool}
codes = {e["task_id"]: e["reason_code"] for e in rep["excluded"]}

# (1) формат пайплайна: словарь типов верификатора, непустые промпт и gold
for t in pool:
    assert t["task_type"] in B.VERIFIER_TASK_TYPES, t
    assert t["prompt"].strip(), t
    assert t.get("expected_slugs") or t.get("keywords"), t
    branch = "expected_slugs" if t["task_type"] in B.SLUG_TASK_TYPES else "keywords"
    assert t.get(branch), t
# (2) проекция gold не даёт пустых ответов: эталонный ответ задачи принимается
#     замороженным верификатором (иначе — гарантированный ложный ноль)
for t in pool:
    if t["task_type"] in B.KEYWORD_TASK_TYPES:   # определения нужны как в пайплайне
        kws = [k for k in t["keywords"] if k not in B.KEYWORD_DROP]
        t["_defs"] = {k: "Алгоритм оптимизации политики и группы выходов для входа" for k in kws}
    assert B.verifier_accepts(t, B.gold_answer(t)), t
    assert not B.verifier_accepts(t, "ответ без проверяемых строк"), t["source_task_id"]
# (3) отбор сред: E2/E3/E5 + env_types в пуле, E1/E4/E6/E7/E8 — нет
assert by_id["E2_alpha_x"]["expected_slugs"] == ["alpha_x"], by_id["E2_alpha_x"]
assert by_id["E2_gamma_z"]["expected_slugs"] == ["gamma_z"], "сломанный YAML: slug по regex"
assert by_id["E3_eps_x"]["task_type"] == "find_concept", by_id["E3_eps_x"]
assert by_id["E5_alpha_x_to_eps_x"]["keywords"] == ["alpha_x", "eps_x"]
assert by_id["E5_alpha_x_to_eps_x"]["task_type"] == "explain_relation"
assert "E1_define_slug" not in by_id and "E4_extract_one" not in by_id, "судья в награде!"
assert "E6_contrast_pair" not in by_id and "E7_repair_one" not in by_id, "среда вне проекции"
assert not [t for t in pool if t.get("source_env") in ("E1_define", "E4_extract",
                                                       "E6_contrast", "E7_repair")], pool
# (4) лейк-правило и достижимость
assert codes["E2_beta_y"] == "prompt_leak", codes
assert codes["E2_unreachable"] == "gold_unreachable", codes
assert codes["E2_noslug"] == "gold_unprojectable", codes
assert codes["E2_short"] == "token_too_short", codes
assert codes["E5_empty_pair"] == "gold_unreachable", codes
# (5) дедуп: неоднозначный промпт снимает обе задачи, обратная пара — дубль
assert codes["E2_dup_one"] == "ambiguous_prompt" and codes["E2_dup_two"] == "ambiguous_prompt", codes
assert codes["E5_eps_x_to_alpha_x"] == "duplicate_gold", codes
assert "E2_dup_one" not in by_id and "E2_dup_two" not in by_id and "E5_eps_x_to_alpha_x" not in by_id
# (6) пересечения
assert codes["E2_sft_seen"] == "sft_overlap", codes
assert codes["E2_eval_seen"] == "eval_overlap", codes
assert codes["E2_v1_seen"] == "v1_overlap", codes
# (7) маршрутизация env_types: ood_pair без expected_slugs → explain_relation
rerouted = [t for t in pool if t.get("rerouted_from") == "ood_pair"]
assert len(rerouted) == 1 and rerouted[0]["task_type"] == "explain_relation", rerouted
assert rerouted[0]["keywords"] == ["gamma_z", "dup_one"], rerouted[0]
assert codes["line:4"] == "unknown_task_type", codes
# (9) проекция цепочек (S3f-fix-2): проверяемый набор — содержание концептов-условий
assert by_id["line:1"]["task_type"] == "chain_reasoning", by_id.get("line:1")
assert codes.get("line:1") is None, codes
assert by_id["line:1"]["expected_slugs"] == ["алгоритм", "оптимизации", "политики"], \
    by_id["line:1"]                       # термины определений, не имена концептов
assert by_id["line:1"]["chain_roles"]["start"] == "alpha_x", by_id["line:1"]
assert by_id["line:1"]["chain_roles"]["end"] == "eps_x", by_id["line:1"]
assert by_id["line:1"]["chain_roles"]["hint"] == "dup_two", by_id["line:1"]
assert "alpha_x" not in by_id["line:1"]["expected_slugs"], "имя концепта в проверке"
# (10) лейк-правило не ослаблено: проверяемые термины в промпте → снять (строка 5),
# keyword-ветка с порогом из промпта → снять (строка 8)
assert codes["line:5"] == "prompt_leak", codes
assert codes["line:8"] == "prompt_leak", codes
leak_forms = {e["task_id"]: e.get("leak_form") for e in rep["excluded"]
              if e["reason_code"] == "prompt_leak"}
assert leak_forms["line:5"] == "slug_in_prompt", leak_forms
assert leak_forms["line:8"] == "keyword_prompt_satisfies_verifier", leak_forms
# (11) проекция невозможна или недостижима → исключение с причиной, а не догадка
assert codes["line:6"] == "gold_unprojectable", codes   # якорей ролей нет
assert codes["line:7"] == "gold_unreachable", codes     # определений концептов нет
# (12) keyword-ветка без лейка фильтром не тронута (промпт не даёт терминов определений)
assert by_id["E5_alpha_x_to_eps_x"]["task_type"] == "explain_relation", by_id
assert card["leak_fix"]["excluded"]["by_form"]["slug_in_prompt"] == 2, card["leak_fix"]
assert card["leak_fix"]["excluded"]["by_form"]["keyword_prompt_satisfies_verifier"] == 1
assert card["leak_fix"]["after_fix"]["residual_answer_in_prompt"] == 0, card["leak_fix"]
assert card["leak_fix"]["rule"].startswith("Проверяемый ответ не должен"), card["leak_fix"]
# (13) числа дельты S3f-fix закреплены (историческая часть карточки), а развилка
# «всего / этим фильтром» названа и для текущей сборки
lf = card["leak_fix"]
assert lf["excluded"]["total"] == 3, lf["excluded"]
assert lf["after_leak_filter_candidates"] == card["counts"]["candidates"], lf
assert lf["delta"]["excluded_by_this_filter"] == 506, lf["delta"]
assert lf["delta"]["net_pool_change"] == 505, lf["delta"]
assert lf["delta"]["current_build_leak_exclusions"] == lf["excluded"]["total"], lf["delta"]
assert lf["after_fix"]["kept"] == lf["baseline_pool"]["lines"] - 505, lf["after_fix"]
assert lf["independent_check"]["after_fix"] == 0, lf["independent_check"]
assert "verify_task" in lf["independent_check"]["command"], lf["independent_check"]
assert lf["rollback"]["result"].startswith("воспроизводит базу ровно"), lf["rollback"]
# (14) проекция цепочек измерена, а не задекларирована: четыре цепочки в фикстуре —
# строка 1 спроецирована, строка 5 снята лейком, строка 6 без ролей, строка 7 без
# определений; подсказка промежуточного концепта измерена отдельно
cp = card["chain_projection"]
cr = card["envs"]["env_types"]["chain_projection"]
assert cr["checked"] == 4 and cr["projected"] == 2, cr
assert cr["roles_recognized"] == 3 and cr["roles_unknown"] == 1, cr
assert cr["projection_impossible"] == 2, cr
assert cr["hint_named"] == 1, cr                  # подсказку «например, 'b'» несёт строка 1
assert cr["by_task_type"] == {"chain_reasoning": 4}, cr
assert cp["chains_returned"]["total"] == 1, cp["chains_returned"]  # строка 1
assert cp["chains_returned"]["by_type"] == {"chain_reasoning": 1}, cp["chains_returned"]
assert cp["still_excluded"]["by_reason"] == {"prompt_leak": 1,
                                             "gold_unprojectable": 1,
                                             "gold_unreachable": 1}, cp["still_excluded"]
assert card["gold_projection"]["chain_definition_terms"].startswith("Цепочки"), card["gold_projection"]
assert card["checks"]["gold_in_prompt_audit"]["tasks_with_gold_in_prompt"] == 0, card["checks"]
# (8) карточка: числа по факту, sha256 пула, правило проекции
assert card["envs"]["E2_formula"]["registry_count"] == 75
assert card["envs"]["E2_formula"]["actual"] == 11, card["envs"]["E2_formula"]
assert card["envs"]["E6_contrast"]["kept"] == 0, card["envs"]["E6_contrast"]
assert card["envs"]["E6_contrast"]["excluded_reason"].startswith("judge_weight"), card
assert card["envs"]["E8_plan_experiment"]["excluded_reason"].startswith("среда не заполнена")
assert card["gold_projection"]["E3_classify"].startswith("gold_card → slug"), card["gold_projection"]
assert card["pool"]["sha256"] == B.sha256_file(T / "rev-pool-v2.jsonl")
assert card["counts"]["by_reason"], card["counts"]
assert any("Смешение двух генераций" in s for s in card["limits"]), card["limits"]
sys.exit(0)
PY

expect_exit 0 "проекция цепочек: роли нача́ло/конец/промежуточное и неослабленное правило" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import build_rev_envs as B

CHAIN = ("Построй цепочку рассуждений.\nНачало: 'start_c'\nКонец: 'end_c'\n"
         "Шаг 1: Найди определение 'start_c'.\n"
         "Шаг 2: найди промежуточный концепт (например, 'mid_c' или другой).")
FORMULA = "Свяжи концепты.\nКонцепт A: 'a_c'\nКонцепт B: 'b_c'\nНайди оба определения."
DEF = "Алгоритм оптимизации политики генерирует группы выходов для каждого входа"
TERMS = ["алгоритм", "оптимизации", "политики"]      # значимые слова DEF, первые три
DEFS = {k: DEF for k in ("start_c", "end_c", "a_c", "b_c", "mid_c", "other_c")}


def defs_with(concept, text):
    d = dict(DEFS)
    d[concept] = text
    return d


def chain(prompt=CHAIN, tt="chain_reasoning", defs=None):
    return {"task_type": tt, "prompt": prompt, "source_task_id": "t",
            "_defs": defs if defs is not None else DEFS}


def kw(keywords, prompt, defs=None):
    return {"task_type": "explain_relation", "prompt": prompt, "keywords": list(keywords),
            "source_task_id": "t",
            "_defs": defs if defs is not None else {k: DEF for k in keywords}}


# (1) роли «начало» и «конец»: проверяется СОДЕРЖАНИЕ их определений, не имена
terms, roles = B.chain_checked_strings(CHAIN, "chain_reasoning", DEFS)
assert terms == TERMS, terms
assert roles["start"] == "start_c" and roles["end"] == "end_c", roles
assert roles["checked_from"]["start"] == TERMS, roles
assert roles["checked_from"]["end"] == TERMS, roles
# (2) имена концептов в проверку не входят — они условие задачи (и лежат в промпте)
assert not any(s in terms for s in ("start_c", "end_c", "mid_c")), terms
# (3) роль «промежуточное» измерена и НЕ проверяется: промпт называет подсказку,
#     проверяемый набор её не требует — ответ из одних имён награды не получает
assert roles["hint"] == "mid_c", roles
task = chain()
task["expected_slugs"] = terms
assert B.verifier_accepts(task, B.gold_answer(task)) is True, "эталон обязан приниматься"
for answer, what in (("Ответ: start_c, end_c", "только концы"),
                     ("Ответ: start_c, mid_c, end_c", "концы + промежуточный"),
                     (CHAIN, "копия промпта")):
    assert not B.verifier_accepts(task, answer), f"{what}: награда без содержания"
# (4) лейк-правило не ослаблено: совпадение проверяемого термина с промптом — лейк,
#     даже когда термин не является именем концепта-условия (прежнее ролевое
#     исключение прощало ровно такие совпадения как «промежуточное значение»)
PROM = CHAIN + "\nАлгоритм оптимизации политики важен."
leaky = chain(PROM)
leaky["expected_slugs"] = terms
form, detail = B.leak_form(leaky, PROM)
assert form == "slug_in_prompt", (form, detail)
# (5) промпт без цитаты определения лейком не считается
clean = chain()
clean["expected_slugs"] = terms
assert B.leak_form(clean, CHAIN) == (None, "")
# (6) роли не распознаны или определений нет → проекция невозможна (не догадка),
#     а если определение есть только у одного концепта — набор собирается из него
assert B.chain_checked_strings("Просто цепочка про 'start_c'.",
                               "chain_reasoning", DEFS) is None
assert B.chain_checked_strings("Начало: 'start_c'\nКонец: 'end_c'", "chain_reasoning",
                               defs_with("start_c", ""))[0] == TERMS, "термины конца"
assert B.chain_checked_strings("Начало: 'x_one'\nКонец: 'x_two'", "chain_reasoning",
                               DEFS) is None, "определений нет ни у одного концепта"
# (7) formula_chain: роли A/B, подсказки промежуточного в формате нет
fterms, froles = B.chain_checked_strings(FORMULA, "formula_chain", DEFS)
assert fterms == TERMS and froles["start"] == "a_c" and froles["end"] == "b_c", froles
assert froles["hint"] is None, froles
# (8) keyword-ветка: порог терминов набран из промпта → лейк; обычный промпт — нет
assert B.leak_form(kw(["k_one", "k_two"], "Как «алгоритм оптимизации политики» связан с «группы выходов»?"),
                   "Как «алгоритм оптимизации политики» связан с «группы выходов»?")[0] \
    == "keyword_prompt_satisfies_verifier"
assert B.leak_form(kw(["k_one", "k_two"], "Как связаны Alpha X и Beta Y?"),
                   "Как связаны Alpha X и Beta Y?") == (None, "")
# (9) keyword-ветка: slug-и (`keywords`) в промпте лейком НЕ считаются — верификатор
#     ищет в ответе термины определений, а не сами концепты
assert B.leak_form(kw(["k_one", "k_two"], "Объясни связь 'k_one' и 'k_two'."),
                   "Объясни связь 'k_one' и 'k_two'.") == (None, "")
# (10) одна форма правила на оба контура: проверка — общая функция стража C-009
from check_eval_leakage import checked_in_prompt
assert checked_in_prompt("Beta_Y", "запиши формулу beta_y в latex") is True
assert checked_in_prompt("beta_y", "запиши формулу Beta Y в latex") is False
sys.exit(0)
PY

expect_exit 0 "пул не выпускает задачи, которые верификатор не примет" \
  python3 - "$TMP" <<'PY'
import json, sys, pathlib
sys.path.insert(0, "tools")
import build_rev_envs as B
pool = [json.loads(l) for l in pathlib.Path(sys.argv[1] + "/rev-pool-v2.jsonl").open(encoding="utf-8")]
assert pool, "пул пуст"
for t in pool:
    assert t["task_type"] in B.VERIFIER_TASK_TYPES, t["task_type"]
sys.exit(0)
PY

expect_exit 0 "с тем же содержимым пул не переписывается (идемпотентность)" \
  python3 tools/build_rev_envs.py "${REV_ARGS[@]}"
expect_exit 1 "существующий пул не перезаписывается молча (нужен --force)" \
  bash -c "printf '%s\n' '{\"task_type\": \"find_concept\"}' >> '$TMP/rev-pool-v2.jsonl' && python3 tools/build_rev_envs.py ${REV_ARGS[*]}"
expect_exit 0 "--force перезаписывает пул (явное решение, а не молчание)" \
  python3 tools/build_rev_envs.py "${REV_ARGS[@]}" --force
expect_exit 2 "нет пула env_types → NOT-VERIFIED, а не пустой пул" \
  python3 tools/build_rev_envs.py "${REV_ARGS[@]}" --env-types "$TMP/нет-такого.jsonl"
expect_exit 2 "--dry-run с --update-registry несовместим" \
  python3 tools/build_rev_envs.py "${REV_ARGS[@]}" --dry-run --update-registry

expect_exit 0 "реестр сред пересобирается по факту, комментарии и бэкап целы" \
  python3 - "$TMP" <<'PY'
import pathlib, re, subprocess, sys
T = pathlib.Path(sys.argv[1])
args = ["python3", "tools/build_rev_envs.py",
        "--envs-root", str(T / "rev"), "--env-types", str(T / "rev-env-types.jsonl"),
        "--sft", str(T / "rev-sft.jsonl"), "--eval", str(T / "rev-eval.jsonl"),
        "--v1", str(T / "rev-v1.jsonl"), "--index", str(T / "rev-index.jsonl"),
        "--out", str(T / "rev-pool-v2.jsonl"), "--card", str(T / "rev-card.json"),
        "--report", str(T / "rev-report.json"), "--link", str(T / "rev-link.jsonl"),
        "--update-registry", "--force"]
r = subprocess.run(args, capture_output=True, text=True)
assert r.returncode == 0, r.stdout + r.stderr
yaml = (T / "rev" / "envs.yaml").read_text(encoding="utf-8")
assert "# комментарий, который обязан выжить" in yaml, "комментарии реестра потеряны"
assert re.search(r"E2_formula:\n(?:.*\n)*?\s*task_count: 11", yaml), yaml
assert re.search(r"E6_contrast:\n(?:.*\n)*?\s*task_count: 1", yaml), yaml
assert re.search(r"E8_plan_experiment:\n\s*status: empty", yaml), yaml
assert "не заполнена" in yaml.split("E8_plan_experiment:")[-1], yaml
backup = (T / "rev" / "envs.yaml.bak_20260916")
assert backup.is_file() and "task_count: 75" in backup.read_text(encoding="utf-8"), "бэкап"
sys.exit(0)
PY

expect_exit 0 "словарь типов и термины верификатора не разошлись с пайплайном" \
  python3 - <<'PY'
import re, sys, pathlib
sys.path.insert(0, "tools")
import build_rev_envs as B
src = pathlib.Path(B.PIPELINE)
if not src.is_file():
    print("SKIP: пайплайн недоступен — словарь не сверить (NOT-VERIFIED)")
    sys.exit(0)
text = src.read_text(encoding="utf-8", errors="replace")
m = re.search(r"if tt in \(([^)]*)\)", text)
assert m, "в пайплайне не найден диспетчер verify_task"
pipeline_types = tuple(re.findall(r'"([a-z_]+)"', m.group(1)))
assert pipeline_types == B.SLUG_TASK_TYPES, (pipeline_types, B.SLUG_TASK_TYPES)
assert tuple(re.findall(r'"([а-яa-z_]+)"',
              re.search(r'k\.lower\(\) not in \(([^)]*)\)', text).group(1))) == B.KEYWORD_DROP
m = re.search(r'_COMPARE_STOP = frozenset\("""(.*?)"""\.split\(\)\)', text, re.S)
assert m, "в пайплайне не найден _COMPARE_STOP"
assert frozenset(m.group(1).split()) == B._COMPARE_STOP, "стоп-слова разошлись с пайплайном"
assert "MULTI_STEP_TASKS = {" in text
sys.exit(0)
PY

expect_exit 0 "пул v2 в кейсе совпадает с карточкой (артефакт собран и не разъехался)" \
  python3 - <<'PY'
import json, sys
sys.path.insert(0, "tools")
from pathlib import Path
import build_rev_envs as B
pool, card = Path("datasets/rl_tasks_revpool_v2.jsonl"), Path("data/rev-envs-v2-card.json")
if not pool.is_file() or not card.is_file():
    print("SKIP: пула v2 или карточки нет — нечего сверять")
    sys.exit(0)
c = json.loads(card.read_text(encoding="utf-8"))
tasks = [json.loads(l) for l in pool.open(encoding="utf-8") if l.strip()]
assert c["pool"]["sha256"] == B.sha256_file(pool), "sha256 карточки != хеш пула"
assert c["pool"]["lines"] == len(tasks), c["pool"]
assert c["sources_unchanged"] is True
# лейк-фикс S3f-fix: числа той дельты закреплены (пул 9197 → 8692, снято 506, в базе 102)
lf = c["leak_fix"]
assert lf["delta"]["net_pool_change"] == 505, lf["delta"]
assert lf["delta"]["excluded_by_this_filter"] == 506, lf["delta"]
assert lf["after_fix"]["kept"] == lf["baseline_pool"]["lines"] - 505, lf["after_fix"]
assert lf["excluded"]["already_in_baseline"]["total"] == 102, lf["excluded"]
assert lf["excluded"]["total"] == 108 + 30, lf["excluded"]   # 102 E2/E3 + 6 E5 + 30 цепочек
assert lf["excluded"]["by_form"]["keyword_prompt_satisfies_verifier"] == 6, lf["excluded"]
assert lf["after_fix"]["residual_answer_in_prompt"] == 0, lf["after_fix"]
assert c["checks"]["gold_in_prompt_audit"]["tasks_with_gold_in_prompt"] == 0, c["checks"]
assert set(c["counts"]["by_task_type"]) <= set(B.VERIFIER_TASK_TYPES), c["counts"]["by_task_type"]
for env in ("E1_define", "E4_extract", "E6_contrast", "E7_repair", "E8_plan_experiment"):
    assert c["envs"][env]["excluded_reason"], env
# ── проекция цепочек (S3f-fix-2): числа карточки против факта пула ────────────
cp = c["chain_projection"]
assert cp["before"]["lines"] == 8692, cp["before"]
assert cp["before"]["by_task_type_chains"] == {"chain_reasoning": 0, "formula_chain": 0}, cp
assert cp["after"]["pool_lines"] == len(tasks) and cp["after"]["sha256"] == c["pool"]["sha256"], cp
live = {}
for t in tasks:
    if t.get("task_type") in B.CHAIN_TASK_TYPES:
        live[t["task_type"]] = live.get(t["task_type"], 0) + 1
assert live == cp["chains_returned"]["by_type"], (live, cp["chains_returned"])
assert live == cp["after"]["by_type_chains"], (live, cp["after"])
assert cp["chains_returned"]["total"] == sum(live.values()) > 0, cp["chains_returned"]
assert c["counts"]["by_task_type"].get("chain_reasoning") == live.get("chain_reasoning", 0), c["counts"]
assert c["counts"]["by_task_type"].get("formula_chain") == live.get("formula_chain", 0), c["counts"]
# каждая цепочка пула несёт провенанс ролей и проверяемый набор из терминов
# определений: имена концептов в проверке не участвуют
for t in tasks:
    if t.get("task_type") not in B.CHAIN_TASK_TYPES:
        continue
    roles = t.get("chain_roles") or {}
    assert roles.get("start") and roles.get("end"), t["source_task_id"]
    assert t["expected_slugs"], t["source_task_id"]
    assert not [s for s in t["expected_slugs"] if s in (roles["start"], roles["end"],
                                                       roles.get("hint"))], t["source_task_id"]
    assert B.leak_form(t, t["prompt"]) == (None, ""), t["source_task_id"]
# отсев цепочек назван поимённо: сколько снято и почему
assert cp["reconciliation"]["unaccounted"] == 0, cp["reconciliation"]
assert cp["reconciliation"]["source_total"] == sum(cp["chains_returned"]["source"].values()), cp
assert cp["still_excluded"]["total"] > 0, cp["still_excluded"]
assert sum(cp["still_excluded"]["by_reason"].values()) == cp["still_excluded"]["total"], cp
assert cp["conservative_not_measured"]["dropped_by_single_term_coincidence"] == \
    cp["still_excluded"]["by_reason"].get("prompt_leak", 0), cp["conservative_not_measured"]
# замер приёмки в карточке — против артефакта пробы (иначе «подтверждено» — слово)
ev = Path(cp["validation"]["evidence"])
assert ev.is_file(), f"нет артефакта пробы: {ev}"
assert cp["validation"]["evidence_sha256"] == B.sha256_file(ev), \
    "карточка ссылается на другие байты замера — проба переснята после сборки"
e = json.loads(ev.read_text(encoding="utf-8"))
assert e["pool"]["sha256"] == c["pool"]["sha256"], "проба снята с другого пула"
assert e["pool"]["lines"] == len(tasks), e["pool"]
m = e["measured"]
assert cp["validation"]["gold_accepted"] == {
    k: m["gold_accepted"][k] for k in ("checked", "accepted")}, cp["validation"]
assert cp["validation"]["prompt_copy_rewarded"]["after_fix"] == m["prompt_copy_rewarded"]["rewarded"], cp
assert cp["validation"]["prompt_copy_rewarded"]["checked"] == m["prompt_copy_rewarded"]["checked"], cp
assert cp["validation"]["ends_only_rejected"] == {
    k: m["ends_only_rejected"][k] for k in ("checked", "rejected")}, cp["validation"]
assert cp["validation"]["intermediate_not_checked"] == {
    k: m["intermediate_not_checked"][k] for k in ("checked", "rejected")}, cp["validation"]
assert e["baseline_projection"]["checked"] == 500, e["baseline_projection"]
assert cp["validation"]["prompt_copy_rewarded"]["before_fix"]["checked"] == 500, cp
assert e["ok"] is True and e["acceptance"] == {
    "gold_accepted": True, "prompt_copy_rewarded_zero": True, "ends_only_rejected": True}, e
# критерий приёмки сформулирован про **пул**: копия промпта не награждается нигде
assert e["pool_wide"]["checked"] == len(tasks), e["pool_wide"]
assert e["pool_wide"]["prompt_copy_rewarded"] == 0, e["pool_wide"]
assert cp["validation"]["prompt_copy_rewarded_pool_wide"] == {
    "checked": e["pool_wide"]["checked"],
    "rewarded": e["pool_wide"]["prompt_copy_rewarded"]}, cp["validation"]
# правило проекции названо в карточке и в конвертере одним текстом
assert cp["rule"] == B.PROJECTION_RULES["chain_definition_terms"], "текст правила разошёлся"
assert c["gold_projection"]["chain_definition_terms"] == cp["rule"], c["gold_projection"]
assert "chain_leak_exception" not in c["gold_projection"], "оставлено снятое правило"
print(f"пул v2: {c['pool']['lines']} задач, sha256 {c['pool']['sha256'][:12]}…, "
      f"цепочек {cp['chains_returned']['total']} ({live})")
sys.exit(0)
PY

echo
echo "== 15. build_gen_eval_v2.py (ADR-018: наборы GEN-EVAL v2) =="
# Фикстуры: фиктивный сетевой диск с корпусами пилота и «уже собранными» v1-наборами.
# Обучающий материал моделируется теми же правилами нарезки, что у пилота: реплей —
# split("\n\n") + первые N чанков, домен — split("\n---\n") целиком.
GEV="$TMP/gev2"
mkdir -p "$GEV/shared/datasets" "$GEV/empty/datasets" "$GEV/out1" "$GEV/out2"
python3 - "$GEV/shared/datasets" "$GEV/tok.json" <<'PY'
import sys
from pathlib import Path
d = Path(sys.argv[1])

def words(k, n):
    return " ".join(f"slovo{k}x{i}" for i in range(n))

# реплей: doc0 — в обученном префиксе; doc1 — почти копия doc0 (near-duplicate);
# doc2 — дословно совпадает с текстом SFT; doc3 — с промптом RL; doc4 — короче порога;
# doc5 — содержит разделитель набора внутри; doc6..doc13 — чистые.
rep = [
    f"doc0 {words(0, 40)}",
    f"doc1 {words(0, 40)} hvost",
    f"doc2 {words(2, 40)}",
    f"doc3 {words(3, 40)}",
    f"doc4 {words(4, 13)}",
    f"doc5 {words(5, 20)}\n---\n{words(5, 20)}",
] + [f"doc{i} {words(i, 40)}" for i in range(6, 14)]
(d / "general_replay_ru.txt").write_text("\n\n".join(rep) + "\n", encoding="utf-8")

def card(slug, body):
    return (f"# Концепт: {slug}\n## Тип: behavioral\n\n---\nslug: {slug}\ntype: behavioral\n---\n\n"
            f"## Определение\n{body}")

# домен, названный в ADR: две карточки — их тексты и slug считаются обученными
(d / "cpt_corpus_v10.1.txt").write_text(
    "\n---\n".join([card("alpha_card", words(100, 40)), card("beta_card", words(101, 40))]) + "\n",
    encoding="utf-8")

# полный дамп: alpha_card — та же карточка (отсев по slug); new_beta — новый slug, но тело
# дословно как у beta_card (отсев по тексту); delta_short — короткая секция; остальные чистые
full = [card("alpha_card", words(100, 40)), card("new_beta", words(101, 40)),
        card("delta_short", "мало")] + [card(f"clean_{i}", words(200 + i, 40)) for i in range(6)]
(d / "cpt_corpus_full.txt").write_text("\n---\n".join(full) + "\n", encoding="utf-8")

(d / "sft_train_v12.jsonl").write_text(
    '{"messages": [{"role": "user", "content": "%s"}]}\n' % rep[2], encoding="utf-8")
(d / "rl_tasks_revpool_v1.jsonl").write_text(
    '{"task_type": "find_concept", "prompt": "%s"}\n' % rep[3], encoding="utf-8")
for name in ("general_eval.txt", "domain_eval.txt"):
    (d / name).write_text("старый набор пилота\n" * 20, encoding="utf-8")
    (d.parent.parent / "empty" / "datasets" / name).write_text("x\n" * 20, encoding="utf-8")

# Крошечный word-level токенизатор (1 слово = 1 токен): делает позиционное правило
# проверяемым без сети и без кэша HF. Стенд собирает теми же путями Qwen2.5.
from tokenizers import Tokenizer, models, pre_tokenizers
tk = Tokenizer(models.WordLevel(vocab={"[UNK]": 0}, unk_token="[UNK]"))
tk.pre_tokenizer = pre_tokenizers.Whitespace()
tk.save(sys.argv[2])
print("  фикстуры gev2 готовы")
PY

GCARD="$GEV/card.json"
GEVARGS=(--shared "$GEV/shared" --out-dir "$GEV/out1" --card "$GCARD"
         --tokenizer "$GEV/tok.json" --replay-chunks 1 --chunk-tokens 41 --docs 4 --min-chars 200)
expect_exit 0 "синтетика: наборы собраны" \
  python3 tools/build_gen_eval_v2.py "${GEVARGS[@]}"
expect_exit 0 "отсев назван по причинам, а не свёрнут в счётчик" \
  python3 - "$GCARD" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
g, m = d["sets"]["general"], d["sets"]["domain"]
assert g["candidates"] == 14, g["candidates"]
assert g["excluded"]["replay_token_prefix"] == 1, g["excluded"]      # doc0 в префиксе
assert g["consumed_prefix"]["tokens"] == 41 and g["consumed_prefix"]["docs"] == 1, g["consumed_prefix"]
assert g["excluded"]["exact_match_train"] == 2, g["excluded"]        # doc2 (SFT), doc3 (RL)
assert g["excluded_by_source"] == {"sft": 1, "rl": 1}, g["excluded_by_source"]
assert g["excluded"]["too_short"] == 1, g["excluded"]                # doc4
assert g["excluded"]["doc_separator_inside"] == 1, g["excluded"]     # doc5
assert g["excluded"]["near_duplicate_ngram"] == 1, g["excluded"]     # doc1
assert g["pool_after_filter"] == 8 and g["documents"] == 4, (g["pool_after_filter"], g["documents"])
assert m["excluded"]["card_identity_in_training"] == 1, m["excluded"]  # тот же slug
assert m["excluded"]["exact_match_train"] == 1, m["excluded"]          # новый slug, тело обучено
assert m["excluded_by_source"] == {"domain_cpt": 1}, m["excluded_by_source"]
assert m["excluded"]["too_short"] == 1, m["excluded"]
assert m["excluded"]["doc_separator_inside"] == 0, m["excluded"]       # структурно невозможно
assert m["excluded"]["not_section_unit"] > 0, m["excluded"]            # заголовки и front matter
assert m["pool_after_filter"] == 6, m["pool_after_filter"]
# корпус, названный в ADR-018: из него набор не собирается — отсев 100 %
assert m["adr_named_source"]["units"] == 6 and m["adr_named_source"]["kept"] == 0, m["adr_named_source"]
assert m["adr_named_source"]["excluded_exact_match"] == 6, m["adr_named_source"]
# остаточное пересечение измерено для обоих наборов (не гейт, но число)
assert g["residual_overlap"]["documents"] == 4, g["residual_overlap"]
assert m["residual_overlap"]["index_shingles"] > 0, m["residual_overlap"]
assert len(d["limits"]) >= 5 and any("пересказ" in l.lower() for l in d["limits"]), d["limits"]
for s in (g, m):
    assert s["sha256"] and s["documents"] == 4 and s["length"]["min"] > 0, s
assert d["training_material"]["domain_cpt"]["sha256"], d["training_material"]
assert d["pilot_files_after"] == "unchanged" and not d["pilot_files_changed"]
sys.exit(0)
PY
expect_exit 0 "формат как у v1: разделитель, хвостовой перевод строки, нет пустых строк вокруг" \
  python3 - "$GEV/out1/general_eval_v2.txt" <<'PY'
import sys
from pathlib import Path
b = Path(sys.argv[1]).read_bytes()
t = b.decode("utf-8")
docs = [x.strip() for x in t.split("\n---\n") if len(x.strip()) > 50]
assert len(docs) == 4, len(docs)
assert b.endswith(b"\n") and not t.rstrip("\n").endswith("---"), "хвост файла"
assert t.count("\n---\n") == 3, t.count("\n---\n")
assert "\n\n---\n\n" not in t, "пустые строки вокруг разделителя: формат v1 их не имеет"
assert all("\n---\n" not in x for x in docs), "внутри документа есть разделитель набора"
sys.exit(0)
PY
expect_exit 0 "идемпотентность: второй прогон в другой каталог" \
  python3 tools/build_gen_eval_v2.py --shared "$GEV/shared" --out-dir "$GEV/out2" \
    --card "$GEV/card2.json" --tokenizer "$GEV/tok.json" --replay-chunks 1 --chunk-tokens 41 \
    --docs 4 --min-chars 200
expect_exit 0 "байты двух прогонов совпали (seed фиксирован)" \
  python3 - "$GEV/out1/general_eval_v2.txt" "$GEV/out2/general_eval_v2.txt" "$GEV/card.json" "$GEV/card2.json" <<'PY'
import hashlib, json, sys
from pathlib import Path
a, b = (hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sys.argv[1:3])
assert a == b, (a, b)
ca, cb = (json.loads(Path(p).read_text(encoding="utf-8")) for p in sys.argv[3:5])
assert ca["sets"]["general"]["source_indices"] == cb["sets"]["general"]["source_indices"]
assert ca["sets"]["domain"]["source_indices"] == cb["sets"]["domain"]["source_indices"]
sys.exit(0)
PY
expect_exit 0 "шингл-индекс: считает общие n-граммы и не путает разные тексты" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import build_gen_eval_v2 as B
a = " ".join(f"w{i}" for i in range(40))
b = a + " hvost"
c = " ".join(f"z{i}" for i in range(40))
ix = B.ShingleIndex(12)
ix.add_text(a)
ix.finalize()
assert ix.shared(a) == (29, 29), ix.shared(a)
assert ix.shared(b) == (29, 30), ix.shared(b)
assert ix.shared(c) == (0, 29), ix.shared(c)
assert B.norm("  А  Б ") == "а б" and B.DOC_SEP == "\n---\n"
sys.exit(0)
PY
expect_exit 0 "norm/render/parse: round-trip формата набора" \
  python3 - "$TMP/gev2-roundtrip.txt" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "tools")
import build_gen_eval_v2 as B
docs = ["документ номер один, достаточно длинный для _ppl_eval"] * 3
p = Path(sys.argv[1])
p.write_text(B.render_docs(docs), encoding="utf-8")
assert B.parse_eval_file(p) == docs, B.parse_eval_file(p)
try:
    B.render_docs(["a\n---\nb"])
except ValueError as e:
    assert "разделитель" in str(e)
else:
    raise AssertionError("документ с разделителем не отвергнут")
sys.exit(0)
PY
expect_exit 1 "пул меньше требуемого объёма → отказ, а не молчаливое усечение" \
  python3 tools/build_gen_eval_v2.py --shared "$GEV/shared" --out-dir "$GEV/out1" \
    --card "$GEV/card2.json" --tokenizer "$GEV/tok.json" --replay-chunks 1 --chunk-tokens 41 \
    --docs 99 --min-chars 200
expect_contains "пул после фильтра" "отказ называет размер пула и требуемый объём" \
  python3 tools/build_gen_eval_v2.py --shared "$GEV/shared" --out-dir "$GEV/out1" \
    --card "$GEV/card2.json" --tokenizer "$GEV/tok.json" --replay-chunks 1 --chunk-tokens 41 \
    --docs 99 --min-chars 200
expect_exit 2 "нет обучающего корпуса → NOT-VERIFIED (не зелёный)" \
  python3 tools/build_gen_eval_v2.py --shared "$GEV/empty" --out-dir "$GEV/out1" \
    --card "$GEV/card2.json" --tokenizer "$GEV/tok.json" --docs 1
expect_exit 0 "--dry-run: считает, но не пишет" \
  python3 tools/build_gen_eval_v2.py --shared "$GEV/shared" --out-dir "$GEV/never" \
    --card "$GEV/never/card.json" --tokenizer "$GEV/tok.json" --replay-chunks 1 \
    --chunk-tokens 41 --docs 4 --min-chars 200 --dry-run
expect_exit 0 "--dry-run не создал ни каталога, ни карточки" \
  test ! -e "$GEV/never"
expect_contains "документов (split" "режим --verify печатает ожидаемое число документов" \
  python3 tools/build_gen_eval_v2.py --verify "$GEV/out1/general_eval_v2.txt" \
    --tokenizer "$GEV/tok.json"
expect_exit 2 "--verify без файла → NOT-VERIFIED" \
  python3 tools/build_gen_eval_v2.py --verify "$GEV/nope.txt" --tokenizer "$GEV/tok.json"
expect_exit 0 "файлы пилота не тронуты сборкой (содержимое то же)" \
  python3 - "$GEV/shared/datasets" <<'PY'
import sys
from pathlib import Path
for n in ("general_eval.txt", "domain_eval.txt"):
    assert (Path(sys.argv[1]) / n).read_text(encoding="utf-8").startswith("старый"), n
sys.exit(0)
PY
expect_exit 0 "реальные наборы v2: ≥200 документов, хеши сходятся с карточкой" \
  python3 - <<'PY'
import hashlib, json, sys
from pathlib import Path
card = json.loads(Path("data/gen-eval-v2-card.json").read_text(encoding="utf-8"))
for key, name in (("general", "datasets/general_eval_v2.txt"),
                  ("domain", "datasets/domain_eval_v2.txt")):
    p = Path(name)
    assert p.is_file(), f"{p} отсутствует"
    raw = p.read_bytes()
    docs = [d.strip() for d in raw.decode("utf-8").split("\n---\n") if len(d.strip()) > 50]
    assert len(docs) >= 200, f"{key}: {len(docs)} документов < 200"
    assert raw.endswith(b"\n"), key
    assert hashlib.sha256(raw).hexdigest() == card["sets"][key]["sha256"], key
    assert card["sets"][key]["documents"] >= 200, key
    assert card["sets"][key]["excluded"], f"{key}: нет чисел отсева"
    assert card["sets"][key]["readback_ok"] is True, key
assert card["limits"] and card["pilot_files_after"] == "unchanged", "limits/пилот"
sys.exit(0)
PY
expect_exit 0 "карточка в кейсе, наборы подключены симлинками на сетевой диск (AD-4)" \
  python3 - <<'PY'
import sys
from pathlib import Path
for n in ("general_eval_v2.txt", "domain_eval_v2.txt"):
    p = Path(n)
    assert p.is_symlink(), f"{n} — не симлинк"
    assert str(p.resolve()).startswith("/home/user/gb10-shared/"), p.resolve()
assert Path("data/gen-eval-v2-card.json").is_file()
sys.exit(0)
PY
echo "== 15. validate_verifiers.py (S3i: валидация верификаторов сред, ADR-021 п.2) =="
export TMP  # вложенные python-проверки читают каталог фикстур из окружения
# Синтетический каталог сред повторяет раскладку настоящего (tasks/*.jsonl,
# verifiers/*.py, envs.yaml): проба берёт среду по имени, а поведение задаёт
# подложенный верификатор. Два верификатора — два полюса: нечувствительный к
# порядку (перемешанный эталон проходит) и точный (не проходит).
VV="$TMP/vv"
mkdir -p "$VV/verifiers" "$VV/tasks"
cat > "$VV/verifiers/v_define.py" <<'PY'
"""Синтетический E1: доля пересечения множеств слов — порядок не важен."""
def verify_define(answer, gold):
    g = set(gold.lower().split())
    a = set(answer.lower().split())
    return len(g & a) / max(len(g), 1)
PY
cat > "$VV/verifiers/v_contrast.py" <<'PY'
"""Синтетический E6: точное совпадение — порядок и состав важны."""
def verify_contrast(answer, gold_contrast):
    return 1.0 if answer.strip() == gold_contrast.strip() else 0.0
PY
cat > "$VV/tasks/define.jsonl" <<'JSONL'
{"task_id": "E1_alpha", "prompt": "Дай определение Alpha.", "gold_card": "## Определение\nalpha равно единице.\n\n## Мотивация\nтак надо."}
{"task_id": "E1_beta", "prompt": "Дай определение Beta.", "gold_card": "## Определение\nbeta равно двум.\n\n## Мотивация\nиначе нельзя."}
{"task_id": "E1_gamma", "prompt": "Дай определение Gamma.", "gold_card": "## Определение\ngamma равно трём.\n\n## Мотивация\nпотому что."}
JSONL
cat > "$VV/tasks/contrast.jsonl" <<'JSONL'
{"task_id": "E6_a", "prompt": "Чем A отличается от B?", "gold_diff": "В отличие от альфы, бета считает дважды."}
{"task_id": "E6_b", "prompt": "Чем C отличается от D?", "gold_diff": "В отличие от гаммы, дельта молчит."}
JSONL
# Реестр фикстуры нарочно врёт в E1 (99 против 3) — сверка «реестр против факта»
# должна это назвать, а не промолчать.
cat > "$VV/envs.yaml" <<'YAML'
environments:
  E1_define:
    status: ready
    tasks_file: tasks/define.jsonl
    task_count: 99
  E6_contrast:
    status: ready
    tasks_file: tasks/contrast.jsonl
    task_count: 2
YAML

echo "  --- 15a. план: что и как измеряется ---"
expect_exit 0 "план печатается без чтения каталога сред" \
  python3 tools/validate_verifiers.py --plan
expect_contains "binary-safe ⇔ min(позитив) > max(негатив)" "вердикт назван в плане" \
  python3 tools/validate_verifiers.py --plan
expect_contains "E7_repair" "в плане все семь сред" \
  python3 tools/validate_verifiers.py --plan

echo "  --- 15b. синтетика: ожидаемый score и вердикт ---"
expect_exit 0 "точный верификатор: эталон отделяется, порог 0.5" \
  python3 - <<'PY'
import json, os, subprocess, sys
tmp = os.environ["TMP"]
r = subprocess.run([sys.executable, "tools/validate_verifiers.py",
                    "--envs-root", f"{tmp}/vv", "--env", "E6_contrast",
                    "--n", "2", "--no-evidence", "--json"],
                   capture_output=True, text=True)
assert r.returncode == 0, (r.returncode, r.stdout[-800:], r.stderr[-800:])
rep = json.loads(r.stdout)
e = rep["verifiers"]["E6_contrast"]
assert e["n"] == 2, e["n"]
assert e["positive_min"] == e["positive_median"] == e["positive_max"] == 1.0, e["positive"]
assert e["false_negatives_share"] == 0.0, e["false_negatives_share"]
assert e["negative_max"] == 0.0, e["negatives"]
assert e["proposed_threshold"] == 0.5, e["proposed_threshold"]
assert e["verdict"] == "binary-safe", e["verdict"]
# односстрочный эталон: перемешивание строк не построить — это сказано, а не спрятано
sh = e["negatives"]["shuffled_lines"]
assert sh["n"] == 0 and sh["not_applicable"] == 2 and "однострочный" in sh["reason"], sh
assert e["negatives"]["empty"]["max"] == 0.0, e["negatives"]["empty"]
assert e["scale_max_reachable"] is True and e["adr_strict_no_false_negatives"] is True
# реестр фикстуры говорит 2, в банке 2 — расхождения нет
rows = {x["env"]: x for x in rep["registry"]["rows"]}
assert rows["E6_contrast"]["matches"] is True, rows["E6_contrast"]
sys.exit(0)
PY

expect_exit 0 "среда без разделимости: перемешанный эталон проходит наравне с эталоном" \
  python3 - <<'PY'
import json, os, subprocess, sys
tmp = os.environ["TMP"]
r = subprocess.run([sys.executable, "tools/validate_verifiers.py",
                    "--envs-root", f"{tmp}/vv", "--env", "E1_define",
                    "--n", "3", "--no-evidence", "--probe-only", "--json"],
                   capture_output=True, text=True)
assert r.returncode == 0, (r.returncode, r.stderr[-800:])
rep = json.loads(r.stdout)
e = rep["verifiers"]["E1_define"]
# позитив максимален, но перемешанный эталон набирает ровно столько же:
# пересечение множеств слов порядок не видит
assert e["positive_min"] == 1.0 and e["false_negatives_share"] == 0.0, e["positive"]
assert e["negatives"]["shuffled_lines"]["max"] == 1.0, e["negatives"]["shuffled_lines"]
assert e["negative_max"] == 1.0 and e["gap"] == 0.0, (e["negative_max"], e["gap"])
assert e["breaking_negative"] == ["shuffled_lines"], e["breaking_negative"]
assert e["proposed_threshold"] is None and e["verdict"] == "unsafe", e
assert rep["excluded"] and rep["excluded"][0]["env"] == "E1_define"
assert rep["adr_021_p2"]["envs_failing_separation"] == ["E1_define"], rep["adr_021_p2"]
# реестр врёт (99 против 3) — расхождение названо поимённо
assert rep["registry"]["stale"] == ["E1_define"], rep["registry"]
sys.exit(0)
PY

expect_exit 1 "среда без порога — сигнал (exit 1), а не тихий зелёный" \
  python3 tools/validate_verifiers.py --envs-root "$VV" --env E1_define \
      --n 3 --no-evidence
expect_exit 0 "тот же замер в режиме вердикта (--probe-only) — exit 0" \
  python3 tools/validate_verifiers.py --envs-root "$VV" --env E1_define \
      --n 3 --no-evidence --probe-only

echo "  --- 15c. негативы детерминированы (тот же seed → те же ответы) ---"
expect_exit 0 "тот же seed даёт тот же замер; evidence не пачкается повторным прогоном" \
  python3 - <<'PY'
import json, os, subprocess, sys
tmp = os.environ["TMP"]
ev = f"{tmp}/vv-evidence.json"
cmd = [sys.executable, "tools/validate_verifiers.py", "--envs-root", f"{tmp}/vv",
       "--env", "E1_define", "--n", "3", "--evidence", ev,
       "--thresholds", f"{tmp}/vv-thresholds.json", "--json"]
first = json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout)
assert first["checks"]["sources_unchanged"] is True, first["checks"]
second = json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout)
assert second["reproduced"] is True, "повторный прогон с тем же замером переписал evidence"
assert first["fingerprint"] == second["fingerprint"], "замер не воспроизвёлся"
assert first["verifiers"] == second["verifiers"], "числа разъехались между прогонами"
sys.exit(0)
PY

expect_exit 0 "негативы строятся от seed|среда|вид|task_id: порядок строк меняется, состав — нет" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import validate_verifiers as V

spec = V.ENVS[0]  # E1_define
pool = [{"task_id": "a", "gold_card": "первая\nвторая\nтретья\nчетвёртая"},
        {"task_id": "b", "gold_card": "другая\nкарточка\nсовсем\nиная"}]
task = pool[0]
a = V.negative_answers(spec, task, pool, task["gold_card"])
b = V.negative_answers(spec, task, pool, task["gold_card"])
assert a == b, "негативы не воспроизвелись при том же входе"
sh = a["shuffled_lines"]["answer"]
assert sorted(sh.split("\n")) == sorted(task["gold_card"].split("\n")), sh
assert sh != task["gold_card"], "перемешивание не изменило порядок строк"
assert a["other_task"]["answer"] == pool[1]["gold_card"], a["other_task"]
ref = task["gold_card"]
assert a["truncated_20pct"]["answer"] == ref[:max(1, int(len(ref) * 0.2))]
assert a["empty"]["answer"] == ""
# тот же вид негатива у другой задачи — другой ответ (от task_id, а не от порядка вызова)
c = V.negative_answers(spec, pool[1], pool, pool[1]["gold_card"])
assert c["shuffled_lines"]["answer"] != sh
sys.exit(0)
PY

expect_exit 0 "проекция score одна на все среды: gate=false → 0, словарь → среднее components" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import validate_verifiers as V
assert V.score_of(0.42) == 0.42
assert V.score_of({"gate": False, "components": {"a": 1.0}, "evidence": {}}) == 0.0
assert V.score_of({"gate": True, "components": {"a": 1.0, "b": 0.0}}) == 0.5
assert V.score_of({"gate": True, "components": {}}) == 0.0
try:
    V.score_of("нет")
except TypeError:
    pass
else:
    raise AssertionError("нечисловой ответ верификатора должен быть виден, а не равен нулю")
# порог берётся внутри зазора и не выдумывается при зазоре ≤ 0
assert V.propose_threshold(0.0, 1.0) == (0.5, 1.0)
assert V.propose_threshold(0.9, 0.4)[0] is None
assert V.propose_threshold(1.0, 1.0)[0] is None, "равные границы не разделяются порогом"
assert V.propose_threshold(0.62, 1.0)[0] == 0.8, V.propose_threshold(0.62, 1.0)
sys.exit(0)
PY

echo "  --- 15d. граница read-only и NOT-VERIFIED ---"
expect_exit 0 "каталог сред не изменился: sha256+mtime до/после, __pycache__ не появился" \
  python3 - <<'PY'
import hashlib, json, os, subprocess, sys
from pathlib import Path
tmp = Path(os.environ["TMP"])
root = tmp / "vv"
def snap():
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (hashlib.sha256(p.read_bytes()).hexdigest(),
                                             st.st_mtime_ns)
    return out
before = snap()
r = subprocess.run([sys.executable, "tools/validate_verifiers.py", "--envs-root", str(root),
                    "--env", "E1_define", "--env", "E6_contrast", "--n", "2",
                    "--no-evidence", "--probe-only", "--json"], capture_output=True, text=True)
assert r.returncode == 0, r.stderr[-500:]
after = snap()
assert before == after, f"каталог сред изменился: {sorted(set(before) ^ set(after))}"
assert not any(p.name == "__pycache__" for p in root.rglob("*")), "импорт написал байткод"
assert json.loads(r.stdout)["source"]["read_only"]["unchanged"] is True
sys.exit(0)
PY

expect_exit 2 "нет каталога сред → NOT-VERIFIED" \
  python3 tools/validate_verifiers.py --envs-root "$TMP/no-such-envs" --no-evidence
mkdir -p "$TMP/vv-partial/tasks" "$TMP/vv-partial/verifiers"
cp "$VV/tasks/contrast.jsonl" "$TMP/vv-partial/tasks/"
cp "$VV/verifiers/v_contrast.py" "$TMP/vv-partial/verifiers/"
expect_exit 2 "нет файла задач среды → NOT-VERIFIED" \
  python3 tools/validate_verifiers.py --envs-root "$TMP/vv-partial" --env E1_define --no-evidence
expect_exit 2 "неизвестное имя среды → NOT-VERIFIED" \
  python3 tools/validate_verifiers.py --envs-root "$VV" --env E9_nope --no-evidence
expect_exit 2 "--n 0 → NOT-VERIFIED" \
  python3 tools/validate_verifiers.py --envs-root "$VV" --env E6_contrast --n 0 --no-evidence

echo "  --- 15e. реальные среды: замер читает, evidence — про текущие байты ---"
expect_exit 0 "семь сред измерены, каталог сред не тронут, evidence привязан к sha256 верификаторов" \
  python3 - <<'PY'
import hashlib, json, os, subprocess, sys
from pathlib import Path
root = Path(os.path.expanduser("~/library/rl_envs"))
if not root.is_dir():
    print("нет каталога сред — проверка не выполняется")
    sys.exit(0)
def snap():
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".bak" not in p.name:
            st = p.stat()
            out[str(p.relative_to(root))] = (hashlib.sha256(p.read_bytes()).hexdigest(),
                                             st.st_mtime_ns)
    return out
before = snap()
r = subprocess.run([sys.executable, "tools/validate_verifiers.py", "--n", "3",
                    "--no-evidence", "--probe-only", "--json"], capture_output=True, text=True)
assert r.returncode == 0, (r.returncode, r.stderr[-800:])
rep = json.loads(r.stdout)
assert len(rep["verifiers"]) == 7, sorted(rep["verifiers"])
assert rep["summary"]["measured"] == 7 and rep["checks"]["all_envs_measured"] is True
assert rep["skipped"] and rep["skipped"][0]["env"] == "E8_plan_experiment", rep["skipped"]
after = snap()
assert before == after, f"каталог сред изменился: {sorted(set(before) ^ set(after))}"
assert rep["source"]["read_only"]["unchanged"] is True, rep["source"]["read_only"]
# evidence в кейсе обязан описывать ТЕКУЩИЕ байты верификаторов: правка
# верификатора (переработка по ADR-021) делает замер устаревшим, и это видно
ev = Path("evidence/verifier-validation.json")
assert ev.is_file(), "нет evidence/verifier-validation.json — проба не запускалась"
d = json.loads(ev.read_text(encoding="utf-8"))
assert len(d["verifiers"]) == 7, sorted(d["verifiers"])
for env, e in d["verifiers"].items():
    path = root / e["verifier"].split("::")[0]
    now = hashlib.sha256(path.read_bytes()).hexdigest()
    assert now == e["verifier_sha256"], f"{env}: evidence снят с других байтов верификатора"
    for key in ("n", "positive_min", "positive_median", "positive_max",
                "false_negatives_share", "negative_max", "proposed_threshold", "verdict"):
        assert key in e, f"{env}: нет поля {key}"
    assert e["verdict"] in ("binary-safe", "unsafe"), e["verdict"]
    assert len(e["negatives"]) == 4, sorted(e["negatives"])
assert d["summary"]["measured"] == 7
assert d["method"]["score_projection"]
excluded = {x["env"] for x in d["excluded"]}
assert excluded == {k for k, e in d["verifiers"].items() if e["verdict"] != "binary-safe"}
th = json.loads(Path("data/verifier-thresholds.json").read_text(encoding="utf-8"))
assert th["not_a_pipeline_config"] is True and th["status"] == "proposal"
assert "валидирован на" in th["caveat"], th["caveat"]
assert set(th["thresholds"]) == set(d["verifiers"]), th["thresholds"]
for env, e in d["verifiers"].items():
    if e["verdict"] == "binary-safe":
        assert th["thresholds"][env] == e["proposed_threshold"], env
    else:
        assert th["thresholds"][env] is None, f"{env}: порог выдуман без зазора"
sys.exit(0)
PY

echo
echo "== 15. ppl_probe.py (S3h: PPL-проба наборов GEN-EVAL v1/v2) =="
# Модуль обязан быть чистым от torch/transformers: разбор набора, агрегация и
# план проверяются без модели, и это же условие делает тесты дешёвыми.
expect_exit 0 "модуль импортируется без torch/transformers (проба не тянет GPU)" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import ppl_probe
assert "torch" not in sys.modules, "import ppl_probe потянул torch"
assert "transformers" not in sys.modules, "import ppl_probe потянул transformers"
assert ppl_probe.MIN_DOC_CHARS == 50 and ppl_probe.MIN_DOC_TOKENS == 8, "пороги разъехались с _ppl_eval"
sys.exit(0)
PY
expect_exit 0 "разбор набора: разделитель, strip и фильтр len>50 — как в _ppl_eval" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
from ppl_probe import split_docs
# 51 символ проходит, ровно 50 — нет; фильтр применяется к обрезанной строке,
# поэтому документ из 50 символов + пробелы по краям отсекается.
d51, d50 = "я" * 51, "я" * 50
text = "\n---\n".join([d51, d50, "  " + d51 + "  ", "я" * 40])
assert split_docs(text) == [d51, d51], split_docs(text)
# Разделитель — ровно "\n---\n": подчёркивание заголовка и "---" без переводов
# строки документ не режут (иначе длинные наборы распались бы по заголовкам).
assert split_docs("a\n---\nb") == [], "короче порога — пусто"
long_doc = "x" * 60
assert len(split_docs(long_doc + "\n---\n" + long_doc)) == 2
assert len(split_docs(long_doc + "\n----\n" + long_doc)) == 1, "'----' не разделитель"
assert len(split_docs(long_doc + "---\n" + long_doc)) == 1, "'---' без \\n не разделитель"
assert split_docs("  " + long_doc + "  ")[0] == long_doc, "документ не обрезан"
sys.exit(0)
PY
expect_exit 0 "обрезка и порог доживания: [:max_len], затем len(enc)>8" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
from ppl_probe import is_counted, predicted_tokens, truncate_ids, MIN_DOC_TOKENS
assert truncate_ids(list(range(2000)), 1024) == list(range(1024)), "обрезка не до max_len"
assert truncate_ids([1, 2, 3], 1024) == [1, 2, 3], "короткий документ обрезан зря"
assert truncate_ids([], 10) == []
# Порядок как в пайплайне: сначала обрезка, потом проверка — документ в 2000
# токенов доживает (1024>8), документ в 8 токенов — нет.
assert is_counted(truncate_ids(list(range(2000)), 1024)) is True
assert is_counted([0] * MIN_DOC_TOKENS) is False, "ровно 8 токенов обязаны отсекаться"
assert is_counted([0] * (MIN_DOC_TOKENS + 1)) is True
# Сдвиг «предсказание против следующего» убирает последний токен: счётчик = len-1.
assert [predicted_tokens(n) for n in (0, 1, 2, 1024)] == [0, 0, 1, 1023]
sys.exit(0)
PY
expect_exit 0 "батчи по 4 и агрегация: PPL корпуса взвешена по токенам, не по документам" \
  python3 - <<'PY'
import math, sys
sys.path.insert(0, "tools")
from ppl_probe import batch_chunks, corpus_ppl, doc_ppl_stats
assert [len(c) for c in batch_chunks(list(range(10)), 4)] == [4, 4, 2]
assert list(batch_chunks([], 4)) == []
# Ключевое свойство: корпусная PPL — exp(Σnll/Σтокенов), а не среднее PPL по
# документам. Берём документы разной длины, чтобы величины разошлись заметно.
nll, tok = [10.0, 10.0], [1000, 1]
corpus = corpus_ppl(nll, tok)
assert abs(corpus - math.exp(20 / 1001)) < 1e-12, corpus
mean_of_doc_ppl = sum(math.exp(n / t) for n, t in zip(nll, tok)) / 2
assert corpus < 1.1 < mean_of_doc_ppl, (corpus, mean_of_doc_ppl)
st = doc_ppl_stats(nll, tok)
assert st["n"] == 2 and st["min"] < st["median"] < st["max"], st
assert abs(st["min"] - math.exp(10 / 1000)) < 1e-12
assert abs(st["max"] - math.exp(10.0)) < 1e-6
assert st["max_over_median"] > 1
# Пустой вход — не деление на ноль, а явная пустота.
empty = doc_ppl_stats([], [])
assert empty["n"] == 0 and empty["median"] is None, empty
assert corpus_ppl([], []) == 1.0, "пустой корпус: exp(0/1)"
sys.exit(0)
PY
expect_exit 2 "отсутствие входа — NOT-VERIFIED (exit 2), а не молчаливый ноль" \
  python3 tools/ppl_probe.py --sets v1_general,нетакого_набора
expect_contains "NOT-VERIFIED" "неизвестный набор назван вслух" \
  python3 tools/ppl_probe.py --sets v1_general,нетакого_набора
expect_exit 0 "locate: отсутствие кандидатов — причина в списке, а не исключение" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
from ppl_probe import locate
path, missed = locate(["/nope/one", "/nope/two"], "model.safetensors")
assert path is None and len(missed) == 2, (path, missed)
assert all("model.safetensors" in m for m in missed), missed
sys.exit(0)
PY
expect_exit 0 "траектория лосса CPT: строки без loss и битые считаются раздельно" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
from ppl_probe import parse_probe_losses
text = "\n".join([
    '{"event": "start", "stage": "cpt"}',   # событие обвязки — без loss
    '{"loss": 2.0989}',
    '{"loss": 2.630323}',
    'не json вовсе',                          # битая строка
    "",
    '{"loss": 4.66028}',
])
p = parse_probe_losses(text)
assert p["losses"] == [2.0989, 2.630323, 4.66028], p["losses"]
# Три исхода различимы: «нет шагов» и «файл не разобрался» — разные диагнозы.
assert p["skipped_without_loss"] == 1, p
assert p["unparsed"] == 1, p
empty = parse_probe_losses("")
assert empty == {"losses": [], "skipped_without_loss": 0, "unparsed": 0}, empty
sys.exit(0)
PY
expect_exit 0 "сводка и доля выхода за уровень: пусто ≠ ноль, одна точка ≠ std 0.0 молча" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
from ppl_probe import above_level, loss_stats
assert loss_stats([])["n"] == 0 and loss_stats([])["mean"] is None
one = loss_stats([2.0])
assert one["n"] == 1 and one["stdev"] == 0.0 and one["first"] == one["last"] == 2.0
st = loss_stats([2.0, 4.0])
assert abs(st["mean"] - 3.0) < 1e-12 and st["min"] == 2.0 and st["max"] == 4.0
# Граница включается (>=): уровень — это «дошло до», а не «перевалило».
a = above_level([2.5, 2.6, 2.61], 2.6)
assert a["count"] == 2 and a["n"] == 3 and abs(a["fraction"] - 2 / 3) < 1e-12, a
assert above_level([], 2.6)["fraction"] is None, "пустой вход — не доля 0"
sys.exit(0)
PY
expect_exit 0 "sha256_file потоком совпадает с hashlib и first_existing различает отказы" \
  python3 - <<'PY'
import hashlib, sys, tempfile
from pathlib import Path
sys.path.insert(0, "tools")
from ppl_probe import first_existing, sha256_file
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "f.bin"
    p.write_bytes(b"x" * 100000)
    assert sha256_file(p, chunk=4096) == hashlib.sha256(b"x" * 100000).hexdigest()
    # Каталог — не файл: кандидат обязан быть файлом, иначе DirectoryPath
    # прошёл бы как «нашлось».
    got, missed = first_existing([td, str(p)])
    assert got == p and len(missed) == 1, (got, missed)
    got, missed = first_existing(["/nope/one"])
    assert got is None and missed == ["/nope/one"], (got, missed)
sys.exit(0)
PY
expect_exit 0 "план печатается без модели и называет наборы, sha256 и методику" \
  python3 tools/ppl_probe.py --plan
expect_contains "v2_domain" "в плане назван набор v2" python3 tools/ppl_probe.py --plan
expect_contains "sha256=" "в плане есть хеш набора (привязка числа к версии файла)" \
  python3 tools/ppl_probe.py --plan
expect_contains "exp(Σnll/Σтокенов)" "в плане названа формула пайплайна" \
  python3 tools/ppl_probe.py --plan
expect_exit 0 "evidence S3h на месте, разбирается и сходится с файлами наборов" \
  python3 - <<'PY'
import hashlib, json, sys
from pathlib import Path
d = json.loads(Path("evidence/ppl-baseline-v1v2.json").read_text(encoding="utf-8"))
assert d["status"] == "complete", d["status"]
assert d["stage"] == "S3h"
for key in ("v1_general", "v1_domain", "v2_general", "v2_domain"):
    e = d["ppl"][key]
    for field in ("ppl", "docs", "tokens", "doc_ppl_median", "doc_ppl_mean",
                  "doc_ppl_min", "doc_ppl_max", "max_len", "batch", "sha256",
                  "dataset", "model", "seconds"):
        assert field in e, f"{key}: нет поля {field}"
    assert e["ppl"] > 1.0 and e["tokens"] > 0 and e["docs"] > 0, e
    assert e["doc_ppl_min"] <= e["doc_ppl_median"] <= e["doc_ppl_max"], key
    assert e["max_len"] == 1024 and e["batch"] == 4, (key, e["max_len"], e["batch"])
    # Хеш набора обязан совпадать с файлом: иначе число не привязано к версии,
    # а наборы v2 пересобираются (ADR-018).
    raw = Path(e["dataset"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == e["sha256"], f"{key}: хеш разошёлся с файлом"
    # Сходимость разбора: измеренные + отсеянные по токенам = документы файла.
    in_file = len([x for x in raw.decode().split("\n---\n") if len(x.strip()) > 50])
    assert e["docs_in_file"] == in_file, (key, e["docs_in_file"], in_file)
    assert e["docs"] + e["docs_dropped_short_tokens"] == in_file, key
    # Самопроверки прибора: маска строки = len(enc)-1, батч = одиночный forward.
    assert e["mask_check"] is True, key
    assert abs(e["cross_check"]["rel_delta"]) < 0.02, (key, e["cross_check"])
sys.exit(0)
PY
expect_exit 0 "evidence несёт вердикт воспроизведения и сдвиг v1→v2 числами" \
  python3 - <<'PY'
import json, sys
from pathlib import Path
d = json.loads(Path("evidence/ppl-baseline-v1v2.json").read_text(encoding="utf-8"))
r = d["reproduction"]
assert r["verdict"] in ("СОВПАЛО", "ЯКОРЬ НЕ БАЗОВАЯ МОДЕЛЬ", "РАСХОЖДЕНИЕ БЕЗ ОБЪЯСНЕНИЯ"), r
assert r["known"]["ppl_general"] == 127.50231470270732, r["known"]
assert r["known"]["ppl_domain"] == 11.75640733743271, r["known"]
assert "s2-smoke.json" in r["known"]["source"], r["known"]
assert r["anchor_provenance"]["value"] == "127.5 / 11.76"
for name, key in (("v1_general", "ppl_general"), ("v1_domain", "ppl_domain")):
    c = r["checks"][name]
    assert abs(c["delta_pct"] - (c["measured"] / c["known"] - 1) * 100) < 1e-9, c
    assert abs(c["known"] - r["known"][key]) < 1e-12, c
# Вердикт «якорь не базовая модель» обязан опираться на независимый якорь
# стенда (лосс при lr=0), а не на наше же число.
if r["verdict"] == "ЯКОРЬ НЕ БАЗОВАЯ МОДЕЛЬ":
    assert r["base_anchor_check"]["anchor_agrees_with_measurement"] is True
    assert r["base_anchor"]["loss"] > 0 and r["base_anchor"]["source"].endswith("cpt.log")
    assert "Loaded CPT ckpt" in r["anchor_provenance"]["preceding_line"]
    # Сверка на ОДНОМ корпусе обязана быть и обязана согласиться: если лосс
    # стенда при lr=0 не попадает в разброс нашей базы, значит разошлись
    # модели/методика, и вердикт «якорь не базовая модель» был бы самоуспокоением.
    ca = r["corpus_anchor"]
    assert ca["pack_sha256"] and ca["n_chunks"] > 0, ca
    assert ca["base_loss"]["stdev"] > 0, ca
    assert ca["stand_step0_within_base_range"] is True, ca
    assert r["same_corpus_check"]["agrees"] is True, r["same_corpus_check"]
    # Арифметика нарезки lm_head обязана совпадать с лоссом transformers —
    # иначе весь якорь на корпусе посчитан другой формулой.
    assert ca["control"]["abs_delta"] < 1e-4, ca["control"]
    # Разброс базы обязан быть на порядок уже наблюдаемой полосы CPT: иначе
    # «полоса 2.1…5.7» объяснялась бы трудностью пачек, а не расхождением.
    assert ca["base_loss"]["max"] < r["divergence"]["trajectory"]["max"], (ca, r["divergence"])
    assert r["divergence"]["status"] == "OK", r["divergence"]
    assert r["divergence"]["step0_matches_log"] is True, r["divergence"]
    assert r["divergence"]["verdict"] == "ПРОГОН РАЗОШЁЛСЯ", r["divergence"]
    assert r["divergence"]["steps_above_base_max"]["count"] > 0, r["divergence"]
sh = d["v1_vs_v2_shift"]
assert sh["general"]["docs_v1"] == 24 and sh["general"]["docs_v2"] == 200, sh["general"]
assert sh["domain"]["docs_v1"] == 5 and sh["domain"]["docs_v2"] == 200, sh["domain"]
assert abs(sh["general"]["ratio_v2_over_v1"] - sh["general"]["v2"] / sh["general"]["v1"]) < 1e-9
assert d["stand"]["note"].startswith("локальная машина"), "прибор не выдаёт 4080 за стенд"
sys.exit(0)
PY

echo
echo "──────────────────────────────────────────────"
echo "итого: PASS=$PASS FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  for f in "${failures[@]}"; do echo "  - $f"; done
  exit 1
fi
echo "TOOL TESTS OK"
exit 0
