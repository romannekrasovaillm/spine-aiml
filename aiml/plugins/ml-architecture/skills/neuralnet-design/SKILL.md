---
name: neuralnet-design
description: >-
  Проектирование нейросетей с нуля: от задачи и индуктивного сдвига — к
  семейству архитектуры (MLP / CNN / RNN / Transformer / MoE / SSM / hybrid),
  затем к парам-бюджету (total vs active params, скейлинг-законы,
  tokens-per-param) и к честной оценке памяти ДО аренды GPU (веса, KV-кэш,
  optimizer state, оверхед). ВСЕГДА используй этот навык, когда пользователь:
  «спроектируй сеть», «выбери архитектуру», «MLP или Transformer», «CNN vs
  ViT», «RNN vs SSM», «нужен MoE», «гибрид Mamba+attention», «сколько нужно
  параметров», «какой размер модели взять», «скейлинг-законы / Chinchilla»,
  «сколько токенов на параметр», «влезет ли модель в GPU», «посчитай VRAM»,
  «сколько памяти займёт KV-кэш», «оцени перед арендой», а также при
  диагностике типовых ловушек — mode collapse, ABI-несовместимости
  движка/весов, OOM (включая замаскированный под NCCL). Грундится в библиотеке
  рецептов (transformer-canon, frontier-lab, moe, liquid-ai, tii-falcon,
  nemotron, vastai-infra) — не выдумывай архитектурные факты, цитируй источники.
---

# Проектирование нейросетей с нуля

Четыре шага, всегда в этом порядке: **(1) семейство архитектуры под задачу →
(2) парам-бюджет и скейлинг → (3) оценка памяти → (4) проверка ловушек.**
Пропуск шага — источник почти всех дорогих ошибок: сеть, спроектированная
«как у всех», без индуктивного сдвига под данные, и модель, «которую должно
хватить», но не влезающая на GPU, стоят одинаково много времени.

## Правило №0 — грунтовка, а не память модели

Архитектурные и числовые утверждения (размерности, число слоёв, скейлинг-
коэффициенты, потолки балансировки) **берутся из библиотеки рецептов с путём
к источнику**, а не из весов модели. Библиотека (176 категорий):
`~/experiments/agents/0710-ariadna/plugins/`. Если факта в
библиотеке нет — так и скажи, не достраивай.

## Шаг 1. Семейство архитектуры под задачу

Сначала назови **индуктивный сдвиг**, который несёт задача, и выбери семейство,
чей сдвиг ему соответствует. «Взять Transformer, потому что он лучший» —
не ответ; лучший он там, где нужен глобальный контекст и нет жёстких
пространственных/временных априори.

| Задача / структура данных | Семейство | Почему |
|---|---|---|
| Таблица, признаки фиксированной длины, средний масштаб | **MLP** (+ baseline градиентного бустинга) | Нет структуры по осям; сильный нелинейный baseline; обязательно сравни с GBDT |
| Сетка/изображение, локальные паттерны, сдвиг-инвариантность | **CNN** (conv) | Weight sharing + локальные рецептивные поля = правильный сдвиг, дёшево по параметрам |
| Поток/real-time, жёсткая рекуррентная зависимость, малая память | **RNN** (LSTM/GRU) | Состояние фиксированного размера, пошаговость; но плохой параллелизм обучения |
| Текст/общий язык, масштаб, длинный контекст | **Transformer** (decoder-only) | Глобальное внимание = точный ассоциативный recall; канон 2023–2025 — см. ниже |
| Нужна ёмкость без пропорционального compute на токен | **MoE** | Total params → VRAM, active params → скорость; разреженность |
| Длинный контекст, KV-кэш запретительно растёт | **SSM** (Mamba-2) / **hybrid** | Линейная сложность и состояние фиксированного размера вместо O(L²) |
| Длинный контекст И точный recall (needle) | **hybrid SSM+attention** | Чистые SSM слабы на точном recall; единицы attention-слоёв возвращают его почти без KV-кэша |
| Sub-3B on-device (телефон/NPU), latency-критично | **hybrid conv+GQA** | Гибрид «gated short conv + редкие GQA» — минимальные ядра, предсказуемая память |

**Канон decoder-трансформера (2023–2025), семь осей** — бери как baseline для
сравнения: RoPE (позиции); полное softmax-внимание + GQA/MLA; SwiGLU (FFN);
fine-grained MoE + 1 shared-эксперт + aux-loss-free балансировка; pre-norm +
тождественный residual; RMSNorm; AdamW с BF16→FP8. У каждой оси есть известная
слабость («точка атаки») — фронтир 2026 отступает именно по ним.
→ `transformer-alternatives/skills/transformer-canon/SKILL.md`

