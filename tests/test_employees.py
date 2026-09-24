"""Тесты раздела «Сотрудники»: разбор файла, синхронизация и REST API."""
import io

import pytest

from mailarchiver.employees import parse_employee_file, sync_employees
from mailarchiver.errors import ValidationError
from mailarchiver.models import Account

CSV_COMMA = (
    "ФИО,E-mail,Должность,Отдел,Телефон,Табельный номер\n"
    "Иванов Иван Иванович,Ivanov@Example.RU,Менеджер,Продажи,+7 900 000-00-00,1024\n"
    "Петрова Анна Сергеевна,petrova@example.ru,Бухгалтер,Бухгалтерия,,1025\n"
)

CSV_SEMICOLON = (
    "Табельный номер;Ф.И.О.;Почта;Должность;Отдел;Тел\n"
    "2048;Сидоров Пётр Петрович;sidorov@example.ru;Инженер;ИТ;+7 901 111-11-11\n"
)


# ---------------------------------------------------------------------------
#  Разбор файла
# ---------------------------------------------------------------------------
def test_parse_csv_utf8_comma():
    rows, problems = parse_employee_file(CSV_COMMA.encode("utf-8"), "employees.csv")
    assert problems == []
    assert len(rows) == 2
    first = rows[0]
    assert first["full_name"] == "Иванов Иван Иванович"
    assert first["email"] == "ivanov@example.ru"      # приведён к нижнему регистру
    assert first["position"] == "Менеджер"
    assert first["department"] == "Продажи"
    assert first["external_id"] == "1024"
    assert first["row"] == 2                          # нумерация как в Excel


def test_parse_csv_utf8_bom():
    """Выгрузка из Excel начинается с BOM — он не должен попасть в заголовок."""
    rows, problems = parse_employee_file(CSV_COMMA.encode("utf-8-sig"), "employees.csv")
    assert problems == [] and len(rows) == 2
    assert rows[0]["full_name"] == "Иванов Иван Иванович"


def test_parse_csv_cp1251_semicolon():
    rows, problems = parse_employee_file(CSV_SEMICOLON.encode("cp1251"), "sotrudniki.csv")
    assert problems == []
    assert len(rows) == 1
    assert rows[0]["full_name"] == "Сидоров Пётр Петрович"
    assert rows[0]["email"] == "sidorov@example.ru"
    assert rows[0]["phone"] == "+7 901 111-11-11"
    assert rows[0]["external_id"] == "2048"


def test_parse_skips_empty_and_reports_bad_rows():
    data = (
        "ФИО;E-mail;Должность\n"
        "Иванов Иван;ivanov@example.ru;Менеджер\n"
        "\n"                                  # пустая строка — просто пропускается
        ";;Курьер\n"                          # нет ФИО и почты — в problems
        "Петров Пётр;не-адрес;Слесарь\n"      # почта не похожа на адрес
    )
    rows, problems = parse_employee_file(data.encode("utf-8"), "e.csv")
    assert [r["full_name"] for r in rows] == ["Иванов Иван", "Петров Пётр"]
    assert {p["row"] for p in problems} == {4, 5}
    reasons = {p["row"]: p["reason"] for p in problems}
    assert reasons[4] == "нет ФИО и e-mail"
    assert "e-mail" in reasons[5]
    # у сотрудника с «битой» почтой карточка создастся, но без адреса
    assert rows[1]["email"] == ""


def test_parse_file_without_known_columns():
    from mailarchiver.errors import ValidationError
    with pytest.raises(ValidationError):
        parse_employee_file("Колонка1;Колонка2\nа;б\n".encode("utf-8"), "e.csv")


def test_parse_xlsx():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Табельный", "ФИО", "Email", "Должность"])
    ws.append([1024, "Иванов Иван Иванович", "ivanov@example.ru", "Менеджер"])
    buf = io.BytesIO()
    wb.save(buf)
    rows, problems = parse_employee_file(buf.getvalue(), "Сотрудники.xlsx")
    assert problems == [] and len(rows) == 1
    # число из Excel не должно превратиться в «1024.0»
    assert rows[0]["external_id"] == "1024"
    assert rows[0]["full_name"] == "Иванов Иван Иванович"


