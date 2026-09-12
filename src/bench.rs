//! Специализированные архитектурные бенчмарки (solution architecture).
//!
//! КОНТРАКТ (владелец: агент `rubric` — общий с rubric.rs):
//! - [`Benchmark`] — YAML-сценарий: имя, описание, постановка задачи
//!   (system+user промпты), ссылка на рубрику оценки, проходной порог;
//! - [`run`] — прогон сценария на модели, оценка ответа рубрикой
//!   ([`crate::rubric::evaluate_with_options`]), запись отчёта в `out_dir` (md+json);
//! - golden-set (`assets/benchmarks/golden/`): синтетические документы
//!   `<имя>.md` + эталонные оценки `<имя>.expected.yaml` ([`GoldenExpectation`]);
//!   [`run_golden`] — прогон судьи по набору, согласие с эталоном — MAE
//!   ([`mean_absolute_error`]); регрессионный порог — на стороне CLI (ADR-004).

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::config::JudgeConfig;
use crate::error::{HarnessError, Result};
use crate::llm::{ChatMessage, ChatRequest, LlmProvider};

/// Сценарий бенчмарка.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Benchmark {
    /// Имя сценария.
    pub name: String,
    /// Описание: что измеряет.
    pub description: String,
    /// Системный промпт (роль solution-архитектора).
    pub system_prompt: String,
    /// Постановка задачи.
    pub task: String,
    /// Файл рубрики (относительно assets/rubrics или абсолютный).
    pub rubric: String,
    /// Проходной взвешенный порог (`0..=scale_max`).
    pub pass_threshold: f64,
    /// Теги (integration, adr, nfr, brownfield, …).
    #[serde(default)]
    pub tags: Vec<String>,
}

/// Сводка для списка.
#[derive(Debug, Clone)]
pub struct BenchSummary {
    /// Путь к YAML.
    pub path: PathBuf,
    /// Имя.
    pub name: String,
    /// Описание.
    pub description: String,
    /// Теги.
    pub tags: Vec<String>,
}

/// Отчёт о прогоне.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BenchReport {
    /// Имя бенчмарка.
    pub bench_name: String,
    /// Модель-испытуемый.
    pub model: String,
    /// Ответ модели (полный текст).
    pub response: String,
    /// Отчёт рубрики (сериализованный).
    pub rubric_report: crate::rubric::RubricReport,
    /// Прошёл ли порог.
    pub passed: bool,
}

/// Эталонные оценки golden-документа (`<имя>.expected.yaml` рядом с `<имя>.md`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GoldenExpectation {
    /// Имя рубрики: файл в assets/rubrics (расширение `.yaml` опционально).
    pub rubric: String,
    /// Ожидаемые баллы по критериям (id → `1..=scale_max`).
    pub scores: BTreeMap<String, u8>,
}

/// Отчёт по одному golden-документу.
#[derive(Debug, Clone)]
pub struct GoldenCaseReport {
    /// Имя файла документа.
    pub doc: String,
    /// MAE по критериям эталона этого документа.
    pub mae: f64,
    /// Сколько пар «судья × эталон» сравнено.
    pub compared: usize,
    /// Взвешенный балл судьи (как в отчёте рубрики).
    pub weighted_judge: f64,
    /// Взвешенный балл эталона (те же веса критериев рубрики).
    pub weighted_expected: f64,
    /// Длина документа в символах (для диагностики length bias).
    pub doc_chars: usize,
}

/// MAE по одному критерию рубрики — по всем документам golden-set.
#[derive(Debug, Clone)]
pub struct CriterionMae {
    /// Идентификатор критерия.
    pub criterion_id: String,
    /// MAE по документам, где критерий оценён эталоном.
    pub mae: f64,
    /// Сколько документов сравнено.
    pub compared: usize,
}

/// Диагностика length bias: корреляция Спирмена взвешенного балла
/// с длиной документа (в символах) — отдельно для судьи и эталона.
#[derive(Debug, Clone, Copy)]
pub struct LengthBias {
    /// ρ(взвешенный балл судьи, длина документа).
    pub judge_rho: f64,
    /// ρ(взвешенный балл эталона, длина документа).
    pub expected_rho: f64,
}

/// Отчёт golden-прогона судьи (ADR-004).
#[derive(Debug, Clone)]
pub struct GoldenReport {
    /// Модель-судья.
    pub judge_model: String,
    /// Разбор по документам.
    pub cases: Vec<GoldenCaseReport>,
    /// Итоговый MAE по всем парам «документ × критерий».
    pub mae: f64,
    /// Всего сравненных пар.
    pub compared: usize,
    /// MAE по критериям (по всем документам), отсортировано по id критерия.
    pub criterion_mae: Vec<CriterionMae>,
    /// Length bias (Спирмен ρ балла с длиной документа); `None`, если
    /// документов меньше двух или дисперсия длин/баллов нулевая.
    pub length_bias: Option<LengthBias>,
}

impl GoldenReport {
    /// Текст механической диагностики: MAE по критериям и length bias
    /// (Спирмен ρ взвешенного балла с длиной документа) судьи и эталона.
    #[must_use]
    pub fn diagnostics_text(&self) -> String {
        let mut out = String::new();
        if !self.criterion_mae.is_empty() {
            let _ = writeln!(out, "MAE по критериям:");
            for c in &self.criterion_mae {
                let _ = writeln!(
                    out,
                    "  {:<32} MAE {:.2} ({} документов)",
                    c.criterion_id, c.mae, c.compared
                );
            }
        }
        if let Some(bias) = self.length_bias {
            let _ = writeln!(
                out,
                "  length bias: судья ρ={:.2}, эталон ρ={:.2}",
                bias.judge_rho, bias.expected_rho
            );
        }
        out
    }
}

/// Загружает бенчмарк из YAML.
///
/// # Errors
/// Файл не читается / не валиден.
pub fn load(path: &Path) -> Result<Benchmark> {
    let text = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let bench: Benchmark = serde_yaml_ng::from_str(&text)?;
    Ok(bench)
}

/// Список бенчмарков каталога (`*.yaml`/`*.yml`); битые файлы пропускаются.
///
/// # Errors
/// Каталог не читается.
pub fn list(dir: &Path) -> Result<Vec<BenchSummary>> {
    let entries = std::fs::read_dir(dir).map_err(|e| HarnessError::io(dir, e))?;
    let mut out = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        let is_yaml = path
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| e.eq_ignore_ascii_case("yaml") || e.eq_ignore_ascii_case("yml"));
        if !is_yaml {
            continue;
        }
        // Битый файл — не ошибка каталога: пропускаем.
        if let Ok(bench) = load(&path) {
            out.push(BenchSummary {
                path,
                name: bench.name,
                description: bench.description,
                tags: bench.tags,
            });
        }
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(out)
}

/// Прогоняет бенчмарк на модели и оценивает рубрикой; пишет отчёты в `out_dir`.
///
/// Ответ модели оценивается тем же провайдером (судья = испытуемая модель),
/// с настройками судьи `judge` (k сэмплов, верификация цитат — ADR-004).
/// Отчёты: `bench-<name>-<model>-<yyyymmdd-hhmmss>.md` (задача, ответ, таблица
/// рубрики, PASS/FAIL) и `.json` (сериализованный [`BenchReport`]).
///
/// # Errors
/// Ошибка модели/судьи/записи.
pub async fn run(
    bench: &Benchmark,
    provider: &dyn LlmProvider,
    rubrics_dir: &Path,
    out_dir: &Path,
    judge: &JudgeConfig,
) -> Result<BenchReport> {
    let request = ChatRequest::chat(vec![
        ChatMessage::system(bench.system_prompt.clone()),
        ChatMessage::user(bench.task.clone()),
    ]);
    let response = provider.complete(request).await?.content;

    let rubric_path = {
        let p = PathBuf::from(&bench.rubric);
        if p.is_absolute() {
            p
        } else {
            rubrics_dir.join(p)
        }
    };
    let rubric = crate::rubric::load(&rubric_path)?;
    // Судья — та же модель, что и испытуемый.
    let rubric_report =
        crate::rubric::evaluate_with_options(&rubric, &response, provider, judge).await?;
    let passed = rubric_report.weighted_total >= bench.pass_threshold;
    let report = BenchReport {
        bench_name: bench.name.clone(),
        model: provider.model().to_string(),
        response,
        rubric_report,
        passed,
    };

    std::fs::create_dir_all(out_dir).map_err(|e| HarnessError::io(out_dir, e))?;
    let stamp = chrono::Local::now().format("%Y%m%d-%H%M%S");
    let base = format!(
        "bench-{}-{}-{stamp}",
        sanitize_file_part(&bench.name),
        sanitize_file_part(provider.model())
    );
    let md_path = out_dir.join(format!("{base}.md"));
    std::fs::write(&md_path, report_markdown(bench, &report))
        .map_err(|e| HarnessError::io(&md_path, e))?;
    let json_path = out_dir.join(format!("{base}.json"));
    let json = serde_json::to_string_pretty(&report)?;
    std::fs::write(&json_path, json).map_err(|e| HarnessError::io(&json_path, e))?;
    Ok(report)
}

