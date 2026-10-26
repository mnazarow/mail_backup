"""
Поддельный HTTP-сервер для тестов драйверов почтовых серверов (Microsoft Graph, Google Admin SDK, REST
Zimbra/Kerio/Mailcow…): маршруты регистрируются функциями, каждый запрос запоминается.

    fake = FakeHttp()
    @fake.route("GET", r"/v1.0/users")
    def users(req):
        return 200, {"value": []}
    ...
    fake.close()

Обработчик получает :class:`Req` и возвращает ``(код, тело)`` или ``(код, тело, заголовки)``; тело —
dict/list (уйдёт JSON), str или bytes. Нет маршрута — 404 с JSON-ошибкой.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple


class Req:
    def __init__(self, method: str, path: str, query: Dict[str, List[str]], headers: Dict[str, str],
                 body: bytes, match: Optional[re.Match] = None) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.match = match

    def q(self, name: str, default: str = "") -> str:
        return (self.query.get(name) or [default])[0]

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def form(self) -> Dict[str, str]:
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.body.decode("utf-8"), keep_blank_values=True).items()}

    def group(self, i: int = 1) -> str:
        return urllib.parse.unquote(self.match.group(i)) if self.match else ""

    def __repr__(self) -> str:
        return f"<Req {self.method} {self.path}>"


Handler = Callable[[Req], Tuple]


class FakeHttp:
    def __init__(self) -> None:
        self.routes: List[Tuple[str, re.Pattern, Handler]] = []
        self.calls: List[Req] = []
        fake = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: D401 — тишина в выводе тестов
                pass

            def _do(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parsed = urllib.parse.urlsplit(self.path)
                req = Req(self.command, parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True),
                          {k.lower(): v for k, v in self.headers.items()}, body)
                fake.calls.append(req)
                out = fake.dispatch(req)
                status, payload = out[0], out[1]
                headers = dict(out[2]) if len(out) > 2 else {}
                if isinstance(payload, (dict, list)):
                    data = json.dumps(payload).encode("utf-8")
                    headers.setdefault("Content-Type", "application/json")
                elif isinstance(payload, str):
                    data = payload.encode("utf-8")
                    headers.setdefault("Content-Type", "text/plain; charset=utf-8")
                else:
                    data = payload or b""
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, str(v))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _do

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def route(self, method: str, pattern: str):
        def deco(fn: Handler) -> Handler:
            self.routes.append((method.upper(), re.compile("^" + pattern + "$"), fn))
            return fn
        return deco

    def dispatch(self, req: Req) -> Tuple:
        for method, rx, fn in self.routes:
            if method != req.method:
                continue
            m = rx.match(req.path)
            if m:
                req.match = m
                try:
                    return fn(req)
                except Exception as exc:  # noqa: BLE001 — ошибка теста видна в ответе
                    return 500, {"error": {"code": "fake", "message": f"{type(exc).__name__}: {exc}"}}
        return 404, {"error": {"code": "notFound", "message": f"нет маршрута {req.method} {req.path}"}}

    def calls_to(self, method: str, path_rx: str) -> List[Req]:
        rx = re.compile("^" + path_rx + "$")
        return [c for c in self.calls if c.method == method and rx.match(c.path)]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def connection(type_: str, settings: Dict[str, Any], secrets: Optional[Dict[str, str]] = None, *, server_id: int = 1,
               name: str = "Тест", allow_changes: bool = True, enabled: bool = True):
    """Объект подключения для драйвера без базы (как :class:`mailarchiver.mailservers.store.Connection`)."""
    from mailarchiver.mailservers.store import Connection
    return Connection(id=server_id, name=name, type=type_, enabled=enabled, allow_changes=allow_changes,
                      settings=dict(settings), secrets=dict(secrets or {}))