# ---------------------------------------------------------------------------
#  Синхронизация
# ---------------------------------------------------------------------------
def test_sync_creates_then_updates_without_duplicates(services):
    rows, _ = parse_employee_file(CSV_COMMA.encode("utf-8"), "e.csv")
    first = sync_employees(services, rows, create_accounts=False)
    assert first["created"] == 2 and first["updated"] == 0
    assert first["total_rows"] == 2 and first["problems"] == []
    assert services.db.count_employees() == 2

    # повторный прогон того же файла: обновление, а не дубли
    changed = CSV_COMMA.replace("Менеджер", "Старший менеджер")
    rows2, _ = parse_employee_file(changed.encode("utf-8"), "e.csv")
    second = sync_employees(services, rows2, create_accounts=False)
    assert second["created"] == 0 and second["updated"] == 2
    assert services.db.count_employees() == 2
    emp = services.db.get_employee_by_email("ivanov@example.ru")
    assert emp["position"] == "Старший менеджер"
    assert emp["last_seen_at"] and emp["source"] == "file"


def test_sync_does_not_wipe_fields_with_empty_values(services):
    rows, _ = parse_employee_file(CSV_COMMA.encode("utf-8"), "e.csv")
    sync_employees(services, rows, create_accounts=False)
    # в новой выгрузке телефон и отдел не заполнены
    thin = "ФИО,E-mail,Телефон,Отдел\nИванов Иван Иванович,ivanov@example.ru,,\n"
    rows2, _ = parse_employee_file(thin.encode("utf-8"), "e.csv")
    sync_employees(services, rows2, create_accounts=False)
    emp = services.db.get_employee_by_email("ivanov@example.ru")
    assert emp["phone"] == "+7 900 000-00-00"
    assert emp["department"] == "Продажи"


def test_sync_matches_by_external_id(services):
    rows, _ = parse_employee_file(CSV_COMMA.encode("utf-8"), "e.csv")
    sync_employees(services, rows, create_accounts=False)
    # у сотрудника сменилась почта, табельный номер прежний → та же карточка
    renamed = CSV_COMMA.replace("Ivanov@Example.RU", "i.ivanov@example.ru")
    rows2, _ = parse_employee_file(renamed.encode("utf-8"), "e.csv")
    result = sync_employees(services, rows2, create_accounts=False)
    assert result["created"] == 0 and result["updated"] == 2
    assert services.db.count_employees() == 2
    assert services.db.get_employee_by_external_id("1024")["email"] == "i.ivanov@example.ru"


def test_sync_creates_disabled_accounts(services):
    services.set_rt("employees", "account_host", "imap.example.ru")
    rows, _ = parse_employee_file(CSV_COMMA.encode("utf-8"), "e.csv")
    result = sync_employees(services, rows, create_accounts=True)
    assert result["accounts_created"] == 2 and result["accounts_linked"] == 0

    emp = services.db.get_employee_by_email("ivanov@example.ru")
    acc = services.db.get_account(emp["account_id"])
    assert acc is not None
    assert acc.enabled is False           # ГЛАВНОЕ: ящик выключен
    assert acc.password == ""             # и без пароля
    assert acc.username == "ivanov@example.ru"
    assert acc.host == "imap.example.ru" and acc.port == 993 and acc.security == "ssl"
    assert acc.name == "Иванов Иван Иванович"
    # повторная синхронизация не плодит ящики
    again = sync_employees(services, rows, create_accounts=True)
    assert again["accounts_created"] == 0 and again["accounts_linked"] == 0
    assert len(services.db.list_accounts()) == 2


