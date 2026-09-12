//! Разбор модели: frontmatter сущностей и загрузка каталога `model/`.
//!
//! Формат сущности (ADR-003): markdown-файл с YAML-frontmatter между
//! маркерами `---`. Обязательные поля: `id`, `type`, `title`, `status`;
//! опциональные: `date`, связи (`depends_on`, `implements`, `affects`,
//! `verified_by`), `verification` (для `NFR`). Количественные поля ADR-007
//! (все опциональны): `latency_budget_ms` (INT), `p99_target_ms`,
//! `availability_target`, `rto_minutes`, `rpo_seconds`, `rps_target`,
//! `currency` (NFR), `availability`, `replicas`, `rps_per_instance`,
//! `instances`, `cost_per_instance_month`, `exit_cost` (CMP/INT); поля
//! сценария атрибута качества (QAS): `source`, `stimulus`, `artifact`,
//! `response`, `measure`; корни кода компонента `code_roots` (для `CMP`,
//! ADR-030 — границы контекстов в `context_boundary`). Контрактный
//! артефакт интеграции `contract` (для `INT`, ADR-035 — путь к файлу
//! контракта относительно корня кейса; проверяется звеном трассировки
//! «INT → контракт»). Тело после frontmatter — свободная проза.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::error::{HarnessError, Result};
use crate::model::{EntityKind, parse_id};

/// Сущность модели архитектуры.
#[derive(Debug, Clone)]
pub struct Entity {
    /// Идентификатор как в frontmatter (например, `ADR-001`).
    pub id: String,
    /// Тип сущности (соответствует префиксу `id`).
    pub kind: EntityKind,
    /// Заголовок.
    pub title: String,
    /// Статус (свободная непустая строка: `ADOPTED`, `Accepted`, …).
    pub status: String,
    /// Дата (как записана, обычно `YYYY-MM-DD`).
    pub date: Option<String>,
    /// Связи `depends_on` (цели — ID сущностей).
    pub depends_on: Vec<String>,
    /// Связи `implements` (цели — ID сущностей).
    pub implements: Vec<String>,
    /// Связи `affects` (цели — ID сущностей).
    pub affects: Vec<String>,
    /// Связи `verified_by` (цели — ID сущностей или правила `C-NNN`
    /// из CONSTRAINTS.yaml).
    pub verified_by: Vec<String>,
    /// Способ проверки (для `NFR`; свободный текст).
    pub verification: Option<String>,
    /// Обоснованный отказ от механической проверки (для `AD`, ADR-006):
    /// непустая строка — почему fitness-правило невозможно.
    pub unverifiable: Option<String>,
    /// Заявленный latency-бюджет hop'а, мс (для `INT`, ADR-007).
    pub latency_budget_ms: Option<f64>,
    /// Целевой p99 цепочки, мс (для `NFR`, ADR-007).
    pub p99_target_ms: Option<f64>,
    /// Целевая доступность (SLA) как доля, 0..1 (для `NFR`, ADR-007).
    pub availability_target: Option<f64>,
    /// Целевое время восстановления, минут (для `NFR`, ADR-007).
    pub rto_minutes: Option<f64>,
    /// Целевая потеря данных, секунд (для `NFR`, ADR-007).
    pub rpo_seconds: Option<f64>,
    /// Целевая пропускная способность, запросов/с (для `NFR`, ADR-007).
    pub rps_target: Option<f64>,
    /// Валюта стоимостного отчёта (для `NFR`, ADR-007; например `RUB`).
    pub currency: Option<String>,
    /// Заявленная доступность участка как доля, 0..1 (для `CMP`/`INT`,
    /// ADR-007).
    pub availability: Option<f64>,
    /// Параллельная избыточность участка: число независимых реплик
    /// (дефолт 1; для `CMP`/`INT`, ADR-007).
    pub replicas: Option<u32>,
    /// Ёмкость одного инстанса, запросов/с (для `CMP`, ADR-007).
    pub rps_per_instance: Option<f64>,
    /// Число запущенных инстансов (для `CMP`, ADR-007).
    pub instances: Option<u32>,
    /// Тариф: стоимость инстанса в месяц, в валюте кейса (для `CMP`,
    /// ADR-007).
    pub cost_per_instance_month: Option<f64>,
    /// Разовая цена выхода компонента (миграция данных, лицензии,
    /// переинтеграция; в валюте кейса; для `CMP`, ADR-007).
    pub exit_cost: Option<f64>,
    /// Корни кода компонента относительно корня репозитория (для `CMP`,
    /// ADR-030): префиксы путей, по которым fitness-правило
    /// `context_boundary` относит файлы и импорты к контексту.
    pub code_roots: Vec<String>,
    /// Контрактный артефакт интеграции (для `INT`, ADR-035): путь к файлу
    /// контракта (openapi/asyncapi/др.) относительно корня кейса.
    pub contract: Option<String>,
    /// Источник стимула (для `QAS`, ADR-007).
    pub source: Option<String>,
    /// Стимул (для `QAS`, ADR-007).
    pub stimulus: Option<String>,
    /// Артефакт, к которому приложен стимул (для `QAS`, ADR-007).
    pub artifact: Option<String>,
    /// Ожидаемая реакция (для `QAS`, ADR-007).
    pub response: Option<String>,
    /// Мера реакции — проверяемый порог (для `QAS`, ADR-007).
    pub measure: Option<String>,
    /// Тело документа (проза после frontmatter); ведущие пустые строки и
    /// хвостовые переводы строк срезаны.
    pub body: String,
    /// Файл, из которого прочитана сущность.
    pub file: PathBuf,
}

