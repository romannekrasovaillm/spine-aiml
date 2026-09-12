package bank.spine.sdk;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.time.Duration;

/**
 * Юнит-тесты клиента на фейк-бинарях (shell-скрипты-заглушки).
 * LLM-прогон {@code run()} не выполняется — проверяется только
 * построение argv и разбор ответов по контракту (§5 CONTRACT.md).
 */
final class ClientUnitTest {

    private ClientUnitTest() {
    }

    static void run(Checks c) throws IOException {
        System.out.println("== SpineBeClient (фейк-бинари, без LLM) ==");
        Path dir = Files.createTempDirectory("spine-sdk-fakes");
        Duration t = Duration.ofSeconds(10);

        // --- argv для run(): фейк печатает все аргументы построчно ---
        String echo = writeFake(dir, "fake-echo.sh", "#!/bin/sh\nprintf '%s\\n' \"$@\"\n");
        SpineBeClient echoClient = new SpineBeClient(echo, t, null);

        RunResult r = echoClient.run("черновик ADR", "glm-5.3", Duration.ofSeconds(5), 3);
        c.ok(r.answer().contains("run") && r.answer().contains("-q"), "run: базовые аргументы run -q");
        c.ok(r.answer().contains("--model") && r.answer().contains("glm-5.3"), "run: флаг --model");
        c.ok(r.answer().contains("--timeout") && r.answer().contains("\n5\n"), "run: флаг --timeout");
        c.ok(r.answer().contains("--max-turns") && r.answer().contains("\n3\n"), "run: флаг --max-turns");
        c.ok(r.answer().contains("черновик ADR"), "run: промпт передан аргументом");
        c.eq(r.model(), "glm-5.3", "run: модель в результате");
        c.ok(r.durationMs() >= 0, "run: durationMs заполнен");

        RunResult bare = echoClient.run("просто промпт");
        c.ok(!bare.answer().contains("--model") && !bare.answer().contains("--max-turns"),
                "run: без опциональных флагов");

        // --- control check, passed=false + exit 1: это ДАННЫЕ, не исключение ---
        String failJson = writeFake(dir, "fake-red.sh",
                "#!/bin/sh\n"
                        + "echo '{\"repo\":\".\",\"passed\":false,"
                        + "\"summary\":\"Правил: 3, нарушений: 1 (error: 1, warn: 0)\","
                        + "\"issues\":[{\"file\":\"src/bad.py\",\"line\":3,"
                        + "\"rule\":\"no_pan_in_code\",\"message\":\"запрещённый паттерн\","
                        + "\"severity\":\"error\"}]}'\n"
                        + "exit 1\n");
        SpineBeClient redClient = new SpineBeClient(failJson, t, null);
        ControlReport red = redClient.controlCheck(Path.of("."));
        c.ok(!red.passed(), "control: passed=false возвращается как данные (без исключения)");
        c.eq(red.issues().size(), 1, "control: одна находка");
        Issue issue = red.issues().get(0);
        c.eq(issue.rule(), "no_pan_in_code", "control: rule находки");
        c.eq(issue.line(), 3L, "control: line находки");
        c.eq(issue.severity(), "error", "control: severity находки");

        // --- control check, exit 2 без JSON: ProcessFailed ---
        String boom = writeFake(dir, "fake-boom.sh", "#!/bin/sh\necho 'что-то сломалось' >&2\nexit 2\n");
        SpineBeClient boomClient = new SpineBeClient(boom, t, null);
        c.throwsCode(SpineBeException.Code.PROCESS_FAILED, "control: exit 2 без JSON → ProcessFailed",
                () -> boomClient.controlCheck(Path.of(".")));
        try {
            boomClient.controlCheck(Path.of("."));
        } catch (SpineBeException e) {
            c.eq(e.exitCode(), 2, "ProcessFailed: exitCode сохранён");
            c.ok(e.stderr().contains("сломалось"), "ProcessFailed: stderr сохранён");
        }

        // --- control check, exit 0 с мусором в stdout: ContractViolation ---
        String garbage = writeFake(dir, "fake-garbage.sh", "#!/bin/sh\necho 'это не JSON'\nexit 0\n");
        SpineBeClient garbageClient = new SpineBeClient(garbage, t, null);
        c.throwsCode(SpineBeException.Code.CONTRACT_VIOLATION,
                "control: невалидный JSON при exit 0 → ContractViolation",
                () -> garbageClient.controlCheck(Path.of(".")));

        // --- run, exit 1 (сбой провайдера): ProcessFailed ---
        String runFail = writeFake(dir, "fake-runfail.sh", "#!/bin/sh\necho 'provider down' >&2\nexit 1\n");
        SpineBeClient runFailClient = new SpineBeClient(runFail, t, null);
        c.throwsCode(SpineBeException.Code.PROCESS_FAILED, "run: exit 1 → ProcessFailed",
                () -> runFailClient.run("промпт"));

        // --- клиентский таймаут: процесс убит ---
        String sleeper = writeFake(dir, "fake-sleep.sh", "#!/bin/sh\nsleep 30\n");
        SpineBeClient sleepClient = new SpineBeClient(sleeper, Duration.ofSeconds(1), null);
        long started = System.nanoTime();
        c.throwsCode(SpineBeException.Code.TIMEOUT, "таймаут: sleep 30 при лимите 1с → Timeout",
                () -> sleepClient.run("промпт"));
        long elapsedMs = (System.nanoTime() - started) / 1_000_000;
        c.ok(elapsedMs < 10_000, "таймаут: процесс реально убит (прошло " + elapsedMs + " мс)");

        // --- бинарь не найден ---
        SpineBeClient missing = new SpineBeClient("/nonexistent/arch-ml-sdk-test", t, null);
        c.throwsCode(SpineBeException.Code.BINARY_NOT_FOUND, "нет бинаря → BinaryNotFound",
                () -> missing.run("промпт"));

        // --- archify receipt: фейк печатает pretty JSON ---
        String receipt = writeFake(dir, "fake-archify.sh",
                "#!/bin/sh\nprintf '{\\n  \"schemaVersion\": 1,\\n  \"ok\": true,\\n"
                        + "  \"command\": \"validate\",\\n  \"type\": \"architecture\",\\n"
                        + "  \"checks\": [{\"name\": \"single_svg\", \"ok\": true, \"details\": []}]\\n}\\n'\n");
        SpineBeClient archClient = new SpineBeClient(receipt, t, null);
        ArchifyReceipt rc = archClient.archifyValidate("architecture", Path.of("ir.json"));
        c.ok(rc.ok(), "archify: receipt ok=true");
        c.eq(rc.command(), "validate", "archify: receipt command");
        c.eq(rc.schemaVersion(), 1L, "archify: schemaVersion=1");
        c.eq(rc.getList("checks").size(), 1, "archify: checks распарсен");
    }

    /** Пишет исполняемый shell-скрипт-заглушку, возвращает абсолютный путь. */
    private static String writeFake(Path dir, String name, String body) throws IOException {
        Path p = dir.resolve(name);
        Files.writeString(p, body, StandardCharsets.UTF_8);
        Files.setPosixFilePermissions(p, PosixFilePermissions.fromString("rwxr-xr-x"));
        return p.toAbsolutePath().toString();
    }
}
