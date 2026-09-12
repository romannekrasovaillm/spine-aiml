//! Веб-доступ: поиск и фетч по доменным архитектурным знаниям.
//!
//! КОНТРАКТ (владелец: агент `web`):
//! - [`search`] — `DuckDuckGo` HTML (`WebConfig::search_base`), парсинг результатов
//!   (`scraper`), опциональное site:-ограничение;
//! - [`search_arch_sites`] — поиск по кураторскому списку сайтов архитектора
//!   (site:domain через тот же поисковик, опц. фильтр по именам сайтов);
//! - [`fetch`] — загрузка страницы → текст (scraper: вырезать script/style/nav,
//!   собрать заголовки/параграфы/li/code), усечение до `max_fetch_chars`;
//! - таймауты на всех запросах, User-Agent из конфига, reqwest async.

use std::collections::HashSet;
use std::fmt::Write as _;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use futures::stream::{self, StreamExt};
use scraper::{Html, Selector};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::config::{ArchSite, WebConfig};
use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Максимум результатов одного поискового запроса.
const SEARCH_LIMIT: usize = 10;
/// Максимум слитых результатов по кураторским сайтам.
const ARCH_SEARCH_LIMIT: usize = 15;
/// Параллелизм поисковых запросов по сайтам.
const ARCH_CONCURRENCY: usize = 4;

/// Теги, вырезаемые из страницы целиком (вместе с содержимым).
const NOISE_TAGS: [&str; 6] = ["script", "style", "nav", "footer", "header", "aside"];
/// Блочные теги, из которых собирается текст страницы.
const CONTENT_TAGS: [&str; 8] = ["h1", "h2", "h3", "p", "li", "pre", "code", "td"];
/// CSS-селектор контентных тегов (зеркалит [`CONTENT_TAGS`]).
const CONTENT_SELECTOR: &str = "h1, h2, h3, p, li, pre, code, td";

/// Результат поиска.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResult {
    /// Заголовок.
    pub title: String,
    /// URL.
    pub url: String,
    /// Сниппет.
    pub snippet: String,
}

/// Поиск в вебе (`DuckDuckGo` HTML).
///
/// GET `WebConfig::search_base` с параметром `q`; пустая выдача — `Ok(vec![])`,
/// а не ошибка.
///
/// # Errors
/// Сеть, таймаут, неуспешный HTTP-статус, разбор HTML.
pub async fn search(query: &str, cfg: &WebConfig) -> Result<Vec<SearchResult>> {
    let client = build_client(cfg)?;
    let resp = http_get(&client, &cfg.search_base, &[("q", query)]).await?;
    let html = resp.text().await.map_err(|e| {
        HarnessError::Web(format!("{}: чтение ответа поиска: {e}", cfg.search_base))
    })?;
    parse_search_results(&html, SEARCH_LIMIT)
}

/// Поиск, ограниченный кураторскими сайтами архитектора.
/// `sites`: подмножество имён из [`WebConfig::arch_sites`]; пустой — все.
///
/// На каждый сайт — запрос `{query} site:{domain}`; запросы идут конкурентно
/// (`buffer_unordered`), результаты сливаются с дедупликацией по URL.
///
/// # Errors
/// Сеть, таймаут; неизвестные имена сайтов в `sites`.
pub async fn search_arch_sites(
    query: &str,
    sites: &[String],
    cfg: &WebConfig,
) -> Result<Vec<SearchResult>> {
    let selected: Vec<&ArchSite> = if sites.is_empty() {
        cfg.arch_sites.iter().collect()
    } else {
        let known: Vec<&str> = cfg.arch_sites.iter().map(|s| s.name.as_str()).collect();
        let unknown: Vec<&str> = sites
            .iter()
            .map(String::as_str)
            .filter(|n| !known.contains(n))
            .collect();
        if !unknown.is_empty() {
            return Err(HarnessError::Web(format!(
                "неизвестные сайты: {}; доступные: {}",
                unknown.join(", "),
                known.join(", ")
            )));
        }
        cfg.arch_sites
            .iter()
            .filter(|s| sites.contains(&s.name))
            .collect()
    };

    let client = build_client(cfg)?;
    let queries: Vec<String> = selected
        .iter()
        .map(|s| format!("{query} site:{}", s.domain))
        .collect();
    let results: Vec<Result<Vec<SearchResult>>> = stream::iter(queries)
        .map(|q| {
            let client = &client;
            let base = cfg.search_base.as_str();
            async move {
                let resp = http_get(client, base, &[("q", q.as_str())]).await?;
                let html = resp
                    .text()
                    .await
                    .map_err(|e| HarnessError::Web(format!("{base}: чтение ответа поиска: {e}")))?;
                parse_search_results(&html, SEARCH_LIMIT)
            }
        })
        .buffer_unordered(ARCH_CONCURRENCY)
        .collect()
        .await;

    let mut merged = Vec::new();
    for batch in results {
        merged.extend(batch?);
    }
    Ok(dedup_by_url(merged, ARCH_SEARCH_LIMIT))
}

