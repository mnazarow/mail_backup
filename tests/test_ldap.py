"""Вход через LDAP / Active Directory (1.20.0): поиск, вход, роли по группам, вложенные группы, сотрудники, API."""
import socket

import pytest

from ldap_fake import Directory, LdapServer

from mailarchiver import ldapauth, models

ADM = "CN=Archive-Admins,OU=G,DC=t"
OPS = "CN=Archive-Ops,OU=G,DC=t"
HELP = "CN=Helpdesk,OU=G,DC=t"


@pytest.fixture()
def ldap_dir():
    d = Directory()
    d.add("CN=svc,OU=S,DC=t", "svcpw", objectClass="user", sAMAccountName="svc")
    d.add("CN=Анна Админ,OU=U,DC=t", "Pa55", objectClass="user", sAMAccountName="anna", mail="anna@x.ru",
          displayName="Анна Админ", memberOf=[ADM])
    d.add("CN=Борис,OU=U,DC=t", "Bob1", objectClass="user", sAMAccountName="bob", memberOf=[HELP])
    d.add(HELP, objectClass="group", memberOf=[OPS])
    d.add(OPS, objectClass="group")
    d.add(ADM, objectClass="group")
    d.add("CN=Иван,OU=U,DC=t", "Iv4n", objectClass="user", sAMAccountName="ivan", mail="Ivanov@x.ru")
    srv = LdapServer(d)
    yield d, srv
    srv.stop()


def _configure(svc, port, **extra):
    values = {"enabled": True, "url": f"ldap://127.0.0.1:{port}", "starttls": False, "bind_dn": "CN=svc,OU=S,DC=t",
              "bind_password": "svcpw", "base_dn": "DC=t", "admin_group": ADM, "operator_group": OPS}
    values.update(extra)
    for k, v in values.items():
        svc.set_rt("ldap", k, v)


def test_lookup_roles_and_password(services, ldap_dir):
    d, srv = ldap_dir
    _configure(services, srv.port)
    info = ldapauth.lookup(services, "anna", "Pa55")
    assert info["ok"] and info["role"] == "admin" and info["mail"] == "anna@x.ru" and info["name"] == "Анна Админ"
    assert ldapauth.lookup(services, "anna", "wrong") == {"ok": False, "reason": "неверный пароль"}
    assert ldapauth.lookup(services, "COMPANY\\bob", "Bob1")["role"] == "operator"      # вложенная группа
    services.set_rt("ldap", "nested_groups", False)
    assert ldapauth.lookup(services, "bob", "Bob1")["role"] == ""
    assert ldapauth.lookup(services, "nobody", "x")["ok"] is False
    # логин с символами фильтра не меняет смысл поиска
    assert ldapauth.lookup(services, "*)(sAMAccountName=anna", "Pa55")["ok"] is False
    assert ldapauth.lookup(services, "an*", None)["ok"] is False


def test_filters_encode():
    assert ldapauth.escape("a*b(c)\\") == "a\\2ab\\28c\\29\\5c"
    raw = ldapauth.parse_filter("(&(objectClass=user)(|(cn=Ив*)(!(mail=*)))(memberOf:1.2.840.113556.1.4.1941:=CN=G,DC=t))")
    assert raw[0] == 0xA0
    with pytest.raises(Exception):
        ldapauth.parse_filter("(&(cn=a)")


def test_password_never_sent_in_clear(services, ldap_dir, monkeypatch):
    d, srv = ldap_dir
    _configure(services, srv.port, url=f"ldap://dc.example.com:{srv.port}")
    monkeypatch.setattr(ldapauth, "CONNECT", lambda h, p, t: socket.create_connection(("127.0.0.1", p), timeout=t))
    with pytest.raises(ldapauth.LdapError) as ei:
        ldapauth.lookup(services, "anna", "Pa55")
    assert "незашифрованному" in str(ei.value) and d.binds == []


def _login(client, user, pw):
    return client.post("/api/login", json={"username": user, "password": pw})


def test_login_via_ldap_and_group_removal(client, ldap_dir):
    d, srv = ldap_dir
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    svc = client.app.state.services
    _configure(svc, srv.port)
    r = _login(client, "anna", "Pa55")
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin", r.text
    me = client.get("/api/me").json()
    assert me["username"] == "anna" and me["role"] == "admin"
    row = svc.db.get_user_by_name("anna")
    assert row["auth_source"] == "ldap"
    # локальный пароль у такой учётной записи случайный — войти можно только через каталог
    client.post("/api/logout")
    assert _login(client, "anna", "wrong").status_code in (400, 401)
    # убрали из группы — вход запрещён, учётная запись отключена
    d.entries["CN=Анна Админ,OU=U,DC=t"]["memberof"] = []
    assert _login(client, "anna", "Pa55").status_code in (400, 401)
    assert svc.db.get_user_by_name("anna")["disabled"] == 1
    # локальный администратор с тем же именем в каталоге не подменяется
    d.add("CN=admin,OU=U,DC=t", "Other1", objectClass="user", sAMAccountName="admin", memberOf=[ADM])
    assert _login(client, "admin", "Other1").status_code in (400, 401)
    assert _login(client, "admin", "Sw0rdfish!").status_code == 200
    audit = [x["detail"] for x in client.get("/api/audit").json() if x["action"] == "login"]
    assert any("LDAP" in a for a in audit)


def test_employee_login_and_test_endpoint(client, ldap_dir):
    d, srv = ldap_dir
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    svc = client.app.state.services
    _configure(svc, srv.port, employee_login=True)
    aid = svc.db.create_account(models.Account(name="Иванов", host="imap.x.ru", username="ivanov@x.ru", password="p"))
    _login(client, "admin", "Sw0rdfish!")
    r = client.post("/api/ldap/test", json={"username": "bob"})
    assert r.json()["ok"] and r.json()["role"] == "operator"
    r = client.post("/api/ldap/test", json={})
    assert r.json() == {"ok": True, "bind_only": True}
    svc.set_rt("ldap", "bind_password", "bad")
    assert client.post("/api/ldap/test", json={}).json()["ok"] is False
    svc.set_rt("ldap", "bind_password", "svcpw")
    client.post("/api/logout")
    r = _login(client, "ivan", "Iv4n")
    assert r.status_code == 200 and r.json()["user"]["role"] == "mailbox" and r.json()["user"]["account_id"] == aid
