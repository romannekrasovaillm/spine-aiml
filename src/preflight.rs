//! Pre-flight gate для ML-эксперимента: детерминированные проверки ДО аренды GPU.
//!
//! Вендорские харнессы решают «написать код». Здесь целевая функция другая —
//! «не потратить GPU-деньги на ошибку, которую уже однажды совершили». Каждый
//! гейт — сжатое, исполняемое правило из доменного war-chest (память
//! `war-chest память ML-контура`), а не проза в контексте модели:
//! харнесс принуждает, а не напоминает. Проверки чистые (не зовут сеть, не
//! трогают клауд) — стоимость pre-flight $0.
//!
//! КОНТРАКТ (владелец: модуль `preflight`):
//! - [`run_preflight`] — чистое ядро: [`ExperimentSpec`] → [`Vec<Gate>`];
//! - [`render`] / [`render_json`] — текстовый / машинный отчёт;
//! - [`exit_code`] — 1 при хотя бы одном [`Verdict::Fail`] (для CLI);
//! - [`parse_spec`] / [`parse_spec_str`] — чтение TOML-спецификации;
//! - [`example_spec`] — образец спецификации (`arch-ml preflight --example`).

use std::fmt::Write as _;
use std::path::Path;

use anyhow::{Context, Result};
use serde::Deserialize;

/// Вердикт одного pre-flight гейта.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    /// Всё в порядке.
    Ok,
    /// Работать можно, но риск не исключён.
    Warn,
    /// Стоп: по опыту эта конфигурация уже ломалась.
    Fail,
}

impl Verdict {
    /// Иконка вердикта для отчёта.
    #[must_use]
    pub const fn icon(self) -> &'static str {
        match self {
            Self::Ok => "✓",
            Self::Warn => "⚠",
            Self::Fail => "✗",
        }
    }

    /// Строковый ключ вердикта (для JSON).
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Warn => "warn",
            Self::Fail => "fail",
        }
    }
}

/// Результат одного pre-flight гейта.
#[derive(Debug, Clone)]
pub struct Gate {
    /// Короткое имя гейта (колонка отчёта).
    pub name: &'static str,
    /// Вердикт.
    pub verdict: Verdict,
    /// Пояснение: что проверено / что не так / как чинить.
    pub text: String,
    /// Источник правила — имя memory-файла (аудируемость: «из какого урока»).
    pub source: &'static str,
}

/// Спецификация ML-эксперимента (вход pre-flight). Читается из TOML.
/// Все поля необязательны: отсутствие поля трактуется гейтом как «нельзя
/// проверить» (`Warn`), а не как молчаливый пропуск.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct ExperimentSpec {
    /// Имя эксперимента (для заголовка отчёта).
    pub name: String,
    /// Число параметров модели, млрд.
    pub params_b: Option<f64>,
    /// dtype весов: `bf16`/`fp16` (2 байта/параметр), `fp8` (1), `fp32` (4).
    pub dtype: Option<String>,
    /// Имя модели (например `Qwen2.5-0.5B-Instruct`) — для гейта chat-формата.
    pub model_name: Option<String>,
    /// Количество GPU.
    pub num_gpus: Option<u32>,
    /// VRAM одного GPU, ГБ.
    pub gpu_vram_gb: Option<f64>,
    /// ID хоста vast.ai (если аренда).
    pub host_id: Option<String>,
    /// Источник ресурса: `local` (своё железо, $0 аренды) или `cloud` (vast.ai).
    /// Влияет на применимость гейтов бюджета и чёрного списка хостов.
    pub resource: Option<String>,
    /// Известное устройство — автозаполняет VRAM, если `gpu_vram_gb` не задан:
    /// `gb10` (DGX Spark, 128 ГБ unified), `rtx4080super` (16 ГБ), `rtx4090`
    /// (24), `rtx5090` (32), `rtx6000ada`/`a6000` (48), `a100`/`h100` (80).
    pub device: Option<String>,
    /// Фреймворк: `verl`, `trl`, `alfworld`, `grpo`, `custom`…
    pub framework: Option<String>,
    /// Версии стека (для ABI-гейта).
    pub verl_version: Option<String>,
    pub vllm_version: Option<String>,
    pub torch_version: Option<String>,
    pub cuda_version: Option<String>,
    /// Docker-образ.
    pub image: Option<String>,
    /// `gpu_memory_utilization` — доля VRAM под vLLM KV-cache (0..1).
    pub gpu_memory_utilization: Option<f64>,
    /// Seed (воспроизводимость).
    pub seed: Option<u64>,
    /// `training_method` для `ALFWorld`: `dqn`, НЕ `rl`.
    pub training_method: Option<String>,
    /// `kl_coef` для GRPO: `0.01` против mode collapse.
    pub kl_coef: Option<f64>,
    /// Формат промпта: `chat` (список dict) или `plain` (строка) — для Qwen.
    pub prompt_format: Option<String>,
    /// Цена инстанса, $/час.
    pub price_per_hour: Option<f64>,
    /// Ожидаемая длительность, часы.
    pub expected_hours: Option<f64>,
    /// Дневной лимит бюджета, $ (дефолт 10).
    pub daily_budget: Option<f64>,
}