**Отступления 2026 (когда канон не тянет):** DeepSeek V4 — гибридное внимание
CSA+HCA (компрессия KV, sparse top-k), mHC (residual как дважды стохастическая
матрица, Sinkhorn-Knopp), Muon; Kimi K3 — KDA (линейное внимание), Stable
LatentMoE, Quantile Balancing; Inkling — learned relative bias, SWA:global 5:1.
→ `transformer-alternatives/skills/canon-deviations-2026/` (SKILL + `references/`)

**Гибриды (главный тренд длинного контекста):**
- Модель-образец — Nemotron 3 Ultra: чередование `Mamba-2 → LatentMoE →
  Mamba-2 → Attention → Mamba-2 → LatentMoE`, 108 слоёв, 550B total / 55B
  active, KV-головы 2 на 64 Q-головы, латентная размерность MoE 1/4 от d_model.
  Рецепт и калькулятор параметров:
  `nvidia-nemotron/skills/nemotron3-hybrid-moe-architecture/` (+ `scripts/param_calculator.py`).
- Параллельный гибрид attention+Mamba-2 на одном входе (Falcon-H1) vs
  последовательный (Jamba): доля attention держится малой.
  → `tii-falcon/skills/falcon-h1-hybrid/SKILL.md`
- Edge-версия — LFM2: **без SSM и linear attention**, gated short convolution +
  редкие GQA-блоки; архитектура найдена hardware-in-the-loop Pareto-поиском.
  → `liquid-ai/skills/lfm2-architecture/SKILL.md`
- Ретрофит готового GQA-чекпоинта в гибрид Lightning Attention + MLA (M=8, 7:1)
  → `antgroup-ling/skills/ling26-hybrid-attention-retrofit/` (arXiv:2606.15079)

**MoE — отдельная дисциплина.** Fine-grained эксперты + 1 shared эксперт;
активация affinity-скоров (DeepSeek V4: `Sqrt(Softplus(·))` вместо Sigmoid);
hash routing для первых слоёв; балансировка без aux-loss.
→ `frontier-lab/skills/deepseek-v4-training-recipe/references/architecture.md`

Правило выбора: **начни с меньшего семейства, которое несёт нужный сдвиг**
(MLP/CNN), и переходи к Transformer/MoE/SSM только когда упёрся в конкретное
ограничение (нет глобального контекста / не хватает ёмкости / не тянет KV).
Каждое усложнение обязано иметь замер, который его оправдывает.

## Шаг 2. Парам-бюджет и скейлинг-законы

1. **Разделяй total и active.** В MoE VRAM определяется **total**
   параметрами, а стоимость токена на инференсе — **active**. Отношение
   total/active — осознанная точка на кривой; у Nemotron 3 Ultra ≈ 10:1
   (550B/55B), sparsity `top-k / experts` = 22/512. Потолок достижимой
   несбалансированности экспертов равен `E/k` — держи его под рукой для
   мониторинга претрейна (для Ultra 23.27).
   → `nvidia-nemotron/skills/nemotron3-hybrid-moe-architecture/SKILL.md`

2. **Chinchilla-допущение «фиксированные ~20 токенов/параметр» неверно.**
   В мире Inference Inflection диапазон — **200–900 токенов/параметр**, и он
   зависит от задачи (Roberts et al.). Число параметров осмысленно только
   вместе с: (а) объёмом данных, (б) куда уходит compute, (в) условиями
   инференса/кто запускает модель.
   → `rl-training/skills/death-of-params-scaling/SKILL.md`

3. **Пять рычагов скейлинга вместо одного** (включая MoE sparsity, нотация
   XA-YB): параметры, данные, распределение compute, рецепт пост-тренинга,
   условия инференса.

4. **Memorization любит параметры, reasoning — пост-тренинг и эффективную
   глубину.** Продвинутые навыки (20+ шагов причинных цепочек, поиск
   уязвимостей) не живут в числе параметров после порога knowledge-holding;
   большие скачки даёт RL на executable, verifiable long-horizon окружениях.
   → тот же скилл `death-of-params-scaling`

5. **Считай параметры калькулятором, не в уме.** Для гибридных MoE-конфигураций
   есть готовые аналитические скрипты:
   - `nvidia-nemotron/skills/nemotron3-hybrid-moe-architecture/scripts/param_calculator.py`
     — total/active, KV-кэш, состав слоёв (`--preset ultra` / `--preset tiny`);
   - `nvidia-nemotron/skills/nemotron3-arch-latentmoe/scripts/latentmoe_budget.py`
     — бюджет латентного MoE;
   - `antgroup-ling/skills/ling26-hybrid-attention-retrofit/scripts/hybrid_ratio_flops.py`
     — цена attention при разных гибридных соотношениях M (выбор доли линейных слоёв).

