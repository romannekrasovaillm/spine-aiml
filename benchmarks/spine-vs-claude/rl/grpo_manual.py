"""Ручной GRPO (H2.1 pipeline test): Qwen2.5-0.5B, reward = fitness-гейт.

Без veRL/vLLM — один скрипт (память п.29). REINFORCE с групповым baseline —
минимальная RL-петля: rollout -> reward(fitness) -> advantage -> update.
Цель прогона — убедиться, что петля харнесса (траектория -> гейт -> градиент)
работает end-to-end и reward растёт.
"""
import json
import torch
from torch.optim import AdamW
from transformers import AutoModelForCausalLM, AutoTokenizer

from reward_soft import compute_score
from dataset import TASKS, load_patterns

MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
N_ROLLOUT = 2      # ответов на задачу (группа для baseline)
STEPS = 24
LR = 1e-6
MAX_NEW = 128


def load_prompts():
    out = []
    for task in TASKS:
        spec = open(f"tasks/{task}/SPEC.md").read()
        tmd = open(f"tasks/{task}/TASK.md").read()
        prompt = (
            spec + "\n\n" + tmd + "\n\n"
            "Сгенерируй артефакты решения одним markdown-документом: "
            "## AD-<n> инварианты, # ADR-NNN (контекст/решение/последствия), "
            "## Решение с Approver: <ФИО> и spec: sha256:<hex>."
        )
        out.append((task, prompt, load_patterns(task)))
    return out


def seq_logprob(model, input_ids, attention_mask, resp_ids):
    """Лог-вероятность ответа resp_ids под моделью (causal LM)."""
    full = torch.cat([input_ids, resp_ids], dim=1)
    am = torch.ones_like(full)
    logits = model(input_ids=full, attention_mask=am).logits
    shift_logits = logits[:, :-1, :]
    shift_ids = full[:, 1:]
    logp = torch.log_softmax(shift_logits, dim=-1)
    tok_logp = logp.gather(2, shift_ids.unsqueeze(-1)).squeeze(-1)
    n = resp_ids.shape[1]
    return tok_logp[:, -n:].sum(dim=1)


def main():
    torch.manual_seed(0)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModelForCausalLM.from_pretrained(MODEL).to(dev)
    opt = AdamW(model.parameters(), lr=LR)
    prompts = load_prompts()

    for step in range(STEPS):
        model.eval()
        batch = []  # (logp_tensor, reward_tensor, group_id)
        group_ids = []
        with torch.no_grad():
            for gid, (task, prompt, patterns) in enumerate(prompts):
                inp = tok(prompt, return_tensors="pt", truncation=True, max_length=512).to(dev)
                for _ in range(N_ROLLOUT):
                    out = model.generate(**inp, max_new_tokens=MAX_NEW,
                                         do_sample=True, temperature=1.0)
                    resp = out[:, inp["input_ids"].shape[1]:]
                    text = tok.decode(resp[0], skip_special_tokens=True)
                    r = compute_score(task, text, None,
                                      json.dumps({"patterns": patterns}))
                    batch.append((resp, inp["input_ids"], float(r)))
                    group_ids.append(gid)
        # групповой baseline (GRPO): advantage = (r - mean_group)/std_group
        rewards = torch.tensor([b[2] for b in batch], device=dev)
        adv = torch.empty_like(rewards)
        for gid in set(group_ids):
            idx = [i for i, g in enumerate(group_ids) if g == gid]
            gr = rewards[idx]
            adv[idx] = (gr - gr.mean()) / (gr.std() + 1e-8)
        # аккумуляция градиента по одному ответу (иначе logits [1,seq,151k] × 8 -> OOM)
        model.train()
        opt.zero_grad()
        for (resp, inp_ids, _r), a in zip(batch, adv):
            logp = seq_logprob(model, inp_ids, torch.ones_like(inp_ids), resp)
            loss = -a * logp / len(batch)
            loss.backward()
            del logp, loss
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        opt.step()
        print(f"step {step+1}/{STEPS} mean_reward={rewards.mean().item():.3f} "
              f"loss={(-(adv * torch.tensor([0.0], device=dev)).sum()).item():.3f}", flush=True)


if __name__ == "__main__":
    main()
