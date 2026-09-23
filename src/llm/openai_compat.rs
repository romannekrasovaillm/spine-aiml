//! Общий OpenAI-совместимый клиент чат-комплишнов с SSE-стримингом.
//!
//! Реализует [`LlmProvider`] для endpoint'ов вида `{base_url}/chat/completions`:
//! function calling (`tools`/`tool_calls`), потоковый разбор `data:`-строк (SSE)
//! со сборкой инкрементальных `tool_calls`, ретраи по матрице классов ошибок
//! ([`crate::retry`]: 429/5xx/транспорт — с backoff'ом и джиттером, 4xx/413 —
//! без повторов). Тонкие фабрики с пресетами `base_url` — [`super::deepseek`],
//! [`super::kimi`], [`super::glm`], [`super::gigachat`] (последний добавляет
//! внешний источник `Authorization` — OAuth2-рефрешер, см. [`AuthSource`]);
//! произвольные endpoint'ы подключаются через [`generic_provider`].

use std::fmt;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use futures::StreamExt;
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::mpsc;

use crate::config::ModelConfig;
use crate::error::{HarnessError, Result};
use crate::llm::{
    ChatMessage, ChatRequest, LlmEvent, LlmProvider, Role, ToolCall, ToolSpec, Usage,
};

/// Таймаут установки TCP/TLS-соединения, секунды.
const CONNECT_TIMEOUT_SECS: u64 = 30;

/// Клиент OpenAI-совместимого чат-комплишн API (`/chat/completions`).
///
/// API-ключ читается из переменной окружения [`ModelConfig::api_key_env`]
/// при создании и нигде не выводится: [`fmt::Debug`] показывает только
/// имя провайдера, модель и `base_url`.
pub struct OpenAiCompat {
    /// Имя провайдера из реестра (ключ `[models]`).
    name: String,
    /// Базовый URL API без завершающего `/`.
    base_url: String,
    /// Идентификатор модели.
    model: String,
    /// Имя переменной окружения с API-ключом (читается лениво, на запросе —
    /// чтобы отсутствие ключа одного провайдера не роняло весь реестр).
    api_key_env: String,
    /// Запасной файл с ключом (читается, если env не задан; `~` раскрывается).
    api_key_file: Option<String>,
    /// Ключ, заданный напрямую (тесты; в проде None — читается из окружения).
    api_key_override: Option<String>,
    /// Температура из конфига (если запрос не переопределяет).
    default_temperature: Option<f32>,
    /// Максимум токенов из конфига (если запрос не переопределяет).
    default_max_tokens: Option<u32>,
    /// Бюджет тишины (сек): ожидание заголовков ответа и максимальная пауза
    /// между чанками SSE-стрима. НЕ общий таймаут запроса: reasoner-модели
    /// стримят минутами, общий таймаут резал их посреди ответа
    /// («error decoding response body» на скриншотах пользователей).
    timeout_secs: u64,
    /// Карты ризонинга из конфига (сливаются в тело при `/think on|off`).
    thinking_on: Option<serde_json::Map<String, Value>>,
    thinking_off: Option<serde_json::Map<String, Value>>,
    /// HTTP-клиент (rustls; connect/read-таймауты, без общего).
    client: reqwest::Client,
    /// Внешний источник заголовка `Authorization` (OAuth2-токен `GigaChat`,
    /// mTLS-профиль без заголовка). None — историческое поведение:
    /// `Bearer <статический api_key>`. Поле сознательно не выводится
    /// в Debug: источник держит секреты.
    auth: Option<Arc<dyn AuthSource>>,
}

// Поля с ключом в Debug сознательно не включаем — это секреты.
#[expect(
    clippy::missing_fields_in_debug,
    reason = "api_key_env/api_key_override — секреты, в Debug не выводятся"
)]
impl fmt::Debug for OpenAiCompat {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // API-ключ сознательно не выводим.
        f.debug_struct("OpenAiCompat")
            .field("name", &self.name)
            .field("model", &self.model)
            .field("base_url", &self.base_url)
            .finish()
    }
}

/// Источник значения заголовка `Authorization` для запросов [`OpenAiCompat`].
///
/// У [`OpenAiCompat`] источник опционален: пока его нет, клиент шлёт
/// `Bearer <статический api_key>` (историческое поведение вендоров).
/// Тонкие фабрики (`GigaChat`, ADR-021) подставляют собственный источник:
/// OAuth2-токен с кэшем и упреждающим рефрешем — либо отсутствие заголовка
/// вовсе (mTLS-профиль `B2Bank`: клиента аутентифицирует сертификат).
/// crate-видимость: реализуют тонкие фабрики этого крейта, наружу
/// конфигурируется через `[models.*]`, не кодом.
#[async_trait]
pub(crate) trait AuthSource: Send + Sync + fmt::Debug {
    /// Значение заголовка `Authorization` для очередного запроса;
    /// `Ok(None)` — заголовок не добавлять.
    ///
    /// # Errors
    /// Сбой получения токена (OAuth-эндпоинт недоступен/отказал).
    async fn authorization(&self) -> Result<Option<String>>;

    /// Сигнал «сервер отверг прошлый токен (HTTP 401)»: источник
    /// инвалидирует кэш, следующий [`AuthSource::authorization`] добудет
    /// свежий токен. Вызывается не чаще одного раза за последовательность
    /// запросов: повторный 401 уходит наверх как ошибка (ретрай не лечит).
    fn invalidate(&self);
}

impl OpenAiCompat {
    /// Создаёт клиента из конфигурации; `base_url` обязателен.
    /// API-ключ не проверяется — читается лениво на первом запросе.
    ///
    /// # Errors
    /// Пустой `base_url`/`model`, ошибка сборки HTTP-клиента.
    pub fn new(name: &str, cfg: &ModelConfig) -> Result<Self> {
        Self::build(name, cfg, cfg.base_url.trim(), None)
    }

    /// Создаёт клиента из конфигурации; пустой `base_url` заменяется пресетом.
    /// API-ключ не проверяется — читается лениво на первом запросе.
    ///
    /// # Errors
    /// Пустой `model`, ошибка сборки HTTP-клиента.
    pub fn with_preset(name: &str, cfg: &ModelConfig, preset_base_url: &str) -> Result<Self> {
        Self::build(name, cfg, preset_base_url, None)
    }

    /// Вариант [`Self::with_preset`] с внешним источником `Authorization`
    /// (OAuth2-рефрешер или mTLS-профиль без заголовка — см. [`AuthSource`]).
    /// Пока источник задан, статический API-ключ не используется.
    /// Вызывается тонкими фабриками вендоров (`GigaChat`).
    ///
    /// # Errors
    /// Пустой `model`, ошибка сборки HTTP-клиента.
    pub(crate) fn with_preset_and_auth(
        name: &str,
        cfg: &ModelConfig,
        preset_base_url: &str,
        auth: Option<Arc<dyn AuthSource>>,
    ) -> Result<Self> {
        let configured = cfg.base_url.trim();
        let base_url = if configured.is_empty() {
            preset_base_url
        } else {
            configured
        };
        Self::build(name, cfg, base_url, auth)
    }

    fn build(
        name: &str,
        cfg: &ModelConfig,
        base_url: &str,
        auth: Option<Arc<dyn AuthSource>>,
    ) -> Result<Self> {
        let base_url = base_url.trim_end_matches('/');
        if base_url.is_empty() {
            return Err(HarnessError::Llm(format!(
                "провайдер '{name}': пустой base_url (задайте в config.toml)"
            )));
        }
        if cfg.model.trim().is_empty() {
            return Err(HarnessError::Llm(format!(
                "провайдер '{name}': пустой model"
            )));
        }
        let api_key_env = cfg.api_key_env.clone();
        let api_key_file = cfg.api_key_file.clone();
        let client = build_client(name, cfg)?;
        Ok(Self {
            name: name.to_string(),
            base_url: base_url.to_string(),
            model: cfg.model.clone(),
            api_key_env,
            api_key_file,
            api_key_override: None,
            default_temperature: cfg.temperature,
            default_max_tokens: cfg.max_tokens,
            timeout_secs: cfg.timeout_secs.max(1),
            thinking_on: cfg.thinking_on.clone(),
            thinking_off: cfg.thinking_off.clone(),
            client,
            auth,
        })
    }

    /// API-ключ: переопределение (тесты) → переменная окружения → запасной
    /// файл (`api_key_file`, `~` раскрывается). Ошибка — на момент запроса,
    /// не на конструировании; содержимое ключа в ошибку не попадает.
    fn api_key(&self) -> Result<String> {
        if let Some(key) = &self.api_key_override {
            return Ok(key.clone());
        }
        resolve_api_key(&self.name, &self.api_key_env, &self.api_key_file)
    }