## Шаг 3. Оценка памяти (VRAM) — ДО аренды GPU

Порядок счёта (грунт: `vastai-infra/skills/vastai-gpu-planning/SKILL.md`;
правила аренды из `CLAUDE.md`):

1. **Веса:** `params × bytes_per_param`. bf16/fp16 = 2.0, fp8/int8 = 1.0,
   int4 ≈ 0.55–0.6 (с квант-метаданными).
2. **Правило «2 копии + оверхед» (обязательный baseline-расчёт):**
   `модель × 2 байта × 2 копии (train + vLLM) + overhead 4–6 GB`. Для
   обучения добавь optimizer state: full FT ≈ 16–20 байт/параметр,
   LoRA ≈ 2, QLoRA ≈ 0.7–1; смешанная точность + Adam ≈ 16 байт/параметр
   **без активаций** (активации зависят от батча и длины — считай отдельно).
3. **KV-кэш:** `2 × n_layers × n_kv_heads × head_dim × bytes` на токен
   (умножь на длину контекста и размер батча). У гибридов KV-кэш есть только
   у attention-слоёв — это и есть выигрыш длинного контекста. Mixed-precision
   KV (BF16 для RoPE-размерностей, FP8 для остальных) ≈ вдвое меньше.
4. **Оверхед рантайма:** CUDA context ≈ 1.5–2 GB. **vLLM преаллоцирует
   `gpu_memory_utilization` (дефолт 0.9) и считает его от ВСЕЙ памяти карты** —
   поэтому **GPU 0 всегда узкое место** в мульти-GPU-сетапе (на нём же живут
   веса тренера и NCCL-буферы). Это подтверждённая причина падений —
   см. память `gpu0-memory-tightrope`.
5. **Эффективная ёмкость ≈ 0.8–0.85 × номинал VRAM.** Закладывай этот запас,
   а не «впритык».
6. **Параллелизм** (когда не влезает): TP даёт на карту
   `(weights + KV)/N + неразделяемый оверхед`. Но TP упирается в ABI
   (см. ловушку 2), а FSDP/ZeRO — в межнодовый трафик; считай `all-to-all` для MoE
   отдельно (LatentMoE существует именно чтобы срезать этот трафик).

Если расчёт даёт <10% запаса — это не «влезет», а «упадёт на следующем шаге
роста батча». Меньше модель / короче контекст / больше карта — дешевле, чем
ночь отладки OOM.

## Шаг 4. Типовые ловушки

### 4.1. Mode collapse (RL/MoE)

- **RL-коллапс:** энтропия политики → 0, обучение вырождается. **KL-регуляризация
  обязательна** — `kl_coef=0.01` полностью предотвращает коллапс (подтверждено
  в GRPO-экспериментах). Мониторь энтропию каждый шаг: <0.1 = коллапс,
  здоровая 1.0–2.0. Антипаттерн — тюнить «всё кроме KL».
  → память `grpo-kl-collapse-prevention`; `rl-training/skills/death-of-params-scaling/`
- **MoE router collapse / dead experts:** симптомы — MaxVio растёт при
  стабильной медиане, часть экспертов не получает токенов. Диагностика
  «симптом → диагноз → лечение»: `moe/skills/moe-router-health/SKILL.md`.
- Смежное: катастрофическое забывание при CPT/SFT — не путать с коллапсом
  (`ml-continual-learning`, стадии пайплайна).

### 4.2. ABI-несовместимость (движок / веса / железо)

- **ABI уровня движка:** PyPI-колёса vLLM × PyTorch часто несовместимы по ABI;
  единственный проверенный путь — контейнер `hiyouga/verl:...-cxx11abi0`.
  Обратный симптом: импорт проходит, а ядра падают/дают мусор.
  → память `vllm-torch-abi-compatibility`, `cuda-stack-setup`.
- **ABI уровня чекпоинта:** fused qkv vs раздельные `q/k/v_proj`, другой
  `head_dim`, tied vs untied `lm_head`, несовпадение `rope_theta`/scaling,
  раскладка тензоров — копирование тензора без учёта даёт молчаливо неверную
  модель. → `ml-grafting/skills/llm-grafting/SKILL.md` (раздел «Риски»).
