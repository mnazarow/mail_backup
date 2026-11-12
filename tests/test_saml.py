"""Единый вход через SAML 2.0 (1.23.0): поставщик-подделка с настоящей подписью XML, проверки ответа, вход."""
import base64
import datetime
import re
import threading
import time
import urllib.parse
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("signxml")

from lxml import etree  # noqa: E402

from mailarchiver import models, saml  # noqa: E402

A = "urn:oasis:names:tc:SAML:2.0:assertion"
P = "urn:oasis:names:tc:SAML:2.0:protocol"
DS = "http://www.w3.org/2000/09/xmldsig#"
IDP_ID = "https://idp.example/saml"
SSO = "https://idp.example/sso"


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class IdP:
    """Поставщик удостоверений: ключ, сертификат, подписанные ответы."""

    def __init__(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.example")])
        now = datetime.datetime.now(datetime.timezone.utc)
        self.cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                     .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                     .not_valid_before(now - datetime.timedelta(days=1))
                     .not_valid_after(now + datetime.timedelta(days=365)).sign(self.key, hashes.SHA256()))
        self.pem = self.cert.public_bytes(serialization.Encoding.PEM).decode()
        self.b64 = "".join(self.pem.strip().splitlines()[1:-1])

    def response(self, *, in_response_to, acs, audience, name_id="anna", groups=("admins",), email="anna@x.ru",
                 sign="assertion", issuer=IDP_ID, not_after=None, recipient=None, assertion_id=None,
                 status="urn:oasis:names:tc:SAML:2.0:status:Success", key=None, raw_name_id=None, tamper=None):
        now = time.time()
        not_after = not_after if not_after is not None else now + 300
        aid = assertion_id or "_" + uuid.uuid4().hex
        attrs = "".join(f'<saml:AttributeValue>{g}</saml:AttributeValue>' for g in groups)
        irt = f' InResponseTo="{in_response_to}"' if in_response_to else ""
        name_xml = raw_name_id if raw_name_id is not None else name_id
        placeholder = f'<ds:Signature xmlns:ds="{DS}" Id="placeholder"></ds:Signature>'
        assertion = (
            f'<saml:Assertion xmlns:saml="{A}" ID="{aid}" Version="2.0" IssueInstant="{_iso(now)}">'
            f'<saml:Issuer>{issuer}</saml:Issuer>{placeholder if sign == "assertion" else ""}'
            f'<saml:Subject><saml:NameID>{name_xml}</saml:NameID>'
            f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
            f'<saml:SubjectConfirmationData{irt} NotOnOrAfter="{_iso(not_after)}" Recipient="{recipient or acs}"/>'
            f'</saml:SubjectConfirmation></saml:Subject>'
            f'<saml:Conditions NotBefore="{_iso(now - 60)}" NotOnOrAfter="{_iso(not_after)}">'
            f'<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>'
            f'</saml:Conditions>'
            f'<saml:AuthnStatement AuthnInstant="{_iso(now)}"><saml:AuthnContext><saml:AuthnContextClassRef>'
            f'urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml:AuthnContextClassRef>'
            f'</saml:AuthnContext></saml:AuthnStatement>'
            f'<saml:AttributeStatement><saml:Attribute Name="groups">{attrs}</saml:Attribute>'
            f'<saml:Attribute Name="email"><saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute>'
            f'</saml:AttributeStatement></saml:Assertion>')
        resp = (f'<samlp:Response xmlns:samlp="{P}" xmlns:saml="{A}" ID="_r{uuid.uuid4().hex}" Version="2.0" '
                f'IssueInstant="{_iso(now)}" Destination="{acs}"{irt}><saml:Issuer>{issuer}</saml:Issuer>'
                f'{placeholder if sign == "response" else ""}'
                f'<samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>{assertion}</samlp:Response>')
        root = etree.fromstring(resp.encode())
        if sign == "assertion":
            el = root.find(f"{{{A}}}Assertion")
            root.replace(el, self._sign(el, aid, key))
        elif sign == "response":
            root = self._sign(root, root.get("ID"), key)
        if tamper:
            tamper(root)
        return base64.b64encode(etree.tostring(root)).decode()

    def _sign(self, el, ref, key=None):
        from signxml import XMLSigner, methods
        return XMLSigner(method=methods.enveloped, signature_algorithm="rsa-sha256", digest_algorithm="sha256",
                         c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#").sign(
            el, key=key or self.key, cert=[self.cert], reference_uri=ref, id_attribute="ID")

    def metadata(self):
        return (f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" xmlns:ds="{DS}" '
                f'entityID="{IDP_ID}"><md:IDPSSODescriptor protocolSupportEnumeration="{P}">'
                f'<md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data><ds:X509Certificate>{self.b64}'
                f'</ds:X509Certificate></ds:X509Data></ds:KeyInfo></md:KeyDescriptor>'
                f'<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" Location="{SSO}/post"/>'
                f'<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
                f'Location="{SSO}"/></md:IDPSSODescriptor></md:EntityDescriptor>').encode()


