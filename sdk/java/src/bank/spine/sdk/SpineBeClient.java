package bank.spine.sdk;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * Тонкий клиент Spine-BE поверх headless CLI {@code arch-ml}.
 *
 * <p>Запускает процесс без shell (argv через {@link ProcessBuilder}),
 * читает stdout/stderr/exit code, разбирает JSON по контракту
 * (sdk/CONTRACT.md). Сетевых вызовов нет.
 *
 * <p>Бинарь: env {@code SPINE_BE_BIN} → иначе {@code arch-ml} из PATH.
 * Клиентский таймаут реализован через {@code waitFor(timeout)}:
 * по истечении процесс убивается ({@code destroyForcibly}) и бросается
 * {@link SpineBeException.Code#TIMEOUT}.
 */
public final class SpineBeClient {

    /** Дефолтный клиентский таймаут (переопределяется per-call). */
    public static final Duration DEFAULT_TIMEOUT = Duration.ofSeconds(120);

    private final String binary;
    private final Duration defaultTimeout;
    private final Path cwd;

    /** Клиент с бинарём из {@code SPINE_BE_BIN} / PATH и дефолтным таймаутом. */
    public SpineBeClient() {
        this(DEFAULT_TIMEOUT);
    }

    /** Клиент с переопределённым дефолтным таймаутом. */
    public SpineBeClient(Duration defaultTimeout) {
        this(resolveBinaryFromEnv(), defaultTimeout, null);
    }

    /** Полная форма: явный бинарь, таймаут, рабочий каталог (null — наследовать). */
    public SpineBeClient(String binary, Duration defaultTimeout, Path cwd) {
        this.binary = normalizeBinary(binary);
        this.defaultTimeout = defaultTimeout;
        this.cwd = cwd;
    }

    /**
     * Путь к бинарю с разделителем каталогов нормализуется в абсолютный:
     * иначе относительный путь резолвился бы от cwd ДОЧЕРНЕГО процесса
     * (который может отличаться от cwd JVM) и ломался бы с BinaryNotFound.
     * Голое имя без разделителей («arch-ml») оставляем как есть — оно
     * резолвится через PATH.
     */
    private static String normalizeBinary(String binary) {
        if (binary.contains("/") || binary.contains("\\")) {
            return Path.of(binary).toAbsolutePath().normalize().toString();
        }
        return binary;
    }

    private static String resolveBinaryFromEnv() {
        String fromEnv = System.getenv("SPINE_BE_BIN");
        return (fromEnv == null || fromEnv.isBlank()) ? "arch-ml" : fromEnv;
    }

    // ---- 1. run — headless-прогон агента (LLM) ----

    /** Прогон агента с дефолтными параметрами. */
    public RunResult run(String prompt) {
        return run(prompt, null, null, null);
    }

    /**
     * Прогон агента: {@code arch-ml run -q [--model M] [--timeout S]
     * [--max-turns N] PROMPT}.
     *
     * @param prompt промпт (передаётся аргументом argv, без shell)
     * @param model имя модели (null — не передавать флаг)
     * @param timeout клиентский таймаут (null — дефолт клиента); он же
     *                передаётся бинарю как {@code --timeout}
     * @param maxTurns лимит ходов агента (null — не передавать флаг)
     */
    public RunResult run(String prompt, String model, Duration timeout, Integer maxTurns) {
        Duration effective = timeout != null ? timeout : defaultTimeout;
        List<String> argv = new ArrayList<>(List.of(binary, "run", "-q"));
        if (model != null) {
            argv.add("--model");
            argv.add(model);
        }
        if (timeout != null) {
            argv.add("--timeout");
            argv.add(String.valueOf(timeout.getSeconds()));
        }
        if (maxTurns != null) {
            argv.add("--max-turns");
            argv.add(String.valueOf(maxTurns));
        }
        // Промпт может начинаться с '-' — clap бинаря иначе примет его за флаг
        // («unexpected argument», exit 2). Сепаратор '--' завершает разбор опций.
        argv.add("--");
        argv.add(prompt);

        long started = System.nanoTime();
        ProcResult r = exec(argv, effective);
        long durationMs = (System.nanoTime() - started) / 1_000_000;
        if (r.exitCode != 0) {
            throw SpineBeException.processFailed(r.exitCode, r.stderr);
        }
        return new RunResult(r.stdout, model, durationMs);
    }

    // ---- 2. control check — fitness-контроль ----

    /** Fitness-контроль без явного файла правил. */
    public ControlReport controlCheck(Path repo) {
        return controlCheck(repo, null, null);
    }

    /**
     * Fitness-контроль: {@code arch-ml control check REPO [--constraints P] --json}.
     *
     * <p>Exit 0 → passed=true; exit 1 с валидным JSON → passed=false
     * (ЭТО ДАННЫЕ, не исключение); exit 1 без JSON → ProcessFailed.
     */
    public ControlReport controlCheck(Path repo, Path constraints, Duration timeout) {
        List<String> argv = new ArrayList<>(List.of(binary, "control", "check", repo.toString()));
        if (constraints != null) {
            argv.add("--constraints");
            argv.add(constraints.toString());
        }
        argv.add("--json");

        ProcResult r = exec(argv, timeout != null ? timeout : defaultTimeout);
        if (r.exitCode == 0 || r.exitCode == 1) {
            try {
                return ControlReport.fromJson(r.stdout.trim());
            } catch (SpineBeException e) {
                if (r.exitCode == 1 && e.code() == SpineBeException.Code.CONTRACT_VIOLATION) {
                    // exit 1 без валидного JSON — ошибка запуска, причина в stderr.
                    throw SpineBeException.processFailed(r.exitCode, r.stderr);
                }
                throw e;
            }
        }
        throw SpineBeException.processFailed(r.exitCode, r.stderr);
    }

    // ---- 3. archify — диаграммы ----

    /** {@code arch-ml archify validate TYPE IR_PATH --json}. */
    public ArchifyReceipt archifyValidate(String type, Path irPath) {
        return archifyValidate(type, irPath, null);
    }

    public ArchifyReceipt archifyValidate(String type, Path irPath, Duration timeout) {
        return archify(List.of("validate", type, irPath.toString()), "validate", timeout);
    }

    /** {@code arch-ml archify deliver TYPE IR_PATH OUT_HTML --json}. */
    public ArchifyReceipt archifyDeliver(String type, Path irPath, Path outHtml) {
        return archifyDeliver(type, irPath, outHtml, null);
    }

    public ArchifyReceipt archifyDeliver(String type, Path irPath, Path outHtml, Duration timeout) {
        return archify(List.of("deliver", type, irPath.toString(), outHtml.toString()), "deliver", timeout);
    }

    /** {@code arch-ml archify compare BASE_IR HEAD_IR OUT_HTML --json}. */
    public ArchifyReceipt archifyCompare(Path baseIr, Path headIr, Path outHtml) {
        return archifyCompare(baseIr, headIr, outHtml, null);
    }

    public ArchifyReceipt archifyCompare(Path baseIr, Path headIr, Path outHtml, Duration timeout) {
        return archify(List.of("compare", baseIr.toString(), headIr.toString(), outHtml.toString()),
                "compare", timeout);
    }

    private ArchifyReceipt archify(List<String> args, String label, Duration timeout) {
        List<String> argv = new ArrayList<>();
        argv.add(binary);
        argv.add("archify");
        argv.addAll(args);
        argv.add("--json");
        ProcResult r = exec(argv, timeout != null ? timeout : defaultTimeout);
        if (r.exitCode != 0) {
            throw SpineBeException.processFailed(r.exitCode, r.stderr);
        }
        return ArchifyReceipt.fromJson(r.stdout, "archify " + label);
    }

    // ---- Запуск процесса ----

    private record ProcResult(int exitCode, String stdout, String stderr) {
    }

    /** Запуск без shell; stdout/stderr читаются параллельно; таймаут — kill. */
    private ProcResult exec(List<String> argv, Duration timeout) {
        ProcessBuilder pb = new ProcessBuilder(argv);
        if (cwd != null) {
            pb.directory(cwd.toFile());
        }
        Process proc;
        try {
            proc = pb.start();
        } catch (IOException e) {
            throw SpineBeException.binaryNotFound(binary, e);
        }
        ByteArrayOutputStream outBuf = new ByteArrayOutputStream();
        ByteArrayOutputStream errBuf = new ByteArrayOutputStream();
        Thread outReader = pump(proc.getInputStream(), outBuf);
        Thread errReader = pump(proc.getErrorStream(), errBuf);
        try {
            boolean done = proc.waitFor(timeout.toMillis(), TimeUnit.MILLISECONDS);
            if (!done) {
                killTree(proc);
                proc.waitFor(); // дождаться смерти, чтобы не оставить зомби
                throw SpineBeException.timeout(binary, timeout.toMillis());
            }
            joinQuietly(outReader);
            joinQuietly(errReader);
            return new ProcResult(proc.exitValue(),
                    outBuf.toString(StandardCharsets.UTF_8),
                    errBuf.toString(StandardCharsets.UTF_8));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            killTree(proc);
            throw SpineBeException.timeout(binary, timeout.toMillis());
        }
    }

    /**
     * Убивает процесс и ВСЕХ его потомков: голый {@code destroyForcibly()}
     * бьёт только прямого ребёнка, а его дети (например, {@code sleep} из
     * shell-скрипта) остаются сиротами и доживают свой срок.
     */
    private static void killTree(Process proc) {
        proc.descendants().forEach(ProcessHandle::destroyForcibly);
        proc.destroyForcibly();
    }

    private static Thread pump(InputStream in, ByteArrayOutputStream buf) {
        Thread t = new Thread(() -> {
            try {
                in.transferTo(buf);
            } catch (IOException ignored) {
                // Поток закрылся при destroyForcibly — это штатно, читаем что есть.
            }
        });
        t.setDaemon(true);
        t.start();
        return t;
    }

    private static void joinQuietly(Thread t) {
        try {
            t.join(TimeUnit.SECONDS.toMillis(5));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    /** Путь к бинарю, который использует клиент. */
    public String binary() {
        return binary;
    }
}
