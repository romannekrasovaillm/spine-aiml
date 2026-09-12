//! Провайдер `GigaChat` (Sber): OpenAI-совместимый `/v1/chat/completions`
//! поверх [`OpenAiCompat`] + OAuth2-рефрешер. ADR-021; фактура верифицирована
//! живыми пробами, `banking/notes/gigachat-api-sa-20260901.md`.
//!
//! Отличия от deepseek/kimi/glm (тонкие фабрики со статическим ключом):
//! - **`OAuth2`, не статический ключ**: `POST {oauth.token_url}` (дефолт
//!   `https://ngw.devices.sberbank.ru:9443/api/v2/oauth`) с заголовками
//!   `RqUID: <uuid4>` и `Authorization: Basic <ключ>` (ключ — из
//!   `api_key_env`/`api_key_file`, как у остальных провайдеров), телом
//!   `scope=…`; в ответ `{access_token, expires_at}`, TTL 30 минут.
//!   Токен кэшируется в памяти, обновляется упреждающе (за 60 с до
//!   истечения) и ровно один раз после 401 со стороны API; лимит
//!   OAuth-эндпоинта ≤10 req/s — 429 повторяется с backoff (общий
//!   [`crate::retry`]). Токен и ключ в Debug/логи не попадают.
//! - **User-Agent обязателен** (без него API отвечает 403): дефолт
//!   `spine-arch/<версия харнесса>` на все запросы (переопределяется
//!   полем `user_agent` конфига модели).
//! - **Reasoning/thinking в API нет** — карты ризонинга не задаются;
//!   контекст моделей GigaChat-2/3 — 128K (пресет `context_limit`).
//! - **TLS**: корень НУЦ Минцифры — поле `ca_pem_file` (добавляется в
//!   rustls root store; НЕ `verify=false` — AD-BE5); банковский контур
//!   `B2Bank` — mTLS парой `client_cert_file`/`client_key_file`.
//!
//! Профили конфигурации (`[models.<имя>]`, имя с префиксом `gigachat`):
//! - публичный контур: `oauth = { scope = "GIGACHAT_API_PERS" }` (или
//!   B2B/CORP — выбор владельца инсталляции; от scope зависят лимиты
//!   одновременных потоков: 1 физлица / 10 юрлица — при параллельных
//!   субагентах упрётесь в 429), `token_url` можно опустить;
//! - банковский контур `B2Bank` (домен sbrf.ru): `base_url` стенда
//!   инсталляции + `client_cert_file`/`client_key_file`, `oauth`
//!   отсутствует — заголовок `Authorization` не шлётся вовсе
//!   (клиента аутентифицирует сертификат).
//!
//! Окно контекста из конфига `[models.<имя>].context_limit` ядро читает
//! при компактификации; фабричный пресет 128000 применяется к копии
//! конфигурации модели, когда инсталляция значение не задала.

use std::fmt;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde::Deserialize;
use tokio::sync::Mutex;

use crate::config::{ModelConfig, OAuthConfig};
use crate::error::{HarnessError, Result};
use crate::llm::LlmProvider;
use crate::llm::openai_compat::{
    AuthSource, OpenAiCompat, build_client, error_chain, next_delay, resolve_api_key,
    retry_after_delay,
};

/// Базовый URL `GigaChat` API (единый с 17.07.2026; применяется, когда
/// `base_url` в конфиге пуст).
const DEFAULT_BASE_URL: &str = "https://api.giga.chat/v1";

/// URL OAuth-эндпоинта Sber по умолчанию: подставляется фабрикой, когда
/// `oauth.token_url` в конфиге пуст (в самом конфиге дефолт не живёт).
const DEFAULT_TOKEN_URL: &str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth";

/// Окно контекста моделей GigaChat-2/3 (пресет `context_limit`).
const DEFAULT_CONTEXT_LIMIT: usize = 128_000;

/// Дефолтный `User-Agent` всех запросов адаптера (API отвечает 403 без него).
const DEFAULT_USER_AGENT: &str = concat!("spine-arch/", env!("CARGO_PKG_VERSION"));

/// Упреждающий рефреш: токен считается протухшим, если до `expires_at`
/// осталось меньше 60 секунд (TTL токена — 30 минут, запас ничтожен).
const REFRESH_SKEW_SECS: u64 = 60;

/// Бюджет ожидания заголовков OAuth-эндпоинта, секунды.
const TOKEN_TIMEOUT_SECS: u64 = 60;

/// Общий дедлайн всего цикла рефреша (попытки + ретраи + backoff), секунды.
/// Без него мьютекс токен-кэша держался бы весь цикл до 8 попыток ×
/// (таймаут + backoff) ≈ 4–8 минут — head-of-line для всех chat-вызовов
/// (ADR-021, M1). 120 с с запасом покрывает терпеливый backoff живого
/// эндпоинта и обрезает мёртвый.
const FETCH_DEADLINE_SECS: u64 = 120;

/// Circuit breaker OAuth-рефрешера: после скольких подряд неудачных циклов
/// рефреша открывается быстрый отказ. При стабильном ауте OAuth без него
/// каждый chat-вызов гонял бы полный цикл до 8 попыток (ADR-021, M2).
const BREAKER_FAIL_THRESHOLD: u32 = 3;

/// На сколько секунд circuit breaker открывается после
/// [`BREAKER_FAIL_THRESHOLD`] подряд неудачных циклов: запросы токена
/// не шлются вовсе, ошибка — мгновенная (ADR-021, M2).
const BREAKER_OPEN_SECS: u64 = 30;

/// Минимальный TTL кэшированного токена, секунды: `expires_at` из ответа
/// клампится к `now + MIN_TTL_SECS`. Серверный 0 или время в прошлом не
/// должны делать токен «вечно протухшим» — иначе OAuth на каждый вызов
/// (ADR-021, L3). Реальный серверный срок не удлиняется.
const MIN_TTL_SECS: u64 = 300;

/// Фабрика провайдера `GigaChat` поверх [`OpenAiCompat`] (тонкий модуль,
/// ветка `gigachat` в реестре). Пресеты профиля применяются, когда конфиг
/// инсталляции поле не задал: `base_url` — [`DEFAULT_BASE_URL`] (в
/// `with_preset`), `context_limit` — 128K, `user_agent` —
/// `spine-arch/<версия>`, `oauth.token_url` — Sber по умолчанию.
/// Basic-ключ OAuth не проверяется — читается лениво на первом запросе.
///
/// # Errors
/// Пустой `model`, ошибка сборки HTTP-клиента: нечитаемый/битый
/// `ca_pem_file`, полупара `client_cert_file`/`client_key_file`.
pub fn provider(name: &str, cfg: &ModelConfig) -> Result<Arc<dyn LlmProvider>> {
    let cfg = with_defaults(cfg);
    let auth = build_auth_source(name, &cfg)?;
    Ok(Arc::new(OpenAiCompat::with_preset_and_auth(
        name,
        &cfg,
        DEFAULT_BASE_URL,
        Some(auth),
    )?))
}

/// Эффективная конфигурация модели: пресеты профиля `GigaChat` применяются
/// к копии, когда конфиг инсталляции поле не задал (старые/минимальные
/// конфиги не ломаются — все новые поля `Option`).
fn with_defaults(cfg: &ModelConfig) -> ModelConfig {
    let mut cfg = cfg.clone();
    if cfg.context_limit.is_none() {
        cfg.context_limit = Some(DEFAULT_CONTEXT_LIMIT);
    }
    let need_default_ua = match cfg.user_agent.as_deref() {
        Some(raw) => raw.trim().is_empty(),
        None => true,
    };
    if need_default_ua {
        cfg.user_agent = Some(DEFAULT_USER_AGENT.to_string());
    }
    if let Some(oauth) = &mut cfg.oauth {
        if oauth.token_url.trim().is_empty() {
            oauth.token_url = DEFAULT_TOKEN_URL.to_string();
        }
    }
    cfg
}