/// Загружает golden-set каталога: пары «`<имя>.md` + `<имя>.expected.yaml`».
///
/// В отличие от [`list`], битый эталон — ошибка, а не пропуск: молчаливо
/// потерянный документ завышал бы измеренное согласие судьи с эталоном.
///
/// # Errors
/// Каталог не читается; эталон не парсится / без оценок / с баллом 0;
/// документ к эталону отсутствует.
pub fn load_golden(dir: &Path) -> Result<Vec<(PathBuf, GoldenExpectation)>> {
    let entries = std::fs::read_dir(dir).map_err(|e| HarnessError::io(dir, e))?;
    let mut out = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        let Some(stem) = name.strip_suffix(".expected.yaml") else {
            continue;
        };
        let text = std::fs::read_to_string(&path).map_err(|e| HarnessError::io(&path, e))?;
        let expectation: GoldenExpectation = serde_yaml_ng::from_str(&text)
            .map_err(|e| HarnessError::Bench(format!("{}: разбор эталона: {e}", path.display())))?;
        if expectation.scores.is_empty() {
            return Err(HarnessError::Bench(format!(
                "{}: эталон без оценок",
                path.display()
            )));
        }
        if let Some((id, _)) = expectation.scores.iter().find(|(_, s)| **s == 0) {
            return Err(HarnessError::Bench(format!(
                "{}: критерий '{id}' — балл 0 вне шкалы",
                path.display()
            )));
        }
        let doc = dir.join(format!("{stem}.md"));
        if !doc.is_file() {
            return Err(HarnessError::Bench(format!(
                "{}: нет документа к эталону {}",
                doc.display(),
                path.display()
            )));
        }
        out.push((doc, expectation));
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(out)
}

/// Прогоняет судью по golden-set и считает согласие с эталоном (MAE, ADR-004).
///
/// Каждый документ оценивается полным контуром
/// [`crate::rubric::evaluate_with_options`] рубрикой из его эталона; MAE —
/// среднее |судья − эталон| по всем парам «документ × критерий». Эталон,
/// ссылающийся на критерий вне рубрики или на балл выше шкалы, — ошибка
/// данных, а не расхождение судьи. Кроме итогового MAE отчёт несёт
/// механическую диагностику: MAE по критериям и length bias — корреляцию
/// Спирмена взвешенного балла с длиной документа (судья и эталон).
///
/// # Errors
/// Пустой/битый golden-set, ошибка чтения, модели или рубрики.
pub async fn run_golden(
    provider: &dyn LlmProvider,
    rubrics_dir: &Path,
    golden_dir: &Path,
    judge: &JudgeConfig,
) -> Result<GoldenReport> {
    let cases = load_golden(golden_dir)?;
    if cases.is_empty() {
        return Err(HarnessError::Bench(format!(
            "golden-set пуст: в {} нет пар <имя>.md + <имя>.expected.yaml",
            golden_dir.display()
        )));
    }
    let mut all_pairs: Vec<(f64, f64)> = Vec::new();
    let mut criterion_pairs: BTreeMap<String, Vec<(f64, f64)>> = BTreeMap::new();
    let mut case_reports = Vec::with_capacity(cases.len());
    for (doc_path, expectation) in &cases {
        let rubric_path = {
            let direct = rubrics_dir.join(&expectation.rubric);
            if direct.is_file() {
                direct
            } else {
                rubrics_dir.join(format!("{}.yaml", expectation.rubric))
            }
        };
        let rubric = crate::rubric::load(&rubric_path)?;
        let text = std::fs::read_to_string(doc_path).map_err(|e| HarnessError::io(doc_path, e))?;
        let report = crate::rubric::evaluate_with_options(&rubric, &text, provider, judge).await?;
        let mut pairs = Vec::with_capacity(expectation.scores.len());
        let mut expected_scores = Vec::with_capacity(expectation.scores.len());
        for (criterion_id, &expected) in &expectation.scores {
            if expected > rubric.scale_max {
                return Err(HarnessError::Bench(format!(
                    "эталон {}: критерий '{criterion_id}' — балл {expected} выше шкалы {}",
                    doc_path.display(),
                    rubric.scale_max
                )));
            }
            let got = report
                .scores
                .iter()
                .find(|s| &s.criterion_id == criterion_id)
                .map(|s| s.score)
                .ok_or_else(|| {
                    HarnessError::Bench(format!(
                        "эталон {} ссылается на критерий '{criterion_id}', которого нет в рубрике {}",
                        doc_path.display(),
                        rubric.name
                    ))
                })?;
            let pair = (f64::from(got), f64::from(expected));
            pairs.push(pair);
            criterion_pairs
                .entry(criterion_id.clone())
                .or_default()
                .push(pair);
            expected_scores.push(crate::rubric::CriterionScore {
                criterion_id: criterion_id.clone(),
                weight: 0.0,
                score: expected,
                rationale: String::new(),
                samples: Vec::new(),
                stdev: 0.0,
                flags: Vec::new(),
            });
        }
        let case_mae = mean_absolute_error(&pairs).ok_or_else(|| {
            HarnessError::Bench(format!(
                "{}: нет сравненных пар для MAE",
                doc_path.display()
            ))
        })?;
        // Взвешенный эталонный балл — той же формулой, что и итог рубрики.
        let weighted_expected = crate::rubric::weighted_total(&rubric.criteria, &expected_scores)?;
        all_pairs.extend(pairs.iter().copied());
        case_reports.push(GoldenCaseReport {
            doc: doc_path.file_name().map_or_else(
                || doc_path.display().to_string(),
                |n| n.to_string_lossy().into_owned(),
            ),
            mae: case_mae,
            compared: pairs.len(),
            weighted_judge: report.weighted_total,
            weighted_expected,
            doc_chars: text.chars().count(),
        });
    }
    let mae = mean_absolute_error(&all_pairs)
        .ok_or_else(|| HarnessError::Bench("golden-set без сравненных пар для MAE".into()))?;
    let criterion_mae = criterion_pairs
        .iter()
        .filter_map(|(criterion_id, pairs)| {
            mean_absolute_error(pairs).map(|mae| CriterionMae {
                criterion_id: criterion_id.clone(),
                mae,
                compared: pairs.len(),
            })
        })
        .collect();
    // Диагностика length bias: Спирмен ρ взвешенного балла с длиной документа
    // отдельно для судьи и эталона; обе корреляции должны быть определены.
    let lengths: Vec<f64> = case_reports.iter().map(|c| c.doc_chars as f64).collect();
    let judge_totals: Vec<f64> = case_reports.iter().map(|c| c.weighted_judge).collect();
    let expected_totals: Vec<f64> = case_reports.iter().map(|c| c.weighted_expected).collect();
    let length_bias = spearman(&judge_totals, &lengths)
        .zip(spearman(&expected_totals, &lengths))
        .map(|(judge_rho, expected_rho)| LengthBias {
            judge_rho,
            expected_rho,
        });
    Ok(GoldenReport {
        judge_model: provider.model().to_string(),
        cases: case_reports,
        mae,
        compared: all_pairs.len(),
        criterion_mae,
        length_bias,
    })
}

