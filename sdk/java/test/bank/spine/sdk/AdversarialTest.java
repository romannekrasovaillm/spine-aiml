package bank.spine.sdk;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.stream.Stream;

/**
 * Adversarial-тесты Spine-BE SDK v1 (QA-прогон): враждебные входы для
 * {@link MiniJson} и {@link SpineBeClient}.
 *
 * <p>Живые LLM-прогоны НЕ выполняются (§5 CONTRACT.md); реальный бинарь
 * дергается только в сценариях, которые гарантированно завершаются до
 * обращения к LLM (ошибка clap/конфига, control check по несуществующему
 * пути).
 */
final class AdversarialTest {

    private AdversarialTest() {
    }

    static void run(Checks c) throws Exception {
        System.out.println("== Adversarial: MiniJson ==");
        miniJson(c);
        System.out.println("== Adversarial: SpineBeClient (фейк-бинари) ==");
        Path dir = Files.createTempDirectory("spine-sdk-adv");
        clientFakes(c, dir);
        System.out.println("== Adversarial: конкурентность и ресурсы ==");
        clientResources(c, dir);
        System.out.println("== Adversarial: живой arch-ml (без LLM) ==");
        clientLive(c);
    }

    // ---------- MiniJson ----------

    @SuppressWarnings("unchecked")
    private static void miniJson(Checks c) {
        // Глубокая вложенность 10 000 уровней: НЕ StackOverflowError,
        // а понятная JsonException про лимит вложенности.
        String deep = "[".repeat(10_000) + "1" + "]".repeat(10_000);
        try {
            MiniJson.parse(deep);
            c.failed++;
            System.out.println("[FAIL] вложенность 10 000: ожидалась JsonException, разбор прошёл");
        } catch (MiniJson.JsonException e) {
            c.ok(e.getMessage().contains("вложенност"),
                    "вложенность 10 000: JsonException с понятным сообщением («" + e.getMessage() + "»)");
        } catch (StackOverflowError e) {
            c.failed++;
            System.out.println("[FAIL] вложенность 10 000: StackOverflowError — парсер рекурсивный без лимита");
        }
        String deepObj = "{\"a\":".repeat(10_000) + "1" + "}".repeat(10_000);
        try {
            MiniJson.parse(deepObj);
            c.failed++;
            System.out.println("[FAIL] вложенность объектов 10 000: ожидалась JsonException");
        } catch (MiniJson.JsonException e) {
            c.ok(true, "вложенность объектов 10 000: JsonException");
        } catch (StackOverflowError e) {
            c.failed++;
            System.out.println("[FAIL] вложенность объектов 10 000: StackOverflowError");
        }

        // Реалистичная глубина (в пределах лимита) должна разбираться.
        String ok100 = "[".repeat(100) + "1" + "]".repeat(100);
        Object v100 = MiniJson.parse(ok100);
        int depth = 0;
        while (v100 instanceof List) {
            depth++;
            v100 = ((List<Object>) v100).get(0);
        }
        c.eq(depth, 100, "вложенность 100 (в пределах лимита) разбирается");

        // Числа: большая экспонента, -0, 1E-5, переполнения.
        c.eq(MiniJson.parse("1e308"), 1e308, "число 1e308 → Double");
        c.eq(MiniJson.parse("1E-5"), 1.0e-5, "число 1E-5 → Double");
        c.eq(MiniJson.parse("-0.0"), -0.0, "число -0.0: знак сохранён (Double)");
        c.eq(MiniJson.parse("-0"), 0L, "число -0 → 0L (целое; знак теряется — задокументировано)");
        Object inf = MiniJson.parse("1e999");
        c.ok(inf instanceof Double && ((Double) inf).isInfinite(),
                "число 1e999 → +Infinity (Double, переполнение экспоненты)");
        c.ok(throwsJson("9223372036854775808"),
                "целое вне long (9223372036854775808) → JsonException, не молчаливая порча");

        // Дубликаты ключей: последний wins (LinkedHashMap.put), позиция ключа — от первого.
        Map<String, Object> dup = (Map<String, Object>) MiniJson.parse("{\"a\":1,\"b\":0,\"a\":2}");
        c.eq(dup.get("a"), 2L, "дубликаты ключей: последний wins");
        c.eq(String.join(",", dup.keySet()), "a,b", "дубликаты ключей: порядок — от первого вхождения");
    }