- **ABI уровня железа:** «no kernel image» на новом sm (Blackwell/sm_120) —
  движок собран без нужной арки. Проверяй `compute capability` против
  заявленной поддержки образа **до** аренды.
  → `vastai-infra/skills/vastai-abi-doctor/SKILL.md`

### 4.3. OOM (и его маскировки)

- **NCCL-«ошибка связи» = OOM.** Крэши NCCL нередко оказываются CUDA out of
  memory на GPU 0, а не проблемой коммуникации. Не чини сеть — чини память.
  → память `nccl-oom-disguise`.
- **vLLM+train дисбаланс на GPU 0** — фундаментальная проблема
  (`gpu_memory_utilization` считается от всей карты): снижай долю vLLM,
  выноси тренер, уменьшай батч/контекст.
  → память `gpu0-memory-tightrope`; `vastai-infra/skills/vastai-oom-recovery/SKILL.md`
- **Диск, а не VRAM:** переполнение `/tmp` (env-файлы, `fast_downward`)
  роняет процесс так же, как OOM. Проверяй `df -h` до старта.
  → память `disk-sizing-for-ml-experiments`.

## Чек-лист перед запуском

- [ ] Назван индуктивный сдвиг задачи и выбрано минимальное семейство под него
- [ ] Для Transformer/MoE/hybrid указано, чем канон не устроил (какая ось/точка атаки)
- [ ] Парам-бюджет посчитан калькулятором, а не в уме; total и active разделены
- [ ] Объём данных соразмерён с диапазоном 200–900 токенов/параметр
- [ ] VRAM посчитан по шагу 3 (веса + 2 копии + optimizer + KV-кэш + оверхед)
- [ ] Заложен запас ≥10%; GPU 0 учтён как узкое место; `gpu_memory_utilization` посчитан от всей карты
- [ ] Проверена ABI-совместимость: движок↔torch↔CUDA, арка GPU, раскладка чекпоинта
- [ ] Для RL заложен KL>0 и мониторинг энтропии; для MoE — метрики роутера
- [ ] Проверено свободное место на диске (`/tmp`), не только VRAM

## Источники (грундовка)

Библиотека плагинов: `~/experiments/agents/0710-ariadna/plugins/`
- `transformer-alternatives/skills/transformer-canon/SKILL.md` — семь осей канона и их «точки атаки»
- `transformer-alternatives/skills/canon-deviations-2026/` — DeepSeek V4 / Kimi K3 / Inkling (`references/deepseek-v4.md`, `kimi-k3.md`, `inkling.md`)
- `frontier-lab/skills/deepseek-v4-training-recipe/references/architecture.md` — MoE (Sqrt(Softplus), hash routing), mHC, CSA/HCA, Muon
- `frontier-lab/skills/nemotron3-ultra-moe-hybrid-mamba-training/references/pretraining.md` — гибрид Mamba-attention MoE, NVFP4, классификация дивергенций
- `frontier-lab/skills/trinity-moe-training-playbook/references/architecture.md` — GQA+QK-norm, gating, 3:1 local/global
- `nvidia-nemotron/skills/nemotron3-hybrid-moe-architecture/` (+ `scripts/param_calculator.py`) и `nemotron3-arch-latentmoe/scripts/latentmoe_budget.py`
- `antgroup-ling/skills/ling26-hybrid-attention-retrofit/` (+ `scripts/hybrid_ratio_flops.py`)
- `liquid-ai/skills/lfm2-architecture/SKILL.md` — conv+GQA edge-гибрид, hardware-in-the-loop
- `tii-falcon/skills/falcon-h1-hybrid/SKILL.md` — параллельный hybrid attention+Mamba-2
- `moe/skills/moe-router-health/SKILL.md` — диагностика роутера/мёртвых экспертов
- `rl-training/skills/death-of-params-scaling/SKILL.md` — 200–900 токенов/параметр, пять рычагов, memorization vs reasoning
- `vastai-infra/skills/vastai-gpu-planning/SKILL.md`, `vastai-llm-scaling/SKILL.md`, `vastai-abi-doctor/SKILL.md`, `vastai-oom-recovery/SKILL.md`
- Библиотека статей: `~/Документы/КОД/gigachat/РАЗБОРЫ/recipes_taxonomy/` (разделы `05_МОДЕЛИ_И_АРХИТЕКТУРЫ/`, `11_Техотчёты_лабораторий_LLM/`)

Память среды (VRAM/OOM/ABI-инциденты): `war-chest память ML-контура` —
`gpu0-memory-tightrope`, `nccl-oom-disguise`, `vllm-torch-abi-compatibility`,
`cuda-stack-setup`, `disk-sizing-for-ml-experiments`, `grpo-kl-collapse-prevention`.
