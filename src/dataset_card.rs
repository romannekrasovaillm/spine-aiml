//! Валидатор датасет-карточек: объявленные поля против пробелов.
//!
//! Карточка — YAML-документ с обязательными полями ([`REQUIRED_FIELDS`]).
//! ЗАФИКСИРОВАННАЯ СЕМАНТИКА [`validate`]:
//! - пустой обязательный филд — это ДЕКЛАРАЦИЯ-ПРОБЕЛ ([`CardReport::missing`]),
//!   НЕ провал: карточек на диске сейчас нет, ложно-красный недопустим;
//! - [`CardReport::problems`] заполняется только фактическими противоречиями
//!   (например, объявлен `sha256`, а датасета нет; объявлены `records`,
//!   а файла нет) — то есть проверками, требующими пути к датасету.
//!
//! `path` в отчёте — путь к файлу (карточке или датасету), если он известен
//! вызывающему; [`validate`] берёт его из `dataset_path`, CLI подставляет
//! путь самой карточки.

use std::fmt::Write as _;

use anyhow::Context;

/// Обязательные поля датасет-карточки.
pub const REQUIRED_FIELDS: &[&str] = &[
    "dataset",
    "source",
    "license",
    "splits",
    "tokenizer",
    "dedup",
    "contamination",
];

/// Разобранная датасет-карточка.
#[derive(Debug, Clone, Default, serde::Deserialize)]
pub struct Card {
    /// Имя датасета (обязательное поле).
    pub dataset: String,
    /// Источник (URL/ревизия), если объявлен.
    #[serde(default)]
    pub source: Option<String>,
    /// Лицензия, если объявлена.
    #[serde(default)]
    pub license: Option<String>,
    /// Сплиты (train/val/test), если объявлены.
    #[serde(default)]
    pub splits: Vec<String>,
    /// Токенизатор, если объявлен.
    #[serde(default)]
    pub tokenizer: Option<String>,
    /// Процедура дедупликации, если объявлена.
    #[serde(default)]
    pub dedup: Option<String>,
    /// Оценка контаминации, если объявлена.
    #[serde(default)]
    pub contamination: Option<String>,
    /// `sha256` датасета, если объявлен.
    #[serde(default)]
    pub sha256: Option<String>,
    /// Число записей, если объявлено.
    #[serde(default)]
    pub records: Option<u64>,
    /// Языки датасета.
    #[serde(default)]
    pub language: Vec<String>,
}

/// Отчёт валидации одной карточки.
#[derive(Debug, Clone)]
pub struct CardReport {
    /// Имя датасета.
    pub dataset: String,
    /// Путь к карточке (или датасету), если известен.
    pub path: std::path::PathBuf,
    /// Объявленные (непустые) обязательные поля.
    pub declared: Vec<String>,
    /// Пустые обязательные поля (декларация-пробел, не провал).
    pub missing: Vec<String>,
    /// Фактические противоречия (требуют пути к датасету).
    pub problems: Vec<String>,
}

/// Разобрать карточку из текста YAML.
///
/// # Errors
/// Возвращает ошибку, если YAML не разбирается в [`Card`].
pub fn parse_card_yaml(text: &str) -> anyhow::Result<Card> {
    if text.trim().is_empty() {
        return Ok(Card::default());
    }
    serde_yaml_ng::from_str(text).context("разбор датасет-карточки (YAML)")
}

/// Прочитать и разобрать карточку с диска.
///
/// # Errors
/// Ошибка чтения файла или разбора YAML.
pub fn load_card(path: &std::path::Path) -> anyhow::Result<Card> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("чтение карточки {}", path.display()))?;
    parse_card_yaml(&text)
}

/// Проверить карточку: объявленные поля, пробелы и противоречия.
///
/// Семантика зафиксирована в doc-комментарии модуля: пустое обязательное
/// поле — декларация-пробел ([`CardReport::missing`]), а
/// [`CardReport::problems`] заполняется только подтверждаемыми
/// противоречиями (`sha256` или `records` объявлены, а файла датасета по
/// известному пути нет).
#[must_use]
pub fn validate(card: &Card, dataset_path: Option<&std::path::Path>) -> CardReport {
    let mut declared = Vec::new();
    let mut missing = Vec::new();
    for field in REQUIRED_FIELDS {
        if card_field_present(card, field) {
            declared.push((*field).to_string());
        } else {
            missing.push((*field).to_string());
        }
    }

    // Противоречие требует пути к датасету: без него «файла нет» не доказать,
    // а выдуманный красный недопустим.
    let mut problems = Vec::new();
    let has_sha256 = card.sha256.as_deref().is_some_and(|s| !s.trim().is_empty());
    let has_records = card.records.is_some();
    if has_sha256 || has_records {
        if let Some(path) = dataset_path {
            if !path.as_os_str().is_empty() && !path.exists() {
                let mut declared_paths = Vec::new();
                if has_sha256 {
                    declared_paths.push("sha256");
                }
                if has_records {
                    declared_paths.push("records");
                }
                problems.push(format!(
                    "объявлены {} при отсутствующем датасете: {}",
                    declared_paths.join(", "),
                    path.display()
                ));
            }
        }
    }

    CardReport {
        dataset: card.dataset.clone(),
        path: dataset_path.map_or_else(std::path::PathBuf::new, std::path::Path::to_path_buf),
        declared,
        missing,
        problems,
    }
}