/// Средняя абсолютная ошибка по парам (факт, эталон); пустой вход — `None`.
#[must_use]
pub fn mean_absolute_error(pairs: &[(f64, f64)]) -> Option<f64> {
    if pairs.is_empty() {
        return None;
    }
    let sum: f64 = pairs.iter().map(|(got, want)| (got - want).abs()).sum();
    Some(sum / pairs.len() as f64)
}

/// Корреляция Спирмена между двумя векторами: ранжирование с усреднением
/// рангов при связях, затем Пирсон на рангах. Меньше двух наблюдений,
/// разная длина входов или нулевая дисперсия — `None`.
#[must_use]
pub fn spearman(xs: &[f64], ys: &[f64]) -> Option<f64> {
    if xs.len() < 2 || xs.len() != ys.len() {
        return None;
    }
    pearson(&average_ranks(xs), &average_ranks(ys))
}

/// Средние ранги (1-based): связанные значения получают средний ранг группы.
fn average_ranks(xs: &[f64]) -> Vec<f64> {
    let mut order: Vec<usize> = (0..xs.len()).collect();
    order.sort_by(|&a, &b| xs[a].total_cmp(&xs[b]));
    let mut ranks = vec![0.0; xs.len()];
    let mut i = 0;
    while i < order.len() {
        let mut j = i;
        while j + 1 < order.len() && xs[order[j + 1]] == xs[order[i]] {
            j += 1;
        }
        let rank = (i + j) as f64 / 2.0 + 1.0;
        for &idx in &order[i..=j] {
            ranks[idx] = rank;
        }
        i = j + 1;
    }
    ranks
}

/// Корреляция Пирсона двух векторов одинаковой длины; нулевая дисперсия
/// хотя бы одного — `None` (корреляция не определена).
fn pearson(xs: &[f64], ys: &[f64]) -> Option<f64> {
    let n = xs.len() as f64;
    let mean_x = xs.iter().sum::<f64>() / n;
    let mean_y = ys.iter().sum::<f64>() / n;
    let (mut sxy, mut sxx, mut syy) = (0.0, 0.0, 0.0);
    for (&x, &y) in xs.iter().zip(ys) {
        let dx = x - mean_x;
        let dy = y - mean_y;
        sxy += dx * dy;
        sxx += dx * dx;
        syy += dy * dy;
    }
    if sxx <= 0.0 || syy <= 0.0 {
        return None;
    }
    Some(sxy / (sxx * syy).sqrt())
}

/// Строка evidence-журнала golden-прогонов (JSONL, `bench run --golden --record`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GoldenRecord {
    /// Дата прогона (`YYYY-MM-DD`).
    pub date: String,
    /// Модель-судья.
    pub model: String,
    /// Итоговый MAE по всем парам «документ × критерий».
    pub mae: f64,
    /// Сравненных пар.
    pub compared: usize,
    /// MAE по критериям.
    pub criterion_mae: BTreeMap<String, f64>,
    /// ρ судьи (length bias); `null`, если корреляция не определена.
    pub judge_rho: Option<f64>,
    /// ρ эталона (length bias); `null`, если корреляция не определена.
    pub expected_rho: Option<f64>,
    /// Число документов набора.
    pub docs: usize,
}

impl GoldenRecord {
    /// Снимок отчёта [`GoldenReport`] на дату `date` (`YYYY-MM-DD`).
    #[must_use]
    pub fn from_report(report: &GoldenReport, date: String) -> Self {
        Self {
            date,
            model: report.judge_model.clone(),
            mae: report.mae,
            compared: report.compared,
            criterion_mae: report
                .criterion_mae
                .iter()
                .map(|c| (c.criterion_id.clone(), c.mae))
                .collect(),
            judge_rho: report.length_bias.map(|b| b.judge_rho),
            expected_rho: report.length_bias.map(|b| b.expected_rho),
            docs: report.cases.len(),
        }
    }
}

/// Дописывает запись строкой JSON в evidence-журнал (M-2); файл создаётся
/// при отсутствии, прежнее содержимое не затирается.
///
/// # Errors
/// Файл не открывается/не пишется; запись не сериализуется.
pub fn record_golden(path: &Path, record: &GoldenRecord) -> Result<()> {
    use std::io::Write as _;
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|e| HarnessError::io(path, e))?;
    let line = serde_json::to_string(record)?;
    writeln!(file, "{line}").map_err(|e| HarnessError::io(path, e))?;
    Ok(())
}

/// Читает evidence-журнал истории golden-прогонов (JSONL).
///
/// Пустые строки пропускаются молча (хвостовой перевод строки), битые —
/// считаются и пропускаются: возвращает `(записи, число битых строк)`.
///
/// # Errors
/// Файл отсутствует или не читается.
pub fn load_golden_history(path: &Path) -> Result<(Vec<GoldenRecord>, usize)> {
    let text = std::fs::read_to_string(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            HarnessError::Bench(format!(
                "журнал истории {} не найден: сначала прогоните `bench run --golden --record <PATH>`",
                path.display()
            ))
        } else {
            HarnessError::io(path, e)
        }
    })?;
    let mut records = Vec::new();
    let mut broken = 0;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        match serde_json::from_str::<GoldenRecord>(line) {
            Ok(record) => records.push(record),
            // Битая строка — не ошибка журнала: считаем и пропускаем.
            Err(_) => broken += 1,
        }
    }
    Ok((records, broken))
}

/// Markdown-таблица истории golden-прогонов: дата | модель | MAE | docs |
/// судья ρ | эталон ρ; неопределённая корреляция — «—».
#[must_use]
pub fn golden_history_markdown(records: &[GoldenRecord]) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "| Дата | Модель | MAE | Docs | Судья ρ | Эталон ρ |");
    let _ = writeln!(out, "| --- | --- | --- | --- | --- | --- |");
    for r in records {
        let fmt_rho = |rho: Option<f64>| rho.map_or_else(|| "—".to_string(), |v| format!("{v:.2}"));
        let _ = writeln!(
            out,
            "| {} | {} | {:.2} | {} | {} | {} |",
            r.date,
            r.model,
            r.mae,
            r.docs,
            fmt_rho(r.judge_rho),
            fmt_rho(r.expected_rho)
        );
    }
    out
}

/// Согласие по одному golden-документу с человеческими анкетами (J-3).
#[derive(Debug, Clone)]
pub struct DocAgreement {
    /// Имя файла документа.
    pub doc: String,
    /// Эталон golden (критерий → балл).
    pub golden: BTreeMap<String, u8>,
    /// Медиана людей по критериям (критерий → медиана).
    pub medians: BTreeMap<String, f64>,
    /// Оценки участников (участник → критерий → балл).
    pub humans: Vec<(String, BTreeMap<String, u8>)>,
}

/// Отчёт о согласии golden-эталонов с оценками живых архитекторов (J-3).
#[derive(Debug, Clone)]
pub struct HumanAgreementReport {
    /// Разбор по документам, имеющим человеческие анкеты.
    pub docs: Vec<DocAgreement>,
    /// (а) MAE «эталон golden vs медиана людей» по всем парам «документ × критерий».
    pub golden_vs_humans_mae: f64,
    /// (б) Среднее MAE «человек vs медиана людей» — уровень межчеловеческого
    /// согласия, бенчмарк для (а).
    pub inter_human_mae: f64,
    /// Сравненных пар «документ × критерий» в (а).
    pub compared: usize,
    /// Документы golden без человеческих анкет (пропущены).
    pub skipped: Vec<String>,
}