    private static boolean throwsJson(String input) {
        try {
            MiniJson.parse(input);
            return false;
        } catch (MiniJson.JsonException e) {
            return true;
        }
    }

    // ---------- Клиент на фейк-бинарях ----------

    private static void clientFakes(Checks c, Path dir) throws Exception {
        Duration t = Duration.ofSeconds(15);

        // 1. PIPE-DEADLOCK: валидный JSON в stdout + 5 МБ мусора в stderr, exit 0.
        //    Жёсткий дедлайн 20с: если клиент читает потоки последовательно — зависнет.
        String pipeJson = "{\"repo\":\".\",\"passed\":true,\"summary\":\"ок\",\"issues\":[]}";
        String pipeFake = writeFake(dir, "fake-pipe.sh",
                "#!/bin/sh\n"
                        + "printf '%s\\n' '" + pipeJson + "'\n"
                        + "head -c 5242880 /dev/zero | tr '\\0' 'E' >&2\n"
                        + "exit 0\n");
        SpineBeClient pipeClient = new SpineBeClient(pipeFake, t, null);
        ExecutorService single = Executors.newSingleThreadExecutor();
        try {
            Future<ControlReport> fut = single.submit(() -> pipeClient.controlCheck(Path.of(".")));
            try {
                ControlReport rep = fut.get(20, TimeUnit.SECONDS);
                c.ok(rep.passed(), "PIPE-DEADLOCK: 5 МБ stderr — JSON распарсен, зависания нет");
            } catch (TimeoutException e) {
                c.failed++;
                System.out.println("[FAIL] PIPE-DEADLOCK: клиент завис на 5 МБ stderr (дедлайн 20с)");
                fut.cancel(true);
            }
        } finally {
            single.shutdownNow();
        }

        // 2. TIMEOUT-KILL: фейк спит 120с, таймаут 1с → Timeout, elapsed < 10с,
        //    shell и дочерний sleep реально мертвы (не зомби, не сироты).
        Path shellPidFile = dir.resolve("shell.pid");
        Path sleepPidFile = dir.resolve("sleep.pid");
        String sleeper = writeFake(dir, "fake-sleep120.sh",
                "#!/bin/sh\n"
                        + "echo $$ > " + shellPidFile + "\n"
                        + "sleep 120 &\n"
                        + "echo $! > " + sleepPidFile + "\n"
                        + "wait\n");
        SpineBeClient sleepClient = new SpineBeClient(sleeper, Duration.ofSeconds(1), null);
        long started = System.nanoTime();
        c.throwsCode(SpineBeException.Code.TIMEOUT, "TIMEOUT-KILL: sleep 120 при лимите 1с → Timeout",
                () -> sleepClient.run("промпт"));
        long elapsedMs = (System.nanoTime() - started) / 1_000_000;
        c.ok(elapsedMs < 10_000, "TIMEOUT-KILL: elapsed " + elapsedMs + " мс < 10с");
        waitForProcGone(shellPidFile, 3_000);
        waitForProcGone(sleepPidFile, 3_000);
        c.ok(!pidAlive(shellPidFile), "TIMEOUT-KILL: процесс shell мёртв (нет зомби)");
        c.ok(!pidAlive(sleepPidFile), "TIMEOUT-KILL: дочерний sleep тоже убит (нет сироты на 120с)");

        // 3. BINARY-NOT-FOUND: несуществующий путь и неисполняемый файл.
        SpineBeClient missing = new SpineBeClient(dir.resolve("no-such-binary").toString(), t, null);
        c.throwsCode(SpineBeException.Code.BINARY_NOT_FOUND, "BINARY-NOT-FOUND: несуществующий путь",
                () -> missing.run("промпт"));
        try {
            missing.run("промпт");
        } catch (SpineBeException e) {
            c.ok(e.getMessage().contains("no-such-binary"),
                    "BINARY-NOT-FOUND: сообщение содержит путь к бинарю");
        }
        Path notExec = dir.resolve("not-executable.sh");
        Files.writeString(notExec, "#!/bin/sh\necho hi\n");
        Files.setPosixFilePermissions(notExec, PosixFilePermissions.fromString("rw-r--r--"));
        SpineBeClient noExec = new SpineBeClient(notExec.toString(), t, null);
        c.throwsCode(SpineBeException.Code.BINARY_NOT_FOUND,
                "BINARY-NOT-FOUND: неисполняемый файл (не generic-ошибка)",
                () -> noExec.run("промпт"));

        // 4. EXIT-2-NO-JSON: exit 2, мусор в stdout и stderr без JSON → ProcessFailed(2, stderr).
        String exit2 = writeFake(dir, "fake-exit2.sh",
                "#!/bin/sh\necho 'мусор в stdout'\necho 'фатальная причина' >&2\nexit 2\n");
        SpineBeClient exit2Client = new SpineBeClient(exit2, t, null);
        try {
            exit2Client.controlCheck(Path.of("."));
            c.failed++;
            System.out.println("[FAIL] EXIT-2-NO-JSON: исключение не брошено");
        } catch (SpineBeException e) {
            c.ok(e.code() == SpineBeException.Code.PROCESS_FAILED, "EXIT-2-NO-JSON: код PROCESS_FAILED");
            c.eq(e.exitCode(), 2, "EXIT-2-NO-JSON: exitCode=2");
            c.ok(e.stderr().contains("фатальная причина"), "EXIT-2-NO-JSON: stderr сохранён");
        }

        // 5. CONTRACT-VIOLATION: exit 0 + битый (обрезанный) JSON для JSON-команд.
        String truncated = writeFake(dir, "fake-truncated.sh",
                "#!/bin/sh\nprintf '{\"schemaVersion\": 1, \"ok\": tr'\nexit 0\n");
        SpineBeClient truncClient = new SpineBeClient(truncated, t, null);
        c.throwsCode(SpineBeException.Code.CONTRACT_VIOLATION,
                "CONTRACT-VIOLATION: обрезанный JSON (archify) при exit 0",
                () -> truncClient.archifyValidate("architecture", Path.of("ir.json")));
        c.throwsCode(SpineBeException.Code.CONTRACT_VIOLATION,
                "CONTRACT-VIOLATION: обрезанный JSON (control) при exit 0",
                () -> truncClient.controlCheck(Path.of(".")));

        // 6. UNICODE: кириллица + эмодзи в message/summary — round-trip без потерь.
        String unicodeMsg = "запрещённый паттерн 🔥⚠️ «Пан» в коде";
        String unicodeSummary = "Правил: 1, нарушений: 1 📊";
        String unicodeFake = writeFake(dir, "fake-unicode.sh",
                "#!/bin/sh\ncat <<'EOF'\n"
                        + "{\"repo\":\".\",\"passed\":true,\"summary\":\"" + unicodeSummary + "\","
                        + "\"issues\":[{\"file\":\"src/ю.py\",\"line\":1,\"rule\":\"r1\","
                        + "\"message\":\"" + unicodeMsg + "\",\"severity\":\"warn\"}]}\n"
                        + "EOF\nexit 0\n");
        SpineBeClient unicodeClient = new SpineBeClient(unicodeFake, t, null);
        ControlReport urep = unicodeClient.controlCheck(Path.of("."));
        c.eq(urep.summary(), unicodeSummary, "UNICODE: summary (кириллица+эмодзи) без потерь");
        c.eq(urep.issues().get(0).message(), unicodeMsg, "UNICODE: message (кириллица+эмодзи) без потерь");
        c.eq(urep.issues().get(0).file(), "src/ю.py", "UNICODE: имя файла с кириллицей");

        // 7. RED-AS-DATA: exit 1 + валидный passed=false — данные, не исключение (перепроверка §4).
        String redFake = writeFake(dir, "fake-red-adv.sh",
                "#!/bin/sh\n"
                        + "echo '{\"repo\":\".\",\"passed\":false,\"summary\":\"Правил: 1, нарушений: 1\","
                        + "\"issues\":[{\"file\":\"a.py\",\"line\":0,\"rule\":\"r\",\"message\":\"m\","
                        + "\"severity\":\"error\"}]}'\nexit 1\n");
        SpineBeClient redClient = new SpineBeClient(redFake, t, null);
        try {
            ControlReport red = redClient.controlCheck(Path.of("."));
            c.ok(!red.passed(), "RED-AS-DATA: exit 1 + passed=false → данные, не исключение");
        } catch (SpineBeException e) {
            c.failed++;
            System.out.println("[FAIL] RED-AS-DATA: брошено исключение " + e.code());
        }

        // 8. PROMPT-DASH (фейк): промпт с '-'; в argv перед промптом должен стоять '--'.
        String echo = writeFake(dir, "fake-echo-adv.sh", "#!/bin/sh\nprintf '%s\\n' \"$@\"\n");
        SpineBeClient echoClient = new SpineBeClient(echo, t, null);
        RunResult dash = echoClient.run("-черновик ADR", "glm-5.3", null, null);
        List<String> lines = dash.answer().lines().toList();
        int sepIdx = lines.indexOf("--");
        int promptIdx = lines.indexOf("-черновик ADR");
        c.ok(sepIdx >= 0 && promptIdx == sepIdx + 1,
                "PROMPT-DASH: перед промптом с '-' в argv стоит '--'");

        // Битый UTF-8 в потоке: клиент не падает, битые байты → U+FFFD.
        String badUtf = writeFake(dir, "fake-badutf8.sh",
                "#!/bin/sh\nprintf 'начало \\377\\376 конец\\n'\nexit 0\n");
        SpineBeClient badUtfClient = new SpineBeClient(badUtf, t, null);
        RunResult bad = badUtfClient.run("промпт");
        c.ok(bad.answer().contains("�"),
                "битый UTF-8 в stdout: заменён на U+FFFD, клиент не упал");

        // 10. RELATIVE-BINARY-CWD: относительный путь к бинарю + другой cwd процесса.
        //     Cwd специально ГЛУБЖЕ, чем каталог JVM: тогда «../../..» из cwd не
        //     докатывается до бинаря — до фикса это BinaryNotFound.
        //     Ожидание: SDK нормализует путь в абсолютный при создании клиента.
        Path jvmCwd = Path.of("").toAbsolutePath();
        String relBin = jvmCwd.relativize(Path.of(echo)).toString();
        Path otherCwd = jvmCwd.resolve("out-test"); // глубже jvmCwd на уровень
        SpineBeClient relClient = new SpineBeClient(relBin, t, otherCwd);
        try {
            RunResult rel = relClient.run("проверка");
            c.ok(rel.answer().contains("проверка"),
                    "RELATIVE-BINARY-CWD: относительный путь к бинарю работает при чужом cwd");
        } catch (SpineBeException e) {
            c.failed++;
            System.out.println("[FAIL] RELATIVE-BINARY-CWD: " + e.code() + ": " + e.getMessage());
        }
    }

