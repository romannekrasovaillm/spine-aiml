<!-- сгенерировано tools/inventory_stand_weights.py 2026-09-22T17:04:07+00:00 на spark-44c3 -->
# Инвентаризация весов лесенки на стенде (Н-3, read-only)

Стенд: `spark-44c3`, режим: read-only (scandir/stat/чтение config.json, заголовков safetensors, tokenizer.json).

| # | Позиция | HF id | Волна | Веса | Где | Размер | Параметров | Токенизатор |
|---|---|---|---|---|---|---|---|---|
| 1 | `qwen25-05b` | `Qwen/Qwen2.5-0.5B` | В-0 (идёт) | **есть** | hf-hub (snapshots/) | 942.3 МБ | 0.494 B | есть |
| 2 | `qwen25-15b` | `Qwen/Qwen2.5-1.5B` | В-2 | **есть** | hf-hub (snapshots/) | 2.9 ГБ | 1.544 B | есть |
| 3 | `qwen25-3b` | `Qwen/Qwen2.5-3B` | В-2 | **есть** | hf-hub (snapshots/) | 5.7 ГБ | 3.086 B | есть |
| 4 | `qwen25-7b` | `Qwen/Qwen2.5-7B` | В-3 | **есть** | hf-hub (snapshots/) | 14.2 ГБ | 7.616 B | есть |
| 5 | `qwen3-06b` | `Qwen/Qwen3-0.6B` | В-1 | **есть** | hf-hub (snapshots/) | 1.4 ГБ | 0.752 B | есть |
| 6 | `qwen3-17b` | `Qwen/Qwen3-1.7B` | В-5 | **есть** | hf-hub (snapshots/) | 3.8 ГБ | 2.032 B | есть |
| 7 | `qwen3-4b` | `Qwen/Qwen3-4B` | В-5 | **есть** | hf-hub (snapshots/) | 7.5 ГБ | 4.022 B | есть |
| 8 | `qwen3-8b` | `Qwen/Qwen3-8B` | В-5 | **нет (пустой снапшот)** | — | — | — | нет |
| 9 | `qwen35-08b` | `Qwen/Qwen3.5-0.8B-Base` | В-1 | **есть** | hf-hub (snapshots/) | 1.6 ГБ | 0.873 B | есть |
| 10 | `qwen35-2b` | `Qwen/Qwen3.5-2B-Base` | В-5 | **есть** | hf-hub (snapshots/) | 4.2 ГБ | 2.274 B | есть |
| 11 | `qwen35-4b` | `Qwen/Qwen3.5-4B-Base` | В-5 | **есть** | hf-hub (snapshots/) | 8.7 ГБ | 4.66 B | есть |
| 12 | `qwen35-9b` | `Qwen/Qwen3.5-9B-Base` | В-5 | **есть** | hf-hub (snapshots/) | 18.0 ГБ | 9.653 B | есть |

**Итог: 11 из 12 позиций с весами**, только метаданные — —, нет — ['qwen3-8b']; суммарно 68.9 ГБ.

## Целостность: что проверено механизмом

| Позиция | заголовки safetensors | `*.incomplete` | индекс сверен | sha256 снят здесь | имя блоба = sha256 |
|---|---|---|---|---|---|
| `qwen25-05b` | валидны | нет | — (одношардовая) | да | проверено равенством |
| `qwen25-15b` | валидны | нет | — (одношардовая) | да | проверено равенством |
| `qwen25-3b` | валидны | нет | да | да | проверено равенством |
| `qwen25-7b` | валидны | нет | да | да | проверено равенством |
| `qwen3-06b` | валидны | нет | — (одношардовая) | да | проверено равенством |
| `qwen3-17b` | валидны | нет | да | да | проверено равенством |
| `qwen3-4b` | валидны | нет | да | да | проверено равенством |
| `qwen35-08b` | валидны | нет | да | да | проверено равенством |
| `qwen35-2b` | валидны | нет | да | да | проверено равенством |
| `qwen35-4b` | валидны | нет | да | да | проверено равенством |
| `qwen35-9b` | валидны | нет | да | да | проверено равенством |

