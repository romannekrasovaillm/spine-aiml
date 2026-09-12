# Подключение моделей

Все провайдеры — OpenAI-совместимые endpoint'ы (`POST {base_url}/chat/completions`,
function calling, SSE-стриминг). Общий клиент — `src/llm/openai_compat.rs`;
конфигурация — секция `[models.*]` (`src/config.rs::ModelConfig`).

## Секция `[models.*]`

```toml
default_model = "deepseek"        # ключ из [models], обязан существовать

[models.deepseek]
base_url = "https://api.deepseek.com/v1"
model = "deepseek-flash"
api_key_env = "DEEPSEEK_API_KEY"
max_tokens = 8192                 # опционально (дефолт 8192)
temperature = 0.2                 # опционально (не задана — дефолт провайдера)
timeout_secs = 180                # дефолт 180
# proxy = "http://127.0.0.1:12080"  # опционально: egress-прокси провайдера (см. таблицу)
thinking_on  = { thinking = { type = "enabled" } }    # опционально: карта для /think on
thinking_off = { thinking = { type = "disabled" } }   # опционально: карта для /think off
```

Поля (`ModelConfig`):

| Поле | Назначение |
|---|---|
| `base_url` | Базовый URL API (без `/chat/completions` — суффикс добавляет клиент). |
| `model` | Идентификатор модели в запросах. |
| `api_key_env` | **Имя** переменной окружения с ключом. Значение ключа в конфиг не пишется, не логируется, не попадает в `Debug`. Ключ читается лениво — на первом запросе к этому провайдеру. |
| `api_key_file` | Запасной путь к файлу с ключом (`~` раскрывается): читается, если env-переменная не задана (удобно для Kimi: `~/.kimi_api_key`). Содержимое не выводится никуда — только путь в тексте ошибки. |
| `max_tokens` | Максимум токенов ответа (передаётся в запрос, если задано). |
| `temperature` | Температура (передаётся, если задана). Внимание: в thinking-режиме DeepSeek игнорирует `temperature`/`top_p`/`presence`/`frequency_penalty` (без ошибки). |
| `timeout_secs` | Бюджет тишины, секунды: ожидание заголовков ответа и максимальная пауза между чанками стрима. Это НЕ общий таймаут запроса — длинные ответы рассуждающих моделей не обрываются по суммарному времени. Поднимайте, если модель «думает» дольше без единого байта (в примере конфига у `deepseek-pro` — 300). |
| `context_limit` | Окно контекста модели в токенах. Автоматическая компактификация работает от `min(agent.context_budget_tokens, context_limit)`: пороги L1/L3 (70%/95%) привязаны к реальному пределу API — L3-саммари запускается на ~95% окна модели, не дожидаясь HTTP 413. Без поля — только конфиг-бюджет. |
| `thinking_on` / `thinking_off` | Карты ризонинга: JSON-объект, сливаемый на верхний уровень тела запроса при `/think on` / `/think off` (TUI) или `arch-ml run --think on\|off`. Обе опциональны; без них `/think` для этой модели отвечает отказом-подсказкой. |
| `proxy` | URL прокси для этого провайдера (`http://host:port`, `socks5://…`). Для loopback-прокси (`127.0.0.1`/`localhost`, напр. локальный egress-шлюз vpn-egress на `127.0.0.1:12080`) харнесс при построении клиента и при переключении модели проверяет порт и автоматически поднимает шлюз (`systemctl --user start vpn-egress`, см. `src/net.rs`) — ручной `source env.sh` перед запуском не нужен; недоступность шлюза — предупреждение в лог/статус, запрос идёт дальше. Внешний прокси используется как есть (автозапуск не применим). Без поля — прямое соединение либо прокси из env (`HTTP(S)_PROXY`), как раньше. |
| `price_in_per_1m` / `price_out_per_1m` | Тариф: цена 1M входных (prompt) / выходных (completion) токенов в валюте пользователя (единица не фиксируется — что задано, то и в отчётах). Оба опциональны; используются в `arch-ml metrics --cost-report` и метрике cost-per-outcome. Без тарифа отчёты показывают только токены (выдуманного курса нет). |

