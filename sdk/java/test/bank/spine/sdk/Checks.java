package bank.spine.sdk;

/**
 * Мини-фреймворк проверок: понятные сообщения, счётчики, без зависимостей.
 */
final class Checks {

    int passed;
    int failed;

    void ok(boolean cond, String name) {
        if (cond) {
            passed++;
            System.out.println("[OK]   " + name);
        } else {
            failed++;
            System.out.println("[FAIL] " + name);
        }
    }

    void eq(Object actual, Object expected, String name) {
        boolean same = expected == null ? actual == null : expected.equals(actual);
        if (same) {
            ok(true, name);
        } else {
            failed++;
            System.out.println("[FAIL] " + name + " — ожидалось: <" + expected + ">, получено: <" + actual + ">");
        }
    }

    /** Проверка, что действие бросает SpineBeException с нужным кодом. */
    void throwsCode(SpineBeException.Code code, String name, Runnable action) {
        try {
            action.run();
            failed++;
            System.out.println("[FAIL] " + name + " — исключение не брошено");
        } catch (SpineBeException e) {
            if (e.code() == code) {
                ok(true, name + " (код " + code + ")");
            } else {
                failed++;
                System.out.println("[FAIL] " + name + " — ожидался код " + code + ", получен " + e.code()
                        + ": " + e.getMessage());
            }
        }
    }
}