    /// Тело запроса `/chat/completions` (None-поля и пустой `tools` не сериализуются).
    /// Значения из запроса переопределяют дефолты конфига. При заданном
    /// переключателе ризонинга (`req.thinking`) в тело сливается карта
    /// `thinking_on`/`thinking_off` из конфига модели.
    fn build_body<'a>(
        &'a self,
        req: &'a ChatRequest,
        mode: RequestMode,
    ) -> ChatCompletionsBody<'a> {
        let extra = match req.thinking {
            Some(true) => self.thinking_on.clone(),
            Some(false) => self.thinking_off.clone(),
            None => None,
        };
        ChatCompletionsBody {
            model: &self.model,
            messages: req.messages.iter().map(OutMessage::from).collect(),
            tools: req.tools.iter().map(OutTool::from).collect(),
            temperature: req.temperature.or(self.default_temperature),
            max_tokens: req.max_tokens.or(self.default_max_tokens),
            stream: matches!(mode, RequestMode::Stream),
            stream_options: match mode {
                RequestMode::Stream => Some(StreamOptions {
                    include_usage: true,
                }),
                RequestMode::Complete => None,
            },
            extra,
        }
    }

    /// POST с ретраями по матрице [`crate::retry`]: 429 — терпеливо
    /// (8 попыток), 5xx — средне, транспортные сбои — быстро; 4xx, 413
    /// (переполнение контекста) и ошибки авторизации сразу идут наверх —
    /// их ретрай не лечит. Между попытками — экспоненциальный backoff
    /// с джиттером; смена класса ошибки пересоздаёт итератор задержек.
    async fn send_with_retry(&self, body: &ChatCompletionsBody<'_>) -> Result<reqwest::Response> {
        let url = format!("{}/chat/completions", self.base_url);
        let seed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| u64::from(d.subsec_nanos()));
        let mut delays: Option<(crate::retry::ErrorKind, crate::retry::Delays)> = None;
        // Для источников Authorization с токеном (OAuth2 GigaChat): первый 401
        // лечится ОДНИМ принудительным рефрешем и повтором запроса (токен мог
        // протухнуть на стороне сервера раньше срока). Повторный 401 ретраю
        // не подлежит — уходит наверх как ошибка. Без источника (статический
        // ключ) поведение историческое: 401 сразу наверх.
        let mut auth_refreshed = false;
        loop {
            // Ok(None) — финальный ответ (успех или неретраибельная ошибка);
            // Err(e) — финальный транспортный сбой; Some(d) — пауза и повтор.
            let pause: Option<Duration> = match self.post_once(&url, body).await {
                Ok(resp) => {
                    if resp.status().is_success() {
                        return Ok(resp);
                    }
                    if resp.status() == StatusCode::UNAUTHORIZED && !auth_refreshed {
                        if let Some(source) = &self.auth {
                            auth_refreshed = true;
                            source.invalidate();
                            continue;
                        }
                    }
                    let kind = crate::retry::classify(Some(resp.status().as_u16()), "");
                    match crate::retry::RetryPolicy::for_kind(kind) {
                        None => return Ok(resp), // разбор ошибки — в ensure_success
                        Some(policy) => match next_delay(&mut delays, kind, policy, seed) {
                            // 429/5xx несут Retry-After: пауза — максимум
                            // заголовка и вычисленного backoff'а (ADR-021, M3).
                            Some(d) => Some(delay_respecting_retry_after(resp.headers(), d)),
                            None => return Ok(resp),
                        },
                    }
                }
                Err(e) => {
                    // Классификация по ПОЛНОЙ цепочке причин: верхнее
                    // «error sending request for url» не несёт причины —
                    // она в source-цепочке (reset/timeout/DNS).
                    let kind = crate::retry::classify(None, &error_text(&e));
                    match crate::retry::RetryPolicy::for_kind(kind) {
                        None => return Err(send_phase_error(&self.name, &e)),
                        Some(policy) => match next_delay(&mut delays, kind, policy, seed) {
                            Some(d) => Some(d),
                            None => return Err(send_phase_error(&self.name, &e)),
                        },
                    }
                }
            };
            if let Some(d) = pause {
                tokio::time::sleep(d).await;
            }
        }
    }

    async fn post_once(
        &self,
        url: &str,
        body: &ChatCompletionsBody<'_>,
    ) -> Result<reqwest::Response> {
        // send() резолвится на получении заголовков: бюджет тишины покрывает
        // фазу «запрос ушёл, сервер думает». Дальше страж — read_timeout.
        let request = match &self.auth {
            Some(source) => {
                // Источник сам решает, слать ли Authorization: OAuth2-токен
                // (GigaChat) или ничего (mTLS-профиль B2Bank — клиента
                // аутентифицирует сертификат, статический ключ не нужен).
                let mut request = self.client.post(url);
                if let Some(header) = source.authorization().await? {
                    request = request.header(reqwest::header::AUTHORIZATION, header);
                }
                request
            }
            None => self.client.post(url).bearer_auth(self.api_key()?),
        };
        let send = request.json(body).send();
        match tokio::time::timeout(Duration::from_secs(self.timeout_secs), send).await {
            Ok(res) => res.map_err(HarnessError::Http),
            Err(_) => Err(HarnessError::Llm(format!(
                "{}: таймаут: сервер не прислал заголовки ответа за {}с (перегрузка или сеть)",
                self.name, self.timeout_secs
            ))),
        }
    }

    /// Проверяет HTTP-статус; ошибку превращает в [`HarnessError::Llm`]
    /// с кодом и первыми 300 символами тела.
    async fn ensure_success(&self, resp: reqwest::Response) -> Result<reqwest::Response> {
        let status = resp.status();
        if status.is_success() {
            return Ok(resp);
        }
        let body = resp.text().await.unwrap_or_default();
        Err(http_error(&self.name, status, &body))
    }

    /// Читает тело SSE-стрима до конца. Обрыв потока оформляется как
    /// [`StreamBreak`] с флагом «контент уже ушёл пользователю».
    async fn pump_stream(
        &self,
        resp: reqwest::Response,
        tx: &mpsc::Sender<LlmEvent>,
    ) -> std::result::Result<ChatMessage, StreamBreak> {
        let mut byte_stream = resp.bytes_stream();
        let mut decoder = SseDecoder::default();
        let mut acc = StreamAcc::default();
        let mut done_sent = false;
        let mut emitted = false;
        while let Some(chunk) = byte_stream.next().await {
            let chunk = match chunk {
                Ok(c) => c,
                Err(e) => {
                    let received_chars = acc.content.chars().count();
                    return Err(StreamBreak {
                        emitted,
                        received_chars,
                        source: stream_break_error(&self.name, &e, received_chars),
                    });
                }
            };
            let events = match decoder.feed(&chunk, &mut acc) {
                Ok(ev) => ev,
                Err(e) => {
                    return Err(StreamBreak {
                        emitted,
                        received_chars: acc.content.chars().count(),
                        source: e,
                    });
                }
            };
            for event in events {
                done_sent |= matches!(event, LlmEvent::Done(_));
                emitted |= matches!(event, LlmEvent::Delta(_));
                if tx.send(event).await.is_err() {
                    // Приёмник ушёл — прерываем стрим без паники.
                    return Ok(acc.finish());
                }
            }
            if done_sent {
                break;
            }
        }
        if !done_sent {
            // Поток закрылся без `data: [DONE]`: добираем хвост и завершаем сами.
            match decoder.flush(&mut acc) {
                Ok(events) => {
                    for event in events {
                        done_sent |= matches!(event, LlmEvent::Done(_));
                        if tx.send(event).await.is_err() {
                            return Ok(acc.finish());
                        }
                    }
                }
                Err(e) => {
                    return Err(StreamBreak {
                        emitted,
                        received_chars: acc.content.chars().count(),
                        source: e,
                    });
                }
            }
            if !done_sent {
                // Поток закрылся без `data: [DONE]`. Принимаем только если
                // модель сама сообщила finish_reason — иначе это усечение
                // сетью/прокси (частичные аргументы tool-вызовов!), и запрос
                // надо повторить, а не исполнять обрезок.
                if acc.finish_reason.is_none() {
                    return Err(StreamBreak {
                        emitted,
                        received_chars: acc.content.chars().count(),
                        source: HarnessError::Llm(format!(
                            "{}: поток закрылся без [DONE] и finish_reason — \
                             ответ усечён (сеть/прокси)",
                            self.name
                        )),
                    });
                }
                let _ = tx.send(LlmEvent::Done(acc.usage)).await;
            }
        }
        Ok(acc.finish())
    }
}

/// Раскрывает ведущий `~/` в домашний каталог (пути `api_key_file`,
/// `ca_pem_file`, сертификатов mTLS). Несуществующий home — путь как есть.
fn expand_home(path: &str) -> std::path::PathBuf {
    match path.strip_prefix("~/") {
        Some(rest) => {
            dirs::home_dir().map_or_else(|| std::path::PathBuf::from(path), |h| h.join(rest))
        }
        None => std::path::PathBuf::from(path),
    }
}

/// API-ключ провайдера: переменная окружения → запасной файл
/// (`api_key_file`, `~` раскрывается). Общая для статического Bearer
/// ([`OpenAiCompat::api_key`]) и OAuth-рефрешеров (Basic-ключ `GigaChat`,
/// [`super::gigachat`]); содержимое ключа в ошибку не попадает.
///
/// # Errors
/// Ключ не найден (env не задан/пуст, файл не задан или пуст) или
/// `api_key_file` не читается — в ошибке путь файла и причина чтения
/// (ADR-021 L4), содержимое файла не выводится.
pub(crate) fn resolve_api_key(provider: &str, env: &str, file: &Option<String>) -> Result<String> {
    if let Ok(raw) = std::env::var(env) {
        let key = raw.trim().to_string();
        if !key.is_empty() {
            return Ok(key);
        }
    }
    if let Some(path) = file {
        let expanded = expand_home(path);
        match std::fs::read_to_string(&expanded) {
            Ok(raw) => {
                let key = raw.trim().to_string();
                if !key.is_empty() {
                    return Ok(key);
                }
            }
            // Нечитаемый файл ключа — конкретная причина вместо обобщённого
            // «нет ключа»: путь + os error, содержимое ключа не выводим.
            Err(read_err) => {
                return Err(HarnessError::Llm(format!(
                    "провайдер '{provider}': нет API-ключа — файл {} не читается: \
                     {read_err} (установите {env} или исправьте api_key_file)",
                    expanded.display()
                )));
            }
        }
    }
    let hint = match file {
        Some(path) => format!(" или положите ключ в файл {}", expand_home(path).display()),
        None => String::new(),
    };
    Err(HarnessError::Llm(format!(
        "провайдер '{provider}': нет API-ключа — установите {env}{hint}"
    )))
}