/// Загружает страницу и возвращает текстовое содержимое.
///
/// # Errors
/// Сеть, таймаут, неуспешный HTTP-статус, ответ не `text/html`.
pub async fn fetch(url: &str, cfg: &WebConfig) -> Result<String> {
    let client = build_client(cfg)?;
    let resp = http_get(&client, url, &[]).await?;
    let content_type = resp
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default()
        .to_lowercase();
    if !content_type.is_empty()
        && !content_type.contains("text/html")
        && !content_type.contains("application/xhtml")
    {
        return Err(HarnessError::Web(format!(
            "{url}: не HTML (content-type: {content_type})"
        )));
    }
    let html = resp
        .text()
        .await
        .map_err(|e| HarnessError::Web(format!("{url}: чтение ответа: {e}")))?;
    let text = html_to_text(&html)?;
    Ok(truncate_chars(&text, cfg.max_fetch_chars))
}

/// Кураторский список сайтов из конфига.
#[must_use]
pub fn curated_sites(cfg: &WebConfig) -> &[ArchSite] {
    &cfg.arch_sites
}

/// Инструменты домена: `web_search`, `web_fetch`, `web_arch_sites`.
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![
        Arc::new(WebSearchTool),
        Arc::new(WebFetchTool),
        Arc::new(WebArchSitesTool),
    ]
}

/// HTTP-клиент с таймаутом и User-Agent из конфига.
fn build_client(cfg: &WebConfig) -> Result<reqwest::Client> {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(cfg.timeout_secs))
        .user_agent(cfg.user_agent.as_str())
        .build()
        .map_err(HarnessError::Http)
}

/// GET с query-параметрами; сетевые сбои и неуспешные статусы — `HarnessError::Web` с кодом.
async fn http_get(
    client: &reqwest::Client,
    url: &str,
    query: &[(&str, &str)],
) -> Result<reqwest::Response> {
    let resp = client
        .get(url)
        .query(query)
        .send()
        .await
        .map_err(|e| HarnessError::Web(format!("{url}: {e}")))?;
    let status = resp.status();
    if !status.is_success() {
        return Err(HarnessError::Web(format!("{url}: HTTP {status}")));
    }
    Ok(resp)
}

/// Разбирает выдачу `DuckDuckGo` HTML: `.result__a` (заголовок+ссылка) и
/// `.result__snippet` внутри контейнеров `.result`.
fn parse_search_results(html: &str, limit: usize) -> Result<Vec<SearchResult>> {
    let document = Html::parse_document(html);
    let result_sel = Selector::parse(".result")
        .map_err(|e| HarnessError::Web(format!("селектор результатов: {e:?}")))?;
    let link_sel = Selector::parse(".result__a")
        .map_err(|e| HarnessError::Web(format!("селектор ссылок: {e:?}")))?;
    let snippet_sel = Selector::parse(".result__snippet")
        .map_err(|e| HarnessError::Web(format!("селектор сниппетов: {e:?}")))?;

    let mut out = Vec::new();
    for result in document.select(&result_sel) {
        if out.len() >= limit {
            break;
        }
        let Some(link) = result.select(&link_sel).next() else {
            continue;
        };
        let Some(href) = link.attr("href") else {
            continue;
        };
        let title = collapse_ws(&link.text().collect::<String>());
        let url = unwrap_duckduckgo_redirect(href);
        let snippet = result
            .select(&snippet_sel)
            .next()
            .map(|s| collapse_ws(&s.text().collect::<String>()))
            .unwrap_or_default();
        out.push(SearchResult {
            title,
            url,
            snippet,
        });
    }
    Ok(out)
}

