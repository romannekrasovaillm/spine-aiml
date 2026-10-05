//! Знания и библиотека: локальная база знаний (`kb`), библиотека скиллов
//! (`skills list|search|show`), плагины — единица распространения знаний
//! (`plugins list|show`, AD-10: скиллы + MCP в одном пакете), веб-доступ
//! (`web`: поиск, фетч, кураторские сайты архитектора).

use anyhow::{Context, Result};
use clap::Subcommand;

use arch_harness::config::Config;

#[derive(Subcommand)]
pub(crate) enum SkillsCmd {
    /// Список всех скиллов библиотеки.
    List,
    /// Поиск по скиллам.
    Search {
        /// Запрос.
        query: String,
        /// Максимум результатов.
        #[arg(long, default_value_t = 8)]
        limit: usize,
    },
    /// Показать полный текст скилла.
    Show {
        /// Точное имя скилла.
        name: String,
    },
}

#[derive(Subcommand)]
pub(crate) enum PluginsCmd {
    /// Список плагинов.
    List,
    /// Подробности плагина (манифест, скиллы, MCP-серверы).
    Show {
        /// Имя плагина.
        name: String,
    },
}

#[derive(Subcommand)]
pub(crate) enum WebCmd {
    /// Поиск в вебе.
    Search {
        /// Запрос.
        query: String,
        /// Ограничить кураторскими архитектурными сайтами.
        #[arg(long)]
        arch: bool,
    },
    /// Загрузить страницу текстом.
    Fetch {
        /// URL.
        url: String,
    },
    /// Кураторский список сайтов архитектора.
    Sites,
}

/// `arch-ml kb`: поиск по локальной базе знаний.
pub(crate) async fn cmd_kb(cfg: &Config, query: &str, limit: usize) -> Result<()> {
    let hits =
        arch_harness::kb::search(&cfg.knowledge.dirs, &cfg.knowledge.extensions, query, limit)
            .await?;
    for hit in &hits {
        outln!(
            "── {}:{} (score {:.1})",
            hit.path.display(),
            hit.line,
            hit.score
        );
        outln!("{}", hit.snippet);
    }
    if hits.is_empty() {
        outln!("Ничего не найдено.");
    }
    Ok(())
}

/// `arch-ml web`: поиск и фетч по архитектурным сайтам.
pub(crate) async fn cmd_web(cfg: &Config, cmd: WebCmd) -> Result<()> {
    match cmd {
        WebCmd::Search { query, arch } => {
            let results = if arch {
                arch_harness::web::search_arch_sites(&query, &[], &cfg.web).await?
            } else {
                arch_harness::web::search(&query, &cfg.web).await?
            };
            for r in &results {
                outln!("• {}\n  {}\n  {}\n", r.title, r.url, r.snippet);
            }
            if results.is_empty() {
                outln!("Ничего не найдено.");
            }
        }
        WebCmd::Fetch { url } => {
            let text = arch_harness::web::fetch(&url, &cfg.web).await?;
            outln!("{text}");
        }
        WebCmd::Sites => {
            outln!("Кураторские сайты архитектора:");
            for s in arch_harness::web::curated_sites(&cfg.web) {
                outln!("  {:<16} {:<40} {}", s.name, s.base_url, s.description);
            }
        }
    }
    Ok(())
}

/// `arch-ml skills`: библиотека скиллов.
pub(crate) fn cmd_skills(cfg: &Config, cmd: SkillsCmd) -> Result<()> {
    let plugins = arch_harness::plugin::discover(&cfg.plugins.dirs);
    match cmd {
        SkillsCmd::List => {
            let total: usize = plugins.iter().map(|p| p.skills.len()).sum();
            outln!("Скиллов: {total} в {} плагинах", plugins.len());
            for p in &plugins {
                for s in &p.skills {
                    outln!(
                        "  {:<28} {:<14} {}",
                        s.name,
                        p.manifest.name,
                        first_line(&s.description, 80)
                    );
                }
            }
        }
        SkillsCmd::Search { query, limit } => {
            let hits = arch_harness::plugin::search(&plugins, &query, limit);
            if hits.is_empty() {
                outln!(
                    "Ничего не найдено (скиллов в индексе: {}).",
                    plugins.iter().map(|p| p.skills.len()).sum::<usize>()
                );
            }
            for h in &hits {
                outln!(
                    "── {} [{}] (score {:.1})",
                    h.meta.name,
                    h.meta.plugin,
                    h.score
                );
                outln!("   {}", first_line(&h.meta.description, 100));
                if !h.snippet.is_empty() {
                    outln!("{}", h.snippet);
                }
            }
        }
        SkillsCmd::Show { name } => {
            let meta = arch_harness::plugin::skill_by_name(&plugins, &name)
                .with_context(|| format!("скилл '{name}' не найден (см. `arch-ml skills list`)"))?;
            outln!("{}", arch_harness::plugin::load_skill(meta)?);
        }
    }
    Ok(())
}

/// `arch-ml plugins`: пакеты скиллов + MCP.
pub(crate) fn cmd_plugins(cfg: &Config, cmd: PluginsCmd) -> Result<()> {
    let plugins = arch_harness::plugin::discover(&cfg.plugins.dirs);
    match cmd {
        PluginsCmd::List => {
            outln!("Плагины ({}):", plugins.len());
            for p in &plugins {
                let mcp_count = if cfg.plugins.include_mcp {
                    arch_harness::plugin::mcp_servers(std::slice::from_ref(p)).len()
                } else {
                    0
                };
                outln!(
                    "  {:<24} v{:<8} скиллов: {:<3} mcp: {:<2} {}",
                    p.manifest.name,
                    p.manifest.version,
                    p.skills.len(),
                    mcp_count,
                    first_line(&p.manifest.description, 60)
                );
            }
        }
        PluginsCmd::Show { name } => {
            let p = plugins
                .iter()
                .find(|p| p.manifest.name == name)
                .with_context(|| format!("плагин '{name}' не найден"))?;
            outln!(
                "{} v{} — {}",
                p.manifest.name,
                p.manifest.version,
                p.manifest.description
            );
            outln!("Каталог: {}", p.dir.display());
            if !p.manifest.keywords.is_empty() {
                outln!("Ключевые слова: {}", p.manifest.keywords.join(", "));
            }
            outln!("Скиллы ({}):", p.skills.len());
            for s in &p.skills {
                outln!("  {:<28} {}", s.name, first_line(&s.description, 70));
            }
            let servers = arch_harness::plugin::mcp_servers(std::slice::from_ref(p));
            if !servers.is_empty() {
                outln!("MCP-серверы ({}):", servers.len());
                for s in &servers {
                    outln!("  {:<24} {} {}", s.name, s.command, s.args.join(" "));
                }
            }
        }
    }
    Ok(())
}

/// Первая строка текста, усечённая до `max` символов.
fn first_line(text: &str, max: usize) -> String {
    let line = text.lines().next().unwrap_or("").trim();
    let cut: String = line.chars().take(max).collect();
    if line.chars().count() > max {
        format!("{cut}…")
    } else {
        cut
    }
}