/// Источник `Authorization` по профилю конфигурации:
/// - `oauth` задан — OAuth2-рефрешер (публичный контур Sber);
/// - иначе — [`NoAuth`]: mTLS-профиль `B2Bank` аутентифицирует клиента
///   сертификатом, заголовок не нужен. Статический API-ключ `GigaChat`
///   не использует никогда (фактура: `OAuth2`, не static-key).
fn build_auth_source(name: &str, cfg: &ModelConfig) -> Result<Arc<dyn AuthSource>> {
    if let Some(oauth) = &cfg.oauth {
        return Ok(Arc::new(OAuthRefresher::new(name, cfg, oauth)?));
    }
    // Сертификаты (если заданы) уже загружены в клиент — fail-fast в build_client.
    Ok(Arc::new(NoAuth))
}

/// Кэшированный токен OAuth (живёт только в памяти; в Debug не выводится).
#[derive(Debug)]
struct Token {
    /// Bearer-токен.
    access_token: String,
    /// Момент истечения (unix-секунды) из ответа OAuth (клампирован
    /// [`clamp_min_ttl`] — ADR-021, L3).
    expires_at: u64,
}

/// Состояние токен-кэша и circuit breaker'а под одним мьютексом: рефреш —
/// single-flight, и счётчик неудач, и окно breaker'а меняются в той же
/// критической секции, гонок «сброс после успеха» ↔ «открыть после серии
/// сбоев» нет. В Debug не выводится (токен — секрет).
struct CacheState {
    /// Кэшированный токен.
    token: Option<Token>,
    /// Подряд неудачных циклов рефреша; сбрасывается первым успехом
    /// (ADR-021, M2).
    failures: u32,
    /// Момент, до которого circuit breaker открыт (`None` — закрыт).
    open_until: Option<Instant>,
}

/// Ответ OAuth-эндпоинта Sber: `{access_token, expires_at}` (unix-секунды).
#[derive(Debug, Deserialize)]
struct TokenResponse {
    /// Bearer-токен для вызовов API.
    access_token: String,
    /// Момент истечения токена, unix-секунды.
    expires_at: u64,
}

/// OAuth2-рефрешер `GigaChat`: Basic-ключ (env/файл) → Bearer-токен.
///
/// Токен кэшируется под мьютексом; рефреш идёт под тем же локом —
/// параллельные запросы не дублируют OAuth-вызовы (митигация гонок
/// ADR-021). 429/5xx/транспортные сбои OAuth-эндпоинта повторяются
/// с backoff'ом по матрице [`crate::retry`] (с учётом `Retry-After`, M3);
/// ошибки ключа (4xx) — наверх. Весь цикл рефреша ограничен общим
/// дедлайном [`FETCH_DEADLINE_SECS`] (M1), а при стабильном отказе
/// открывается circuit breaker — быстрый отказ без новых попыток (M2).
struct OAuthRefresher {
    /// Имя провайдера из реестра (для текстов ошибок).
    name: String,
    /// URL OAuth-эндпоинта (дефолт Sber подставлен фабрикой).
    token_url: String,
    /// Запрашиваемый scope (`GIGACHAT_API_PERS|B2B|CORP`).
    scope: String,
    /// Имя переменной окружения с Basic-ключом (читается лениво).
    api_key_env: String,
    /// Запасной файл с Basic-ключом (`~` раскрывается).
    api_key_file: Option<String>,
    /// HTTP-клиент OAuth-запросов (те же TLS-опции, что у API-клиента:
    /// НУЦ CA / mTLS задаются полями конфига модели).
    client: reqwest::Client,
    /// Кэш токена + circuit breaker под мьютексом (рефреш — single-flight).
    cache: Mutex<CacheState>,
}

// Токен и Basic-ключ в Debug сознательно не включаем — это секреты.
#[expect(
    clippy::missing_fields_in_debug,
    reason = "api_key_env/cache — секреты, в Debug не выводятся"
)]
impl fmt::Debug for OAuthRefresher {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("OAuthRefresher")
            .field("name", &self.name)
            .field("token_url", &self.token_url)
            .field("scope", &self.scope)
            .finish()
    }
}

impl OAuthRefresher {
    /// Создаёт рефрешер из конфигурации модели и oauth-блока.
    /// Ключ не проверяется — читается лениво на первом запросе токена.
    ///
    /// # Errors
    /// Ошибка сборки HTTP-клиента (битый `ca_pem_file` и т.п.).
    fn new(name: &str, cfg: &ModelConfig, oauth: &OAuthConfig) -> Result<Self> {
        let token_url = oauth.token_url.trim();
        if token_url.is_empty() {
            return Err(HarnessError::Llm(format!(
                "провайдер '{name}': пустой oauth.token_url"
            )));
        }
        Ok(Self {
            name: name.to_string(),
            token_url: token_url.to_string(),
            scope: oauth.scope.clone(),
            api_key_env: cfg.api_key_env.clone(),
            api_key_file: cfg.api_key_file.clone(),
            client: build_client(name, cfg)?,
            cache: Mutex::new(CacheState {
                token: None,
                failures: 0,
                open_until: None,
            }),
        })
    }

    /// Bearer-токен для API-вызова: свежий из кэша либо полученный по OAuth.
    /// Упреждающий рефреш: за [`REFRESH_SKEW_SECS`] до истечения токен
    /// считается протухшим. Рефреш идёт под локом кэша — параллельные
    /// вызовы ждут и забирают один свежий токен. Circuit breaker (M2):
    /// после [`BREAKER_FAIL_THRESHOLD`] подряд неудачных циклов рефреш не
    /// пробуется вовсе — мгновенная ошибка, пока не истечёт окно.
    ///
    /// # Errors
    /// OAuth-эндпоинт отказал (после исчерпания ретраев/дедлайна), breaker
    /// открыт или ключ не задан.
    async fn access_token(&self) -> Result<String> {
        let mut cache = self.cache.lock().await;
        if let Some(token) = cache
            .token
            .as_ref()
            .filter(|token| is_fresh(token.expires_at, unix_now()))
        {
            return Ok(token.access_token.clone());
        }
        if let Some(until) = cache.open_until {
            if until > Instant::now() {
                return Err(breaker_error(&self.name, &self.token_url));
            }
            // Окно быстрого отказа истекло — пробуем снова.
            cache.open_until = None;
        }
        match self.fetch_token().await {
            Ok(token) => {
                // Первый успех сбрасывает счётчик breaker'а (ADR-021, M2).
                cache.failures = 0;
                let access_token = token.access_token.clone();
                cache.token = Some(token);
                Ok(access_token)
            }
            Err(err) => {
                cache.token = None;
                // Считаем любой неудачный цикл — и исчерпавшие ретраи, и
                // мгновенные 4xx, и дедлайн: при стабильном отказе breaker
                // открывается быстрее (ADR-021, M2).
                cache.failures = cache.failures.saturating_add(1);
                if cache.failures >= BREAKER_FAIL_THRESHOLD {
                    cache.open_until =
                        Some(Instant::now() + Duration::from_secs(BREAKER_OPEN_SECS));
                }
                Err(err)
            }
        }
    }