@pytest.fixture()
def idp():
    return IdP()


def _configure(svc, idp, **extra):
    vals = {"enabled": True, "idp_entity_id": IDP_ID, "idp_sso_url": SSO, "idp_cert": idp.pem,
            "admin_values": ["Admins"], "operator_values": ["ops"], "employee_login": True}
    vals.update(extra)
    for k, v in vals.items():
        svc.set_rt("saml", k, v)
    saml.clear_cache()


def _request_of(location):
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
    xml = zlib.decompress(base64.b64decode(q["SAMLRequest"]), -15)
    root = etree.fromstring(xml)
    return root, q


def _login(client, idp, **kw):
    r = client.get("/api/auth/saml/start", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(SSO + "?"), r.text
    req, q = _request_of(r.headers["location"])
    assert q["RelayState"] and req.get("ProtocolBinding").endswith("HTTP-POST")
    acs = req.get("AssertionConsumerServiceURL")
    audience = req.find(f"{{{A}}}Issuer").text
    body = idp.response(in_response_to=req.get("ID"), acs=acs, audience=audience, **kw)
    # ответ приходит POST-запросом с сайта поставщика: Origin чужой, cookie нет
    r = client.post("/api/auth/saml/acs", data={"SAMLResponse": body, "RelayState": q["RelayState"]},
                    headers={"Origin": "https://idp.example", "X-Requested-With": ""}, follow_redirects=False)
    return r, body


def _finish(client, r):
    assert r.status_code == 303 and r.headers["location"].startswith("/api/auth/saml/finish?t="), r.headers
    return client.get(r.headers["location"], follow_redirects=False)


def test_saml_login_roles_replay_and_browser_binding(client, idp):
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/logout")
    svc = client.app.state.services
    assert client.get("/api/needs-setup").json()["saml"]["enabled"] is False
    _configure(svc, idp)
    ns = client.get("/api/needs-setup").json()
    assert ns["saml"] == {"enabled": True, "button": "Войти через SAML"}
    # описание архива для поставщика
    md = etree.fromstring(client.get("/api/auth/saml/metadata").content)
    assert md.get("entityID") == "http://testserver/api/auth/saml/metadata"
    acs = md.find(".//{urn:oasis:names:tc:SAML:2.0:metadata}AssertionConsumerService")
    assert acs.get("Location") == "http://testserver/api/auth/saml/acs"
    # вход администратором (группы — без учёта регистра)
    r, body = _login(client, idp, groups=("admins", "staff"))
    r = _finish(client, r)
    assert r.status_code == 303 and r.headers["location"] == "/#/"
    me = client.get("/api/me").json()
    assert me["username"] == "anna" and me["role"] == "admin"
    assert svc.db.get_user_by_name("anna")["auth_source"] == "saml"
    assert svc.db.query_one("SELECT 1 FROM audit WHERE action='login' AND detail LIKE '%(SAML)%'")
    # обязательный 2FA архива к единому входу не применяется
    svc.set_rt("security", "require_2fa", True)
    assert client.get("/api/state").status_code == 200
    svc.set_rt("security", "require_2fa", False)
    client.post("/api/logout")
    # повтор того же ответа — отказ
    r = client.post("/api/auth/saml/acs", data={"SAMLResponse": body}, follow_redirects=False)
    assert "sso_error" in r.headers["location"]
    assert svc.db.query_one("SELECT 1 FROM audit WHERE action='login_saml_error' AND detail LIKE '%уже использовано%'")
    # ответ, полученный в одном браузере, не открывает сеанс в другом (нет cookie начала входа)
    r, _ = _login(client, idp, name_id="petr", groups=("ops",))
    other = type(client)(client.app)
    r2 = other.get(r.headers["location"], follow_redirects=False)
    assert "sso_error" in r2.headers["location"] and other.get("/api/me").status_code == 401
    assert client.get(r.headers["location"], follow_redirects=False).headers["location"].endswith("sso_error=1#/")
    # роль меняется вместе с группами; без групп — отключение, сотрудник входит в архив своего ящика
    r, _ = _login(client, idp, groups=("ops",))
    _finish(client, r)
    assert client.get("/api/me").json()["role"] == "operator"
    client.post("/api/logout")
    svc.db.create_account(models.Account(name="Анна", host="h", username="anna@x.ru", password="p"))
    r, _ = _login(client, idp, groups=())
    _finish(client, r)
    me = client.get("/api/me").json()
    assert me["role"] == "mailbox" and svc.db.get_user_by_name("anna")["disabled"] == 1
    client.post("/api/logout")
    # локальный пользователь с тем же логином через SAML не входит
    r, _ = _login(client, idp, name_id="admin")
    assert "sso_error" in _finish(client, r).headers["location"]
    assert client.get("/api/me").status_code == 401


def _consume(svc, idp, **kw):
    return saml.consume(svc, idp.response(in_response_to="_req1", acs="https://arch.test/acs",
                                          audience="https://arch.test/sp", **kw))


def test_response_checks(services, idp):
    svc = services
    _configure(svc, idp, sp_entity_id="https://arch.test/sp", acs_url="https://arch.test/acs")
    info = _consume(svc, idp)
    assert info["username"] == "anna" and info["role"] == "admin" and info["in_response_to"] == "_req1"
    assert _consume(svc, idp, sign="response")["role"] == "admin"          # подписан весь ответ
    other = IdP()
    bad = [
        (dict(sign=None), "не подписан"),
        (dict(key=other.key), "не сходится"),                               # чужой ключ
        (dict(audience_override=True), "не архиву"),
        (dict(recipient="https://evil/acs"), "подтверждения субъекта"),
        (dict(not_after=time.time() - 600), "срок"),
        (dict(issuer="https://evil"), "издатель"),
        (dict(status="urn:oasis:names:tc:SAML:2.0:status:Responder"), "отказал"),
    ]
    for kw, what in bad:
        with pytest.raises(saml.SamlError) as ei:
            if kw.pop("audience_override", False):
                saml.consume(svc, idp.response(in_response_to="_req1", acs="https://arch.test/acs",
                                               audience="https://other/sp"))
            else:
                _consume(svc, idp, **kw)
        assert what in str(ei.value), (what, str(ei.value))
    # вход, начатый поставщиком (без InResponseTo), не принимается
    with pytest.raises(saml.SamlError, match="InResponseTo"):
        saml.consume(svc, idp.response(in_response_to="", acs="https://arch.test/acs",
                                       audience="https://arch.test/sp"))
    # повтор утверждения
    body = idp.response(in_response_to="_req1", acs="https://arch.test/acs", audience="https://arch.test/sp")
    saml.consume(svc, body)
    with pytest.raises(saml.SamlError, match="уже использовано"):
        saml.consume(svc, body)


def test_signature_wrapping_and_comments(services, idp):
    svc = services
    _configure(svc, idp, sp_entity_id="https://arch.test/sp", acs_url="https://arch.test/acs")

    def add_evil(root):
        """Подписанное утверждение «Боба» прячется, рядом — поддельное «админа» (XML signature wrapping)."""
        good = root.find(f"{{{A}}}Assertion")
        evil = etree.fromstring(etree.tostring(good))
        evil.set("ID", "_evil")
        for sig in evil.findall(f"{{{DS}}}Signature"):
            evil.remove(sig)
        evil.find(f"{{{A}}}Subject/{{{A}}}NameID").text = "admin"
        ext = etree.SubElement(root, f"{{{P}}}Extensions")
        root.remove(good)
        ext.append(good)
        root.append(evil)

    def edit_group(root):
        root.find(f".//{{{A}}}AttributeValue").text = "admins"

    for tamper, what in ((add_evil, "ровно одно"), (edit_group, "не сходится")):
        with pytest.raises(saml.SamlError) as ei:
            _consume(svc, idp, name_id="bob", groups=("users",), tamper=tamper)
        assert what in str(ei.value), str(ei.value)
    # комментарий внутри NameID не обрезает имя (уязвимость 2018 г.)
    info = _consume(svc, idp, raw_name_id="admin<!---->.evil")
    assert info["username"] == "admin.evil"
    # DOCTYPE не принимается
    doc = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "y">]><samlp:Response xmlns:samlp="' + P.encode() + b'"/>'
    with pytest.raises(saml.SamlError, match="DOCTYPE"):
        saml.consume(svc, base64.b64encode(doc).decode())


