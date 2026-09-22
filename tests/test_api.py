"""Тесты REST API через FastAPI TestClient (без реального IMAP)."""


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_index_page_renders(client):
    """Корневая страница должна отдавать HTML (регрессия на сигнатуру Starlette
    TemplateResponse: на новых версиях требуется request первым аргументом)."""
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "root" in r.text  # <div id="root"> из шаблона index.html


def test_needs_setup_flow(client):
    assert client.get("/api/needs-setup").json()["needs_setup"] is True
    # доступ к защищённому endpoint без входа
    assert client.get("/api/state").status_code == 401
    # создать администратора
    r = client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    assert r.status_code == 200
    assert client.get("/api/needs-setup").json()["needs_setup"] is False
    # логин
    r = client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})
    assert r.status_code == 200
    # теперь state доступен
    st = client.get("/api/state")
    assert st.status_code == 200
    assert "accounts" in st.json() and "engines" in st.json()


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})


def test_accounts_crud(client):
    _login(client)
    r = client.post("/api/accounts", json={
        "name": "Ящик", "host": "imap.example.com", "port": 993,
        "username": "u@example.com", "password": "secret", "security": "ssl", "auth_type": "password",
    })
    assert r.status_code == 200
    aid = r.json()["id"]
    got = client.get(f"/api/accounts/{aid}").json()
    assert got["name"] == "Ящик" and got["has_password"] is True
    # обновление без пароля не должно его стирать
    client.put(f"/api/accounts/{aid}", json={
        "name": "Ящик-2", "host": "imap.example.com", "port": 993,
        "username": "u@example.com", "password": "", "security": "ssl", "auth_type": "password",
    })
    assert client.get(f"/api/accounts/{aid}").json()["name"] == "Ящик-2"
    assert client.get(f"/api/accounts/{aid}").json()["has_password"] is True
    # список
    assert len(client.get("/api/accounts").json()) == 1
    # удаление
    assert client.delete(f"/api/accounts/{aid}").status_code == 200
    assert len(client.get("/api/accounts").json()) == 0


def test_settings_and_help(client):
    _login(client)
    s = client.get("/api/settings").json()
    assert "values" in s and "sections" in s and "help" in s
    # изменить настройку
    r = client.put("/api/settings", json={"values": {"backup.fetch_batch_size": 123}})
    assert r.status_code == 200
    s2 = client.get("/api/settings").json()
    assert s2["values"]["backup"]["fetch_batch_size"] == 123
    # справка
    hp = client.get("/api/help").json()
    assert "server.port" in hp["params"]


def test_export_engines_and_jobs(client):
    _login(client)
    engines = client.get("/api/export/engines").json()
    assert any(e["name"] == "native" for e in engines)
    assert client.get("/api/jobs").json() == []


def test_validation_error(client):
    _login(client)
    # пустое имя ящика → доменная ошибка 400 с понятным сообщением
    r = client.post("/api/accounts", json={
        "name": "", "host": "", "port": 993, "username": "", "password": "",
        "security": "ssl", "auth_type": "password",
    })
    assert r.status_code == 400
    assert r.json()["error"] is True and "message" in r.json()


# ---------------------------------------------------------------------------
#  Источник списка сотрудников и шаблон создаваемых ящиков
# ---------------------------------------------------------------------------
def test_employee_source_defaults_to_file(client):
    _login(client)
    src = client.get("/api/employees/source").json()["source"]
    assert src["type"] == "file" and src["configured"] is False
    # без настроенного источника синхронизация не запускается, а объясняет почему
    r = client.post("/api/employees/sync")
    assert r.status_code >= 400
    assert "файл" in r.json().get("message", "").lower()


def test_employee_source_url_requires_address(client):
    _login(client)
    assert client.put("/api/settings", json={"values": {"employees.source_type": "url"}}).status_code == 200
    src = client.get("/api/employees/source").json()["source"]
    assert src["type"] == "url" and src["configured"] is False
    r = client.post("/api/employees/sync")
    assert r.status_code >= 400
    assert "адрес" in r.json().get("message", "").lower()


