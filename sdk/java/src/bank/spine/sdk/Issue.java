package bank.spine.sdk;

/**
 * Одна находка fitness-контроля (элемент {@code issues} FitnessReport).
 *
 * @param file путь к файлу
 * @param line строка (0 — находка на файл целиком)
 * @param rule имя правила
 * @param message текст нарушения
 * @param severity "error" | "warn"
 */
public record Issue(String file, long line, String rule, String message, String severity) {
}