def test_metadata_and_check(client, idp, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            data = idp.metadata()
            self.send_response(200)
            self.send_header("Content-Type", "application/samlmetadata+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
        client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})
        svc = client.app.state.services
        _configure(svc, idp, idp_entity_id="", idp_sso_url="", idp_cert="",
                   idp_metadata_url=f"http://127.0.0.1:{srv.server_address[1]}/metadata")
        r = client.post("/api/saml/check").json()
        assert r["ok"] and r["entity_id"] == IDP_ID and r["sso_url"] == SSO, r
        assert r["certs"][0]["days_left"] >= 360 and not r["cert_warning"]
        assert r["acs_url"] == "http://testserver/api/auth/saml/acs"
        # вход по настройкам из описания
        client.post("/api/logout")
        r, _ = _login(client, idp)
        assert _finish(client, r).headers["location"] == "/#/"
        assert client.get("/api/me").json()["role"] == "admin"
        # сертификат из настроек важнее описания: ответ, подписанный «старым» ключом, не принимается
        svc.set_rt("saml", "idp_cert", IdP().b64)
        saml.clear_cache()
        client.post("/api/logout")
        r, _ = _login(client, idp)
        assert "sso_error" in r.headers["location"]
    finally:
        srv.shutdown()
    assert re.match(r"^[0-9A-F:]+$", saml.cert_info(idp.pem)["sha256"])