/// Единственная проверенная комбинация veRL-стека
/// (`verl-version-compatibility-matrix`, 30+ запусков).
const VERIFIED_VERL: &str = "0.4.0";
const VERIFIED_VLLM: &str = "0.8.3";
const VERIFIED_TORCH: &str = "2.6.0";
const VERIFIED_CUDA: &str = "12.4";
/// Единственный образ с верным ABI (`cxx11abi0`).
const VERIFIED_IMAGE: &str = "hiyouga/verl:ngc-th2.6.0-cu126-vllm0.8.3-flashinfer0.2.2-cxx11abi0";

/// Хосты, исключённые по опыту (`vastai-cli-patterns`, `broken-host-14804`).
const SLOW_HOSTS: &[&str] = &["26537133", "33613899"];
const BLACKLISTED_HOSTS: &[&str] = &["14804"];
const FAST_HOSTS: &[&str] = &["38955740", "38955836", "38955896"];

/// Дневной бюджет по умолчанию, $ (`vastai-daily-budget`, hard cap).
const DEFAULT_DAILY_BUDGET: f64 = 10.0;

/// Источник ресурса: своё железо vs облако vs не указано.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Resource {
    Local,
    Cloud,
    Unknown,
}

/// Спецификация устройства из реестра [`crate::gpu`] (по полю `device` спеки).
fn device_spec(spec: &ExperimentSpec) -> Option<&'static crate::gpu::DeviceSpec> {
    spec.device.as_deref().and_then(crate::gpu::resolve_device)
}

/// Устройство — локальный GB10 (DGX Spark), со своим режимом эксплуатации.
fn is_gb10(spec: &ExperimentSpec) -> bool {
    device_spec(spec).is_some_and(|d| d.key == "gb10")
}

/// Эффективный источник ресурса: явный `resource` > сигналы облака > GB10.
fn effective_resource(spec: &ExperimentSpec) -> Resource {
    match spec.resource.as_deref() {
        Some("local") => Resource::Local,
        Some("cloud") => Resource::Cloud,
        _ => {
            if spec.host_id.is_some() || spec.price_per_hour.is_some() {
                Resource::Cloud
            } else if is_gb10(spec) {
                Resource::Local
            } else {
                Resource::Unknown
            }
        }
    }
}

/// Все pre-flight гейты по спецификации. Чистая функция — без вывода и сети.
/// Неприменимые гейты не эмитятся (отчёт не шумит по несущественным осям).
#[must_use]
pub fn run_preflight(spec: &ExperimentSpec) -> Vec<Gate> {
    if spec.name.is_empty()
        && spec.params_b.is_none()
        && spec.num_gpus.is_none()
        && spec.framework.is_none()
        && spec.model_name.is_none()
        && spec.device.is_none()
        && spec.resource.is_none()
    {
        return vec![Gate {
            name: "spec",
            verdict: Verdict::Warn,
            text: "спецификация пуста — нечего проверять (см. --example)".into(),
            source: "preflight",
        }];
    }
    let mut gates = vec![gate_resource(spec)];
    gates.extend(
        [
            gate_versions(spec),
            gate_image(spec),
            gate_vram(spec),
            gate_gpu_mem_util(spec),
            gate_host(spec),
            gate_budget(spec),
            gate_seed(spec),
            gate_alfworld(spec),
            gate_kl(spec),
            gate_prompt_format(spec),
        ]
        .into_iter()
        .flatten(),
    );
    gates
}

/// Текстовый отчёт по списку гейтов.
#[must_use]
pub fn render(gates: &[Gate]) -> String {
    let mut out = String::from("arch-ml preflight — проверки до аренды GPU\n\n");
    for g in gates {
        let _ = writeln!(
            out,
            "  {} {:<14} {}  [{}]",
            g.verdict.icon(),
            g.name,
            g.text,
            g.source
        );
    }
    let fails = gates.iter().filter(|g| g.verdict == Verdict::Fail).count();
    let warns = gates.iter().filter(|g| g.verdict == Verdict::Warn).count();
    let _ = writeln!(out);
    if fails == 0 && warns == 0 {
        let _ = writeln!(
            out,
            "Итог: готово ({} гейтов, риск не выявлен)",
            gates.len()
        );
    } else {
        let _ = writeln!(
            out,
            "Итог: {fails} стоп-ошибок, {warns} предупреждений из {} гейтов",
            gates.len()
        );
    }
    out
}