    // ---------- Конкурентность и ресурсы ----------

    private static void clientResources(Checks c, Path dir) throws Exception {
        Duration t = Duration.ofSeconds(15);
        String echo = writeFake(dir, "fake-echo-res.sh", "#!/bin/sh\nprintf '%s\\n' \"$@\"\n");
        SpineBeClient client = new SpineBeClient(echo, t, null);

        // Параллельные вызовы из 10 потоков.
        ExecutorService pool = Executors.newFixedThreadPool(10);
        try {
            Callable<Boolean> task = () -> client.run("параллельный промпт").answer().contains("run");
            List<Future<Boolean>> futs = new java.util.ArrayList<>();
            for (int i = 0; i < 10; i++) {
                futs.add(pool.submit(task));
            }
            boolean all = true;
            for (Future<Boolean> f : futs) {
                all &= f.get(30, TimeUnit.SECONDS);
            }
            c.ok(all, "конкурентность: 10 потоков × run() — все успешны");
        } finally {
            pool.shutdownNow();
        }

        // fd-leak: число открытых /proc/self/fd до и после серии вызовов.
        long fdBefore = countFds();
        for (int i = 0; i < 25; i++) {
            client.run("fd " + i);
        }
        long fdAfter = countFds();
        c.ok(fdAfter <= fdBefore,
                "fd-leak: /proc/self/fd до=" + fdBefore + ", после 25 вызовов=" + fdAfter);
    }

