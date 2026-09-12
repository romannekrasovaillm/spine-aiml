/// Parity-пример: каноническая сводка четырёх вызовов SDK одной строкой JSON.
/// Используется для кросс-языкового сравнения поведения SDK (sdk/CONTRACT.md).
use spine_be_sdk::{ArchifyReceipt, Client};
use std::path::PathBuf;

fn main() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let fixtures = root.join("banking/demos/cli-from-claude-code");
    let gate = fixtures.join("scenario3-gate/fixtures");
    let ir1 = fixtures.join("scenario2-archify-cli/sbp-v1.architecture.json");
    let ir2 = fixtures.join("scenario2-archify-cli/sbp-v2.architecture.json");

    let client = Client::new();
    let green = client
        .control_check(&gate, Some(&gate.join("CONSTRAINTS.yaml")))
        .expect("green control_check");
    let validate: ArchifyReceipt = client
        .archify_validate("architecture", &ir1)
        .expect("archify_validate");
    let tmp = std::env::temp_dir().join("sdk-parity-delta.html");
    let compare = client
        .archify_compare(&ir1, &ir2, &tmp)
        .expect("archify_compare");
    let summary = compare.summary().cloned().unwrap_or_default();

    println!(
        "{{\"green_passed\":{},\"green_issues\":{},\"validate_ok\":{},\"checks\":{},\"compare_ok\":{},\"comp_added\":{},\"conn_added\":{}}}",
        green.passed,
        green.issues.len(),
        validate.ok(),
        validate.checks().map_or(0, |c| c.len()),
        compare.ok(),
        summary["components"]["added"],
        summary["connections"]["added"],
    );
}
