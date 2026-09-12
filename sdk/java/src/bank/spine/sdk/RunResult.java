package bank.spine.sdk;

/**
 * Результат headless-прогона агента ({@code arch-ml run -q}).
 *
 * @param answer финальный ответ ассистента (произвольный текст, не JSON)
 * @param model имя модели, если было запрошено (может быть null)
 * @param durationMs длительность прогона в миллисекундах (клиентские часы)
 */
public record RunResult(String answer, String model, long durationMs) {
}