def test_sync_links_existing_account(services):
    """Если ящик с таким логином уже заведён — привязываем его, а не создаём новый."""
    services.set_rt("employees", "account_host", "imap.example.ru")
    account_id = services.db.create_account(
        Account(name="Почта Иванова", host="imap.example.ru", username="ivanov@example.ru", enabled=True))
    rows, _ = parse_employee_file(CSV_COMMA.encode("utf-8"), "e.csv")
    result = sync_employees(services, rows, create_accounts=True)
    assert result["accounts_linked"] == 1 and result["accounts_created"] == 1
    assert services.db.get_employee_by_email("ivanov@example.ru")["account_id"] == account_id
    # существующий ящик не трогаем: он как был включён, так и остался
    assert services.db.get_account(account_id).enabled is True


def test_sync_keeps_employees_missing_from_file(services):
    """Уволенных не трогаем: карточка и ящик остаются как есть."""
    services.set_rt("employees", "account_host", "imap.example.ru")
    rows, _ = parse_employee_file(CSV_COMMA.encode("utf-8"), "e.csv")
    sync_employees(services, rows, create_accounts=True)
    gone = services.db.get_employee_by_email("petrova@example.ru")
    services.db.set_account_enabled(gone["account_id"], True)   # ящик включили руками

    # новая выгрузка: Петровой в ней больше нет
    only_first = "\n".join(CSV_COMMA.splitlines()[:2]) + "\n"
    rows2, _ = parse_employee_file(only_first.encode("utf-8"), "e.csv")
    result = sync_employees(services, rows2, create_accounts=True)
    assert result["total_rows"] == 1 and result["created"] == 0

    still = services.db.get_employee(gone["id"])
    assert still is not None
    assert still["status"] == "active"                 # не архивируем
    assert still["account_id"] == gone["account_id"]   # ящик не отвязали
    assert services.db.get_account(gone["account_id"]).enabled is True   # и не выключили
    assert services.db.count_employees() == 2


def test_sync_reports_problem_rows(services):
    result = sync_employees(services, [{"row": 7, "full_name": "", "email": ""}], create_accounts=False)
    assert result["created"] == 0
    assert result["problems"] == [{"row": 7, "reason": "нет ФИО и e-mail"}]


# ---------------------------------------------------------------------------
#  REST API
# ---------------------------------------------------------------------------
def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})


def test_employees_require_auth(client):
    assert client.get("/api/employees").status_code == 401
    assert client.post("/api/employees", json={"full_name": "Иванов"}).status_code == 401
    assert client.put("/api/employees/1", json={"full_name": "Иванов"}).status_code == 401
    assert client.delete("/api/employees/1").status_code == 401
    assert client.post("/api/employees/1/link-account?account_id=1").status_code == 401
    assert client.post("/api/employees/sync").status_code == 401
    assert client.get("/api/employees/template.csv").status_code == 401
    assert client.post("/api/employees/import", files={"file": ("e.csv", b"x", "text/csv")}).status_code == 401