/// Машиночитаемый отчёт (JSON) по спецификации.
#[must_use]
pub fn render_json(spec: &ExperimentSpec) -> String {
    let gates = run_preflight(spec);
    let fails = gates.iter().filter(|g| g.verdict == Verdict::Fail).count();
    let warns = gates.iter().filter(|g| g.verdict == Verdict::Warn).count();
    let verdict = if fails > 0 {
        "fail"
    } else if warns > 0 {
        "warn"
    } else {
        "ok"
    };
    let gates_json: Vec<serde_json::Value> = gates
        .iter()
        .map(|g| {
            serde_json::json!({
                "name": g.name,
                "verdict": g.verdict.as_str(),
                "text": g.text,
                "source": g.source,
            })
        })
        .collect();
    serde_json::json!({
        "experiment": spec.name,
        "verdict": verdict,
        "failures": fails,
        "warnings": warns,
        "gates": gates_json,
    })
    .to_string()
}

/// Код выхода CLI: 1 при любом `Fail`, иначе 0.
#[must_use]
pub fn exit_code(gates: &[Gate]) -> i32 {
    i32::from(gates.iter().any(|g| g.verdict == Verdict::Fail))
}

/// Прочитать TOML-спецификацию из файла.
///
/// # Errors
/// Возвращает ошибку, если файл не читается или не парсится как TOML.
pub fn parse_spec(path: &Path) -> Result<ExperimentSpec> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("чтение спецификации {}", path.display()))?;
    parse_spec_str(&text).with_context(|| format!("разбор спецификации {}", path.display()))
}

/// Разобрать TOML-спецификацию из строки (чисто, для тестов).
///
/// # Errors
/// Возвращает ошибку парсера TOML при невалидном вводе.
pub fn parse_spec_str(text: &str) -> Result<ExperimentSpec> {
    let spec: ExperimentSpec = toml::from_str(text).context("спецификация не TOML")?;
    Ok(spec)
}

/// Образец TOML-спецификации (`arch-ml preflight --example`).
#[must_use]
pub fn example_spec() -> String {
    r#"# Pre-flight спецификация ML-эксперимента (arch-ml preflight SPEC.toml)
# Все поля необязательны: что не указано — тот гейт проверить не сможет (Warn).

name = "grpo-alfworld-3b"

# Модель
params_b = 3.0          # млрд параметров
dtype = "bf16"          # bf16|fp16|fp8|fp32
model_name = "Qwen2.5-3B-Instruct"

# Железо / аренда
resource = "cloud"      # "local" (своё железо, $0) | "cloud" (vast.ai)
# device = "gb10"       # автозаполняет VRAM: gb10=128 (unified), rtx4080super=16,
                        # rtx4090=24, rtx5090=32, rtx6000ada/a6000=48, a100/h100=80
num_gpus = 2
gpu_vram_gb = 40.0      # VRAM одного GPU (явный важнее device)
host_id = "38955836"    # vast.ai host (проверяем чёрный список)

# Стек
framework = "verl"
verl_version = "0.4.0"
vllm_version = "0.8.3"
torch_version = "2.6.0"
cuda_version = "12.4"
image = "hiyouga/verl:ngc-th2.6.0-cu126-vllm0.8.3-flashinfer0.2.2-cxx11abi0"
gpu_memory_utilization = 0.45

# Воспроизводимость
seed = 42

# ALFWorld / RL-специфика
training_method = "dqn" # НЕ "rl" (NotImplementedError в alfred_tw_env.py)
kl_coef = 0.01          # против mode collapse
prompt_format = "chat"  # для Qwen: chat, не plain

# Бюджет ($10/день — hard cap)
price_per_hour = 0.35
expected_hours = 6.0
daily_budget = 10.0
"#
    .to_string()
}