## Построчно (для протокола)

```
qwen25-05b: есть — /home/user/.cache/huggingface/hub/models--Qwen--Qwen2.5-0.5B (942.3 МБ, 1 шард(ов), ≈0.494 B параметров)
qwen25-15b: есть — /home/user/.cache/huggingface/hub/models--Qwen--Qwen2.5-1.5B (2.9 ГБ, 1 шард(ов), ≈1.544 B параметров)
qwen25-3b: есть — /home/user/.cache/huggingface/hub/models--Qwen--Qwen2.5-3B (5.7 ГБ, 2 шард(ов), ≈3.086 B параметров)
qwen25-7b: есть — /home/user/.cache/huggingface/hub/models--Qwen--Qwen2.5-7B (14.2 ГБ, 4 шард(ов), ≈7.616 B параметров)
qwen3-06b: есть — /home/user/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B (1.4 ГБ, 1 шард(ов), ≈0.752 B параметров)
qwen3-17b: есть — /home/user/.cache/huggingface/hub/models--Qwen--Qwen3-1.7B (3.8 ГБ, 2 шард(ов), ≈2.032 B параметров)
qwen3-4b: есть — /home/user/.cache/huggingface/hub/models--Qwen--Qwen3-4B (7.5 ГБ, 3 шард(ов), ≈4.022 B параметров)
qwen3-8b: нет (пустой снапшот) — ни в локальном HF-кэше стенда, ни в общем сторе
qwen35-08b: есть — /home/user/.cache/huggingface/hub/models--Qwen--Qwen3.5-0.8B-Base (1.6 ГБ, 1 шард(ов), ≈0.873 B параметров)
qwen35-2b: есть — /home/user/.cache/huggingface/hub/models--Qwen--Qwen3.5-2B-Base (4.2 ГБ, 1 шард(ов), ≈2.274 B параметров)
qwen35-4b: есть — /home/user/.cache/huggingface/hub/models--Qwen--Qwen3.5-4B-Base (8.7 ГБ, 2 шард(ов), ≈4.66 B параметров)
qwen35-9b: есть — /home/user/.cache/huggingface/hub/models--Qwen--Qwen3.5-9B-Base (18.0 ГБ, 4 шард(ов), ≈9.653 B параметров)
```

## Токен-пространство (AD-3) по семействам

| Позиция | vocab в tokenizer.json | added | единый id нативно (6 токенов v12) | добавляет пайплайн |
|---|---|---|---|---|
| `qwen25-05b` | 151643 | 22 | </tool_call>, <tool_call> | </think>, </tool_response>, <think>, <tool_response> |
| `qwen25-15b` | 151643 | 22 | </tool_call>, <tool_call> | </think>, </tool_response>, <think>, <tool_response> |
| `qwen25-3b` | 151643 | 22 | </tool_call>, <tool_call> | </think>, </tool_response>, <think>, <tool_response> |
| `qwen25-7b` | 151643 | 22 | </tool_call>, <tool_call> | </think>, </tool_response>, <think>, <tool_response> |
| `qwen3-06b` | 151643 | 26 | </think>, </tool_call>, </tool_response>, <think>, <tool_call>, <tool_response> | — |
| `qwen3-17b` | 151643 | 26 | </think>, </tool_call>, </tool_response>, <think>, <tool_call>, <tool_response> | — |
| `qwen3-4b` | 151643 | 26 | </think>, </tool_call>, </tool_response>, <think>, <tool_call>, <tool_response> | — |
| `qwen35-08b` | 248044 | 22 | </tool_call>, <tool_call> | </think>, </tool_response>, <think>, <tool_response> |
| `qwen35-2b` | 248044 | 22 | </tool_call>, <tool_call> | </think>, </tool_response>, <think>, <tool_response> |
| `qwen35-4b` | 248044 | 22 | </tool_call>, <tool_call> | </think>, </tool_response>, <think>, <tool_response> |
| `qwen35-9b` | 248044 | 22 | </tool_call>, <tool_call> | </think>, </tool_response>, <think>, <tool_response> |
