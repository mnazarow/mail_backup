"""Единый вход через OpenID Connect (1.22.0): поддельный поставщик, PKCE, проверка id_token, роли, сотрудники."""
import base64
import hashlib
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mailarchiver import models, oidc


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class IdP:
    """Поставщик удостоверений для тестов: описание, ключи, обмен кода на id_token."""

    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ec_key = ec.generate_private_key(ec.SECP256R1())
        self.codes = {}
        self.token_requests = []
        self.userinfo, self.userinfo_auth, self.userinfo_redirect = {}, [], ""
        idp = self

        class H(BaseHTTPRequestHandler):
            def _json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if self.path == "/.well-known/openid-configuration":
                    self._json({"issuer": idp.issuer, "authorization_endpoint": idp.issuer + "/auth",
                                "token_endpoint": idp.issuer + "/token", "jwks_uri": idp.issuer + "/jwks",
                                "userinfo_endpoint": idp.issuer + "/userinfo",
                                "code_challenge_methods_supported": ["S256"],
                                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                                "id_token_signing_alg_values_supported": ["RS256", "ES256"]})
                elif self.path == "/jwks":
                    pub = idp.key.public_key().public_numbers()
                    ecn = idp.ec_key.public_key().public_numbers()
                    self._json({"keys": [
                        {"kty": "RSA", "kid": "r1", "use": "sig", "n": _b64(pub.n.to_bytes(256, "big")),
                         "e": _b64(pub.e.to_bytes(3, "big"))},
                        {"kty": "EC", "kid": "e1", "crv": "P-256", "x": _b64(ecn.x.to_bytes(32, "big")),
                         "y": _b64(ecn.y.to_bytes(32, "big"))}]})
                elif self.path == "/userinfo":
                    idp.userinfo_auth.append(self.headers.get("Authorization"))
                    if idp.userinfo_redirect:
                        self.send_response(302)
                        self.send_header("Location", idp.userinfo_redirect)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self._json(idp.userinfo)
                else:
                    self._json({}, 404)

            def do_POST(self):  # noqa: N802
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                idp.token_requests.append((self.headers.get("Authorization"), form))
                entry = idp.codes.pop(form.get("code"), None)
                expected = "Basic " + base64.b64encode(b"mailarchiver:s3cret").decode()
                if entry is None or self.headers.get("Authorization") != expected:
                    return self._json({"error": "invalid_grant"}, 400)
                challenge = _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
                if challenge != entry["challenge"] or form.get("redirect_uri") != entry["redirect_uri"]:
                    return self._json({"error": "invalid_grant"}, 400)
                claims = {"iss": idp.issuer, "aud": "mailarchiver", "exp": time.time() + 300, "iat": time.time(),
                          "nonce": entry["nonce"], **entry["claims"]}
                self._json({"access_token": "at", "token_type": "Bearer", "id_token": idp.sign(claims)})

            def log_message(self, *a):
                pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.issuer = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def sign(self, claims, alg="RS256", kid="r1", tamper=False):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding, utils
        head = _b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
        body = _b64(json.dumps(claims).encode())
        inp = f"{head}.{body}".encode()
        if alg == "RS256":
            sig = self.key.sign(inp, padding.PKCS1v15(), hashes.SHA256())
        elif alg == "ES256":
            r, s = utils.decode_dss_signature(self.ec_key.sign(inp, ec.ECDSA(hashes.SHA256())))
            sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        else:
            sig = b""
        if tamper:
            body = _b64(json.dumps({**claims, "groups": ["admins"]}).encode())
        return f"{head}.{body}.{_b64(sig)}"

    def login(self, auth_url, claims):
        """«Пользователь вошёл у поставщика»: код для обратного адреса."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(auth_url).query))
        code = "c" + str(len(self.codes) + len(self.token_requests))
        self.codes[code] = {"nonce": q["nonce"], "challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"],
                            "claims": claims}
        assert q["code_challenge_method"] == "S256" and "openid" in q["scope"].split()
        return code, q["state"]

    def stop(self):
        self.srv.shutdown()


@pytest.fixture()
def idp(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    p = IdP()
    oidc.clear_cache()
    yield p
    p.stop()
    oidc.clear_cache()


def _configure(svc, idp, **extra):
    vals = {"enabled": True, "issuer": idp.issuer, "client_id": "mailarchiver", "client_secret": "s3cret",
            "admin_values": ["admins"], "operator_values": ["ops"], "employee_login": True}
    vals.update(extra)
    for k, v in vals.items():
        svc.set_rt("oidc", k, v)


def _sso(client, idp, claims, *, state=None):
    r = client.get("/api/auth/oidc/start", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(idp.issuer + "/auth?")
    code, st = idp.login(r.headers["location"], claims)
    return client.get(f"/api/auth/oidc/callback?code={code}&state={state or st}", follow_redirects=False)


def test_sso_login_roles_and_errors(client, idp):
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/logout")
    svc = client.app.state.services
    _configure(svc, idp)
    ns = client.get("/api/needs-setup").json()
    assert ns["oidc"]["enabled"] and ns["oidc"]["button"]
    r = _sso(client, idp, {"sub": "1", "preferred_username": "anna", "email": "anna@x.ru", "groups": ["Admins"]})
    assert r.status_code == 303 and r.headers["location"] == "/#/"
    me = client.get("/api/me").json()
    assert me["username"] == "anna" and me["role"] == "admin"
    user = svc.db.get_user_by_name("anna")
    assert user["auth_source"] == "oidc"
    # обязательный 2FA архива к единому входу не применяется: второй фактор — у поставщика
    svc.set_rt("security", "require_2fa", True)
    assert client.get("/api/state").status_code == 200
    svc.set_rt("security", "require_2fa", False)
    client.post("/api/logout")
    # роль меняется вместе с группами у поставщика
    _sso(client, idp, {"sub": "1", "preferred_username": "anna", "groups": ["ops"]})
    assert client.get("/api/me").json()["role"] == "operator"
    client.post("/api/logout")
    # подмена state — отказ, в аудите
    r = _sso(client, idp, {"sub": "2", "preferred_username": "bob", "groups": ["admins"]}, state="forged")
    assert "sso_error" in r.headers["location"] and client.get("/api/me").status_code == 401
    assert svc.db.query_one("SELECT 1 FROM audit WHERE action='login_oidc_error' AND detail LIKE '%state%'")
    # локальный пользователь с тем же логином единым входом не входит
    r = _sso(client, idp, {"sub": "3", "preferred_username": "admin", "groups": ["admins"]})
    assert "sso_error" in r.headers["location"] and client.get("/api/me").status_code == 401
    # без групп архива — отключается; сотрудник — входит в архив своего ящика по подтверждённому email
    acc = svc.db.create_account(models.Account(name="Анна", host="h", username="anna@x.ru", password="p"))
    _sso(client, idp, {"sub": "1", "preferred_username": "anna", "email": "anna@x.ru", "email_verified": True,
                       "groups": []})
    me = client.get("/api/me").json()
    assert me["role"] == "mailbox" and svc.db.get_user_by_name("anna")["disabled"] == 1 and acc
    client.post("/api/logout")
    # без email_verified (Entra ID, ADFS) — только если администратор разрешил (1.25.0)
    _sso(client, idp, {"sub": "1", "preferred_username": "anna", "email": "anna@x.ru", "groups": []})
    assert client.get("/api/me").status_code == 401
    _sso(client, idp, {"sub": "1", "preferred_username": "anna", "email": "anna@x.ru", "email_verified": False,
                       "groups": []})
    assert client.get("/api/me").status_code == 401
    svc.set_rt("oidc", "trust_unverified_email", True)
    _sso(client, idp, {"sub": "1", "preferred_username": "anna", "email": "anna@x.ru", "groups": []})
    assert client.get("/api/me").json()["role"] == "mailbox"
    client.post("/api/logout")
    _sso(client, idp, {"sub": "1", "preferred_username": "anna", "email": "anna@x.ru", "email_verified": "false",
                       "groups": []})
    assert client.get("/api/me").status_code == 401
    svc.set_rt("oidc", "trust_unverified_email", False)
    client.post("/api/logout")
    # повтор без cookie состояния (ответ подсмотрели) — отказ
    r = client.get("/api/auth/oidc/start", follow_redirects=False)
    code, st = idp.login(r.headers["location"], {"sub": "9", "preferred_username": "eve", "groups": ["admins"]})
    client.cookies.clear()
    r = client.get(f"/api/auth/oidc/callback?code={code}&state={st}", follow_redirects=False)
    assert "sso_error" in r.headers["location"]


def test_id_token_checks(services, idp):
    svc = services
    _configure(svc, idp)
    base = {"iss": idp.issuer, "aud": "mailarchiver", "exp": time.time() + 60, "iat": time.time(), "nonce": "n1",
            "sub": "1"}
    assert oidc.verify_id_token(svc, idp.sign(base), "n1")["sub"] == "1"
    assert oidc.verify_id_token(svc, idp.sign(base, alg="ES256", kid="e1"), "n1")["sub"] == "1"
    bad = [(idp.sign(base, tamper=True), "подпись"), (idp.sign({**base, "aud": "other"}), "aud"),
           (idp.sign({**base, "exp": time.time() - 600}), "срок"), (idp.sign(base), "nonce"),
           (idp.sign({**base, "iss": "https://evil"}), "издатель"), (idp.sign(base, alg="none"), "алгоритм"),
           (idp.sign(base, kid="nope"), "ключа"), (idp.sign({**base, "aud": ["mailarchiver", "x"], "azp": "x"}), "azp")]
    for token, what in bad:
        nonce = "n2" if what == "nonce" else "n1"
        with pytest.raises(oidc.OidcError) as ei:
            oidc.verify_id_token(svc, token, nonce)
        assert what in str(ei.value), (what, str(ei.value))
    assert oidc.role_of(svc, {"realm_access": {"roles": ["ADMINS"]}}) == ""
    svc.set_rt("oidc", "role_claim", "realm_access.roles")
    assert oidc.role_of(svc, {"realm_access": {"roles": ["ADMINS"]}}) == "admin"
    chk = oidc.check(svc)
    assert chk["ok"] and chk["keys"] == 2 and chk["pkce"]
    svc.set_rt("oidc", "issuer", "http://sso.example.com")
    oidc.clear_cache()
    assert "https" in oidc.check(svc)["error"]


# ---------------------------------------------------------------------------
#  Разбор 16.11 (1.25.0)
# ---------------------------------------------------------------------------
def test_account_bound_to_subject(client, idp):
    """Тот же логин у другого пользователя поставщика в чужую учётную запись не входит."""
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/logout")
    svc = client.app.state.services
    _configure(svc, idp)
    _sso(client, idp, {"sub": "u-1", "preferred_username": "anna", "groups": ["admins"]})
    user = svc.db.get_user_by_name("anna")
    assert user["sso_subject"] == f"oidc|{idp.issuer}|u-1" and user["last_sso_at"]
    client.post("/api/logout")
    r = _sso(client, idp, {"sub": "u-2", "preferred_username": "anna", "groups": ["admins"]})
    assert "sso_error" in r.headers["location"] and client.get("/api/me").status_code == 401
    assert svc.db.query_one("SELECT 1 FROM audit WHERE action='sso_subject_mismatch'")
    # учётная запись прежней версии (без привязки) привязывается при первом входе
    svc.db.execute("UPDATE users SET sso_subject='' WHERE username='anna'")
    _sso(client, idp, {"sub": "u-2", "preferred_username": "anna", "groups": ["admins"]})
    assert client.get("/api/me").json()["username"] == "anna"
    assert svc.db.get_user_by_name("anna")["sso_subject"].endswith("|u-2")


def test_idle_sso_account_and_tokens(client, idp):
    """Не входил через поставщика N дней — токены не действуют, ежечасная проверка отключает; вход — включает."""
    from mailarchiver import apitokens, sso
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/logout")
    svc = client.app.state.services
    _configure(svc, idp)
    _sso(client, idp, {"sub": "u-1", "preferred_username": "anna", "groups": ["admins"]})
    anna = svc.db.get_user_by_name("anna")
    tok = apitokens.create(svc, dict(anna), name="скрипт", role="admin", days=0)["token"]
    assert apitokens.user_for(svc, tok)["owner"] == "anna"
    client.post("/api/logout")
    svc.db.execute("UPDATE users SET last_sso_at='2026-01-01T00:00:00+00:00' WHERE username='anna'")
    assert apitokens.user_for(svc, tok) is None                     # сразу, ещё до ежечасной проверки
    assert sso.disable_idle(svc) == 1
    assert svc.db.get_user_by_name("anna")["disabled"] == 1
    assert svc.db.query_one("SELECT 1 FROM audit WHERE action='user_disable' AND detail LIKE 'anna:%не входил%'")
    # снова вошла через поставщика — включена, токен действует
    _sso(client, idp, {"sub": "u-1", "preferred_username": "anna", "groups": ["admins"]})
    assert client.get("/api/me").json()["username"] == "anna"
    assert apitokens.user_for(svc, tok)["owner"] == "anna"
    # 0 — не отключать; локальных пользователей проверка не касается
    svc.set_rt("security", "sso_inactive_days", 0)
    svc.db.execute("UPDATE users SET last_sso_at='2020-01-01T00:00:00+00:00'")
    assert sso.disable_idle(svc) == 0 and apitokens.user_for(svc, tok) is not None
    svc.set_rt("security", "sso_inactive_days", 30)
    assert sso.disable_idle(svc) == 1 and not svc.db.get_user_by_name("admin")["disabled"]


def test_userinfo_does_not_follow_redirects(client, idp):
    """Токен доступа не уходит по перенаправлению userinfo на другой адрес."""
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/logout")
    svc = client.app.state.services
    _configure(svc, idp, use_userinfo=True)
    other = IdP()
    try:
        idp.userinfo_redirect = other.issuer + "/userinfo"
        _sso(client, idp, {"sub": "u-1", "preferred_username": "anna", "groups": ["admins"]})
        assert idp.userinfo_auth == ["Bearer at"] and other.userinfo_auth == []
        assert client.get("/api/me").json()["username"] == "anna"          # без userinfo — по id_token
        client.post("/api/logout")
        # без перенаправления userinfo дополняет данные (группы)
        idp.userinfo_redirect = ""
        idp.userinfo = {"sub": "u-2", "groups": ["ops"]}
        _sso(client, idp, {"sub": "u-2", "preferred_username": "boris"})
        assert client.get("/api/me").json()["role"] == "operator"
    finally:
        other.stop()