/// Гейт источника ресурса: локальное железо ($0) vs облако vs не указано.
fn gate_resource(spec: &ExperimentSpec) -> Gate {
    let resource = effective_resource(spec);
    let device = spec.device.as_deref();
    let vram = device_spec(spec).map(|d| d.vram_gb);
    match resource {
        Resource::Local => {
            let (verdict, text) = match (device, vram) {
                (Some(d), Some(v)) if is_gb10(spec) => (
                    Verdict::Ok,
                    format!(
                        "локальный {d} (DGX Spark): unified {v:.0} ГБ, аренда $0. ⚠ NVRM-бури на UMA — держи gpu_memory_utilization≈0.32"
                    ),
                ),
                (Some(d), Some(v)) => (
                    Verdict::Ok,
                    format!("локальный {d}: {v:.0} ГБ VRAM, аренда $0 — бюджет/хост не применяются"),
                ),
                _ => (
                    Verdict::Warn,
                    "локальный ресурс не распознан — VRAM не сверен (известны: gb10, rtx4080super, rtx4090…)".into(),
                ),
            };
            Gate {
                name: "resource",
                verdict,
                text,
                source: if is_gb10(spec) {
                    "gb10-storm-infra-fixes"
                } else {
                    "ml-experiment-workflow"
                },
            }
        }
        Resource::Cloud => Gate {
            name: "resource",
            verdict: Verdict::Ok,
            text: "облачный ресурс (vast.ai) — применяются бюджет и чёрный список хостов".into(),
            source: "vastai-daily-budget",
        },
        Resource::Unknown => Gate {
            name: "resource",
            verdict: Verdict::Warn,
            text: "источник ресурса не указан (resource = \"local\"|\"cloud\") — бюджет и хосты не проверяются".into(),
            source: "ml-experiment-workflow",
        },
    }
}

/// ABI-гейт: совпадение версий veRL×vLLM×torch×CUDA с проверенной матрицей.
fn gate_versions(spec: &ExperimentSpec) -> Option<Gate> {
    let framework = spec.framework.as_deref().unwrap_or("");
    let is_verl = framework.contains("verl")
        || spec.verl_version.is_some()
        || spec.vllm_version.is_some()
        || spec.torch_version.is_some()
        || spec.cuda_version.is_some();
    if !is_verl {
        return None;
    }

    let mut mismatches: Vec<String> = Vec::new();
    if let Some(v) = &spec.verl_version
        && v != VERIFIED_VERL
    {
        mismatches.push(format!("veRL {v} (проверена {VERIFIED_VERL})"));
    }
    if let Some(v) = &spec.vllm_version
        && v != VERIFIED_VLLM
    {
        mismatches.push(format!("vLLM {v} (проверен {VERIFIED_VLLM})"));
    }
    if let Some(v) = &spec.torch_version
        && !v.starts_with(VERIFIED_TORCH)
    {
        mismatches.push(format!("torch {v} (проверен {VERIFIED_TORCH}+cu126)"));
    }
    if let Some(v) = &spec.cuda_version
        && v != VERIFIED_CUDA
    {
        mismatches.push(format!("CUDA {v} (проверена {VERIFIED_CUDA})"));
    }

    let (verdict, text) = if !mismatches.is_empty() {
        (
            Verdict::Warn,
            format!(
                "непроверенная комбинация ({}); проверена только {VERIFIED_VERL}+{VERIFIED_VLLM}+{VERIFIED_TORCH}+cu126/CUDA {VERIFIED_CUDA}",
                mismatches.join(", ")
            ),
        )
    } else if spec.verl_version.is_none() && spec.vllm_version.is_none() {
        (
            Verdict::Warn,
            format!(
                "версии veRL/vLLM не указаны — ABI не сверен (проверены: veRL {VERIFIED_VERL} + vLLM {VERIFIED_VLLM})"
            ),
        )
    } else {
        (Verdict::Ok, "проверенная комбинация (30+ запусков)".into())
    };
    Some(Gate {
        name: "versions",
        verdict,
        text,
        source: "verl-version-compatibility-matrix",
    })
}

/// Гейт образа: ABI (`cxx11abi0`) + ловушка HF-зеркала.
fn gate_image(spec: &ExperimentSpec) -> Option<Gate> {
    let framework = spec.framework.as_deref().unwrap_or("");
    let is_verl = framework.contains("verl")
        || spec.verl_version.is_some()
        || spec.vllm_version.is_some()
        || spec.image.is_some();
    if !is_verl {
        return None;
    }

    let (verdict, text) = match spec.image.as_deref() {
        Some(img) if img == VERIFIED_IMAGE => (
            Verdict::Ok,
            "проверенный образ (cxx11abi0). ⚠ HF-запросы уходят на китайское зеркало (500) — качать модели Python `snapshot_download` с `HF_ENDPOINT=https://huggingface.co`".into(),
        ),
        Some(img)
            if img.contains("pytorch/pytorch") || img.contains("nvidia/cuda") =>
        {
            (
                Verdict::Fail,
                format!("известный ABI-конфликт (cxx11abi1 vs abi0) / OOM при сборке: {img}"),
            )
        }
        Some(_) => (
            Verdict::Warn,
            format!("образ не проверен — ABI не сверен; проверенный: {VERIFIED_IMAGE}"),
        ),
        None => (
            Verdict::Warn,
            format!("образ не указан — для veRL обязателен {VERIFIED_IMAGE} (cxx11abi0)"),
        ),
    };
    Some(Gate {
        name: "image",
        verdict,
        text,
        source: "vllm-torch-abi-compatibility",
    })
}

