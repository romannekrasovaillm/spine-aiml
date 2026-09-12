package bank.spine.sdk;

import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * Обёртка над receipt'ом Archify CLI ({@code arch-ml archify … --json}).
 *
 * <p>По §3 контракта SDK передаёт receipt вызывающему коду целиком:
 * типизированы только общие поля ({@code schemaVersion}, {@code ok},
 * {@code command}, {@code type}), остальное доступно как generic JSON
 * через {@link #raw()} и навигационные хелперы.
 */
public final class ArchifyReceipt {

    private final Map<String, Object> raw;

    private ArchifyReceipt(Map<String, Object> raw) {
        this.raw = raw;
    }

    /** Разбирает JSON-receipt (допускается pretty-printed вывод). */
    @SuppressWarnings("unchecked")
    static ArchifyReceipt fromJson(String json, String commandLabel) {
        Object root;
        try {
            root = MiniJson.parse(json);
        } catch (MiniJson.JsonException e) {
            throw SpineBeException.contractViolation(commandLabel + ": " + e.getMessage(), e);
        }
        if (!(root instanceof Map)) {
            throw SpineBeException.contractViolation(commandLabel + ": корень не объект", null);
        }
        return new ArchifyReceipt((Map<String, Object>) root);
    }

    /** Весь receipt как неизменяемое представление generic JSON. */
    public Map<String, Object> raw() {
        return Collections.unmodifiableMap(raw);
    }

    /** Поле {@code schemaVersion} (по контракту — 1). */
    public long schemaVersion() {
        return getLong("schemaVersion");
    }

    /** Поле {@code ok}. */
    public boolean ok() {
        Object v = raw.get("ok");
        return v instanceof Boolean && (Boolean) v;
    }

    /** Поле {@code command} (validate/deliver/compare). */
    public String command() {
        return getString("command");
    }

    /** Поле {@code type} (architecture/workflow/…). */
    public String type() {
        return getString("type");
    }

    /** Произвольное верхнеуровневое поле. */
    public Object get(String key) {
        return raw.get(key);
    }

    /** Строковое поле (null → null). */
    public String getString(String key) {
        Object v = raw.get(key);
        return v == null ? null : v.toString();
    }

    /** Числовое поле как long (отсутствует → 0). */
    public long getLong(String key) {
        Object v = raw.get(key);
        return v instanceof Number ? ((Number) v).longValue() : 0L;
    }

    /** Вложенный объект поля (отсутствует → пустая map). */
    @SuppressWarnings("unchecked")
    public Map<String, Object> getMap(String key) {
        Object v = raw.get(key);
        return v instanceof Map ? (Map<String, Object>) v : Map.of();
    }

    /** Список-поле (отсутствует → пустой список). */
    @SuppressWarnings("unchecked")
    public List<Object> getList(String key) {
        Object v = raw.get(key);
        return v instanceof List ? (List<Object>) v : List.of();
    }

    /**
     * Число по пути из вложенных объектов, напр. {@code longAt("summary",
     * "components", "added")}; отсутствующий путь → 0.
     */
    public long longAt(String... path) {
        Object cur = raw;
        for (String key : path) {
            if (!(cur instanceof Map)) {
                return 0L;
            }
            cur = ((Map<?, ?>) cur).get(key);
        }
        return cur instanceof Number ? ((Number) cur).longValue() : 0L;
    }

    @Override
    public String toString() {
        return "ArchifyReceipt{command=" + command() + ", type=" + type() + ", ok=" + ok() + "}";
    }
}
