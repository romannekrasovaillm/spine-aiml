package bank.spine.sdk;

/**
 * Исключение SDK с кодом по §4 контракта (sdk/CONTRACT.md).
 *
 * <p>Коды: BINARY_NOT_FOUND — бинарь не найден/не исполняемый;
 * TIMEOUT — клиентский таймаут, процесс убит;
 * PROCESS_FAILED — ненулевой exit без валидного JSON-контракта
 * (несёт exitCode и stderr); CONTRACT_VIOLATION — stdout не парсится
 * как JSON там, где контракт требует JSON.
 *
 * <p>Результат {@code control check} с {@code passed=false} — это ДАННЫЕ
 * ({@link ControlReport}), а не исключение.
 */
public final class SpineBeException extends RuntimeException {

    /** Коды ошибок по §4 контракта. */
    public enum Code {
        BINARY_NOT_FOUND,
        TIMEOUT,
        PROCESS_FAILED,
        CONTRACT_VIOLATION
    }

    private final Code code;
    private final int exitCode;
    private final String stderr;

    private SpineBeException(Code code, String message, int exitCode, String stderr, Throwable cause) {
        super(message, cause);
        this.code = code;
        this.exitCode = exitCode;
        this.stderr = stderr;
    }

    public static SpineBeException binaryNotFound(String binary, Throwable cause) {
        return new SpineBeException(Code.BINARY_NOT_FOUND,
                "бинарь arch-ml не найден или не исполняемый: " + binary, -1, "", cause);
    }

    public static SpineBeException timeout(String binary, long timeoutMs) {
        return new SpineBeException(Code.TIMEOUT,
                "клиентский таймаут " + timeoutMs + " мс, процесс убит: " + binary, -1, "", null);
    }

    public static SpineBeException processFailed(int exitCode, String stderr) {
        return new SpineBeException(Code.PROCESS_FAILED,
                "arch-ml завершился с кодом " + exitCode + ": " + abbrev(stderr),
                exitCode, stderr, null);
    }

    public static SpineBeException contractViolation(String detail, Throwable cause) {
        return new SpineBeException(Code.CONTRACT_VIOLATION,
                "stdout не соответствует JSON-контракту: " + detail, -1, "", cause);
    }

    private static String abbrev(String s) {
        if (s == null) {
            return "";
        }
        String t = s.trim();
        return t.length() <= 200 ? t : t.substring(0, 200) + "…";
    }

    /** Код ошибки. */
    public Code code() {
        return code;
    }

    /** Exit-код процесса (только для PROCESS_FAILED, иначе -1). */
    public int exitCode() {
        return exitCode;
    }

    /** stderr процесса (только для PROCESS_FAILED, иначе пусто). */
    public String stderr() {
        return stderr;
    }
}
