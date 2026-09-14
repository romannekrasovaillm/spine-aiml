//! Локальный GPU-инвентарь и выбор вычислительного ресурса.
//!
//! Вендорский харнесс про GPU не знает ничего — он пишет код. Здесь целевая
//! функция другая: знать, **чем** гнать эксперимент, и гнать его на своём
//! железе ($0) до того, как тратить облачный бюджет. Реестр устройств —
//! единый источник истины: pre-flight, выбор ресурса и `doctor` сверяются с ним.
//!
//! КОНТРАКТ (владелец: модуль `gpu`):
//! - [`known_devices`] / [`resolve_device`] — декларативный реестр устройств;
//! - [`required_vram_gb`] / [`bytes_per_param`] — формула VRAM (веса ×2 + overhead);
//! - [`recommend`] — чистая функция выбора ресурса (local-first, детерминированная);
//! - [`parse_nvidia_smi`] — парсер CSV `nvidia-smi` (чистый, для тестов);
//! - [`detect`] — живая детекция локальных GPU (вызов `nvidia-smi`).

use std::fmt::Write as _;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

/// Overhead VRAM сверх весов, ГБ (активации + KV-cache + фрагментация).
pub const VRAM_OVERHEAD_GB: f64 = 5.0;

/// Порядок колонок `--query-gpu` для **дискретных** GPU.
///
/// КОНТРАКТ: порядок колонок здесь ОБЯЗАН совпадать с порядком чтения полей в
/// [`parse_nvidia_smi`] (name → total → used → temp → util). Парсер позиционный:
/// вставка/перестановка колонки в запросе без правки парсера молча сдвинет поля,
/// поэтому обе стороны покрыты тестом `tests::parse_order_matches_query_columns`.
pub const GPU_QUERY: &str = "name,memory.total,memory.used,temperature.gpu,utilization.gpu";

/// Порядок колонок `--query-gpu` для **unified** GPU (GB10/DGX Spark).
///
/// `memory.*` на unified-ветке возвращает `[N/A]`, поэтому она идёт ОТДЕЛЬНЫМ
/// запросом мимо [`parse_nvidia_smi`] (общий парсер отбросил бы такую строку как
/// «нет памяти»). Порядок: name → utilization.gpu → temperature.gpu.
pub const UNIFIED_QUERY: &str = "name,utilization.gpu,temperature.gpu";

/// Спецификация известного GPU-устройства.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DeviceSpec {
    /// Канонический ключ (используется в конфиге/спеке).
    pub key: &'static str,
    /// Другие написания того же устройства.
    pub aliases: &'static [&'static str],
    /// VRAM одного GPU, ГБ.
    pub vram_gb: f64,
    /// Unified memory (`GB10`/DGX Spark) или дискретная VRAM.
    pub unified: bool,
    /// Источник факта (memory-файл).
    pub source: &'static str,
}

/// Реестр устройств. Источники VRAM — память: `gb10-storm-infra-fixes`
/// (128 unified), `vastai-daily-budget` (4090/5090/6000Ada), `gpu0-memory-tightrope`
/// (A6000/A100/H100).
const DEVICES: &[DeviceSpec] = &[
    DeviceSpec {
        key: "gb10",
        aliases: &["digits", "spark"],
        vram_gb: 128.0,
        unified: true,
        source: "gb10-storm-infra-fixes",
    },
    DeviceSpec {
        key: "rtx4080super",
        aliases: &["rtx4080", "4080super", "4080"],
        vram_gb: 16.0,
        unified: false,
        source: "vastai-daily-budget",
    },
    DeviceSpec {
        key: "rtx4090",
        aliases: &["4090"],
        vram_gb: 24.0,
        unified: false,
        source: "vastai-daily-budget",
    },
    DeviceSpec {
        key: "rtx5090",
        aliases: &["5090"],
        vram_gb: 32.0,
        unified: false,
        source: "vastai-daily-budget",
    },
    DeviceSpec {
        key: "rtx6000ada",
        aliases: &["rtx6000", "6000ada", "a6000"],
        vram_gb: 48.0,
        unified: false,
        source: "gpu0-memory-tightrope",
    },
    DeviceSpec {
        key: "a100",
        aliases: &[],
        vram_gb: 80.0,
        unified: false,
        source: "gpu0-memory-tightrope",
    },
    DeviceSpec {
        key: "h100",
        aliases: &[],
        vram_gb: 80.0,
        unified: false,
        source: "gpu0-memory-tightrope",
    },
];

