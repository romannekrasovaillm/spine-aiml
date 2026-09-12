//! Редакция секретов в выводе инструментов (по опыту Theseus `secrets.rs`,
//! там — переработка Codex `secrets`): `bash/read_file/web_fetch` могут
//! прочитать `.env`, конфиги с ключами, приватные ключи — и всё это уехало бы
//! в контекст модели (то есть на чужой API) и в журнал сессии. Редактор
//! маскирует типовые форматы до записи в историю.
//!
//! КОНТРАКТ (владелец: агент `agent`):
//! - [`Redactor::redact`] применяется к ЛЮБОМУ выводу инструмента до записи
//!   в историю/журнал: совпадение заменяется на `keep_prefix` + `***`;
//! - встроенные правила ([`builtin_rules`]): PEM-блоки, `*_API_KEY=…`,
//!   user:pass@ в URL, Bearer-токены, sk-/AKIA-ключи, длинные hex-токены;
//! - [`Redactor::from_env_keys`] добавляет точные значения из переменных
//!   окружения провайдеров (`DEEPSEEK_API_KEY` и др.) — литеральная подмена
//!   без regex-спецсимволов;
//! - `keep_prefix` оставляет не-секретную часть формата (`sk-`, `Bearer `),
//!   чтобы текст оставался читаемым, а модель понимала, что здесь был ключ.

use regex::Regex;

/// Минимальная длина env-значения, которое стоит маскировать (короткие
/// значения — обычно не секреты, а ложные срабатывания дороги).
const MIN_ENV_VALUE_LEN: usize = 8;

/// Переменные окружения провайдеров харнесса (точные значения — в редакцию).
const PROVIDER_KEY_ENVS: &[&str] = &[
    "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY",
    "KIMI_API_KEY",
    "ZHIPU_API_KEY",
    "GLM_API_KEY",
    "ZAI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
];

/// Одно правило редакции: имя, regex и длина сохраняемого префикса.
#[derive(Debug, Clone)]
pub struct SecretRule {
    /// Имя правила (для диагностики и тестов).
    pub name: String,
    /// Скомпилированный шаблон совпадения.
    pub regex: Regex,
    /// Сколько символов совпадения оставить открытыми (не-секретная часть
    /// формата: `sk-`, `Bearer `, `AKIA`, 8 hex как git-хэш).
    pub keep_prefix: usize,
}

impl SecretRule {
    /// Компилирует правило из шаблона.
    ///
    /// # Errors
    /// Невалидный regex-шаблон.
    pub fn new(
        name: impl Into<String>,
        pattern: &str,
        keep_prefix: usize,
    ) -> Result<Self, regex::Error> {
        Ok(Self {
            name: name.into(),
            regex: Regex::new(pattern)?,
            keep_prefix,
        })
    }
}

/// Встроенные правила: типовые форматы секретов.
///
/// # Panics
/// Паника при невалидном шаблоне — это ошибка программиста в исходнике,
/// а не входных данных; тест `builtin_rules_compile` гарантирует
/// недостижимость.
#[must_use]
pub fn builtin_rules() -> Vec<SecretRule> {
    let compile =
        |name: &str, pattern: &str, keep: usize| match SecretRule::new(name, pattern, keep) {
            Ok(rule) => rule,
            Err(err) => panic!("невалидный встроенный шаблон `{name}`: {err}"),
        };
    vec![
        // Многострочный PEM-блок целиком; `(?s)` — точка захватывает \n.
        compile(
            "pem-private-key",
            r"(?s)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            0,
        ),
        // `SOME_API_KEY=значение` / `SOME_TOKEN=…` (опционально в кавычках):
        // сохраняем имя переменной и `=` — видно, ЧТО за секрет скрыт.
        compile(
            "env-api-key",
            r#"(?i)\b[A-Z][A-Z0-9_]*(_API_KEY|_TOKEN|_SECRET|_PASSWORD)=["']?[A-Za-z0-9._\-/+]{8,}"#,
            0, // keep_prefix вычисляется динамически в redact_env
        ),
        // userinfo в URL: схема остаётся, user:pass маскируется
        // (пароль от 4 символов — не цеплять экзотику `://a:b@`).
        compile("url-password", r"://[^/\s:@]{1,64}:[^@\s/]{4,}@", 3),
        // Bearer-токен: keep = "Bearer" + один пробел.
        compile("bearer-token", r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}", 7),
        // OpenAI-формат: keep = "sk-".
        compile("openai-api-key", r"\bsk-[A-Za-z0-9_-]{20,}", 3),
        // AWS Access Key ID: keep = "AKIA".
        compile("aws-access-key-id", r"\bAKIA[0-9A-Z]{16}\b", 4),
        // Длинный hex (токены/секреты 32+): keep = 8 символов, как git-хэш.
        compile("hex-token", r"\b[0-9a-fA-F]{32,}\b", 8),
    ]
}

/// Редактор секретов: набор правил + точные значения из окружения.
///
/// Дешёв в клонировании (regex делит внутренний кэш), потокобезопасен
/// (`Regex: Send + Sync`) — один `Redactor` на сессию.
#[derive(Debug, Clone)]
pub struct Redactor {
    rules: Vec<SecretRule>,
    /// Точные значения секретов из env (литеральная замена).
    env_values: Vec<String>,
}