/// Гейт VRAM: формула «веса ×2 копии (train + vLLM) + overhead» против ёмкости.
fn gate_vram(spec: &ExperimentSpec) -> Option<Gate> {
    let params = spec.params_b?;
    // Явный gpu_vram_gb важнее реестра device.
    let vram = match spec.gpu_vram_gb {
        Some(v) => Some(v),
        None => device_spec(spec).map(|d| d.vram_gb),
    }?;
    let gpus = spec.num_gpus.unwrap_or(1);
    if params <= 0.0 || vram <= 0.0 {
        return None;
    }
    let dtype = spec.dtype.as_deref().unwrap_or("bf16");
    let weights_gb = params * crate::gpu::bytes_per_param(dtype);
    let total_needed = crate::gpu::required_vram_gb(params, dtype); // train + vLLM + overhead
    let total_avail = f64::from(gpus) * vram;
    let ratio = total_needed / total_avail;
    let pct = ratio * 100.0;

    let (verdict, text) = if ratio > 1.0 {
        (
            Verdict::Fail,
            format!(
                "не влезает: нужно ≈{total_needed:.1} ГБ (веса {weights_gb:.1} ГБ ×2 + overhead {:.0} ГБ), есть {total_avail:.1} ГБ ({gpus}×{vram:.0} ГБ)",
                crate::gpu::VRAM_OVERHEAD_GB
            ),
        )
    } else if ratio > 0.85 {
        (
            Verdict::Warn,
            format!(
                "впритык: {pct:.0}% VRAM ({total_needed:.1}/{total_avail:.1} ГБ) — активации/оптимизатор оставят <15%"
            ),
        )
    } else {
        (
            Verdict::Ok,
            format!(
                "{pct:.0}% VRAM: нужно ≈{total_needed:.1} ГБ из {total_avail:.1} ГБ ({gpus}×{vram:.0} ГБ)"
            ),
        )
    };
    Some(Gate {
        name: "vram",
        verdict,
        text,
        source: "gpu0-memory-tightrope",
    })
}

/// Гейт `gpu_memory_utilization` (GPU-0 tightrope: 0.60 ловит OOM, 0.45 стабильно).
fn gate_gpu_mem_util(spec: &ExperimentSpec) -> Option<Gate> {
    match spec.gpu_memory_utilization {
        Some(u) if is_gb10(spec) && u > 0.35 => Some(Gate {
            name: "gpu-mem-util",
            verdict: Verdict::Warn,
            text: format!("{u:.2} для GB10 высоко — NVRM-бури на UMA; держи ≈0.32 (enforce_eager)"),
            source: "gb10-storm-infra-fixes",
        }),
        Some(u) if u > 0.60 => Some(Gate {
            name: "gpu-mem-util",
            verdict: Verdict::Fail,
            text: format!("{u:.2} → CUDA OOM на GPU 0 (доказано на 4×GPU, шаг 17–37); ≤0.50"),
            source: "gpu0-memory-tightrope",
        }),
        Some(u) if u > 0.50 => Some(Gate {
            name: "gpu-mem-util",
            verdict: Verdict::Warn,
            text: format!(
                "{u:.2} — риск OOM (0.45 стабильно, 0.60 ломается); NCCL-краши = замаскированный OOM"
            ),
            source: "nccl-oom-disguise",
        }),
        Some(_) => Some(Gate {
            name: "gpu-mem-util",
            verdict: Verdict::Ok,
            text: format!(
                "{} — в безопасной зоне",
                spec.gpu_memory_utilization.unwrap_or(0.0)
            ),
            source: "gpu0-memory-tightrope",
        }),
        None if spec.num_gpus.is_some_and(|n| n >= 2) => Some(Gate {
            name: "gpu-mem-util",
            verdict: Verdict::Warn,
            text: "не задан — при TP≥4 дефолт 0.60 ловит OOM; ставьте 0.45".into(),
            source: "gpu0-memory-tightrope",
        }),
        None => None,
    }
}

/// Гейт хоста: чёрный/медленный/быстрый список vast.ai.
fn gate_host(spec: &ExperimentSpec) -> Option<Gate> {
    let host = spec.host_id.as_deref()?;
    if BLACKLISTED_HOSTS.contains(&host) {
        return Some(Gate {
            name: "host",
            verdict: Verdict::Fail,
            text: format!("хост {host} исключён навсегда (6 NCCL-крашей/12ч)"),
            source: "broken-host-14804",
        });
    }
    if SLOW_HOSTS.contains(&host) {
        return Some(Gate {
            name: "host",
            verdict: Verdict::Fail,
            text: format!("хост {host} — образ грузится >25 мин («никогда не грузился»)"),
            source: "vastai-cli-patterns",
        });
    }
    if FAST_HOSTS.contains(&host) {
        return Some(Gate {
            name: "host",
            verdict: Verdict::Ok,
            text: format!("хост {host} — быстрый (образ 30–70 с)"),
            source: "vastai-cli-patterns",
        });
    }
    Some(Gate {
        name: "host",
        verdict: Verdict::Warn,
        text: format!(
            "хост {host} не в списках — надёжность не подтверждена (чёрный: 14804; медленные: 26537133, 33613899)"
        ),
        source: "vastai-cli-patterns",
    })
}