/// Строит HTTP-клиента провайдера из конфигурации (общая для
/// [`OpenAiCompat`] и OAuth-рефрешера GigaChat):
/// - rustls; таймаут соединения и таймаут чтения, БЕЗ общего `.timeout()`
///   (он измеряет запрос целиком и обрывает длинный SSE-стрим посреди
///   ответа) — таймаут ожидания заголовков живёт в `post_once`;
/// - `user_agent` — заголовок `User-Agent` (`GigaChat` без него отвечает 403);
/// - `ca_pem_file` — доп. корневые CA в root store (НУЦ Минцифры);
/// - `client_cert_file`/`client_key_file` — mTLS-идентичность (`B2Bank`);
/// - `proxy` — egress-прокси провайдера; loopback-шлюз поднимается
///   автоматически через [`crate::net::ensure_local_egress`] (сбой — мягкое
///   предупреждение, не отказ).
///
/// Опции применяются только когда поля заданы; системные корни не
/// отключаются. ЗАПРЕЩЕНО ослаблять верификацию TLS (AD-BE5): никаких
/// `danger_accept_invalid_certs`/`verify(false)` — кастомный CA добавляется
/// к проверке, а не заменяет её. Ошибки чтения/разбора файлов — fail-fast
/// на построении клиента (не в рантайме запроса).
pub(crate) fn build_client(name: &str, cfg: &ModelConfig) -> Result<reqwest::Client> {
    let mut builder = reqwest::Client::builder()
        // Общий .timeout() здесь НЕ устанавливаем сознательно: он измеряет
        // весь запрос целиком и обрывает длинный SSE-стрим посреди ответа.
        .connect_timeout(Duration::from_secs(CONNECT_TIMEOUT_SECS))
        .read_timeout(Duration::from_secs(cfg.timeout_secs.max(1)));
    if let Some(raw) = cfg
        .user_agent
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        let value = reqwest::header::HeaderValue::from_str(raw).map_err(|e| {
            HarnessError::Llm(format!(
                "провайдер '{name}': невалидный User-Agent {raw:?}: {e}"
            ))
        })?;
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(reqwest::header::USER_AGENT, value);
        builder = builder.default_headers(headers);
    }
    if let Some(ca_path) = cfg
        .ca_pem_file
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        for cert in read_ca_certs(name, ca_path)? {
            builder = builder.add_root_certificate(cert);
        }
    }
    if let Some(identity) = read_client_identity(name, cfg)? {
        builder = builder.identity(identity);
    }
    if let Some(proxy_url) = cfg
        .proxy
        .as_deref()
        .map(str::trim)
        .filter(|u| !u.is_empty())
    {
        // Локальный egress-шлюз (напр. sing-box на loopback) поднимаем
        // автоматически; недоступность — предупреждение, не отказ:
        // запрос всё равно пойдёт через настроенный прокси.
        match crate::net::ensure_local_egress(proxy_url) {
            Ok(Some(crate::net::EgressStatus::Started)) => {
                tracing::info!("провайдер '{name}': vpn-egress запущен автоматически");
            }
            Ok(Some(crate::net::EgressStatus::AlreadyRunning) | None) => {}
            Err(e) => {
                tracing::warn!("провайдер '{name}': {e}");
            }
        }
        builder = builder.proxy(reqwest::Proxy::all(proxy_url).map_err(|e| {
            HarnessError::Llm(format!(
                "провайдер '{name}': битый proxy URL '{proxy_url}': {e}"
            ))
        })?);
    }
    builder.build().map_err(HarnessError::Http)
}

/// PEM-сертификаты из файла `ca_pem_file` (несколько блоков подряд
/// поддерживаются — НУЦ-бандлы иногда содержат корень и подчинённый CA).
///
/// # Errors
/// Файл не читается или не содержит ни одного разбираемого сертификата.
fn read_ca_certs(provider: &str, path: &str) -> Result<Vec<reqwest::Certificate>> {
    let expanded = expand_home(path);
    let pem = std::fs::read(&expanded).map_err(|e| HarnessError::io(&expanded, e))?;
    let text = String::from_utf8_lossy(&pem);
    let mut certs = Vec::new();
    for block in text.split_inclusive("-----END CERTIFICATE-----") {
        if block.contains("-----BEGIN CERTIFICATE-----") {
            let cert = reqwest::Certificate::from_pem(block.as_bytes()).map_err(|e| {
                HarnessError::Llm(format!(
                    "{provider}: не удалось разобрать CA-сертификат из {}: {e}",
                    expanded.display()
                ))
            })?;
            certs.push(cert);
        }
    }
    if certs.is_empty() {
        return Err(HarnessError::Llm(format!(
            "{provider}: в CA-файле {} нет ни одного PEM-сертификата",
            expanded.display()
        )));
    }
    Ok(certs)
}

/// mTLS-идентичность из пары PEM-файлов (`client_cert_file` + `client_key_file`).
/// Поля задаются строго парой; None — mTLS не нужен (обычный TLS).
///
/// # Errors
/// Задан только один файл пары; файлы не читаются/не разбираются.
fn read_client_identity(provider: &str, cfg: &ModelConfig) -> Result<Option<reqwest::Identity>> {
    let cert_path = cfg
        .client_cert_file
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty());
    let key_path = cfg
        .client_key_file
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty());
    match (cert_path, key_path) {
        (None, None) => Ok(None),
        (Some(cert), Some(key)) => {
            let cert_path = expand_home(cert);
            let key_path = expand_home(key);
            let cert_pem =
                std::fs::read(&cert_path).map_err(|e| HarnessError::io(&cert_path, e))?;
            let key_pem = std::fs::read(&key_path).map_err(|e| HarnessError::io(&key_path, e))?;
            let mut combined = cert_pem;
            combined.extend_from_slice(&key_pem);
            let identity = reqwest::Identity::from_pem(&combined).map_err(|e| {
                HarnessError::Llm(format!(
                    "{provider}: не удалось собрать mTLS-идентичность из {} и {}: {e}",
                    cert_path.display(),
                    key_path.display()
                ))
            })?;
            Ok(Some(identity))
        }
        _ => Err(HarnessError::Llm(format!(
            "{provider}: client_cert_file и client_key_file задаются парой (mTLS-профиль)"
        ))),
    }
}

#[async_trait]
impl LlmProvider for OpenAiCompat {
    fn name(&self) -> &str {
        &self.name
    }

    fn model(&self) -> &str {
        &self.model
    }

    /// Нестриминговый запрос: `POST /chat/completions`, ответ целиком.
    async fn complete(&self, req: ChatRequest) -> Result<ChatMessage> {
        let body = self.build_body(&req, RequestMode::Complete);
        let resp = self.send_with_retry(&body).await?;
        let resp = self.ensure_success(resp).await?;
        let parsed: CompletionResponse = resp.json().await?;
        let choice = parsed.choices.into_iter().next().ok_or_else(|| {
            HarnessError::Llm(format!("{}: пустой ответ API (нет choices)", self.name))
        })?;
        let mut msg = choice.message.into_chat_message();
        msg.finish_reason = choice.finish_reason;
        Ok(msg)
    }

    /// Стриминговый запрос: дельты в `tx`, собранный ответ — результатом.
    ///
    /// Если приёмник `tx` закрыт, стрим тихо прерывается (без паники)
    /// и возвращается накопленная к этому моменту часть ответа.
    ///
    /// Обрыв тела стрима (сеть/DPI/перегрузка, молчание модели дольше
    /// `timeout_secs`) ретраится по политике [`crate::retry::ErrorKind::Network`]
    /// — запрос идемпотентен: вызовы инструментов исполняются только после
    /// полной сборки ответа, так что повтор безопасен на любой фазе. Обрыв
    /// ДО первой дельты повторяется молча; обрыв ПОСЛЕ контента — с заметкой
    /// [`LlmEvent::Note`] в UI (фрагмент остаётся в чате, ответ придёт
    /// целиком заново). Бюджет попыток общий на вызов; при исчерпании —
    /// ошибка с человеческой причиной и счётчиком полученного.
    async fn stream(&self, req: ChatRequest, tx: mpsc::Sender<LlmEvent>) -> Result<ChatMessage> {
        let body = self.build_body(&req, RequestMode::Stream);
        let seed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| u64::from(d.subsec_nanos()));
        let mut delays = crate::retry::RetryPolicy::for_kind(crate::retry::ErrorKind::Network)
            .map(|p| p.delays(seed));
        loop {
            let resp = self.send_with_retry(&body).await?;
            let resp = self.ensure_success(resp).await?;
            match self.pump_stream(resp, &tx).await {
                Ok(msg) => return Ok(msg),
                Err(brk) => {
                    let Some(pause) = delays.as_mut().and_then(Iterator::next) else {
                        return Err(brk.source);
                    };
                    if brk.emitted {
                        // Фрагмент уже в чате — честно предупреждаем: ответ
                        // придёт целиком заново, показанный кусок не дублируется
                        // в журнале (туда попадёт только финальное сообщение).
                        let _ = tx
                            .send(LlmEvent::Note(format!(
                                "поток ответа оборвался (в чат успели уйти {} символов — \
                                 фрагмент выше неполный); повторяю запрос…",
                                brk.received_chars
                            )))
                            .await;
                    }
                    tokio::time::sleep(pause).await;
                }
            }
        }
    }
}

/// Generic-провайдер для произвольного OpenAI-совместимого endpoint'а
/// из конфигурации (неизвестное имя в `[models]`).
///
/// # Errors
/// Отсутствует API-ключ в окружении, пустой `base_url`/`model`.
pub fn generic_provider(name: &str, cfg: &ModelConfig) -> Result<Arc<dyn LlmProvider>> {
    Ok(Arc::new(OpenAiCompat::new(name, cfg)?))
}

/// Режим запроса: разовый ответ или SSE-стрим.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RequestMode {
    /// Обычный комплишн: ответ целиком.
    Complete,
    /// `stream: true` + `stream_options.include_usage`.
    Stream,
}

/// Тело запроса `/chat/completions` в формате `OpenAI`.
#[derive(Debug, Serialize)]
struct ChatCompletionsBody<'a> {
    model: &'a str,
    messages: Vec<OutMessage<'a>>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    tools: Vec<OutTool<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    temperature: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    max_tokens: Option<u32>,
    stream: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    stream_options: Option<StreamOptions>,
    /// Доп. поля верхнего уровня (карта ризонинга `thinking`/`reasoning_effort`).
    #[serde(skip_serializing_if = "Option::is_none", flatten)]
    extra: Option<serde_json::Map<String, Value>>,
}

/// Опции стриминга (`include_usage` — финальный чанк несёт статистику токенов).
#[derive(Debug, Serialize)]
struct StreamOptions {
    include_usage: bool,
}

/// Контент сообщения в wire-формате: строка или массив блоков
/// (текст + изображения) — нативная мультимодальность `deepseek-flash`.
#[derive(Debug, Serialize)]
#[serde(untagged)]
enum Content<'a> {
    Text(&'a str),
    Parts(Vec<ContentPart<'a>>),
}

/// Блок контента мультимодального сообщения (`text` | `image_url`).
#[derive(Debug, Serialize)]
struct ContentPart<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    text: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    image_url: Option<ImageUrl<'a>>,
}

/// `image_url`-блок: data-URL изображения (base64, `<mime>;base64,<data>`).
#[derive(Debug, Serialize)]
struct ImageUrl<'a> {
    url: &'a str,
}

/// Сообщение в wire-формате `OpenAI`.
#[derive(Debug, Serialize)]
struct OutMessage<'a> {
    role: Role,
    content: Content<'a>,
    /// Эхо цепочки рассуждений: `DeepSeek` thinking + `tools` требует возврата
    /// `reasoning_content` (иначе HTTP 400); прочие провайдеры его игнорируют.
    #[serde(skip_serializing_if = "Option::is_none")]
    reasoning_content: Option<&'a str>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    tool_calls: Vec<OutToolCall<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tool_call_id: Option<&'a str>,
}