    // ---------- Живой arch-ml (без LLM) ----------

    private static void clientLive(Checks c) throws IOException {
        Path binary = resolveRealBinary();
        if (binary == null || !Files.isExecutable(binary)) {
            System.out.println("[SKIP] живой arch-ml не найден — live-часть adversarial пропущена");
            return;
        }
        binary = binary.toAbsolutePath().normalize();
        Duration t = Duration.ofSeconds(60);
        SpineBeClient client = new SpineBeClient(binary.toString(), t, null);

        // 8. PROMPT-DASH (живой): промпт с '-' и несуществующая модель.
        //    clap обязан доразобрать argv до конца (ошибка «модель не настроена»,
        //    exit 1), а не упасть на «unexpected argument» (exit 2). LLM не вызывается.
        try {
            client.run("-черновик ADR", "bogus-nonexistent-model", Duration.ofSeconds(30), 1);
            c.failed++;
            System.out.println("[FAIL] PROMPT-DASH live: ожидался ProcessFailed");
        } catch (SpineBeException e) {
            c.ok(e.code() == SpineBeException.Code.PROCESS_FAILED && e.exitCode() == 1
                            && e.stderr().contains("модель"),
                    "PROMPT-DASH live: clap принял '-- -промпт' (exit 1, «модель не настроена»), "
                            + "LLM не вызывался");
        }

        // 9. ERROR-SURFACING: control check по несуществующему пути → ProcessFailed + stderr.
        try {
            client.controlCheck(Path.of("/nonexistent-repo-path-xyz"));
            c.failed++;
            System.out.println("[FAIL] ERROR-SURFACING: исключение не брошено");
        } catch (SpineBeException e) {
            c.ok(e.code() == SpineBeException.Code.PROCESS_FAILED,
                    "ERROR-SURFACING: control check по несуществующему пути → ProcessFailed");
            c.ok(e.stderr().contains("/nonexistent-repo-path-xyz"),
                    "ERROR-SURFACING: stderr содержит путь и причину («" + abbrev(e.stderr()) + "»)");
        }
    }

