#!/usr/bin/env python3
"""Верификатор gs-http-01 (G3: образец «читает то, что писал не агент»).

Читается ровно один источник — журнал мока, который сервер ведёт сам на хосте,
вне контейнера. Снимок состояния песочницы здесь не нужен вовсе: агент может
написать в /work что угодно, на вердикт это не влияет.

Числа верификатор не берёт из тела запроса как из истины: он **пересчитывает**
ожидаемый отчёт тем же генератором выборки (`gen_items`, импортируется из
mock_server.py по пути --mock-module) и сверяет с телом записи. Так даже
согласованная подделка журнала не проходит: генератор живёт вне песочницы.

Контракт выхода — как у остальных верификаторов набора.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

ALLOWED_READ_PREFIXES = ("snapshot:", "pristine:", "journal:", "holdout:", "reexec:", "generator:")


def load_mock_module(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location("gs_mock_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def emit(score: int, reason: str, expected_public: str, reads: list[str], detail: dict) -> int:
    assert score in (0, 1)
    for r in reads:
        assert r.split(":", 1)[0] + ":" in ALLOWED_READ_PREFIXES, r
    print(
        json.dumps(
            {
                "score": score,
                "reason": reason,
                "expected_public": expected_public,
                "reads": reads,
                "detail": detail,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True, type=pathlib.Path)
    ap.add_argument("--task", required=True, type=pathlib.Path)
    ap.add_argument("--seed", required=True, type=pathlib.Path)
    ap.add_argument("--image", default="")
    ap.add_argument("--holdout", default="")
    ap.add_argument("--journal", required=True, type=pathlib.Path)
    ap.add_argument("--mock-module", required=True, type=pathlib.Path)
    ap.add_argument("--mock-seed", required=True, type=int)
    ap.add_argument("--scratch", default="")
    args = ap.parse_args()

    mock = load_mock_module(args.mock_module)
    want = mock.expected_report(args.mock_seed)
    public = f"items_count={want['items_count']}, total_qty={want['total_qty']}"
    reads = ["journal:POST /v1/report", "generator:gen_items(seed)"]

    if not args.journal.is_file():
        return emit(0, "журнал мока отсутствует — сервер не был запущен", public, reads, {})
    entries = []
    for line in args.journal.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    posts = [e for e in entries if e.get("method") == "POST" and str(e.get("path", "")).startswith("/v1/report")]
    gets = [e for e in entries if e.get("method") == "GET"]
    # Маркер ротации в первой строке — след отката состояния среды: журнал обнулён
    # сервером перед этим прогоном, а не накоплен предыдущими.
    rotated = bool(entries) and entries[0].get("event") == "journal_rotated"
    detail = {"journal_entries": len(entries), "posts": len(posts), "gets": len(gets), "journal_rotated": rotated}
    if not posts:
        return emit(0, "в журнале мока нет ни одного POST /v1/report — действия не было", public, reads, detail)
    accepted = [e for e in posts if e.get("accepted") is True]
    if not accepted:
        return emit(0, f"мок не принял ни одного отчёта (последняя причина: {posts[-1].get('reason')!r})", public, reads, detail)
    body = accepted[-1].get("body") or {}
    if body.get("items_count") != want["items_count"] or body.get("total_qty") != want["total_qty"]:
        detail["body"] = body
        return emit(0, "принятое сервером тело расходится с независимым пересчётом", public, reads, detail)
    detail["receipt_seq"] = accepted[-1].get("seq")
    return emit(1, f"сервер принял отчёт {body} — совпал с независимым пересчётом по генератору выборки", public, reads, detail)


if __name__ == "__main__":
    sys.exit(main())
