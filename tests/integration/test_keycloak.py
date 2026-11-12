"""Единый вход с настоящим поставщиком (1.23.0): Keycloak — OpenID Connect и SAML 2.0.

Стенд поднимает Keycloak, если задан его каталог: ``sudo MA_STAND_KEYCLOAK=/opt/keycloak-26.4.2
scripts/integration/stand.sh start`` (область ``corp`` из ``scripts/integration/keycloak-realm.json``: anna —
администраторы архива, petr — операторы, ivanov — без групп). Тест ведёт себя как браузер: страница входа
Keycloak, форма с паролем, возврат в архив.
"""
import html
import os
import re
import urllib.parse

import pytest

from test_stand import ENV

KC = ENV.get("MA_IT_KEYCLOAK", "")
pytestmark = pytest.mark.skipif(os.environ.get("MA_IT") != "1" or not KC,
                                reason="нет Keycloak на стенде: MA_STAND_KEYCLOAK=<каталог> stand.sh start, MA_IT=1")
REALM = KC.rstrip("/") + "/realms/corp"
PASS = ENV.get("MA_IT_PASS", "Passw0rd-1")


@pytest.fixture()
def kc_env(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


class _Browser:
    """Браузер для страниц Keycloak. Cookie Keycloak помечены Secure: настоящий браузер считает
    http://127.0.0.1 безопасным и отправляет их, а клиент httpx — нет, поэтому cookie носим сами."""

    def __init__(self):
        import httpx
        self.http = httpx.Client(follow_redirects=False, timeout=30, trust_env=False)
        self.jar = {}

    def _go(self, method, url, **kw):
        headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.jar.items())} if self.jar else {}
        r = self.http.request(method, url, headers=headers, **kw)
        for line in r.headers.get_list("set-cookie"):
            name, _, rest = line.partition("=")
            self.jar[name.strip()] = rest.split(";", 1)[0]
        return r

    def get(self, url):
        return self._go("GET", url)

    def post(self, url, data):
        return self._go("POST", url, data=data)


def _browser():
    return _Browser()


def _kc_login(browser, url, username):
    """Страница входа Keycloak → форма с паролем → ответ (переадресация или форма с SAMLResponse)."""
    r = browser.get(url)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    action = html.unescape(re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', r.text).group(1))
    return browser.post(action, data={"username": username, "password": PASS, "credentialId": ""})


def _setup(client):
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/logout")
    return client.app.state.services


def test_oidc_with_keycloak(client, kc_env):
    from mailarchiver import oidc
    svc = _setup(client)
    for k, v in {"enabled": True, "issuer": REALM, "client_id": "mailarchiver", "client_secret": "kc-secret",
                 "admin_values": ["mail-archive-admins"], "operator_values": ["mail-archive-operators"],
                 "employee_login": True}.items():
        svc.set_rt("oidc", k, v)
    oidc.clear_cache()
    chk = oidc.check(svc)
    assert chk["ok"] and chk["issuer"] == REALM and chk["pkce"], chk
    for user, role in (("anna", "admin"), ("petr", "operator")):
        browser = _browser()
        r = client.get("/api/auth/oidc/start", follow_redirects=False)
        assert r.headers["location"].startswith(REALM + "/protocol/openid-connect/auth?")
        r = _kc_login(browser, r.headers["location"], user)
        assert r.status_code == 302, (r.status_code, r.text[:300])
        back = urllib.parse.urlsplit(r.headers["location"])
        assert back.path == "/api/auth/oidc/callback" and "code=" in back.query
        r = client.get(f"{back.path}?{back.query}", follow_redirects=False)
        assert r.headers["location"] == "/#/", r.headers
        me = client.get("/api/me").json()
        assert me["username"] == user and me["role"] == role
        assert svc.db.get_user_by_name(user)["auth_source"] == "oidc"
        client.post("/api/logout")


def test_saml_with_keycloak(client, kc_env):
    from mailarchiver import models, saml
    svc = _setup(client)
    for k, v in {"enabled": True, "idp_metadata_url": REALM + "/protocol/saml/descriptor",
                 "admin_values": ["mail-archive-admins"], "operator_values": ["mail-archive-operators"],
                 "employee_login": True}.items():
        svc.set_rt("saml", k, v)
    saml.clear_cache()
    chk = saml.check(svc)
    assert chk["ok"] and chk["entity_id"] == REALM and chk["certs"], chk
    svc.db.create_account(models.Account(name="Иванов", host="h", username="ivanov@corp.test", password="p"))
    for user, role in (("anna", "admin"), ("petr", "operator"), ("ivanov", "mailbox")):
        browser = _browser()
        r = client.get("/api/auth/saml/start", follow_redirects=False)
        assert r.headers["location"].startswith(REALM + "/protocol/saml?SAMLRequest="), r.headers
        r = _kc_login(browser, r.headers["location"], user)
        assert r.status_code == 200, (r.status_code, r.text[:300])
        form = dict(re.findall(r'name="(SAMLResponse|RelayState)"\s+value="([^"]*)"', r.text))
        action = html.unescape(re.search(r'<form[^>]+action="([^"]+)"', r.text).group(1))
        assert action == "http://testserver/api/auth/saml/acs" and form.get("SAMLResponse")
        r = client.post("/api/auth/saml/acs", data={k: html.unescape(v) for k, v in form.items()},
                        headers={"Origin": KC.rstrip("/")}, follow_redirects=False)
        assert r.headers["location"].startswith("/api/auth/saml/finish?t="), (r.headers, svc.db.query(
            "SELECT detail FROM audit WHERE action='login_saml_error' ORDER BY id DESC LIMIT 1"))
        r = client.get(r.headers["location"], follow_redirects=False)
        assert r.headers["location"] == "/#/", r.headers
        me = client.get("/api/me").json()
        assert me["role"] == role and (me["username"] == user or role == "mailbox"), me
        client.post("/api/logout")