/// Гейт бюджета: смета `цена × часы` против дневного hard-cap.
fn gate_budget(spec: &ExperimentSpec) -> Option<Gate> {
    let Some(price) = spec.price_per_hour else {
        return spec.expected_hours.map(|h| Gate {
            name: "budget",
            verdict: Verdict::Warn,
            text: format!("нет цены — смета неполна (ожидается {h:.1} ч)"),
            source: "vastai-daily-budget",
        });
    };
    let cap = spec.daily_budget.unwrap_or(DEFAULT_DAILY_BUDGET);
    let Some(hours) = spec.expected_hours else {
        return Some(Gate {
            name: "budget",
            verdict: Verdict::Warn,
            text: format!(
                "${price:.2}/ч, длительность не задана — смета неполна (кап ${cap:.0}/день)"
            ),
            source: "vastai-daily-budget",
        });
    };
    let cost = price * hours;
    let (verdict, text) = if cost > cap {
        (
            Verdict::Fail,
            format!("${cost:.2} > капа ${cap:.0}/день — нужно явное согласие владельца"),
        )
    } else {
        (
            Verdict::Ok,
            format!("${cost:.2} (${price:.2}/ч × {hours:.1} ч) в рамках капа ${cap:.0}/день"),
        )
    };
    Some(Gate {
        name: "budget",
        verdict,
        text,
        source: "vastai-daily-budget",
    })
}

/// Гейт воспроизводимости: seed задан.
fn gate_seed(spec: &ExperimentSpec) -> Option<Gate> {
    let is_experiment = spec.params_b.is_some()
        || spec.num_gpus.is_some()
        || spec.framework.is_some()
        || spec.model_name.is_some();
    if !is_experiment {
        return None;
    }
    Some(Gate {
        name: "seed",
        verdict: if spec.seed.is_some() {
            Verdict::Ok
        } else {
            Verdict::Warn
        },
        text: if spec.seed.is_some() {
            "seed задан — выдачу можно воспроизвести".into()
        } else {
            "seed не задан — эксперимент невоспроизводим".into()
        },
        source: "ml-experiment-workflow",
    })
}

/// Гейт `ALFWorld`: `training_method` обязан быть `dqn`, а не `rl`.
fn gate_alfworld(spec: &ExperimentSpec) -> Option<Gate> {
    let framework = spec.framework.as_deref().unwrap_or("");
    let is_alfworld = framework.contains("alfworld")
        || framework.contains("rl-env")
        || spec.training_method.is_some();
    if !is_alfworld {
        return None;
    }
    let (verdict, text) = match spec.training_method.as_deref() {
        Some("rl") => (
            Verdict::Fail,
            "`training_method = \"rl\"` → NotImplementedError (alfred_tw_env.py:269); ставьте \"dqn\"".into(),
        ),
        Some("dqn") => (
            Verdict::Ok,
            "`training_method = \"dqn\"` — корректно (даже для RL-прогонов)".into(),
        ),
        Some(other) => (
            Verdict::Warn,
            format!("`training_method = \"{other}\"` — поддержаны только dqn/dagger"),
        ),
        None => (
            Verdict::Warn,
            "`training_method` не задан — для ALFWorld нужен \"dqn\" (не \"rl\")".into(),
        ),
    };
    Some(Gate {
        name: "alfworld",
        verdict,
        text,
        source: "alfworld-config-training-method",
    })
}

/// Гейт GRPO: `kl_coef` обязателен против mode collapse.
fn gate_kl(spec: &ExperimentSpec) -> Option<Gate> {
    let framework = spec.framework.as_deref().unwrap_or("");
    let is_rl = framework.contains("verl")
        || framework.contains("grpo")
        || framework == "rl"
        || spec.kl_coef.is_some();
    if !is_rl {
        return None;
    }
    match spec.kl_coef {
        Some(k) if k > 0.0 => Some(Gate {
            name: "kl",
            verdict: Verdict::Ok,
            text: format!("kl_coef={k} — защита от mode collapse"),
            source: "grpo-kl-collapse-prevention",
        }),
        Some(_) => Some(Gate {
            name: "kl",
            verdict: Verdict::Fail,
            text: "kl_coef=0 → mode collapse на шаге ~169 (энтропия→0); ставьте 0.01".into(),
            source: "grpo-kl-collapse-prevention",
        }),
        None => Some(Gate {
            name: "kl",
            verdict: Verdict::Warn,
            text: "kl_coef не задан — GRPO на малых батчах детерминированно коллапсирует; ставьте 0.01".into(),
            source: "grpo-kl-collapse-prevention",
        }),
    }
}