/// Разворачивает редирект-ссылку DDG вида `/l/?uddg=<urlencoded>`
/// (или `//duckduckgo.com/l/?uddg=...`) в целевой URL. Прочие ссылки — как есть.
fn unwrap_duckduckgo_redirect(href: &str) -> String {
    let absolute = if href.starts_with("//") {
        format!("https:{href}")
    } else if href.starts_with('/') {
        format!("https://duckduckgo.com{href}")
    } else {
        href.to_string()
    };
    if let Ok(url) = reqwest::Url::parse(&absolute) {
        if url.path() == "/l/" {
            for (key, value) in url.query_pairs() {
                if key == "uddg" {
                    return value.into_owned();
                }
            }
        }
    }
    href.to_string()
}

/// Дедупликация по URL (первое вхождение выигрывает) с ограничением количества.
fn dedup_by_url(results: Vec<SearchResult>, cap: usize) -> Vec<SearchResult> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for r in results {
        if seen.insert(r.url.clone()) {
            out.push(r);
            if out.len() >= cap {
                break;
            }
        }
    }
    out
}

/// Конвертирует HTML в плоский текст: вырезает навигацию/скрипты/стили,
/// собирает заголовки, параграфы, списки, код и ячейки таблиц построчно.
fn html_to_text(html: &str) -> Result<String> {
    let document = Html::parse_document(html);
    let content_sel = Selector::parse(CONTENT_SELECTOR)
        .map_err(|e| HarnessError::Web(format!("селектор контента: {e:?}")))?;

    let mut out = String::new();
    for el in document.select(&content_sel) {
        // Пропускаем элементы внутри шумных тегов, а также вложенные контентные
        // блоки (code внутри p/pre и т.п. уже учтены текстом родителя).
        let mut skip = false;
        for ancestor in el.ancestors() {
            let Some(tag) = ancestor
                .value()
                .as_element()
                .map(scraper::node::Element::name)
            else {
                continue;
            };
            if NOISE_TAGS.contains(&tag) || CONTENT_TAGS.contains(&tag) {
                skip = true;
                break;
            }
        }
        if skip {
            continue;
        }
        let raw = el.text().collect::<String>();
        let line = if el.value().name() == "pre" {
            raw.trim().to_string()
        } else {
            collapse_ws(&raw)
        };
        if line.is_empty() {
            continue;
        }
        out.push_str(&line);
        out.push('\n');
    }
    Ok(collapse_blank_lines(&out))
}