impl Entity {
    /// Цели связи `kind`.
    #[must_use]
    pub fn link_targets(&self, kind: LinkKind) -> &[String] {
        match kind {
            LinkKind::DependsOn => &self.depends_on,
            LinkKind::Implements => &self.implements,
            LinkKind::Affects => &self.affects,
            LinkKind::VerifiedBy => &self.verified_by,
        }
    }
}

/// Вид связи между сущностями.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum LinkKind {
    /// Зависимость («не работает без»); циклы по этому виду — ошибка.
    DependsOn,
    /// Реализация/детализация (например, `ADR` реализует `AD`, `CMP` реализует `NFR`).
    Implements,
    /// Затронутые сущности (например, компоненты, которых касается решение).
    Affects,
    /// Чем проверяется (сущность или правило `C-NNN` из CONSTRAINTS.yaml).
    VerifiedBy,
}

impl LinkKind {
    /// Все виды связей в стабильном порядке.
    pub const ALL: [LinkKind; 4] = [
        LinkKind::DependsOn,
        LinkKind::Implements,
        LinkKind::Affects,
        LinkKind::VerifiedBy,
    ];

    /// Имя поля frontmatter.
    #[must_use]
    pub fn field_name(self) -> &'static str {
        match self {
            LinkKind::DependsOn => "depends_on",
            LinkKind::Implements => "implements",
            LinkKind::Affects => "affects",
            LinkKind::VerifiedBy => "verified_by",
        }
    }
}

/// Сырой frontmatter сущности (serde-отображение YAML-блока).
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Frontmatter {
    /// Идентификатор (`PREFIX-NNN`).
    id: String,
    /// Тип (`cap`, `sys`, `cmp`, …).
    #[serde(rename = "type")]
    kind: String,
    /// Заголовок.
    title: String,
    /// Статус.
    status: String,
    /// Дата (опционально).
    date: Option<String>,
    /// Связи `depends_on`.
    #[serde(default)]
    depends_on: Vec<String>,
    /// Связи `implements`.
    #[serde(default)]
    implements: Vec<String>,
    /// Связи `affects`.
    #[serde(default)]
    affects: Vec<String>,
    /// Связи `verified_by`.
    #[serde(default)]
    verified_by: Vec<String>,
    /// Способ проверки (для `NFR`).
    verification: Option<String>,
    /// Отказ от механической проверки с обоснованием (для `AD`, ADR-006).
    unverifiable: Option<String>,
    /// Latency-бюджет hop'а, мс (для `INT`, ADR-007).
    latency_budget_ms: Option<f64>,
    /// Цель p99 цепочки, мс (для `NFR`, ADR-007).
    p99_target_ms: Option<f64>,
    /// Цель доступности (SLA), доля 0..1 (для `NFR`, ADR-007).
    availability_target: Option<f64>,
    /// Цель RTO, минут (для `NFR`, ADR-007).
    rto_minutes: Option<f64>,
    /// Цель RPO, секунд (для `NFR`, ADR-007).
    rpo_seconds: Option<f64>,
    /// Цель пропускной способности, RPS (для `NFR`, ADR-007).
    rps_target: Option<f64>,
    /// Валюта стоимостного отчёта (для `NFR`, ADR-007).
    currency: Option<String>,
    /// Доступность участка, доля 0..1 (для `CMP`/`INT`, ADR-007).
    availability: Option<f64>,
    /// Число параллельных реплик участка (для `CMP`/`INT`, ADR-007).
    replicas: Option<u32>,
    /// Ёмкость инстанса, RPS (для `CMP`, ADR-007).
    rps_per_instance: Option<f64>,
    /// Число инстансов (для `CMP`, ADR-007).
    instances: Option<u32>,
    /// Тариф за инстанс в месяц (для `CMP`, ADR-007).
    cost_per_instance_month: Option<f64>,
    /// Разовая цена выхода (для `CMP`, ADR-007).
    exit_cost: Option<f64>,
    /// Корни кода компонента (для `CMP`, ADR-030).
    #[serde(default)]
    code_roots: Vec<String>,
    /// Контрактный артефакт интеграции (для `INT`, ADR-035).
    contract: Option<String>,
    /// Источник стимула (для `QAS`, ADR-007).
    source: Option<String>,
    /// Стимул (для `QAS`, ADR-007).
    stimulus: Option<String>,
    /// Артефакт (для `QAS`, ADR-007).
    artifact: Option<String>,
    /// Реакция (для `QAS`, ADR-007).
    response: Option<String>,
    /// Мера реакции (для `QAS`, ADR-007).
    measure: Option<String>,
}