def test_employee_source_url_password_is_secret(client):
    """Пароль к источнику не должен уезжать обратно в браузер."""
    _login(client)
    client.put("/api/settings", json={"values": {
        "employees.source_type": "url",
        "employees.source_url": "https://hr.example.ru/e.csv",
        "employees.source_url_user": "ma",
        "employees.source_url_password": "s3cret",
    }})
    data = client.get("/api/settings").json()
    assert data["values"]["employees"]["source_url_password"] == ""
    assert data["secrets_set"]["employees.source_url_password"] is True
    # пустое значение при сохранении не затирает сохранённый пароль
    client.put("/api/settings", json={"values": {"employees.source_url_password": ""}})
    assert client.get("/api/settings").json()["secrets_set"]["employees.source_url_password"] is True
    # и наружу он не просачивается через сведения об источнике
    assert "s3cret" not in client.get("/api/employees/source").text


def test_employee_account_template_preview(client):
    _login(client)
    client.put("/api/settings", json={"values": {
        "employees.account_host": "imap.example.ru",
        "employees.account_name_template": "{full_name} ({department})",
        "employees.account_username_template": "{local}",
    }})
    data = client.get("/api/employees/account-template").json()
    assert data["preview"]["name"] == "Иванов Иван Иванович (Отдел продаж)"
    assert data["preview"]["username"] == "ivanov"
    assert data["preview"]["host"] == "imap.example.ru"
    assert "full_name" in data["placeholders"]


def test_employee_account_schedule_cron_is_validated(client):
    _login(client)
    bad = client.put("/api/settings", json={"values": {"employees.account_schedule_cron": "каждый день"}})
    assert bad.status_code >= 400
    ok = client.put("/api/settings", json={"values": {"employees.account_schedule_cron": "30 3 * * *"}})
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
#  Исключение папок, которые почтовый сервер не даёт прочитать
# ---------------------------------------------------------------------------
def _make_account(client, name="Ящик"):
    r = client.post("/api/accounts", json={
        "name": name, "host": "mx.example.ru", "port": 993, "username": "u@example.ru",
        "password": "pw", "security": "ssl", "enabled": True,
    })
    assert r.status_code == 200
    return r.json()["id"]


def test_exclude_folders_adds_to_account(client):
    _login(client)
    acc_id = _make_account(client)
    r = client.post(f"/api/accounts/{acc_id}/exclude-folders",
                    json={"folders": ["Отправленные/s2022", "Отправленные/s2022_000"]})
    assert r.status_code == 200
    assert r.json()["added"] == ["Отправленные/s2022", "Отправленные/s2022_000"]

    acc = [a for a in client.get("/api/accounts").json() if a["id"] == acc_id][0]
    assert acc["folder_exclude"] == ["Отправленные/s2022", "Отправленные/s2022_000"]

    # повторный вызов ничего не дублирует
    again = client.post(f"/api/accounts/{acc_id}/exclude-folders",
                        json={"folders": ["Отправленные/s2022"]})
    assert again.json()["added"] == []
    acc = [a for a in client.get("/api/accounts").json() if a["id"] == acc_id][0]
    assert acc["folder_exclude"] == ["Отправленные/s2022", "Отправленные/s2022_000"]


def test_exclude_folders_keeps_password(client):
    """Исключение папок не должно затирать пароль ящика."""
    _login(client)
    acc_id = _make_account(client)
    client.post(f"/api/accounts/{acc_id}/exclude-folders", json={"folders": ["Спам"]})
    acc = [a for a in client.get("/api/accounts").json() if a["id"] == acc_id][0]
    assert acc["has_password"] is True


def test_exclude_folders_validates_input(client):
    _login(client)
    acc_id = _make_account(client)
    assert client.post(f"/api/accounts/{acc_id}/exclude-folders", json={"folders": []}).status_code >= 400
    assert client.post("/api/accounts/9999/exclude-folders", json={"folders": ["X"]}).status_code == 404


