package bank.spine.sdk;

/**
 * Точка входа тестов Spine-BE SDK v1 (Java).
 *
 * <p>Прогоняет юнит-тесты MiniJson, юнит-тесты клиента на фейк-бинарях
 * и живые интеграционные тесты (skip с предупреждением, если бинаря нет).
 * При любом падении — {@code System.exit(1)}.
 */
public final class TestRunner {

    private TestRunner() {
    }

    public static void main(String[] args) throws Exception {
        Checks c = new Checks();
        MiniJsonTest.run(c);
        ClientUnitTest.run(c);
        AdversarialTest.run(c);
        IntegrationTest.run(c);

        System.out.println();
        System.out.println("Итого: " + c.passed + " passed, " + c.failed + " failed");
        if (c.failed > 0) {
            System.out.println("ТЕСТЫ УПАЛИ");
            System.exit(1);
        }
        System.out.println("ВСЕ ТЕСТЫ ЗЕЛЁНЫЕ");
    }
}