/// Все известные устройства.
#[must_use]
pub fn known_devices() -> &'static [DeviceSpec] {
    DEVICES
}

/// Разрешить ключ устройства (с учётом алиасов) в спецификацию.
#[must_use]
pub fn resolve_device(key: &str) -> Option<&'static DeviceSpec> {
    let k = key.to_lowercase();
    DEVICES
        .iter()
        .find(|d| d.key == k || d.aliases.iter().any(|a| *a == k))
}

/// Сопоставить имя GPU из `nvidia-smi` с устройством реестра (эвристика:
/// имя содержит ключ/алиас). Best-effort — только для аннотации `resources check`.
#[must_use]
pub fn match_detected(name: &str) -> Option<&'static DeviceSpec> {
    let n = name.to_lowercase();
    DEVICES
        .iter()
        .find(|d| n.contains(d.key) || d.aliases.iter().any(|a| n.contains(a)))
}

/// Объявленный локальный GPU из конфига (`[[gpus.local]]`).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct LocalGpu {
    /// Имя для отчётов (например `spark-44c3`).
    pub name: String,
    /// Ключ устройства в реестре (`gb10`, `rtx4080super`, …).
    pub device: String,
    /// Переопределение VRAM (иначе — из реестра).
    pub vram_gb: Option<f64>,
    /// SSH-алиас удалённого GPU (запись `Host` в `~/.ssh/config`, напр. `gb10`).
    /// `None` — GPU на этой машине (детекция локальным `nvidia-smi`).
    pub ssh: Option<String>,
}

/// Живой GPU, увиденный `nvidia-smi`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DetectedGpu {
    /// Имя (напр. `NVIDIA GeForce RTX 4080 SUPER`).
    pub name: String,
    /// Всего памяти, МБ.
    pub total_mb: u64,
    /// Занято памяти, МБ.
    pub used_mb: u64,
    /// Температура ядра, °C (`None` — `nvidia-smi` вернул `[N/A]`).
    pub temp_c: Option<u32>,
    /// Утилизация GPU, % (`None` — `nvidia-smi` вернул `[N/A]`).
    pub util_pct: Option<u32>,
}

/// Память удалённой unified-системы (GB10/DGX Spark): `nvidia-smi memory.*`
/// возвращает `[N/A]` (память общая с CPU), поэтому состояние читается из
/// `/proc/meminfo` + утилизация/температура GPU из `nvidia-smi`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnifiedMemory {
    /// Имя GPU (напр. `NVIDIA GB10`).
    pub name: String,
    /// GPU-Util, % (из `nvidia-smi`).
    pub gpu_util_pct: Option<u32>,
    /// Температура ядра, °C (`None` — `[N/A]`/не поддержано).
    pub temp_c: Option<u32>,
    /// Всего unified-памяти, МБ (`MemTotal`).
    pub total_mb: u64,
    /// Доступно unified-памяти, МБ (`MemAvailable`).
    pub avail_mb: u64,
}

/// Рекомендация ресурса для модели.
#[derive(Debug, Clone, PartialEq)]
pub struct Recommendation {
    /// Метка ресурса: `local:<имя>` или `cloud`.
    pub label: String,
    /// VRAM ресурса, ГБ.
    pub vram_gb: f64,
    /// Модель влезает в этот ресурс.
    pub fits: bool,
    /// Пояснение выбора.
    pub reason: String,
}