    /// POST на OAuth-эндпоинт: Basic-ключ (env/файл, лениво), `RqUID`
    /// uuid4, form-urlencoded `scope`. Ключ в текст ошибки не попадает.
    ///
    /// # Errors
    /// Ключ не задан; эндпоинт не прислал заголовки за
    /// [`TOKEN_TIMEOUT_SECS`]; транспортная ошибка.
    async fn post_oauth(&self) -> Result<reqwest::Response> {
        let key = resolve_api_key(&self.name, &self.api_key_env, &self.api_key_file)?;
        let send = self
            .client
            .post(&self.token_url)
            .header(reqwest::header::AUTHORIZATION, format!("Basic {key}"))
            .header("RqUID", uuid_v4())
            .header(
                reqwest::header::CONTENT_TYPE,
                "application/x-www-form-urlencoded",
            )
            .body(format!("scope={}", self.scope))
            .send();
        match tokio::time::timeout(Duration::from_secs(TOKEN_TIMEOUT_SECS), send).await {
            Ok(res) => res.map_err(HarnessError::Http),
            Err(_) => Err(HarnessError::Llm(format!(
                "{}: OAuth-эндпоинт {} не прислал заголовки за {TOKEN_TIMEOUT_SECS}с \
                 (перегрузка или сеть)",
                self.name, self.token_url
            ))),
        }
    }

    /// Получает свежий токен с OAuth-эндпоинта. 429/5xx/транспорт — ретрай
    /// с backoff'ом по матрице [`crate::retry`] (429 на OAuth — лимит
    /// ≤10 req/s); 4xx (Auth/BadRequest) и исчерпание попыток — наверх
    /// с кодом и выдержкой тела. Весь цикл ограничен общим дедлайном
    /// [`FETCH_DEADLINE_SECS`] (ADR-021, M1) — см. [`Self::fetch_token_loop`].
    ///
    /// # Errors
    /// Эндпоинт стабильно отказывает; цикл не уложился в дедлайн; ошибка
    /// разбора успешного ответа.
    async fn fetch_token(&self) -> Result<Token> {
        self.fetch_token_with_deadline(Duration::from_secs(FETCH_DEADLINE_SECS))
            .await
    }

    /// Цикл рефреша под общим дедлайном `deadline`: попытки, ретраи и backoff
    /// укладываются в него целиком. Дедлайн обрезает «мёртвый» эндпоинт,
    /// чтобы мьютекс токен-кэша не держал head-of-line для всех chat-вызовов
    /// (ADR-021, M1); в тестах дедлайн инъецируется коротким.
    ///
    /// # Errors
    /// Цикл не уложился в `deadline` (ошибка дедлайна), либо исходная
    /// ошибка [`Self::fetch_token_loop`].
    async fn fetch_token_with_deadline(&self, deadline: Duration) -> Result<Token> {
        match tokio::time::timeout(deadline, self.fetch_token_loop()).await {
            Ok(res) => res,
            Err(_elapsed) => Err(HarnessError::Llm(format!(
                "{}: OAuth-эндпоинт {}: цикл рефреша не уложился в общий дедлайн {}с \
                 (эндпоинт не отвечает или retry-цепочка не сходится)",
                self.name,
                self.token_url,
                deadline.as_secs()
            ))),
        }
    }

    /// Цикл ретраев рефреша (без внешнего дедлайна; вызывается только
    /// из [`Self::fetch_token_with_deadline`]).
    ///
    /// # Errors
    /// Эндпоинт стабильно отказывает; ошибка разбора успешного ответа.
    async fn fetch_token_loop(&self) -> Result<Token> {
        let seed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| u64::from(d.subsec_nanos()));
        let mut delays: Option<(crate::retry::ErrorKind, crate::retry::Delays)> = None;
        loop {
            match self.post_oauth().await {
                Ok(resp) if resp.status().is_success() => {
                    let parsed: TokenResponse = resp.json().await?;
                    return Ok(Token {
                        access_token: parsed.access_token,
                        // L3: 0/прошлое от сервера клампится — иначе токен
                        // «вечно протухший» и OAuth на каждый вызов.
                        expires_at: clamp_min_ttl(parsed.expires_at, unix_now()),
                    });
                }
                Ok(resp) => {
                    let status = resp.status();
                    // Retry-After читаем до потребления тела: resp.text()
                    // забирает response (ADR-021, M3).
                    let retry_after = retry_after_delay(resp.headers());
                    let body = resp.text().await.unwrap_or_default();
                    let kind = crate::retry::classify(Some(status.as_u16()), &body);
                    match crate::retry::RetryPolicy::for_kind(kind) {
                        None => return Err(oauth_http_error(&self.name, status, &body)),
                        Some(policy) => match next_delay(&mut delays, kind, policy, seed) {
                            Some(delay) => {
                                // Пауза — максимум из вычисленного backoff'а
                                // и указания сервера (ADR-021, M3).
                                let pause = retry_after.map_or(delay, |server| server.max(delay));
                                tokio::time::sleep(pause).await;
                            }
                            None => return Err(oauth_http_error(&self.name, status, &body)),
                        },
                    }
                }
                Err(err) => {
                    // Классификация по полной цепочке причин: причина сбоя —
                    // в source-цепочке reqwest, не в верхнем сообщении.
                    let text = match &err {
                        HarnessError::Http(e) => error_chain(e),
                        other => other.to_string(),
                    };
                    let kind = crate::retry::classify(None, &text);
                    match crate::retry::RetryPolicy::for_kind(kind) {
                        None => return Err(err),
                        Some(policy) => match next_delay(&mut delays, kind, policy, seed) {
                            Some(delay) => tokio::time::sleep(delay).await,
                            None => return Err(err),
                        },
                    }
                }
            }
        }
    }
}

#[async_trait]
impl AuthSource for OAuthRefresher {
    async fn authorization(&self) -> Result<Option<String>> {
        Ok(Some(format!("Bearer {}", self.access_token().await?)))
    }

    fn invalidate(&self) {
        // Сбрасываем кэш, не дожидаясь захвата: если параллельный рефреш
        // уже идёт, он положит свежий токен — сбрасывать нечего (401 был
        // от старого, который как раз заменяется). Счётчик breaker'а и окно
        // не трогаем: 401-рефреш при мёртвом OAuth — тоже неудачный цикл.
        if let Ok(mut cache) = self.cache.try_lock() {
            cache.token = None;
        }
    }
}

/// Источник без заголовка `Authorization`: mTLS-профиль `B2Bank` — сервер
/// аутентифицирует клиентский сертификат, статический ключ не требуется.
#[derive(Debug)]
struct NoAuth;

#[async_trait]
impl AuthSource for NoAuth {
    async fn authorization(&self) -> Result<Option<String>> {
        Ok(None)
    }

    fn invalidate(&self) {}
}

/// Токен свеж, если до `expires_at` осталось не меньше [`REFRESH_SKEW_SECS`]
/// секунд (упреждающий рефреш за 60 с до истечения). `now` передаётся
/// параметром — функция чистая, тестируется без часов.
#[must_use]
fn is_fresh(expires_at: u64, now: u64) -> bool {
    expires_at.saturating_sub(now) >= REFRESH_SKEW_SECS
}

/// Кламп `expires_at` из ответа OAuth (ADR-021, L3): серверный 0 или время
/// в прошлом не должны делать токен «вечно протухшим» (рефреш на каждый
/// вызов) — минимум `now + MIN_TTL_SECS`. Реальный серверный срок не
/// укорачивается. `now` передаётся параметром — функция чистая.
#[must_use]
fn clamp_min_ttl(expires_at: u64, now: u64) -> u64 {
    expires_at.max(now.saturating_add(MIN_TTL_SECS))
}

