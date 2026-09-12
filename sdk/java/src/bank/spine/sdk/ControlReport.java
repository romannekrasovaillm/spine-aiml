package bank.spine.sdk;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * Типизированный FitnessReport ({@code arch-ml control check --json}).
 *
 * <p>ВАЖНО по §4 контракта: {@code passed=false} — это данные, а не
 * исключение. Исключение бросается только когда JSON не распарсился
 * ({@link SpineBeException.Code#CONTRACT_VIOLATION}) или exit отличен
 * от 0/1 без валидного JSON ({@link SpineBeException.Code#PROCESS_FAILED}).
 */
public final class ControlReport {

    private final String repo;
    private final boolean passed;
    private final String summary;
    private final List<Issue> issues;

    private ControlReport(String repo, boolean passed, String summary, List<Issue> issues) {
        this.repo = repo;
        this.passed = passed;
        this.summary = summary;
        this.issues = Collections.unmodifiableList(issues);
    }

    /** Разбирает одну строку JSON FitnessReport. */
    @SuppressWarnings("unchecked")
    static ControlReport fromJson(String json) {
        Object root;
        try {
            root = MiniJson.parse(json);
        } catch (MiniJson.JsonException e) {
            throw SpineBeException.contractViolation("control check: " + e.getMessage(), e);
        }
        if (!(root instanceof Map)) {
            throw SpineBeException.contractViolation("control check: корень не объект", null);
        }
        Map<String, Object> m = (Map<String, Object>) root;
        Object passed = m.get("passed");
        if (!(passed instanceof Boolean)) {
            throw SpineBeException.contractViolation("control check: нет булева поля passed", null);
        }
        List<Issue> issues = new ArrayList<>();
        Object rawIssues = m.get("issues");
        if (rawIssues instanceof List) {
            for (Object o : (List<Object>) rawIssues) {
                if (o instanceof Map) {
                    Map<String, Object> im = (Map<String, Object>) o;
                    issues.add(new Issue(
                            str(im.get("file")),
                            num(im.get("line")),
                            str(im.get("rule")),
                            str(im.get("message")),
                            str(im.get("severity"))));
                }
            }
        }
        return new ControlReport(str(m.get("repo")), (Boolean) passed, str(m.get("summary")), issues);
    }

    private static String str(Object o) {
        return o == null ? "" : o.toString();
    }

    private static long num(Object o) {
        return o instanceof Number ? ((Number) o).longValue() : 0L;
    }

    /** Репозиторий, для которого выполнялась проверка. */
    public String repo() {
        return repo;
    }

    /** true — гейт зелёный; false — есть нарушения (это данные, не ошибка). */
    public boolean passed() {
        return passed;
    }

    /** Человекочитаемая сводка («Правил: 3, нарушений: 0 …»). */
    public String summary() {
        return summary;
    }

    /** Список нарушений (пуст при passed=true). */
    public List<Issue> issues() {
        return issues;
    }

    @Override
    public String toString() {
        return "ControlReport{passed=" + passed + ", issues=" + issues.size()
                + ", summary=\"" + summary + "\"}";
    }
}