/// Байт на параметр для dtype весов.
#[must_use]
pub fn bytes_per_param(dtype: &str) -> f64 {
    match dtype {
        "fp8" | "int8" => 1.0,
        "fp32" | "float32" => 4.0,
        _ => 2.0, // bf16/fp16 по умолчанию
    }
}

/// Требуемая VRAM, ГБ: веса ×2 копии (train + vLLM) + overhead.
#[must_use]
pub fn required_vram_gb(params_b: f64, dtype: &str) -> f64 {
    params_b * bytes_per_param(dtype) * 2.0 + VRAM_OVERHEAD_GB
}

/// Выбрать, чем гнать модель: локальное железо ($0) первым, облако — последним.
/// Чистая функция: ранжирует локальные GPU (влезающие первыми, затем по VRAM),
/// затем добавляет облако замыкающим. `cloud_vram_gb` — VRAM облачного GPU.
#[must_use]
pub fn recommend(
    params_b: f64,
    dtype: &str,
    local: &[LocalGpu],
    cloud_vram_gb: f64,
) -> Vec<Recommendation> {
    let needed = required_vram_gb(params_b, dtype);
    let mut out: Vec<Recommendation> = local
        .iter()
        .map(|g| {
            let vram = g
                .vram_gb
                .or_else(|| resolve_device(&g.device).map(|d| d.vram_gb))
                .unwrap_or(0.0);
            let fits = vram >= needed;
            Recommendation {
                label: format!("local:{}", g.name),
                vram_gb: vram,
                fits,
                reason: if fits {
                    format!("{vram:.0} ГБ ≥ {needed:.1} ГБ — гони локально ($0)")
                } else {
                    format!("{vram:.0} ГБ < {needed:.1} ГБ — не влезает")
                },
            }
        })
        .collect();
    out.sort_by(|a, b| b.fits.cmp(&a.fits).then(b.vram_gb.total_cmp(&a.vram_gb)));
    out.push(Recommendation {
        label: "cloud".into(),
        vram_gb: cloud_vram_gb,
        fits: cloud_vram_gb >= needed,
        reason: format!("облако vast.ai ({cloud_vram_gb:.0} ГБ) — платно, $/час"),
    });
    out
}

/// Разобрать необязательное числовое поле `nvidia-smi`: `[N/A]`,
/// `[Not Supported]`, прочий мусор и пустая строка → `None` (graceful).
fn parse_opt_u32(s: &str) -> Option<u32> {
    s.trim().parse::<u32>().ok()
}

/// Разобрать CSV-вывод `nvidia-smi` (`--format=csv,noheader,nounits`) в GPU.
///
/// ПАРСЕР ПОЗИЦИОННЫЙ: порядок чтения обязан совпадать с [`GPU_QUERY`]
/// (name → total → used → temp → util). Строки с нечитаемой/нулевой памятью
/// (`memory.total` = `[N/A]`/0) пропускаются (graceful) — именно поэтому
/// unified-ветка (GB10) идёт отдельным запросом [`UNIFIED_QUERY`], иначе здесь
/// получился бы пустой вектор.
#[must_use]
pub fn parse_nvidia_smi(csv: &str) -> Vec<DetectedGpu> {
    csv.lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .filter_map(|line| {
            let mut parts = line.split(',');
            let name = parts.next()?.trim().to_string();
            let total = parts
                .next()
                .and_then(|s| s.trim().parse::<u64>().ok())
                .unwrap_or(0);
            let used = parts
                .next()
                .and_then(|s| s.trim().parse::<u64>().ok())
                .unwrap_or(0);
            let temp_c = parts.next().and_then(parse_opt_u32);
            let util_pct = parts.next().and_then(parse_opt_u32);
            (total > 0).then_some(DetectedGpu {
                name,
                total_mb: total,
                used_mb: used,
                temp_c,
                util_pct,
            })
        })
        .collect()
}