impl Redactor {
    /// Редактор из произвольного набора правил (без env-значений).
    #[must_use]
    pub fn new(rules: Vec<SecretRule>) -> Self {
        Self {
            rules,
            env_values: Vec::new(),
        }
    }

    /// Редактор со встроенными правилами (без env-значений).
    #[must_use]
    pub fn with_builtin_rules() -> Self {
        Self::new(builtin_rules())
    }

    /// Встроенные правила + точные значения переменных окружения
    /// провайдеров ([`PROVIDER_KEY_ENVS`], только значения длиной
    /// ≥ [`MIN_ENV_VALUE_LEN`]) — такие значения маскируются где бы
    /// ни встретились, даже без маркеров формата.
    #[must_use]
    pub fn from_environment() -> Self {
        let mut r = Self::with_builtin_rules();
        r.env_values = PROVIDER_KEY_ENVS
            .iter()
            .filter_map(|k| std::env::var(k).ok())
            .filter(|v| v.len() >= MIN_ENV_VALUE_LEN)
            .collect();
        r
    }

    /// Маскирует секреты в тексте: совпадение → `keep_prefix` + `***`.
    /// Env-значения заменяются литерально на `***`.
    #[must_use]
    pub fn redact(&self, text: &str) -> String {
        let mut out = text.to_string();
        for value in &self.env_values {
            if out.contains(value) {
                out = out.replace(value.as_str(), "***");
            }
        }
        for rule in &self.rules {
            out = rule
                .regex
                .replace_all(&out, |caps: &regex::Captures<'_>| {
                    let m = caps.get(0).map_or("", |m| m.as_str());
                    let keep = if rule.name == "env-api-key" {
                        // Для `NAME=значение` сохраняем `NAME=`.
                        m.find('=').map_or(rule.keep_prefix, |eq| eq + 1)
                    } else {
                        rule.keep_prefix
                    };
                    let prefix: String = m.chars().take(keep).collect();
                    format!("{prefix}***")
                })
                .into_owned();
        }
        out
    }

    /// Есть ли в тексте что-то похожее на секрет (без модификации).
    #[must_use]
    pub fn contains_secret(&self, text: &str) -> bool {
        self.env_values.iter().any(|v| text.contains(v))
            || self.rules.iter().any(|r| r.regex.is_match(text))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builtin_rules_compile() {
        let rules = builtin_rules();
        assert!(rules.len() >= 7);
    }

    #[test]
    fn redacts_env_assignment_keeping_name() {
        let r = Redactor::with_builtin_rules();
        let out = r.redact("DEEPSEEK_API_KEY=sk-abcdef1234567890xyz в конфиге");
        assert!(out.contains("DEEPSEEK_API_KEY=***"), "имя сохранено: {out}");
        assert!(!out.contains("abcdef"), "значение скрыто: {out}");
    }

    #[test]
    fn redacts_pem_block() {
        let r = Redactor::with_builtin_rules();
        let pem = "-----BEGIN PRIVATE KEY-----\nMIIEvwIBADA\n-----END PRIVATE KEY-----";
        let out = r.redact(&format!("ключ: {pem} конец"));
        assert!(!out.contains("MIIEvw"), "тело ключа скрыто: {out}");
        assert!(out.contains("***"), "маска на месте: {out}");
    }

    #[test]
    fn redacts_url_password_bearer_and_sk() {
        let r = Redactor::with_builtin_rules();
        let out = r.redact("postgres://admin:s3cretpass@db.local:5432/core");
        assert!(out.contains("://***"), "userinfo скрыт: {out}");
        assert!(!out.contains("s3cretpass"), "пароль не протёк: {out}");
        let out = r.redact("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6");
        assert!(out.starts_with("Authorization: Bearer ***"), "{out}");
        let out = r.redact("key = sk-0123456789abcdef0123456789");
        assert!(out.contains("sk-***"), "{out}");
    }

    #[test]
    fn redacts_aws_and_hex_but_keeps_short_hex() {
        let r = Redactor::with_builtin_rules();
        let out = r.redact("AKIAIOSFODNN7EXAMPLE");
        assert_eq!(out, "AKIA***");
        let out = r.redact("токен 0123456789abcdef0123456789abcdef тут");
        assert!(out.contains("01234567***"), "{out}");
        // Короткий git-хэш (7-8 символов) — не секрет.
        let out = r.redact("коммит abc1234");
        assert_eq!(out, "коммит abc1234");
    }

    #[test]
    fn env_values_redacted_literally() {
        // Имитируем через unsafe-free путь: проверяем механику напрямую.
        let mut r = Redactor::with_builtin_rules();
        r.env_values.push("суперсекретно123".to_string());
        let out = r.redact("токен суперсекретно123 в середине строки");
        assert_eq!(out, "токен *** в середине строки");
        assert!(r.contains_secret("суперсекретно123"));
        assert!(!r.contains_secret("обычный текст"));
    }

    #[test]
    fn ordinary_text_passes_through() {
        let r = Redactor::with_builtin_rules();
        let text = "cargo test: 250 passed; контекст 60k токенов; порт 10.0.0.1:8080";
        assert_eq!(r.redact(text), text);
    }
}
