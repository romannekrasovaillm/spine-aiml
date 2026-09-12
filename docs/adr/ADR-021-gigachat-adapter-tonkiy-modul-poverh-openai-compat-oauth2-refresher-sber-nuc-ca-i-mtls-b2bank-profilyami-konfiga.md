# ADR-021. GigaChat-адаптер: тонкий модуль поверх openai_compat, OAuth2-рефрешер Sber, НУЦ CA и mTLS B2Bank профилями конфига

- Date: 2026-09-01
- Status: Accepted

> Реализовано траншем 2026-09-02 (коммиты 7d9fbf0, 8c3bec4): гейт A4 —
> fitness PASS (12/12), fmt/clippy/test (633+) /release зелёные.
> Открытый эксплуатационный вопрос (не блокирует): подтвердить у
> владельца инсталляции формат Basic-ключа — значение из
> `api_key_env`/`api_key_file` используется как есть после `Basic `
> (по фактуре банкноты; base64-шаг не добавляется).

## Context

Модельная матрица ADR-012: GigaChat — разрешённый внешний вендор
(плюс self-hosted open-source внутри периметра; YandexGPT исключён).
Ядро уже имеет `openai_compat` (src/llm/openai_compat.rs) с тонкими
фабриками по префиксу имени модели (deepseek/kimi/glm, ветка в
`Providers::build`). GigaChat частично OpenAI-совместим, но фактура
(верифицировано субагентом 2026-09-01, `banking/notes/
gigachat-api-sa-20260901.md`, живые пробы + OpenAPI gigachat/api.yml):

1. **OAuth2, не статический ключ**: `POST
   https://ngw.devices.sberbank.ru:9443/api/v2/oauth`, заголовки
   `RqUID` (uuid4) + `Authorization: Basic <ключ>`, body
   `scope=GIGACHAT_API_PERS|B2B|CORP`; ответ `{access_token,
   expires_at}`, TTL **30 мин**, ≤10 запросов/сек; далее Bearer.
2. Base URL `https://api.giga.chat` (единый с 17.07.2026); **обязателен
   User-Agent** (иначе 403); `/v1/chat/completions` (v1) и `/v2/…`
   (tools/tool_config в v2; functions — легаси v1); SSE-стрим,
   `data: [DONE]`; reasoning/thinking в API **нет**.
3. **TLS**: корневой сертификат НУЦ Минцифры (Russian Trusted Root
   CA) — кастомный root store (rustls), НЕ verify=false (AD-BE5);
   это не ГОСТ-TLS — GAP-C2 в части GigaChat закрывается CA-бандлом.
4. **B2Bank** (банковский контур, домен sbrf.ru): mTLS клиентским
   сертификатом, без OAuth; та же /v1/chat/completions.
5. Модели GigaChat-2 (Lite/Pro/Max), 3-Ultra; контекст 128K.
6. Лимиты: 1 поток (ФЛ) / 10 (ЮЛ) — иначе 429.

Significance: Standard (new_component + new_vendor). Код — в MIT-ядре
(src/, доступность модели — продуктовая функция, не банковская
тайна); банк-контур управляется конфигом (AD-BE2: endpoint'ы задаёт
банк-профиль). Код пишет кодовый харнесс (handoff → harness_run).

## Decision

1. **Тонкий модуль `src/llm/gigachat.rs`** по образцу deepseek/kimi:
   ветка `n.starts_with("gigachat")` в `Providers::build` →
   фабрика поверх `OpenAiCompat::with_preset` (base_url
   `https://api.giga.chat/v1`, context_limit 128K, без
   thinking_on — reasoning в API нет).
2. **OAuth2-рефрешер внутри модуля**: получение токена (RqUID uuid4,
   Basic-ключ из `api_key_env`/`api_key_file`, scope из конфига),
   кэш с упреждением (рефреш за 60 с до expires_at, TTL 30 мин),
   retry с backoff на 429 (общий retry.rs), токен — только в
   памяти (Debug-маскирование как у OpenAiCompat).
