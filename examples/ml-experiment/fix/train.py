"""Учебный скрипт обучения (исправленная версия: seed зафиксирован, ML-06)."""

import random

SEED = 42  # seed фиксируется до прогона и дублируется в run-manifest.yaml

DATA = [0.1, 0.4, 0.2, 0.3]


def train(data, seed=SEED):
    random.seed(seed)
    weight = 0.0
    for x in data:
        weight += x * 0.01
    return weight


if __name__ == "__main__":
    print(f"weight={train(DATA):.4f}")