Встроенные дефолты (работают без конфига, нужны только ключи). Идентификаторы
сверены с официальной документацией провайдеров (август 2026; GigaChat —
сентябрь 2026, [developers.sber.ru](https://developers.sber.ru/docs/ru/gigachat/guides/main)):

| Имя | Endpoint / модель | Ризонинг | Ключ |
|---|---|---|---|
| `deepseek` | `https://api.deepseek.com/v1`, `deepseek-flash` | `thinking.type` | `DEEPSEEK_API_KEY` |
| `deepseek-pro` | тот же, `deepseek-v4-pro` | `thinking.type` | `DEEPSEEK_API_KEY` |
| `kimi` | `https://api.kimi.com/coding/v1`, `k3` (coding-поверхность Kimi Code, 256k) | встроенный (параметры не принимает) | `KIMI_API_KEY` или файл `~/.kimi_api_key` |
| `glm` | `https://api.z.ai/api/paas/v4` (международная площадка Z.AI; для Китая — `open.bigmodel.cn`, тот же ключ), `glm-5.2` (флагман) | `thinking.type` | `ZHIPU_API_KEY` |
| `glm-4.7` | тот же, `glm-4.7` (дешевле флагмана вдвое) | `thinking.type` | `ZHIPU_API_KEY` |
| `glm-air` | тот же, `glm-4.5-air` (≈$0.14/$0.86 за 1М) | `thinking.type` | `ZHIPU_API_KEY` |
| `glm-flash` | тот же, `glm-4.7-flash` (бесплатный тариф: крон, черновики) | `thinking.type` | `ZHIPU_API_KEY` |
| `glm-5.3-flash` | тот же, `glm-5.3-flash` (окно 1.3M; thinking не отключается — «off» = enabled + `reasoning_effort=low`) | `thinking.type` + `reasoning_effort` | `ZHIPU_API_KEY` |
| `gigachat` | `https://api.giga.chat/v1` (пресет фабрики), `GigaChat-2-Pro` (128K; ADR-021) | нет (в API отсутствует) | `GIGACHAT_API_KEY` (Authorization-key из кабинета Сбера) |
| `gigachat-max` | тот же, `GigaChat-2-Max` (флагман линейки) | нет | `GIGACHAT_API_KEY` |
| `gigachat-ultra` | тот же, `GigaChat-3-Ultra` (только физлица, Freemium; платным тарифам и юрлицам недоступна) | нет | `GIGACHAT_API_KEY` |

Особенности профиля `gigachat*` (ADR-021): авторизация — **OAuth2**, а не
статический ключ: Basic-ключ из `GIGACHAT_API_KEY` идёт на
`https://ngw.devices.sberbank.ru:9443/api/v2/oauth` (заголовок `RqUID`, form
`scope=…`), далее `Bearer` с упреждающим рефрешем (TTL 30 мин). Поле
`oauth.scope` — `GIGACHAT_API_PERS` (дефолт, 1 одновременный поток) или
`GIGACHAT_API_B2B`/`CORP` (юрлица, 10 потоков — важно при параллельных
субагентах). Корпоративный ключ подключается копией любой секции
`gigachat*` под новым именем с заменой `api_key_env` (например, на
`GIGACHAT_API_KEY_CORP`) и `oauth.scope` — личный и корпоративный
Authorization-key живут в пикере одновременно (шаблон в `config.example.toml`).
Ограничение по докам (17.07.2026) касается только Ultra: юрлицам она пока
недоступна. Заголовок `User-Agent` обязателен (без него 403) — ставится
фабрикой. TLS: серверный сертификат выдан цепочкой **НУЦ Минцифры** — задайте
`ca_pem_file` с PEM корня (ослабление проверки запрещено, AD-BE5). Банковский
контур `B2Bank` (домен sbrf.ru) — отдельный mTLS-профиль без OAuth, шаблон в
`config.example.toml`.

Внимание при ручной правке `config.toml`: ключи моделей с точкой квотируются
(`[models."glm-4.7"]`), иначе TOML трактует точку как вложенную таблицу.

Снятые провайдерами идентификаторы (НЕ использовать): `deepseek-chat` и
`deepseek-reasoner` выведены 2026-07-24 (замена — `deepseek-flash` /
`deepseek-v4-pro`, [thinking_mode](https://api-docs.deepseek.com/guides/thinking_mode/));
серия `kimi-k2` снята 2025-05-25 (замена — `kimi-k3`,
[platform.kimi.ai/docs/models](https://platform.kimi.ai/docs/models));
`glm-4.6` → `glm-4.7` ([docs.z.ai](https://docs.z.ai/guides/llm/glm-4.7)).

Проверка: `arch-ml models` — список реестра и модель по умолчанию.
Переключение: `arch-ml run --model <имя>`, в TUI — `/model <имя>` или `/model`
(пикер: список с id моделей и меткой ризонинга, ★ — текущая).

## Ризонинг-режим (thinking)

- `/think on|off|auto` в TUI и `arch-ml run --think on|off` в CLI: в тело
  запроса сливается карта `thinking_on`/`thinking_off` активной модели.
  `auto` (дефолт) — ничего не шлётся, действует дефолт провайдера
  (у DeepSeek V4 ризонинг включён по умолчанию, effort `high`; у Kimi K3 —
  `reasoning_effort: max`; выключить ризонинг у K3 нельзя, только ослабить
  до `low`).
- Индикатор в статус-баре TUI: `🧠` у бейджа модели — on, `🧠off` — off.
- Цепочка рассуждений приходит полем `reasoning_content`: харнесс хранит её
  в истории и **возвращает в API** эхом — это обязательное требование DeepSeek
  для thinking-запросов с инструментами (иначе HTTP 400). В чате CoT не
  отображается и не смешивается с текстом ответа; в журнал сессии пишется
  (аудит цепочек рассуждений).
- Проверено живыми прогонами (2026-08-15): DeepSeek `on` → CoT пришёл (256 зн),
  `off` → без CoT, `auto` → CoT есть (дефолт провайдера «включён», как в доках);
  GLM `on` → CoT пришёл, `off` → нет. Kimi — без живой проверки (нет ключа):
  форма тела покрыта юнит-тестами.
- **GLM-4.7 quirk** (пойман прокси-ловушкой): с thinking=on на коротких
  ответах модель недетерминированно (~2/3 случаев на тривиальном промпте)
  кладёт весь ответ в `reasoning_content`, а `content` оставляет пустым.
  Харнесс нормализует это на границе API (`normalize_reply` в
  `openai_compat.rs`): пустой ответ без tool_calls подменяется цепочкой
  рассуждений. Если ответ GLM выглядит как голый CoT — это тот случай.

## Сетевые замечания (корпоративный/домашний контур РФ)

- **Kimi — coding-поверхность** `https://api.kimi.com/coding/v1` (модели
  `k3`/`k3-256k`): работает свободно, ключ — подписка Kimi Code
  (env `KIMI_API_KEY` или файл `~/.kimi_api_key`). Старая `/v1` → 404.
  Проверено живьём 2026-08-15: базовый ответ, function calling, CoT-эхо.
- **`api.moonshot.ai` душится DPI по SNI** (флапает: GET /models проскакивает,
  POST /chat/completions режется reset/timeout) — платформа недоступна без
  VPN; ключ платформы (другой файл ключа, не `~/.kimi_api_key`) к coding-поверхности
  не подходит (401). Это РАЗНЫЕ ключи и поверхности.
- **`api.deepseek.com`** (CloudFront) и **`api.kimi.com`** работают без VPN.
- **Локальные прокси с обрезкой тел запросов** (если поднимали такой для
  других инструментов) ломают продакшн-промпты харнесса: системный промпт +
  контекст + инструменты легко превышают лимит обрезки. Не прописывайте
  прокси в `base_url` для рабочих сценариев.
- Ретраи по матрице классов ошибок (`src/retry.rs`): 429 — до 8 попыток,
  5xx — до 5, транспортные сбои — до 5, с экспоненциальным backoff'ом и
  джиттером; 4xx/413/auth не ретраятся (413 ведёт на compact & resubmit).
  Обрыв SSE-стрима (молчание модели дольше `timeout_secs`, reset сетью)
  ретраится на любой фазе: до первой дельты — молча, после частичного
  контента — с заметкой «повторяю запрос…» в чате (показанный фрагмент
  остаётся, собранный ответ и журнал содержат только успешную попытку;
  дублей вызовов инструментов нет — они исполняются после полной сборки).
  Постоянные сбои — повышайте `timeout_secs` и проверяйте маршрут до endpoint'а.

## Свой OpenAI-совместимый endpoint

Код писать не нужно — добавьте секцию с любым именем:

```toml
[models.local-qwen]
base_url = "http://127.0.0.1:8080/v1"   # llama.cpp server, vLLM, ollama-compat...
model = "qwen3-local"
api_key_env = "LOCAL_LLM_KEY"           # переменная обязана существовать;
                                        # для безключевого сервера задайте любое значение
timeout_secs = 300
```

`LlmRegistry::build` (`src/llm.rs`) маршрутизирует по префиксу имени:
`deepseek*`/`kimi*`/`glm*` → фабрики-пресеты `src/llm/{deepseek,kimi,glm}.rs`;
любое другое имя → `openai_compat::generic_provider`. Требования к endpoint'у:
OpenAI-совместимый чат-комплишн с `tools`/`tool_calls`; стриминг — SSE
`data:`-строки (без стриминга работает нестриминговый путь, `stream = false`
в `[agent]` или дефолтная реализация `LlmProvider::stream`).

Нестандартный транспорт: своя фабрика по образцу `src/llm/deepseek.rs`
(возвращает `Arc<dyn LlmProvider>`) + ветка в `LlmRegistry::build`.
Контракт — `docs/architecture.md` («Контракты»).
