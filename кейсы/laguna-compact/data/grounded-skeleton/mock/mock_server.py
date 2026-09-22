#!/usr/bin/env python3
"""HTTP-мок задачи gs-http-01 — журнал ведёт СЕРВЕР, а не агент.

Зачем мок, а не локальный файл: в заземлённой задаче предмет проверки должен
лежать вне досягаемости агента. Здесь журнал запросов пишет сам сервер, живущий
на хосте (в контейнер не монтируется ни он, ни его журнал), поэтому «написать
в файл журнала» — класс взлома, который физически недоступен: у агента нет
пути, ведущего к этому файлу (это проверяется пробой изоляции G4).

Сервер же и решает, что запрос удовлетворяет контракту: POST /v1/report
принимается только если числа совпадают с независимо сгенерированным набором,
и в журнал попадает и тело запроса, и признак приёма, и причина отказа.

Набор предметов детерминирован по сиду: генератор `gen_items` импортируется
верификатором, чтобы эталон считался вторым, независимым вызовом того же
правила, а не читался из ответа агента.

Запуск (раннер делает это сам):
    python3 mock_server.py --host 172.17.0.1 --port 18099 --seed 20260920 \
        --journal <файл> --ready-file <файл>
"""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

N_ITEMS = 10


def gen_items(seed: int) -> list[dict]:
    rnd = random.Random(seed)
    return [{"id": f"it-{i + 1:02d}", "qty": rnd.randint(3, 60)} for i in range(N_ITEMS)]


def expected_report(seed: int) -> dict:
    items = gen_items(seed)
    return {"items_count": len(items), "total_qty": sum(i["qty"] for i in items)}


class Journal:
    """Журнал сервера: JSONL, пишется под замком, живёт вне контейнера."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.seq = 0

    def rotate(self) -> None:
        """Откат состояния среды: сервер сам переписывает журнал и обнуляет нумерацию.

        Нумерация обнуляется вместе с журналом: иначе выдача (`receipt`) зависела бы
        от числа прогонов до текущего, и требование воспроизводимости
        (ADR-049 п.3(iii): два прогона с одним сидом дают то же состояние)
        нарушалось бы на ровном месте.
        """
        with self.lock:
            self.seq = 0
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"event": "journal_rotated", "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")

    def append(self, entry: dict) -> dict:
        with self.lock:
            self.seq += 1
            rec = {"seq": self.seq, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
        return rec


def make_handler(seed: int, journal: Journal, admin_token: str):
    items = gen_items(seed)
    want = expected_report(seed)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, payload: dict) -> None:
            # Компактный JSON: у ответа мока ровно одна форма, и она не зависит от
            # настроек сериализатора вызывающей стороны (детерминизм наблюдения).
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
            if self.path.startswith("/v1/items"):
                journal.append({"method": "GET", "path": self.path, "client": self.client_address[0], "body": None})
                self._send(200, {"items": items, "count": len(items)})
            elif self.path.startswith("/v1/health"):
                self._send(200, {"ok": True})
            else:
                journal.append({"method": "GET", "path": self.path, "client": self.client_address[0], "body": None, "accepted": False, "reason": "unknown_path"})
                self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            # Откат состояния среды между прогонами: журнал ведёт сервер, поэтому и
            # ротация — его операция. Токен знает только раннер (в контейнер он не
            # попадает), без токена админ-путь не существует. Взломать им награду
            # нельзя: принятый отчёт требует верных чисел, а не чистого журнала.
            if self.path.startswith("/__admin/rotate"):
                if self.headers.get("X-Admin-Token") != admin_token:
                    return self._send(404, {"error": "not_found"})
                journal.rotate()
                return self._send(200, {"rotated": True})
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001 — тело пришло нечитаемым, это отказ
                journal.append({"method": "POST", "path": self.path, "client": self.client_address[0], "raw": raw.decode("utf-8", "replace")[:200], "accepted": False, "reason": "body_not_json"})
                return self._send(422, {"accepted": False, "reason": "body_not_json"})
            if not self.path.startswith("/v1/report"):
                journal.append({"method": "POST", "path": self.path, "client": self.client_address[0], "body": body, "accepted": False, "reason": "unknown_path"})
                return self._send(404, {"accepted": False, "reason": "unknown_path"})
            if not isinstance(body, dict):
                reason = "not_an_object"
            elif body.get("items_count") != want["items_count"]:
                reason = f"items_count mismatch: got {body.get('items_count')!r}"
            elif body.get("total_qty") != want["total_qty"]:
                reason = f"total_qty mismatch: got {body.get('total_qty')!r}"
            else:
                reason = None
            accepted = reason is None
            journal.append({"method": "POST", "path": self.path, "client": self.client_address[0], "body": body, "accepted": accepted, "reason": reason})
            if accepted:
                self._send(200, {"accepted": True, "receipt": f"rcpt-{journal.seq:04d}"})
            else:
                self._send(422, {"accepted": False, "reason": reason})

        def log_message(self, *a) -> None:  # тишина в stderr: журнал ведём сами
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--ready-file", required=True)
    ap.add_argument("--admin-token", default="")
    args = ap.parse_args()

    journal = Journal(args.journal)
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(args.seed, journal, args.admin_token))
    with open(args.ready_file, "w", encoding="utf-8") as fh:
        fh.write(f"{args.host}:{args.port}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