/// Отделяет frontmatter от тела: `---` в первой строке, затем ближайший
/// `---` на собственной строке. Возвращает (yaml-блок, тело).
///
/// # Errors
/// Нет открывающего или закрывающего маркера `---`.
pub fn split_frontmatter(text: &str) -> Result<(&str, &str)> {
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let after_open = if let Some(rest) = text.strip_prefix("---\n") {
        rest
    } else if text == "---" {
        ""
    } else {
        return Err(HarnessError::Model(
            "нет открывающего маркера frontmatter `---`".into(),
        ));
    };
    // Закрывающий маркер — `---`, за которым следует перевод строки или конец
    // файла (строка `----` маркером не считается).
    for (pos, _) in after_open.match_indices("\n---") {
        let rest = &after_open[pos + 4..];
        if rest.is_empty() {
            return Ok((&after_open[..pos], ""));
        }
        if let Some(body) = rest.strip_prefix("\r\n") {
            return Ok((&after_open[..pos], body));
        }
        if let Some(body) = rest.strip_prefix('\n') {
            return Ok((&after_open[..pos], body));
        }
    }
    Err(HarnessError::Model("нет закрывающего маркера `---`".into()))
}

/// Разбирает одну сущность из текста файла `file`.
///
/// Проверки структуры: наличие frontmatter, обязательные поля, строгий
/// формат `id`, известный `type`. Семантика (дубли, ссылки, циклы) —
/// в [`crate::model::validate`].
///
/// # Errors
/// Битый frontmatter/YAML, отсутствуют обязательные поля, невалидный `id`,
/// неизвестный `type`.
pub fn parse_entity(file: &Path, text: &str) -> Result<Entity> {
    let (yaml, body) = split_frontmatter(text)
        .map_err(|e| HarnessError::Model(format!("{}: {e}", file.display())))?;
    let fm: Frontmatter = serde_yaml_ng::from_str(yaml)
        .map_err(|e| HarnessError::Model(format!("{}: frontmatter: {e}", file.display())))?;
    if fm.id.trim().is_empty() {
        return Err(HarnessError::Model(format!(
            "{}: пустой `id`",
            file.display()
        )));
    }
    if fm.title.trim().is_empty() {
        return Err(HarnessError::Model(format!(
            "{}: пустой `title`",
            file.display()
        )));
    }
    if fm.status.trim().is_empty() {
        return Err(HarnessError::Model(format!(
            "{}: пустой `status`",
            file.display()
        )));
    }
    if parse_id(&fm.id).is_none() {
        return Err(HarnessError::Model(format!(
            "{}: невалидный `id` '{}' (ожидается PREFIX-NNN, префиксы: {})",
            file.display(),
            fm.id,
            EntityKind::prefixes().join(", ")
        )));
    }
    let kind = EntityKind::from_type_str(&fm.kind).ok_or_else(|| {
        HarnessError::Model(format!(
            "{}: неизвестный `type` '{}' (допустимы: {})",
            file.display(),
            fm.kind,
            EntityKind::type_names().join(", ")
        ))
    })?;
    Ok(Entity {
        id: fm.id,
        kind,
        title: fm.title,
        status: fm.status,
        date: fm.date,
        depends_on: fm.depends_on,
        implements: fm.implements,
        affects: fm.affects,
        verified_by: fm.verified_by,
        verification: fm.verification,
        unverifiable: fm.unverifiable,
        latency_budget_ms: fm.latency_budget_ms,
        p99_target_ms: fm.p99_target_ms,
        availability_target: fm.availability_target,
        rto_minutes: fm.rto_minutes,
        rpo_seconds: fm.rpo_seconds,
        rps_target: fm.rps_target,
        currency: fm.currency,
        availability: fm.availability,
        replicas: fm.replicas,
        rps_per_instance: fm.rps_per_instance,
        instances: fm.instances,
        cost_per_instance_month: fm.cost_per_instance_month,
        exit_cost: fm.exit_cost,
        code_roots: fm.code_roots,
        contract: fm.contract,
        source: fm.source,
        stimulus: fm.stimulus,
        artifact: fm.artifact,
        response: fm.response,
        measure: fm.measure,
        body: body.trim_start_matches(['\r', '\n']).trim_end().to_string(),
        file: file.to_path_buf(),
    })
}