3. **Расширение `ModelConfig`** (generic-поля, без GigaChat-специфики
   в имени): `user_agent: Option<String>`, `ca_pem_file:
   Option<String>` (добавить в rustls root store), `client_cert_file`
   + `client_key_file` (mTLS-профиль B2Bank: без OAuth, ключ не
   требуется), `oauth: Option<OAuthConfig{token_url, scope}>`.
   Дефолт профиля gigachat: oauth на ngw.devices.sberbank.ru,
   scope задаётся явно (PERS/B2B/CORP — выбор владельца инсталляции).
4. **User-Agent обязателен**: дефолт `spine-arch/<version>` в
   заголовках всех запросов (фактура: без него 403).
5. **B2Bank-профиль** = конфиг-вариант модели gigachat: base_url
   IFT-стенда, client_cert_file/client_key_file, oauth: None.
   On-prem/airgap в публичных доках отсутствует [НЕ ВЕРИФИЦИРОВАНО] —
   если появится, это ещё один профиль того же адаптера.
6. **Тесты**: unit (маппинг OAuth-ответа, упреждение рефреша,
   Debug-маскирование) + mock-сервер (wiremock/httpmock): 403 без
   User-Agent, 401→рефреш→retry, SSE-стрим, [DONE]-терминатор.
   Живые пробы GigaChat в CI не заводить (секреты, external).

## Alternatives Considered

| Вариант | Плюсы | Минусы |
|---------|-------|-------|
| Универсальный OAuth-хук в generic_provider (без отдельного модуля) | меньше файлов | RqUID/Basic/scope-специфика Sber всё равно ветвится по вендору; теряется образец «тонкая фабрика»; отклонено |
| Внешний OAuth→static-key прокси-сервис в контуре банка | ядро не меняется вообще | новый поставляемый компонент (эксплуатация, hardening, H-трек) ради пары заголовков; секрет Basic-ключа теперь в прокси; отклонено |
| Ждать полноценного on-prem GigaChat [НЕ ВЕРИФИЦИРОВАНО] | ноль работы сейчас | модельная матрица ADR-012 без единого работающего внешнего вендора; B2Bank уже покрывает контур Сбера; отклонено |
| **Принято: тонкий модуль + generic-поля конфига** | ложится в существующую архитектуру; mTLS/CA переиспользуются другими вендорами; код — один транш харнесса | OAuth-состояние в адаптере (кэш токена) — новая движущаяся часть |

## Consequences

### Positive

- Модельная матрица ADR-012 получает первого внешнего вендора
  (walk-the-talk демо), B2Bank-профиль — вариант «внутри периметра
  Сбера» без self-hosted GPU.
- ca_pem_file/client_cert_file — generic: годятся для любого
  вендора с кастомным CA/mTLS (не только Sber).
- Разведка уже верифицирована живыми пробами (openssl-цепочки,
  GET /v1/models) — фактура для харнесса готова.

### Negative

- Кэш токена с TTL 30 мин — движущаяся часть: гонки при параллельных
  запросах (митигация: мьютекс на рефреш, один рефрешер).
- НУЦ-сертификат — файл в инсталляции (обновление раз в N лет,
  владелец — эксплуатация; проверка загрузки при старте с понятной
  ошибкой).
- Лимит 1 поток (ФЛ-ключи) — харнесс с параллельными субагентами
  упрётся; scope/scope-выбор — за владельцем инсталляции, в доке
  адаптера явно предупредить.
- 429/403-поведение в mock-тестах — модель поведения, живое
  поведение может отличаться [частично НЕ ВЕРИФИЦИРОВАНО].

## Reversibility

reversible — новый модуль + optional-поля конфига; откат = удалить
ветку build() и модуль, конфиги без gigachat не меняются.

## References

- ADR-012 (модельная матрица), ADR-016 (банк-профиль, egress),
  AD-BE2/AD-BE5 (ARCHITECTURE-SPINE-BE.md)
- `banking/notes/gigachat-api-sa-20260901.md` (верифицированная
  фактура; разделы [НЕ ВЕРИФИЦИРОВАНО] перепроверить при пилоте)
- `src/llm.rs` (Providers::build), `src/llm/openai_compat.rs`,
  `src/llm/{deepseek,kimi,glm}.rs` (образцы), `src/config.rs`
  (ModelConfig), `src/retry.rs`
- Handoff-пакет `.arch-handoff/` (задача кодовому харнессу)