/// Живая детекция локальных GPU через `nvidia-smi`.
///
/// # Errors
/// Возвращает ошибку, если `nvidia-smi` недоступен или завершился с ошибкой.
pub fn detect() -> Result<Vec<DetectedGpu>> {
    let query = format!("--query-gpu={GPU_QUERY}");
    let out = std::process::Command::new("nvidia-smi")
        .args([query.as_str(), "--format=csv,noheader,nounits"])
        .output()
        .context("запуск nvidia-smi")?;
    if !out.status.success() {
        anyhow::bail!("nvidia-smi завершился с кодом {}", out.status);
    }
    Ok(parse_nvidia_smi(&String::from_utf8_lossy(&out.stdout)))
}

/// Выполнить read-only команду по SSH-алиасу (запись `Host` в `~/.ssh/config`)
/// и вернуть stdout. `BatchMode=yes` (без интерактивного пароля) и
/// `ConnectTimeout=10` — мёртвый хост не вешает CLI.
///
/// # Errors
/// Возвращает ошибку, если `ssh` недоступен, алиас не резолвится или команда падает.
pub fn ssh_out(alias: &str, cmd: &str) -> Result<String> {
    let out = std::process::Command::new("ssh")
        .arg("-o")
        .arg("ConnectTimeout=10")
        .arg("-o")
        .arg("BatchMode=yes")
        .arg(alias)
        .arg(cmd)
        .output()
        .with_context(|| format!("ssh {alias}: {cmd}"))?;
    if !out.status.success() {
        anyhow::bail!(
            "ssh {alias}: команда завершилась с кодом {}: {}",
            out.status,
            String::from_utf8_lossy(&out.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// Живая детекция удалённого **дискретного** GPU (`nvidia-smi memory.*`).
///
/// # Errors
/// Возвращает ошибку, если `ssh` недоступен, алиас не резолвится или `nvidia-smi` падает.
pub fn detect_remote(alias: &str) -> Result<Vec<DetectedGpu>> {
    let out = ssh_out(
        alias,
        &format!("nvidia-smi --query-gpu={GPU_QUERY} --format=csv,noheader,nounits"),
    )?;
    Ok(parse_nvidia_smi(&out))
}

/// Живая детекция удалённого **unified** GPU (GB10/DGX Spark): `nvidia-smi memory.*`
/// = `[N/A]`, поэтому память читается из `/proc/meminfo`, утилизация — из `nvidia-smi`.
/// Read-only, не грузит GPU.
///
/// # Errors
/// Возвращает ошибку, если `ssh` недоступен, алиас не резолвится или команды падают.
pub fn detect_remote_unified(alias: &str) -> Result<UnifiedMemory> {
    let smi = ssh_out(
        alias,
        &format!("nvidia-smi --query-gpu={UNIFIED_QUERY} --format=csv,noheader,nounits"),
    )?;
    let first = smi.lines().next().unwrap_or("");
    let mut cols = first.split(',');
    // Порядок чтения обязан совпадать с UNIFIED_QUERY: name → util → temp.
    let name = cols.next().unwrap_or("NVIDIA GB10").trim().to_string();
    let util = cols.next().and_then(parse_opt_u32);
    let temp = cols.next().and_then(parse_opt_u32);
    let mem = ssh_out(alias, "grep -E 'MemTotal|MemAvailable' /proc/meminfo")?;
    let (total_kb, avail_kb) = parse_meminfo(&mem);
    Ok(UnifiedMemory {
        name,
        gpu_util_pct: util,
        temp_c: temp,
        total_mb: total_kb / 1024,
        avail_mb: avail_kb / 1024,
    })
}

/// Разобрать `/proc/meminfo`: `(MemTotal_kB, MemAvailable_kB)`. Ключи стабильны
/// (не локализованы), значения — в килобайтах.
#[must_use]
pub fn parse_meminfo(text: &str) -> (u64, u64) {
    let mut total = 0_u64;
    let mut avail = 0_u64;
    for line in text.lines() {
        let mut it = line.split_whitespace();
        let key = it.next().unwrap_or("");
        let val = it.next().and_then(|v| v.parse::<u64>().ok()).unwrap_or(0);
        match key {
            "MemTotal:" => total = val,
            "MemAvailable:" => avail = val,
            _ => {}
        }
    }
    (total, avail)
}

/// Свободная VRAM по списку детектированных GPU, ГБ (total − used, saturating).
#[must_use]
pub fn free_gb(gpus: &[DetectedGpu]) -> f64 {
    let free_mb: u64 = gpus
        .iter()
        .map(|d| d.total_mb.saturating_sub(d.used_mb))
        .sum();
    free_mb as f64 / 1024.0
}

/// Живой свободный объём локального GPU, ГБ (`None` — недоступен: нет детекции/SSH).
/// Для unified-устройств — `MemAvailable`; для дискретных — сумма свободной VRAM.
#[must_use]
pub fn live_free_gb(g: &LocalGpu) -> Option<f64> {
    match &g.ssh {
        None => detect().ok().map(|gpus| free_gb(&gpus)),
        Some(alias) => {
            let unified = resolve_device(&g.device).is_some_and(|d| d.unified);
            if unified {
                detect_remote_unified(alias)
                    .ok()
                    .map(|m| m.avail_mb as f64 / 1024.0)
            } else {
                detect_remote(alias).ok().map(|gpus| free_gb(&gpus))
            }
        }
    }
}

/// Текстовый отчёт детекции: сколько GPU увидел `nvidia-smi`, их память,
/// температура и утилизация. Плейн-текст без ANSI (это lib) — читается в логе.
#[must_use]
pub fn render_detected(gpus: &[DetectedGpu]) -> String {
    if gpus.is_empty() {
        return "nvidia-smi не вернул ни одного GPU\n".into();
    }
    let mut out = String::new();
    for (i, g) in gpus.iter().enumerate() {
        let known = match_detected(&g.name);
        let tag = known.map_or("—", |d| d.key);
        let temp = g
            .temp_c
            .map_or_else(|| "темп —".to_string(), |t| format!("темп {t}°C"));
        let util = g
            .util_pct
            .map_or_else(|| "util —".to_string(), |u| format!("util {u}%"));
        let _ = writeln!(
            out,
            "  {}. {}  {} МБ ({} занято)  {temp}  {util}  [{}]",
            i, g.name, g.total_mb, g.used_mb, tag
        );
    }
    out
}

/// Текстовый отчёт unified-памяти (GB10): всего/доступно + GPU-Util и
/// температура (если `nvidia-smi` их отдал). Плейн-текст без ANSI.
#[must_use]
pub fn render_unified(m: &UnifiedMemory) -> String {
    let total_gb = m.total_mb as f64 / 1024.0;
    let avail_gb = m.avail_mb as f64 / 1024.0;
    let util = m
        .gpu_util_pct
        .map_or_else(|| "—".to_string(), |u| format!("{u}%"));
    let temp = m
        .temp_c
        .map_or_else(String::new, |t| format!(", темп {t}°C"));
    format!(
        "  {}. {}  unified {total_gb:.0} ГБ всего, {avail_gb:.0} ГБ доступно  (GPU-Util {util}{temp})\n",
        0, m.name
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_device_with_aliases() {
        assert_eq!(resolve_device("gb10").map(|d| d.vram_gb), Some(128.0));
        assert_eq!(resolve_device("spark").map(|d| d.key), Some("gb10"));
        assert_eq!(resolve_device("4080").map(|d| d.key), Some("rtx4080super"));
        assert_eq!(resolve_device("RTX4090").map(|d| d.vram_gb), Some(24.0));
        assert_eq!(resolve_device("unknown"), None);
    }

    #[test]
    fn vram_formula_matches_guidance() {
        // 3B bf16 → 3×2×2 + 5 = 17 ГБ (совпадает с «3B+ на GPU ≥ 40GB»).
        assert_eq!(required_vram_gb(3.0, "bf16"), 17.0);
        assert_eq!(bytes_per_param("fp8"), 1.0);
        assert_eq!(bytes_per_param("fp32"), 4.0);
        assert_eq!(bytes_per_param("bf16"), 2.0);
    }

    #[test]
    fn recommend_prefers_local_that_fits() {
        let local = vec![
            LocalGpu {
                name: "rtx4080super".into(),
                device: "rtx4080super".into(),
                vram_gb: None,
                ssh: None,
            },
            LocalGpu {
                name: "gb10".into(),
                device: "gb10".into(),
                vram_gb: None,
                ssh: None,
            },
        ];
        // 1.5B → 11 ГБ: 4080 (16) влезает, gb10 (128) влезает, облако (24) влезает.
        let recs = recommend(1.5, "bf16", &local, 24.0);
        assert_eq!(recs.len(), 3);
        assert!(recs[0].fits && recs[1].fits);
        assert_eq!(recs[2].label, "cloud", "облако замыкает список");
        // Влезающие идут раньше, среди них — по убыванию VRAM (gb10 128 > 4080 16).
        assert_eq!(recs[0].label, "local:gb10");
        assert_eq!(recs[1].label, "local:rtx4080super");
    }

    #[test]
    fn recommend_flags_over_capacity_local() {
        let local = vec![LocalGpu {
            name: "rtx4080super".into(),
            device: "rtx4080super".into(),
            vram_gb: None,
            ssh: None,
        }];
        // 70B → 285 ГБ: 4080 (16) не влезает, облако (80) не влезает.
        let recs = recommend(70.0, "bf16", &local, 80.0);
        assert!(!recs.iter().all(|r| r.fits));
        assert!(!recs[0].fits);
    }

    #[test]
    fn parse_nvidia_smi_parses_typical_lines() {
        let csv = "NVIDIA GeForce RTX 4080 SUPER, 16376, 512, 42, 4\nNVIDIA RTX 4090, 24564, 2048, 55, 91\n";
        let gpus = parse_nvidia_smi(csv);
        assert_eq!(gpus.len(), 2);
        assert_eq!(gpus[0].total_mb, 16376);
        assert_eq!(gpus[0].temp_c, Some(42));
        assert_eq!(gpus[0].util_pct, Some(4));
        assert_eq!(gpus[1].total_mb, 24564);
        assert_eq!(gpus[1].util_pct, Some(91));
        // Строки с мусором/нулём пропускаются.
        assert!(parse_nvidia_smi("bad line, 0, 0, 1, 2\n\n").is_empty());
    }

    #[test]
    fn parse_order_matches_query_columns() {
        // Закрепляем порядок колонок строкой запроса: перестановка колонки в
        // GPU_QUERY без правки парсера (или наоборот) обязана уронить этот тест.
        assert_eq!(
            GPU_QUERY,
            "name,memory.total,memory.used,temperature.gpu,utilization.gpu"
        );
        let csv = "NVIDIA GeForce RTX 4080 SUPER, 16376, 14037, 42, 4\n";
        let g = &parse_nvidia_smi(csv)[0];
        assert_eq!(g.name, "NVIDIA GeForce RTX 4080 SUPER");
        assert_eq!(g.total_mb, 16376);
        assert_eq!(g.used_mb, 14037);
        assert_eq!(g.temp_c, Some(42));
        assert_eq!(g.util_pct, Some(4));
    }

    #[test]
    fn parse_nvidia_smi_na_fields_are_graceful() {
        // Строка с [N/A] в темпе/утилизации — поля None, но GPU не теряется.
        let csv = "NVIDIA GeForce RTX 4080 SUPER, 16376, 512, [N/A], [N/A]\n\
                   NVIDIA GB10, [N/A], [N/A], [N/A], 96\n";
        let gpus = parse_nvidia_smi(csv);
        assert_eq!(gpus.len(), 1, "unified-строка (память [N/A]) отброшена");
        assert_eq!(gpus[0].temp_c, None);
        assert_eq!(gpus[0].util_pct, None);
    }

    #[test]
    fn parse_opt_u32_handles_not_supported() {
        assert_eq!(parse_opt_u32("42"), Some(42));
        assert_eq!(parse_opt_u32(" 42 "), Some(42));
        assert_eq!(parse_opt_u32("[N/A]"), None);
        assert_eq!(parse_opt_u32("[Not Supported]"), None);
        assert_eq!(parse_opt_u32(""), None);
    }

    #[test]
    fn match_detected_annotates_known_gpus() {
        assert_eq!(
            match_detected("NVIDIA GeForce RTX 4080 SUPER").map(|d| d.key),
            Some("rtx4080super")
        );
        assert_eq!(match_detected("NVIDIA GB10").map(|d| d.key), Some("gb10"));
        assert_eq!(match_detected("Intel UHD Graphics"), None);
    }

    #[test]
    fn parse_meminfo_reads_total_and_available() {
        let text = "MemTotal:       132105216 kB\nMemFree:         2097152 kB\n\
                    MemAvailable:   22000000 kB\nBuffers:          1024 kB\n";
        let (total, avail) = parse_meminfo(text);
        assert_eq!(total, 132_105_216);
        assert_eq!(avail, 22_000_000);
    }

    #[test]
    fn render_unified_shows_gb_and_util() {
        let m = UnifiedMemory {
            name: "NVIDIA GB10".into(),
            gpu_util_pct: Some(96),
            temp_c: Some(58),
            total_mb: 131_072,
            avail_mb: 21_504,
        };
        let out = render_unified(&m);
        assert!(out.contains("128 ГБ всего"));
        assert!(out.contains("21 ГБ доступно"));
        assert!(out.contains("96%"));
        assert!(out.contains("темп 58°C"));
    }

    #[test]
    fn render_unified_without_temp_is_graceful() {
        let m = UnifiedMemory {
            name: "NVIDIA GB10".into(),
            gpu_util_pct: None,
            temp_c: None,
            total_mb: 131_072,
            avail_mb: 21_504,
        };
        let out = render_unified(&m);
        assert!(out.contains("GPU-Util —"));
        assert!(!out.contains("темп"), "нет температуры — нет и упоминания");
    }

    #[test]
    fn render_detected_shows_temp_and_util() {
        let gpus = vec![DetectedGpu {
            name: "NVIDIA GeForce RTX 4080 SUPER".into(),
            total_mb: 16_376,
            used_mb: 512,
            temp_c: Some(42),
            util_pct: Some(4),
        }];
        let out = render_detected(&gpus);
        assert!(out.contains("темп 42°C"));
        assert!(out.contains("util 4%"));
        assert!(out.contains("rtx4080super"), "тег устройства сохранён");
    }

    #[test]
    fn render_detected_without_temp_is_graceful() {
        let gpus = vec![DetectedGpu {
            name: "NVIDIA GeForce RTX 4080 SUPER".into(),
            total_mb: 16_376,
            used_mb: 512,
            temp_c: None,
            util_pct: None,
        }];
        let out = render_detected(&gpus);
        assert!(out.contains("темп —"));
        assert!(out.contains("util —"));
    }

    #[test]
    fn free_gb_sums_unused_memory() {
        let gpus = vec![
            DetectedGpu {
                name: "a".into(),
                total_mb: 16_376,
                used_mb: 512,
                temp_c: Some(42),
                util_pct: Some(4),
            },
            DetectedGpu {
                name: "b".into(),
                total_mb: 16_376,
                used_mb: 16_376,
                temp_c: None,
                util_pct: None,
            },
        ];
        // (16376 − 512) + 0 = 15864 МБ.
        assert_eq!((free_gb(&gpus) * 1024.0) as u64, 15_864);
    }
}