def test_folder_problem_history_is_stored(client):
    """История «папка не открывается» живёт в БД и сбрасывается при успехе."""
    _login(client)
    acc_id = _make_account(client, "История папок")
    svc = client.app.state.services
    assert svc.db.record_folder_problem(acc_id, "Отправленные/s2022", "failed EXAMINE") == 1
    assert svc.db.record_folder_problem(acc_id, "Отправленные/s2022", "failed EXAMINE") == 2
    row = svc.db.get_folder_problem(acc_id, "Отправленные/s2022")
    assert row["fails"] == 2 and row["first_failed"] and "EXAMINE" in row["last_error"]
    assert [r["folder"] for r in svc.db.list_folder_problems(acc_id)] == ["Отправленные/s2022"]
    svc.db.clear_folder_problem(acc_id, "Отправленные/s2022")
    assert svc.db.list_folder_problems(acc_id) == []


# ---------------------------------------------------------------------------
#  Копирование заново («докачать потерянные» и «с нуля»)
# ---------------------------------------------------------------------------
def test_backup_rebuild_modes_are_validated(client):
    _login(client)
    acc_id = _make_account(client, "Пересоздание")
    assert client.post(f"/api/accounts/{acc_id}/backup", json={}).status_code == 200
    assert client.post(f"/api/accounts/{acc_id}/backup", json={"rebuild": "missing"}).status_code == 200
    bad = client.post(f"/api/accounts/{acc_id}/backup", json={"rebuild": "wipe"})
    assert bad.status_code >= 400 and "режим" in bad.json().get("message", "").lower()


def test_backup_rebuild_passes_mode_to_the_job(client):
    _login(client)
    acc_id = _make_account(client, "Пересоздание 2")
    r = client.post(f"/api/accounts/{acc_id}/backup", json={"rebuild": "full"})
    assert r.status_code == 200 and r.json()["rebuild"] == "full"
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["params"]["rebuild"] == "full"
    # необратимое действие должно попадать в аудит
    actions = [a["action"] for a in client.get("/api/audit?limit=20").json()]
    assert "backup_rebuild_full_request" in actions


def test_purge_account_index_clears_everything(client):
    """«С нуля» стирает и индекс писем, и состояние папок, и историю отказов."""
    _login(client)
    acc_id = _make_account(client, "Очистка")
    svc = client.app.state.services
    svc.db.add_message_index(acc_id, "INBOX", 1000, 1, "<a@b>", 10, "2026-09-01T00:00:00+00:00",
                             "", "cur/1.eml", "sha", subject="тест")
    svc.db.upsert_folder_state(acc_id, "INBOX", 1000, 1, 1)
    svc.db.record_folder_problem(acc_id, "Архив/битая", "failed EXAMINE")
    assert svc.db.count_messages(acc_id) == 1

    removed = svc.db.purge_account_index(acc_id)
    assert removed == 1
    assert svc.db.count_messages(acc_id) == 0
    assert svc.db.get_folder_state(acc_id, "INBOX") is None
    assert svc.db.list_folder_problems(acc_id) == []


def test_import_passwords_endpoint(client):
    """Загрузка паролей: ящик получает пароль, наружу пароль не возвращается."""
    _login(client)
    r = client.post("/api/accounts", json={
        "name": "Иванов", "host": "imap.example.ru", "port": 993, "username": "ivanov@example.ru",
        "password": "", "security": "ssl", "enabled": False,
    })
    acc_id = r.json()["id"]

    csv_data = "email;пароль\nivanov@example.ru;Secret1!\nnobody@example.ru;Secret2!\n"
    files = {"file": ("passwords.csv", csv_data.encode("utf-8"), "text/csv")}
    resp = client.post("/api/accounts/import-passwords", files=files, data={"enable": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 1 and body["enabled"] == 1
    assert body["not_found"] == ["nobody@example.ru"]
    assert "Secret1!" not in resp.text            # пароль наружу не уходит

    acc = [a for a in client.get("/api/accounts").json() if a["id"] == acc_id][0]
    assert acc["has_password"] is True and acc["enabled"] is True
    # в аудите — только счётчики, без паролей
    audit = [a for a in client.get("/api/audit?limit=20").json()
             if a["action"] == "accounts_import_passwords"]
    assert audit and "Secret1!" not in audit[0]["detail"]


def test_import_passwords_rejects_empty_file(client):
    _login(client)
    files = {"file": ("passwords.csv", b"", "text/csv")}
    assert client.post("/api/accounts/import-passwords", files=files).status_code >= 400
