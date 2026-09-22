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
# SKIP считаются отдельно: пропуск обязан быть назван (нет docker — нет пробы G4)
SKIP=0
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

# expect_absent <строка> <описание> <команда...> — негативная сторона
# expect_contains: проверяется, что утверждения в выводе НЕТ. Нужна там, где
# дефект — лишнее срабатывание (например, «ВНИМАНИЕ» от платформенного сервиса),
# а не отсутствие нужной строки.
expect_absent() {
  local needle="$1" what="$2"; shift 2
  local out
  out="$("$@" 2>&1)"
  if printf '%s' "$out" | grep -qF -- "$needle"; then
    FAIL=$((FAIL + 1)); failures+=("$what: в выводе есть «$needle»")
    printf '  FAIL %-58s (есть «%s»)\n' "$what" "$needle"
    printf '%s\n' "$out" | sed 's/^/       | /' | head -6
  else
    PASS=$((PASS + 1)); printf '  ok   %-58s (нет «%s»)\n' "$what" "$needle"
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
# S3ao: прогон не записал, какой редакцией прибора снят, и провенанс числа остался
# на коммите — отсюда класс «цитата против дерева». Теперь прибор пиннится хешем.
expect_exit 0 "манифест с приборами прогона (--instrument)" \
  python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/smoke-4" \
    --dataset datasets/eval_ood_clean.jsonl --base-model Qwen/Qwen2.5-0.5B \
    --pipeline laguna_pipeline_v8.py --seed 42 --stages probe=done \
    --instrument tools/ppl_probe.py --instrument tools/calib_ppl_probe.py \
    --relative-to "$CASE_ROOT"
expect_exit 0 "хеши приборов в манифесте — хеши файлов, ключ — путь прибора" \
  python3 - "$TMP/runsgen/smoke-4/run_manifest.json" <<'PY'
import hashlib, json, pathlib, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
ins = d["instruments"]
assert set(ins) == {"tools/ppl_probe.py", "tools/calib_ppl_probe.py"}, ins
for path, rec in ins.items():
    assert rec["path"] == path, rec
    assert rec["sha256"] == hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest(), path
# Без --instrument поля нет: прибор не угадывается (у прогонов CPT/SFT его может
# не быть вовсе), но и молчания нет — причина печатается предупреждением в stderr.
assert "instruments" not in json.load(
    open(sys.argv[1].replace("smoke-4", "smoke-2"), encoding="utf-8")), \
    "без флага манифест не должен обзаводиться полем"
sys.exit(0)
PY
expect_exit 0 "манифест с приборами проходит стража C-012" \
  python3 tools/check_run_manifest.py --runs "$TMP/runsgen"
expect_contains "уже актуален" "повтор с теми же приборами не перезатирает манифест" \
  python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/smoke-4" \
    --dataset datasets/eval_ood_clean.jsonl --base-model Qwen/Qwen2.5-0.5B \
    --pipeline laguna_pipeline_v8.py --seed 42 --stages probe=done \
    --instrument tools/ppl_probe.py --instrument tools/calib_ppl_probe.py \
    --relative-to "$CASE_ROOT"
expect_exit 2 "--instrument с несуществующим прибором → NOT-VERIFIED" \
  python3 tools/write_run_manifest.py --run-dir "$TMP/runsgen/x" \
    --dataset datasets/eval_ood_clean.jsonl --base-model m \
    --pipeline laguna_pipeline_v8.py --seed 1 --stages cpt \
    --instrument tools/no_such_probe.py --relative-to "$CASE_ROOT"
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
# ADR-054 п.1: набором стадии решением выбран v2 — план и манифест разведки обязаны
# называть ИМЕННО его. v1 остаётся историческим носителем и здесь не ожидается.
assert m["dataset_path"].endswith("datasets/rl_tasks_revpool_v2.jsonl"), m["dataset_path"]
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
# ADR-054 п.1–3: имя версии в плане — v2 (набор стадии), а не v1 (исторический
# носитель без карточки AD-2). Проверяются ОБЕ стороны: v2 назван, v1 — нет.
expect_contains "rl_tasks_revpool_v2.jsonl" "в плане назван набор курикулума с версионным именем" \
  python3 tools/run_rl_probe.py --plan --ts 20260101-0000
expect_exit 0 "в плане разведки нет v1: набор стадии переключён, а не «оба сразу»" \
  python3 - <<'PY'
import subprocess, sys
out = subprocess.run([sys.executable, "tools/run_rl_probe.py", "--plan",
                      "--ts", "20260101-0000"], capture_output=True, text=True).stdout
assert "rl_tasks_revpool_v2.jsonl" in out, out
assert "rl_tasks_revpool_v1.jsonl" not in out, \
    "план всё ещё называет исторический носитель набором стадии"
PY
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
  reward_degenerate)
    # Синтетический ряд вырожденной награды: три класса действия `stop` сразу —
    # мёртвая награда (pass=0, reward=0), доля нулевой награды 100 %, нулевая
    # дисперсия по группе (adv≠0=0 %). Проверяется не «детектор видит ноль», а
    # «стадия ОСТАНОВЛЕНА и пауза поставлена» — то, чего до ADR-017 в цепочке не было.
    for s in 10 20 30 40 50 60; do
      echo "RL step $s/500 | reward=0.000 | pass=0% | turns=1 | len=10t | kl=0.0001 | entropy=1.50 | clip=0.00 | adv=0.000 | adv≠0=0% | active=0/4 | mem=80.0GB"
    done
    wait_or_stop 60; exit 137 ;;
  reward_ok)
    # Здоровый ряд: контроль ложного срабатывания. Числа — порядок разведки
    # S3-pre (pass_rate 0.3887, reward_mean 0.3782, zero_reward 51 %).
    for s in 10 20 30 40 50 60; do
      echo "RL step $s/500 | reward=0.378 | pass=39% | turns=3 | len=400t | kl=0.0100 | entropy=1.60 | clip=0.05 | adv=0.210 | adv≠0=62% | active=3/4 | mem=80.0GB"
    done
    #: Стадия обязана прожить дольше одного опроса стража: страж опрашивает лог
    #: раз в `--poll-seconds`, и стадия, кончившаяся за миллисекунды, не дала бы
    #: ему ни одного куска — тест проверял бы не критерий, а гонку.
    wait_or_stop 6
    echo "ckpt" > "$FAKE_RUN_DIR/checkpoints/rl_checkpoint_final.pt"
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

echo "  --- 14c2. вырожденная награда RL (ADR-017): класс, порог, остановка ---"
#: До этой секции у критерия не было ни одного зуба: ADR-017 был принят, а в
#: цепочке жили только энтропия, NVRM, память и молчание — награда могла быть
#: мертва при честно отработанных 500 шагах. Проверяются обе стороны: вырожденный
#: ряд обязан остановить стадию, здоровый — не тронуть (иначе «страж», краснеющий
#: на здоровом прогоне, обесценивает и себя, и решение).
mkstages_rl() {  # <файл> — одна стадия rl (артефакт — финальный чекпойнт RL)
  printf 'rl\trl\trl\tcheckpoints/rl_checkpoint_final.pt\t500\t2\t-\tpilot-compact-s42-rl-20260101-0000\n' > "$1"
}
CHAIN_DIR="$CHAIN_TMP/m"; mkdir -p "$CHAIN_DIR"; mkstages_rl "$CHAIN_DIR/stages.tsv"
expect_exit 3 "вырожденный ряд награды → стоп и пауза (ADR-017 п.2)" \
  test_chain "$CHAIN_DIR" reward_degenerate "$CHAIN_TMP/guard_ok.sh" \
      --degeneracy-tool tools/rl_degeneracy.py
expect_contains "вырожденная награда RL (ADR-017)" "причина паузы — вырожденная награда, а не «тихий» конец" \
  cat "$CHAIN_DIR/var/STOPPED"
expect_contains "dead_reward" "класс вырождения назван в маркере паузы" \
  cat "$CHAIN_DIR/var/STOPPED"
expect_contains "no_group_variance" "назван и второй класс (нулевая дисперсия по группе)" \
  cat "$CHAIN_DIR/var/STOPPED"
expect_exit 0 "наблюдения награды сохранены: вердикт воспроизводим после стадии" \
  python3 - "$CHAIN_DIR/var/rl_reward_points.jsonl" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
assert len(rows) == 6, len(rows)
assert rows[-1]["step"] == 60 and rows[-1]["pass_rate"] == 0.0, rows[-1]
assert rows[-1]["adv_nonzero"] == 0.0, rows[-1]
PY
#: Живая точка замера видит не всё, и это **названо**: в строке шага нет доли
#: нулевой награды — она считается по траекториям. Класс, который по этому входу
#: не измерим, обязан стоять в `not_evaluated` с причиной, а не молчать: иначе
#: «не проверяли» читалось бы как «чисто».
expect_exit 0 "вердикт прибора записан машинно (var/degenerate_verdict.json)" \
  python3 - "$CHAIN_DIR/var/degenerate_verdict.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1], encoding="utf-8"))["report"]
assert r["verdict"] == "degenerate_reward", r["verdict"]
assert r["action"] == "stop", r["action"]
assert r["window_complete"] is True, r["window_complete"]
assert set(r["stop_classes"]) == {"dead_reward", "no_group_variance"}, r["stop_classes"]
ne = {n["class"]: n["why"] for n in r["not_evaluated"]}
assert "zero_reward_dominated" in ne, ne
assert "rollouts_log" in ne["zero_reward_dominated"], ne["zero_reward_dominated"]
assert "template_collapse" in ne, ne
PY

CHAIN_DIR="$CHAIN_TMP/n"; mkdir -p "$CHAIN_DIR"; mkstages_rl "$CHAIN_DIR/stages.tsv"
expect_exit 0 "здоровый ряд награды стадию НЕ роняет (контроль ложного срабатывания)" \
  test_chain "$CHAIN_DIR" reward_ok "$CHAIN_TMP/guard_ok.sh" \
      --degeneracy-tool tools/rl_degeneracy.py
test ! -f "$CHAIN_DIR/var/STOPPED" \
  && { PASS=$((PASS+1)); echo "  ok   пауза не поставлена: награда живая"; } \
  || { FAIL=$((FAIL+1)); failures+=("пауза поставлена на здоровом ряде награды"); echo "  FAIL пауза поставлена зря"; }
expect_contains "вырождения не найдено" "вердикт «чисто» назван в логе, а не подразумевается" \
  cat "$CHAIN_DIR/chain.out"

#: Прибор недоступен — это не «чисто»: молчаливый пропуск критерия вернул бы ровно
#: тот дефект, ради которого он заводится (награда мертва, а стадия отработала).
CHAIN_DIR="$CHAIN_TMP/o"; mkdir -p "$CHAIN_DIR"; mkstages_rl "$CHAIN_DIR/stages.tsv"
expect_exit 0 "нет прибора → стадия идёт, но пропуск критерия НАЗВАН" \
  test_chain "$CHAIN_DIR" reward_ok "$CHAIN_TMP/guard_ok.sh" \
      --degeneracy-tool "$CHAIN_TMP/нет-такого-прибора.py"
expect_contains "критерий ADR-017 не оценён" "пропуск критерия назван в логе цепочки" \
  cat "$CHAIN_DIR/chain.out"

echo "  --- 14c3. прибор критерия: вторая точка замера и зуб на синтетике ---"
# Вторая точка замера — артефакты стадии (`--run-dir`). Она видит то, чего нет в
# строке лога: долю нулевой награды (по траекториям) и повторы в текстах. Без неё
# класс `zero_reward_dominated` не измерялся бы нигде.
DEGEN_TMP="$CHAIN_TMP/degen"; mkdir -p "$DEGEN_TMP/dead/logs" "$DEGEN_TMP/live/logs"

expect_exit 0 "контроль синтетикой: здоровый ряд не краснеет, вырожденный краснеет" \
  python3 tools/rl_degeneracy.py --self-test

expect_exit 2 "нет каталога стадии → NOT-VERIFIED (не «чисто»)" \
  python3 tools/rl_degeneracy.py --run-dir "$CHAIN_TMP/нет-такого-каталога"

#: Вырожденный итог: награда нулевая на всех траекториях, преимущества обнулены,
#: шаблон повторяется. Три класса сразу — в том числе `zero_reward_dominated`,
#: который живой точкой не измеряется по построению.
python3 - "$DEGEN_TMP/dead" <<'PY'
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
(d / "logs" / "rl_metrics.json").write_text(json.dumps({
    "rewards": [0.0] * 6, "pass_rates": [0.0] * 6, "lengths": [10] * 6,
    "entropy": [1.5] * 6, "clip_frac": [0.0] * 6}), encoding="utf-8")
loop = "альфа бета гамма дельта " * 12
rows = [{"step": s, "reward": 0.0, "verifier_passed": False, "hit_timeout": False,
         "text": loop} for s in range(6) for _ in range(8)]
(d / "rollouts_log.jsonl").write_text(
    "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
PY
expect_exit 1 "вырожденный итог стадии → exit 1 (стоп-класс сработал)" \
  python3 tools/rl_degeneracy.py --run-dir "$DEGEN_TMP/dead" --final
expect_exit 0 "вторая точка замера видит долю нулевой награды (rollouts_log)" \
  python3 - "$DEGEN_TMP/dead" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("rld", "tools/rl_degeneracy.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
points, notes = m.points_from_run(__import__("pathlib").Path(sys.argv[1]))
rep = m.evaluate(points, final=True)
assert rep["verdict"] == "degenerate_reward", rep["verdict"]
assert "zero_reward_dominated" in rep["stop_classes"], rep["stop_classes"]
assert "template_collapse" in rep["warning_classes"], rep["warning_classes"]
assert notes["rollouts"]["trajectories"] == 48, notes["rollouts"]
PY

#: Здоровый итог: числа — порядок разведки S3-pre (pass 0.39, zero_reward 51 %).
#: Порог ADR-017 (90 %) заведомо не достигнут — контроль ложного срабатывания.
python3 - "$DEGEN_TMP/live" <<'PY'
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
(d / "logs" / "rl_metrics.json").write_text(json.dumps({
    "rewards": [0.378] * 6, "pass_rates": [0.389] * 6, "lengths": [400] * 6,
    "entropy": [1.6] * 6, "clip_frac": [0.05] * 6}), encoding="utf-8")
rows = [{"step": s, "reward": (1.0 if g < 3 else 0.0), "verifier_passed": g < 3,
         "hit_timeout": False, "text": "разные слова в этом ответе без повторов"}
        for s in range(6) for g in range(8)]
(d / "rollouts_log.jsonl").write_text(
    "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
PY
expect_exit 0 "здоровый итог стадии → exit 0 (ложного красного нет)" \
  python3 tools/rl_degeneracy.py --run-dir "$DEGEN_TMP/live" --final

expect_contains "dead_reward" "порог класса назван вместе с источником, а не только числом" \
  python3 tools/rl_degeneracy.py --run-dir "$DEGEN_TMP/dead" --final

#: Режим стража: строки лога со stdin → jsonl наблюдений, в stdout — максимальный шаг.
expect_exit 0 "разбор строки пайплайна: наблюдение собрано из полей шага" \
  python3 - "$DEGEN_TMP/extract.jsonl" <<'PY'
import importlib.util, json, pathlib, subprocess, sys
out = pathlib.Path(sys.argv[1])
line = ("RL step 50/500 | reward=0.000 | pass=0% | turns=1 | len=10t | kl=0.0001 | "
        "entropy=1.50 | clip=0.00 | adv=0.000 | adv≠0=0% | active=0/4 | mem=80.0GB\n")
p = subprocess.run([sys.executable, "tools/rl_degeneracy.py", "--extract",
                    "--points-file", str(out)], input=line, text=True,
                   capture_output=True, check=True)
assert p.stdout.strip() == "50", p.stdout
row = json.loads(out.read_text(encoding="utf-8").strip())
assert row == {"step": 50, "reward_mean": 0.0, "pass_rate": 0.0, "clip_frac": 0.0,
               "adv_nonzero": 0.0}, row
PY

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

# Карточка пула — карточка ФАКТА, а не плана (S3p): тест пересчитывает файл сам и
# сверяет с карточкой построчно, поэтому пересборка пула без обновления карточки
# красит тест (и по хешу, и по раскладке), а обновление карточки без пересборки —
# нет. Числа в тесте не зашиты: зашитое число ловило бы честную пересборку.
expect_exit 0 "пул v2 в кейсе совпадает с карточкой: хеш, объём и раскладка сверены пересчётом файла" \
  python3 - <<'PY'
import collections, datetime, hashlib, json, sys
sys.path.insert(0, "tools")
from pathlib import Path
import build_rev_envs as B
pool, card = Path("datasets/rl_tasks_revpool_v2.jsonl"), Path("data/rev-envs-v2-card.json")
if not pool.is_file() or not card.is_file():
    print("SKIP: пула v2 или карточки нет — нечего сверять")
    sys.exit(0)
c = json.loads(card.read_text(encoding="utf-8"))

n, by_type, by_env, h = 0, collections.Counter(), collections.Counter(), hashlib.sha256()
with pool.open("rb") as f:                       # хеш по байтам файла, не по строкам
    while chunk := f.read(1 << 20):
        h.update(chunk)
with pool.open(encoding="utf-8") as f:
    for line in f:
        t = json.loads(line)
        n += 1
        by_type[t["task_type"]] += 1
        by_env[t["source_env"]] += 1

# 1. Артефакт и его карточка — одно и то же: хеш, объём, раскладка по типам и средам.
assert c["pool"]["sha256"] == h.hexdigest() == B.sha256_file(pool), "sha256 карточки != хеш пула"
assert c["pool"]["lines"] == n, (c["pool"]["lines"], n)
assert c["counts"]["kept"] == n, (c["counts"]["kept"], n)
assert c["counts"]["by_task_type"] == dict(by_type), (c["counts"]["by_task_type"], dict(by_type))
assert c["counts"]["by_env_in_pool"] == dict(by_env), (c["counts"]["by_env_in_pool"], dict(by_env))
assert c["filters"]["duplicates"]["remaining"] == n, c["filters"]["duplicates"]
# Раскладка по средам обязана сойтись с объёмом: карточка не «две правды».
kept = sum(e["kept_in_pool"] for e in c["envs"].values() if "kept_in_pool" in e)
assert kept == n, (kept, n)
assert set(c["counts"]["by_task_type"]) <= set(B.VERIFIER_TASK_TYPES), c["counts"]["by_task_type"]
assert c["sources_unchanged"] is True
for env in ("E1_define", "E4_extract", "E6_contrast", "E7_repair", "E8_plan_experiment"):
    assert c["envs"][env]["excluded_reason"], env

# 2. Факт назван: дата пересборки, коммит пересборки, статус лейк-фикса (S3p, ADR-021).
datetime.datetime.fromisoformat(c["pool"]["rebuilt_at"])
commit = c["pool"]["rebuilt_by_commit"]
assert len(commit) == 40 and all(ch in "0123456789abcdef" for ch in commit), commit
assert c["pool"]["s3f_fix_status"] in ("applied", "not_applied", "partial"), c["pool"]

# 3. Лейк-инвариант в форме гейта A стража C-009: проверяемая строка slug-ветки не
# лежит в промпте буквально — иначе ответ копируется из условия (S3f-fix).
leaks = []
for i, line in enumerate(pool.open(encoding="utf-8"), 1):
    t = json.loads(line)
    if t["task_type"] in B.SLUG_TASK_TYPES:
        prompt = t["prompt"].lower()
        hit = [s for s in t.get("expected_slugs", []) if s.lower() in prompt]
        if hit:
            leaks.append((i, t["source_task_id"], hit))
assert not leaks, f"проверяемая строка в промпте у {len(leaks)} задач, например {leaks[:3]}"

print(f"пул v2: {n} задач, sha256 {c['pool']['sha256'][:12]}…, "
      f"типы {dict(by_type)}, пересобран {c['pool']['rebuilt_at']} "
      f"({commit[:12]}, S3f-fix {c['pool']['s3f_fix_status']})")
sys.exit(0)
PY

# Сверка «источник = пул + поимённо снятые» (S3p): карточка и отчёт пересборки
# обязаны объяснять каждую задачу, которой нет в пуле. Ловит пересборку, при
# которой файл, карточка и отчёт разъезжаются молча.
expect_exit 0 "снятые из пула задачи названы поимённо: источник = пул + отчёт (env_types и E5)" \
  python3 - <<'PY'
import collections, json, sys
from pathlib import Path

ENV_SRC = Path("/home/user/gb10-shared/datasets/rl_tasks_env_types.jsonl")
E5_SRC = Path("/home/user/library/rl_envs/tasks/relate.jsonl")
pool = Path("datasets/rl_tasks_revpool_v2.jsonl")
report = Path("runs/rev-pool-v2/exclusions.json")
if not pool.is_file() or not report.is_file():
    print("SKIP: пула v2 или отчёта пересборки нет")
    sys.exit(0)

pids = collections.defaultdict(set)
for line in pool.open(encoding="utf-8"):
    t = json.loads(line)
    pids[t["source_env"]].add(t["source_task_id"])
rep = json.loads(report.read_text(encoding="utf-8"))
named = collections.defaultdict(set)
for e in rep["excluded"]:
    named[e["env"]].add(e["task_id"])

for env, src, key in (("env_types", ENV_SRC, "line"), ("E5_relate", E5_SRC, "task_id")):
    if not src.is_file():
        print(f"SKIP: источника {env} нет — сверка неполная (NOT-VERIFIED)")
        continue
    total, ids = 0, []
    for i, line in enumerate(src.open(encoding="utf-8"), 1):
        line = line.strip()
        if not line:
            continue
        t = json.loads(line)
        total += 1
        ids.append(f"{key}:{i}" if key == "line" else t.get("task_id"))
    assert set(pids[env]) <= set(ids), f"{env}: в пуле задачи не из источника"
    assert named[env] <= set(ids), f"{env}: отчёт снимает задачу не из источника"
    dropped = total - len(pids[env])
    assert dropped == len(named[env]), (env, dropped, len(named[env]))
    print(f"  {env}: источник {total}, в пуле {len(pids[env])}, снято поимённо "
          f"{len(named[env])}, неучтённых {len(set(ids) - pids[env] - named[env])}")
    if env == "env_types":
        # Идентификаторы уникальны (line:N) — снятые сверяются множеством, не числом.
        assert set(ids) - pids[env] == named[env], sorted(named[env] ^ (set(ids) - pids[env]))
    else:
        # У E5 дубли gold делят task_id с близнецом, оставшимся в пуле: снятый
        # дубль по id неотличим от оставшегося, поэтому сверяется объём, а
        # поимённость — вложенностью (обе проверки выше).
        assert len(ids) > len(set(ids)), "у E5 ожидались дубли gold с общим task_id"

# Каждая причина снятия названа, и лейк-причины — с проверяемой строкой в тексте.
for e in rep["excluded"]:
    assert e.get("reason_code") and e.get("reason"), e
    if e["reason_code"] == "prompt_leak":
        assert "промпт" in e["reason"], e
sys.exit(0)
PY

# Проба лейка (S3p) — тот же инвариант, но судьёй: ответ = сам промпт через
# замороженный verify_task. Зелёный и красный пути пробы + NOT-VERIFIED на
# отсутствующем входе; проверка 2 без пайплайна/индекса обязана быть именованной.
python3 - "$TMP" <<'PY'
import json, sys
from pathlib import Path
F = Path(sys.argv[1]) / "leakprobe"; F.mkdir(parents=True, exist_ok=True)
(F / "clean.jsonl").write_text(json.dumps({
    "task_type": "find_concept", "prompt": "Запиши формулу 3D Latent Alignment Loss.",
    "verifier": "multi_slug_match", "reward": 1.0, "source_env": "E2_formula",
    "source_task_id": "E2_a", "expected_slugs": ["3d_latent_alignment_loss"]}) + "\n", encoding="utf-8")
(F / "leaked.jsonl").write_text(json.dumps({
    "task_type": "chain_reasoning", "prompt": "Начало: 'alpha_x'. Конец: 'beta_y'.",
    "verifier": "multi_slug_match", "reward": 1.0, "source_env": "env_types",
    "source_task_id": "line:9", "expected_slugs": ["alpha_x", "beta_y"]}) + "\n", encoding="utf-8")
(F / "keyword.jsonl").write_text(json.dumps({
    "task_type": "explain_relation", "prompt": "Как связаны alpha_x и beta_y?",
    "verifier": "keyword_match", "reward": 1.0, "source_env": "E5_relate",
    "source_task_id": "E5_a", "keywords": ["alpha_x", "beta_y", "related"]}) + "\n", encoding="utf-8")
sys.exit(0)
PY

expect_exit 0 "проба лейка: чистый пул — зелёный (буквальный счёт, без пайплайна)" \
  python3 tools/probe_pool_v2_leak.py --no-verifier --pool "$TMP/leakprobe/clean.jsonl"
expect_exit 1 "проба лейка: проверяемая строка в промпте — красный" \
  python3 tools/probe_pool_v2_leak.py --no-verifier --pool "$TMP/leakprobe/leaked.jsonl"
expect_contains "alpha_x" "проба лейка: красный путь называет задачу и строку" \
  python3 tools/probe_pool_v2_leak.py --no-verifier --pool "$TMP/leakprobe/leaked.jsonl"
expect_exit 2 "проба лейка: пула нет — NOT-VERIFIED, не «зелено»" \
  python3 tools/probe_pool_v2_leak.py --no-verifier --pool "$TMP/leakprobe/nope.jsonl"
expect_exit 2 "проба лейка: keyword-ветке нужен индекс концептов — NOT-VERIFIED, если его нет" \
  env -u CONCEPTS_INDEX python3 tools/probe_pool_v2_leak.py --pool "$TMP/leakprobe/keyword.jsonl"
expect_exit 0 "проба лейка: судья — замороженный verify_task (или NOT-VERIFIED без пайплайна)" \
  python3 - "$TMP" <<'PY'
import subprocess, sys
from pathlib import Path
r = subprocess.run([sys.executable, "tools/probe_pool_v2_leak.py", "--json",
                    "--pool", str(Path(sys.argv[1]) / "leakprobe" / "clean.jsonl")],
                   capture_output=True, text=True)
# Пайплайн недоступен — проба обязана сказать это вслух, а не отрапортовать «чисто».
assert r.returncode in (0, 2), (r.returncode, r.stdout[-400:], r.stderr[-400:])
if r.returncode == 2:
    assert "пайплайн" in r.stderr or "индекс" in r.stderr, r.stderr[-300:]
else:
    assert '"prompt_copy_state": "ok"' in r.stdout, r.stdout[-300:]   # проверка 2 состоялась
    assert '"rewarded": 0' in r.stdout, r.stdout[-300:]               # и лейка не нашла
sys.exit(0)
PY


echo

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
#: Имя версии — v2: умолчание прибора переключено решением ADR-054 п.1, и фикстура
#: обязана лежать там, куда прибор смотрит, иначе «набор не найден» читалось бы как
#: поломка прибора, а не как расхождение фикстуры с решением.
(d / "rl_tasks_revpool_v2.jsonl").write_text(
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
expect_exit 0 "evidence несёт вердикт воспроизведения и сдвиг v1→v2; якорь — свойством" \
  python3 - <<'PY'
import json, re, sys
from pathlib import Path
d = json.loads(Path("evidence/ppl-baseline-v1v2.json").read_text(encoding="utf-8"))
r = d["reproduction"]
assert r["verdict"] in ("СОВПАЛО", "ЯКОРЬ НЕ БАЗОВАЯ МОДЕЛЬ", "РАСХОЖДЕНИЕ БЕЗ ОБЪЯСНЕНИЯ"), r

# Якорь 127.5 / 11.76 проверяется СВОЙСТВОМ, а не значением: ADR-022 снял это число
# (оно измерено после загрузки CPT-чекпойнта, а не на базовой модели). Тест, который
# закреплял его буквально — `known.ppl_general == 127.5023…` и
# `anchor_provenance.value == "127.5 / 11.76"`, — зеленел ровно до тех пор, пока
# мёртвое число лежало в артефакте, а «PASS» читался как «якоря валидны». Свойство
# из двух частей:
#   (а) якорь помечен историческим/отозванным — с названной причиной, — либо убран
#       из артефакта целиком: убрать мёртвое число тест не запрещает;
#   (б) если якорь подан как действительный, его числа обязаны совпасть с базой,
#       объявленной в docs/specs: носитель объявленной базы — спека, не артефакт.
known = r.get("known") or {}
prov = r.get("anchor_provenance") or {}
ANCHOR_KEYS = ("ppl_general", "ppl_domain")
DEAD_MARKS = ("revoked", "revoked_reason", "retracted", "withdrawn",
              "historical", "why_it_is_not_base")
NUMBER = re.compile(r"\d+\.\d+")
SET_NAME = re.compile(r"\b(?:v|k)\d+_(?:general|domain)\b")


def dead_mark(block):
    """Пометка отзыва: (имя поля, причина) — чем блок сам говорит, что базой не является.

    Флаг (`revoked: true`) и строка с причиной равноправны; пустая пара — «не говорит».
    """
    for key in DEAD_MARKS:
        mark = block.get(key)
        if mark is True:
            return key, ""
        if isinstance(mark, str) and mark.strip():
            return key, mark.strip()
    return "", ""


def declared_bases():
    """Объявленные базы из таблиц спеки: имя набора → число.

    Имя набора стоит либо в самой ячейке («11.931924 (v1_general)»), либо в шапке
    столбца («| `v3_domain` | `v2_domain` |»), поэтому читаются обе формы.
    """
    found = {}
    for path in ("docs/specs/EVAL-SETS.md", "docs/specs/DOMAIN-EVAL-V3.md"):
        header = []
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line.startswith("|"):
                header = []
                continue
            if set(line) <= set("|-: "):
                continue                       # разделитель шапки: шапка прочитана
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not header:
                header = cells
                continue
            if not cells[0].startswith("База"):
                continue
            for i, cell in enumerate(cells[1:], start=1):
                names = SET_NAME.findall(cell) or SET_NAME.findall(
                    header[i] if i < len(header) else "")
                numbers = NUMBER.findall(cell)
                if names and numbers:
                    found.setdefault(names[0], float(numbers[0]))
    return found


numbers = {k: float(known[k]) for k in ANCHOR_KEYS
           if isinstance(known.get(k), (int, float))}
keeps_anchor = bool(numbers or prov.get("value"))
mark_key, mark_reason = dead_mark(prov)
if not mark_key:
    mark_key, mark_reason = dead_mark(known)

# Строка провенанса, если она есть, обязана нести те же числа, что и машиночитаемый
# блок: иначе «127.5 / 11.76» и `known.ppl_general` жили бы каждый своей жизнью, и
# снятие блока оставляло бы мёртвое число в прозе.
for token in NUMBER.findall(str(prov.get("value", ""))):
    digits = len(token.split(".")[1])
    assert numbers and any(abs(float(token) - v) <= 10 ** -digits for v in numbers.values()), \
        f"строка провенанса {token!r} не сходится с числами якоря {numbers}"

if not keeps_anchor:
    print("якорь снят из артефакта целиком — сверять нечего (ADR-022)")
elif mark_key:
    # Мёртвое число, оставленное в артефакте, обязано нести причину отзыва: «отозван»
    # без объяснения — не провенанс, а отписка (флаг `revoked: true` причины не требует).
    assert not mark_reason or len(mark_reason) >= 20, \
        f"пометка отзыва {mark_key!r} без причины: {mark_reason!r}"
    print(f"якорь помечен отозванным ({mark_key}): {mark_reason[:70]}")
else:
    sets = known.get("sets")
    assert sets, f"действующий якорь не называет набор: {known}"
    declared = declared_bases()
    for key, value in numbers.items():
        name = f"{sets}_{key.split('_', 1)[1]}"
        assert name in declared, f"{name} не объявлен в доке: {sorted(declared)}"
        assert abs(value - declared[name]) <= 1e-5 * max(1.0, declared[name]), \
            f"якорь {name} разошёлся с докой: артефакт {value}, спека {declared[name]}"

# Источник известной базы — артефакт S2, а не память инструмента: число без носителя
# непроверяемо. Проверка не применяется, только если якорь убран целиком.
if keeps_anchor:
    assert "s2-smoke.json" in str(known.get("source", "")), known
    assert Path("evidence/s2-smoke.json").is_file(), "носитель источника якоря пропал"

for name, key in (("v1_general", "ppl_general"), ("v1_domain", "ppl_domain")):
    c = r["checks"][name]
    carries = isinstance(c.get("known"), (int, float))
    # Блоки `known` и `checks` ходят парой: якорь либо есть в обоих, либо ни в одном —
    # иначе мёртвое число переживёт отзыв в одном из них и останется «зелёным».
    assert carries == (key in numbers), f"{name}: known и checks разошлись по якорю: {c}"
    if carries:
        assert abs(c["delta_pct"] - (c["measured"] / c["known"] - 1) * 100) < 1e-9, c
        assert abs(c["known"] - numbers[key]) < 1e-12, c
# Вердикт «якорь не базовая модель» обязан опираться на независимый якорь
# стенда (лосс при lr=0), а не на наше же число.
if r["verdict"] == "ЯКОРЬ НЕ БАЗОВАЯ МОДЕЛЬ":
    assert r["base_anchor_check"]["anchor_agrees_with_measurement"] is True
    assert r["base_anchor"]["loss"] > 0 and r["base_anchor"]["source"].endswith("cpt.log")
    # Строка провенанса — не самостоятельная опора вердикта (её несут проверки на
    # независимом якоре стенда и на одном корпусе ниже), поэтому при снятом якоре
    # она не требуется: тест не падает от того, что мёртвое число убрано.
    if prov:
        assert "Loaded CPT ckpt" in prov.get("preceding_line", ""), prov
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

echo "== 16. S3m: калибровка микса и пика LR (ADR-022 п.2) =="

echo "  --- 16a. build_mix_v12r50.py: рецепт, самопроверка, отказы ---"
expect_exit 0 "чанковка, шафл и position_ids воспроизводят рецепт v12r" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import numpy as np
import build_mix_v12r50 as B
# Чанковка оригинала: НЕхвостовые блоки max_len (хвост < max_len отброшен).
assert B.chunk_tokens(list(range(10)), 4) == [[0, 1, 2, 3], [4, 5, 6, 7]], B.chunk_tokens(list(range(10)), 4)
assert B.chunk_tokens(list(range(8)), 4) == [[0, 1, 2, 3]], "хвост обязан быть отброшен"
# Шафл: тот же сид — та же перестановка; другой сид — другая (иначе «шафла нет»).
a = {"d": [list(range(4)), list(range(4, 8))], "r": [list(range(100, 104))]}
import numpy as _np
def build(seed):
    x = _np.concatenate([_np.array(a["d"], dtype=_np.int32), _np.array(a["r"], dtype=_np.int32)])
    _np.random.RandomState(seed).shuffle(x)
    return x
assert _np.array_equal(build(42), build(42))
assert not _np.array_equal(build(42), build(43))
# position_ids: сброс на старте документа, продолжение через границу чанка.
starts = [0, 5]
assert list(B.pos_for_range(starts, 0, 8, _np)) == [0, 1, 2, 3, 4, 0, 1, 2]
assert list(B.pos_for_range(starts, 3, 6, _np)) == [3, 4, 0]
# Обе части микса — префиксы частей v12r: доли 50 % из 3493+3493, а не из полного домена.
assert B.V12R_DOMAIN_CHUNKS == 7332 and B.V12R_REPLAY_CHUNKS == 2444 and B.V12R_SEED == 42
# Быстрый путь (плоский массив + reshape) обязан давать РОВНО те же чанки, что
# эталон рецепта `chunk_tokens`, включая границы (хвост отброшен). Иначе ускорение
# сборки молча поменяло бы состав корпуса.
for T in (0, 1, 4, 5, 8, 9, 17, 100):
    ref = B.chunk_tokens(list(range(T)), 4)
    got = B.chunks_from_stream(list(range(T)), 4, _np)
    assert got.shape[0] == len(ref), (T, got.shape, len(ref))
    if ref:
        assert _np.array_equal(got, _np.array(ref, dtype=_np.int32)), T
# Арифметика `range(0, T-L, L)`: последний ПОЛНЫЙ блок тоже отбрасывается, когда
# длина потока кратна L (поток ровно из 9776 чанков даёт 9775) — это свойство
# рецепта, а не ошибка округления: cp. домен 60066303 ток → 7332 чанка.
assert B.n_chunks(9776 * 8192, 8192) == 9775, B.n_chunks(9776 * 8192, 8192)
assert B.n_chunks(60066303, 8192) == 7332, B.n_chunks(60066303, 8192)
# position_ids одним проходом обязаны совпасть с пофрагментным расчётом.
starts = [0, 5, 11]
ps = B.pos_stream(starts, 14, _np)
for lo in range(14):
    assert list(ps[lo:lo + 1]) == list(B.pos_for_range(starts, lo, lo + 1, _np)), lo
sys.exit(0)
PY
# Синтетический прогон сборщика целиком: он ловит то, чего не видят проверки
# чистых функций, — согласованность форм массивов и структуру отчёта. Реальный
# дефект 16.09 (reshape без обрезки хвоста, `cannot reshape array of size
# Синтетический прогон сборщика целиком: он ловит то, чего не видят проверки
# чистых функций, — согласованность форм массивов, порядок входов и структуру
# отчёта. Реальные дефекты 16.09 всплывали только здесь и только на 60 млн
# токенов: reshape без обрезки хвоста (`cannot reshape array of size 60066303
# into shape (7332,8192)`) и сверка доменного кэша с миксом (`7332` против
# `9776`). Фикстура — семь документов домена и девять реплея; доменный кэш
# собирается **функциями самого сборщика**, а не второй их копией в тесте.
MIXFIX="$TMP/mixfix"; mkdir -p "$MIXFIX/tok"
python3 - "$MIXFIX" <<'PY'
import sys
from pathlib import Path
d = Path(sys.argv[1])
# Текст фикстуры нарочно НЕпериодический: на повторяющейся фразе строки кэша
# совпадают, и проверка различимости падает на здоровой сборке (факт 16.09).
(d / "dev.txt").write_text("\n---\n".join(
    f"Документ номер {i}: " + " ".join(f"домен{i}лексема{k}" for k in range(60))
    for i in range(7)), encoding="utf-8")
(d / "rep.txt").write_text("\n\n".join(
    f"Общий текст {i}: " + " ".join(f"реплей{i}слово{k}" for k in range(60))
    for i in range(9)), encoding="utf-8")
sys.path.insert(0, "tools")
import numpy as np
import build_mix_v12r50 as B
tok, _ = B.load_tokenizer("qwen25", "Qwen/Qwen2.5-0.5B")
tokens, starts, _n = B.stream_with_starts(d / "dev.txt", B.DOMAIN_SEP, B.MIN_DOC_CHARS, tok)
# Доменный кэш — вход сборщика (в контуре его собирает претокенизация v10.1);
# здесь он берётся из тех же функций, что и в сборке, — тогда проверка чанковки
# проверяет путь, а не фикстуру.
np.save(d / "tok" / "dev_16_qwen25.npy", B.chunks_from_stream(tokens, 16, np))
assert (d / "tok" / "dev_16_qwen25.npy").is_file()
PY
expect_exit 0 "синтетика: кэш собран, формы массивов сходятся, хвост отброшен" \
  python3 tools/build_mix_v12r50.py --datasets-dir "$MIXFIX" --domain-txt dev.txt \
      --general-txt rep.txt --domain-stem dev --out-stem-base dev_v12r --out-stem dev_v12r50 \
      --max-len 16 --replay-chunks 3 --skip-v12r-check --force \
      --report "$MIXFIX/report.json"
expect_exit 0 "синтетика: кэш и pos-компаньон одной формы, токены = вход, отчёт полон" \
  python3 - "$MIXFIX" <<'PY'
import json, sys
from pathlib import Path
import numpy as np
d = Path(sys.argv[1])
tok = np.load(d / "tok" / "dev_v12r50_16_qwen25.npy")
pos = np.load(d / "tok" / "dev_v12r50_16_qwen25_pos.npy")
assert tok.shape == pos.shape == (6, 16), (tok.shape, pos.shape)
assert tok.dtype == pos.dtype == np.int32, (tok.dtype, pos.dtype)
assert len({tuple(r) for r in tok.tolist()}) == 6, "строки не различимы — фикстура слаба"
# position_ids — смещение от старта СВОЕГО документа, а не номер внутри чанка:
# чанк может начинаться в середине документа, и тогда позиция больше длины чанка
# (так же считает build_cpt_v12r_pos.py). Проверяется семантика, а не граница:
# внутри чанка позиция растёт на 1 или сбрасывается на границе документа.
assert (pos >= 0).all(), pos.min()
diffs = np.diff(pos, axis=1)
assert ((diffs == 1) | (diffs < 0)).all(), "позиции не растут на 1 и не сбрасываются"
assert (diffs < 0).any() or (pos[:, 0] == 0).any(), "нет ни одного сброса — маскирование не проверено"
r = json.loads((d / "report.json").read_text(encoding="utf-8"))
m = r["mix"]
assert m["domain_chunks"] == m["replay_chunks"] == 3, m
assert m["chunks"] == 6 and m["tokens"] == 96, m
assert abs(m["mix_replay_ratio"] - 0.5) < 1e-9, m
assert r["written"] and len(r["written"]) == 3, r["written"]
assert (d / "dev_v12r50.txt").is_file(), "манифест микса не написан"
assert r["tool"]["sha256"] and r["tool"]["path"].endswith("build_mix_v12r50.py"), r["tool"]
sys.exit(0)
PY
expect_exit 1 "синтетика: повторная сборка без --force отвергается (AD-7)" \
  python3 tools/build_mix_v12r50.py --datasets-dir "$MIXFIX" --domain-txt dev.txt \
      --general-txt rep.txt --domain-stem dev --out-stem-base dev_v12r --out-stem dev_v12r50 \
      --max-len 16 --replay-chunks 3 --skip-v12r-check
expect_exit 0 "синтетика: --force пересобирает, хеши те же (сборка детерминирована)" \
  python3 tools/build_mix_v12r50.py --datasets-dir "$MIXFIX" --domain-txt dev.txt \
      --general-txt rep.txt --domain-stem dev --out-stem-base dev_v12r --out-stem dev_v12r50 \
      --max-len 16 --replay-chunks 3 --skip-v12r-check --force --report "$MIXFIX/report2.json"
expect_exit 0 "синтетика: хеш первого отчёта равен хешу второго (тот же вход — тот же кэш)" \
  python3 - "$MIXFIX" <<'PY'
import json, sys
from pathlib import Path
d = Path(sys.argv[1])
a = json.loads((d / "report.json").read_text(encoding="utf-8"))
b = json.loads((d / "report2.json").read_text(encoding="utf-8"))
ka = {w["path"].split("/")[-1]: w["sha256"] for w in a["written"]}
kb = {w["path"].split("/")[-1]: w["sha256"] for w in b["written"]}
assert ka == kb, (ka, kb)
assert a["mix"] == b["mix"], "состав разошёлся между сборками"
sys.exit(0)
PY
# Эталон «v12r» для самопроверки: 3 домена + 2 реплея. Собирается ДО проверяемой
# сборки и с --skip-v12r-check: сверять его пока не с чем.
expect_exit 0 "синтетика: эталонный v12r (3 домена + 2 реплея) собран" \
  python3 tools/build_mix_v12r50.py --datasets-dir "$MIXFIX" --domain-txt dev.txt \
      --general-txt rep.txt --domain-stem dev --out-stem-base dev_v12r --out-stem dev_v12r \
      --max-len 16 --replay-chunks 2 --domain-chunks 3 --skip-v12r-check --force
expect_exit 0 "синтетика: сборка с самопроверкой против синтетического v12r проходит" \
  python3 tools/build_mix_v12r50.py --datasets-dir "$MIXFIX" --domain-txt dev.txt \
      --general-txt rep.txt --domain-stem dev --out-stem-base dev_v12r --out-stem dev_v12r50chk \
      --max-len 16 --replay-chunks 3 --domain-chunks 0 \
      --v12r-domain-chunks 3 --v12r-replay-chunks 2 --force --report "$MIXFIX/report50.json"
expect_exit 0 "синтетика: отчёт несёт обе сверки самопроверки" \
  python3 - "$MIXFIX" <<'PY'
import json, sys
from pathlib import Path
r = json.loads((Path(sys.argv[1]) / "report50.json").read_text(encoding="utf-8"))
fc = r["fidelity_check"]
assert fc["domain_chunks_equal"] is True, fc
assert fc["tokens_equal"] is True and fc["pos_equal"] is True, fc
assert fc["reference"]["sha256"] and fc["reference_domain"]["sha256"], fc
assert r["mix"]["chunks"] == 6, r["mix"]
sys.exit(0)
PY
python3 - "$MIXFIX" <<'PY'
import sys
import numpy as np
from pathlib import Path
p = Path(sys.argv[1]) / "tok" / "dev_16_qwen25.npy"
a = np.load(p)
a[0, 0] = (int(a[0, 0]) + 1) % 1000
np.save(p, a)
PY
expect_exit 1 "синтетика: подменённый доменный кэш ловится сверкой чанковки (отказ, а не запись)" \
  python3 tools/build_mix_v12r50.py --datasets-dir "$MIXFIX" --domain-txt dev.txt \
      --general-txt rep.txt --domain-stem dev --out-stem-base dev_v12r --out-stem dev_v12r50bad \
      --max-len 16 --replay-chunks 3 --domain-chunks 0 \
      --v12r-domain-chunks 3 --v12r-replay-chunks 2 --force
expect_exit 2 "нет источника → NOT-VERIFIED, а не пустой кэш" \
  python3 tools/build_mix_v12r50.py --datasets-dir /nonexistent-datasets --plan
expect_exit 2 "неизвестный тег токенизатора отвергается" \
  python3 tools/build_mix_v12r50.py --tag qwen99 --plan
expect_contains "перезапись только с --force" "защита существующего кэша названа не только в коде" \
  grep -h "перезапись только с --force" tools/build_mix_v12r50.py

echo "  --- 16b. run_mix_lr_calib.py: сетка, патч копии пайплайна, корпус руки ---"
expect_exit 0 "план калибровки печатается без обращения к стенду" \
  python3 tools/run_mix_lr_calib.py --plan
expect_contains "50-0.35" "в плане названы все четыре руки сетки" \
  python3 tools/run_mix_lr_calib.py --plan
expect_contains "ADR-022" "в плане названо основание" \
  python3 tools/run_mix_lr_calib.py --plan
expect_exit 0 "патч применяется ровно по трём якорям; хеш базовой ревизии фиксируется" \
  python3 - <<'PY'
import argparse, sys
sys.path.insert(0, "tools")
import run_mix_lr_calib as R
src = R.PIPELINE_SRC.read_text(encoding="utf-8")
out, rec = R.patched_pipeline(src)
assert rec["pipeline_base_sha256"] == R.sha256_text(src), "хеш базовой ревизии не тот"
assert rec["patched_sha256"] == R.sha256_text(out)
assert rec["pipeline_base_file"] == "laguna_pipeline_v8.py"
# Вставки: траектория — один вызов на шаг, чекпойнт — свой интервал БЕЗ ретенции
# (штатная ретенция удалила бы шаги 500/1000, ради которых прогон и идёт).
assert out.count("_calib_trace(step, loss.item(), lr, args)") == 1
assert out.count("def _calib_trace(") == 1
assert 'os.environ.get("CALIB_CKPT_EVERY", "500")' in out
assert "keep_last=0" in out, "номерной чекпойнт сохраняется с ретенцией — точки замера пропадут"
# `keep_last=0` выключает ретенцию ВНУТРИ save_checkpoint_atomic, но сразу за
# вставкой в цикле CPT стоит собственная ретенция пайплайна: glob по
# `checkpoint_[0-9]*.pt` и «оставить два последних по mtime». На шаге 1500 она
# удалила точку шага 500 у руки 25-0.7 — то есть ровно то, ради чего вставка и
# делалась. Точка замера обязана быть ВНЕ этого glob, и это проверяется машиной,
# а не комментарием.
import fnmatch
assert 'glob("checkpoint_[0-9]*.pt")' in out, \
    "в копии пайплайна нет ретенции номерных чекпойнтов — проверка ниже ничего не значит"
calib_name = R.CALIB_CKPT_FMT.format(step=500)
assert not fnmatch.fnmatch(calib_name, "checkpoint_[0-9]*.pt"), \
    f"имя точки замера {calib_name} попадает под ретенцию пайплайна — точка шага 500 пропадёт"
assert f'f"{R.CALIB_CKPT_STEM}_{{step}}.pt"' in out, \
    "патч пишет точки замера не тем именем, которое проверено выше"
assert R.LEGACY_CKPT_FMT.format(step=500) == "checkpoint_500.pt"
# Шаг 2000 отдельным файлом не пишется: цикл идёт по 0…1999, и состояние после
# 2000 шагов оптимизатора — это checkpoint_final.pt. Разрешение имён — одно.
assert R.STEPS == 2000 and R.CKPT_POINTS[-1] == R.STEPS
fake = {f"{R.STAND_RUNS}/r/checkpoints/checkpoint_final.pt": True}
assert R.ckpt_path_for_step("r", 2000, exists=fake.__contains__,
                            runs_root=R.STAND_RUNS).endswith("checkpoint_final.pt")
fake_new = {f"{R.STAND_RUNS}/r/checkpoints/calib_checkpoint_500.pt": True}
assert R.ckpt_path_for_step("r", 500, exists=fake_new.__contains__,
                            runs_root=R.STAND_RUNS).endswith("calib_checkpoint_500.pt")
fake_leg = {f"{R.STAND_RUNS}/r/checkpoints/checkpoint_1000.pt": True}
assert R.ckpt_path_for_step("r", 1000, exists=fake_leg.__contains__,
                            runs_root=R.STAND_RUNS).endswith("checkpoint_1000.pt")
assert R.ckpt_path_for_step("r", 500, exists=lambda p: False,
                            runs_root=R.STAND_RUNS) is None
# Штатный блок CPT-чекпойнта заменён (у SFT свой такой же — он не трогается:
# правка копии ограничена стадией CPT, иначе патч вышел бы за объявленную границу).
assert R.ANCHOR_CKPT not in out, "штатный интервал чекпойнтов CPT остался"
assert "sft_checkpoint_" in out, "патч задел SFT-чекпойнты — граница нарушена"
assert out.count("if step % 200 == 0 and step > 0:") == 1, "тронут не только CPT-блок"
# Точки замера обязаны попадать на интервал сохранения.
assert all(p % R.CKPT_EVERY == 0 for p in R.CKPT_POINTS), (R.CKPT_POINTS, R.CKPT_EVERY)
assert R.CKPT_POINTS == [500, 1000, 1500, 2000]
# Якорь, встречающийся не один раз, — отказ, а не «применилось не туда».
try:
    R.patched_pipeline(src + "\n" + R.ANCHOR_LOSS)
except SystemExit as e:
    assert "якорь" in str(e) and "2 раз" in str(e), e
else:
    raise AssertionError("патч применился к неоднозначному якорю")
# Сетка: четыре руки, доли replay только 25/50, у каждой свой корпус.
assert list(R.ARMS) == ["25-0.7", "25-0.35", "50-0.7", "50-0.35"], list(R.ARMS)
assert {R.ARMS[a]["replay"] for a in R.ARMS} == {25, 50}
assert {R.ARMS[a]["lr_scale"] for a in R.ARMS} == {0.7, 0.35}
assert set(R.CORPORA) == {25, 50}
assert R.CORPORA[25]["cache_host"].endswith("cpt_corpus_v12r_8192_qwen25.npy")
assert R.CORPORA[50]["cache_host"].endswith("cpt_corpus_v12r50_8192_qwen25.npy")
# TSV стадии: восемь полей, шаг = STEPS, батч 1 (flex eager), артефакт — финальный чекпойнт
f = R.stages_tsv("50-0.35", "20260101-0000").strip().split("\t")
assert len(f) == 8, f
assert f[0] == "cpt" and f[1] == "cpt" and f[3] == "checkpoints/checkpoint_final.pt", f
assert f[4] == str(R.STEPS) == "2000" and f[5] == "1", f
assert f[7] == "calib-50-0.35-20260101-0000", f
# Команда цепочки: корпус руки (а не константа), манифест AD-2 с тем же кэшем,
# оба гиперпараметра руки и хеш базовой ревизии пайплайна.
args = argparse.Namespace(stall_minutes=20, sampler_seconds=3600, image="test-image")
cmd = R.chain_command("50-0.7", "20260101-0000", "/stand/calib-50-0.7-20260101-0000", args)
assert "--cpt-data /workspace/shared/datasets/cpt_corpus_v12r50.txt" in cmd, cmd
assert "--dataset /home/user/gb10-shared/datasets/tok/cpt_corpus_v12r50_8192_qwen25.npy" in cmd, cmd
assert "--peak-lr-scale 0.7" in cmd and "--extra-hyperparam peak_lr_scale=0.7" in cmd, cmd
assert "--extra-hyperparam replay_share_pct=50" in cmd, cmd
assert "pipeline_base_sha256=" in cmd, cmd
assert "--ctr-run-dir /workspace/shared/calib/calib-50-0.7-20260101-0000" in cmd, cmd
# GEN-EVAL пайплайна выключен: его «base» — шаг 50 (дефект S3k), а меряет он только v1.
assert "--cpt-gen-eval-every 0" in cmd, cmd
cmd25 = R.chain_command("25-0.7", "20260101-0000", "/stand/x", args)
assert "--cpt-data /workspace/shared/datasets/cpt_corpus_v12r.txt" in cmd25, cmd25
assert "--peak-lr-scale 0.35" not in cmd25, "LR руки 25-0.7 перепутан"
# Тренд по третям и время шага считаются, а не описываются словами.
tr = [{"step": i, "loss": 3.0 - i * 0.001, "t": 1000 + i * 2.0} for i in range(9)]
assert R.thirds([r["loss"] for r in tr])["monotone_down"] is True
assert R.step_seconds(tr)["mean"] == 2.0, R.step_seconds(tr)
# Повтор стадии цепочкой дописывает траекторию: на шаг — последняя запись, не дубль.
import pathlib, tempfile, json as _json
with tempfile.TemporaryDirectory() as td:
    p = pathlib.Path(td) / "loss_trace.jsonl"
    p.write_text("\n".join(_json.dumps(r) for r in
                           [{"step": 0, "loss": 9.0, "t": 1}, {"step": 1, "loss": 8.0, "t": 2},
                            {"step": 1, "loss": 2.0, "t": 3}]) + "\n", encoding="utf-8")
    got = R.read_trace(p)
    assert [r["step"] for r in got] == [0, 1] and got[1]["loss"] == 2.0, got
sys.exit(0)
PY
expect_exit 0 "вердикт: эффекты, вклад факторов, потолок и обе ветки рекомендации" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import run_mix_lr_calib as R
B = {"v1_general": 11.93, "v2_general": 10.77}


def arm(name, replay, lr, r1, r2, base=None):
    d = {"arm": name, "replay": replay, "lr_scale": lr, "steps_logged": 2000,
         "ppl_v1_2000": r1, "ppl_v2_2000": r2}
    if base:
        d["ppl_base"] = base
    return d


# Ветка 1: одна рука проходит потолок 2× — рекомендация называет её конфигом.
ok = [arm("25-0.7", 25, 0.7, 200.0, 40.0, B), arm("25-0.35", 25, 0.35, 120.0, 30.0),
      arm("50-0.7", 50, 0.7, 80.0, 25.0), arm("50-0.35", 50, 0.35, 15.0, 12.0)]
v = R.verdict(ok)
assert v["verdict_status"] == "OK"
assert v["under_ceiling"] == ["50-0.35"], v["under_ceiling"]
assert v["best_arm_step2000"] == "50-0.35"
assert "полный CPT вести конфигом руки 50-0.35" in v["recommendation"], v["recommendation"]
# Знак эффекта: минус = уровень фактора снижает деградацию (доля 50 % против 25 %).
e = v["effects_v1_ratio"]
assert e["replay_effect"] < 0 and e["lr_effect"] < 0, e
assert e["replay_effect"] == round(e["replay_50"] - e["replay_25"], 3), e
assert e["lr_effect"] == round(e["lr_0.35"] - e["lr_0.7"], 3), e
c = v["factor_contribution"]["v1"]
assert c["stronger"] in ("replay", "lr"), c
assert abs(c["replay_share_pct"] + c["lr_share_pct"] - 100.0) < 0.11, c
# Ветка 2: потолок не проходит никто — рекомендация НЕ выдаёт конфиг за выбранный.
bad = [arm("25-0.7", 25, 0.7, 205.0, 40.0, B), arm("25-0.35", 25, 0.35, 180.0, 35.0),
       arm("50-0.7", 50, 0.7, 150.0, 30.0), arm("50-0.35", 50, 0.35, 120.0, 25.0)]
v2 = R.verdict(bad)
assert v2["under_ceiling"] == [], v2["under_ceiling"]
assert v2["recommendation"].startswith("конфиг полного CPT этой сеткой НЕ выбран"), v2["recommendation"]
assert "решение владельца" in v2["recommendation"], v2["recommendation"]
# Без базового замера вердикт не выдумывает числа.
v3 = R.verdict([{**a, "ppl_base": {}} for a in bad])
assert v3["verdict_status"] == "NOT-VERIFIED" and "reason" in v3, v3
sys.exit(0)
PY
expect_exit 0 "разбор руки читает числа пробы по именам состояний из плана" \
  python3 - <<'PY'
import json, pathlib, sys, tempfile
sys.path.insert(0, "tools")
import run_mix_lr_calib as R
ts = "20260101-0000"
arm = "25-0.35"
run = R.run_id(arm, ts)
SETS = ("v1_general", "v1_domain", "v2_general", "v2_domain")
with tempfile.TemporaryDirectory() as td:
    root = pathlib.Path(td)
    d = root / run
    (d / "logs").mkdir(parents=True)
    (d / "checkpoints").mkdir(parents=True)
    (d / "calib_params.json").write_text(json.dumps({
        "replay_share_pct": 25, "peak_lr_scale": 0.35, "corpus_label": "v12r (фикстура)",
        "cpt_dataset_host": "/nonexistent/fixture.npy"}), encoding="utf-8")
    (d / "logs" / "loss_trace.jsonl").write_text("\n".join(
        json.dumps({"step": i, "loss": 2.0 - i * 0.001, "lr": 1e-4, "t": 1000 + i * 2.0})
        for i in range(9)) + "\n", encoding="utf-8")
    for step in (500, 1000, 1500):
        (d / "checkpoints" / R.CALIB_CKPT_FMT.format(step=step)).write_text("x", encoding="utf-8")
    (d / "checkpoints" / R.FINAL_CKPT_NAME).write_text("x", encoding="utf-8")

    def state(v1, v2):
        return {"sets": {k: {"ppl": (v1 if k.startswith("v1") else v2)} for k in SETS}}

    # Имена состояний — ровно те, что строит probe_plan (дефект 16.09: разбор
    # искал «c<шаг>» и не находил ничего, evidence выходил без чисел пробы).
    plan = R.probe_plan(ts, arms=[arm], runs_root=str(root))
    names = [n for n, _ in plan["states"]]
    assert f"{run}-c500" in names and plan["gaps"] == [], (names, plan["gaps"])
    ppl = {"states": {"base": state(11.93, 10.77),
                      **{n: state(100.0 + s, 50.0 + s) for s in R.CKPT_POINTS
                         for n in [f"{run}-c{s}"]}}}
    rep = R.arm_report(arm, ts, ppl, runs_root=root)
    assert rep["ppl_v1_2000"] == 2100.0, rep["ppl_v1_2000"]
    assert rep["ppl_v2_2000"] == 2050.0, rep["ppl_v2_2000"]
    assert sorted(rep["ppl_trace"]) == R.CKPT_POINTS, sorted(rep["ppl_trace"])
    assert rep["ppl_gaps"] == [], rep["ppl_gaps"]
    assert rep["steps_logged"] == 9 and rep["step_sec"]["mean"] == 2.0, rep["step_sec"]
    # Состояния со старыми именами («c500») не читаются: числа не подменяются
    # похожими, а честно отсутствуют — с причиной на каждый шаг.
    rep2 = R.arm_report(arm, ts, {"states": {"c500": state(1, 1), "base": state(1, 1)}},
                        runs_root=root)
    assert rep2["ppl_v1_2000"] is None and len(rep2["ppl_gaps"]) == 4, rep2["ppl_gaps"]
    # Пропуск назван причиной, и причины различаются: «файла нет» против
    # «файл есть, но проба его не застала». Оба случая — разные действия.
    (d / "checkpoints" / R.CALIB_CKPT_FMT.format(step=1500)).unlink()
    ppl_nofile = {"states": {k: v for k, v in ppl["states"].items()
                             if k != f"{run}-c1500"}}
    rep3 = R.arm_report(arm, ts, ppl_nofile, runs_root=root)
    assert [g["step"] for g in rep3["ppl_gaps"]] == [1500], rep3["ppl_gaps"]
    assert "нет файла состояния" in rep3["ppl_gaps"][0]["reason"], rep3["ppl_gaps"]
    # Замер уже сделан — удаление файла его не отменяет: пропажа файла после
    # замера обесценила бы доказательство задним числом.
    (d / "checkpoints" / R.FINAL_CKPT_NAME).unlink()
    rep4 = R.arm_report(arm, ts, ppl_nofile, runs_root=root)
    assert rep4["ppl_v1_2000"] == 2100.0, rep4["ppl_v1_2000"]
    # Обратный случай: файл на месте, а проба его не застала — причина другая.
    (d / "checkpoints" / R.FINAL_CKPT_NAME).write_text("x", encoding="utf-8")
    ppl_nostate = {"states": {k: v for k, v in ppl_nofile["states"].items()
                              if k != f"{run}-c2000"}}
    rep5 = R.arm_report(arm, ts, ppl_nostate, runs_root=root)
    reasons = {g["step"]: g["reason"] for g in rep5["ppl_gaps"]}
    assert set(reasons) == {1500, 2000}, reasons
    assert "в отчёте пробы его нет" in reasons[2000], reasons
    assert "нет файла состояния" in reasons[1500], reasons
sys.exit(0)
PY
expect_exit 2 "план пробы: пропуск решающей точки — NOT-VERIFIED, а не «частичный» замер" \
  python3 - <<'PY'
import argparse, pathlib, sys, tempfile
sys.path.insert(0, "tools")
import run_mix_lr_calib as R
ts = "20260101-0000"
with tempfile.TemporaryDirectory() as td:
    root = pathlib.Path(td)
    # Есть только точка 500: без шага 2000 вердикт по потолку считать не на чем.
    d = root / R.run_id("25-0.35", ts) / "checkpoints"
    d.mkdir(parents=True)
    (d / R.CALIB_CKPT_FMT.format(step=500)).write_text("x", encoding="utf-8")
    plan = R.probe_plan(ts, arms=["25-0.35"], runs_root=str(root))
    assert [g["step"] for g in plan["gaps"]] == [1000, 1500, 2000], plan["gaps"]
    assert [g["decisive"] for g in plan["gaps"]] == [False, False, True], plan["gaps"]
    assert [n for n, _ in plan["states"]] == ["base", "calib-25-0.35-20260101-0000-c500",
                                             "base_untouched"], plan["states"]
# На стенде точек нет вовсе → проба не запускается, и это видно по коду возврата
# (никакого ssh: отказ случается до обращения к стенду).
args = argparse.Namespace(ts=ts, arms=["25-0.35"], host="gb10-nohost", image="none",
                          probe_timeout=5)
sys.exit(R.do_probe(args))
PY
expect_exit 2 "команды руки без --ts адресуются не туда и отвергаются" \
  python3 tools/run_mix_lr_calib.py --status --arm 25-0.7
expect_exit 2 "--launch без --arm отвергается" \
  python3 tools/run_mix_lr_calib.py --launch --ts 20260101-0000

echo "  --- 16c. pilot_chain.sh: корпус и версия прогона — параметры (S3m) ---"
# Регресс-защита: значения по умолчанию обязаны остаться прежними (v12r), иначе
# существующие вызовы run_pilot.py молча поедут по другому корпусу.
expect_exit 0 "умолчание корпуса не изменилось (v12r) и новые флаги разбираются" \
  python3 - <<'PY'
import pathlib, sys
src = pathlib.Path("tools/pilot_chain.sh").read_text(encoding="utf-8")
assert 'CPT_DATA="/workspace/shared/datasets/cpt_corpus_v12r.txt"' in src, "умолчание корпуса поехало"
assert 'DATASET_NPY="$SHARED/datasets/tok/cpt_corpus_v12r_8192_qwen25.npy"' in src
for flag in ("--cpt-data)", "--dataset)", "--runner-name)", "--extra-hyperparam)"):
    assert flag in src, flag
# Зашитых путей в теле больше нет: корпус приходит флагом.
assert "printf -- ' --cpt_data /workspace/shared/datasets/cpt_corpus_v12r.txt'" not in src
assert 'printf -- \' --cpt_data %s\' "$CPT_DATA"' in src
assert '--dataset "$DATASET_NPY"' in src
assert '--run-version "${RUNNER_NAME}@${RUNNER_SHA12:-?}"' in src
# Дополнительные гиперпараметры передаются по одному флагу на пару (значение с «=»
# внутри не разбирается повторно).
assert 'extra+=(--hyperparams "$kv")' in src
sys.exit(0)
PY
CHAIN_DIR="$CHAIN_TMP/s3m-default"; mkdir -p "$CHAIN_DIR"; mkstages "$CHAIN_DIR/stages.tsv"
expect_exit 0 "стадия без новых флагов идёт по прежнему корпусу" \
  test_chain_args "$CHAIN_DIR" ok "$CHAIN_TMP/guard_ok.sh" "$CHAIN_TMP/s3m-default/args.txt"
expect_exit 0 "в контейнер ушёл корпус по умолчанию (v12r)" \
  grep -q -- "--cpt_data /workspace/shared/datasets/cpt_corpus_v12r.txt" "$CHAIN_TMP/s3m-default/args.txt"
CHAIN_DIR="$CHAIN_TMP/s3m-corpus"; mkdir -p "$CHAIN_DIR"; mkstages "$CHAIN_DIR/stages.tsv"
expect_exit 0 "стадия с --cpt-data идёт по корпусу руки" \
  test_chain_args "$CHAIN_DIR" ok "$CHAIN_TMP/guard_ok.sh" "$CHAIN_TMP/s3m-corpus/args.txt" \
      --cpt-data /workspace/shared/datasets/cpt_corpus_v12r50.txt \
      --dataset /tmp/fixture-v12r50.npy --runner-name tools/run_mix_lr_calib.py
expect_exit 0 "переданный корпус доехал до пайплайна (а не только до лога)" \
  grep -q -- "--cpt_data /workspace/shared/datasets/cpt_corpus_v12r50.txt" "$CHAIN_TMP/s3m-corpus/args.txt"
expect_exit 1 "корпус руки не подменяет корпус соседней руки" \
  grep -q -- "--cpt_data /workspace/shared/datasets/cpt_corpus_v12r.txt" "$CHAIN_TMP/s3m-corpus/args.txt"

echo "  --- 16d. calib_ppl_probe.py: методика, состояния, эталон S3h ---"
expect_exit 0 "план пробы называет состояния и эталон S3h" \
  python3 tools/calib_ppl_probe.py --plan --state base=base --state c500=ckpt:/tmp/x.pt
expect_contains "v2_domain" "в плане назван набор v2 (ADR-018)" \
  python3 tools/calib_ppl_probe.py --plan --state base=base
expect_contains "11.931924" "эталон S3h берётся из evidence, а не вписан в код" \
  python3 tools/calib_ppl_probe.py --plan --state base=base
expect_exit 0 "методика импортируется из ppl_probe, а не переписана" \
  python3 - <<'PY'
import inspect, pathlib, sys
sys.path.insert(0, "tools")
import calib_ppl_probe as C
src = pathlib.Path("tools/calib_ppl_probe.py").read_text(encoding="utf-8")
# Замер обязан быть функцией ppl_probe.measure (методика _ppl_eval S3h), а загрузка
# чекпойнта — функцией пайплайна прогона: своя реализация разошлась бы с контуром.
assert "P.measure(" in src and "def measure(" not in src, "проба завела свой замер"
assert "pipe._load_ckpt_with_resize(" in src, "загрузка чекпойнта не из пайплайна прогона"
assert 'P.SETS' in src, "наборы берутся не из ppl_probe"
# parse_states: base и ckpt:PATH, мусор — отказ
assert C.parse_states(["a=base"]) == [("a", "base", None)]
assert C.parse_states(["b=ckpt:/p/c.pt"]) == [("b", "ckpt", "/p/c.pt")]
for bad in ("a", "a=weird", "a=ckpt:"):
    try:
        C.parse_states([bad])
    except SystemExit:
        pass
    else:
        raise AssertionError(f"{bad}: мусор принят как состояние")
# Допуск согласия с S3h — не «на глаз»
assert C.BASE_AGREEMENT_TOL == 1e-6, C.BASE_AGREEMENT_TOL
# Эталон S3h ищется от корня кейса; корень — параметр (`--case-root`), потому что
# проба гоняется и в контейнере стенда, где каталога кейса нет.
assert C.S3H_NAME == "evidence/ppl-baseline-v1v2.json", C.S3H_NAME
assert C.s3h_reference(C.CASE_ROOT, ["v1_general"])["v1_general"] > 1.0
assert C.CASE_ROOT.is_dir() and (C.CASE_ROOT / C.S3H_NAME).is_file()
sys.exit(0)
PY
expect_exit 2 "нет файла пайплайна прогона → NOT-VERIFIED" \
  python3 tools/calib_ppl_probe.py --pipeline /nonexistent/laguna.py --state base=base
expect_exit 2 "нет чекпойнта → NOT-VERIFIED, а не «пропущено»" \
  python3 tools/calib_ppl_probe.py --pipeline laguna_pipeline_v8.py \
      --state base=base --state c500=ckpt:/nonexistent/checkpoint_500.pt
expect_exit 2 "неизвестный набор → NOT-VERIFIED" \
  python3 tools/calib_ppl_probe.py --pipeline laguna_pipeline_v8.py \
      --sets v1_general,нетакого --state base=base

echo "  --- 16e. карточка микса v12r50 (AD-7 / C-010, C-016) ---"
expect_exit 0 "карточка v12r50: арифметика, доли, лицензия, происхождение" \
  python3 - <<'PY'
import json, sys
c = json.load(open("data/corpus-card-v12r50.json", encoding="utf-8"))
m = c["mix"]
assert m["chunks"] == m["domain_chunks"] + m["replay_chunks"], "чанки не сходятся"
assert m["tokens"] == m["chunks"] * m["chunk_len"], "токены не сходятся"
assert m["chunk_len"] == 8192 and m["seed"] == 42, m
# Доля replay — предмет дельты: 50 %, и она объявлена обеими долями.
assert abs(m["mix_replay_ratio"] - 0.5) < 1e-9, m["mix_replay_ratio"]
assert abs(m["mix_domain_ratio"] - m["domain_chunks"] / m["chunks"]) < 1e-9
assert m["domain_chunks"] == m["replay_chunks"] == 3493, m
# Потолок источника назван, а не замолчан: без него «50 %» выглядит произволом.
assert m["source_ceiling"]["replay_chunks_available"] == 3493, m.get("source_ceiling")
for f in ("source", "license", "sha256", "seed"):
    assert f in c or f in m, f"нет обязательного поля C-010: {f}"
lic = c["license"]
assert lic.get("license_status") == "internal_only", lic.get("license_status")
assert lic.get("domain_license_unresolved") is True
assert "публикация" in lic.get("license_note", ""), "нет границы «внутреннее/публикация»"
# Самопроверка сборки обязана быть в карточке: пересборка v12r совпала с кэшем.
fc = c["fidelity_check"]
assert fc["tokens_equal"] is True and fc["pos_equal"] is True, fc
assert c["sha256"]["cpt_corpus_v12r_8192_qwen25.npy"] == fc["reference"]["sha256"], \
    "хеш кэша v12r в карточке разошёлся с тем, против чего сверялась сборка"
sys.exit(0)
PY
expect_exit 0 "хеши карточки v12r50 совпадают с файлами на сетевом диске" \
  python3 - <<'PY'
import hashlib, json, sys
from pathlib import Path
c = json.load(open("data/corpus-card-v12r50.json", encoding="utf-8"))
bad = []
for a in c["artifacts"]:
    p = Path(a["resolved"])
    if not p.is_file():
        bad.append(f"{a['path']}: файла нет"); continue
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    if h.hexdigest() != a["sha256"]:
        bad.append(f"{a['path']}: хеш разошёлся")
    if p.stat().st_size != a["bytes"]:
        bad.append(f"{a['path']}: размер разошёлся")
print("РАСХОЖДЕНИЯ:", bad)
sys.exit(1 if bad else 0)
PY
expect_exit 0 "существующие кэши v12r не изменены сборкой (AD-7, откат)" \
  python3 - <<'PY'
import hashlib, json, sys
from pathlib import Path
# Хеши v12r до сборки — из карточки v12r (закоммичена 14.09, до S3m): если они
# сходятся с файлами сейчас, сборка v12r50 чужие кэши не трогала.
old = json.load(open("data/corpus-card.json", encoding="utf-8"))
new = json.load(open("data/corpus-card-v12r50.json", encoding="utf-8"))
for name in ("cpt_corpus_v12r_8192_qwen25.npy",):
    assert old["sha256"][name] == new["sha256"][name], f"{name}: кэш v12r разошёлся"
ref = Path("/home/user/gb10-shared/datasets/tok/cpt_corpus_v12r_8192_qwen25.npy")
if ref.is_file():
    h = hashlib.sha256()
    with open(ref, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    assert h.hexdigest() == old["sha256"][ref.name], "кэш v12r на диске не тот, что в карточке"
sys.exit(0)
PY

echo "  --- 16f. mix_lr_grid_chain.sh: руки последовательно, отказ не размножается ---"
# Драйвер проверяется на фикстуре каталогов рук: цепочка руки подменена командой,
# которая пишет `var/chain.status`. Ни стенда, ни GPU, ни обучения — проверяется
# ровно то, ради чего драйвер существует: последовательность и стоп на отказе.
GRID_TMP="$TMP/grid"; mkdir -p "$GRID_TMP/calib"
mkarm() {  # $1=рука $2=статус, которым кончится её «цепочка»
  local arm="$1" st="$2" d="$GRID_TMP/calib/calib-$1-20260101-0000"
  mkdir -p "$d/logs" "$d/var" "$d/checkpoints"
  printf 'sleep 1; echo %s > %s/var/chain.status\n' "$st" "$d" > "$d/chain_command.txt"
}
mkarm 25-0.35 done
mkarm 50-0.7 done
expect_exit 0 "сетка из двух рук проходит последовательно и завершается done" \
  bash tools/mix_lr_grid_chain.sh --ts 20260101-0000 --arms "25-0.35 50-0.7" \
      --shared "$GRID_TMP" --poll 1 --arm-timeout 60
expect_exit 0 "в status.tsv есть строка на старт и на итог каждой руки" \
  grep -c "done" "$GRID_TMP/calib/grid-20260101-0000/status.tsv"
expect_contains "серия завершена: done" "итог сетки записан в grid.log" \
  cat "$GRID_TMP/calib/grid-20260101-0000/grid.log"
expect_exit 0 "порядок в status.tsv: старт и итог руки, и только потом следующая" \
  python3 - "$GRID_TMP" <<'PY'
import pathlib, sys
rows = [l.split("\t") for l in
        (pathlib.Path(sys.argv[1]) / "calib/grid-20260101-0000/status.tsv")
        .read_text(encoding="utf-8").splitlines() if "\t" in l]
seq = [(r[1], r[2]) for r in rows]
want = [("25-0.35", "start"), ("25-0.35", "done"),
        ("50-0.7", "start"), ("50-0.7", "done")]
assert seq == want, seq
sys.exit(0)
PY
GRID_TMP2="$TMP/grid-fail"; mkdir -p "$GRID_TMP2/calib"
mkarm2() {
  local arm="$1" st="$2" d="$GRID_TMP2/calib/calib-$1-20260101-0000"
  mkdir -p "$d/logs" "$d/var" "$d/checkpoints"
  printf 'sleep 1; echo %s > %s/var/chain.status\n' "$st" "$d" > "$d/chain_command.txt"
}
mkarm2 25-0.35 failed
mkarm2 50-0.7 done
expect_exit 1 "отказ руки останавливает сетку (exit 1), а не «идём дальше»" \
  bash tools/mix_lr_grid_chain.sh --ts 20260101-0000 --arms "25-0.35 50-0.7" \
      --shared "$GRID_TMP2" --poll 1 --arm-timeout 60
expect_exit 1 "следующая рука не запускалась: её строки в status.tsv нет" \
  grep -q "50-0.7" "$GRID_TMP2/calib/grid-20260101-0000/status.tsv"
expect_exit 0 "итог сетки помечен failed, а не done" \
  grep -q "^failed$" "$GRID_TMP2/calib/grid-20260101-0000/status"
GRID_TMP3="$TMP/grid-empty"; mkdir -p "$GRID_TMP3/calib/calib-25-0.35-20260101-0000"
expect_exit 2 "рука без chain_command.txt — NOT-VERIFIED, а не «пропустить и идти дальше»" \
  bash tools/mix_lr_grid_chain.sh --ts 20260101-0000 --arms "25-0.35" \
      --shared "$GRID_TMP3" --poll 1 --arm-timeout 60
expect_exit 2 "драйвер без --ts и --arms отвергается" \
  bash tools/mix_lr_grid_chain.sh --arms "25-0.35"
expect_exit 0 "план сетки печатается без стенда и без запуска" \
  bash tools/mix_lr_grid_chain.sh --ts 20260101-0000 --arms "25-0.35,50-0.7" \
      --shared "$GRID_TMP" --print-plan
expect_contains "50-0.7" "список рук принимается и через запятую" \
  bash tools/mix_lr_grid_chain.sh --ts 20260101-0000 --arms "25-0.35,50-0.7" \
      --shared "$GRID_TMP" --print-plan
expect_exit 0 "манифесты каталогов сетки и пробы проходят страж AD-2 (C-012), а не похожи на него" \
  python3 - "$TMP" <<'PY'
import argparse, json, pathlib, shutil, subprocess, sys, tempfile
sys.path.insert(0, "tools")
import run_mix_lr_calib as R
ts = "20260101-0000"
tmp = pathlib.Path(tempfile.mkdtemp(dir=sys.argv[1]))
args = argparse.Namespace(image="nvcr.io/nvidia/pytorch:26.07-py3-vllm")
try:
    # Каталог пробы: манифест собирается ИЗ отчёта пробы (хеши — оттуда же).
    d = tmp / f"calib-ppl-{ts}"; d.mkdir(parents=True)
    (d / "ppl.json").write_text(json.dumps({
        "complete": True, "pending_states": [], "measured_states": ["base"],
        "instrument": {"pipeline": f"runs/calib-25-0.7-{ts}/laguna_pipeline_calib.py",
                       "pipeline_sha256": "a" * 64, "device": "cuda", "dtype": "bfloat16",
                       "batch": 4, "max_len": 1024, "base_weights": "/abs/weights"},
        "sets": {"v1_general": {"path": "datasets/general_eval.txt", "sha256": "b" * 64,
                                "docs_kept": 24}}}), encoding="utf-8")
    R.write_probe_manifest(d, ts, ["25-0.7"], pathlib.Path("runs/x/p.py"), args, {"gaps": []})
    g = tmp / f"calib-grid-{ts}"; g.mkdir(parents=True)
    R.write_grid_manifest(g, ts, ["25-0.35"], args, {"25-0.35": "done"})
    runs = tmp / "runs"; runs.mkdir()
    shutil.move(str(d), str(runs / d.name)); shutil.move(str(g), str(runs / g.name))
    p = subprocess.run(["python3", "tools/check_run_manifest.py", "--runs", str(runs)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    man = json.loads((runs / g.name / "run_manifest.json").read_text(encoding="utf-8"))
    assert man["pipeline_complete"] is True, man
    assert man["stages"] == [{"name": "cpt:25-0.35", "status": "done"}], man["stages"]
    # Статус руки берётся из status.tsv драйвера: не «done» по умолчанию.
    R.write_grid_manifest(runs / g.name, ts, ["25-0.35"], args, {"25-0.35": "failed"})
    man2 = json.loads((runs / g.name / "run_manifest.json").read_text(encoding="utf-8"))
    assert man2["pipeline_complete"] is False, man2
    assert man2["stages"] == [{"name": "cpt:25-0.35", "status": "failed"}], man2["stages"]
finally:
    shutil.rmtree(tmp)
sys.exit(0)
PY
expect_exit 2 "раннер: --chain без --ts отвергается" \
  python3 tools/run_mix_lr_calib.py --chain --arms 25-0.35
expect_exit 2 "раннер: --grid-wait без --ts отвергается" \
  python3 tools/run_mix_lr_calib.py --grid-wait
expect_contains "--chain" "в плане названа автономная сетка, а не только поштучный запуск" \
  python3 tools/run_mix_lr_calib.py --plan
expect_contains "--grid-fetch" "в плане названы шаги после прогона (ожидание, забор, проба, разбор)" \
  python3 tools/run_mix_lr_calib.py --plan
# Требование дельты после инцидента hr-40: цепочка не должна зависеть от раннера.
expect_contains "переживает раннер" "раннер называет причину автономного запуска" \
  python3 tools/run_mix_lr_calib.py --plan

echo "  --- 16g. calib_ppl_arms.py: свод четырёх рук, потолок от базы, вердикт ---"
expect_exit 0 "план свода печатается без замеров" \
  python3 tools/calib_ppl_arms.py --plan --ts 20260916-0820
expect_contains "v1_general" "в плане назван решающий набор критерия ADR-022" \
  python3 tools/calib_ppl_arms.py --plan --ts 20260916-0820
expect_exit 0 "чистые функции свода: повтор, эффект фактора, кэш хешей, загрузчик" \
  python3 - <<'PY'
import hashlib, os, pathlib, sys, tempfile
sys.path.insert(0, "tools")
import calib_ppl_arms as A

# Зацикливание ловится окном, а не строкой: у этих моделей повтор приходит ОДНОЙ
# строкой («Господа, вы готовите хлеб. Господа, вы готовите хлеб. …»), и построчный
# счётчик его не видит — на этом первая версия признака и спотыкалась.
loop = "Приготовить домашний хлеб. " * 12
coherent = ("Закваска требует муки и воды в равных долях. " * 1 +
            "Футбольная тактика изменилась: прессинг и быстрые переходы. " +
            "История железных дорог началась в Англии в девятнадцатом веке.")
assert A.max_window_repeat(loop) >= 5, A.max_window_repeat(loop)
assert A.max_window_repeat(coherent) < A.REPEAT_THRESHOLD, A.max_window_repeat(coherent)

rows = [{"arm": "a-0.35", "ratio_vs_base": 2.0}, {"arm": "a-0.7", "ratio_vs_base": 4.0},
        {"arm": "b-0.35", "ratio_vs_base": 2.0}, {"arm": "b-0.7", "ratio_vs_base": 4.0}]
assert abs(A.factor_effect(rows, [("a-0.35", "a-0.7"), ("b-0.35", "b-0.7")]) - 2.0) < 1e-12
assert abs(A.factor_effect(rows, [("a-0.35", "b-0.35"), ("a-0.7", "b-0.7")]) - 1.0) < 1e-12
assert A.factor_effect(rows, [("нет", "нет2")]) is None, "отсутствующая пара дала число"

# Кэша хешей нет намеренно: кэш по метаданным (размер + метка времени) мог бы молча
# вернуть хеш прежнего файла. Здесь проверяется и то, что хеш честный, и то, что
# метаданные содержимое НЕ определяют — иначе запрет кэша был бы мифом.
#
# Признак — устойчивый, а не «совпадение тика часов ФС». Прежняя редакция требовала,
# чтобы две записи подряд получили ОДИН тик: это свойство НАГРУЗКИ, а не прибора —
# под флотом запись пересекает границу coarse-часов (Δ ≈ 1 мс), и ассерт падал,
# проходя изолированно; красное, зависящее от загрузки машины, сигналом не является
# (ADR-023 п.12). Совпадение метаданных теперь создаётся ПО ПОСТРОЕНИЮ (os.utime —
# mtime назначается), и проверяемое свойство сохраняется буквально: два РАЗНЫХ
# содержимых несут одинаковые (размер, mtime), значит кэш по ним вернул бы чужой хеш.
# ctime в признак не берётся: он не назначается, и строить на нём доказательство
# значило бы вернуться к «повезло с тиком».
tmp = pathlib.Path(tempfile.mkdtemp())
f = tmp / "c.pt"; f.write_bytes(b"x" * 1024)
h1 = A.sha256_file(f)
assert h1 == hashlib.sha256(b"x" * 1024).hexdigest(), "хеш не от содержимого файла"
st1 = f.stat()
f.write_bytes(b"y" * 1024)
os.utime(f, ns=(st1.st_mtime_ns, st1.st_mtime_ns))  # метаданные — как до перезаписи
st2 = f.stat()
h2 = A.sha256_file(f)
assert h2 != h1, "перезапись не изменила хеш"
# Кэш, каким он был бы в коде: ключ — метаданные, значение — хеш. Первый прогон
# кладёт h1 под ключ (размер, mtime); второй прогон видит ТОТ ЖЕ ключ, потому что
# метаданные совпали, — и кэш отдаёт чужой хеш для другого содержимого. Это и есть
# проверяемое свойство: кэш по метаданным некорректен (не «повезло с тиком» —
# совпадение создано по построению, os.utime).
cache = {(st1.st_size, st1.st_mtime_ns): h1}
key = (st2.st_size, st2.st_mtime_ns)
assert key in cache, "ключ кэша не совпал — совпадение метаданных не воспроизведено"
assert cache[key] != h2, "кэш по метаданным отдал верный хеш — обоснование устарело"
assert "Кэша хешей чекпойнтов здесь нет" in pathlib.Path(
    "tools/calib_ppl_arms.py").read_text(encoding="utf-8"), "решение о кэше не названо в коде"

# Загрузчик сравнивается по исходнику: копии пайплайнов рук отличаются вставками
# раннера, и «файлы не совпали» — не то же самое, что «грузят по-разному».
p1 = tmp / "p1.py"; p2 = tmp / "p2.py"
body = ("def _load_ckpt_with_resize(model, state, log_prefix=''):\n"
        "    return False\n")
p1.write_text("import os\n" + body, encoding="utf-8")
p2.write_text("import sys\n# вставка раннера\n" + body + "\ndef _calib_trace():\n    pass\n",
              encoding="utf-8")
d1, d2 = A.loader_digest(p1), A.loader_digest(p2)
assert d1["sha256"] == d2["sha256"], (d1, d2)
(tmp / "p3.py").write_text("def other():\n    pass\n", encoding="utf-8")
assert "error" in A.loader_digest(tmp / "p3.py"), "отсутствие функции не названо отказом"

# База, разошедшаяся между отчётами, — отказ: свод по «среднему из приборов» молча
# выдал бы число, которого не мерил ни один прогон.
rep = lambda p: {"complete": True, "checks": [], "states": {"base": {"sets": {"v1_general": {"ppl": p}}}}}
try:
    A.base_ppl({"a": rep(10.0), "b": rep(10.5)})
except ValueError:
    pass
else:
    raise AssertionError("база, разошедшаяся между отчётами, принята молча")
assert A.base_ppl({"a": rep(10.0), "b": rep(10.0)})["v1_general"] == 10.0

# Неполный отчёт руки — не доказательство, и проверка прибора не-ok тоже.
bad = {"a": {"complete": False, "checks": []}}
assert A.require_complete(bad), "неполный отчёт принят"
bad2 = {"a": {"complete": True, "checks": [{"name": "base_vs_s3h", "verdict": "расхождение"}]}}
assert A.require_complete(bad2), "не-ok проверка прибора принята"
ok = {"a": {"complete": True, "checks": [{"name": "base_vs_s3h", "verdict": "ok"}]}}
assert A.require_complete(ok) == [], A.require_complete(ok)
sys.exit(0)
PY
expect_exit 0 "фикстура: свод считает потолок от базы и вершит по нему (обе ветки)" \
  python3 - "$TMP" <<'PY'
import json, pathlib, subprocess, sys, tempfile
tmp = pathlib.Path(tempfile.mkdtemp(dir=sys.argv[1]))
ts = "20260101-0000"
arms = ["25-0.35", "25-0.7", "50-0.35", "50-0.7"]

def fixture(name, base, ppl):
    """Корень кейса-фикстуры: эталон S3h, отчёты пробы, параметры рук, чекпойнты."""
    root = tmp / name
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "ppl-baseline-v1v2.json").write_text(json.dumps(
        {"ppl": {k: {"ppl": v} for k, v in (("v1_general", base), ("v1_domain", 9.0),
                                            ("v2_general", 10.0), ("v2_domain", 11.0))}}),
        encoding="utf-8")
    d = root / "runs" / f"calib-ppl-{ts}"; d.mkdir(parents=True)
    for arm in arms:
        rp = root / "runs" / f"calib-{arm}-{ts}"; rp.mkdir(parents=True)
        (rp / "calib_params.json").write_text(json.dumps(
            {"replay_share_pct": int(arm.split("-")[0]),
             "peak_lr_scale": float(arm.split("-")[1])}), encoding="utf-8")
        ck = rp / "checkpoints" / "checkpoint_final.pt"
        ck.parent.mkdir(parents=True); ck.write_bytes(b"ckpt-" + arm.encode())
        pipe = rp / "laguna_pipeline_calib.py"
        pipe.write_text("def _load_ckpt_with_resize(m, s, log_prefix=''):\n    return False\n",
                        encoding="utf-8")
        sets = {k: {"ppl": v, "docs_kept": 24, "tokens": 3682} for k, v in ppl.items()}
        base_sets = {k: {"ppl": v, "docs_kept": 24, "tokens": 3682}
                     for k, v in (("v1_general", base), ("v1_domain", 9.0),
                                  ("v2_general", 10.0), ("v2_domain", 11.0))}
        (d / f"ppl-{arm}.json").write_text(json.dumps({
            "schema": "calib-ppl-probe/1", "complete": True, "pending_states": [],
            "case_root": str(root),
            "checks": [{"name": "base_vs_s3h", "verdict": "ok", "detail": "фикстура"},
                       {"name": "base_restored", "verdict": "ok", "detail": "фикстура"}],
            "instrument": {"pipeline": f"runs/calib-{arm}-{ts}/laguna_pipeline_calib.py",
                           "pipeline_sha256": "a" * 64, "device": "cpu", "dtype": "bfloat16",
                           "batch": 4, "max_len": 1024, "tokenizer_setup": "pipeline",
                           "base_weights": "/abs/weights"},
            "sets": {k: {"path": f"datasets/{k}.txt", "sha256": "b" * 64, "docs_kept": 24}
                     for k in base_sets},
            "states": {"base": {"load": {"kind": "base"}, "sets": base_sets},
                       "final": {"load": {"kind": "ckpt", "checkpoint": str(ck)},
                                 "sets": sets}},
        }, ensure_ascii=False), encoding="utf-8")
    return root

def run(root, out):
    return subprocess.run(
        ["python3", "tools/calib_ppl_arms.py", "--ts", ts, "--case-root", str(root),
         "--shared", str(root / "нет-такого-диска"), "--out", str(out),
         "--no-replay-overlap", "--no-manifest"],
        capture_output=True, text=True)

# Ветка «ни одна рука не проходит»: все четыре выше 2× базы (20.0).
all_fail = fixture("all_fail", 10.0, {"v1_general": 30.0, "v1_domain": 4.0,
                                      "v2_general": 12.0, "v2_domain": 5.0})
out1 = tmp / "e1.json"
p = run(all_fail, out1)
assert p.returncode == 0, p.stdout + p.stderr
e = json.loads(out1.read_text(encoding="utf-8"))
assert e["baseline_ppl"] == 10.0, e["baseline_ppl"]
assert abs(e["ceiling"] - 20.0) < 1e-12, e["ceiling"]
assert e["verdict"]["recommended_arm"] is None, e["verdict"]
assert e["verdict"]["arms_passing_ceiling"] == [], e["verdict"]
assert all(r["passes_ceiling"] is False for r in e["arms"]), e["arms"]
assert all(abs(r["ratio_vs_base"] - 3.0) < 1e-12 for r in e["arms"]), "отношение не к базе набора"
assert "20.00" in e["verdict"]["reason"], e["verdict"]["reason"]
# Хеш чекпойнта в evidence — хеш файла, а не поле из отчёта: отчёт его не несёт,
# и без счёта по файлу «хеш чекпойнта» был бы украшением.
import hashlib
for r in e["arms"]:
    want = hashlib.sha256(b"ckpt-" + r["arm"].encode()).hexdigest()
    assert r["checkpoint_sha256"] == want, (r["arm"], r["checkpoint_sha256"])

# Ветка «проходит одна рука»: потолок не поднят, а именно посчитан от базы.
one_pass = fixture("one_pass", 10.0, {"v1_general": 30.0, "v1_domain": 4.0,
                                      "v2_general": 12.0, "v2_domain": 5.0})
dd = one_pass / "runs" / f"calib-ppl-{ts}"
for f in dd.glob("ppl-*.json"):
    j = json.loads(f.read_text(encoding="utf-8"))
    if "25-0.35" in f.name:
        j["states"]["final"]["sets"]["v1_general"]["ppl"] = 19.0
    f.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
out2 = tmp / "e2.json"
p = run(one_pass, out2)
assert p.returncode == 0, p.stdout + p.stderr
e2 = json.loads(out2.read_text(encoding="utf-8"))
assert e2["verdict"]["recommended_arm"] == "25-0.35", e2["verdict"]
assert e2["verdict"]["arms_passing_ceiling"] == ["25-0.35"], e2["verdict"]

# База, не совпавшая с эталоном S3h, — отказ: свод считался бы по другому прибору.
bad = fixture("bad_base", 10.0, {"v1_general": 30.0, "v1_domain": 4.0,
                                 "v2_general": 12.0, "v2_domain": 5.0})
f = bad / "runs" / f"calib-ppl-{ts}" / "ppl-25-0.35.json"
j = json.loads(f.read_text(encoding="utf-8"))
j["states"]["base"]["sets"]["v1_general"]["ppl"] = 11.0
f.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
assert run(bad, tmp / "e3.json").returncode == 1, "база, разошедшаяся с эталоном, принята"

# Нет отчёта руки — NOT-VERIFIED, а не «свод по трём».
missing = fixture("missing", 10.0, {"v1_general": 30.0, "v1_domain": 4.0,
                                    "v2_general": 12.0, "v2_domain": 5.0})
(missing / "runs" / f"calib-ppl-{ts}" / "ppl-50-0.7.json").unlink()
assert run(missing, tmp / "e4.json").returncode == 2, "пропавший отчёт не назван отказом"
sys.exit(0)
PY

echo "== 16. passrate_probe.py (S3j: baseline pass-rate пула v2) =="
# Проба измеряет решаемость задач, а не обучает: модель здесь не нужна — проверяются
# отбор, отказы, арифметика сводки и то, что семантика контура берётся импортом из
# пайплайна, а не копией (копия разошлась бы с контуром молча).
PR="$TMP/pr"
mkdir -p "$PR"
python3 - "$PR/pool.jsonl" <<'PY'
import json, sys
rows = []
for i in range(250):
    rows.append({"task_type": "find_concept", "prompt": f"Формула {i}", "expected_slugs": [f"slug_{i}"]})
for i in range(120):
    rows.append({"task_type": "explain_relation", "prompt": f"Связь {i}", "keywords": [f"k{i}", "j"]})
for i in range(5):
    rows.append({"task_type": "chain_reasoning", "prompt": f"Цепочка {i}", "expected_slugs": [f"c{i}"]})
with open(sys.argv[1], "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
PY

expect_exit 0 "пул: стратификация по типам, не больше per-type, хвост берётся целиком" \
  python3 - "$PR/pool.jsonl" <<'PY'
import json, subprocess, sys
out = subprocess.run([sys.executable, "tools/passrate_probe.py", "--pool", sys.argv[1],
                      "--per-type", "200", "--seed", "42", "--dry-run"],
                     capture_output=True, text=True)
assert out.returncode == 0, out.stderr
d = json.loads(out.stdout)
strata = {k: (v["in_pool"], v["taken"]) for k, v in d["strata"].items()}
assert strata == {"chain_reasoning": (5, 5), "explain_relation": (120, 120),
                  "find_concept": (250, 200)}, strata
assert d["n_taken"] == 325, d["n_taken"]
assert d["pool"]["lines"] == 375, d["pool"]["lines"]
sys.exit(0)
PY
expect_exit 0 "отбор детерминирован сидом и зависит от него" \
  python3 - "$PR/pool.jsonl" <<'PY'
import json, subprocess, sys
def run(seed):
    out = subprocess.run([sys.executable, "tools/passrate_probe.py", "--pool", sys.argv[1],
                          "--per-type", "50", "--seed", str(seed), "--dry-run"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["strata"]
a, b, c = run(42), run(42), run(7)
assert a == b, "один сид дал разный отбор"
assert a["find_concept"]["taken"] == c["find_concept"]["taken"] == 50
sys.exit(0)
PY
expect_exit 0 "sha256 читаемого пула попадает в отчёт (пул переписывается параллельно)" \
  python3 - "$PR/pool.jsonl" <<'PY'
import hashlib, json, subprocess, sys
raw = open(sys.argv[1], "rb").read()
out = subprocess.run([sys.executable, "tools/passrate_probe.py", "--pool", sys.argv[1],
                      "--per-type", "1", "--dry-run"], capture_output=True, text=True)
d = json.loads(out.stdout)
assert d["pool"]["sha256"] == hashlib.sha256(raw).hexdigest(), d["pool"]["sha256"]
assert d["pool"]["sha256"][:12] == hashlib.sha256(raw).hexdigest()[:12]
sys.exit(0)
PY
expect_exit 1 "пул не тот, что ожидался → отказ, а не измерение чужого файла" \
  python3 tools/passrate_probe.py --pool "$PR/pool.jsonl" --dry-run \
    --expect-pool-sha256 "$(printf '0%.0s' $(seq 64))"
expect_contains "!= ожидаемого" "отказ называет фактический и ожидаемый sha256" \
  python3 tools/passrate_probe.py --pool "$PR/pool.jsonl" --dry-run \
    --expect-pool-sha256 "$(printf '0%.0s' $(seq 64))"
expect_exit 0 "ожидаемый sha256 совпал → проба идёт" \
  python3 tools/passrate_probe.py --pool "$PR/pool.jsonl" --dry-run \
    --expect-pool-sha256 "$(sha256sum "$PR/pool.jsonl" | cut -d' ' -f1)"
expect_exit 2 "нет пула → NOT-VERIFIED (не зелёный)" \
  python3 tools/passrate_probe.py --pool "$PR/нет-такого.jsonl" --dry-run
expect_exit 1 "без --run-dir проба не запускается (AD-2: прогон без каталога)" \
  python3 tools/passrate_probe.py --pool "$PR/pool.jsonl"

# Сырые записи и мета пробы — так, как их пишет сама проба (по задаче: тип, pass,
# ходы, tool_error, длина ответа). Сводка обязана воспроизводиться ИЗ НИХ.
python3 - "$PR/run" <<'PY'
import json, sys
from pathlib import Path
d = Path(sys.argv[1]); d.mkdir(parents=True, exist_ok=True)
recs = []
for i in range(10):
    recs.append({"task_index": i, "task_type": "find_concept", "source_env": "E3_classify",
                 "source_task_id": f"t{i}", "turns": 1 if i % 2 else 3, "tool_calls": i % 2,
                 "tool_errors": 1 if i == 0 else 0, "tool_error": i == 0, "hit_timeout": False,
                 "hit_context_guard": False, "answer_chars": 100 + i, "assistant_tokens": 50 + i,
                 "pass": 1 if i < 5 else 0, "reward": 1.0 if i < 5 else 0.0,
                 "gold_resolvable": True})
for i in range(10):
    recs.append({"task_index": 100 + i, "task_type": "explain_relation", "source_env": "E5_relate",
                 "source_task_id": f"e{i}", "turns": 1, "tool_calls": 1, "tool_errors": 0,
                 "tool_error": False, "hit_timeout": False, "hit_context_guard": False,
                 "answer_chars": 900, "assistant_tokens": 400, "pass": 1 if i == 0 else 0,
                 "reward": 1.0 if i == 0 else -0.05, "gold_resolvable": True})
with open(d / "tasks.jsonl", "w", encoding="utf-8") as f:
    for r in recs:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
(d / "probe_meta.json").write_text(json.dumps({
    "pool": {"path": "datasets/rl_tasks_revpool_v2.jsonl", "sha256": "ab" * 32, "lines": 8692},
    "strata": {"find_concept": {"in_pool": 8298, "taken": 10, "seed": 42},
               "explain_relation": {"in_pool": 394, "taken": 10, "seed": 42}},
    "protocol": {"n_attempts": 1, "pass_at_k": False, "max_turns": 15, "temperature": 1.0,
                 "top_k": 20, "toolcall_force": True, "weights": "qwen2.5-0.5b-base"},
}, ensure_ascii=False, indent=2), encoding="utf-8")
PY
expect_exit 0 "сводка из сырых записей: доля, медиана ходов, tool_error, полоса" \
  python3 - "$PR/run/tasks.jsonl" "$PR/summary.json" <<'PY'
import json, subprocess, sys
out = subprocess.run([sys.executable, "tools/passrate_probe.py", "--summarize", sys.argv[1],
                      "--evidence", sys.argv[2]], capture_output=True, text=True)
assert out.returncode == 0, out.stderr
ev = json.load(open(sys.argv[2], encoding="utf-8"))
p = ev["probe"]
assert p["n_taken"] == 20 and p["n_total"] == 8692, p["n_taken"]
assert p["overall"]["pass_rate"] == 0.3, p["overall"]           # 6 из 20
assert p["overall"]["dead_share"] == 0.7 and p["overall"]["trivial_share"] == 0.3
by = {r["task_type"]: r for r in p["by_type"]}
assert by["find_concept"]["median_turns"] == 2.0, by["find_concept"]["median_turns"]
assert by["explain_relation"]["median_turns"] == 1.0
assert by["find_concept"]["tool_error_share"] == 0.1, by["find_concept"]["tool_error_share"]
assert by["find_concept"]["pass_rate"] == 0.5 and by["explain_relation"]["pass_rate"] == 0.1
# полоса 0.25–0.80: find_concept внутри, explain_relation ниже
assert p["band"]["types_in_band"] == ["find_concept"], p["band"]
assert p["band"]["tasks_in_band_types"] == 10 and p["band"]["dead_tasks"] == 14
assert p["band"]["trivial_tasks"] == 6
# одна попытка на задачу — это не pass@k: сводка обязана это нести
assert p["n_attempts"] == 1 and ev["protocol"]["pass_at_k"] is False, ev["protocol"]
assert len(p["overall"]["pass_rate_ci95"]) == 2
sys.exit(0)
PY
expect_exit 0 "сводка считается из сырых записей, а не из готовых чисел" \
  python3 - "$PR/run" "$PR/summary2.json" <<'PY'
import json, subprocess, sys
from pathlib import Path
d = Path(sys.argv[1])
recs = [json.loads(l) for l in (d / "tasks.jsonl").open(encoding="utf-8")]
alt = d.parent / "run2"; alt.mkdir(exist_ok=True)
for r in recs:
    r["pass"] = 0                                  # ни одного прохода во всех типах
(alt / "tasks.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
                                 encoding="utf-8")
(alt / "probe_meta.json").write_text((d / "probe_meta.json").read_text(encoding="utf-8"),
                                     encoding="utf-8")
out = subprocess.run([sys.executable, "tools/passrate_probe.py", "--summarize",
                      str(alt / "tasks.jsonl"), "--evidence", sys.argv[2]],
                     capture_output=True, text=True)
assert out.returncode == 0, out.stderr
ev = json.load(open(sys.argv[2], encoding="utf-8"))
assert ev["probe"]["overall"]["pass_rate"] == 0.0, ev["probe"]["overall"]
assert ev["probe"]["band"]["types_in_band"] == [], ev["probe"]["band"]
assert ev["probe"]["band"]["dead_tasks"] == 20
sys.exit(0)
PY
expect_exit 2 "сводка из пустых записей → NOT-VERIFIED (не «ноль процентов»)" \
  python3 tools/passrate_probe.py --summarize "$PR/empty.jsonl"
expect_exit 2 "сводка из отсутствующего файла → NOT-VERIFIED" \
  python3 tools/passrate_probe.py --summarize "$PR/нет-записей.jsonl"
: > "$PR/empty.jsonl"

expect_exit 0 "верификатор достижим: 1 берётся, 0 при пустом ответе, tool_response не зачитывается" \
  python3 - <<'PY'
import json, os, sys
from pathlib import Path
pool, index = Path("datasets/rl_tasks_revpool_v2.jsonl"), Path("datasets/concepts_search_index.jsonl")
if not pool.is_file() or not index.is_file() or not Path("laguna_pipeline_v8.py").is_file():
    print("SKIP: пул/индекс/пайплайн недоступны — недостижимость верификатора не сверить")
    sys.exit(0)
os.environ["CONCEPTS_INDEX"] = str(index)
sys.path.insert(0, ".")
import laguna_pipeline_v8 as pipe
rows = [json.loads(l) for l in pool.open(encoding="utf-8")]
by = {}
for r in rows:
    by.setdefault(r["task_type"], []).append(r)
fc, er = by["find_concept"][0], by["explain_relation"][0]
# slug-ветка: ответ, называющий концепт, проходит; пустой — нет
assert pipe.verify_task("slug " + fc["expected_slugs"][0], fc) is True
assert pipe.verify_task("не знаю", fc) is False
# keyword-ветка: слова из ОПРЕДЕЛЕНИЙ обоих концептов проходят, пустой — нет
d = pipe._ensure_def_index()
words = " ".join(" ".join(pipe._sig_words(d.get(k, ""))[:4]) for k in er["keywords"])
assert pipe.verify_task("Связь: " + words, er) is True, "gold-ответ не проходит keyword-ветку"
assert pipe.verify_task("не знаю", er) is False
# те же слова, но подложенные инструментом, НЕ зачитываются: слова модели != слова среды
assert pipe.verify_task("<tool_response>\n" + words + "\n</tool_response>", er) is False
sys.exit(0)
PY
expect_exit 0 "семантика контура — импортом из пайплайна, а не копией" \
  python3 - <<'PY'
import re, sys
from pathlib import Path
tool = Path("tools/passrate_probe.py").read_text(encoding="utf-8")
for name in ("verify_task", "search_concepts", "execute_tool_call", "compute_reward",
             "keyword_coverage", "UNIFIED_SYSTEM_PROMPT"):
    assert f"def {name}" not in tool, f"{name}: в пробе появилась своя копия — разойдётся с контуром"
assert "import laguna_pipeline_v8 as pipe" in tool, "проба не импортирует пайплайн"
for call in ("pipe.verify_task(", "pipe.execute_tool_call(", "pipe.compute_reward(",
             "pipe._load_concepts_index()"):
    assert call in tool, f"нет вызова {call}"
# стоп-строки хода должны совпадать с пайплайном дословно
pipeline = Path("laguna_pipeline_v8.py").read_text(encoding="utf-8")
m = re.search(r'stop=\[("[^"]+", "[^"]+")\]', pipeline)
assert m, "в пайплайне не найдена строка stop=[...] цикла RL"
assert re.findall(r'"([^"]+)"', m.group(1)) == ["</tool_call>", "<|im_end|>"], m.group(1)
assert 'STOP_STRINGS = ["</tool_call>", "<|im_end|>"]' in tool, "стоп-строки разошлись с пайплайном"
assert "n_attempts" in tool and "pass_at_k" in tool, "проба не помечает одну попытку"
sys.exit(0)
PY
expect_exit 0 "проба не пишет в пул и в сетевой диск (AD-4: чтение)" \
  python3 - <<'PY'
import sys
from pathlib import Path
tool = Path("tools/passrate_probe.py").read_text(encoding="utf-8")
for bad in ("shutil.copy", "pool.write", "open(str(pool), \"w\""):
    assert bad not in tool, f"проба пишет в пул: {bad}"
assert "gb10-shared" in tool and "models-store" in tool  # веса читаются по месту, не копируются
sys.exit(0)
PY

echo
echo "== 16. run_flex_check.py / flex_ppl_probe.py (S3k: диагностика flex-маски, ADR-022 п.1) =="

# ── план: обе руки названы, и названо ровно одно различие между ними ──────────
expect_exit 0 "run_flex_check --plan" python3 tools/run_flex_check.py --plan
expect_contains "LAGUNA_ATTN=flex" "план: рука flex" python3 tools/run_flex_check.py --plan
expect_contains "LAGUNA_ATTN=none" "план: штатная рука" python3 tools/run_flex_check.py --plan
expect_contains "не правится" "план: рабочий пайплайн не трогается" python3 tools/run_flex_check.py --plan
expect_contains "loss_trace.jsonl" "план: траектория по шагам как основание" python3 tools/run_flex_check.py --plan

# ── недоступный стенд: предусловие — отказ, а не «пропущено» ──────────────────
expect_exit 1 "--preflight на недоступном стенде" python3 tools/run_flex_check.py --preflight --host no-such-host.invalid

# ── проба PPL: план и отказы на отсутствующих входах (NOT-VERIFIED = 2) ───────
expect_exit 0 "flex_ppl_probe --plan" python3 tools/flex_ppl_probe.py --plan
expect_exit 2 "проба без --pipeline" python3 tools/flex_ppl_probe.py \
  --set "x=$TMP/ppl_set.txt" --state base=base --out "$TMP/ppl_never.json"
expect_exit 2 "проба с несуществующим пайплайном" python3 tools/flex_ppl_probe.py \
  --pipeline "$TMP/нет-такого.py" --set "x=$TMP/ppl_set.txt" --state base=base --out "$TMP/ppl_never.json"
expect_exit 2 "проба с несуществующим набором" python3 tools/flex_ppl_probe.py \
  --pipeline tools/laguna_pipeline_v8.py --set "x=$TMP/нет-такого.txt" --state base=base --out "$TMP/ppl_never.json"

if python3 - <<'PY'
import ast, json, math, sys, tempfile
from pathlib import Path

sys.path.insert(0, "tools")
import run_flex_check as R
import flex_ppl_probe as P

# ── 1. патч копии пайплайна ───────────────────────────────────────────────────
# Якоря обязаны применяться ровно на свои места, а их исчезновение — быть отказом,
# а не тихой вставкой не туда: иначе траектория окажется от чужого шага и вердикт
# по ADR-022 п.4 будет основан на подменённых числах.
src = R.PIPELINE_CASE.read_text(encoding="utf-8")
patched, rec = R.patched_pipeline(src)
assert rec["base_sha256"] == "8fe7ac7bb57e22a3ea8f9d6d75ef479e2169212abdd2c45e16cd0d3aecf7621d", \
    f"база патча не совпала с контурным пайплайном: {rec['base_sha256']}"
assert rec["patched_sha256"] != rec["base_sha256"]
assert patched.count("_flexcheck_trace") == 2, "хелпер должен быть объявлен и вызван по одному разу"
assert patched.index("def _flexcheck_trace") < patched.index('if __name__ == "__main__":'), \
    "хелпер объявлен после точки входа — к моменту вызова его не существует"
compile(patched, "patched_pipeline", "exec")
assert patched.count("FLEX_CHECK_CKPT_EVERY") >= 1
for broken, tag in ((src.replace(R.ANCHOR_LOSS, "# вырезано"), "loss"),
                    (src.replace(R.ANCHOR_CKPT, "# вырезано"), "ckpt"),
                    (src.replace(R.ANCHOR_MAIN, "# вырезано"), "main")):
    try:
        R.patched_pipeline(broken)
    except SystemExit as e:
        assert tag in str(e), (tag, str(e))
    else:
        raise AssertionError(f"патч «применился» при отсутствующем якоре «{tag}»")


# ── 1b. патч не переписывает тело исходной ветки ──────────────────────────────
# Дефект первого прогона S3k: `elif` встал ПЕРЕД ретенцией чекпойнтов, ретенция
# (строки того же отступа) стала телом новой ветки, и на шаге 150 она снесла
# `checkpoint_50.pt` — то есть патч не добавил точку, а съел чужую. Проверка
# структурная (AST), а не текстовая: отступы здесь и есть семантика.
def _ckpt_if(text):
    """Узел `if step % 200 == 0` внутри `run_cpt` — ровно один."""
    tree = ast.parse(text)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_cpt")
    nodes = [n for n in ast.walk(fn)
             if isinstance(n, ast.If) and "step % 200" in ast.unparse(n.test)]
    assert len(nodes) == 1, f"ветка сохранения чекпойнта не найдена однозначно: {len(nodes)}"
    return nodes[0]


def _structure(text):
    node = _ckpt_if(text)
    return ([type(s).__name__ for s in node.body],
            [ast.unparse(s) for s in node.body],
            [type(s).__name__ for s in node.orelse])


orig_struct = _structure(src)
patched_struct = _structure(patched)
assert orig_struct[0] == patched_struct[0], \
    "патч изменил состав тела исходной ветки сохранения — ретенция уехала в новую ветку"
assert orig_struct[1] == patched_struct[1], "патч переписал строки исходной ветки"
assert "unlink" in " ".join(patched_struct[1]), "ретенция обязана остаться в исходной ветке"
assert orig_struct[2] == [], "исходная ветка не имеет elif — иначе якорь выбран неверно"
assert patched_struct[2] == ["If"], "новая ветка обязана быть `elif`, а не вторым `if`"
_new = _ckpt_if(patched).orelse[0]
assert "_fc_every" in ast.unparse(_new.test), ast.unparse(_new.test)
assert "FLEX_CHECK_CKPT_EVERY" in patched, "шаг номерного чекпойнта обязан читаться из переменной"
assert "keep_last=0" in ast.unparse(_new), "номерной чекпойнт обязан писаться без ретенции"

# ...и что проверка не вакуумна. Воспроизводим ровно дефектную редакцию: `elif`
# вставлен сразу после строки сохранения, то есть ПЕРЕД ретенцией — как это и было
# в первом прогоне. Структура обязана это увидеть.
_save_line = ('                save_checkpoint_atomic(model, optimizer, '
              'Path(args.ckpt_dir)/f"checkpoint_{step}.pt")\n')
assert src.count(_save_line) == 1, "строка сохранения не уникальна — контрольный пример недостоверен"
_half = src.replace(_save_line, _save_line + '            elif True:\n                pass\n', 1)
_broken = _structure(_half)
assert _broken[0] == ["Expr"], f"контрольный пример не воспроизвёл дефект: {_broken[0]}"
assert _broken[0] != orig_struct[0], "проверка структуры обязана отвергать дефектную редакцию"

# ── 2. таблица стадий: одна стадия CPT на 200 шагов, batch 1 ──────────────────
fields = R.stages_tsv("a", "20260101-0000").strip().split("\t")
assert len(fields) == 8, fields
assert fields[:3] == ["cpt", "cpt", "cpt"] and fields[4] == "200" and fields[5] == "1", fields
assert fields[6] == "-" and fields[7] == "flex-check-a-20260101-0000", fields

# ── 3. тренд по третям: направление и порог «плоско» ──────────────────────────
assert R.thirds([float(x) for x in range(100, 0, -1)])["direction"] == "убывает"
assert R.thirds([float(x) for x in range(100)])["direction"] == "растёт"
flat = [1.0 + (0.001 if i % 2 else -0.001) for i in range(60)]
assert R.thirds(flat)["direction"] == "плоско", R.thirds(flat)
assert R.thirds([1.0, 2.0])["available"] is False

# ── 4. попарное сравнение рук: одинаковые траектории — в разбросе, сдвиг — нет ─
noise = [0.001 * ((i * 7) % 5) for i in range(60)]
ta = [{"step": i, "loss": 2.0 + noise[i]} for i in range(60)]
tb = [{"step": i, "loss": 2.0 + noise[i]} for i in range(60)]
d = R.paired_delta(ta, tb)
assert d["available"] and d["within_noise_2se"] and d["mean_loss_a_minus_b"] == 0.0, d
tb2 = [{"step": i, "loss": 2.5 + noise[i]} for i in range(60)]
d2 = R.paired_delta(ta, tb2)
assert d2["within_noise_2se"] is False, d2
# ...и это ровно тот случай, ради которого сдвиг уровня отделён от формы: у рук
# разные уровни (маска запрещает кросс-документное внимание внутри чанка), но
# кривые одной формы — вердикт «траектории совпадают побитово» был бы ложью,
# а «траектории различаются по существу» — тоже.
assert d2["shape_agrees"] is True and abs(d2["correlation"] - 1.0) < 1e-9, d2
assert abs(d2["mean_loss_a_minus_b"] + 0.5) < 0.2, d2
assert d2["first_step"] == {"a": 2.0, "b": 2.5}, d2["first_step"]
# разная форма — не разный уровень
tb3 = [{"step": i, "loss": (3.0 if i % 2 else 1.0)} for i in range(60)]
assert R.paired_delta(ta, tb3)["shape_agrees"] is False, R.paired_delta(ta, tb3)
assert R.pearson([1.0, 2.0], [1.0, 2.0]) is None  # мало точек — не коэффициент
assert R.paired_delta(ta[:3], tb[:3])["available"] is False  # мало общих шагов — не судим

# ── 5. чтение траектории: мусорные строки не ломают разбор ────────────────────
tmp = Path(tempfile.mkdtemp())
tr = tmp / "loss_trace.jsonl"
tr.write_text('{"step": 1, "loss": 2.0, "lr": 1e-4, "t": 100.0}\n'
              'не json\n'
              '{"step": 0, "loss": 3.0, "lr": 0.0, "t": 97.5}\n'
              '{"step": 2, "loss": 1.5, "lr": 2e-4, "t": 102.6}\n'
              '{"нет": "шага"}\n', encoding="utf-8")
rows = R.read_loss_trace(tr)
assert [r["step"] for r in rows] == [0, 1, 2], rows
assert R.read_loss_trace(tmp / "нет-такого.jsonl") == []
ss = R.step_seconds(rows)
assert ss["available"] and abs(ss["median_sec"] - 2.55) < 0.2, ss

# ── 6. разбор лога стадии: GEN-EVAL, режим внимания, предупреждения о маске ───
log = tmp / "cpt.log"
log.write_text(
    "04:37:44 [INFO] ATTN: flex_docmask — диагональное маскирование документов из position_ids\n"
    "04:37:45 [INFO] CPT: N=0.49B, peak_lr=3.50e-04 (x0.7), steps=200\n"
    "04:37:56 [INFO] CPT step 0/200 | loss=2.0989 | lr=0.00e+00 | tok/s=1389\n"
    "04:38:20 [INFO] CPT step 50/200 | loss=3.0162 | lr=3.50e-04 | tok/s=3321\n"
    "04:39:00 [INFO] GEN-EVAL step 50: ppl_general=21.9 (+0.0% vs base), ppl_domain=7.6 (+0.0%)\n"
    "04:40:00 [WARNING] flex_docmask: маска не построена — position_ids отсутствуют\n"
    "04:41:00 [ERROR] CUDA out of memory\n", encoding="utf-8")
li = R.parse_cpt_log(log)
assert li["gen_eval"] == [{"step": 50, "ppl_general": 21.9, "ppl_domain": 7.6}], li["gen_eval"]
assert li["peak_lr"] == 3.5e-04, li["peak_lr"]
assert [s["step"] for s in li["steps_logged"]] == [0, 50], li["steps_logged"]
assert any("flex_docmask" in l for l in li["flex_lines"]), li["flex_lines"]
assert len(li["mask_warnings"]) == 1 and "маска" in li["mask_warnings"][0], li["mask_warnings"]
assert len(li["errors"]) == 1, li["errors"]

# ── 7. вердикт: три исхода — «flex не причина», «flex причина», «нет PPL» ──────
def ppl_fixture(x_a, x_b, v2_a=1.0, v2_b=1.0):
    def st(v1, v2):
        return {"sets": {"v1_general": {"ppl_corpus": v1}, "v2_general": {"ppl_corpus": v2}}}
    return {"states": {"base": st(12.0, 11.0),
                       f"a_{R.CPT_STEPS}": st(12.0 * x_a, 11.0 * v2_a),
                       f"b_{R.CPT_STEPS}": st(12.0 * x_b, 11.0 * v2_b)}}

v = R.verdict_of(ta, tb, ppl_fixture(8.0, 8.0))
assert v["code"] == "flex_not_cause", v
assert v["trajectories_identical"] is True, v
assert abs(v["flex_vs_stock_x"] - 1.0) < 1e-9, v
assert v["ceiling_exceeded_by"] == ["a_flex", "b_stock"], v

v = R.verdict_of(ta, tb, ppl_fixture(16.0, 2.0))
assert v["code"] == "flex_is_cause", v
assert abs(v["flex_vs_stock_x"] - 8.0) < 1e-9, v
assert v["ceiling_exceeded_by"] == ["a_flex"], v

v = R.verdict_of(ta, tb, {"states": {"base": {"sets": {}}}})
assert v["code"] == "ppl_missing", v
assert "вердикт" in v["text"] or "PPL" in v["text"], v

# ── 7b. снимок «запрещённое не тронуто»: расхождение ловится по каждому полю ───
base_snap = {"pipeline_v8_sha256": "a", "pilot_checkpoints": {"x": "b"},
             "v12_3b_s42_tree_sha256": "c", "v12_3b_s42_files": 3,
             "platform_containers": ["llm-platform-a"]}
same = dict(base_snap, diagnostic_containers_running=[])
c = R.compare_untouched(base_snap, same)
assert c["identical"] is True and c["differences"] == [], c
assert set(c["paths"]) == set(R.UNTOUCHABLE), c["paths"]
for field in ("pipeline_v8_sha256", "pilot_checkpoints", "v12_3b_s42_tree_sha256", "v12_3b_s42_files"):
    broken = dict(same)
    broken[field] = "изменено"
    c = R.compare_untouched(base_snap, broken)
    assert c["identical"] is False and c["differences"][0]["field"] == field, (field, c)
# отсутствие снимков — «не проверено», а не «всё хорошо»
ni = R._non_interference("19990101-0000")
assert ni["checked"] is False and "why" in ni, ni

# ── 8. методика пробы повторяет _ppl_eval, а не «похожа на неё» ───────────────
# Разбор набора и пороги отсева проверяются на данных, для которых _ppl_eval
# требует GPU: локальная машина его имеет. Без CUDA проба не идёт вовсе
# (NOT-VERIFIED), поэтому тест здесь — вакуумно зелёный, с явной причиной.
try:
    import torch
except ImportError:
    print("  ok   методика пробы: SKIP — нет torch")
    sys.exit(0)
if not torch.cuda.is_available():
    print("  ok   методика пробы: SKIP — нет CUDA (проба сама даёт NOT-VERIFIED)")
    sys.exit(0)

VOCAB, BIAS, MAXLEN, BATCH = 8, 1.5, 64, 2


class _FakeTok:
    pad_token_id = 0

    def encode(self, text, add_special_tokens=False):
        return [1 + (ord(c) % (VOCAB - 1)) for c in text]


class _FakeModel:
    """Логиты детерминированы: bias стоит на истинном следующем токене, иначе 0.

    Тогда nll каждого токена равен log((V-1)+e^bias) − bias, и PPL набора
    известна в замкнутой форме — сверять агрегации можно с числом, а не друг с
    другом.
    """

    def __call__(self, input_ids=None, attention_mask=None):
        B, L = input_ids.shape
        logits = torch.zeros((B, L, VOCAB), device=input_ids.device, dtype=torch.float32)
        nxt = torch.zeros_like(input_ids)
        nxt[:, :-1] = input_ids[:, 1:]
        logits.scatter_(2, nxt.unsqueeze(-1), BIAS)
        return type("Out", (), {"logits": logits})()


docs = [" ".join(f"d1token{i}" for i in range(20)),
        " ".join(f"d2token{i}" for i in range(15)),
        "кort"]  # документ короче 50 символов — обязан быть отброшен фильтром
setfile = tmp / "ppl_set.txt"
setfile.write_text("\n---\n".join(docs), encoding="utf-8")

pipeline = P.load_pipeline_module(R.PIPELINE_CASE)
expected = math.exp(math.log((VOCAB - 1) + math.exp(BIAS)) - BIAS)
model, tok = _FakeModel(), _FakeTok()
ref = pipeline._ppl_eval(model, tok, str(setfile), max_len=MAXLEN, batch=BATCH)
per = P.measure_set_perdoc(model, tok, setfile, MAXLEN, BATCH, torch)
rel = abs(ref - per["ppl_corpus"]) / ref
assert rel <= P.CORPUS_AGREEMENT_TOL, f"два пути счёта разошлись: {ref} против {per['ppl_corpus']}"
assert per["docs"] == 2, f"фильтр документов не сработал: {per['docs']}"
assert abs(ref - expected) < 1e-4, (ref, expected)
assert abs(per["ppl_doc_mean"] - expected) < 1e-4, per
assert per["ppl_doc_min"] == per["ppl_doc_max"], per
# набор без единого годного документа — не «PPL = 1», а честный отказ агрегации
empty = tmp / "empty.txt"
empty.write_text("коротко", encoding="utf-8")
per_e = P.measure_set_perdoc(model, tok, empty, MAXLEN, BATCH, torch)
assert per_e["docs"] == 0 and per_e["ppl_doc_mean"] is None, per_e
print("  ok   методика пробы: два пути счёта согласны, документный разбор сходится с формой")
sys.exit(0)
PY
then
  PASS=$((PASS + 1)); printf '  ok   %-58s\n' "S3k: патч, тренд, вердикт, методика пробы"
else
  FAIL=$((FAIL + 1)); failures+=("S3k: патч/тренд/вердикт/методика пробы PPL")
  printf '  FAIL %-58s\n' "S3k: патч, тренд, вердикт, методика пробы"
fi

echo
echo "== 16. check_eval_set_purity.py (S3q/ADR-025 п.1: гейт чистоты набора) =="
# Фикстуры синтетические: корпуса и наборы пишутся в $TMP, реальные данные не
# читаются (AD-4). 12-граммовое окно собирается из различимых слов, чтобы
# совпадение было намеренным, а не случайным.
PUR="$TMP/pur"
mkdir -p "$PUR"
python3 - "$PUR" <<'PY'
import sys, pathlib
d = pathlib.Path(sys.argv[1])
def doc(tag, n=40):
    return " ".join(f"{tag}{i}" for i in range(n))
# Корпус обучения: два документа реплея и один доменный.
(d / "replay.txt").write_text(doc("rep") + "\n\n" + doc("rep2") + "\n", encoding="utf-8")
(d / "domain.txt").write_text(doc("dom") + "\n---\n" + doc("dom2") + "\n", encoding="utf-8")
# Набор: два чистых документа общего языка (51+ символов, ≥12 слов).
clean = [doc("free", 30), doc("other", 30)]
(d / "set_clean.txt").write_text("\n---\n".join(clean) + "\n", encoding="utf-8")
# Набор с утечкой документа: документ корпуса целиком.
(d / "set_docleak.txt").write_text(doc("rep") + "\n---\n" + clean[0] + "\n", encoding="utf-8")
# Набор с утечкой ТОЛЬКО по окну: сдвиг на одно слово снимает совпадение
# документов, но 39 окон из 40 остаются общими — это и проверяет второе условие.
(d / "set_windowleak.txt").write_text(
    doc("prefix", 1) + " " + doc("rep") + "\n---\n" + clean[0] + "\n", encoding="utf-8")
PY
GATE=(python3 tools/check_eval_set_purity.py --domain "$PUR/domain.txt" --replay "$PUR/replay.txt" --calib-dir "$PUR/no-calib")
expect_exit 0 "набор без пересечений — гейт зелёный" \
  "${GATE[@]}" --set "$PUR/set_clean.txt"
expect_exit 1 "документ набора найден в корпусе — отказ" \
  "${GATE[@]}" --set "$PUR/set_docleak.txt"
expect_exit 1 "документ не совпал, но 12-граммовое окно общее — тоже отказ" \
  "${GATE[@]}" --set "$PUR/set_windowleak.txt"
expect_exit 2 "нет файла набора — NOT-VERIFIED, а не зелёный" \
  "${GATE[@]}" --set "$PUR/nope.txt"
expect_exit 2 "нет корпуса обучения — NOT-VERIFIED" \
  python3 tools/check_eval_set_purity.py --set "$PUR/set_clean.txt" \
  --domain "$PUR/nope.txt" --replay "$PUR/replay.txt" --calib-dir "$PUR/no-calib"
expect_exit 0 "окна нарезаются внутри документа: короче 12 слов — пусто" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
from check_eval_set_purity import WINDOW, norm, windows
assert WINDOW == 12, "длина окна разъехалась с аудитом S3m"
assert windows(["a"] * (WINDOW - 1)) == [], "окно из 11 слов не должно нарезаться"
assert windows(["a"] * WINDOW) == [" ".join(["a"] * WINDOW)], "ровно 12 слов — одно окно"
assert len(windows(list(map(str, range(WINDOW + 3))))) == 4, "число окон = n-k+1"
assert norm("  А  Б\n\nВ ") == "а б в", "нормализация разъехалась с check_eval_leakage"
sys.exit(0)
PY

echo "== 16b. eval_purity_matrix.py (S3x: тот же инвариант, но по всем наборам) =="
# Фикстуры — те же ($PUR): свод обязан воспроизвести гейт ЧИСЛОМ, и это проверяется
# сравнением отчётов (ниже), а не согласием формулировок. Порог у роли свой, поэтому
# у одного и того же фикстора-утечки два разных ожидания: у набора общего языка
# утечка по окну — нарушение, у доменного — названное число (§3.4).
MAT=(python3 tools/eval_purity_matrix.py)
MIXSRC=(--source-path "replay_ru=$PUR/replay.txt" --source-path "domain_v10.1=$PUR/domain.txt")
expect_exit 0 "чистый набор против обоих источников микса — свод зелёный" \
  "${MAT[@]}" --sets v3_general_K1 --sources replay_ru,domain_v10.1 \
  --set-path "v3_general_K1=$PUR/set_clean.txt" "${MIXSRC[@]}" --report "$TMP/mx_clean.json"
expect_exit 1 "документ набора в корпусе — нарушение на уровне микса" \
  "${MAT[@]}" --sets v3_general_K1 --sources replay_ru,domain_v10.1 \
  --set-path "v3_general_K1=$PUR/set_docleak.txt" "${MIXSRC[@]}" --report "$TMP/mx_doc.json"
expect_exit 1 "утечка только по окну — для решающего набора тоже нарушение" \
  "${MAT[@]}" --sets v3_general_K1 --sources replay_ru,domain_v10.1 \
  --set-path "v3_general_K1=$PUR/set_windowleak.txt" "${MIXSRC[@]}" --report "$TMP/mx_win.json"
expect_exit 0 "ТА ЖЕ утечка по окну у доменного набора — не нарушение, а число" \
  "${MAT[@]}" --sets v2_domain --sources replay_ru,domain_v10.1 \
  --set-path "v2_domain=$PUR/set_windowleak.txt" "${MIXSRC[@]}" --report "$TMP/mx_domwin.json"
expect_exit 1 "документ доменного набора в корпусе — нарушение и здесь" \
  "${MAT[@]}" --sets v2_domain --sources replay_ru,domain_v10.1 \
  --set-path "v2_domain=$PUR/set_docleak.txt" "${MIXSRC[@]}" --report "$TMP/mx_domdoc.json"
# Молчание о непроверенном источнике не читается как ноль: неполный прогон — не «чисто».
expect_exit 2 "один источник микса из двух — partial, а не зелёный" \
  "${MAT[@]}" --sets v3_general_K1 --sources replay_ru \
  --set-path "v3_general_K1=$PUR/set_clean.txt" "${MIXSRC[@]}" --report "$TMP/mx_partial.json"
expect_exit 2 "нет корпуса — NOT-VERIFIED" \
  "${MAT[@]}" --sets v3_general_K1 --sources replay_ru,domain_v10.1 \
  --set-path "v3_general_K1=$PUR/set_clean.txt" \
  --source-path "replay_ru=$PUR/nope.txt" --source-path "domain_v10.1=$PUR/domain.txt"
expect_exit 1 "опечатка в ключе перенаправления — отказ, а не тихий пропуск" \
  "${MAT[@]}" --sets v3_general_K1 --source-path "нет_такого=$PUR/replay.txt"
# Тождество свода и гейта: одна правда о том, что считалось совпадением, — иначе это
# второй прибор, а не сводный вид. Сверяются документы И окна по каждому миксу.
python3 - "$PUR" "$TMP" <<'PY'
import json, pathlib, subprocess, sys
pur, tmp = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])

def load(p):
    return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))

mx = tmp / "mx_identity.json"
subprocess.run([sys.executable, "tools/eval_purity_matrix.py",
                "--sets", "v3_general_K1,v2_domain",
                "--sources", "replay_ru,domain_v10.1",
                "--set-path", f"v3_general_K1={pur}/set_windowleak.txt",
                "--set-path", f"v2_domain={pur}/set_docleak.txt",
                "--source-path", f"replay_ru={pur}/replay.txt",
                "--source-path", f"domain_v10.1={pur}/domain.txt",
                "--report", str(mx)], capture_output=True)
# Прогон заведомо красный (утечка в наборе K1) — важен не код, а числа в отчёте.
mine = load(mx)
by = {(m["set"], m["mix"]): m for m in mine["mix_matrix"]}
checked = 0
for set_file, set_key in [("set_windowleak.txt", "v3_general_K1"),
                          ("set_docleak.txt", "v2_domain")]:
    ref_out = tmp / f"gate_{pathlib.Path(set_file).stem}.json"
    subprocess.run([sys.executable, "tools/check_eval_set_purity.py",
                    "--set", f"{pur}/{set_file}",
                    "--domain", f"{pur}/domain.txt", "--replay", f"{pur}/replay.txt",
                    "--calib-dir", f"{pur}/no-calib", "--report", str(ref_out)],
                   capture_output=True)
    ref = load(ref_out)
    for m in ref["overlap_matrix"]:
        cell = by.get((set_key, m["mix"]))
        if cell is None:
            continue
        assert cell["overlap_docs"] == m["overlap_docs"], \
            (set_file, m["mix"], "документы", cell["overlap_docs"], m["overlap_docs"])
        assert cell["overlap_ngram"] == m["overlap_ngram"], \
            (set_file, m["mix"], "окна", cell["overlap_ngram"], m["overlap_ngram"])
        checked += 1
assert checked >= 6, f"сверено клеток слишком мало: {checked}"
print(f"  ok   тождество свода и гейта: {checked} клеток совпали числом")
sys.exit(0)
PY

echo "== 17. S3o: контрольные руки CPT — чистый протокол и низкий LR =="

echo "  --- 17a. build_ctrl_c1_corpus.py: 100 % общего языка, побайтовая самопроверка, отказы ---"
# Фикстура своя, а не MIXFIX из 16a: там доменный кэш намеренно испорчен негативным
# тестом, и «сборка C1 прошла» зависела бы от порядка разделов, а не от кода.
C1FIX="$TMP/c1fix"; mkdir -p "$C1FIX/tok"
python3 - "$C1FIX" <<'PY'
import sys
from pathlib import Path
d = Path(sys.argv[1])
(d / "dev.txt").write_text("\n---\n".join(
    f"Документ номер {i}: " + " ".join(f"домен{i}лексема{k}" for k in range(60))
    for i in range(7)), encoding="utf-8")
(d / "rep.txt").write_text("\n\n".join(
    f"Общий текст {i}: " + " ".join(f"реплей{i}слово{k}" for k in range(60))
    for i in range(9)), encoding="utf-8")
sys.exit(0)
PY
# Эталоны: v12r = 3 домена + 2 реплея, v12r50 = 3 + 3. Собраны ТЕМ ЖЕ сборщиком:
# сверять C1 не с чем, пока эталонов нет.
expect_exit 0 "синтетика: эталонный v12r (3 домена + 2 реплея) собран" \
  python3 tools/build_mix_v12r50.py --datasets-dir "$C1FIX" --domain-txt dev.txt \
      --general-txt rep.txt --domain-stem dev --out-stem-base dev --out-stem cpt_corpus_v12r \
      --max-len 16 --replay-chunks 2 --domain-chunks 3 --skip-v12r-check --force
expect_exit 0 "синтетика: эталонный v12r50 (3 + 3) собран" \
  python3 tools/build_mix_v12r50.py --datasets-dir "$C1FIX" --domain-txt dev.txt \
      --general-txt rep.txt --domain-stem dev --out-stem-base dev --out-stem cpt_corpus_v12r50 \
      --max-len 16 --replay-chunks 3 --domain-chunks 3 --skip-v12r-check --force
expect_exit 0 "корпус C1 собирается: реплей-блоки обоих эталонов воспроизведены" \
  python3 tools/build_ctrl_c1_corpus.py --datasets-dir "$C1FIX" --general-txt rep.txt \
      --v12r-stem cpt_corpus_v12r --v12r50-stem cpt_corpus_v12r50 --out-stem dev_ctrl100 \
      --max-len 16 --replay-chunks 3 --ref-v12r-domain-chunks 3 --ref-v12r-replay-chunks 2 \
      --ref-v12r50-domain-chunks 3 --ref-v12r50-replay-chunks 3 \
      --force --report "$C1FIX/report.json"
expect_exit 0 "корпус C1 = шафл реплей-блока, pos-компаньон той же формы, отчёт полон" \
  python3 - "$C1FIX" <<'PY'
import json, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, "tools")
import build_ctrl_c1_corpus as C
d = Path(sys.argv[1])
got = np.load(d / "tok" / "dev_ctrl100_16_qwen25.npy")
pos = np.load(d / "tok" / "dev_ctrl100_16_qwen25_pos.npy")
assert got.shape == pos.shape == (3, 16), (got.shape, pos.shape)
blk, blkp, why = C.replay_block_in_mix(
    np, d / "tok" / "cpt_corpus_v12r50_16_qwen25.npy",
    d / "tok" / "cpt_corpus_v12r50_16_qwen25_pos.npy", 3, 3)
assert why is None, why
# Домена нет вовсе: корпус — это ровно реплей-материал, а не «микс с нулевой долей».
assert {tuple(r) for r in got.tolist()} == {tuple(r) for r in blk.tolist()}
assert np.array_equal(got, C.B.build_arrays(blk, blk, blkp, blkp, 0, 3, 42, np)[0]), \
    "шафл отличается от рецепта build_arrays с n_dom=0"
r = json.loads((d / "report.json").read_text(encoding="utf-8"))
m = r["mix"]
assert m["domain_chunks"] == 0 and m["replay_chunks"] == 3 and m["chunks"] == 3, m
assert abs(m["mix_replay_ratio"] - 1.0) < 1e-9 and m["mix_domain_ratio"] == 0.0, m
assert all(c["tokens_equal"] and c["pos_equal"] for c in r["fidelity_check"]["checks"]), \
    r["fidelity_check"]
# Сопряжённое отличие названо, а не замолчано: корпус короче эталонов.
assert any("короче эталонов" in c for c in r["coupled_changes"]), r["coupled_changes"]
assert (d / "dev_ctrl100.txt").is_file(), "манифест корпуса не написан"
assert r["tool"]["path"].endswith("build_ctrl_c1_corpus.py"), r["tool"]
assert r["tool"]["recipe_library"]["sha256"], "ревизия библиотеки рецепта не зафиксирована"
sys.exit(0)
PY
expect_exit 1 "повторная сборка без --force отвергается (AD-7)" \
  python3 tools/build_ctrl_c1_corpus.py --datasets-dir "$C1FIX" --general-txt rep.txt \
      --v12r-stem cpt_corpus_v12r --v12r50-stem cpt_corpus_v12r50 --out-stem dev_ctrl100 \
      --max-len 16 --replay-chunks 3 --ref-v12r-domain-chunks 3 --ref-v12r-replay-chunks 2 \
      --ref-v12r50-domain-chunks 3 --ref-v12r50-replay-chunks 3
expect_exit 2 "чанков меньше реплей-блока эталона → NOT-VERIFIED, а не «сверю на глазок»" \
  python3 tools/build_ctrl_c1_corpus.py --datasets-dir "$C1FIX" --general-txt rep.txt \
      --v12r-stem cpt_corpus_v12r --v12r50-stem cpt_corpus_v12r50 --out-stem short_ctrl100 \
      --max-len 16 --replay-chunks 1 --ref-v12r50-domain-chunks 3 \
      --ref-v12r50-replay-chunks 3 --force
expect_exit 2 "нет источника → NOT-VERIFIED" \
  python3 tools/build_ctrl_c1_corpus.py --datasets-dir /nonexistent-datasets --plan
# Негатив: портим ОДИН чанк реплея в эталоне. Сверка обязана это увидеть — иначе
# «самопроверка» не проверяет ничего (главный дефект такого класса инструментов).
C1BAD="$TMP/c1bad"; cp -r "$C1FIX" "$C1BAD"
python3 - "$C1BAD" <<'PY'
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, "tools")
import build_ctrl_c1_corpus as C
p = Path(sys.argv[1]) / "tok" / "cpt_corpus_v12r50_16_qwen25.npy"
ref = np.load(p)
perm = np.random.RandomState(42).permutation(ref.shape[0])
inv = np.empty(ref.shape[0], dtype=np.int64); inv[perm] = np.arange(ref.shape[0])
row = int(inv[3])                 # первый чанк реплея в эталоне v12r50
ref[row, 0] = (int(ref[row, 0]) + 1) % 1000
np.save(p, ref)
sys.exit(0)
PY
expect_exit 1 "подменённый реплей-чанк эталона ловится сверкой (отказ, а не запись)" \
  python3 tools/build_ctrl_c1_corpus.py --datasets-dir "$C1BAD" --general-txt rep.txt \
      --v12r-stem cpt_corpus_v12r --v12r50-stem cpt_corpus_v12r50 --out-stem bad_ctrl100 \
      --max-len 16 --replay-chunks 3 --ref-v12r-domain-chunks 3 --ref-v12r-replay-chunks 2 \
      --ref-v12r50-domain-chunks 3 --ref-v12r50-replay-chunks 3 --force

echo "  --- 17b. audit_ctrl_corpus_leak.py: утечка eval в корпус контрольной руки ---"
LEAKFIX="$TMP/leakfix"; mkdir -p "$LEAKFIX/datasets"
python3 - "$LEAKFIX" <<'PY'
import sys
from pathlib import Path
d = Path(sys.argv[1]) / "datasets"
# Набор измерения: документы длиннее 50 символов (как в пробах v1/v2).
# Разделитель документов — как в настоящих наборах (split "\\n---\\n" в _ppl_eval).
(d / "general_eval.txt").write_text(
    "Первый документ набора измерения про измерение качества языка в модели.\n---\n"
    "Второй документ набора измерения про совсем другую тему и другие слова.\n",
    encoding="utf-8")
(d / "general_eval_v2.txt").write_text(
    "Третий документ набора измерения: длинная фраза про признаки и свойства объекта.\n",
    encoding="utf-8")
# Источник корпуса: текст, НЕ совпадающий с набором измерения.
(d / "general_replay_ru.txt").write_text(
    "Совершенно посторонний текст корпуса, который не пересекается с набором измерения "
    "ни одним непрерывным окном из двенадцати слов подряд, что и требуется проверить.\n",
    encoding="utf-8")
(d / "cpt_corpus_v10.1.txt").write_text(
    "Доменный текст корпуса: карточка концепта про обучение модели и её свойства.\n",
    encoding="utf-8")
sys.exit(0)
PY
expect_exit 0 "чистый источник: вердикт CLEAN, отчёт записан" \
  python3 tools/audit_ctrl_corpus_leak.py --shared "$LEAKFIX" --out "$LEAKFIX/leak.json"
expect_exit 0 "в вердикте CLEAN названы оба общих набора (v1 и v2), а не только v1" \
  python3 - "$LEAKFIX" <<'PY'
import json, sys
from pathlib import Path
r = json.loads((Path(sys.argv[1]) / "leak.json").read_text(encoding="utf-8"))
assert r["verdict"] == "CLEAN", r["verdict"]
assert set(r["sets"]) == {"v1_general", "v2_general"}, r["sets"]
# Единица счёта — документ набора (блок между разделителями), а не строка файла.
assert r["sets"]["v1_general"]["documents"] == 2, r["sets"]["v1_general"]
assert r["doc_unit"] == "\n---\n", r["doc_unit"]
for name, src in r["sources"].items():
    assert set(src["sets"]) == {"v1_general", "v2_general"}, (name, src["sets"])
# Ограничение метода названо: «0» — нижняя граница, а не доказательство непохожести.
assert any("нижняя граница" in m for m in r["method_limits"]), r["method_limits"]
sys.exit(0)
PY
python3 - "$LEAKFIX" <<'PY'
import sys
from pathlib import Path
d = Path(sys.argv[1]) / "datasets"
# Документ набора измерения, дословно переписанный в источник корпуса.
doc = "Первый документ набора измерения про измерение качества языка в модели."
(d / "general_replay_ru.txt").write_text(
    "Посторонний текст вокруг. " + doc + " И ещё немного текста после него.\n",
    encoding="utf-8")
sys.exit(0)
PY
expect_exit 0 "дословный документ набора в источнике → вердикт LEAK (уровень A)" \
  python3 - "$LEAKFIX" <<'PY'
import json, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
p = subprocess.run(["python3", "tools/audit_ctrl_corpus_leak.py", "--shared", str(root),
                    "--out", str(root / "leak2.json")], capture_output=True, text=True)
assert p.returncode == 0, p.stderr
r = json.loads((root / "leak2.json").read_text(encoding="utf-8"))
assert r["verdict"].startswith("LEAK"), r["verdict"]
assert r["overlap_docs_total"] >= 1, r["overlap_docs_total"]
sys.exit(0)
PY
expect_exit 2 "нет набора измерения → NOT-VERIFIED (не «утечки нет»)" \
  python3 tools/audit_ctrl_corpus_leak.py --shared "$TMP/empty-contour" --out "$TMP/x.json"

echo "  --- 17c. run_mix_lr_calib.py: контрольные руки тем же протоколом, что сетка ---"
expect_exit 0 "руки адресуются в ctrl-*, сетка — по-прежнему в calib-*, корпуса не смешаны" \
  python3 - <<'PY'
import argparse, sys
sys.path.insert(0, "tools")
import run_mix_lr_calib as R
ts = "20260101-0000"
# Сетка S3m не тронута: те же четыре руки, те же корпуса, тот же префикс каталогов.
assert list(R.ARMS) == ["25-0.7", "25-0.35", "50-0.7", "50-0.35"], list(R.ARMS)
assert set(R.CORPORA) == {25, 50}, set(R.CORPORA)
assert list(R.CTRL_ARMS) == ["C1-100-0.35", "C2-25-0.035"], list(R.CTRL_ARMS)
assert R.run_id("25-0.35", ts) == f"calib-25-0.35-{ts}"
assert R.run_id("C1-100-0.35", ts) == f"ctrl-C1-100-0.35-{ts}"
assert R.run_id("C2-25-0.035", ts) == f"ctrl-C2-25-0.035-{ts}"
assert R.is_control(["C1-100-0.35", "C2-25-0.035"]) is True
assert R.is_control(["C1-100-0.35", "25-0.35"]) is False, "смешанный список — не контроль"
assert R.is_control([]) is False and R.is_control(None) is False
# Ровно один фактор отличия на руку: это числа, а не комментарий.
c1, c2 = R.arm_spec("C1-100-0.35"), R.arm_spec("C2-25-0.035")
assert (c1["replay"], c1["lr_scale"]) == (100, 0.35), c1
assert (c2["replay"], c2["lr_scale"]) == (25, 0.035), c2
assert c1["corpus"]["cache_host"].endswith("cpt_corpus_ctrl100_8192_qwen25.npy"), c1
assert c2["corpus"]["cache_host"].endswith("cpt_corpus_v12r_8192_qwen25.npy"), c2
# C2 отличается от лучшей руки сетки ТОЛЬКО пиком LR — иначе сравнение не о том.
best = R.arm_spec("25-0.35")
assert (c2["replay"], c2["corpus"]["cache_host"]) == (best["replay"],
                                                      best["corpus"]["cache_host"])
assert c2["lr_scale"] != best["lr_scale"]
args = argparse.Namespace(stall_minutes=20, sampler_seconds=3600, image="test-image")
cmd = R.chain_command("C1-100-0.35", ts, f"/stand/ctrl-C1-100-0.35-{ts}", args)
assert "--cpt-data /workspace/shared/datasets/cpt_corpus_ctrl100.txt" in cmd, cmd
assert ("--dataset /home/user/gb10-shared/datasets/tok/"
        "cpt_corpus_ctrl100_8192_qwen25.npy") in cmd, cmd
assert "--peak-lr-scale 0.35" in cmd and "--extra-hyperparam replay_share_pct=100" in cmd, cmd
assert f"--ctr-run-dir /workspace/shared/calib/ctrl-C1-100-0.35-{ts}" in cmd, cmd
assert "--ctr-prefix laguna-ctrl-" in cmd, "контейнер руки назван не по её префиксу"
cmd2 = R.chain_command("C2-25-0.035", ts, "/stand/x", args)
assert "--peak-lr-scale 0.035" in cmd2 and "--extra-hyperparam peak_lr_scale=0.035" in cmd2
assert "--cpt-data /workspace/shared/datasets/cpt_corpus_v12r.txt" in cmd2, cmd2
# Протокол идентичен сетке: всё, что не названо фактором руки, совпадает дословно.
base = R.chain_command("25-0.35", ts, "/stand/x", args)
for token in ("--seed 42", "--max-len 8192", "--max-samples 50000", "--attn flex",
              "--cpt-gen-eval-every 0", "--mem-cap 100g", "--model Qwen/Qwen2.5-0.5B",
              "--image test-image"):
    assert token in cmd and token in base, token
assert R.stages_tsv("C1-100-0.35", ts).strip().split("\t")[7] == f"ctrl-C1-100-0.35-{ts}"
# Каталоги серии: у контроля свой каталог и своя tmux-сессия, у сетки — свои.
assert R.grid_stand_dir(ts, ["C1-100-0.35"]).endswith(f"/ctrl-grid-{ts}")
assert R.grid_stand_dir(ts, ["25-0.35"]).endswith(f"/grid-{ts}")
assert R.grid_stand_dir(ts).endswith(f"/grid-{ts}"), "по умолчанию — каталог сетки S3m"
assert R.grid_session(ts, ["C1-100-0.35"]) == f"ctrl-grid-{ts}"
assert R.grid_session(ts, ["25-0.35"]) == f"calib-grid-{ts}"
assert R.ppl_run_name(ts, ["C1-100-0.35"]) == f"ctrl-ppl-{ts}"
assert R.ppl_run_name(ts, ["25-0.35"]) == f"calib-ppl-{ts}"
sys.exit(0)
PY
expect_exit 0 "проба PPL адресуется в контрольные каталоги, а не в каталоги сетки" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import run_mix_lr_calib as R
ts = "20260101-0000"
plan = R.probe_plan(ts, arms=["C1-100-0.35", "C2-25-0.035"], exists=lambda p: True)
names = [n for n, _ in plan["states"]]
assert "base" in names and "base_untouched" in names, names
assert f"ctrl-C1-100-0.35-{ts}-c500" in names, names
assert f"ctrl-C2-25-0.035-{ts}-c2000" in names, names
assert all("calib-" not in n for n in names), "проба ушла в каталоги сетки S3m"
assert plan["pipeline"].startswith(f"runs/ctrl-C1-100-0.35-{ts}/"), plan["pipeline"]
assert plan["gaps"] == [], plan["gaps"]
# Дыра не молчит: нет файла — есть причина, и она уезжает в evidence.
gap = R.probe_plan(ts, arms=["C1-100-0.35"], exists=lambda p: False)
assert len(gap["gaps"]) == 4 and any(g["decisive"] for g in gap["gaps"]), gap["gaps"]
sys.exit(0)
PY
expect_exit 2 "раннер: смешанный список рук (сетка + контроль) отвергается, а не запускается" \
  python3 tools/run_mix_lr_calib.py --chain --arms 25-0.35 C1-100-0.35 --ts 20260101-0000
expect_exit 0 "вердикт контроля: потолок 2× по v1, заражённый v2 не решает" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import run_mix_lr_calib as R
BASE = {"v1_general": 11.93, "v2_general": 10.77}


def rep(name, r1, r2):
    return {"arm": name, "replay": R.arm_spec(name)["replay"],
            "lr_scale": R.arm_spec(name)["lr_scale"], "corpus": name,
            "ppl_base": BASE, "ppl_v1_2000": r1, "ppl_v2_2000": r2}


# C1 провалила потолок по v1 → виноват протокол. v2 у неё заражён её же обучением
# и в вердикт не идёт: 12.0 по v2 выглядело бы «проходом», хотя это то, что она учила.
v = R.ctrl_verdict([rep("C1-100-0.35", 120.0, 12.0), rep("C2-25-0.035", 15.0, 11.0)])
assert v["verdict_status"] == "OK", v
t = {r["arm"]: r for r in v["table"]}
assert t["C1-100-0.35"]["v2_contaminated"] is True, t["C1-100-0.35"]
assert t["C2-25-0.035"]["v2_contaminated"] is False, t["C2-25-0.035"]
assert t["C1-100-0.35"]["under_ceiling"] is False, "заражённый v2 закрыл провал по v1"
assert t["C2-25-0.035"]["under_ceiling"] is True, t["C2-25-0.035"]
assert v["protocol_blamed"] is True and v["lr_blamed"] is True, v
assert v["under_ceiling"] == ["C2-25-0.035"], v["under_ceiling"]
assert any("протокол" in f for f in v["findings"]), v["findings"]
# Обратный случай: C1 прошла, C2 нет → причина в домене, а не в протоколе и не в LR.
v2 = R.ctrl_verdict([rep("C1-100-0.35", 14.0, 11.0), rep("C2-25-0.035", 90.0, 30.0)])
assert v2["protocol_blamed"] is False and v2["lr_blamed"] is False, v2
# Нет базового замера — вердикта нет (а не «прошло по умолчанию»).
v3 = R.ctrl_verdict([{"arm": "C1-100-0.35", "replay": 100, "lr_scale": 0.35}])
assert v3["verdict_status"] == "NOT-VERIFIED", v3
sys.exit(0)
PY

echo "  --- 17d. манифест AD-2 в каталоге контрольной руки + драйвер с префиксом ---"
expect_exit 0 "манифест старта: относительные пути, стадия running, гейт C-012 зелёный" \
  python3 - "$TMP" <<'PY'
import argparse, json, shutil, subprocess, sys
from pathlib import Path
sys.path.insert(0, "tools")
import run_mix_lr_calib as R
tmp = Path(sys.argv[1]) / "ctrlman"; shutil.rmtree(tmp, ignore_errors=True)
root = tmp / "case"                     # «корень кейса» фикстуры: в дерево кейса не пишем
d = root / "runs" / "ctrl-C1-100-0.35-20260101-0000"
(root / "datasets" / "tok").mkdir(parents=True)   # словарь кейса, а не каталог руки
d.mkdir(parents=True)
(d / "laguna_pipeline_calib.py").write_text("# копия пайплайна (фикстура)\n", encoding="utf-8")
# Датасет подключается СИМЛИНКОМ на реальный кэш (AD-4: копий данных не держим) и
# передаётся в манифест через словарь кейса: так путь выходит относительным.
link = root / "datasets" / "tok" / Path(R.arm_spec("C1-100-0.35")["corpus"]["cache_host"]).name
link.symlink_to(R.arm_spec("C1-100-0.35")["corpus"]["cache_host"])
args = argparse.Namespace(image="nvcr.io/nvidia/pytorch:26.07-py3-vllm")
info = R.write_launch_manifest(d, "C1-100-0.35", args, case_root=root)
assert info["written"] is True, info
man = json.loads((d / "run_manifest.json").read_text(encoding="utf-8"))
assert man["stages"] == [{"name": "cpt", "status": "running"}], man["stages"]
assert man["pipeline_complete"] is False, man
assert not man["pipeline_path"].startswith("/"), man["pipeline_path"]
assert not man["dataset_path"].startswith("/"), man["dataset_path"]
assert man["seed"] == 42 and man["base_model_id"] == "Qwen/Qwen2.5-0.5B", man
assert man["dataset_sha256"] and len(man["dataset_sha256"]) == 64, man
# Полный манифест не перезаписывается: повторный запуск не имеет права стереть
# фактический манифест законченного прогона.
man["pipeline_complete"] = True
(d / "run_manifest.json").write_text(json.dumps(man), encoding="utf-8")
again = R.write_launch_manifest(d, "C1-100-0.35", args, case_root=root)
assert again["written"] is False, again
# И этот манифест проходит страж AD-2, а не похож на него.
p = subprocess.run(["python3", "tools/check_run_manifest.py", "--runs", str(root / "runs")],
                   capture_output=True, text=True)
assert p.returncode == 0, p.stdout + p.stderr
sys.exit(0)
PY
# Драйвер: у контрольной серии свои каталоги рук и свой каталог серии. Проверяется
# на фикстуре — цепочка руки подменена командой, пишущей chain.status.
GRID_CTRL="$TMP/grid-ctrl"; mkdir -p "$GRID_CTRL/calib"
for arm in C1-100-0.35 C2-25-0.035; do
  d="$GRID_CTRL/calib/ctrl-$arm-20260101-0000"
  mkdir -p "$d/logs" "$d/var" "$d/checkpoints"
  printf 'sleep 1; echo done > %s/var/chain.status\n' "$d" > "$d/chain_command.txt"
done
expect_exit 0 "серия контрольных рук идёт под своим префиксом и своим каталогом" \
  bash tools/mix_lr_grid_chain.sh --ts 20260101-0000 --arms "C1-100-0.35 C2-25-0.035" \
      --shared "$GRID_CTRL" --run-prefix ctrl --grid-name ctrl-grid-20260101-0000 \
      --poll 1 --arm-timeout 60
expect_exit 0 "обе руки дошли до done в ctrl-grid-каталоге" \
  grep -c "done" "$GRID_CTRL/calib/ctrl-grid-20260101-0000/status.tsv"
expect_exit 0 "план контрольной серии печатается с префиксом ctrl (без стенда)" \
  bash tools/mix_lr_grid_chain.sh --ts 20260101-0000 --arms "C1-100-0.35" \
      --shared "$GRID_CTRL" --run-prefix ctrl --print-plan
expect_contains "ctrl-C1-100-0.35-20260101-0000" "в плане назван каталог контрольной руки" \
  bash tools/mix_lr_grid_chain.sh --ts 20260101-0000 --arms "C1-100-0.35" \
      --shared "$GRID_CTRL" --run-prefix ctrl --print-plan
expect_exit 0 "по умолчанию драйвер остаётся драйвером сетки S3m (префикс calib)" \
  bash tools/mix_lr_grid_chain.sh --ts 20260101-0000 --arms "25-0.35" \
      --shared "$GRID_TMP" --print-plan

echo "== 17. s3n_ppl_curve.py (S3n: профиль обвала языка по точкам калибровки) =="

echo "  --- 17a. обнаружение точек: обе схемы имён, финал, отсутствие как факт ---"
expect_exit 0 "имена точек обеих схем разбираются, финал привязан к cpt_steps, утрата названа" \
  python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "tools")
import s3n_ppl_curve as C

# Обе схемы имён — факт прогонов (25-0.7 сохраняла штатным именем, остальные — вне
# ретенции), а не выбор инструмента: разобрать обязан обе.
assert C.parse_step("calib_checkpoint_500.pt") == 500
assert C.parse_step("checkpoint_1500.pt") == 1500
assert C.parse_step("checkpoint_final.pt") is None, "финал — не шаг, номер берётся из cpt_steps"
assert C.parse_step("sft_checkpoint_final.pt") is None
assert C.parse_step("checkpoint_1000.pt.bak") is None

# Финал обязан попасть в найденные под номером последнего шага, иначе профиль
# потеряет последнюю точку, а «отсутствующие» покажет ложные.
import tempfile
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    for n in ("calib_checkpoint_500.pt", "calib_checkpoint_1000.pt",
              "calib_checkpoint_1500.pt", "checkpoint_final.pt", "run_manifest.json"):
        (d / n).write_text("x")
    found, missing = C.discover_checkpoints(d, 2000)
    assert sorted(found) == [500, 1000, 1500, 2000], sorted(found)
    assert missing == [], missing
    # Нет точки 500 (рука 25-0.7: утрачена ретенцией) — это missing, а не тихий пропуск.
    (d / "calib_checkpoint_500.pt").unlink()
    found2, missing2 = C.discover_checkpoints(d, 2000)
    assert missing2 == [500], missing2
    assert 500 not in found2
# Каталога нет вовсе — все точки «отсутствуют», а не падение.
found3, missing3 = C.discover_checkpoints(Path("/nope"), 2000)
assert found3 == {} and missing3 == [500, 1000, 1500, 2000]
sys.exit(0)
PY

echo "  --- 17b. форма кривой: ступенька с якорем, переход внутри диапазона, край ---"
expect_exit 0 "классификатор различает ступеньку, наблюдаемый переход и одну точку" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import s3n_ppl_curve as C

CEIL = 23.863847776868994   # 2 × 11.931923888434497

# Обвал завершён ДО первой точки: якорь (шаг 0) в норме, все точки выше потолка.
r = C.classify_curve([(500, 60.15), (1000, 74.42), (1500, 68.33), (2000, 56.78)], CEIL,
                     anchor=(0, 11.9319))
assert r["all_above_ceiling"] is True
assert r["crossed_at_first_measured"] is True
assert r["first_crossing_step"] == 500
assert r["transition_bracket"] == [0, 500], r["transition_bracket"]
assert "ступенька" in r["shape"] and "(0, 500]" in r["shape"], r["shape"]
assert r["monotone_up"] is False and r["monotone_down"] is False, "60→74→68→57 немонотонно"

# Порог пробит внутри диапазона: у деградации есть наблюдаемый ход.
r2 = C.classify_curve([(500, 8.0), (1000, 30.0), (1500, 60.0)], CEIL, anchor=(0, 11.93))
assert r2["first_crossing_step"] == 1000
assert r2["transition_bracket"] == [500, 1000], r2["transition_bracket"]
assert "между шагами 500 и 1000" in r2["shape"], r2["shape"]
assert r2["monotone_up"] is True

# Потолок не пересечён вовсе — это не «ступенька» и не «переход».
r3 = C.classify_curve([(500, 12.0), (2000, 13.0)], CEIL, anchor=(0, 11.93))
assert r3["first_crossing_step"] is None and "не пересечён" in r3["shape"], r3

# Одна точка — не кривая: инструмент обязан это сказать, а не нарисовать форму.
r4 = C.classify_curve([(500, 60.0)], CEIL, anchor=(0, 11.93))
assert r4["shape"] == "одна точка — не кривая", r4["shape"]
assert C.classify_curve([], CEIL)["shape"] == "нет данных"
sys.exit(0)
PY

echo "  --- 17c. сборка профиля: контракт отчёта, отсутствие как факт, отказ без входа ---"
S3NROOT="$TMP/s3n"; mkdir -p "$S3NROOT/calib" "$S3NROOT/runs"
python3 - "$S3NROOT" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, "tools")
import s3n_ppl_curve as C

BASE_G, BASE_D = 11.931923888434497, 11.134116
# Фикстура — синтетические отчёты прибора в формате calib_ppl_probe/1. Числа
# нарочно разные у рук: если сборка их усреднит или перепутает руки, тест упадёт.
ARM_PPL = {"25-0.35": [60.15, 74.42, 68.33, 56.78],
           "25-0.7": [None, 199.83, 149.38, 113.08],   # шаг 500 утрачен
           "50-0.35": [70.0, 80.0, 75.0, 65.0],
           "50-0.7": [90.0, 85.0, 80.0, 70.0]}
for arm, dirname in C.ARM_DIRS.items():
    arm_dir = root / "calib" / dirname / "checkpoints"
    arm_dir.mkdir(parents=True)
    (root / "calib" / dirname / "calib_params.json").write_text(
        json.dumps({"cpt_steps": 2000}), encoding="utf-8")
    states = {"base": {"load": {"kind": "base"}, "sets": {}},
              "base_untouched": {"load": {"kind": "base"}, "sets": {}}}
    for st, gen in (("base", BASE_G), ("base_untouched", BASE_G)):
        states[st]["sets"] = {s: {"ppl": p, "tokens": 1, "doc_ppl": {"n": 1, "median": p}}
                              for s, p in (("v1_general", gen), ("v2_domain", BASE_D),
                                           ("v2_general", 10.765154), ("v1_domain", 9.290194))}
    for i, (step, gen) in enumerate(zip(C.EXPECTED_STEPS, ARM_PPL[arm])):
        if step == 500 and arm == "25-0.7":
            continue                      # утрачена — файла нет, и состояния быть не должно
        name = C.state_name(step, 2000)
        (arm_dir / (f"calib_checkpoint_{step}.pt" if arm != "25-0.7"
                    else ("checkpoint_final.pt" if step == 2000 else f"checkpoint_{step}.pt"))
         ).write_text("ckpt", encoding="utf-8")
        states[name] = {"load": {"kind": "ckpt", "checkpoint": f"/x/{step}.pt",
                                 "resized_embeddings": False},
                        "sets": {s: {"ppl": p, "tokens": 1, "doc_ppl": {"n": 1, "median": p}}
                                 for s, p in (("v1_general", gen),
                                              ("v2_domain", BASE_D / (i + 2)),
                                              ("v2_general", gen / 4.0),
                                              ("v1_domain", 9.0 / (i + 2)))}}
    rep = {"schema": "calib-ppl-probe/1", "states": states,
           "sets": {s: {"path": f"datasets/{s}.txt", "sha256": "a" * 64, "docs_kept": 24}
                    for s in ("v1_general", "v2_domain", "v2_general", "v1_domain")},
           "instrument": {"base_weights": "/abs/base", "tokenizer": "/abs/tok"},
           "checks": [{"name": "base_vs_s3h", "verdict": "ok", "detail": "0.00e+00"}]}
    (root / "runs" / f"{arm}.json").write_text(json.dumps(rep), encoding="utf-8")
sys.exit(0)
PY
expect_exit 0 "профиль собран: матрица, база, потолок, первое пересечение, форма, вердикт" \
  python3 tools/s3n_ppl_curve.py --case-root "$PWD" --calib-root "$S3NROOT/calib" \
      --mode assemble --runs-dir "$S3NROOT/runs" --out "$S3NROOT/curve.json"
expect_exit 0 "отчёт несёт контракт целиком, и числа в нём — из фикстуры, а не выдуманы" \
  python3 - "$S3NROOT" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
d = json.loads((root / "curve.json").read_text(encoding="utf-8"))
assert d["schema"] == "s3n-ppl-curve/1", d["schema"]
assert abs(d["baseline_ppl"] - 11.931923888434497) < 1e-9, d["baseline_ppl"]
assert abs(d["ceiling"] - 2 * d["baseline_ppl"]) < 1e-9, d["ceiling"]
assert d["first_crossing_step"] == 500, d["first_crossing_step"]
# Матрица: по 5 строк на руку 25-0.7 (4 точки + шаг 0) и по 5 у остальных —
# строка шага 0 есть у всех, base_untouched в матрицу не попадает вовсе.
arms = [r["arm"] for r in d["matrix"]]
assert set(arms) == set(ROWS := {"25-0.35", "25-0.7", "50-0.35", "50-0.7"}), arms
assert all(a != "base_untouched" for a in (r["state"] for r in d["matrix"]))
zero = [r for r in d["matrix"] if r["step"] == 0]
assert len(zero) == 4 and all(r["source"].startswith("base") for r in zero), zero
gen = {r["arm"]: r for r in d["matrix"] if r["arm"] == "25-0.35" and r["step"] == 500}
assert abs(gen["25-0.35"]["ppl_general"] - 60.15) < 1e-9, gen
assert abs(gen["25-0.35"]["ratio_vs_base"] - 60.15 / 11.931923888434497) < 1e-9
assert gen["25-0.35"]["ppl_domain"] is not None
# Отсутствующая точка руки 25-0.7 обязана быть названа с причиной, а не исчезнуть.
miss = {(m["arm"], m["step"]) for m in d["missing_points"]}
assert ("25-0.7", 500) in miss, miss
assert sum(1 for m in d["missing_points"] if m["step"] == 500) == 1, d["missing_points"]
assert "ретенц" in [m["reason"] for m in d["missing_points"] if m["arm"] == "25-0.7"][0]
# Вердикт выведен из чисел и опирается на одинаковость формы у 25 % и 50 %.
assert d["verdict"]["supported"], d["verdict"]
assert any("replay" in x or "дол" in x for x in d["verdict"]["excluded"]), d["verdict"]
assert d["complete"] is True, d["complete"]
sys.exit(0)
PY
# Отчёт прибора несёт свой вердикт: подделанный на «расхождение» — это отказ сборки.
python3 - "$S3NROOT" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1]) / "runs" / "50-0.7.json"
d = json.loads(p.read_text(encoding="utf-8"))
d["checks"] = [{"name": "base_vs_s3h", "verdict": "расхождение", "detail": "1e-2"}]
p.write_text(json.dumps(d), encoding="utf-8")
PY
expect_exit 1 "вердикт прибора «расхождение» роняет профиль, а не проходит молча" \
  python3 tools/s3n_ppl_curve.py --case-root "$PWD" --calib-root "$S3NROOT/calib" \
      --mode assemble --runs-dir "$S3NROOT/runs" --out "$S3NROOT/curve2.json"
# Нет входа — NOT-VERIFIED (exit 2), а не «профиль без рук».
expect_exit 2 "нет каталога артефактов калибровки → NOT-VERIFIED" \
  python3 tools/s3n_ppl_curve.py --case-root "$PWD" --calib-root "$TMP/no-such-calib" \
      --mode assemble --runs-dir "$S3NROOT/runs" --out "$S3NROOT/x.json"
expect_exit 2 "нет отчёта прибора по руке → NOT-VERIFIED" \
  python3 tools/s3n_ppl_curve.py --case-root "$PWD" --calib-root "$S3NROOT/calib" \
      --mode assemble --runs-dir "$TMP/no-such-runs" --out "$S3NROOT/x.json"
expect_exit 2 "неизвестная рука отвергается" \
  python3 tools/s3n_ppl_curve.py --mode assemble --arms 99-9.9 --calib-root "$S3NROOT/calib"
expect_contains "нет [500]" "в плане названа утрата точки 500, а не только руки" \
  python3 tools/s3n_ppl_curve.py --mode plan --calib-root "$S3NROOT/calib"

echo
echo "== 17. build_general_eval_v3.py (S3q: сборка расширенного набора) =="
expect_exit 0 "план печатается без сети и без токенизатора" \
  python3 tools/build_general_eval_v3.py --plan
# Главный запрет ADR-025 п.4: расширение идёт ОТДЕЛЬНЫМ файлом. Отказ по имени
# случается раньше любой работы — ни токенизатор, ни сеть не нужны.
expect_exit 1 "исходный набор ревизии не перезаписывается ни при каких флагах" \
  python3 tools/build_general_eval_v3.py --out datasets/general_eval.txt --force
expect_exit 1 "и при явном --force тоже: запрет по имени, а не по хешу" \
  python3 tools/build_general_eval_v3.py --out datasets/general_eval_v2.txt --force
expect_exit 2 "нет корпуса обучения — NOT-VERIFIED (сеть не трогается)" \
  python3 tools/build_general_eval_v3.py --out "$TMP/gev3_never.txt" \
  --replay "$TMP/nope.txt" --domain "$TMP/nope.txt"
expect_exit 0 "фильтры прозы отделяют стабы и списки от текста" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import build_general_eval_v3 as B
assert B.prose_reject("я" * (B.PROSE_MIN_CHARS - 1)) == "short", "стаб прошёл"
assert B.prose_reject("слово " * 400) == "not_prose_sentences", "список без концов предложений прошёл"
# Цифровая таблица с концами предложений, но без букв: ловится долей букв, а не
# длиной — иначе проверялось бы первое правило вместо нужного.
assert B.prose_reject("1. 2. 3. " * 300) == "not_prose_alpha", "цифровая таблица прошла"
good = ("Это обычный текст предложения. " * 40)
assert B.prose_reject(good) is None, "проза отсеяна"
assert B.prose_reject(good + "\n---\n" + good) == "doc_sep_inside", \
    "разделитель внутри документа обязан отсекаться (иначе набор распадётся при чтении)"
sys.exit(0)
PY
expect_exit 0 "отбор: детерминирован по сиду и выбрасывает пересечения с корпусом" \
  python3 - "$PUR" <<'PY'
import sys, pathlib
sys.path.insert(0, "tools")
import build_general_eval_v3 as B

class Tok:
    """Заглушка токенизатора: токен на каждые 4 символа — как у русского текста."""
    def encode(self, text, add_special_tokens=False):
        return list(range(len(text) // 4))

tmp = pathlib.Path(sys.argv[1])
body = "Это длинный документ общего языка. " * 40          # ≥800 символов, ≥5 предложений
# Документ корпуса — тоже проза (иначе он отсеялся бы фильтром прозы, и проверялось
# бы не то правило): длинный и с концами предложений.
corpus_doc = "Это предложение обучающего корпуса. " * 40
filler = "Совсем другой текст доменного корпуса. " * 40   # в корпусах, но не у кандидатов
tmp.joinpath("replay.txt").write_text(corpus_doc + "\n\n" + filler + "\n", encoding="utf-8")
tmp.joinpath("domain.txt").write_text(filler + "\n---\n" + filler + "\n", encoding="utf-8")
corpora = {"replay": tmp / "replay.txt", "domain": tmp / "domain.txt"}
rows = [{"id": f"{i:03d}", "text": body + f" Тема номер {i}."} for i in range(40)]
# Два разных нарушения, и оба обязаны отсечь кандидата:
#  * dirty_doc — текст целиком лежит в корпусе (условие «документы»);
#  * dirty_win — своё начало, но 12-граммовые окна хвоста общие (условие «окна»).
rows.append({"id": "dirty_doc", "text": corpus_doc})
rows.append({"id": "dirty_win", "text": "Другое начало документа. " + corpus_doc})
docs1, sel1 = B.select(list(rows), Tok(), 10, 7, corpora)
docs2, sel2 = B.select(list(rows), Tok(), 10, 7, corpora)
assert [d["id"] for d in docs1] == [d["id"] for d in docs2], "отбор не детерминирован"
assert len(docs1) == 10, f"выбрано {len(docs1)} вместо 10"
ids1 = [d["id"] for d in docs1]
assert "dirty_doc" not in ids1 and "dirty_win" not in ids1, "кандидат из корпуса попал в набор"
assert sel1["rejects"].get("overlap_with_training_corpus") == 2, sel1["rejects"]
# Сид меняет СОСТАВ, но не порядок записей: файл набора воспроизводим по составу.
docs3, _ = B.select(list(rows), Tok(), 10, 8, corpora)
assert [d["id"] for d in docs3] == sorted(d["id"] for d in docs3), "записи не отсортированы по id"
assert {d["id"] for d in docs1} != {d["id"] for d in docs3}, "другой сид дал тот же состав"
# Пул меньше требуемого — отказ, а не набор из меньшего числа документов.
try:
    B.select(list(rows), Tok(), 999, 7, corpora)
except RuntimeError as e:
    assert "NOT-VERIFIED" in str(e), str(e)
else:
    raise AssertionError("нехватка пула не названа отказом")
sys.exit(0)
PY
expect_exit 0 "verify принимает записанный набор и считает документы/токены" \
  python3 tools/build_general_eval_v3.py --verify datasets/general_eval_v3.txt
expect_exit 1 "verify отказывает набору из 24 документов при заявке в 200" \
  python3 tools/build_general_eval_v3.py --verify datasets/general_eval.txt
expect_exit 2 "verify на отсутствующем файле — NOT-VERIFIED" \
  python3 tools/build_general_eval_v3.py --verify "$TMP/nope.txt"

echo "== 18. S3u: полный CPT на замороженном миксе v12r (ADR-029) =="

# 18.1 Число шагов и его ИСТОЧНИК. Дельта требует назвать, откуда взято число
# шагов: «9776» без источника — это «так получилось», а не бюджет стадии ADR-008.
expect_exit 0 "план полного CPT строится (обучение не запускается)" \
  python3 tools/run_full_cpt.py --plan --ts 20260101-0000
expect_contains "9776 (источник: ADR-008" \
  "в плане назван источник числа шагов (ADR-008), а не только число" \
  python3 tools/run_full_cpt.py --plan --ts 20260101-0000
expect_contains "replay 25 %, peak_lr_scale 0.035" \
  "план несёт конфигурацию ADR-031 (25 % replay, пик LR ×0.035)" \
  python3 tools/run_full_cpt.py --plan --ts 20260101-0000
expect_contains "ADR-031" "план называет решение, которым выбран пик LR" \
  python3 tools/run_full_cpt.py --plan --ts 20260101-0000
expect_contains "K1 (general_eval_v3.txt) + K2 (general_eval_k2.txt)" \
  "план называет обе компоненты меры языка (ADR-027: не смешивать)" \
  python3 tools/run_full_cpt.py --plan --ts 20260101-0000
expect_contains "checkpoint_final.pt" \
  "последняя точка замера — финальный чекпойнт, а не номерной файл" \
  python3 tools/run_full_cpt.py --plan --ts 20260101-0000
expect_exit 2 "раннер без шага отказывает (NOT-VERIFIED), а не «молча ничего»" \
  python3 tools/run_full_cpt.py

# 18.2 Копия пайплайна полного прогона обязана быть ТОЙ ЖЕ, что у калибровочных
# рук: иначе числа полного CPT и калибровки K1/K2 несопоставимы.
expect_exit 0 "патч пайплайна воспроизводит копию калибровочных рук (1571cbd1…)" \
  python3 -c "import sys; sys.path.insert(0,'tools'); import run_mix_lr_calib as C, run_full_cpt as F; \
src=C.PIPELINE_SRC.read_text(encoding='utf-8'); out,rec=C.patched_pipeline(src); \
sys.exit(0 if rec['patched_sha256']==F.CALIB_PIPELINE_SHA256 else 1)"
expect_exit 1 "патч отказывает, если якорь пайплайна исчез (правка пайплайна не проходит молча)" \
  python3 -c "import sys; sys.path.insert(0,'tools'); import run_mix_lr_calib as C; \
C.patched_pipeline('ни одного якоря')"

# 18.3 Число шагов = один проход по корпусу — проверяется файлом, а не памятью.
expect_exit 0 "форма кэша v12r совпадает с числом шагов 9776 (один проход)" \
  python3 -c "import sys; sys.path.insert(0,'tools'); import run_full_cpt as F; \
sys.exit(0 if F.corpus_shape_check()['ok'] else 1)"

# 18.4 Прибор K1/K2: наборы зарегистрированы в приборе и лежат на сетевом диске
# (AD-4: в кейсе — симлинк, копий нет).
expect_exit 0 "наборы K1/K2 зарегистрированы в приборе ppl_probe (тот же прибор, ADR-027)" \
  python3 -c "import sys; sys.path.insert(0,'tools'); import ppl_probe as P; \
sys.exit(0 if P.SETS.get('v3_general')=='datasets/general_eval_v3.txt' and \
P.SETS.get('k2_general')=='datasets/general_eval_k2.txt' else 1)"
expect_exit 0 "наборы K1/K2 читаются (через симлинк кейса на сетевой диск)" \
  test -f datasets/general_eval_v3.txt
expect_exit 0 "набор K2 читается (вне обучающего распределения, ADR-027)" \
  test -f datasets/general_eval_k2.txt

# 18.5 Сторож замеров: на фикстуре (без стенда и без данных) — ждёт чекпойнтов,
# без каталога прогона — отказывает; обе компоненты названы в плане.
mkdir -p "$TMP/shared-full-cpt/full-cpt-20260101-0000/checkpoints"
expect_exit 0 "сторож: чекпойнтов ещё нет — ждём, а не падаем" \
  python3 tools/full_cpt_probe.py --ts 20260101-0000 --shared "$TMP/shared-full-cpt" --once
expect_contains "ждём чекпойнтов" "сторож говорит вслух, что ждёт чекпойнтов" \
  python3 tools/full_cpt_probe.py --ts 20260101-0000 --shared "$TMP/shared-full-cpt" --once
expect_exit 2 "сторож отказывает без каталога прогона (NOT-VERIFIED)" \
  python3 tools/full_cpt_probe.py --ts 19000101-0000 --shared "$TMP/shared-full-cpt" --once
expect_contains "K1 = v3_general, K2 = k2_general" \
  "план сторожа называет компоненты и их наборы" \
  python3 tools/full_cpt_probe.py --ts 20260101-0000 --shared "$TMP/shared-full-cpt" --plan
expect_contains "20 точек" \
  "план сторожа называет число точек (19 промежуточных + финальная)" \
  python3 tools/full_cpt_probe.py --ts 20260101-0000 --shared "$TMP/shared-full-cpt" --plan
#: Журнал сторожа пишется в кейс (`runs/<проба>-<ts>/`) — на фикстуре он тоже
#: появляется, и в дереве кейса ему делать нечего: удаляется здесь же, а не «потом».
rm -rf runs/full-cpt-ppl-20260101-0000

echo "== 18. ppl_probe_v3.py (S3q: база на новом наборе и потолок) =="
expect_exit 0 "модуль импортируется без torch/transformers (план проверяется без GPU)" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import ppl_probe_v3 as P3
assert "torch" not in sys.modules, "import ppl_probe_v3 потянул torch"
assert "transformers" not in sys.modules, "import ppl_probe_v3 потянул transformers"
sys.exit(0)
PY
expect_exit 0 "план печатается без модели" python3 tools/ppl_probe_v3.py --plan
expect_exit 0 "историческая база в приборе — из evidence S3h, а не из памяти" \
  python3 - <<'PY'
import json, sys
sys.path.insert(0, "tools")
import ppl_probe_v3 as P3
s3h = json.load(open("evidence/ppl-baseline-v1v2.json", encoding="utf-8"))
arms = json.load(open("evidence/s3m-ppl-arms.json", encoding="utf-8"))
v1 = s3h["ppl"]["v1_general"]["ppl"]
assert abs(P3.OLD_BASE - v1) < 1e-9, f"база разъехалась: {P3.OLD_BASE} против {v1}"
assert abs(P3.OLD_CEILING - arms["ceiling"]) < 1e-9, "потолок разъехался с S3m-2"
assert P3.SETS["v3_general"].endswith("general_eval_v3.txt"), "новый набор не назван"
sys.exit(0)
PY

echo "== 19. S3x: переключение CPT на LR×0.035 (ADR-031) и остановка LR×0.35 (ADR-016) =="

# 19.1 Пик LR — параметр, а не константа: обе конфигурации воспроизводятся одним
# кодом (иначе сравнение конфигураций опиралось бы на разные раннеры).
expect_exit 0 "конфигурация LR×0.35 воспроизводится из того же раннера (откат возможен)" \
  python3 tools/run_full_cpt.py --plan --ts 20260101-0000 --peak-lr-scale 0.35
expect_contains "peak_lr_scale 0.35 — 25-0.35 (S3m)" \
  "в плане названа историческая конфигурация своим именем (рука S3m)" \
  python3 tools/run_full_cpt.py --plan --ts 20260101-0000 --peak-lr-scale 0.35
expect_contains "ADR-029 п.2" "историческая конфигурация названа своим ADR, а не выдана за текущую" \
  python3 tools/run_full_cpt.py --plan --ts 20260101-0000 --peak-lr-scale 0.35
expect_contains "domain_eval_v2.txt" "план называет доменный набор (ADR-031 п.4) на тех же точках" \
  python3 tools/run_full_cpt.py --plan --ts 20260101-0000
expect_contains "C2 (S3o/S3v)" "план называет руку, у которой конфигурация взята" \
  python3 tools/run_full_cpt.py --plan --ts 20260101-0000

# 19.2 Домен — та же мера и тот же прибор: набор зарегистрирован в приборе, базовое
# число сверяется с эталоном S3h (правка эталона обязана ломать сторожа).
expect_exit 0 "набор домена зарегистрирован в приборе ppl_probe (ADR-031 п.4)" \
  python3 -c "import sys; sys.path.insert(0,'tools'); import ppl_probe as P; \
sys.exit(0 if P.SETS.get('v2_domain')=='datasets/domain_eval_v2.txt' else 1)"
expect_exit 0 "набор домена читается (через симлинк кейса на сетевой диск, AD-4)" \
  test -f datasets/domain_eval_v2.txt
expect_exit 0 "домен входит в компоненты сторожа и его база совпадает с эталоном S3h" \
  python3 -c "import sys, json; sys.path.insert(0,'tools'); import full_cpt_probe as F; \
ref=json.load(open('evidence/ppl-baseline-v1v2.json'))['ppl']; \
sys.exit(0 if F.COMPONENTS.get('DOMAIN')=='v2_domain' and 'v2_domain' in F.SETS \
   and abs(F.BASELINE['v2_domain']-ref['v2_domain']['ppl'])<1e-12 else 1)"
#: Родословная чисел названа по частям, а не «всё из эталона»: в S3h лежат только
#: наборы v1/v2 (v3 и k2 добавлены позже, ADR-025/ADR-027) — их базовое число
#: берётся из отчёта прибора на базовых весах (`states.base.sets.*`), и это
#: проверяемо по артефакту, а не по памяти.
expect_exit 0 "базовые числа v1_general и v2_domain совпадают с эталоном S3h" \
  python3 -c "import sys, json; sys.path.insert(0,'tools'); import full_cpt_probe as F; \
ref=json.load(open('evidence/ppl-baseline-v1v2.json'))['ppl']; \
sys.exit(0 if abs(F.BASELINE['v1_general']-ref['v1_general']['ppl'])<1e-12 \
           and abs(F.BASELINE['v2_domain']-ref['v2_domain']['ppl'])<1e-12 else 1)"
expect_exit 0 "базовые числа K1/K2 совпадают с базовым состоянием в отчётах точек" \
  python3 -c "import sys, json, pathlib, re; sys.path.insert(0,'tools'); import full_cpt_probe as F; \
d=pathlib.Path('runs/full-cpt-ppl-20260916-1933'); \
(sys.exit(0) if not d.is_dir() else None); \
reps=sorted(p for p in d.glob('s*.json') if re.fullmatch(r's\\d+\\.json', p.name)); \
got=json.loads(reps[-1].read_text())['states']['base']['sets']; \
sys.exit(0 if abs(F.BASELINE['v3_general']-got['v3_general']['ppl'])<1e-12 \
           and abs(F.BASELINE['k2_general']-got['k2_general']['ppl'])<1e-12 else 1)"
expect_exit 0 "тождество прибора доказано числом: расхождение базовых чисел 0.0 при допуске 1e-4" \
  python3 -c "import sys, json; sys.path.insert(0,'tools'); import run_full_cpt as R; \
p=R.identity_proof('20260916-1933'); \
sys.exit(0 if p['available'] and p['all_within_1e-4'] is True \
           and all(v['abs_delta']==0.0 for v in p['numbers'].values()) else 1)"
expect_exit 0 "доказательство тождества не выдумывает числа при отсутствии журнала" \
  python3 -c "import sys; sys.path.insert(0,'tools'); import run_full_cpt as R; \
p=R.identity_proof('19000101-0000'); sys.exit(0 if p['available'] is False else 1)"
expect_contains "домен = v2_domain" "план сторожа называет доменную компоненту" \
  python3 tools/full_cpt_probe.py --ts 20260101-0000 --shared "$TMP/shared-full-cpt" --plan

# 19.3 Правило точек ADR-030 — механически, по журналу. Фикстуры журнала: три
# состояния (в потолке / выше потолка / не измерено) обязаны различаться, иначе
# «нет числа» читается как «деградация» (или наоборот) и правило врёт.
mkdir -p "$TMP/probe-fixtures"
python3 - "$TMP/probe-fixtures" <<'PYFIX'
import json, sys, pathlib
root = pathlib.Path(sys.argv[1])

def journal(name, rows):
    pts = {}
    for step, (k1, k2, dom) in rows:
        #: Ключи отношений — те же, что пишет сторож: `<компонента>@<имя точки>`.
        #: Фикстура с другим ключом проверяла бы парсер, которого нет.
        pt = f"s{step}"
        pts[pt] = {"step": step, "ckpt": f"checkpoints/calib_checkpoint_{step}.pt",
                   "verdict": {"baseline_ok": True,
                               "ratios": {f"K1@{pt}": k1, f"K2@{pt}": k2,
                                          f"DOMAIN@{pt}": dom}}}
    (root / name).write_text(json.dumps({"ts": "fix", "points": pts, "steps": 9776,
                                         "baseline": {"v3_general": 7.50468637420902,
                                                      "k2_general": 6.1599356842437585,
                                                      "v1_general": 11.931923888434497,
                                                      "v2_domain": 11.134115855539092}},
                                        ensure_ascii=False), encoding="utf-8")

# норма: ступенька откатилась, к первой четверти всё в потолке, домен выучен
journal("ok.json", [(500, (1.4, 1.5, 0.9)), (1000, (1.2, 1.3, 0.7)),
                    (2000, (1.05, 1.1, 0.5)), (2500, (1.005, 1.053, 0.384))])
# тревога: точка выше потолка, следующая не лучше
journal("alarm.json", [(500, (2.4, 2.5, 0.9)), (1000, (2.5, 2.6, 0.8)),
                       (1500, (1.4, 1.5, 0.7)), (2500, (1.1, 1.2, 0.4))])
# остановка: три точки подряд выше потолка, каждая хуже предыдущей
journal("stop.json", [(500, (2.1, 2.2, 0.9)), (1000, (2.2, 2.3, 0.8)),
                      (1500, (2.3, 2.4, 0.7)), (2500, (1.1, 1.2, 0.4))])
# первая четверть: к шагу 2500 конъюнкция не в потолке
journal("quarter.json", [(500, (1.2, 1.3, 0.9)), (2500, (2.1, 2.2, 0.4))])
# домен не измерен: «нет числа» обязано читаться как «не проверено», а не «нарушено»
j = json.loads((root / "ok.json").read_text(encoding="utf-8"))
for name, pt in j["points"].items():
    pt["verdict"]["ratios"].pop(f"DOMAIN@{name}", None)
(root / "nodomain.json").write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
PYFIX
for fx in ok alarm stop quarter nodomain; do
  mkdir -p "runs/full-cpt-ppl-fix-$fx"
  cp "$TMP/probe-fixtures/$fx.json" "runs/full-cpt-ppl-fix-$fx/state.json"
done
expect_exit 0 "норма: ступенька откатилась, первая четверть в потолке — тренд зелёный" \
  python3 tools/full_cpt_probe.py --ts fix-ok --trend
expect_contains "первая четверть (шаг ≥ 2444): точка s2500 — в потолке" \
  "вердикт первой четверти назван точкой, а не общими словами" \
  python3 tools/full_cpt_probe.py --ts fix-ok --trend
expect_exit 1 "тревога ADR-030: точка выше потолка и следующая не лучше — сигнал, не остановка" \
  python3 tools/full_cpt_probe.py --ts fix-alarm --trend
expect_contains "ТРЕВОГА" "тревога произносится вслух" \
  python3 tools/full_cpt_probe.py --ts fix-alarm --trend
expect_exit 1 "остановка ADR-030 п.4: рост на трёх точках выше потолка" \
  python3 tools/full_cpt_probe.py --ts fix-stop --trend
expect_contains "ОСТАНОВКА (ADR-030 п.4)" "остановка отличается от тревоги, а не слита с ней" \
  python3 tools/full_cpt_probe.py --ts fix-stop --trend
expect_exit 1 "первая четверть ADR-030 п.5: конъюнкция не в потолке — нарушение" \
  python3 tools/full_cpt_probe.py --ts fix-quarter --trend
expect_contains "НАРУШЕН" "нарушение контрольной точки не молчит" \
  python3 tools/full_cpt_probe.py --ts fix-quarter --trend
expect_exit 0 "домен не измерен — это «не проверено», а не ложное нарушение" \
  python3 tools/full_cpt_probe.py --ts fix-nodomain --trend
expect_contains "не проверено" "отсутствие числа названо своим именем" \
  python3 tools/full_cpt_probe.py --ts fix-nodomain --trend
expect_exit 2 "тренд без снятых точек — NOT-VERIFIED, а не «норма»" \
  python3 tools/full_cpt_probe.py --ts 19000101-0000 --trend
rm -rf runs/full-cpt-ppl-fix-ok runs/full-cpt-ppl-fix-alarm runs/full-cpt-ppl-fix-stop \
       runs/full-cpt-ppl-fix-quarter runs/full-cpt-ppl-fix-nodomain

# 19.4 Остановка живой стадии (ADR-016 п.2) на фикстуре: стенда нет, «ssh» и
# «docker» — заглушки, которые выполняют команду локально и пишут порядок вызовов.
# Порядок проверяется не для красоты: маркер обязан быть записан ДО остановки
# контейнера, иначе цепочка запишет стадии `failed` вместо `stopped`.
cat > "$TMP/fakebin/ssh" <<'SH'
#!/usr/bin/env bash
printf 'ssh %s\n' "$*" >> "${FAKE_ORDER_LOG:-/dev/null}"
args=("$@"); i=0
while [ $i -lt ${#args[@]} ]; do
  case "${args[$i]}" in
    -o) i=$((i+2)) ;;
    -*) i=$((i+1)) ;;
    *) break ;;
  esac
done
cmd="${args[@]:$((i+1))}"
[ -n "$cmd" ] || exit 0
bash -c "$cmd"
SH
#: Заглушка docker повторяет **наблюдаемое** поведение: `ps` печатает имена
#: работающих контейнеров, `ps -a` — существующих (после `stop` контейнер исчезает:
#: цепочка запускает его с `--rm`), `stop` его гасит. Имя контейнера задаётся
#: `FAKE_CTR` — сторож строит его сам (`laguna-<каталог прогона>-cpt`).
cat > "$TMP/fakebin/docker" <<'SH'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >> "${FAKE_ORDER_LOG:-/dev/null}"
state="${FAKE_DOCKER_STATE:-/dev/null}"
running() { [ -f "$state" ] && return 1; return 0; }
case "$1" in
  stop) : > "$state"; echo "${FAKE_CTR:-ctr}"; exit 0 ;;
  inspect) running && { echo true; exit 0; }; exit 1 ;;
  ps) # `-a` в этой заглушке не отличается от обычного: контейнер либо есть, либо нет
      running && echo "${FAKE_CTR:-ctr}"; exit 0 ;;
esac
exit 0
SH
chmod +x "$TMP/fakebin/ssh" "$TMP/fakebin/docker"

STOP_TS=20260101-0100
STOP_SHARED="$TMP/shared-stop"
STOP_RUN="$STOP_SHARED/full-cpt-$STOP_TS"
rm -f "$TMP/docker-state" "$TMP/stop-order.log"
mkdir -p "$STOP_RUN/checkpoints" "$STOP_RUN/logs" "$STOP_RUN/var/status"
printf '{"step": 2651, "loss": 1.7, "lr": 0.000175, "t": 1}\n' > "$STOP_RUN/logs/loss_trace.jsonl"
printf '{"step": 2652, "loss": 1.8, "lr": 0.000175, "t": 2}\n' >> "$STOP_RUN/logs/loss_trace.jsonl"
printf 'cpt\tcpt\tcpt\tcheckpoints/checkpoint_final.pt\t9776\t1\t-\tfull-cpt-20260101-0100\n' > "$STOP_RUN/stages.tsv"
printf 'running\n' > "$STOP_RUN/var/chain.status"
head -c 4096 /dev/urandom > "$STOP_RUN/checkpoints/calib_checkpoint_500.pt"
printf '{"dataset_sha256": "%064d", "base_model_id": "Qwen/Qwen2.5-0.5B", "pipeline_version": "p@x", "image": "img", "seed": 42, "stages": [{"name": "cpt", "status": "running"}], "pipeline_complete": false}\n' 0 > "$STOP_RUN/run_manifest.json"
run_stop() {
  env PATH="$TMP/fakebin:$PATH" FAKE_DOCKER_STATE="$TMP/docker-state" \
      FAKE_ORDER_LOG="$TMP/stop-order.log" FAKE_CTR="laguna-full-cpt-$STOP_TS-cpt" \
      python3 tools/run_full_cpt.py --stop --ts "$STOP_TS" --shared "$STOP_SHARED" \
      --stand fake-stand --stop-settle-seconds 5 "$@"
}
expect_exit 2 "остановка без каталога прогона — NOT-VERIFIED (не «остановлено»)" \
  python3 tools/run_full_cpt.py --stop --ts 19000101-0000 --shared "$STOP_SHARED" --stand fake-stand
expect_contains "контейнер yes" "сторож видит работающий контейнер (иная ветка — не остановка)" \
  run_stop
expect_exit 0 "остановка фикстуры проходит штатным путём цепочки" run_stop
expect_exit 0 "маркер остановки записан в каталог прогона (цепочка увидит его)" \
  test -f "$STOP_RUN/var/STOPPED"
expect_contains "ADR-031" "причина остановки названа в маркере, а не «стоп-условие»" \
  head -1 "$STOP_RUN/var/STOPPED"
expect_exit 0 "маркер записан ДО остановки контейнера (иначе статус стадии был бы failed)" \
  python3 -c "import sys,pathlib; \
lines=pathlib.Path('$TMP/stop-order.log').read_text().splitlines(); \
mk=[i for i,l in enumerate(lines) if 'var/STOPPED' in l]; \
st=[i for i,l in enumerate(lines) if l.startswith('docker stop')]; \
sys.exit(0 if mk and st and min(mk)<min(st) else 1)"
expect_exit 0 "жёсткое убийство не применялось (docker kill в журнале вызовов отсутствует)" \
  python3 -c "import sys,pathlib; \
sys.exit(1 if 'docker kill' in pathlib.Path('$TMP/stop-order.log').read_text() else 0)"
expect_exit 0 "пометка stopped_by: ADR-031 в манифесте прогона (AD-2)" \
  python3 -c "import json, sys; d=json.load(open('$STOP_RUN/run_manifest.json')); \
sys.exit(0 if d['stop']['stopped_by']=='ADR-031' and d['stages'][0]['status']=='stopped' else 1)"
expect_exit 0 "stopped_at_step и purpose названы числом и словами решения" \
  python3 -c "import json, sys; d=json.load(open('$STOP_RUN/run_manifest.json'))['stop']; \
sys.exit(0 if d['stopped_at_step']==2652 and 'цена высокого LR' in d['purpose'] else 1)"
expect_exit 0 "чекпойнт остановленного прогона сохранён (ничего не удалено)" \
  test -f "$STOP_RUN/checkpoints/calib_checkpoint_500.pt"
expect_exit 0 "запись остановки легла и в кейс (runs/<ts>/stopped.json)" \
  test -f "runs/full-cpt-$STOP_TS/stopped.json"
expect_exit 0 "в записи остановки — перечень сохранённого с размерами и хешами" \
  python3 -c "import json, sys; d=json.load(open('runs/full-cpt-$STOP_TS/stopped.json')); \
ck=d['artifacts_kept']['checkpoints']['calib_checkpoint_500.pt']; \
sys.exit(0 if ck['bytes']==4096 and d['artifacts_kept']['files']['logs/loss_trace.jsonl']['sha256'] else 1)"
expect_exit 0 "траектория лосса разобрана целиком (остановка не оставила усечённой строки)" \
  python3 -c "import json, sys; d=json.load(open('runs/full-cpt-$STOP_TS/stopped.json')); \
sys.exit(0 if d['loss_trace']['intact'] and d['loss_trace']['last_step']==2652 else 1)"
expect_exit 0 "повторная остановка идемпотентна (контейнер уже не работал — не отказ)" run_stop

# Прогон, у которого есть финальный артефакт, не останавливают: это завершённая стадия.
STOP2_TS=20260101-0101
mkdir -p "$STOP_SHARED/full-cpt-$STOP2_TS/checkpoints" "$STOP_SHARED/full-cpt-$STOP2_TS/var/status"
printf 'x' > "$STOP_SHARED/full-cpt-$STOP2_TS/checkpoints/checkpoint_final.pt"
expect_exit 1 "прогон с финальным артефактом не останавливается (ОТКАЗ, а не тихая остановка)" \
  env PATH="$TMP/fakebin:$PATH" FAKE_DOCKER_STATE="$TMP/docker-state" \
      FAKE_ORDER_LOG="$TMP/stop-order.log" FAKE_CTR="laguna-full-cpt-$STOP2_TS-cpt" \
      python3 tools/run_full_cpt.py --stop --ts "$STOP2_TS" --shared "$STOP_SHARED" \
      --stand fake-stand
rm -rf runs/full-cpt-$STOP_TS "runs/full-cpt-$STOP2_TS" "$STOP_SHARED" \
       "$TMP/fakebin/ssh" "$TMP/fakebin/docker"

echo
# ─────────────────────────────────────────────────────────────────────────────
# 20. S3aa: запуск SFT-стадии (вход — CPT-чекпойнт, монитор на K1/K2/домен)
#
# Что здесь проверяется и почему именно это:
#  * патч пайплайна обязан ПАДАТЬ на неверной базе, а не применять «похожий»
#    анкер: молча пропатченный не тот файл — это стадия, контролируемая не тем
#    прибором, и цена ошибки — 50+ часов стенда;
#  * монитор в пропатченной копии обязан мерить K1/K2/домен (ADR-033 п.3), а
#    штатные наборы — оставаться только справочными;
#  * точки замера обязаны идти под именем `sft_probe_<step>.pt`, а НЕ под
#    штатным (`sft_checkpoint_<step>.pt`): штатную ретенцию (`keep_last=2`) они
#    не переживут, и точка шага 500 исчезла бы на 1000 — дефект S3m;
#  * поведение сторожа для CPT обязано остаться байт-в-байт прежним: старые
#    доказательства сняты этим же прибором.
echo "== 20. S3aa: запуск SFT-стадии (ADR-032 → ADR-033) =="

# 20.1 Патч пайплайна отказывает на базе без анкеров (а не патчит «похожее»).
printf 'def foo():\n    return 1\n' > "$TMP/not_a_pipeline.py"
expect_exit 1 "patch_pipeline_sft: база без анкеров → ОТКАЗ (анкер не найден)" \
  python3 tools/patch_pipeline_sft.py --base "$TMP/not_a_pipeline.py" --out "$TMP/x.py" --check

# 20.2 Патч на настоящей базе: собирается, синтаксис валиден, анкеры описаны.
expect_exit 0 "patch_pipeline_sft: база контура → патч собирается (--check)" \
  python3 tools/patch_pipeline_sft.py --base laguna_pipeline_v8.py --out "$TMP/sft_pipe.py" \
  --patch-json "$TMP/patch.json" --check
expect_exit 0 "patch_pipeline_sft: паспорт патча несёт пять применённых правок и sha256" \
  python3 - "$TMP/patch.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
# S3av добавила правку идентичности SFT-набора, S3be — идентичности набора
# курикулума RL (ADR-054 п.2). Считаем ровно их, чтобы «применилось меньше, чем
# задумано» не проходило за успех.
assert len(d["applied"]) == 5, d["applied"]
assert len(d["pipeline_base_sha256"]) == 64 and len(d["patched_sha256"]) == 64
assert set(d["anchors"]) == {"monitor", "trace", "ckpt", "helpers", "sft_init",
                             "sft_val_npz", "sft_argparse", "sft_identity_call",
                             "datasets_hash", "rl_init", "rl_argparse",
                             "rl_identity_call"}, d["anchors"]
assert d["probe_keep_last"] >= 1
# Оба носителя идентичности объявлены и оба сторожит один прибор: механизм один,
# а не параллельный (ADR-054 п.2).
assert d["sft_input_identity"]["guard"] == "tools/check_dataset_identity.py"
rl = d["rl_input_identity"]
assert rl["guard"].startswith("tools/check_dataset_identity.py"), rl
assert rl["recorded_in"] == "run_manifest.json стадии (checkpoints/), блок rl_input", rl
assert "declared_sha256" in rl["fields"] and "tasks_loaded" in rl["fields"], rl
# Зуб на носитель: у набора курикулума тензора нет — обязательный tensor был бы
# красным по построению (ADR-023 п.12), и это названо, а не забыто.
assert "no_tensor" in rl and "tensor" not in rl["fields"], rl
PY

# 20.3 Патч реально кладёт три хука стадии в текст пайплайна.
expect_exit 0 "patch_pipeline_sft: в копии есть траектория, точки замера и монитор K1/K2/домена" \
  python3 - <<'PY'
import pathlib, subprocess, sys, tempfile
with tempfile.TemporaryDirectory() as t:
    out = pathlib.Path(t) / "p.py"
    subprocess.run([sys.executable, "tools/patch_pipeline_sft.py", "--base",
                    "laguna_pipeline_v8.py", "--out", str(out)], check=True,
                   capture_output=True)
    txt = out.read_text(encoding="utf-8")
    compile(txt, str(out), "exec")                       # копия обязана компилироваться
    assert "_sft_trace(step, loss.item(), scheduler.get_last_lr()[0], args)" in txt, "нет хука траектории"
    assert 'f"sft_probe_{step}.pt"' in txt, "нет чекпойнта-точки под своим именем"
    assert "keep_last=PROBE_KEEP_LAST" in txt, "точки не выведены из ретенции"
    assert "_SFT_MONITOR_SETS" in txt and "_SFT_MONITOR_REFERENCE" in txt
    # Монитор — на действующих наборах; исторические — только справочно.
    for decisive in ("general_eval_v3.txt", "general_eval_k2.txt", "domain_eval_v2.txt"):
        assert f"datasets/{decisive}" in txt, decisive
    assert txt.index("_SFT_MONITOR_REFERENCE = (") > txt.index("_SFT_MONITOR_SETS = ("), \
        "справочные наборы объявлены раньше решающих"
PY

# 20.4 Точки замера: у SFT — свои имена, у CPT — прежние (регресс на доказательства CPT).
expect_exit 0 "full_cpt_probe.points: SFT берёт sft_probe_<step>.pt и финал стадии" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("probe", pathlib.Path("tools/full_cpt_probe.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
m.CKPT_PATTERN, m.FINAL_CKPT = "sft_probe_{step}.pt", "sft_checkpoint_final.pt"
pts = m.points(1500, 500)
assert [p["ckpt"] for p in pts] == [
    "checkpoints/sft_probe_500.pt", "checkpoints/sft_probe_1000.pt",
    "checkpoints/sft_checkpoint_final.pt"], pts
assert pts[-1]["step"] == 1500 and pts[-1].get("decisive") is True
PY
expect_exit 0 "full_cpt_probe.points: умолчания = CPT (calib_checkpoint_*, behavior не сдвинут)" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("probe", pathlib.Path("tools/full_cpt_probe.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
assert m.CKPT_PATTERN == "calib_checkpoint_{step}.pt", m.CKPT_PATTERN
assert m.FINAL_CKPT == "checkpoint_final.pt", m.FINAL_CKPT
assert m.points(1000, 500)[0]["ckpt"] == "checkpoints/calib_checkpoint_500.pt"
PY

# 20.5 Маркер «не прогон» называет стадию: ложное имя в файле читает страж C-012.
expect_exit 0 "not_a_run_text: SFT-каталог не объявляется каталогом CPT" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("probe", pathlib.Path("tools/full_cpt_probe.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
m.SESSION_PREFIX = "sft"
sft = m.not_a_run_text()
m.SESSION_PREFIX = "full-cpt"
cpt = m.not_a_run_text()
assert "SFT" in sft and "CPT" not in sft, sft
assert "полного CPT" in cpt, cpt
assert "runs/sft-<ts>/" in sft and "runs/full-cpt-<ts>/" in cpt
PY

# 20.6 Команда цепочки: стадия одна, вход — CPT-финал, монитор и точки объявлены.
expect_exit 0 "launch_sft_stage: команда цепочки несёт вход CPT, шаги 67423 и приметы ADR-033" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("launch", pathlib.Path("tools/launch_sft_stage.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
cmd = m.build_chain_command({"name": "sft-X", "ctr_run_dir": "/workspace/shared/sft-X"},
                            pathlib.Path("/home/user/gb10-shared/sft-X"))
assert "--stages-file /home/user/gb10-shared/sft-X/stages.tsv" in cmd
assert "--pipeline-ctr /workspace/shared/sft-X/laguna_pipeline_sft.py" in cmd
assert "cpt_ckpt_sha256=" + m.CPT_CKPT_SHA256 in cmd
assert f"sft_steps={m.SFT_STEPS}" in cmd and m.SFT_STEPS == 67423, m.SFT_STEPS
assert "monitor_sets=K1:general_eval_v3,K2:general_eval_k2,DOMAIN:domain_eval_v2" in cmd
assert "--sampler-seconds" in cmd and "--stall-minutes" in cmd
PY

# 20.6a Маска лосса в команде цепочки: think_masked называет и слив, и решение.
#       Нужен потому, что остановленный прогон и его перезапуск различались бы иначе
#       только хешем копии пайплайна — по манифесту разницы не видно (ADR-036 п.4),
#       а после ADR-037 «с маской» обязан быть ещё и явный признак решения.
expect_exit 0 "launch_sft_stage: loss_mask и adr названы в команде цепочки, маска не молчит" \
  python3 - <<'PY2'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("launch", pathlib.Path("tools/launch_sft_stage.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
common = {"name": "sft-X", "ctr_run_dir": "/workspace/shared/sft-X"}
plain = m.build_chain_command({**common, "loss_mask": "prefix_only"},
                              pathlib.Path("/home/user/gb10-shared/sft-X"))
masked = m.build_chain_command({**common, "loss_mask": "think_masked"},
                               pathlib.Path("/home/user/gb10-shared/sft-X"))
assert "loss_mask=prefix_only" in plain and "adr=ADR-033 " in plain, plain
assert "adr=ADR-033+ADR-036" not in plain
assert "loss_mask=think_masked" in masked and "adr=ADR-033+ADR-036" in masked
assert "--think-mask" not in masked, "флаг патча не должен ехать в команду цепочки"
partial = m.build_chain_command(common, pathlib.Path("/home/user/gb10-shared/sft-X"))
assert "loss_mask=prefix_only" in partial, "частичный словарь обязан давать умолчание"
PY2

# 20.7 Порог свободной памяти карты: умолчание прежнее (5 ГБ), но поднимается флагом.
#      Нужен потому, что на 4080 идут ДВЕ пробы, и порог ниже фактической потребности
#      превращает ожидание в отказ, а отказ съедает бюджет попыток точки (наблюдено).
expect_exit 0 "min-free-gb: умолчание 5 ГБ сохранено, флаг объявлен и принимается" \
  python3 - <<'PY'
import importlib.util as u, pathlib, subprocess, sys
spec = u.spec_from_file_location("probe", pathlib.Path("tools/full_cpt_probe.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
assert m.DEVICE_FREE_BYTES_MIN == 5 * 1024 ** 3, m.DEVICE_FREE_BYTES_MIN
help_txt = subprocess.run([sys.executable, "tools/full_cpt_probe.py", "--help"],
                          capture_output=True, text=True).stdout
assert "--min-free-gb" in help_txt, "флаг порога не объявлен"
# порог, а не «жёсткий отказ»: цикл обязан уметь ждать, не тратя попытку
src = pathlib.Path("tools/full_cpt_probe.py").read_text(encoding="utf-8")
assert 'return "waiting-gpu"' in src and "free < DEVICE_FREE_BYTES_MIN" in src
PY


echo
echo "== 21. probe_language_split.py / launch_sft_stage --resume (S3ai: раздельные языки, ADR-039) =="

# 21.1 Фикстуры разбора на сегменты: ответ без рассуждения, <think> без закрытия,
#      одиночный </think> (баг v7/v8), два блока, разметка не считается речью.
expect_exit 0 "probe_language_split: фикстуры разбора проходят (нет ответа, незакрытый think)" \
  python3 tools/probe_language_split.py --selftest

# 21.2 Правила разбора — обращением к функциям, а не к CLI: прибор обязан
#      разделять сегменты там, где общий скор их смешивает (ADR-039 п.2).
expect_exit 0 "probe_language_split: сегменты и правило «разметка — не буквы»" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

# ответ без рассуждения: весь ход — ответная часть
s = m.split_segments("Хлеб пекут из муки.")
assert s["think"] == "" and s["answer"] == "Хлеб пекут из муки."
assert m.language_metrics("Хлеб пекут из муки.")["cyr_answer"] == 1.0

# <think> без закрытия: остаток хода — рассуждение, ответной части нет
s = m.split_segments("Начало.<think>The user asks and I keep going")
assert s["unclosed_think"] is True and s["answer"] == "Начало."
assert m.language_metrics("Начало.<think>The user asks")["cyr_think"] == 0.0

# нормальный ход: разделение работает, теги в долю не входят
mt = m.language_metrics("<think>Reasoning in English.</think>Ответ по-русски.")
assert mt["cyr_think"] == 0.0 and mt["cyr_answer"] == 1.0, mt
# если бы теги считались буквами, доля в think была бы < 1.0 у чистой латиницы
assert mt["letters_think"] == len("ReasoninginEnglish"), mt["letters_think"]

# одиночный </think> без открытия — помечен, но в ответной части как текст не считается
s = m.split_segments("</think>Ответ без открывающего тега.")
assert s["stray_think_close"] is True and m.share(s["answer"]) == 1.0

# нет букв — доля None, а не 0.0 (иначе «нет текста» читалось бы как «латиница»)
assert m.share("<think></think>") is None
assert m.language_metrics("<think></think>")["cyr_answer"] is None

# tool_call/tool_response — не проза: отдельная диагностика cyr_answer_prose
mt = m.language_metrics('<think>x</think><tool_call>{"name": "q"}</tool_call>'
                        '<tool_response>slug: grpo</tool_response>Итог: grpo.')
# ответная часть: 4 кириллицы («Итог») против 17 латинских букв JSON и slug'ов
assert mt["cyr_answer"] == 0.1905 and mt["cyr_answer_prose"] == 0.5, mt
PY

# 21.3 Свод: основная метрика (с нулями) отделена от диагностики покрытия,
#      режим и обрезка считаются отдельно — без них «нет ответа» неотличимо
#      от «ответ не влез в лимит».
expect_exit 0 "probe_language_split: свод — with_zeros/defined_only/coverage, режим, обрезка" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

def rec(tag, text, hit=False):
    return {"tag": tag, "hit_limit": hit, "metrics": m.language_metrics(text)}

recs = [rec("a", "<think>English reasoning.</think>Русский ответ."),
        rec("a", "<think>Only reasoning, never closed"),
        rec("b", "Ответ без рассуждения."),
        rec("b", "<think>x</think>", hit=True)]
agg = m.aggregate(recs)
assert agg["n"] == 4
assert agg["cyr_answer"]["defined"] == 2 and agg["cyr_answer"]["coverage"] == 0.5
assert agg["cyr_answer"]["with_zeros"] == 0.5, agg["cyr_answer"]
assert agg["cyr_answer"]["defined_only"] == 1.0, agg["cyr_answer"]
assert agg["cyr_think"]["defined"] == 3 and agg["cyr_think"]["with_zeros"] == 0.0
assert agg["mode_share_think"] == 0.75 and agg["mode_share_any"] == 0.75
assert agg["unclosed_think_share"] == 0.25 and agg["truncated_share"] == 0.25
assert agg["per_tag"]["b"]["n"] == 2 and agg["per_tag"]["b"]["cyr_answer"] == 0.5
assert "cyr_overall_reference_only" in agg, "общий скор обязан быть помечен справочным"
PY

# 21.4 Критерий ADR-039 п.3: четыре зоны (три объявленных + неописанная комбинация)
#      и страж покрытия. Симметрия стража проверяется нарочно: он обязан блокировать
#      вердикт и «за», и «против», иначе он не страж, а подыгрывание.
expect_exit 0 "probe_language_split: критерий — зоны ADR-039 и страж покрытия (симметрично)" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
assert (m.CRIT_ANSWER_RU_MIN, m.CRIT_THINK_EN_MAX, m.CRIT_ANSWER_REFUTE_MAX) == (0.5, 0.3, 0.4)

assert m.apply_criterion(0.6, 0.1)["zone"] == "confirmed"
assert m.apply_criterion(0.5, 0.3)["zone"] == "confirmed", "границы включительны"
assert m.apply_criterion(0.39, 0.1)["zone"] == "refuted"
assert m.apply_criterion(0.4, 0.1)["zone"] == "intermediate"
assert m.apply_criterion(0.4999, 0.1)["zone"] == "intermediate"
assert m.apply_criterion(0.9, 0.31)["zone"] == "out_of_scope_think_not_english"
assert m.apply_criterion(None, 0.1)["zone"] == "not_measured"

# страж покрытия: обрезанная генерация не даёт ни подтвердить, ни опровергнуть
assert m.apply_criterion(0.9, 0.1, answer_coverage=0.2)["zone"] == "insufficient_coverage"
assert m.apply_criterion(0.1, 0.1, answer_coverage=0.2)["zone"] == "insufficient_coverage"
assert m.apply_criterion(0.9, 0.1, answer_coverage=0.5)["zone"] == "confirmed"
PY

# 21.5 Красные пути CLI: неизвестный набор промптов и вход без чекпойнтов.
#      Оба обязаны отказать до загрузки весов (иначе «отказ» стоит минуты GPU).
expect_exit 2 "probe_language_split: неизвестный набор промптов — отказ (exit 2)" \
  python3 tools/probe_language_split.py --prompts bogus --out "$TMP/lang_bogus.json"
expect_exit 2 "probe_language_split: ни одного --ckpt — NOT-VERIFIED до загрузки модели" \
  python3 tools/probe_language_split.py --ckpt broken --out "$TMP/lang_nockpt.json"
expect_contains "модель не загружалась" "probe_language_split: отказ назван причиной, а не молчанием" \
  python3 tools/probe_language_split.py --ckpt broken --out "$TMP/lang_nockpt.json"

# 21.6 Ядро протокола — импорт из probe_control, а не копия. Если это станет копией,
#      сопоставимость с S3ab рассыпется молча (промпты разойдутся при первой правке).
expect_exit 0 "probe_language_split: ядро промптов и системный промпт — из probe_control" \
  python3 - <<'PY'
import importlib.util as u, pathlib, sys
sys.path.insert(0, "tools")
import probe_control as PC
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
assert m.UNIFIED_SYSTEM_PROMPT == PC.UNIFIED_SYSTEM_PROMPT
assert m.PROMPT_SETS["core"] == PC.PROBE_PROMPTS, "ядро обязано совпадать с S3ab дословно"
assert m.LANG_PROMPTS[:5] == PC.PROBE_PROMPTS
assert len(m.PROMPT_SETS["extended"]) == 24, len(m.PROMPT_SETS["extended"])
assert m.MAX_NEW_TOKENS == 384, "протокол S3ab — 384 токена"
assert m.LANG_MAX_NEW_TOKENS > m.MAX_NEW_TOKENS
assert m.prompts_digest(m.PROMPT_SETS["core"]) == m.prompts_digest(list(PC.PROBE_PROMPTS))
# набор пинится хешем: без него «тот же набор промптов» не проверить
assert len(m.prompts_digest(m.LANG_PROMPTS)) == 64
PY

# 21.7 Возобновление SFT: точка ложится под **штатным** именем (иначе пайплайн
#      начал бы с CPT-входа и «возобновление» стало бы перезапуском), хеш сверен,
#      расписание LR не сдвинуто (шаги те же 67423), признаки resume — в команде.
expect_exit 0 "launch_sft_stage --resume: штатное имя точки, хеш сверен, шаги и resume_* в команде" \
  python3 - "$TMP" <<'PY'
import hashlib, importlib.util as u, inspect, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("launch", pathlib.Path("tools/launch_sft_stage.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
shared = tmp / "shared7"
(shared / "src-run" / "checkpoints").mkdir(parents=True)
cpt = shared / "cpt.pt"; cpt.write_bytes(b"CPT")
rck = shared / "src-run" / "checkpoints" / "sft_probe_3000.pt"; rck.write_bytes(b"SFT3000")
pipe = b"# pipeline\n"
(shared / "src-run" / "laguna_pipeline_sft.py").write_bytes(pipe)
(shared / "src-run" / "run_manifest.json").write_text(json.dumps(
    {"pipeline_sha256": hashlib.sha256(pipe).hexdigest()}))
m.SHARED = shared
m.CPT_CKPT = cpt
m.CPT_CKPT_SHA256 = hashlib.sha256(b"CPT").hexdigest()
# Данные стадии сборка пиннит ХЕШЕМ, снятым с диска: фикстура обязана их иметь,
# иначе проверялся бы не тот контракт (S3an: набор выбирается именем и сверяется).
(ds_dir := shared / "datasets").mkdir(parents=True)
(dj := ds_dir / "sft_train_v12.jsonl").write_bytes(b'{"messages": []}\n')
m.DATASETS = {**m.DATASETS, "v12": {**m.DATASETS["v12"],
                                    "jsonl_sha256": hashlib.sha256(dj.read_bytes()).hexdigest()}}

resume = m.resume_input(rck)
assert resume["step"] == 3000, "шаг читается из имени: иначе start_step разойдётся с файлом"
assert resume["pipeline_verified_by_manifest"] is True
resume["planned_stop_step"] = 5500
d = m.build("20990101-0000", "prefix_only", resume)
assert d["name"] == "sft-resume-20990101-0000", d["name"]
run = shared / d["name"]
ck = run / "checkpoints" / "sft_checkpoint_3000.pt"
assert ck.is_file()
assert hashlib.sha256(ck.read_bytes()).hexdigest() == hashlib.sha256(b"SFT3000").hexdigest()
assert not list((run / "checkpoints").glob("sft_probe_*.pt")), \
    "под именем sft_probe_N.pt пайплайн точку не найдёт (глоб sft_checkpoint_[0-9]*.pt)"
assert (run / "laguna_pipeline_sft.py").read_bytes() == pipe, "тот же код, что тренировал"
stages = (run / "stages.tsv").read_text().strip().split("\t")
assert stages[4] == str(m.SFT_STEPS) == "67423", stages
cmd = (run / "chain_command.txt").read_text()
for token in ("resume_step=3000", "resume_ckpt_sha256=", "resume_source_run=",
              "resume_pipeline_sha256=", "planned_stop_step=5500",
              "adr=ADR-033+ADR-039", "loss_mask=prefix_only"):
    assert token in cmd, token
params = json.loads((run / "full_sft_params.json").read_text())
assert params["resume"]["from_step"] == 3000 and params["resume"]["first_step"] == 3001
assert params["resume"]["planned_stop_step"] == 5500
# предусловие проверяется по записанной команде, а не по пересобранной
assert "chain_command.txt" in inspect.getsource(m.preflight)
# Красный путь S3an: данные, разошедшиеся с записанным хешем, останавливают сборку
# (иначе прогон ушёл бы на молча подменённом наборе — AD-2 запрещает).
dj.write_bytes('{"messages": [{"role": "user", "content": "\u0434\u0440\u0443\u0433\u043e\u0435"}]}\n'.encode())
try:
    m.build("20990101-0001", "prefix_only", None)
    raise AssertionError("сборка обязана остановиться при расхождении хеша данных")
except SystemExit as exc:
    assert "не совпали с записанным хешем" in str(exc), exc
PY

# 24.6 Выбор набора стадии: v13 даёт свои шаги, свой кэш и свой ADR в манифесте.
#      Шаги считаются от числа примеров набора — «те же гиперпараметры» не должны
#      означать другое число эпох (3 × 44105 // 2 = 66157).
expect_exit 0 "S3an: --dataset v13 — шаги от 44105, кэш v13, ADR-042 в команде" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("launch", pathlib.Path("tools/launch_sft_stage.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
base = {"name": "sft-x", "ctr_run_dir": "/c"}
c12 = m.build_chain_command(dict(base), pathlib.Path("/r"))
assert "sft_train_v12_8192_qwen25.npz" in c12 and "sft_steps=67423" in c12, c12[:200]
assert "ADR-042" not in c12 and "sft_dataset=datasets/sft_train_v12.jsonl" in c12
ds13 = m.DATASETS["v13"]
c13 = m.build_chain_command({**base, "dataset": "v13", "dataset_spec": ds13,
                             "dataset_sha256": ds13["jsonl_sha256"], "steps": 66157},
                            pathlib.Path("/r"))
assert "sft_train_v13_fixed_8192_qwen25.npz" in c13
assert "sft_steps=66157" in c13 and "adr=ADR-033+ADR-042" in c13
assert "sft_dataset=datasets/sft_train_v13_fixed.jsonl" in c13
assert f"sft_dataset_sha256={ds13['jsonl_sha256']}" in c13
assert f"duplicates_share_pct={ds13['duplicates_share_pct']}" in c13
assert 3 * ds13["examples"] // m.SFT_BATCH == 66157
PY

# 21.6a Пометка остановки — параметр: вторую остановку стадии (S3ai/ADR-039,
#       достигнутый шаг) формулировка ADR-036 описала бы неверно, а манифест —
#       это доказательство (AD-2), а не место для удобной неправды.
expect_exit 0 "stop_stage_run: пометка остановки из --stopped-note, умолчание — ADR-036" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("stop", pathlib.Path("tools/stop_stage_run.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
run = tmp / "stop-note-run"; (run / "logs").mkdir(parents=True)
(run / "logs" / "loss_trace.jsonl").write_text('{"step": 5504, "loss": 1.0, "lr": 1e-6}\n')
(run / "run_manifest.json").write_text(json.dumps({"stages": [{"name": "sft", "status": "running"}]}))
note = "прогон остановлен на объявленном шаге 5500 (S3ai/ADR-039)"
m.do_record(run, "ADR-039", "артефакт «SFT после дообучения»", "prefix_only", {}, note=note)
man = json.loads((run / "run_manifest.json").read_text())
assert man["stopped_note"] == note, man["stopped_note"]
assert man["stopped_at_step"] == 5504 and man["stopped_by"] == "ADR-039"
assert man["stages"][0]["status"] == "stopped"
rec = json.loads((run / "STOP-RECORD.json").read_text())
assert rec["stopped_at_step"] == 5504 and rec["purpose"].startswith("артефакт")
# без параметра — прежнее умолчание: остановка S3ae читается по-старому
run2 = tmp / "stop-note-run2"; (run2 / "logs").mkdir(parents=True)
(run2 / "logs" / "loss_trace.jsonl").write_text('{"step": 3395, "loss": 1.0, "lr": 1e-6}\n')
(run2 / "run_manifest.json").write_text(json.dumps({"stages": [{"name": "sft", "status": "running"}]}))
m.do_record(run2, "ADR-036", "артефакт «SFT без маски think»", "prefix_only", {})
assert "без маски think" in json.loads((run2 / "run_manifest.json").read_text())["stopped_note"]
PY

# 21.8a Состояние «база» — опора «откуда пришёл язык»: у него нет чекпойнта, и отчёт
#       обязан говорить это явно, а не оставлять пустое место (пустое место читалось
#       бы как «чекпойнт не найден» — другая причина, другой вывод).
expect_exit 0 "probe_language_split: состояние базы описано явно (нет чекпойнта — не ошибка)" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
rec = m.base_record({"weights": "/w/base"}, [{"tag": "p", "metrics": m.language_metrics("Ответ.")}], "cuda")
assert rec["checkpoint"] is None and rec["checkpoint_note"], rec
assert "нетронутая база" in rec["checkpoint_note"]
assert rec["aggregate"]["n"] == 1 and rec["aggregate"]["cyr_answer"]["with_zeros"] == 1.0
help_txt = pathlib.Path("tools/probe_language_split.py").read_text(encoding="utf-8")
assert '"--base"' in help_txt and "опора" in help_txt, "флаг базы не объявлен"
PY

# 21.9 Паспорт прогона проб (AD-2/C-012): каталог прогона без манифеста — нарушение,
#      и оно ловится гейтом; здесь проверяется, что манифест собирается по отчёту
#      прибора и что режим «только манифест» ничего не перемеряет.
expect_exit 0 "probe_language_split: манифест прогона проб проходит C-012 (AD-2)" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

run_dir = tmp / "runs" / "probe-X"
run_dir.mkdir(parents=True)
out = {"schema": "probe-language-split/1", "tool_sha256": "a" * 64,
       "protocol": {"prompts_set": "extended", "prompts_digest": "d" * 64,
                    "max_new_tokens": 1024, "decoding": "greedy"},
       "states": {"sft_resume": {"checkpoint": "/x/sft_probe_5500.pt",
                                 "checkpoint_sha256": "b" * 64, "checkpoint_bytes": 1,
                                 "probes": [{"tag": "p"}]}}}
report = run_dir / "probe_lang.json"
report.write_text(json.dumps(out), encoding="utf-8")
before = report.read_bytes()
man = m.write_run_manifest(report, out, None)
assert pathlib.Path(run_dir / "run_manifest.json").is_file()
for key in ("dataset_sha256", "base_model_id", "pipeline_version", "image",
            "seed", "stages", "pipeline_complete"):
    assert key in man, key
assert man["pipeline_complete"] is True and man["stages"][0]["status"] == "done"
assert man["probed_states"]["sft_resume"]["checkpoint_sha256"] == "b" * 64
assert not man["probe_run"].startswith("/") and ".." not in man["probe_run"]

# гейт C-012 по этому каталогу обязан быть зелёным
r = subprocess.run([sys.executable, "tools/check_run_manifest.py", "--runs", str(tmp / "runs")],
                   capture_output=True, text=True)
assert r.returncode == 0, (r.returncode, r.stdout[-500:])

# режим «только манифест»: собирает паспорт по снятым отчётам и НЕ перемеряет
r = subprocess.run([sys.executable, "tools/probe_language_split.py",
                    "--write-manifest-only", str(run_dir)], capture_output=True, text=True)
assert r.returncode == 0, (r.returncode, r.stderr[-300:])
assert report.read_bytes() == before, "отчёт не должен меняться: перезамер запрещён"
man2 = json.loads((run_dir / "run_manifest.json").read_text())
assert man2["probed_states"]["sft_resume"]["report"] == str(pathlib.Path("runs/probe-X/probe_lang.json")) \
    or man2["probed_states"]["sft_resume"]["report"].endswith("probe_lang.json"), man2["probed_states"]
# пустой каталог → NOT-VERIFIED (2), а не «манифест без состояний»
empty = tmp / "runs" / "empty"; empty.mkdir()
r = subprocess.run([sys.executable, "tools/probe_language_split.py",
                    "--write-manifest-only", str(empty)], capture_output=True, text=True)
assert r.returncode == 2, r.returncode
PY
# 21.8 Свод: вердикт считается кодом, и красные пути у него — свои. Проверяются
#      отказы (тождество не подтвердилось, тесты провалены, данных нет) и то, что
#      «недостаточно данных» — отдельная зона, а не тихая трактовка в чью-то пользу.
expect_exit 0 "assemble_s3ai_evidence: вердикт по критерию, отказы и NOT-VERIFIED" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1]); tmp.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, "tools")
import probe_control as PC

def load(name, path):
    spec = u.spec_from_file_location(name, pathlib.Path(path))
    m = u.module_from_spec(spec); spec.loader.exec_module(m); return m

L = load("lang", "tools/probe_language_split.py")

RU = "<think>Reasoning in English about the question.</think>Ответ по-русски на вопрос."
EN = "<think>Reasoning in English about the question.</think>The answer is in English."
OPEN = "<think>Only reasoning, never closed and never answering the question"
REF = "<think>Английский.</think>И русский ответ, и русское рассуждение."

def st(texts, sha="a" * 64):
    pr = [{"tag": f"p{i}", "system": False, "prompt": "q", "response": t,
           "n_new_tokens": 120, "hit_limit": False, "metrics": L.language_metrics(t)}
          for i, t in enumerate(texts)]
    return {"checkpoint": "/ck.pt", "checkpoint_sha256": sha, "checkpoint_bytes": 1,
            "load": {}, "probes": pr, "aggregate": L.aggregate(pr)}

def write_run(path, states):
    path.write_text(json.dumps({
        "schema": "probe-language-split/1", "tool_sha256": "x",
        "protocol": {"prompts_set": "extended", "prompts_digest": "d", "n_prompts": 24,
                     "max_new_tokens": 1024, "decoding": "greedy"},
        "states": states}, ensure_ascii=False))
    return path

# ── тождество: 5 ответов ядра, метрики считает СВОЙ прибор S3ab
core = [RU, EN, OPEN, "<think>English.</think>Short answer.", REF]
ident_run = write_run(tmp / "ident.json", {"cfinal": st(core)})
fields = ("cyrillic_share", "has_think", "has_tool_call", "words", "chars",
          "uniq4", "max_repeat_of_char_block")
ab = {"states": {"cfinal": {"probes": [
    {"tag": f"p{i}", **{k: PC.degenerate_metrics(t)[k] for k in fields}}
    for i, t in enumerate(core)]}}}
ab_path = tmp / "s3ab.json"; ab_path.write_text(json.dumps(ab, ensure_ascii=False))
ok_log = tmp / "ok.log"
ok_log.write_text("  ok   probe_language_split: x (exit 0)\nитого: PASS=10 FAIL=0\n")
bad_log = tmp / "bad.log"
bad_log.write_text("  ok   probe_language_split: x (exit 0)\nитого: PASS=10 FAIL=1\n"
                   "  - чужая проверка: сломалась\n")
red_log = tmp / "red.log"
red_log.write_text("  ok   probe_language_split: x (exit 0)\nитого: PASS=10 FAIL=1\n"
                   "  - пул v2 в кейсе: разъехался\n")
TOOL = [sys.executable, "tools/assemble_s3ai_evidence.py"]
COMMON = ["--identity-run", str(ident_run), "--s3ab-evidence", str(ab_path)]

def asm(run, log, *extra, out="out.json"):
    return subprocess.run(TOOL + ["--probe-run", str(run), "--tests-log", str(log),
                                  "--final-state", "sft_resume",
                                  "--out", str(tmp / out)] + COMMON + list(extra),
                          capture_output=True, text=True)

# (1) подтверждение: ответ русский, рассуждение английское, покрытие полное
run_ok = write_run(tmp / "run_ok.json",
                   {"cfinal": st([EN, EN, OPEN, EN]), "sft3000": st([EN, RU]),
                    "sft_resume": st([RU] * 20 + [EN] * 4)})
r = asm(run_ok, ok_log, out="ok.json")
assert r.returncode == 0, (r.returncode, r.stderr[-800:])
ev = json.loads((tmp / "ok.json").read_text())
assert ev["verdict"]["zone"] == "confirmed", ev["verdict"]
assert ev["status"] == "complete", ev["status"]
assert ev["instrument"]["identity_with_s3ab"]["identical"] is True
assert ev["instrument"]["identity_with_s3ab"]["values_total"] == 35
assert [p["state"] for p in ev["points"]] == ["cfinal", "sft3000", "sft_resume"]
assert ev["points"][2]["n"] == 24 and ev["points"][2]["answer_coverage"] == 1.0
assert ev["verdict"]["cyr_answer_final"] == 0.8333, ev["verdict"]  # 20 русских из 24
assert ev["verdict"]["cyr_think_final"] == 0.0, ev["verdict"]
# общий скор помечен справочным — решения по нему не принимаются (ADR-039 п.2)
assert ev["points"][2]["cyr_overall_reference_only"] is not None

# (2) страж покрытия: до ответа не доходит никто — «недостаточно данных», не «ноль»
run_cov = write_run(tmp / "run_cov.json",
                    {"sft_resume": st([OPEN] * 24), "cfinal": st([EN])})
r = asm(run_cov, ok_log, out="cov.json")
assert r.returncode == 0, r.stderr[-500:]
cov = json.loads((tmp / "cov.json").read_text())
assert cov["verdict"]["zone"] == "insufficient_coverage", cov["verdict"]
assert cov["status"] == "partial" and cov["open_questions"], cov["status"]

# (3) опровержение и промежуточная зона — по числам, без трактовки
run_no = write_run(tmp / "run_no.json", {"sft_resume": st([EN] * 24)})
r = asm(run_no, ok_log, out="no.json"); assert r.returncode == 0, r.stderr[-500:]
assert json.loads((tmp / "no.json").read_text())["verdict"]["zone"] == "refuted"
run_mid = write_run(tmp / "run_mid.json",
                    {"sft_resume": st(["<think>English.</think>Ответ english."] * 24)})
r = asm(run_mid, ok_log, out="mid.json"); assert r.returncode == 0, r.stderr[-500:]
mid = json.loads((tmp / "mid.json").read_text())
assert 0.4 <= mid["verdict"]["cyr_answer_final"] < 0.5, mid["verdict"]
assert mid["verdict"]["zone"] == "intermediate", mid["verdict"]
# промежуточная зона — валидный исход («недостаточно данных»), а не поломка прибора:
# статус остаётся complete, но вопрос архитектору обязан быть назван
assert mid["status"] == "complete" and any("зона" in q for q in mid["open_questions"])

# (4) вне критерия: и ответ, и рассуждение русские — трактовка уходит архитектору
run_oos = write_run(tmp / "run_oos.json", {"sft_resume": st([REF] * 24)})
r = asm(run_oos, ok_log, out="oos.json"); assert r.returncode == 0, r.stderr[-500:]
oos = json.loads((tmp / "oos.json").read_text())
assert oos["verdict"]["zone"] == "out_of_scope_think_not_english", oos["verdict"]
assert any("архитектор" in q for q in oos["open_questions"])

# (5) ОТКАЗ: тождество не подтвердилось (одно поле метрики разошлось)
bad_ab = json.loads(ab_path.read_text())
bad_ab["states"]["cfinal"]["probes"][0]["words"] += 1
bad_ab_path = tmp / "s3ab_bad.json"; bad_ab_path.write_text(json.dumps(bad_ab, ensure_ascii=False))
r = subprocess.run(TOOL + ["--probe-run", str(run_ok), "--tests-log", str(ok_log),
                           "--final-state", "sft_resume", "--out", str(tmp / "b.json"),
                           "--identity-run", str(ident_run), "--s3ab-evidence", str(bad_ab_path)],
                   capture_output=True, text=True)
assert r.returncode == 1 and "ОТКАЗ" in r.stderr, (r.returncode, r.stderr[-400:])
assert json.loads((tmp / "b.json").read_text())["status"] == "partial"

# (6) ОТКАЗ: красное вне списка известных; то же красное в списке — не отказ
r = asm(run_ok, bad_log, out="b2.json")
assert r.returncode == 1 and "ОТКАЗ" in r.stderr, r.stderr[-300:]
r = asm(run_ok, red_log, "--known-red", "пул v2 в кейсе", out="b3.json")
assert r.returncode == 0, r.stderr[-300:]
ev = json.loads((tmp / "b3.json").read_text())
assert ev["instrument"]["tests"]["failures_outside_known_red"] == []
assert ev["instrument"]["tests"]["passed"] is True

# (7) NOT-VERIFIED: нет прогона проб / нет запрошенного состояния
r = subprocess.run(TOOL + ["--probe-run", str(tmp / "nope.json"), "--out", str(tmp / "n.json")],
                   capture_output=True, text=True)
assert r.returncode == 2, r.returncode
r = subprocess.run(TOOL + ["--probe-run", str(run_ok), "--final-state", "нет_такого",
                           "--out", str(tmp / "n2.json")], capture_output=True, text=True)
assert r.returncode == 2, r.returncode
PY

# 21.7a Красные пути возобновления: имя без шага, пайплайн не по манифесту,
#       и вынужденная копия (protected_hardlinks) — обязан быть сверен хеш.
expect_exit 0 "launch_sft_stage --resume: имя без шага, чужой пайплайн и копия-подмена — отказы" \
  python3 - "$TMP" <<'PY2'
import hashlib, importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("launch", pathlib.Path("tools/launch_sft_stage.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
shared = tmp / "shared7a"
src = shared / "src-run"
(src / "checkpoints").mkdir(parents=True, exist_ok=True)
cpt = shared / "cpt.pt"; cpt.write_bytes(b"CPT")
m.SHARED = shared
m.CPT_CKPT = cpt
m.CPT_CKPT_SHA256 = hashlib.sha256(b"CPT").hexdigest()
# Данные стадии пиннятся хешем с диска (S3an) — фикстура обязана их нести.
(ds_dir := shared / "datasets").mkdir(parents=True)
(dj := ds_dir / "sft_train_v12.jsonl").write_bytes(b'{"messages": []}\n')
m.DATASETS = {**m.DATASETS, "v12": {**m.DATASETS["v12"],
                                    "jsonl_sha256": hashlib.sha256(dj.read_bytes()).hexdigest()}}

def expect_systemexit(fn, what):
    try:
        fn()
    except SystemExit:
        return
    raise AssertionError(f"ожидался отказ: {what}")

bad = src / "checkpoints" / "sft_probe_final.pt"; bad.write_bytes(b"x")
expect_systemexit(lambda: m.resume_input(bad), "имя без читаемого шага")

rck = src / "checkpoints" / "sft_probe_3000.pt"; rck.write_bytes(b"SFT3000")
# манифест говорит про другой пайплайн — «тот же код» должно быть равенством хешей
(src / "run_manifest.json").write_text(json.dumps({"pipeline_sha256": "0" * 64}))
expect_systemexit(lambda: m.resume_input(rck), "пайплайн разошёлся с манифестом")

(src / "laguna_pipeline_sft.py").write_bytes(b"# pipeline\n")
(src / "run_manifest.json").write_text(json.dumps(
    {"pipeline_sha256": hashlib.sha256(b"# pipeline\n").hexdigest()}))
resume = m.resume_input(rck)
resume["planned_stop_step"] = None

# hardlink запрещён (fs.protected_hardlinks: чекпойнт пишет контейнер от root) —
# путь обязан деградировать в **проверенную** копию, а не в непроверенную
real_link = m.os.link
m.os.link = lambda *a, **k: (_ for _ in ()).throw(OSError(1, "Operation not permitted"))
try:
    d = m.build("20990101-0100", "prefix_only", resume)
finally:
    m.os.link = real_link
placed = d["resume"]
assert placed["kind"].startswith("copy"), placed["kind"]
assert placed["sha_verified"] is True and placed["same_inode"] is False
assert d["link"]["sha_verified"] is True, "вход CPT копируется так же проверенно"
PY2

echo
echo "== 21a. S3aj: снятие артефакта усечения (конец хода, длины, вырождение) =="

# 21a.1 Где кончается ход и почему оборвалась генерация. Разбор обязан называть
#       ВНУТРЕННИЙ блок (вызов инструмента внутри рассуждения — штатная форма v12):
#       иначе «обрыв в рассуждении» скрыл бы, что обрыв пришёлся на JSON вызова.
expect_exit 0 "probe_language_split: регион обрыва (вложенность) и причина конца" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

assert m.tail_region("просто ответ") == "answer"
assert m.tail_region("a<think>b") == "think"
assert m.tail_region("a<think>b</think>c") == "answer"
assert m.tail_region("a<think>b<tool_call>{\"n\": 1}") == "tool_call", \
    "вложенный блок обязан называться внутренним"
assert m.tail_region("a<think>b<tool_call>{}</tool_call>d") == "think", \
    "закрытый внутренний блок возвращает состояние к объемлющему"
assert m.tail_region("a<tool_response>d") == "tool_response"

assert m.stop_reason("<think>x</think>Ответ.", False) == "turn_end"
assert m.stop_reason("<think>не кончилось", True) == "limit_in_think"
assert m.stop_reason("<think>x</think>начал вызов<tool_call>{\"a\"", True) == "limit_in_tool_call"
assert m.stop_reason("Ответ без блоков", True) == "limit_in_answer"
# упор ровно на последнем токене читается как лимит, а не как дописанный ход
assert m.stop_reason("Ответ.", True) == "limit_in_answer"

# конец хода: обрезается по первому <|im_end|>, без него текст не трогается
assert m.first_turn("Ответ.<|im_end|>мусор дальше") == "Ответ."
assert m.first_turn("Ответ без конца хода") == "Ответ без конца хода"
assert m.first_turn("") == ""
PY

# 21a.2 Признак вырождения: порог объявлен константой, но числа обязаны лежать
#       рядом — «зациклилось» без чисел это суждение, а не замер.
expect_exit 0 "probe_language_split: вырождение (порог и метрики S3ab рядом)" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
assert m.LOOP_MAX4GRAM_REP == 8
loop = "размер группы G " * 40
d = m.degenerate(loop)
assert d["looped"] is True and d["max4gram_rep"] >= m.LOOP_MAX4GRAM_REP, d
assert d["uniq4"] is not None
ok = m.degenerate("Хлеб пекут из муки, воды и соли. Тесто ставят подходить на ночь.")
assert ok["looped"] is False, ok
# метрики вырождения — импортом из прибора S3ab, а не своей арифметикой
import probe_control as PC
import sys; sys.path.insert(0, "tools")
assert m.degenerate_metrics is PC.degenerate_metrics
PY

# 21a.3 Свод: длины (медиана/p90 ближайшим рангом), разбор причин конца, и —
#       главное — формат считается ОТДЕЛЬНО по дописанным ходам: без этого
#       «модель не закрыла think» неотличимо от «её оборвал лимит».
expect_exit 0 "probe_language_split: длины, причины конца и формат по дописанным ходам" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

def rec(tok, text, hit, reason):
    return {"tag": "t", "n_new_tokens": tok, "hit_limit": hit, "stop_reason": reason,
            "metrics": m.language_metrics(text)}

recs = [
    # дописанный ход с закрытым think, ответ русский
    rec(1200, "<think>Reasoning.</think>Ответ по-русски.", False, "turn_end"),
    # оборван лимитом внутри рассуждения (think не закрыт)
    rec(4096, "<think>длинное рассуждение без конца", True, "limit_in_think"),
    rec(900, "<think>Reasoning.</think>Русский ответ.", False, "turn_end"),
    rec(4096, "<think>x</think>Начал вызов<tool_call>{\"name\": \"s\"", True,
        "limit_in_tool_call"),
]
agg = m.aggregate(recs, 4096)
assert agg["lengths"]["percentile_method"] == "nearest-rank"
assert agg["lengths"]["median"] == 1200 and agg["lengths"]["p90"] == 4096, agg["lengths"]
assert agg["lengths"]["budget"] == 4096 and agg["lengths"]["max"] == 4096
s = agg["stop"]
assert s["natural_stop_share"] == 0.5 and s["truncated_share"] == 0.5
assert s["where_truncated"] == {"limit_in_think": 1, "limit_in_tool_call": 1}, s
assert s["natural"]["unclosed_think_share"] == 0.0, "у дописанных think закрыт"
assert s["truncated"]["unclosed_think_share"] == 0.5, s["truncated"]
assert agg["unclosed_think_share"] == 0.25, agg["unclosed_think_share"]
# ближайший ранг, а не интерполяция: медиана 24 значений — 12-е, p90 — 22-е
vals = [rec(i, "Ответ.", False, "turn_end") for i in range(1, 25)]
assert m._pct([r["n_new_tokens"] for r in vals], 0.5) == 12
assert m._pct([r["n_new_tokens"] for r in vals], 0.9) == 22
assert m._pct([], 0.5) is None
PY

# 21a.4 Перечитывание снятого отчёта правилом конца хода: greedy префиксно
#       детерминирован, поэтому это НЕ новый замер и не «второй шанс», а способ
#       развести две причины обрыва на одном бюджете. Длины при этом не
#       выдумываются: токенов префикса в отчёте нет.
expect_exit 0 "probe_language_split: --recut-report — разбор причин на том же бюджете" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

src = tmp / "recut_src.json"
probes = [
    # дописанный ход есть внутри окна — исправленный прибор встал бы здесь
    {"tag": "a", "system": False, "prompt": "q", "n_new_tokens": 1024, "hit_limit": True,
     "stop_reason": "limit_in_think",
     "response": "<think>Reasoning in English.</think>Ответ по-русски.<|im_end|>"
                 "<|im_start|>user и дальше мусор",
     "metrics": m.language_metrics("<think>Reasoning in English.</think>Ответ по-русски."
                                   "<|im_end|><|im_start|>user и дальше мусор")},
    # конца хода в окне нет — перечитывать нечего
    {"tag": "b", "system": False, "prompt": "q", "n_new_tokens": 1024, "hit_limit": True,
     "stop_reason": "limit_in_think", "response": "<think>Не кончилось",
     "metrics": m.language_metrics("<think>Не кончилось")},
]
src.write_text(json.dumps({"schema": "probe-language-split/1", "tool_sha256": "x",
                           "protocol": {"prompts_set": "extended", "max_new_tokens": 1024},
                           "states": {"sft3000": {"checkpoint": "/ck.pt", "probes": probes,
                                                  "aggregate": m.aggregate(probes, 1024)}}},
                          ensure_ascii=False))
dst = tmp / "recut_dst.json"
r = subprocess.run([sys.executable, "tools/probe_language_split.py",
                    "--recut-report", str(src), "--out", str(dst)],
                   capture_output=True, text=True)
assert r.returncode == 0, r.stderr[-400:]
out = json.loads(dst.read_text())
st = out["states"]["sft3000"]
a, b = st["probes"]
assert a["response"].endswith("Ответ по-русски.") and a["cut_at_turn_end"] is True
assert a["hit_limit"] is False, "конец хода внутри окна снимает упор в лимит"
assert a["stop_reason"] == "turn_end" and a["metrics"]["cyr_answer"] == 1.0
assert a["n_new_tokens"] == 1024 and a["n_new_tokens_original"] == 1024
assert b["cut_at_turn_end"] is False and b["hit_limit"] is True
assert b["stop_reason"] == "limit_in_think"
assert out["protocol"]["recut"]["from"].endswith("recut_src.json")
assert "длины в токенах — исходного окна" in out["protocol"]["recut"]["lengths_note"]
# исходный отчёт не тронут (перезапись базовых замеров запрещена)
assert json.loads(src.read_text())["states"]["sft3000"]["probes"][0]["response"] \
    .startswith("<think>Reasoning in English.</think>Ответ")
# --recut-report без --out — отказ (1), нет файла — NOT-VERIFIED (2)
r = subprocess.run([sys.executable, "tools/probe_language_split.py",
                    "--recut-report", str(src)], capture_output=True, text=True)
assert r.returncode == 1, r.returncode
r = subprocess.run([sys.executable, "tools/probe_language_split.py",
                    "--recut-report", str(tmp / "nope.json"), "--out", str(tmp / "x.json")],
                   capture_output=True, text=True)
assert r.returncode == 2, r.returncode
PY

# 21a.5 Остановка на конце хода объявлена и выключена по умолчанию: включённая
#       молча, она сломала бы сопоставимость с S3ab, а незаписанная в протокол —
#       сделала бы неизвестным, чем снят отчёт.
expect_exit 0 "probe_language_split: --stop-at-turn-end объявлен и по умолчанию выключен" \
  python3 - <<'PY'
import importlib.util as u, pathlib, sys
sys.path.insert(0, "tools")
import ppl_probe  # noqa: F401  (не нужен, но проверяем, что прибор не тянет torch на импорте)
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
txt = pathlib.Path("tools/probe_language_split.py").read_text(encoding="utf-8")
assert '"--stop-at-turn-end"' in txt and 'action="store_true"' in txt
assert "stop_at_turn_end" in txt and 'STOP_TURN_END' in txt
assert m.FULL_MAX_NEW_TOKENS == 4096 and m.FULL_MAX_NEW_TOKENS > m.LANG_MAX_NEW_TOKENS

class T:
    unk_token_id = 0
    eos_token_id = 151643
    def convert_tokens_to_ids(self, tok):
        return {"<|im_end|>": 151645}.get(tok, 0)
assert m.turn_end_ids(T()) == [151643, 151645], m.turn_end_ids(T())
PY

# 21a.6 Свод S3aj: вердикт «артефакт / деградация» по полному замеру, отказы и
#       NOT-VERIFIED. Красные пути обязательны: свод, который только подтверждает,
#       не свод, а украшение.
expect_exit 0 "assemble_s3aj_evidence: вердикт по формату, отказы и NOT-VERIFIED" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1]); tmp.mkdir(parents=True, exist_ok=True)

def load(name, path):
    spec = u.spec_from_file_location(name, pathlib.Path(path))
    m = u.module_from_spec(spec); spec.loader.exec_module(m); return m

L = load("lang", "tools/probe_language_split.py")
A = load("asmj", "tools/assemble_s3aj_evidence.py")

CLOSED = "<think>Reasoning in English.</think>Ответ по-русски на вопрос."
OPEN = "<think>Рассуждение, которое не закрывается"
LOOP = "<think>" + "размер группы G " * 60

def probe(tok, text, hit, reason):
    return {"tag": "p", "system": False, "prompt": "q", "response": text,
            "n_new_tokens": tok, "hit_limit": hit, "stop_reason": reason,
            "metrics": L.language_metrics(text)}

def state(probes):
    return {"checkpoint": "/ck.pt", "checkpoint_sha256": "a" * 64, "checkpoint_bytes": 1,
            "load": {}, "probes": probes, "aggregate": L.aggregate(probes, 4096)}

def write_run(path, states, *, mnt=4096, stop=True):
    path.write_text(json.dumps({
        "schema": "probe-language-split/1", "tool_sha256": "x",
        "protocol": {"prompts_set": "extended", "prompts_digest": "d", "n_prompts": 24,
                     "max_new_tokens": mnt, "stop_at_turn_end": stop,
                     "decoding": "greedy"},
        "states": states}, ensure_ascii=False))
    return path

# (1) АРТЕФАКТ: в полном замере SFT-состояние дописывает ходы, think закрыт
full_ok = write_run(tmp / "full_ok.json", {
    "cfinal": state([probe(1500, CLOSED, False, "turn_end")] * 24),
    "sft3000": state([probe(1600, CLOSED, False, "turn_end")] * 24),
    "sft_resume": state([probe(1700, CLOSED, False, "turn_end")] * 24)})
trunc = write_run(tmp / "trunc.json", {
    "cfinal": state([probe(1024, OPEN, True, "limit_in_think")] * 24),
    "sft3000": state([probe(1024, OPEN, True, "limit_in_think")] * 24),
    "sft_resume": state([probe(1024, OPEN, True, "limit_in_think")] * 24)}, mnt=1024, stop=False)
TOOL = [sys.executable, "tools/assemble_s3aj_evidence.py"]
def asm(full, *extra, out="s3aj.json"):
    return subprocess.run(TOOL + ["--full-run", str(full), "--out", str(tmp / out)]
                          + list(extra), capture_output=True, text=True)

r = asm(full_ok, "--truncated-run", str(trunc))
assert r.returncode == 0, (r.returncode, r.stderr[-800:])
ev = json.loads((tmp / "s3aj.json").read_text())
assert ev["verdict_format"]["artifact_or_degradation"] == "artifact", ev["verdict_format"]
assert ev["verdict_format"]["third_cause"] is None
assert ev["verdict_language_by_adr039"]["zone"] == "confirmed", ev["verdict_language_by_adr039"]
assert [l["state"] for l in ev["lengths"]] == ["cfinal", "sft3000", "sft_resume"]
assert ev["lengths"][2]["median_tokens"] == 1700 and ev["lengths"][2]["budget"] == 4096
assert ev["lengths"][2]["where_truncated"] == {}
assert ev["lengths"][2]["censored_share"] == 0.0 and ev["lengths"][2]["p90_censored"] is False
# длины дописанных ходов считаются отдельно: у состояния, половина генераций
# которого упёрлась в бюджет, общая медиана равна бюджету и о модели не говорит
assert ev["lengths"][2]["median_tokens_natural"] == 1700
assert ev["lengths"][2]["p90_tokens_natural"] == 1700
assert ev["truncated_measure"]["sft_resume"]["unclosed_think_share"] == 1.0
assert ev["status"] == "measured"

# (2) ДЕГРАДАЦИЯ: незакрытые think остались и в полном замере
full_bad = write_run(tmp / "full_bad.json", {
    "cfinal": state([probe(4096, LOOP, True, "limit_in_think")] * 24),
    "sft_resume": state([probe(4096, LOOP, True, "limit_in_think")] * 24)})
r = asm(full_bad, out="bad.json")
assert r.returncode == 0, r.stderr[-400:]
bad = json.loads((tmp / "bad.json").read_text())
assert bad["verdict_format"]["artifact_or_degradation"] == "degradation", bad["verdict_format"]
assert bad["lengths"][-1]["where_truncated"] == {"limit_in_think": 24}
assert any("зациклен" in q for q in bad["open_questions"]), bad["open_questions"]

# (3) ТРЕТЬЯ ПРИЧИНА: у дописанных ходов формат цел, весь остаток — зацикленные
#     обрывы. Это НЕ «формат потерян», и вердикт обязан сказать это словами.
half = [probe(1500, CLOSED, False, "turn_end")] * 12 + [probe(4096, LOOP, True, "limit_in_think")] * 12
full_third = write_run(tmp / "full_third.json", {"sft_resume": state(half)})
r = asm(full_third, out="third.json")
assert r.returncode == 0, r.stderr[-400:]
third = json.loads((tmp / "third.json").read_text())
assert third["verdict_format"]["artifact_or_degradation"] == "degradation"
assert third["verdict_format"]["third_cause"], third["verdict_format"]
assert any("вырождением" in q for q in third["open_questions"])

# (4) СМЕШАННО: между порогами — решение не выносится, вопрос назван
mid = [probe(1500, CLOSED, False, "turn_end")] * 18 + [probe(4096, LOOP, True, "limit_in_think")] * 6
full_mid = write_run(tmp / "full_mid.json", {"sft_resume": state(mid)})
r = asm(full_mid, out="mid.json")
assert r.returncode == 0, r.stderr[-400:]
assert json.loads((tmp / "mid.json").read_text())["verdict_format"]["artifact_or_degradation"] == "mixed"

# (5) Страж покрытия ADR-039 работает и на полном замере: обрывов нет, но ответа нет
nocov = write_run(tmp / "full_nocov.json", {
    "sft_resume": state([probe(4096, LOOP, True, "limit_in_think")] * 24)})
r = asm(nocov, out="nocov.json")
assert r.returncode == 0, r.stderr[-400:]
nc = json.loads((tmp / "nocov.json").read_text())
assert nc["verdict_language_by_adr039"]["zone"] == "insufficient_coverage", nc["verdict_language_by_adr039"]
# вторая читка того же числа — без стража покрытия — названа рядом, а не спрятана
assert nc["verdict_language_by_adr039"]["thresholds_only_zone"] == "refuted", \
    nc["verdict_language_by_adr039"]

# (6) ОТКАЗ: тождество прибора не подтвердилось (байты ответов ядра разошлись)
ident_now = write_run(tmp / "ident_now.json", {"cfinal": state(
    [probe(384, CLOSED, True, "limit_in_answer")] * 5)})
ident_ref = write_run(tmp / "ident_ref.json", {"cfinal": state(
    [probe(384, CLOSED.replace("русски", "русск"), True, "limit_in_answer")] * 5)})
r = asm(full_ok, "--identity-run", str(ident_now), "--identity-reference", str(ident_ref),
        out="refuse.json")
assert r.returncode == 1 and "ОТКАЗ" in r.stderr, (r.returncode, r.stderr[-400:])
assert json.loads((tmp / "refuse.json").read_text())["status"] == "refused"
# то же самое, но ответы совпадают — не отказ
r = asm(full_ok, "--identity-run", str(ident_now), "--identity-reference", str(ident_now),
        out="ident_ok.json")
assert r.returncode == 0, r.stderr[-400:]
assert json.loads((tmp / "ident_ok.json").read_text())["identity"]["identical"] is True
# тождество не передано — это НЕ «совпало», и это сказано словами; то же и когда
# прогон есть, а эталона для сравнения нет
r = asm(full_ok, out="ident_none.json")
assert r.returncode == 0, r.stderr[-400:]
ident_none = json.loads((tmp / "ident_none.json").read_text())["identity"]
assert ident_none["available"] is False and "НЕ «совпало»" in ident_none["why"]
r = asm(full_ok, "--identity-run", str(ident_now), out="ident_noref.json")
assert r.returncode == 0, r.stderr[-400:]
ident_noref = json.loads((tmp / "ident_noref.json").read_text())["identity"]
assert ident_noref["available"] is True and ident_noref["identical"] is None
assert "НЕ «совпало»" in ident_noref["why"]

# (7) ОТКАЗ на красном в журнале тестов и NOT-VERIFIED без полного замера
(tmp / "red.log").write_text("  ok   probe_language_split: x (exit 0)\nитого: PASS=9 FAIL=1\n"
                             "  - assemble_s3aj_evidence: сломалось\n")
r = asm(full_ok, "--tests-log", str(tmp / "red.log"), out="tests_red.json")
assert r.returncode == 1 and "тесты не PASS" in r.stderr, (r.returncode, r.stderr[-200:])
# журнала нет — это «не подтверждено», а не «провалено»: отказ не выносится,
# но вопрос архитектору обязан быть назван
r = asm(full_ok, "--tests-log", str(tmp / "no_such.log"), out="tests_missing.json")
assert r.returncode == 0, r.stderr[-200:]
assert any("Журнал тестов не передан" in q
           for q in json.loads((tmp / "tests_missing.json").read_text())["open_questions"])
(tmp / "ok.log").write_text("  ok   probe_language_split: x (exit 0)\nитого: PASS=9 FAIL=0\n")
r = asm(full_ok, "--tests-log", str(tmp / "ok.log"), out="tests_ok.json")
assert r.returncode == 0 and json.loads((tmp / "tests_ok.json").read_text())["tests"]["passed"]
r = asm(tmp / "nope.json", out="nv.json")
assert r.returncode == 2, r.returncode

# (8) Слияние состояний из нескольких отчётов и --alias: имя состояния содержит
#     шаг чекпойнта, а сопоставлять «на глаз» свод не имеет права. Совпадение
#     имён после переименования — отказ, а не «взяли последнее».
trunc2 = write_run(tmp / "trunc2.json", {
    "cfinal": state([probe(1024, OPEN, True, "limit_in_think")] * 24),
    "sft3000": state([probe(1024, OPEN, True, "limit_in_think")] * 24)}, mnt=1024, stop=False)
other = write_run(tmp / "other.json", {"sftresume_4500": state(
    [probe(1024, OPEN, True, "limit_in_think")] * 24)}, mnt=1024, stop=False)
r = asm(full_ok, "--truncated-run", str(trunc2), "--truncated-run", str(other),
        "--alias", "sftresume_4500=sft_resume", out="alias.json")
assert r.returncode == 0, r.stderr[-400:]
alias = json.loads((tmp / "alias.json").read_text())
assert set(alias["truncated_measure"]) == {"cfinal", "sft3000", "sft_resume"}, \
    alias["truncated_measure"].keys()
assert alias["truncated_measure"]["sft_resume"]["source"].endswith("other.json")
# два отчёта про одно состояние — отказ, а не молчаливый выбор
other2 = write_run(tmp / "other2.json", {"sft3000": state(
    [probe(1024, OPEN, True, "limit_in_think")] * 24)}, mnt=1024, stop=False)
r = asm(full_ok, "--truncated-run", str(trunc2), "--truncated-run", str(other2), out="dup.json")
assert r.returncode == 1 and "неоднозначно" in r.stderr, (r.returncode, r.stderr[-200:])
PY

echo
echo "== 21b. S3ak: разбор вырождения, правка прибора v2 и вердикт по языку на ≥100 =="

# 21b.1 Фикстуры v2: служебные токены — не буквы, язык — по ходу модели. Проверка
#       идёт парой (v2 и v1): ценность правки прибора именно в расхождении, и если
#       v2 совпадёт с v1 на тексте со служебным токеном — правило не работает.
expect_exit 0 "probe_language_split: селфтест v2 (служебные токены, ход модели)" \
  python3 tools/probe_language_split.py --selftest

expect_exit 0 "probe_language_split: правило 4/5 и сохранность прежней арифметики" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

assert m.INSTRUMENT_VERSION == 2
assert m.TURN_MARKERS == ("<|im_end|>", "<|im_start|>", "<|endoftext|>")

# правило 4: латинские буквы служебного токена не попадают в долю ответа
t = "<think>Reasoning.</think>Ответ по-русски.<|im_end|>"
mm = m.language_metrics(t)
assert mm["cyr_answer"] == 1.0 and mm["legacy"]["cyr_answer"] == round(13 / 18, 4), mm["cyr_answer"]
assert "<|im_end|>" in mm["special_tokens_stripped"]
# правило 5: продолжение после конца хода не речь модели
t2 = "Ответ.<|im_end|><|im_start|>user latin words after the turn"
m2 = m.language_metrics(t2)
assert m2["cyr_answer"] == 1.0 and m2["own_turn_chars"] < m2["chars"], m2
# служебный токен вне названной тройки — тоже не речь: правило не зависит от ревизии
m3 = m.language_metrics("<|object_ref_start|>Ответ по-русски.")
assert m3["cyr_answer"] == 1.0 and m3["legacy"]["cyr_answer"] < 1.0
assert m3["special_tokens_stripped"] == ["<|object_ref_start|>"]
# прежние (v1) числа восстанавливаются из того же текста — иначе дельту не с чем сравнивать
assert m.legacy_metrics(t)["cyr_answer"] == m.language_metrics(t)["legacy"]["cyr_answer"]
# старые фикстуры S3ai/S3aj не сдвинулись (тексты без служебных токенов)
assert m.language_metrics("<think>Reasoning in English.</think>Ответ по-русски.")["cyr_answer"] == 1.0
PY

# 21b.2 Чтение языка по «чистым» ходам: зацикленные и оборванные исключены и
#       посчитаны отдельно. Без этого запрет TASK («не судить о языке по петлям»)
#       выполнялся бы молча, а «чистое» чтение можно было бы выдать за все.
expect_exit 0 "probe_language_split: чистое чтение (без петель и обрывов) отдельно" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

def rec(text, reason, hit):
    return {"tag": "t", "n_new_tokens": len(text), "hit_limit": hit,
            "stop_reason": reason, "metrics": m.language_metrics(text)}

loop = "<think>x</think>" + "размер группы G " * 40
recs = [rec("<think>Reasoning.</think>Ответ по-русски.", "turn_end", False),
        rec("<think>не кончилось", "limit_in_think", True),
        rec(loop, "limit_in_think", True)]
agg = m.aggregate(recs)
cl = agg["clean"]
assert cl["n"] == 1 and cl["excluded_looped"] == 1 and cl["excluded_by_limit"] == 1, cl
assert cl["cyr_answer"]["with_zeros"] == 1.0
assert cl["margin_to_refute"] == round(1.0 - m.CRIT_ANSWER_REFUTE_MAX, 4)
assert agg["looped_share"] == round(1 / 3, 4)
# бутстрап-интервал: он и есть ответ на «вердикт не должен держаться на 0.008»
ci = m.bootstrap_ci([1.0] * 10)
assert ci["lo"] == 1.0 and ci["hi"] == 1.0 and ci["n"] == 10 and ci["n_boot"] == m.BOOTSTRAP_N
assert m.bootstrap_ci([])["lo"] is None
assert cl["ci95"]["n"] == 1
PY

# 21b.3 Режим декодирования — имя из чисел, а не из флага: два прогона с разной
#       температурой обязаны различаться и в таблице «режим × доля петель».
expect_exit 0 "probe_language_split: режим декодирования собирается из параметров" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
assert m.decoding_label("greedy", 0.7, 0.9, None, None) == "greedy"
assert m.decoding_label("greedy", 0.7, 0.9, 1.0, None) == "greedy", "rp=1.0 — не режим"
assert m.decoding_label("greedy", 0.7, 0.9, 1.1, None) == "greedy_rp1.1"
assert m.decoding_label("sample", 0.7, 0.9, None, None) == "sample_T0.7_top_p0.9"
assert m.decoding_label("sample", 0.5, 0.9, 1.1, 4) == "sample_T0.5_top_p0.9_rp1.1_nogram4"
# greedy не тянет за собой ни одного параметра sampling
kw = m._gen_kwargs("greedy", 0.7, 0.9, None, None, [151645], 0)
assert kw["do_sample"] is False and "temperature" not in kw and "top_p" not in kw, kw
assert kw["eos_token_id"] == [151645] and kw["pad_token_id"] == 0
kw2 = m._gen_kwargs("sample", 0.7, 0.9, 1.1, 4, None, 0)
assert kw2["do_sample"] is True and kw2["temperature"] == 0.7 and kw2["top_p"] == 0.9
assert kw2["repetition_penalty"] == 1.1 and kw2["no_repeat_ngram_size"] == 4
assert "eos_token_id" not in kw2, "без остановки на конце хода eos не передаётся"
PY

# 21b.4 Широкий набор: ≥100 проб — требование TASK к выборке вердикта. Набор
#       расширяется промптами, а не повторами одного: у greedy повторы дали бы те
#       же байты, и «100 генераций» оказались бы одной.
expect_exit 0 "probe_language_split: широкий набор ≥100 и ядро не сдвинулось" \
  python3 - <<'PY'
import importlib.util as u, pathlib, sys, collections
sys.path.insert(0, "tools")
import probe_control as PC
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
assert len(m.PROMPT_SETS["wide"]) >= 100, len(m.PROMPT_SETS["wide"])
assert len(m.PROMPT_SETS["extended"]) == 24, "прежний набор остаётся 24"
assert m.PROMPT_SETS["wide"][:24] == m.PROMPT_SETS["extended"], \
    "широкий набор — надмножество прежнего, а не другой набор"
assert len({p["text"] for p in m.PROMPT_SETS["wide"]}) == len(m.PROMPT_SETS["wide"]), \
    "дубли промптов сделали бы выборку фиктивной"
# ядро и его хеш не сдвинулись: сопоставимость с S3ab/S3ai/S3aj
assert m.prompts_digest(m.PROMPT_SETS["core"]) == \
    m.prompts_digest(list(PC.PROBE_PROMPTS))
assert m.PROMPT_SETS["core"] == PC.PROBE_PROMPTS
# доменные — с системным промптом, общие и инструкции — без (условие деплоя)
tags = collections.Counter(p["tag"] for p in m.PROMPT_SETS["wide"])
assert set(tags) == {"domain_tool", "domain_knowledge", "general_language",
                     "general_reasoning", "instruction"}, tags
for p in m.PROMPT_SETS["wide"]:
    assert p["system"] == p["tag"].startswith("domain_"), p
PY

# 21b.5 CLI: режим декодирования ограничен объявленными, а перечитывание прежнего
#       отчёта без --out — отказ (исходный отчёт перезаписывать запрещено).
expect_exit 2 "probe_language_split: неизвестный режим декодирования — отказ" \
  python3 tools/probe_language_split.py --decoding bogus --out "$TMP/lang_dec.json"
expect_exit 1 "probe_language_split: --reaudit-report без --out — отказ" \
  python3 tools/probe_language_split.py --reaudit-report runs/s3ai-probes-20260917/probe_lang_baselines.json
expect_exit 2 "probe_language_split: --reaudit-report на не-отчёте — NOT-VERIFIED" \
  python3 tools/probe_language_split.py --reaudit-report runs/rev-pool/exclusions.json \
    --out "$TMP/reaudit_bad.json"

# 21b.6 Перечитывание прежнего замера: прежняя (v1) арифметика обязана
#       воспроизвестись из текста — иначе дельта правки прибора ничего не значит,
#       а исходный отчёт обязан остаться нетронутым.
expect_exit 0 "probe_language_split: --reaudit-report воспроизводит v1 и даёт дельту" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

text = "Ответ по-русски.<|im_end|><|im_start|>user and latin tail"
#: Прежний отчёт нёс **арифметику v1** в тех же полях: собираем её наложением на
#: полный набор полей (иначе записанные числа не были бы похожи на настоящие).
met = m.language_metrics(text); met.update(m.legacy_metrics(text))
probes = [{"tag": "instruction", "system": False, "prompt": "q", "response": text,
           "n_new_tokens": 100, "hit_limit": False, "stop_reason": "turn_end",
           "metrics": dict(met)}] * 4
src = tmp / "reaudit_src.json"
src.write_text(json.dumps({"schema": "probe-language-split/1", "tool_sha256": "x",
                           "protocol": {"prompts_set": "wide", "max_new_tokens": 4096},
                           "states": {"sft9": {"checkpoint": "/ck.pt", "probes": probes,
                                               "aggregate": m.aggregate(probes, 4096)}}},
                          ensure_ascii=False))
dst = tmp / "reaudit_dst.json"
r = subprocess.run([sys.executable, "tools/probe_language_split.py",
                    "--reaudit-report", str(src), "--out", str(dst), "--label", "проба"],
                   capture_output=True, text=True)
assert r.returncode == 0, r.stderr[-400:]
out = json.loads(dst.read_text())
assert out["schema"] == "probe-language-reaudit/1" and out["label"] == "проба"
st = out["states"]["sft9"]
assert st["legacy_reproduced"] is True and st["legacy_mismatches"] == []
assert st["reading_v1"]["cyr_answer"]["with_zeros"] < st["reading_v2"]["cyr_answer"]["with_zeros"]
assert st["delta"]["cyr_answer_with_zeros"] > 0, st["delta"]
assert st["reading_v2"]["cyr_answer"]["with_zeros"] == 1.0
# исходный отчёт не переписан
assert json.loads(src.read_text())["states"]["sft9"]["probes"][0]["response"] == text
PY

# 21b.7 Свод S3ak: вердикт о причине считается правилами, а не называется словами.
#       Красные пути: нет данных — NOT-VERIFIED; тождество прибора не подтверждено —
#       отказ, потому что числа тогда сняты другим прибором.
expect_exit 0 "assemble_s3ak_evidence: правила вердикта (декодирование / шаг / данные)" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1]); run = tmp / "run21b"; run.mkdir(parents=True, exist_ok=True)
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
spec = u.spec_from_file_location("asmk", pathlib.Path("tools/assemble_s3ak_evidence.py"))
A = u.module_from_spec(spec); spec.loader.exec_module(A)

def probe(text, reason, hit):
    #: Ответ отличается от текста промпта: у одной пробы не может быть двух разных
    #: ответов, а проверка «режим применён» сравнивает именно ответы (тексты),
    #: поэтому ответ несёт метку режима — иначе фикстура доказывала бы, что режим
    #: не применился, из-за собственной одинаковости, а не из-за прибора.
    return {"tag": "t", "system": False, "prompt": text, "response": text,
            "n_new_tokens": len(text), "hit_limit": hit, "stop_reason": reason,
            "metrics": m.language_metrics(text)}

LOOP = "<think>x</think>" + "размер группы G " * 40
def report(mode, n_loop, n=24):
    mark = "" if mode == "greedy" else f" [{mode}]"
    loop_p = dict(probe(LOOP, "limit_in_think", True))
    loop_p["response"] = loop_p["response"] + mark
    loop_p["metrics"] = m.language_metrics(loop_p["response"])
    ok_p = dict(probe("<think>Reasoning.</think>Ответ по-русски.", "turn_end", False))
    ok_p["response"] = ok_p["response"] + mark
    ok_p["metrics"] = m.language_metrics(ok_p["response"])
    pr = [loop_p] * n_loop + [ok_p] * (n - n_loop)
    return {"schema": "probe-language-split/1", "tool_sha256": "x",
            "protocol": {"prompts_set": "extended", "max_new_tokens": 4096,
                         "decoding": mode, "decoding_params": {"decoding": mode}},
            "states": {"sft": {"checkpoint": "/ck.pt", "probes": pr,
                               "aggregate": m.aggregate(pr, 4096)}}}

(run / "decoding_greedy.json").write_text(json.dumps(report("greedy", 15)), encoding="utf-8")
(run / "decoding_sample.json").write_text(json.dumps(report("sample_T0.7_top_p0.9", 1)), encoding="utf-8")
# тождество прибора подтверждается копией эталонного замера ядра
(run / "identity_core_cfinal_batched.json").write_text(
    pathlib.Path("runs/s3ai-probes-20260917/identity_core_cfinal.json").read_text(),
    encoding="utf-8")
(run / "loops_in_data.json").write_text(json.dumps(
    {"available": True, "n_examples": 100, "loops_share_prose": 0.01,
     "loops_share_all": 0.02}), encoding="utf-8")
# Широкий замер языка: нужен, чтобы у вердикта были числа (иначе свод обязан
# отказать по контракту — это проверяется отдельным случаем ниже).
lang = [probe("<think>Reasoning.</think>Ответ по-русски.", "turn_end", False)] * 104
(run / "language_wide.json").write_text(json.dumps(
    {"schema": "probe-language-split/1", "tool_sha256": "x",
     "protocol": {"prompts_set": "wide", "max_new_tokens": 4096, "decoding": "greedy"},
     "states": {"sft_resume_6000": {"checkpoint": "/ck.pt", "probes": lang,
                                    "aggregate": m.aggregate(lang, 4096)}}},
    ensure_ascii=False), encoding="utf-8")
out = tmp / "evidence21b.json"
r = subprocess.run([sys.executable, "tools/assemble_s3ak_evidence.py",
                    "--runs", str(run), "--out", str(out)], capture_output=True, text=True)
assert r.returncode == 0, r.stderr[-500:]
ev = json.loads(out.read_text())
dec = ev["degeneration"]["by_decoding"]
assert len(dec) == 2, dec
g = [x for x in dec if x["mode"] == "greedy"][0]
assert g["looped_share"] == round(15 / 24, 4) and g["truncated_not_looped_share"] == 0.0
v = ev["degeneration"]["verdict_cause"]
assert v["primary"] == "decoding", v
assert "training_step" not in v["causes_confirmed"], "шагов в замере не было — не выдумывать"
assert "data" not in v["causes_confirmed"], "1 % петель в данных — не источник"
# правило данных проверено и НЕ сработало — это тоже результат, и он записан
dr = [x for x in v["rules_evaluated"] if x["rule"].startswith("данные")][0]
assert dr["fired"] is False and dr["observed"]["loops_share_prose"] == 0.01
# прибор исключён: обрыва без петли нет
assert "instrument" in v["causes_excluded"]
assert ev["c012_closed"]["present"] is True
# режим доказан применённым по текстам, а не по флагу
ap = ev["degeneration"]["mode_applied"]["modes"]["sample_T0.7_top_p0.9@sft"]
assert ap["applied"] is True and ap["responses_changed"] > 0
# контракт свода сверяется производителем: все обязательные поля на месте
assert ev["contract_check"]["required_keys_present"] == [], ev["contract_check"]
for k in ("n", "state", "cyr_answer_with_zeros", "cyr_answer_defined", "cyr_think",
          "coverage", "margin_to_0.4"):
    assert ev["language"].get(k) is not None, k
assert ev["language"]["n"] == 104 and ev["language"]["cyr_answer_with_zeros"] == 1.0
assert ev["language"]["margin_to_0.4"] == round(1.0 - 0.4, 4)
assert ev["status"] == "measured" and ev["refusals"] == [], ev["refusals"]
PY

# 21b.7a Свод без замера языка обязан отказать по контракту: «поля нет» и «поле
#        пустое» для читателя одно и то же, и свод, у которого нет вердикта, не
#        должен прикидываться собранным.
# Подготовка входа — отдельным шагом: expect_exit проверяет код возврата САМОЙ
# команды, поэтому проверяемый прибор обязан быть этой командой, а не её вызовом
# изнутри скрипта-обёртки (иначе «отказ» подменялся бы кодом обёртки).
python3 - "$TMP" <<'PY'
import pathlib, shutil, sys
tmp = pathlib.Path(sys.argv[1])
run = tmp / "run21b7a"; shutil.rmtree(run, ignore_errors=True)
shutil.copytree(tmp / "run21b", run)          # каталог уже собран тестом 21b.7
(run / "language_wide.json").unlink()         # убираем ровно замер языка
PY
expect_exit 1 "assemble_s3ak_evidence: нет замера языка — отказ по контракту" \
  python3 tools/assemble_s3ak_evidence.py --runs "$TMP/run21b7a" --out "$TMP/ev_nolang.json"
expect_exit 0 "assemble_s3ak_evidence: отказ назван причиной, а не молчанием" \
  python3 - "$TMP" <<'PY'
import json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
ev = json.loads((tmp / "ev_nolang.json").read_text())
assert ev["status"] == "refused", ev["status"]
assert "language.n" in ev["contract_check"]["required_keys_present"]
assert ev["contract_check"]["note"]
assert ev["refusals"] and "обязательных полей контракта" in ev["refusals"][0]
PY

expect_exit 2 "assemble_s3ak_evidence: нет замеров — NOT-VERIFIED (не «пусто и хорошо»)" \
  python3 tools/assemble_s3ak_evidence.py --runs "$TMP/empty-contour" --out "$TMP/ev_none.json"

# 21b.8 Прибор «петли в данных»: порог берётся импортом из прибора генераций (тот же
#       самый), чтений два, а доля дубликатов сверяется с контролем ADR-033. Красный
#       путь обязателен: прибор, который только подтверждает гипотезу о данных, не
#       прибор. Проверяется фикстурой: несуществующий вход — NOT-VERIFIED.
expect_exit 0 "analyze_sft_loops: фикстуры метрики, двух чтений и дубликатов" \
  python3 tools/analyze_sft_loops.py --selftest
expect_exit 2 "analyze_sft_loops: нет входа — NOT-VERIFIED, а не «петель нет»" \
  python3 tools/analyze_sft_loops.py --input "$TMP/nope.jsonl" --out "$TMP/loops_none.json"

expect_exit 0 "analyze_sft_loops: порог и метрика — импортом, не копией" \
  python3 - <<'PY'
import importlib.util as u, pathlib, sys
sys.path.insert(0, "tools")
spec = u.spec_from_file_location("loops", pathlib.Path("tools/analyze_sft_loops.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
import probe_language_split as LS
import probe_control as PC
# тот же порог, по которому мерились генерации: иначе «та же метрика» — обещание.
# Порог не переопределён: прибор пинует ожидаемое значение и отказывает при
# расхождении (код 1), а не считает по новому молча.
assert m.LOOP_MAX4GRAM_REP_EXPECTED == LS.LOOP_MAX4GRAM_REP == 8
assert m.degenerate_metrics is PC.degenerate_metrics
assert "не переопределяется" in pathlib.Path("tools/analyze_sft_loops.py").read_text(
    encoding="utf-8")
PY
expect_exit 1 "assemble_s3ak_evidence: тождество прибора не подтверждено — отказ" \
  python3 - "$TMP" <<'PY'
import json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1]); run = tmp / "run21b7"; run.mkdir(parents=True, exist_ok=True)
(run / "decoding_greedy.json").write_text(json.dumps(
    {"schema": "probe-language-split/1", "tool_sha256": "x",
     "protocol": {"decoding": "greedy", "max_new_tokens": 4096},
     "states": {"sft": {"checkpoint": "/ck.pt", "probes": [
         {"tag": "t", "response": "Ответ.", "n_new_tokens": 3, "hit_limit": False,
          "stop_reason": "turn_end", "metrics": {}}], "aggregate": {}}}}),
    encoding="utf-8")
r = subprocess.run([sys.executable, "tools/assemble_s3ak_evidence.py", "--runs", str(run),
                    "--out", str(tmp / "ev_noident.json")], capture_output=True, text=True)
assert r.returncode == 1, r.returncode
assert "тождество прибора не подтверждено" in r.stderr, r.stderr[-300:]
PY

# 22. S3al: штатный режим декодирования (запрет повторов 4-грамм по умолчанию) и
#     прибор повторов в данных. Проверяются оба пути разрешения режима и то, что
#     «прогон без запрета» — отказ (1), а не тихое согласие.
echo "== 22. probe_language_split: штатный режим декодирования (S3al) =="

# 22.1 Разрешение режима и блок протокола — на уровне функций, без модели и без GPU.
expect_exit 0 "S3al: умолчание — запрет 4-грамм; 0 без --legacy-decoding — отказ" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
L = u.module_from_spec(spec); spec.loader.exec_module(L)

# умолчание: штатный режим, а не «как раньше»
assert L.resolve_no_repeat_ngram(None, False) == (L.STANDARD_NO_REPEAT_NGRAM, None)
assert L.STANDARD_NO_REPEAT_NGRAM == 4
# прежний протокол доступен, но только названным флагом
assert L.resolve_no_repeat_ngram(None, True) == (None, None)
# явный ноль без флага — отказ с указанием, чем чинить
n, refusal = L.resolve_no_repeat_ngram(0, False)
assert n is None and refusal and "--legacy-decoding" in refusal, (n, refusal)
assert "запрещён" in refusal
# с флагом — разрешено (воспроизведение прежних замеров)
assert L.resolve_no_repeat_ngram(0, True) == (None, None)
# положительное N принимается и без флага: это осознанный выбор, и он виден в имени
assert L.resolve_no_repeat_ngram(4, False) == (4, None)
assert L.resolve_no_repeat_ngram(8, False) == (8, None)

# блок протокола: он и есть «закреплено в конфиге», а не только в тексте плана
std = L.decoding_protocol_block(4, None, False)
assert std["standard"] is True and std["allowed_for_conclusions"] is True
assert "4" in std["default_mode"] and std["legacy_decoding"] is False
legacy = L.decoding_protocol_block(None, 0, True)
assert legacy["standard"] is False and legacy["allowed_for_conclusions"] is False
assert "greedy_only_forbidden" in legacy["rules"]
PY

# 22.2 Красный путь прибора: отказ приходит ДО загрузки модели (иначе он стоил бы
#      минуту чтения чекпойнта по сети) и называет, чем режим плох.
expect_exit 1 "S3al: прогон без запрета повторов без --legacy-decoding — отказ" \
  python3 tools/probe_language_split.py --no-repeat-ngram 0 \
  --ckpt x=/tmp/nonexistent-s3al.pt --out "$TMP/s3al_refuse.json"
expect_contains "запрещён" "S3al: отказ называет запрет, а не молчит" \
  python3 tools/probe_language_split.py --no-repeat-ngram 0 \
  --ckpt x=/tmp/nonexistent-s3al.pt --out "$TMP/s3al_refuse.json"
expect_contains "legacy-decoding" "S3al: отказ называет выход для воспроизведения" \
  python3 tools/probe_language_split.py --no-repeat-ngram 0 \
  --ckpt x=/tmp/nonexistent-s3al.pt --out "$TMP/s3al_refuse.json"

# 22.3 Штатный флаг принимается: отказ по режиму (1) не наступает, доходим до
#      NOT-VERIFIED по чекпойнту (2) — то есть режим прошёл проверку конфигурации.
expect_exit 2 "S3al: штатный --no-repeat-ngram 4 конфигурацию проходит" \
  python3 tools/probe_language_split.py --no-repeat-ngram 4 \
  --ckpt broken --out "$TMP/s3al_std.json"

# 22.4 Устройство проверяется настоящей аллокацией: недоступное устройство —
#      NOT-VERIFIED с причиной, а не трейсбек. Проверяется на подставном torch,
#      поэтому тест не зависит от наличия GPU на машине.
expect_exit 0 "S3al: недоступное устройство — причина, а не трейсбек (NOT-VERIFIED)" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("lang", pathlib.Path("tools/probe_language_split.py"))
L = u.module_from_spec(spec); spec.loader.exec_module(L)

class Boom:
    class cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def synchronize():
            raise RuntimeError("CUDA unknown error")

    @staticmethod
    def zeros(*a, **k):
        raise RuntimeError("CUDA unknown error")

class NoCuda:
    class cuda:
        @staticmethod
        def is_available():
            return False

class Fine:
    class cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def synchronize():
            return None

    @staticmethod
    def zeros(*a, **k):
        class T:
            def sum(self):
                class S:
                    @staticmethod
                    def item():
                        return 0
                return S()
        return T()

# cpu-прогон устройство не проверяет вовсе
assert L.device_problem(Fine(), "cpu") is None
assert "is_available" in L.device_problem(NoCuda(), "cuda")
p = L.device_problem(Boom(), "cuda")
assert p and "CUDA unknown error" in p, p
assert L.device_problem(Fine(), "cuda") is None
PY

# 22.5 Прибор повторов в данных: фикстуры подсчёта юнитов.
expect_exit 0 "analyze_loop_repetition: фикстуры подсчёта юнитов" \
  python3 tools/analyze_loop_repetition.py --selftest

# 22.6 Сквозной зелёный путь на фикстурах: юнит петли, который в наборе лежит
#      повтором 8+ раз, обязан включить правило «набор учит петле как образцу»;
#      юнит из генерации, которого в наборе нет, — не обязан.
expect_exit 0 "analyze_loop_repetition: вердикт по юнитам петель (учит / не учит)" \
  python3 - "$TMP" <<'PY'
import json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
ds = tmp / "s3al_ds.jsonl"
# юнит повторён в одном примере 10 раз — это и есть «набор учит петле как образцу»
taught = ("альфа бета гамма дельта " * 10) + " ".join(f"шум{i}" for i in range(40))
rows = [{"messages": [{"role": "assistant", "content": taught}]}] * 4
ds.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

gen = tmp / "s3al_gen.json"
unit = "альфа бета гамма дельта"
gen.write_text(json.dumps({"states": {"s1": {"probes": [
    {"tag": "t1", "response": (unit + " ") * 12, "metrics": {"looped": True}},
    {"tag": "t2", "response": ("совсем другой текст без повторов тут " * 6),
     "metrics": {"looped": False}}]}}}), encoding="utf-8")

out = tmp / "s3al_loops.json"
r = subprocess.run([sys.executable, "tools/analyze_loop_repetition.py",
                    "--input", str(ds), "--generations", str(gen), "--out", str(out)],
                   capture_output=True, text=True)
assert r.returncode == 0, (r.returncode, r.stderr[-500:])
d = json.loads(out.read_text())
prov = d["provenance"]["loop_units"]
assert prov["n_units"] == 1, prov["n_units"]
assert prov["found_share"] == 1.0, prov
assert prov["taught_share"] == 1.0, prov
assert prov["taught_as_pattern"] == 1
fired = {f["key"] for f in d["verdict"]["fired"]}
assert "taught_pattern" in fired, fired
# правило «набор учит петле» объявлено до прогона и названо ключом; несработавшие
# правила остаются в отчёте — иначе отрицательный результат выглядел бы как
# отсутствие вопроса
keys = [r["key"] for r in d["verdict"]["rules_evaluated"]]
assert keys == ["length_controlled", "taught_pattern", "memorized_fragment"], keys
assert all(r["fired"] for r in d["verdict"]["rules_evaluated"]
           if r["key"] in fired), "сработавшее правило обязано быть помечено fired"
assert d["method"]["loops_from"].startswith("probe_language_split")
assert d["method"]["dataset"]["n_bad_lines"] == 0
assert d["method"]["dataset"]["n_examples"] == 4
PY

# 22.7 Красные пути прибора повторов: нет набора / нет отчёта генераций / нет
#      зацикленных генераций — NOT-VERIFIED (2), а не «пустой отчёт».
expect_exit 2 "analyze_loop_repetition: нет набора — NOT-VERIFIED" \
  python3 tools/analyze_loop_repetition.py --input "$TMP/no-such-s3al.jsonl" \
  --out "$TMP/s3al_loops_missing.json"
expect_exit 2 "analyze_loop_repetition: нет отчёта генераций — NOT-VERIFIED" \
  python3 tools/analyze_loop_repetition.py --generations "$TMP/no-such-gen.json" \
  --limit 5 --out "$TMP/s3al_loops_missing2.json"
expect_exit 0 "analyze_loop_repetition: зацикленных генераций нет — NOT-VERIFIED (2)" \
  python3 - "$TMP" <<'PY'
import json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
gen = tmp / "s3al_gen_clean.json"
gen.write_text(json.dumps({"states": {"s1": {"probes": [
    {"tag": "t", "response": "обычный ответ без повторов", "metrics": {"looped": False}}]}}}),
    encoding="utf-8")
r = subprocess.run([sys.executable, "tools/analyze_loop_repetition.py",
                    "--generations", str(gen), "--limit", "5",
                    "--out", str(tmp / "s3al_none.json")], capture_output=True, text=True)
assert r.returncode == 2, (r.returncode, r.stderr[-300:])
assert "нет ни одной зацикленной" in r.stderr, r.stderr[-300:]
PY

# 23. S3am: штатный режим декодирования в АГЕНТНОЙ пробе (ADR-041) — запрет повторов
#     4-грамм по умолчанию, отказ на прогон без него и `protocol.decoding` в отчёте.
#     Проверяются оба пути и, отдельно, что запрет — это правило fairseq/transformers,
#     а не «примерно похожее»: сверка идёт с независимой реализацией того же правила.
echo "== 23. passrate_probe: штатный режим декодирования (S3am) =="

# 23.1 Разрешение режима, имя режима и блок протокола — на уровне функций, без модели
#      и без GPU. Штатный режим агентной пробы — запрет повторов ПРИ параметрах
#      контура (temperature 1.0, top_k 20), а не greedy: иначе агентность мерилась бы
#      не тем режимом, каким её мерит RL-контур.
expect_exit 0 "S3am: умолчание — запрет 4-грамм; 0 без --legacy-decoding — отказ" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("pr", pathlib.Path("tools/passrate_probe.py"))
P = u.module_from_spec(spec); spec.loader.exec_module(P)

# умолчание: штатный режим, а не «как раньше»
assert P.STANDARD_NO_REPEAT_NGRAM == 4
assert P.resolve_no_repeat_ngram(None, False) == (4, None)
# прежний протокол доступен, но только названным флагом
assert P.resolve_no_repeat_ngram(None, True) == (None, None)
# явный ноль без флага — отказ с указанием, чем чинить
n, refusal = P.resolve_no_repeat_ngram(0, False)
assert n is None and refusal and "--legacy-decoding" in refusal, (n, refusal)
assert "запрещён" in refusal and "45.1" in refusal
assert P.resolve_no_repeat_ngram(0, True) == (None, None)
# положительное N принимается и без флага: это осознанный выбор, и он виден в имени
assert P.resolve_no_repeat_ngram(4, False) == (4, None)
assert P.resolve_no_repeat_ngram(8, False) == (8, None)

# имя режима собирается из чисел, поэтому подделать его флагом нельзя
assert P.decoding_label(1.0, 20, 4) == "sample_T1_topk20_nogram4"
assert P.decoding_label(1.0, 20, None) == "sample_T1_topk20_nogram0"
assert P.decoding_label(0.7, 50, 8) == "sample_T0.7_topk50_nogram8"

# блок протокола: то, что читает свод, — и три поля, которых требует задание
std = P.decoding_protocol_block(4, None, False, 1.0, 20, 0)
assert std["mode"] == "sample_T1_topk20_nogram4"
assert std["no_repeat_ngram"] == 4 and std["temperature"] == 1.0 and std["top_k"] == 20
assert std["standard"] is True and std["allowed_for_conclusions"] is True
assert std["legacy_decoding"] is False and std["all_banned_fallbacks"] == 0
legacy = P.decoding_protocol_block(None, 0, True, 1.0, 20, 0)
assert legacy["standard"] is False and legacy["allowed_for_conclusions"] is False
assert legacy["mode"] == "sample_T1_topk20_nogram0"
assert "same_mode_as_language" in legacy["rules"]
PY

# 23.2 Красный путь: прогон без запрета отклоняется КОДОМ 1 — и отказ приходит до
#      чтения пула (то есть до кода 2 «нет входа»). Порядок проверок и есть предмет
#      теста: отказ, который можно обойти, сломав вход, — не отказ.
expect_exit 1 "S3am: прогон без запрета — отказ (1), а не «пул не найден» (2)" \
  python3 tools/passrate_probe.py --no-repeat-ngram 0 \
  --pool v1="$TMP/no-such-s3am-pool.jsonl" --run-dir "$TMP/s3am_refuse" \
  --evidence "$TMP/s3am_refuse.json"
expect_contains "запрещён" "S3am: отказ называет запрет, а не молчит" \
  python3 tools/passrate_probe.py --no-repeat-ngram 0 \
  --pool v1="$TMP/no-such-s3am-pool.jsonl" --run-dir "$TMP/s3am_refuse" \
  --evidence "$TMP/s3am_refuse.json"
expect_contains "legacy-decoding" "S3am: отказ называет выход для прежних чисел" \
  python3 tools/passrate_probe.py --no-repeat-ngram 0 \
  --pool v1="$TMP/no-such-s3am-pool.jsonl" --run-dir "$TMP/s3am_refuse" \
  --evidence "$TMP/s3am_refuse.json"

# 23.3 Зелёный путь: умолчание (штатный режим) конфигурацию проходит — прибор доходит
#      до отсутствующего входа и отвечает NOT-VERIFIED (2), а не отказом по режиму.
expect_exit 2 "S3am: штатное умолчание конфигурацию проходит" \
  python3 tools/passrate_probe.py \
  --pool v1="$TMP/no-such-s3am-pool.jsonl" --run-dir "$TMP/s3am_std" \
  --evidence "$TMP/s3am_std.json"
# ...и то же для моста воспроизводимости: legacy-режим разрешён, но назван
expect_exit 2 "S3am: --legacy-decoding снимает запрет (мост воспроизводимости)" \
  python3 tools/passrate_probe.py --no-repeat-ngram 0 --legacy-decoding \
  --pool v1="$TMP/no-such-s3am-pool.jsonl" --run-dir "$TMP/s3am_legacy" \
  --evidence "$TMP/s3am_legacy.json"

# 23.4 Запрет повторов — правило fairseq/transformers, а не «похожая правка»: сверка с
#      НЕЗАВИСИМОЙ реализацией того же правила (наивный перебор по последовательности
#      в самом тесте) на случайных последовательностях, построчно и пошагово. Плюс
#      фикстуры и проверка, что -inf ставится до top-k и не протекает между строками
#      батча.
expect_exit 0 "S3am: запрет повторов = правило transformers (сверка на фикстурах)" \
  python3 - <<'PY'
import importlib.util as u, pathlib, random
spec = u.spec_from_file_location("pr", pathlib.Path("tools/passrate_probe.py"))
P = u.module_from_spec(spec); spec.loader.exec_module(P)

def reference_banned(n, ids):
    """Независимая ссылка: токен T запрещён, если n-грамма «последние n−1 + T» уже
    встречалась в последовательности (правило fairseq; считается перебором)."""
    if len(ids) < n - 1:
        return set()
    pref = tuple(ids[len(ids) - (n - 1):]) if n > 1 else ()
    out = set()
    for i in range(len(ids) - n + 1):
        if tuple(ids[i:i + n - 1]) == pref:
            out.add(ids[i + n - 1])
    return out

# фикстуры: 4-грамма (1,2,3,4) уже была — 4 запрещён, а 9 свободен
b = P.NoRepeatNGramBan(4, [[1, 2, 3, 4, 1, 2, 3]])
assert b.banned(0) == {4}, b.banned(0)
assert 9 not in b.banned(0)
b.extend(0, 9)
assert b.banned(0) == reference_banned(4, [1, 2, 3, 4, 1, 2, 3, 9])
# n=1 — вырожденный случай: запрещено всё, что уже встречалось
assert P.NoRepeatNGramBan(1, [[5, 6, 5]]).banned(0) == {5, 6}
# пустая последовательность: запрещать нечего (и не падать)
assert P.NoRepeatNGramBan(4, [[]]).banned(0) == set()

rnd = random.Random(42)
V = 6
for _ in range(600):
    n = rnd.choice([1, 2, 3, 4, 5, 8]); L = rnd.randint(0, 14)
    ids = [rnd.randint(0, V - 1) for _ in range(L)]
    assert P.NoRepeatNGramBan(n, [ids]).banned(0) == reference_banned(n, ids), (n, ids)
# пошагово: состояние обязано совпадать со ссылкой на КАЖДОМ шаге, а не только в конце
for _ in range(150):
    n = rnd.choice([2, 3, 4]); ids = [rnd.randint(0, V - 1) for _ in range(rnd.randint(1, 8))]
    ban = P.NoRepeatNGramBan(n, [ids])
    for _ in range(6):
        assert ban.banned(0) == reference_banned(n, ids), (n, ids)
        nxt = rnd.randint(0, V - 1); ban.extend(0, nxt); ids = ids + [nxt]

import torch
ban = P.NoRepeatNGramBan(4, [[1, 2, 3, 4, 1, 2, 3], [7, 8, 9, 5, 7, 8, 9]])
out = P.apply_ngram_ban(torch.zeros(2, 12), ban, torch)
assert out[0, 4].item() == float("-inf") and out[0, 5].item() == 0.0
assert out[1, 5].item() == float("-inf") and out[1, 4].item() == 0.0, out[1][:6]
assert out[0, 5].item() == 0.0 and out[1, 4].item() == 0.0   # запреты строк не смешались
# без запрета логиты не тронуты вовсе (мост воспроизводимости прежних чисел)
clean = torch.zeros(1, 8)
assert float(P.apply_ngram_ban(clean, None, torch)[0, 4]) == 0.0
PY

# 23.5 Прибор языковых проб и агентная проба обязаны решать вопрос о режиме одинаково:
#      «единый штатный режим стадии» — это утверждение о двух приборах, и оно
#      проверяется сравнением их разрешителей, а не текстом плана.
expect_exit 0 "S3am: разрешитель режима тот же, что у языковой пробы" \
  python3 - <<'PY'
import importlib.util as u, pathlib
def load(name, path):
    spec = u.spec_from_file_location(name, pathlib.Path(path))
    m = u.module_from_spec(spec); spec.loader.exec_module(m); return m
PR = load("pr", "tools/passrate_probe.py")
LS = load("ls", "tools/probe_language_split.py")
assert PR.STANDARD_NO_REPEAT_NGRAM == LS.STANDARD_NO_REPEAT_NGRAM == 4
for req in (None, 0, 4, 8):
    for legacy in (False, True):
        a = PR.resolve_no_repeat_ngram(req, legacy)
        b = LS.resolve_no_repeat_ngram(req, legacy)
        assert (a[0], bool(a[1])) == (b[0], bool(b[1])), (req, legacy, a, b)
PY

# 23.6 Режим доезжает до ОТЧЁТА, а не только до функций: сводка пересобирается из
#      сырых записей (путь без GPU) — и `protocol.decoding` обязан быть в отчёте с
#      четырьмя полями, которых требует задание. Проверять это на функциях
#      недостаточно: «поле в блоке» и «поле в отчёте» — разные утверждения.
expect_exit 0 "S3am: protocol.decoding доезжает до отчёта (пересборка без GPU)" \
  python3 - "$TMP" <<'PY'
import json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
run = tmp / "s3am_sum"; run.mkdir(parents=True, exist_ok=True)
rec = {"task_index": 1, "task_type": "find_concept", "turns": 1, "tool_calls": 1,
       "tool_errors": 0, "hit_timeout": False, "hit_context_guard": False,
       "answer_chars": 10, "assistant_tokens": 5, "pass": 1, "reward": 1.0,
       "tool_error": False, "gold_resolvable": True, "coverage": 1.0, "diag_class": None}
(run / "tasks.jsonl").write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
proto = {"temperature": 1.0, "top_k": 20, "n_attempts": 1,
         "decoding": {"mode": "sample_T1_topk20_nogram4", "no_repeat_ngram": 4,
                      "temperature": 1.0, "top_k": 20, "standard": True,
                      "allowed_for_conclusions": True}}
(run / "probe_meta.json").write_text(
    json.dumps({"pool_name": "v1", "pool": {"sha256": "x"}, "protocol": proto, "strata": {}}),
    encoding="utf-8")
out = tmp / "s3am_sum_ev.json"
r = subprocess.run([sys.executable, "tools/passrate_probe.py", "--summarize",
                    str(run / "tasks.jsonl"), "--evidence", str(out)],
                   capture_output=True, text=True)
assert r.returncode == 0, (r.returncode, r.stderr[-400:])
dec = json.loads(out.read_text())["protocol"]["decoding"]
assert dec["mode"] == "sample_T1_topk20_nogram4", dec
assert dec["no_repeat_ngram"] == 4 and dec["temperature"] == 1.0 and dec["top_k"] == 20, dec
assert dec["allowed_for_conclusions"] is True
PY

# 23.7 Прибор «запрет против канала вызова»: фикстуры правила (без токенизатора),
#      NOT-VERIFIED без входа и — важно — NOT-VERIFIED, а не трейсбек, на
#      интерпретаторе без рабочего transformers (в кейсе их два, и `python3` из PATH
#      именно такой). Последнее ловится зелёным путём прибора на настоящем контуре.
expect_exit 0 "analyze_agentic_ban_interaction: фикстуры правила и контролей" \
  python3 tools/analyze_agentic_ban_interaction.py --selftest
expect_exit 2 "analyze_agentic_ban_interaction: нет прогона — NOT-VERIFIED" \
  python3 tools/analyze_agentic_ban_interaction.py --run "$TMP/no-such-s3am-run" \
  --out "$TMP/s3am_ban_none.json"
expect_exit 2 "analyze_agentic_ban_interaction: без transformers — NOT-VERIFIED, не трейсбек" \
  python3 tools/analyze_agentic_ban_interaction.py \
  --run runs/passrate-agentic-cpt-20260917-0153 --out "$TMP/s3am_ban_nohf.json"
expect_contains "NOT-VERIFIED" "analyze_agentic_ban_interaction: причина названа, а не молчание" \
  python3 tools/analyze_agentic_ban_interaction.py \
  --run runs/passrate-agentic-cpt-20260917-0153 --out "$TMP/s3am_ban_nohf.json"

# 23.8 Зелёный путь прибора на настоящем контуре и КРАСНЫЙ путь отказа: требует
#      токенизатора, поэтому исполняется документированным интерпретатором прогонов.
#      Если его нет — тесты **пропускаются с названной причиной**, а не зеленеют по
#      тишине (выше уже проверено, что без transformers прибор отвечает NOT-VERIFIED).
cat > "$TMP/s3am_fake_pipeline.py" <<'PY'
SPECIAL_TOKENS = ["<think>", "</think>"]
UNIFIED_SYSTEM_PROMPT = "Ты помощник. Отвечай кратко."
PY
if [ -x /usr/bin/python3 ] && /usr/bin/python3 -c "import transformers" >/dev/null 2>&1; then
  expect_exit 0 "analyze_agentic_ban_interaction: канонический вызов запрещён (реальный контур)" \
    /usr/bin/python3 tools/analyze_agentic_ban_interaction.py \
    --run runs/passrate-agentic-cpt-20260917-0153 \
    --out "$TMP/s3am_ban_real.json"
  expect_contains "blocked_forms" "analyze_agentic_ban_interaction: отчёт называет формы" \
    /usr/bin/python3 tools/analyze_agentic_ban_interaction.py \
    --run runs/passrate-agentic-cpt-20260917-0153 \
    --out "$TMP/s3am_ban_real.json"
  expect_exit 1 "analyze_agentic_ban_interaction: механизм не подтверждён — отказ" \
    /usr/bin/python3 tools/analyze_agentic_ban_interaction.py \
    --run runs/passrate-agentic-cpt-20260917-0153 --pipeline "$TMP/s3am_fake_pipeline.py" \
    --out "$TMP/s3am_ban_fake.json"
  expect_contains "не подтверждён" "analyze_agentic_ban_interaction: отказ называет причину" \
    /usr/bin/python3 tools/analyze_agentic_ban_interaction.py \
    --run runs/passrate-agentic-cpt-20260917-0153 --pipeline "$TMP/s3am_fake_pipeline.py" \
    --out "$TMP/s3am_ban_fake.json"
else
  echo "  skip analyze_agentic_ban_interaction: нет /usr/bin/python3 с transformers —"
  echo "       зелёный и красный пути на настоящем контуре не проверены (фикстуры проверены)"
fi

echo "== 24. normalize_sft_dataset.py + check_measurement_overlap.py (S3an, ADR-042) =="

# 24.1 Правила R1–R7 и проверка обратимости — на фикстурах, без данных и без стенда.
#      Каждое правило проверяется и тем, что оно СРАБОТАЛО (tag_rules), и тем, что
#      структура после него валидна (дефектов 0). Проверка «сработало» без второй
#      половины пропускала бы правило, которое ломает формат.
expect_exit 0 "S3an: правила нормализации R1/R2/R3/R4/R6/R7 + обратимость" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("nz", pathlib.Path("tools/normalize_sft_dataset.py"))
N = u.module_from_spec(spec); spec.loader.exec_module(N)

CALL = '<tool_call>{"name": "search_concepts", "query": "x"}</tool_call>'
RESP = "<tool_response>slug: x</tool_response>"
ANS = "Ответ на задачу, достаточно длинный, чтобы пройти порог в двадцать символов."

def norm(text):
    new, edits, rules = N.normalize_message(text)
    # структура обязана быть валидной: блоки парны и не вложены
    assert N._strip_blocks(new) is not None
    return new, edits, rules

# R1: <tool_call> без нагрузки-вызова (остаток шаблона) выбрасывается, текст остаётся
new, edits, rules = norm(f"<think>рассуждение\n<tool_call>\nещё рассуждение</think>{CALL}\n{RESP}\n{ANS}")
assert rules["R1_drop_noncall_tool_call"] == 1, rules
assert "<tool_call>\nещё" not in new and "ещё рассуждение" in new
assert N.revert(new, [e.as_dict() for e in edits]) == f"<think>рассуждение\n<tool_call>\nещё рассуждение</think>{CALL}\n{RESP}\n{ANS}"

# R2: рассуждение закрывается в точке перехода в вызов; рассуждения не было — блок пуст
new, edits, rules = norm(f"<think>{CALL}\n{RESP}\n{ANS}")
assert rules["R2_close_think_before_call"] == 1, rules
assert new.startswith("<think></think>"), new[:40]

# R3: повторный <think> при открытом рассуждении выбрасывается, текст сохраняется
new, edits, rules = norm(f"<think>первая часть<think>вторая часть</think>\n{ANS}")
assert rules["R3_drop_duplicate_think_open"] == 1, rules
assert new.startswith("<think>первая частьвторая часть</think>"), new[:50]

# R4: </think> вне открытого рассуждения выбрасывается
new, edits, rules = norm(f"<think>р</think></think>\n{ANS}")
assert rules["R4_drop_stray_think_close"] == 1, rules
assert new.count("</think>") == 1

# R6: </tool_call> вне вызова выбрасывается
new, edits, rules = norm(f"<think>р</think></tool_call>{CALL}\n{RESP}\n{ANS}")
assert rules["R6_drop_stray_block_close"] == 1, rules

# R7: валидный JSON-вызов без закрывающего тега — тег достраивается по концу JSON
new, edits, rules = norm(f"<think>р</think><tool_call>{{\"name\": \"search_concepts\", \"query\": \"x\"}}\n{RESP}\n{ANS}")
assert rules["R7_insert_missing_call_close"] == 1, rules
assert '</tool_call>' in new and N.defects_of({"messages": [{"role": "assistant", "content": new}]}) == {
    "unclosed_think": False, "tool_call_in_think": False, "no_answer": False}

# R5 (ветка ответа): повторный <tool_response> при открытом выбрасывается
new, edits, rules = norm(f"<think>р</think>{CALL}<tool_response>первый<tool_response>второй</tool_response>\n{ANS}")
assert rules["R5_drop_duplicate_block_open"] == 1, rules

# system/user не трогаются: в system-промпте теги стоят как пример формата
rec = {"messages": [{"role": "system", "content": "Вызови <tool_call>{\"name\": \"x\"}</tool_call>"},
                    {"role": "user", "content": "вопрос <think>как бы</think>"},
                    {"role": "assistant", "content": f"<think>р</think>{CALL}\n{RESP}\n{ANS}"}]}
out, log_edits, rules, exc = N.normalize_record(rec)
assert exc is None and log_edits == [] and out is rec, (exc, log_edits)
assert rec["messages"][0]["content"].startswith("Вызови <tool_call>")
PY

# 24.2 Красные пути: каждый класс, который механикой НЕ чинится, исключается со
#      своей причиной. Причина — данные, по ней считается доля исключённых (ADR-042 п.2).
expect_exit 0 "S3an: красные пути — обрыв, цитата тега, отсутствие ответа" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("nz", pathlib.Path("tools/normalize_sft_dataset.py"))
N = u.module_from_spec(spec); spec.loader.exec_module(N)

def why(text):
    rec = {"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": text}]}
    return N.normalize_record(rec)[3]

# (а) рассуждение обрывается на середине — закрывать нечем, ответа тоже нет
assert why("<think>Рассуждение оборвалось на середине и не закрылось") == "unclosed_block_at_end"
# (б) вложенный tool_call: пара с нагрузкой-не-JSON (тег процитирован внутри рассуждения)
assert why("<think>Выведи так: <tool_call>...</tool_call>\nВот ответ длиной больше двадцати символов.") == "quoted_tag_pair_in_reasoning"
# (в) ответа после рассуждения нет
assert why('<think>р</think><tool_call>{"a": 1}</tool_call><tool_response>d</tool_response>') == "no_answer"
# (г) ответ короче порога — тоже «нет ответа»
assert why('<think>р</think><tool_call>{"a": 1}</tool_call><tool_response>d</tool_response>Да.') == "no_answer"
# (д) обрыв внутри вызова: нагрузка не разбирается и не закрыта. Тег снимается
#     как остаток шаблона (R1) — тогда текстом примера становится сам обрывок
#     JSON, и он ответом не считается: пример отсекается по ответу.
assert why('<think>р</think><tool_call>{"name": broken') == "no_answer"
assert N.normalize_message('<think>р</think><tool_call>{"name": broken')[2][
    "R1_drop_noncall_tool_call"] == 1
# и то же с валидным JSON без тега — чинится (R7), а не исключается (контроль к (д))
rec = {"messages": [{"role": "assistant", "content": '<think>р</think><tool_call>{"a": 1}<tool_response>d</tool_response>Ответ длиннее двадцати символов.'}]}
assert N.normalize_record(rec)[3] is None
# (е) названный остаток: ответ инструмента без вызова — не дефект ADR-042, но и не
#     «всё хорошо»; он обязан считаться, иначе «0 % дефектов» читается как гарантия
res = N.residual_notes({"messages": [{"role": "assistant",
                                      "content": "<think>р</think><tool_response>d</tool_response>Ответ длиннее двадцати символов."}]})
assert res == {"tool_response_without_call"}, res
PY

# 24.3 Зелёный путь целиком через CLI: дефекты до — названы, после — нули, примеры
#      без правок выходят байт-в-байт (иначе «нормализация» меняет весь набор).
expect_exit 0 "S3an CLI: аудит+запись на фикстуре — дефекты после нулевые" \
  bash -c 'python3 - "$1" <<PY
import json, sys
# Фикстура пишется тем же сериализатором, что и набор (json.dumps по умолчанию):
# иначе «байт-в-байт» проверяло бы формат фикстуры, а не нормализацию.
d = sys.argv[1]
def rec(content):
    return {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
                         {"role": "assistant", "content": content}]}
CALL = chr(60) + "tool_call" + chr(62) + json.dumps({"name": "search_concepts", "query": "x"}) + chr(60) + "/tool_call" + chr(62)
lines = [
    rec("<think>размышление" + CALL + chr(10) + "<tool_response>d</tool_response>" + chr(10)
        + "Ответ на вопрос, длиннее двадцати символов."),
    rec("<think>р</think>" + CALL + chr(10) + "<tool_response>d</tool_response>" + chr(10)
        + "Второй ответ, тоже достаточно длинный."),
]
with open(d + "/fx_in.jsonl", "w", encoding="utf-8") as fh:
    for r in lines:
        fh.write(json.dumps(r, ensure_ascii=False) + chr(10))
PY
python3 tools/normalize_sft_dataset.py --in "$1/fx_in.jsonl" --out "$1/fx_out.jsonl" \
  --report "$1/fx_rep.json" --log "$1/fx_log.jsonl" >/dev/null && python3 -c "
import json,sys
r=json.load(open(sys.argv[1])); assert r[\"input\"][\"examples\"]==2, r[\"input\"]
assert r[\"defects_before\"][\"unclosed_think\"][\"examples\"]==1, r[\"defects_before\"]
assert r[\"defects_after\"]=={}, r[\"defects_after\"]
assert r[\"changed_examples\"]==1 and r[\"byte_identical_records\"]==1, r
assert r[\"reversibility\"][\"failures\"]==0, r[\"reversibility\"]
log=open(sys.argv[2]).read(); assert \"R2_close_think_before_call\" in log, log
assert \"edits\" in log and \"content_sha256_before\" in log, log
" "$1/fx_rep.json" "$1/fx_log.jsonl"' _ "$TMP"

# 24.4 Красные пути CLI: нечитаемый выход остаётся дефектным → код 1, нет входа → 2
expect_exit 2 "S3an CLI: нет входа — NOT-VERIFIED (2), а не зелёный" \
  python3 tools/normalize_sft_dataset.py --audit --in "$TMP/no-such-s3an.jsonl"
printf '%s\n' '{"messages":[{"role":"assistant","content":"<think>не закрыт"}]}' > "$TMP/s3an_red.jsonl"
expect_exit 1 "S3an CLI: --verify на дефектном наборе — отказ (1)" \
  python3 tools/normalize_sft_dataset.py --verify --in "$TMP/s3an_red.jsonl"
expect_contains '"pass": false' "S3an CLI: --verify называет непройденную проверку" \
  python3 tools/normalize_sft_dataset.py --verify --in "$TMP/s3an_red.jsonl"

# 24.5 check_measurement_overlap: документ измерительного набора внутри примера — красный
mkdir -p "$TMP/s3an"
DOC="Павлов Александр Сергеевич — государственный и политический деятель Республики Казахстан, биография которого описана в измерительном наборе K1 и потому не должна встречаться в обучении."
printf '%s\n' "$DOC" > "$TMP/s3an/k1.txt"
printf '%s\n' '{"messages":[{"role":"assistant","content":"<think>р</think> Ответ достаточно длинный, чтобы пройти порог по длине текста."}]}' > "$TMP/s3an/train_clean.jsonl"
python3 - "$TMP/s3an/train_dirty.jsonl" "$DOC" <<'PY'
import json, sys
json.dump({"messages": [{"role": "assistant", "content": "начало " + sys.argv[2] + " конец"}]},
          open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False)
open(sys.argv[1], "a").write("\n")
PY
expect_exit 0 "S3an overlap: чистый набор — вхождений нет (0)" \
  python3 tools/check_measurement_overlap.py --train "$TMP/s3an/train_clean.jsonl" --set K1:"$TMP/s3an/k1.txt"
expect_exit 1 "S3an overlap: документ замера внутри примера — отказ (1)" \
  python3 tools/check_measurement_overlap.py --train "$TMP/s3an/train_dirty.jsonl" --set K1:"$TMP/s3an/k1.txt"
expect_contains "нарушений 1" "S3an overlap: нарушение названо числом, а не свёрнуто" \
  python3 tools/check_measurement_overlap.py --train "$TMP/s3an/train_dirty.jsonl" --set K1:"$TMP/s3an/k1.txt"
expect_exit 2 "S3an overlap: нет набора замера — NOT-VERIFIED (2)" \
  python3 tools/check_measurement_overlap.py --train "$TMP/s3an/train_clean.jsonl" --set K1:"$TMP/no-such-k1.txt"

# 24.6 check_measurement_overlap --train-raw: обучение — не только jsonl задач.
#      CPT-корпуса лежат сырым текстом, и вопрос «документ замера внутри обучения»
#      ставится ко всему обучению. Красный путь здесь не «тот же тест другим
#      флагом»: единица сырого корпуса — блок, а документ замера может лечь ровно
#      на границу блоков. Проверяется именно это — что склейка соседних блоков
#      ищется (иначе гейт молча пропускал бы такое вхождение).
python3 - "$TMP/s3an" <<'PY'
import pathlib, sys
d = pathlib.Path(sys.argv[1])
first = "Первый блок корпуса, достаточно длинный для порога прибора замера перплексии."
second = "Второй блок корпуса, тоже длинный, он и станет второй половиной документа замера."
(d / "raw_hit.txt").write_text(first + "\n\n" + second + "\n\n"
                               + "Третий блок, с замером не связанный, длинный настолько, чтобы пройти порог." + "\n",
                               encoding="utf-8")
(d / "raw_miss.txt").write_text(first + "\n\n"
                                + "Совсем другой блок, длинный, порог проходит, с замером не пересекается ничем." + "\n",
                                encoding="utf-8")
(d / "span.txt").write_text(first + "\n\n" + second + "\n", encoding="utf-8")
PY
expect_exit 1 "S3ao overlap raw: документ на границе блоков корпуса — отказ (1)" \
  python3 tools/check_measurement_overlap.py --train-raw RAW:"$TMP/s3an/raw_hit.txt" --set M:"$TMP/s3an/span.txt"
expect_contains "абзацы 0+1" "S3ao overlap raw: вхождение названо парой абзацев, а не свёрнуто" \
  python3 tools/check_measurement_overlap.py --train-raw RAW:"$TMP/s3an/raw_hit.txt" --set M:"$TMP/s3an/span.txt"
expect_exit 0 "S3ao overlap raw: второй половины в корпусе нет — зелёный (0)" \
  python3 tools/check_measurement_overlap.py --train-raw RAW:"$TMP/s3an/raw_miss.txt" --set M:"$TMP/s3an/span.txt"
expect_exit 2 "S3ao overlap: обучающий вход не задан вовсе — NOT-VERIFIED (2)" \
  python3 tools/check_measurement_overlap.py --set M:"$TMP/s3an/span.txt"
expect_exit 2 "S3ao overlap: нет сырого корпуса — NOT-VERIFIED (2)" \
  python3 tools/check_measurement_overlap.py --train-raw RAW:"$TMP/no-such-raw.txt" --set M:"$TMP/s3an/span.txt"

# 24.7 Журнал указывает на ПРАВИЛЬНУЮ строку нового набора. Проверка ловит сдвиг
#      на единицу: если исключённый пример стоит до изменённого, а `out_index`
#      считается после добавления, журнал укажет на следующий пример — и снятие
#      правок вернёт ЧУЖОЙ текст (проверено end-to-end на наборе, 10 821 случай).
expect_exit 0 "S3an: out_index журнала указывает на свой пример (снятие правок сходится)" \
  bash -c 'python3 - "$1" <<PY
import json, sys
d = sys.argv[1]
def rec(c):
    return {"messages": [{"role": "assistant", "content": c}]}
lines = [
    rec("<think>обрыв на середине без ответа"),                                  # исключён
    rec("<think>р<think>ещё</think> Ответ длиной больше двадцати символов."),     # изменён
    rec("<think>р</think> Ответ длиной больше двадцати символов, второй."),        # без правок
]
with open(d + "/oi_in.jsonl", "w", encoding="utf-8") as fh:
    for r in lines:
        fh.write(json.dumps(r, ensure_ascii=False) + chr(10))
PY
python3 tools/normalize_sft_dataset.py --in "$1/oi_in.jsonl" --out "$1/oi_out.jsonl" \
  --log "$1/oi_log.jsonl" >/dev/null && python3 - "$1" <<PY
import hashlib, importlib.util as u, json, pathlib, sys
d = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("nz", pathlib.Path("tools/normalize_sft_dataset.py"))
N = u.module_from_spec(spec); spec.loader.exec_module(N)
out = [json.loads(l) for l in open(d / "oi_out.jsonl", encoding="utf-8") if l.strip()]
src = [json.loads(l) for l in open(d / "oi_in.jsonl", encoding="utf-8") if l.strip()]
entries = [json.loads(l) for l in open(d / "oi_log.jsonl", encoding="utf-8")]
assert len(entries) == 2, entries           # один исключён, один изменён
e = [x for x in entries if "excluded" not in x][0]
assert e["index"] == 2 and e["out_index"] == 1, e   # сдвиг на единицу — уже ошибка
me = e["messages"][0]
got = out[e["out_index"] - 1]["messages"][0]["content"]
assert hashlib.sha256(got.encode()).hexdigest() == me["content_sha256_after"]
back = N.revert(got, me["edits"])
assert back == src[e["index"] - 1]["messages"][0]["content"], back
PY' _ "$TMP"

echo "== 25. build_domain_eval_v3.py + measure_corpus_residual.py (S3ao, ADR-043 п.3) =="

# 25.1 Сборщик домен-набора: формат, детерминизм, отказ на пустом корне.
#      Фикстура — .md-файлы с русской ML-лексикой: расширение читается тем же
#      путём, что и DOCX, а в дерево кейса ничего не копируется.
mkdir -p "$TMP/s3ao/root"
python3 - "$TMP/s3ao/root" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
body = ("Обучение агентной модели с подкреплением: контекст, промпты и токены. "
        "Модель получает reward от верификатора, rollout собирается в датасет, "
        "инференс идёт через vLLM, а бенчмарк считается по GRPO. ") * 8
for i in range(6):
    (root / f"doc{i}.md").write_text(f"# Разбор {i}\n\n" + body + f"\n\nВторая часть {i}. " + body,
                                     encoding="utf-8")
# английский и короткий файлы обязаны отсеяться языковым порогом и порогом длины
(root / "en.md").write_text("# Paper\n\n" + "This paper studies reinforcement learning for agents. " * 40,
                            encoding="utf-8")
(root / "short.md").write_text("Короткая заметка про модель.", encoding="utf-8")
PY
expect_exit 0 "S3ao build: сборка из .md-корня (6 источников × док.)" \
  python3 tools/build_domain_eval_v3.py --root "$TMP/s3ao/root" --jobs 1 \
    --min-chars 500 --min-ml 20 --docs 6 \
    --out "$TMP/s3ao/out1.txt" --card "$TMP/s3ao/card1.json" --dry-run
expect_exit 0 "S3ao build: набор пишется и проходит --verify" \
  bash -c "python3 tools/build_domain_eval_v3.py --root '$TMP/s3ao/root' --jobs 1 \
    --min-chars 500 --min-ml 20 --docs 6 --out '$TMP/s3ao/out1.txt' \
    --card '$TMP/s3ao/card1.json' >/dev/null && \
    python3 tools/build_domain_eval_v3.py --verify '$TMP/s3ao/out1.txt'"
# Детерминизм: второй прогон обязан дать тот же файл (сид, сортировка, разрез).
expect_exit 0 "S3ao build: повторная сборка даёт тот же файл (детерминизм)" \
  bash -c "python3 tools/build_domain_eval_v3.py --root '$TMP/s3ao/root' --jobs 1 \
    --min-chars 500 --min-ml 20 --docs 6 --out '$TMP/s3ao/out2.txt' \
    --card '$TMP/s3ao/card2.json' >/dev/null && \
    cmp -s '$TMP/s3ao/out1.txt' '$TMP/s3ao/out2.txt'"
# Красный путь: в наборе есть кусок короче порога прибора — он молча выпал бы из
# замера, и «200 документов» оказались бы 199. Проверка обязана это назвать.
expect_exit 1 "S3ao build: документ короче порога прибора — отказ (1)" \
  bash -c "printf 'Первый документ, достаточно длинный, чтобы пройти порог прибора замера перплексии.\\n---\\nКороткий.\\n' > '$TMP/s3ao/bad.txt'; \
    python3 tools/build_domain_eval_v3.py --verify '$TMP/s3ao/bad.txt'"
expect_exit 2 "S3ao build: нет корня поиска — NOT-VERIFIED (2)" \
  python3 tools/build_domain_eval_v3.py --root "$TMP/s3ao/no-such-root" --dry-run
expect_contains "ОТКАЗ" "S3ao build: пул меньше заказанного объёма назван отказом" \
  python3 tools/build_domain_eval_v3.py --root "$TMP/s3ao/root" --jobs 1 \
    --min-chars 500 --min-ml 20 --docs 999 --dry-run

# 25.2 Замер остатка корпуса. Проверяется не «сколько токенов», а правило:
#      воспроизведение обязано сойтись с кэшем чанков, иначе это отказ (1), а не
#      число. Токенизатор — настоящий: подделка проверяла бы не тот прибор.
expect_exit 2 "S3ao residual: нет корпуса — NOT-VERIFIED (2)" \
  python3 tools/measure_corpus_residual.py --corpus "$TMP/s3ao/no-corpus.txt" \
    --chunks-npy "$TMP/s3ao/no.npy"
expect_exit 2 "S3ao residual: нет кэша чанков — NOT-VERIFIED (2)" \
  python3 tools/measure_corpus_residual.py --corpus "$TMP/s3ao/out1.txt" --chunks-npy "$TMP/s3ao/no.npy"
expect_exit 1 "S3ao residual: чанков в кэше больше, чем даёт поток — отказ (1)" \
  python3 - "$TMP/s3ao" <<'PY'
import pathlib, subprocess, sys, numpy as np
d = pathlib.Path(sys.argv[1])
np.save(d / "two.npy", np.zeros((2, 100), dtype=np.int32))
r = subprocess.run([sys.executable, "tools/measure_corpus_residual.py",
                    "--corpus", str(d / "out1.txt"), "--chunks-npy", str(d / "two.npy"),
                    "--json", str(d / "res_two.json")], capture_output=True, text=True)
sys.exit(r.returncode)
PY

# 25.3 Зелёный путь сторожа остатка: кэш чанков построен ИЗ ТОГО ЖЕ потока, что
#      считает инструмент (та же нарезка, тот же токенизатор — берутся из самого
#      инструмента, а не переписаны рядом). Проверяется не «exit 0», а совпадение
#      предсказанного остатка с посчитанным независимо: иначе тест проверял бы
#      арифметику сам собой.
expect_exit 0 "S3ao residual: воспроизведение сошлось, остаток назван числом" \
  python3 - "$TMP/s3ao" <<'PY'
import importlib.util as u, json, pathlib, subprocess, sys
import numpy as np
d = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("mcr", pathlib.Path("tools/measure_corpus_residual.py"))
M = u.module_from_spec(spec); spec.loader.exec_module(M)
tf, _ = M.import_transformers()
tok = M.build_tokenizer(M.resolve_tokenizer_path(M.TOKENIZER_CANDIDATES[0]), tf)
text = (d / "out1.txt").read_text(encoding="utf-8")
units = [x.strip() for x in text.split(M.DOC_SEP) if len(x.strip()) >= M.MIN_UNIT_CHARS]
total = sum(len(tok.encode(x, add_special_tokens=False)) for x in units)
max_len = 64
chunks = (total - 1) // max_len                # ровно то число, что предскажет инструмент
np.save(d / "fit.npy", np.zeros((chunks, max_len), dtype=np.int32))
r = subprocess.run([sys.executable, "tools/measure_corpus_residual.py",
                    "--corpus", str(d / "out1.txt"), "--chunks-npy", str(d / "fit.npy"),
                    "--json", str(d / "res_fit.json")], capture_output=True, text=True)
if r.returncode != 0:
    print(r.stdout, r.stderr)
    sys.exit(r.returncode)
rep = json.loads((d / "res_fit.json").read_text(encoding="utf-8"))
assert rep["chunks_match"] is True, rep
assert rep["tokens_total"] == total, (rep["tokens_total"], total)
assert rep["tokens_used"] == chunks * max_len, rep
assert rep["tokens_residual"] == total - chunks * max_len, rep
PY

echo "== 26. S3ap: свод монитора формата на точках SFT(v13) =="

# 26.1 Нормативная база пиннуется **источником**, а не памятью: числа ADR-042 п.5
#      (0.7308 / 0.3365 / 0.8269 / 0.0481 / 0.0096, медиана 1164) обязаны читаться
#      из замороженного артефакта S3al тем же разбором, каким их читает свод. Тест
#      сломается, если артефакт перепишут или разбор метрик разъедется с ним.
expect_exit 0 "S3ap: эталон CPT читается из артефакта S3al (ADR-042 п.5) тем же разбором" \
  python3 - <<'PY'
import importlib.util as u, json, pathlib
spec = u.spec_from_file_location("s3ap", pathlib.Path("tools/assemble_s3ap_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
rep = json.loads(pathlib.Path(m.BASELINE_REPORT).read_text(encoding="utf-8"))
agg = rep["states"]["cfinal"]["aggregate"]
got = m.state_metrics(agg)
assert got["unclosed_think"] == 0.3365, got
assert got["tool_call"] == 0.7308, got
assert got["answer_coverage"] == 0.8269, got
assert got["truncated"] == 0.0481 and got["looped"] == 0.0096, got
assert got["median_len"] == 1164 and got["n"] == 104, got
# парность маркеров — не то же, что «незакрытые»: лишний `</think>` виден отдельно
assert got["marker_pairing_ok"] == round(1 - 0.3365 - 0.3942, 4), got["marker_pairing_ok"]
PY

# 26.2 Фикстура свода: состояния собираются из синтетического отчёта, знак дельты
#      читается по смыслу метрики (у обрывов «меньше — лучше»), вердикт и
#      рекомендация следуют за числами, а не за ожиданием автора. Проверяются все
#      три исхода: хуже CPT, лучше CPT, в пределах шума.
expect_exit 0 "S3ap: вердикт и рекомендация — по знаку метрики (хуже / лучше / шум)" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("s3ap", pathlib.Path("tools/assemble_s3ap_evidence.py"))
# CASE оставляем настоящим (нужен хеш прибора), а входы свода уводим в фикстуру:
# абсолютный путь в `CASE / rel` подменяет базу — так тест не пишет в дерево кейса.
m = u.module_from_spec(spec); spec.loader.exec_module(m)
m.REPORT = str(tmp / "rep.json"); m.BASELINE_REPORT = str(tmp / "base.json")
m.IDENTITY_NEW = str(tmp / "id_new.json"); m.IDENTITY_REF = str(tmp / "id_ref.json")

def agg(uncl, tool, cov, trunc=0.05, loop=0.01, median=1200):
    return {"n": 104, "unclosed_think_share": uncl, "mode_share_tool_call": tool,
            "cyr_answer": {"with_zeros": cov * 0.5, "coverage": cov},
            "cyr_think": {"with_zeros": 0.3}, "truncated_share": trunc,
            "looped_share": loop, "lengths": {"median": median},
            "stray_think_close_share": 0.1, "mode_share_think": 0.9,
            "think_blocks_mean": 1.5, "stop": {"natural_stop_share": 0.95,
            "reasons_share": {"turn_end": 0.95, "limit_in_think": 0.05}}}

PROTO = {"prompts_set": "wide", "prompts_digest": "d" * 64, "n_prompts": 104,
         "max_new_tokens": 4096, "stop_at_turn_end": True,
         "decoding_protocol": {"standard": True, "legacy_decoding": False,
                               "allowed_for_conclusions": True, "default_mode": "greedy_nogram4"}}
def report(states):
    return {"tool_sha256": m.sha256_file(pathlib.Path("tools/probe_language_split.py")),
            "device": "cuda", "protocol": PROTO, "states": states}
def st(ckpt, a):
    return {"checkpoint": ckpt, "checkpoint_sha256": "0" * 64, "checkpoint_bytes": 1,
            "aggregate": a}

BEST = {"n": 104, "unclosed_think_share": 0.3365, "mode_share_tool_call": 0.7308,
        "cyr_answer": {"with_zeros": 0.3964, "coverage": 0.8269}, "cyr_think": {"with_zeros": 0.3072},
        "truncated_share": 0.0481, "looped_share": 0.0096, "lengths": {"median": 1164},
        "stray_think_close_share": 0.3942, "mode_share_think": 0.8654,
        "think_blocks_mean": 1.6538, "stop": {"natural_stop_share": 0.9519,
        "reasons_share": {"turn_end": 0.9519}}}
(tmp / "base.json").write_text(json.dumps(report({"cfinal": st("/m/cpt.pt", BEST)})), "utf-8")
(tmp / "id_ref.json").write_text(json.dumps({"states": {"cfinal": {"probes": [
    {"response": "same"}]}}}), "utf-8")
(tmp / "id_new.json").write_text(json.dumps({"states": {"cfinal": {"probes": [
    {"response": "same"}]}}}), "utf-8")

# (а) все три метрики значимо хуже CPT → рекомендация остановить стадию
worse = report({"cfinal": st("/m/cpt.pt", BEST),
                "sft_v13_0500": st("/m/sft_probe_500.pt", agg(0.55, 0.50, 0.50)),
                "sft_v13_5000": st("/m/sft_probe_5000.pt", agg(0.70, 0.45, 0.48))})
(tmp / "rep.json").write_text(json.dumps(worse), "utf-8")
rc, doc = m.build()
assert rc == 0 and doc["status"] == "complete", (rc, doc["problems"])
assert doc["answers"]["a_obryvy"].startswith("нет — хуже"), doc["answers"]
assert doc["answers"]["b_agentnost"].startswith("нет — хуже"), doc["answers"]
assert doc["verdict"]["net_effect"]["worse_than_cpt"] == ["obryvy", "agentnost", "pokrytie"]
assert "остановить" in doc["recommendation"]["recommendation"], doc["recommendation"]
# база идёт первой строкой таблицы, точки — по возрастанию шага
assert [s["state"] for s in doc["states"]] == ["cfinal", "sft_v13_0500", "sft_v13_5000"], doc["states"]
d1 = doc["states"][1]["deltas_vs_cpt"]["unclosed_think"]
assert d1 == {"delta": 0.2135, "direction": "хуже", "significant": True}, d1
# тренд «хуже со временем» виден направлением, а не только знаком дельты
assert doc["trends"]["unclosed_think"]["direction"] == "растёт", doc["trends"]

# (б) метрики значимо лучше CPT по всем трём → стадия продолжается
better = report({"cfinal": st("/m/cpt.pt", BEST),
                 "sft_v13_0500": st("/m/sft_probe_500.pt", agg(0.20, 0.85, 0.92))})
(tmp / "rep.json").write_text(json.dumps(better), "utf-8")
rc, doc = m.build()
assert rc == 0 and doc["answers"]["a_obryvy"].startswith("да"), doc["answers"]
assert "продолжать: формат не хуже CPT" == doc["recommendation"]["recommendation"]

# (в) различия в пределах шума (|Δ| < 0.10 ≈ 2σ при n=104) — ни «лучше», ни «хуже»
noise = report({"cfinal": st("/m/cpt.pt", BEST),
                "sft_v13_0500": st("/m/sft_probe_500.pt", agg(0.38, 0.70, 0.80))})
(tmp / "rep.json").write_text(json.dumps(noise), "utf-8")
rc, doc = m.build()
assert rc == 0 and doc["answers"]["a_obryvy"].startswith("нет — в пределах"), doc["answers"]
assert doc["recommendation"]["recommendation"].startswith("продолжать под наблюдением")
PY

# 26.3 Красные пути свода: не тот режим декодирования, мало проб, нарушенное
#      тождество прибора. Каждый обязан быть отказом с названной причиной — свод,
#      который выдаёт вердикт по несопоставимым числам, хуже отсутствия свода.
#      Проверяется возвратом `build()` (assert-блок), а не кодом CLI: CLI-путь
#      отказа проверяется в 26.4 — здесь важен сам гейт и текст причины.
expect_exit 0 "S3ap: не штатный режим (legacy) — build отказывает (rc=1)" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("s3ap", pathlib.Path("tools/assemble_s3ap_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
base = json.loads((tmp / "base.json").read_text(encoding="utf-8"))
bad = json.loads(json.dumps(base))
bad["protocol"]["decoding_protocol"]["standard"] = False
bad["protocol"]["decoding_protocol"]["legacy_decoding"] = True
(tmp / "rep_bad.json").write_text(json.dumps(bad), encoding="utf-8")
m.REPORT = str(tmp / "rep_bad.json"); m.BASELINE_REPORT = str(tmp / "base.json")
m.IDENTITY_NEW = str(tmp / "id_new.json"); m.IDENTITY_REF = str(tmp / "id_ref.json")
rc, doc = m.build()
assert rc == 1, rc
assert any("штатном режиме" in p for p in doc["problems"]), doc["problems"]
assert any("legacy" in p for p in doc["problems"]), doc["problems"]
PY

expect_exit 0 "S3ap: n < 100 на состояние — build отказывает (rc=1)" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("s3ap", pathlib.Path("tools/assemble_s3ap_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
base = json.loads((tmp / "base.json").read_text(encoding="utf-8"))
small = json.loads(json.dumps(base))
small["states"]["cfinal"]["aggregate"]["n"] = 24
small["protocol"]["n_prompts"] = 24
(tmp / "rep_small.json").write_text(json.dumps(small), encoding="utf-8")
m.REPORT = str(tmp / "rep_small.json"); m.BASELINE_REPORT = str(tmp / "base.json")
m.IDENTITY_NEW = str(tmp / "id_new.json"); m.IDENTITY_REF = str(tmp / "id_ref.json")
rc, doc = m.build()
assert rc == 1 and any("< 100" in p for p in doc["problems"]), doc["problems"]
PY

expect_exit 0 "S3ap: тождество прибора нарушено (ядро разошлось) — build отказывает" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("s3ap", pathlib.Path("tools/assemble_s3ap_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
(tmp / "id_split.json").write_text(json.dumps({"states": {"cfinal": {"probes": [
    {"response": "OTHER"}]}}}), encoding="utf-8")
m.REPORT = str(tmp / "base.json"); m.BASELINE_REPORT = str(tmp / "base.json")
m.IDENTITY_NEW = str(tmp / "id_split.json"); m.IDENTITY_REF = str(tmp / "id_ref.json")
rc, doc = m.build()
assert rc == 1 and any("тождество прибора нарушено" in p for p in doc["problems"]), doc["problems"]
PY

# 26.4 CLI-путь: отказ по входу (exit 1) и NOT-VERIFIED (exit 2) — «нет данных»
#      обязано быть отличимо от «данные плохие». Отказ по входу берёт настоящие
#      эталон и гейт тождества из репозитория (свод обязан отказывать и на них),
#      а испорченный отчёт подставляется фикстурой.
expect_exit 1 "S3ap: CLI — отказ по несопоставимому входу (exit 1), свод всё равно записан" \
  python3 tools/assemble_s3ap_evidence.py --out "$TMP/s3ap_bad.json" \
    --report "$TMP/rep_bad.json"
expect_exit 2 "S3ap: CLI — нет отчёта прогона, NOT-VERIFIED (exit 2)" \
  python3 tools/assemble_s3ap_evidence.py --out "$TMP/s3ap_none.json" \
    --report "$TMP/no-such-report.json"
expect_exit 0 "S3ap: отказ по входу назван в problems, а не проглочен" \
  python3 - "$TMP" <<'PY'
import json, pathlib, sys
doc = json.loads((pathlib.Path(sys.argv[1]) / "s3ap_bad.json").read_text(encoding="utf-8"))
assert doc["status"] == "partial", doc["status"]
assert any("штатном режиме" in p for p in doc["problems"]), doc["problems"]
PY

echo "== 27. S3aq: различающий замер бюджета (усечение против деградации) =="

# 27.1 Правило вердикта — код, а не текст: зоны ADR-045 п.2 проверяются на границах
#      (0.35 включительно — уровень CPT; ровно 0.5 — ещё промежуточная зона; 0.5001
#      — уже деградация). Тест ломается, если границу сдвинут после того, как числа
#      станут известны, — ради этого правило и зашито константами.
expect_exit 0 "S3aq: зоны вердикта — ровно по правилу ADR-045 п.2, с границами" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("s3aq", pathlib.Path("tools/assemble_s3aq_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
assert (m.CPT_LEVEL_MAX, m.DEGRADATION_MIN) == (0.35, 0.50), (m.CPT_LEVEL_MAX, m.DEGRADATION_MIN)
assert m.rule_zone(0.10)[0] == "truncation_confirmed"
assert m.rule_zone(0.35)[0] == "truncation_confirmed"      # граница включительно
assert m.rule_zone(0.3501)[0] == "insufficient_data"
assert m.rule_zone(0.50)[0] == "insufficient_data"         # «строго больше 0.5»
assert m.rule_zone(0.5001)[0] == "degradation"
assert m.rule_zone(None)[0] == "not_measured"
# механизм называется словом, а не «зоной»: по нему принимается решение о стадии
assert m.rule_zone(0.2)[1] == "усечение выросших рассуждений"
assert "модель/обучение" in m.rule_zone(0.9)[1]
PY

# 27.2 Три исхода вердикта на фикстуре + таблица «состояние × бюджет», правило п.5
#      и рост длины. Проверяется, что вердикт следует за числом, а не за ожиданием
#      автора, и что рекомендация остановить стадию появляется ровно в зоне
#      «деградация» (в остальных — нет).
expect_exit 0 "S3aq: вердикт по трём зонам, таблица бюджетов, правило п.5, рост длины" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("s3aq", pathlib.Path("tools/assemble_s3aq_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
m.IDENTITY_NEW = str(tmp / "aq_id_new.json"); m.IDENTITY_REF = str(tmp / "aq_id_ref.json")
m.PIN_CHECK = str(tmp / "aq_pin.json")
m.REPORT_8192 = str(tmp / "aq_rep8192.json")
m.REPORT_4096_PAIR = str(tmp / "aq_rep4096.json")
m.BASELINE_4096 = str(tmp / "aq_base.json")

TOOL_SHA = m.sha256_file(pathlib.Path("tools/probe_language_split.py"))
def proto(budget):
    return {"prompts_set": "wide", "prompts_digest": "d" * 64, "n_prompts": 104,
            "max_new_tokens": budget, "stop_at_turn_end": True, "batch_size": 8,
            "decoding_protocol": {"standard": True, "legacy_decoding": False,
                                  "allowed_for_conclusions": True,
                                  "default_mode": "greedy_nogram4"}}
def agg(uncl, cov, tool=0.6, median=1200, natural_uncl=None, n_natural=80, trunc=None):
    return {"n": 104, "unclosed_think_share": uncl, "mode_share_tool_call": tool,
            "cyr_answer": {"with_zeros": cov * 0.5, "coverage": cov},
            "cyr_think": {"with_zeros": 0.3},
            "truncated_share": 0.3 if trunc is None else trunc,
            "looped_share": 0.01, "stray_think_close_share": 0.05,
            "mode_share_think": 0.9, "think_blocks_mean": 1.5,
            "lengths": {"median": median, "p90": median * 2, "mean": median,
                        "median_over_budget": round(median / 8192, 4)},
            "stop": {"natural_stop_share": round(n_natural / 104, 4),
                     "natural": {"n": n_natural,
                                 "unclosed_think_share": (uncl if natural_uncl is None
                                                          else natural_uncl),
                                 "answer_coverage": cov},
                     "truncated": {"n": 104 - n_natural, "unclosed_think_share": 1.0,
                                   "answer_coverage": 0.0},
                     "reasons_share": {"turn_end": round(n_natural / 104, 4)}}}
def st(ckpt, a, budget, toks):
    return {"checkpoint": ckpt, "checkpoint_sha256": "0" * 64, "checkpoint_bytes": 1,
            "aggregate": a,
            "probes": [{"n_new_tokens": t, "response": "x", "hit_limit": t >= budget}
                       for t in toks]}
def report(budget, states):
    return {"tool_sha256": TOOL_SHA, "device": "cuda", "protocol": proto(budget),
            "states": states}
def toks(median, n=104):
    return [median] * n

CPT = agg(0.3365, 0.8269, tool=0.7308, median=1164, natural_uncl=0.3333, n_natural=99)
(tmp / "aq_base.json").write_text(json.dumps(report(4096, {
    "cfinal": st("/m/cpt.pt", CPT, 4096, toks(1164)),
    "sft_v13_21500": st("/m/sft_probe_21500.pt",
                        agg(0.78, 0.30, median=4096, natural_uncl=0.70, n_natural=45),
                        4096, toks(4096))})), "utf-8")
(tmp / "aq_id_ref.json").write_text(json.dumps({"states": {"cfinal": {"probes": [
    {"response": "same"}]}}}), "utf-8")
(tmp / "aq_id_new.json").write_text(json.dumps({"states": {"cfinal": {"probes": [
    {"response": "same"}]}}}), "utf-8")
(tmp / "aq_pin.json").write_text(json.dumps({"all_identical": True}), "utf-8")

def paired(cov=0.30, uncl=0.78, median=4096):
    return report(4096, {"sft_v13_21500": st("/m/sft_probe_21500.pt",
                                             agg(uncl, cov, median=median,
                                                 natural_uncl=0.70, n_natural=45),
                                             4096, toks(median))})

def solve(uncl, cov, median, natural_uncl=None, n_natural=90, skipped=False):
    """Прогон одного сценария: решающий 8192 + парный 4096 + база S3ap."""
    states = {"cfinal": st("/m/cpt.pt", CPT, 8192, toks(1164, 104)),
              "sft_v13_21500": st("/m/sft_probe_21500.pt",
                                  agg(uncl, cov, median=median, natural_uncl=natural_uncl,
                                      n_natural=n_natural), 8192, toks(median))}
    if skipped:
        states["sft_v13_9000"] = {"skipped": "чекпойнт не найден: /m/sft_probe_9000.pt"}
    (tmp / "aq_rep8192.json").write_text(json.dumps(report(8192, states)), "utf-8")
    (tmp / "aq_rep4096.json").write_text(json.dumps(paired()), "utf-8")
    return m.build()

# (а) обрывы 0.20 ≤ 0.35 → механизм «усечение», стадию не останавливать, покрытие
#     вернулось к CPT (0.80 против 0.8269 — в пределах порога 0.10)
rc, doc = solve(0.20, 0.80, 6000, skipped=True)
assert rc == 0 and doc["status"] == "complete", (rc, doc["problems"])
v = doc["verdict"]
assert v["rule_zone"] == "truncation_confirmed", v
assert v["recommend_stop_stage"] is False and "не считать проваленной" in v["recommendation"]
assert v["coverage_restored_to_cpt"] is True, v
# таблица «состояние × бюджет»: обе оси на месте, утраченное состояние — строкой
budgets = {(r["state"], r["budget"]) for r in doc["table"]}
assert ("cfinal", 8192) in budgets and ("sft_v13_21500", 8192) in budgets, budgets
assert ("sft_v13_21500", 4096) in budgets, budgets          # парный контроль
assert ("cfinal", 4096) in budgets, budgets                 # база S3ap
assert ("sft_v13_9000", 8192) in budgets, budgets           # пропущенное — тоже строка
# правило п.5: обрывы среди естественно завершённых идут рядом с полным чтением
ra = doc["unclosed_rule_applied"]
assert ra["in_instrument"] is True and ra["max_abs_shift"] is not None, ra
assert any(p["budget"] == 8192 and p["state"] == "sft_v13_21500" for p in ra["per_state"])
# рост длины: медиана 4096 → 6000 при бюджете 8192
lg = doc["length_growth"]
row = next(r for r in lg["per_state"] if r["state"] == "sft_v13_21500")
assert row["median_4096"] == 4096 and row["median_8192"] == 6000, row
assert row["median_ratio"] == 1.465, row
assert lg["saturated_states"] == [], lg["saturated_states"]
# парное сравнение — «то же состояние, два бюджета»
assert doc["paired_comparison"]["available"] is True
assert doc["paired_comparison"]["per_metric"]["unclosed_think"]["delta"] == -0.58

# (б) обрывы 0.65 > 0.5 → причина в модели/обучении, рекомендация остановить стадию,
#     покрытие к CPT не вернулось
rc, doc = solve(0.65, 0.35, 8192, natural_uncl=0.60, n_natural=40)
assert rc == 0, doc["problems"]
v = doc["verdict"]
assert v["rule_zone"] == "degradation", v
assert v["recommend_stop_stage"] is True and "остановить" in v["recommendation"]
assert v["coverage_restored_to_cpt"] is False, v
assert doc["length_growth"]["saturated_states"] == ["sft_v13_21500"], doc["length_growth"]

# (в) промежуточная зона 0.35 < 0.42 ≤ 0.5 → «недостаточно данных», с названным
#     домером (бюджет 12288), и без решения об остановке
rc, doc = solve(0.42, 0.55, 5000)
assert rc == 0, doc["problems"]
v = doc["verdict"]
assert v["rule_zone"] == "insufficient_data", v
assert v["recommend_stop_stage"] is False and "12288" in v["recommendation"]
PY

# 27.3 Красные пути: числа, снятые не тем прибором, не тем бюджетом или на меньшей
#      выборке, вердикта не дают. Отказ обязан быть назван в problems — свод, который
#      выдаёт вердикт по несопоставимым числам, хуже отсутствия свода.
expect_exit 0 "S3aq: чужой бюджет, чужой набор промптов, n<104 — отказ с причиной" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("s3aq", pathlib.Path("tools/assemble_s3aq_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
m.IDENTITY_NEW = str(tmp / "aq_id_new.json"); m.IDENTITY_REF = str(tmp / "aq_id_ref.json")
m.PIN_CHECK = str(tmp / "aq_pin.json")
m.REPORT_8192 = str(tmp / "aq_rep8192.json"); m.REPORT_4096_PAIR = str(tmp / "aq_rep4096.json")
m.BASELINE_4096 = str(tmp / "aq_base.json")

def mutate(**kw):
    rep = json.loads((tmp / "aq_rep8192.json").read_text(encoding="utf-8"))
    for k, v in kw.items():
        if k == "budget":
            rep["protocol"]["max_new_tokens"] = v
        elif k == "digest":
            rep["protocol"]["prompts_digest"] = v
        elif k == "legacy":
            rep["protocol"]["decoding_protocol"].update(v)
        elif k == "batch":
            rep["protocol"]["batch_size"] = v
        elif k == "n":
            for s in rep["states"].values():
                if "aggregate" in s:
                    s["aggregate"]["n"] = v
        elif k == "drop_pair_state":
            rep["states"].pop(v, None)
    return rep

def run(rep):
    (tmp / "aq_mut.json").write_text(json.dumps(rep), encoding="utf-8")
    return m.build(str(tmp / "aq_mut.json"), str(tmp / "aq_rep4096.json"))

def run_pair(rep41):
    (tmp / "aq_pair_mut.json").write_text(json.dumps(rep41), encoding="utf-8")
    return m.build(str(tmp / "aq_rep8192.json"), str(tmp / "aq_pair_mut.json"))

rc, doc = run(mutate(budget=4096))
assert rc == 1 and any("бюджет 4096 вместо 8192" in p for p in doc["problems"]), doc["problems"]
rc, doc = run(mutate(digest="f" * 64))
assert rc == 1 and any("prompts_digest расходится" in p for p in doc["problems"]), doc["problems"]
rc, doc = run(mutate(legacy={"legacy_decoding": True, "standard": False}))
assert rc == 1 and any("штатном режиме" in p for p in doc["problems"]), doc["problems"]
rc, doc = run(mutate(batch=1))
assert rc == 1 and any("batch_size расходится" in p for p in doc["problems"]), doc["problems"]
rc, doc = run(mutate(n=24))
assert rc == 1 and any("n=24 < 104" in p for p in doc["problems"]), doc["problems"]
# нет точки v13 в решающем прогоне — вердикт строить не на чем
rc, doc = run(mutate(drop_pair_state="sft_v13_21500"))
assert rc == 1 and any("нет ни одной точки v13" in p for p in doc["problems"]), doc["problems"]
# точка v13 есть в 8192, но парного замера 4096 для неё нет — различение не построить
pair = json.loads((tmp / "aq_rep4096.json").read_text(encoding="utf-8"))
pair["states"].pop("sft_v13_21500")
rc, doc = run_pair(pair)
assert rc == 1 and any("нет парного замера 4096" in p for p in doc["problems"]), doc["problems"]
PY

expect_exit 0 "S3aq: тождество прибора нарушено (ядро разошлось) — отказ" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("s3aq", pathlib.Path("tools/assemble_s3aq_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
m.IDENTITY_NEW = str(tmp / "aq_id_split.json"); m.IDENTITY_REF = str(tmp / "aq_id_ref.json")
m.PIN_CHECK = str(tmp / "aq_pin.json")
m.REPORT_8192 = str(tmp / "aq_rep8192.json"); m.REPORT_4096_PAIR = str(tmp / "aq_rep4096.json")
m.BASELINE_4096 = str(tmp / "aq_base.json")
(tmp / "aq_id_split.json").write_text(json.dumps({"states": {"cfinal": {"probes": [
    {"response": "OTHER"}]}}}), encoding="utf-8")
rc, doc = m.build()
assert rc == 1 and any("тождество прибора нарушено" in p for p in doc["problems"]), doc["problems"]
PY

# 27.4 CLI-путь: отказ по входу (exit 1) и NOT-VERIFIED (exit 2) — «нет данных»
#      обязано быть отличимо от «данные плохие».
expect_exit 2 "S3aq: CLI — нет отчёта прогона, NOT-VERIFIED (exit 2)" \
  python3 tools/assemble_s3aq_evidence.py --out "$TMP/s3aq_none.json" \
    --report-8192 "$TMP/no-such-report.json" --report-4096 "$TMP/aq_rep4096.json"
expect_exit 1 "S3aq: CLI — отказ по несопоставимому входу (exit 1), свод всё равно записан" \
  python3 tools/assemble_s3aq_evidence.py --out "$TMP/s3aq_bad.json" \
    --report-8192 "$TMP/aq_mut.json" --report-4096 "$TMP/aq_rep4096.json"
expect_exit 0 "S3aq: отказ по входу назван в problems, а не проглочен" \
  python3 - "$TMP" <<'PY'
import json, pathlib, sys
doc = json.loads((pathlib.Path(sys.argv[1]) / "s3aq_bad.json").read_text(encoding="utf-8"))
assert doc["status"] == "partial", doc["status"]
assert doc["problems"], doc
PY

# 27.5 Правило ADR-045 п.5 уже реализовано прибором: правка tools/probe_language_split.py
#      не нужна и вредна — она сломала бы тождество прибора с базой S3ap (хеш файла
#      сверяется сводом). Тест проверяет семантику правила на синтетических ходах:
#      незакрытый блок среди усечённых в показатель «среди завершённых» не попадает.
expect_exit 0 "S3aq: правило «только завершённые ходы» реализовано прибором (ADR-045 п.5)" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("ls", pathlib.Path("tools/probe_language_split.py"))
ls = u.module_from_spec(spec); spec.loader.exec_module(ls)
def rec(stop, unclosed):
    return {"stop_reason": stop, "hit_limit": stop != "turn_end",
            "metrics": {"unclosed_think": unclosed, "has_tool_call": False,
                        "looped": False, "cyr_answer": 0.5, "cyr_think": 0.5}}
sb = ls.stop_breakdown([rec("turn_end", False), rec("turn_end", True),
                        rec("limit_in_think", True), rec("limit_in_think", True)])
assert sb["n"] == 4 and sb["natural"]["n"] == 2 and sb["truncated"]["n"] == 2, sb
assert sb["natural"]["unclosed_think_share"] == 0.5, sb["natural"]
assert sb["truncated"]["unclosed_think_share"] == 1.0, sb["truncated"]
# полное чтение смешивает оба случая — ровно то, что правило п.5 разводит
assert sb["reasons_share"] == {"limit_in_think": 0.5, "turn_end": 0.5}, sb["reasons_share"]
PY

# 27.6 Свод, лежащий в дереве, обязан подчиняться объявленному правилу: зона в
#      артефакте пересчитывается из его же числа обрывов. Тест ломается, если
#      evidence поправят руками или правило сдвинут после замера.
expect_exit 0 "S3aq: вердикт в evidence/s3aq-budget-8192.json — по объявленному правилу" \
  python3 - <<'PY'
import importlib.util as u, json, pathlib
p = pathlib.Path("evidence/s3aq-budget-8192.json")
assert p.is_file(), "нет свода S3aq"
doc = json.loads(p.read_text(encoding="utf-8"))
spec = u.spec_from_file_location("s3aq", pathlib.Path("tools/assemble_s3aq_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
v = doc["verdict"]
assert v["rule_zone"] == m.rule_zone(v["unclosed_8192"])[0], (v["rule_zone"], v["unclosed_8192"])
assert v["rule"]["cpt_level_max"] == m.CPT_LEVEL_MAX
assert v["rule"]["degradation_min"] == m.DEGRADATION_MIN
assert v["recommend_stop_stage"] is (v["rule_zone"] == "degradation")
# вердикт обязан опираться на свежую точку, а не на удобную: состояние вердикта
# совпадает с самым старшим шагом решающего прогона
steps = [r["state"] for r in doc["budget_8192"]["states"] if r["state"] != "cfinal"]
assert doc["paired_comparison"]["state"] in steps, (doc["paired_comparison"], steps)
for key in ("status", "artifacts", "budget_4096", "budget_8192", "verdict",
            "length_growth", "unclosed_rule_applied", "open_questions"):
    assert key in doc, key
PY

# 27.7 Контракт входа: отсутствие ЛЮБОГО объявленного входа → NOT-VERIFIED (rc 2).
#      «Входа нет» и «вход плохой» — разные факты, и различать их обязан свод: свод,
#      собранный на половине входов, неотличим от полного. Тест перебирает все входы
#      по одному, а не проверяет один: ослабить контракт, добавив новый вход без
#      проверки, после этого нельзя.
expect_exit 0 "S3aq: нет любого объявленного входа → NOT-VERIFIED, отказ поимённый" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("s3aq", pathlib.Path("tools/assemble_s3aq_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
m.REPORT_8192 = str(tmp / "aq_rep8192.json"); m.REPORT_4096_PAIR = str(tmp / "aq_rep4096.json")
m.BASELINE_4096 = str(tmp / "aq_base.json"); m.IDENTITY_NEW = str(tmp / "aq_id_new.json")
m.IDENTITY_REF = str(tmp / "aq_id_ref.json"); m.PIN_CHECK = str(tmp / "aq_pin.json")

rc, doc = m.build()
assert rc == 0, (rc, doc.get("problems"))          # все входы на месте — свод собирается
assert sorted(m.declared_inputs()) == sorted(m.INPUTS), m.declared_inputs()

for name, spec_in in m.declared_inputs().items():
    p = pathlib.Path(spec_in["path"])
    backup = p.read_bytes()
    p.unlink()
    rc, doc = m.build()
    assert rc == 2, (name, rc, doc.get("problems"))
    assert not doc.get("problems"), f"{name}: отсутствие входа выдано за плохой вход"
    assert any(g["input"] == name for g in doc["missing_inputs"]), (name, doc["missing_inputs"])
    assert spec_in["path"] in doc["error"], doc["error"]
    p.write_bytes(backup)

# вход есть, но не разобран (битый JSON) — тоже «нечего сводить», и причина названа
rep_ok = (tmp / "aq_rep8192.json").read_bytes()
(tmp / "aq_rep8192.json").write_text("{не json", encoding="utf-8")
rc, doc = m.build()
assert rc == 2 and "не разобран" in doc["error"], (rc, doc)
assert doc["unreadable_inputs"] == ["report_8192"], doc["unreadable_inputs"]
(tmp / "aq_rep8192.json").write_bytes(rep_ok)      # фикстура возвращается целой: её читают тесты ниже
PY

# 27.8 ADR-045 п.5 в вердикте: рядом с вердиктным числом (полное чтение) стоит число
#      по «только завершённым ходам», и когда зоны расходятся — это НАЗВАНО, а не
#      выбрано задним числом. Проверяются оба числа против прибора и против правила.
expect_exit 0 "S3aq: вердикт несёт оба чтения (полное и по п.5) и называет расхождение зон" \
  python3 - <<'PY'
import importlib.util as u, json, pathlib
spec = u.spec_from_file_location("s3aq", pathlib.Path("tools/assemble_s3aq_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
d = json.loads(pathlib.Path("evidence/s3aq-budget-8192.json").read_text(encoding="utf-8"))
v = d["verdict"]
u_all, u_nat = v["unclosed_8192"], v["unclosed_8192_natural"]
assert u_all is not None and u_nat is not None, v
assert v["rule_zone"] == m.rule_zone(u_all)[0], v          # вердикт — по полному чтению
assert v["rule_zone_natural"] == m.rule_zone(u_nat)[0], v   # п.5 — рядом, тем же правилом
assert v["rule_zones_agree"] == (v["rule_zone"] == v["rule_zone_natural"]), v
assert v["recommend_stop_stage"] is (v["rule_zone"] == "degradation"), v
assert v["n_natural_8192"] is not None and v["n_natural_8192"] < 104, v
# оба числа — те же, что в разборе правила п.5 (один источник, а не два пересказа)
row = [p for p in d["unclosed_rule_applied"]["per_state"]
       if p["budget"] == 8192 and p["state"] == "sft_v13_21500"]
assert len(row) == 1, row
assert row[0]["unclosed_all"] == u_all and row[0]["unclosed_natural"] == u_nat, row[0]
# расхождение зон обязано быть названо словами и с обоими числами
if not v["rule_zones_agree"]:
    assert str(u_nat) in v["rule_zones_note"] and str(u_all) in v["rule_zones_note"], v
    assert v["recommendation_caveat"], v
# разрешение оценки (границы при этом не сдвинуты)
er = v["estimate_resolution"]
assert er["full_read"]["bound"] == m.DEGRADATION_MIN, er
assert er["full_read"]["two_sigma"] > 0, er
print(f"обрывы: полное {u_all} → {v['rule_zone']}; по п.5 {u_nat} → {v['rule_zone_natural']}")
PY

# 27.9 ADR-045 п.2/п.4: различение «усечение против деградации» — сравнение бюджетов
#      на ОДНОМ состоянии. В своде обязаны стоять обе оси (4096 и 8192) с медианой,
#      p90 и долей упёршихся в лимит, а парное сравнение — отвечать, снял ли бюджет
#      обрывы, а не просто перечислять числа.
expect_exit 0 "S3aq: бюджеты на одном состоянии — медиана/p90/доля в лимите и вывод парного сравнения" \
  python3 - <<'PY'
import json, pathlib
d = json.loads(pathlib.Path("evidence/s3aq-budget-8192.json").read_text(encoding="utf-8"))
lg = {r["state"]: r for r in d["length_growth"]["per_state"]}
assert set(lg) == {"cfinal", "sft_v13_21500"}, lg
for state, r in lg.items():
    for key in ("median_4096", "median_8192", "p90_4096", "p90_8192",
                "share_at_budget_4096", "share_at_budget_8192", "median_over_budget"):
        assert r.get(key) is not None, (state, key, r)
    assert 0.0 <= r["share_at_budget_8192"] <= 1.0, r
    assert r["budget_note"], r
# контроль CPT: медиана не зависит от бюджета (бюджет не ограничитель)
assert lg["cfinal"]["median_4096"] == lg["cfinal"]["median_8192"], lg["cfinal"]
assert lg["cfinal"]["share_at_budget_8192"] < 0.25, lg["cfinal"]
# решающее состояние: сравнение бюджетов на нём, а не на соседних шагах
pc = d["paired_comparison"]
assert pc["available"] is True and pc["state"] == "sft_v13_21500", pc
assert pc["unclosed_delta"] is not None and pc["budget_explains_unclosed"] in (True, False), pc
assert pc["per_metric"]["unclosed_think"]["budget_4096"] is not None, pc
assert pc["per_metric"]["unclosed_think"]["budget_8192"] is not None, pc
assert "бюджет обрывы НЕ объясняет" in pc["reading"] or "различие бюджетов объясняет" in pc["reading"], pc["reading"]
# сводная таблица: обе оси по обоим состояниям
axes = {(r["state"], r["budget"]) for r in d["table"]}
assert ("sft_v13_21500", 4096) in axes and ("sft_v13_21500", 8192) in axes, axes
assert ("cfinal", 4096) in axes and ("cfinal", 8192) in axes, axes
# артефакты записаны фактом: отсутствующее объявлено отсутствующим, а не умолчано
assert "probe_record" in d["artifacts"]["missing"], d["artifacts"]["missing"]
assert d["artifacts"]["probe_record"]["why_absent"], d["artifacts"]["probe_record"]
# манифест AD-2 описывает последний этап каталога, а не решающий: это названо фактом
rm = d["artifacts"]["run_manifest"]
assert rm["exists"] and rm.get("covers") and rm.get("note"), rm
assert rm["covers"].endswith("format_wide_4096_pair.json"), rm
print("бюджеты: " + "; ".join(
    f"{s}: медиана {r['median_4096']}→{r['median_8192']}, в лимит {r['share_at_budget_8192']}"
    for s, r in lg.items()))
PY

# 27.11 Парный контроль на уровне ПРОБ, а не агрегатов: что стало с ходами, которым
#       дополнительный бюджет снял усечение. Агрегат «доля усечённых упала» не говорит,
#       закрылся ли у этих ходов блок; ответ лежит в пробах и сопоставляется по индексу.
#       Там же — детерминизм: дописанные при меньшем бюджете ходы обязаны совпасть
#       побайтово, значит совпадение агрегатов само по себе свидетельством не является.
expect_exit 0 "S3aq: судьба ходов, снятых бюджетом, — по пробам; совпадение агрегатов объяснено детерминизмом" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("s3aq", pathlib.Path("tools/assemble_s3aq_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

# фикстура: 3 хода упираются в 4096, из них 2 остаются незакрытыми и с запасом бюджета
def probe(tok, hit, unclosed, resp):
    return {"n_new_tokens": tok, "hit_limit": hit, "response": resp, "prompt": "p",
            "metrics": {"unclosed_think": unclosed}}
rep_b = {"states": {"s": {"probes": [probe(4096, True, True, "b0"), probe(100, False, False, "b1"),
                                   probe(4096, True, True, "b2"), probe(50, False, True, "b3")]}}}
rep_a = {"states": {"s": {"probes": [probe(5000, False, True, "a0"), probe(100, False, False, "b1"),
                                   probe(5100, False, True, "a2"), probe(50, False, True, "b3")]}}}
pf = m.probe_fate(rep_a, rep_b, "s")
assert pf["available"] and pf["prompts_aligned_by_index"], pf
assert pf["truncated_4096"] == 2 and pf["truncated_8192"] == 0, pf
assert pf["recovered_by_budget"]["n"] == 2, pf
assert pf["recovered_by_budget"]["still_unclosed_indexes"] == [0, 2], pf
assert pf["recovered_by_budget"]["closed_indexes"] == [], pf
assert pf["determinism"] == {"finished_at_4096": 2, "byte_identical_at_8192": 2,
                             "all_identical": True}, pf
# пробы не той длины — судьба не восстанавливается, и это названо, а не выдумано
short = {"states": {"s": {"probes": [probe(1, False, False, "x")]}}}
bad = m.probe_fate(rep_a, short, "s")
assert bad["available"] is False and bad["why"], bad

# в своде — те же величины
d = json.loads(pathlib.Path("evidence/s3aq-budget-8192.json").read_text(encoding="utf-8"))
pf = d["paired_comparison"]["probe_fate"]
assert pf["available"] and pf["prompts_aligned_by_index"], pf
assert pf["recovered_by_budget"]["n"] == pf["truncated_4096"] - pf["truncated_8192"], pf
st, cl = set(pf["recovered_by_budget"]["still_unclosed_indexes"]), set(pf["recovered_by_budget"]["closed_indexes"])
assert st | cl == set(pf["recovered_by_budget"]["indexes"]) and not (st & cl), pf
assert pf["determinism"]["all_identical"] is True, pf["determinism"]
assert pf["reading"] and "не от бюджета" in pf["reading"], pf
# 0.5 — точка смеси: знак отклонения у полной доли и у доли по завершённым разный
note = d["verdict"]["estimate_resolution"]["subsample_note"]
assert "выше" in note and "ниже" in note and "смеси" in note, note
print(f"судьба проб: снято бюджетом {pf['recovered_by_budget']['n']}, "
      f"из них незакрытых {len(st)}; детерминизм {pf['determinism']['byte_identical_at_8192']}"
      f"/{pf['determinism']['finished_at_4096']}")
PY

# 27.10 Цена замера и добора — из факта (отметки журнала + токены отчётов), а не из
#       оценки; журнала нет — числа не выдумываются. Токены каждого этапа берутся из
#       ЕГО отчёта: подстановка одного в оба выдала бы длину 8192-прогона за 4096.
expect_exit 0 "S3aq: цена добора считается из журнала и отчётов; без журнала — не выдумывается" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
spec = u.spec_from_file_location("s3aq", pathlib.Path("tools/assemble_s3aq_evidence.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
rep_a = json.loads((tmp / "aq_rep8192.json").read_text(encoding="utf-8"))
rep_b = json.loads((tmp / "aq_rep4096.json").read_text(encoding="utf-8"))

m.RESUME_LOG = str(tmp / "aq_cost.log")
(tmp / "aq_cost.log").write_text("\n".join([
    "[2026-09-20 10:00:00] носитель сессии: pid=1 sid=1",
    "[2026-09-20 10:00:00] A: решающий — wide × 2 состояния на бюджете 8192",
    "[2026-09-20 12:00:00] A: код возврата 0",
    "[2026-09-20 13:00:00] B: код возврата 0",
    "[2026-09-20 13:00:00] добор завершён: rc8192=0 rc4096=0",
]), encoding="utf-8")
c = m.measurement_cost(rep_a, rep_b)
assert c["available"] is True and c["log_marks"] == 5, c
assert c["leg_a_8192"]["hours"] == 2.0 and c["leg_b_4096"]["hours"] == 1.0, c
assert c["leg_a_8192"]["probes"] == 208 and c["leg_b_4096"]["probes"] == 104, c
# токены — из своего отчёта каждому этапу
ta = sum(p["n_new_tokens"] for s in rep_a["states"].values() for p in s["probes"])
tb = sum(p["n_new_tokens"] for s in rep_b["states"].values() for p in s["probes"])
assert c["leg_a_8192"]["tokens"] == ta and c["leg_b_4096"]["tokens"] == tb, (c, ta, tb)
assert c["measured_total_h"] == 3.0, c
assert c["extrapolated_further_measurement"]["lower_h"] <= c["extrapolated_further_measurement"]["upper_h"], c
# журнала нет — «нет числа», а не ноль
m.RESUME_LOG = str(tmp / "нет-такого.log")
bad = m.measurement_cost(rep_a, rep_b)
assert bad["available"] is False and bad["why"], bad
print(f"цена: A {c['leg_a_8192']['hours']} ч, B {c['leg_b_4096']['hours']} ч, "
      f"добор {c['extrapolated_further_measurement']['lower_h']}–"
      f"{c['extrapolated_further_measurement']['upper_h']} ч (оценка)")
PY

echo
echo "== 28. S3aq: состояние прогона (что есть, чего нет, применимо ли правило) =="

# 28.1 Зелёный путь: решающий отчёт на месте → правило применимо (exit 0). Пробы в
#      журнале считаются отдельно от наличия отчёта: «отчёт есть» и «проб хватает» —
#      разные утверждения, и смешивать их значило бы прятать недобор выборки.
expect_exit 0 "S3aq: состояние прогона — решающий отчёт есть, правило применимо" \
  python3 - "$TMP" <<'PY'
import json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1]); run = tmp / "aq_state_ok"; run.mkdir(exist_ok=True)
(run / "format_wide_8192.json").write_text('{"states": {}}', encoding="utf-8")
(run / "format_wide_8192.log").write_text("\n".join(
    f"  [cfinal/greedy_nogram4] domain_tool: cyr_think=0.1 cyr_answer=0.2 "
    f"mode=True/True tok={100 + i} stop=turn_end | '<think>…'" for i in range(3)),
    encoding="utf-8")
rc = subprocess.run([sys.executable, "tools/report_s3aq_run_state.py",
                     "--run", str(run), "--out", str(tmp / "aq_state_ok.json")],
                    capture_output=True, text=True)
sys.exit(rc.returncode)
PY

# 28.2 Красный путь: отчёта нет → NOT-VERIFIED (exit 2), каталог цел и читается.
expect_exit 2 "S3aq: состояние прогона — отчёта нет, NOT-VERIFIED (exit 2)" \
  python3 - "$TMP" <<'PY'
import pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1]); run = tmp / "aq_state_none"; run.mkdir(exist_ok=True)
(run / "format_wide_8192.log").write_text("  [cfinal/greedy_nogram4] domain_tool: tok=10 "
                                          "stop=turn_end | '<think>…'\n", encoding="utf-8")
rc = subprocess.run([sys.executable, "tools/report_s3aq_run_state.py",
                     "--run", str(run), "--out", str(tmp / "aq_state_none.json")],
                    capture_output=True, text=True)
sys.exit(rc.returncode)
PY

# 28.3 Что именно записано: состояние, недостающие состояния, разбор журнала, правило
#      «не применено», названная причина и путь добора — по пиннутым копиям.
expect_exit 0 "S3aq: запись состояния — состояния, длины, правило, путь добора" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1]); sys.path.insert(0, "tools")
spec = u.spec_from_file_location("aq_state", pathlib.Path("tools/report_s3aq_run_state.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

run = tmp / "aq_state_struct"; run.mkdir(exist_ok=True)
(run / "pin_checkpoints.json").write_text(json.dumps({"checkpoints": {
    "cfinal": {"pinned": "/pin/cfinal.pt"},
    "sft_v13_21500": {"pinned": "/pin/sft_probe_21500.pt"}}}), encoding="utf-8")
(run / "format_wide_8192.log").write_text("\n".join(
    [f"  [cfinal/greedy_nogram4] t: tok={t} stop=turn_end | '<think>…'"
     for t in (100, 200, 9000)]
    + [f"  [sft_v13_21500/greedy_nogram4] t: tok={t} stop=turn_end | '<think>…'"
       for t in (4000, 5000)]), encoding="utf-8")
m.BASELINE_4096 = str(tmp / "нет-такого.json")      # базы нет — сверка префиксов не выдумывается

doc = m.build(run)
assert doc["status"] == "measurement_incomplete", doc["status"]
assert doc["decisive_input"]["exists"] is False
assert doc["artifacts"]["format_wide_8192.json"]["exists"] is False
assert doc["artifacts"]["format_wide_8192.log"]["exists"] is True
assert doc["expected_states"] == ["cfinal", "sft_v13_21500"], doc["expected_states"]
# состояние, попавшее в журнал, недостающим не называется; пропавшее — называется
assert doc["states_missing_from_log"] == [], doc["states_missing_from_log"]
(run / "format_wide_8192.log").write_text(
    "  [cfinal/greedy_nogram4] t: tok=100 stop=turn_end | '<think>…'\n", encoding="utf-8")
assert m.build(run)["states_missing_from_log"] == ["sft_v13_21500"], "пропажа состояния не названа"
(run / "format_wide_8192.log").write_text("\n".join(
    [f"  [cfinal/greedy_nogram4] t: tok={t} stop=turn_end | '<think>…'"
     for t in (100, 200, 9000)]
    + [f"  [sft_v13_21500/greedy_nogram4] t: tok={t} stop=turn_end | '<think>…'"
       for t in (4000, 5000)]), encoding="utf-8")
assert doc["probe_log"]["states_seen"]["cfinal"]["probes"] == 3, doc["probe_log"]["states_seen"]
assert doc["probe_log"]["states_seen"]["sft_v13_21500"]["probes"] == 2
assert doc["probe_log"]["states_seen"]["cfinal"]["complete"] is False
assert doc["probe_log"]["tokens"]["n"] == 5 and doc["probe_log"]["tokens"]["median"] == 4000, \
    doc["probe_log"]["tokens"]
assert doc["probe_log"]["tokens"]["at_or_over_budget"] == 1, doc["probe_log"]["tokens"]
assert doc["budget_prefix_check"] is None, "сверка префиксов без базы не выдумывается"
assert doc["rule"]["applied"] is False and doc["rule"]["applicable"] is False
assert doc["rule"]["rule_zone"] == "not_measured", doc["rule"]["rule_zone"]
assert doc["rule"]["cpt_level_max"] == 0.35 and doc["rule"]["degradation_min"] == 0.50
assert doc["recommend_stop_stage"] is False and doc["verdict"] is None
# правило ADR-045 п.5 не пересчитывается и не подменяется нулём: полного хода нет
assert doc["unclosed_rule_applied"]["value"] is None, doc["unclosed_rule_applied"]
assert "stop.natural" in doc["unclosed_rule_applied"]["implemented_in"]
assert "70 знаков" in doc["unclosed_rule_applied"]["why_not_computed"]
# добор — по пиннутым копиям, а не по «свежей точке»: иначе измерится другой шаг
assert "/pin/sft_probe_21500.pt" in doc["resume"]["command"], doc["resume"]
assert "setsid" in doc["resume"]["detach"]
for s in ("sft_v13_0500", "sft_v13_5000", "sft_v13_9000"):
    assert s in doc["unavailable_states"], s
print("состояние прогона: разобрано")
PY

# 28.4 Инвариант самого артефакта: запись в каталоге обязана следовать за наличием
#      решающего отчёта и за журналом. Тест переживает добор замера: он не требует
#      «прогон оборван», он требует «запись не разошлась с файлами».
#
#      ФАЗА (ADR-023 п.12): пока процесс замера жив, `states_seen` строго не
#      сверяется — журнал дописывается по построению, и требовать равенства значило
#      бы держать красный тест, который красен ровно потому, что замер идёт. Сверка
#      идёт как `active`: состояния из записи обязаны остаться, недостающие к моменту
#      снимка — ещё не появиться (монотонность), а не совпасть. Строгое равенство —
#      только когда процесс мёртв или отчёт записан.
expect_exit 0 "S3aq: run_state.json не разошёлся с каталогом прогона" \
  python3 - <<'PY'
import importlib.util as u, json, pathlib, sys
sys.path.insert(0, "tools")
RUN = pathlib.Path("runs/s3aq-budget-8192-20260920")
doc = json.loads((RUN / "run_state.json").read_text(encoding="utf-8"))
spec = u.spec_from_file_location("aq_state", pathlib.Path("tools/report_s3aq_run_state.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
fresh = m.build(RUN)
phase = fresh["run_phase"]
assert phase["phase"] in ("active", "finished", "unknown"), phase
assert doc["status"] == fresh["status"], (doc["status"], fresh["status"])
assert doc["status"] in ("rules_applicable", "measurement_incomplete"), doc["status"]
assert doc["decisive_input"]["exists"] == (RUN / "format_wide_8192.json").is_file()
seen_stored = doc["probe_log"]["states_seen"]
seen_fresh = fresh["probe_log"]["states_seen"]
if phase["strict_comparison_required"]:
    assert seen_stored == seen_fresh, "журнал разошёлся со сводом"
else:
    # фаза active/unknown: журнал растёт — проверяется монотонность, а не равенство
    for state, rec in seen_stored.items():
        assert state in seen_fresh, f"состояние {state} пропало из журнала"
        assert rec["probes"] <= seen_fresh[state]["probes"], \
            f"{state}: проб в журнале стало меньше ({rec['probes']} → {seen_fresh[state]['probes']})"
assert doc["rule"]["rule_zone"] == m.rule_module().rule_zone(None)[0]
assert doc["rule"]["applied"] is False, "инструмент состояния вердикта не выносит"
print(f"состояние прогона: {doc['status']}, фаза {phase['phase']} "
      f"(строгая сверка: {phase['strict_comparison_required']}); пробы: "
      f"{ {k: v['probes'] for k, v in seen_fresh.items()} }")
PY

# 28.5 Фаза берётся из факта процесса, а не из наличия файлов (ADR-023 п.12). Живой
#      процесс (берём свой: pid этого теста) без отчёта → `active` и НЕстрогая сверка;
#      тот же каталог с мёртвым pid → `finished` и строгая. Проверка ломается, если
#      фазу снова начнут выводить из отсутствия файла.
expect_exit 0 "S3aq: фаза замера — по факту процесса (active у живого, finished у мёртвого)" \
  python3 - "$TMP" <<'PY'
import importlib.util as u, json, os, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1]); sys.path.insert(0, "tools")
spec = u.spec_from_file_location("aq_state", pathlib.Path("tools/report_s3aq_run_state.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

run = tmp / "aq_phase"; run.mkdir(exist_ok=True)          # отчёта нет — иначе фаза finished
(run / "format_wide_8192.log").write_text(
    "  [cfinal/greedy_nogram4] t: tok=100 stop=turn_end | '<think>…'\n", encoding="utf-8")

# (а) живой процесс: pid этого же теста — замер «идёт»
(run / "resume_confirmed.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
d = m.build(run)
assert d["run_phase"]["phase"] == "active", d["run_phase"]
assert d["run_phase"]["strict_comparison_required"] is False, d["run_phase"]
assert d["run_phase"]["liveness"]["alive"] is True, d["run_phase"]["liveness"]
assert d["run_phase"]["adr"] == "ADR-023 п.12", d["run_phase"]

# (б) процесс мёртв: запускаем и убиваем — тот же каталог, но фаза другая
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
pid = proc.pid
proc.kill(); proc.wait()
(run / "resume_confirmed.json").write_text(json.dumps({"pid": pid}), encoding="utf-8")
(run / "resume_probe.pid").write_text(str(pid), encoding="utf-8")
d = m.build(run)
assert d["run_phase"]["phase"] == "finished", d["run_phase"]
assert d["run_phase"]["strict_comparison_required"] is True, d["run_phase"]

# (в) ни отчёта, ни процесса: фаза НЕ определена и названа таковой, а не «зелёная»
(run / "resume_confirmed.json").unlink(); (run / "resume_probe.pid").unlink()
d = m.build(run)
assert d["run_phase"]["phase"] == "unknown", d["run_phase"]
assert d["run_phase"]["liveness"]["known"] is False, d["run_phase"]["liveness"]
assert d["run_phase"]["strict_comparison_required"] is False, d["run_phase"]
# запасной источник pid: sidecar утрачен, pid-файл остался — фаза снова определяется
(run / "resume_probe.pid").write_text(str(os.getpid()), encoding="utf-8")
assert m.build(run)["run_phase"]["phase"] == "active", m.build(run)["run_phase"]
print("фазы: active по живому pid, finished по мёртвому, unknown без следа процесса")
PY

# 28.6 ADR-023 п.13: пересобираемый свод не хранит долговечные поля — они в sidecar
#      рядом. Проверка механическая с двух сторон: (а) в записи нет блока
#      долговечных полей, (б) ссылка на sidecar есть и sidecar действительно несёт
#      эти поля. Иначе «пересборка затрёт факт» остаётся обещанием.
expect_exit 0 "S3aq: долговечные поля — в sidecar, а не в пересобираемом своде (ADR-023 п.13)" \
  python3 - <<'PY'
import importlib.util as u, json, pathlib, sys
sys.path.insert(0, "tools")
RUN = pathlib.Path("runs/s3aq-budget-8192-20260920")
spec = u.spec_from_file_location("aq_state", pathlib.Path("tools/report_s3aq_run_state.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
doc = json.loads((RUN / "run_state.json").read_text(encoding="utf-8"))

for key in m.DURABLE_BLOCK_KEYS:
    assert key not in doc, f"долговечный блок {key} вернулся в пересобираемый свод"
side = doc["resume_sidecar"]
assert side["exists"] is True and side["path"].endswith(m.SIDECAR), side
assert set(m.DURABLE_FIELDS_FOR_STATE) <= set(side["durable_fields"]), side
# sidecar на месте и несёт те поля, ради которых он существует
sdoc = json.loads(pathlib.Path(side["path"]).read_text(encoding="utf-8"))
assert isinstance(sdoc.get("pid"), int) and sdoc.get("cmd_8192"), sdoc.keys()
# пересборка не теряет ссылку: build() даёт тот же sidecar и по-прежнему без блока
fresh = m.build(RUN)
assert fresh["resume_sidecar"]["path"] == side["path"], fresh["resume_sidecar"]
assert all(k not in fresh for k in m.DURABLE_BLOCK_KEYS), "build() вернул долговечный блок"
print(f"sidecar: {side['path']} (поля: {', '.join(side['durable_fields'])})")
PY

# 28.7 ADR-028 п.1 «факт первичен»: список пиннинга чекпойнтов приведён к факту —
#      строка файла, которого нет (ретенция прогона SFT), помечена `removed_by_retention`
#      и снята из проверки, а значения остальных строк не тронуты. Проверяется
#      машиночитаемо: список остаётся годным для `sha256sum -c` (комментарии он
#      пропускает), а имени отсутствующего файла в проверяемых строках нет.
expect_exit 0 "S3aq: список пиннинга — отсутствующий чекпойнт помечен, остальные строки на месте" \
  python3 - "$TMP" <<'PY'
import hashlib, json, pathlib, re, sys
run = pathlib.Path("runs/s3aq-budget-8192-20260920")
pin = run / "PIN.sha256"
assert pin.is_file(), "нет снимка списка пиннинга в каталоге прогона"
lines = pin.read_text(encoding="utf-8").splitlines()
verifiable = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
commented = [ln for ln in lines if ln.lstrip().startswith("#")]

# (а) проверяемые строки — ровно те, чьи файлы есть: формат `sha256sum -c` и имя
LIVE = {"cfinal.pt", "sft_probe_21500.pt"}
GONE = "sft_probe_10000.pt"
for ln in verifiable:
    assert re.fullmatch(r"[0-9a-f]{64} {2}\S+", ln), ln
assert {ln.split()[-1] for ln in verifiable} == LIVE, verifiable
assert all(GONE not in ln for ln in verifiable), "строка отсутствующего файла осталась проверяемой"

# (б) пометка есть, причина названа, sha256 сохранён для следа (не вычеркнут)
mark = [ln for ln in commented if "removed_by_retention" in ln]
assert mark, "нет пометки removed_by_retention"
block = "\n".join(commented)
assert "ретенция" in block and "ADR-028" in block, block
assert re.search(r"[0-9a-f]{64}", block), "sha256 снятой строки не сохранён"

# (в) копия в каталоге прогона побайтово равна источнику, если источник доступен
host = pathlib.Path("/home/user/s3aq-ckpts/PIN.sha256")
if host.is_file():
    assert hashlib.sha256(host.read_bytes()).hexdigest() == hashlib.sha256(pin.read_bytes()).hexdigest(), \
        "снимок списка пиннинга разошёлся с источником"
    print("снимок совпал с источником по sha256")
print(f"пиннинг: {len(verifiable)} проверяемых строк, {len(mark)} пометка об утраченном чекпойнте")
PY

echo "== 29. check_grounding.py (S4-pre: граундинг-аудит по ADR-049) =="

# 29.1 Нормализация и покрытие: регистр, ё→е, разделители `-`/`_`/пробел, транслитерация
#      в обе стороны, однобуквенные слова. Правила инструмента, а не его вывод.
expect_exit 0 "граундинг: нормализация слага и промпта — регистр, ё, разделители" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("cg", pathlib.Path("tools/check_grounding.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

assert m.norm_words("3D_Latent-Alignment Loss") == "3d latent alignment loss"
assert m.norm_words("  ЁЖИК,  ёлка! ") == "ежик елка", m.norm_words("  ЁЖИК,  ёлка! ")
assert m.norm_words(None) == "" and m.norm_words("") == ""
assert m.translit("ёж", m.CYR2LAT) == "ezh"

P = set(m.norm_words("Запиши формулу 3D Latent Alignment Loss в LaTeX").split())
Pt = set(m.norm_words(m.translit(m.norm_words("Запиши формулу 3D Latent Alignment Loss"),
                                 m.CYR2LAT)).split())
c = m.covered("3d_latent_alignment_loss", P, Pt)
assert c["words"] is True and c["single_char_tokens"] == 0, c
# разделители не важны — но порядок слов важен: «loss alignment» не покрытие
assert m.covered("latent_loss_alignment", P, Pt)["words"] is True, "порядок слов не должен мешать"
assert m.covered("attention_is_all_you_need", P, Pt)["words"] is False
# однобуквенное слово: ловится почти всегда — это документированный источник завышения
P2 = set(m.norm_words("концепт C и только он").split())
assert m.covered("c_ceiling", P2, P2)["single_char_tokens"] == 1
# транслитерация: gold латиницей, промпт кириллицей — покрытие появляется только с ней
rup = set(m.norm_words("Агент-интегратор собирает кандидатов").split())
assert m.covered("agent_integrator", rup, rup)["words"] is False
rt = set(m.norm_words(m.translit(m.norm_words("Агент-интегратор собирает кандидатов"),
                                 m.CYR2LAT)).split())
assert m.covered("agent_integrator", rup, rt)["words_translit"] is True
print("нормализация и покрытие: разобраны")
PY

# 29.2 Зелёный путь на фикстуре: числа считаются, а не берутся из текста отчёта.
#      Фикстура — три задачи: покрытая словами, непокрытая и keyword_match.
expect_exit 0 "граундинг: фикстура из трёх задач — точные числа и пометка proxy" \
  python3 - "$TMP" <<'PY'
import json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1]); pool = tmp / "ground_pool.jsonl"
rows = [
    {"task_type": "find_concept", "prompt": "Запиши формулу 3D Latent Alignment Loss в LaTeX.",
     "verifier": "multi_slug_match", "reward": 1.0, "source_env": "E2_formula",
     "source_task_id": "E2_3d_latent_alignment_loss",
     "expected_slugs": ["3d_latent_alignment_loss"]},
    {"task_type": "find_concept", "prompt": "Дано определение концепции:\n\nЗадача 2-AND.",
     "verifier": "multi_slug_match", "reward": 1.0, "source_env": "E3_classify",
     "source_task_id": "E3_2_and_problem", "expected_slugs": ["2_and_problem"]},
    {"task_type": "explain_relation", "prompt": "Как связаны Abductive Hypothesis и Justification?",
     "verifier": "keyword_match", "reward": 1.0, "source_env": "E5_relate",
     "source_task_id": "E5_a", "expected_relation": "connected",
     "keywords": ["abductive_hypothesis", "justification"]},
]
pool.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8")
out = tmp / "ground_ok.json"
rc = subprocess.run([sys.executable, "tools/check_grounding.py", "--pool", str(pool),
                     "--out", str(out), "--no-probe"], capture_output=True, text=True)
assert rc.returncode == 0, rc.stdout + rc.stderr
doc = json.loads(out.read_text(encoding="utf-8"))
assert doc["pool"]["n_tasks"] == 3 and doc["pool"]["n_tasks"] is not None
p = doc["no_action_proxy"]
assert p["is_measurement"] is False, "proxy обязан быть помечен как не-замер"
assert p["totals"]["words_all"] == {"n": 3, "covered": 2, "share": 0.6667}, p["totals"]
assert p["totals"]["literal"]["covered"] == 0, "буквальная подстрока слага в промпте не встречается"
assert len(p["examples"]) == 2, [e["where"] for e in p["examples"]]
assert all(e["where"].endswith((":1", ":3")) for e in p["examples"]), p["examples"]
assert len(p["examples_not_covered"]) == 1 and p["examples_not_covered"][0]["where"].endswith(":2")
# разрез по группам сходится с итогом — иначе отчёт описывает не тот пул
assert sum(g["words_all"]["n"] for g in p["by_group"]) == 3
assert doc["invariants"]["ok"] is True and doc["invariants"]["findings"] == []
# действия: структурных признаков нет; слова-концепты считаются отдельно
acts = doc["actions_in_pool"]
assert acts["n_tasks_with_structural_marker"] == 0 and acts["action_field_present"] is False
assert acts["fields_unknown"] == [], acts["fields_unknown"]
# цена и скелет — на месте, и цена помечена оценкой
assert doc["cost_estimate"]["is_measurement"] is False
assert len(doc["cost_estimate"]["scenarios"]) == 2
sk = doc["walking_skeleton"]
assert sk["is_data"] is False and sk["size"]["n_tasks"] == 50
assert sum(g["n_tasks"] for g in sk["groups"]) == 50
assert doc["verdict"]["pool_is_grounded"] is False
print("фикстура: числа, proxy-пометка, скелет — сходятся")
PY

# 29.3 Красный путь: пул объявлен заземлённым, а действий в нём нет. Отчёт всё равно
#      записан — «красное» это вердикт о пуле, а не отказ инструмента.
expect_exit 1 "граундинг: --expect-grounded на пуле без действий → красное" \
  python3 - "$TMP" <<'PY'
import json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
out = tmp / "ground_red.json"
rc = subprocess.run([sys.executable, "tools/check_grounding.py",
                     "--pool", "datasets/rl_tasks_revpool_v2.jsonl",
                     "--out", str(out), "--expect-grounded", "--no-probe"],
                    capture_output=True, text=True)
assert rc.returncode == 1, (rc.returncode, rc.stdout[-400:], rc.stderr[-400:])
assert "объявлен заземлённым" in rc.stdout, rc.stdout[-300:]
doc = json.loads(out.read_text(encoding="utf-8"))
assert doc["verdict"]["pool_is_grounded"] is False
assert doc["verdict"]["gate_applied"] is False, "страж не подключён к CONSTRAINTS.yaml"
assert doc["verdict"]["axis_ready"] is False
print("красный путь: назван словом и кодом")
sys.exit(rc.returncode)
PY

# 29.4 NOT-VERIFIED: нет пула и пустой пул — разные причины, один код (2).
expect_exit 2 "граундинг: нет пула → NOT-VERIFIED (exit 2)" \
  python3 - "$TMP" <<'PY'
import pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
rc = subprocess.run([sys.executable, "tools/check_grounding.py",
                     "--pool", str(tmp / "нет-такого.jsonl"),
                     "--out", str(tmp / "x.json")], capture_output=True, text=True)
assert rc.returncode == 2, (rc.returncode, rc.stdout, rc.stderr)
assert "нет пула" in rc.stdout and not (tmp / "x.json").exists(), \
    "отчёт по отсутствующему входу не пишется"
print("нет входа: NOT-VERIFIED")
sys.exit(rc.returncode)
PY

expect_exit 2 "граундинг: пустой пул → NOT-VERIFIED (exit 2), а не «0 % утечек»" \
  python3 - "$TMP" <<'PY'
import pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1]); pool = tmp / "ground_empty.jsonl"
pool.write_text("", encoding="utf-8")
rc = subprocess.run([sys.executable, "tools/check_grounding.py", "--pool", str(pool),
                     "--out", str(tmp / "e.json")], capture_output=True, text=True)
assert rc.returncode == 2, (rc.returncode, rc.stdout, rc.stderr)
assert "пул пуст" in rc.stdout
print("пустой пул: NOT-VERIFIED")
sys.exit(rc.returncode)
PY

# 29.4b Пул без gold — не «ноль утечек», а «нечего считать»: доля остаётся null.
expect_exit 0 "граундинг: пул без gold → доля null, а не ноль" \
  python3 - "$TMP" <<'PY'
import json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1]); pool = tmp / "ground_nogold.jsonl"
pool.write_text(json.dumps({"task_type": "x", "prompt": "привет мир"}, ensure_ascii=False) + "\n",
                encoding="utf-8")
out = tmp / "nogold.json"
rc = subprocess.run([sys.executable, "tools/check_grounding.py", "--pool", str(pool),
                     "--out", str(out), "--no-probe"], capture_output=True, text=True)
assert rc.returncode == 0, (rc.returncode, rc.stderr[-300:])
doc = json.loads(out.read_text(encoding="utf-8"))
assert doc["no_action_proxy"]["totals"]["words_all"]["share"] is None
assert doc["no_action_proxy"]["totals"]["words_all"]["n"] == 0
assert doc["no_action_proxy"]["pool"]["n_tasks_with_gold"] == 0
assert "не считается" in doc["no_action_proxy"]["what_it_is"]
# в сводке на месте доли — прочерк, а не «0.00 %»: null и ноль не одно и то же
assert "—" in doc["summary"]["no_action_proxy"], doc["summary"]["no_action_proxy"]
assert "0.00%" not in doc["summary"]["no_action_proxy"]
print("пул без gold: доля не выдумана")
PY

# 29.5 Отчёт в evidence/ не разошёлся с прибором и с пулом: числа, хеш, примеры и
#      источник цены. Тест переживает пересборку отчёта — он требует согласия.
expect_exit 0 "граундинг: evidence/grounding-audit.json согласован с пулом и прибором" \
  python3 - <<'PY'
import hashlib, importlib.util as u, json, pathlib, sys
sys.path.insert(0, "tools")
doc = json.loads(pathlib.Path("evidence/grounding-audit.json").read_text(encoding="utf-8"))
spec = u.spec_from_file_location("cg", pathlib.Path("tools/check_grounding.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)

pool = pathlib.Path(doc["pool"]["path"])
assert pool.is_file(), doc["pool"]["path"]
sha = hashlib.sha256(pool.read_bytes()).hexdigest()
assert sha == doc["pool"]["sha256"], "хеш пула разошёлся с отчётом"
n_lines = sum(1 for l in pool.read_text(encoding="utf-8").splitlines() if l.strip())
assert n_lines == doc["pool"]["n_tasks"] == 9162, (n_lines, doc["pool"]["n_tasks"])

p = doc["no_action_proxy"]
assert p["is_measurement"] is False
assert sum(g["words_all"]["n"] for g in p["by_group"]) == p["totals"]["words_all"]["n"]
assert doc["invariants"]["ok"] is True, doc["invariants"]
# отчёт не разошёлся сам с собой: хеш прибора пересчитывается по его же содержимому
assert doc["audit_sha1"] == m.audit_digest(doc), "sha1 прибора не сходится с телом отчёта"
# примеры указывают на строки, которые действительно несут gold словами промпта
lines = pool.read_text(encoding="utf-8").splitlines()
for e in p["examples"]:
    idx = int(e["where"].rsplit(":", 1)[1])
    task = json.loads(lines[idx - 1])
    assert task["prompt"] == e["prompt"] or task["prompt"].startswith(e["prompt"][:200])
    P = set(m.norm_words(task["prompt"]).split())
    assert all(all(t in P for t in m.norm_words(g).split()) for g in e["gold"]), e["where"]
# цена читается из чужого замера, а не переписана числом в код
probe = json.loads(pathlib.Path("evidence/s3-rl-probe.json").read_text(encoding="utf-8"))
c = doc["cost_estimate"]
assert c["base"]["step_seconds_mean"] == probe["measurements"]["step_seconds"]["mean"]
assert c["base"]["generation_share_pct"] == \
    probe["measurements"]["phase_split"]["shares_pct"]["t_generate"]
assert c["is_measurement"] is False and c["label"] == "оценка, не замер"
lo, hi = c["scenarios"]["process_jail"]["step_seconds"]
assert lo < hi and c["scenarios"]["container_per_rollout"]["step_seconds"][1] > hi
# верификаторы: пороги берутся из файла порогов, а не из текста
th = json.loads(pathlib.Path("data/verifier-thresholds.json").read_text(encoding="utf-8"))
assert doc["verifier_binarization"]["n_thresholds_null"] == \
    sum(1 for v in th["thresholds"].values() if v is None)
print(f"отчёт: пул {doc['pool']['n_tasks']}, proxy {p['totals']['words_all']['share']:.3f}, "
      f"цена {lo}–{hi} с/шаг (оценка)")
PY

# 29.6 Воспроизводимость прибора: два прогона дают один и тот же sha1 содержимого
#      (отметка времени в него не входит). Иначе числа отчёта нельзя перепроверить.
expect_exit 0 "граундинг: прибор воспроизводим — два прогона, один sha1" \
  python3 - "$TMP" <<'PY'
import json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
digests = []
for i in (1, 2):
    out = tmp / f"ground_rep{i}.json"
    rc = subprocess.run([sys.executable, "tools/check_grounding.py",
                         "--pool", "datasets/rl_tasks_revpool_v2.jsonl",
                         "--out", str(out), "--no-probe"], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stderr[-300:]
    d = json.loads(out.read_text(encoding="utf-8"))
    digests.append((d["audit_sha1"], d["generated_at"]))
assert digests[0][0] == digests[1][0], digests
assert digests[0][1] != digests[1][1], "отметка времени обязана различаться — иначе тест пуст"
print(f"sha1 прибора устойчив: {digests[0][0][:12]}")
PY

# 29.7 Класс утечки, который не ловит буквальное сравнение. Это не мнение, а разрыв
#      между двумя чтениями одних и тех же данных: если он схлопнется — сигнал.
expect_exit 0 "граундинг: буквальное сравнение слага не ловит класс, который ловят слова" \
  python3 - <<'PY'
import json, pathlib
doc = json.loads(pathlib.Path("evidence/grounding-audit.json").read_text(encoding="utf-8"))
t = doc["no_action_proxy"]["totals"]
words, literal = t["words_all"]["covered"], t["literal"]["covered"]
# буквальное сравнение находит единицы (слаги без разделителей: `kvzip`, полные
# slug-и в промпте env_types), словесное — тысячи: разрыв и есть предмет
assert literal <= 5, f"буквальная подстрока вдруг нашла {literal} задач — конвенция изменилась"
assert words >= 3000, words
assert words / max(literal, 1) > 100, (words, literal)
# конвенция гейта A (check_eval_leakage.py) — буквальная подстрока; здесь показано,
# что она даёт на этом пуле. Числа берутся из отчёта, а не из прозы.
assert doc["no_action_proxy"]["pool"]["n_gold_kind"].get("slug", 0) > 8000
print(f"слова: {words} задач, буквальная подстрока: {literal} — разрыв виден числом")
PY

# 29.8 Только чтение: прогон аудита не меняет пул. sha256 до и после совпадают
#      (AD-4: данные не копируются и не правятся).
expect_exit 0 "граундинг: аудит не меняет пул (sha256 до и после)" \
  python3 - "$TMP" <<'PY'
import hashlib, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
pool = pathlib.Path("datasets/rl_tasks_revpool_v2.jsonl")
before = hashlib.sha256(pool.read_bytes()).hexdigest()
rc = subprocess.run([sys.executable, "tools/check_grounding.py",
                     "--out", str(tmp / "ground_readonly.json"), "--no-probe"],
                    capture_output=True, text=True)
assert rc.returncode == 0, rc.stderr[-300:]
after = hashlib.sha256(pool.read_bytes()).hexdigest()
assert before == after, "аудит изменил пул"
print("пул не тронут")
PY

echo "== 30. grounded_runner.py + check_grounded_pool.py (S4-skeleton: заземление, ADR-049) =="
# Правило ADR-049 п.5 («успех без tool_call > 0 → красное») обязано быть живым
# правилом, а не декларацией: проверяются и красный путь (нарушение в
# доказательстве), и PENDING-путь (артефакта нет → НЕ error, ADR-023 п.12), и
# устаревшее доказательство (дрейф набора после прогона, ADR-012).
HAVE_DOCKER=0
docker version >/dev/null 2>&1 && HAVE_DOCKER=1
SKIP=0
# Пропуск назван вслух: молчаливый зелёный без docker запрещён.
run_or_skip() {
  if [ "$HAVE_DOCKER" = 1 ]; then
    expect_exit "$@"
  else
    SKIP=$((SKIP + 1))
    printf '  SKIP %-58s (нет docker: изоляция G4 не проверяема)\n' "$2"
  fi
}

# 30.1 Фикстуры стража: набор-пустышка + доказательства (чистое, нарушение,
#      устаревшее, дрейф верификатора, битая схема). Хеш набора считается тем же
#      кодом, что и в страже, — иначе тест проверял бы копию формулы, а не её.
python3 - "$TMP" <<'PY'
import importlib.util, json, pathlib, sys

spec = importlib.util.spec_from_file_location("cgp", "tools/check_grounded_pool.py")
cgp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cgp)

tmp = pathlib.Path(sys.argv[1])
st = tmp / "guardset"
t = st / "tasks" / "gs-fix-01"
t.mkdir(parents=True, exist_ok=True)
(t / "task.json").write_text(json.dumps({"id": "gs-fix-01", "tool": "shell", "network": "none",
                                         "verifier": "verify.py", "prompt": "фикстура"}, ensure_ascii=False), encoding="utf-8")
(t / "verify.py").write_text("# верификатор-фикстура\n", encoding="utf-8")
canon, _files = cgp.set_digest(st)
vsha = cgp.sha256_file(t / "verify.py")


def doc(no_action=0, empty=0, run=1, share=0.0, set_sha=canon, verifier_sha=vsha, schema="grounded-skeleton-evidence/1",
        leak=None, gate_pass=True):
    return {
        "schema": schema, "generated_at": "2026-09-20T00:00:00+00:00", "status": "ok",
        "set": {"sha256_full": set_sha},
        "no_action": {"share": share},
        "tasks": [{
            "id": "gs-fix-01", "spec": {"verifier": "verify.py"},
            "res": {"run": {"score": run}, "no-action": {"score": no_action}, "empty-action": {"score": empty}},
            "gates": {"G1": {"pass": gate_pass}, "G2": {"pass": gate_pass},
                      "G3": {"pass": gate_pass, "verifier_sha256": verifier_sha}, "G4": {"pass": gate_pass},
                      "leak": leak or {}},
        }],
    }


(tmp / "ev-clean.json").write_text(json.dumps(doc()), encoding="utf-8")
(tmp / "ev-violation.json").write_text(json.dumps(doc(no_action=1, share=1.0)), encoding="utf-8")
(tmp / "ev-stale.json").write_text(json.dumps(doc(set_sha="deadbeef")), encoding="utf-8")
(tmp / "ev-drift.json").write_text(json.dumps(doc(verifier_sha="0" * 64)), encoding="utf-8")
(tmp / "ev-leak.json").write_text(json.dumps(doc(leak={"gold_in_prompt": True})), encoding="utf-8")
(tmp / "ev-norun.json").write_text(json.dumps(doc(run=0, gate_pass=True)), encoding="utf-8")
(tmp / "ev-badschema.json").write_text(json.dumps(doc(schema="something/else")), encoding="utf-8")
print(f"фикстуры стража собраны: sha набора {canon[:12]}")
PY

# 30.2 Страж: чистое доказательство — зелёный; артефакта нет — тоже зелёный
#      (правило п.5 применяется только к существующему заземлённому артефакту).
expect_exit 0 "страж: чистое доказательство → зелёный" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/guardset" --evidence "$TMP/ev-clean.json"
expect_exit 0 "страж: доказательства нет → по п.5 красное не срабатывает" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/guardset" --evidence "$TMP/ev-absent.json"

# 30.3 Красный путь: успех без tool_call > 0 — предмет правила (ADR-049 п.5).
expect_exit 1 "страж: успех без tool_call > 0 → красное" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/guardset" --evidence "$TMP/ev-violation.json"
expect_contains "success-without-tool-call" "страж: находка названа по имени класса" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/guardset" --evidence "$TMP/ev-violation.json"

# 30.4 Прочие находки стража: устаревшее доказательство, дрейф верификатора,
#      утечка gold, среда не пропускает действия.
expect_contains "evidence-stale" "страж: доказательство не о текущем наборе → красное" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/guardset" --evidence "$TMP/ev-stale.json"
expect_contains "verifier-drift" "страж: верификатор изменился после прогона → красное" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/guardset" --evidence "$TMP/ev-drift.json"
expect_contains "gold-leak" "страж: gold в промпте → красное (ADR-049 п.9)" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/guardset" --evidence "$TMP/ev-leak.json"
expect_contains "environment-blocks-actions" "страж: среда не пропускает действия → красное (п.6)" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/guardset" --evidence "$TMP/ev-norun.json"

# 30.5 NOT-VERIFIED: доказательство есть, но схема не та — молчание не доказательство.
expect_exit 2 "страж: чужая схема доказательства → NOT-VERIFIED" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/guardset" --evidence "$TMP/ev-badschema.json"
expect_exit 2 "страж: доказательство есть, набора нет → NOT-VERIFIED" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/no-such-set" --evidence "$TMP/ev-clean.json"

# 30.6 PENDING-путь (ADR-049 п.11): отсутствие артефакта даёт PENDING-EVIDENCE, а
#      не error — иначе правило краснеет на пустом месте (класс ADR-023 п.12).
expect_exit 1 "страж pending: артефактов нет → PENDING-EVIDENCE" \
  python3 tools/check_grounded_pool.py --root "$TMP" --set-dir "$TMP/none" --evidence "$TMP/none.json" --mode pending
expect_contains "PENDING-EVIDENCE" "страж pending: состояние названо вслух" \
  python3 tools/check_grounded_pool.py --root "$TMP" --set-dir "$TMP/none" --evidence "$TMP/none.json" --mode pending
expect_exit 0 "страж pending: артефакт появился → PENDING снят" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/guardset" --evidence "$TMP/ev-clean.json" --mode pending
expect_exit 0 "страж pending: нарушение — не его предмет (за него отвечает guard)" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/guardset" --evidence "$TMP/ev-violation.json" --mode pending

# 30.7 Раннер: без docker — NOT-VERIFIED, а не «заземление без изоляции».
mkdir -p "$TMP/fakebin"
cat > "$TMP/fakebin/docker" <<'SH'
#!/bin/sh
echo "docker: cannot connect to the Docker daemon" >&2
exit 1
SH
chmod +x "$TMP/fakebin/docker"
expect_exit 2 "раннер: docker недоступен → NOT-VERIFIED" \
  env PATH="$TMP/fakebin:$PATH" python3 tools/grounded_runner.py --tasks gs-shell-01 --run --out "$TMP/gs-nodocker.json"

# 30.8 Фикстура-задача, решение которой не делает работы: положительный контроль
#      обязан покраснеть (иначе зелёный скелет ничего не доказывает).
python3 - "$TMP" <<'PY'
import json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
t = tmp / "fixset" / "tasks" / "gs-fix-01"
(t / "seed").mkdir(parents=True, exist_ok=True)
(t / "seed" / "in.txt").write_text("данные\n", encoding="utf-8")
(t / "solution.sh").write_text("#!/bin/sh\n# действие без работы: файла out.txt не создаёт\ntrue\n", encoding="utf-8")
(t / "verify.py").write_text(
    "import argparse, json, pathlib\n"
    "ap = argparse.ArgumentParser()\n"
    "ap.add_argument('--snapshot', required=True); ap.add_argument('--task', required=True)\n"
    "ap.add_argument('--seed', required=True); ap.add_argument('--image', default='')\n"
    "ap.add_argument('--holdout', default=''); ap.add_argument('--journal', default='')\n"
    "ap.add_argument('--scratch', default='')\n"
    "a = ap.parse_args()\n"
    "ok = (pathlib.Path(a.snapshot) / 'out.txt').is_file()\n"
    "print(json.dumps({'score': 1 if ok else 0, 'reason': 'out.txt есть' if ok else 'out.txt нет',\n"
    "                  'expected_public': 'out.txt', 'reads': ['snapshot:out.txt']}, ensure_ascii=False))\n",
    encoding="utf-8")
(t / "task.json").write_text(json.dumps({
    "schema": "grounded-task/2", "id": "gs-fix-01", "tool": "shell", "network": "none", "max_steps": 4, "workdir": "/work",
    "prompt": "фикстура", "initial_state": "фикстура", "goal_action": "фикстура", "success_criterion": "фикстура",
    "no_action_probe": "фикстура", "empty_action_probe": "фикстура", "seed_dir": "seed",
    "solution": "solution.sh", "verifier": "verify.py", "verifier_reads": ["snapshot:out.txt"], "timeout_s": 30,
}, ensure_ascii=False), encoding="utf-8")
print("фикстура-задача собрана")
PY
GS_RUN_ROOT="$HOME/.cache/arch-ml/grounded-skeleton-tests-$$"
run_or_skip 1 "раннер: действие без работы → положительный контроль красный" \
  python3 tools/grounded_runner.py --set-dir "$TMP/fixset" --run --json --run-dir "$GS_RUN_ROOT" --out "$TMP/gs-fix-run.json"
run_or_skip 0 "раннер: та же фикстура, проба no-action → зелёный (0)" \
  python3 tools/grounded_runner.py --set-dir "$TMP/fixset" --no-action --run-dir "$GS_RUN_ROOT" --out "$TMP/gs-fix-na.json"

# 30.9 Полный проход по настоящему набору: три режима, отчёт в $TMP (в дерево
#      кейса тест не пишет), затем — сверка отчёта стражем.
run_or_skip 0 "раннер: полный проход по скелету (5 задач × 3 режима)" \
  python3 tools/grounded_runner.py --run --no-action --empty-action --repeat 2 --json \
    --sidecar-probe --run-dir "$GS_RUN_ROOT" --out "$TMP/gs-evidence.json"

# 30.9b Метка площадки честна: заявка «внутри контейнера стадии» на хосте — отказ,
#       а не строка в отчёте. Проверка условна: внутри контейнера заявка законна,
#       и подсовывать туда ложный отказ значило бы проверять не то.
if [ ! -e /.dockerenv ]; then
  run_or_skip 2 "раннер: заявка «в контейнере стадии» на хосте → NOT-VERIFIED, не метка" \
    python3 tools/grounded_runner.py --tasks gs-http-01 --run --in-stage-container --out "$TMP/gs-claim.json"
else
  SKIP=$((SKIP + 1)); printf '  SKIP %-58s (проба идёт в контейнере: ложная заявка неотличима)\n' \
    "раннер: заявка «в контейнере стадии» на хосте → NOT-VERIFIED, не метка"
fi

# 30.10 В отчёте: положительный контроль 1, проба no-action 0 на каждой задаче,
#       косты среды измерены (а не унаследованы), изоляция подтверждена пробами.
expect_exit 0 "раннер: run=1 и no-action=0 на каждой задаче отчёта" \
  python3 - "$TMP/gs-evidence.json" <<'PY'
import json, pathlib, sys
d = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert d["schema"] == "grounded-skeleton-evidence/1", d["schema"]
assert d["tasks"], "в отчёте нет задач"
for t in d["tasks"]:
    assert t["res"]["run"]["score"] == 1, (t["id"], t["res"]["run"])
    assert t["res"]["no-action"]["score"] == 0, (t["id"], t["res"]["no-action"])
    assert t["res"]["empty-action"]["score"] == 0, (t["id"], t["res"]["empty-action"])
    for g in ("G1", "G2", "G3", "G4"):
        assert t["gates"][g]["pass"] is True, (t["id"], g)
    assert t["res"]["no-action"]["text_answer_carried_expected"] is True, t["id"]
    assert t["res"]["no-action"]["verifier_ignores_text"] is True, t["id"]
assert d["no_action"]["share"] == 0.0, d["no_action"]
assert d["cost"]["is_measurement"] is True
assert d["cost"]["measured"]["t_sandbox_per_rollout_s"] is not None
assert d["cost"]["comparison"]["ratio_to_base_recomputed"], "цена не пересчитана фактом"
assert d["verdict"]["skeleton_ready"] is True
# Мок — sidecar-контейнер в сети прогона (поправка ADR-049 п.10), и это видно из
# отчёта: имя, образ, адрес В СЕТИ ПРОГОНА, журнал вне песочницы. Канарейка —
# контейнер вне сети прогона, подтвердивший собственную живость.
mock = d["network"]["sidecars"]["mock"]
assert mock["kind"] == "container", mock
assert mock["in_run_network"] is True and mock["ip"], mock
assert mock["network"] in set(d["network"]["networks"].values()), mock
assert mock["module_sha256"], "модуль мока не назван хешем"
canary = d["network"]["sidecars"]["canary"]
assert canary["kind"] == "container" and canary["self_check"] is True, canary
assert canary["network"] != mock["network"], "канарейка обязана быть ВНЕ сети прогона"
for name, p in d["isolation"].items():
    assert p["canary_attempted"] is True and p["canary_unreachable"] is True, (name, p.get("canary_attempted"))
assert d["cost"]["sidecar_mock"]["per_step_s"] > 0, d["cost"]["sidecar_mock"]
assert d["cost"]["comparison"]["ratio_to_base_with_sidecar"], "цена с sidecar не пересчитана"
# Проба цены повторяема, а не единична: один замер неотличим от задержки демона.
probe = d["sidecar"]
assert probe["reps"] == 3 and len(probe["measurements"]) == 3, probe
assert not probe["errors"], probe["errors"]
assert probe["total_s_mean"] > 0, probe
# Место пробы названо по признакам, а не по флагу: заявки не было, а метка всё
# равно обязана следовать факту (прогон может идти и внутри контейнера).
ex = d["host"]["execution"]
assert ex["claim_in_stage_container"] is False, ex
assert ex["in_container"] is (ex["where"] != "хост"), ex
print(f"задач: {len(d['tasks'])}, цена среды {d['cost']['measured']['rollout_overhead_s']} с/rollout, "
      f"sidecar {d['cost']['sidecar_mock']['per_step_s']} с/шаг")
PY

# 30.11 Воспроизводимость состояния (ADR-049 п.3(iii)): два прогона одного режима
#       с одним сидом дают одно состояние — иначе сравнение траекторий недоказуемо.
expect_exit 0 "раннер: состояние воспроизводимо между прогонами (--repeat 2)" \
  python3 - "$TMP/gs-evidence.json" <<'PY'
import json, pathlib, sys
d = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert d["reproducibility"]["repeat"] == 2, d["reproducibility"]
for tid, modes in d["reproducibility"]["state_digests"].items():
    for mode, digests in modes.items():
        assert len(digests) == 1, f"{tid}/{mode}: состояния разошлись ({len(digests)})"
print("состояния совпали во всех повторных прогонах")
PY

# 30.12 Отчёт сверяется стражем: доказательство относится к текущему набору и к
#       текущим верификаторам (иначе «зелёный» устарел).
run_or_skip 0 "раннер → страж: доказательство о текущем наборе → зелёный" \
  python3 tools/check_grounded_pool.py --root . --evidence "$TMP/gs-evidence.json"

# 30.13 AD-2/AD-12: карточка набора несёт ПОЛНЫЙ sha256 и совпадает с отчётом.
expect_exit 0 "карточка набора: полный sha256 совпадает с записанным в отчёте" \
  python3 - "$TMP/gs-evidence.json" <<'PY'
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location("cgp", "tools/check_grounded_pool.py")
cgp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cgp)
ev = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
card = json.loads(pathlib.Path("data/grounded-skeleton/card.json").read_text(encoding="utf-8"))
canon, files = cgp.set_digest(pathlib.Path("data/grounded-skeleton"))
assert card["sha256_full"] == canon, "карточка устарела: прогони tools/grounded_runner.py --card"
assert ev["set"]["sha256_full"] == canon, "отчёт описывает другой набор"
assert card["n_tasks"] == 5 and len(card["tasks"]) == 5, card["n_tasks"]
assert len(files) == len(card["files"]), (len(files), len(card["files"]))
for t in card["tasks"]:
    assert cgp.sha256_file(pathlib.Path("data/grounded-skeleton/tasks") / t["id"] / t["verifier"]) == t["verifier_sha256"], t["id"]
print(f"карточка: {card['n_tasks']} задач, {card['size_bytes']} Б, sha {canon[:12]}")
PY

# 30.14 ADR-049 п.9: идентификатор задачи не содержит ожидаемого ответа, а проба
#       no-action подставляет в текст ТОЧНОЕ ожидаемое значение (иначе G2 вакуумна).
expect_exit 0 "задачи: id без ожидаемого ответа, текст пробы несёт gold" \
  python3 - "$TMP/gs-evidence.json" <<'PY'
import json, pathlib, sys
d = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for t in d["tasks"]:
    leak = t["gates"]["leak"]
    assert leak["gold_in_id"] is False, (t["id"], leak)
    assert leak["gold_in_prompt"] is False, (t["id"], leak)
    assert t["gates"]["G2"]["no_action_text_carried_expected"] is True, t["id"]
print("id чистые, состязательный текст пробы несёт ожидаемое значение")
PY

# 30.15 Политика канонического хеша набора: пересобираемый байткод — вне карты, а
#       прежнее объявление идентичности — проверяемое свидетельство, не подпись.
#       Класс дефекта не выдуман: 20.09.2026 байткод попал в каноническую карту
#       набора скелета, и правило C-026 краснело by construction (ADR-023 п.12) —
#       хеш из чистого клона не воспроизводился.
python3 - "$TMP" <<'PY'
import importlib.util, json, pathlib, sys


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cgp = load("cgp", "tools/check_grounded_pool.py")
gr = load("gr", "tools/grounded_runner.py")

# Формулу политики держат два файла (страж самостоятелен, AD-10): расхождение
# означало бы, что карточка и страж считают разные наборы.
assert cgp.REBUILDABLE_DIRS == gr.REBUILDABLE_DIRS, (cgp.REBUILDABLE_DIRS, gr.REBUILDABLE_DIRS)
assert cgp.REBUILDABLE_SUFFIXES == gr.REBUILDABLE_SUFFIXES, (cgp.REBUILDABLE_SUFFIXES, gr.REBUILDABLE_SUFFIXES)
for rel in ("a/b.pyc", "__pycache__/m.py", "tasks/t/__pycache__/v.cpython-311.pyc"):
    assert cgp.is_rebuildable(rel) and gr.is_rebuildable(rel), rel
for rel in ("a/b.py", "tasks/t/verify.py", "README.md", "pyc/not_bytecode.txt"):
    assert not cgp.is_rebuildable(rel) and not gr.is_rebuildable(rel), rel

tmp = pathlib.Path(sys.argv[1])
st = tmp / "polset"
t = st / "tasks" / "gs-pol-01"
t.mkdir(parents=True, exist_ok=True)
(t / "task.json").write_text(json.dumps({
    "schema": "grounded-task/1", "id": "gs-pol-01", "tool": "shell", "network": "none",
    "verifier": "verify.py", "prompt": "фикстура политики хеша",
}, ensure_ascii=False), encoding="utf-8")
(t / "verify.py").write_text("# верификатор-фикстура\n", encoding="utf-8")
(st / "README.md").write_text("набор\n", encoding="utf-8")
canon_before, _ = cgp.set_digest(st)
gr.write_card(st, st / "card.json")
# байткод появился ПОСЛЕ записи карточки — как от одного импорта верификатора
cache = t / "__pycache__"
cache.mkdir()
(cache / "verify.cpython-311.pyc").write_bytes(b"\x00bytecode")
canon_after, files_after = cgp.set_digest(st)
assert canon_before == canon_after, "пересобираемый байткод изменил канонический хеш набора"
card = json.loads((st / "card.json").read_text(encoding="utf-8"))
assert card["sha256_full"] == canon_after, "карточка разошлась с канонической картой"
assert not [k for k in card["files"] if k.endswith(".pyc")], card["files"]
assert card["excluded_rebuildable"], "политика не объявлена в карточке"
print(f"политика: байткод вне карты, хеш {canon_after[:12]}, файлов в карте {len(card['files'])}")
PY

# Смена политики: состояние «до» (байткод в карте + другой документ корня) и
# пересборка карточки — прежнее объявление обязано лечь рядом (ADR-028 п.1).
python3 - "$TMP" <<'PY'
import importlib.util, json, pathlib, sys


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gr = load("gr", "tools/grounded_runner.py")
tmp = pathlib.Path(sys.argv[1])
st = tmp / "polset"
card = json.loads((st / "card.json").read_text(encoding="utf-8"))
old_files = dict(card["files"])
old_files["tasks/gs-pol-01/__pycache__/verify.cpython-311.pyc"] = "a" * 64
old_files["README.md"] = "b" * 64
(st / "card.json").write_text(json.dumps(
    {**card, "files": old_files, "sha256_full": gr.canonical_digest(old_files)}, ensure_ascii=False), encoding="utf-8")
(st / "README.md").write_text("набор, правленый документ\n", encoding="utf-8")
gr.write_card(st, st / "card.json")
card = json.loads((st / "card.json").read_text(encoding="utf-8"))
assert card["previous_sha256_full"] == gr.canonical_digest(old_files), card["previous_sha256_full"]
assert card["previous_files_excluded"] == {"tasks/gs-pol-01/__pycache__/verify.cpython-311.pyc": "a" * 64}, card
assert card["previous_files_doc_delta"] == {"README.md": "b" * 64}, card
for field in ("previous_sha256_full_method", "previous_sha256_full_at", "previous_sha256_full_reason"):
    assert card.get(field), field
assert not card["previous_files_doc_delta"].get("tasks/gs-pol-01/verify.py"), "измеренный артефакт попал в класс документов"
# доказательство, записанное ПОД ПРЕЖНЕЙ идентичностью
import hashlib
(tmp / "pol-ev.json").write_text(json.dumps({
    "schema": "grounded-skeleton-evidence/1", "generated_at": "2026-09-20T00:00:00+00:00", "status": "ok",
    "set": {"sha256_full": card["previous_sha256_full"]}, "no_action": {"share": 0.0},
    "tasks": [{"id": "gs-pol-01", "spec": {"verifier": "verify.py"},
               "res": {"run": {"score": 1}, "no-action": {"score": 0}, "empty-action": {"score": 0}},
               "gates": {"G1": {"pass": True}, "G2": {"pass": True},
                         "G3": {"pass": True,
                                "verifier_sha256": hashlib.sha256((st / "tasks/gs-pol-01/verify.py").read_bytes()).hexdigest()},
                         "G4": {"pass": True}, "leak": {}}}],
}, ensure_ascii=False), encoding="utf-8")
print("фикстура прежней идентичности собрана")
PY

expect_exit 0 "страж: доказательство под прежней идентичностью → зелёный" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/polset" --evidence "$TMP/pol-ev.json"
expect_contains "прежней идентичностью" "страж: опознание по прежней идентичности названо вслух" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/polset" --evidence "$TMP/pol-ev.json"
expect_exit 0 "страж: карточка с прежним объявлением → карточка не «устарела»" \
  python3 - "$TMP" <<'PY'
import json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
out = subprocess.run([sys.executable, "tools/check_grounded_pool.py", "--root", ".",
                      "--set-dir", str(tmp / "polset"), "--evidence", str(tmp / "pol-ev.json"), "--json"],
                     capture_output=True, text=True)
d = json.loads(out.stdout)
assert d["detail"]["evidence_identity"] == "previous", d["detail"].get("evidence_identity")
assert d["detail"]["previous_identity"]["restored_sha256_full"] == d["detail"]["previous_identity"]["previous_sha256_full"]
assert d["detail"]["previous_identity"]["previous_files_doc_delta"] == ["README.md"]
print(f"опознание: {d['detail']['evidence_identity']}, отличия {d['detail']['previous_identity']['previous_files_doc_delta']}")
PY

# Измеренный артефакт в классе «документов корня» — не опознание, а дыра: подмена
# задачи пряталась бы за прежней идентичностью. Проверяется в обе стороны.
python3 - "$TMP" <<'PY'
import json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
st = tmp / "polset"
card = json.loads((st / "card.json").read_text(encoding="utf-8"))
card["previous_files_doc_delta"]["tasks/gs-pol-01/verify.py"] = "c" * 64
(st / "card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
PY
expect_contains "card-previous-inconsistent" "страж: измеренный артефакт в классе документов → красное" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/polset" --evidence "$TMP/pol-ev.json"

# Подмена измеренного артефакта при пересобранной карточке: прежняя идентичность из
# текущего содержимого не восстанавливается — значит изменилось не объявленное.
python3 - "$TMP" <<'PY'
import importlib.util, pathlib, sys


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gr = load("gr", "tools/grounded_runner.py")
tmp = pathlib.Path(sys.argv[1])
st = tmp / "polset"
# снять подсунутый класс документов: проверяется именно подмена измеренного артефакта
card = json.loads((st / "card.json").read_text(encoding="utf-8"))
card["previous_files_doc_delta"].pop("tasks/gs-pol-01/verify.py", None)
(st / "card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
(st / "tasks" / "gs-pol-01" / "verify.py").write_text("# подмена верификатора\n", encoding="utf-8")
gr.write_card(st, st / "card.json")
card = json.loads((st / "card.json").read_text(encoding="utf-8"))
assert card["previous_files_doc_delta"] == {"README.md": "b" * 64}, card["previous_files_doc_delta"]
PY
expect_contains "card-previous-inconsistent" "страж: подмена верификатора не прячется за прежним объявлением" \
  python3 tools/check_grounded_pool.py --root . --set-dir "$TMP/polset" --evidence "$TMP/pol-ev.json"

# 30.16 Предпроверка монтирования — МАШИННАЯ, а не по следствию. Дефект, ради
#       которого она заведена: несовпадение путей (same-path) ловилось по
#       следствию — «мок не отметился готовым», «задача упала на mv»; то же лицо
#       имел найденный дефект владения, когда «среда не дала писать» читалось как
#       «задача не решена». Маркер-контейнер называет причину ДО прогона: nonce
#       каталога прогона и состав дерева набора обязаны быть видны из контейнера по
#       тем путям, которые назвал раннер.
cat > "$TMP/gs-preflight-check.py" <<'PY'
import json, subprocess, sys
p = subprocess.run([sys.executable, "tools/grounded_runner.py", "--mount-preflight-only", "--json",
                    "--run-dir", sys.argv[1]], capture_output=True, text=True)
assert p.returncode == 0, (p.returncode, p.stdout[-600:], p.stderr[-600:])
d = json.loads(p.stdout)
assert d["ok"] is True and not d["findings"], d["findings"]
assert d["observed"]["run_dir_marker_read"] is True, d["observed"]
assert d["observed"]["set_tree_listing_matches"] is True, d["observed"]
assert d["probe"]["set_dir"], "дерево набора не проверялось, хотя в наборе есть задача с сетью"
assert d["is_measurement"] is False, "проба контракта выдана за измерение"
print(d["reading"])
PY
run_or_skip 0 "монтирование: предпроверка same-path → зелёный (каталог и дерево видны)" \
  python3 "$TMP/gs-preflight-check.py" "$GS_RUN_ROOT"

# 30.17 Негативная проба предпроверки — ЖИВАЯ: раннер запускается ВНУТРИ контейнера.
#       Только так у демона и у раннера оказываются РАЗНЫЕ виды на один и тот же
#       путь — ровно то, что даёт контейнер стадии без same-path монтирования:
#       (A) демон пути не видит вовсе — docker отказывает;
#       (B) демон путь видит, но это ДРУГОЙ каталог — контейнер молча получает не то
#           (так под snap-конфайнментом выглядит /tmp: bind «удался», а внутри пусто);
#           именно это лицо и маскировало причину.
#       CLI демона внутрь контейнера нести приходится: проба гоняет docker через
#       демон хоста. Где он там неисполним (другая сборка, другое ABI) — проба
#       НАЗЫВАЕТ негативный путь непоказанным, а не выдаёт молчание за проверку.
GS_CLI_DIR="$HOME/.cache/arch-ml/gs-cli-$$"
GS_DECOY="$HOME/.cache/arch-ml/gs-decoy-$$"
mkdir -p "$GS_CLI_DIR" "$GS_DECOY"
cp "$(command -v docker)" "$GS_CLI_DIR/docker" 2>/dev/null || true
chmod +x "$GS_CLI_DIR/docker" 2>/dev/null || true
printf '{}\n' > "$GS_DECOY/card.json"
cat > "$TMP/gs-preflight-mutant.py" <<'PY'
import glob, json, pathlib, subprocess, sys

MODE, CLI, RUNROOT = sys.argv[1], sys.argv[2], sys.argv[3]
case = pathlib.Path.cwd()
mounts = []
for pat in ("/lib64/ld-linux-*.so.*", "/lib/ld-linux-*.so.*", "/lib/*-linux-gnu"):
    for path in glob.glob(pat):
        mounts += ["-v", f"{path}:{path}:ro"]
base = ["docker", "run", "--rm", "--network", "none", "-v", f"{case}:{case}:ro",
        "-v", f"{CLI}:/usr/local/bin/docker:ro", *mounts,
        "-v", "/var/run/docker.sock:/var/run/docker.sock", "-w", str(case)]
run = ["python3", "tools/grounded_runner.py", "--mount-preflight-only", "--json"]

if MODE == "gate":            # исполнимо ли CLI демона внутри контейнера вообще
    p = subprocess.run(base + ["python:3.12-alpine", "docker", "version"],
                       capture_output=True, text=True, timeout=180)
    print("дан" if p.returncode == 0 else (p.stderr.strip()[:200] or "нет"))
    raise SystemExit(0 if p.returncode == 0 else 2)

if MODE == "A":
    cmd = base + ["python:3.12-alpine"] + run + ["--run-dir", "/relocated-run"]
    want = ("не видит",)
else:
    decoy, real_set = sys.argv[4], case / "data" / "grounded-skeleton"
    cmd = base + ["-v", f"{real_set}:{decoy}:ro", "-v", f"{RUNROOT}:{RUNROOT}",
                  "python:3.12-alpine"] + run + ["--set-dir", decoy, "--run-dir", RUNROOT]
    want = ("ДРУГОЙ каталог", "не та")
p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
assert p.returncode == 2, (p.returncode, p.stdout[-600:], p.stderr[-600:])
d = json.loads(p.stdout)
assert d["ok"] is False and d["findings"], d
joined = " | ".join(d["findings"])
assert "same-path монтирование не выполнено" in joined, joined
assert any(w in joined for w in want), joined
print(f"мутант {MODE}: " + d["findings"][0][:100])
PY
if [ "$HAVE_DOCKER" = 1 ] && python3 "$TMP/gs-preflight-mutant.py" gate \
     "$GS_CLI_DIR/docker" "$GS_RUN_ROOT" >/dev/null 2>&1; then
  run_or_skip 0 "монтирование: пути не совпали (демон пути не видит) → красное (мутант A)" \
    python3 "$TMP/gs-preflight-mutant.py" A "$GS_CLI_DIR/docker" "$GS_RUN_ROOT"
  run_or_skip 0 "монтирование: тот же путь, ДРУГОЙ каталог → красное (мутант B)" \
    python3 "$TMP/gs-preflight-mutant.py" B "$GS_CLI_DIR/docker" "$GS_RUN_ROOT" "$GS_DECOY"
else
  SKIP=$((SKIP + 1)); printf '  SKIP %-58s (CLI демона неисполним внутри контейнера)\n' \
    "монтирование: несовпадение путей → красное (мутанты A/B)"
fi
rm -rf "$GS_CLI_DIR" "$GS_DECOY"

rm -rf "$GS_RUN_ROOT"

echo "== 31. Запись об унаследованных красных (S3aq: что осталось и почему) =="

# 31.1 Запись о находке не переживает саму находку. Пока страж заземления красен,
#      запись обязана числить находку открытой; как только он зелёный — запись без
#      пометки о снятии читалась бы как актуальная, то есть как красное, которого нет.
#      Проверка в обе стороны: иначе запись либо тихо устареет, либо закроет находку
#      раньше, чем её починят.
expect_exit 0 "запись о красных: статус находки следует за стражем, а не за текстом" \
  python3 - <<'PY'
import json, pathlib, subprocess, sys
rec = json.loads(pathlib.Path("evidence/s3aq-acceptance-reds.json").read_text(encoding="utf-8"))
assert rec["schema"] == "s3aq-acceptance-reds/1", rec["schema"]
assert rec["findings"], rec
for name, f in rec["findings"].items():
    assert f.get("status") in ("open", "resolved"), (name, f.get("status"))
    assert f.get("why_not_fixed_here"), (name, "не названа причина, почему не правилось здесь")
    assert "command" in rec["tools_tests"] or "check" in f, name

guard = subprocess.run([sys.executable, "tools/check_grounded_pool.py"],
                       capture_output=True, text=True)
grounded = rec["findings"]["grounded_card_stale"]
if guard.returncode == 0:
    assert grounded["status"] == "resolved", \
        "страж заземления зелёный, а запись всё ещё числит находку открытой"
    assert grounded.get("resolved_at"), "находка снята, а когда — не записано"
else:
    assert grounded["status"] != "resolved", \
        "страж заземления красен, а запись уже числит находку снятой"
    assert grounded["root_cause"]["verify_by"], "нет команд, которыми находка воспроизводится"
print(f"запись честна: страж заземления exit {guard.returncode}, "
      f"находка числится {grounded['status']}")
PY

echo
echo "== 19. assemble_s3q_evidence.py (S3q: свод контракта) =="
# Фикстура: мини-кейс с карточкой, гейтом, пробой и историческим замером. Свод
# обязан СВЕРЯТЬ, что во всех отчётах один и тот же набор, а не склеивать их.
ASM="$TMP/asm"
mkdir -p "$ASM/data" "$ASM/evidence" "$ASM/datasets"
printf 'x' > "$ASM/datasets/general_eval.txt"
printf 'y' > "$ASM/datasets/general_eval_v2.txt"
python3 - "$ASM" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
sha = "a" * 64
(d / "data" / "gen-eval-v3-card.json").write_text(json.dumps({
    "set": {"path": "p", "sha256": sha, "bytes": 1, "docs": 200, "tokens": 1000,
            "doc_chars_min": 1, "doc_chars_max": 2, "doc_tokens_min": 3, "doc_tokens_max": 4},
    "source": {"dataset": "d", "config": "c", "split": "s", "window": {}, "license": "l"},
    "filters": {}, "selection": {}, "purity": {"rejected_candidates": 0, "scan": {}},
}), encoding="utf-8")
(d / "evidence" / "s3q-purity.json").write_text(json.dumps({
    "date": "2026-01-01", "set": {"sha256": sha},
    "instrument": {"window_size": 12}, "verdict": "clean",
    "overlap_matrix": [{"mix": "v12r", "overlap_docs": 0, "overlap_ngram": 0,
                        "composition": {}, "card": "c", "checked_corpora": ["replay"],
                        "chunk_len": 8192, "verdict": "clean"}],
}), encoding="utf-8")
(d / "evidence" / "s3q-baseline-v3.json").write_text(json.dumps({
    "datasets": {"v3_general": {"docs_kept": 200}},
    "ppl": {"v3_general": {"tokens": 800, "ppl": 7.5}},
    "instrument": {"measure": "m", "probe_revision": {"sha256": "b"}},
    "tokenizer_control": {"delta_pct": {"v3_general": 0.0}},
    "baseline": {"new_ppl": 7.5, "new_ceiling": 15.0, "new_set_sha256": sha,
                 "old_ppl": 11.93, "old_ceiling": 23.86, "old_set": "v1",
                 "v1_remeasured_ppl": 11.93, "v1_rel_delta_pct": 0.0,
                 "v1_reproduced": True},
    "comparability": {},
}), encoding="utf-8")
(d / "evidence" / "s3m-ppl-arms.json").write_text(json.dumps({
    "datasets": {"v1_general": {"sha256": "1" * 64},
                 "v2_general": {"sha256": "y" * 1}},
}), encoding="utf-8")
PY
# Хеши исходных наборов в фикстуре подставлены «не те» — свод обязан отказать.
expect_exit 1 "исходный набор не свернулся с историческим хешем — отказ" \
  python3 tools/assemble_s3q_evidence.py --case-root "$ASM" --out e.json
python3 - "$ASM" <<'PY'
import hashlib, json, sys, pathlib
d = pathlib.Path(sys.argv[1])
def h(p):
    return hashlib.sha256((d / p).read_bytes()).hexdigest()
a = json.loads((d / "evidence" / "s3m-ppl-arms.json").read_text(encoding="utf-8"))
a["datasets"] = {"v1_general": {"sha256": h("datasets/general_eval.txt")},
                 "v2_general": {"sha256": h("datasets/general_eval_v2.txt")}}
(d / "evidence" / "s3m-ppl-arms.json").write_text(json.dumps(a), encoding="utf-8")
PY
expect_exit 0 "свод собирается, когда все четыре отчёта сходятся" \
  python3 tools/assemble_s3q_evidence.py --case-root "$ASM" --out e.json
expect_exit 0 "в своде — форма leak_check из ADR-025 п.2 и нули" \
  python3 - "$ASM" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
e = json.loads((d / "e.json").read_text(encoding="utf-8"))
lc = e["leak_check"]
for field in ("dataset_sha256", "overlap_docs", "overlap_ngram_windows",
              "window_size", "verdict"):
    assert field in lc, f"в leak_check нет поля {field} (ADR-025 п.2)"
assert lc["overlap_docs"] == 0 and lc["overlap_ngram_windows"] == 0, lc
assert e["baseline"]["new_ceiling"] == 2 * e["baseline"]["new_ppl"], "потолок не 2× базы"
assert e["sources_unchanged"]["v1_general"]["unchanged"] is True
sys.exit(0)
PY
# Гейт нашёл пересечение — свод не собирается (числа набора непригодны).
python3 - "$ASM" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
p = d / "evidence" / "s3q-purity.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["verdict"] = "overlap"
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "набор не прошёл гейт — свод не собирается" \
  python3 tools/assemble_s3q_evidence.py --case-root "$ASM" --out e2.json
# Проба меряла ДРУГОЙ набор — свод обязан это заметить, а не «взять последнее».
python3 - "$ASM" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
for rel, fix in (("evidence/s3q-purity.json", lambda j: j.__setitem__("verdict", "clean")),
                 ("evidence/s3q-baseline-v3.json",
                  lambda j: j["baseline"].__setitem__("new_set_sha256", "b" * 64))):
    p = d / rel
    j = json.loads(p.read_text(encoding="utf-8"))
    fix(j)
    p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "проба меряла другой набор — отказ" \
  python3 tools/assemble_s3q_evidence.py --case-root "$ASM" --out e3.json
expect_exit 2 "нет входного отчёта — NOT-VERIFIED" \
  python3 tools/assemble_s3q_evidence.py --case-root "$TMP/asm-empty" --out e4.json

echo "== 19. check_handoff_contract.py (C-021 proposal / AD-12 / ADR-023) =="

# Фикстуры — лог прогона в формате харнесса (шапка + --- stdout --- + --- stderr ---)
# либо голый stdout (маркеров нет): сторож обязан отработать оба случая.
mkdir -p "$TMP/handoff-logs"

cat > "$TMP/handoff-logs/hr-valid.log" <<'EOF'
Харнесс 'claude-code' завершился: код 0, 942.2 с.
Контракт результата: status=complete; assumptions: 0; open_questions: 0; conflicts: 0.
--- stdout ---
Дельта доведена до конца.
## Контракт
```json
{"status": "complete", "assumptions": [], "open_questions": [], "conflicts_with_prior_decisions": []}
```
--- stderr ---
Permission allow rule …
EOF

cat > "$TMP/handoff-logs/hr-missing.log" <<'EOF'
Харнесс 'claude-code' завершился: код 0, 1308.0 с.
ВНИМАНИЕ: JSON-контракт результата (```json с полем status) в stdout не найден — ответ может быть неполным; при необходимости перезапустите с напоминанием о контракте.
--- stdout ---
Waiting on the run (v1 batch 9/10). I'll respond only when a watcher fires.
--- stderr ---
[claude-code:unrecognized_model] {"model":"flash"}
EOF

cat > "$TMP/handoff-logs/hr-invalid-json.log" <<'EOF'
Харнесс 'claude-code' завершился: код 0, 1385.2 с.
ВНИМАНИЕ: JSON-контракт найден, но НЕВАЛИДЕН по схеме.
--- stdout ---
```json
{"status": "complete",
```
--- stderr ---
EOF

cat > "$TMP/handoff-logs/hr-invalid-status.log" <<'EOF'
--- stdout ---
```json
{"status": "done"}
```
EOF

cat > "$TMP/handoff-logs/hr-bare.log" <<'EOF'
проза ответа
{"status": "blocked", "open_questions": ["нужен доступ к КШД"]}
EOF

# 19.1 Три обязательных случая: контракт есть / нет / есть, но невалидный JSON.
expect_exit 0 "контракт есть (valid) — сторож зелёный" \
  python3 tools/check_handoff_contract.py "$TMP/handoff-logs/hr-valid.log"
expect_contains "status=complete" "сторож называет извлечённый статус" \
  python3 tools/check_handoff_contract.py "$TMP/handoff-logs/hr-valid.log"
expect_exit 1 "контракта нет — сторож падает (missing)" \
  python3 tools/check_handoff_contract.py "$TMP/handoff-logs/hr-missing.log"
expect_contains "контракт не найден" "missing назван явно, с выжимкой stdout" \
  python3 tools/check_handoff_contract.py "$TMP/handoff-logs/hr-missing.log"
expect_exit 1 "контракт есть, но невалидный JSON — сторож падает (invalid)" \
  python3 tools/check_handoff_contract.py "$TMP/handoff-logs/hr-invalid-json.log"
expect_contains "невалидный JSON" "битый JSON назван причиной invalid" \
  python3 tools/check_handoff_contract.py "$TMP/handoff-logs/hr-invalid-json.log"

# 19.2 Схема: status вне перечисления — тоже не зелёный (реальный hr-14).
expect_exit 1 "status вне complete|partial|blocked — invalid" \
  python3 tools/check_handoff_contract.py "$TMP/handoff-logs/hr-invalid-status.log"
expect_contains "вне complete|partial|blocked" "причина схемы названа" \
  python3 tools/check_handoff_contract.py "$TMP/handoff-logs/hr-invalid-status.log"

# 19.3 Голый JSON в хвосте (fence уронен) — валидный контракт; blocked тоже контракт.
expect_exit 0 "голый JSON-объект в хвосте — valid (blocked)" \
  python3 tools/check_handoff_contract.py "$TMP/handoff-logs/hr-bare.log"

# 19.4 NOT-VERIFIED: нет входа / файла нет — «не зелёный», а не «чисто».
expect_exit 2 "без входа — NOT-VERIFIED" \
  python3 tools/check_handoff_contract.py
expect_exit 2 "файла нет — NOT-VERIFIED" \
  python3 tools/check_handoff_contract.py "$TMP/handoff-logs/no-such.log"

# 19.5 Выбор логов из каталога: --log-dir + --latest берёт свежайший по mtime.
# Свежайший — hr-missing → --latest 1 падает; переставив свежайшим hr-valid — зелёный.
touch "$TMP/handoff-logs/hr-missing.log"
expect_exit 1 "--latest 1 по каталогу берёт свежайший (missing)" \
  python3 tools/check_handoff_contract.py --log-dir "$TMP/handoff-logs" --glob 'hr-*.log' --latest 1
sleep 1
touch "$TMP/handoff-logs/hr-valid.log"
expect_exit 0 "--latest 1 после перестановки свежайшего (valid)" \
  python3 tools/check_handoff_contract.py --log-dir "$TMP/handoff-logs" --glob 'hr-*.log' --latest 1

echo
echo "== 20. build_general_eval_k2.py (S3t: компонента K2 — набор вне распределения) =="
expect_exit 0 "план печатается без источника и без токенизатора" \
  python3 tools/build_general_eval_k2.py --plan
# ADR-027: компонента K2 идёт ОТДЕЛЬНЫМ файлом. Запрет по имени срабатывает
# раньше любой работы — ни токенизатор, ни источник не нужны.
expect_exit 1 "набор ревизии не перезаписывается ни при каких флагах" \
  python3 tools/build_general_eval_k2.py --out datasets/general_eval.txt --force
expect_exit 1 "и компонента K1 тоже: запрет по имени, а не по хешу" \
  python3 tools/build_general_eval_k2.py --out datasets/general_eval_v3.txt --force
expect_exit 2 "нет источника K2 — NOT-VERIFIED, а не набор из меньшего пула" \
  python3 tools/build_general_eval_k2.py --source "$TMP/nope.txt" --out "$TMP/k2_never.txt"
printf 'Текст источника для проверки отсутствия корпуса. %s\n' "$(seq 1 40)" > "$TMP/k2_src.txt"
expect_exit 2 "нет корпуса обучения — NOT-VERIFIED" \
  python3 tools/build_general_eval_k2.py --source "$TMP/k2_src.txt" \
  --corpus "replay=$TMP/nope.txt" --out "$TMP/k2_never2.txt"
expect_exit 0 "нарезка: перенос строки снимается, строки соединяются, фильтры различают дефекты" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import build_general_eval_k2 as B
# Нарезка: выгрузка из PDF жёстко переносит строки; измерять вёрстку, а не язык, нельзя.
assert B.to_document("государствен-\nный аудит") == "государственный аудит", "перенос не снят"
assert B.to_document("обоснован‐\nным") == "обоснованным", "U+2010 (HYPHEN) не снят"
assert B.to_document("а\nб") == "а б", "строки не соединены пробелом"
assert B.to_document("а  \n  б") == "а б", "пробелы не схлопнуты"
# Фильтры: каждый дефект ловится своим правилом, а не «первым подходящим».
good = "Это обычный текст предложения. " * 40
assert B.prose_reject(good) is None, "проза отсеяна"
assert B.prose_reject("я" * (B.PROSE_MIN_CHARS - 1)) == "short", "короткий блок прошёл"
assert B.prose_reject("слово " * 400) == "not_prose_sentences", "список без концов предложений прошёл"
assert B.prose_reject("1. 2. 3. " * 300) == "not_prose_alpha", "цифровая таблица прошла"
assert B.prose_reject("This is an English sentence. " * 40) == "not_russian", \
    "англоязычный блок (оглавление выгрузки) прошёл"
glued = "чтовыручкаотреализациитоваров " * 8 + good
assert B.prose_reject(glued) == "ocr_glued_words", "сбой извлечения текста из PDF прошёл"
assert B.prose_reject(good + "\n---\n" + good) == "doc_sep_inside", \
    "разделитель внутри документа обязан отсекаться (иначе набор распадётся при чтении)"
sys.exit(0)
PY
expect_exit 0 "отбор: детерминирован по сиду и выбрасывает пересечения с корпусами обучения" \
  python3 - "$TMP" <<'PY'
import sys, pathlib
sys.path.insert(0, "tools")
import build_general_eval_k2 as B

class Tok:
    """Заглушка токенизатора: токен на каждые 4 символа — как у русского текста."""
    def encode(self, text, add_special_tokens=False):
        return list(range(len(text) // 4))

tmp = pathlib.Path(sys.argv[1]) / "k2pur"
tmp.mkdir(exist_ok=True)
body = "Это длинный документ аудиторской прозы. " * 40
# Корпус обучения — ТОТ ЖЕ текст, что у «грязных» кандидатов: иначе проверялось бы
# не совпадение, а его отсутствие.
corpus_doc = "Это предложение обучающего корпуса. " * 40
filler = "Совсем другой текст доменного корпуса. " * 40
(tmp / "replay.txt").write_text(corpus_doc + "\n\n" + filler + "\n", encoding="utf-8")
(tmp / "domain.txt").write_text(filler + "\n---\n" + filler + "\n", encoding="utf-8")
corpora = {"replay": tmp / "replay.txt", "domain": tmp / "domain.txt"}

def cand(cid, text):
    return {"id": cid, "text": text, "chars": len(text), "tokens": len(text) // 4,
            "norm": B.norm(text)}

rows = [cand(i, body + f" Тема номер {i}.") for i in range(40)]
rows.append(cand(1000, corpus_doc))                     # документ целиком из корпуса
parts = corpus_doc.split(" ")
# Вставка ВНУТРЬ документа: целого документа в корпусе уже нет, а 12-граммовые окна
# по обе стороны вставки остаются общими — это и проверяет второе условие.
rows.append(cand(1001, " ".join(parts[:20]) + " вставка " + " ".join(parts[20:])))
docs1, sel1 = B.select(list(rows), corpora, 10, 7, len(rows))
docs2, _ = B.select(list(rows), corpora, 10, 7, len(rows))
assert [d["id"] for d in docs1] == [d["id"] for d in docs2], "отбор не детерминирован"
assert len(docs1) == 10, f"выбрано {len(docs1)} вместо 10"
ids = [d["id"] for d in docs1]
assert 1000 not in ids, "документ, найденный в корпусе целиком, попал в набор"
assert 1001 not in ids, "кандидат, общий с корпусом по 12-граммовым окнам, попал в набор"
assert sel1["rejected_by_purity"] == 2, sel1["rejected_by_purity"]
assert ids == sorted(ids), "записи не отсортированы по id (порядок источника)"
# Сид меняет СОСТАВ, но не правило: файл набора воспроизводим по составу.
docs3, _ = B.select(list(rows), corpora, 10, 8, len(rows))
assert [d["id"] for d in docs3] == sorted(d["id"] for d in docs3), "записи не отсортированы"
assert {d["id"] for d in docs1} != {d["id"] for d in docs3}, "другой сид дал тот же состав"
# Пул меньше объявленного окна проверки — отказ, а не проверка «по чему получилось».
try:
    B.select(list(rows), corpora, 10, 7, 9999)
except RuntimeError as e:
    assert "NOT-VERIFIED" in str(e), str(e)
else:
    raise AssertionError("нехватка пула не названа отказом")
sys.exit(0)
PY
expect_exit 0 "verify принимает собранный набор K2 и считает документы/токены" \
  python3 tools/build_general_eval_k2.py --verify datasets/general_eval_k2.txt
expect_exit 1 "verify отказывает набору K1 при заявке в 200 документов K2" \
  python3 tools/build_general_eval_k2.py --verify datasets/general_eval.txt
expect_exit 2 "verify на отсутствующем файле — NOT-VERIFIED" \
  python3 tools/build_general_eval_k2.py --verify "$TMP/nope.txt"

echo "== 21. ppl_probe_k2.py (S3t: база и потолок K2 в одном прогоне с эталонами) =="
expect_exit 0 "модуль импортируется без torch/transformers (план проверяется без GPU)" \
  python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
import ppl_probe_k2 as PK
assert "torch" not in sys.modules, "import ppl_probe_k2 потянул torch"
assert "transformers" not in sys.modules, "import ppl_probe_k2 потянул transformers"
# Компонента, для которой снимается база, — K2; контрольные наборы названы явно.
assert PK.COMPONENT == "k2_general", PK.COMPONENT
assert set(PK.REFERENCES) == {"v1_general", "v3_general"}, PK.REFERENCES
assert PK.SETS[PK.COMPONENT].endswith("general_eval_k2.txt")
sys.exit(0)
PY
expect_exit 0 "план печатается без модели и называет оба эталона" \
  python3 tools/ppl_probe_k2.py --plan
expect_exit 2 "эталоны читаются из evidence, а не из памяти инструмента: нет файла — NOT-VERIFIED" \
  python3 tools/ppl_probe_k2.py --case-root "$TMP/probe-empty" --out "$TMP/never.json"

echo "== 22. assemble_s3t_evidence.py (S3t: свод двухкомпонентного контракта) =="
# Фикстура: мини-кейс с карточкой K2, гейтом, пробой и отчётами рук. Свод обязан
# СВЕРЯТЬ, что во всех отчётах один и тот же набор, и считать конъюнкцию сам.
AS2="$TMP/asm2"
mkdir -p "$AS2/data" "$AS2/evidence" "$AS2/datasets" "$AS2/runs/s3t-arms-20260916"
python3 - "$AS2" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
sha = "a" * 64
(d / "datasets" / "general_eval.txt").write_text("x", encoding="utf-8")
(d / "datasets" / "general_eval_v2.txt").write_text("y", encoding="utf-8")
(d / "datasets" / "general_eval_v3.txt").write_text("z", encoding="utf-8")
(d / "datasets" / "general_replay_ru.txt").write_text("r", encoding="utf-8")
(d / "data" / "gen-eval-k2-card.json").write_text(json.dumps({
    "set": {"path": "p", "sha256": sha, "bytes": 1, "docs": 200, "tokens": 1000,
            "doc_chars_min": 1, "doc_chars_max": 2, "doc_tokens_min": 3, "doc_tokens_max": 4},
    "source": {"path": "s", "sha256": "s", "provenance": "p", "genre": "g",
               "language": "ru", "distribution_relation": "out-of-distribution",
               "distribution_why": "w", "license_status": "internal_only",
               "license_note": "n"},
    "segmentation": {}, "filters": {}, "purity": {}, "selection": {},
}), encoding="utf-8")
(d / "evidence" / "s3t-purity-k2.json").write_text(json.dumps({
    "date": "2026-01-01", "set": {"sha256": sha},
    "instrument": {"window_size": 12}, "verdict": "clean",
    "overlap_matrix": [{"mix": "v12r", "overlap_docs": 0, "overlap_ngram": 0,
                        "composition": {}, "card": "c", "checked_corpora": ["replay"],
                        "chunk_len": 8192, "verdict": "clean"}],
}), encoding="utf-8")
# Обратная проверка гейта: он обязан РАЗЛИЧАТЬ случаи, а не «всегда быть чистым».
# v2 собран из источника реплея — пересечение по всем документам; v3 (K1) — нули.
(d / "evidence" / "s3t-purity-v2.json").write_text(json.dumps({
    "date": "2026-01-01", "set": {"sha256": "2" * 64, "docs": 200}, "verdict": "overlap",
    "instrument": {"window_size": 12},
    "overlap_matrix": [{"mix": "v12r", "overlap_docs": 200, "overlap_ngram": 12876,
                        "composition": {}, "card": "c", "checked_corpora": ["replay"],
                        "chunk_len": 8192, "verdict": "overlap"}],
}), encoding="utf-8")
(d / "evidence" / "s3q-purity.json").write_text(json.dumps({
    "date": "2026-01-01", "set": {"sha256": "3" * 64, "docs": 200}, "verdict": "clean",
    "instrument": {"window_size": 12},
    "overlap_matrix": [{"mix": "v12r", "overlap_docs": 0, "overlap_ngram": 0,
                        "composition": {}, "card": "c", "checked_corpora": ["replay"],
                        "chunk_len": 8192, "verdict": "clean"}],
}), encoding="utf-8")
(d / "evidence" / "s3t-baseline-k2.json").write_text(json.dumps({
    "instrument": {"measure": "m", "probe_revision": {"sha256": "b"}},
    "component": {"set_sha256": sha, "ppl": 20.0, "ceiling": 40.0, "tokens": 800,
                  "docs_counted": 200, "doc_ppl": {"median": 1.0}},
    "peer_components": {
        "K1": {"set": "v3_general", "set_sha256": "z" * 4, "ppl": 7.5, "ceiling": 15.0},
        "historical": {"set": "v1_general", "set_sha256": "1" * 4, "ppl": 11.93,
                       "ceiling": 23.86}},
    "instrument_identity": {
        "what": "w", "tolerance_pct": 5.0, "reproduced": True,
        "checks": {"v1_general": {"ppl_measured": 11.931924, "ppl_historical": 11.931924,
                                  "rel_delta_pct": 0.0},
                   "v3_general": {"ppl_measured": 7.504686, "ppl_historical": 7.504686,
                                  "rel_delta_pct": 0.0}}},
}), encoding="utf-8")
(d / "evidence" / "s3m-ppl-arms.json").write_text(json.dumps({
    "datasets": {"v1_general": {"sha256": "?"}, "v2_general": {"sha256": "?"}},
}), encoding="utf-8")
(d / "evidence" / "s3q-eval-set.json").write_text(json.dumps({
    "set": {"sha256": "?"},
    "build": {"purity_scan": {"replay": {"sha256": "?"}}},
}), encoding="utf-8")
def arm(name, k1_final, k2_final):
    return {"instrument": {"pipeline": f"runs/calib-{name}-20260916-0820/laguna_pipeline_calib.py"},
            "states": {"base": {"sets": {"v3_general": {"ppl": 7.5}, "k2_general": {"ppl": 20.0}}},
                       "final": {"load": {"checkpoint": "c"},
                                 "sets": {"v3_general": {"ppl": k1_final},
                                          "k2_general": {"ppl": k2_final}}}}}
# 25-0.35: K1 в потолке (10 ≤ 15), K2 вне (50 > 40) — «жанр держит, обобщение потеряно».
(d / "runs" / "s3t-arms-20260916" / "25-0.35.json").write_text(
    json.dumps(arm("25-0.35", 10.0, 50.0)), encoding="utf-8")
# 50-0.35: обе компоненты в потолке.
(d / "runs" / "s3t-arms-20260916" / "50-0.35.json").write_text(
    json.dumps(arm("50-0.35", 9.0, 30.0)), encoding="utf-8")
PY
expect_exit 1 "наборы ревизии не свернулись с историческими хешами — отказ" \
  python3 tools/assemble_s3t_evidence.py --case-root "$AS2" --out e.json
python3 - "$AS2" <<'PY'
import hashlib, json, sys, pathlib
d = pathlib.Path(sys.argv[1])
def h(p):
    return hashlib.sha256((d / p).read_bytes()).hexdigest()
a = json.loads((d / "evidence" / "s3m-ppl-arms.json").read_text(encoding="utf-8"))
a["datasets"] = {"v1_general": {"sha256": h("datasets/general_eval.txt")},
                 "v2_general": {"sha256": h("datasets/general_eval_v2.txt")}}
(d / "evidence" / "s3m-ppl-arms.json").write_text(json.dumps(a), encoding="utf-8")
q = json.loads((d / "evidence" / "s3q-eval-set.json").read_text(encoding="utf-8"))
q["set"] = {"sha256": h("datasets/general_eval_v3.txt")}
q["build"] = {"purity_scan": {"replay": {"sha256": h("datasets/general_replay_ru.txt")}}}
(d / "evidence" / "s3q-eval-set.json").write_text(json.dumps(q), encoding="utf-8")
PY
expect_exit 0 "свод собирается, когда все отчёты сходятся" \
  python3 tools/assemble_s3t_evidence.py --case-root "$AS2" --out e.json
expect_exit 0 "конъюнкция считается сводом: рука, прошедшая K1 и провалившая K2, не проходит" \
  python3 - "$AS2" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
e = json.loads((d / "e.json").read_text(encoding="utf-8"))
lc = e["leak_check"]
for field in ("dataset_sha256", "overlap_docs", "overlap_ngram_windows",
              "window_size", "verdict"):
    assert field in lc, f"в leak_check нет поля {field} (ADR-025 п.2)"
assert e["k2_baseline"]["ceiling"] == 2 * e["k2_baseline"]["ppl"], "потолок K2 не 2× базы"
assert e["k2"]["distribution_relation"] == "out-of-distribution", "отношение к распределению не названо"
assert e["k2"]["license_status"] == "internal_only", "лицензионный статус не назван"
assert set(e["instrument_identity"]) >= {"v1_ppl", "v3_ppl"}, e["instrument_identity"].keys()
assert e["untouched_check"]["v1_general"]["unchanged"] is True
# Обратная проверка: числа берутся из отчётов, а не пересказываются в своде.
assert e["reverse_gate"]["v2_general"]["as_expected"] is True, e["reverse_gate"]["v2_general"]
assert e["reverse_gate"]["v2_general"]["worst_overlap_docs"] == 200
assert e["reverse_gate"]["v3_general"]["as_expected"] is True
assert e["reverse_gate"]["v3_general"]["worst_overlap_ngram"] == 0
arms = {a["arm"]: a for a in e["arms"]["arms"]}
a1 = arms["25-0.35"]
assert a1["k1_pass"] is True and a1["k2_pass"] is False, a1
assert a1["conjunction"] is False, "проход по одной компоненте зачтён как проход стадии"
assert "обобщение потеряно" in a1["diagnosis"], a1["diagnosis"]
a2 = arms["50-0.35"]
assert a2["k1_pass"] and a2["k2_pass"] and a2["conjunction"], a2
assert e["arms"]["arms_passing_conjunction"] == ["50-0.35"], e["arms"]["arms_passing_conjunction"]
assert e["arms"]["control_arms_missing"], "незакрытые контрольные руки не названы"
assert "не усредняются" in e["arms"]["not_averaged"]
sys.exit(0)
PY
# Прибор не воспроизвёл эталон — сравнивать компоненты нечем, свод не собирается.
python3 - "$AS2" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1]) / "evidence" / "s3t-baseline-k2.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["instrument_identity"]["reproduced"] = False
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "эталон прибора не воспроизведён — свод не собирается" \
  python3 tools/assemble_s3t_evidence.py --case-root "$AS2" --out e2.json
python3 - "$AS2" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
for rel, fix in (
    ("evidence/s3t-baseline-k2.json",
     lambda j: (j["instrument_identity"].__setitem__("reproduced", True),
                j["component"].__setitem__("set_sha256", "b" * 64))),
    ("evidence/s3t-purity-k2.json", lambda j: j.__setitem__("verdict", "clean")),
):
    p = d / rel
    j = json.loads(p.read_text(encoding="utf-8"))
    fix(j)
    p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "проба меряла другой набор — отказ" \
  python3 tools/assemble_s3t_evidence.py --case-root "$AS2" --out e3.json
# Гейт нашёл пересечение — числа набора непригодны для решения о стадии.
python3 - "$AS2" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
p = d / "evidence" / "s3t-purity-k2.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["verdict"] = "overlap"
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "набор K2 не прошёл гейт — свод не собирается" \
  python3 tools/assemble_s3t_evidence.py --case-root "$AS2" --out e4.json
expect_exit 2 "нет входного отчёта — NOT-VERIFIED" \
  python3 tools/assemble_s3t_evidence.py --case-root "$TMP/asm-empty" --out e5.json

echo "== 23. assemble_s3v_evidence.py (S3v: PPL контрольных рук по обеим компонентам) =="
# Фикстура: мини-кейс с тремя эталонами и двумя отчётами прибора. Свод обязан
# САМ считать отношения, проходы и конъюнкцию, и обязан отказать, если отчёт
# прибора не воспроизвёл эталон (тогда «тот же прибор» — не доказано).
AS3="$TMP/asm3"
mkdir -p "$AS3/evidence" "$AS3/runs/s3v-arms-20260916" \
         "$AS3/stand/ctrl-C1-100-0.35-20260916-1632" \
         "$AS3/stand/ctrl-C2-25-0.035-20260916-1632"
python3 - "$AS3" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
# Параметры прогонов рук: из них берётся шаг финальной точки и проверяется, что
# рука в таблице не перепутана (replay_share_pct / peak_lr_scale).
for run, replay, lr in (("ctrl-C1-100-0.35-20260916-1632", 100, 0.35),
                        ("ctrl-C2-25-0.035-20260916-1632", 25, 0.035)):
    (d / "stand" / run / "calib_params.json").write_text(json.dumps({
        "cpt_steps": 2000, "seed": 42, "replay_share_pct": replay,
        "peak_lr_scale": lr}), encoding="utf-8")
REF = {"v1_general": (12.0, "1" * 64), "v3_general": (7.5, "3" * 64),
       "k2_general": (6.0, "2" * 64)}
(d / "evidence" / "s3q-baseline-v3.json").write_text(json.dumps({
    "datasets": {"v3_general": {"sha256": REF["v3_general"][1]}},
    "ppl": {"v3_general": {"ppl": 7.5}}, "baseline": {"new_ceiling": 15.0},
}), encoding="utf-8")
(d / "evidence" / "s3t-baseline-k2.json").write_text(json.dumps({
    "datasets": {"k2_general": {"sha256": REF["k2_general"][1]}},
    "ppl": {"k2_general": {"ppl": 6.0}}, "component": {"ceiling": 12.0},
}), encoding="utf-8")
(d / "evidence" / "ppl-baseline-v1v2.json").write_text(json.dumps({
    "datasets": {"v1_general": {"sha256": REF["v1_general"][1]}},
    "ppl": {"v1_general": {"ppl": 12.0}},
}), encoding="utf-8")
(d / "evidence" / "s3t-k2-set.json").write_text(json.dumps({
    "arms": {"arms": [{"arm": "25-0.35", "k1_ppl": 11.4, "k1_ratio": 1.52,
                       "k1_pass": True, "k2_ppl": 10.7, "k2_ratio": 1.78,
                       "k2_pass": True}]},
}), encoding="utf-8")
(d / "evidence" / "s3m-ppl-arms.json").write_text(json.dumps({
    "arms": [{"arm": "25-0.35", "ppl_general": 57.0, "ratio_vs_base": 4.77}],
    "loss_trajectory_criterion_a": {"25-0.35": {"delta": -0.38, "decreasing": True}},
}), encoding="utf-8")
def report(v1_base, v3_base, k2_base, *points):
    # points: (step, v1, v3, k2) — по точке на каждый чекпойнт руки.
    def sets(v1, v3, k2):
        return {"v1_general": {"ppl": v1}, "v3_general": {"ppl": v3},
                "k2_general": {"ppl": k2}}
    states = {"base": {"load": {"kind": "base"}, "sets": sets(v1_base, v3_base, k2_base)}}
    for step, v1, v3, k2 in points:
        name = "cfinal" if step == 2000 else f"c{step}"
        states[name] = {"load": {"kind": "ckpt",
                                 "checkpoint": f"/nonexistent/ckpt_{step}.pt"},
                        "sets": sets(v1, v3, k2)}
    return {"schema": "calib-ppl-probe/1", "complete": True,
            "instrument": {"pipeline_sha256": "p" * 64},
            "sets": {n: {"path": f"datasets/{n}.txt", "sha256": s, "docs_kept": 1}
                     for n, (_, s) in REF.items()},
            "states": states, "checks": []}
# C1: на 500-м шаге K2 за потолком (14 > 12), K1 в потолке (8 ≤ 15) — конъюнкция
# провалена; к финалу обе компоненты в потолке (9 ≤ 15, 8 ≤ 12) — конъюнкция пройдена;
# между 500 и финалом потолок не пробит ни одной компонентой.
(d / "runs" / "s3v-arms-20260916" / "ctrl-C1-100-0.35.json").write_text(
    json.dumps(report(12.0, 7.5, 6.0,
                      (500, 18.0, 8.0, 14.0), (1000, 15.0, 7.8, 11.5),
                      (1500, 13.0, 7.6, 9.0), (2000, 11.0, 9.0, 8.0))), encoding="utf-8")
# C2: ни на одной точке потолок не пробит — «низкий LR язык не сдвигает».
(d / "runs" / "s3v-arms-20260916" / "ctrl-C2-25-0.035.json").write_text(
    json.dumps(report(12.0, 7.5, 6.0,
                      (500, 12.0, 7.6, 6.1), (1000, 12.0, 7.6, 6.15),
                      (1500, 12.0, 7.55, 6.1), (2000, 12.0, 7.5, 6.2))), encoding="utf-8")
PY
expect_exit 0 "тождество прибора доказано, конъюнкция посчитана" \
  python3 tools/assemble_s3v_evidence.py --case-root "$AS3" --stand-calib "$AS3/stand" --out "$AS3/e.json"
python3 - "$AS3/e.json" "$AS3" <<'PY'
import json, sys
e = json.load(open(sys.argv[1]))
assert e["instrument_identity"]["reproduced"], "тождество не воспроизведено"
assert e["instrument_identity"]["worst_abs_delta"] == 0.0, e["instrument_identity"]
by = {(r["arm"], r["step"]): r for r in e["arms"]}
c1_500 = by[("ctrl-C1-100-0.35", 500)]
assert c1_500["k1_pass"] is True and c1_500["k2_pass"] is False, c1_500
assert c1_500["conjunction"] is False, "проход по одной компоненте зачтён как проход"
assert "обобщение потеряно" in c1_500["diagnosis"], c1_500["diagnosis"]
c1_fin = by[("ctrl-C1-100-0.35", 2000)]
assert c1_fin["conjunction"] is True, c1_fin
c2 = [r for r in e["arms"] if r["arm"] == "ctrl-C2-25-0.035" and r["step"] > 0]
assert all(r["conjunction"] for r in c2), "низкий LR должен проходить на всех точках"
# Первый пересечённый шаг — из чисел, а не из глаз; потолки — 2× баз эталонов.
assert e["curves"]["ctrl-C1-100-0.35"]["k1"]["first_crossing_step"] is None
assert e["curves"]["ctrl-C1-100-0.35"]["k2"]["first_crossing_step"] == 500
assert abs(e["baselines"]["k1"]["ceiling_ppl"] - 15.0) < 1e-9, e["baselines"]["k1"]
assert abs(e["baselines"]["k2"]["ceiling_ppl"] - 12.0) < 1e-9, e["baselines"]["k2"]
assert e["baselines"]["v1"]["used_for_decision"] is False, "потолок v1 назван решающим"
assert "не усредняются" in e["not_averaged"], e["not_averaged"]
# Вердикт отвечает на все три вопроса контракта, и каждый — с числами.
for q in ("protocol_destroys_language", "low_lr_effect", "consistent_with_s3n"):
    assert q in e["verdict"] and e["verdict"][q].get("why"), q
# Сравнение по LR: чужое число читается из чужого свода, а не измеряется заново.
assert e["lr_comparison"]["peer"]["k1_final"]["ratio"] == 1.52, e["lr_comparison"]
assert "v1" in e["lr_comparison"]["ratio_of_ratios"], e["lr_comparison"]
# Манифест AD-2 у каталога пробы: без него правило C-012 красное.
m = json.load(open(sys.argv[2] + "/runs/s3v-arms-20260916/run_manifest.json"))
assert m["pipeline_complete"] is True and m["stages"], m
assert not m["pipeline_path"].startswith("/"), m["pipeline_path"]
sys.exit(0)
PY
# Проба не воспроизвела эталон: «тот же прибор» не доказано — свод не собирается.
python3 - "$AS3" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1]) / "runs" / "s3v-arms-20260916" / "ctrl-C1-100-0.35.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["states"]["base"]["sets"]["v3_general"]["ppl"] = 7.9   # +5 % — при пороге 1e-4
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "эталон прибора не воспроизведён — отказ" \
  python3 tools/assemble_s3v_evidence.py --case-root "$AS3" --stand-calib "$AS3/stand" --out e2.json
python3 - "$AS3" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1]) / "runs" / "s3v-arms-20260916" / "ctrl-C1-100-0.35.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["states"]["base"]["sets"]["v3_general"]["ppl"] = 7.5   # вернули
j["sets"]["k2_general"]["sha256"] = "f" * 64             # набор подменён
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "проба мерила другой набор — отказ" \
  python3 tools/assemble_s3v_evidence.py --case-root "$AS3" --stand-calib "$AS3/stand" --out e3.json
python3 - "$AS3" <<'PY'
import json, sys, pathlib
# База руки не совпала с базой эталона: отношение считалось бы от другой шкалы.
p = pathlib.Path(sys.argv[1]) / "runs" / "s3v-arms-20260916" / "ctrl-C2-25-0.035.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["sets"]["k2_general"]["sha256"] = "2" * 64
j["states"]["base"]["sets"]["k2_general"]["ppl"] = 6.03
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "база руки разошлась с базой эталона — отказ" \
  python3 tools/assemble_s3v_evidence.py --case-root "$AS3" --stand-calib "$AS3/stand" --out e4.json
python3 - "$AS3" <<'PY'
import json, sys, pathlib
# Потолок, записанный в эталоне, перестал быть 2× базы: считать проход не по чему.
p = pathlib.Path(sys.argv[1]) / "evidence" / "s3t-baseline-k2.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["component"]["ceiling"] = 13.0
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "вычисленный потолок разошёлся с записанным в эталоне — отказ" \
  python3 tools/assemble_s3v_evidence.py --case-root "$AS3" --stand-calib "$AS3/stand" --out e5.json
expect_exit 2 "нет отчёта прибора — NOT-VERIFIED" \
  python3 tools/assemble_s3v_evidence.py --case-root "$TMP/asm-empty" --out e6.json

echo "== 24. assemble_s3w_evidence.py (S3w: домен-метрика по всем рукам) =="
# Фикстура: мини-кейс с тремя эталонами языка/домена, шестью чужими сводами
# (S3m/S3t/S3v), двумя отчётами гейта чистоты и шестью отчётами прибора.
# Числа заданы в одном месте и из них же порождаются чужие отчёты, поэтому
# сверка «своё против чужого» проверяет логику свода, а не совпадение копий.
AS4="$TMP/asm4"
mkdir -p "$AS4"
cat > "$TMP/mk_s3w_fixture.py" <<'FIXTURE'
import json, pathlib, sys
D = pathlib.Path(sys.argv[1])
HIST = {"v1_general": 12.0, "v1_domain": 9.0, "v2_domain": 11.0,
        "v3_general": 7.5, "k2_general": 6.0}
SET_SHA = {"v1_general": "1" * 64, "v1_domain": "2" * 64, "v2_domain": "3" * 64,
           "v3_general": "4" * 64, "k2_general": "5" * 64}
ORDER = ("v2_domain", "v1_domain", "v1_general", "v3_general", "k2_general")
PIPE = {"calib-25-0.35": "a" * 64, "calib-25-0.7": "b" * 64,
        "calib-50-0.35": "a" * 64, "calib-50-0.7": "a" * 64,
        "ctrl-C1-100-0.35": "a" * 64, "ctrl-C2-25-0.035": "a" * 64}
PARAMS = {"calib-25-0.35": (25, 0.35, "calib-25-0.35-20260916-0820"),
          "calib-25-0.7": (25, 0.7, "calib-25-0.7-20260916-0820"),
          "calib-50-0.35": (50, 0.35, "calib-50-0.35-20260916-0820"),
          "calib-50-0.7": (50, 0.7, "calib-50-0.7-20260916-0820"),
          "ctrl-C1-100-0.35": (100, 0.35, "ctrl-C1-100-0.35-20260916-1632"),
          "ctrl-C2-25-0.035": (25, 0.035, "ctrl-C2-25-0.035-20260916-1632")}
# (v2_domain, v1_domain, v1_general, v3_general, k2_general)
TRAJ = {
    "calib-25-0.35": {500: (7.0, 7.4, 66.76, 18.0, 14.5), 1000: (6.1, 6.6, 74.42, 16.0, 13.2),
                      1500: (5.2, 5.6, 68.33, 13.0, 11.5), 2000: (4.35, 4.85, 56.77, 11.41, 10.75)},
    "calib-25-0.7": {1000: (7.2, 7.8, 130.0, 24.0, 22.0), 1500: (6.1, 6.8, 120.0, 19.0, 18.0),
                     2000: (5.14, 5.90, 113.08, 15.61, 14.98)},
    "calib-50-0.35": {500: (7.2, 7.5, 70.0, 16.0, 15.0), 1000: (6.0, 6.4, 68.0, 13.0, 12.5),
                      1500: (5.1, 5.5, 60.0, 11.0, 10.5), 2000: (4.48, 4.97, 55.27, 9.74, 9.10)},
    "calib-50-0.7": {500: (7.9, 8.2, 120.0, 21.0, 20.0), 1000: (6.6, 7.0, 115.0, 17.0, 16.0),
                     1500: (5.9, 6.5, 108.0, 14.0, 13.0), 2000: (5.41, 6.20, 102.08, 12.34, 11.20)},
    # C1 училась без домена: домен обязан **расти** — вакуумный контроль набора.
    "ctrl-C1-100-0.35": {500: (14.0, 11.5, 66.76, 13.87, 12.53),
                         1000: (13.0, 10.5, 110.13, 12.61, 11.26),
                         1500: (12.2, 10.0, 86.68, 10.60, 9.70),
                         2000: (11.6, 9.6, 71.42, 9.34, 8.62)},
    # C2: домен взят (отношение 0.70 против 0.395 у 25-0.35) — в полосе ×2.
    "ctrl-C2-25-0.035": {500: (9.8, 8.6, 11.80, 7.61, 6.43), 1000: (9.2, 8.2, 11.89, 7.61, 6.49),
                         1500: (8.4, 7.6, 11.85, 7.55, 6.49),
                         2000: (7.7, 7.2, 11.81, 7.54, 6.48)},
}
BASEV = tuple(HIST[n] for n in ORDER)
def sets_of(vals):
    return {n: {"ppl": v} for n, v in zip(ORDER, vals)}
def report(arm):
    states = {"base": {"load": {"kind": "base"}, "sets": sets_of(BASEV)},
              "base_untouched": {"load": {"kind": "base"}, "sets": sets_of(BASEV)}}
    for step in sorted(TRAJ[arm]):
        name = "cfinal" if step == 2000 else f"c{step}"
        states[name] = {"load": {"kind": "ckpt", "checkpoint": f"/nowhere/{arm}_{step}.pt",
                                 "resized_embeddings": False}, "sets": sets_of(TRAJ[arm][step])}
    return {"schema": "calib-ppl-probe/1", "complete": True,
            "instrument": {"pipeline_sha256": PIPE[arm], "load_checkpoint": "_load_ckpt_with_resize"},
            "sets": {n: {"path": f"datasets/{n}.txt", "sha256": s, "docs_kept": 1}
                     for n, s in SET_SHA.items()},
            "states": states, "checks": []}
def main():
    for sub in ("evidence", "runs/s3w-domain-20260916", "stand"):
        (D / sub).mkdir(parents=True, exist_ok=True)
    (D / "evidence" / "ppl-baseline-v1v2.json").write_text(json.dumps({
        "datasets": {n: {"sha256": SET_SHA[n]} for n in ("v1_general", "v1_domain", "v2_domain")},
        "ppl": {n: {"ppl": HIST[n], "docs": 200, "tokens": 125322}
                for n in ("v1_general", "v1_domain", "v2_domain")}}), encoding="utf-8")
    (D / "evidence" / "s3q-baseline-v3.json").write_text(json.dumps({
        "datasets": {"v3_general": {"sha256": SET_SHA["v3_general"]}},
        "ppl": {"v3_general": {"ppl": HIST["v3_general"]}},
        "baseline": {"new_ceiling": 15.0}}), encoding="utf-8")
    (D / "evidence" / "s3t-baseline-k2.json").write_text(json.dumps({
        "datasets": {"k2_general": {"sha256": SET_SHA["k2_general"]}},
        "ppl": {"k2_general": {"ppl": HIST["k2_general"]}},
        "component": {"ceiling": 12.0}}), encoding="utf-8")
    calib = [a for a in TRAJ if a.startswith("calib")]
    # Чужие своды S3m/S3t зовут калибровочные руки БЕЗ префикса `calib-`: фикстура
    # воспроизводит это расхождение имён намеренно — иначе перевод имени не
    # проверялся бы, а сверка с чужим отчётом молча выродилась бы в ноль сравнений.
    (D / "evidence" / "s3m-ppl-arms.json").write_text(json.dumps({"arms": [
        {"arm": a[len("calib-"):], "ppl_all_sets": {n: TRAJ[a][2000][i] for i, n in
                                                    enumerate(("v2_domain", "v1_domain",
                                                               "v1_general"))}}
        for a in calib]}), encoding="utf-8")
    (D / "evidence" / "s3t-k2-set.json").write_text(json.dumps({"arms": {"arms": [
        {"arm": a[len("calib-"):], "k1_ppl": TRAJ[a][2000][3],
         "k2_ppl": TRAJ[a][2000][4], "conjunction": True} for a in calib]}}),
        encoding="utf-8")
    arms_s3v = []
    for a in [x for x in TRAJ if x.startswith("ctrl")]:
        arms_s3v.append({"arm": a, "step": 0, "v1_ppl": HIST["v1_general"],
                         "k1_ppl": HIST["v3_general"], "k2_ppl": HIST["k2_general"]})
        for step in sorted(TRAJ[a]):
            arms_s3v.append({"arm": a, "step": step, "v1_ppl": TRAJ[a][step][2],
                             "k1_ppl": TRAJ[a][step][3], "k2_ppl": TRAJ[a][step][4]})
    (D / "evidence" / "s3v-control-arms-ppl.json").write_text(
        json.dumps({"arms": arms_s3v}), encoding="utf-8")
    for name, sha in (("v2domain", SET_SHA["v2_domain"]), ("v1domain", SET_SHA["v1_domain"])):
        (D / "evidence" / f"s3w-purity-{name}.json").write_text(json.dumps({
            "schema": "eval-set-purity/1",
            "set": {"path": "datasets/domain_eval_v2.txt" if name == "v2domain"
                    else "datasets/domain_eval.txt", "sha256": sha, "bytes": 1, "docs": 200},
            "instrument": {"set_windows": 49962},
            "corpora": {
                "domain": {"path": "cpt_corpus_v10.1.txt", "sha256": "d" * 64, "overlap_docs": 0,
                           "overlap_ngram_windows": 1733,
                           "window_examples": ["group relative policy optimization (grpo)"]},
                "replay": {"path": "general_replay_ru.txt", "sha256": "e" * 64,
                           "overlap_docs": 0, "overlap_ngram_windows": 0, "window_examples": []}},
            "verdict": "overlap"}), encoding="utf-8")
    for arm, (replay, lr, run_dir) in PARAMS.items():
        p = D / "stand" / run_dir
        p.mkdir(parents=True, exist_ok=True)
        (p / "calib_params.json").write_text(json.dumps({
            "cpt_steps": 2000, "seed": 42, "replay_share_pct": replay,
            "peak_lr_scale": lr}), encoding="utf-8")
        (D / "runs" / "s3w-domain-20260916" / f"{arm}.json").write_text(
            json.dumps(report(arm)), encoding="utf-8")
main()
FIXTURE
python3 "$TMP/mk_s3w_fixture.py" "$AS4"
expect_exit 0 "тождество прибора доказано, домен и язык сведены" \
  python3 tools/assemble_s3w_evidence.py --case-root "$AS4" --stand-calib "$AS4/stand" --out "$AS4/e.json"
python3 - "$AS4/e.json" "$AS4" <<'PY'
import json, sys
e = json.load(open(sys.argv[1]))
# Прибор: пять исторических чисел у каждой из шести рук — и все сошлись.
ii = e["instrument_identity"]
assert ii["reproduced"] and ii["worst_abs_delta"] == 0.0, ii
assert len(ii["checks"]) == 30, len(ii["checks"])
assert set(ii["checks"]) == {f"{a}:{s}" for a in e["arms_params"] for s in
                             ("v1_general", "v1_domain", "v2_domain", "v3_general",
                              "k2_general")}, "не все пары рука×набор проверены"
# Чужие отчёты того же прибора воспроизведены, а не переписаны.
cr = e["cross_run_reproduction"]
assert cr["ok"] and cr["worst_abs_delta"] == 0.0, cr
assert len(cr["checks"]) == 40, len(cr["checks"])
assert cr["expected_checks"] == len(cr["checks"]), cr["expected_checks"]
# Имя руки в чужом своде переводится (`calib-25-0.35` ↔ `25-0.35`): без перевода
# сверка не дала бы ни одного сравнения по калибровочным рукам — и «прошла» бы.
assert "calib-25-0.35:v2_domain" in cr["checks"], sorted(cr["checks"])
assert "calib-50-0.7:v3_general" in cr["checks"], sorted(cr["checks"])
assert "ctrl-C1-100-0.35:c500:v1_general" in cr["checks"], sorted(cr["checks"])
# Решающий набор назван, исторический — рядом; оба обязаны быть в таблице.
assert e["domain_set"]["chosen"] == "v2_domain", e["domain_set"]
assert e["domain_set"]["why_chosen"] and e["domain_set"]["historical_alt"]["set"] == "v1_domain"
by = {(r["arm"], r["step"]): r for r in e["arms"]}
assert len(e["arms"]) == 29, len(e["arms"])
# База: отношение 1.0 по построению, «выучен/не выучен» на ней не определено.
b = by[("ctrl-C2-25-0.035", 0)]
assert b["is_base"] and b["v2_domain_ratio"] == 1.0 and b["v2_domain_learned"] is None, b
# Домен: C1 без домена в обучении — домен испорчен (отношение > 1);
# C2 — выучен (< 1), но хуже, чем у 25-0.35 (0.70 против 0.395) — в полосе ×2.
c1 = by[("ctrl-C1-100-0.35", 2000)]
assert c1["v2_domain_ratio"] > 1.0 and c1["v2_domain_learned"] is False, c1
c2 = by[("ctrl-C2-25-0.035", 2000)]
assert c2["v2_domain_ratio"] < 1.0 and c2["v2_domain_learned"] is True, c2
# Язык — в том же прогоне, обеими компонентами, и конъюнкция считается здесь же.
assert c2["k1_ratio"] and c2["k2_ratio"] and c2["conjunction"] is not None, c2
assert c2["k1_pass"] is True and c2["k2_pass"] is True, c2
# Вердикт: ответ одним словом + цена обеими половинами.
v = e["verdict"]
assert v["answer"] == "сопоставимо", v["answer"]
assert v["low_lr_domain_comparable"] is True and v["domain_learned_by_low_lr"] is True
assert abs(v["numbers"]["ratio_of_ratios"] - 0.70 / 0.39545) < 1e-3, v["numbers"]
assert v["price_of_low_lr"]["domain_taken_less_by"] > 1.0, v["price_of_low_lr"]
assert v["price_of_low_lr"]["language_preserved_better_by"]["k1"] > 1.0, v["price_of_low_lr"]
assert v["price_of_low_lr"]["language_preserved_better_by"]["k2"] > 1.0, v["price_of_low_lr"]
assert "×2.0" in v["criterion"]["D2_comparable"], v["criterion"]
# Тренд — внутри руки; у C2 домен падает, у C1 тоже падает, но отношение > 1.
assert e["curves"]["ctrl-C2-25-0.035"]["domain_deciding"]["trend"] == "падает"
assert e["curves"]["ctrl-C1-100-0.35"]["domain_deciding"]["trend"] == "падает"
assert e["curves"]["calib-25-0.35"]["k1"]["trend"] == "падает"
# Пропущенная точка названа, а не пропущена: у 25-0.7 профиль начинается с 1000.
assert e["curves"]["calib-25-0.7"]["steps"] == [1000, 1500, 2000], e["curves"]["calib-25-0.7"]
assert e["arms_params"]["calib-25-0.7"]["gap"], "пропуск точки не назван"
assert e["removed_checkpoint"]["missing_step"] == 500, e["removed_checkpoint"]
# Гейт чистоты: документное пересечение нулевое, 12-граммы названы числом.
p = e["purity"]["v2_domain"]
assert p["overlap_docs_total"] == 0 and p["verdict"] == "clean", p
assert p["overlap_ngram_total"] == 1733 and p["ngram_share"] is not None, p
assert p["window_examples"], "примеры совпавших окон не приведены"
# Пара по LR названа явно: один корпус, варьируется пик.
lp = e["lr_pair_comparison"]
assert lp["pair"] == {"low_lr_arm": "ctrl-C2-25-0.035", "reference_arm": "calib-25-0.35"}
assert len(lp["per_step"]) == 4 and lp["final"]["step"] == 2000, lp["per_step"]
# Манифест AD-2 у каталога пробы: без него правило C-012 красное.
m = json.load(open(sys.argv[2] + "/runs/s3w-domain-20260916/run_manifest.json"))
assert m["pipeline_complete"] is True and m["stages"], m
assert len(m["pipeline_sha256_all"]) == 2, m["pipeline_sha256_all"]
assert len(m["datasets_extra"]) == 5, m["datasets_extra"]
sys.exit(0)
PY
# Низкий LR взял домен хуже полосы: ответ обязан перевернуться в «хуже»,
# и это не отказ свода, а его ответ — вердикт бинарный (AD-11).
python3 - "$AS4" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "runs" / "s3w-domain-20260916" / "ctrl-C2-25-0.035.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["states"]["cfinal"]["sets"]["v2_domain"]["ppl"] = 22.0
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 0 "домен хуже полосы — свод отвечает «хуже», а не отказывает" \
  python3 tools/assemble_s3w_evidence.py --case-root "$AS4" --stand-calib "$AS4/stand" --out "$AS4/e2.json"
python3 - "$AS4/e2.json" <<'PY'
import json, sys
v = json.load(open(sys.argv[1]))["verdict"]
assert v["answer"] == "хуже", v["answer"]
assert v["low_lr_domain_comparable"] is False, v
assert "обмен" in v["basis_for_decision"], v["basis_for_decision"]
sys.exit(0)
PY
python3 "$TMP/mk_s3w_fixture.py" "$AS4"
# Прибор не воспроизвёл историческое число: «тот же прибор» не доказано.
python3 - "$AS4" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "runs" / "s3w-domain-20260916" / "calib-50-0.35.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["states"]["base"]["sets"]["v1_domain"]["ppl"] = 9.4   # +4.4 % — при пороге 1e-4
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "база руки не воспроизвела историческое число — отказ" \
  python3 tools/assemble_s3w_evidence.py --case-root "$AS4" --stand-calib "$AS4/stand" --out "$AS4/e3.json"
python3 "$TMP/mk_s3w_fixture.py" "$AS4"
# Набор подменён: числа домена несопоставимы по построению.
python3 - "$AS4" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "runs" / "s3w-domain-20260916" / "ctrl-C1-100-0.35.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["sets"]["v2_domain"]["sha256"] = "f" * 64
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "проба мерила другой доменный набор — отказ" \
  python3 tools/assemble_s3w_evidence.py --case-root "$AS4" --stand-calib "$AS4/stand" --out "$AS4/e4.json"
python3 "$TMP/mk_s3w_fixture.py" "$AS4"
# Объявленная точка пропала из отчёта: таблица «рука × шаг» была бы неполной молча.
python3 - "$AS4" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "runs" / "s3w-domain-20260916" / "calib-50-0.7.json"
j = json.loads(p.read_text(encoding="utf-8"))
del j["states"]["c1500"]
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "пропала объявленная точка руки — отказ" \
  python3 tools/assemble_s3w_evidence.py --case-root "$AS4" --stand-calib "$AS4/stand" --out "$AS4/e5.json"
python3 "$TMP/mk_s3w_fixture.py" "$AS4"
# Дословное пересечение доменного набора с обучающим корпусом: домен меряет
# обучающий текст, и отношение к базе перестаёт быть свойством модели.
python3 - "$AS4" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "evidence" / "s3w-purity-v2domain.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["corpora"]["domain"]["overlap_docs"] = 3
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "у доменного набора есть дословное пересечение — отказ" \
  python3 tools/assemble_s3w_evidence.py --case-root "$AS4" --stand-calib "$AS4/stand" --out "$AS4/e6.json"
python3 "$TMP/mk_s3w_fixture.py" "$AS4"
# Чужой свод говорит про другое число: либо прогон не детерминирован, либо свод
# читает не то состояние — и то и другое отказ.
python3 - "$AS4" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "evidence" / "s3m-ppl-arms.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["arms"][0]["ppl_all_sets"]["v2_domain"] = 4.9
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "число домена разошлось с чужим сводом — отказ" \
  python3 tools/assemble_s3w_evidence.py --case-root "$AS4" --stand-calib "$AS4/stand" --out "$AS4/e7.json"
python3 "$TMP/mk_s3w_fixture.py" "$AS4"
# Рука в таблице перепутана: у 25-0.35 в прогоне другой пик LR.
python3 - "$AS4" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "stand" / "calib-25-0.35-20260916-0820" / "calib_params.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["peak_lr_scale"] = 0.7
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "пик LR руки не совпал с прогоном — рука перепутана" \
  python3 tools/assemble_s3w_evidence.py --case-root "$AS4" --stand-calib "$AS4/stand" --out "$AS4/e8.json"
python3 "$TMP/mk_s3w_fixture.py" "$AS4"
# Отчёт помечен незавершённым: проба пишет его инкрементально, и «нужные состояния
# на месте» ещё не значит, что проба дошла до конца и сверила себя.
python3 - "$AS4" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "runs" / "s3w-domain-20260916" / "ctrl-C2-25-0.035.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["complete"] = False
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "отчёт пробы помечен незавершённым — отказ" \
  python3 tools/assemble_s3w_evidence.py --case-root "$AS4" --stand-calib "$AS4/stand" --out "$AS4/e9.json"
python3 "$TMP/mk_s3w_fixture.py" "$AS4"
# Веса не восстановлены: вакуумная точка разошлась с базой — числа чекпойнтов
# снимались бы с осевших весов.
python3 - "$AS4" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "runs" / "s3w-domain-20260916" / "calib-25-0.35.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["states"]["base_untouched"]["sets"]["k2_general"]["ppl"] = 6.03
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "вакуумная точка разошлась с базой — отказ" \
  python3 tools/assemble_s3w_evidence.py --case-root "$AS4" --stand-calib "$AS4/stand" --out "$AS4/e10.json"
python3 "$TMP/mk_s3w_fixture.py" "$AS4"
# Гейт чистоты без корпусов: «ноль из отсутствия данных» неотличим от нуля по
# существу — это отказ, а не «чисто».
python3 - "$AS4" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "evidence" / "s3w-purity-v1domain.json"
j = json.loads(p.read_text(encoding="utf-8"))
j["corpora"] = {}
p.write_text(json.dumps(j), encoding="utf-8")
PY
expect_exit 1 "гейт чистоты без корпусов — отказ" \
  python3 tools/assemble_s3w_evidence.py --case-root "$AS4" --stand-calib "$AS4/stand" --out "$AS4/e11.json"
python3 "$TMP/mk_s3w_fixture.py" "$AS4"
expect_exit 2 "нет отчёта прибора — NOT-VERIFIED" \
  python3 tools/assemble_s3w_evidence.py --case-root "$TMP/asm-empty" --out "$AS4/e12.json"

echo
echo "== 31. check_instrument_versions.py (реестр редакций приборов, ADR-023 п.10) =="
# Фикстура — свой мини-репозиторий с историей ДВУХ приборов реестра: без git проверять
# нечего, а на дереве кейса негативные сценарии не поставишь (правка реестра и
# замороженных цитат в дереве запрещена — ADR-028 п.4). Поэтому копии фикстуры портятся,
# а оригинал остаётся зелёным: каждый красный — ровно одна внесённая порча. Порчи
# заведены на ОБА прибора: страж, покрывающий только первый, от стража без второго
# неотличим — проверяется и это (раздел второго прибора, его история, его цитата).
IV="$TMP/iv"
# Без git история приборов недоступна: страж честно вернёт NOT-VERIFIED, и требовать
# от него вердикта нечем. Пропуск печатается, а не превращается в красное: тест,
# красный by construction, сигналом не является (ADR-023 п.12). Блок ниже намеренно
# не переотступлен — он весь внутри ветки `else`.
if ! command -v git >/dev/null 2>&1; then
  echo "  пропуск: git недоступен — реестр редакций проверить нечем (страж вернёт NOT-VERIFIED)"
else
python3 - "$IV" tools/check_instrument_versions.py <<'PY'
import hashlib, json, pathlib, re, shutil, subprocess, sys

base = pathlib.Path(sys.argv[1])
guard = pathlib.Path(sys.argv[2]).resolve()
PPL = "tools/ppl_probe.py"
POOL = "tools/probe_pool_v2_reachability.py"
RUN = "tools/grounded_runner.py"
#: Содержимое приборов в фикстуре; второй прибор — тот же класс объекта (файл
#: каталога tools/), но свой путь и свой раздел реестра. Третий цитируется не
#: хешем, а **числом**: у него проверяются носитель и цитата (проверка «з»).
BODY = {PPL: "SETS = {\"a\": 1}\n", POOL: "POOL = \"p\"\n", RUN: "BASE_STEP_S = 123.87\n"}
#: Артефакт-цитата второго прибора: структура как в evidence/pool-v2-reproducibility.json
#: (``"tools/probe_pool_v2_reachability.py": {"sha256": …}``) — иначе хеш не отнесётся
#: к прибору, и проверка цитат была бы зелёной по построению.
CITER = "evidence/pool-v2-reproducibility.json"
#: Замороженное число третьего прибора: носителей ДВА (константа прибора и поле
#: артефакта) — они обязаны сходиться между собой; цитата — фраза в README.
CARRIER = "evidence/mini.json"
READS = "README.md"
FROZEN_CITE = "фикстура: шаг 123.87 с\n"


def run(root, *args):
    return subprocess.run(args, cwd=root, check=True, capture_output=True, text=True).stdout


def commit(root, message):
    run(root, "git", "add", "-A")
    run(root, "git", "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-q", "-m", message)
    return run(root, "git", "rev-parse", "HEAD").strip()


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def frozen_table(value="123.87", cite=None):
    """Таблица замороженных чисел третьего прибора (носитель + цитата)."""
    cite = FROZEN_CITE.strip() if cite is None else cite
    return ("| # | Величина | Носитель (файл → поле) | Значение | Цитата (файл → фраза) |\n"
            "|---|---|---|---|---|\n"
            f"| F-1 | шаг фикстуры, с | `{RUN}` → `BASE_STEP_S`; `{CARRIER}` → `step` | `{value}` | "
            f"`{READS}` → `{cite}` |\n")


def registry(sections, frozen=None):
    """Реестр из разделов приборов: заголовок раздела называет путь прибора."""
    out = ["# INSTRUMENT-VERSIONS.md — реестр редакций приборов кейса", ""]
    for instrument, rows in sections:
        out += [f"### Прибор `{instrument}`", "",
                "| Редакция | sha256 | Коммит | Дата | Статус |", "|---|---|---|---|---|",
                *rows, ""]
        if instrument == RUN:
            out += [frozen if frozen is not None else frozen_table(), ""]
    return "\n".join(out) + "\n"


def spec(sha_ppl):
    return ("# EVAL-SETS.md\n\n"
            f"- Прибор: `{PPL}` (методика `_ppl_eval`),\n"
            f"  sha256 `{sha_ppl}`\n"
            "- Реестр редакций: `docs/specs/INSTRUMENT-VERSIONS.md`\n")


def citer(sha_pool):
    return json.dumps({"inputs": {"tools": {POOL: {"sha256": sha_pool}}}},
                      ensure_ascii=False, indent=2) + "\n"


def row(rev, digest, commit_hash, actual):
    return (f"| {rev} | `{digest}` | `{commit_hash}` | 2026-09-20 | "
            f"{'**актуальная**' if actual else 'историческая'} |")


def all_rows(commit_hash):
    return [(PPL, [row("P-1", sha(BODY[PPL]), commit_hash, True)]),
            (POOL, [row("R-1", sha(BODY[POOL]), commit_hash, True)]),
            (RUN, [row("G-1", sha(BODY[RUN]), commit_hash, True)])]


def build(root):
    """Мини-репозиторий: три прибора, согласованный реестр, цитаты, снимок, спека."""
    root.mkdir(parents=True)
    for sub in ("tools", "docs/specs", "evidence"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    run(root, "git", "init", "-q")
    for instrument, content in BODY.items():
        (root / instrument).write_text(content, encoding="utf-8")
    (root / "docs/specs/EVAL-SETS.md").write_text(spec(sha(BODY[PPL])), encoding="utf-8")
    (root / CITER).write_text(citer(sha(BODY[POOL])), encoding="utf-8")
    (root / CARRIER).write_text(json.dumps({"step": 123.87}, ensure_ascii=False) + "\n", encoding="utf-8")
    (root / READS).write_text(FROZEN_CITE, encoding="utf-8")
    (root / "docs/specs/INSTRUMENT-VERSIONS.md").write_text(
        registry(all_rows("HEAD")), encoding="utf-8")
    (root / "evidence/instrument-hash-audit.json").write_text("{}\n", encoding="utf-8")
    head = commit(root, "P-1 / R-1 / G-1")
    (root / "docs/specs/INSTRUMENT-VERSIONS.md").write_text(
        registry(all_rows(head)), encoding="utf-8")
    commit(root, "реестр: коммиты редакций")
    subprocess.run([sys.executable, str(guard), "--case-root", str(root), "--emit"],
                   check=True, capture_output=True)
    return head


head = build(base)

# Порча 1: актуальная редакция первого прибора объявлена чужим хешем (реестр «уехал» от дерева).
a = base.parent / "iv-a"; shutil.copytree(base, a)
p = a / "docs/specs/INSTRUMENT-VERSIONS.md"
text = p.read_text(encoding="utf-8")
declared = re.search(r"`([0-9a-f]{64})`", text).group(1)
p.write_text(text.replace(declared, ("0" if declared[0] != "0" else "1") + declared[1:]),
             encoding="utf-8")

# Порча 2: цитата первого прибора переписана в дереве (число в EVAL-SETS.md затёрто).
b = base.parent / "iv-b"; shutil.copytree(base, b)
p = b / "docs/specs/EVAL-SETS.md"
p.write_text(p.read_text(encoding="utf-8").replace("sha256 `", "sha256 `0"), encoding="utf-8")

# Порча 3: правка ВТОРОГО прибора прошла без реестра (новая редакция живёт в git).
c = base.parent / "iv-c"; shutil.copytree(base, c)
(c / POOL).write_text("POOL = \"p2\"\n", encoding="utf-8")
new_head = commit(c, "R-2 без реестра")

# Порча 4: снимок аудита разошёлся с реестром (подменена редакция второго прибора).
d = base.parent / "iv-d"; shutil.copytree(base, d)
p = d / "evidence/instrument-hash-audit.json"
audit = json.loads(p.read_text(encoding="utf-8"))
audit["instruments"][1]["revisions"][0]["sha256"] = "0" * 64
p.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

# Порча 5: у объявленного хеша завёлся второй носитель — спека перестала
# ссылаться на реестр.
e = base.parent / "iv-e"; shutil.copytree(base, e)
p = e / "docs/specs/EVAL-SETS.md"
p.write_text(p.read_text(encoding="utf-8").replace("INSTRUMENT-VERSIONS.md", "какой-то реестр"),
             encoding="utf-8")

# Порча 6: из реестра убран раздел ВТОРОГО прибора — прибор объявлен в константе
# стража, а раздела с таблицей нет (иначе «второй прибор» жил бы вне проверки).
h = base.parent / "iv-h"; shutil.copytree(base, h)
(h / "docs/specs/INSTRUMENT-VERSIONS.md").write_text(
    registry([(PPL, [row("P-1", sha(BODY[PPL]), head, True)]),
              (RUN, [row("G-1", sha(BODY[RUN]), head, True)])]), encoding="utf-8")
subprocess.run([sys.executable, str(guard), "--case-root", str(h), "--emit"],
               check=True, capture_output=True)

# Порча 7: цитата второго прибора переписана в дереве (его хеш в артефакте-цитате затёрт).
j = base.parent / "iv-j"; shutil.copytree(base, j)
p = j / CITER
p.write_text(p.read_text(encoding="utf-8").replace(sha(BODY[POOL]),
                                                    "0" + sha(BODY[POOL])[1:]),
             encoding="utf-8")

# Починка порчи 3 тем же механизмом, что предписан правилом: строка редакции +
# пересборка снимка. Зелёный после красного — доказательство, что гейт проходим.
g = base.parent / "iv-g"; shutil.copytree(c, g)
(g / "docs/specs/INSTRUMENT-VERSIONS.md").write_text(
    registry([(PPL, [row("P-1", sha(BODY[PPL]), head, True)]),
              (POOL, [row("R-1", sha(BODY[POOL]), head, False),
                      row("R-2", sha("POOL = \"p2\"\n"), new_head, True)]),
              (RUN, [row("G-1", sha(BODY[RUN]), head, True)])]),
    encoding="utf-8")
subprocess.run([sys.executable, str(guard), "--case-root", str(g), "--emit"],
               check=True, capture_output=True)

# Порча 8: число уехало ВМЕСТЕ с носителями (прибор и артефакт правлены, строка
# реестра есть), а цитата в README осталась прежней: фраза на месте, но несёт уже
# не то число. Это ровно тот случай, ради которого цитата сверяется с реестром,
# а не живёт отдельно.
k = base.parent / "iv-k"; shutil.copytree(base, k)
(k / RUN).write_text("BASE_STEP_S = 150.0\n", encoding="utf-8")
k_head = commit(k, "G-2: шаг изменён")
(k / CARRIER).write_text(json.dumps({"step": 150.0}, ensure_ascii=False) + "\n", encoding="utf-8")
(k / "docs/specs/INSTRUMENT-VERSIONS.md").write_text(
    registry([(PPL, [row("P-1", sha(BODY[PPL]), head, True)]),
              (POOL, [row("R-1", sha(BODY[POOL]), head, True)]),
              (RUN, [row("G-1", sha(BODY[RUN]), head, False),
                     row("G-2", sha("BASE_STEP_S = 150.0\n"), k_head, True)])],
             frozen=frozen_table("150.0")),
    encoding="utf-8")
subprocess.run([sys.executable, str(guard), "--case-root", str(k), "--emit"],
               check=True, capture_output=True)

# Порча 9: носитель замороженного числа уехал — поле артефакта изменено, реестр нет.
l = base.parent / "iv-l"; shutil.copytree(base, l)
p = l / CARRIER
p.write_text(json.dumps({"step": 200.0}, ensure_ascii=False) + "\n", encoding="utf-8")

# Порча 10: правка ТРЕТЬЕГО прибора прошла без строки реестра (новая редакция в git).
#          Страж обязан покрывать и его, а не только первые два.
m = base.parent / "iv-m"; shutil.copytree(base, m)
(m / RUN).write_text("BASE_STEP_S = 1.0\n", encoding="utf-8")
commit(m, "G-2 без реестра")

# Нет реестра — NOT-VERIFIED, а не «зелено по умолчанию».
f = base.parent / "iv-f"; shutil.copytree(base, f)
(f / "docs/specs/INSTRUMENT-VERSIONS.md").unlink()
PY

expect_exit 0 "реестр, снимок, дерево и история согласованы (три прибора)" \
  python3 tools/check_instrument_versions.py --case-root "$IV"
expect_contains "согласован" "согласие печатается, а не подразумевается" \
  python3 tools/check_instrument_versions.py --case-root "$IV"
expect_contains "tools/probe_pool_v2_reachability.py" "снимок несёт раздел второго прибора" \
  python3 tools/check_instrument_versions.py --case-root "$IV" --stdout
expect_contains "tools/grounded_runner.py" "снимок несёт раздел третьего прибора (числа)" \
  python3 tools/check_instrument_versions.py --case-root "$IV" --stdout
# Идемпотентность --emit: снимок пересказывает реестр и сам себя не цитирует,
# поэтому вторая пересборка обязана дать байт-в-байт тот же файл. Без этого
# свойства счёт цитат зависел бы от числа прогонов, а не от дерева.
expect_exit 0 "повторная пересборка снимка не меняет его (идемпотентность --emit)" \
  bash -c 'set -e; snap="$1/iv/evidence/instrument-hash-audit.json"; cp "$snap" "$snap.1"
           python3 tools/check_instrument_versions.py --case-root "$1/iv" --emit >/dev/null
           cmp "$snap" "$snap.1"' _ "$TMP"
expect_exit 1 "актуальная редакция объявлена чужим хешем — отказ" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-a"
expect_exit 1 "цитата первого прибора переписана — отказ" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-b"
expect_exit 1 "правка второго прибора без строки реестра — отказ" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-c"
expect_exit 1 "снимок аудита разошёлся с реестром — отказ" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-d"
expect_exit 1 "спека не ссылается на реестр — отказ" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-e"
expect_exit 1 "раздел второго прибора убран из реестра — отказ" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-h"
expect_exit 1 "цитата второго прибора переписана — отказ" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-j"
expect_exit 1 "цитата замороженного числа несёт другое число — отказ" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-k"
expect_contains "не несёт числа" "находка называет, какое число не донесено" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-k"
expect_exit 1 "носитель замороженного числа уехал — отказ" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-l"
expect_contains "уехало от носителя" "находка называет расхождение с носителем" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-l"
expect_exit 1 "правка третьего прибора без строки реестра — отказ" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-m"
expect_exit 2 "нет реестра — NOT-VERIFIED" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-f"
expect_exit 0 "строка реестра + пересборка снимка — снова зелено" \
  python3 tools/check_instrument_versions.py --case-root "$TMP/iv-g"
fi

echo "== 32. check_env_contract.py (контракт среды заземлённой оси, ADR-049 п.1 G4) =="
# Страж живёт ВНЕ гейта: номер правила выдаёт канон (ADR-046 п.8), а самовольная
# выдача уже один раз столкнула номера ветки (C-026/C-027). Проверяется он всё
# равно как правило — у каждого пути свой случай:
#   (а) зелёный — внутренняя сеть держит выход, мок достижим, задачи несут max_steps;
#   (б) красный — выход ОТКРЫТ: зубы стража, а не декларация (на дефолтной bridge
#       пробу держит канарейка на шлюзе — она достижима и без интернета, поэтому
#       случай не зависит от наличия сети у хоста);
#   (в) красный — доказательство противоречит контракту (нет supervision, бюджет не
#       связывает награду, выход в доказательстве не закрыт, cleanup не назван);
#   (г) красный — задача без max_steps: лимит держит вызывающая сторона, а не среда;
#   (д) NOT-VERIFIED — без docker молчаливого зелёного нет (exit 2, ADR-023 п.12).
mkdir -p "$TMP/fakebin"
cat > "$TMP/fakebin/docker" <<'SH'
#!/bin/sh
echo "docker: cannot connect to the Docker daemon" >&2
exit 1
SH
chmod +x "$TMP/fakebin/docker"

# Доказательство, противоречащее контракту по четырём разделам сразу: бюджеты
# 0/1 дают одну и ту же награду (лимит не связывает), supervision отсутствует,
# выход в пробе изоляции не подтверждён, cleanup не назван, max_steps разошёлся.
cat > "$TMP/gs-env-bad.json" <<'JSON'
{
  "schema": "grounded-skeleton-evidence/1",
  "generated_at": "2026-09-20T00:00:00+00:00",
  "status": "ok",
  "tasks": [{"id": "gs-fs-01", "spec": {"max_steps": 99}}],
  "isolation": {"gs-x-net": {"network_profile": "internal", "external_unreachable": null,
                             "dns_unresolvable": null, "mock_reachable": true}},
  "step_limit": {"binds_on_state": false,
                 "per_task": [{"task": "gs-fs-01", "budget_probe": {"0": {"score": 1}, "1": {"score": 1}}}]},
  "cleanup": null
}
JSON

# 32.1 Зелёный путь: живая проба + доказательство текущего набора.
run_or_skip 0 "контракт среды: internal-сеть, мок достижим, задачи несут max_steps" \
  python3 tools/check_env_contract.py --set-dir data/grounded-skeleton

# 32.2 Зубы: на дефолтной bridge выход обязан ПРОЙТИ → красное. Если случай
#      перестанет краснеть, страж перестал проверять выход и стал декларацией.
run_or_skip 1 "контракт среды: выход ОТКРЫТ (bridge) → красное, а не чтение профиля" \
  python3 tools/check_env_contract.py --set-dir data/grounded-skeleton --probe-network bridge

# 32.3 Доказательство противоречит контракту → красное (и именно по этим разделам).
run_or_skip 1 "контракт среды: доказательство без supervision/лимита/cleanup → красное" \
  python3 tools/check_env_contract.py --set-dir data/grounded-skeleton --evidence "$TMP/gs-env-bad.json"
expect_contains "step-supervision-unproven" "контракт среды: находка называет раздел, а не «что-то не так»" \
  python3 tools/check_env_contract.py --set-dir data/grounded-skeleton --evidence "$TMP/gs-env-bad.json"

# 32.4 Лимит шагов — часть схемы задачи: задача без него отказ, а не «без лимита».
cp -r data/grounded-skeleton "$TMP/gs-set-nolimit"
python3 - "$TMP" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "gs-set-nolimit" / "tasks" / "gs-shell-01" / "task.json"
d = json.loads(p.read_text(encoding="utf-8"))
d.pop("max_steps", None)
p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
run_or_skip 1 "контракт среды: задача без max_steps → красное (лимит держит не среда)" \
  python3 tools/check_env_contract.py --set-dir "$TMP/gs-set-nolimit" --evidence "$TMP/gs-env-bad.json"

# 32.5 Три состояния, а не два: без docker — NOT-VERIFIED, а не вакуумный зелёный.
expect_exit 2 "контракт среды: docker недоступен → NOT-VERIFIED, не зелёный" \
  env PATH="$TMP/fakebin:$PATH" python3 tools/check_env_contract.py --set-dir data/grounded-skeleton

# 32.6 Механизм мока — часть доказательства (поправка ADR-049 п.10 от 20.09.2026):
#      мок обязан быть назван sidecar-КОНТЕЙНЕРОМ В СЕТИ ПРОГОНА. Доказательство
#      прежнего механизма (мок-слушатель на шлюзе) обязано краснеть: «мок достижим»
#      без названного механизма не отличает контейнер от процесса на адресе хоста,
#      а такой процесс из контейнера стадии неисполним (Errno 99).
python3 - "$TMP" <<'PY'
import json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
base = json.loads(pathlib.Path("evidence/grounded-skeleton.json").read_text(encoding="utf-8"))
# Песочница с моком — та, чей профиль сети внутренний; у неё и меняются пробы.
(mock_net,), = [(k,) for k, v in base["isolation"].items()
                if str(v.get("network_profile", "")).startswith("internal")][:1]

def doc(**over):
    d = json.loads(json.dumps(base))
    d.update(over)
    return d


# (а) прежний механизм: следа sidecar в доказательстве нет
old = doc()
old["network"].pop("sidecars", None)
for p in old["isolation"].values():
    p.pop("canary_attempted", None)
(tmp / "gs-env-hostmock.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

# (б) sidecar объявлен, но не в сети прогона (адрес хоста под другим именем)
foreign = doc()
foreign["network"]["sidecars"]["mock"]["in_run_network"] = False
foreign["network"]["sidecars"]["mock"]["ip"] = ""
(tmp / "gs-env-foreignmock.json").write_text(json.dumps(foreign, ensure_ascii=False), encoding="utf-8")

# (в) канарейка мертва: «недостижима» тогда вакуумна
dead = doc()
dead["network"]["sidecars"]["canary"]["self_check"] = False
(tmp / "gs-env-deadcanary.json").write_text(json.dumps(dead, ensure_ascii=False), encoding="utf-8")

# (г) канарейка ДОСТИЖИМА: выход открыт, и это видно по попытке, а не по профилю
open_ = doc()
open_["isolation"][mock_net]["canary_unreachable"] = False
(tmp / "gs-env-opencanary.json").write_text(json.dumps(open_, ensure_ascii=False), encoding="utf-8")
print("доказательства-мутанты собраны")
PY

# Страж зовётся один раз на мутанта: живая проба поднимает контейнеры, и второй
# вызов ради второй строки стоил бы столько же, сколько первый.
expect_guard_rule() {  # <мутант> <ожидаемое правило> <описание>
  local ev="$1" rule="$2" what="$3" out rc
  out="$(python3 tools/check_env_contract.py --set-dir data/grounded-skeleton --evidence "$ev" --json 2>&1)"; rc=$?
  if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -qF -- "\"$rule\""; then
    PASS=$((PASS + 1)); printf '  ok   %-58s (exit 1, правило %s)\n' "$what" "$rule"
  else
    FAIL=$((FAIL + 1)); failures+=("$what: ожидался exit 1 с правилом $rule, получен $rc")
    printf '  FAIL %-58s (exit %s, правило %s)\n' "$what" "$rc" "$rule"
    printf '%s\n' "$out" | sed 's/^/       | /' | head -6
  fi
}

expect_guard_rule "$TMP/gs-env-hostmock.json" "mock-mechanism-unnamed" \
  "контракт среды: доказательство прежнего механизма (мок на шлюзе) → красное"
expect_guard_rule "$TMP/gs-env-foreignmock.json" "mock-not-in-run-network" \
  "контракт среды: мок не в сети прогона → красное (достижимость зависит от топологии)"
expect_guard_rule "$TMP/gs-env-deadcanary.json" "canary-dead" \
  "контракт среды: мёртвая канарейка → красное (недостижимость мёртвого слушателя вакуумна)"
expect_guard_rule "$TMP/gs-env-opencanary.json" "egress-open-in-evidence" \
  "контракт среды: канарейка ДОСТИЖИМА в доказательстве → красное (выход открыт)"

# Проба не должна оставлять за собой контейнеров и сетей: широкий шаблон здесь
# запрещён (docker делят параллельные дельты), снимается по точному префиксу.
run_or_skip 0 "контракт среды: за пробой не осталось объектов (точный префикс)" \
  bash -c 'docker ps -a --format "{{.Names}}" | grep -q "^gs-envcheck-" && exit 1; docker network ls --format "{{.Name}}" | grep -q "^gs-envcheck-" && exit 1; exit 0'

echo "== 33. S3av: идентичность набора (вход стадии — параметр, факт — в манифесте) =="
# Дефект, который проверяется: цепочка несла `--sft_data …/sft_train_v12.jsonl`
# ЛИТЕРАЛОМ, объявленный набор уходил только в манифест AD-2, а копия пайплайна
# стадии выводила имя val-тензора тем же литералом. Декларация и факт расходились
# молча. Три пути проверки, и ни один не запускает обучение:
#   (а) вход доезжает до контейнера ИЗ ПАРАМЕТРА, литерала в вызове нет;
#   (б) несходящееся объявление — отказ цепочки (exit 2), а не тихое умолчание;
#   (в) стадия предъявляет факт (блок sft_input, полные sha256) и отказывает до
#       первого шага, если объявленный хеш не совпал с прочитанным;
#   (г) страж краснеет на мутантах «объявлен один набор — загружен другой» и
#       «хеш записан не целиком», и зеленеет на честном манифесте.

echo "  --- 33a. вход стадии SFT — параметр, а не литерал ---"
S3AV_DIR="$TMP/s3av"; mkdir -p "$S3AV_DIR/datasets"
#: Фикстура набора своя и малая: хеш цепочка считает с диска, а 486 МБ настоящего
#: набора тесту не нужны (AD-4: данные в кейс не копируются, фикстура — под
#: префиксом дельты в общем /tmp).
printf '{"messages": []}\n' > "$S3AV_DIR/datasets/sft_train_v13_fixed.jsonl"
S3AV_SFT="/workspace/shared/datasets/sft_train_v13_fixed.jsonl"
S3AV_CHAIN="$TMP/s3av-chain"; mkdir -p "$S3AV_CHAIN"
printf 'sft\tsft\tsft\tcheckpoints/checkpoint_final.pt\t67423\t2\t-\tp-sft\n' \
  > "$S3AV_CHAIN/stages.tsv"
expect_exit 0 "цепочка принимает --sft-data и считает хеш объявленного набора с диска" \
  test_chain_args "$S3AV_CHAIN" ok "$CHAIN_TMP/guard_ok.sh" "$TMP/s3av-args.txt" \
  --shared "$S3AV_DIR" --sft-data "$S3AV_SFT"
expect_exit 0 "параметр доехал до контейнера: литерала v12 в вызове стадии нет" \
  python3 - "$TMP/s3av-args.txt" <<'PY'
import sys
args = open(sys.argv[1], encoding="utf-8").read()
assert "--sft_data /workspace/shared/datasets/sft_train_v13_fixed.jsonl" in args, args
assert "sft_train_v12" not in args, "литерал v12 остался в вызове стадии"
PY
expect_contains "набор SFT: /workspace/shared/datasets/sft_train_v13_fixed.jsonl" \
  "объявленный набор назван в логе цепочки" cat "$S3AV_CHAIN/chain.out"

#: Манифест AD-2 берёт путь из ТОГО ЖЕ значения: хостовый выводится подстановкой
#: монтирования. Второй флаг («хостовый набор») намеренно не введён — два флага
#: расходятся молча, и это ровно чинимый дефект.
#: Заглушка генератора манифеста — на python3, а не на bash: цепочка зовёт
#: `python3 "$MANIFEST_TOOL"`, и bash-скрипт на этом месте даёт rc=1 (проверено:
#: манифест «не записан», и это видно в логе как ВНИМАНИЕ, а не как тишина).
cat > "$CHAIN_TMP/manifest_args.py" <<'PY'
import os, pathlib, sys
pathlib.Path(os.environ["MANIFEST_ARGS_FILE"]).write_text(
    " ".join(sys.argv[1:]), encoding="utf-8")
PY
S3AV_M="$TMP/s3av-manifest"; mkdir -p "$S3AV_M"
printf 'sft\tsft\tsft\tcheckpoints/checkpoint_final.pt\t67423\t2\t-\tp-sft\n' \
  > "$S3AV_M/stages.tsv"
MANIFEST_ARGS_FILE="$TMP/s3av-manifest-args.txt" \
  test_chain "$S3AV_M" ok "$CHAIN_TMP/guard_ok.sh" --shared "$S3AV_DIR" \
  --manifest-tool "$CHAIN_TMP/manifest_args.py" --sft-data "$S3AV_SFT" >/dev/null 2>&1
expect_exit 0 "объявление манифеста выведено из того же параметра (хостовый путь)" \
  python3 - "$TMP/s3av-manifest-args.txt" "$S3AV_DIR" <<'PY'
import sys
args, root = open(sys.argv[1], encoding="utf-8").read(), sys.argv[2]
assert f"--dataset-extra sft={root}/datasets/sft_train_v13_fixed.jsonl" in args, args
PY

#: Способность пайплайна проверяется по САМОМУ файлу: у базового пайплайна контура
#: флага `--sft_data_sha256` нет, и передать его значило бы уронить стадию в
#: argparse за секунды. Проверяются оба хода — «знает» и «не знает».
printf 'def main():\n    pass\n' > "$TMP/s3av-pipe-plain.py"
printf 'p.add_argument("--sft_data_sha256", default=None)\n' > "$TMP/s3av-pipe-flag.py"
S3AV_FLAG="$TMP/s3av-flag"; mkdir -p "$S3AV_FLAG"
printf 'sft\tsft\tsft\tcheckpoints/checkpoint_final.pt\t67423\t2\t-\tp-sft\n' \
  > "$S3AV_FLAG/stages.tsv"
test_chain_args "$S3AV_FLAG" ok "$CHAIN_TMP/guard_ok.sh" "$TMP/s3av-flag-args.txt" \
  --shared "$S3AV_DIR" --pipeline "$TMP/s3av-pipe-plain.py" \
  --pipeline-ctr "$TMP/s3av-pipe-plain.py" --sft-data "$S3AV_SFT" >/dev/null 2>&1
expect_exit 0 "в вызов стадии с базовым пайплайном --sft_data_sha256 не попал" \
  python3 - "$TMP/s3av-flag-args.txt" <<'PY'
import sys
args = open(sys.argv[1], encoding="utf-8").read()
assert "--sft_data_sha256" not in args, args
assert "--sft_data /workspace/shared/datasets/sft_train_v13_fixed.jsonl" in args, args
PY
expect_contains "НЕ передаётся в стадию" "граница названа в логе, а не проглочена" \
  cat "$S3AV_FLAG/chain.out"
S3AV_FLAG2="$TMP/s3av-flag2"; mkdir -p "$S3AV_FLAG2"
printf 'sft\tsft\tsft\tcheckpoints/checkpoint_final.pt\t67423\t2\t-\tp-sft\n' \
  > "$S3AV_FLAG2/stages.tsv"
test_chain_args "$S3AV_FLAG2" ok "$CHAIN_TMP/guard_ok.sh" "$TMP/s3av-flag2-args.txt" \
  --shared "$S3AV_DIR" --pipeline "$TMP/s3av-pipe-flag.py" \
  --pipeline-ctr "$TMP/s3av-pipe-flag.py" --sft-data "$S3AV_SFT" >/dev/null 2>&1
expect_exit 0 "пайплайн с флагом: объявленный хеш уходит в стадию (сверка внутри стадии)" \
  python3 - "$TMP/s3av-flag2-args.txt" <<'PY'
import sys
args = open(sys.argv[1], encoding="utf-8").read().split()
assert "--sft_data_sha256" in args, args
sha = args[args.index("--sft_data_sha256") + 1]
assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), sha
PY

echo "  --- 33b. несходящееся объявление — отказ, а не тихое умолчание ---"
mkdir -p "$TMP/s3av-no" "$TMP/s3av-sha"
printf 'sft\tsft\tsft\tcheckpoints/checkpoint_final.pt\t67423\t2\t-\tp-sft\n' \
  > "$TMP/s3av-no/stages.tsv"
printf 'sft\tsft\tsft\tcheckpoints/checkpoint_final.pt\t67423\t2\t-\tp-sft\n' \
  > "$TMP/s3av-sha/stages.tsv"
expect_exit 2 "непереводимый путь --sft-data → отказ (а не «остаться на прежнем наборе»)" \
  test_chain "$TMP/s3av-no" ok "$CHAIN_TMP/guard_ok.sh" --shared "$S3AV_DIR" \
  --sft-data /somewhere/else/sft.jsonl
expect_exit 2 "объявленный sha256 не совпал с файлом → отказ (ADR-028 п.1: факт первичен)" \
  test_chain "$TMP/s3av-sha" ok "$CHAIN_TMP/guard_ok.sh" --shared "$S3AV_DIR" \
  --sft-data "$S3AV_SFT" --sft-data-sha256 deadbeefdeadbeef
expect_contains "объявленный sha256 набора SFT" "причина отказа названа, а не проглочена" \
  cat "$TMP/s3av-sha/chain.out"

echo "  --- 33c. стадия предъявляет факт и отказывает до первого шага ---"
expect_exit 0 "sft_input: полные sha256 jsonl и тензора, число примеров; расхождение — отказ" \
  python3 - "$TMP" <<'PY'
import ast, hashlib, json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
out = tmp / "s3av_pipe.py"
subprocess.run([sys.executable, "tools/patch_pipeline_sft.py", "--base",
                "laguna_pipeline_v8.py", "--out", str(out)], check=True,
               capture_output=True)
tree = ast.parse(out.read_text(encoding="utf-8"))
# S3be добавила четвёртого помощника — идентичность набора курикулума RL
# (ADR-054 п.2). Набор перечислен поимённо: «применилось меньше, чем задумано» не
# должно проходить за успех, а исчезновение правки — быть незамеченным.
wanted = {"_sha256_full_digest", "_sft_jsonl_samples", "_sft_record_input_identity",
          "_rl_record_input_identity"}
funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
assert {f.name for f in funcs} == wanted, [f.name for f in funcs]
# Исполняются ТОЛЬКО четыре помощника: остальной пайплайн тянет torch, и обучение
# здесь не запускается — проверяется предъявление факта, а не тренировка.
log = type("L", (), {"info": staticmethod(lambda *a, **k: None),
                     "warning": staticmethod(lambda *a, **k: None)})()
ns = {"hashlib": hashlib, "json": json, "Path": pathlib.Path,
      "datetime": __import__("datetime").datetime, "log": log}
exec(compile(ast.Module(body=funcs, type_ignores=[]), "<sft_input>", "exec"), ns)

def full(p):
    h = hashlib.sha256()
    h.update(pathlib.Path(p).read_bytes())
    return h.hexdigest()

work = tmp / "s3av-identity"; work.mkdir(exist_ok=True)
j = work / "sft_train_v13_fixed.jsonl"; j.write_text('{"a": 1}\n{"a": 2}\n')
n = work / "sft_train_v13_fixed_8192_qwen25.npz"; n.write_bytes(b"npz-stand-in")

class D:
    def __len__(self): return 2

class A: pass

d = D(); d.source_path, d.npz_path, d.samples_total, d.npz_candidates = j, n, 7, [str(n)]
ck = work / "checkpoints"; ck.mkdir(exist_ok=True)
a = A()
a.sft_data, a.sft_data_sha256, a.ckpt_dir = str(j), full(j), str(ck)
a.max_samples, a.stage, a.exp_name = 5, "sft", "s3av-fixture"
ns["_sft_record_input_identity"](a, d)
block = json.loads((ck / "run_manifest.json").read_text())["sft_input"]
assert block["jsonl"]["sha256"] == full(j) and block["jsonl"]["hash_scope"] == "full"
assert block["jsonl"]["samples"] == 2 and block["jsonl"]["path"] == str(j)
assert block["tensor"]["sha256"] == full(n) and block["tensor"]["hash_scope"] == "full"
assert block["tensor"]["samples"] == 2 and block["tensor"]["samples_total"] == 7
assert block["declared_sha256"] == full(j)

# Расхождение объявленного хеша — отказ ДО первого шага и без ложного следа.
ck2 = work / "checkpoints-refused"; ck2.mkdir(exist_ok=True)
a2 = A()
a2.sft_data, a2.sft_data_sha256, a2.ckpt_dir = str(j), "0" * 64, str(ck2)
a2.max_samples, a2.stage, a2.exp_name = 5, "sft", "s3av-fixture"
try:
    ns["_sft_record_input_identity"](a2, d)
    raise AssertionError("стадия не отказала на необъявленном наборе")
except SystemExit as e:
    assert "ОТКАЗ" in str(e), e
assert not (ck2 / "run_manifest.json").exists(), "отказ оставил ложный манифест"

# ── Тот же круг для набора курикулума RL (S3be, ADR-054 п.2) ──────────────────
# Проверяется не «похожесть» механизма, а тот же самый: объявленный набор приходит
# параметром, фактический — от загрузчика, расхождение останавливает стадию.
r = work / "rl_tasks_revpool_v2.jsonl"; r.write_text('{"t": 1}\n{"t": 2}\n{"t": 3}\n')
class R:
    def __init__(self, p): self.source_path = p
    def __len__(self): return 3
rd = R(str(r))
ckr = work / "checkpoints-rl"; ckr.mkdir(exist_ok=True)
ar = A()
ar.rl_data, ar.rl_data_sha256, ar.ckpt_dir = str(r), full(r), str(ckr)
ar.max_samples, ar.stage, ar.exp_name = 9162, "rl", "s3be-fixture"
ns["_rl_record_input_identity"](ar, rd)
rblock = json.loads((ckr / "run_manifest.json").read_text())["rl_input"]
assert rblock["jsonl"]["sha256"] == full(r) and rblock["jsonl"]["hash_scope"] == "full"
assert rblock["jsonl"]["samples"] == 3 and rblock["jsonl"]["path"] == str(r)
assert rblock["declared_sha256"] == full(r) and rblock["tasks_loaded"] == 3
# Тензора у набора курикулума нет и быть не должно: загрузчик читает jsonl напрямую,
# и обязательный `tensor` был бы красным по построению (ADR-023 п.12).
assert "tensor" not in rblock, rblock

# Объявлен v1 — прочитан v2: это и есть дефект «набор остался бумажным». Отказ до
# первого шага и без ложного следа (манифест не создан).
ckr2 = work / "checkpoints-rl-refused"; ckr2.mkdir(exist_ok=True)
ar2 = A()
ar2.rl_data, ar2.rl_data_sha256, ar2.ckpt_dir = str(r), "a" * 64, str(ckr2)
ar2.max_samples, ar2.stage, ar2.exp_name = 9162, "rl", "s3be-fixture"
try:
    ns["_rl_record_input_identity"](ar2, rd)
    raise AssertionError("стадия RL не отказала на необъявленном наборе")
except SystemExit as e:
    assert "ОТКАЗ" in str(e), e
assert not (ckr2 / "run_manifest.json").exists(), "отказ оставил ложный манифест"
PY

echo "  --- 33d. страж: мутанты краснеют, честный манифест зеленеет ---"
S3AV_RUNS="$TMP/s3av-runs"
expect_exit 0 "мутанты собраны (объявлен одно — загружено другое, усечённый хеш, без блока)" \
  python3 - "$S3AV_RUNS" <<'PY'
import json, pathlib, sys
V12_J = "39f616f1e47b1c50490bb9e01167271bac5191c71e4bff727e4940094d6d49a6"
V13_J = "71c4bd2b011b210b906e7c77eca373ad094c14264b86353189faaa0596983b01"
V12_N = "aa06a26dc7917d76dc4be0f6edeefa6562bffa7c937cf00bb90bf3e598a66388"
V13_N = "dc3d4838d15b83c3f1abefb549473e1efd1dc822000a675e34f548f759b94d75"
root = pathlib.Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=True)

def build(name, *, decl_jsonl, decl_sha, decl_samples, act_jsonl, act_sha, act_samples,
          act_tensor, act_tensor_sha, scope="full", block=True, decl_tensor=None,
          blk_decl_jsonl=None, blk_decl_sha=None, blk_declared=True,
          legacy_declared=True):
    """Собрать каталог прогона-мутанта.

    Носителей объявления ДВА, и мутант обязан уметь разводить их между собой
    (S3be): `decl_*` — исторический носитель манифеста прогона AD-2
    (`datasets_extra`/`hyperparameters`), `blk_decl_*` — первичный, `declared_*`
    блока `sft_input` манифеста СТАДИИ. `blk_decl_*` по умолчанию повторяет
    фактический набор (честный случай); `blk_declared=False` снимает поля вовсе —
    тогда слово берёт исторический носитель, и только если и его нет, состояние
    называется «объявления нет».
    """
    #: Стражу отдаётся КАТАЛОГ КАТАЛОГОВ прогонов, а сам прогон — вложенный: у
    #: каждого мутанта свой корень, поэтому вердикт по нему читается отдельно.
    run = root / name / "s3av-run"; (run / "checkpoints").mkdir(parents=True, exist_ok=True)
    # Копия пайплайна стадии с признаком того, что она УМЕЕТ писать sft_input:
    # без него страж справедливо считает прогон старше правила и не красит его.
    (run / "laguna_pipeline_sft.py").write_text(
        "def _sft_record_input_identity(args, dataset):\n    return None\n")
    host = {
        "created_at": "2026-09-22T10:00:00+00:00",
        "stages": [{"name": "sft", "status": "running"}],
        "dataset_path": f"datasets/tok/{decl_tensor or act_tensor}",
        "dataset_sha256": act_tensor_sha}
    if legacy_declared:
        host["datasets_extra"] = {"sft": {"path": f"datasets/{decl_jsonl}",
                                          "sha256": decl_sha}}
        host["hyperparameters"] = {"sft_dataset": f"datasets/{decl_jsonl}",
                                   "sft_dataset_sha256": decl_sha,
                                   "sft_dataset_samples": decl_samples}
    (run / "run_manifest.json").write_text(json.dumps(host, ensure_ascii=False))
    stage = {"stage": "sft", "datasets_hash_scope": "full", "datasets": {"x": "y"}}
    if block:
        stage["sft_input"] = {
            "jsonl": {"path": f"/workspace/shared/datasets/{act_jsonl}", "sha256": act_sha,
                      "hash_scope": scope, "samples": act_samples},
            "tensor": {"path": f"/workspace/shared/datasets/tok/{act_tensor}",
                       "sha256": act_tensor_sha, "hash_scope": scope,
                       "samples": act_samples, "samples_total": act_samples,
                       "candidates": [f"/workspace/shared/datasets/tok/{act_tensor}"]}}
        if blk_declared:
            stage["sft_input"]["declared_path"] = \
                f"/workspace/shared/datasets/{blk_decl_jsonl or act_jsonl}"
            stage["sft_input"]["declared_sha256"] = blk_decl_sha or act_sha
    (run / "checkpoints" / "run_manifest.json").write_text(
        json.dumps(stage, ensure_ascii=False))

# (а) честный: объявлен v13_fixed и прочитан v13_fixed
build("honest", decl_jsonl="sft_train_v13_fixed.jsonl", decl_sha=V13_J, decl_samples=44105,
      act_jsonl="sft_train_v13_fixed.jsonl", act_sha=V13_J, act_samples=44105,
      act_tensor="sft_train_v13_fixed_8192_qwen25.npz", act_tensor_sha=V13_N)
# (б) МУТАНТ S3av: объявлен v13_fixed — загружен v12 (тот самый прогон).
# Объявление разведено ПО ОБОИМ носителям: стадия объявила v13_fixed (declared_*),
# а прочитала v12 — это и есть «набор остался бумажным».
build("mutant-declared-v13-read-v12", decl_jsonl="sft_train_v13_fixed.jsonl", decl_sha=V13_J,
      decl_samples=44105, act_jsonl="sft_train_v12.jsonl", act_sha=V12_J, act_samples=44949,
      act_tensor="sft_train_v12_8192_qwen25.npz", act_tensor_sha=V12_N,
      decl_tensor="sft_train_v13_fixed_8192_qwen25.npz",
      blk_decl_jsonl="sft_train_v13_fixed.jsonl", blk_decl_sha=V13_J)
# (б2) ЗУБ НА НОСИТЕЛЬ: исторический носитель назван ЧЕСТНО (v12 = прочитанному), а
# первичный объявляет v13. Если бы прибор читал только старые поля, мутант был бы
# зелёным — то есть расхождение «объявлено стадией — прочитано стадией» осталось бы
# невидимым ровно там, где оно и возникает (вход стадии — параметр, S3av).
build("mutant-legacy-honest-block-drift", decl_jsonl="sft_train_v12.jsonl", decl_sha=V12_J,
      decl_samples=44949, act_jsonl="sft_train_v12.jsonl", act_sha=V12_J, act_samples=44949,
      act_tensor="sft_train_v12_8192_qwen25.npz", act_tensor_sha=V12_N,
      blk_decl_jsonl="sft_train_v13_fixed.jsonl", blk_decl_sha=V13_J)
# (б3) Объявления в первичном носителе нет — слово берёт исторический, и он сходится
# с фактом: прогон до S3av объявлял набор ТАМ, и это по-прежнему объявление, а не
# «объявления нет» (история не переписывается, ADR-023 п.9).
build("legacy-only-declared", decl_jsonl="sft_train_v13_fixed.jsonl", decl_sha=V13_J,
      decl_samples=44105, act_jsonl="sft_train_v13_fixed.jsonl", act_sha=V13_J,
      act_samples=44105, act_tensor="sft_train_v13_fixed_8192_qwen25.npz",
      act_tensor_sha=V13_N, blk_declared=False)
# (б4) Ни одного носителя: это и есть состояние «объявления нет» — оно обязано быть
# названо, а не превращаться в зелёное молчание.
build("no-declaration-anywhere", decl_jsonl="sft_train_v13_fixed.jsonl", decl_sha=V13_J,
      decl_samples=44105, act_jsonl="sft_train_v13_fixed.jsonl", act_sha=V13_J,
      act_samples=44105, act_tensor="sft_train_v13_fixed_8192_qwen25.npz",
      act_tensor_sha=V13_N, blk_declared=False, legacy_declared=False)
# (в) МУТАНТ: хеш записан не целиком (класс слепоты _sha256_head)
build("mutant-head-hash", decl_jsonl="sft_train_v13_fixed.jsonl", decl_sha=V13_J,
      decl_samples=44105, act_jsonl="sft_train_v13_fixed.jsonl", act_sha=V13_J,
      act_samples=44105, act_tensor="sft_train_v13_fixed_8192_qwen25.npz",
      act_tensor_sha=V13_N, scope="head")
# (г) МУТАНТ: стадия исполнена, а факта нет вовсе
build("mutant-no-block", decl_jsonl="sft_train_v13_fixed.jsonl", decl_sha=V13_J,
      decl_samples=44105, act_jsonl="sft_train_v13_fixed.jsonl", act_sha=V13_J,
      act_samples=44105, act_tensor="sft_train_v13_fixed_8192_qwen25.npz",
      act_tensor_sha=V13_N, block=False)
PY

expect_exit 0 "честный манифест (объявлен v13 — прочитан v13) → зелёный" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/honest" \
  --stand-runs /nonexistent-stand
expect_exit 1 "МУТАНТ «объявлен v13_fixed — загружен v12» → КРАСНЫЙ" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/mutant-declared-v13-read-v12" \
  --stand-runs /nonexistent-stand
expect_contains "declared-actual-mismatch" "находка названа: объявление против факта" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/mutant-declared-v13-read-v12" \
  --stand-runs /nonexistent-stand
expect_contains "tensor-mismatch" "объявленный прогоном тензор не тот, что прочитала стадия" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/mutant-declared-v13-read-v12" \
  --stand-runs /nonexistent-stand
expect_exit 1 "МУТАНТ «хеш записан не целиком» (hash_scope=head) → КРАСНЫЙ" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/mutant-head-hash" \
  --stand-runs /nonexistent-stand
expect_contains "hash-not-full" "усечённый хеш назван отдельной находкой" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/mutant-head-hash" \
  --stand-runs /nonexistent-stand
expect_exit 1 "МУТАНТ «стадия исполнена, факта нет» → КРАСНЫЙ" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/mutant-no-block" \
  --stand-runs /nonexistent-stand
expect_contains "no-input-identity" "отсутствие предъявления факта — находка, не молчание" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/mutant-no-block" \
  --stand-runs /nonexistent-stand

echo "  --- 33д. S3be: носитель объявления назван, и это проверяется подменой ---"
# Зуб на НОСИТЕЛЬ, а не на слово. Прогон, у которого исторический носитель честен
# (v12 = прочитанному), а первичный объявляет v13, обязан быть красным: именно так
# выглядит дефект после S3av, когда вход стадии стал параметром. Если бы прибор
# читал только старые поля, тот же каталог был бы зелёным — тест это и называет.
expect_exit 1 "МУТАНТ «историк честен — объявление стадии разошлось» → КРАСНЫЙ" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/mutant-legacy-honest-block-drift" \
  --stand-runs /nonexistent-stand
expect_contains "declared-actual-mismatch" \
  "красное называет объявление против факта, а не «нет объявления»" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/mutant-legacy-honest-block-drift" \
  --stand-runs /nonexistent-stand
expect_exit 0 "зуб проверен: без первичного носителя тот же каталог был бы зелёным" \
  python3 - "$S3AV_RUNS/mutant-legacy-honest-block-drift" "$TMP" <<'PY'
import json, pathlib, subprocess, sys
run, tmp = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
# Копия каталога со снятым первичным объявлением (declared_*): она и есть «что
# увидел бы прибор, читай он только исторический носитель». Здесь она обязана быть
# зелёной — иначе зуб ничего не доказывал бы. Стражу отдаётся КАТАЛОГ КАТАЛОГОВ
# (`--runs` перебирает подкаталоги), поэтому прогон кладётся вложенным: иначе
# пустой обход дал бы зелёное «ничего не найдено» и зуб был бы фиктивным.
src, alias = run / "s3av-run", tmp / "s3be-legacy-only" / "s3av-run-alias"
alias.mkdir(parents=True, exist_ok=True)
for name in ("run_manifest.json", "laguna_pipeline_sft.py"):
    (alias / name).write_bytes((src / name).read_bytes())
(alias / "checkpoints").mkdir(exist_ok=True)
stage = json.loads((src / "checkpoints" / "run_manifest.json").read_text(encoding="utf-8"))
stage["sft_input"].pop("declared_path", None)
stage["sft_input"].pop("declared_sha256", None)
(alias / "checkpoints" / "run_manifest.json").write_text(
    json.dumps(stage, ensure_ascii=False), encoding="utf-8")
r = subprocess.run([sys.executable, "tools/check_dataset_identity.py", "--runs",
                    str(alias.parent), "--stand-runs", "/nonexistent-stand"],
                   capture_output=True, text=True)
assert r.returncode == 0, (r.returncode, r.stdout[-800:])
# Тот же обход обязан быть НЕпустым: «ничего не найдено» зелёным не считается.
assert "s3av-run-alias" in r.stdout, r.stdout[-800:]
PY
expect_exit 0 "исторический носитель: без declared_* объявление берётся из старых полей → зелёный" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/legacy-only-declared" \
  --stand-runs /nonexistent-stand
expect_exit 1 "МУТАНТ «объявления нет ни в одном носителе» → КРАСНЫЙ" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/no-declaration-anywhere" \
  --stand-runs /nonexistent-stand
expect_contains "no-declaration" "состояние названо: сверять нечего, а не «всё сошлось»" \
  python3 tools/check_dataset_identity.py --runs "$S3AV_RUNS/no-declaration-anywhere" \
  --stand-runs /nonexistent-stand
expect_exit 0 "история не переписывается: старые прогоны — legacy, не находки" \
  python3 tools/check_dataset_identity.py --runs runs/ --stand-runs /nonexistent-stand \
  --since 2026-12-31
echo "== 34. check_execution_proof.py («Доказательство исполнения» у новых ADR, ADR-051 / C-031) =="
# Страж введён по разбору дефекта S3av: решение ADR-042 было исполнено полностью
# (данные исправлены, набор верифицирован, старый сохранён), но доказать, что
# ПЕРЕЗАПУЩЕННАЯ стадия прочла исправленный файл, никто не требовал — проверка
# стояла на артефакте, а не на потребителе, и правило было зелёным. Поэтому у
# стража проверяются обе стороны: красное на «обещано вместо доказано» и на
# «артефакт вместо потребителя», и зелёное на истории (ADR-023 п.12: красное
# by construction сигналом не является).
EP="$TMP/ep"
mkdir -p "$EP"
python3 - "$EP" "docs/adr/ADR-051-identichnost-vhoda-stadii-obyavlennyy-nabor-obyazan-sovpadat-s-prochitannym.md" <<'PY'
import pathlib, re, sys

root = pathlib.Path(sys.argv[1])
real_adr = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
GATE = "2026-09-21"

PROOF = """## Доказательство исполнения

Носитель — поле `checkpoints.run_manifest.sft_input.jsonl.sha256`: полный sha256
набора, который стадия открыла, записан пайплайном в `checkpoints/run_manifest.json`.
Потребитель — страж `tools/check_dataset_identity.py`: он сравнивает объявленный вход
с прочитанным, и строка загрузки набора в логе (`logs/sft.log:42`) обязана совпасть
с числом сэмплов из самого тензора. Носители открываются и читаются человеком.
"""

body = lambda title, date, text: (
    f'---\nid: {title}\ndate: "{date}"\n---\n\n# {title}\n\n{text}\n')

CASES = {
    # Зелёный путь: носитель и потребитель названы.
    "good/ADR-100.md": body("ADR-100", GATE, PROOF),
    # Зелёный путь: история раздела не несёт и красной не становится.
    "legacy/ADR-101.md": body("ADR-101", "2026-09-18", "## Decision\n\nРешение без раздела.\n"),
    # Явная неприменимость с причиной: названное исключение, а не находка.
    "notapp/ADR-102.md": body("ADR-102", "2026-09-22",
                              "## Доказательство исполнения\n\nНе применимо: решение не меняет ни вход "
                              "стадии, ни артефакт-потребитель, ни контракт — потребителя, до которого "
                              "решение должно «дойти», у него нет.\n"),
    # Решение от даты правила без раздела — сам дефект, ради которого страж введён.
    "nosection/ADR-103.md": body("ADR-103", GATE, "## Decision\n\nРешение есть, раздела нет.\n"),
    # Обещание проверки вместо проверки.
    "stub/ADR-104.md": body("ADR-104", GATE,
                            "## Доказательство исполнения\n\nБудет проверено после ближайшего прогона "
                            "стадии; сейчас проверять нечем по причине отсутствия прогона и артефакта.\n"),
    # Артефакт вместо потребителя: «создан» — пишущая сторона.
    "artifact/ADR-105.md": body("ADR-105", GATE,
                                "## Доказательство исполнения\n\nАртефакт создан: `evidence/x.json` "
                                "несёт манифест набора, поле `stage_manifest.sft_input.samples` "
                                "заполнено, путь `data/sft-card-v12.json` указан, sha256 набора в "
                                "карточке приведён полностью. Носители перечислены исчерпывающе, и "
                                "все они лежат в дереве кейса; этого, по замыслу раздела, довольно, "
                                "чтобы признать требование закрытым.\n"),
    # Потребитель назван, носителя нет — проверять нечем.
    "nocarrier/ADR-106.md": body("ADR-106", GATE,
                                 "## Доказательство исполнения\n\nПотребитель прочитал решение и "
                                 "применил его в работе; так уже было в этом кейсе, и порядок "
                                 "передачи работы установлен разделом практики выше. Отдельного "
                                 "носителя здесь нет: сказано, что решение дошло, но не сказано, "
                                 "чем это видно и куда смотреть проверяющему.\n"),
    # Шапка без даты: принадлежность к правилу не определяется.
    "nodate/ADR-107.md": "---\nid: ADR-107\ntitle: \"без даты\"\n---\n\n# ADR-107\n\n## Decision\n\nЕсть.\n",
    # Мутант ИЗ ОБРАЗЦА: у настоящего ADR-051 вырезан раздел — страж обязан покраснеть.
    "mutant-real/ADR-108.md": re.sub(r"## Доказательство исполнения.*?(?=## Alternatives)",
                                     "", real_adr, flags=re.S),
}
for rel, text in CASES.items():
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
(root / "empty").mkdir(exist_ok=True)          # каталог без ADR — NOT-VERIFIED
print("фикстуры-мутанты собраны")
PY

# Носитель и потребитель печатаются поимённо: по этой строке разбирается ложное красное.
expect_contains '"carriers"' "доказательство: носители напечатаны поимённо (--json)" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/good" --json
expect_exit 0 "доказательство: носитель и потребитель названы → зелёное" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/good"
expect_exit 0 "доказательство: ADR-101 (2026-09-18) раздела не несёт → legacy, не красное" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/legacy"
expect_contains 'legacy-not-checked' "доказательство: история названа, а не молча пропущена" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/legacy"
expect_exit 0 "доказательство: «Не применимо» с причиной → зелёное (названное исключение)" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/notapp"
expect_exit 1 "доказательство: решение от 2026-09-21 без раздела → красное" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/nosection"
expect_contains '"no-proof-section"' "доказательство: находка названа правилом, а не общим «нет»" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/nosection" --json
expect_exit 1 "доказательство: «будет проверено» вместо проверки → красное" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/stub"
expect_contains '"proof-stub"' "доказательство: обещание проверки ловится как обещание" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/stub" --json
expect_exit 1 "доказательство: «артефакт создан» без потребителя → красное" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/artifact"
expect_contains '"no-consumer-witness"' "доказательство: пишущая сторона потребителем не считается" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/artifact" --json
expect_exit 1 "доказательство: потребитель без носителя → красное (проверять нечем)" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/nocarrier"
expect_exit 1 "доказательство: ADR без даты в шапке → красное (не датируется)" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/nodate"
expect_exit 1 "доказательство: мутант ИЗ ОБРАЗЦА (раздел вырезан у ADR-051) → красное" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/mutant-real"
expect_exit 0 "доказательство: дерево кейса зелёное — история не краснеет, ADR-051 несёт раздел" \
  python3 tools/check_execution_proof.py
expect_exit 2 "доказательство: каталог без ADR → NOT-VERIFIED (не зелёное)" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/empty"
expect_exit 2 "доказательство: нет каталога ADR → NOT-VERIFIED" \
  python3 tools/check_execution_proof.py --adr-dir "$EP/nope"
echo "== 35. decode-diagnosis: разбор трёх режимов декодирования и свод (свойство весов против артефакта протокола) =="
# Фикстуры синтетические, и метрики в них считает **сам прибор** (language_metrics):
# проверяется согласие инструментов с арифметикой прибора, а не с выдуманными
# полями. Управляемые величины — где кончается ход, длина и повторы 4-грамм;
# ожидаемый вердикт следует из них по объявленному правилу, а не подставлен.
DD="$TMP/dd"
python3 - "$DD" <<'FIXTURE_EOF'
"""Генератор фикстур decode-diagnosis: синтетические отчёты прибора на 104 пробы.

Метрики проб считает **сам прибор** (`language_metrics`), а не подставленные поля:
иначе фикстура проверяла бы согласие свода с выдумкой, а не с арифметикой.
Управляются три вещи, от которых зависит вердикт: где кончается ход
(`stop_reason`), длина и повторы 4-грамм.
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "tools")
import probe_language_split as LS  # noqa: E402

RUN = "runs/sft-decode-diagnosis-20260920"
CKPT_SHA = "72bcbe76a8bc93ba0cb796abebc0b7a4955c1f99a81400e106bd19091531b360"
TOOL_SHA = "99dafa8d9551caaa57b770d4cd321da6f0c09b28deb1885500034c9d62eeb276"
TAGS = ["domain_tool"] * 34 + ["domain_knowledge"] * 25 + ["general_language"] * 17 \
       + ["general_reasoning"] * 13 + ["instruction"] * 15
LOOP = "альфа бета гамма дельта " * 10


def probe(i: int, kind: str) -> dict:
    tag = TAGS[i]
    if kind == "unfinished":
        text = ("<think>\nИщу концепт по запросу номер %d.\n<tool_call>"
                "{\"name\": \"search_concepts\", \"query\": \"q%d\"}</tool_call>" % (i, i))
        return _p(tag, text, "limit_in_tool_call", 8192, True, i)
    if kind == "unfinished_loop":
        text = ("<think>\n" + LOOP + "\n<tool_call>"
                "{\"name\": \"search_concepts\", \"query\": \"q%d\"}</tool_call>" % i)
        return _p(tag, text, "limit_in_tool_call", 8192, True, i)
    if kind == "badjson":
        text = ("<think>\nИщу.\n</think>\n<tool_call>{\"name\": \"search_concepts\", "
                "\"query\": q%d}</tool_call>" % i)
        return _p(tag, text, "turn_end", 300, False, i)
    if kind == "loop":
        text = "<think>\n" + LOOP + "\n</think>\nОтвет номер %d." % i
        return _p(tag, text, "turn_end", 700, False, i)
    if kind == "unclosed":
        text = "<think>\nРассуждение без закрытия, номер %d." % i
        return _p(tag, text, "turn_end", 200, False, i)
    if kind == "call":
        text = ("<think>\nРазберу задачу.\n</think>\n<tool_call>{\"name\": "
                "\"search_concepts\", \"query\": \"q%d\"}</tool_call>\nОтвет готов." % i)
        return _p(tag, text, "turn_end", 420, False, i)
    text = "<think>\nКороткое рассуждение, номер %d.\n</think>\nОтвет номер %d." % (i, i)
    return _p(tag, text, "turn_end", 250, False, i)


def _p(tag: str, text: str, stop: str, n: int, hit: bool, i: int) -> dict:
    return {"tag": tag, "system": False, "prompt": "промпт %d" % i, "response": text,
            "n_new_tokens": n, "hit_limit": hit, "stop_reason": stop,
            "decoding": "", "batch_size": 8, "metrics": LS.language_metrics(text)}


#: Раскладка «штатного» состояния: 32 незавершённых вызова (как у S3aq, 0.3077),
#: 10 из них зациклены, 30 с валидным вызовом, 13 коротких, 16 петель, 12 незакрытых
#: при естественном конце, 3 с битым JSON. Сумма — 104.
LAYOUT = (["unfinished"] * 22 + ["unfinished_loop"] * 10 + ["call"] * 30 + ["loop"] * 16
          + ["unclosed"] * 12 + ["badjson"] * 3 + ["plain"] * 11)


def state(kind: str) -> dict:
    if kind == "closed":
        # Альтернативный режим «вылечил» незавершённые вызовы: они стали
        # дописанными и короткими. Метрики пересчитываются прибором.
        probes = [probe(i, "call" if LAYOUT[i].startswith("unfinished") else LAYOUT[i])
                  for i in range(104)]
    elif kind == "same":
        probes = [probe(i, LAYOUT[i]) for i in range(104)]
    elif kind == "one_diff":
        probes = [probe(i, LAYOUT[i]) for i in range(104)]
        probes[7]["response"] = probes[7]["response"] + " (другой вход)"
        probes[7]["metrics"] = LS.language_metrics(probes[7]["response"])
    else:
        raise SystemExit(f"неизвестный вид состояния: {kind}")
    return {"checkpoint": "/home/user/s3aq-ckpts/sft_probe_21500.pt",
            "checkpoint_bytes": 2963139967, "checkpoint_sha256": CKPT_SHA,
            "load": {"moved_for_resize": [], "missing_keys": 0, "unexpected_keys": 0},
            "probes": probes, "aggregate": LS.aggregate(probes, 8192)}


def report(decoding: str, kind: str, allowed: bool = True) -> dict:
    st = state(kind)
    return {
        "schema": "probe-language-split/1", "stage": "фикстура decode-diagnosis",
        "tool": "tools/probe_language_split.py", "tool_sha256": TOOL_SHA,
        "device": "cpu (фикстура)", "model_provenance": {"weights": "фикстура"},
        "protocol": {"prompts_set": "wide", "prompts_digest": "фикстура",
                     "n_prompts": 104, "decoding": decoding,
                     "decoding_params": {"decoding": "greedy", "no_repeat_ngram": 4},
                     "decoding_protocol": {"allowed_for_conclusions": allowed},
                     "batch_size": 8, "max_new_tokens": 8192,
                     "stop_at_turn_end": True, "instrument": 2},
        "states": {"sft_v13_21500": st},
    }


MODES = (("v1_greedy_nogram4", "greedy_nogram4"),
         ("v2_greedy_rp115", "greedy_rp1.15"),
         ("v3_sample_t07_p09_rp11", "sample_T0.7_top_p0.9_rp1.1_nogram4"))


#: Заглушка пайплайна: в корень фикстуры кладётся файл с тем же регекспом, что у
#: свода, — иначе проверку «определение вызова сверено с исходником» не на чем
#: провести. Здесь проверяется механизм (есть паттерн → сверка проходит, нет →
#: находка); в реальном прогоне сверяется настоящий laguna_pipeline_v8.py.
import analyze_decode_modes as AD  # noqa: E402
PIPELINE_STUB = ('def execute_tool_call(text):\n'
                 '    m = re.search(r"' + AD.TOOL_CALL_RE.pattern + '", text, re.DOTALL)\n')


def write_case(root: Path, alt_kind: str, v1_kind: str, modes=MODES) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "laguna_pipeline_v8.py").write_text(PIPELINE_STUB, encoding="utf-8")
    run = root / RUN
    (run / "reference").mkdir(parents=True, exist_ok=True)
    ref = report("greedy_nogram4", "same")
    (run / "reference" / "s3aq_format_wide_8192.json").write_text(
        json.dumps(ref, ensure_ascii=False), encoding="utf-8")
    for stem, dec in modes:
        kind = v1_kind if stem == "v1_greedy_nogram4" else alt_kind
        allowed = stem != "v2_greedy_rp115"
        (run / f"{stem}.json").write_text(
            json.dumps(report(dec, kind, allowed), ensure_ascii=False), encoding="utf-8")
    (run / "reference" / "provenance.json").write_text(json.dumps(
        {"tool_identity": True, "git_commit": "0" * 40, "sha256": "1" * 64},
        ensure_ascii=False), encoding="utf-8")
    (run / "pin_check.json").write_text(json.dumps({"all_identical": True}),
                                        encoding="utf-8")
    (run / "probe_record.json").write_text(json.dumps({"stage": "фикстура"}),
                                           encoding="utf-8")
    if (run / "v1_greedy_nogram4.json").is_file():
        import probe_language_split as LS2
        rep = json.loads((run / "v1_greedy_nogram4.json").read_text(encoding="utf-8"))
        LS2.write_run_manifest(run / "v1_greedy_nogram4.json", rep, None)
        man = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
        man["mode_set"] = {"modes_present": len(modes)}
        (run / "run_manifest.json").write_text(json.dumps(man, ensure_ascii=False),
                                               encoding="utf-8")


def write_probe_log(run: Path, probes: list, *, mode: str = "greedy_nogram4",
                    corrupt: str | None = None) -> None:
    """Журнал оборванного луча V1 — **в том формате, что печатает прибор**.

    Формат не выдуман: строка собирается из тех же полей и в том же порядке, что
    `note(f"  [{tag}/{mode}] {p['tag']}: cyr_think=...")` прибора. Разбор сверяет
    формат с исходником прибора (`log_format_check`), поэтому выдуманный формат
    фикстуры не прошёл бы — и это правильно: фикстура обязана говорить на языке
    прибора, иначе она проверяет согласие разбора с самой собой.
    """
    (run / "v1_greedy_nogram4.log").parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for p in probes:
        m = p["metrics"]
        cyr_think, tok, stop = m["cyr_think"], p["n_new_tokens"], p["stop_reason"]
        if corrupt == "tokens":
            tok = tok + 1          # чужая длина: тождество обязано сломаться
        elif corrupt == "cyr_think":
            cyr_think = 0.1234
        lines.append(
            f"  [sft_v13_21500/{mode}] {p['tag']}: cyr_think={cyr_think} "
            f"cyr_answer={m['cyr_answer']} (v1 {m['legacy']['cyr_answer']}) "
            f"mode={m['has_think']}/{m['has_tool_call']} tok={tok} stop={stop}"
            f"{' (ЛИМИТ)' if p['hit_limit'] else ''}"
            f"{' ЗАЦИКЛ' if m['looped'] else ''} | {p['response'][:70]!r}")
    (run / "v1_greedy_nogram4.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def historical_case(root: Path, *, alt: str = "same", comparison: str = "identical") -> None:
    """Фикстура «житого луча V1 нет»: отчёты V2/V3 + журнал оборванного V1.

    `comparison` управляет тем, сходится ли журнал с решающим отчётом: `identical`
    (сверка обязана пройти), `tokens`/`cyr_think` (обязана сломаться). Правка ровно
    одна и названа — иначе непонятно, что именно поймал контроль.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "laguna_pipeline_v8.py").write_text(PIPELINE_STUB, encoding="utf-8")
    # Ссылка на **настоящий** исходник прибора, а не заглушка: разбор журнала —
    # копия формата, и проверка «формат взят из прибора» обязана сверяться с
    # прибором, а не с выдумкой фикстуры. Заглушка здесь проверяла бы согласие
    # разбора с самой собой.
    tools_link = root / "tools"
    if not tools_link.exists():
        tools_link.symlink_to(Path.cwd().resolve() / "tools")
    run = root / RUN
    (run / "reference").mkdir(parents=True, exist_ok=True)
    ref = report("greedy_nogram4", "same")
    (run / "reference" / "s3aq_format_wide_8192.json").write_text(
        json.dumps(ref, ensure_ascii=False), encoding="utf-8")
    for stem, dec in MODES[1:]:
        (run / f"{stem}.json").write_text(
            json.dumps(report(dec, alt, stem != "v2_greedy_rp115"), ensure_ascii=False),
            encoding="utf-8")
    # живого отчёта V1 нет — намеренно
    probes = ref["states"]["sft_v13_21500"]["probes"]
    write_probe_log(run, probes[:8],
                    corrupt=None if comparison == "identical" else comparison)
    (run / "reference" / "provenance.json").write_text(json.dumps(
        {"tool_identity": True, "git_commit": "0" * 40, "sha256": "1" * 64},
        ensure_ascii=False), encoding="utf-8")
    (run / "pin_check.json").write_text(json.dumps({"all_identical": True}),
                                        encoding="utf-8")
    (run / "probe_record.json").write_text(json.dumps({"stage": "фикстура"}),
                                           encoding="utf-8")
    import probe_language_split as LS3
    rep2 = json.loads((run / "v2_greedy_rp115.json").read_text(encoding="utf-8"))
    LS3.write_run_manifest(run / "v2_greedy_rp115.json", rep2, None)
    man = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
    man["mode_set"] = {"modes_present": 2}
    (run / "run_manifest.json").write_text(json.dumps(man, ensure_ascii=False),
                                           encoding="utf-8")


def mutate(src: Path, dst: Path, change) -> None:
    """Копия фикстуры с одной правкой — для негативных ветвей контракта входа.

    Правка ровно одна и названа: иначе непонятно, что именно поймал контракт.
    """
    shutil.copytree(src, dst, dirs_exist_ok=True)
    run = dst / RUN
    stem = "v1_greedy_nogram4.json"
    d = json.loads((run / stem).read_text(encoding="utf-8"))
    change(d, run)
    (run / stem).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    root = Path(sys.argv[1])
    root.mkdir(parents=True, exist_ok=True)
    for name, alt, v1 in (("persist", "same", "same"),
                          ("artifact", "closed", "same"),
                          ("broken", "same", "one_diff")):
        (root / name).mkdir(parents=True, exist_ok=True)
        write_case(root / name, alt, v1)
    # неполный прогон: снят только V1
    (root / "partial").mkdir(parents=True, exist_ok=True)
    write_case(root / "partial", "same", "same", modes=MODES[:1])
    # нечего разбирать
    (root / "empty").mkdir(parents=True, exist_ok=True)
    (root / "empty" / RUN).mkdir(parents=True, exist_ok=True)
    # негативные ветви контракта входа: чужой чекпойнт и подменённое имя режима
    mutate(root / "persist", root / "wrong_ckpt",
           lambda d, r: d["states"]["sft_v13_21500"].__setitem__("checkpoint_sha256", "0" * 64))
    mutate(root / "persist", root / "wrong_mode",
           lambda d, r: d["protocol"].__setitem__("decoding", "greedy_bez_zapreta"))
    # снят один альтернативный луч (V1+V2): вердикт считается, но помечен
    # partial_evidence — «лучи не изменили ничего» и «луч не снят» разные вещи
    (root / "one").mkdir(parents=True, exist_ok=True)
    write_case(root / "one", "same", "same", modes=MODES[:2])
    # пайплайн, в котором определения вызова нет: сверка обязана это назвать
    write_case(root / "nopattern", "same", "same")
    (root / "nopattern" / "laguna_pipeline_v8.py").write_text(
        "def execute_tool_call(text):\n    return None, False, text\n", encoding="utf-8")
    # исторический контроль: живого отчёта V1 нет, есть журнал оборванного прогона
    historical_case(root / "hist")
    # ...и две ветви, где журнал с отчётом не сходится (чужая длина / чужой cyr_think)
    historical_case(root / "hist_badtok", comparison="tokens")
    historical_case(root / "hist_badthink", comparison="cyr_think")
    print("фикстуры decode-diagnosis:", ", ".join(sorted(p.name for p in root.iterdir())))
    return 0


if __name__ == "__main__":
    sys.exit(main())

FIXTURE_EOF

expect_exit 0 "разбор: альтернативные режимы не сдвинули индикатор — свойство весов" \
  python3 tools/analyze_decode_modes.py --case-root "$DD/persist"
expect_exit 0 "разбор: режимы закрыли вызовы — артефакт протокола" \
  python3 tools/analyze_decode_modes.py --case-root "$DD/artifact"
expect_exit 1 "разбор: тождество V1 с решающим отчётом нарушено — отказ" \
  python3 tools/analyze_decode_modes.py --case-root "$DD/broken"
expect_exit 1 "разбор: режим не снят — отказ (частичный прогон называет себя частичным)" \
  python3 tools/analyze_decode_modes.py --case-root "$DD/partial"
expect_exit 1 "разбор: чужой чекпойнт — отказ" \
  python3 tools/analyze_decode_modes.py --case-root "$DD/wrong_ckpt"
expect_exit 1 "разбор: подменённое имя режима — отказ" \
  python3 tools/analyze_decode_modes.py --case-root "$DD/wrong_mode"
expect_exit 2 "разбор: нечего разбирать — NOT-VERIFIED" \
  python3 tools/analyze_decode_modes.py --case-root "$DD/empty"
expect_exit 1 "разбор: снят один альтернативный луч — отказ (прогон называет себя неполным)" \
  python3 tools/analyze_decode_modes.py --case-root "$DD/one"
expect_exit 1 "разбор: определения вызова разошлись с пайплайном — находка, а не тихий пропуск" \
  python3 tools/analyze_decode_modes.py --case-root "$DD/nopattern"
# ── исторический контроль ─────────────────────────────────────────────────────
# Подстановка разрешена только флагом, требует журнала и подтверждается частично.
expect_exit 1 "исторический контроль: без флага отчёт V1 не подменяется молча" \
  python3 tools/analyze_decode_modes.py --case-root "$DD/hist"
expect_exit 0 "исторический контроль: с флагом разбор идёт и вердикт считается" \
  python3 tools/analyze_decode_modes.py --historical-control --case-root "$DD/hist"
expect_exit 0 "исторический контроль: роль V1 названа полем, а не подразумевается" \
  python3 -c "import json;d=json.load(open('$DD/hist/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));assert d['control_mode']['kind']=='historical_s3aq_report', d['control_mode']"
expect_exit 0 "исторический контроль: побайтовый контроль НЕ выдаётся за пройденный" \
  python3 -c "import json;d=json.load(open('$DD/hist/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));assert d['identity_control_v1_vs_s3aq'] is None and d['partial_identity_control']['kind']=='partial', d['identity_control_v1_vs_s3aq']"
expect_exit 0 "исторический контроль: сверенные пробы названы числом, неизмеренные — тоже" \
  python3 -c "import json;d=json.load(open('$DD/hist/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));p=d['partial_identity_control'];assert p['compared']==8 and p['matched']==8 and p['unmeasured']==96 and p['identical'] is True, p"
expect_exit 0 "исторический контроль: формат журнала сверен с исходником прибора" \
  python3 -c "import json;d=json.load(open('$DD/hist/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));f=d['partial_identity_control']['log_format_check'];assert f['checked'] is True and f['format_present'] is True, f"
expect_exit 1 "исторический контроль: чужая длина в журнале — тождество нарушено, отказ" \
  python3 tools/analyze_decode_modes.py --historical-control --case-root "$DD/hist_badtok"
expect_exit 0 "исторический контроль: расхождение названо полем и индексом, а не счётом" \
  python3 -c "import json;d=json.load(open('$DD/hist_badtok/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));p=d['partial_identity_control'];assert p['identical'] is False and p['mismatches'] and 'n_new_tokens' in p['mismatches'][0]['fields'], p"
expect_exit 1 "исторический контроль: чужой cyr_think в журнале — тоже отказ" \
  python3 tools/analyze_decode_modes.py --historical-control --case-root "$DD/hist_badthink"
expect_exit 0 "исторический контроль: без журнала подстановка запрещена (утверждение ≠ сверка)" \
  python3 -c "
import json, shutil, pathlib, subprocess, sys
src = pathlib.Path('$DD/hist'); dst = pathlib.Path('$DD/hist_nolog')
shutil.copytree(src, dst, dirs_exist_ok=True)
(dst / 'runs/sft-decode-diagnosis-20260920/v1_greedy_nogram4.log').unlink()
r = subprocess.run([sys.executable, 'tools/analyze_decode_modes.py', '--historical-control',
                    '--case-root', str(dst)], capture_output=True)
assert r.returncode == 1, r.returncode
d = json.loads((dst / 'runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json').read_text(encoding='utf-8'))
assert any('журнал' in p for p in d['problems']), d['problems']
"
expect_exit 0 "ось длины выведена числами: «усиливается» — из сдвига доли, а не назначено" \
  python3 -c "import json;d=json.load(open('$DD/hist/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));a=d['answer']['length_axis']['V2'];assert a['direction'] in ('усиливается','ослабевает','без значимого сдвига') and a['control_budget_hit'] is not None and a['allowed_for_conclusions'] is False, a"
expect_exit 0 "ветка правила названа полем: третий исход отличается от «данных нет»" \
  python3 -c "import json;d=json.load(open('$DD/artifact/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));assert d['answer']['rule_branch']['fired']=='first', d['answer']['rule_branch']"
expect_exit 0 "вердикт по одному лучу помечен partial_evidence, а не выдан за полный" \
  python3 -c "import json;d=json.load(open('$DD/one/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));assert d['answer']['partial_evidence'] is True and d['answer']['alternative_modes_measured']==['V2'], d['answer']"
expect_exit 0 "вердикт артефакта протокола назван полем answer, а не текстом" \
  python3 -c "import json;d=json.load(open('$DD/artifact/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));assert d['answer']['verdict']=='protocol_artifact', d['answer']['verdict']"
expect_exit 0 "вердикт свойства весов назван полем answer" \
  python3 -c "import json;d=json.load(open('$DD/persist/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));assert d['answer']['verdict']=='weights_property', d['answer']['verdict']"
expect_exit 0 "первичный индикатор напечатан числом по каждому режиму" \
  python3 -c "import json;d=json.load(open('$DD/artifact/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));p=d['answer']['primary_indicator'];assert abs(p['V1']-0.3077)<1e-3 and p['V2']==0.0 and p['V3']==0.0, p"
expect_exit 0 "парный точный Мак-Немар посчитан, расхождения названы числом" \
  python3 -c "import json;d=json.load(open('$DD/artifact/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));r=[x for x in d['comparisons_vs_v1']['V2']['rows'] if x['metric']=='unfinished_tool_call'][0];assert r['mcnemar_exact']['base_only']==32 and r['significant'] is True, r"
expect_exit 0 "порог значимости — тот же, что в своде S3aq (0.10 при n = 104)" \
  python3 -c "import json;d=json.load(open('$DD/persist/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));s=d['comparisons_vs_v1']['V2']['significance'];assert s['threshold_pp']==0.10 and s['n']==104, s"
expect_exit 0 "ядро метрик — вызов арифметики прибора: агрегат отчёта воспроизведён" \
  python3 -c "import json;d=json.load(open('$DD/persist/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));assert all(m['recompute_check']['identical'] for m in d['modes'].values()), d['modes']['V1']['recompute_check']"
expect_exit 0 "определение JSON-валидности сверено с исходником пайплайна" \
  python3 -c "import json;d=json.load(open('$DD/persist/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));assert d['pipeline_definition']['pattern_present'] is True, d['pipeline_definition']"
expect_exit 0 "незавершённые вызовы и петли среди них считаются раздельно" \
  python3 -c "import json;d=json.load(open('$DD/persist/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));e=d['modes']['V1']['extra'];assert e['unfinished_tool_call']['n']==32 and e['among_unfinished_tool_call']['looped']==0.3125, e['among_unfinished_tool_call']"
expect_exit 0 "битый JSON вызова назван числом (доля валидных среди ходов с вызовом)" \
  python3 -c "import json;d=json.load(open('$DD/persist/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));j=d['modes']['V1']['extra']['json_valid']['with_tool_call'];assert 0 < j['share'] < 1 and j['n'] > 0, j"
expect_exit 0 "«чего не решает» перечислено, а не оставлено пустым" \
  python3 -c "import json;d=json.load(open('$DD/persist/runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json'));assert len(d['not_decided'])>=5 and any('ADR-041' in x for x in d['not_decided']), d['not_decided']"

echo "== 36. decode-diagnosis: условие отказа V3, страж CUDA-прогона и пригодность rollout'ов =="
# Фикстуры этой секции проверяют не «зелёный после правки», а два утверждения,
# которые обязаны быть ложными до неё:
#   * у многочасового CUDA-прогона есть **объявленный** запрет сна, и расписка
#     называет состояние устройства и число дошедших проб (стража не существовало);
#   * свод пригодности rollout'ов **проверяет** вход (прибор, лог, журнал), а не
#     пересказывает: подделанное число проб или хеш прибора обязаны его остановить.
G33="$TMP/g33"
mkdir -p "$G33"
G=tools/guard_cuda_run.sh
RUN33="runs/sft-decode-diagnosis-20260920"
CRASHED_LOG="$RUN33/v3_sample_t07_p09_rp11.log"

# ── страж CUDA-прогона ────────────────────────────────────────────────────────
tools/guard_cuda_run.sh --check --out "$G33/guard_check.json" >/dev/null 2>&1
echo $? > "$G33/guard_check.rc"
expect_exit 0 "страж: --check называет состояние устройства и согласован с кодом возврата" \
  python3 -c "import json,sys;d=json.load(open(sys.argv[1]));rc=int(open(sys.argv[2]).read().strip());assert d['verdict'] in ('ok','device_unusable'), d['verdict'];assert d['exit_code']==rc, (d['exit_code'],rc);assert d['device']['health'] in ('ok','unusable'), d['device']['health']" "$G33/guard_check.json" "$G33/guard_check.rc"
expect_exit 1 "страж: без --out отказывает (отказ неотличим от успеха, если расписки нет)" \
  "$G" --device-check skip -- /bin/true
expect_exit 1 "страж: без нагрузки и без --check отказывает" \
  "$G" --out "$G33/guard_nopayload.json"
expect_exit 1 "страж: --check с запретом читать устройство отказывает (иначе расписка утверждала бы неизмеренное)" \
  "$G" --check --device-check skip --out "$G33/guard_conflict.json"

# Синтетический журнал: источник Xid отдаёт по строке за вызов, поэтому «до» и
# «после» различаются — красный путь проверяется без ожидания настоящего отказа GPU.
: > "$G33/xid_fake.txt"
XID_SRC="cat $G33/xid_fake.txt; echo 'NVRM: Xid (PCI:0000:01:00): 31, pid=1, name=python3' >> $G33/xid_fake.txt"
expect_exit 5 "страж: новый Xid за время прогона → device_faulted (красный контур)" \
  "$G" --device-check skip --xid-source "$XID_SRC" --probe-log "$CRASHED_LOG" \
       --out "$G33/guard_xid.json" -- /bin/true
expect_exit 0 "страж: расписка называет число дошедших проб по логу прибора (24 из 104)" \
  python3 -c "import json;d=json.load(open('$G33/guard_xid.json'));assert d['probes']['completed']==24,d['probes'];assert d['xid']['new']==1,d['xid'];assert d['verdict']=='device_faulted',d['verdict']"
expect_exit 0 "страж: расписка полна и несёт причину вердикта, а не только код" \
  python3 -c "import json;d=json.load(open('$G33/guard_xid.json'));assert set(d)>= {'schema','created_at','device_check','device','xid','sleep_inhibit','payload','probes','verdict','exit_code','warnings','why'},set(d);assert d['why'] and isinstance(d['warnings'],list)"
# Запрет сна: либо взят и подтверждён самопроверкой, либо страж отказал и назвал это.
# Второе — тоже верное поведение (машина без systemd-inhibit), поэтому проверяются оба.
expect_exit 0 "страж: запрет сна взят и подтверждён, либо отказ назван отказом" \
  python3 -c "import json;d=json.load(open('$G33/guard_xid.json'));si=d['sleep_inhibit'];assert (si['held'] and si['self_test']) or d['verdict']=='refused_no_inhibit',si"
expect_exit 4 "страж: код нагрузки проброшен отдельным вердиктом (payload_failed = 4)" \
  "$G" --device-check skip --out "$G33/guard_rc.json" -- sh -c 'exit 3'
expect_exit 0 "страж: ненулевой код нагрузки — payload_failed, а не отказ устройства" \
  python3 -c "import json;d=json.load(open('$G33/guard_rc.json'));assert d['verdict']=='payload_failed' and d['exit_code']==4 and d['xid']['new']==0,d"

# ── разбор пригодности rollout'ов ─────────────────────────────────────────────
# Фикстура — отчёт прибора, метрики проб считает **сам прибор** (`language_metrics`),
# а не подставленные поля: проверяется правило счёта, а не согласие с выдумкой.
python3 - "$G33/fake_report.json" <<'FIXTURE_EOF'
import json, sys
sys.path.insert(0, "tools")
import probe_language_split as LS  # noqa: E402

LOOP = "альфа бета гамма дельта " * 10


def rec(kind: str, i: int) -> dict:
    if kind == "reached":
        text, stop, tok, lim = "<think>\nКороткое рассуждение.\n</think>\nОтвет.", "turn_end", 300, False
    elif kind == "looped":
        text, stop, tok, lim = "<think>\n" + LOOP + "\n</think>\nОтвет.", "turn_end", 700, False
    elif kind == "unclosed":
        text, stop, tok, lim = "<think>\nРассуждение без закрытия, номер %d." % i, "turn_end", 200, False
    else:
        text, stop, tok, lim = "<think>\nДлинное рассуждение без конца.", "limit_in_think", 4096, True
    return {"tag": "instruction", "system": False, "prompt": "p%d" % i, "response": text,
            "n_new_tokens": tok, "hit_limit": lim, "stop_reason": stop,
            "decoding": "sample_T0.7_top_p0.9_rp1.1_nogram4", "batch_size": 1,
            "metrics": LS.language_metrics(text)}


doc = {"schema": "probe-language-split/1", "tool_sha256": "synthetic", "device": "cpu",
       "protocol": {"prompts_set": "synthetic", "n_prompts": 4, "decoding": "sample_T0.7_top_p0.9_rp1.1_nogram4",
                    "decoding_params": {"decoding": "sample", "temperature": 0.7, "top_p": 0.9,
                                        "repetition_penalty": 1.1, "no_repeat_ngram": 4, "seed": 42},
                    "batch_size": 1, "max_new_tokens": 4096, "stop_at_turn_end": True, "instrument": 2},
       "states": {"synthetic": {"probes": [rec("reached", 0), rec("looped", 1),
                                           rec("unclosed", 2), rec("truncated", 3)],
                                "aggregate": {"lengths": {"budget": 4096}}}}}
json.dump(doc, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False)
FIXTURE_EOF

expect_exit 0 "пригодность: дошёл до ответа — только закрытый ход без петли (1 из 4)" \
  python3 tools/analyze_rollout_reach.py --report "$G33/fake_report.json" --out "$G33/reach_fake.json"
expect_exit 0 "пригодность: правило счёта названо полем, а не выведено читателем" \
  python3 -c "import json;d=json.load(open('$G33/reach_fake.json'));assert 'turn_end' in d['rule'] and 'unclosed_think' in d['rule'] and 'looped' in d['rule'],d['rule']"
expect_exit 0 "пригодность: усечённый ход в «дошли» не попадает, доля усечённых названа" \
  python3 -c "import json;s=json.load(open('$G33/reach_fake.json'))['reports'][0]['states']['synthetic'];assert s['reached_answer_no_loop']==1 and s['truncated']==1 and abs(s['truncated_share']-0.25)<1e-9,s"
expect_exit 0 "пригодность: сатурация прибора (ADR-050 п.4.3) гасит вердикт по формату" \
  python3 -c "import json;s=json.load(open('$G33/reach_fake.json'))['reports'][0]['states']['synthetic'];assert s['saturation']['saturated'] is True and s['format_verdict_issuable'] is False,s['saturation']"
expect_exit 0 "пригодность: незакрытый think считается только среди естественно завершённых (ADR-045 п.5)" \
  python3 -c "import json;s=json.load(open('$G33/reach_fake.json'))['reports'][0]['states']['synthetic'];assert s['natural_stop']==3 and abs(s['unclosed_think_share_among_natural']-1/3)<1e-9,s"
expect_exit 2 "пригодности нечего считать (нет --report) — NOT-VERIFIED" \
  python3 tools/analyze_rollout_reach.py
expect_exit 2 "пригодность: отчёта нет — NOT-VERIFIED, а не ноль" \
  python3 tools/analyze_rollout_reach.py --report "$G33/нет-такого.json"
printf 'не json\n' > "$G33/broken.json"
expect_exit 2 "пригодность: битый отчёт — NOT-VERIFIED" \
  python3 tools/analyze_rollout_reach.py --report "$G33/broken.json"
expect_exit 0 "пригодность: на контрольном отчёте 4096 — 35 из 104 ходов на чекпойнте 21 500" \
  python3 tools/analyze_rollout_reach.py --report "$RUN33/reference/greedy_format_wide_4096_pair.json" --out "$G33/reach_control.json"
expect_exit 0 "пригодность: контрольный ряд сатурирован (47 % в бюджет) — вердикт по формату не выносится" \
  python3 -c "import json;r=json.load(open('$G33/reach_control.json'))['reports'][0]['states'];s=r['sft_v13_21500'];assert s['reached_answer_no_loop']==35 and abs(s['truncated_share']-0.4712)<1e-4 and s['format_verdict_issuable'] is False,s"

# ── свод пригодности rollout'ов ───────────────────────────────────────────────
expect_exit 0 "свод пригодности собран и вход сошёлся (прибор, лог, журнал)" \
  python3 tools/assemble_rl_rollout_viability.py \
    --reach "$RUN33/reach_survey.json" --loop-origin "$RUN33/loop_origin_greedy4096.json" \
    --crash-facts "$RUN33/repro/crash_facts.json" --prior evidence/s3ak-degeneration.json \
    --guard "$G" --out "$G33/viability.json"
expect_exit 0 "свод: вход сошёлся без единой ошибки, и это записано полем" \
  python3 -c "import json;d=json.load(open('$G33/viability.json'));assert d['input_errors']==[],d['input_errors']"
expect_exit 0 "свод: прибор не тронут — хеш сверен и с объявленным, и с пиннутым" \
  python3 -c "import json;d=json.load(open('$G33/viability.json'))['crash']['instrument'];assert d['matches_declared'] and d['matches_pinned_instrument'],d"
expect_exit 0 "свод: условие падения названо S3 и подтверждено журналом ядра" \
  python3 -c "import json;j=json.load(open('$G33/viability.json'))['crash']['journal'];assert j.get('verifiable') is True and j['all_present'] is True,j"
expect_exit 0 "свод: число проб сверено с логом (24 из 104), а не пересказано" \
  python3 -c "import json;l=json.load(open('$G33/viability.json'))['crash']['log'];assert l['probes_in_log']==24 and l['probes_match'] and l['has_launch_failure'],l"
expect_exit 0 "свод: статус назван так, что «диагноз есть, измерения нет» не читается как измерено" \
  python3 -c "import json;d=json.load(open('$G33/viability.json'));assert d['status']=='diagnosis_complete_measurement_partial' and d['status_note'],d['status']"
expect_exit 0 "свод: число дошедших проб названо и в блоке отказа, а не только в логе" \
  python3 -c "import json;c=json.load(open('$G33/viability.json'))['crash'];assert c['probes_completed']==24 and c['probes_total']==104,c"
expect_exit 0 "свод: вердикт по сэмплированию не выносится — режим не измерен" \
  python3 -c "import json;a=json.load(open('$G33/viability.json'))['answer'];assert a['sample_arm_measured'] is False and a['verdict']=='not_established_sample_arm_not_measured' and 'НЕ ИЗМЕРЕН' in a['text'],a['verdict']"
expect_exit 0 "свод: число штатного режима приведено в тексте ответа, а не спрятано в поле" \
  python3 -c "import json;a=json.load(open('$G33/viability.json'))['answer'];assert '35 из 104' in a['text'] and '0.4712' in a['text'],a['text']"
expect_exit 0 "свод: границы («чего не проверено») перечислены, а не пусты" \
  python3 -c "import json;d=json.load(open('$G33/viability.json'));assert len(d['not_checked'])>=5 and any('32 600' in x for x in d['not_checked']),d['not_checked']"
expect_exit 0 "свод: прежние числа сэмплирования названы с расхождениями, а не как свои" \
  python3 -c "import json;p=json.load(open('$G33/viability.json'))['metrics']['prior_evidence'];assert p['state']=='sft_resume_6000' and len(p['mismatches_with_this_delta'])==3,p"
expect_exit 0 "свод: происхождение петель названо числом (собственная дегенерация весов)" \
  python3 -c "import json;l=json.load(open('$G33/viability.json'))['answer']['loop_origin'];assert l['verdict']=='own_degeneration' and l['n_loops']>0,l"

# Красные пути свода: он обязан остановиться, если вход подделан.
python3 - "$RUN33/repro/crash_facts.json" "$G33/facts_badtok.json" "$G33/facts_badsha.json" <<'TAMPER_EOF'
import json, sys
src = json.load(open(sys.argv[1], encoding="utf-8"))
a = json.loads(json.dumps(src))
a["crash"]["probes_completed"] = 25
json.dump(a, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
b = json.loads(json.dumps(src))
b["crash"]["instrument_sha256"] = "0" * 64
json.dump(b, open(sys.argv[3], "w", encoding="utf-8"), ensure_ascii=False)
TAMPER_EOF
expect_exit 1 "свод отказывает, когда число проб в фактах не сходится с логом" \
  python3 tools/assemble_rl_rollout_viability.py \
    --reach "$RUN33/reach_survey.json" --loop-origin "$RUN33/loop_origin_greedy4096.json" \
    --crash-facts "$G33/facts_badtok.json" --out "$G33/viability_badtok.json"
expect_exit 1 "свод отказывает, когда объявленный хеш прибора не совпал с файлом" \
  python3 tools/assemble_rl_rollout_viability.py \
    --reach "$RUN33/reach_survey.json" --loop-origin "$RUN33/loop_origin_greedy4096.json" \
    --crash-facts "$G33/facts_badsha.json" --out "$G33/viability_badsha.json"
expect_exit 2 "своду нечего сводить (нет разбора пригодности) — NOT-VERIFIED" \
  python3 tools/assemble_rl_rollout_viability.py \
    --reach "$G33/нет-такого.json" --crash-facts "$RUN33/repro/crash_facts.json" \
    --out "$G33/viability_none.json"

# Артефакт в дереве обязан совпасть с тем, что даёт инструмент: иначе «числа в
# evidence» и «числа инструмента» разошлись бы молча (ADR-023 п.10).
expect_exit 0 "артефакт evidence/rl-rollout-viability.json сходится с инструментом" \
  python3 -c "import json;f=json.load(open('$G33/viability.json'));t=json.load(open('evidence/rl-rollout-viability.json'));assert f['tool_sha256']==t['tool_sha256'] and f['answer']==t['answer'] and f['crash']['instrument']==t['crash']['instrument'] and f['input_errors']==t['input_errors']==[]"

# ── свод ──────────────────────────────────────────────────────────────────────
expect_exit 0 "свод собран на разборе (status=complete)" \
  python3 tools/assemble_sft_decode_diagnosis_evidence.py --case-root "$DD/persist"
expect_exit 0 "ответ свода выведен из чисел: текст несёт значение первичного индикатора" \
  python3 -c "import json;d=json.load(open('$DD/persist/evidence/sft-decode-diagnosis.json'));t=d['answer']['text'];assert '0.3077' in t and 'УБЕГАНИЕ СОХРАНЯЕТСЯ' in t, t"
expect_exit 0 "таблица «режим × метрика» несёт числа всех режимов и определение каждой строки" \
  python3 -c "import json;d=json.load(open('$DD/persist/evidence/sft-decode-diagnosis.json'));rows=d['mode_metric_table']['rows'];assert all(set(r['values'])=={'V1','V2','V3'} and r['definition'] for r in rows.values()), list(rows)"
expect_exit 0 "свод называет артефакты хешами (из чего собран)" \
  python3 -c "import json;d=json.load(open('$DD/persist/evidence/sft-decode-diagnosis.json'));a=d['artifacts'];assert a['analysis']['sha256'] and all(v['sha256'] for v in a['mode_reports'].values()), a"
expect_exit 1 "свод отказывает, когда тождество V1 нарушено (status=measurement_incomplete)" \
  python3 tools/assemble_sft_decode_diagnosis_evidence.py --case-root "$DD/broken"
expect_exit 1 "свод отказывает на частичном прогоне" \
  python3 tools/assemble_sft_decode_diagnosis_evidence.py --case-root "$DD/partial"
expect_exit 0 "свод на историческом контроле собран и называет его полем" \
  python3 tools/assemble_sft_decode_diagnosis_evidence.py --case-root "$DD/hist"
expect_exit 0 "свод: исторический контроль назван и в строке ответа, а не только полем" \
  python3 -c "import json;d=json.load(open('$DD/hist/evidence/sft-decode-diagnosis.json'));assert d['control']['kind']=='historical_s3aq_report' and 'ИСТОРИЧЕСКИЙ' in d['answer']['text'], d['answer']['text']"
expect_exit 0 "свод: неизмеренная доля тождества названа числом в evidence" \
  python3 -c "import json;d=json.load(open('$DD/hist/evidence/sft-decode-diagnosis.json'));p=d['control']['partial_identity'];assert p['compared']==8 and p['unmeasured']==96, p"
expect_exit 0 "свод: ось длины — отдельным полем со своей границей, а не в тексте вердикта" \
  python3 -c "import json;d=json.load(open('$DD/hist/evidence/sft-decode-diagnosis.json'));r=d['runaway_reading'];assert r['available'] is True and r['not_the_verdict'] and len(r['caveats'])>=3, r.get('caveats')"
expect_exit 2 "своду нечего сводить (нет разбора) — NOT-VERIFIED" \
  python3 tools/assemble_sft_decode_diagnosis_evidence.py --case-root "$DD/empty"

# ── 34. C-029 (AD-9): запрет сна во время GPU-нагрузки ────────────────────────
# Проверяются обе половины контракта и, главное, **зубы**: мутант «нагрузка
# запущена без обвязки» обязан покраснеть, а не остаться зелёным по умолчанию.
# Живые GPU-нагрузки не запускаются ни разу (локальная карта в отказе, стенд занят
# замером): таблица процессов подменяется синтетическим `/proc`, площадка стенда —
# поддельным `ssh`, а отказ обвязки берётся её собственным путём отказа.
echo "== 37. check_gpu_sleep_guard.py + guard_cuda_run.sh (C-029 / AD-9) =="
SG="$TMP/sg"
SP="$SG/proc"; SR="$SG/runs"
mkdir -p "$SP/1" "$SP/100" "$SP/101" "$SP/200" "$SP/201" "$SP/300" "$SP/301" "$SP/400" \
         "$SG/clean-proc/1" "$SG/clean-proc/500" "$SG/clean-proc/600" \
         "$SR/run_unguarded" "$SR/run_guarded" "$SR/run_chained" "$SR/run_old" "$SR/run_refused" \
         "$SG/clean-runs/run_guarded" \
         "$SG/fakebin" "$SG/nosystemd"

# mkproc <корень> <pid> <ppid> <метка: yes|no> <аргументы командной строки...>
mkproc() {
  local root="$1" pid="$2" ppid="$3" marker="$4"; shift 4
  printf 'Name:\t%s\nPPid:\t%s\n' "$(basename "$1")" "$ppid" > "$root/$pid/status"
  python3 -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b"\0".join(a.encode() for a in sys.argv[2:]) + b"\0")' \
    "$root/$pid/cmdline" "$@"
  if [ "$marker" = yes ]; then printf 'CUDA_SLEEP_GUARD=1\0' > "$root/$pid/environ"
  else : > "$root/$pid/environ"; fi
}

mkproc "$SP" 1 0 no /sbin/init
# 100 — проба без обвязки (мутант); 101 — та же проба с меткой обвязки;
# 200/201 — проба под предком `systemd-inhibit --mode=block` (защита);
# 300/301 — под предком `--mode=delay`: сон он НЕ удерживает, и принять его за
#           защиту значило бы объявить защитой намерение (на машине такие
#           держатели есть всегда — GNOME держит `sleep` в режиме `delay`);
# 400 — `grep` с тем же именем в аргументах: не нагрузка, иначе находка
#       обесценилась бы шумом.
mkproc "$SP" 100 1 no   python3 tools/probe_language_split.py --prompts wide --max-new-tokens 8192
mkproc "$SP" 101 1 yes  python3 tools/probe_language_split.py --prompts wide --max-new-tokens 8192
mkproc "$SP" 200 1 no   systemd-inhibit --what=sleep --mode=block --why=x -- python3 tools/probe_language_split.py
mkproc "$SP" 201 200 no python3 tools/probe_language_split.py --prompts wide
mkproc "$SP" 300 1 no   systemd-inhibit --what=sleep --mode=delay --why=x -- python3 tools/probe_language_split.py
mkproc "$SP" 301 300 no python3 tools/probe_language_split.py --prompts wide
mkproc "$SP" 400 1 no   grep -r probe_language_split.py tools/

# Прогоны, начатые **до** порога применимости правила (ADR-053): правило вошло в
# ветвь 21.09.2026 в 14:22:35 MSK (11:22:35 UTC), и расписки у такого прогона быть
# не могло. `run_before_enactment` взят формой настоящего прогона
# `sft-loop-trend-20260921` (начат 14:10:13 MSK = 11:10:13 UTC) — он и стал
# причиной решения: находка на нём была бы красным by construction (ADR-023 п.12).
mkdir -p "$SR/run_before_enactment" "$SG/legacy-runs/run_before_enactment"
for d in "$SR/run_before_enactment" "$SG/legacy-runs/run_before_enactment"; do
  printf '{"created_at": "2026-09-21T11:10:13+00:00"}' > "$d/run_manifest.json"
  : > "$d/chain.sh"
done

# Доказательства запрета у прогонов: начатый после правила без доказательства
# (мутант), с распиской обвязки, с запретом в ЗАПИСАННОЙ команде (так стартует
# отсоединённая стадия), исторический (до правила) и отказ обвязки до старта.
printf '{"created_at": "2026-09-21T12:00:00+00:00"}' > "$SR/run_unguarded/run_manifest.json"
: > "$SR/run_unguarded/chain.sh"
printf '{"created_at": "2026-09-21T12:00:00+00:00"}' > "$SR/run_guarded/run_manifest.json"
: > "$SR/run_guarded/chain.sh"
printf '{"sleep_inhibit": {"held": true, "held_by": "guard", "protection": "inhibit"}, "payload": {"rc": 0}}' > "$SR/run_guarded/guard.json"
printf '{"created_at": "2026-09-21T12:00:00+00:00"}' > "$SR/run_chained/run_manifest.json"
: > "$SR/run_chained/chain.sh"
# Нынешняя форма записанной команды: первым идёт шаг защиты сна, который решает на
# площадке прогона, каким из двух способов защищаться.
printf 'bash /x/chain_sleep_guard.sh bash /x/pilot_chain.sh --run-dir /x\n' \
  > "$SR/run_chained/chain_command.txt"
# Прежняя форма (до 21.09.2026): `systemd-inhibit` прямо в команде. Читается так же —
# прогон под ней защищён, и обвинять его задним числом не за что.
mkdir -p "$SR/run_chained_old"
printf '{"created_at": "2026-09-21T12:00:00+00:00"}' > "$SR/run_chained_old/run_manifest.json"
: > "$SR/run_chained_old/chain.sh"
printf 'systemd-inhibit --what=sleep --mode=block %s env CUDA_SLEEP_GUARD=1 bash /x/pilot_chain.sh --run-dir /x\n' \
  "'--why=sleep guard: S3 during a CUDA run kills the context (Xid 31)'" > "$SR/run_chained_old/chain_command.txt"
# Расписка, которая говорит `held = true`, но способа не называет: «зелено без
# причины» доказательством не считается — это находка, а не «расписка есть».
mkdir -p "$SR/run_unnamed"
printf '{"created_at": "2026-09-21T12:00:00+00:00"}' > "$SR/run_unnamed/run_manifest.json"
: > "$SR/run_unnamed/chain.sh"
printf '{"sleep_inhibit": {"held": true, "held_by": "guard"}, "payload": {"rc": 0}}' > "$SR/run_unnamed/guard.json"
# Расписка со способом, которого не бывает: имя способа — перечень, а не свободный текст.
mkdir -p "$SR/run_badmethod"
printf '{"created_at": "2026-09-21T12:00:00+00:00"}' > "$SR/run_badmethod/run_manifest.json"
: > "$SR/run_badmethod/chain.sh"
printf '{"sleep_inhibit": {"held": true, "held_by": "guard", "protection": "наверное"}, "payload": {"rc": 0}}' > "$SR/run_badmethod/guard.json"
printf '{"created_at": "2026-09-20T09:00:00+00:00"}' > "$SR/run_old/run_manifest.json"
: > "$SR/run_old/chain.sh"
printf '{"created_at": "2026-09-21T12:00:00+00:00"}' > "$SR/run_refused/run_manifest.json"
: > "$SR/run_refused/chain.sh"
printf '{"sleep_inhibit": {"held": false}, "payload": null}' > "$SR/run_refused/guard.json"
# Признание в обходе: расписка есть, запрета в ней нет, а нагрузка шла — находка,
# а не «расписка есть, значит всё в порядке».
mkdir -p "$SR/run_bypass"
printf '{"created_at": "2026-09-21T12:00:00+00:00"}' > "$SR/run_bypass/run_manifest.json"
: > "$SR/run_bypass/chain.sh"
printf '{"sleep_inhibit": {"held": false}, "payload": {"rc": 0}}' > "$SR/run_bypass/guard.json"

# Чистая раскладка: нагрузка есть, но под запретом, и прогон закрыт распиской —
# на ней проверяется, что страж умеет быть зелёным (иначе «всегда красный» страж
# неотличим от сломанного).
mkproc "$SG/clean-proc" 1 0 no /sbin/init
mkproc "$SG/clean-proc" 600 1 no systemd-inhibit --what=sleep --mode=block --why=x -- bash
mkproc "$SG/clean-proc" 500 600 no python3 tools/probe_language_split.py --prompts wide
printf '{"created_at": "2026-09-21T12:00:00+00:00"}' > "$SG/clean-runs/run_guarded/run_manifest.json"
: > "$SG/clean-runs/run_guarded/chain.sh"
printf '{"sleep_inhibit": {"held": true, "held_by": "guard", "protection": "inhibit"}, "payload": {"rc": 0}}' > "$SG/clean-runs/run_guarded/guard.json"

# Площадка стенда: поддельный ssh отдаёт подготовленную `ps`-раскладку и ответ о
# механизме запрета (тот же формат, что у настоящего вызова: таблица, разделитель,
# ответ), поэтому ветка стенда проверяется механически, а не ожиданием чужого прогона.
cat > "$SG/fakebin/ssh" <<'SH'
#!/bin/bash
sentinel="---SLEEP-GUARD-MECHANISM---"
case "${FAKE_SSH_MODE:-load}" in
  load)
    cat <<PS
  900       1 python3 tools/probe_language_split.py --prompts wide --max-new-tokens 8192
  901     900 systemd-inhibit --what=sleep --mode=block --why=x -- python3 tools/probe_language_split.py
    1       0 /sbin/init
$sentinel
MECHANISM-OK
PS
  ;;
  clean) printf '    1       0 /sbin/init\n%s\nMECHANISM-OK\n' "$sentinel" ;;
  # Стенд gb10-fast, 21.09.2026: механизм есть, но запрет не берётся — у
  # пользователя нет logind-сессии. Нагрузку там защитить нечем, и это блокер.
  nopermit) printf '    1       0 /sbin/init\n%s\nFailed to inhibit: Access denied\nMECHANISM-FAIL\n' "$sentinel" ;;
  # Стенд gb10-fast, 21.09.2026 (после маскировки): инхибит взять неоткуда, но сон
  # структурно недостижим — все четыре цели masked, и попытка старта suspend.target
  # провалилась с причиной о маскировке. Это защита, и она сильнее инхибита.
  masked) printf '    1       0 /sbin/init\n%s\nFailed to inhibit: Access denied\nMECHANISM-FAIL\nMASK-TARGETS: sleep.target=masked suspend.target=masked hibernate.target=masked hybrid-sleep.target=masked\nMASK-ATTEMPT-RC: 1\nMASK-ATTEMPT-OUT: Failed to start suspend.target: Unit suspend.target is masked.\n' "$sentinel" ;;
  # Мутант: маскировка прочитана, а попытка старта НЕ провалилась — сон достижим,
  # и «флаг masked» доказательством не является.
  attempt_ok) printf '    1       0 /sbin/init\n%s\nFailed to inhibit: Access denied\nMECHANISM-FAIL\nMASK-TARGETS: sleep.target=masked suspend.target=masked hibernate.target=masked hybrid-sleep.target=masked\nMASK-ATTEMPT-RC: 0\nMASK-ATTEMPT-OUT: спать пошли\n' "$sentinel" ;;
  # Мутант: маскировки нет вовсе (и инхибита нет) — площадка не защищена.
  unmasked) printf '    1       0 /sbin/init\n%s\nFailed to inhibit: Access denied\nMECHANISM-FAIL\nMASK-TARGETS: sleep.target=static suspend.target=static hibernate.target=static hybrid-sleep.target=static\nMASK-ATTEMPT-RC: skipped\nMASK-ATTEMPT-OUT: маскировка неполна - попытка не делалась\n' "$sentinel" ;;
  # Мутант: маскировка прочитана, попытка провалилась, но причина не о маскировке
  # (так отвечает незамаскированная площадка без прав: polkit, а не маска).
  polkit) printf '    1       0 /sbin/init\n%s\nFailed to inhibit: Access denied\nMECHANISM-FAIL\nMASK-TARGETS: sleep.target=masked suspend.target=masked hibernate.target=masked hybrid-sleep.target=masked\nMASK-ATTEMPT-RC: 1\nMASK-ATTEMPT-OUT: Failed to start suspend.target: Interactive authentication required.\n' "$sentinel" ;;
  down) echo "ssh: connect to host gb10-fast port 22: Connection timed out" >&2; exit 255 ;;
esac
SH
chmod +x "$SG/fakebin/ssh"
# Тот же PATH, но без systemd-inhibit: так проверяется отказ обвязки на машине,
# где запрет сна взять нечем.
for b in bash sh env python3 grep sed awk date cat journalctl head tr cut dirname test; do
  p="$(command -v "$b" 2>/dev/null)" && ln -sf "$p" "$SG/nosystemd/$b"
done
# Та же урезанная оболочка, но с **подделанным** `systemctl`: так проверяется второй
# способ доказательства — «сон площадки структурно недостижим», который на стенде и
# работает (там инхибит взять неоткуда). Подделка нужна потому, что настоящий
# `systemctl start suspend.target` незамаскированную площадку действительно усыпил бы:
# проверять красный путь на живой машине здесь нельзя, а подделанный ответ — можно.
mkdir -p "$SG/maskbin"
for b in bash sh env python3 grep sed awk date cat journalctl head tr cut dirname test; do
  p="$(command -v "$b" 2>/dev/null)" && ln -sf "$p" "$SG/maskbin/$b"
done
cat > "$SG/maskbin/systemctl" <<'SH'
#!/bin/bash
# Подделка ровно в объёме пробы стража: `is-enabled` четырёх целей и попытка их старта.
mode="${FAKE_SYSTEMCTL:-unmasked}"
case "$*" in
  *is-enabled*)
    case "$mode" in
      masked|attempt_ok|polkit) echo masked; exit 1 ;;
      *) echo static; exit 0 ;;
    esac ;;
esac
case "$mode" in
  masked) echo "Failed to start suspend.target: Unit suspend.target is masked." >&2; exit 1 ;;
  polkit) echo "Failed to start suspend.target: Interactive authentication required." >&2; exit 1 ;;
  attempt_ok) echo "спать пошли" >&2; exit 0 ;;
  *) echo "Failed to start suspend.target: Interactive authentication required." >&2; exit 1 ;;
esac
SH
chmod +x "$SG/maskbin/systemctl"

expect_exit 1 "мутант: проба запущена без обвязки — нагрузка без запрета сна" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe --proc-root "$SP" --runs-root "$SR"
expect_contains "локально GPU-нагрузка без запрета сна: pid 100" \
  "находка называет pid и командную строку нагрузки" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe --proc-root "$SP" --runs-root "$SR"
expect_contains "run_unguarded: прогон начат" "находка называет прогон без доказательства поимённо" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe --proc-root "$SP" --runs-root "$SR"
expect_exit 0 "разбор нагрузки: метка обвязки и предок systemd-inhibit — защита; delay — нет; grep — не нагрузка" \
  python3 - "$SP" <<'PY'
import pathlib, sys
sys.path.insert(0, "tools")
import check_gpu_sleep_guard as g
procs, err = g.local_processes(pathlib.Path(sys.argv[1]))
assert err is None, err
loads = {r["pid"]: r for r in g.find_loads(procs, g.GPU_LOADS)}
assert 400 not in loads, "grep с именем прибора в аргументах — не нагрузка"
assert loads[101]["sleep_inhibit"] and "CUDA_SLEEP_GUARD" in loads[101]["why"], loads[101]
assert loads[201]["sleep_inhibit"] and "systemd-inhibit" in loads[201]["why"], loads[201]
assert not loads[301]["sleep_inhibit"], "--mode=delay сон не удерживает: " + loads[301]["why"]
assert not loads[100]["sleep_inhibit"], loads[100]
PY
expect_exit 0 "доказательства прогонов: расписка, записанная команда, отказ, исторический — и находка" \
  python3 - "$SR" <<'PY'
import datetime, pathlib, sys
sys.path.insert(0, "tools")
import check_gpu_sleep_guard as g
# Порог — **момент** вступления правила в силу, а не начало суток (ADR-053 п.1):
# проверяется константой, чтобы молчаливый откат к 00:00:00 не прошёл.
assert g.ENACTED_AT == "2026-09-21T11:22:35+00:00", g.ENACTED_AT
assert g._parse_iso(g.ENACTED_AT) == datetime.datetime(
    2026, 9, 21, 11, 22, 35, tzinfo=datetime.timezone.utc), g.ENACTED_AT
assert g._parse_iso(g.ENACTED_AT).timetz() != datetime.time(0, 0), \
    "суточная гранулярность — источник дефекта (ADR-053): порог обязан быть моментом"
rec = g.check_receipts(pathlib.Path(sys.argv[1]), g._parse_iso(g.ENACTED_AT))
by = {r["run"]: r for r in rec["runs"]}
assert by["run_guarded"]["guard_state"] == "held", by["run_guarded"]
assert by["run_chained"]["guard_state"] == "chain", by["run_chained"]
assert by["run_chained"]["guard"] is True, by["run_chained"]
assert by["run_chained_old"]["guard_state"] == "chain", by["run_chained_old"]
assert by["run_refused"]["guard_state"] == "no_payload", by["run_refused"]
assert by["run_old"]["in_scope"] is False, by["run_old"]
assert by["run_unguarded"]["in_scope"] is True, by["run_unguarded"]
assert any("run_unguarded" in f for f in rec["findings"]), rec["findings"]
# Прогон, начатый в 12 минут до рождения правила, находкой не выставляется — и он
# же не «проверен»: печатается поимённо как legacy-not-checked (ADR-053 п.2).
assert by["run_before_enactment"]["in_scope"] is False, by["run_before_enactment"]
assert by["run_before_enactment"]["guard_state"] == "none", by["run_before_enactment"]
assert not any("run_before_enactment" in f for f in rec["findings"]), rec["findings"]
assert any("run_before_enactment" in h and "legacy-not-checked" in h
           and "защита сна НЕ доказана" in h for h in rec["historical_without_guard"]), \
    rec["historical_without_guard"]
assert by["run_bypass"]["guard_state"] == "bypass", by["run_bypass"]
assert any("run_bypass" in f and "под запретом не шёл" in f for f in rec["findings"]), rec["findings"]
# Расписка без имени способа защиты — находка, а не доказательство: «зелено без
# причины» неотличимо от «зелено потому, что забыли проверить». Имя способа —
# перечень, а не свободный текст: «наверное» им не является.
assert by["run_unnamed"]["guard_state"] == "unnamed", by["run_unnamed"]
assert by["run_badmethod"]["guard_state"] == "unnamed", by["run_badmethod"]
assert sum(1 for f in rec["findings"] if "способ защиты сна" in f) == 2, rec["findings"]
# Мутант на самой записанной команде: без защиты в ней доказательства нет.
assert by["run_unguarded"]["guard_state"] == "none", by["run_unguarded"]
PY
expect_exit 0 "чистая раскладка (нагрузка под запретом, прогон с распиской) — зелено" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs"

# ── три состояния порога применимости (ADR-053 п.1–3, п.6) ───────────────────
# (а) прогон, начатый ДО порога, без расписки → legacy-not-checked, гейт зелёный:
#     требовать расписку у прогона, стартовавшего до рождения правила, значило бы
#     красное by construction (ADR-023 п.12);
# (б) прогон, начатый ПОСЛЕ порога, без расписки → находка (без послабления);
# (в) прогон после порога С распиской, называющей способ → зелёный.
# Вход всех трёх — один и тот же ($SG/legacy-runs и $SG/clean-runs), различаются
# они только порогом и распиской: так видно, что решает именно граница, а не шум.
expect_exit 0 "(а) прогон, начатый до порога применимости, без расписки — гейт зелёный" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe \
    --proc-root "$SG/clean-proc" --runs-root "$SG/legacy-runs"
expect_contains "legacy-not-checked — старше правила" \
  "(а) прогон назван поимённо, а не молча исключён" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe \
    --proc-root "$SG/clean-proc" --runs-root "$SG/legacy-runs"
expect_contains "защита сна НЕ доказана" \
  "(а) «историческое» названо словами, а не выдано за проверенное" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe \
    --proc-root "$SG/clean-proc" --runs-root "$SG/legacy-runs"
expect_exit 1 "ЗУБЫ: тот же вход с порогом-началом-суток (00:00:00) — уже находка" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe \
    --proc-root "$SG/clean-proc" --runs-root "$SG/legacy-runs" \
    --since 2026-09-21T00:00:00+00:00
expect_contains "run_before_enactment: прогон начат" \
  "ЗУБЫ: подмена порога обратно на 00:00:00 роняет этот вход в находку" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe \
    --proc-root "$SG/clean-proc" --runs-root "$SG/legacy-runs" \
    --since 2026-09-21T00:00:00+00:00
expect_exit 1 "(б) прогон, начатый после порога, без расписки — находка, как и раньше" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe \
    --proc-root "$SG/clean-proc" --runs-root "$SR"
expect_exit 0 "(в) прогон после порога с распиской, называющей способ — зелёный" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs"
expect_exit 2 "нет таблицы процессов — NOT-VERIFIED, а не «зелено»" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe \
    --proc-root "$SG/nowhere" --runs-root "$SG/clean-runs"
expect_exit 1 "мутант на стенде: нагрузка на площадке стенда без запрета сна" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=load python3 tools/check_gpu_sleep_guard.py \
    --no-mechanism-probe --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast
expect_contains "на площадке gb10-fast GPU-нагрузка без запрета сна: pid 900" \
  "ветка стенда краснеет и называет площадку" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=load python3 tools/check_gpu_sleep_guard.py \
    --no-mechanism-probe --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast
expect_exit 0 "на стенде без нагрузок — зелено (площадка проверена, а не пропущена)" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=clean python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast
expect_exit 0 "механизм запрета на стенде недоступен — блокер назван, гейт не краснеет" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=nopermit python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast
expect_contains "механизм запрета сна недоступен (Failed to inhibit: Access denied)" \
  "блокер называет причину, а не отделывается «не найдено»" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=nopermit python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast
expect_exit 2 "блокер механизма и --strict — красное, а не «пропущено»" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=nopermit python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast --strict
expect_exit 0 "блокер механизма и --unreachable-not-verified — профиль гейта: не красное" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=nopermit python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast \
    --unreachable-not-verified --strict
expect_exit 2 "стенд недоступен и объявлен --strict — красное, а не «пропущено»" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=down python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast --strict
expect_exit 0 "стенд недоступен с --unreachable-not-verified — NOT-VERIFIED, не красное" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=down python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast \
    --unreachable-not-verified --strict

# ── второй способ доказательства: сон площадки структурно недостижим ──────────
# Стенд gb10-fast защищён именно так: инхибит там взять неоткуда (нет logind-сессии),
# а цели сна замаскированы, и это доказано **попыткой**, а не чтением флага. Прежде
# такая площадка отдавалась блокером — то есть старт стадии на ней был невозможен,
# хотя фактическая защита там сильнее инхибита.
expect_exit 0 "стенд с маскировкой сна: защита доказана попыткой — площадка защищена, не блокер" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=masked python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast
expect_exit 0 "защита названа способом: masked_targets, verdict=protected, NOT-VERIFIED нет" \
  python3 - "$SG" <<'PY'
import json, os, subprocess, sys
sg = sys.argv[1]
env = dict(os.environ, PATH=f"{sg}/fakebin:" + os.environ["PATH"], FAKE_SSH_MODE="masked")
done = subprocess.run(["python3", "tools/check_gpu_sleep_guard.py", "--proc-root",
                       f"{sg}/clean-proc", "--runs-root", f"{sg}/clean-runs",
                       "--host", "gb10-fast", "--json"],
                      capture_output=True, text=True, env=env)
try:
    rep = json.loads(done.stdout)
except ValueError as exc:
    raise SystemExit(f"отчёт не разобран как JSON ({exc}; rc={done.returncode}): "
                     f"stdout={done.stdout[:200]!r} … {done.stdout[-200:]!r}")
hub = next(p for p in rep["platforms"] if p["platform"] == "gb10-fast")
assert hub["protection"]["method"] == "masked_targets", hub["protection"]
assert hub["verdict"] == "protected", hub["verdict"]
assert hub["protection"]["measured"] is True, hub["protection"]
assert "masked" in hub["protection"]["why"], hub["protection"]["why"]
assert not rep["findings"] and done.returncode == 0, (rep["findings"], done.returncode)
assert not any("gb10-fast" in n for n in rep["not_verified"]), rep["not_verified"]
PY
expect_exit 1 "мутант: маскировка прочитана, а старт suspend.target НЕ провалился — площадка блокер" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=attempt_ok python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast
expect_contains "попытка старта suspend.target НЕ провалилась" \
  "мутант назван причиной, а не «что-то не так»" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=attempt_ok python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast
expect_exit 1 "мутант: ни инхибита, ни маскировки — блокер (красное, а не предупреждение)" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=unmasked python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast
expect_contains "не защищена ни одним из двух способов" \
  "блокер называет площадку и оба способа, которыми её мерили" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=unmasked python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast
expect_exit 1 "мутант: отказ в попытке не о маскировке (polkit) — доказательством не считается" \
  env PATH="$SG/fakebin:$PATH" FAKE_SSH_MODE=polkit python3 tools/check_gpu_sleep_guard.py \
    --proc-root "$SG/clean-proc" --runs-root "$SG/clean-runs" --host gb10-fast

# Тот же второй способ на **локальной** площадке: проба одна на обе площадки, поэтому
# проверяется и здесь — с подделанным `systemctl` (настоящий усыпил бы машину).
expect_exit 0 "локально: маскировка доказана попыткой — защита принята (способ назван)" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=masked python3 tools/check_gpu_sleep_guard.py --prove-protection
expect_exit 2 "локально: маскировка есть, попытка не провалилась — защитой не считается" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=attempt_ok python3 tools/check_gpu_sleep_guard.py --prove-protection
expect_exit 2 "локально: маскировки нет — защитой не считается" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=unmasked python3 tools/check_gpu_sleep_guard.py --prove-protection
expect_exit 2 "локально: отказ не о маскировке (polkit) — защитой не считается" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=polkit python3 tools/check_gpu_sleep_guard.py --prove-protection
expect_exit 0 "вопрос «защита взята?» принимает второй способ (--assert-held под маскировкой)" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=masked python3 tools/check_gpu_sleep_guard.py --assert-held

# ── запуск без запрета сна невозможен ────────────────────────────────────────
expect_exit 2 "раннер отказывается стартовать без запрета сна (--do launch)" \
  python3 tools/launch_sft_stage.py --do launch --ts 20260921-0000
expect_contains "ОТКАЗ: --do launch ставит GPU-нагрузку" "отказ раннера называет причину и путь обвязки" \
  python3 tools/launch_sft_stage.py --do launch --ts 20260921-0000
expect_exit 0 "раннер под systemd-inhibit нагрузку пускает (проверка модулем, без запуска стадии)" \
  systemd-inhibit --what=sleep --mode=block --why="тест C-029" \
    python3 -c "import sys; sys.path.insert(0,'tools'); import launch_sft_stage as m; \
held, why = m.sleep_guard_state(); assert held, why; print(why)"
# Защита ставится в САМУ записанную команду цепочки, а не вокруг запускающего её
# ssh: тот возвращается сразу, и снятая с него защита не защитила бы ничего. Первым
# идёт шаг защиты — он решает **на площадке прогона**, каким из двух способов
# защищаться (инхибит берётся сам; где его взять нечем — требуется доказанная
# недостижимость сна), и отказывает закрыто, если не выходит ни то, ни другое.
expect_exit 0 "стадия: шаг защиты сна стоит в записанной команде цепочки, перед самой цепочкой" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("launch", pathlib.Path("tools/launch_sft_stage.py"))
m = u.module_from_spec(spec); spec.loader.exec_module(m)
cmd = m.build_chain_command({"name": "sft-x", "ctr_run_dir": "/tmp/ctr"}, pathlib.Path("/tmp/run"))
assert cmd.startswith("bash /tmp/run/chain_sleep_guard.sh"), cmd[:120]
assert "bash /tmp/run/pilot_chain.sh --run-dir /tmp/run" in cmd, cmd[:300]
# Двойных кавычек в записанной команде быть не должно: она едет внутри
# `tmux new-session … "…"`, и кавычка закрыла бы эту строку на середине.
assert '"' not in cmd, cmd[:300]
# Шаг защиты едет в каталог прогона вместе с источником ответа (его хеш — в ship.json).
assert "tools/chain_sleep_guard.sh" in m.SHIPPED, m.SHIPPED
assert "tools/check_gpu_sleep_guard.py" in m.SHIPPED, m.SHIPPED
PY
# Механизм отказывает ЗАКРЫТО — это и делает вшивание защиты защитой, а не
# украшением. Проверяется на настоящем `systemd-inhibit` тем же путём отказа, что
# даёт стенд gb10-fast (`Failed to inhibit: Access denied`, 21.09.2026): отказ
# берётся не «по правам» (их в тесте не отнять), а негодной спецификацией —
# ветка одна и та же, и `systemd-inhibit` не запускает полезную нагрузку.
expect_exit 0 "механизм отказывает закрыто: нет запрета — нет нагрузки" \
  python3 - <<'PROBE'
import pathlib, subprocess
sentinel = pathlib.Path("/tmp/sg-chain-guard-probe.txt")
sentinel.unlink(missing_ok=True)
done = subprocess.run(["systemd-inhibit", "--what=sleep", "--mode=bogus", "--why=x",
                       "sh", "-c", f"echo RAN > {sentinel}"],
                      capture_output=True, text=True)
assert done.returncode != 0, done
assert not sentinel.exists(), "нагрузка запустилась без запрета сна — это и есть дыра"
assert "Failed to inhibit" in (done.stdout + done.stderr), done.stdout + done.stderr
PROBE
# Шаг защиты цепочки: тот же закрытый отказ, но уже на самом шаге — «нет защиты →
# цепочки нет». Проверяется на урезанной оболочке без systemd-inhibit и с
# незамаскированным (подделанным) `systemctl`, то есть там, где защиты нет вовсе.
expect_exit 2 "шаг защиты цепочки отказывает закрыто: ни инхибита, ни маскировки — цепочка не стартует" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=unmasked bash tools/chain_sleep_guard.sh \
    bash -c 'echo НАГРУЗКА-ПОШЛА'
expect_contains "защита сна не доказана ни одним из двух способов" \
  "отказ шага защиты называет, чего не хватило" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=unmasked bash tools/chain_sleep_guard.sh \
    bash -c 'echo НАГРУЗКА-ПОШЛА'
expect_exit 2 "шаг защиты цепочки: маскировка прочитана, попытка не провалилась — тоже отказ" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=attempt_ok bash tools/chain_sleep_guard.sh \
    bash -c 'echo НАГРУЗКА-ПОШЛА'
expect_exit 0 "шаг защиты цепочки принимает второй способ: сон площадки недостижим — нагрузка идёт" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=masked bash tools/chain_sleep_guard.sh \
    bash -c 'test "$CUDA_SLEEP_GUARD" = 1'
expect_contains "защита сна: площадка" "шаг называет способ защиты в логе цепочки" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=masked bash tools/chain_sleep_guard.sh \
    bash -c true
expect_exit 0 "шаг защиты цепочки предпочитает инхибит, когда он берётся (локальная 4080)" \
  bash tools/chain_sleep_guard.sh bash -c 'test "$CUDA_SLEEP_GUARD" = 1'
expect_exit 2 "вопрос «запрет сна взят?» без запрета — отказ (exit 2)" \
  python3 tools/check_gpu_sleep_guard.py --no-mechanism-probe --assert-held
expect_exit 0 "тот же вопрос под systemd-inhibit — «взят»" \
  systemd-inhibit --what=sleep --mode=block --why="тест C-029" \
    python3 tools/check_gpu_sleep_guard.py --assert-held
expect_exit 2 "мутант: --no-inhibit, а запрета снаружи нет — отказ до старта" \
  env PATH="$SG/nosystemd" bash tools/guard_cuda_run.sh --out "$SG/g-refuse.json" \
    --device-check skip --no-inhibit -- sh -c 'echo НАГРУЗКА-ПОШЛА'
expect_exit 0 "расписка отказа: payload=null и held=false — GPU-работы не было" \
  python3 -c "import json;d=json.load(open('$SG/g-refuse.json')); \
assert d['verdict']=='refused_no_inhibit' and d['exit_code']==2, d['verdict']; \
assert d['sleep_inhibit']['held'] is False and d['payload'] is None, d['sleep_inhibit']"
expect_exit 0 "обвязка берёт запрет сама и ставит нагрузке метку CUDA_SLEEP_GUARD" \
  bash -c 'bash tools/guard_cuda_run.sh --out "$1/g-ok.json" --device-check skip -- \
    sh -c "test \"\$CUDA_SLEEP_GUARD\" = 1"' _ "$SG"
expect_exit 0 "расписка обвязки: защиту держит обвязка, способ назван, нагрузка дошла" \
  python3 -c "import json;d=json.load(open('$SG/g-ok.json')); \
si=d['sleep_inhibit']; assert si['held'] and si['held_by']=='guard' and si['self_test'], si; \
assert si['protection']=='inhibit' and si['mechanism'].startswith('systemd-inhibit'), si; \
assert d['payload']['rc']==0 and d['verdict']=='ok', d['payload']"
# Стенд gb10-fast: инхибит взять нечем, но сон структурно недостижим (маскировка,
# проверенная попыткой). Обвязка обязана стартовать нагрузку — и **назвать способ**
# в расписке: иначе «зелено» неотличимо от «зелено без причины».
expect_exit 0 "обвязка на площадке со структурно недостижимым сном: нагрузка идёт под защитой площадки" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=masked bash tools/guard_cuda_run.sh \
    --out "$SG/g-masked.json" --device-check skip -- sh -c 'test "$CUDA_SLEEP_GUARD" = 1'
expect_exit 0 "расписка называет способ masked_targets и держателя platform" \
  python3 -c "import json;d=json.load(open('$SG/g-masked.json'));si=d['sleep_inhibit']; \
assert si['held'] and si['protection']=='masked_targets' and si['held_by']=='platform', si; \
assert 'masked' in si['protection_why'], si; assert d['verdict']=='ok' and d['payload']['rc']==0, d"
expect_exit 2 "мутант: маскировка снята (сон достижим) — обвязка не стартует нагрузку" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=unmasked bash tools/guard_cuda_run.sh \
    --out "$SG/g-unmasked.json" --device-check skip -- sh -c 'echo НАГРУЗКА-ПОШЛА'
expect_exit 0 "расписка отказа на площадке без защиты: способа нет, нагрузки не было" \
  python3 -c "import json;d=json.load(open('$SG/g-unmasked.json'));si=d['sleep_inhibit']; \
assert d['verdict']=='refused_no_inhibit' and d['payload'] is None, d['verdict']; \
assert si['held'] is False and si['protection'] is None, si"
expect_exit 2 "мутант: маскировка прочитана, попытка не провалилась — обвязка не стартует" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=attempt_ok bash tools/guard_cuda_run.sh \
    --out "$SG/g-attempt.json" --device-check skip -- sh -c 'echo НАГРУЗКА-ПОШЛА'
expect_exit 0 "--no-inhibit под уже взятым запретом: держит вызывающий" \
  bash -c 'systemd-inhibit --what=sleep --mode=block --why=outer bash tools/guard_cuda_run.sh \
    --out "$1/g-outer.json" --device-check skip --no-inhibit -- sh -c "true"' _ "$SG"
expect_exit 0 "расписка различает, кто держал запрет (held_by=caller)" \
  python3 -c "import json;d=json.load(open('$SG/g-outer.json')); \
assert d['sleep_inhibit']['held_by']=='caller', d['sleep_inhibit']"
expect_exit 2 "мутант: --no-inhibit, а защиты снаружи нет — отказ до старта" \
  env PATH="$SG/maskbin" FAKE_SYSTEMCTL=unmasked bash tools/guard_cuda_run.sh \
    --out "$SG/g-noinj.json" --device-check skip --no-inhibit -- sh -c 'echo НАГРУЗКА-ПОШЛА'
# 32. S3ar: откуда петли SFT-состояния — воспроизведение выученного из набора или
#     собственная дегенерация. Проверяются оба исхода объявленного правила: набор-
#     фикстура, где текст петли в нём ЛЕЖИТ (memorized_prevails), и набор, где его
#     НЕТ (own_degeneration), — прибор, у которого сработал только один исход,
#     второго исхода не проверяет. Контроль берётся из тех же генераций, поэтому
#     «нашлось» без контрольной руки вердикта не даёт: в фикстурах контрольные
#     юниты нарочно вне набора, и без них разрыв был бы другого знака.
echo "== 38. analyze_loop_origin.py (S3ar: выученное против дегенерации) =="
python3 - "$TMP" <<'PY'
import hashlib, json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
UNIT = "альфа бета гамма дельта "
mem_row = {"messages": [
    {"role": "user", "content": "Что такое тандемный повтор?"},
    {"role": "assistant", "content": "Определение: " + (UNIT * 2) + "это тандемный повтор."}]}
mem_rows = [mem_row, mem_row]                      # кратность 2 у одной строки
(tmp / "s3ar_mem.jsonl").write_text(
    "\n".join(json.dumps(r, ensure_ascii=False) for r in mem_rows), encoding="utf-8")
none_rows = [{"messages": [
    {"role": "user", "content": "Что такое петля?"},
    {"role": "assistant", "content": "Ответ без повторов: " + " ".join(
        f"слово{i}" for i in range(30))}]}]
(tmp / "s3ar_none.jsonl").write_text(
    "\n".join(json.dumps(r, ensure_ascii=False) for r in none_rows), encoding="utf-8")

mem_resp = (UNIT * 8) + " ".join(f"уникум{i}" for i in range(40))
none_resp = ("зетта " * 12) + " ".join(f"другое{i}" for i in range(40))
for name, resp in (("mem", mem_resp), ("none", none_resp)):
    (tmp / f"s3ar_{name}_report.json").write_text(json.dumps(
        {"schema": "probe-language-split/1",
         "states": {"sft_x": {"checkpoint": "/нет/такого.pt", "aggregate": {"looped_share": 0.5},
                              "probes": [{"tag": "t1", "prompt": "вопрос", "response": resp,
                                          "n_new_tokens": 512, "metrics": {"looped": True}},
                                         {"tag": "t2", "prompt": "вопрос",
                                          "response": "обычный ответ без единого повтора тут",
                                          "n_new_tokens": 32, "metrics": {"looped": False}}]}}},
        ensure_ascii=False), encoding="utf-8")
# CPT-корпус фикстуры: текст петли «зетта …» в нём ЛЕЖИТ, поэтому ось `cpt` обязана
# сработать; манифест объявляет его sha256 — сверка входа, а не доверие имени файла.
cpt = tmp / "s3ar_cpt.txt"
cpt.write_text("шум " + ("зетта " * 12) + " хвост", encoding="utf-8")
(tmp / "s3ar_cpt_manifest.txt").write_text(
    "sha256 ВХОДОВ:\n%s  домен\n" % hashlib.sha256(cpt.read_bytes()).hexdigest(),
    encoding="utf-8")
(tmp / "s3ar_cpt_manifest_wrong.txt").write_text(
    "sha256 ВХОДОВ:\n%s  домен\n" % ("00" * 32), encoding="utf-8")
(tmp / "s3ar_clean_report.json").write_text(json.dumps(
    {"states": {"s": {"probes": [{"tag": "t", "response": "короткий ответ", "metrics": {}}]}}}),
    encoding="utf-8")
print("фикстуры собраны", file=sys.stderr)
PY
expect_exit 0 "analyze_loop_origin: фикстуры механики (тандем, ярусы, χ²)" \
  python3 tools/analyze_loop_origin.py --selftest
expect_exit 2 "analyze_loop_origin: нет отчёта проб — NOT-VERIFIED" \
  python3 tools/analyze_loop_origin.py --report "$TMP/нет-такого.json" \
  --dataset "$TMP/s3ar_mem.jsonl" --out "$TMP/s3ar_none.json"
expect_exit 2 "analyze_loop_origin: нет набора — NOT-VERIFIED" \
  python3 tools/analyze_loop_origin.py --report "$TMP/s3ar_mem_report.json" \
  --dataset "$TMP/нет-такого.jsonl" --out "$TMP/s3ar_none.json"
expect_exit 2 "analyze_loop_origin: петель нет — NOT-VERIFIED, а не «повторов нет»" \
  python3 tools/analyze_loop_origin.py --report "$TMP/s3ar_clean_report.json" \
  --dataset "$TMP/s3ar_mem.jsonl" --out "$TMP/s3ar_none.json"
expect_exit 1 "analyze_loop_origin: sha набора не совпал — отказ" \
  python3 tools/analyze_loop_origin.py --report "$TMP/s3ar_mem_report.json" \
  --dataset "$TMP/s3ar_mem.jsonl" --expect-dataset-sha256 00ff \
  --out "$TMP/s3ar_none.json"
expect_exit 0 "analyze_loop_origin: оба исхода правила (выучено / дегенерация)" \
  python3 - "$TMP" <<'PY'
import hashlib, importlib.util, json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
TOOL = ["python3", "tools/analyze_loop_origin.py"]
def run(name):
    out = tmp / f"s3ar_{name}_out.json"
    r = subprocess.run(TOOL + ["--report", str(tmp / f"s3ar_{name}_report.json"),
                               "--dataset", str(tmp / f"s3ar_{name}.jsonl"),
                               "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, (name, r.returncode, r.stderr[-400:])
    return json.loads(out.read_text())
mem = run("mem")
# (а) текст петли ЛЕЖИТ в наборе: вердикт — выученное, и находка названа строкой набора
assert mem["answer"]["verdict"] == "memorized_prevails", mem["answer"]["text"]
assert mem["origin"]["mild"]["by_origin"].get("dataset_verbatim", 0) >= 1
ex = mem["origin"]["examples"]["dataset_verbatim"][0]
assert ex["length"] >= 8 and ex["period"] <= 4, ex
assert ex["dataset_rows"] and ex["dataset_groups"] == 1, ex
assert mem["multiplicity"]["base_rows"] == {"2": 2}, mem["multiplicity"]["base_rows"]
# контроль — из той же генерации, его юниты в наборе не лежат: без него «нашлось»
# не значило бы ничего, и разрыв считался бы не с чем
assert mem["origin"]["control_all"]["n"] >= 1
assert mem["origin"]["control_all"]["found_in_dataset"] == 0
assert [r["key"] for r in mem["answer"]["rules_evaluated"]] == ["origin", "multiplicity"]
# (б) того же текста в наборе НЕТ: вердикт — собственная дегенерация
none = run("none")
assert none["answer"]["verdict"] == "own_degeneration", none["answer"]["text"]
assert none["origin"]["mild"]["by_origin"] == {"none": 1}, none["origin"]["mild"]
assert none["answer"]["multiplicity_branch"]["fired"] == "underpowered"
# (в) детерминизм: пересборка тем же входом даёт тот же отчёт (кроме времени)
again = run("mem")
for d in (mem, again):
    d.pop("generated_at")
assert json.dumps(mem, ensure_ascii=False, sort_keys=True) == \
       json.dumps(again, ensure_ascii=False, sort_keys=True)
# (г) вход по ссылке на ветку: отчёт прибора живёт не в каждой ветви, и читать его
#     надо ссылкой, а не копией (ADR-023 п.9)
spec = importlib.util.spec_from_file_location("lo", pathlib.Path("tools/analyze_loop_origin.py"))
lo = importlib.util.module_from_spec(spec); spec.loader.exec_module(lo)
got = lo.read_input("git:HEAD:кейсы/laguna-compact/tools/analyze_sft_loops.py")
assert got and "analyze_sft_loops" in got["text"], got
assert got["sha256"] == hashlib.sha256(got["text"].encode()).hexdigest()
assert lo.read_input("git:нет-такой-ветки:файл") is None
# (д) ось CPT: текст петли лежит в CPT-корпусе, и вход сверен с манифестом по sha256
def run_cpt(manifest):
    out = tmp / "s3ar_cpt_out.json"
    r = subprocess.run(TOOL + ["--report", str(tmp / "s3ar_none_report.json"),
                               "--dataset", str(tmp / "s3ar_none.jsonl"),
                               "--cpt", str(tmp / "s3ar_cpt.txt"),
                               "--cpt-manifest", str(manifest), "--out", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, (r.returncode, r.stderr[-400:])
    return json.loads(out.read_text())
cpt_doc = run_cpt(tmp / "s3ar_cpt_manifest.txt")
assert cpt_doc["origin"]["mild"]["by_origin"].get("cpt", 0) == 1, cpt_doc["origin"]["mild"]
assert cpt_doc["inputs"]["cpt_corpora"][0]["declared_in_manifest"] is True
assert cpt_doc["answer"]["verdict"] == "own_degeneration", "CPT — не набор: вердикт не меняется"
cpt_bad = run_cpt(tmp / "s3ar_cpt_manifest_wrong.txt")
assert cpt_bad["inputs"]["cpt_corpora"][0]["declared_in_manifest"] is False
PY

echo "== 39. assemble_s3at_onset.py (S3at: кривая зарождения петель) =="

# 33.1 Фикстуры правил. Каждая держит ветку, которую легко «съехать» в удобную:
#      «есть» против «выше базы», направленность (значимое ПАДЕНИЕ ниже базы —
#      не зарождение), окно не выдумывается без роста, матрица пар полная, а
#      расхождение определений (блоки против доли) называется, а не сглаживается.
expect_exit 0 "S3at: фикстуры правил (направленность, окно, матрица пар)" \
  python3 tools/assemble_s3at_onset.py --selftest

# 33.2 Артефакт держит ОТВЕТ, а не только форму. Числа ответа (2/104 на 500,
#      окно (5000, 9000], разные начала трёх дефектов) — то, ради чего свод собран:
#      «файл есть» без них ничего не доказывает.
expect_exit 0 "S3at: артефакт держит ответ и его границы" \
  python3 - <<'PY'
import json, pathlib
d = json.loads(pathlib.Path("evidence/sft-loop-onset.json").read_text(encoding="utf-8"))
ans = d["answer"]
assert d["status"] == "complete", d["status"]
# склейка действительна только при одном приборе и одном наборе промптов
assert d["comparability"]["verdict"] == "comparable", d["comparability"]
assert len(d["comparability"]["instrument_sha256"]) == 1, d["comparability"]
assert len(d["comparability"]["prompts_digest"]) == 1, d["comparability"]
assert d["comparability"]["n_states"] == 12, d["comparability"]
# петли на 500 ЕСТЬ, но НЕ выше базы: два разных утверждения, оба названы
assert ans["loops_present_at_500"] is True, ans["loops_at_500"]
assert (ans["loops_at_500"]["k"], ans["loops_at_500"]["n"]) == (2, 104), ans["loops_at_500"]
assert ans["loops_above_base_at_500"] is False, ans
assert (ans["loops_base"]["k"], ans["loops_base"]["n"]) == (1, 104), ans["loops_base"]
win = ans["loops_onset_window"]
assert (win["lo_step"], win["hi_step"]) == (5000, 9000), win
assert win["edge_of_resolution"] is True, win      # Δ 0.0577 < MDD 0.0747
# три дефекта стартуют по-разному — «одна причина на все три» не подтверждается
assert "выше базы уже на 500" in ans["truncations_at_500"]["verdict"], ans["truncations_at_500"]
assert "НИЖЕ базы" in ans["unclosed_think_at_500"]["verdict"], ans["unclosed_think_at_500"]
PY

# 33.3 Доли посчитаны ВЫЗОВОМ инструмента тренда (не переписанной рядом
#      арифметикой), база идёт отдельным якорем (инструмент исключает cfinal:
#      в метке нет шага), разрыв диапазона и цена границы устройств названы числом.
expect_exit 0 "S3at: кривая — вызовом тренда, разрыв и мост названы числом" \
  python3 - <<'PY'
import json, pathlib
d = json.loads(pathlib.Path("evidence/sft-loop-onset.json").read_text(encoding="utf-8"))
assert d["curve_trend"]["tool"] == "tools/analyze_loop_trend.py", d["curve_trend"]["tool"]
assert len(d["curve_trend"]["points"]) == 11, len(d["curve_trend"]["points"])
assert [s["tag"] for s in d["curve_trend"]["states_excluded"]] == ["cfinal"]
steps = [p["step"] for p in d["curve"]]
assert steps == [0, 500, 5000, 9000, 21500, 23000, 24500, 26000, 27500, 29000,
                 30500, 32600], steps
g = d["coverage"]["gap"]
assert (g["from_step"], g["to_step"]) == (9000, 21500), g
assert g["n_unmeasured_grid_points"] == 24, g["n_unmeasured_grid_points"]
assert g["retention_points_lost"] == 41, g["retention_points_lost"]
b = d["device_bridge"]
assert b["available"] and b["flips"]["looped"] == 4, b
assert b["byte_identical_responses"] == 57, b
# повторный замер того же чекпойнта на ТОМ ЖЕ устройстве — база сравнения для
# «цена границы устройств»: 104/104 совпали, ноль разошедшихся флагов
same = [p for p in d["same_device_reproducibility"]["pairs"] if p["same_device"]]
assert same and same[0]["byte_identical_responses"] == 104, same
assert same[0]["flips_looped"] == 0, same
# расхождение определений названо ПОИМЁННО: на 500 блоков много, а доля — на уровне базы
sc = d["metric_definition_check"]["sharpest_contrast"]
assert d["metric_definition_check"]["diverges"] is True
assert sc["tag"] == "sft_v13_0500" and sc["blocks_mild"] == 399, sc
assert sc["instrument_looped_k"] == 2, sc
PY

# 33.4 Детерминизм: пересборка тем же входом даёт тот же артефакт. Проверка ловит
#      «числа зависят от порядка обхода», а не только опечатку.
expect_exit 0 "S3at: пересборка тем же входом даёт тот же артефакт" \
  python3 - "$TMP" <<'PY'
import json, pathlib, subprocess, sys
tmp = pathlib.Path(sys.argv[1])
out = tmp / "s3at_again.json"
ck = pathlib.Path("/home/user/gb10-shared/sft-20260919-0810/checkpoints")
cmd = [sys.executable, "tools/assemble_s3at_onset.py",
       "--origin", "runs/s3at-loop-onset-20260921/loop_origin.json", "--out", str(out)]
if ck.is_dir():
    cmd += ["--ckpt-dir", str(ck), "--grid-lo", "500", "--grid-hi", "32600"]
r = subprocess.run(cmd, capture_output=True, text=True)
assert r.returncode == 0, (r.returncode, r.stderr[-500:])
a = json.loads(pathlib.Path("evidence/sft-loop-onset.json").read_text(encoding="utf-8"))
b = json.loads(out.read_text(encoding="utf-8"))
for d in (a, b):
    d.pop("generated_at")
    if not ck.is_dir():                     # перепись диска зависит от хоста, не от входа
        d["coverage"].pop("disk_census", None)
        d.pop("inventory", None)
assert json.dumps(a, ensure_ascii=False, sort_keys=True) == \
       json.dumps(b, ensure_ascii=False, sort_keys=True)
PY

# 33.5 Красный путь: склейка разных приборов и разных наборов промптов обязана быть
#      отказом (1). Молчаливая склейка мерила бы разницу приборов, а не обучения —
#      и «кривая зарождения» была бы кривой смены прибора.
expect_exit 0 "S3at: разные приборы/наборы — отказ, а не склейка" \
  python3 - <<'PY'
import importlib.util as u, pathlib
spec = u.spec_from_file_location("s3at", pathlib.Path("tools/assemble_s3at_onset.py"))
M = u.module_from_spec(spec); spec.loader.exec_module(M)
def rec(tag, step, inst, dig):
    v = {"instrument_looped_share": [1] + [0] * 103, "truncated_share": [0] * 104,
         "unclosed_think_natural": [0] * 104}
    return {"report": "r", "tag": tag, "step": step, "n_probes": 104,
            "instrument_sha256": inst, "prompts_digest": dig, "prompts_set": "wide",
            "max_new_tokens": 4096, "batch_size": 8, "stop_at_turn_end": True,
            "decoding": {}, "runtime_marker": "/root/.cache", "_probes": [],
            "vectors": v,
            "shares": {k: {"k": sum(x), "n": len(x), "share": sum(x) / len(x),
                           "ci95": M.T.wilson(sum(x), len(x))} for k, x in v.items()}}
assert M.comparability([rec("a", 1, "p1", "d1"), rec("b", 2, "p2", "d1")])["verdict"] \
    == "not_comparable"                                    # разные приборы
assert M.comparability([rec("a", 1, "p1", "d1"), rec("b", 2, "p1", "d2")])["verdict"] \
    == "not_comparable"                                    # разные наборы промптов
assert M.comparability([rec("a", 1, "p1", "d1"), rec("b", 2, "p1", "d1")])["verdict"] \
    == "comparable"
PY

# ── rl_env_probe_gb10.py (ADR-049 п.10): проба предусловий RL ────────────────
# Фикстура — синтетический набор из ОДНОЙ задачи: контракт прибора проверяется на
# нём целиком (свой вердикт, свои пробы), а не на пяти задачах скелета (тот набор
# живёт в другой ветви и здесь только читается). Зелёный путь требует docker;
# где docker нет — проверяются только NOT-VERIFIED-пути, и это названо в выводе.
echo "== 40. rl_env_probe_gb10.py (ADR-049 п.10: предусловия RL на стенде) =="
if true; then
  GT="$TMP/gs-fixture"
  mkdir -p "$GT/tasks/gs-t-01/seed/in"
  printf 'label,value\na,7\nb,35\n' > "$GT/tasks/gs-t-01/seed/in/series.csv"
  cat > "$GT/tasks/gs-t-01/solution.sh" <<'SOL'
set -e
mkdir -p out
awk -F, 'NR > 1 { s += $2 } END { print s }' in/series.csv > out/total.txt
SOL
  cat > "$GT/tasks/gs-t-01/verify.py" <<'VER'
import argparse, json, pathlib, sys
ap = argparse.ArgumentParser()
for a in ("snapshot", "task", "seed"):
    ap.add_argument(f"--{a}", required=True)
for a in ("image", "holdout", "journal", "mock-module", "scratch"):
    ap.add_argument(f"--{a}", default="")
ap.add_argument("--mock-seed", type=int, default=0)
args = ap.parse_args()
want = 0
for n, line in enumerate((pathlib.Path(args.seed) / "in" / "series.csv").read_text().splitlines()):
    if n and line.strip():
        want += int(line.split(",")[1])
reads = ["pristine:in/series.csv", "snapshot:out/total.txt"]
f = pathlib.Path(args.snapshot) / "out" / "total.txt"
if not f.is_file():
    print(json.dumps({"score": 0, "reason": "out/total.txt отсутствует", "expected_public": str(want), "reads": reads}))
    sys.exit(0)
tok = f.read_text().split()
score = 1 if len(tok) == 1 and tok[0].isdigit() and int(tok[0]) == want else 0
print(json.dumps({"score": score, "reason": "сверено с пересчётом", "expected_public": str(want), "reads": reads}))
VER
  cat > "$GT/tasks/gs-t-01/task.json" <<'TJ'
{"schema": "grounded-task/1", "id": "gs-t-01", "tool": "shell", "network": "none",
 "prompt": "Собери сумму столбца value.", "seed_dir": "seed", "solution": "solution.sh",
 "verifier": "verify.py", "timeout_s": 30}
TJ
  # Каталог прогона — в $HOME, не в $TMP: docker на локальной машине (snap-конфайнмент)
  # не видит /tmp и молча отдаёт в контейнер пустой каталог вместо bind-mount,
  # из-за чего проба изоляции честно краснеет на bind_mount_writable.
  GRUN="${HOME}/.cache/arch-ml/rlprobe-tool-test-$$"
  rm -rf "$GRUN"
  if docker version >/dev/null 2>&1; then
    expect_exit 0 "набор заземлён: run=1, пробы 0, изоляция подтверждена" \
      python3 tools/rl_env_probe_gb10.py --set-dir "$GT" --passes 1 --reps 1 \
        --run-dir "$GRUN" --out "$TMP/gs-ok.json"
    # Отчёт обязан нести вердикт и цену, а не только код возврата.
    if python3 - "$TMP/gs-ok.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
ok = (d["status"] == "ok" and d["verdict"]["isolation_works"]
      and all(d["gates"][f"G{i}"]["pass"] for i in (1, 2, 3, 4))
      and d["cost"]["measured"]["t_step_env_s"] is not None
      and d["step_limit"]["binds_on_state"] is True
      and d["no_action"]["share"] == 0.0)
sys.exit(0 if ok else 1)
PY
    then
      PASS=$((PASS + 1)); printf '  ok   %-58s\n' "отчёт: вердикт, цена, лимит шагов и no-action=0"
    else
      FAIL=$((FAIL + 1)); failures+=("отчёт пробы неполон: $TMP/gs-ok.json")
      printf '  FAIL %-58s\n' "отчёт: вердикт, цена, лимит шагов и no-action=0"
    fi
    # Красный путь: верификатор, всегда возвращающий 1, обязан покраснеть на пробе G2.
    GT2="$TMP/gs-fixture-red"
    cp -r "$GT" "$GT2"
    printf 'import json\nprint(json.dumps({"score": 1, "reason": "всегда единица", "expected_public": "42", "reads": ["snapshot:out/total.txt"]}))\n' > "$GT2/tasks/gs-t-01/verify.py"
    expect_exit 1 "верификатор «всегда 1» — проба G2 красная, а не зелёная" \
      python3 tools/rl_env_probe_gb10.py --set-dir "$GT2" --passes 1 --reps 1 \
        --run-dir "$GRUN-red" --out "$TMP/gs-red.json"
  else
    printf '  --   %-58s\n' "docker недоступен: зелёный и красный пути не проверены"
  fi
  expect_exit 2 "нет набора задач — NOT-VERIFIED, а не «заземление без скелета»" \
    python3 tools/rl_env_probe_gb10.py --set-dir "$TMP/gs-absent" --passes 1 --reps 1 \
      --run-dir "$GRUN-absent" --out "$TMP/gs-absent.json"
  rm -rf "$GRUN" "$GRUN-red" "$GRUN-absent"
fi
echo "== 41. check_rule_numbers.py (уникальность номеров правил, ADR-046 п.8 / C-032) =="
# Зубы проверяются на СИНТЕТИЧЕСКОМ репозитории: канон, ветвь со свободным номером,
# ветвь со занятым, два кандидата на один номер и слитая ветвь. Ветви настоящего
# репозитория тест не создаёт и не трогает: соседние дельты делят один git.
RN="$TMP/rn"; rm -rf "$RN"; mkdir -p "$RN"
git -C "$RN" init -q -b main
git -C "$RN" config user.email t@example.invalid
git -C "$RN" config user.name t
mk_rules() {  # mk_rules <файл> <id>...  — минимальный конформный реестр
  local f="$1"; shift
  { echo "constraints:"
    local id
    for id in "$@"; do
      printf '  - id: %s\n    name: "правило %s"\n    type: command_succeeds\n    command: "true"\n' "$id" "$id"
    done
  } > "$f"
}
mk_rules "$RN/CONSTRAINTS.yaml" C-001 C-002
git -C "$RN" add -A && git -C "$RN" commit -qm "канон: C-001, C-002"
# ветвь, выдавшая СВОБОДНЫЙ номер, и ветвь, выдавшая номер, который канон займёт
# позже: ровно так номер выдаётся локальным счётом каталога (ADR-046 п.8)
git -C "$RN" checkout -qb feat-free
mk_rules "$RN/CONSTRAINTS.yaml" C-001 C-002 C-005
git -C "$RN" commit -qam "feat-free: C-005"
git -C "$RN" checkout -qb feat-stale main
mk_rules "$RN/CONSTRAINTS.yaml" C-001 C-002 C-003
git -C "$RN" commit -qam "feat-stale: C-003 (канон займёт его позже)"
git -C "$RN" checkout -qb pair-a main
mk_rules "$RN/CONSTRAINTS.yaml" C-001 C-002 C-006
git -C "$RN" commit -qam "pair-a: C-006"
git -C "$RN" checkout -qb pair-b main
mk_rules "$RN/CONSTRAINTS.yaml" C-001 C-002 C-006
git -C "$RN" commit -qam "pair-b: C-006"
git -C "$RN" checkout -q main
mk_rules "$RN/CONSTRAINTS.yaml" C-001 C-002 C-003 C-004
git -C "$RN" commit -qam "канон: C-003, C-004"

expect_exit 1 "номер, выданный ветвью, занят каноном → КРАСНЫЙ" \
  python3 tools/check_rule_numbers.py --repo "$RN" --case "$RN"
expect_contains "feat-stale" "ветвь-нарушитель названа поимённо" \
  python3 tools/check_rule_numbers.py --repo "$RN" --case "$RN"
expect_contains "УЖЕ ЗАНЯТ в каноне" "находка сформулирована как занятость номера каноном" \
  python3 tools/check_rule_numbers.py --repo "$RN" --case "$RN"
expect_contains '"C-003"' "номер назван в машинном вердикте (--json)" \
  python3 tools/check_rule_numbers.py --repo "$RN" --case "$RN" --json
expect_contains "feat-free" "ветвь со свободным номером показана, а не скрыта" \
  python3 tools/check_rule_numbers.py --repo "$RN" --case "$RN"
expect_contains "pair-a" "кандидат из пары назван" \
  python3 tools/check_rule_numbers.py --repo "$RN" --case "$RN"
expect_contains "pair-b" "второй кандидат пары назван" \
  python3 tools/check_rule_numbers.py --repo "$RN" --case "$RN"
expect_contains "выдан более чем одной незалитой ветвью" "класс «два кандидата на один номер» назван" \
  python3 tools/check_rule_numbers.py --repo "$RN" --case "$RN"

# слитая ветвь (её вершина — предок канона) кандидатом не считается
git -C "$RN" checkout -qb feat-merged main
mk_rules "$RN/CONSTRAINTS.yaml" C-001 C-002 C-003 C-004 C-007
git -C "$RN" commit -qam "feat-merged: C-007"
git -C "$RN" checkout -q main
git -C "$RN" merge -q --no-ff feat-merged -m "приёмка feat-merged"
expect_contains "feat-merged — merged-into-canon" "уже слитая ветвь не красит — названа пропуском" \
  python3 tools/check_rule_numbers.py --repo "$RN" --case "$RN"

# коллизии убраны (ветви удалены) → зелёное; находка не «залипает»
git -C "$RN" branch -qD feat-stale pair-a pair-b
expect_exit 0 "коллизий нет — свободный номер ветви и слитая ветвь зелёные" \
  python3 tools/check_rule_numbers.py --repo "$RN" --case "$RN"
expect_contains "ВЕРДИКТ: коллизий нет" "вердикт назван словом, а не только кодом" \
  python3 tools/check_rule_numbers.py --repo "$RN" --case "$RN"

# дубль номера внутри самого файла правил — находка, даже если ветвей нет
RN2="$TMP/rn-dup"; rm -rf "$RN2"; mkdir -p "$RN2"
git -C "$RN2" init -q -b main
git -C "$RN2" config user.email t@example.invalid
git -C "$RN2" config user.name t
mk_rules "$RN2/CONSTRAINTS.yaml" C-001 C-001
git -C "$RN2" add -A && git -C "$RN2" commit -qm "дубль номера"
expect_exit 1 "дубль номера внутри файла правил → КРАСНЫЙ" \
  python3 tools/check_rule_numbers.py --repo "$RN2" --case "$RN2"
expect_contains "дубль C-001" "дубль назван номером" \
  python3 tools/check_rule_numbers.py --repo "$RN2" --case "$RN2"

# нет файла правил канона → NOT-VERIFIED (exit 2), а не «зелено»
RN3="$TMP/rn-none"; rm -rf "$RN3"; mkdir -p "$RN3"
git -C "$RN3" init -q -b main
git -C "$RN3" config user.email t@example.invalid
git -C "$RN3" config user.name t
echo ok > "$RN3/README.md"; git -C "$RN3" add -A; git -C "$RN3" commit -qm "без правил"
expect_exit 2 "нет файла правил канона → NOT-VERIFIED" \
  python3 tools/check_rule_numbers.py --repo "$RN3" --case "$RN3"
# не репозиторий вовсе → NOT-VERIFIED
expect_exit 2 "не git-репозиторий → NOT-VERIFIED" \
  python3 tools/check_rule_numbers.py --repo "$TMP/no-repo-here" --case "$TMP/no-repo-here"
# страж настоящего кейса: зелёный и ТОЛЬКО чтение — ссылок не создаёт и не двигает
expect_exit 0 "страж настоящего кейса зелёный (номера ветвей свободны в каноне)" \
  python3 tools/check_rule_numbers.py
RN_BEFORE="$(git for-each-ref --format='%(refname) %(objectname)' refs/heads/ | sort)"
python3 tools/check_rule_numbers.py >/dev/null 2>&1
RN_AFTER="$(git for-each-ref --format='%(refname) %(objectname)' refs/heads/ | sort)"
expect_exit 0 "страж только читает: набор ссылок и их вершины не изменились" \
  bash -c 'test "$1" = "$2"' _ "$RN_BEFORE" "$RN_AFTER"

echo
echo
echo "== 42. S3ay: ранний гейт полной стадии — память, полнота прибора, вердикт (S3ay-fix) =="
# Дефект, который проверяется. Сторож полной стадии `sft-v13-2e6-20260921-1748`
# висел в `wait_memory` с `available_gb: None`, хотя триггер (шаг 500 и точка
# `sft_probe_500.pt`) сработал: на стенде `free` печатает по локали («Память:»),
# `awk '/Mem:/'` не находил строку, `int('')` давал `None`. Предохранителя не было,
# при том что выглядел он работающим. Тот же урок уже был пройден в
# `start_full_stage.py` — здесь он не был применён.
#
# Второй дефект той же природы: прибор доставлялся БЕЗ модулей (`probe_control`,
# `ppl_probe`), которые импортирует на уровне файла, — проба упала бы
# ModuleNotFoundError за секунды, а `CHAIN_DONE` при этом появился бы.
#
# Проверяется: (а) локализованный `free` больше не влияет — память читается из
# /proc/meminfo; (б) запасной путь — `free` с ПРИНУДИТЕЛЬНОЙ локалью C; (в) оба
# источника недоступны → ЯВНЫЙ отказ (exit 2, `memory_guard=not_armed`), а не тихий
# вечный `wait_memory`; (г) МУТАНТ со старой строкой чтения обязан УПАСТЬ на (а), а
# МУТАНТ на (в) — не выйти сам; (д) прибор без модулей не запускается; (е) арифметика
# вердикта (продолжать / предупредить / остановить по каждой метрике); (ж) сценарии
# идентичности входа C-030.

GATE_TMP="$TMP/s3ay-gate"; mkdir -p "$GATE_TMP/bin" "$GATE_TMP/run/tools" \
                                  "$GATE_TMP/run/checkpoints" "$GATE_TMP/run/logs"

#: Заглушка `free` в локали стенда: `LANG=ru_RU.utf8` печатает «Память:» и русские
#: заголовки, `LC_ALL=C` — английские. Заглушка повторяет ОБА поведения, потому что
#: проверяется именно то, что код не зависит от локали: если запасной путь забудет
#: `LC_ALL=C`, он получит русский вывод и не найдёт строку — тест это увидит.
cat > "$GATE_TMP/bin/free" <<'SH'
#!/usr/bin/env bash
loc="${LC_ALL:-${LANG:-}}"
case "$loc" in
  C|C.UTF-8|POSIX)
    printf '               total        used        free      shared  buff/cache   available\n'
    printf 'Mem:          124610       47539       82861        1070        2033       77070\n'
    printf 'Swap:          32767         336       32431\n' ;;
  *)
    printf '               всего        занят        своб      общая  буф/врем.   доступно\n'
    printf 'Память:       124610       47539       82861        1070        2033       77070\n'
    printf 'Подкачка:      32767         336       32431\n' ;;
esac
SH
chmod +x "$GATE_TMP/bin/free"

#: Заглушка ssh: выполняет команду локально, подсовывая заглушки из `$FAKE_BIN_DIR`.
#: `FAKE_MEMINFO_OK=0` — /proc/meminfo недоступен (второй источник обязан подхватить),
#: `FAKE_SSH_FAIL=1` — ssh не поднялся вовсе (недоступны оба).
cat > "$GATE_TMP/bin/ssh" <<'SH'
#!/usr/bin/env bash
args=("$@"); i=0
while [ $i -lt ${#args[@]} ]; do
  case "${args[$i]}" in
    -o) i=$((i+2)) ;;
    -*) i=$((i+1)) ;;
    *) break ;;
  esac
done
cmd="${args[@]:$((i+1))}"
[ -n "$cmd" ] || exit 0
if [ "${FAKE_SSH_FAIL:-0}" = 1 ]; then
  echo "ssh: connect to host ${args[$i]} port 22: Connection refused" >&2
  exit 255
fi
case "$cmd" in
  *meminfo*)
    if [ "${FAKE_MEMINFO_OK:-1}" != 1 ]; then
      echo "awk: /proc/meminfo: No such file or directory" >&2; exit 127
    fi ;;
esac
PATH="${FAKE_BIN_DIR:-/nonexistent}:$PATH" bash -c "$cmd"
SH
chmod +x "$GATE_TMP/bin/ssh"

#: Фикстура каталога прогона: полное объявление гейта, доказанный вход (C-030),
#: шаг больше 500 и точка на месте — то есть ВСЁ, что нужно, чтобы дойти до
#: `wait_memory`. Дальше решает только чтение памяти.
python3 - "$GATE_TMP/run" <<'PY'
import json, pathlib, sys
run = pathlib.Path(sys.argv[1])
#: Пути как в живом прогоне (сверено с input_identity_verdict.json остановленной
#: стадии): объявление пишет путь ОТНОСИТЕЛЬНО общего диска, пайплайн — путь
#: КОНТЕЙНЕРА. Сверка идёт по `endswith`, поэтому фикстура обязана это повторять.
(run / "full_sft_params.json").write_text(json.dumps({"data": {
    "sft_jsonl": "datasets/sft_train_v13_fixed.jsonl",
    "sft_jsonl_sha256": "71c4bd2b" + "0" * 56,
    "sft_examples": 44105,
    "tok_cache": "datasets/tok/sft_train_v13_fixed_8192_qwen25.npz",
    "tok_cache_sha256": "dc3d4838" + "0" * 56}}, ensure_ascii=False), encoding="utf-8")
(run / "checkpoints" / "run_manifest.json").write_text(json.dumps({"sft_input": {
    "jsonl": {"path": "/workspace/shared/datasets/sft_train_v13_fixed.jsonl",
              "sha256": "71c4bd2b" + "0" * 56, "samples": 44105, "hash_scope": "full"},
    "tensor": {"path": "/workspace/shared/datasets/tok/sft_train_v13_fixed_8192_qwen25.npz",
               "sha256": "dc3d4838" + "0" * 56, "samples": 44105, "hash_scope": "full"}}},
    ensure_ascii=False), encoding="utf-8")
(run / "logs" / "loss_trace.jsonl").write_text(
    "\n".join(json.dumps({"step": s, "loss": 1.0}) for s in (400, 500, 517)) + "\n",
    encoding="utf-8")
(run / "checkpoints" / "sft_probe_500.pt").write_bytes(b"fixture")   # файл-признак
gate = {"declared_before_run": True, "why": "фикстура теста",
        "when": {"step": 500, "artifact": "checkpoints/sft_probe_500.pt"},
        "probe": {"prompts": "wide", "n_probes": 104, "max_new_tokens": 4096,
                  "batch_size": 8, "tool_sha256": "99dafa8d" + "0" * 56},
        "reference": [], "metrics": [], "mdd": 0.15, "warn_delta": 0.05}
(run / "early_gate.json").write_text(json.dumps(gate, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
PY
#: Прибор с модулями — как его доставляет сборщик (`tools/` внутри каталога прогона).
cp tools/probe_language_split.py tools/probe_control.py tools/ppl_probe.py "$GATE_TMP/run/tools/"

gate_run() {   # gate_run <каталог> <аргументы...>
  local dir="$1"; shift
  env PATH="$GATE_TMP/bin:$PATH" FAKE_BIN_DIR="$GATE_TMP/bin" \
      python3 tools/early_gate_watch.py --run-dir "$dir" "$@"
}

echo "  --- 34a. память читается из /proc/meminfo, локаль стенда не мешает ---"
expect_exit 0 "локализованный free («Память:») не мешает: память прочитана" \
  gate_run "$GATE_TMP/run" --check-memory
expect_exit 0 "источник назван, число совпало с /proc/meminfo этой машины (допуск — величина живая)" \
  python3 - "$GATE_TMP/run/early_gate_memory_preflight.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
kb = None
for line in open("/proc/meminfo", encoding="utf-8"):
    if line.startswith("MemAvailable:"):
        kb = int(line.split()[1]); break
assert kb is not None, "нет MemAvailable в /proc/meminfo"
want = round(kb / 1048576.0, 2)
assert d["source"] == "proc_meminfo", d
assert d["available_gb"] is not None, "число есть, а не None"
# Точное равенство здесь было бы гонкой: MemAvailable — живая величина, и между
# записью отчёта прибором и повторным чтением тестом она уезжает. Измерено на
# приёмке 22.09.2026 (машина делится с флотом): 37.78 против 37.79 — 0.01 ГБ за
# секунды, то есть ровно на пороге округления до двух знаков; на площадке ветви
# тест проходил по случайности. Допуск проверку не ослабляет: неверный источник,
# None или число, взятое не из meminfo, расходятся с ней на гигабайты.
assert abs(d["available_gb"] - want) <= 1.0, (d["available_gb"], want)
PY

echo "  --- 34б. запасной путь: free с принудительной локалью C ---"
expect_exit 0 "meminfo недоступен — запасной источник подхватил" \
  env FAKE_MEMINFO_OK=0 PATH="$GATE_TMP/bin:$PATH" FAKE_BIN_DIR="$GATE_TMP/bin" \
  python3 tools/early_gate_watch.py --run-dir "$GATE_TMP/run" --check-memory
expect_exit 0 "запасной путь потребовал локаль C (иначе «Память:» не разобрать)" \
  python3 - "$GATE_TMP/run/early_gate_memory_preflight.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["source"] == "free_locale_c", d
assert d["available_gb"] == round(77070 / 1024.0, 2), d["available_gb"]
assert "LC_ALL=C" in d["sources"][1]["cmd"], d["sources"][1]["cmd"]
assert d["sources"][0]["rc"] not in (0, None), "первый источник обязан был отказать"
PY

echo "  --- 34в. оба источника недоступны → явный отказ, а не вечное ожидание ---"
#: Бюджет времени объявлен тестом: старый код на этих же данных не выходил сам
#: (ждал таймаут `--wait-mem-hours`), новый обязан выйти за секунды. Отсюда
#: `timeout 60` вокруг: он и есть проверка «не блокироваться навсегда».
GATE_T0=$(date +%s)
expect_exit 2 "память не читается → NOT-VERIFIED (exit 2), стадия не останавливается" \
  timeout 60 env FAKE_SSH_FAIL=1 PATH="$GATE_TMP/bin:$PATH" FAKE_BIN_DIR="$GATE_TMP/bin" \
  python3 tools/early_gate_watch.py --run-dir "$GATE_TMP/run" --mem-unreadable-polls 2 \
  --poll-secs 1 --wait-mem-hours 3
GATE_DT=$(( $(date +%s) - GATE_T0 ))
expect_exit 0 "отказ назван явно: memory_guard=not_armed и число попыток" \
  python3 - "$GATE_TMP/run/early_gate_verdict.json" "$GATE_TMP/run/early_gate_state.json" <<'PY'
import json, sys
v = json.load(open(sys.argv[1], encoding="utf-8"))
assert v["verdict"] == "not_verified", v
assert v["memory_guard"] == "not_armed", v
assert "не читается" in v["why"], v["why"]
st = json.load(open(sys.argv[2], encoding="utf-8"))
phases = [e["phase"] for e in st["events"]]
assert phases.count("memory_unreadable") == 2, phases     # ровно объявленное число
assert "memory_guard_not_armed" in phases, phases
PY
expect_exit 0 "выход за секунды, а не за объявленные 3 часа (замер: ${GATE_DT}с)" \
  test "$GATE_DT" -lt 60

echo "  --- 34г. зубы: МУТАНТ со старой строкой чтения обязан упасть ---"
#: Мутант — тот же файл со старой строкой чтения памяти (`free -m | awk '/Mem:/'`).
#: Он обязан НЕ прочитать память на локализованном `free`; если прочитает, тест (а)
#: ничего не проверяет.
python3 - "$GATE_TMP/mutant-free.py" <<'PY'
import pathlib, sys
src = pathlib.Path("tools/early_gate_watch.py").read_text(encoding="utf-8")
old = 'MEM_SOURCES = (("proc_meminfo", MEMINFO_CMD, 1048576.0),\n' \
      '               ("free_locale_c", MEMINFO_FALLBACK_CMD, 1024.0))'
new = 'MEM_SOURCES = (("old_free_mem", "free -m | awk \'/Mem:/{print $7}\'", 1024.0),)'
assert src.count(old) == 1, "якорь мутанта изменился — тест перестал быть зубастым"
src = src.replace(old, new)
#: Второй дефект — тихое ожидание до таймаута: снимаем выход по нечитаемости,
#: оставляя цикл. Мутант обязан повторять ОБА дефекта, иначе «не выходит сам»
#: проверяло бы только половину.
old2 = """                misses += 1
                self.beat("memory_unreadable", misses=misses,
                          limit=self.a.mem_unreadable_polls, **mem_beat_kw(ev))
                if misses >= self.a.mem_unreadable_polls:
                    self.beat("memory_guard_not_armed", misses=misses, **mem_beat_kw(ev))
                    return "unreadable"
"""
assert src.count(old2) == 1, "якорь мутанта (тихое ожидание) изменился"
src = src.replace(old2, "                misses += 1\n")
pathlib.Path(sys.argv[1]).write_text(src, encoding="utf-8")
PY
expect_exit 2 "МУТАНТ «free по слову Mem:» на локали стенда память НЕ читает" \
  timeout 60 env PATH="$GATE_TMP/bin:$PATH" FAKE_BIN_DIR="$GATE_TMP/bin" \
  python3 "$GATE_TMP/mutant-free.py" --run-dir "$GATE_TMP/run" --check-memory
expect_exit 124 "МУТАНТ на недоступной памяти не выходит сам (тихое вечное ожидание)" \
  timeout 12 env FAKE_SSH_FAIL=1 PATH="$GATE_TMP/bin:$PATH" FAKE_BIN_DIR="$GATE_TMP/bin" \
  python3 "$GATE_TMP/mutant-free.py" --run-dir "$GATE_TMP/run" --poll-secs 1 --wait-mem-hours 3

echo "  --- 34д. прибор без модулей не запускается (а не падает на середине) ---"
rm -f "$GATE_TMP/run/tools/probe_control.py"
expect_exit 0 "неполный прибор назван вслух: probe_tool.incomplete непуст" \
  python3 - "$GATE_TMP" <<'PY'
import json, subprocess, sys
tmp = sys.argv[1]
p = subprocess.run(["python3", "tools/early_gate_watch.py", "--run-dir", f"{tmp}/run",
                    "--dry-run"], capture_output=True, text=True,
                   env={"PATH": f"{tmp}/bin:/usr/bin:/bin", "FAKE_BIN_DIR": f"{tmp}/bin"})
d = json.loads(p.stdout)
assert d["probe_tool"]["path"] is None, d["probe_tool"]
assert any("probe_control.py" in m for m in d["probe_tool"]["incomplete"]), d["probe_tool"]
assert d["host_memory"]["available_gb"] is not None, d["host_memory"]
PY
cp tools/probe_control.py "$GATE_TMP/run/tools/"
expect_exit 0 "полный прибор найден той же раскладкой (tools/ внутри каталога прогона)" \
  python3 - "$GATE_TMP" <<'PY'
import json, subprocess, sys
tmp = sys.argv[1]
p = subprocess.run(["python3", "tools/early_gate_watch.py", "--run-dir", f"{tmp}/run",
                    "--dry-run"], capture_output=True, text=True,
                   env={"PATH": f"{tmp}/bin:/usr/bin:/bin", "FAKE_BIN_DIR": f"{tmp}/bin"})
d = json.loads(p.stdout)
assert d["probe_tool"]["path"].endswith("tools/probe_language_split.py"), d["probe_tool"]
assert d["probe_tool"]["incomplete"] == [], d["probe_tool"]
PY

#: Раскладка доставки строится СБОРЩИКОМ, а не руками. Тест выше копирует три файла
#: сам — и потому не видел дефекта первой редакции правки: сборка клала прибор в
#: КОРЕНЬ каталога прогона, а модули в `tools/`, и сторож не находил прибор НИ ОДНОЙ
#: из двух раскладок, которые пробует (`probe_tool_path`). На 500-м шаге гейт остался
#: бы без замера, назвав отказ `probe_tool_missing` — предохранитель, которого нет,
#: снова выглядел бы работающим. Проверяется САМА РАСКЛАДКА: путь доставки берётся у
#: сборщика, полнота — у сторожа; зубы — старая раскладка обязана НЕ находиться.
expect_exit 0 "раскладка сборщика: прибор с модулями в одной папке — сторож находит" \
  python3 - "$GATE_TMP" <<'PY'
import pathlib, shutil, sys
sys.path.insert(0, "tools")
import launch_full_stage as F          # импорт ради ПРАВИЛА раскладки, не ради сборки
import early_gate_watch as W
root = pathlib.Path(sys.argv[1])

def lay(name, subdir_pred):
    """Разложить орудия гейта правилом `subdir_pred` (rel → подкаталог `tools/`?)."""
    run = root / name
    shutil.rmtree(run, ignore_errors=True)
    (run / "tools").mkdir(parents=True)
    for rel in F.GATE_TOOLS:
        dst = (run / "tools" / pathlib.Path(rel).name) if subdir_pred(rel) else (run / pathlib.Path(rel).name)
        shutil.copyfile(F.CASE_ROOT / rel, dst)
    return run

#: Раскладка сборщика: путь берётся у САМОГО сборщика (`gate_delivery`), а не задаётся
#: здесь — иначе тест проверял бы свою копию правила, а не то правило, что исполняется.
run = root / "layout-run"
shutil.rmtree(run, ignore_errors=True)
(run / "tools").mkdir(parents=True)
for rel in F.GATE_TOOLS:
    dst = F.gate_delivery(run, rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(F.CASE_ROOT / rel, dst)
tool, missing, tried = W.probe_tool_path(run)
assert tool is not None, f"сборщик разложил прибор так, что сторож его не находит: {missing} / {tried}"
assert missing == [], missing
assert tool == run / "tools" / "probe_language_split.py", tool
assert (run / "early_gate_watch.py").is_file(), "сторож обязан лежать в корне (его так запускает стартер)"

#: Зубы: СТАРОЕ правило (в подкаталог уходили только модули) обязано не находиться.
buggy = lay("layout-run-buggy", lambda rel: rel in F.GATE_PROBE_MODULES)
tool2, missing2, _ = W.probe_tool_path(buggy)
assert tool2 is None, f"старая раскладка не должна находиться, а нашлась: {tool2}"
assert any("probe_control.py" in m for m in missing2), missing2
PY

echo "  --- 34е. арифметика вердикта: продолжать / предупредить / остановить ---"
expect_exit 0 "5 случаев: равенство, улучшение, предупреждение, две остановки" \
  python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("egw", "tools/early_gate_watch.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
GATE = {"mdd": 0.15, "warn_delta": 0.05, "metrics": [
    {"key": "truncated_share", "path": ["aggregate", "truncated_share"],
     "name": "доля усечений", "role": "r"},
    {"key": "natural_unclosed_think_share",
     "path": ["aggregate", "stop", "natural", "unclosed_think_share"],
     "name": "незакрытый <think>", "role": "r"},
    {"key": "natural_looped_share", "path": ["aggregate", "stop", "natural", "looped_share"],
     "name": "петли по сегментам", "role": "r"}]}

def rep(t, u, l):
    return {"states": {"s": {"aggregate": {"n": 104, "truncated_share": t,
            "stop": {"natural": {"unclosed_think_share": u, "looped_share": l}}}}}}

REF = {"tag": "arm", "states_key": "s", "report": rep(0.0962, 0.02, 0.01)}
go = m.compare_metrics(GATE, "s", rep(0.0962, 0.02, 0.01), REF)
assert go["verdict"] == "continue" and go["reference_used"] == "arm", go
assert go["warnings"] == [], go
better = m.compare_metrics(GATE, "s", rep(0.05, 0.0, 0.0), REF)
assert better["verdict"] == "continue" and better["warnings"] == [], better
warn = m.compare_metrics(GATE, "s", rep(0.0962 + 0.08, 0.02, 0.01), REF)
assert warn["verdict"] == "continue", warn
assert warn["warnings"] == ["truncated_share"], warn
stop_t = m.compare_metrics(GATE, "s", rep(0.0962 + 0.20, 0.02, 0.01), REF)
assert stop_t["verdict"] == "fail" and "доля усечений" in stop_t["why"], stop_t
stop_u = m.compare_metrics(GATE, "s", rep(0.0962, 0.02 + 0.30, 0.01), REF)
assert stop_u["verdict"] == "fail" and "незакрытый" in stop_u["why"], stop_u
stop_l = m.compare_metrics(GATE, "s", rep(0.0962, 0.02, 0.01 + 0.16), REF)
assert stop_l["verdict"] == "fail" and "петли по сегментам" in stop_l["why"], stop_l
#: Порог объявлен: ровно MDD — уже остановка, «строго больше» было бы шумом при n=104.
edge = m.compare_metrics(GATE, "s", rep(0.0962 + 0.15, 0.02, 0.01), REF)
assert edge["verdict"] == "fail", edge
#: Отсутствие метрики в отчёте — это НЕ «ноль»: сравнить нечем, и строка не судится.
miss = m.compare_metrics(GATE, "s", {"states": {"s": {"aggregate": {"n": 104}}}}, REF)
assert miss["verdict"] == "continue", miss
assert "delta" not in miss["metrics"][0], "отсутствие числа не судится как 0"
PY
expect_exit 0 "опора берётся первая ЧИТАЕМАЯ, а состояние ищется по states_key" \
  python3 - <<'PY'
import importlib.util, json, pathlib, tempfile
spec = importlib.util.spec_from_file_location("egw", "tools/early_gate_watch.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
tmp = pathlib.Path(tempfile.mkdtemp(prefix="laguna-s3ay-gate-"))
missing = tmp / "нет.json"
ok = tmp / "ok.json"
ok.write_text(json.dumps({"states": {"arm_prev": {"aggregate": {"n": 104,
    "truncated_share": 0.0962, "stop": {"natural": {"unclosed_think_share": 0.0,
                                                    "looped_share": 0.0}}}}}}), encoding="utf-8")
gate = {"reference": [{"tag": "нет", "path": str(missing)},
                      {"tag": "prev", "states_key": "arm_prev", "path": str(ok),
                       "why": "w"}]}
ref = m.pick_reference(gate, 104)
assert ref and ref["tag"] == "prev" and ref["states_key"] == "arm_prev", ref
#: Опора с чужим n (обрезанный отчёт) не берётся: сравнивать 104 с 24 нельзя.
ok.write_text(json.dumps({"states": {"arm_prev": {"aggregate": {"n": 24}}}}),
              encoding="utf-8")
assert m.pick_reference(gate, 104) is None, "неполный отчёт принят за опору"
PY

echo "  --- 34ж. идентичность входа (C-030): честный / подмена / усечённый хеш ---"
expect_exit 0 "4 сценария входа: честный, подмена набора, hash_scope=head, блок без sft_input" \
  python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("egw", "tools/early_gate_watch.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
DECL = {"sft_jsonl": "datasets/sft_train_v13_fixed.jsonl",
        "sft_jsonl_sha256": "71c4bd2b" + "0" * 56, "sft_examples": 44105,
        "tok_cache": "datasets/tok/sft_train_v13_fixed_8192_qwen25.npz",
        "tok_cache_sha256": "dc3d4838" + "0" * 56}

def got(jsonl, jsha, n, tensor, tsha, scope="full"):
    return {"jsonl": {"path": jsonl, "sha256": jsha, "samples": n, "hash_scope": scope},
            "tensor": {"path": tensor, "sha256": tsha, "samples": n, "hash_scope": scope}}

#: (а) честный: объявлено v13 — прочитано v13 (факт — путь контейнера, объявление —
#: относительно диска; сверка по endswith, как в живом прогоне)
rows, bad = m.identity_rows(DECL, got("/workspace/shared/datasets/sft_train_v13_fixed.jsonl",
                                      DECL["sft_jsonl_sha256"], 44105,
                                      "/workspace/shared/datasets/tok/sft_train_v13_fixed_8192_qwen25.npz",
                                      DECL["tok_cache_sha256"]))
assert bad == [], (rows, bad)
#: (б) подмена: объявлен v13 — прочитан v12 (ровно тот дефект, что чинил S3av)
rows, bad = m.identity_rows(DECL, got("/workspace/shared/datasets/sft_train_v12.jsonl",
                                      "aa" * 32, 44949,
                                      "/workspace/shared/datasets/tok/sft_train_v12_8192_qwen25.npz",
                                      "bb" * 32))
assert set(bad) >= {"jsonl_path", "jsonl_sha256", "jsonl_samples", "tensor_sha256"}, bad
#: (в) усечённый хеш: записан не целиком — отдельная находка на каждую сторону
rows, bad = m.identity_rows(DECL, got("/workspace/shared/datasets/sft_train_v13_fixed.jsonl",
                                      DECL["sft_jsonl_sha256"], 44105,
                                      "/workspace/shared/datasets/tok/sft_train_v13_fixed_8192_qwen25.npz",
                                      DECL["tok_cache_sha256"], scope="head"))
assert bad == ["jsonl_hash_scope", "tensor_hash_scope"], bad
PY
#: (г) стадия исполнена, а факта нет вовсе: ожидание с дедлайном, затем not_verified —
#: «вход не доказан и не опровергнут», а не молчаливое продолжение.
GATE_NB="$GATE_TMP/noblock"; mkdir -p "$GATE_NB/checkpoints" "$GATE_NB/logs" "$GATE_NB/tools"
cp "$GATE_TMP/run/full_sft_params.json" "$GATE_NB/"
cp "$GATE_TMP/run/early_gate.json" "$GATE_NB/"
printf '%s\n' '{"step": 517, "loss": 1.0}' > "$GATE_NB/logs/loss_trace.jsonl"
printf '%s' '{"stages": []}' > "$GATE_NB/checkpoints/run_manifest.json"
expect_exit 2 "манифест без блока sft_input → NOT-VERIFIED по дедлайну, не тишина" \
  timeout 60 env PATH="$GATE_TMP/bin:$PATH" FAKE_BIN_DIR="$GATE_TMP/bin" \
  python3 tools/early_gate_watch.py --run-dir "$GATE_NB" --wait-input-hours 0.002 \
  --wait-ckpt-hours 0.002 --poll-secs 1
expect_exit 0 "отказ входа назван в вердикте отдельным файлом" \
  python3 - "$GATE_NB/input_identity_verdict.json" <<'PY'
import json, sys
v = json.load(open(sys.argv[1], encoding="utf-8"))
assert v["verdict"] == "not_verified", v
assert "не появился" in v["why"] or "не доказан" in v["why"], v["why"]
PY

echo "  --- 34з. сторож не запускает пробу не тем прибором ---"
#: Хеш прибора берётся из ОБЪЯВЛЕНИЯ гейта: доставленный прибор с другим хешем —
#: это разность приборов, а не состояний. Проверяется на синтетике (без стенда):
#: подменённый файл прибора обязан дать отказ, а не замер.
expect_exit 0 "подменённый прибор назван отказом, а не замером" \
  python3 - "$GATE_TMP" <<'PY'
import importlib.util, json, pathlib, shutil, sys
tmp = pathlib.Path(sys.argv[1])
run = tmp / "sha-run"
shutil.rmtree(run, ignore_errors=True)
shutil.copytree(tmp / "run", run)
(run / "tools" / "probe_language_split.py").write_bytes("# not the instrument\n".encode())
spec = importlib.util.spec_from_file_location("egw", "tools/early_gate_watch.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
gate = json.loads((run / "early_gate.json").read_text(encoding="utf-8"))
gate["probe"]["tool_sha256"] = "99dafa8d" + "0" * 56
(run / "early_gate.json").write_text(json.dumps(gate), encoding="utf-8")

class A:
    tag = "t"; min_device_free_gb = 6.0
w = m.Watch(run, A())
assert w.probe(run / "checkpoints" / "sft_probe_500.pt") is None, "подменённый прибор принят"
phases = [e["phase"] for e in w.state["events"]]
assert "probe_tool_sha_mismatch" in phases, phases
PY
rm -rf "$GATE_TMP"

echo "== 43. C-014: фаза отчёта точки решения и полнота по схеме (S3be) =="
# Дефект, который закрывает секция. Правило C-014 требовало наличия отчёта
# `evidence/result-report.json`, а §5 протокола **запрещает** создавать его заранее:
# «иначе правило C-014 станет ложно зелёным». Значит до точки S4 правило было
# красным **по построению** — класс, который лечится фазой, а не ослаблением
# (ADR-023 п.12). У правила, знающего фазу, три состояния, и красное — только у двух:
#   (а) отчёта нет и точка решения НЕ пройдена → PENDING-EVIDENCE (warn, exit 0);
#   (б) отчёта нет, а точка ПРОЙДЕНА (RL-чекпойнт, журнал метрик стадии) → долг (error);
#   (в) отчёт есть → полнота по ОБЯЗАТЕЛЬНЫМ полям схемы (конъюнкция, exit 0/1).
# Плюс зубы: носитель состояния и конъюнкция проверяются подменой, а не словом.
# Фикстуры живут в $TMP и НЕ кладутся в `evidence/` кейса: заранее созданный отчёт
# сделал бы правило ложно зелёным (S4-PROTOCOL §5) — тот же запрет, что у человека.
RQ="$TMP/rule-c014"; rm -rf "$RQ"; mkdir -p "$RQ/evidence" "$RQ/docs/specs"
cp docs/specs/result-report.schema.json "$RQ/docs/specs/result-report.schema.json"

echo "  --- 43а. правило объявлено вызывающим прибор, смысл сохранён ---"
expect_exit 0 "C-014: тип command_succeeds, имя/severity на месте, смысл назван" \
  python3 - "$RQ" <<'PY'
import json, pathlib, re, sys
import yaml

case = pathlib.Path(".")
rule = next(r for r in yaml.safe_load(
    (case / "CONSTRAINTS.yaml").read_text(encoding="utf-8"))["constraints"]
    if r["id"] == "C-014")
# Номер, имя и severity — не менялись (ADR-046 п.8: номер не выдаётся заново).
assert rule["id"] == "C-014", rule
assert rule["severity"] == "high", rule
assert rule["name"].startswith("AD-1: отчёт результата несёт ось"), rule["name"]
# Правило стало ЖИВОЙ проверкой, а не чтением буквы в файле: тип и вид сменились
# вместе, и это следствие перевода на фазу, а не отдельное решение.
assert rule["type"] == "command_succeeds", rule
assert rule["kind"] == "behavioural", rule
assert rule["command"] == "python3 tools/check_result_report.py", rule["command"]
assert int(rule["timeout_secs"]) >= 30, rule
# Смысл сохранён: все семь прежних требований названы в evidence правила, но уже
# как обязательные поля схемы, а не как подстроки-дизъюнкция.
for token in ("axis.metric", "judge_mean", "std", "k_of_k", "n_attempts",
              "entropy", "clip_frac", "zero_reward_share", "hit_timeout"):
    assert token in rule["evidence"], token
# Носитель — прибор; прежней пары glob/pattern у правила больше нет: она читала
# наличие ОДНОГО слова, и отчёт с одним словом «axis» был для неё полным.
assert "glob" not in rule and "pattern" not in rule, sorted(rule)
assert (case / "tools" / "check_result_report.py").is_file()
# Каноническое имя берётся из ДВУХ источников и они обязаны совпасть: §5 протокола
# и описание схемы. Иначе неизвестно, какое имя считать каноническим.
proto = (case / "docs/specs/S4-PROTOCOL.md").read_text(encoding="utf-8")
names = set(re.findall(r"evidence/result-report[\w.-]*\.json", proto))
assert names == {"evidence/result-report.json"}, names
schema = json.loads((case / "docs/specs/result-report.schema.json").read_text(encoding="utf-8"))
assert "evidence/result-report.json" in json.dumps(schema, ensure_ascii=False), \
    "схема не называет файл"
print(f"C-014: {rule['type']} → {rule['command']}; каноническое имя = {sorted(names)[0]}")
PY

echo "  --- 43б. четыре состояния прибора: фаза / долг / неполный / полный ---"
expect_exit 0 "нет отчёта и точка не пройдена → PENDING-EVIDENCE, гейт НЕ краснеет" \
  python3 - "$RQ" <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
# Состояние читается из МАШИННОГО вердикта (`--json`), а не из подстроки в тексте:
# имя состояния — часть контракта прибора, и проверять его надо там, где оно
# обещано, иначе тест ловит формулировку, а не поведение.
r = subprocess.run([sys.executable, "tools/check_result_report.py", "--root", str(root),
                    "--json"], capture_output=True, text=True)
assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
v = json.loads(r.stdout)
assert v["state"] == "PENDING-EVIDENCE" and v["severity"] == "warn", v
assert v["rc"] == 0 and v["finding_severity"] is None, v
# Причина названа, а не «всё хорошо»: молчаливый зелёный здесь неотличим от
# проверенного зелёного — тот же класс, что ложное красное.
assert "точка решения не пройдена" in v["message"] and "заранее" in v["message"], v
print(f"{v['state']} / {v['severity']}: {v['message'][:80]}")
PY
expect_exit 1 "режим --mode pending: предмет режима — сам PENDING (красное здесь его)" \
  python3 tools/check_result_report.py --root "$RQ" --mode pending
expect_exit 0 "PENDING — это фаза, а не долг: признак разведки в КОРНЕ прогона не считается" \
  python3 - "$RQ" <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
# Проба цены шага (tools/run_rl_probe.py) пишет rl_metrics.json в КОРЕНЬ каталога
# прогона — это другой предмет (ADR-010 п.3: «сходимость не критерий»), и точку
# решения S4 такая проба не проходит. Иначе runs/rl-probe-20260914-0956 держал бы
# правило красным по построению — ровно тот класс, который правило и чинит.
d = root / "runs" / "rl-probe-20260914-0956"; d.mkdir(parents=True, exist_ok=True)
(d / "rl_metrics.json").write_text("{}", encoding="utf-8")
r = subprocess.run([sys.executable, "tools/check_result_report.py", "--root", str(root),
                    "--json"], capture_output=True, text=True)
v = json.loads(r.stdout)
assert r.returncode == 0 and v["state"] == "PENDING-EVIDENCE", v
PY
expect_exit 0 "отчёта нет, а точка ПРОЙДЕНА (журнал метрик стадии) → долг, красное" \
  python3 - "$RQ" <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
d = root / "runs" / "pilot-compact-s42-20260922-0000" / "logs"
d.mkdir(parents=True, exist_ok=True)
# Признак живёт в `logs/`, а не в корне: там его пишет САМА стадия
# (`Path(args.log_dir)/"rl_metrics.json"`), а не проба цены.
(d / "rl_metrics.json").write_text("{}", encoding="utf-8")
r = subprocess.run([sys.executable, "tools/check_result_report.py", "--root", str(root),
                    "--json"], capture_output=True, text=True)
v = json.loads(r.stdout)
assert r.returncode == 1 and v["state"] == "MISSING-REPORT", v
assert v["severity"] == "error", v
assert "долг" in v["message"], v
# Признак назван поимённо, а не «что-то найдено»: без имени файла читатель не
# отличит долг от «нашлось не то».
assert any("rl_metrics.json" in s for s in v["signs"]), v
print(f"{v['state']}: {v['signs'][0][:90]}")
PY
expect_exit 0 "признак точки — RL-чекпойнт стадии (вход замера §6.1) → красное" \
  python3 - "$RQ" <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
p = root / "runs" / "pilot-compact-s42-20260922-0000" / "checkpoints"
p.mkdir(parents=True, exist_ok=True)
(p / "rl_checkpoint_final.pt").write_bytes(b"pt")
r = subprocess.run([sys.executable, "tools/check_result_report.py", "--root", str(root),
                    "--json"], capture_output=True, text=True)
v = json.loads(r.stdout)
assert r.returncode == 1 and v["state"] == "MISSING-REPORT", v
assert any("rl_checkpoint_final.pt" in s for s in v["signs"]), v
PY
expect_exit 0 "отчёт по ОБЯЗАТЕЛЬНЫМ полям схемы → зелёное (сборка, не рукопись)" \
  python3 - "$RQ" <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
schema = json.loads(pathlib.Path("docs/specs/result-report.schema.json").read_text(encoding="utf-8"))


def build(sch, defs):
    """Минимальный объект, удовлетворяющий схеме: обязательные поля — рекурсивно."""
    if "$ref" in sch:
        return build(defs[sch["$ref"].split("/")[-1]], defs)
    if sch.get("type") == "object" or "properties" in sch:
        return {name: build(sch["properties"][name], defs)
                for name in sch.get("required", [])}
    t = sch.get("type")
    if isinstance(t, list):                       # ["number","null"] → берём первый
        t = t[0]
    if "const" in sch:
        return sch["const"]
    if "enum" in sch:
        return sch["enum"][0]
    return {"number": 0.0, "integer": 1, "boolean": False,
            "string": "x", "array": []}.get(t, None)


report = build(schema, schema.get("$defs", {}))
# Схему проверяем своим прибором, а не глазами: фикстура обязана ей соответствовать.
assert set(schema["required"]) <= set(report), set(schema["required"]) - set(report)
(root / "evidence" / "result-report.json").write_text(
    json.dumps(report, ensure_ascii=False), encoding="utf-8")
r = subprocess.run([sys.executable, "tools/check_result_report.py", "--root", str(root),
                    "--json"], capture_output=True, text=True)
v = json.loads(r.stdout)
assert r.returncode == 0 and v["state"] == "COMPLETE", v
# Схема-носитель списка полей: их число названо в вердикте, и оно > 9 обязательных
# верхнего уровня — вложенные уровни считаются тоже (иначе «рекурсивно» — надпись).
assert "обязательные поля схемы" in v["message"], v
print(f"{v['state']}: {v['message'][:100]}")
PY

echo "  --- 43в. зубы: подмена носителя и обязательного поля роняет тест ---"
expect_exit 0 "отчёт есть, но ОДНОГО обязательного поля не хватает → красное поимённо" \
  python3 - "$RQ" <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
p = root / "evidence" / "result-report.json"
report = json.loads(p.read_text(encoding="utf-8"))
# Снимается ВЛОЖЕННОЕ обязательное поле: прежняя дизъюнкция подстрок считала такой
# отчёт полным (слово «clip_frac» рядом осталось), конъюнкция — нет.
del report["rl_health"]["zero_reward_share"]
p.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
r = subprocess.run([sys.executable, "tools/check_result_report.py", "--root", str(root),
                    "--json"], capture_output=True, text=True)
v = json.loads(r.stdout)
assert r.returncode == 1 and v["state"] == "INCOMPLETE", v
assert v["missing"] == ["rl_health.zero_reward_share"], v["missing"]
print(f"{v['state']}: не выведено {v['missing']}")
PY
expect_exit 0 "зуб: заглушка с одним словом «axis» полным отчётом НЕ считается" \
  python3 - "$RQ" <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
# Тот самый отчёт, который прежнее правило (pattern-дизъюнкция) пропускало: одно
# слово «axis» и «std» в теле — и всё. Дизъюнкция зеленела; конъюнкция обязана
# краснеть, иначе смысл правила не сохранён, а подменён.
(root / "evidence" / "result-report.json").write_text(
    json.dumps({"axis": "axis", "limits": ["std", "entropy", "clip_frac"]},
               ensure_ascii=False), encoding="utf-8")
r = subprocess.run([sys.executable, "tools/check_result_report.py", "--root", str(root),
                    "--json"], capture_output=True, text=True)
v = json.loads(r.stdout)
assert r.returncode == 1 and v["state"] == "INCOMPLETE", v
# Список полей берётся из схемы, а не из списка в коде: в находках — вложенные
# уровни (`axis.metric`, `rl_health.entropy`), которых в заглушке нет и близко.
assert "axis.metric" in v["missing"] and "rl_health.entropy" in v["missing"], v["missing"]
print(f"{v['state']}: не выведено полей {len(v['missing'])}")
PY
expect_exit 0 "зуб на носитель списка: поле, снятое из СХЕМЫ, из вердикта уходит" \
  python3 - "$RQ" <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
sp = root / "docs" / "specs" / "result-report.schema.json"
schema = json.loads(sp.read_text(encoding="utf-8"))
# Подмена носителя: список обязательных полей — это СХЕМА, и её правка обязана
# двигать вердикт. Здесь требование `rl_health` снято — и отчёт-заглушка перестаёт
# краснеть на нём (остальные уровни всё ещё не выведены, поэтому проверяется не
# «зелёное», а ИСЧЕЗНОВЕНИЕ поля из списка находок).
schema["required"] = [x for x in schema["required"] if x != "rl_health"]
schema["properties"].pop("rl_health")
sp.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
r = subprocess.run([sys.executable, "tools/check_result_report.py", "--root", str(root),
                    "--json"], capture_output=True, text=True)
v = json.loads(r.stdout)
assert not any(m.startswith("rl_health") for m in v["missing"]), v["missing"]
assert "axis.metric" in v["missing"], "остальные требования схемы обязаны остаться"
# Схема фикстуры возвращается в исходное состояние: подменённый носитель был
# предметом ЭТОЙ проверки, а не нового состояния стенда.
sp.write_bytes(pathlib.Path("docs/specs/result-report.schema.json").read_bytes())
PY
expect_exit 2 "нет входа (схема не прочитана) → NOT-VERIFIED, а не «зелёное»" \
  python3 tools/check_result_report.py --root "$TMP/s3be-no-such-root"

echo "  --- 43г. связка с движком: вердикт правила — код возврата прибора ---"
# Фикстура-правило объявляет ту же команду, что канон, но с явным `--root`: движок
# запускает команду из своего рабочего каталога, и без `--root` вердикт относился бы
# не к фикстуре. Проверяются ОБЕ стороны — зелёный PENDING и красный долг.
if command -v arch-ml >/dev/null 2>&1; then
  expect_exit 0 "движок: PENDING (фаза) → правило не красит (passed)" \
    python3 - "$RQ" <<'PY'
import json, pathlib, subprocess, sys
import yaml

root = pathlib.Path(sys.argv[1])
# Вернуть каталог в состояние фазы: отчёта нет, точка не пройдена.
(root / "evidence" / "result-report.json").unlink(missing_ok=True)
import shutil
shutil.rmtree(root / "runs", ignore_errors=True)
#: Путь к прибору — АБСОЛЮТНЫЙ: движок запускает команду правила из того каталога,
#: который ему назван (`path`), а это фикстура, а не дерево кейса. Относительный
#: `tools/…` там не найдётся, и «красное» относилось бы к отсутствию файла, а не к
#: предмету правила. В каноне `command` относителен намеренно — гейт зовётся из
#: корня кейса (проверено: `fitness_check` по CONSTRAINTS.yaml).
tool = pathlib.Path("tools/check_result_report.py").resolve()
rule = {"id": "C-014", "name": "fixture", "type": "command_succeeds",
        "command": f"python3 {tool} --root {root}",
        "timeout_secs": 60, "severity": "high", "scope": "case",
        "kind": "behavioural"}
(root / "CONSTRAINTS.yaml").write_text(
    yaml.safe_dump({"constraints": [rule]}, allow_unicode=True), encoding="utf-8")
r = subprocess.run(["arch-ml", "control", "check", str(root), "--constraints",
                    str(root / "CONSTRAINTS.yaml"), "--json"],
                   capture_output=True, text=True)
d = json.loads(r.stdout)
assert d["passed"] is True, d["issues"]
print(f"правил: 1, находок: {len(d['issues'])}")
PY
  expect_exit 0 "движок: долг (точка пройдена, отчёта нет) → правило КРАСИТ поимённо" \
    python3 - "$RQ" <<'PY'
import json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
d = root / "runs" / "pilot-compact-s42-20260922-0000" / "logs"
d.mkdir(parents=True, exist_ok=True)
(d / "rl_metrics.json").write_text("{}", encoding="utf-8")
r = subprocess.run(["arch-ml", "control", "check", str(root), "--constraints",
                    str(root / "CONSTRAINTS.yaml"), "--json"],
                   capture_output=True, text=True)
doc = json.loads(r.stdout)
assert doc["passed"] is False, "долг принят правилом"
msg = " ".join(i["message"] for i in doc["issues"])
assert "ПРОЙДЕНА" in msg, msg
print(msg[:120])
PY
else
  SKIP=$((SKIP + 1)); printf '  SKIP %-58s (%s)\n' \
    "C-014 в движке fitness" "нет arch-ml: правило проверено только по объявлению"
fi
echo "== 44. S3bg: арифметика оси, карточки AD-2, инвентаризация весов (read-only) =="
# Три прибора дельты S3bg. Стенд НЕ занимается: арифметика оси — чистый CPU,
# инвентаризация проверяется на СИНТЕТИЧЕСКОМ сторе (реальные каталоги стенда этот
# прогон не читает), карточки — на синтетическом отчёте инвентаризации. У каждого
# прибора проверяются ОБЕ стороны, и проверка идёт через expect_exit: проваленное
# утверждение обязано стать FAIL, а не строкой traceback в логе. Предметы:
# у арифметики — что числа не подгоняются под удобный вывод (SE разности ≠ SE руки,
# «2 из 3 по знаку» = 50 %, а не «критерий»); у инвентаризации — что «файл есть, а
# весов нет» ловится (LFS-указатель и `*.incomplete` — ровно тот класс, из-за
# которого §1.2 плана видел «только метаданные»); у карточек — что отсутствие
# записано отсутствием, а не придуманным путём, и что невалидный файл базой не стал.

echo "  --- 44а. арифметика оси: числа и их границы (Н-6) ---"
AX="$TMP/s3bg-axis.json"
expect_exit 0 "арифметика оси считается (CPU, нулевая цена)" \
  python3 tools/ladder_axis_arithmetic.py --json "$AX"
expect_exit 0 "числа оси: SE разности ≠ SE руки, пороги и нехватка задач" \
  python3 - "$AX" <<'PYS3BG'
import json, sys
d = json.load(open(sys.argv[1]))
rows = {(r["m"], r["n"]): r for r in d["se_table"]}
assert len(d["se_table"]) == 12, len(d["se_table"])
r = rows[(192, 1)]
# SE РАЗНОСТИ при 192 задачах и одной попытке — 5,10 %, а не 3,61 % (это SE руки):
# множитель sqrt(2) объявлен явно, иначе порог §6 плана занижен.
assert abs(r["se_arm_pct"] - 3.608) < 0.01, r
assert abs(r["se_diff_pct"] - 5.103) < 0.01, r
assert abs(r["mdd_95_pct"] - 10.002) < 0.01, r
assert abs(rows[(400, 3)]["mdd_95_bonf3_pct"] - 4.887) < 0.01, rows[(400, 3)]
# Требуемое m под эффект 5 п.п.: при пороге 1 SE пула хватает, при 95 %+Бонф. — нет.
req = {b["effect_pct"]: {x["n"]: x for x in b["rows"]} for b in d["required_m"]}
assert req[5.0][3]["m_1se"] == 67 and req[5.0][3]["m_95_bonf3"] == 383, req[5.0][3]
assert req[5.0][1]["m_95"] == 769, req[5.0][1]
assert d["available"]["task_set"]["tasks"] == 192, d["available"]["task_set"]
short = {x["n"]: x for x in d["shortfall"]["for_effect_5pct_95bonf3"]}
assert short[3]["shortfall_tasks"] == 191, short
assert short[1]["shortfall_tasks"] == 955, short
# Порог 1 SE слабый — это число, а не мнение; «2 из 3 по знаку» при нуле = 50 %.
assert d["model"]["thresholds"]["1se"]["false_positive_rate_two_sided_pct"] == 31.7
assert d["reproduction_criterion"]["under_null_pct"] == 50.0
assert d["reproduction_criterion"]["alternative_per_family_95"]["k2_of_3_pct"] == 0.725
print("   числа: SE разности 5,10 %; m=383 при n=3; нехватка 191 задача; "
      "2 из 3 по знаку = 50 %")
PYS3BG

echo "  --- 44б. инвентаризация весов: синтетический стор, LFS-указатель и *.incomplete ---"
SHOP="$TMP/s3bg-shop"
expect_exit 0 "фикстура: стор из четырёх классов дефектов" \
  python3 - "$SHOP" <<'PYS3BG'
import json, os, struct, sys
shop = sys.argv[1]

def put(model, name, data: bytes):
    snap = os.path.join(shop, model, "snapshots", "rev0001")
    os.makedirs(snap, exist_ok=True)
    with open(os.path.join(snap, name), "wb") as f:
        f.write(data)

def safetensors():
    meta = {"t": {"dtype": "BF16", "shape": [2, 2], "data_offsets": [0, 8]}}
    blob = json.dumps(meta).encode()
    blob += b" " * ((8 - len(blob) % 8) % 8)
    return struct.pack("<Q", len(blob)) + blob + b"\x00" * 8

# 1) настоящие веса (заголовок safetensors) + конфиг + токенизатор
put("models--Qwen--Qwen2.5-0.5B", "model.safetensors", safetensors())
put("models--Qwen--Qwen2.5-0.5B", "config.json",
    json.dumps({"model_type": "qwen2", "vocab_size": 151936}).encode())
put("models--Qwen--Qwen2.5-0.5B", "tokenizer.json",
    json.dumps({"model": {"type": "BPE", "vocab": {"a": 0}},
                "added_tokens": [{"content": "<tool_call>", "id": 1},
                                 {"content": "</tool_call>", "id": 2}]}).encode())
# 2) только метаданные: файлов весов нет вовсе
put("models--Qwen--Qwen3-8B", "config.json", b'{"model_type": "qwen3"}')
# 3) класс дефекта §1.2 плана: файл весов есть, но он .incomplete (нулевой)
put("models--Qwen--Qwen3-4B", "model-00001-of-00002.safetensors.incomplete", b"")
put("models--Qwen--Qwen3-4B", "config.json", b'{"model_type": "qwen3"}')
# 4) красный путь: файл НАЗВАН весами, а внутри мусор (LFS-указатель)
put("models--Qwen--Qwen3.5-0.8B-Base", "model.safetensors-00001-of-00001.safetensors",
    b"version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef\nsize 1746942600\n")
put("models--Qwen--Qwen3.5-0.8B-Base", "config.json",
    json.dumps({"model_type": "qwen3_5"}).encode())
PYS3BG
expect_exit 0 "инвентаризация синтетического стора (12 позиций, без стенда)" \
  python3 tools/inventory_stand_weights.py --roots "$SHOP" --json "$TMP/s3bg-inv.json"
expect_exit 0 "веса, метаданные и LFS-указатель различены; мусор за веса не выдан" \
  python3 - "$TMP/s3bg-inv.json" <<'PYS3BG'
import json, sys
d = json.load(open(sys.argv[1]))
by = {p["short"]: p for p in d["positions"]}
# «Есть» — только там, где файл действительно safetensors: 1 из 12
assert d["summary"]["of_12"] == 1, d["summary"]
assert d["summary"]["list_present"] == ["qwen25-05b"], d["summary"]
# у «только метаданные» два разных лица: конфиг без весов и веса-не-safetensors;
# «нет» — это когда файлов нет вовсе
assert "qwen3-8b" in d["summary"]["list_metadata_only"], d["summary"]
assert set(d["summary"]["list_absent"]) >= {"qwen25-3b", "qwen35-9b"}, d["summary"]
# Файл с именем весов и мусором внутри НЕ посчитан весами — назван отдельно
assert d["summary"]["list_weights_but_not_safetensors"] == ["qwen35-08b"], d["summary"]
bad = by["qwen35-08b"]["matches"][0]
assert bad["integrity"]["safetensors_headers_valid"] is False, bad["integrity"]
assert bad["shards_bad"], bad
# «только метаданные» ловит ровно класс дефекта §1.2 — incomplete без весов
assert by["qwen3-4b"]["verdict"] == "только метаданные", by["qwen3-4b"]["verdict"]
ok = by["qwen25-05b"]["matches"][0]
assert ok["integrity"]["safetensors_headers_valid"] is True
assert ok["integrity"]["no_incomplete_files"] is True
assert ok["config"]["model_type"] == "qwen2"
t = ok["tokenizer_facts"]
# Единый id у <tool_call> признан, а <think> назван тем, что добавляет пайплайн
assert t["special_v12_native_single_id"] == ["</tool_call>", "<tool_call>"], t
assert "<think>" in t["special_v12_added_by_pipeline"], t
print("   синтетика: 1/12 весов, «только метаданные»: %s, не-safetensors: %s"
      % (d["summary"]["list_metadata_only"],
         d["summary"]["list_weights_but_not_safetensors"]))
PYS3BG

echo "  --- 44в. карточки AD-2: отсутствие кэша — факт, а не придуманный путь ---"
CARDS="$TMP/s3bg-cards"
expect_exit 0 "карточки собираются на синтетическом отчёте инвентаризации" \
  python3 tools/build_ladder_cards.py --inventory "$TMP/s3bg-inv.json" \
    --out-dir "$CARDS" --tok-dir "$TMP/s3bg-tok"
expect_exit 0 "карточка: база — только валидные веса; отсутствие названо отсутствием" \
  python3 - "$CARDS" <<'PYS3BG'
import json, os, sys
d = sys.argv[1]
cards = {f: json.load(open(os.path.join(d, f))) for f in os.listdir(d)}
assert set(cards) == {"ad2-qwen25.json", "ad2-qwen3.json", "ad2-qwen35.json"}, set(cards)
q3 = cards["ad2-qwen3.json"]
pos = {b["short"]: b for b in q3["bases"]}
# Весов в синтетическом сторе нет — путь не придумывается, отсутствие названо
assert pos["qwen3-06b"]["verdict"] == "нет", pos["qwen3-06b"]
assert "path" not in pos["qwen3-06b"], "путь не придумывается"
assert "весов нет" in pos["qwen3-06b"]["note"], pos["qwen3-06b"]
# Мусорный файл базой НЕ стал: ни пути, ни sha256 в карточке нет
q35 = {b["short"]: b for b in cards["ad2-qwen35.json"]["bases"]}
assert "path" not in q35["qwen35-08b"], q35["qwen35-08b"]
assert "weights_sha256" not in q35["qwen35-08b"], q35["qwen35-08b"]
# Токенизатор: id, контракт AD-3 и то, что добавляет пайплайн
assert q3["tokenizer"]["hf_id"] == "Qwen/Qwen3-0.6B"
assert "AD-3" in q3["tokenizer"]["contract"]
# Кэшей нет — это записано отсутствием, а не «предполагается»
assert q3["tokens_cache"]["present"] == [], q3["tokens_cache"]
assert {m["stage"] for m in q3["tokens_cache"]["missing"]} == {"cpt", "sft"}, q3["tokens_cache"]
assert "не собран" in q3["tokens_cache"]["missing"][0]["state"]
q25 = cards["ad2-qwen25.json"]
assert q25["sets"]["sft"]["examples"] == 44105, q25["sets"]["sft"]
assert len(q25["sets"]["sft"]["sha256_full"]) == 64
print("   карточки: 3 файла; отсутствие кэшей записано фактом («не собран»)")
PYS3BG
# Положительный путь записи кэша: файл есть → карточка несёт его sha256 (--hash-caches)
mkdir -p "$TMP/s3bg-tok2"
printf 'synthetic-cache' > "$TMP/s3bg-tok2/cpt_corpus_v12r_8192_qwen3.npy"
printf 'synthetic-cache' > "$TMP/s3bg-tok2/sft_train_v13_fixed_8192_qwen3.npz"
expect_exit 0 "карточка фиксирует РЕАЛЬНО прочитанный кэш (sha256 снят прибором)" \
  python3 tools/build_ladder_cards.py --inventory "$TMP/s3bg-inv.json" \
    --out-dir "$CARDS" --tok-dir "$TMP/s3bg-tok2" --hash-caches
expect_exit 0 "кэш: sha256 в карточке совпадает с содержимым файла" \
  python3 - "$CARDS" <<'PYS3BG'
import hashlib, json, os, sys
q3 = json.load(open(os.path.join(sys.argv[1], "ad2-qwen3.json")))
assert len(q3["tokens_cache"]["present"]) == 2, q3["tokens_cache"]
want = hashlib.sha256(b"synthetic-cache").hexdigest()
got = {c["sha256"] for c in q3["tokens_cache"]["present"]}
assert got == {want}, got
assert q3["tokens_cache"]["missing"] == [], q3["tokens_cache"]
print("   кэш: sha256 =", want[:16], "… (совпал с содержимым файла)")
PYS3BG

echo "  --- 44г. перевыпуск носителя оси: счёт рук, архив и неподвижность порога (S3bk) ---"
# Предмет: носитель оси перевыпущен под счёт «две руки на модель» (Решение 3 дельты
# S3bk). Проверяются ТРИ стороны, и все три — механически, а не глазами:
#   (1) счёт рук назван ЧИСЛОМ (arms_per_model, wave_arms, В-1 = 6 рук × 12 ч = 72 ч),
#       а не примечанием — прежняя редакция считала руки «по семейству» и давала 36 ч;
#   (2) прежняя редакция НЕ переписана: sha256 архива совпадает с объявленным в
#       supersedes (ADR-023 п.9 — архив вместо перезаписи);
#   (3) порог и модель дисперсии перевыпуск НЕ затронул: все ОБЩИЕ числовые поля
#       архива и нового носителя совпадают, а se_table/model/required_m/shortfall/
#       reproduction_criterion — покомпонентно равны.
# Негативная сторона обязательна (иначе проверка не проверена): подмена счёта рук,
# подмена объявленного хеша архива и сдвиг порога — каждая обязана краснеть.
CHK="$TMP/s3bk-carrier-check.py"
cat > "$CHK" <<'PYS3BK'
import hashlib, json, sys
ARCH, NEW = sys.argv[1], sys.argv[2]
a = json.load(open(ARCH)); b = json.load(open(NEW))
ec = b["eval_cost"]
assert ec["arms_per_model"] == 2, f"arms_per_model={ec['arms_per_model']}"
assert "две на модель" in ec["arms_rule"], ec["arms_rule"]
arms = {w["wave"]: w["arms"] for w in ec["wave_arms"]}
assert arms == {"В-1": 6, "В-2": 4, "В-3": 2, "В-5": 10, "В-4": 4}, arms
wc = ec["wave_eval_cost_calibration_model"]
assert (wc["arms"], wc["hours_per_arm"], wc["hours"], wc["days"]) == (6, 12.0, 72.0, 3.0), wc
h = hashlib.sha256(open(ARCH, "rb").read()).hexdigest()
assert b["supersedes"]["sha256"] == h, f"supersedes.sha256 разошёлся с архивом: {h}"
assert b["supersedes"]["file"] == ARCH, b["supersedes"]["file"]


def nums(o, p=""):
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            out.update(nums(v, f"{p}.{k}" if p else k))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            out.update(nums(v, f"{p}[{i}]"))
    elif isinstance(o, (int, float)) and not isinstance(o, bool):
        out[p] = o
    return out


na, nb = nums(a), nums(b)
shared = set(na) & set(nb)
diff = {k: (na[k], nb[k]) for k in shared if na[k] != nb[k]}
assert not diff, f"числовые расхождения с архивом: {diff}"
assert len(shared) >= 190, f"общих числовых полей всего {len(shared)}"
assert a["se_table"] == b["se_table"], "таблица SE изменилась"
assert a["model"] == b["model"], "модель дисперсии изменилась"
assert a["required_m"] == b["required_m"] and a["shortfall"] == b["shortfall"]
assert a["reproduction_criterion"] == b["reproduction_criterion"]
print(f"   счёт рук: {arms} | В-1 = {wc['hours']} ч = {wc['days']} сут | архив {h[:16]}… | "
      f"общих числовых полей {len(shared)}, расхождений 0 | порог и модель дисперсии совпали")
PYS3BK
MUT="$TMP/s3bk-carrier-mutate.py"
cat > "$MUT" <<'PYS3BK'
import json, sys
kind, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
b = json.load(open(src))
if kind == "arms":
    b["eval_cost"]["arms_per_model"] = 1
    b["eval_cost"]["wave_arms"][0]["arms"] = 3      # возврат к счёту «по семейству»
elif kind == "sha":
    b["supersedes"]["sha256"] = "0" * 64            # объявление архива разошлось
elif kind == "threshold":
    b["se_table"][2]["mdd_95_bonf3_pct"] += 0.5     # сдвиг порога при перевыпуске
json.dump(b, open(dst, "w"), ensure_ascii=False)
PYS3BK
ARCH="evidence/ladder-axis-arithmetic-2026-09-22.json"
NEW="evidence/ladder-axis-arithmetic.json"
expect_exit 0 "носитель оси: счёт рук, архив по sha256, порог не двинулся" \
  python3 "$CHK" "$ARCH" "$NEW"
for mut in arms sha threshold; do
  expect_exit 1 "носитель оси: мутация «$mut» краснеет, а не проходит" \
    bash -c "python3 '$MUT' '$mut' '$NEW' '$TMP/s3bk-mut-$mut.json' && python3 '$CHK' '$ARCH' '$TMP/s3bk-mut-$mut.json'"
done

echo "== 45. S3bh: ворота поддержки qwen3_5 — три состояния и отвязка от чужой лесенки =="
# Предмет гейта — один вопрос: берёт ли стек архитектуру `model_type qwen3_5`.
# Стенд НЕ занимается: проба подменяется (`--probe-cmd`), сенсор занятости —
# (`--busy-cmd`), транспорт — `--local`. Проверяются ОБЕ стороны каждого
# утверждения, потому что дефект прежнего гейта был именно в отсутствии
# негативной стороны: он не падал ни на одном сценарии, он вообще не выполнялся
# (14 циклов ожидания чужой лесенки, ни одной строки пробы).
#
# Предметы: (а) три состояния различаются по НОСИТЕЛЮ, а не по догадке, и
# «пробы не было» — это «не проверено», а не «не поддержано»; (б) гейт отвязан
# от чужого события — в исполняемом коде нет ни ожидания чужого маркера, ни
# `sleep`, ни автозапуска лесенки, а режим `status` не трогает стенд вовсе;
# (в) занятость — единственное, что может отказать `run`, и отказ честный:
# занят / не измерено / названный `--ignore-busy`; проба ограничена по времени.

echo "  --- 45а. три состояния: not-run ≠ unsupported ---"
G1="$TMP/s3bh-gate1"; G2="$TMP/s3bh-gate2"; G3="$TMP/s3bh-gate3"
mkdir -p "$G1" "$G2" "$G3"
LEGACY="$TMP/s3bh-gate-legacy.log"
for _ in $(seq 1 14); do
  echo "[gate] Веса скачаны. Жду финиша лесенки Qwen3 (V4 LADDER QWEN3 COMPLETE)..."
done > "$LEGACY"

expect_exit 2 "пробы не было → not-run (вердикта нет), а не «не поддержано»" \
  bash tools/qwen35_gate.sh status --state-dir "$G1" --legacy-log "$LEGACY"
expect_contains "not-run" "состояние названо словом not-run" \
  bash tools/qwen35_gate.sh status --state-dir "$G1" --legacy-log "$LEGACY"
expect_contains "14 цикл" "причина «вердикта нет» названа фактом журнала прежнего гейта" \
  bash tools/qwen35_gate.sh status --state-dir "$G1" --legacy-log "$LEGACY"

expect_exit 0 "проба выполнена и поддержана (HF_LOAD_OK + VLLM_LOAD_OK)" \
  bash tools/qwen35_gate.sh run --local --state-dir "$G1" \
    --busy-cmd 'printf "тренировочных нагрузок: 0\n"' \
    --probe-cmd 'echo HF_LOAD_OK; echo VLLM_LOAD_OK'
expect_exit 0 "состояние supported читается с носителя, а не выводится из наличия файла" \
  bash tools/qwen35_gate.sh status --state-dir "$G1"
expect_exit 0 "носитель вердикта: поля пробы и sha256 журнала, снятый прибором" \
  python3 - "$G1/qwen35-loadtest.json" "$G1/qwen35-loadtest.log" <<'PYS3BH'
import hashlib, json, sys
d = json.load(open(sys.argv[1]))
assert d["verdict"] == "supported", d
assert d["model"] == "Qwen/Qwen3.5-0.8B-Base", d
assert d["probe"]["hf_load_ok"] is True and d["probe"]["vllm_load_ok"] is True, d["probe"]
assert d["probe"]["timed_out"] is False, d["probe"]
want = hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest()
assert d["carrier"]["log_sha256"] == want, (d["carrier"], want)
assert d["carrier"]["log_bytes"] == len(open(sys.argv[2], "rb").read())
print("   supported: обе строки на месте, sha256 журнала =", want[:16], "…")
PYS3BH

expect_exit 1 "половина ответа вердиктом не считается (HF есть, vLLM нет) → unsupported" \
  bash tools/qwen35_gate.sh run --local --state-dir "$G2" \
    --busy-cmd 'printf "тренировочных нагрузок: 0\n"' \
    --probe-cmd 'echo HF_LOAD_OK; echo "ImportError: qwen3_5"; exit 1'
expect_exit 1 "состояние unsupported читается с носителя" \
  bash tools/qwen35_gate.sh status --state-dir "$G2"
expect_exit 0 "unsupported называет, что именно не загрузилось" \
  python3 - "$G2/qwen35-loadtest.json" <<'PYS3BH'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["verdict"] == "unsupported", d
assert d["probe"]["hf_load_ok"] is True, d["probe"]
assert d["probe"]["vllm_load_ok"] is False, d["probe"]
print("   unsupported: hf_load_ok=True, vllm_load_ok=False (причина названа полем)")
PYS3BH

echo "  --- 45б. отвязка от чужой лесенки: в исполняемом коде её нет ---"
expect_exit 0 "чужой маркер остался только текстом сообщения, а не условием ожидания" \
  bash -c 'test "$(grep -vE "^\s*#" tools/qwen35_gate.sh | grep -v echo | grep -c "V4 LADDER QWEN3 COMPLETE")" = 0'
expect_exit 0 "журнал чужой лесенки не читается: ни условием, ни источником" \
  bash -c 'test "$(grep -vE "^\s*#" tools/qwen35_gate.sh | grep -v echo | grep -c "v4_ladder_qwen3_status")" = 0'
expect_exit 0 "ожидания нет: ни одного sleep в исполняемом коде" \
  bash -c 'test "$(grep -vE "^\s*#" tools/qwen35_gate.sh | grep -cE "(^|[^a-zA-Z_])sleep ")" = 0'
expect_exit 0 "автозапуска лесенки нет: гейт даёт вердикт, а не нагрузку" \
  bash -c 'test "$(grep -vE "^\s*#" tools/qwen35_gate.sh | grep -c "run_v4_ladder")" = 0'
# Поведенческий зуб: режим по умолчанию стенд НЕ трогает. Подставные docker/ssh/
# nvidia-smi пишут след и валятся — если гейт их позовёт, след появится.
FAKE="$TMP/s3bh-gatebin"; mkdir -p "$FAKE"
for c in docker ssh nvidia-smi; do
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$0" >> "%s/touched"\nexit 1\n' "$TMP" > "$FAKE/$c"
  chmod +x "$FAKE/$c"
done
expect_exit 0 "status не зовёт ни docker, ни ssh, ни nvidia-smi (стенд не занимается)" \
  bash -c "PATH=\"$FAKE:\$PATH\" bash tools/qwen35_gate.sh status --state-dir '$G1'"
expect_exit 0 "status стенда не касался: следов вызовов нет" \
  bash -c "test ! -e '$TMP/touched'"
expect_exit 0 "сенсор занятости — страж сериализации (один носитель понятия «нагрузка»)" \
  bash -c 'grep -vE "^\s*#" tools/qwen35_gate.sh | grep -q check_gb10_serialization'
expect_exit 1 "зависшая проба снимается по таймауту: стенд не удерживается" \
  bash tools/qwen35_gate.sh run --local --state-dir "$G3" \
    --busy-cmd 'printf "тренировочных нагрузок: 0\n"' \
    --probe-cmd 'sleep 30' --probe-timeout 1
expect_exit 0 "таймаут назван в вердикте полем, а не умолчанием" \
  python3 - "$G3/qwen35-loadtest.json" <<'PYS3BH'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["verdict"] == "unsupported", d
assert d["probe"]["timed_out"] is True, d["probe"]
assert d["probe"]["timeout_s"] == 1, d["probe"]
assert d["probe"]["hf_load_ok"] is False and d["probe"]["vllm_load_ok"] is False, d["probe"]
print("   зависшая проба: rc=124, timed_out=True (предел назван в вердикте)")
PYS3BH

echo "  --- 45в. занятость стенда: занят / не измерено / названный обход ---"
expect_exit 2 "стенд занят → проба не запускается (AD-5/AD-9)" \
  bash tools/qwen35_gate.sh run --local --state-dir "$G3" \
    --busy-cmd 'printf "тренировочных нагрузок: 1\n"' \
    --probe-cmd 'echo HF_LOAD_OK; echo VLLM_LOAD_OK'
expect_exit 2 "занятость не измерена → отказ закрыто, а не «наверное, свободно»" \
  bash tools/qwen35_gate.sh run --local --state-dir "$G3" \
    --busy-cmd 'echo NOT-VERIFIED; exit 2' \
    --probe-cmd 'echo HF_LOAD_OK; echo VLLM_LOAD_OK'
expect_exit 0 "отказ по занятости не создаёт вердикта: пробы не было — вердикта нет" \
  python3 - "$G3/qwen35-loadtest.json" <<'PYS3BH'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["probe"]["timed_out"] is True, d  # носитель — от прошлой пробы, не от отказа
print("   отказ по занятости носитель не тронул (в нём остался вердикт прошлой пробы)")
PYS3BH
expect_exit 1 "ноль нагрузок — это окно, а не «занят»: проба выполняется" \
  bash tools/qwen35_gate.sh run --local --state-dir "$G3" \
    --busy-cmd 'printf "тренировочных нагрузок: 0\n"' \
    --probe-cmd 'echo HF_LOAD_OK'
expect_contains "НЕ проверяется" "--ignore-busy — названное решение владельца, видно в выводе" \
  bash tools/qwen35_gate.sh run --local --state-dir "$G3" \
    --busy-cmd 'printf "тренировочных нагрузок: 1\n"' \
    --probe-cmd 'echo HF_LOAD_OK; echo VLLM_LOAD_OK' --ignore-busy

echo
echo "== 46. S3bi: платформенный сервис — не нагрузка кейса (AD-5) =="
# На стенде постоянно живут сервисы площадки (llama-server роутера и его дочерний
# сервер, ollama serve, dgx-dashboard-service, llm-platform-*): они держат CUDA и
# GPU-память, но нагрузками кейса НЕ являются. Если страж опознаёт их как нагрузку,
# гейт отказывает по построению — правило нельзя сделать зелёным, пока стоит
# платформа. Проверяются три мутанта: (а) только сервисы → зелёный; (б) нагрузка
# кейса → посчитана (и потребитель, qwen35_gate, отказывает); (в) и то и другое →
# названо РАЗДЕЛЬНО, ни одно не спрятано за другим.
cat > "$TMP/ad5_run.sh" <<SH
#!/usr/bin/env bash
# Гоняет страж AD-5 на подставной площадке: <ps-файл> <smi-файл> [tmux-файл]
env PATH="$TMP/fakebin:\$PATH" \\
  FAKE_PS_FILE="\${1:-/dev/null}" FAKE_SMI_FILE="\${2:-/dev/null}" \\
  FAKE_TMUX_FILE="\${3:-/dev/null}" \\
  bash tools/check_gb10_serialization.sh
SH
chmod +x "$TMP/ad5_run.sh"

# Сервисы площадки, включая тот, чья командная строка совпала бы и с LOAD_RE
# (путь `/opt/llm-platform/sft-router.py`): сервис обязан остаться сервисом.
cat > "$TMP/ps_platform_only.txt" <<'PS'
 3061661       1 11-03:30:52 /opt/llama/llama-server --models-preset /models/llm-router-presets.ini --host 0.0.0.0 --port 8080
 2461766 3061661  5-04:22:03 /opt/llama/llama-server --host 127.0.0.1 --port 37211 --alias qwen3.8-27b
 3061371 3061320 11-03:30:52 /bin/ollama serve
 7770001       1  3-00:00:00 python3 /opt/llm-platform/sft-router.py --stage sft
PS
cat > "$TMP/smi_platform_only.txt" <<'SMI'
3061661, /opt/llama/llama-server, 28137 MiB
2461766, /opt/llama/llama-server, 170 MiB
7770001, python3, 512 MiB
SMI
# Нагрузка кейса (`--exp_name`) — своя метка, а не «на GPU есть процесс».
cat > "$TMP/ps_platform_and_load.txt" <<'PS'
 3061661       1 11-03:30:52 /opt/llama/llama-server --host 0.0.0.0 --port 8080
 2461766 3061661  5-04:22:03 /opt/llama/llama-server --host 127.0.0.1 --port 37211
 562493  562469 19:19:16 python3 /workspace/shared/laguna_pipeline_sft.py --stage sft --exp_name sft-v13-2e6-20260921-2054
PS
cat > "$TMP/smi_platform_and_load.txt" <<'SMI'
3061661, /opt/llama/llama-server, 28137 MiB
562493, python3, 38382 MiB
SMI
cat > "$TMP/ps_platform_two_loads.txt" <<'PS'
 3061661       1 11-03:30:52 /opt/llama/llama-server --host 0.0.0.0 --port 8080
  101       1 01:20:00 python3 /workspace/shared/laguna_pipeline_v8.py --stage cpt --exp_name run_a
  102       1 01:10:00 python3 /workspace/shared/laguna_pipeline_v8.py --stage rl --exp_name run_b
PS

echo "  --- 46а. мутант «а»: платформенные сервисы на GPU → гейт зелёный ---"
expect_exit 0 "сервисы площадки на GPU не красят гейт (exit 0)" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_only.txt" "$TMP/smi_platform_only.txt"
expect_contains "тренировочных нагрузок: 0" "сервис площадки нагрузкой НЕ считается (в т.ч. llm-platform/sft-router)" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_only.txt" "$TMP/smi_platform_only.txt"
expect_contains "платформенных сервисов: 4 — не предмет правила" "сервисы НАЗВАНЫ отдельной строкой, а не спрятаны" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_only.txt" "$TMP/smi_platform_only.txt"
expect_contains "платформенных сервисов: 3" "сенсор GPU называет сервисы отдельным счётом" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_only.txt" "$TMP/smi_platform_only.txt"
expect_contains "нагрузок кейса: 0" "среди CUDA-процессов нагрузок кейса нет — и это сказано словом" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_only.txt" "$TMP/smi_platform_only.txt"
expect_absent "ВНИМАНИЕ" "лишнего «ВНИМАНИЕ» от постоянного сервиса нет (прежний ложный сигнал снят)" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_only.txt" "$TMP/smi_platform_only.txt"
expect_contains "вне опознания: 0" "ни один сервис площадки не остался неопознанным" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_only.txt" "$TMP/smi_platform_only.txt"

echo "  --- 46б. мутант «б»: нагрузка кейса → посчитана, потребитель отказывает ---"
expect_exit 0 "одна нагрузка кейса при живых сервисах — не нарушение (AD-5: одна за раз)" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_and_load.txt" "$TMP/smi_platform_and_load.txt"
expect_contains "тренировочных нагрузок: 1" "нагрузка кейса посчитана, несмотря на сервисы рядом" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_and_load.txt" "$TMP/smi_platform_and_load.txt"
expect_contains "нагрузок кейса: 1" "нагрузка кейса опознана и на стороне GPU" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_and_load.txt" "$TMP/smi_platform_and_load.txt"
# Потребитель стража: занятость читается его строкой — нагрузка кейса отказывает пробе.
expect_exit 2 "потребитель (qwen35_gate run) отказывает по факту нагрузки кейса" \
  bash -c "bash tools/qwen35_gate.sh run --local --state-dir '$TMP/s3bi-busy' \
    --busy-cmd 'bash $TMP/ad5_run.sh $TMP/ps_platform_and_load.txt $TMP/smi_platform_and_load.txt' \
    --probe-cmd 'echo HF_LOAD_OK; echo VLLM_LOAD_OK'"
expect_exit 1 "две нагрузки кейса → нарушение AD-5 (exit 1), даже если сервис площадки рядом" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_two_loads.txt" /dev/null

echo "  --- 46в. мутант «в»: и сервис, и нагрузка → названо РАЗДЕЛЬНО ---"
expect_contains "тренировочных нагрузок: 1" "«в»: нагрузка названа своим счётом" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_and_load.txt" "$TMP/smi_platform_and_load.txt"
expect_contains "платформенных сервисов: 2 — не предмет правила" "«в»: сервисы названы своим счётом" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_platform_and_load.txt" "$TMP/smi_platform_and_load.txt"
cat > "$TMP/ps_idle.txt" <<'PS'
  900       1 02:00:00 /usr/sbin/cron -f
  901     900 02:00:00 /usr/bin/dashboard-plain --serve
PS
expect_exit 0 "«в»: строка сервисов печатается и при нуле — «сервисов нет» ≠ «нагрузок нет»" \
  bash -c "bash $TMP/ad5_run.sh $TMP/ps_idle.txt /dev/null | grep -q 'платформенных сервисов: 0 — не предмет правила'"

echo "  --- 46г. граница: неопознанный CUDA-процесс называется, а не прячется ---"
cat > "$TMP/ps_foreign.txt" <<'PS'
 3061661       1 11-03:30:52 /opt/llama/llama-server --host 0.0.0.0 --port 8080
  900000       1 02:00:00 python3 /home/user/experiments/whatever.py --sleep-forever
PS
cat > "$TMP/smi_foreign.txt" <<'SMI'
3061661, /opt/llama/llama-server, 28137 MiB
900000, python3, 700 MiB
SMI
expect_contains "вне опознания: 1" "чужой CUDA-процесс назван третьей корзиной, а не приписан сервису или нагрузке" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_foreign.txt" "$TMP/smi_foreign.txt"
expect_contains "не опознан ни как сервис площадки, ни как нагрузка кейса" "граница названа словами, а не умолчанием" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_foreign.txt" "$TMP/smi_foreign.txt"
expect_exit 0 "неопознанное — предупреждение, а не нарушение: вердикт AD-5 решает сенсор нагрузок" \
  bash "$TMP/ad5_run.sh" "$TMP/ps_foreign.txt" "$TMP/smi_foreign.txt"

echo
echo "== 47. S3bi: «not-run» — отсутствие носителя, а не файл-заглушка =="
# Состояние «проба не запускалась» обязано называться СЛОВОМ в выводе гейта, а не
# существованием файла: заранее записанная «not-run»-запись читалась бы как вердикт
# (класс «зелёное без результата»). Носитель создаётся ТОЛЬКО фактом прогона.
G4="$TMP/s3bi-notrun"; mkdir -p "$G4"
expect_exit 2 "пустой каталог состояния → not-run (вердикта нет)" \
  bash tools/qwen35_gate.sh status --state-dir "$G4" --legacy-log "$TMP/no-such-legacy.log"
expect_contains "состояние: not-run" "состояние названо словом" \
  bash tools/qwen35_gate.sh status --state-dir "$G4" --legacy-log "$TMP/no-such-legacy.log"
expect_contains "вердикта НЕТ" "«не проверено» названо прямо, а не выдано за «не поддержано»" \
  bash tools/qwen35_gate.sh status --state-dir "$G4" --legacy-log "$TMP/no-such-legacy.log"
expect_exit 0 "status не создал носителя: отсутствие файла = «не запускалось»" \
  bash -c "test ! -e '$G4/qwen35-loadtest.json' && test ! -e '$G4/qwen35-loadtest.log'"
expect_exit 2 "отказ по занятости (пробы не было) носителя тоже не создаёт" \
  bash tools/qwen35_gate.sh run --local --state-dir "$G4" \
    --busy-cmd 'printf "тренировочных нагрузок: 1\n"' \
    --probe-cmd 'echo HF_LOAD_OK; echo VLLM_LOAD_OK'
expect_exit 0 "после отказа носителя по-прежнему нет: вердикт не выдуман" \
  bash -c "test ! -e '$G4/qwen35-loadtest.json'"
expect_exit 2 "состояние после отказа — всё ещё not-run, а не «не поддержано»" \
  bash tools/qwen35_gate.sh status --state-dir "$G4" --legacy-log "$TMP/no-such-legacy.log"
expect_exit 0 "носитель появляется РОВНО фактом прогона" \
  bash tools/qwen35_gate.sh run --local --state-dir "$G4" \
    --busy-cmd 'printf "тренировочных нагрузок: 0\n"' \
    --probe-cmd 'echo HF_LOAD_OK; echo VLLM_LOAD_OK'
expect_exit 0 "и в носителе — вердикт прогона, а не «not-run»" \
  python3 - "$G4/qwen35-loadtest.json" <<'PYS3BI'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["verdict"] == "supported", d["verdict"]
assert d["verdict"] != "not-run", "«not-run» в носителе читался бы как вердикт"
print("   носитель создан прогоном: verdict =", d["verdict"])
PYS3BI
expect_exit 0 "вердикт в носителе принимает только supported/unsupported: «not-run» записать нечем" \
  bash -c 'grep -vE "^\s*#" tools/qwen35_gate.sh | grep -q "verdict=\"unsupported\""'

echo "──────────────────────────────────────────────"
echo "итого: PASS=$PASS FAIL=$FAIL SKIP=$SKIP"
if [ "$FAIL" -gt 0 ]; then
  for f in "${failures[@]}"; do echo "  - $f"; done
  exit 1
fi
echo "TOOL TESTS OK"
exit 0