def test_employees_crud_api(client):
    _login(client)
    # без сервера в шаблоне ящик не заводится — задаём его
    client.put("/api/settings", json={"values": {"employees.account_host": "imap.example.ru"}})
    empty = client.get("/api/employees").json()
    assert empty["employees"] == [] and empty["total"] == 0
    assert empty["counts"] == {"active": 0, "archived": 0, "with_account": 0, "without_account": 0}

    r = client.post("/api/employees", json={
        "full_name": "Иванов Иван Иванович", "email": "IVANOV@example.ru", "position": "Менеджер",
        "department": "Продажи", "phone": "+7 900 000-00-00", "external_id": "1024",
        "status": "active", "notes": "испытательный срок", "create_account": True,
    })
    assert r.status_code == 200
    created = r.json()
    assert created["ok"] is True and created["account_id"]
    employee_id = created["id"]

    # ящик заведён выключенным
    acc = client.get(f"/api/accounts/{created['account_id']}").json()
    assert acc["enabled"] is False and acc["username"] == "ivanov@example.ru"

    data = client.get("/api/employees").json()
    assert data["total"] == 1
    item = data["employees"][0]
    assert item["full_name"] == "Иванов Иван Иванович"
    assert item["email"] == "ivanov@example.ru"           # нормализован
    assert item["account_id"] == created["account_id"]
    assert item["account_enabled"] is False
    assert item["account_name"] == "Иванов Иван Иванович"
    assert item["source"] == "manual" and item["status"] == "active"
    assert data["counts"] == {"active": 1, "archived": 0, "with_account": 1, "without_account": 0}

    # изменение
    assert client.put(f"/api/employees/{employee_id}", json={
        "full_name": "Иванов Иван Иванович", "email": "ivanov@example.ru", "position": "Руководитель отдела",
        "department": "Продажи", "phone": "", "external_id": "1024", "status": "archived", "notes": "",
    }).status_code == 200
    item = client.get("/api/employees").json()["employees"][0]
    assert item["position"] == "Руководитель отдела" and item["status"] == "archived"

    # поиск и фильтр по статусу
    assert client.get("/api/employees?query=иванов").json()["total"] == 1
    assert client.get("/api/employees?query=сидоров").json()["total"] == 0
    assert client.get("/api/employees?status=active").json()["total"] == 0
    assert client.get("/api/employees?status=archived").json()["total"] == 1

    # отвязать и привязать ящик обратно
    assert client.post(f"/api/employees/{employee_id}/link-account?account_id=0").status_code == 200
    assert client.get("/api/employees").json()["employees"][0]["account_id"] is None
    assert client.post(
        f"/api/employees/{employee_id}/link-account?account_id={created['account_id']}").status_code == 200
    assert client.get("/api/employees").json()["employees"][0]["account_id"] == created["account_id"]

    # удаление сотрудника НЕ удаляет почтовый ящик
    assert client.delete(f"/api/employees/{employee_id}").status_code == 200
    assert client.get("/api/employees").json()["total"] == 0
    assert client.get(f"/api/accounts/{created['account_id']}").status_code == 200


def test_employee_api_validation(client):
    _login(client)
    assert client.post("/api/employees", json={"full_name": "  "}).status_code == 400
    assert client.post("/api/employees", json={"full_name": "Иванов", "email": "не-адрес"}).status_code == 400
    assert client.post("/api/employees", json={"full_name": "Иванов", "status": "уволен"}).status_code == 400
    # ящик без почты завести нельзя
    assert client.post("/api/employees", json={"full_name": "Иванов", "create_account": True}).status_code == 400
    assert client.put("/api/employees/999", json={"full_name": "Нет такого"}).status_code == 404
    assert client.delete("/api/employees/999").status_code == 404
    assert client.post("/api/employees/999/link-account?account_id=0").status_code == 404


def test_employees_template_csv(client):
    _login(client)
    r = client.get("/api/employees/template.csv")
    assert r.status_code == 200
    assert "sotrudniki-obrazec.csv" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    assert "ФИО" in text and "ivanov@example.ru" in text
    # образец должен без вопросов разбираться нашим же парсером
    rows, problems = parse_employee_file(r.content, "sotrudniki-obrazec.csv")
    assert problems == [] and len(rows) == 1


