package bank.spine.sdk;

import java.nio.file.Path;
import java.util.List;

/** Parity-пример: каноническая сводка четырёх вызовов SDK одной строкой JSON
 *  (кросс-языковое сравнение поведения SDK, sdk/CONTRACT.md). */
public final class Parity {
    public static void main(String[] args) throws Exception {
        // Корень репо: аргумент командной строки либо <cwd>/../.. (запуск из sdk/java).
        Path repoRoot = args.length > 0
                ? Path.of(args[0])
                : Path.of("").toAbsolutePath().resolve("../..").normalize();
        Path root = repoRoot.resolve("banking/demos/cli-from-claude-code");
        Path gate = root.resolve("scenario3-gate/fixtures");
        Path ir1 = root.resolve("scenario2-archify-cli/sbp-v1.architecture.json");
        Path ir2 = root.resolve("scenario2-archify-cli/sbp-v2.architecture.json");

        SpineBeClient c = new SpineBeClient();
        ControlReport green = c.controlCheck(gate, gate.resolve("CONSTRAINTS.yaml"), null);
        ArchifyReceipt validate = c.archifyValidate("architecture", ir1);
        Path out = Path.of(System.getProperty("java.io.tmpdir"), "sdk-parity-delta-java.html");
        ArchifyReceipt compare = c.archifyCompare(ir1, ir2, out);
        List<Object> checks = validate.getList("checks");

        System.out.printf(
                "{\"green_passed\":%b,\"green_issues\":%d,\"validate_ok\":%b,\"checks\":%d,"
                        + "\"compare_ok\":%b,\"comp_added\":%d,\"conn_added\":%d}%n",
                green.passed(), green.issues().size(), validate.ok(), checks.size(),
                compare.ok(),
                compare.longAt("summary", "components", "added"),
                compare.longAt("summary", "connections", "added"));
    }
}