/// Гейт формата промпта: для Qwen — `chat` (список dict), не `plain` (строка).
fn gate_prompt_format(spec: &ExperimentSpec) -> Option<Gate> {
    let model = spec.model_name.as_deref().unwrap_or("");
    let is_qwen = model.to_lowercase().contains("qwen") || spec.prompt_format.is_some();
    if !is_qwen {
        return None;
    }
    let (verdict, text) = match spec.prompt_format.as_deref() {
        Some("plain") => (
            Verdict::Fail,
            "Qwen + plain string → дефолтный system prompt, score 0 (вместо 9.8); ставьте chat"
                .into(),
        ),
        Some("chat") => (
            Verdict::Ok,
            "chat-формат (список dict) — наш system prompt, score 9.8".into(),
        ),
        Some(other) => (
            Verdict::Warn,
            format!("prompt_format = \"{other}\" — для Qwen нужен chat (список dict)"),
        ),
        None => (
            Verdict::Warn,
            "prompt_format не задан — для Qwen в veRL нужен chat (список dict), не plain".into(),
        ),
    };
    Some(Gate {
        name: "prompt-format",
        verdict,
        text,
        source: "prompt-format-critical",
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Спека с полным проверенным стеком.
    fn verified_spec() -> ExperimentSpec {
        ExperimentSpec {
            name: "grpo-alfworld-3b".into(),
            params_b: Some(3.0),
            dtype: Some("bf16".into()),
            model_name: Some("Qwen2.5-3B-Instruct".into()),
            num_gpus: Some(2),
            gpu_vram_gb: Some(40.0),
            host_id: Some("38955836".into()),
            resource: Some("cloud".into()),
            device: None,
            framework: Some("verl".into()),
            verl_version: Some("0.4.0".into()),
            vllm_version: Some("0.8.3".into()),
            torch_version: Some("2.6.0".into()),
            cuda_version: Some("12.4".into()),
            image: Some(VERIFIED_IMAGE.into()),
            gpu_memory_utilization: Some(0.45),
            seed: Some(42),
            training_method: None,
            kl_coef: Some(0.01),
            prompt_format: Some("chat".into()),
            price_per_hour: Some(0.35),
            expected_hours: Some(6.0),
            daily_budget: None,
        }
    }

    fn by<'a>(gates: &'a [Gate], name: &str) -> &'a Gate {
        gates.iter().find(|g| g.name == name).expect("gate present")
    }

    #[test]
    fn verified_stack_passes() {
        let gates = run_preflight(&verified_spec());
        assert_eq!(by(&gates, "versions").verdict, Verdict::Ok);
        assert_eq!(by(&gates, "image").verdict, Verdict::Ok);
        assert_eq!(by(&gates, "vram").verdict, Verdict::Ok);
        assert_eq!(by(&gates, "gpu-mem-util").verdict, Verdict::Ok);
        assert_eq!(by(&gates, "host").verdict, Verdict::Ok);
        assert_eq!(by(&gates, "budget").verdict, Verdict::Ok);
        assert_eq!(by(&gates, "seed").verdict, Verdict::Ok);
        assert_eq!(by(&gates, "kl").verdict, Verdict::Ok);
        assert_eq!(by(&gates, "prompt-format").verdict, Verdict::Ok);
        assert_eq!(exit_code(&gates), 0);
    }

    #[test]
    fn unknown_verl_version_warns() {
        let mut s = verified_spec();
        s.verl_version = Some("0.7.0".into());
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "versions").verdict, Verdict::Warn);
    }

    #[test]
    fn pytorch_image_fails_abi() {
        let mut s = verified_spec();
        s.image = Some("pytorch/pytorch:2.6.0-cuda12.4-cudnn9-devel".into());
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "image").verdict, Verdict::Fail);
        assert_eq!(exit_code(&gates), 1);
    }

    #[test]
    fn vram_over_capacity_fails() {
        let mut s = verified_spec();
        s.params_b = Some(70.0);
        s.num_gpus = Some(1);
        s.gpu_vram_gb = Some(24.0);
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "vram").verdict, Verdict::Fail);
    }

    #[test]
    fn high_gpu_mem_util_fails() {
        let mut s = verified_spec();
        s.gpu_memory_utilization = Some(0.65);
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "gpu-mem-util").verdict, Verdict::Fail);
    }

    #[test]
    fn blacklisted_host_fails() {
        let mut s = verified_spec();
        s.host_id = Some("14804".into());
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "host").verdict, Verdict::Fail);
    }

    #[test]
    fn budget_over_cap_fails() {
        let mut s = verified_spec();
        s.price_per_hour = Some(2.0);
        s.expected_hours = Some(8.0);
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "budget").verdict, Verdict::Fail);
        assert!(by(&gates, "budget").text.contains("согласие владельца"));
    }

    #[test]
    fn alfworld_rl_method_fails() {
        let mut s = verified_spec();
        s.framework = Some("alfworld".into());
        s.training_method = Some("rl".into());
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "alfworld").verdict, Verdict::Fail);
    }

    #[test]
    fn kl_missing_warns() {
        let mut s = verified_spec();
        s.kl_coef = None;
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "kl").verdict, Verdict::Warn);
    }

    #[test]
    fn qwen_plain_prompt_fails() {
        let mut s = verified_spec();
        s.prompt_format = Some("plain".into());
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "prompt-format").verdict, Verdict::Fail);
    }

    #[test]
    fn missing_seed_warns() {
        let mut s = verified_spec();
        s.seed = None;
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "seed").verdict, Verdict::Warn);
    }

    #[test]
    fn empty_spec_warns_only_spec_gate() {
        let gates = run_preflight(&ExperimentSpec::default());
        assert_eq!(gates.len(), 1);
        assert_eq!(gates[0].name, "spec");
        assert_eq!(gates[0].verdict, Verdict::Warn);
    }

    #[test]
    fn parse_example_roundtrip() {
        let spec = parse_spec_str(&example_spec()).expect("example parses");
        assert_eq!(spec.name, "grpo-alfworld-3b");
        assert_eq!(spec.kl_coef, Some(0.01));
        assert_eq!(spec.training_method.as_deref(), Some("dqn"));
        let gates = run_preflight(&spec);
        assert_eq!(exit_code(&gates), 0, "образец должен проходить pre-flight");
    }

    #[test]
    fn render_and_json_shape() {
        let gates = run_preflight(&verified_spec());
        let text = render(&gates);
        assert!(text.contains("arch-ml preflight"));
        assert!(text.contains("готово"));
        let json = render_json(&verified_spec());
        assert!(json.contains("\"verdict\":\"ok\""));
        assert!(json.contains("\"source\":\"verl-version-compatibility-matrix\""));
    }

    #[test]
    fn local_gb10_resolves_vram_and_skips_cloud_gates() {
        let mut s = verified_spec();
        s.resource = Some("local".into());
        s.device = Some("gb10".into());
        s.gpu_vram_gb = None; // пусть реестр device подскажет 128 ГБ
        s.host_id = None;
        s.price_per_hour = None;
        s.expected_hours = None;
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "resource").verdict, Verdict::Ok);
        assert!(by(&gates, "resource").text.contains("unified 128"));
        assert_eq!(by(&gates, "vram").verdict, Verdict::Ok);
        // Бюджет и хост не применяются к локальному ресурсу.
        assert!(gates.iter().all(|g| g.name != "budget"));
        assert!(gates.iter().all(|g| g.name != "host"));
        assert_eq!(exit_code(&gates), 0);
    }

    #[test]
    fn gb10_high_mem_util_warns_nvrm() {
        let mut s = verified_spec();
        s.resource = Some("local".into());
        s.device = Some("gb10".into());
        s.gpu_memory_utilization = Some(0.60);
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "gpu-mem-util").verdict, Verdict::Warn);
        assert!(by(&gates, "gpu-mem-util").text.contains("NVRM"));
    }

    #[test]
    fn rtx4080super_local_recognized() {
        let mut s = verified_spec();
        s.resource = Some("local".into());
        s.device = Some("rtx4080super".into());
        s.gpu_vram_gb = None;
        s.num_gpus = Some(1);
        s.host_id = None;
        s.price_per_hour = None;
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "resource").verdict, Verdict::Ok);
        assert!(by(&gates, "resource").text.contains("16 ГБ"));
        // 3B-модель (≈17 ГБ нужно) на единственном 16 ГБ — не влезает.
        assert_eq!(by(&gates, "vram").verdict, Verdict::Fail);
    }

    #[test]
    fn unknown_resource_warns() {
        let mut s = verified_spec();
        s.resource = None;
        s.host_id = None;
        s.price_per_hour = None;
        s.device = None;
        let gates = run_preflight(&s);
        assert_eq!(by(&gates, "resource").verdict, Verdict::Warn);
        assert!(by(&gates, "resource").text.contains("local"));
    }
}
