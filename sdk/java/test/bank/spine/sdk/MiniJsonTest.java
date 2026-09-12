package bank.spine.sdk;

import java.util.List;
import java.util.Map;

/**
 * Юнит-тесты встроенного JSON-парсера {@link MiniJson}.
 * Запускаются из {@link TestRunner}, внешних зависимостей нет.
 */
final class MiniJsonTest {

    private MiniJsonTest() {
    }

    @SuppressWarnings("unchecked")
    static void run(Checks c) {
        System.out.println("== MiniJson ==");

        // Скаляры и литералы.
        c.eq(MiniJson.parse("42"), 42L, "целое число → Long");
        c.eq(MiniJson.parse("-7"), -7L, "отрицательное целое");
        c.eq(MiniJson.parse("3.14"), 3.14, "дробное → Double");
        c.eq(MiniJson.parse("1e3"), 1000.0, "экспоненциальная запись");
        c.eq(MiniJson.parse("-2.5E-2"), -0.025, "отрицательная экспонента");
        c.eq(MiniJson.parse("true"), Boolean.TRUE, "true");
        c.eq(MiniJson.parse("false"), Boolean.FALSE, "false");
        c.eq(MiniJson.parse("null"), null, "null");
        c.eq(MiniJson.parse("\"текст\""), "текст", "простая строка");

        // Пробельные символы вокруг значения.
        c.eq(MiniJson.parse("  \n\t 7 \r\n"), 7L, "пробелы вокруг значения");

        // Экранирование в строках.
        c.eq(MiniJson.parse("\"a\\\"b\\\\c\\/d\""), "a\"b\\c/d", "эскейпы \" \\ /");
        c.eq(MiniJson.parse("\"x\\ny\\tz\\r\\b\\f\""), "x\ny\tz\r\b\f", "управляющие эскейпы");

        // Unicode-эскейпы.
        c.eq(MiniJson.parse("\"\\u041f\\u0440\\u0438\\u0432\\u0435\\u0442\""),
                "Привет", "unicode-эскейпы (кириллица)");
        c.eq(MiniJson.parse("\"\\u0041\\u00e9\""), "Aé", "unicode-эскейпы (латиница/диакритика)");
        c.eq(MiniJson.parse("\"\\uD83D\\uDE00\""), "\uD83D\uDE00", "суррогатная пара (смайлик)");
        c.eq(MiniJson.parse("\"пре \\u0041 пост\""), "пре A пост", "эскейп внутри строки");

        // Вложенные объекты и массивы.
        Object nested = MiniJson.parse(
                "{\"a\":{\"b\":{\"c\":[1,2,{\"d\":null}]}},\"e\":[[true],[false]]}");
        c.ok(nested instanceof Map, "вложенность: корень — объект");
        Map<String, Object> a = (Map<String, Object>) ((Map<String, Object>) nested).get("a");
        Map<String, Object> b = (Map<String, Object>) a.get("b");
        List<Object> arr = (List<Object>) b.get("c");
        c.eq(arr.size(), 3, "вложенность: длина массива a.b.c");
        c.eq(arr.get(0), 1L, "вложенность: a.b.c[0]");
        c.eq(((Map<String, Object>) arr.get(2)).get("d"), null, "вложенность: null внутри");
        List<Object> e = (List<Object>) ((Map<String, Object>) nested).get("e");
        c.eq(((List<Object>) e.get(1)).get(0), Boolean.FALSE, "вложенность: e[1][0]");

        // Порядок ключей объекта сохраняется (LinkedHashMap).
        Map<String, Object> ordered = (Map<String, Object>) MiniJson.parse("{\"z\":1,\"a\":2,\"m\":3}");
        c.eq(String.join(",", ordered.keySet()), "z,a,m", "порядок ключей объекта");

        // Пустые структуры.
        c.eq(((Map<?, ?>) MiniJson.parse("{}")).size(), 0, "пустой объект");
        c.eq(((List<?>) MiniJson.parse("[]")).size(), 0, "пустой массив");

        // Pretty-printed JSON в стиле receipt'ов Archify CLI.
        String receipt = """
                {
                  "schemaVersion": 1,
                  "ok": true,
                  "summary": {
                    "components": {"added": 1, "removed": 0},
                    "connections": {"added": 2}
                  },
                  "checks": [{"name": "single_svg", "ok": true, "details": []}]
                }
                """;
        Map<String, Object> rc = (Map<String, Object>) MiniJson.parse(receipt);
        Map<String, Object> summary = (Map<String, Object>) rc.get("summary");
        Map<String, Object> components = (Map<String, Object>) summary.get("components");
        c.eq(components.get("added"), 1L, "receipt-подобный JSON: summary.components.added");
        c.eq(((Map<String, Object>) summary.get("connections")).get("added"), 2L,
                "receipt-подобный JSON: summary.connections.added");
        c.eq(((List<Object>) rc.get("checks")).size(), 1, "receipt-подобный JSON: checks");

        // Ошибки разбора.
        c.ok(throwsJson("{\"a\":1} хвост"), "ошибка: лишние символы после значения");
        c.ok(throwsJson("\"незакрытая"), "ошибка: незакрытая строка");
        c.ok(throwsJson("\"bad \\x\""), "ошибка: неизвестный эскейп");
        c.ok(throwsJson("{\"a\" 1}"), "ошибка: нет двоеточия");
        c.ok(throwsJson("[1,2"), "ошибка: незакрытый массив");
        c.ok(throwsJson("tru"), "ошибка: битый литерал");
        c.ok(throwsJson("{\"a\":01x}"), "ошибка: мусор вместо значения");
    }

    private static boolean throwsJson(String input) {
        try {
            MiniJson.parse(input);
            return false;
        } catch (MiniJson.JsonException e) {
            return true;
        }
    }
}