impl HumanAgreementReport {
    /// (в) Вердикт: в пределах ли расхождение эталона с людьми
    /// межчеловеческого разброса.
    #[must_use]
    pub fn verdict(&self) -> &'static str {
        if self.golden_vs_humans_mae <= self.inter_human_mae {
            "расхождение эталона с людьми в пределах межчеловеческого разброса"
        } else {
            "расхождение эталона с людьми ВЫШЕ межчеловеческого разброса — пересмотреть golden-эталоны"
        }
    }

    /// Markdown-представление: таблица «документ × критерий → медиана людей
    /// vs эталон golden», итоговые метрики (а)/(б) и строка-вердикт (в).
    #[must_use]
    pub fn to_markdown(&self) -> String {
        let mut out = String::new();
        let _ = writeln!(
            out,
            "| Документ | Критерий | Медиана людей | Эталон golden | Δ |"
        );
        let _ = writeln!(out, "| --- | --- | --- | --- | --- |");
        for doc in &self.docs {
            for (criterion, &expected) in &doc.golden {
                // Медиана посчитана для каждого критерия эталона; её отсутствие
                // невозможно по построению, но не паникуем на битых данных.
                let Some(&median) = doc.medians.get(criterion) else {
                    continue;
                };
                let _ = writeln!(
                    out,
                    "| {} | {} | {:.1} | {} | {:.1} |",
                    doc.doc,
                    criterion,
                    median,
                    expected,
                    (f64::from(expected) - median).abs()
                );
            }
        }
        let _ = writeln!(
            out,
            "\n(а) MAE эталон golden vs медиана людей: {:.2} ({} пар)",
            self.golden_vs_humans_mae, self.compared
        );
        let _ = writeln!(
            out,
            "(б) Среднее MAE человек vs медиана людей (межчеловеческое согласие): {:.2}",
            self.inter_human_mae
        );
        let _ = writeln!(out, "(в) Вердикт: {}", self.verdict());
        out
    }
}

/// Считает согласие golden-эталонов с человеческими анкетами (J-3).
///
/// Анкеты — файлы `<имя-документа>.<участник>.expected.yaml` в `humans_dir`
/// той же схемы [`GoldenExpectation`]. По каждому документу и критерию —
/// медиана людей; документы golden без анкет пропускаются (список —
/// в [`HumanAgreementReport::skipped`]). Участник с неполным набором
/// критериев — ошибка данных с именем файла.
///
/// # Errors
/// Каталоги не читаются; анкета не парсится, содержит балл 0 или неполный
/// набор критериев; ни один документ не имеет анкет.
pub fn human_agreement(golden_dir: &Path, humans_dir: &Path) -> Result<HumanAgreementReport> {
    let cases = load_golden(golden_dir)?;
    if cases.is_empty() {
        return Err(HarnessError::Bench(format!(
            "golden-set пуст: в {} нет пар <имя>.md + <имя>.expected.yaml",
            golden_dir.display()
        )));
    }
    let human_files = list_human_files(humans_dir)?;
    let mut docs = Vec::new();
    let mut skipped = Vec::new();
    for (doc_path, expectation) in &cases {
        let stem = doc_path
            .file_stem()
            .and_then(|s| s.to_str())
            .ok_or_else(|| {
                HarnessError::Bench(format!("{}: нечитаемое имя документа", doc_path.display()))
            })?;
        let prefix = format!("{stem}.");
        let mut humans: Vec<(String, BTreeMap<String, u8>)> = Vec::new();
        for path in human_files.iter().filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with(&prefix))
        }) {
            humans.push(parse_human_expectation(path, &prefix, expectation)?);
        }
        if humans.is_empty() {
            skipped.push(
                doc_path
                    .file_name()
                    .map_or_else(|| stem.to_string(), |n| n.to_string_lossy().into_owned()),
            );
            continue;
        }
        // Медиана людей по каждому критерию эталона.
        let mut medians = BTreeMap::new();
        for criterion in expectation.scores.keys() {
            let scores: Vec<f64> = humans
                .iter()
                .filter_map(|(_, scores)| scores.get(criterion).map(|&s| f64::from(s)))
                .collect();
            let median = median(&scores).ok_or_else(|| {
                HarnessError::Bench(format!(
                    "{}: критерий '{criterion}' без человеческих оценок",
                    doc_path.display()
                ))
            })?;
            medians.insert(criterion.clone(), median);
        }
        docs.push(DocAgreement {
            doc: doc_path
                .file_name()
                .map_or_else(|| stem.to_string(), |n| n.to_string_lossy().into_owned()),
            golden: expectation.scores.clone(),
            medians,
            humans,
        });
    }
    if docs.is_empty() {
        return Err(HarnessError::Bench(format!(
            "в {} нет анкет ни к одному документу golden-set",
            humans_dir.display()
        )));
    }
    // (а) MAE «эталон golden vs медиана людей» по всем парам.
    let golden_pairs: Vec<(f64, f64)> = docs
        .iter()
        .flat_map(|d| {
            d.golden
                .iter()
                .filter_map(|(c, &expected)| d.medians.get(c).map(|&m| (f64::from(expected), m)))
        })
        .collect();
    let golden_vs_humans_mae = mean_absolute_error(&golden_pairs)
        .ok_or_else(|| HarnessError::Bench("нет пар для MAE эталона с людьми".into()))?;
    // (б) Среднее MAE «человек vs медиана людей».
    let human_pairs: Vec<(f64, f64)> = docs
        .iter()
        .flat_map(|d| {
            d.humans.iter().flat_map(|(_, scores)| {
                scores
                    .iter()
                    .filter_map(|(c, &s)| d.medians.get(c).map(|&m| (f64::from(s), m)))
            })
        })
        .collect();
    let inter_human_mae = mean_absolute_error(&human_pairs)
        .ok_or_else(|| HarnessError::Bench("нет пар для межчеловеческого MAE".into()))?;
    Ok(HumanAgreementReport {
        docs,
        golden_vs_humans_mae,
        inter_human_mae,
        compared: golden_pairs.len(),
        skipped,
    })
}

/// Список файлов анкет каталога (`*.expected.yaml`), отсортированный по имени.
fn list_human_files(humans_dir: &Path) -> Result<Vec<PathBuf>> {
    let entries = std::fs::read_dir(humans_dir).map_err(|e| HarnessError::io(humans_dir, e))?;
    let mut out: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.ends_with(".expected.yaml"))
        })
        .collect();
    out.sort();
    Ok(out)
}

/// Разбирает анкету участника: имя участника — из имени файла (префикс
/// `<документ>.` срезается); проверяет полноту набора критериев эталона.
fn parse_human_expectation(
    path: &Path,
    prefix: &str,
    golden: &GoldenExpectation,
) -> Result<(String, BTreeMap<String, u8>)> {
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| HarnessError::Bench(format!("{}: нечитаемое имя анкеты", path.display())))?;
    let participant = name
        .strip_prefix(prefix)
        .and_then(|s| s.strip_suffix(".expected.yaml"))
        .ok_or_else(|| {
            HarnessError::Bench(format!(
                "{}: имя анкеты не по схеме <документ>.<участник>.expected.yaml",
                path.display()
            ))
        })?;
    let text = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let expectation: GoldenExpectation = serde_yaml_ng::from_str(&text)
        .map_err(|e| HarnessError::Bench(format!("{}: разбор анкеты: {e}", path.display())))?;
    if let Some((id, _)) = expectation.scores.iter().find(|(_, s)| **s == 0) {
        return Err(HarnessError::Bench(format!(
            "{}: критерий '{id}' — балл 0 вне шкалы",
            path.display()
        )));
    }
    if let Some(missing) = golden
        .scores
        .keys()
        .find(|c| !expectation.scores.contains_key(*c))
    {
        return Err(HarnessError::Bench(format!(
            "{}: неполный набор критериев: нет оценки '{missing}'",
            path.display()
        )));
    }
    // Лишние критерии участника игнорируются: сравнение — только по эталону.
    let scores = expectation
        .scores
        .into_iter()
        .filter(|(c, _)| golden.scores.contains_key(c))
        .collect();
    Ok((participant.to_string(), scores))
}

/// Медиана выборки; чётное число — среднее двух центральных; пустой вход — `None`.
#[must_use]
pub fn median(xs: &[f64]) -> Option<f64> {
    if xs.is_empty() {
        return None;
    }
    let mut sorted = xs.to_vec();
    sorted.sort_by(f64::total_cmp);
    let mid = sorted.len() / 2;
    if sorted.len() % 2 == 1 {
        Some(sorted[mid])
    } else {
        Some(sorted[mid - 1].midpoint(sorted[mid]))
    }
}

/// Заменяет символы, небезопасные в имени файла, на `-`.
fn sanitize_file_part(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_alphanumeric() || matches!(c, '-' | '_' | '.') {
                c
            } else {
                '-'
            }
        })
        .collect()
}

