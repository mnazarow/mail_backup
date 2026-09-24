"""Тесты источника списка сотрудников по URL и шаблона создаваемых ящиков.

HTTP-сервер поднимается локально на свободном порту: тесты не должны зависеть
от сети и от доступности чужих сервисов.
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mailarchiver.employees import (
    account_placeholders, account_template, fetch_employee_source, load_employee_source,
    parse_employee_file, preview_account_template, render_account_field, sync_employees,
)
from mailarchiver.errors import ValidationError
from mailarchiver.models import JobType, ScheduleKind

CSV = (
    "ФИО,E-mail,Должность,Отдел\n"
    "Иванов Иван Иванович,ivanov@example.ru,Менеджер,Продажи\n"
    "Петрова Анна Сергеевна,petrova@example.ru,Бухгалтер,Бухгалтерия\n"
)


class _Handler(BaseHTTPRequestHandler):
    """Мини-сервер: отдаёт выгрузку и умеет изображать типовые сбои."""

    routes: dict = {}

    def log_message(self, *args):        # тишина в выводе тестов
        pass

    def do_GET(self):                    # noqa: N802 (имя задано базовым классом)
        route = self.routes.get(self.path.split("?", 1)[0])
        if route is None:
            self.send_error(404, "Not Found")
            return
        status, headers, body, need_auth = route
        if need_auth and self.headers.get("Authorization") != need_auth:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="hr"')
            self.end_headers()
            return
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def http_source():
    """Локальный HTTP-сервер; возвращает функцию url(path)."""
    _Handler.routes = {
        "/employees.csv": (200, {"Content-Type": "text/csv; charset=utf-8"},
                           CSV.encode("utf-8"), None),
        "/export": (200, {"Content-Type": "application/octet-stream",
                          "Content-Disposition": 'attachment; filename="hr-vygruzka.csv"'},
                    CSV.encode("cp1251"), None),
        "/secure.csv": (200, {"Content-Type": "text/csv"}, CSV.encode("utf-8"),
                        "Basic bWE6c2VjcmV0"),          # ma:secret
        "/login": (200, {"Content-Type": "text/html"},
                   b"<!DOCTYPE html><html><head><title>Login</title></head></html>", None),
        "/empty.csv": (200, {"Content-Type": "text/csv"}, b"", None),
        "/boom": (500, {"Content-Type": "text/plain"}, b"oops", None),
        "/big.csv": (200, {"Content-Type": "text/csv"}, b"x" * 200_000, None),
    }
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield lambda path: f"http://{host}:{port}{path}"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
#  Загрузка по URL
# ---------------------------------------------------------------------------
def test_fetch_plain_csv(http_source):
    data, name = fetch_employee_source(http_source("/employees.csv"))
    assert name.endswith(".csv")
    rows, problems = parse_employee_file(data, name)
    assert problems == [] and len(rows) == 2


def test_fetch_uses_content_disposition_name(http_source):
    """Имя файла из заголовка важнее пути: по нему выбирается парсер."""
    _, name = fetch_employee_source(http_source("/export"))
    assert name == "hr-vygruzka.csv"


def test_fetch_basic_auth(http_source):
    with pytest.raises(ValidationError) as err:
        fetch_employee_source(http_source("/secure.csv"))
    assert "401" in str(err.value)
    data, _ = fetch_employee_source(http_source("/secure.csv"), username="ma", password="secret")
    assert b"ivanov" in data.lower()


def test_fetch_rejects_html_login_page(http_source):
    with pytest.raises(ValidationError) as err:
        fetch_employee_source(http_source("/login"))
    assert "HTML" in str(err.value)


def test_fetch_reports_server_error(http_source):
    with pytest.raises(ValidationError) as err:
        fetch_employee_source(http_source("/boom"))
    assert "500" in str(err.value)


def test_fetch_reports_missing_file(http_source):
    with pytest.raises(ValidationError) as err:
        fetch_employee_source(http_source("/nope.csv"))
    assert "404" in str(err.value)


def test_fetch_rejects_empty_answer(http_source):
    with pytest.raises(ValidationError):
        fetch_employee_source(http_source("/empty.csv"))


def test_fetch_size_limit(http_source):
    """Лишнего не качаем: по ссылке мог оказаться дамп базы, а не выгрузка."""
    with pytest.raises(ValidationError) as err:
        fetch_employee_source(http_source("/big.csv"), max_bytes=1024)
    assert "большой" in str(err.value) or "больше" in str(err.value)


def test_fetch_rejects_non_http_scheme():
    with pytest.raises(ValidationError):
        fetch_employee_source("ftp://example.ru/employees.csv")
    with pytest.raises(ValidationError):
        fetch_employee_source("/var/lib/employees.csv")


def test_fetch_unreachable_host():
    with pytest.raises(ValidationError) as err:
        fetch_employee_source("http://127.0.0.1:9/employees.csv", timeout_s=5)
    assert "подключиться" in str(err.value)


def test_load_source_url_and_file(http_source, tmp_path):
    rows, problems, origin = load_employee_source(source_type="url", url=http_source("/employees.csv"))
    assert len(rows) == 2 and problems == [] and origin.startswith("URL ")

    path = tmp_path / "hr.csv"
    path.write_text(CSV, encoding="utf-8")
    rows, problems, origin = load_employee_source(source_type="file", path=str(path))
    assert len(rows) == 2 and "hr.csv" in origin


def test_load_source_file_missing_says_where_to_look(tmp_path):
    with pytest.raises(ValidationError) as err:
        load_employee_source(source_type="file", path=str(tmp_path / "нет.csv"))
    assert "не найден" in str(err.value)


def test_sync_job_reads_url(services, http_source):
    """Сквозная проверка: настройка источника → синхронизация из задания."""
    from mailarchiver.queue.jobs import handle_sync_employees

    services.set_rt("employees", "source_type", "url")
    services.set_rt("employees", "source_url", http_source("/employees.csv"))

    class _Ctx:
        services = None
        params: dict = {}

        def event(self, *_a, **_k):
            pass

        def progress(self, *_a, **_k):
            pass

    ctx = _Ctx()
    ctx.services = services
    result = handle_sync_employees(ctx)
    assert result["created"] == 2
    assert services.db.count_employees() == 2


# ---------------------------------------------------------------------------
#  Шаблон создаваемых ящиков
# ---------------------------------------------------------------------------
def test_render_account_field_substitutes_and_cleans():
    values = account_placeholders("Иванов Иван Иванович", "ivanov@example.ru",
                                  {"position": "Менеджер", "department": "Продажи"})
    assert render_account_field("{full_name} ({department})", values) == "Иванов Иван Иванович (Продажи)"
    assert render_account_field("{local}", values) == "ivanov"
    assert render_account_field("{local}@corp.local", values) == "ivanov@corp.local"
    assert render_account_field("{last_name} {first_name}", values) == "Иванов Иван"
    # пустая подстановка не оставляет пустых скобок и висящих разделителей
    thin = account_placeholders("Петров Пётр", "p@example.ru")
    assert render_account_field("{full_name} ({department})", thin) == "Петров Пётр"
    # неизвестная подстановка остаётся видимой, а не рушит синхронизацию
    assert render_account_field("{отдел}", values) == "{отдел}"


def test_account_template_defaults(services):
    tpl = account_template(services)
    assert tpl["name_template"] == "{full_name}" and tpl["username_template"] == "{email}"
    assert tpl["enabled"] is False and tpl["retention_days"] == -1
    assert tpl["schedule_enabled"] is False


def test_template_applied_to_created_account(services):
    services.set_rt("employees", "account_host", "imap.example.ru")
    services.set_rt("employees", "account_port", 143)
    services.set_rt("employees", "account_security", "starttls")
    services.set_rt("employees", "account_name_template", "{full_name} ({department})")
    services.set_rt("employees", "account_username_template", "{local}")
    services.set_rt("employees", "account_notes_template", "Создан автоматически: {position}, {department}")
    services.set_rt("employees", "account_folder_exclude", ["Спам", "Корзина"])
    services.set_rt("employees", "account_retention_days", 365)

    rows, _ = parse_employee_file(CSV.encode("utf-8"), "e.csv")
    result = sync_employees(services, rows, create_accounts=True)
    assert result["accounts_created"] == 2

    emp = services.db.get_employee_by_email("ivanov@example.ru")
    acc = services.db.get_account(emp["account_id"])
    assert acc.name == "Иванов Иван Иванович (Продажи)"
    assert acc.username == "ivanov"           # логин по шаблону, не адрес
    assert acc.host == "imap.example.ru" and acc.port == 143 and acc.security == "starttls"
    assert acc.folder_exclude == ["Спам", "Корзина"]
    assert acc.retention_days == 365
    assert acc.notes == "Создан автоматически: Менеджер, Продажи"
    assert acc.enabled is False and not acc.password


def test_template_can_create_enabled_account_with_schedule(services):
    services.set_rt("employees", "account_host", "imap.example.ru")
    services.set_rt("employees", "account_enabled", True)
    services.set_rt("employees", "account_schedule_enabled", True)
    services.set_rt("employees", "account_schedule_cron", "30 3 * * *")

    rows, _ = parse_employee_file(CSV.encode("utf-8"), "e.csv")
    sync_employees(services, rows, create_accounts=True)
    emp = services.db.get_employee_by_email("ivanov@example.ru")
    acc = services.db.get_account(emp["account_id"])
    assert acc.enabled is True

    schedules = services.db.list_schedules(account_id=acc.id)
    assert len(schedules) == 1
    assert schedules[0]["kind"] == ScheduleKind.CRON
    assert schedules[0]["job_type"] == JobType.BACKUP
    assert schedules[0]["cron_expr"] == "30 3 * * *"


def test_second_sync_does_not_duplicate_account_after_template_change(services):
    """Смена шаблона логина не должна плодить второй ящик тому же сотруднику."""
    services.set_rt("employees", "account_host", "imap.example.ru")
    rows, _ = parse_employee_file(CSV.encode("utf-8"), "e.csv")
    sync_employees(services, rows, create_accounts=True)
    before = len(services.db.list_accounts())

    services.set_rt("employees", "account_username_template", "{local}")
    services.db.set_employee_account(services.db.get_employee_by_email("ivanov@example.ru")["id"], None)
    sync_employees(services, rows, create_accounts=True)
    assert len(services.db.list_accounts()) == before


def test_preview_account_template(services):
    services.set_rt("employees", "account_name_template", "{last_name} — {position}")
    preview = preview_account_template(services)
    assert preview["name"] == "Иванов — Менеджер"
    assert preview["username"] == "ivanov@example.ru"


class _RawHandler(BaseHTTPRequestHandler):
    """Сервер «с дефектами»: обрывает ответ, отвечает не по HTTP, шлёт на ftp://."""

    mode = "truncated"

    def log_message(self, *args):
        pass

    def do_GET(self):                    # noqa: N802
        body = CSV.encode("utf-8")
        if self.mode == "truncated":
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:-3])          # последние байты не дошли
            self.wfile.flush()
            self.close_connection = True
        elif self.mode == "ftp":
            self.send_response(302)
            self.send_header("Location", "ftp://example.com/employees.csv")
            self.end_headers()


def _raw_server(mode):
    _RawHandler.mode = mode
    server = HTTPServer(("127.0.0.1", 0), _RawHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_truncated_download_is_refused():
    server = _raw_server("truncated")
    try:
        with pytest.raises(ValidationError) as err:
            fetch_employee_source(f"http://127.0.0.1:{server.server_port}/e.csv", timeout_s=5)
        assert "не целиком" in str(err.value)
    finally:
        server.shutdown()


def test_redirect_to_other_scheme_is_refused():
    server = _raw_server("ftp")
    try:
        with pytest.raises(ValidationError):
            fetch_employee_source(f"http://127.0.0.1:{server.server_port}/e.csv", timeout_s=5)
    finally:
        server.shutdown()


def test_redact_url_hides_more_secrets():
    from mailarchiver.employees import redact_url
    out = redact_url("https://u:p@hr.example.ru/export/3f9c2a7b1e4d5f60a8b9c0d1e2f3a4b5/list.csv"
                     "?ticket=abc&sid=42&format=csv#access_token=zzz")
    assert "p@" not in out and "abc" not in out and "sid=42" not in out and "zzz" not in out
    assert "3f9c2a7b1e4d5f60a8b9c0d1e2f3a4b5" not in out and "format=csv" in out
