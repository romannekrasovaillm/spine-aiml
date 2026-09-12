package bank.spine.sdk;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Минимальный JSON-парсер без внешних зависимостей.
 *
 * <p>Отображение типов: object → {@link LinkedHashMap} (порядок ключей
 * сохраняется), array → {@link ArrayList}, string → {@link String},
 * целые числа → {@link Long}, дробные/экспоненциальные → {@link Double},
 * true/false → {@link Boolean}, null → {@code null}.
 *
 * <p>Поддерживаются экранирование (\" \\ \/ \b \f \n \r \t), unicode-эскейпы
 * \\uXXXX (включая суррогатные пары), произвольная вложенность и ведущие/
 * завершающие пробельные символы. При ошибке разбора бросается
 * {@link JsonException} с позицией во входной строке.
 *
 * <p>Пределы и особенности (QA-документация):
 * <ul>
 *   <li>Глубина вложенности ограничена {@link #MAX_DEPTH} уровнями — парсер
 *       рекурсивный, без лимита глубокий JSON ронял бы поток с
 *       {@link StackOverflowError}. Превышение — понятная {@link JsonException}.</li>
 *   <li>Дубликаты ключей объекта: последний wins ({@code LinkedHashMap.put}),
 *       позиция ключа в порядке итерации — от первого вхождения.</li>
 *   <li>Целое {@code -0} разбирается как {@code 0L} (знак теряется);
 *       {@code -0.0} сохраняет знак (Double). Переполнение экспоненты
 *       ({@code 1e999}) → {@code Double.POSITIVE_INFINITY}; целое вне диапазона
 *       long → {@link JsonException}.</li>
 * </ul>
 */
public final class MiniJson {

    /**
     * Максимальная глубина вложенности JSON (объекты + массивы суммарно).
     * Реальные receipt'ы контракта не глубже десятка уровней; лимит с большим
     * запасом защищает рекурсивный разбор от {@link StackOverflowError}.
     */
    public static final int MAX_DEPTH = 1024;

    /** Ошибка разбора JSON с позицией во входном тексте. */
    public static final class JsonException extends RuntimeException {
        public JsonException(String message, int pos) {
            super(message + " (позиция " + pos + ")");
        }
    }

    private final String src;
    private int pos;
    private int depth;

    private MiniJson(String src) {
        this.src = src;
        this.pos = 0;
    }

    /** Разбирает строку целиком; хвост после значения — ошибка. */
    public static Object parse(String text) {
        if (text == null) {
            throw new JsonException("входная строка равна null", 0);
        }
        MiniJson p = new MiniJson(text);
        p.skipWs();
        Object value = p.parseValue();
        p.skipWs();
        if (p.pos != p.src.length()) {
            throw new JsonException("лишние символы после JSON-значения", p.pos);
        }
        return value;
    }

    private Object parseValue() {
        if (++depth > MAX_DEPTH) {
            throw error("превышена максимальная вложенность JSON (" + MAX_DEPTH + ")");
        }
        try {
            return parseValueAtDepth();
        } finally {
            depth--;
        }
    }

    private Object parseValueAtDepth() {
        if (pos >= src.length()) {
            throw error("неожиданный конец строки");
        }
        char c = src.charAt(pos);
        return switch (c) {
            case '{' -> parseObject();
            case '[' -> parseArray();
            case '"' -> parseString();
            case 't' -> parseLiteral("true", Boolean.TRUE);
            case 'f' -> parseLiteral("false", Boolean.FALSE);
            case 'n' -> parseLiteral("null", null);
            default -> {
                if (c == '-' || (c >= '0' && c <= '9')) {
                    yield parseNumber();
                }
                throw error("неожиданный символ '" + c + "'");
            }
        };
    }

    private Map<String, Object> parseObject() {
        expect('{');
        Map<String, Object> map = new LinkedHashMap<>();
        skipWs();
        if (peek('}')) {
            pos++;
            return map;
        }
        while (true) {
            skipWs();
            if (!peek('"')) {
                throw error("ожидался строковый ключ объекта");
            }
            String key = parseString();
            skipWs();
            expect(':');
            skipWs();
            map.put(key, parseValue());
            skipWs();
            if (peek(',')) {
                pos++;
                continue;
            }
            expect('}');
            return map;
        }
    }

    private List<Object> parseArray() {
        expect('[');
        List<Object> list = new ArrayList<>();
        skipWs();
        if (peek(']')) {
            pos++;
            return list;
        }
        while (true) {
            skipWs();
            list.add(parseValue());
            skipWs();
            if (peek(',')) {
                pos++;
                continue;
            }
            expect(']');
            return list;
        }
    }

    private String parseString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= src.length()) {
                throw error("незакрытая строка");
            }
            char c = src.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                parseEscapeInto(sb);
            } else {
                if (c < 0x20) {
                    throw error("неэкранированный управляющий символ в строке");
                }
                sb.append(c);
            }
        }
    }

    private void parseEscapeInto(StringBuilder sb) {
        if (pos >= src.length()) {
            throw error("обрыв escape-последовательности");
        }
        char e = src.charAt(pos++);
        switch (e) {
            case '"' -> sb.append('"');
            case '\\' -> sb.append('\\');
            case '/' -> sb.append('/');
            case 'b' -> sb.append('\b');
            case 'f' -> sb.append('\f');
            case 'n' -> sb.append('\n');
            case 'r' -> sb.append('\r');
            case 't' -> sb.append('\t');
            case 'u' -> {
                int hi = readHex4();
                // Суррогатная пара: старший \\uXXXX сразу за ним младший.
                if (Character.isHighSurrogate((char) hi)
                        && pos + 1 < src.length()
                        && src.charAt(pos) == '\\' && src.charAt(pos + 1) == 'u') {
                    int save = pos;
                    pos += 2;
                    int lo = readHex4();
                    if (Character.isLowSurrogate((char) lo)) {
                        sb.appendCodePoint(Character.toCodePoint((char) hi, (char) lo));
                        return;
                    }
                    pos = save;
                }
                sb.append((char) hi);
            }
            default -> throw error("неизвестная escape-последовательность \\" + e);
        }
    }

    private int readHex4() {
        if (pos + 4 > src.length()) {
            throw error("обрыв \\uXXXX-эскейпа");
        }
        int v = 0;
        for (int i = 0; i < 4; i++) {
            char h = src.charAt(pos++);
            int d = Character.digit(h, 16);
            if (d < 0) {
                throw error("недопустимая hex-цифра '" + h + "' в \\uXXXX");
            }
            v = (v << 4) | d;
        }
        return v;
    }

    private Object parseLiteral(String lit, Object value) {
        if (src.startsWith(lit, pos)) {
            pos += lit.length();
            return value;
        }
        throw error("ожидался литерал '" + lit + "'");
    }

    private Object parseNumber() {
        int start = pos;
        if (peek('-')) {
            pos++;
        }
        while (pos < src.length() && Character.isDigit(src.charAt(pos))) {
            pos++;
        }
        boolean fractional = false;
        if (peek('.')) {
            fractional = true;
            pos++;
            while (pos < src.length() && Character.isDigit(src.charAt(pos))) {
                pos++;
            }
        }
        if (peek('e') || peek('E')) {
            fractional = true;
            pos++;
            if (peek('+') || peek('-')) {
                pos++;
            }
            while (pos < src.length() && Character.isDigit(src.charAt(pos))) {
                pos++;
            }
        }
        String num = src.substring(start, pos);
        try {
            return fractional ? (Object) Double.valueOf(num) : Long.valueOf(num);
        } catch (NumberFormatException e) {
            throw error("некорректное число '" + num + "'");
        }
    }

    private void skipWs() {
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                pos++;
            } else {
                break;
            }
        }
    }

    private boolean peek(char c) {
        return pos < src.length() && src.charAt(pos) == c;
    }

    private void expect(char c) {
        if (!peek(c)) {
            throw error("ожидался символ '" + c + "'");
        }
        pos++;
    }

    private JsonException error(String message) {
        return new JsonException(message, pos);
    }
}