    // ---------- Хелперы ----------

    private static Path resolveRealBinary() {
        String fromEnv = System.getenv("SPINE_BE_BIN");
        if (fromEnv != null && !fromEnv.isBlank()) {
            return Path.of(fromEnv);
        }
        return Path.of("..", "..", "target", "release", "arch-ml");
    }

    private static String writeFake(Path dir, String name, String body) throws IOException {
        Path p = dir.resolve(name);
        Files.writeString(p, body, StandardCharsets.UTF_8);
        Files.setPosixFilePermissions(p, PosixFilePermissions.fromString("rwxr-xr-x"));
        return p.toAbsolutePath().toString();
    }

    /** Число открытых файловых дескрипторов процесса (Linux /proc). */
    private static long countFds() throws IOException {
        try (Stream<Path> s = Files.list(Path.of("/proc/self/fd"))) {
            return s.count();
        }
    }

    /** Жив ли процесс из pid-файла (false, если /proc/<pid> исчез). */
    private static boolean pidAlive(Path pidFile) throws IOException {
        if (!Files.exists(pidFile)) {
            return false;
        }
        String pid = Files.readString(pidFile).trim();
        return Files.exists(Path.of("/proc", pid));
    }

    /** Даёт процессу до millis на исчезновение из /proc. */
    private static void waitForProcGone(Path pidFile, long millis) throws IOException, InterruptedException {
        long deadline = System.currentTimeMillis() + millis;
        while (pidAlive(pidFile) && System.currentTimeMillis() < deadline) {
            Thread.sleep(50);
        }
    }

    private static String abbrev(String s) {
        if (s == null) {
            return "";
        }
        String t = s.trim();
        return t.length() <= 80 ? t : t.substring(0, 80) + "…";
    }
}
