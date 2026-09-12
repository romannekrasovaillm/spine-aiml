#!/usr/bin/env python3
"""deps-check — проверка направления зависимостей по графу `crate::…` (GEN-16).

Детерминированный dogfood-гейт репозитория Spine BE: строит граф импортов
Rust-модулей `src/` и enforce'ит слоистость из AGENTS.md («Architecture map»).
Это стек-нативный аналог ArchUnit/dependency-cruiser для данного репозитория;
в проектах на JVM/TS по той же схеме подключаются ArchUnit/depcruise
(карточка GEN-16, `banking/library/fitness/wave1-mechanizable.yaml`).

Прогон:  python3 scripts/deps-check.py [корень репозитория]
Гейт:    CONSTRAINTS.yaml, правило C-28 (`command_succeeds`); CI-джоба dogfood.

Семантика извлечения совпадает с нативным правилом `dependency_direction`
движка (`src/control.rs`, ADR-029): учитываются и `use crate::…`, и инлайн-пути
`crate::…::`; строки-комментарии (`//`, `///`, `//!`) игнорируются;
гранулярность сопоставления — префикс модульного пути по сегментам.

Код выхода: 0 — нарушений нет; 1 — есть нарушения (список на stdout).
Только stdlib, без сети и внешних зависимостей.
"""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

# Правила слоёв (координаты — сегменты модульного пути после `crate::`).
# allow: модуль обязан префиксно совпадать с одним из разрешённых
#        (пустое множество — запрещены любые crate-зависимости);
# forbid: модуль не должен префиксно совпадать ни с одним запрещённым.
# Ровно одно из двух. Все правила ЗЕЛЁНЫ на коде по состоянию введения —
# неретроактивность (ADR-019): гейт фиксирует текущую слоистость и
# блокирует её эрозию, а не требует рефакторинга задним числом.
RULES = [
    {
        "name": "llm-provider-isolation",
        "include": ["src/llm.rs", "src/llm/**"],
        "allow": {"config", "error", "llm", "net", "retry"},
        "why": "AD-4: провайдерный слой не знает об агенте, инструментах и UI",
    },
    {
        "name": "no-tui-below-ui",
        "include": ["src/**/*.rs"],
        "exclude": ["src/tui.rs", "src/tui/**", "src/export.rs"],
        "forbid": {"tui"},
        "why": "TUI — верхний слой; export.rs — задокументированное "
        "исключение (переиспользует tui::app::ChatBlock, ADR-029)",
    },
    {
        "name": "tools-no-agent-tui",
        "include": ["src/tool.rs", "src/tools.rs", "src/tools/**"],
        "forbid": {"agent", "tui"},
        "why": "реестр инструментов вызывается агентом, а не наоборот",
    },
    {
        "name": "leaf-modules-stay-leaf",
        "include": ["src/error.rs", "src/secrets.rs", "src/retry.rs"],
        "allow": set(),
        "why": "листовые модули не зависят от остального крейта",
    },
    {
        "name": "no-banking-in-core",
        "include": ["src/**/*.rs"],
        "forbid": {"banking"},
        "why": "AD-BE1: банковский слой не импортируется из MIT-ядра",
    },
]

CRATE_PATH = re.compile(r"\bcrate::([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*)")


def glob_match(pattern: str, path: str) -> bool:
    """Простой glob: `**` — любая глубина (включая ноль), `*` — внутри сегмента."""
    pat = pattern.split("/")
    parts = path.split("/")

    def rec(pi: int, ni: int) -> bool:
        while True:
            if pi == len(pat):
                return ni == len(parts)
            if pat[pi] == "**":
                return any(rec(pi + 1, k) for k in range(ni, len(parts) + 1))
            if ni == len(parts):
                return False
            if not fnmatch.fnmatchcase(parts[ni], pat[pi]):
                return False
            pi += 1
            ni += 1

    return rec(0, 0)


def matches_any(patterns: list[str], rel: str) -> bool:
    return any(glob_match(p, rel) for p in patterns)


def extract_modules(file: Path):
    """(строка, модульный путь) для каждого `crate::…` вне строк-комментариев."""
    with file.open(encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            if line.lstrip().startswith("//"):
                continue
            for m in CRATE_PATH.finditer(line):
                yield lineno, m.group(1)


def prefix_hit(module: str, entry: str) -> bool:
    """Совпадение по префиксу модульного пути с границей сегмента."""
    return module == entry or module.startswith(entry + "::")


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    src = root / "src"
    if not src.is_dir():
        print(f"deps-check: каталог не найден: {src}")
        return 1

    files = sorted(p for p in src.rglob("*.rs") if p.is_file())
    violations = []
    for rule in RULES:
        allow = rule.get("allow")
        forbid = rule.get("forbid")
        if (allow is None) == (forbid is None):
            print(f"deps-check: правило '{rule['name']}': ровно одно из allow/forbid")
            return 1
        exclude = rule.get("exclude", [])
        for f in files:
            rel = f.relative_to(root).as_posix()
            if not matches_any(rule["include"], rel) or matches_any(exclude, rel):
                continue
            for lineno, module in extract_modules(f):
                top = module.split("::")[0]
                if top == "crate":
                    continue
                if allow is not None:
                    bad = not any(prefix_hit(module, e) for e in allow)
                else:
                    bad = any(prefix_hit(module, e) for e in forbid)
                if bad:
                    violations.append(
                        f"{rel}:{lineno}: [{rule['name']}] запрещённая зависимость "
                        f"crate::{module} — {rule['why']}"
                    )

    if violations:
        print(f"deps-check: нарушений: {len(violations)}")
        for v in violations:
            print(f"  {v}")
        return 1
    print(f"deps-check: OK (правил: {len(RULES)}, файлов: {len(files)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