/// Ошибка быстрого отказа по открытому circuit breaker'у (ADR-021, M2).
/// Класс — как у прочих OAuth-ошибок ([`HarnessError::Llm`]); текст содержит
/// маркер `circuit breaker`, по которому [`crate::retry::classify`] не
/// ретраит отказ поверх (маркер — служебный, в сообщении он на месте).
fn breaker_error(name: &str, token_url: &str) -> HarnessError {
    HarnessError::Llm(format!(
        "{name}: OAuth-эндпоинт {token_url}: circuit breaker открыт \
         ({BREAKER_FAIL_THRESHOLD} подряд неудачных рефрешей) — быстрый отказ на {BREAKER_OPEN_SECS}с; повторите позже"
    ))
}

/// Текущее время в unix-секундах (0 при ошибке часов — токен обновится).
#[must_use]
fn unix_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

/// Случайный UUID v4 для заголовка `RqUID` (Sber требует uuid4; это
/// request-id заголовок — криптостойкость не нужна). Энтропия: ключи
/// [`std::collections::hash_map::RandomState`] (зернятся из ОС), время
/// и счётчик вызовов; формат — RFC 4122, version 4, variant 10.
#[must_use]
fn uuid_v4() -> String {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};

    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos() as u64);
    let counter = COUNTER.fetch_add(1, Ordering::Relaxed);
    let state = RandomState::new();
    let mut high = state.build_hasher();
    high.write_u64(stamp);
    let mut low = state.build_hasher();
    low.write_u64(counter);
    low.write_u64(u64::from(std::process::id()));
    // RFC 4122: version 4 — в старших 4 битах time_hi; variant 10xx — в clock_seq.
    let hi = (high.finish() & 0xFFFF_FFFF_FFFF_0FFF) | 0x0000_0000_0000_4000;
    let lo = (low.finish() & 0x3FFF_FFFF_FFFF_FFFF) | 0x8000_0000_0000_0000;
    format!(
        "{:08x}-{:04x}-{:04x}-{:04x}-{:012x}",
        (hi >> 32) as u32,
        ((hi >> 16) & 0xFFFF) as u16,
        (hi & 0xFFFF) as u16,
        ((lo >> 48) & 0xFFFF) as u16,
        lo & 0xFFFF_FFFF_FFFF
    )
}