def test_employees_import_api(client):
    _login(client)
    client.put("/api/settings", json={"values": {"employees.account_host": "imap.example.ru"}})
    payload = CSV_COMMA + "Без Имени,,,,\n" + ",,Курьер,Склад,\n"
    r = client.post("/api/employees/import",
                    files={"file": ("Сотрудники.csv", payload.encode("utf-8"), "text/csv")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["created"] == 3 and body["updated"] == 0
    assert body["accounts_created"] == 2 and body["accounts_linked"] == 0
    assert body["total_rows"] == 3
    assert body["problems"] == [{"row": 5, "reason": "нет ФИО и e-mail"}]

    listed = client.get("/api/employees").json()
    assert listed["total"] == 3
    assert listed["counts"] == {"active": 3, "archived": 0, "with_account": 2, "without_account": 1}
    assert all(e["source"] == "file" for e in listed["employees"])

    # повторный импорт того же файла — только обновление
    again = client.post("/api/employees/import",
                        files={"file": ("Сотрудники.csv", payload.encode("utf-8"), "text/csv")}).json()
    assert again["created"] == 0 and again["updated"] == 3
    assert client.get("/api/employees").json()["total"] == 3


def test_employees_settings_exposed(client):
    """Секция «Сотрудники» должна приезжать в настройки вместе с подсказками."""
    _login(client)
    settings = client.get("/api/settings").json()
    assert settings["values"]["employees"]["cron"] == "0 5 * * *"
    assert settings["values"]["employees"]["create_accounts"] is True
    assert settings["values"]["employees"]["account_port"] == 993
    section = [s for s in settings["sections"] if s["section"] == "employees"]
    assert section and section[0]["title"] == "Сотрудники" and section[0]["icon"]
    for key in section[0]["keys"]:
        help_item = settings["help"]["params"][f"employees.{key}"]
        assert help_item["title"] and help_item["help"] and help_item["recommend"]
        assert help_item["example"] is not None and help_item["default"] is not None
    # кривое расписание не сохраняем молча
    assert client.put("/api/settings", json={"values": {"employees.cron": "каждый день"}}).status_code == 400
    assert client.put("/api/settings", json={"values": {"employees.cron": "30 4 * * 1"}}).status_code == 200


def test_employees_sync_requires_source_file(client, tmp_path):
    _login(client)
    # файл не задан → понятная ошибка, а не 500
    r = client.post("/api/employees/sync")
    assert r.status_code == 400 and r.json()["error"] is True and r.json()["hint"]

    # задан несуществующий путь
    client.put("/api/settings", json={"values": {"employees.source_file": str(tmp_path / "нет.csv")}})
    assert client.post("/api/employees/sync").status_code == 400

    # реальный файл → задание встаёт в очередь
    src = tmp_path / "employees.csv"
    src.write_bytes(CSV_COMMA.encode("utf-8"))
    client.put("/api/settings", json={"values": {"employees.source_file": str(src)}})
    r = client.post("/api/employees/sync")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["job_id"]


def test_sync_employees_job(services, tmp_path):
    """Обработчик фонового задания читает файл из настроек."""
    from mailarchiver.queue.jobs import HANDLERS, JobContext
    from mailarchiver.models import JobStatus, JobType

    src = tmp_path / "employees.csv"
    src.write_bytes(CSV_SEMICOLON.encode("cp1251"))
    services.set_rt("employees", "source_file", str(src))
    services.set_rt("employees", "account_host", "imap.example.ru")

    job_id = services.db.enqueue_job(JobType.SYNC_EMPLOYEES, None, {})
    ctx = JobContext(services, job_id, JobType.SYNC_EMPLOYEES, None, {})
    result = HANDLERS[JobType.SYNC_EMPLOYEES](ctx)
    assert result["final_status"] == JobStatus.SUCCESS
    assert result["created"] == 1 and result["total_rows"] == 1
    assert "Сотрудников добавлено 1" in result["summary"]
    emp = services.db.get_employee_by_email("sidorov@example.ru")
    assert emp is not None and services.db.get_account(emp["account_id"]).enabled is False
    assert services.db.list_job_events(job_id)


def test_scheduler_registers_employees_sync(services):
    """reload() должен пересоздавать задание синхронизации сотрудников."""
    services.scheduler.start()
    try:
        job_id = services.scheduler.EMPLOYEES_JOB_ID
        assert services.scheduler._sched.get_job(job_id) is None   # по умолчанию выключено
        services.set_rt("employees", "sync_enabled", True)
        services.set_rt("employees", "cron", "30 4 * * *")
        services.scheduler.reload()
        job = services.scheduler._sched.get_job(job_id)
        assert job is not None
        assert "hour='4'" in str(job.trigger) and "minute='30'" in str(job.trigger)
        services.set_rt("employees", "sync_enabled", False)
        services.scheduler.reload()
        assert services.scheduler._sched.get_job(job_id) is None
    finally:
        services.scheduler.stop()


def test_sync_without_host_links_but_does_not_create(services):
    """Сервер в шаблоне не задан: существующие ящики привязываются, новые не заводятся."""
    account_id = services.db.create_account(
        Account(name="Почта Иванова", host="imap.example.ru", username="ivanov@example.ru", enabled=True))
    rows, _ = parse_employee_file(CSV_COMMA.encode("utf-8"), "e.csv")
    result = sync_employees(services, rows, create_accounts=True)
    assert result["accounts_linked"] == 1 and result["accounts_created"] == 0
    assert result["warnings"] and "IMAP-сервер" in result["warnings"][0]
    assert services.db.get_employee_by_email("ivanov@example.ru")["account_id"] == account_id


def test_deleted_account_is_not_recreated(services):
    """Ящик, удалённый администратором, ночная синхронизация не пересоздаёт."""
    services.set_rt("employees", "account_host", "imap.example.ru")
    rows, _ = parse_employee_file(CSV_COMMA.encode("utf-8"), "e.csv")
    first = sync_employees(services, rows, create_accounts=True)
    assert first["accounts_created"] == 2
    emp = services.db.get_employee_by_email("petrova@example.ru")
    services.db.delete_account(emp["account_id"])
    again = sync_employees(services, rows, create_accounts=True)
    assert again["accounts_created"] == 0
    assert services.db.get_employee_by_email("petrova@example.ru")["account_id"] is None


def test_parallel_sync_is_refused_for_upload(services):
    from mailarchiver import employees as emp_mod
    rows, _ = parse_employee_file(CSV_COMMA.encode("utf-8"), "e.csv")
    with emp_mod._SYNC_LOCK:
        with pytest.raises(ValidationError):
            sync_employees(services, rows, create_accounts=False, wait=False)


def test_email_column_found_by_content_and_free_header():
    """«Электронная почта» и колонка адресов без узнаваемого заголовка распознаются,
    а «Адрес» (почтовый) не перекрывает настоящую почту."""
    data = ("ФИО;Адрес;Электронная почта сотрудника\n"
            "Иванов Иван;г. Челябинск, ул. Ленина 1;ivanov@example.ru\n"
            "Петрова Анна;г. Москва;petrova@example.ru\n").encode("utf-8")
    rows, _ = parse_employee_file(data, "e.csv")
    assert [r["email"] for r in rows] == ["ivanov@example.ru", "petrova@example.ru"]
    data = ("ФИО;Контакт\nИванов Иван;ivanov@example.ru\nПетрова Анна;petrova@example.ru\n").encode("utf-8")
    rows, _ = parse_employee_file(data, "e.csv")
    assert rows[0]["email"] == "ivanov@example.ru"


def test_whole_column_is_checked_for_dismissed():
    """«нет» только в конце длинного списка — колонка «работает» всё равно распознаётся."""
    lines = ["ФИО;E-mail;"] + [f"Сотрудник {i};u{i}@example.ru;да" for i in range(100)] \
        + ["Уволенный;gone@example.ru;нет"]
    rows, _ = parse_employee_file("\n".join(lines).encode("utf-8"), "e.csv")
    assert [r["email"] for r in rows if r["inactive"]] == ["gone@example.ru"]


def test_email_is_filled_into_card_found_by_name(services):
    """Карточка без почты (колонку раньше не распознали) дозаполняется, а не дублируется."""
    first = ("ФИО;Должность\nИванов Иван;Менеджер\n").encode("utf-8")
    rows, _ = parse_employee_file(first, "e.csv")
    sync_employees(services, rows, create_accounts=False)
    second = ("ФИО;E-mail\nИванов Иван;ivanov@example.ru\n").encode("utf-8")
    rows, _ = parse_employee_file(second, "e.csv")
    result = sync_employees(services, rows, create_accounts=False)
    assert result["created"] == 0 and services.db.count_employees() == 1
    assert services.db.get_employee_by_email("ivanov@example.ru") is not None


def test_header_row_below_report_title():
    data = ("Список сотрудников на 01.09.2026;;\nФИО;E-mail;Отдел\nИванов Иван;ivanov@example.ru;Продажи\n"
            ).encode("utf-8")
    rows, _ = parse_employee_file(data, "e.csv")
    assert rows[0]["full_name"] == "Иванов Иван" and rows[0]["department"] == "Продажи"