/// Модель архитектуры: набор сущностей каталога `model/`.
#[derive(Debug)]
pub struct Model {
    /// Каталог, из которого загружена модель.
    pub dir: PathBuf,
    /// Сущности в детерминированном порядке (по имени файла).
    pub entities: Vec<Entity>,
    /// Индекс `id → позиция первого вхождения` (дубли отслеживает валидация).
    index: BTreeMap<String, usize>,
}

impl Model {
    /// Сущность по идентификатору (при дублях — первая по имени файла).
    #[must_use]
    pub fn get(&self, id: &str) -> Option<&Entity> {
        self.index.get(id).map(|&i| &self.entities[i])
    }

    /// Кто ссылается на `id`: пары (сущность, вид связи) в порядке файлов.
    #[must_use]
    pub fn referents(&self, id: &str) -> Vec<(&Entity, LinkKind)> {
        let mut out = Vec::new();
        for e in &self.entities {
            for kind in LinkKind::ALL {
                if e.link_targets(kind).iter().any(|t| t == id) {
                    out.push((e, kind));
                }
            }
        }
        out
    }
}

/// Загружает модель из каталога `dir`: все `*.md` верхнего уровня,
/// сортировка по имени файла (детерминированность).
///
/// # Errors
/// Каталог не читается; любой `.md`-файл не разбирается как сущность.
pub fn load_model(dir: &Path) -> Result<Model> {
    let rd = std::fs::read_dir(dir).map_err(|e| HarnessError::io(dir, e))?;
    let mut files: Vec<PathBuf> = Vec::new();
    for entry in rd {
        let entry = entry.map_err(|e| HarnessError::io(dir, e))?;
        let path = entry.path();
        if path.is_file() && path.extension().is_some_and(|ext| ext == "md") {
            files.push(path);
        }
    }
    files.sort();
    let mut entities = Vec::with_capacity(files.len());
    for file in files {
        let text = std::fs::read_to_string(&file).map_err(|e| HarnessError::io(&file, e))?;
        entities.push(parse_entity(&file, &text)?);
    }
    let mut index = BTreeMap::new();
    for (i, e) in entities.iter().enumerate() {
        index.entry(e.id.clone()).or_insert(i);
    }
    Ok(Model {
        dir: dir.to_path_buf(),
        entities,
        index,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(dir: &Path, name: &str, text: &str) -> PathBuf {
        let p = dir.join(name);
        std::fs::write(&p, text).expect("запись фикстуры");
        p
    }

    const VALID: &str = "---\n\
                         id: CMP-001\n\
                         type: cmp\n\
                         title: Payment Gateway\n\
                         status: adopted\n\
                         date: 2026-08-15\n\
                         depends_on:\n\
                           - CMP-002\n\
                         verified_by: [C-001]\n\
                         ---\n\
                         \n\
                         Приём платёжных запросов.\n";

    #[test]
    fn parse_valid_entity() {
        let dir = tempfile::tempdir().expect("tmp");
        let file = write(dir.path(), "CMP-001-payment-gateway.md", VALID);
        let e = parse_entity(&file, &std::fs::read_to_string(&file).expect("чтение"))
            .expect("валидная сущность");
        assert_eq!(e.id, "CMP-001");
        assert_eq!(e.kind, EntityKind::Cmp);
        assert_eq!(e.title, "Payment Gateway");
        assert_eq!(e.status, "adopted");
        assert_eq!(e.date.as_deref(), Some("2026-08-15"));
        assert_eq!(e.depends_on, ["CMP-002"]);
        assert_eq!(e.verified_by, ["C-001"]);
        assert_eq!(e.body, "Приём платёжных запросов.");
        // Количественные поля ADR-007 опциональны: без них — None.
        assert_eq!(e.latency_budget_ms, None);
        assert_eq!(e.instances, None);
        assert_eq!(e.source, None);
    }

    #[test]
    fn parse_qas_and_quantitative_fields() {
        let dir = tempfile::tempdir().expect("tmp");
        let qas = write(
            dir.path(),
            "QAS-001-latency.md",
            "---\nid: QAS-001\ntype: qas\ntitle: Latency при пике\nstatus: accepted\n\
             implements: [NFR-001]\nsource: клиент канала\nstimulus: запрос в пике 5000 TPS\n\
             artifact: CMP-003 Authorization\nresponse: ответ об авторизации\n\
             measure: p99 < 2000 мс\n---\n\nПроза.\n",
        );
        let e = parse_entity(&qas, &std::fs::read_to_string(&qas).expect("чтение"))
            .expect("QAS разбирается");
        assert_eq!(e.kind, EntityKind::Qas);
        assert_eq!(e.implements, ["NFR-001"]);
        assert_eq!(e.source.as_deref(), Some("клиент канала"));
        assert_eq!(e.stimulus.as_deref(), Some("запрос в пике 5000 TPS"));
        assert_eq!(e.artifact.as_deref(), Some("CMP-003 Authorization"));
        assert_eq!(e.response.as_deref(), Some("ответ об авторизации"));
        assert_eq!(e.measure.as_deref(), Some("p99 < 2000 мс"));

        let int = write(
            dir.path(),
            "INT-001-rail.md",
            "---\nid: INT-001\ntype: int\ntitle: Рельс\nstatus: accepted\n\
             latency_budget_ms: 800\n---\n",
        );
        let e = parse_entity(&int, &std::fs::read_to_string(&int).expect("чтение"))
            .expect("INT разбирается");
        assert_eq!(e.latency_budget_ms, Some(800.0));
        assert_eq!(e.contract, None, "поле contract опционально (ADR-035)");

        // Поле contract (ADR-035): путь к контрактному артефакту.
        let int2 = write(
            dir.path(),
            "INT-002-api.md",
            "---\nid: INT-002\ntype: int\ntitle: API\nstatus: accepted\n\
             contract: contracts/api.yaml\n---\n",
        );
        let e = parse_entity(&int2, &std::fs::read_to_string(&int2).expect("чтение"))
            .expect("INT с contract разбирается");
        assert_eq!(e.contract.as_deref(), Some("contracts/api.yaml"));

        let cmp = write(
            dir.path(),
            "CMP-001-gw.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: GW\nstatus: designed\n\
             availability: 0.999\nreplicas: 3\nrps_per_instance: 2000\ninstances: 4\n\
             cost_per_instance_month: 45000\nexit_cost: 300000\n---\n",
        );
        let e = parse_entity(&cmp, &std::fs::read_to_string(&cmp).expect("чтение"))
            .expect("CMP разбирается");
        assert_eq!(e.availability, Some(0.999));
        assert_eq!(e.replicas, Some(3));
        assert_eq!(e.rps_per_instance, Some(2000.0));
        assert_eq!(e.instances, Some(4));
        assert_eq!(e.cost_per_instance_month, Some(45000.0));
        assert_eq!(e.exit_cost, Some(300_000.0));

        let nfr = write(
            dir.path(),
            "NFR-001-lat.md",
            "---\nid: NFR-001\ntype: nfr\ntitle: Latency\nstatus: accepted\n\
             verification: histogram\np99_target_ms: 2000\navailability_target: 0.9999\n\
             rto_minutes: 15\nrpo_seconds: 0\nrps_target: 5000\ncurrency: RUB\n---\n",
        );
        let e = parse_entity(&nfr, &std::fs::read_to_string(&nfr).expect("чтение"))
            .expect("NFR разбирается");
        assert_eq!(e.p99_target_ms, Some(2000.0));
        assert_eq!(e.availability_target, Some(0.9999));
        assert_eq!(e.rto_minutes, Some(15.0));
        assert_eq!(e.rpo_seconds, Some(0.0));
        assert_eq!(e.rps_target, Some(5000.0));
        assert_eq!(e.currency.as_deref(), Some("RUB"));
    }

    #[test]
    fn split_frontmatter_missing_open_marker() {
        let err = split_frontmatter("# Заголовок\n").expect_err("нет маркера");
        assert!(err.to_string().contains("открывающего"), "{err}");
    }

    #[test]
    fn split_frontmatter_missing_close_marker() {
        let err = split_frontmatter("---\nid: CMP-001\n").expect_err("нет закрытия");
        assert!(err.to_string().contains("закрывающего"), "{err}");
    }

    #[test]
    fn parse_invalid_yaml_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let file = write(dir.path(), "x.md", "---\nid: [unclosed\n---\nтело\n");
        let err = parse_entity(&file, "---\nid: [unclosed\n---\nтело\n").expect_err("битый YAML");
        assert!(err.to_string().contains("frontmatter"), "{err}");
    }

    #[test]
    fn parse_missing_fields_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        // Без title — serde откажет (поле обязательное).
        let file = write(
            dir.path(),
            "x.md",
            "---\nid: CMP-001\ntype: cmp\nstatus: ok\n---\nтело\n",
        );
        assert!(parse_entity(&file, &std::fs::read_to_string(&file).expect("чтение")).is_err());
    }

    #[test]
    fn parse_invalid_id_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let file = write(
            dir.path(),
            "x.md",
            "---\nid: FOO-1\ntype: cmp\ntitle: T\nstatus: ok\n---\n",
        );
        let err = parse_entity(&file, &std::fs::read_to_string(&file).expect("чтение"))
            .expect_err("неизвестный префикс");
        assert!(err.to_string().contains("невалидный `id`"), "{err}");
    }

    #[test]
    fn parse_unknown_type_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let file = write(
            dir.path(),
            "x.md",
            "---\nid: CMP-001\ntype: widget\ntitle: T\nstatus: ok\n---\n",
        );
        let err = parse_entity(&file, &std::fs::read_to_string(&file).expect("чтение"))
            .expect_err("неизвестный type");
        assert!(err.to_string().contains("неизвестный `type`"), "{err}");
    }

    #[test]
    fn parse_unknown_frontmatter_field_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let file = write(
            dir.path(),
            "x.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: T\nstatus: ok\ndependsOn: []\n---\n",
        );
        let err = parse_entity(&file, &std::fs::read_to_string(&file).expect("чтение"))
            .expect_err("опечатка в поле");
        assert!(err.to_string().contains("frontmatter"), "{err}");
    }

    #[test]
    fn load_model_sorts_by_filename_and_ignores_non_md() {
        let dir = tempfile::tempdir().expect("tmp");
        write(dir.path(), "b.md", VALID);
        write(
            dir.path(),
            "a.md",
            "---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\n---\nтекст\n",
        );
        write(dir.path(), "notes.txt", "не сущность");
        let m = load_model(dir.path()).expect("модель");
        assert_eq!(m.entities.len(), 2, "txt пропущен");
        assert_eq!(m.entities[0].id, "AD-1", "a.md раньше b.md");
        assert_eq!(m.entities[1].id, "CMP-001");
        assert!(m.get("CMP-001").is_some());
        assert!(m.get("CMP-999").is_none());
    }

    #[test]
    fn load_model_missing_dir_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        assert!(load_model(&dir.path().join("ghost")).is_err());
    }

    #[test]
    fn referents_finds_backlinks() {
        let dir = tempfile::tempdir().expect("tmp");
        write(dir.path(), "a.md", VALID);
        write(
            dir.path(),
            "b.md",
            "---\nid: CMP-002\ntype: cmp\ntitle: Orchestrator\nstatus: adopted\n---\n",
        );
        let m = load_model(dir.path()).expect("модель");
        let refs = m.referents("CMP-002");
        assert_eq!(refs.len(), 1);
        assert_eq!(refs[0].0.id, "CMP-001");
        assert_eq!(refs[0].1, LinkKind::DependsOn);
        assert!(m.referents("CMP-001").is_empty());
    }
}
