//! Бенчмарки горячих чистых функций харнесса (criterion, dev-only).
//!
//! Запуск: `cargo bench`. В CI не гейтится — инструмент локальной охоты за
//! регрессиями производительности (зона роста из ревью: «нет bench-тестов»).
//! Бенчится только публичное API библиотеки: классификация политики (на каждый
//! вызов инструмента), spine-линтер, mermaid→ASCII, загрузка+валидация
//! типизированной модели (кейс 002, ~96 сущностей).

use criterion::{Criterion, black_box, criterion_group, criterion_main};

use arch_harness::{control, mermaid, model, policy};

/// Классификация bash-команд и вердикты политики — на каждом tool-call.
fn bench_policy(c: &mut Criterion) {
    let cmds = [
        "rm -rf /tmp/x",
        "cargo test --quiet",
        "git status --short",
        "kubectl delete pod payments-0",
        "cat README.md | grep -i архитект",
    ];
    c.bench_function("classify_bash_x5", |b| {
        b.iter(|| {
            for cmd in &cmds {
                black_box(policy::classify_bash(black_box(cmd)));
            }
        });
    });

    let pol = policy::Policy::default();
    let args = serde_json::json!({"command": "rm -rf /tmp/x"});
    c.bench_function("policy_check_destructive", |b| {
        b.iter(|| black_box(pol.check("bash", black_box(&args))));
    });
}

/// Линтер spine по реальному файлу репозитория (10 инвариантов).
fn bench_spine_lint(c: &mut Criterion) {
    let spine = concat!(env!("CARGO_MANIFEST_DIR"), "/ARCHITECTURE-SPINE.md");
    c.bench_function("lint_spine_self", |b| {
        b.iter(|| black_box(control::lint_spine(std::path::Path::new(spine)).expect("lint")));
    });
}

/// Рендер mermaid → ASCII (flowchart с кириллицей).
fn bench_mermaid_render(c: &mut Criterion) {
    const FLOW: &str = "graph TD\n  A[Клиент] --> B[API Gateway]\n  B --> C[Сервис заказов]\n  C --> D[(PostgreSQL)]\n  B --> E[(Redis кэш)]\n  C --> F[Очередь событий]\n";
    c.bench_function("mermaid_render_flowchart", |b| {
        b.iter(|| black_box(mermaid::render(black_box(FLOW)).expect("render")));
    });
}

/// Загрузка модели кейса 002 (~96 сущностей) + референциальная валидация.
fn bench_model_load_validate(c: &mut Criterion) {
    let dir = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/кейсы/payment-processing-platform/model"
    );
    c.bench_function("model_load_validate_case002", |b| {
        b.iter(|| {
            let m = model::load_model(std::path::Path::new(dir)).expect("load model");
            black_box(model::validate(&m));
        });
    });
}

criterion_group!(
    benches,
    bench_policy,
    bench_spine_lint,
    bench_mermaid_render,
    bench_model_load_validate
);
criterion_main!(benches);