/// Markdown-отчёт прогона: задача, ответ модели, таблица рубрики, PASS/FAIL.
fn report_markdown(bench: &Benchmark, report: &BenchReport) -> String {
    let mut out = String::new();
    let verdict = if report.passed { "PASS" } else { "FAIL" };
    let _ = writeln!(out, "# Бенчмарк «{}» — {}\n", report.bench_name, verdict);
    let _ = writeln!(out, "**Модель:** {}", report.model);
    let _ = writeln!(
        out,
        "**Дата:** {}",
        chrono::Local::now().format("%Y-%m-%d %H:%M:%S")
    );
    let _ = writeln!(
        out,
        "**Взвешенный итог:** {:.2}/5 (порог {:.2})\n",
        report.rubric_report.weighted_total, bench.pass_threshold
    );
    let _ = writeln!(out, "## Задача\n\n{}\n", bench.task);
    let _ = writeln!(out, "## Ответ модели\n\n{}\n", report.response);
    let _ = writeln!(
        out,
        "## Оценка по рубрике\n\n{}\n",
        report.rubric_report.to_markdown()
    );
    let cmp = if report.passed { ">=" } else { "<" };
    let _ = writeln!(
        out,
        "## Результат\n\n**{}**: взвешенный итог {:.2} {} порог {:.2}.",
        verdict, report.rubric_report.weighted_total, cmp, bench.pass_threshold
    );
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;

    /// Рубрика-пример для прогонов.
    const RUBRIC_YAML: &str = "\
name: core
description: Базовая рубрика
scale_max: 5
origin: anchor
criteria:
  - id: context
    name: Контекст
    description: Описан контекст
    weight: 1.0
    anchors:
      1: нет
      3: частично
      5: полный
  - id: risks
    name: Риски
    description: Названы риски
    weight: 1.0
";

    /// Бенчмарк-пример, ссылающийся на [`RUBRIC_YAML`] по имени файла.
    const BENCH_YAML: &str = "\
name: adr-basic
description: Написать ADR
system_prompt: Ты solution-архитектор.
task: Напиши ADR миграции ML-inference сервиса на локальный шлюз vLLM.
rubric: core.yaml
pass_threshold: 3.0
tags:
  - adr
";

    /// Фейк-провайдер: на задачу отвечает `answer`, на судейский промпт — `judge`.
    #[derive(Debug)]
    struct FakeLlm {
        answer: String,
        judge: String,
    }

    #[async_trait]
    impl LlmProvider for FakeLlm {
        fn name(&self) -> &'static str {
            "fake"
        }
        fn model(&self) -> &'static str {
            "fake-model"
        }
        async fn complete(&self, req: ChatRequest) -> Result<ChatMessage> {
            let is_judge = req
                .messages
                .iter()
                .any(|m| m.content.contains("архитектурный судья"));
            let content = if is_judge {
                self.judge.clone()
            } else {
                self.answer.clone()
            };
            Ok(ChatMessage::assistant(content, Vec::new()))
        }
    }

    #[test]
    fn benchmark_yaml_roundtrip() {
        let bench: Benchmark = serde_yaml_ng::from_str(BENCH_YAML).expect("parse");
        let yaml = serde_yaml_ng::to_string(&bench).expect("serialize");
        let back: Benchmark = serde_yaml_ng::from_str(&yaml).expect("reparse");
        assert_eq!(back.name, "adr-basic");
        assert_eq!(back.system_prompt, "Ты solution-архитектор.");
        assert_eq!(back.rubric, "core.yaml");
        assert_eq!(back.tags, vec!["adr".to_string()]);
        assert!((back.pass_threshold - 3.0).abs() < 1e-9);
    }

    #[test]
    fn list_skips_broken_files() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("good.yaml"), BENCH_YAML).expect("write");
        std::fs::write(dir.path().join("broken.yml"), "{{{").expect("write");
        std::fs::write(dir.path().join("README.md"), "не бенч").expect("write");
        let items = list(dir.path()).expect("list");
        assert_eq!(
            items.len(),
            1,
            "битый и не-yaml файлы должны быть пропущены"
        );
        assert_eq!(items[0].name, "adr-basic");
        assert_eq!(items[0].tags, vec!["adr".to_string()]);
    }

    #[test]
    fn sanitize_replaces_unsafe_chars() {
        assert_eq!(sanitize_file_part("openai/gpt 5"), "openai-gpt-5");
        assert_eq!(sanitize_file_part("deepseek-chat"), "deepseek-chat");
    }

    /// Полный прогон бенчмарка на фейковой модели: файлы во временном каталоге.
    async fn run_case(judge: &str) -> (tempfile::TempDir, BenchReport, PathBuf) {
        let dir = tempfile::tempdir().expect("tempdir");
        let rubrics = dir.path().join("rubrics");
        std::fs::create_dir_all(&rubrics).expect("mkdir");
        std::fs::write(rubrics.join("core.yaml"), RUBRIC_YAML).expect("write rubric");
        let bench: Benchmark = serde_yaml_ng::from_str(BENCH_YAML).expect("bench");
        let llm = FakeLlm {
            answer: "ADR-001: мигрируем ML-inference на vLLM. Контекст: вендорский API \
                     уходит. Риски: латентность, дрейф версий."
                .into(),
            judge: judge.into(),
        };
        let out = dir.path().join("out");
        let report = run(&bench, &llm, &rubrics, &out, &JudgeConfig::default())
            .await
            .expect("run");
        (dir, report, out)
    }

    #[tokio::test]
    async fn run_writes_reports_and_passes_above_threshold() {
        // Цитаты в rationale — дословные фрагменты ответа модели (ADR-004).
        let judge = "{\"scores\": [\
             {\"criterion_id\": \"context\", \"score\": 5, \"rationale\": \"Цитата: \\\"Контекст: вендорский API уходит\\\" — контекст назван\"}, \
             {\"criterion_id\": \"risks\", \"score\": 4, \"rationale\": \"Цитата: \\\"Риски: латентность, дрейф версий\\\" — риски перечислены\"}], \
             \"verdict\": \"годно\"}";
        let (_dir, report, out) = run_case(judge).await;
        assert!(report.passed, "4.5 >= 3.0");
        assert_eq!(report.bench_name, "adr-basic");
        assert_eq!(report.model, "fake-model");
        assert!((report.rubric_report.weighted_total - 4.5).abs() < 1e-9);

        // На диске — md и json с контрактным именем.
        let files: Vec<PathBuf> = std::fs::read_dir(&out)
            .expect("read_dir")
            .flatten()
            .map(|e| e.path())
            .collect();
        let md = files
            .iter()
            .find(|p| p.extension().is_some_and(|e| e == "md"))
            .expect("md отчёт");
        let json_path = files
            .iter()
            .find(|p| p.extension().is_some_and(|e| e == "json"))
            .expect("json отчёт");
        let file_name = md.file_name().expect("имя файла").to_string_lossy();
        assert!(
            file_name.starts_with("bench-adr-basic-fake-model-"),
            "имя отчёта: {file_name}"
        );
        let md_text = std::fs::read_to_string(md).expect("read md");
        assert!(md_text.contains("PASS"));
        assert!(md_text.contains("ADR-001"), "ответ модели в отчёте");
        assert!(md_text.contains("| Критерий | Вес | Балл | Метки | Обоснование |"));
        let parsed: BenchReport =
            serde_json::from_str(&std::fs::read_to_string(json_path).expect("read json"))
                .expect("parse json");
        assert!(parsed.passed);
        assert_eq!(parsed.response, report.response);
        assert_eq!(parsed.rubric_report.scores.len(), 2);
    }

    #[tokio::test]
    async fn run_fails_below_threshold() {
        let judge = "{\"scores\": [\
             {\"criterion_id\": \"context\", \"score\": 2, \"rationale\": \"Цитата: \\\"Контекст: вендорский API уходит\\\" — слабо\"}, \
             {\"criterion_id\": \"risks\", \"score\": 2, \"rationale\": \"Цитата: \\\"Риски: латентность, дрейф версий\\\" — слабо\"}], \
             \"verdict\": \"плохо\"}";
        let (_dir, report, _out) = run_case(judge).await;
        assert!(!report.passed, "2.0 < 3.0");
        assert!(report.rubric_report.weighted_total < 3.0);
    }

    #[test]
    fn mean_absolute_error_math() {
        assert_eq!(mean_absolute_error(&[]), None, "пустой вход — None");
        let mae = mean_absolute_error(&[(4.0, 5.0), (5.0, 5.0), (1.0, 2.0)]).expect("mae");
        assert!((mae - 2.0 / 3.0).abs() < 1e-9, "MAE: {mae}");
    }

    /// Документ golden-фикстуры без свидетельств (эталон — единицы).
    const GOLDEN_BAD_MD: &str = "Проект важен. Дедлайн скоро.";
    /// Эталон к [`GOLDEN_BAD_MD`].
    const GOLDEN_BAD_YAML: &str = "rubric: core\nscores:\n  context: 1\n  risks: 1\n";
    /// Документ golden-фикстуры со свидетельствами (эталон — пятёрки).
    const GOLDEN_GOOD_MD: &str = "Контекст описан полностью. Риски названы и разобраны подробно.";
    /// Эталон к [`GOLDEN_GOOD_MD`].
    const GOLDEN_GOOD_YAML: &str = "rubric: core\nscores:\n  context: 5\n  risks: 5\n";

    /// Фейк-судья с очередью ответов (по одному на прогон оценки).
    #[derive(Debug)]
    struct QueueLlm {
        replies: std::sync::Mutex<std::collections::VecDeque<String>>,
    }

    impl QueueLlm {
        fn new(replies: &[String]) -> Self {
            Self {
                replies: std::sync::Mutex::new(replies.iter().cloned().collect()),
            }
        }
    }

    #[async_trait]
    impl LlmProvider for QueueLlm {
        fn name(&self) -> &'static str {
            "fake"
        }
        fn model(&self) -> &'static str {
            "fake-queue"
        }
        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            let reply = self
                .replies
                .lock()
                .expect("mutex poisoned")
                .pop_front()
                .unwrap_or_default();
            Ok(ChatMessage::assistant(reply, Vec::new()))
        }
    }

    /// Пишет golden-set (два документа + эталоны) и рубрику во временный каталог.
    fn golden_dirs() -> (tempfile::TempDir, PathBuf, PathBuf) {
        let dir = tempfile::tempdir().expect("tempdir");
        let rubrics = dir.path().join("rubrics");
        let golden = dir.path().join("golden");
        std::fs::create_dir_all(&rubrics).expect("mkdir");
        std::fs::create_dir_all(&golden).expect("mkdir");
        std::fs::write(rubrics.join("core.yaml"), RUBRIC_YAML).expect("rubric");
        std::fs::write(golden.join("bad.md"), GOLDEN_BAD_MD).expect("doc");
        std::fs::write(golden.join("bad.expected.yaml"), GOLDEN_BAD_YAML).expect("exp");
        std::fs::write(golden.join("good.md"), GOLDEN_GOOD_MD).expect("doc");
        std::fs::write(golden.join("good.expected.yaml"), GOLDEN_GOOD_YAML).expect("exp");
        (dir, rubrics, golden)
    }

    /// JSON-ответ судьи для golden-фикстуры.
    fn judge_reply(
        context: u8,
        risks: u8,
        context_rationale: &str,
        risks_rationale: &str,
    ) -> String {
        format!(
            "{{\"scores\": [\
             {{\"criterion_id\": \"context\", \"score\": {context}, \"rationale\": \"{context_rationale}\"}}, \
             {{\"criterion_id\": \"risks\", \"score\": {risks}, \"rationale\": \"{risks_rationale}\"}}], \
             \"verdict\": \"ok\"}}"
        )
    }

    #[test]
    fn load_golden_reads_sorted_pairs() {
        let (_dir, _rubrics, golden) = golden_dirs();
        let cases = load_golden(&golden).expect("load_golden");
        assert_eq!(cases.len(), 2);
        assert!(cases[0].0.ends_with("bad.md"), "сортировка по имени файла");
        assert_eq!(cases[0].1.scores["context"], 1);
        assert_eq!(cases[1].1.rubric, "core");
        assert_eq!(cases[1].1.scores.len(), 2);
    }

    #[test]
    fn load_golden_rejects_broken_inputs() {
        let dir = tempfile::tempdir().expect("tempdir");
        let golden = dir.path().join("golden");
        std::fs::create_dir_all(&golden).expect("mkdir");
        // Эталон без документа — ошибка, а не пропуск.
        std::fs::write(golden.join("orphan.expected.yaml"), GOLDEN_BAD_YAML).expect("write");
        let err = load_golden(&golden).expect_err("эталон-сирота");
        assert!(err.to_string().contains("нет документа"), "{err}");
        // Балл 0 вне шкалы.
        std::fs::write(golden.join("orphan.md"), "текст").expect("write");
        std::fs::write(
            golden.join("zero.expected.yaml"),
            "rubric: core\nscores:\n  context: 0\n",
        )
        .expect("write");
        std::fs::write(golden.join("zero.md"), "текст").expect("write");
        let err = load_golden(&golden).expect_err("балл 0");
        assert!(err.to_string().contains("вне шкалы"), "{err}");
        // Битый YAML.
        std::fs::remove_file(golden.join("zero.expected.yaml")).expect("rm");
        std::fs::remove_file(golden.join("orphan.expected.yaml")).expect("rm");
        std::fs::write(golden.join("broken.expected.yaml"), "rubric: [unclosed").expect("write");
        std::fs::write(golden.join("broken.md"), "текст").expect("write");
        assert!(load_golden(&golden).is_err(), "битый yaml — ошибка");
    }

    #[tokio::test]
    async fn run_golden_computes_mae_against_expectations() {
        let (_dir, rubrics, golden) = golden_dirs();
        let llm = QueueLlm::new(&[
            // bad.md идёт первым по сортировке: судья совпал с эталоном.
            judge_reply(
                1,
                1,
                "свидетельство отсутствует",
                "свидетельство отсутствует",
            ),
            // good.md: судья занизил context на балл, risks угадал.
            judge_reply(
                4,
                5,
                "Цитата: \\\"Контекст описан полностью\\\" — почти полный",
                "Цитата: \\\"Риски названы и разобраны подробно\\\" — разобраны",
            ),
        ]);
        let cfg = JudgeConfig {
            samples: 1,
            ..JudgeConfig::default()
        };
        let report = run_golden(&llm, &rubrics, &golden, &cfg)
            .await
            .expect("golden");
        assert_eq!(report.judge_model, "fake-queue");
        assert_eq!(report.compared, 4, "2 документа × 2 критерия");
        assert_eq!(report.cases.len(), 2);
        assert_eq!(report.cases[0].doc, "bad.md");
        assert!(
            (report.cases[0].mae - 0.0).abs() < 1e-9,
            "точное совпадение"
        );
        assert_eq!(report.cases[1].doc, "good.md");
        assert!(
            (report.cases[1].mae - 0.5).abs() < 1e-9,
            "(|4−5| + |5−5|) / 2"
        );
        assert!((report.mae - 0.25).abs() < 1e-9, "итоговый MAE по 4 парам");
        // MAE по критериям: context — (|1−1| + |4−5|)/2, risks — 0.
        assert_eq!(report.criterion_mae.len(), 2);
        assert_eq!(report.criterion_mae[0].criterion_id, "context");
        assert!((report.criterion_mae[0].mae - 0.5).abs() < 1e-9);
        assert_eq!(report.criterion_mae[0].compared, 2);
        assert_eq!(report.criterion_mae[1].criterion_id, "risks");
        assert!((report.criterion_mae[1].mae - 0.0).abs() < 1e-9);
        // Взвешенные баллы: bad — 1.0/1.0, good — 4.5/5.0.
        assert!((report.cases[0].weighted_judge - 1.0).abs() < 1e-9);
        assert!((report.cases[0].weighted_expected - 1.0).abs() < 1e-9);
        assert!((report.cases[1].weighted_judge - 4.5).abs() < 1e-9);
        assert!((report.cases[1].weighted_expected - 5.0).abs() < 1e-9);
        assert_eq!(report.cases[0].doc_chars, GOLDEN_BAD_MD.chars().count());
        // Два документа разной длины, баллы растут с длиной: обе ρ = 1.0.
        let bias = report.length_bias.expect("length bias при 2 документах");
        assert!(
            (bias.judge_rho - 1.0).abs() < 1e-9,
            "ρ судьи: {}",
            bias.judge_rho
        );
        assert!(
            (bias.expected_rho - 1.0).abs() < 1e-9,
            "ρ эталона: {}",
            bias.expected_rho
        );
        // Диагностика попадает в текст отчёта.
        let text = report.diagnostics_text();
        assert!(text.contains("MAE по критериям:"), "{text}");
        assert!(text.contains("context"), "{text}");
        assert!(
            text.contains("length bias: судья ρ=1.00, эталон ρ=1.00"),
            "{text}"
        );
    }

    #[tokio::test]
    async fn run_golden_length_bias_none_for_single_doc() {
        let dir = tempfile::tempdir().expect("tempdir");
        let rubrics = dir.path().join("rubrics");
        let golden = dir.path().join("golden");
        std::fs::create_dir_all(&rubrics).expect("mkdir");
        std::fs::create_dir_all(&golden).expect("mkdir");
        std::fs::write(rubrics.join("core.yaml"), RUBRIC_YAML).expect("rubric");
        std::fs::write(golden.join("solo.md"), GOLDEN_BAD_MD).expect("doc");
        std::fs::write(golden.join("solo.expected.yaml"), GOLDEN_BAD_YAML).expect("exp");
        let llm = QueueLlm::new(&[judge_reply(
            1,
            1,
            "свидетельство отсутствует",
            "свидетельство отсутствует",
        )]);
        let cfg = JudgeConfig {
            samples: 1,
            ..JudgeConfig::default()
        };
        let report = run_golden(&llm, &rubrics, &golden, &cfg)
            .await
            .expect("golden");
        assert_eq!(report.cases.len(), 1);
        assert!(
            report.length_bias.is_none(),
            "один документ — корреляция не определена"
        );
        assert!(
            !report.diagnostics_text().contains("length bias"),
            "строка length bias не печатается"
        );
        // MAE по критериям при этом считается и по одному документу.
        assert_eq!(report.criterion_mae.len(), 2);
    }

    #[test]
    fn spearman_math() {
        // Монотонная зависимость → 1.0, обратная → −1.0.
        let xs = [1.0, 2.0, 3.0, 4.0];
        let up = [10.0, 20.0, 30.0, 40.0];
        let down = [40.0, 30.0, 20.0, 10.0];
        assert!((spearman(&xs, &up).expect("ρ") - 1.0).abs() < 1e-9);
        assert!((spearman(&xs, &down).expect("ρ") + 1.0).abs() < 1e-9);
        // Связи усредняют ранги: xs ранжируется в [1.5, 1.5, 3, 4].
        let tied = [1.0, 1.0, 2.0, 3.0];
        let rho = spearman(&tied, &up).expect("ρ со связями");
        let expected = 4.5 / (4.5_f64 * 5.0).sqrt();
        assert!(
            (rho - expected).abs() < 1e-9,
            "ρ: {rho}, ожидалось {expected}"
        );
        // Неопределённые случаи — None.
        assert_eq!(spearman(&[1.0], &[2.0]), None, "n < 2");
        assert_eq!(spearman(&[], &[]), None, "пустые входы");
        assert_eq!(spearman(&xs, &up[..3]), None, "разная длина");
        assert_eq!(
            spearman(&[2.0, 2.0, 2.0], &up[..3]),
            None,
            "нулевая дисперсия"
        );
    }

    #[tokio::test]
    async fn run_golden_rejects_unknown_criterion_in_expectation() {
        let (_dir, rubrics, golden) = golden_dirs();
        // Ломаем эталон good.md: критерий, которого нет в рубрике core.
        std::fs::write(
            golden.join("good.expected.yaml"),
            "rubric: core\nscores:\n  context: 5\n  ghost: 4\n",
        )
        .expect("write");
        let llm = QueueLlm::new(&[
            judge_reply(
                1,
                1,
                "свидетельство отсутствует",
                "свидетельство отсутствует",
            ),
            judge_reply(
                1,
                1,
                "свидетельство отсутствует",
                "свидетельство отсутствует",
            ),
        ]);
        let cfg = JudgeConfig {
            samples: 1,
            ..JudgeConfig::default()
        };
        let err = run_golden(&llm, &rubrics, &golden, &cfg)
            .await
            .expect_err("критерий-призрак — ошибка данных");
        assert!(err.to_string().contains("ghost"), "{err}");
    }

    /// Live-прогон golden-set репозитория: `cargo test -- --ignored golden_live`.
    #[tokio::test]
    #[ignore = "нужен API-ключ и сеть: живой прогон судьи по golden-set репозитория"]
    async fn golden_live_repo_set() {
        let cfg = crate::config::Config::load(None).expect("config");
        let registry = crate::llm::LlmRegistry::from_config(&cfg).expect("registry");
        let root = Path::new(env!("CARGO_MANIFEST_DIR"));
        let report = run_golden(
            registry.default().as_ref(),
            &root.join("assets/rubrics"),
            &root.join("assets/benchmarks/golden"),
            &cfg.judge,
        )
        .await
        .expect("golden run");
        assert!(
            report.compared >= 30,
            "пар документ×критерий: {}",
            report.compared
        );
        eprintln!("golden MAE = {:.2}", report.mae);
    }

    /// Детерминированный golden-прогон фикстуры (судья: bad — точно,
    /// good — context занижен на балл).
    async fn golden_fixture_report() -> (tempfile::TempDir, GoldenReport) {
        let (dir, rubrics, golden) = golden_dirs();
        let llm = QueueLlm::new(&[
            judge_reply(
                1,
                1,
                "свидетельство отсутствует",
                "свидетельство отсутствует",
            ),
            judge_reply(
                4,
                5,
                "Цитата: \\\"Контекст описан полностью\\\" — почти полный",
                "Цитата: \\\"Риски названы и разобраны подробно\\\" — разобраны",
            ),
        ]);
        let cfg = JudgeConfig {
            samples: 1,
            ..JudgeConfig::default()
        };
        let report = run_golden(&llm, &rubrics, &golden, &cfg)
            .await
            .expect("golden");
        (dir, report)
    }

    #[tokio::test]
    async fn record_golden_appends_valid_json_lines() {
        let (_dir, report) = golden_fixture_report().await;
        let journal = tempfile::NamedTempFile::new().expect("journal");
        let first = GoldenRecord::from_report(&report, "2026-09-01".to_string());
        let second = GoldenRecord::from_report(&report, "2026-09-04".to_string());
        record_golden(journal.path(), &first).expect("record 1");
        record_golden(journal.path(), &second).expect("record 2");
        let text = std::fs::read_to_string(journal.path()).expect("read");
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines.len(), 2, "append не затирает прежние записи");
        let parsed: GoldenRecord = serde_json::from_str(lines[0]).expect("валидный JSON");
        assert_eq!(parsed.date, "2026-09-01");
        assert_eq!(parsed.model, "fake-queue");
        assert!((parsed.mae - 0.25).abs() < 1e-9);
        assert_eq!(parsed.compared, 4);
        assert_eq!(parsed.docs, 2);
        assert!((parsed.criterion_mae["context"] - 0.5).abs() < 1e-9);
        assert!((parsed.criterion_mae["risks"] - 0.0).abs() < 1e-9);
        assert!((parsed.judge_rho.expect("rho судьи") - 1.0).abs() < 1e-9);
        assert!((parsed.expected_rho.expect("rho эталона") - 1.0).abs() < 1e-9);
        // Контрактные ключи JSON-строки.
        let raw: serde_json::Value = serde_json::from_str(lines[1]).expect("json");
        for key in [
            "date",
            "model",
            "mae",
            "compared",
            "criterion_mae",
            "judge_rho",
            "expected_rho",
            "docs",
        ] {
            assert!(raw.get(key).is_some(), "нет ключа '{key}': {raw}");
        }
        assert_eq!(raw["date"], "2026-09-04");
    }

    #[tokio::test]
    async fn record_golden_writes_null_rho_when_bias_undefined() {
        let dir = tempfile::tempdir().expect("tempdir");
        let rubrics = dir.path().join("rubrics");
        let golden = dir.path().join("golden");
        std::fs::create_dir_all(&rubrics).expect("mkdir");
        std::fs::create_dir_all(&golden).expect("mkdir");
        std::fs::write(rubrics.join("core.yaml"), RUBRIC_YAML).expect("rubric");
        std::fs::write(golden.join("solo.md"), GOLDEN_BAD_MD).expect("doc");
        std::fs::write(golden.join("solo.expected.yaml"), GOLDEN_BAD_YAML).expect("exp");
        let llm = QueueLlm::new(&[judge_reply(
            1,
            1,
            "свидетельство отсутствует",
            "свидетельство отсутствует",
        )]);
        let cfg = JudgeConfig {
            samples: 1,
            ..JudgeConfig::default()
        };
        let report = run_golden(&llm, &rubrics, &golden, &cfg)
            .await
            .expect("golden");
        let entry = GoldenRecord::from_report(&report, "2026-09-04".to_string());
        let raw = serde_json::to_string(&entry).expect("json");
        assert!(raw.contains("\"judge_rho\":null"), "{raw}");
        assert!(raw.contains("\"expected_rho\":null"), "{raw}");
    }

    #[tokio::test]
    async fn load_golden_history_reads_and_skips_broken_lines() {
        let (_dir, report) = golden_fixture_report().await;
        let journal = tempfile::NamedTempFile::new().expect("journal");
        let good = serde_json::to_string(&GoldenRecord::from_report(
            &report,
            "2026-09-01".to_string(),
        ))
        .expect("json");
        std::fs::write(
            journal.path(),
            format!("{good}\n{{битая строка\n\n{good}\n"),
        )
        .expect("write");
        let (records, broken) = load_golden_history(journal.path()).expect("history");
        assert_eq!(records.len(), 2, "пустая строка молча пропущена");
        assert_eq!(broken, 1, "одна битая строка");
        // Отсутствующий файл — понятная ошибка.
        let missing = journal.path().with_file_name("no-such-journal.jsonl");
        let err = load_golden_history(&missing).expect_err("нет файла");
        assert!(err.to_string().contains("не найден"), "{err}");
    }

    #[test]
    fn golden_history_markdown_renders_table() {
        let records = vec![
            GoldenRecord {
                date: "2026-09-01".into(),
                model: "glm-5.3".into(),
                mae: 0.42,
                compared: 45,
                criterion_mae: BTreeMap::new(),
                judge_rho: Some(0.93),
                expected_rho: Some(0.71),
                docs: 9,
            },
            GoldenRecord {
                date: "2026-09-04".into(),
                model: "deepseek-v4-flash".into(),
                mae: 0.5,
                compared: 10,
                criterion_mae: BTreeMap::new(),
                judge_rho: None,
                expected_rho: None,
                docs: 1,
            },
        ];
        let md = golden_history_markdown(&records);
        assert!(
            md.contains("| Дата | Модель | MAE | Docs | Судья ρ | Эталон ρ |"),
            "{md}"
        );
        assert!(
            md.contains("| 2026-09-01 | glm-5.3 | 0.42 | 9 | 0.93 | 0.71 |"),
            "{md}"
        );
        assert!(
            md.contains("| 2026-09-04 | deepseek-v4-flash | 0.50 | 1 | — | — |"),
            "неопределённая ρ — прочерк: {md}"
        );
    }

    /// Пишет анкеты двух участников к двум документам golden-фикстуры.
    fn human_dirs(dir: &Path) -> PathBuf {
        let humans = dir.join("humans");
        std::fs::create_dir_all(&humans).expect("mkdir");
        let write = |name: &str, context: u8, risks: u8| {
            std::fs::write(
                humans.join(name),
                format!("rubric: core\nscores:\n  context: {context}\n  risks: {risks}\n"),
            )
            .expect("анкета");
        };
        write("bad.ivanov.expected.yaml", 1, 2);
        write("bad.petrov.expected.yaml", 2, 1);
        write("good.ivanov.expected.yaml", 5, 4);
        write("good.petrov.expected.yaml", 4, 5);
        humans
    }

    #[test]
    fn human_agreement_computes_medians_mae_and_verdict() {
        let (dir, _rubrics, golden) = golden_dirs();
        let humans = human_dirs(dir.path());
        let report = human_agreement(&golden, &humans).expect("agreement");
        assert_eq!(report.docs.len(), 2);
        assert!(report.skipped.is_empty());
        // Медианы: bad — 1.5/1.5, good — 4.5/4.5.
        let bad = &report.docs[0];
        assert_eq!(bad.doc, "bad.md");
        assert!((bad.medians["context"] - 1.5).abs() < 1e-9);
        assert!((bad.medians["risks"] - 1.5).abs() < 1e-9);
        assert_eq!(bad.humans.len(), 2);
        let good = &report.docs[1];
        assert!((good.medians["context"] - 4.5).abs() < 1e-9);
        // (а): (0.5 + 0.5 + 0.5 + 0.5) / 4 = 0.5.
        assert_eq!(report.compared, 4);
        assert!((report.golden_vs_humans_mae - 0.5).abs() < 1e-9);
        // (б): все восемь пар |человек − медиана| = 0.5 → 0.5.
        assert!((report.inter_human_mae - 0.5).abs() < 1e-9);
        // (в): 0.5 <= 0.5 — в пределах разброса.
        assert!(
            report.verdict().contains("в пределах"),
            "{}",
            report.verdict()
        );
        let md = report.to_markdown();
        assert!(
            md.contains("| Документ | Критерий | Медиана людей | Эталон golden | Δ |"),
            "{md}"
        );
        assert!(md.contains("| bad.md | context | 1.5 | 1 | 0.5 |"), "{md}");
        assert!(
            md.contains("(а) MAE эталон golden vs медиана людей: 0.50 (4 пар)"),
            "{md}"
        );
        assert!(md.contains("(в) Вердикт:"), "{md}");
    }

    #[test]
    fn human_agreement_verdict_flags_divergence_above_spread() {
        let (dir, _rubrics, golden) = golden_dirs();
        let humans = human_dirs(dir.path());
        // Эталон bad завышен (3/3 при медиане людей 1.5/1.5): расхождение
        // эталона с людьми выше межчеловеческого разброса.
        std::fs::write(
            golden.join("bad.expected.yaml"),
            "rubric: core\nscores:\n  context: 3\n  risks: 3\n",
        )
        .expect("exp");
        let report = human_agreement(&golden, &humans).expect("agreement");
        // (а): bad даёт 1.5+1.5, good 0.5+0.5 → 4/4 = 1.0; (б) = 0.5.
        assert!((report.golden_vs_humans_mae - 1.0).abs() < 1e-9);
        assert!(report.verdict().contains("ВЫШЕ"), "{}", report.verdict());
    }

    #[test]
    fn human_agreement_skips_doc_without_humans() {
        let (dir, _rubrics, golden) = golden_dirs();
        let humans = human_dirs(dir.path());
        std::fs::remove_file(humans.join("good.ivanov.expected.yaml")).expect("rm");
        std::fs::remove_file(humans.join("good.petrov.expected.yaml")).expect("rm");
        let report = human_agreement(&golden, &humans).expect("agreement");
        assert_eq!(report.docs.len(), 1, "good.md без анкет — пропущен");
        assert_eq!(report.skipped, vec!["good.md".to_string()]);
    }

    #[test]
    fn human_agreement_rejects_incomplete_participant() {
        let (dir, _rubrics, golden) = golden_dirs();
        let humans = human_dirs(dir.path());
        // Иванов не оценил risks к bad.md — ошибка данных с именем файла.
        std::fs::write(
            humans.join("bad.ivanov.expected.yaml"),
            "rubric: core\nscores:\n  context: 1\n",
        )
        .expect("анкета");
        let err = human_agreement(&golden, &humans).expect_err("неполная анкета");
        assert!(
            err.to_string().contains("bad.ivanov.expected.yaml"),
            "{err}"
        );
        assert!(err.to_string().contains("неполный набор"), "{err}");
    }

    #[test]
    fn median_math() {
        assert_eq!(median(&[]), None);
        assert!((median(&[3.0, 1.0, 2.0]).expect("медиана") - 2.0).abs() < 1e-9);
        assert!((median(&[1.0, 2.0]).expect("медиана") - 1.5).abs() < 1e-9);
    }
}