/// Схлопывает пробельные серии в один пробел (и обрезает по краям).
fn collapse_ws(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Схлопывает серии пустых строк до одной.
fn collapse_blank_lines(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut blank_run = 0usize;
    for line in text.lines() {
        if line.trim().is_empty() {
            blank_run += 1;
            if blank_run > 1 {
                continue;
            }
        } else {
            blank_run = 0;
        }
        out.push_str(line);
        out.push('\n');
    }
    out
}

/// Усекает текст до `max_chars` символов с пометкой об усечении.
fn truncate_chars(text: &str, max_chars: usize) -> String {
    if text.chars().count() <= max_chars {
        return text.to_string();
    }
    let cut: String = text.chars().take(max_chars).collect();
    format!("{cut}\n… [усечено до {max_chars} символов]")
}

/// Аргументы инструмента `web_search`.
#[derive(Debug, Deserialize)]
struct WebSearchArgs {
    /// Поисковый запрос.
    query: String,
    /// Ограничить поиск кураторскими сайтами архитектора.
    #[serde(default)]
    arch: bool,
}

/// Инструмент `web_search`: поиск в вебе, опционально — только по сайтам архитектора.
struct WebSearchTool;

#[async_trait]
impl Tool for WebSearchTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "web_search".into(),
            description: "Поиск в вебе (DuckDuckGo). С arch=true — только по кураторским сайтам архитектора (AWS/Azure/GCP architecture centers, Fowler, microservices.io, C4, arc42, TOGAF, SEI).".into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                    "arch": {"type": "boolean", "description": "Искать только по кураторским архитектурным сайтам", "default": false}
                },
                "required": ["query"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: WebSearchArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("web_search: невалидные аргументы: {e}")))?;
        let results = if args.arch {
            search_arch_sites(&args.query, &[], &ctx.config.web).await?
        } else {
            search(&args.query, &ctx.config.web).await?
        };
        if results.is_empty() {
            return Ok(ToolOutput::ok(format!(
                "По запросу «{}» ничего не найдено.",
                args.query
            )));
        }
        let mut buf = String::new();
        for (i, r) in results.iter().enumerate() {
            // записи в String инфаллибильны
            let _ = writeln!(
                buf,
                "{}. {}\n   {}\n   {}",
                i + 1,
                r.title,
                r.url,
                r.snippet
            );
        }
        Ok(ToolOutput::ok(buf))
    }
}

/// Аргументы инструмента `web_fetch`.
#[derive(Debug, Deserialize)]
struct WebFetchArgs {
    /// URL страницы.
    url: String,
}

/// Инструмент `web_fetch`: загрузка страницы → текст.
struct WebFetchTool;

#[async_trait]
impl Tool for WebFetchTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "web_fetch".into(),
            description: "Загрузить веб-страницу и вернуть её текст (HTML → текст: без скриптов, навигации и подвалов; усекается до web.max_fetch_chars).".into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL страницы (http/https)"}
                },
                "required": ["url"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: WebFetchArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("web_fetch: невалидные аргументы: {e}")))?;
        let text = fetch(&args.url, &ctx.config.web).await?;
        Ok(ToolOutput::ok(text))
    }
}

/// Инструмент `web_arch_sites`: кураторский список сайтов архитектора.
struct WebArchSitesTool;

