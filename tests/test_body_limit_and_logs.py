"""Предел размера тела запроса (до проверки входа) и журнал глазами оператора.

* Без действующей сессии сервер не читает больше 256 КБ: раньше «файл PST» от кого угодно целиком
  ложился на диск, а JSON на /api/login — в память, и только потом приходил отказ.
* Оператору в журнале не показываются записи заданий, подробности которых от него скрыты в «Очереди
  и заданиях» (выгрузки, восстановление…): раньше текст их ошибок он читал в «Журнале».
"""
import logging

import pytest
from fastapi.testclient import TestClient

from mailarchiver import roles
from mailarchiver.logging_setup import job_log_scope, memory_handler
from mailarchiver.web import proxy
from mailarchiver.web.api import MAX_PST_UPLOAD_BYTES

HDR = {"X-Requested-With": "fetch"}
PW = "СложныйПароль1"


@pytest.fixture()
def admin(client):
    assert client.post("/api/setup", json={"username": "admin", "password": PW}).status_code == 200
    assert client.post("/api/login", json={"username": "admin", "password": PW}).status_code == 200
    return client


def _anonymous(client):
    return TestClient(client.app, headers=HDR)


def _chunks(total, piece=64 * 1024):
    """Тело без Content-Length."""
    for _ in range(total // piece):
        yield b"x" * piece


def test_stream_is_cut_right_after_the_limit():
    """Без Content-Length чтение обрывается сразу за пределом, а не после всего тела."""
    import asyncio
    read = {"n": 0}

    async def app(scope, receive, send):
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        read["n"] += 1
        return {"type": "http.request", "body": b"x" * 40, "more_body": read["n"] < 1000}

    sent = []

    async def send(message):
        sent.append(message)

    limited = proxy.BodyLimitMiddleware(app, anonymous=100)
    asyncio.run(limited({"type": "http", "method": "POST", "path": "/x", "headers": []}, receive, send))
    assert read["n"] == 3 and sent[0]["status"] == 413 and len(sent) == 2


def test_anonymous_big_body_is_refused_before_it_is_read(admin):
    anon = _anonymous(admin)
    big = proxy.BODY_LIMIT_ANONYMOUS + 1
    r = anon.post("/api/login", content=b"{" + b" " * big + b"}", headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    assert r.headers["x-content-type-options"] == "nosniff"        # отказ идёт через защитные заголовки
    # без Content-Length (поток): отказ тот же
    r = anon.post("/api/accounts/1/import-pst", content=_chunks(8 * 1024 * 1024),
                  headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    # обычный вход по-прежнему работает
    assert anon.post("/api/login", json={"username": "admin", "password": PW}).status_code == 200


def test_signed_in_limits(admin):
    aid = admin.post("/api/accounts", json={"name": "И", "host": "h", "port": 993, "username": "i@x.ru",
                                            "password": "p"}).json()["id"]
    # после входа — до 32 МБ: файл в мегабайт проходит до обработчика (тот отвечает по существу)
    r = admin.post(f"/api/accounts/{aid}/import-pst",
                   files={"file": ("a.pst", b"0" * (1024 * 1024), "application/octet-stream")})
    assert r.status_code != 413
    r = admin.post("/api/employees/import", content=b"x" * (proxy.BODY_LIMIT_SIGNED_IN + 1),
                   headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413


def test_big_path_limit_needs_a_session():
    seen = {}

    async def app(scope, receive, send):
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body") or b"")
            if not message.get("more_body"):
                break
        seen["total"] = total
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    users = {"u": None}
    limited = proxy.BodyLimitMiddleware(app, user_getter=lambda scope: users["u"], anonymous=10, signed_in=100,
                                        big_paths=[("/import-pst", 1000)])
    c = TestClient(limited)
    assert c.post("/x", content=b"1" * 10).status_code == 200
    assert c.post("/x", content=b"1" * 11).status_code == 413
    assert c.post("/a/import-pst", content=b"1" * 500).status_code == 413          # без входа — как все
    users["u"] = {"role": "admin"}
    assert c.post("/x", content=b"1" * 100).status_code == 200
    assert c.post("/x", content=b"1" * 101).status_code == 413
    assert c.post("/a/import-pst", content=b"1" * 1000).status_code == 200 and seen["total"] == 1000
    assert c.post("/a/import-pst", content=b"1" * 1001).status_code == 413
    assert c.get("/x").status_code == 200                                          # чтение не трогаем
    assert MAX_PST_UPLOAD_BYTES > proxy.BODY_LIMIT_SIGNED_IN


def test_operator_does_not_see_log_lines_of_hidden_jobs(admin):
    assert admin.post("/api/users", json={"username": "op", "password": PW, "role": "operator"}).status_code == 200
    op = TestClient(admin.app, headers=HDR)
    assert op.post("/api/login", json={"username": "op", "password": PW}).status_code == 200
    log = logging.getLogger("mailarchiver.queue")
    with job_log_scope("export"):
        log.error("Задание #7 провалено: /var/lib/x/Договор с ООО Ромашка.eml")
    with job_log_scope("backup"):
        log.error("Задание #8 провалено: сервер недоступен")
    log.warning("вне заданий")

    def texts(c, path="/api/logs?limit=50"):
        data = c.get(path).json()
        return " | ".join(r["message"] for r in (data["logs"] if isinstance(data, dict) else data))

    assert "Ромашка" in texts(admin) and "Ромашка" in texts(admin, "/api/live")
    for path in ("/api/logs?limit=50", "/api/logs?limit=50&level=error", "/api/live"):
        seen = texts(op, path)
        assert "Ромашка" not in seen and "сервер недоступен" in seen
    assert "вне заданий" in texts(op)
    assert "export" not in roles.OPERATOR_JOB_TYPES and "backup" in roles.OPERATOR_JOB_TYPES
    assert memory_handler.tail(limit=5)[-1]["job_type"] is None