impl<'a> From<&'a ChatMessage> for OutMessage<'a> {
    fn from(msg: &'a ChatMessage) -> Self {
        // Изображения принимаются только в user-сообщениях (DeepSeek, HTTP 400
        // иначе): с изображениями content становится массивом блоков.
        let content = if msg.role == Role::User && !msg.images.is_empty() {
            let mut parts = vec![ContentPart {
                kind: "text",
                text: Some(&msg.content),
                image_url: None,
            }];
            parts.extend(msg.images.iter().map(|img| ContentPart {
                kind: "image_url",
                text: None,
                image_url: Some(ImageUrl { url: img }),
            }));
            Content::Parts(parts)
        } else {
            Content::Text(&msg.content)
        };
        Self {
            role: msg.role,
            content,
            reasoning_content: msg.reasoning_content.as_deref(),
            tool_calls: msg.tool_calls.iter().map(OutToolCall::from).collect(),
            tool_call_id: msg.tool_call_id.as_deref(),
        }
    }
}

/// Вызов инструмента в wire-формате (`type: "function"`).
#[derive(Debug, Serialize)]
struct OutToolCall<'a> {
    id: &'a str,
    #[serde(rename = "type")]
    kind: &'static str,
    function: OutFunction<'a>,
}

impl<'a> From<&'a ToolCall> for OutToolCall<'a> {
    fn from(call: &'a ToolCall) -> Self {
        Self {
            id: &call.id,
            kind: "function",
            function: OutFunction {
                name: &call.name,
                arguments: call.arguments.to_string(),
            },
        }
    }
}

/// `function` внутри вызова: `arguments` — строка с JSON.
#[derive(Debug, Serialize)]
struct OutFunction<'a> {
    name: &'a str,
    arguments: String,
}

/// Инструмент в wire-формате (`type: "function"`).
#[derive(Debug, Serialize)]
struct OutTool<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    function: OutToolSpec<'a>,
}

impl<'a> From<&'a ToolSpec> for OutTool<'a> {
    fn from(spec: &'a ToolSpec) -> Self {
        Self {
            kind: "function",
            function: OutToolSpec {
                name: &spec.name,
                description: &spec.description,
                parameters: &spec.parameters,
            },
        }
    }
}

/// `function` внутри описания инструмента.
#[derive(Debug, Serialize)]
struct OutToolSpec<'a> {
    name: &'a str,
    description: &'a str,
    parameters: &'a Value,
}

/// Ответ `/chat/completions` (нестриминговый).
#[derive(Debug, Deserialize)]
struct CompletionResponse {
    #[serde(default)]
    choices: Vec<CompletionChoice>,
}

#[derive(Debug, Deserialize)]
struct CompletionChoice {
    message: CompletionMessage,
    /// `stop` | `length` | `tool_calls`… (`length` = усечение `max_tokens`).
    #[serde(default)]
    finish_reason: Option<String>,
}

/// `message` из ответа: `content` может быть `null` при `tool_calls`.
#[derive(Debug, Deserialize)]
struct CompletionMessage {
    #[serde(default)]
    content: Option<String>,
    /// Цепочка рассуждений ризонинг-модели (`DeepSeek` V4/Kimi/GLM).
    #[serde(default)]
    reasoning_content: Option<String>,
    #[serde(default)]
    tool_calls: Vec<CompletionToolCall>,
}

impl CompletionMessage {
    /// Конвертирует wire-ответ в доменное сообщение ассистента.
    fn into_chat_message(self) -> ChatMessage {
        let tool_calls = self
            .tool_calls
            .into_iter()
            .map(|call| ToolCall {
                id: call.id,
                name: call.function.name,
                arguments: parse_arguments(&call.function.arguments),
            })
            .collect();
        let mut msg = ChatMessage::assistant(self.content.unwrap_or_default(), tool_calls);
        msg.reasoning_content = self.reasoning_content.filter(|r| !r.is_empty());
        normalize_reply(msg)
    }
}

/// Нормализация исхода на границе API (defensive pattern: «контракт с обеих
/// сторон»). GLM-4.7 quirk: на коротких ответах с thinking=on модель
/// недетерминированно кладёт весь ответ в `reasoning_content`, оставляя
/// `content` пустым (проверено прогонами: ~2/3 случаев, `finish_reason=stop`).
/// Без нормализации ход возвращал бы пустую строку — подменяем пустой ответ
/// цепочкой рассуждений (там и есть ответ). Сообщения с `tool_calls` не
/// трогаем: у них пустой content — норма.
fn normalize_reply(mut msg: ChatMessage) -> ChatMessage {
    // Let-chains стабилизированы в 1.88 — выше MSRV 1.85: условия разнесены
    // в плоские выражения (семантика та же, что у цепочки `&& let`).
    let empty_reply = msg.tool_calls.is_empty() && msg.content.trim().is_empty();
    let reasoning = msg.reasoning_content.as_deref().unwrap_or_default().trim();
    if empty_reply && !reasoning.is_empty() {
        msg.content = reasoning.to_string();
    }
    msg
}

#[derive(Debug, Deserialize)]
struct CompletionToolCall {
    #[serde(default)]
    id: String,
    function: CompletionFunction,
}

#[derive(Debug, Deserialize)]
struct CompletionFunction {
    #[serde(default)]
    name: String,
    #[serde(default, deserialize_with = "de_arguments")]
    arguments: String,
}

/// Чанк SSE-стрима (`data: {...}`).
#[derive(Debug, Deserialize)]
struct StreamChunk {
    #[serde(default)]
    choices: Vec<StreamChoice>,
    #[serde(default)]
    usage: Option<StreamUsage>,
}

#[derive(Debug, Deserialize)]
struct StreamChoice {
    #[serde(default)]
    delta: StreamDelta,
    /// `stop` | `length` | `tool_calls`… в финальном чанке (`length` —
    /// ответ усечён потолком `max_tokens`: аргументы tool-вызова обрезаны).
    #[serde(default)]
    finish_reason: Option<String>,
}

/// Дельта стрима: кусок текста и/или инкрементальные `tool_calls`.
#[derive(Debug, Default, Deserialize)]
struct StreamDelta {
    #[serde(default)]
    content: Option<String>,
    /// Кусок цепочки рассуждений (ризонинг-модели); копится, но не стримится
    /// в чат — `CoT` не смешиваем с текстом ответа.
    #[serde(default)]
    reasoning_content: Option<String>,
    #[serde(default)]
    tool_calls: Vec<StreamToolCall>,
}

/// Инкрементальный вызов инструмента: поля приходят по частям (по `index`).
#[derive(Debug, Deserialize)]
struct StreamToolCall {
    #[serde(default)]
    index: usize,
    #[serde(default)]
    id: Option<String>,
    #[serde(default)]
    function: Option<StreamFunction>,
}

#[derive(Debug, Deserialize)]
struct StreamFunction {
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    arguments: Option<String>,
}

/// Статистика токенов из финального чанка (`stream_options.include_usage`).
#[derive(Debug, Deserialize)]
struct StreamUsage {
    #[serde(default)]
    prompt_tokens: u64,
    #[serde(default)]
    completion_tokens: u64,
    /// Попадание промпта в кэш провайдера (форма `DeepSeek`).
    #[serde(default)]
    prompt_cache_hit_tokens: Option<u64>,
    /// Детализация токенов промпта (OpenAI-форма, внутри — `cached_tokens`).
    #[serde(default)]
    prompt_tokens_details: Option<PromptTokensDetails>,
}

/// Детализация токенов промпта в OpenAI-форме (`prompt_tokens_details`).
#[derive(Debug, Deserialize)]
struct PromptTokensDetails {
    /// Токены, прочитанные из кэша провайдера.
    #[serde(default)]
    cached_tokens: Option<u64>,
}

/// Накопленные части одного вызова инструмента из стрима.
#[derive(Debug, Default)]
struct ToolCallAcc {
    id: String,
    name: String,
    arguments: String,
}

/// Аккумулятор состояния стрима: текст, `tool_calls`, usage.
#[derive(Debug, Default)]
struct StreamAcc {
    content: String,
    /// Накопленная цепочка рассуждений (эхо для следующих запросов).
    reasoning: String,
    tool_calls: Vec<ToolCallAcc>,
    usage: Usage,
    /// `finish_reason` из финального чанка; признак «модель закончила сама»
    /// (без него и без [DONE] закрытие потока = усечение сетью/прокси).
    finish_reason: Option<String>,
}

/// Исход разбора одной SSE-строки.
#[derive(Debug)]
enum LineOutcome {
    /// Служебная/пустая строка — событий нет.
    None,
    /// Дельты чанка: `reasoning` — «мысли» (может быть пустой), `text` —
    /// видимый текст (может быть пустой); обе пустые быть не могут.
    Delta {
        /// Текстовая дельта для отправки в канал.
        text: String,
        /// Дельта `reasoning_content` для отправки в канал.
        reasoning: String,
    },
    /// Терминатор `data: [DONE]`.
    Done,
}

impl StreamAcc {
    /// Разбирает одну SSE-строку и применяет к аккумулятору.
    ///
    /// # Errors
    /// Битый JSON в `data:`-строке.
    fn apply_line(&mut self, line: &str) -> Result<LineOutcome> {
        let Some(data) = line.strip_prefix("data:") else {
            // Пустые строки, комментарии (`:...`), служебные поля (event:, id:).
            return Ok(LineOutcome::None);
        };
        let data = data.trim();
        if data == "[DONE]" {
            return Ok(LineOutcome::Done);
        }
        let chunk: StreamChunk = serde_json::from_str(data)?;
        Ok(self.apply_chunk(&chunk))
    }

    /// Применяет распарсенный чанк: склеивает текст, «мысли» и куски `tool_calls`.
    fn apply_chunk(&mut self, chunk: &StreamChunk) -> LineOutcome {
        let mut text = String::new();
        let mut reasoning = String::new();
        for choice in &chunk.choices {
            if let Some(reason) = &choice.finish_reason {
                self.finish_reason = Some(reason.clone());
            }
            if let Some(content) = &choice.delta.content {
                self.content.push_str(content);
                text.push_str(content);
            }
            if let Some(r) = &choice.delta.reasoning_content {
                self.reasoning.push_str(r);
                reasoning.push_str(r);
            }
            for call in &choice.delta.tool_calls {
                if self.tool_calls.len() <= call.index {
                    self.tool_calls
                        .resize_with(call.index + 1, ToolCallAcc::default);
                }
                // Слот гарантированно существует после resize_with выше.
                let slot = &mut self.tool_calls[call.index];
                if let Some(id) = &call.id {
                    slot.id.push_str(id);
                }
                if let Some(function) = &call.function {
                    if let Some(name) = &function.name {
                        slot.name.push_str(name);
                    }
                    if let Some(arguments) = &function.arguments {
                        slot.arguments.push_str(arguments);
                    }
                }
            }
        }
        if let Some(usage) = &chunk.usage {
            self.usage = Usage {
                prompt_tokens: usage.prompt_tokens,
                completion_tokens: usage.completion_tokens,
                // Кэш: форма DeepSeek (`prompt_cache_hit_tokens`) приоритетнее
                // OpenAI-формы (`prompt_tokens_details.cached_tokens`).
                cached_tokens: usage.prompt_cache_hit_tokens.or(usage
                    .prompt_tokens_details
                    .as_ref()
                    .and_then(|d| d.cached_tokens)),
            };
        }
        if text.is_empty() && reasoning.is_empty() {
            LineOutcome::None
        } else {
            LineOutcome::Delta { text, reasoning }
        }
    }