#[async_trait]
impl Tool for WebArchSitesTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "web_arch_sites".into(),
            description: "Список кураторских сайтов архитектора (используются при web_search с arch=true): имя, домен, URL, назначение.".into(),
            parameters: json!({"type": "object", "properties": {}}),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let _ = args;
        let sites = curated_sites(&ctx.config.web);
        let mut buf = String::from("Кураторские сайты архитектора:\n");
        for s in sites {
            // записи в String инфаллибильны
            let _ = writeln!(
                buf,
                "  {:<18} {:<28} {:<44} {}",
                s.name, s.domain, s.base_url, s.description
            );
        }
        Ok(ToolOutput::ok(buf))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unwraps_uddg_redirect_links() {
        assert_eq!(
            unwrap_duckduckgo_redirect(
                "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Farch%3Fa%3D1%26b%3D2&rut=deadbeef"
            ),
            "https://example.com/arch?a=1&b=2"
        );
        assert_eq!(
            unwrap_duckduckgo_redirect("/l/?uddg=https%3A%2F%2Fc4model.com%2F"),
            "https://c4model.com/"
        );
    }

    #[test]
    fn leaves_plain_links_untouched() {
        assert_eq!(
            unwrap_duckduckgo_redirect("https://martinfowler.com/architecture/"),
            "https://martinfowler.com/architecture/"
        );
    }

    #[test]
    fn parses_duckduckgo_html_results() {
        let html = r#"
            <html><body>
            <div class="results">
              <div class="result results_links results_links_deep web-result">
                <h2 class="result__title">
                  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fmicroservices.io%2Fpatterns&rut=abc">Microservices Patterns</a>
                </h2>
                <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">A pattern language for <b>microservices</b>.</a>
              </div>
              <div class="result results_links results_links_deep web-result">
                <h2 class="result__title">
                  <a class="result__a" href="https://c4model.com/">The C4 model</a>
                </h2>
                <a class="result__snippet">Visualising software architecture.</a>
              </div>
            </div>
            </body></html>"#;
        let results = parse_search_results(html, 10).expect("parse");
        assert_eq!(results.len(), 2);
        assert_eq!(results[0].url, "https://microservices.io/patterns");
        assert_eq!(results[0].title, "Microservices Patterns");
        assert!(results[0].snippet.contains("microservices"));
        assert_eq!(results[1].url, "https://c4model.com/");
    }

    #[test]
    fn empty_page_yields_empty_vec_not_error() {
        let results = parse_search_results("<html><body></body></html>", 10).expect("parse");
        assert!(results.is_empty());
    }

    #[test]
    fn html_to_text_drops_noise_and_keeps_content() {
        let html = r"
        <html><head><style>.x{color:red}</style><script>track()</script></head>
        <body>
          <header><h2>Меню сайта</h2></header>
          <nav><ul><li>Навигация</li></ul></nav>
          <main>
            <h1>Архитектура платежей</h1>
            <p>Сервис <code>payments</code> обрабатывает транзакции.</p>
            <ul><li>Идемпотентность</li><li>Ретраи</li></ul>
            <pre>POST /payments</pre>
          </main>
          <footer><p>Подвал</p></footer>
          <aside><p>Реклама</p></aside>
        </body></html>";
        let text = html_to_text(html).expect("text");
        assert!(text.contains("Архитектура платежей"));
        assert!(text.contains("Сервис payments обрабатывает транзакции."));
        assert!(text.contains("Идемпотентность"));
        assert!(text.contains("POST /payments"));
        for noise in [
            "Меню сайта",
            "Навигация",
            "Подвал",
            "Реклама",
            "track()",
            "color:red",
        ] {
            assert!(!text.contains(noise), "шум не вырезан: {noise}");
        }
    }

    #[test]
    fn nested_code_inside_pre_is_not_duplicated() {
        let text = html_to_text("<pre><code>fn main() {}</code></pre>").expect("text");
        assert_eq!(text.matches("fn main()").count(), 1);
    }

    #[test]
    fn collapses_blank_line_runs() {
        assert_eq!(collapse_blank_lines("a\n\n\n\n\nb\n"), "a\n\nb\n");
        assert_eq!(collapse_blank_lines("a\n\nb\n"), "a\n\nb\n");
    }

    #[test]
    fn truncates_overlong_text_with_marker() {
        let long = "x".repeat(100);
        let out = truncate_chars(&long, 10);
        assert!(out.starts_with(&"x".repeat(10)));
        assert!(out.contains("усечено"));
        assert_eq!(truncate_chars("короткий", 100), "короткий");
    }

    #[test]
    fn dedup_by_url_keeps_first_and_caps() {
        let mk = |url: &str, title: &str| SearchResult {
            title: title.into(),
            url: url.into(),
            snippet: String::new(),
        };
        let out = dedup_by_url(vec![mk("a", "first"), mk("b", "b"), mk("a", "second")], 15);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].title, "first");
        let many: Vec<SearchResult> = (0..20).map(|i| mk(&format!("u{i}"), "t")).collect();
        assert_eq!(dedup_by_url(many, 15).len(), 15);
    }

    #[test]
    fn exposes_three_tools() {
        let names: Vec<String> = tools().iter().map(|t| t.spec().name).collect();
        assert_eq!(names, ["web_search", "web_fetch", "web_arch_sites"]);
    }

    #[tokio::test]
    #[ignore = "требует сети: html.duckduckgo.com"]
    async fn live_search_returns_results() {
        let cfg = WebConfig::default();
        let results = search("event sourcing pattern", &cfg)
            .await
            .expect("search");
        assert!(!results.is_empty());
    }

    #[tokio::test]
    #[ignore = "требует сети: c4model.com"]
    async fn live_fetch_returns_text() {
        let cfg = WebConfig::default();
        let text = fetch("https://c4model.com/", &cfg).await.expect("fetch");
        assert!(text.len() > 100);
    }
}
