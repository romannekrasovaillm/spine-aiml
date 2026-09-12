package bank.spine.sdk;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Duration;

/**
 * Живые интеграционные тесты против настоящего бинаря {@code arch-ml}.
 *
 * <p>Бинарь: env {@code SPINE_BE_BIN} → иначе {@code ../../target/release/arch-ml}
 * (относительно sdk/java — корня репозитория монорепо). Если бинаря нет —
 * все проверки пропускаются с предупреждением (не считаются падением).
 *
 * <p>LLM-прогоны ({@code run}) здесь НЕ выполняются (§5 CONTRACT.md).
 */
final class IntegrationTest {

    private IntegrationTest() {
    }

    static void run(Checks c) throws IOException {
        System.out.println("== Интеграционные тесты (живой arch-ml) ==");
        Path binary = resolveBinary();
        if (binary == null || !Files.isExecutable(binary)) {
            System.out.println("[SKIP] бинарь arch-ml не найден (SPINE_BE_BIN / ../../target/release/arch-ml) — "
                    + "интеграционные тесты пропущены");
            return;
        }
        // Абсолютный путь: клиент меняет cwd процесса, относительный сломается.
        binary = binary.toAbsolutePath().normalize();
        Path repoRoot = binary.getParent().getParent().getParent();
        Path scenario2 = repoRoot.resolve("banking/demos/cli-from-claude-code/scenario2-archify-cli");
        Path gateFixtures = repoRoot.resolve("banking/demos/cli-from-claude-code/scenario3-gate/fixtures");
        if (!Files.isDirectory(scenario2) || !Files.isDirectory(gateFixtures)) {
            System.out.println("[SKIP] нет эталонных фикстур banking/demos — интеграционные тесты пропущены");
            return;
        }
        Duration t = Duration.ofSeconds(180);
        SpineBeClient client = new SpineBeClient(binary.toString(), t, repoRoot);

        // --- control check: зелёная фикстура ---
        ControlReport green = client.controlCheck(gateFixtures, gateFixtures.resolve("CONSTRAINTS.yaml"), t);
        c.ok(green.passed(), "control green: гейт зелёный");
        c.ok(green.issues().isEmpty(), "control green: нет нарушений");
        c.ok(green.summary().contains("Правил: 3"), "control green: сводка заполнена");

        // --- control check: красная копия (tmp + bad.py с 16-значным числом) ---
        Path redCopy = Files.createTempDirectory("spine-sdk-red");
        copyTree(gateFixtures, redCopy);
        Files.writeString(redCopy.resolve("src/bad.py"),
                "# (c) Банк, внутренний контур\n"
                        + "# Idempotency-Key\n"
                        + "PAN = \"4276123456789012\"  # 16-значное число — нарушение BANK-01\n");
        ControlReport red = client.controlCheck(redCopy, redCopy.resolve("CONSTRAINTS.yaml"), t);
        c.ok(!red.passed(), "control red: passed=false как данные (не исключение)");
        c.ok(!red.issues().isEmpty(), "control red: есть нарушения");
        c.ok(red.issues().stream().anyMatch(i -> i.rule().equals("no_pan_in_code")),
                "control red: сработало правило no_pan_in_code");
        c.ok(red.issues().stream().anyMatch(i -> i.file().endsWith("bad.py")),
                "control red: находка указывает на bad.py");

        // --- archify validate ---
        Path v1 = scenario2.resolve("sbp-v1.architecture.json");
        Path v2 = scenario2.resolve("sbp-v2.architecture.json");
        ArchifyReceipt val = client.archifyValidate("architecture", v1, t);
        c.ok(val.ok(), "validate: ok=true");
        c.eq(val.command(), "validate", "validate: command=validate");
        c.eq(val.type(), "architecture", "validate: type=architecture");
        c.eq(val.schemaVersion(), 1L, "validate: schemaVersion=1");
        c.eq(val.getList("checks").size(), 9, "validate: 9 проверок");
        c.eq(val.getMap("composition").get("status"), "pass", "validate: composition.status=pass");

        // --- archify deliver в tmp ---
        Path tmpOut = Files.createTempDirectory("spine-sdk-deliver");
        Path html = tmpOut.resolve("sbp-v1.html");
        ArchifyReceipt del = client.archifyDeliver("architecture", v1, html, t);
        c.ok(del.ok(), "deliver: ok=true");
        c.eq(del.command(), "deliver", "deliver: command=deliver");
        c.ok(Files.exists(html) && Files.size(html) > 0, "deliver: HTML-артефакт создан");
        c.ok(del.longAt("artifact", "bytes") > 0, "deliver: artifact.bytes > 0");
        c.ok(del.getMap("artifact").get("sha256") != null, "deliver: artifact.sha256 есть");

        // --- archify compare v1 → v2 ---
        Path delta = tmpOut.resolve("sbp-delta.html");
        ArchifyReceipt cmp = client.archifyCompare(v1, v2, delta, t);
        c.ok(cmp.ok(), "compare: ok=true");
        c.eq(cmp.command(), "compare", "compare: command=compare");
        c.eq(cmp.longAt("summary", "components", "added"), 1L, "compare: components.added == 1");
        c.eq(cmp.longAt("summary", "connections", "added"), 2L, "compare: connections.added == 2");
        c.ok(Files.exists(delta) && Files.size(delta) > 0, "compare: HTML-дифф создан");
    }

    /** Бинарь: SPINE_BE_BIN → ../../target/release/arch-ml (cwd = sdk/java). */
    private static Path resolveBinary() {
        String fromEnv = System.getenv("SPINE_BE_BIN");
        if (fromEnv != null && !fromEnv.isBlank()) {
            return Path.of(fromEnv);
        }
        return Path.of("..", "..", "target", "release", "arch-ml");
    }

    /** Рекурсивная копия директории фикстур во временный каталог. */
    private static void copyTree(Path from, Path to) throws IOException {
        try (var walk = Files.walk(from)) {
            for (Path src : walk.toList()) {
                Path dst = to.resolve(from.relativize(src).toString());
                if (Files.isDirectory(src)) {
                    Files.createDirectories(dst);
                } else {
                    Files.copy(src, dst, StandardCopyOption.REPLACE_EXISTING);
                }
            }
        }
    }
}