    /// Собирает финальное сообщение ассистента из накопленного.
    fn finish(self) -> ChatMessage {
        let tool_calls = self
            .tool_calls
            .into_iter()
            .filter(|call| !call.id.is_empty() || !call.name.is_empty())
            .map(|call| ToolCall {
                id: call.id,
                name: call.name,
                arguments: parse_arguments(&call.arguments),
            })
            .collect();
        let mut msg = ChatMessage::assistant(self.content, tool_calls);
        if !self.reasoning.is_empty() {
            msg.reasoning_content = Some(self.reasoning);
        }
        msg.finish_reason = self.finish_reason;
        normalize_reply(msg)
    }
}

/// Декодер SSE: режет поток байт на строки и разбирает `data:`-события.
///
/// UTF-8 безопасен: строки режутся только по `\n`, а этот байт не встречается
/// внутри многобайтовых последовательностей.
#[derive(Debug, Default)]
struct SseDecoder {
    buf: Vec<u8>,
}

impl SseDecoder {
    /// Скармливает порцию байт, возвращает готовые события.
    ///
    /// # Errors
    /// Битый JSON в `data:`-строке.
    fn feed(&mut self, chunk: &[u8], acc: &mut StreamAcc) -> Result<Vec<LlmEvent>> {
        self.buf.extend_from_slice(chunk);
        let mut events = Vec::new();
        while let Some(pos) = self.buf.iter().position(|b| *b == b'\n') {
            let raw: Vec<u8> = self.buf.drain(..=pos).collect();
            let line = String::from_utf8_lossy(&raw);
            match acc.apply_line(line.trim_end_matches(['\r', '\n']))? {
                LineOutcome::Delta { text, reasoning } => {
                    // Мысли — перед видимым текстом (они и идут первыми).
                    if !reasoning.is_empty() {
                        events.push(LlmEvent::ReasoningDelta(reasoning));
                    }
                    if !text.is_empty() {
                        events.push(LlmEvent::Delta(text));
                    }
                }
                LineOutcome::Done => events.push(LlmEvent::Done(acc.usage)),
                LineOutcome::None => {}
            }
        }
        Ok(events)
    }

    /// Добирает последнюю строку без завершающего `\n` в конце потока.
    ///
    /// # Errors
    /// Битый JSON в `data:`-строке.
    fn flush(&mut self, acc: &mut StreamAcc) -> Result<Vec<LlmEvent>> {
        if self.buf.is_empty() {
            return Ok(Vec::new());
        }
        let line = String::from_utf8_lossy(&self.buf).into_owned();
        self.buf.clear();
        Ok(match acc.apply_line(line.trim_end_matches('\r'))? {
            LineOutcome::Delta { text, reasoning } => {
                let mut events = Vec::new();
                if !reasoning.is_empty() {
                    events.push(LlmEvent::ReasoningDelta(reasoning));
                }
                if !text.is_empty() {
                    events.push(LlmEvent::Delta(text));
                }
                events
            }
            LineOutcome::Done => vec![LlmEvent::Done(acc.usage)],
            LineOutcome::None => Vec::new(),
        })
    }
}

/// Разбирает строку аргументов инструмента в JSON.
///
/// Битый JSON возвращается как [`Value::String`] — модель иногда присылает
/// невалидные аргументы, терять их текст нельзя. Пустая строка → `{}`.
fn parse_arguments(raw: &str) -> Value {
    if raw.trim().is_empty() {
        return Value::Object(serde_json::Map::new());
    }
    serde_json::from_str(raw).unwrap_or_else(|_| Value::String(raw.to_string()))
}

/// Аргументы инструмента: у OpenAI/DeepSeek это строка с JSON, но некоторые
/// провайдеры отдают уже объект — принимаем оба варианта.
fn de_arguments<'de, D>(deserializer: D) -> std::result::Result<String, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let value = Value::deserialize(deserializer)?;
    Ok(match value {
        Value::String(s) => s,
        other => other.to_string(),
    })
}

/// Следующая задержка из итератора ретраев; при смене класса ошибки
/// итератор пересоздаётся (политика 429 сильно терпеливее политики 5xx).
/// Общая для ретраев `/chat/completions` и OAuth-запроса токена (`GigaChat`).
pub(crate) fn next_delay(
    delays: &mut Option<(crate::retry::ErrorKind, crate::retry::Delays)>,
    kind: crate::retry::ErrorKind,
    policy: crate::retry::RetryPolicy,
    seed: u64,
) -> Option<Duration> {
    let matches_kind = matches!(delays, Some((k, _)) if *k == kind);
    if !matches_kind {
        *delays = Some((kind, policy.delays(seed)));
    }
    delays.as_mut().and_then(|(_, it)| it.next())
}

/// Пауза по заголовку `Retry-After` из ответа (целые секунды).
///
/// HTTP-date (RFC 7231) сознательно не разбираем: LLM-провайдеры шлют
/// секунды, а дата потребовала бы календарного парсинга с часовым поясом —
/// добавить только по фактуре (ADR-021, M3).
#[must_use]
pub(crate) fn retry_after_delay(headers: &reqwest::header::HeaderMap) -> Option<Duration> {
    let value = headers.get(reqwest::header::RETRY_AFTER)?.to_str().ok()?;
    value.trim().parse::<u64>().ok().map(Duration::from_secs)
}

/// Итоговая пауза между попытками: максимум из вычисленного backoff'а
/// и `Retry-After` сервера — указание «когда можно снова» имеет приоритет
/// над оценкой клиента (ADR-021, M3).
#[must_use]
fn delay_respecting_retry_after(
    headers: &reqwest::header::HeaderMap,
    backoff: Duration,
) -> Duration {
    match retry_after_delay(headers) {
        Some(server) => server.max(backoff),
        None => backoff,
    }
}

/// Ошибка HTTP-статуса: код + первые 300 символов тела.
fn http_error(provider: &str, status: StatusCode, body: &str) -> HarnessError {
    let excerpt: String = body.trim().chars().take(300).collect();
    HarnessError::Llm(format!("{provider}: HTTP {status}: {excerpt}"))
}

/// Обрыв SSE-стрима в середине тела ответа.
#[derive(Debug)]
struct StreamBreak {
    /// Успела ли уйти пользователю хотя бы одна текстовая дельта
    /// (true → повтор запроса покажет ответ целиком, фрагмент останется —
    /// перед ретраем пользователю уходит [`LlmEvent::Note`]).
    emitted: bool,
    /// Сколько символов контента накоплено к моменту обрыва (для заметки).
    received_chars: usize,
    /// Ошибка с человеческой причиной обрыва.
    source: HarnessError,
}

/// Ошибка обрыва потока с человеческой причиной: reqwest сверху пишет
/// только «error decoding response body» — реальная причина (таймаут
/// чтения, reset, раннее закрытие) прячется в цепочке источников.
fn stream_break_error(provider: &str, err: &reqwest::Error, received_chars: usize) -> HarnessError {
    let chain = error_chain(err);
    let lower = chain.to_lowercase();
    let reason = if lower.contains("timed out") || lower.contains("timeout") {
        "таймаут чтения: модель молчала дольше timeout_secs".to_string()
    } else if lower.contains("connection reset")
        || lower.contains("connection closed")
        || lower.contains("broken pipe")
    {
        "соединение сброшено (сеть/DPI/перегрузка сервера)".to_string()
    } else if lower.contains("eof") || lower.contains("incomplete") || lower.contains("closed") {
        "сервер закрыл поток раньше времени".to_string()
    } else if lower.contains("decode") || lower.contains("gzip") || lower.contains("utf8") {
        "сбой декодирования тела ответа".to_string()
    } else {
        let short: String = chain.trim().chars().take(160).collect();
        format!("сетевой сбой ({short})")
    };
    let got = if received_chars > 0 {
        format!("; успело прийти {received_chars} символов — текст выше НЕПОЛНЫЙ")
    } else {
        String::new()
    };
    HarnessError::Llm(format!(
        "{provider}: поток ответа оборвался{got}: {reason}. \
         Автоматические ретраи исчерпаны. Повторите ход; при повторах — \
         увеличьте timeout_secs у модели в config.toml."
    ))
}

/// Полная цепочка причин ошибки reqwest (само сообщение + source-цепочка).
/// Общая для диагностики `/chat/completions` и OAuth-запроса токена (`GigaChat`).
pub(crate) fn error_chain(err: &reqwest::Error) -> String {
    let mut out = err.to_string();
    let mut src = std::error::Error::source(err);
    while let Some(e) = src {
        out.push_str(": ");
        out.push_str(&e.to_string());
        src = e.source();
    }
    out
}

/// Текст ошибки харнесса для классификации ретраев: для HTTP-ошибок —
/// полная цепочка причин, для остальных — Display.
fn error_text(err: &HarnessError) -> String {
    match err {
        HarnessError::Http(r) => error_chain(r),
        other => other.to_string(),
    }
}

