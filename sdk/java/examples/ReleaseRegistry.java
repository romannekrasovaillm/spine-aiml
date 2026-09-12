package bank.spine.sdk;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

/**
 * Демо-сценарий «Реестр архитектурных версий» (показ архитекторам банка).
 *
 * <p>Сюжет: реестр архитектурных решений банка — Java-сервис. При новой
 * версии диаграммы реестр вызывает Spine-BE: validate → deliver (HTML +
 * SHA-256) → compare с предыдущей версией, и пишет каждый receipt строкой
 * JSONL в журнал ({@code <outDir>/receipts.jsonl}). Ценность: zero-dep SDK
 * встраивается в любой микросервис без новых зависимостей; SHA-256 receipt —
 * аудиторский след; машинная дельта — повестка архитектурного комитета.
 *
 * <p>Использование: {@code java -cp out:out-examples bank.spine.sdk.ReleaseRegistry
 * [outDir] [repoRoot]}. По умолчанию outDir = ./registry-out, repoRoot =
 * {@code <cwd>/../..} (запуск из sdk/java). LLM-вызовов нет — только
 * детерминированные команды archify. Exit 0 при полном успехе.
 */
public final class ReleaseRegistry {

    private ReleaseRegistry() {
    }

    public static void main(String[] args) throws Exception {
        Path outDir = Path.of(args.length > 0 ? args[0] : "registry-out");
        Path repoRoot = (args.length > 1
                ? Path.of(args[1])
                : Path.of("").toAbsolutePath().resolve("../..").normalize())
                .toAbsolutePath().normalize();
        Files.createDirectories(outDir);

        Path ir1 = repoRoot.resolve(
                "banking/demos/cli-from-claude-code/scenario2-archify-cli/sbp-v1.architecture.json");
        Path ir2 = repoRoot.resolve(
                "banking/demos/cli-from-claude-code/scenario2-archify-cli/sbp-v2.architecture.json");

        SpineBeClient client = new SpineBeClient(resolveBinary(repoRoot), Duration.ofSeconds(180), repoRoot);
        List<String> journal = new ArrayList<>();

        System.out.println("Реестр архитектурных версий — Spine-BE SDK (Java, zero-dep)");
        System.out.println("Репозиторий: " + repoRoot);
        System.out.println("Журнал receipt'ов: " + outDir.resolve("receipts.jsonl").toAbsolutePath());
        System.out.println();

        registerVersion(client, "v1", ir1, outDir, journal);
        registerVersion(client, "v2", ir2, outDir, journal);

        // Дельта v1 → v2 — повестка архитектурного комитета.
        Path delta = outDir.resolve("delta.html");
        ArchifyReceipt cmp = client.archifyCompare(ir1, ir2, delta);
        long compAdded = cmp.longAt("summary", "components", "added");
        long connAdded = cmp.longAt("summary", "connections", "added");
        journal.add("{\"version\":\"v1->v2\",\"cmd\":\"compare\",\"ok\":" + cmp.ok()
                + ",\"artifact_sha256\":\"" + str(cmp.getMap("artifact").get("sha256")) + "\""
                + ",\"bytes\":" + cmp.longAt("artifact", "bytes")
                + ",\"comp_added\":" + compAdded + ",\"conn_added\":" + connAdded + "}");
        System.out.println("compare v1->v2: ok=" + cmp.ok()
                + ", дельта: +" + compAdded + " компонент, +" + connAdded + " связи"
                + ", html=" + delta.getFileName()
                + ", sha256=" + short12(str(cmp.getMap("artifact").get("sha256"))));

        Path journalPath = outDir.resolve("receipts.jsonl");
        Files.writeString(journalPath, String.join("\n", journal) + "\n", StandardCharsets.UTF_8);

        System.out.println();
        System.out.println("Журнал: " + journal.size() + " receipt'ов → " + journalPath.toAbsolutePath());
        System.out.println("Готово: exit 0");
    }

    /** Регистрация одной версии IR: validate → deliver (HTML + SHA-256). */
    private static void registerVersion(SpineBeClient client, String version, Path ir,
                                        Path outDir, List<String> journal) {
        ArchifyReceipt val = client.archifyValidate("architecture", ir);
        int checks = val.getList("checks").size();
        journal.add("{\"version\":\"" + version + "\",\"cmd\":\"validate\",\"ok\":" + val.ok()
                + ",\"artifact_sha256\":\"\",\"bytes\":0,\"checks\":" + checks + "}");
        System.out.println(version + " validate: ok=" + val.ok() + ", проверок=" + checks);

        Path html = outDir.resolve("sbp-" + version + ".html");
        ArchifyReceipt del = client.archifyDeliver("architecture", ir, html);
        String sha = str(del.getMap("artifact").get("sha256"));
        long bytes = del.longAt("artifact", "bytes");
        journal.add("{\"version\":\"" + version + "\",\"cmd\":\"deliver\",\"ok\":" + del.ok()
                + ",\"artifact_sha256\":\"" + sha + "\",\"bytes\":" + bytes + "}");
        System.out.println(version + " deliver:  ok=" + del.ok()
                + ", html=" + html.getFileName() + " (" + bytes + " байт), sha256=" + short12(sha));
    }

    /** Бинарь: SPINE_BE_BIN → <repoRoot>/target/release/arch-ml. */
    private static String resolveBinary(Path repoRoot) {
        String fromEnv = System.getenv("SPINE_BE_BIN");
        if (fromEnv != null && !fromEnv.isBlank()) {
            return fromEnv;
        }
        return repoRoot.resolve("target/release/arch-ml").toString();
    }

    private static String str(Object o) {
        return o == null ? "" : o.toString();
    }

    private static String short12(String sha) {
        return sha.length() <= 12 ? sha : sha.substring(0, 12);
    }
}
