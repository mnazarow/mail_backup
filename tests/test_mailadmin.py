"""Вход в ящики через учётную запись администратора почты (без паролей сотрудников)."""
import pytest
from imapclient.exceptions import LoginError

from mailarchiver import models
from mailarchiver.errors import ImapAuthError
from mailarchiver.imap import client as client_mod
from mailarchiver.imap.client import ConnectOptions, ImapConnection, check_login

PW = "Sw0rdfish!1"
MASTER = {"host": "mx.company.ru", "user": "archmaster", "password": "mpw", "mode": "sasl_plain", "separator": "*"}


class FakeClient:
    instances = []

    def __init__(self, host, port=None, ssl=True, ssl_context=None, timeout=None):
        self.host = host
        self.calls = []
        self.normalise_times = True
        FakeClient.instances.append(self)

    def starttls(self, ctx):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if password != "mpw" or "*archmaster" not in user:
            raise LoginError("[AUTHENTICATIONFAILED] Authentication failed.")

    def plain_login(self, identity, password, authorization_identity=None):
        self.calls.append(("plain", identity, password, authorization_identity))
        if password != "mpw":
            raise LoginError("[AUTHENTICATIONFAILED] Authentication failed.")

    def capabilities(self):
        return [b"IMAP4REV1"]

    def logout(self):
        pass

    def shutdown(self):
        pass


@pytest.fixture()
def fake(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(client_mod, "IMAPClient", FakeClient)
    return FakeClient


def _acc(**kw):
    base = dict(name="Иванов", host="mx.company.ru", port=993, username="ivanov@company.ru",
                auth_type=models.AuthType.MASTER)
    base.update(kw)
    return models.Account(**base)


def test_sasl_plain_with_authzid(fake):
    with ImapConnection(_acc(), ConnectOptions(master=dict(MASTER))):
        pass
    assert fake.instances[0].calls == [("plain", "archmaster", "mpw", "ivanov@company.ru")]


def test_separator_mode(fake):
    opts = ConnectOptions(master=dict(MASTER, mode="separator"))
    with ImapConnection(_acc(), opts):
        pass
    assert fake.instances[0].calls == [("login", "ivanov@company.ru*archmaster", "mpw")]


def test_admin_password_goes_only_to_its_own_server(fake):
    with pytest.raises(ImapAuthError, match="разрешён только для сервера"):
        with ImapConnection(_acc(host="evil.example.com"), ConnectOptions(master=dict(MASTER))):
            pass
    assert all(not c.calls for c in fake.instances)       # логина не было вовсе


def test_not_configured_and_rejected(fake):
    with pytest.raises(ImapAuthError, match="не настроен"):
        with ImapConnection(_acc(), ConnectOptions()):
            pass
    status, error = check_login(_acc(), ConnectOptions(master=dict(MASTER, password="wrong")))
    assert status == "auth_error" and "администратора почты" in error


def test_master_accounts_count_as_having_credentials():
    acc = _acc()
    assert models.account_has_credentials(acc)
    assert acc.redacted()["has_password"] is True
    assert not models.account_has_credentials(_acc(auth_type=models.AuthType.PASSWORD))


def test_master_credentials_from_settings(services):
    assert services.master_credentials() is None
    for key, value in {"enabled": True, "host": "mx.company.ru", "user": "archmaster", "password": "mpw"}.items():
        services.set_rt("mailadmin", key, value)
    creds = services.connect_options().master
    assert creds["user"] == "archmaster" and creds["password"] == "mpw" and creds["mode"] == "sasl_plain"
    raw = services.db.query_one("SELECT value FROM settings WHERE key='mailadmin.password'")["value"]
    assert "mpw" not in raw                                   # пароль в базе зашифрован


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": "admin", "password": PW})


def test_mailadmin_api(client, monkeypatch):
    _login(client)
    svc = client.app.state.services
    assert client.post("/api/mailadmin/check", json={}).status_code == 400
    r = client.put("/api/settings", json={"values": {"mailadmin.enabled": True, "mailadmin.host": "mx.company.ru",
                                                    "mailadmin.user": "archmaster", "mailadmin.password": "mpw"}})
    assert r.status_code == 200, r.text
    a1 = svc.db.create_account(models.Account(name="A", host="MX.company.ru", username="a@company.ru", password="p"))
    a2 = svc.db.create_account(models.Account(name="B", host="other.ru", username="b@other.ru", password="p"))
    seen = []

    def fake_check(acc, opts):
        seen.append((acc.username, acc.auth_type, opts.master["user"]))
        return "ok", ""
    monkeypatch.setattr("mailarchiver.imap.client.check_login", fake_check)
    res = client.post("/api/mailadmin/check", json={}).json()
    assert res["ok"] and res["username"] == "a@company.ru"
    assert seen == [("a@company.ru", "master", "archmaster")]
    conv = client.post("/api/mailadmin/convert", json={"to": "master"}).json()
    assert conv["changed"] == 1
    assert svc.db.get_account(a1).auth_type == "master" and svc.db.get_account(a2).auth_type == "password"
    assert svc.db.get_account(a1).password == "p"             # пароль не стёрт — можно вернуть
    back = client.post("/api/mailadmin/convert", json={"to": "password"}).json()
    assert back["changed"] == 1 and svc.db.get_account(a1).auth_type == "password"
    settings = client.get("/api/settings").json()
    assert settings["values"]["mailadmin"]["password"] == ""


def test_employee_template_uses_master_only_for_its_server(services):
    from mailarchiver.employees import account_template
    services.set_rt("employees", "account_host", "mx.company.ru")
    services.set_rt("employees", "account_use_master", True)
    assert account_template(services)["use_master"] is False          # вход администратора выключен
    services.set_rt("mailadmin", "enabled", True)
    services.set_rt("mailadmin", "host", "MX.company.ru")
    assert account_template(services)["use_master"] is True
    services.set_rt("employees", "account_host", "other.ru")
    assert account_template(services)["use_master"] is False
