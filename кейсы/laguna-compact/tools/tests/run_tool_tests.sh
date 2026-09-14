#!/usr/bin/env bash
# Тесты стражей S0/S1: у каждого инструмента проверяются и зелёный, и красный путь.
#
# Принцип: гейт, который не падал ни на одном негативном сценарии, не проверен.
# Поэтому на каждый инструмент есть (а) положительный случай, (б) синтетическое
# нарушение, (в) отсутствие входа → NOT-VERIFIED (exit 2) или вакуумный exit 0
# там, где он объявлен в контракте.
#
# Фикстуры живут в mktemp-каталоге и удаляются на выходе: в дерево кейса ничего
# не пишется, данные не копируются (AD-4).
#
# Запуск: bash tools/tests/run_tool_tests.sh

set -uo pipefail

CASE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$CASE_ROOT"

TMP="$(mktemp -d)"
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

echo
echo "== 9. data/corpus-card.json (C-010, C-016) =="
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
expect_exit 0 "карточка несёт лицензионный статус (C-016, ADR-005→ADR-011)" \
  python3 - <<'PY'
import json, sys
lic = json.load(open("data/corpus-card.json", encoding="utf-8"))["license"]
assert lic.get("license_status") == "internal_only", lic.get("license_status")
assert lic.get("domain_license_unresolved") is True
assert "публикация" in lic.get("license_note", ""), "нет границы «внутреннее/публикация»"
pub = lic.get("publication")
assert isinstance(pub, dict), "нет границы публикации (ADR-011)"
assert pub.get("corpus_and_derivatives") == "prohibited"
assert pub.get("case_documents") == "allowed"
assert "ADR-011" in lic.get("license_adr", ""), "не назван источник решения"
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
echo "──────────────────────────────────────────────"
echo "итого: PASS=$PASS FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  for f in "${failures[@]}"; do echo "  - $f"; done
  exit 1
fi
echo "TOOL TESTS OK"
exit 0