/// Ошибка HTTP-статуса OAuth-эндпоинта: код + первые 300 символов тела
/// (тело ошибок Sber секретов не содержит).
fn oauth_http_error(name: &str, status: reqwest::StatusCode, body: &str) -> HarnessError {
    let excerpt: String = body.trim().chars().take(300).collect();
    HarnessError::Llm(format!("{name}: OAuth {status}: {excerpt}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::{ChatMessage, ChatRequest, LlmEvent};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::sync::mpsc;

    /// Тестовая пара PEM (самоподписанный EC P-256 корень + PKCS#8-ключ):
    /// нужна, чтобы mTLS-идентичность реально собралась в `read_client_identity`
    /// и CA-файл разобрался (без внешних openssl-зависимостей в тестах).
    /// Срок действия — до 2036 года. Ключ фиктивный, только для тестов.
    const TEST_CERT_PEM: &str = "-----BEGIN CERTIFICATE-----\n\
MIIBVjCB/aADAgECAhQVJBvK6R0TMVLbfDD6dmEcnMQB4DAKBggqhkjOPQQDAjAh\n\
MR8wHQYDVQQDDBZhcmNoLWhhcm5lc3MtbXRscy10ZXN0MB4XDTI2MDkwMTA4MDY0\n\
M1oXDTM2MDgzMDA4MDY0M1owITEfMB0GA1UEAwwWYXJjaC1oYXJuZXNzLW10bHMt\n\
dGVzdDBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABOLlkWmTv7i7X0VLN/0SgYwN\n\
bR8/pUcTF4pBEWsydZ5eTN7Oqru6QIhaDQHVFG1dZdgr3gZoARHLGf1NP8YYs2qj\n\
EzARMA8GA1UdEwEB/wQFMAMBAf8wCgYIKoZIzj0EAwIDSAAwRQIhALzBrC9NS9rf\n\
V/B12rfLzx1ejO5i0fJF4XoFLRSSKIwlAiBxyOW3fpbjMht8EfmF3rta3AOiF3ag\n\
PSCRP1UX8+MizQ==\n\
-----END CERTIFICATE-----\n";

    const TEST_KEY_PEM: &str = "-----BEGIN PRIVATE KEY-----\n\
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgYmQX4yMhMK9MTrVQ\n\
Y4jvF+vWJbcZyH7R5y26uUe93xShRANCAATi5ZFpk7+4u19FSzf9EoGMDW0fP6VH\n\
ExeKQRFrMnWeXkzezqq7ukCIWg0B1RRtXWXYK94GaAERyxn9TT/GGLNq\n\
-----END PRIVATE KEY-----\n";

    /// Разбор OAuth-ответа: оба поля возвращаются как есть.
    #[test]
    fn parses_oauth_token_response() {
        let parsed: TokenResponse =
            serde_json::from_str(r#"{"access_token":"tok-abc","expires_at":1725200000}"#)
                .expect("json");
        assert_eq!(parsed.access_token, "tok-abc");
        assert_eq!(parsed.expires_at, 1_725_200_000);
    }

    /// Упреждающий рефреш: за 60 с до истечения токен ещё свеж, раньше — нет.
    #[test]
    fn token_is_fresh_only_outside_refresh_skew() {
        assert!(
            is_fresh(1_000 + 60, 1_000),
            "ровно 60 с — порог включителен"
        );
        assert!(is_fresh(1_000 + 3_600, 1_000), "час до истечения — свеж");
        assert!(!is_fresh(1_000 + 59, 1_000), "59 с — пора обновлять");
        assert!(!is_fresh(1_000 + 30, 1_000), "за полминуты — протухший");
        assert!(!is_fresh(1_000, 1_000), "истёк ровно сейчас");
        assert!(!is_fresh(900, 1_000), "истёк давно");
    }

    /// `RqUID` — валидный UUID v4 (RFC 4122): формат 8-4-4-4-12, version 4,
    /// variant 10xx. Случайность между вызовами не проверяем, формат — да.
    #[test]
    fn rquid_is_well_formed_uuid_v4() {
        let uuid = uuid_v4();
        let bytes = uuid.as_bytes();
        assert_eq!(uuid.len(), 36, "uuid: {uuid}");
        assert_eq!([bytes[8], bytes[13], bytes[18], bytes[23]], [b'-'; 4]);
        assert!(
            uuid.chars()
                .filter(|c| *c != '-')
                .all(|c| c.is_ascii_hexdigit()),
            "только hex: {uuid}"
        );
        assert_eq!(&uuid[14..15], "4", "version 4: {uuid}");
        assert!(matches!(bytes[19], b'8'..=b'b'), "variant 10xx: {uuid}");
        // Два вызова подряд не коллизируют (счётчик + время + ОС-энтропия).
        assert_ne!(uuid_v4(), uuid);
    }

    /// Пресеты профиля применяются к копии конфигурации: `context_limit` 128K,
    /// дефолтный User-Agent, дефолтный `token_url`; явные значения сохраняются.
    #[test]
    fn factory_presets_profile_defaults() {
        let cfg = ModelConfig {
            model: "GigaChat-2-Pro".into(),
            api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
            context_limit: None,
            user_agent: Some("custom-agent/1.0".into()),
            oauth: Some(OAuthConfig {
                token_url: String::new(),
                scope: "GIGACHAT_API_CORP".into(),
            }),
            ..ModelConfig::default()
        };
        let eff = with_defaults(&cfg);
        assert_eq!(eff.context_limit, Some(DEFAULT_CONTEXT_LIMIT));
        assert_eq!(eff.user_agent.as_deref(), Some("custom-agent/1.0"));
        let oauth = eff.oauth.expect("oauth");
        assert_eq!(oauth.token_url, DEFAULT_TOKEN_URL);
        assert_eq!(oauth.scope, "GIGACHAT_API_CORP");
        assert!(eff.base_url.is_empty(), "base_url пресетует with_preset");
    }

    /// Фабрика падает на старте (не в рантайме запроса), когда TLS-файлы
    /// не читаются/не разбираются или mTLS-поля заданы не парой (fail-fast).
    #[test]
    fn factory_fails_fast_on_bad_tls_config() {
        let tmp = tempfile::tempdir().expect("tmp");
        let bad_ca = tmp.path().join("bad.pem");
        std::fs::write(&bad_ca, "не PEM вовсе").expect("write");
        let missing = tmp.path().join("missing.pem");

        let mut cfg = ModelConfig {
            model: "GigaChat-2-Pro".into(),
            api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
            oauth: Some(OAuthConfig {
                token_url: "https://example.test/oauth".into(),
                scope: "GIGACHAT_API_PERS".into(),
            }),
            ..ModelConfig::default()
        };
        // Несуществующий CA-файл.
        cfg.ca_pem_file = Some(missing.to_string_lossy().into_owned());
        assert!(provider("gigachat", &cfg).is_err(), "нет файла — Err");
        // Битый CA-файл.
        cfg.ca_pem_file = Some(bad_ca.to_string_lossy().into_owned());
        let err = provider("gigachat", &cfg).expect_err("битый PEM — Err");
        assert!(err.to_string().contains("CA"), "причина в ошибке: {err}");
        // Полупара mTLS.
        cfg.ca_pem_file = None;
        cfg.client_cert_file = Some("x.pem".into());
        let err = provider("gigachat", &cfg).expect_err("полупара mTLS — Err");
        assert!(err.to_string().contains("парой"), "причина в ошибке: {err}");
    }

    /// Валидный CA-файл и полная mTLS-пара конструируются без ошибок
    /// (клиент собирается, корень добавляется в root store).
    #[test]
    fn factory_accepts_valid_ca_and_mtls_pair() {
        let tmp = tempfile::tempdir().expect("tmp");
        let ca_path = tmp.path().join("ca.pem");
        std::fs::write(&ca_path, TEST_CERT_PEM).expect("write");
        let cert_path = tmp.path().join("client.pem");
        std::fs::write(&cert_path, TEST_CERT_PEM).expect("write");
        let key_path = tmp.path().join("client-key.pem");
        std::fs::write(&key_path, TEST_KEY_PEM).expect("write");

        let cfg = ModelConfig {
            model: "GigaChat-2-Pro".into(),
            api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
            ca_pem_file: Some(ca_path.to_string_lossy().into_owned()),
            client_cert_file: Some(cert_path.to_string_lossy().into_owned()),
            client_key_file: Some(key_path.to_string_lossy().into_owned()),
            // B2Bank-профиль: OAuth нет, клиента аутентифицирует сертификат.
            oauth: None,
            ..ModelConfig::default()
        };
        let provider = provider("gigachat-b2bank", &cfg).expect("конструирование");
        let dbg = format!("{provider:?}");
        assert!(dbg.contains("gigachat-b2bank"), "debug: {dbg}");
    }

    /// HTTP-ответ мок-сервера: JSON-контент по умолчанию.
    fn http_response(status: &str, body: &str) -> String {
        format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
    }

    /// HTTP-ответ с дополнительными заголовками (например, `Retry-After`).
    fn http_response_with_headers(status: &str, body: &str, extra: &[(&str, &str)]) -> String {
        use std::fmt::Write as _;
        let extra = extra.iter().fold(String::new(), |mut acc, (name, value)| {
            // Запись в String не может завершиться ошибкой — игнор безопасен.
            let _ = write!(acc, "{name}: {value}\r\n");
            acc
        });
        format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\n{extra}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
    }

    /// SSE-ответ мок-сервера (`text/event-stream`).
    fn sse_response(body: &str) -> String {
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
    }

    /// Тело успешного OAuth-ответа: токен живёт 30 минут (как у Sber).
    fn oauth_ok_body(token: &str) -> String {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_secs());
        format!(
            r#"{{"access_token":"{token}","expires_at":{}}}"#,
            now + 1800
        )
    }

    /// Тело нестримингового ответа `/chat/completions`.
    fn chat_ok_body(text: &str) -> String {
        format!(
            r#"{{"choices":[{{"message":{{"role":"assistant","content":"{text}"}},"finish_reason":"stop"}}]}}"#
        )
    }

    /// Мок-сервер на loopback (паттерн `TcpListener` из тестов `openai_compat)`:
    /// обслуживает `responses.len()` соединений подряд, текст каждого запроса
    /// уходит в канал (для проверки заголовков), ответ — из очереди.
    async fn serve_mock(
        responses: Vec<String>,
    ) -> (
        u16,
        tokio::task::JoinHandle<()>,
        mpsc::UnboundedReceiver<String>,
    ) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let port = listener.local_addr().expect("local_addr").port();
        let (tx, rx) = mpsc::unbounded_channel();
        let handle = tokio::spawn(async move {
            for response in responses {
                let (mut socket, _) = listener.accept().await.expect("accept");
                let mut buf = [0u8; 16_384];
                let mut raw = Vec::new();
                // Читаем запрос целиком: заголовки до \r\n\r\n + тело по
                // Content-Length (TCP может разбить запрос на сегменты).
                loop {
                    let n = socket.read(&mut buf).await.expect("read");
                    if n == 0 {
                        break;
                    }
                    raw.extend_from_slice(&buf[..n]);
                    let Some(end) = raw.windows(4).position(|w| w == b"\r\n\r\n") else {
                        continue;
                    };
                    let head = String::from_utf8_lossy(&raw[..end]);
                    let body_len: usize = header(&head, "content-length")
                        .and_then(|v| v.trim().parse().ok())
                        .unwrap_or(0);
                    if raw.len() >= end + 4 + body_len {
                        break;
                    }
                }
                let _ = tx.send(String::from_utf8_lossy(&raw).into_owned());
                socket.write_all(response.as_bytes()).await.expect("write");
                socket.shutdown().await.expect("shutdown");
            }
        });
        (port, handle, rx)
    }

    /// Значение заголовка в тексте HTTP-запроса (регистронезависимо).
    fn header<'a>(request: &'a str, name: &str) -> Option<&'a str> {
        request.lines().find_map(|line| {
            let (n, v) = line.split_once(':')?;
            n.trim().eq_ignore_ascii_case(name).then(|| v.trim())
        })
    }

    /// Путь из стартовой строки запроса (`POST /api/v2/oauth HTTP/1.1`).
    fn request_path(request: &str) -> &str {
        request
            .lines()
            .next()
            .and_then(|line| line.split_whitespace().nth(1))
            .unwrap_or("")
    }

    /// Конфиг модели `GigaChat` с OAuth на мок-сервер и ключом из файла
    /// (env в тестах не трогаем: `set_var` unsafe в edition 2024).
    fn oauth_model_cfg(port: u16, key_file: &std::path::Path) -> ModelConfig {
        ModelConfig {
            base_url: format!("http://127.0.0.1:{port}"),
            model: "GigaChat-2-Pro".into(),
            api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
            api_key_file: Some(key_file.to_string_lossy().into_owned()),
            oauth: Some(OAuthConfig {
                token_url: format!("http://127.0.0.1:{port}/api/v2/oauth"),
                scope: "GIGACHAT_API_PERS".into(),
            }),
            ..ModelConfig::default()
        }
    }

    fn write_key_file(tmp: &tempfile::TempDir, contents: &str) -> std::path::PathBuf {
        let path = tmp.path().join("key");
        std::fs::write(&path, contents).expect("write key file");
        path
    }

    /// Полный поток провайдера: OAuth-запрос (Basic-ключ, `RqUID`, UA, scope) →
    /// chat/completions с Bearer-токеном и User-Agent. Проверяем заголовки
    /// обоих запросов на моке: User-Agent обязателен (без него API отвечает
    /// 403 — фактура), `RqUID` — uuid4, Basic/Bearer — без утечки в Debug.
    #[tokio::test]
    async fn sends_basic_rquid_ua_and_bearer_headers() {
        let tmp = tempfile::tempdir().expect("tmp");
        let key_file = write_key_file(&tmp, "basic-secret-key-42");
        let (port, server, mut rx) = serve_mock(vec![
            http_response("200 OK", &oauth_ok_body("top-secret-token-42")),
            http_response("200 OK", &chat_ok_body("Привет из GigaChat")),
        ])
        .await;
        let cfg = oauth_model_cfg(port, &key_file);
        let provider = provider("gigachat", &cfg).expect("provider");

        let msg = provider
            .complete(ChatRequest::chat(vec![ChatMessage::user("привет")]))
            .await
            .expect("complete");
        server.await.expect("join");
        assert_eq!(msg.content, "Привет из GigaChat");

        let oauth_req = rx.recv().await.expect("oauth-запрос");
        let chat_req = rx.recv().await.expect("chat-запрос");
        assert!(rx.recv().await.is_none(), "лишних запросов нет");

        // OAuth-запрос: Basic-ключ, RqUID uuid4, scope, UA.
        assert_eq!(request_path(&oauth_req), "/api/v2/oauth");
        assert_eq!(
            header(&oauth_req, "authorization"),
            Some("Basic basic-secret-key-42")
        );
        let rquid = header(&oauth_req, "RqUID").expect("RqUID");
        assert_eq!(rquid.len(), 36, "uuid4: {rquid}");
        assert!(oauth_req.contains("scope=GIGACHAT_API_PERS"), "{oauth_req}");
        assert!(
            oauth_req
                .to_lowercase()
                .contains("content-type: application/x-www-form-urlencoded"),
            "form-urlencoded: {oauth_req}"
        );
        let oauth_ua = header(&oauth_req, "user-agent").expect("UA на OAuth");
        assert!(oauth_ua.starts_with("spine-arch/"), "UA: {oauth_ua}");

        // chat/completions: Bearer-токен + UA (мок без UA отвечал бы 403).
        assert_eq!(request_path(&chat_req), "/chat/completions");
        assert_eq!(
            header(&chat_req, "authorization"),
            Some("Bearer top-secret-token-42")
        );
        let chat_ua = header(&chat_req, "user-agent").expect("UA на chat");
        assert!(chat_ua.starts_with("spine-arch/"), "UA: {chat_ua}");
        assert!(chat_ua.contains(env!("CARGO_PKG_VERSION")));
        assert!(
            chat_req.contains("GigaChat-2-Pro"),
            "имя модели в теле: {chat_req}"
        );
    }

    /// 401 на chat/completions → РОВНО один принудительный рефреш (второй
    /// OAuth-вызов) → повтор с новым Bearer. Повторный 401 — ошибка наверх.
    #[tokio::test]
    async fn chat_401_triggers_single_refresh_and_retry_with_new_bearer() {
        let tmp = tempfile::tempdir().expect("tmp");
        let key_file = write_key_file(&tmp, "basic-secret-key-42");
        let (port, server, mut rx) = serve_mock(vec![
            http_response("200 OK", &oauth_ok_body("tok-expired-server-side")),
            http_response(
                "401 Unauthorized",
                r#"{"error":{"message":"token expired"}}"#,
            ),
            http_response("200 OK", &oauth_ok_body("tok-fresh-after-401")),
            http_response("200 OK", &chat_ok_body("после рефреша")),
        ])
        .await;
        let cfg = oauth_model_cfg(port, &key_file);
        let provider = provider("gigachat", &cfg).expect("provider");

        let msg = provider
            .complete(ChatRequest::chat(vec![ChatMessage::user("продолжай")]))
            .await
            .expect("complete после рефреша");
        server.await.expect("join");
        assert_eq!(msg.content, "после рефреша");

        let mut reqs = Vec::new();
        while let Some(req) = rx.recv().await {
            reqs.push(req);
        }
        assert_eq!(reqs.len(), 4, "oauth, chat(401), oauth(refresh), chat(ok)");
        assert_eq!(request_path(&reqs[0]), "/api/v2/oauth");
        assert_eq!(request_path(&reqs[1]), "/chat/completions");
        assert_eq!(request_path(&reqs[2]), "/api/v2/oauth");
        assert_eq!(request_path(&reqs[3]), "/chat/completions");
        assert_eq!(
            header(&reqs[1], "authorization"),
            Some("Bearer tok-expired-server-side")
        );
        assert_eq!(
            header(&reqs[3], "authorization"),
            Some("Bearer tok-fresh-after-401"),
            "повтор идёт со свежим токеном"
        );
    }

    /// Токен из кэша переиспользуется: два API-вызова подряд — один OAuth.
    #[tokio::test]
    async fn cached_token_is_reused_across_calls() {
        let tmp = tempfile::tempdir().expect("tmp");
        let key_file = write_key_file(&tmp, "basic-secret-key-42");
        let (port, server, mut rx) = serve_mock(vec![
            http_response("200 OK", &oauth_ok_body("tok-cached")),
            http_response("200 OK", &chat_ok_body("раз")),
            http_response("200 OK", &chat_ok_body("два")),
        ])
        .await;
        let cfg = oauth_model_cfg(port, &key_file);
        let provider = provider("gigachat", &cfg).expect("provider");

        let first = provider
            .complete(ChatRequest::chat(vec![ChatMessage::user("раз")]))
            .await
            .expect("первый");
        let second = provider
            .complete(ChatRequest::chat(vec![ChatMessage::user("два")]))
            .await
            .expect("второй");
        server.await.expect("join");
        assert_eq!(
            (first.content.as_str(), second.content.as_str()),
            ("раз", "два")
        );

        let mut paths = Vec::new();
        while let Some(req) = rx.recv().await {
            paths.push(request_path(&req).to_string());
        }
        assert_eq!(
            paths,
            vec![
                "/api/v2/oauth".to_string(),
                "/chat/completions".to_string(),
                "/chat/completions".to_string(),
            ],
            "OAuth ровно один раз, токен из кэша"
        );
    }

    /// SSE-стрим до `data: [DONE]` парсится штатным парсером `openai_compat`
    /// (дублирования нет — провайдер это `OpenAiCompat` с OAuth-источником).
    #[tokio::test]
    async fn streams_sse_until_done() {
        let tmp = tempfile::tempdir().expect("tmp");
        let key_file = write_key_file(&tmp, "basic-secret-key-42");
        let body = "data: {\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"При\"}}]}\n\n\
data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"вет\"}}]}\n\n\
data: {\"choices\":[],\"usage\":{\"prompt_tokens\":7,\"completion_tokens\":2}}\n\n\
data: [DONE]\n\n";
        let (port, server, mut rx) = serve_mock(vec![
            http_response("200 OK", &oauth_ok_body("tok-stream")),
            sse_response(body),
        ])
        .await;
        let cfg = oauth_model_cfg(port, &key_file);
        let provider = provider("gigachat", &cfg).expect("provider");

        let (tx, mut events) = mpsc::channel(16);
        let msg = provider
            .stream(
                ChatRequest::chat(vec![ChatMessage::user("скажи Привет")]),
                tx,
            )
            .await
            .expect("stream");
        server.await.expect("join");
        assert_eq!(msg.content, "Привет");
        let mut deltas = Vec::new();
        while let Some(ev) = events.recv().await {
            if let LlmEvent::Delta(text) = ev {
                deltas.push(text);
            }
        }
        assert_eq!(deltas, vec!["При".to_string(), "вет".to_string()]);
        let mut paths = Vec::new();
        while let Some(req) = rx.recv().await {
            paths.push(request_path(&req).to_string());
        }
        assert_eq!(
            paths,
            vec!["/api/v2/oauth".to_string(), "/chat/completions".to_string(),],
            "OAuth ровно один раз, токен из кэша"
        );
    }

    /// 429 на OAuth-эндпоинте (лимит ≤10 req/s) повторяется с backoff'ом —
    /// второй запрос токена успешен, провайдер работает.
    #[tokio::test]
    async fn oauth_429_is_retried_with_backoff() {
        let tmp = tempfile::tempdir().expect("tmp");
        let key_file = write_key_file(&tmp, "basic-secret-key-42");
        let (port, server, mut rx) = serve_mock(vec![
            http_response("429 Too Many Requests", r#"{"error":"rate limit"}"#),
            http_response("200 OK", &oauth_ok_body("tok-after-429")),
            http_response("200 OK", &chat_ok_body("ок")),
        ])
        .await;
        let cfg = oauth_model_cfg(port, &key_file);
        let provider = provider("gigachat", &cfg).expect("provider");

        let msg = provider
            .complete(ChatRequest::chat(vec![ChatMessage::user("пинг")]))
            .await
            .expect("complete после 429-ретрая");
        server.await.expect("join");
        assert_eq!(msg.content, "ок");
        let mut reqs = Vec::new();
        while let Some(req) = rx.recv().await {
            reqs.push(req);
        }
        assert_eq!(reqs.len(), 3, "oauth(429), oauth(ok), chat");
        assert_eq!(
            header(&reqs[2], "authorization"),
            Some("Bearer tok-after-429")
        );
    }

    /// B2Bank-профиль (oauth: None + `client_cert`_*): mTLS-клиент собирается,
    /// API-ключ не требуется, заголовок Authorization не добавляется вовсе.
    #[tokio::test]
    async fn b2bank_mtls_profile_sends_no_authorization() {
        let tmp = tempfile::tempdir().expect("tmp");
        let cert_path = tmp.path().join("client.pem");
        std::fs::write(&cert_path, TEST_CERT_PEM).expect("write cert");
        let key_path = tmp.path().join("client-key.pem");
        std::fs::write(&key_path, TEST_KEY_PEM).expect("write key");
        let (port, server, mut rx) = serve_mock(vec![http_response(
            "200 OK",
            &chat_ok_body("из банковского контура"),
        )])
        .await;

        let cfg = ModelConfig {
            base_url: format!("http://127.0.0.1:{port}"),
            model: "GigaChat-2-Max".into(),
            // Ключ не нужен вовсе — env пуст, файла нет.
            api_key_env: String::new(),
            client_cert_file: Some(cert_path.to_string_lossy().into_owned()),
            client_key_file: Some(key_path.to_string_lossy().into_owned()),
            oauth: None,
            ..ModelConfig::default()
        };
        let provider = provider("gigachat-b2bank", &cfg).expect("provider");

        let msg = provider
            .complete(ChatRequest::chat(vec![ChatMessage::user("привет")]))
            .await
            .expect("complete без Authorization");
        server.await.expect("join");
        assert_eq!(msg.content, "из банковского контура");

        let req = rx.recv().await.expect("chat-запрос");
        assert!(rx.recv().await.is_none());
        assert_eq!(request_path(&req), "/chat/completions");
        assert!(
            header(&req, "authorization").is_none(),
            "Bearer не добавляется: {req}"
        );
        let ua = header(&req, "user-agent").expect("UA обязателен и в B2Bank");
        assert!(ua.starts_with("spine-arch/"), "UA: {ua}");
    }

    /// Debug-маскирование: ни токен, ни Basic-ключ не попадают в `{:?}`
    /// ни рефрешера, ни провайдера целиком.
    #[tokio::test]
    async fn debug_masks_token_and_basic_key() {
        let tmp = tempfile::tempdir().expect("tmp");
        let key_file = write_key_file(&tmp, "basic-secret-key-42");
        let (port, server, _rx) = serve_mock(vec![http_response(
            "200 OK",
            &oauth_ok_body("top-secret-token-42"),
        )])
        .await;
        let cfg = oauth_model_cfg(port, &key_file);

        let refresher = OAuthRefresher::new("gigachat", &cfg, cfg.oauth.as_ref().expect("oauth"))
            .expect("refresher");
        let auth = refresher.authorization().await.expect("токен получен");
        assert_eq!(auth.as_deref(), Some("Bearer top-secret-token-42"));
        server.await.expect("join");

        let dbg = format!("{refresher:?}");
        assert!(!dbg.contains("top-secret-token-42"), "токен в Debug: {dbg}");
        assert!(!dbg.contains("basic-secret-key-42"), "ключ в Debug: {dbg}");
        assert!(dbg.contains("GIGACHAT_API_PERS"), "scope виден: {dbg}");

        let provider = provider("gigachat", &cfg).expect("provider");
        let pdbg = format!("{provider:?}");
        assert!(
            !pdbg.contains("top-secret-token-42") && !pdbg.contains("basic-secret-key-42"),
            "провайдер в Debug: {pdbg}"
        );
        assert!(pdbg.contains("gigachat"), "имя видно: {pdbg}");
    }

    /// Кламп `expires_at` (ADR-021, L3): 0/прошлое поднимаются до
    /// `now + MIN_TTL_SECS`, реальный серверный срок не укорачивается.
    #[test]
    fn clamp_min_ttl_floors_zero_and_past_expiry() {
        assert_eq!(clamp_min_ttl(0, 1_000), 1_300, "0 от сервера — к минимуму");
        assert_eq!(
            clamp_min_ttl(900, 1_000),
            1_300,
            "прошлое время — к минимуму"
        );
        assert_eq!(clamp_min_ttl(1_000, 1_000), 1_300, "истёк ровно сейчас");
        assert_eq!(
            clamp_min_ttl(1_000 + 1_800, 1_000),
            2_800,
            "серверный срок длиннее минимума — не трогаем"
        );
    }

    /// Мок «капает»: соединение принимает, запрос читает, ответа не шлёт
    /// (`пост_oauth` ждал бы свои 60 с). Общий дедлайн цикла рефреша
    /// (ADR-021, M1) обязан оборвать цикл за ~дедлайн.
    #[tokio::test]
    async fn fetch_deadline_cuts_off_drip_oauth_endpoint() {
        let tmp = tempfile::tempdir().expect("tmp");
        let key_file = write_key_file(&tmp, "basic-secret-key-42");
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let port = listener.local_addr().expect("local_addr").port();
        let _server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.expect("accept");
            let mut buf = [0u8; 16_384];
            let mut raw = Vec::new();
            loop {
                let n = socket.read(&mut buf).await.expect("read");
                if n == 0 {
                    break;
                }
                raw.extend_from_slice(&buf[..n]);
                let Some(end) = raw.windows(4).position(|w| w == b"\r\n\r\n") else {
                    continue;
                };
                let head = String::from_utf8_lossy(&raw[..end]);
                let body_len: usize = header(&head, "content-length")
                    .and_then(|v| v.trim().parse().ok())
                    .unwrap_or(0);
                if raw.len() >= end + 4 + body_len {
                    break;
                }
            }
            // Запрос прочитан целиком — держим соединение без ответа.
            tokio::time::sleep(Duration::from_secs(3600)).await;
        });
        let cfg = oauth_model_cfg(port, &key_file);
        let refresher = OAuthRefresher::new("gigachat", &cfg, cfg.oauth.as_ref().expect("oauth"))
            .expect("refresher");

        // Страховочный таймаут: если дедлайн сломается, тест упадёт за 10 с
        // (попытка ждёт заголовки 60 с), а не зависнет.
        let started = Instant::now();
        let res = tokio::time::timeout(
            Duration::from_secs(10),
            refresher.fetch_token_with_deadline(Duration::from_millis(400)),
        )
        .await
        .expect("дедлайн обязан сработать, а не ждать 60 с попытки");
        let err = res.expect_err("ожидалась ошибка дедлайна");
        let msg = err.to_string();
        assert!(msg.contains("дедлайн"), "текст ошибки: {msg}");
        assert!(msg.contains("/api/v2/oauth"), "URL эндпоинта: {msg}");
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "обрыв по дедлайну занял {:?}",
            started.elapsed()
        );
    }

    /// Circuit breaker (ADR-021, M2): счётчик копится по неудачным циклам,
    /// сбрасывается первым успехом; после порога — fail-fast без новых
    /// обращений к OAuth (считаем запросы на моке).
    #[tokio::test]
    async fn breaker_opens_after_failures_resets_on_success_and_fails_fast() {
        let tmp = tempfile::tempdir().expect("tmp");
        let key_file = write_key_file(&tmp, "basic-secret-key-42");
        let bad = || http_response("400 Bad Request", r#"{"error":"bad request"}"#);
        let ok_body = oauth_ok_body("tok-after-recovery");
        let (port, _server, mut rx) = serve_mock(vec![
            bad(),
            http_response("200 OK", &ok_body),
            bad(),
            bad(),
            bad(),
            bad(),
            bad(),
            bad(),
        ])
        .await;
        let cfg = oauth_model_cfg(port, &key_file);
        let refresher = OAuthRefresher::new("gigachat", &cfg, cfg.oauth.as_ref().expect("oauth"))
            .expect("refresher");

        let err = refresher.authorization().await.expect_err("1-й сбой");
        assert!(
            !err.to_string().contains("circuit breaker"),
            "счётчик 1 — breaker ещё закрыт: {err}"
        );
        refresher.invalidate();
        let auth = refresher
            .authorization()
            .await
            .expect("успех после сбоя — счётчик сбрасывается");
        assert_eq!(auth.as_deref(), Some("Bearer tok-after-recovery"));
        refresher.invalidate();
        refresher
            .authorization()
            .await
            .expect_err("2-й сбой после сброса");
        refresher.invalidate();
        let err = refresher
            .authorization()
            .await
            .expect_err("3-й сбой после сброса");
        assert!(
            !err.to_string().contains("circuit breaker"),
            "счётчик 2 (< порога) — обычная ошибка: {err}"
        );
        refresher.invalidate();
        refresher
            .authorization()
            .await
            .expect_err("4-й сбой — порог достигнут");
        let err = refresher
            .authorization()
            .await
            .expect_err("breaker открыт — быстрый отказ");
        let msg = err.to_string();
        assert!(msg.contains("circuit breaker"), "маркер в ошибке: {msg}");
        assert!(msg.contains("быстрый отказ"), "суть ошибки: {msg}");

        // До мока дошло ровно 5 OAuth-запросов (сбой, успех, сбой, сбой,
        // сбой): fail-fast обращений не делает.
        let mut oauth_hits = 0;
        while let Ok(req) = rx.try_recv() {
            if request_path(&req) == "/api/v2/oauth" {
                oauth_hits += 1;
            }
        }
        assert_eq!(oauth_hits, 5, "fail-fast без новых попыток");
    }

    /// `Retry-After` на 429 уважается (ADR-021, M3): пауза — максимум
    /// заголовка и вычисленного backoff'а. Backoff 429: база 2 с ±25 %
    /// джиттера ≤ 2.5 с < `Retry-After: 3` — паузу задаёт заголовок, и тест
    /// реально ловит регрессию (без учёта заголовка sleep был бы ≤ 2.5 с).
    #[tokio::test]
    async fn oauth_429_waits_out_retry_after_header() {
        let tmp = tempfile::tempdir().expect("tmp");
        let key_file = write_key_file(&tmp, "basic-secret-key-42");
        let (port, server, mut rx) = serve_mock(vec![
            http_response_with_headers(
                "429 Too Many Requests",
                r#"{"error":"rate limit"}"#,
                &[("Retry-After", "3")],
            ),
            http_response("200 OK", &oauth_ok_body("tok-after-retry-after")),
            http_response("200 OK", &chat_ok_body("ок")),
        ])
        .await;
        let cfg = oauth_model_cfg(port, &key_file);
        let provider = provider("gigachat", &cfg).expect("provider");

        let started = Instant::now();
        let msg = provider
            .complete(ChatRequest::chat(vec![ChatMessage::user("пинг")]))
            .await
            .expect("complete после Retry-After");
        let elapsed = started.elapsed();
        server.await.expect("join");
        assert_eq!(msg.content, "ок");
        assert!(
            elapsed >= Duration::from_millis(2_900),
            "пауза по Retry-After: {elapsed:?}"
        );
        let mut reqs = Vec::new();
        while let Some(req) = rx.recv().await {
            reqs.push(req);
        }
        assert_eq!(reqs.len(), 3, "oauth(429), oauth(ok), chat");
        assert_eq!(
            header(&reqs[2], "authorization"),
            Some("Bearer tok-after-retry-after")
        );
    }

    /// `expires_at: 0` от сервера клампится до `now + MIN_TTL_SECS`
    /// (ADR-021, L3): токен живёт в кэше, второй вызов — без рефреша.
    #[tokio::test]
    async fn oauth_zero_expires_at_is_clamped_and_token_reused() {
        let tmp = tempfile::tempdir().expect("tmp");
        let key_file = write_key_file(&tmp, "basic-secret-key-42");
        let zero_expiry = r#"{"access_token":"tok-clamped","expires_at":0}"#.to_string();
        let (port, server, mut rx) = serve_mock(vec![
            http_response("200 OK", &zero_expiry),
            http_response("200 OK", &chat_ok_body("раз")),
            http_response("200 OK", &chat_ok_body("два")),
        ])
        .await;
        let cfg = oauth_model_cfg(port, &key_file);
        let provider = provider("gigachat", &cfg).expect("provider");

        let first = provider
            .complete(ChatRequest::chat(vec![ChatMessage::user("раз")]))
            .await
            .expect("первый");
        let second = provider
            .complete(ChatRequest::chat(vec![ChatMessage::user("два")]))
            .await
            .expect("второй");
        server.await.expect("join");
        assert_eq!(
            (first.content.as_str(), second.content.as_str()),
            ("раз", "два")
        );
        let mut paths = Vec::new();
        while let Some(req) = rx.recv().await {
            paths.push(request_path(&req).to_string());
        }
        assert_eq!(
            paths,
            vec![
                "/api/v2/oauth".to_string(),
                "/chat/completions".to_string(),
                "/chat/completions".to_string(),
            ],
            "OAuth ровно один раз: expires_at=0 клампится, токен кэшируется"
        );
    }
}
