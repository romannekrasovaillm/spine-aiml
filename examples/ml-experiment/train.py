"""Учебный скрипт обучения (нарочито красный: демо fitness-гейта).

Гейт ML-06 требует зафиксированное зерно генератора — здесь его нет.
Исправленная версия — в fix/train.py.
"""

DATA = [0.1, 0.4, 0.2, 0.3]


def train(data):
    weight = 0.0
    for x in data:
        weight += x * 0.01
    return weight


if __name__ == "__main__":
    print(f"weight={train(DATA):.4f}")
