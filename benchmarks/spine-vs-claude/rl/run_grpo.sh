#!/usr/bin/env bash
# GRPO H2.1: Qwen2.5-0.5B, reward = fitness-гейт (reward.py), 4 задачи.
set -e
export PYTHONUNBUFFERED=1
python dataset.py data/train.parquet
python -m verl.trainer.main_ppo \
  algorithm.adv_estimator=grpo \
  data.train_files=data/train.parquet \
  data.val_files=data/train.parquet \
  data.train_batch_size=4 \
  data.max_prompt_length=2048 \
  data.max_response_length=2048 \
  data.prompt_key=prompt \
  data.truncation=error \
  actor_rollout_ref.model.path=Qwen/Qwen2.5-0.5B-Instruct \
  actor_rollout_ref.actor.optim.lr=1e-6 \
  actor_rollout_ref.actor.ppo_mini_batch_size=4 \
  actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=2 \
  actor_rollout_ref.rollout.name=vllm \
  actor_rollout_ref.rollout.gpu_memory_utilization=0.5 \
  actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
  actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=2 \
  actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=2 \
  actor_rollout_ref.rollout.n=2 \
  critic.model.path=Qwen/Qwen2.5-0.5B-Instruct \
  critic.ppo_micro_batch_size_per_gpu=2 \
  algorithm.use_kl_in_reward=False \
  trainer.logger='[console]' \
  trainer.project_name=spine_aiml_h21 \
  trainer.experiment_name=grpo_05b_fitness \
  trainer.n_gpus_per_node=1 \
  trainer.nnodes=1 \
  trainer.save_freq=10 \
  trainer.total_epochs=2 \
  custom_reward_function.path=reward.py \
  custom_reward_function.name=compute_score