/// Отрисовать отчёт валидации в текст.
#[must_use]
pub fn render_report(r: &CardReport) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "Датасет: {}", r.dataset);
    let _ = writeln!(
        out,
        "  заявлено {}/{}: {}",
        r.declared.len(),
        REQUIRED_FIELDS.len(),
        r.declared.join(", ")
    );
    if !r.missing.is_empty() {
        let _ = writeln!(out, "  пробелы: {}", r.missing.join(", "));
    }
    for p in &r.problems {
        let _ = writeln!(out, "  проблема: {p}");
    }
    out
}

/// Объявлено ли непустое значение обязательного поля.
fn card_field_present(card: &Card, field: &str) -> bool {
    let opt = |v: &Option<String>| v.as_deref().is_some_and(|s| !s.trim().is_empty());
    match field {
        "dataset" => !card.dataset.trim().is_empty(),
        "source" => opt(&card.source),
        "license" => opt(&card.license),
        "splits" => !card.splits.is_empty(),
        "tokenizer" => opt(&card.tokenizer),
        "dedup" => opt(&card.dedup),
        "contamination" => opt(&card.contamination),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validate_separates_declared_from_missing() {
        let card = parse_card_yaml("dataset: demo\n").expect("карточка");
        let r = validate(&card, None);
        assert_eq!(r.dataset, "demo");
        assert!(r.declared.contains(&"dataset".to_string()));
        assert!(r.missing.contains(&"source".to_string()));
        assert!(r.problems.is_empty(), "пробел — не провал");
    }

    #[test]
    fn empty_yaml_is_empty_card() {
        let card = parse_card_yaml("").expect("пустой YAML");
        assert!(card.dataset.is_empty());
        assert_eq!(validate(&card, None).missing.len(), REQUIRED_FIELDS.len());
    }

    #[test]
    fn full_card_has_no_missing_and_no_problems() {
        let card = parse_card_yaml(
            "dataset: demo\nsource: https://example.test/demo\nlicense: MIT\n\
             splits: [train, test]\ntokenizer: qwen\ndedup: exact\ncontamination: none\n",
        )
        .expect("карточка");
        let r = validate(&card, None);
        assert!(r.missing.is_empty(), "{:?}", r.missing);
        assert_eq!(r.declared.len(), REQUIRED_FIELDS.len());
        assert!(r.problems.is_empty(), "{:?}", r.problems);
    }

    #[test]
    fn missing_dataset_with_declared_sha256_is_problem() {
        let dir = tempfile::tempdir().expect("временный каталог");
        let absent = dir.path().join("demo.jsonl");
        let card = parse_card_yaml("dataset: demo\nsha256: ba7816bf\n").expect("карточка");
        let r = validate(&card, Some(&absent));
        assert_eq!(r.problems.len(), 1, "{:?}", r.problems);
        assert!(r.problems[0].contains("sha256"), "{}", r.problems[0]);
        assert!(
            r.problems[0].contains("отсутствующем датасете"),
            "{}",
            r.problems[0]
        );
        assert!(r.missing.contains(&"source".to_string()), "{:?}", r.missing);
    }

    #[test]
    fn missing_dataset_with_declared_records_is_problem() {
        let dir = tempfile::tempdir().expect("временный каталог");
        let absent = dir.path().join("demo.jsonl");
        let card = parse_card_yaml("dataset: demo\nrecords: 1000\n").expect("карточка");
        let r = validate(&card, Some(&absent));
        assert_eq!(r.problems.len(), 1, "{:?}", r.problems);
        assert!(r.problems[0].contains("records"), "{}", r.problems[0]);
    }

    #[test]
    fn existing_dataset_leaves_problems_empty() {
        let dir = tempfile::tempdir().expect("временный каталог");
        let present = dir.path().join("demo.jsonl");
        std::fs::write(&present, b"abc").expect("датасет");
        let card =
            parse_card_yaml("dataset: demo\nsha256: ba7816bf\nrecords: 3\n").expect("карточка");
        let r = validate(&card, Some(&present));
        assert!(r.problems.is_empty(), "{:?}", r.problems);
    }

    #[test]
    fn unknown_dataset_path_yields_no_problem() {
        // Путь не передан — «файла нет» не доказано, противоречие не выдумываем.
        let card =
            parse_card_yaml("dataset: demo\nsha256: ba7816bf\nrecords: 3\n").expect("карточка");
        let r = validate(&card, None);
        assert!(r.problems.is_empty(), "{:?}", r.problems);
        assert!(r.path.as_os_str().is_empty());
    }

    #[test]
    fn declared_fields_without_sha_or_records_have_no_problems() {
        let dir = tempfile::tempdir().expect("временный каталог");
        let absent = dir.path().join("demo.jsonl");
        let card =
            parse_card_yaml("dataset: demo\nsource: https://example.test\n").expect("карточка");
        let r = validate(&card, Some(&absent));
        assert!(r.problems.is_empty(), "{:?}", r.problems);
    }

    #[test]
    fn report_keeps_dataset_path() {
        let dir = tempfile::tempdir().expect("временный каталог");
        let path = dir.path().join("card.yaml");
        let card = parse_card_yaml("dataset: demo\n").expect("карточка");
        let r = validate(&card, Some(&path));
        assert_eq!(r.path, path);
        assert_eq!(r.dataset, "demo");
    }

    #[test]
    fn render_reports_gaps_and_problems() {
        let dir = tempfile::tempdir().expect("временный каталог");
        let absent = dir.path().join("demo.jsonl");
        let card = parse_card_yaml("dataset: demo\nsha256: ba7816bf\n").expect("карточка");
        let text = render_report(&validate(&card, Some(&absent)));
        assert!(text.contains("Датасет: demo"), "{text}");
        assert!(text.contains("пробелы:"), "{text}");
        assert!(text.contains("проблема:"), "{text}");
    }
}