/// Ошибка фазы отправки запроса («error sending request for url») с
/// человеческой причиной из цепочки источников — вместо сырого текста reqwest.
fn send_phase_error(provider: &str, err: &HarnessError) -> HarnessError {
    let chain = error_text(err);
    let lower = chain.to_lowercase();
    let reason = if lower.contains("timed out")
        || lower.contains("timeout")
        || lower.contains("таймаут")
    {
        "таймаут соединения (сервер недоступен или сеть/DPI режет запрос)"
    } else if lower.contains("connection refused") {
        "соединение отклонено (сервер down или порт закрыт)"
    } else if lower.contains("connection reset") || lower.contains("broken pipe") {
        "соединение сброшено сетью/DPI"
    } else if lower.contains("dns") || lower.contains("resolve") || lower.contains("no such host") {
        "DNS не разрешил хост (проверьте сеть/VPN)"
    } else if lower.contains("certificate") || lower.contains("tls") {
        "TLS-ошибка (сертификат/прокси)"
    } else {
        "сетевой сбой"
    };
    let short: String = chain.trim().chars().take(200).collect();
    HarnessError::Llm(format!(
        "{provider}: не удалось отправить запрос: {reason}. \
         Проверьте сеть/VPN и повторите ход. [детали: {short}]"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    /// Провайдер с фиктивным ключом (env в тестах не трогаем: `set_var` unsafe в 2024).
    fn test_provider() -> OpenAiCompat {
        OpenAiCompat {
            name: "test".into(),
            base_url: "https://example.test/v1".into(),
            model: "test-model".into(),
            api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
            api_key_file: None,
            api_key_override: Some("sk-secret-dummy".into()),
            default_temperature: Some(0.25),
            default_max_tokens: Some(1024),
            timeout_secs: 30,
            thinking_on: None,
            thinking_off: None,
            client: reqwest::Client::new(),
            auth: None,
        }
    }

    #[test]
    fn api_key_falls_back_to_file_when_env_missing() {
        let tmp = tempfile::tempdir().expect("tmp");
        let key_path = tmp.path().join("key");
        std::fs::write(&key_path, "  sk-file-key-xyz\n").expect("write");
        let mut provider = test_provider();
        provider.api_key_override = None;
        provider.api_key_file = Some(key_path.to_string_lossy().into_owned());
        assert_eq!(
            provider.api_key().expect("ключ из файла"),
            "sk-file-key-xyz",
            "читается и обрезается от пробелов"
        );
        // Нет ни env, ни файла — внятная ошибка без содержимого.
        provider.api_key_file = Some(tmp.path().join("missing").to_string_lossy().into_owned());
        let err = provider.api_key().expect_err("нет ключа — ошибка");
        let msg = err.to_string();
        assert!(msg.contains("ARCH_HARNESS_TEST_MISSING_KEY_XYZ"), "{msg}");
        // ADR-021 L4: нечитаемый файл — конкретная причина (путь + os error),
        // содержимое ключа не выводится.
        assert!(msg.contains("missing"), "путь файла в ошибке: {msg}");
        assert!(
            msg.contains("не читается"),
            "причина чтения в ошибке: {msg}"
        );
        assert!(msg.contains("os error"), "os error в тексте: {msg}");
        assert!(!msg.contains("sk-"), "содержимое ключа не светится: {msg}");
    }

    #[test]
    fn provider_with_proxy_builds_client_without_network() {
        // Внешний (не loopback) прокси: egress-автозапуск не применим,
        // клиент собирается детерминированно — без сети и системных вызовов.
        let cfg = ModelConfig {
            base_url: "https://openrouter.ai/api/v1".into(),
            model: "stealth/ox-alpha".into(),
            api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
            proxy: Some("http://proxy.internal:3128".into()),
            ..ModelConfig::default()
        };
        let provider = OpenAiCompat::new("openrouter", &cfg).expect("провайдер собирается");
        assert_eq!(provider.name(), "openrouter");
        assert_eq!(provider.model(), "stealth/ox-alpha");
    }

    #[test]
    fn provider_rejects_unparseable_proxy_url() {
        // Битый URL прокси — ошибка конфигурации на этапе сборки, не в рантайме
        // (egress-проверка завершается ещё на разборе, до всякой сети).
        let cfg = ModelConfig {
            base_url: "https://openrouter.ai/api/v1".into(),
            model: "stealth/ox-alpha".into(),
            api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
            proxy: Some("http://127.0.0.1:abc".into()),
            ..ModelConfig::default()
        };
        let err = OpenAiCompat::new("openrouter", &cfg).expect_err("битый прокси — отказ");
        assert!(err.to_string().contains("proxy"), "{err}");
    }

    /// `Retry-After` разбирается как целые секунды; отсутствие заголовка,
    /// мусор и HTTP-date (RFC 7231, сознательно не поддержан) — `None`.
    #[test]
    fn retry_after_is_parsed_as_seconds_only() {
        use reqwest::header::{HeaderMap, RETRY_AFTER};
        let mut headers = HeaderMap::new();
        assert_eq!(retry_after_delay(&headers), None, "нет заголовка");
        headers.insert(RETRY_AFTER, "1".parse().expect("header"));
        assert_eq!(retry_after_delay(&headers), Some(Duration::from_secs(1)));
        headers.insert(RETRY_AFTER, " 120 ".parse().expect("header"));
        assert_eq!(
            retry_after_delay(&headers),
            Some(Duration::from_secs(120)),
            "пробелы обрезаются"
        );
        headers.insert(RETRY_AFTER, "abc".parse().expect("header"));
        assert_eq!(retry_after_delay(&headers), None, "мусор — None");
        headers.insert(
            RETRY_AFTER,
            "Wed, 21 Oct 2015 07:28:00 GMT".parse().expect("header"),
        );
        assert_eq!(retry_after_delay(&headers), None, "HTTP-date — None");
        // Максимум из заголовка и вычисленного backoff'а.
        let headers = HeaderMap::new();
        assert_eq!(
            delay_respecting_retry_after(&headers, Duration::from_secs(2)),
            Duration::from_secs(2),
            "без заголовка — вычисленный backoff"
        );
        let mut headers = HeaderMap::new();
        headers.insert(RETRY_AFTER, "7".parse().expect("header"));
        assert_eq!(
            delay_respecting_retry_after(&headers, Duration::from_secs(2)),
            Duration::from_secs(7),
            "заголовок больше backoff'а — ждём по заголовку"
        );
    }

    #[test]
    fn serializes_request_body_in_openai_wire_format() {
        let provider = test_provider();
        let req = ChatRequest {
            messages: vec![
                ChatMessage::system("Ты — архитектор."),
                ChatMessage::user("Сравни Kafka и NATS"),
                ChatMessage::assistant(
                    "",
                    vec![ToolCall {
                        id: "call_1".into(),
                        name: "kb_search".into(),
                        arguments: serde_json::json!({"query": "Kafka vs NATS"}),
                    }],
                ),
                ChatMessage::tool_result("call_1", "Kafka: лог; NATS: очередь"),
            ],
            tools: vec![ToolSpec {
                name: "kb_search".into(),
                description: "Поиск по базе знаний".into(),
                parameters: serde_json::json!({
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]
                }),
            }],
            // 0.5 точно представимо в f32/f64 — сравнение Value без сюрпризов.
            temperature: Some(0.5),
            max_tokens: None,
            thinking: None,
        };
        let body = serde_json::to_value(provider.build_body(&req, RequestMode::Complete))
            .expect("to_value");
        let want = serde_json::json!({
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "Ты — архитектор."},
                {"role": "user", "content": "Сравни Kafka и NATS"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "kb_search", "arguments": "{\"query\":\"Kafka vs NATS\"}"}
                    }]
                },
                {"role": "tool", "content": "Kafka: лог; NATS: очередь", "tool_call_id": "call_1"}
            ],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "kb_search",
                    "description": "Поиск по базе знаний",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"]
                    }
                }
            }],
            "temperature": 0.5,
            "max_tokens": 1024,
            "stream": false
        });
        assert_eq!(body, want);
    }

    #[test]
    fn stream_body_skips_none_fields_and_adds_stream_options() {
        let provider = test_provider();
        let req = ChatRequest::chat(vec![ChatMessage::user("hi")]);
        let body =
            serde_json::to_value(provider.build_body(&req, RequestMode::Stream)).expect("to_value");
        let want = serde_json::json!({
            "model": "test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.25,
            "max_tokens": 1024,
            "stream": true,
            "stream_options": {"include_usage": true}
        });
        assert_eq!(body, want);
    }

    #[test]
    fn glm_quirk_empty_content_falls_back_to_reasoning() {
        // GLM-4.7 с thinking=on: весь ответ в reasoning_content, content пуст.
        let msg = CompletionMessage {
            content: Some(String::new()),
            reasoning_content: Some("\n3".into()),
            tool_calls: Vec::new(),
        }
        .into_chat_message();
        assert_eq!(msg.content, "3", "ответ поднят из цепочки рассуждений");
        assert_eq!(
            msg.reasoning_content.as_deref(),
            Some("\n3"),
            "reasoning сохранён для эха (DeepSeek-контракт)"
        );
        // С tool_calls пустой content — норма протокола, подмены нет.
        let with_call = CompletionMessage {
            content: None,
            reasoning_content: Some("думаю".into()),
            tool_calls: vec![CompletionToolCall {
                id: "c1".into(),
                function: CompletionFunction {
                    name: "bash".into(),
                    arguments: "{}".into(),
                },
            }],
        }
        .into_chat_message();
        assert_eq!(with_call.content, "");
        // Непустой content никогда не подменяется.
        let plain = CompletionMessage {
            content: Some("ответ".into()),
            reasoning_content: Some("cot".into()),
            tool_calls: Vec::new(),
        }
        .into_chat_message();
        assert_eq!(plain.content, "ответ");
    }

    #[test]
    fn thinking_toggle_merges_config_maps_into_body() {
        let mut provider = test_provider();
        let mut inner = serde_json::Map::new();
        inner.insert("type".into(), Value::String("enabled".into()));
        let mut on = serde_json::Map::new();
        on.insert("thinking".into(), Value::Object(inner));
        provider.thinking_on = Some(on);
        let mut off_inner = serde_json::Map::new();
        off_inner.insert("type".into(), Value::String("disabled".into()));
        let mut off = serde_json::Map::new();
        off.insert("thinking".into(), Value::Object(off_inner));
        provider.thinking_off = Some(off);

        let mut req = ChatRequest::chat(vec![ChatMessage::user("hi")]);
        // None — карты не шлются (дефолт провайдера).
        let body = serde_json::to_value(provider.build_body(&req, RequestMode::Complete))
            .expect("to_value");
        assert!(body.get("thinking").is_none(), "без флага поля нет: {body}");
        // on/off — соответствующая карта на верхнем уровне тела.
        req.thinking = Some(true);
        let body = serde_json::to_value(provider.build_body(&req, RequestMode::Complete))
            .expect("to_value");
        assert_eq!(body["thinking"]["type"], "enabled", "on: {body}");
        req.thinking = Some(false);
        let body = serde_json::to_value(provider.build_body(&req, RequestMode::Complete))
            .expect("to_value");
        assert_eq!(body["thinking"]["type"], "disabled", "off: {body}");
    }

    #[test]
    fn reasoning_content_is_collected_from_stream_and_echoed_back() {
        // Приём: reasoning_content в дельтах копится отдельно от контента.
        let raw = "data: {\"choices\":[{\"index\":0,\"delta\":{\"reasoning_content\":\"считаю\",\"content\":\"отв\"}}]}\n\ndata: {\"choices\":[{\"index\":0,\"delta\":{\"reasoning_content\":\" дальше\",\"content\":\"ет\"}}]}\n\ndata: [DONE]\n\n";
        let mut decoder = SseDecoder::default();
        let mut acc = StreamAcc::default();
        let events = decoder.feed(raw.as_bytes(), &mut acc).expect("feed");
        let text: String = events
            .iter()
            .filter_map(|e| match e {
                LlmEvent::Delta(t) => Some(t.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(text, "ответ", "CoT не смешивается с текстом ответа");
        // «Мысли» приходят отдельными событиями (для блока «мысли» в TUI).
        let reasoning: String = events
            .iter()
            .filter_map(|e| match e {
                LlmEvent::ReasoningDelta(t) => Some(t.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(reasoning, "считаю дальше", "events: {events:?}");
        // Порядок внутри чанка: мысли раньше видимого текста.
        assert!(
            matches!(&events[0], LlmEvent::ReasoningDelta(_))
                && matches!(&events[1], LlmEvent::Delta(_)),
            "events: {events:?}"
        );
        let msg = acc.finish();
        assert_eq!(msg.reasoning_content.as_deref(), Some("считаю дальше"));
        // Эхо: OutMessage возвращает reasoning_content в API (DeepSeek 400-trap).
        let out = OutMessage::from(&msg);
        let v = serde_json::to_value(&out).expect("to_value");
        assert_eq!(v["reasoning_content"], "считаю дальше", "эхо: {v}");
        // Без ризонинга поле не сериализуется вовсе.
        let plain_msg = ChatMessage::user("hi");
        let plain = OutMessage::from(&plain_msg);
        let v = serde_json::to_value(&plain).expect("to_value");
        assert!(v.get("reasoning_content").is_none(), "нет поля: {v}");
    }

    #[test]
    fn multimodal_user_message_serializes_content_as_parts() {
        // С изображениями content становится массивом блоков (OpenAI-формат).
        let mut msg = ChatMessage::user("опиши картинку");
        msg.images = vec!["data:image/png;base64,AAAA".to_string()];
        let v = serde_json::to_value(OutMessage::from(&msg)).expect("to_value");
        let parts = v["content"].as_array().expect("content — массив блоков");
        assert_eq!(parts[0]["type"], "text");
        assert_eq!(parts[0]["text"], "опиши картинку");
        assert_eq!(parts[1]["type"], "image_url");
        assert_eq!(parts[1]["image_url"]["url"], "data:image/png;base64,AAAA");

        // Без изображений content остаётся строкой.
        let plain = serde_json::to_value(OutMessage::from(&ChatMessage::user("просто текст")))
            .expect("to_value");
        assert!(plain["content"].is_string(), "content — строка: {plain}");

        // Изображения в НЕ-user сообщении игнорируются (DeepSeek: HTTP 400 иначе).
        let mut sys = ChatMessage::system("система");
        sys.images = vec!["data:image/png;base64,BBBB".to_string()];
        let sv = serde_json::to_value(OutMessage::from(&sys)).expect("to_value");
        assert!(sv["content"].is_string(), "system content — строка: {sv}");
    }

    #[test]
    fn sse_decoder_parses_deltas_and_done_usage() {
        let raw: &[u8] = b"data: {\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"Hel\"}}]}\n\n\
data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"lo\"}}]}\n\n\
data: {\"choices\":[],\"usage\":{\"prompt_tokens\":12,\"completion_tokens\":2}}\n\n\
data: [DONE]\n\n";
        let mut decoder = SseDecoder::default();
        let mut acc = StreamAcc::default();
        // Две порции, чтобы проверить склейку строк на границе чанков.
        let mid = raw.len() / 2;
        let mut events = decoder.feed(&raw[..mid], &mut acc).expect("feed 1");
        events.extend(decoder.feed(&raw[mid..], &mut acc).expect("feed 2"));
        let text: String = events
            .iter()
            .filter_map(|e| match e {
                LlmEvent::Delta(t) => Some(t.as_str()),
                LlmEvent::ReasoningDelta(_) | LlmEvent::Note(_) | LlmEvent::Done(_) => None,
            })
            .collect();
        assert_eq!(text, "Hello");
        assert!(
            matches!(events.last(), Some(LlmEvent::Done(u)) if *u == Usage { prompt_tokens: 12, completion_tokens: 2, cached_tokens: None }),
            "events: {events:?}"
        );
        let msg = acc.finish();
        assert_eq!(msg.content, "Hello");
        assert!(msg.tool_calls.is_empty());
    }

    /// Прогон одного SSE-потока через декодер; возвращает usage из `Done`.
    fn decode_done_usage(raw: &str) -> Usage {
        let mut decoder = SseDecoder::default();
        let mut acc = StreamAcc::default();
        let events = decoder.feed(raw.as_bytes(), &mut acc).expect("feed");
        events
            .into_iter()
            .find_map(|e| match e {
                LlmEvent::Done(u) => Some(u),
                LlmEvent::Delta(_) | LlmEvent::ReasoningDelta(_) | LlmEvent::Note(_) => None,
            })
            .expect("событие Done")
    }

    #[test]
    fn sse_decoder_maps_deepseek_cache_hit_tokens() {
        // Форма DeepSeek: `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
        // в финальном usage-чанке.
        let raw = "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":100,\"completion_tokens\":5,\"prompt_cache_hit_tokens\":80,\"prompt_cache_miss_tokens\":20}}\n\ndata: [DONE]\n\n";
        let usage = decode_done_usage(raw);
        assert_eq!(usage.cached_tokens, Some(80));
        assert_eq!(usage.prompt_tokens, 100);
        assert_eq!(usage.completion_tokens, 5);
    }

    #[test]
    fn sse_decoder_maps_openai_prompt_tokens_details() {
        // Форма OpenAI: вложенный объект `prompt_tokens_details.cached_tokens`.
        let raw = "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":100,\"completion_tokens\":5,\"prompt_tokens_details\":{\"cached_tokens\":42}}}\n\ndata: [DONE]\n\n";
        let usage = decode_done_usage(raw);
        assert_eq!(usage.cached_tokens, Some(42));
    }

    #[test]
    fn sse_decoder_without_cache_fields_yields_none() {
        // Провайдер не отдал ни одной из форм — кэш неизвестен (None).
        let raw = "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":100,\"completion_tokens\":5}}\n\ndata: [DONE]\n\n";
        let usage = decode_done_usage(raw);
        assert_eq!(usage.cached_tokens, None);
    }

    #[test]
    fn assembles_tool_call_arguments_split_across_three_chunks() {
        // Аргументы приходят тремя кусками, id/name — первым чанком.
        let raw = r#"data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"kb_search","arguments":"{\"que"}}]}}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ry\":\"арх"}}]}}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"итектура\"}"}}]}}]}

data: [DONE]

"#;
        let mut decoder = SseDecoder::default();
        let mut acc = StreamAcc::default();
        // Граница порции режет строку (и, возможно, многобайтовый UTF-8) — декодер обязан склеить.
        let mid = raw.len() / 2;
        let mut events = decoder
            .feed(&raw.as_bytes()[..mid], &mut acc)
            .expect("feed 1");
        events.extend(
            decoder
                .feed(&raw.as_bytes()[mid..], &mut acc)
                .expect("feed 2"),
        );
        assert!(
            events.iter().any(|e| matches!(e, LlmEvent::Done(_))),
            "events: {events:?}"
        );
        let msg = acc.finish();
        assert_eq!(msg.tool_calls.len(), 1, "tool_calls: {:?}", msg.tool_calls);
        let call = &msg.tool_calls[0];
        assert_eq!(call.id, "call_1");
        assert_eq!(call.name, "kb_search");
        assert_eq!(call.arguments, serde_json::json!({"query": "архитектура"}));
    }

    #[test]
    fn broken_tool_call_arguments_fall_back_to_raw_string() {
        let raw = "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c2\",\"type\":\"function\",\"function\":{\"name\":\"bash\",\"arguments\":\"{битый-json\"}}]}}]}\n\ndata: [DONE]\n\n";
        let mut decoder = SseDecoder::default();
        let mut acc = StreamAcc::default();
        decoder.feed(raw.as_bytes(), &mut acc).expect("feed");
        let msg = acc.finish();
        assert_eq!(msg.tool_calls.len(), 1);
        assert_eq!(
            msg.tool_calls[0].arguments,
            Value::String("{битый-json".to_string())
        );
    }

    #[test]
    fn finish_reason_is_captured_from_stream() {
        // finish_reason=length — признак усечения по max_tokens: агентный цикл
        // отклоняет битые аргументы с точной причиной. Чанк с finish_reason
        // обычно идёт с пустой дельтой перед [DONE].
        let raw = "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"текст\"}}]}\n\ndata: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"length\"}]}\n\ndata: [DONE]\n\n";
        let mut decoder = SseDecoder::default();
        let mut acc = StreamAcc::default();
        decoder.feed(raw.as_bytes(), &mut acc).expect("feed");
        let msg = acc.finish();
        assert_eq!(msg.content, "текст");
        assert_eq!(msg.finish_reason.as_deref(), Some("length"));
        // Обычный стоп тоже фиксируется.
        let raw = "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n";
        let mut decoder = SseDecoder::default();
        let mut acc = StreamAcc::default();
        decoder.feed(raw.as_bytes(), &mut acc).expect("feed");
        assert_eq!(acc.finish().finish_reason.as_deref(), Some("stop"));
    }

    #[test]
    fn parse_arguments_handles_empty_valid_and_broken() {
        assert_eq!(parse_arguments(""), Value::Object(serde_json::Map::new()));
        assert_eq!(parse_arguments("{\"a\": 1}"), serde_json::json!({"a": 1}));
        assert_eq!(parse_arguments("{oops"), Value::String("{oops".into()));
    }

    #[test]
    fn http_error_maps_status_and_truncates_body() {
        let body = "x".repeat(500);
        let err = http_error("test", StatusCode::UNAUTHORIZED, &body);
        let msg = match err {
            HarnessError::Llm(m) => m,
            other => panic!("ожидался Llm, получено: {other:?}"),
        };
        assert!(msg.contains("401"), "нет кода статуса: {msg}");
        assert!(
            msg.len() < 400,
            "тело не обрезано до 300 символов: {}",
            msg.len()
        );
    }

    #[test]
    fn retryable_only_on_429_and_5xx() {
        use crate::retry::{ErrorKind, RetryPolicy, classify};
        for status in [
            StatusCode::TOO_MANY_REQUESTS,
            StatusCode::INTERNAL_SERVER_ERROR,
            StatusCode::BAD_GATEWAY,
        ] {
            let kind = classify(Some(status.as_u16()), "");
            assert!(
                RetryPolicy::for_kind(kind).is_some(),
                "{status} должен ретраиться"
            );
        }
        for status in [
            StatusCode::OK,
            StatusCode::BAD_REQUEST,
            StatusCode::UNAUTHORIZED,
            StatusCode::PAYLOAD_TOO_LARGE,
        ] {
            let kind = classify(Some(status.as_u16()), "");
            assert!(
                RetryPolicy::for_kind(kind).is_none() || kind == ErrorKind::Unknown,
                "{status} не должен ретраиться"
            );
        }
    }

    #[test]
    fn debug_hides_api_key() {
        let dbg = format!("{:?}", test_provider());
        assert!(dbg.contains("test-model"), "debug: {dbg}");
        assert!(dbg.contains("https://example.test/v1"), "debug: {dbg}");
        assert!(!dbg.contains("sk-secret-dummy"), "ключ утёк в Debug: {dbg}");
    }

    #[test]
    fn new_rejects_empty_base_url_but_not_missing_key() {
        let mut cfg = ModelConfig {
            base_url: "  ".into(),
            model: "m".into(),
            api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
            ..ModelConfig::default()
        };
        let err = OpenAiCompat::new("x", &cfg).expect_err("пустой base_url");
        assert!(matches!(err, HarnessError::Llm(_)), "err: {err:?}");
        // Ключ читается лениво: конструирование без ключа — ОК, ошибка — на запросе.
        cfg.base_url = "https://example.test/v1".into();
        let provider = OpenAiCompat::new("x", &cfg).expect("без ключа конструируется");
        let err = provider.api_key().expect_err("нет ключа в окружении");
        assert!(matches!(err, HarnessError::Llm(_)), "err: {err:?}");
    }

    /// Одноразовый HTTP-сервер на loopback: читает заголовки запроса, отдаёт готовый ответ.
    async fn serve_once(response: String) -> (u16, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let port = listener.local_addr().expect("local_addr").port();
        let handle = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.expect("accept");
            let mut buf = [0u8; 4096];
            let mut request = Vec::new();
            loop {
                let n = socket.read(&mut buf).await.expect("read");
                if n == 0 {
                    break;
                }
                request.extend_from_slice(&buf[..n]);
                if request.windows(4).any(|w| w == b"\r\n\r\n") {
                    break;
                }
            }
            socket.write_all(response.as_bytes()).await.expect("write");
            socket.shutdown().await.expect("shutdown");
        });
        (port, handle)
    }

    fn loopback_provider(port: u16) -> OpenAiCompat {
        OpenAiCompat {
            base_url: format!("http://127.0.0.1:{port}"),
            ..test_provider()
        }
    }

    #[tokio::test]
    async fn maps_http_401_response_to_llm_error() {
        let body = r#"{"error":{"message":"invalid authentication credentials"}}"#;
        let response = format!(
            "HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let (port, server) = serve_once(response).await;
        let provider = loopback_provider(port);
        let err = provider
            .complete(ChatRequest::chat(vec![ChatMessage::user("hi")]))
            .await
            .expect_err("ожидалась ошибка 401");
        server.await.expect("join");
        let msg = match err {
            HarnessError::Llm(m) => m,
            other => panic!("ожидался Llm, получено: {other:?}"),
        };
        assert!(msg.contains("401"), "нет кода статуса: {msg}");
        assert!(
            msg.contains("invalid authentication"),
            "нет выдержки тела: {msg}"
        );
    }

    #[tokio::test]
    async fn streams_deltas_and_done_over_http() {
        let body = "data: {\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"При\"}}]}\n\n\
data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"вет\"}}]}\n\n\
data: {\"choices\":[],\"usage\":{\"prompt_tokens\":7,\"completion_tokens\":3}}\n\n\
data: [DONE]\n\n";
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let (port, server) = serve_once(response).await;
        let provider = loopback_provider(port);
        let (tx, mut rx) = mpsc::channel(16);
        let msg = provider
            .stream(ChatRequest::chat(vec![ChatMessage::user("hi")]), tx)
            .await
            .expect("stream");
        server.await.expect("join");
        assert_eq!(msg.content, "Привет");
        assert!(msg.tool_calls.is_empty());
        // tx дропнут при выходе из stream(): recv дочитает буфер и вернёт None.
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }
        assert_eq!(events.len(), 3, "events: {events:?}");
        assert!(matches!(&events[0], LlmEvent::Delta(t) if t == "При"));
        assert!(matches!(&events[1], LlmEvent::Delta(t) if t == "вет"));
        assert!(
            matches!(&events[2], LlmEvent::Done(u) if *u == Usage { prompt_tokens: 7, completion_tokens: 3, cached_tokens: None })
        );
    }

    /// Сервер, обслуживающий несколько соединений подряд своими ответами
    /// (для тестов ретраев: первый ответ битый, второй — полноценный).
    async fn serve_sequence(responses: Vec<String>) -> (u16, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let port = listener.local_addr().expect("local_addr").port();
        let handle = tokio::spawn(async move {
            for response in responses {
                let (mut socket, _) = listener.accept().await.expect("accept");
                let mut buf = [0u8; 4096];
                let mut request = Vec::new();
                loop {
                    let n = socket.read(&mut buf).await.expect("read");
                    if n == 0 {
                        break;
                    }
                    request.extend_from_slice(&buf[..n]);
                    if request.windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                }
                socket.write_all(response.as_bytes()).await.expect("write");
                socket.shutdown().await.expect("shutdown");
            }
        });
        (port, handle)
    }

    /// SSE-ответ с заголовками; `declared_len` позволяет соврать о длине
    /// (больше реальной → клиент увидит обрыв тела при закрытии соединения).
    fn sse_response(body: &str, declared_len: usize) -> String {
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {declared_len}\r\nConnection: close\r\n\r\n{body}"
        )
    }

    #[tokio::test]
    async fn stream_break_before_content_is_retried_and_recovers() {
        // Первое соединение: заголовки обещают тело, но соединение закрывается
        // ни с чем — классический «error decoding response body».
        let full = "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"ок\"}}]}\n\ndata: [DONE]\n\n";
        let (port, server) =
            serve_sequence(vec![sse_response("", 4096), sse_response(full, full.len())]).await;
        let provider = loopback_provider(port);
        let (tx, _rx) = mpsc::channel(16);
        let msg = provider
            .stream(ChatRequest::chat(vec![ChatMessage::user("hi")]), tx)
            .await
            .expect("стрим обязан восстановиться ретраем");
        server.await.expect("join");
        assert_eq!(msg.content, "ок");
    }

    #[tokio::test]
    async fn stream_break_after_content_is_retried_with_note_and_recovers() {
        // Первая попытка: дельта ушла, потом обрыв (как на скриншоте —
        // модель «замолчала» посреди ответа). Вторая — полный ответ.
        // Ретрай безопасен: инструменты исполняются только после полной
        // сборки, дублей вызовов нет; фрагмент остаётся в чате + Note.
        let partial = "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"Пол\"}}]}\n\n";
        let full = "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"Полный ответ\"}}]}\n\ndata: [DONE]\n\n";
        let (port, server) = serve_sequence(vec![
            sse_response(partial, 4096),
            sse_response(full, full.len()),
        ])
        .await;
        let provider = loopback_provider(port);
        let (tx, mut rx) = mpsc::channel(16);
        let msg = provider
            .stream(ChatRequest::chat(vec![ChatMessage::user("hi")]), tx)
            .await
            .expect("стрим обязан восстановиться ретраем после частичного контента");
        server.await.expect("join");
        // Собранное сообщение — ТОЛЬКО успешная попытка (фрагмент не склеен).
        assert_eq!(msg.content, "Полный ответ");
        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }
        let notes = events
            .iter()
            .filter(|e| matches!(e, LlmEvent::Note(_)))
            .count();
        assert_eq!(notes, 1, "одна заметка о ретрае: {events:?}");
        let note = events.iter().find_map(|e| match e {
            LlmEvent::Note(t) => Some(t.as_str()),
            _ => None,
        });
        assert!(
            note.is_some_and(|t| t.contains("3 символов") && t.contains("повторяю")),
            "заметка со счётчиком фрагмента: {note:?}"
        );
    }

    #[tokio::test]
    async fn stream_break_after_content_exhaustion_returns_friendly_russian_error() {
        // Все 5 попыток обрываются после первой дельты — финальная ошибка
        // обязана объяснять причину, неполноту текста и исчерпание ретраев.
        let partial = "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"Пол\"}}]}\n\n";
        let broken: Vec<String> = (0..5).map(|_| sse_response(partial, 4096)).collect();
        let (port, server) = serve_sequence(broken).await;
        let provider = loopback_provider(port);
        let (tx, mut rx) = mpsc::channel(64);
        let err = provider
            .stream(ChatRequest::chat(vec![ChatMessage::user("hi")]), tx)
            .await
            .expect_err("ретраи исчерпаны — ошибка наверх");
        server.await.expect("join");
        let msg = err.to_string();
        assert!(msg.contains("оборвался"), "человеческая причина: {msg}");
        assert!(
            msg.contains("НЕПОЛНЫЙ"),
            "предупреждение о неполноте: {msg}"
        );
        assert!(
            msg.contains("ретраи исчерпаны"),
            "исчерпание ретраев: {msg}"
        );
        assert!(msg.contains("Повторите ход"), "подсказка действия: {msg}");
        // 4 повтора → 4 заметки (после попыток 1–4).
        let mut notes = 0;
        while let Some(ev) = rx.recv().await {
            if matches!(ev, LlmEvent::Note(_)) {
                notes += 1;
            }
        }
        assert_eq!(notes, 4, "по заметке на каждый повтор");
    }

    /// Live-прогон: `cargo test -- --ignored live_deepseek`.
    #[tokio::test]
    #[ignore = "нужен DEEPSEEK_API_KEY и доступ к api.deepseek.com"]
    async fn live_deepseek_complete() {
        let cfg = ModelConfig {
            base_url: String::new(),
            model: "deepseek-chat".into(),
            api_key_env: "DEEPSEEK_API_KEY".into(),
            max_tokens: Some(64),
            ..ModelConfig::default()
        };
        let provider = crate::llm::deepseek::provider("deepseek", &cfg).expect("provider");
        let msg = provider
            .complete(ChatRequest::chat(vec![ChatMessage::user(
                "Ответь одним словом: ок",
            )]))
            .await
            .expect("complete");
        assert!(!msg.content.is_empty());
    }
}
