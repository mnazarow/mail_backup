"""Тесты REST API через FastAPI TestClient (без реального IMAP)."""


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


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
