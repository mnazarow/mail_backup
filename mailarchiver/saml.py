"""
Единый вход через SAML 2.0 (1.23.0): ADFS, Microsoft Entra ID, Keycloak, Shibboleth, «Яндекс 360», Okta и другие
поставщики удостоверений (IdP). Архив — поставщик услуг (SP).

Поток (SP-initiated, Web Browser SSO):

1. ``/api/auth/saml/start`` — запрос входа ``AuthnRequest`` (привязка HTTP-Redirect) со случайным ``ID``;
   ``ID`` и ``RelayState`` — в подписанной cookie (10 минут, ``SameSite=Lax``, только путь ``/api/auth/saml``);
2. поставщик возвращает браузер POST-запросом на ``/api/auth/saml/acs`` с ``SAMLResponse``. Подпись проверяется
   библиотекой ``signxml`` **по сертификату поставщика из настроек или его описания** (сертификат из самого ответа
   не доверяется); дальше разбирается только подписанная часть (защита от «подмены обёртки», XML signature
   wrapping). Проверяется: издатель, получатель (``Audience`` = идентификатор архива), адрес возврата
   (``Recipient``), ответ на наш запрос (``InResponseTo``), сроки (``NotBefore``/``NotOnOrAfter``, допуск 2 минуты),
   повтор утверждения (``ID`` запоминается до конца срока). Зашифрованные утверждения, SHA-1 и вход,
   начатый поставщиком (IdP-initiated), не принимаются;
3. POST от поставщика приходит без cookie (``SameSite=Lax``), поэтому проверенный вход ждёт браузер 2 минуты
   под одноразовым номером, а ``/api/auth/saml/finish`` (обычный переход, cookie уже есть) сверяет, что вход
   начат в этом же браузере, и открывает сеанс — так чужой ответ поставщика не «впишет» вас под чужим именем.

Роль — по значениям атрибута групп (``saml.role_attribute``), как у OIDC; учётная запись — :mod:`sso`.
Описание архива для поставщика — ``/api/auth/saml/metadata``.
"""
from __future__ import annotations

import base64
import hmac
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

from .logging_setup import get_logger
from .sso import SsoError, ensure_user as _sso_ensure_user, remember_once, role_from, take_once

log = get_logger("saml")

COOKIE = "ma_saml"
COOKIE_PATH = "/api/auth/saml"
STATE_TTL_S = 600
TICKET_TTL_S = 120
CLOCK_SKEW_S = 120
TIMEOUT = 15
MAX_RESPONSE = 1024 * 1024
METADATA_TTL_S = 3600

NS = {"samlp": "urn:oasis:names:tc:SAML:2.0:protocol", "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
      "md": "urn:oasis:names:tc:SAML:2.0:metadata", "ds": "http://www.w3.org/2000/09/xmldsig#"}
_P, _A = "{%s}" % NS["samlp"], "{%s}" % NS["saml"]
BINDING_REDIRECT = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
BINDING_POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
#: Где обычно лежит адрес почты, если атрибут не задан.
EMAIL_ATTRS = ("email", "mail", "emailaddress", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
               "urn:oid:0.9.2342.19200300.100.1.3")

_cache: Dict[str, Tuple[float, Any]] = {}
_cache_lock = threading.Lock()


class SamlError(SsoError):
    """Вход через SAML не удался — текст для журнала и аудита."""


def available() -> bool:
    try:
        import lxml  # noqa: F401
        import signxml  # noqa: F401
    except ImportError:
        return False
    return True


def enabled(svc) -> bool:
    if not bool(svc.rt("saml", "enabled")):
        return False
    return bool(str(svc.rt("saml", "idp_metadata_url") or "").strip()) or (
        bool(str(svc.rt("saml", "idp_sso_url") or "").strip()) and bool(str(svc.rt("saml", "idp_cert") or "").strip()))


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
#  Разбор XML
# ---------------------------------------------------------------------------
def _parse(data: bytes):
    """Разбор XML без DTD, сущностей и обращений в сеть."""
    from lxml import etree
    if re.search(rb"<!DOCTYPE|<!ENTITY", data[:20000], re.I):
        raise SamlError("XML с DOCTYPE не принимается")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False,
                             remove_pis=True)
    try:
        return etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SamlError(f"неверный XML: {exc}") from None


def _text(el) -> str:
    """Весь текст элемента (комментарии внутри не обрезают значение — уязвимость 2018 г.)."""
    return "".join(el.itertext()).strip() if el is not None else ""


def _time(value: str) -> Optional[float]:
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$", (value or "").strip())
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    tz = m.group(3) or "Z"
    dt = dt.replace(tzinfo=timezone.utc)
    ts = dt.timestamp() + float(m.group(2) or 0)
    if tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        ts -= sign * (int(tz[1:3]) * 3600 + int(tz[4:6]) * 60)
    return ts


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cert_pem(text: str) -> str:
    """Сертификат из PEM, base64 без заголовков или с пробелами вместо переводов строк → PEM."""
    from cryptography import x509
    body = re.sub(r"-----(BEGIN|END) CERTIFICATE-----", "", text or "")
    body = re.sub(r"\s+", "", body)
    try:
        der = base64.b64decode(body, validate=True)
        cert = x509.load_der_x509_certificate(der)
    except Exception:  # noqa: BLE001
        raise SamlError("сертификат поставщика не читается (нужен X.509 в PEM или base64)") from None
    from cryptography.hazmat.primitives.serialization import Encoding
    return cert.public_bytes(Encoding.PEM).decode("ascii")


def cert_info(pem: str) -> Dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
    try:
        until = cert.not_valid_after_utc
    except AttributeError:  # cryptography < 42
        until = cert.not_valid_after.replace(tzinfo=timezone.utc)
    return {"subject": cert.subject.rfc4514_string()[:200], "until": until.isoformat(),
            "days_left": int((until - datetime.now(timezone.utc)).total_seconds() // 86400),
            "sha256": cert.fingerprint(hashes.SHA256()).hex(":").upper()}


# ---------------------------------------------------------------------------
#  Описание поставщика (IdP metadata) и настройки
# ---------------------------------------------------------------------------
def _local(host: str) -> bool:
    return (host or "").lower() in ("localhost", "127.0.0.1", "::1")


def _check_url(url: str, what: str) -> str:
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise SamlError(f"{what}: нужен адрес https://")
    if parts.scheme == "http" and not _local(parts.hostname):
        raise SamlError(f"{what}: только https:// (кроме этой машины)")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _fetch(svc, url: str) -> bytes:
    _check_url(url, "описание поставщика")
    ctx = ssl.create_default_context()
    if svc.rt("saml", "verify_ssl") is False:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, headers={"Accept": "application/samlmetadata+xml, application/xml",
                                               "User-Agent": "MailArchiver"})
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return resp.read(MAX_RESPONSE)
    except urllib.error.HTTPError as exc:
        raise SamlError(f"{urllib.parse.urlsplit(url).netloc}: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise SamlError(f"нет связи с {urllib.parse.urlsplit(url).netloc}: {getattr(exc, 'reason', exc)}") from None


def parse_metadata(data: bytes, entity_id: str = "") -> Dict:
    """Издатель, адрес входа (HTTP-Redirect) и сертификаты подписи из описания поставщика."""
    root = _parse(data)
    if root.tag == "{%s}EntitiesDescriptor" % NS["md"]:
        ents = root.findall("md:EntityDescriptor", NS)
        ents = [e for e in ents if e.find("md:IDPSSODescriptor", NS) is not None]
        if entity_id:
            ents = [e for e in ents if e.get("entityID") == entity_id]
        if len(ents) != 1:
            raise SamlError("в описании несколько поставщиков — укажите «Идентификатор поставщика»")
        root = ents[0]
    if root.tag != "{%s}EntityDescriptor" % NS["md"]:
        raise SamlError("это не описание SAML (нет EntityDescriptor)")
    idp = root.find("md:IDPSSODescriptor", NS)
    if idp is None:
        raise SamlError("в описании нет IDPSSODescriptor — это не поставщик удостоверений")
    sso = ""
    for el in idp.findall("md:SingleSignOnService", NS):
        if el.get("Binding") == BINDING_REDIRECT:
            sso = el.get("Location") or ""
            break
    certs = []
    for kd in idp.findall("md:KeyDescriptor", NS):
        if kd.get("use") not in (None, "", "signing"):
            continue
        for c in kd.findall("ds:KeyInfo/ds:X509Data/ds:X509Certificate", NS):
            certs.append(_cert_pem(_text(c)))
    if not sso:
        raise SamlError("поставщик не заявил вход по HTTP-Redirect")
    if not certs:
        raise SamlError("в описании поставщика нет сертификата подписи")
    return {"entity_id": root.get("entityID") or "", "sso_url": sso, "certs": list(dict.fromkeys(certs))}


def idp(svc, refresh: bool = False) -> Dict:
    """Настройки поставщика: из описания по адресу, вручную заданное — поверх."""
    url = str(svc.rt("saml", "idp_metadata_url") or "").strip()
    out = {"entity_id": "", "sso_url": "", "certs": []}
    if url:
        now = time.monotonic()
        with _cache_lock:
            hit = _cache.get(url)
        if hit and not refresh and now - hit[0] < METADATA_TTL_S:
            out = dict(hit[1])
        else:
            out = parse_metadata(_fetch(svc, url), str(svc.rt("saml", "idp_entity_id") or "").strip())
            with _cache_lock:
                _cache[url] = (now, dict(out))
    if str(svc.rt("saml", "idp_entity_id") or "").strip():
        out["entity_id"] = str(svc.rt("saml", "idp_entity_id")).strip()
    if str(svc.rt("saml", "idp_sso_url") or "").strip():
        out["sso_url"] = str(svc.rt("saml", "idp_sso_url")).strip()
    manual = str(svc.rt("saml", "idp_cert") or "").strip()
    if manual:
        out["certs"] = [_cert_pem(manual)]
    if not out["entity_id"] or not out["sso_url"] or not out["certs"]:
        raise SamlError("не хватает настроек поставщика: идентификатор, адрес входа и сертификат подписи "
                        "(или адрес описания поставщика)")
    _check_url(out["sso_url"], "адрес входа поставщика")
    return out


def _base(svc, request) -> str:
    public = str(svc.rt("server", "public_url") or "").strip().rstrip("/")
    if public:
        return public
    return str(request.base_url).rstrip("/") if request is not None else ""


def sp_entity_id(svc, request=None) -> str:
    fixed = str(svc.rt("saml", "sp_entity_id") or "").strip()
    return fixed or _base(svc, request) + "/api/auth/saml/metadata"


def acs_url(svc, request=None) -> str:
    fixed = str(svc.rt("saml", "acs_url") or "").strip()
    return fixed or _base(svc, request) + "/api/auth/saml/acs"


def sp_metadata(svc, request=None) -> str:
    """Описание архива для поставщика (импортируется в ADFS, Entra ID, Keycloak)."""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<md:EntityDescriptor xmlns:md="{NS["md"]}" entityID={quoteattr(sp_entity_id(svc, request))}>\n'
            f'  <md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" '
            f'protocolSupportEnumeration="{NS["samlp"]}">\n'
            '    <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified</md:NameIDFormat>\n'
            '    <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>\n'
            f'    <md:AssertionConsumerService Binding="{BINDING_POST}" '
            f'Location={quoteattr(acs_url(svc, request))} index="0" isDefault="true"/>\n'
            '  </md:SPSSODescriptor>\n'
            '</md:EntityDescriptor>\n')


# ---------------------------------------------------------------------------
#  Вход
# ---------------------------------------------------------------------------
def _sign(svc, payload: Dict) -> str:
    import json
    from .security import sign_value
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return sign_value(raw, svc.cfg.secret_key())


def _unsign(svc, value: str) -> Optional[Dict]:
    import json
    from .security import unsign_value
    raw = unsign_value(value or "", svc.cfg.secret_key())
    if not raw:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or time.time() - float(data.get("t") or 0) > STATE_TTL_S:
        return None
    return data


def start(svc, request) -> Tuple[str, str]:
    """→ (адрес входа у поставщика с запросом, значение cookie)."""
    conf = idp(svc)
    req_id = "_" + secrets.token_hex(20)
    relay = secrets.token_urlsafe(24)
    xml = (f'<samlp:AuthnRequest xmlns:samlp="{NS["samlp"]}" xmlns:saml="{NS["saml"]}" ID="{req_id}" '
           f'Version="2.0" IssueInstant="{_iso(time.time())}" Destination={quoteattr(conf["sso_url"])} '
           f'AssertionConsumerServiceURL={quoteattr(acs_url(svc, request))} ProtocolBinding="{BINDING_POST}">'
           f'<saml:Issuer>{escape(sp_entity_id(svc, request))}</saml:Issuer>'
           '<samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified" AllowCreate="true"/>'
           '</samlp:AuthnRequest>')
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    deflated = comp.compress(xml.encode("utf-8")) + comp.flush()
    query = urllib.parse.urlencode({"SAMLRequest": base64.b64encode(deflated).decode("ascii"), "RelayState": relay})
    sep = "&" if "?" in conf["sso_url"] else "?"
    return conf["sso_url"] + sep + query, _sign(svc, {"i": req_id, "r": relay, "t": time.time()})


def _verify(xml: bytes, certs: List[str]):
    """Проверить подпись ответа или утверждения → подписанное утверждение (элемент lxml)."""
    from signxml import SignatureConfiguration, XMLVerifier
    from signxml.exceptions import InvalidInput, InvalidSignature
    root = _parse(xml)
    if root.tag != _P + "Response":
        raise SamlError("это не ответ SAML (нет samlp:Response)")
    if root.find("saml:EncryptedAssertion", NS) is not None:
        raise SamlError("зашифрованные утверждения не поддерживаются — выключите шифрование утверждений для "
                        "архива у поставщика (ответ всё равно идёт по https)")
    assertions = root.findall("saml:Assertion", NS)
    if len(assertions) != 1 or root.findall(".//saml:Assertion", NS) != assertions:
        raise SamlError("в ответе должно быть ровно одно утверждение")
    if root.find("ds:Signature", NS) is not None:
        location = "./"
    elif assertions[0].find("ds:Signature", NS) is not None:
        location = "./{%s}Assertion/" % NS["saml"]
    else:
        raise SamlError("ответ поставщика не подписан — включите подпись утверждений (или ответа)")
    config = SignatureConfiguration(location=location, ignore_ambiguous_key_info=True)
    last = ""
    for pem in certs:
        try:
            res = XMLVerifier().verify(xml, x509_cert=pem, expect_config=config)
        except (InvalidSignature, InvalidInput) as exc:
            last = str(exc)
            continue
        signed = res.signed_xml
        if signed.tag == _P + "Response":
            inner = signed.findall("saml:Assertion", NS)
            if len(inner) != 1:
                raise SamlError("в подписанном ответе должно быть ровно одно утверждение")
            return signed, inner[0]
        if signed.tag == _A + "Assertion":
            return root, signed
        raise SamlError("подписан не ответ и не утверждение")
    raise SamlError(f"подпись ответа не сходится с сертификатом поставщика: {last[:200]}")


def _attributes(assertion) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for attr in assertion.findall("saml:AttributeStatement/saml:Attribute", NS):
        names = {attr.get("Name") or "", attr.get("FriendlyName") or ""} - {""}
        vals = [_text(v) for v in attr.findall("saml:AttributeValue", NS)]
        vals = [v for v in vals if v]
        for n in names | {n.lower() for n in names}:
            out.setdefault(n, []).extend(vals)
    return out


def consume(svc, saml_response: str, request=None, now: Optional[float] = None) -> Dict:
    """Ответ поставщика (POST на /acs) → проверенный пользователь: username, email, role, in_response_to."""
    now = time.time() if now is None else now
    raw = re.sub(r"\s+", "", saml_response or "")
    if not raw or len(raw) > MAX_RESPONSE * 4 // 3 + 8:
        raise SamlError("нет ответа поставщика (SAMLResponse)")
    try:
        xml = base64.b64decode(raw, validate=True)
    except ValueError:
        raise SamlError("ответ поставщика не в base64") from None
    conf = idp(svc)
    envelope = _parse(xml)
    status = envelope.find("samlp:Status/samlp:StatusCode", NS)
    if status is not None and status.get("Value") != STATUS_SUCCESS:
        sub = status.find("samlp:StatusCode", NS)
        msg = _text(envelope.find("samlp:Status/samlp:StatusMessage", NS))
        raise SamlError(f"поставщик отказал: {(sub.get('Value') if sub is not None else status.get('Value'))} {msg}"
                        .strip())
    response, assertion = _verify(xml, conf["certs"])
    acs = acs_url(svc, request)
    # конверт (подписан — если подпись на ответе): адресат и издатель
    dest = response.get("Destination")
    if response.tag == _P + "Response" and dest and dest != acs:
        raise SamlError(f"ответ адресован не архиву: {dest}")
    issuer = _text(assertion.find("saml:Issuer", NS))
    if issuer != conf["entity_id"]:
        raise SamlError(f"издатель «{issuer}» не тот, что в настройках («{conf['entity_id']}»)")
    cond = assertion.find("saml:Conditions", NS)
    if cond is None:
        raise SamlError("в утверждении нет условий (Conditions)")
    nb, noa = _time(cond.get("NotBefore") or ""), _time(cond.get("NotOnOrAfter") or "")
    if nb is not None and now + CLOCK_SKEW_S < nb:
        raise SamlError("утверждение ещё не действует (часы сервера или поставщика спешат?)")
    if noa is None or now - CLOCK_SKEW_S >= noa:
        raise SamlError("срок утверждения истёк")
    audiences = [_text(a) for a in cond.findall("saml:AudienceRestriction/saml:Audience", NS)]
    if sp_entity_id(svc, request) not in audiences:
        raise SamlError(f"утверждение выдано не архиву (Audience: {', '.join(audiences) or 'нет'}); "
                        f"идентификатор архива у поставщика — {sp_entity_id(svc, request)}")
    subject = assertion.find("saml:Subject", NS)
    in_resp, ok_bearer = "", False
    for sc in (subject.findall("saml:SubjectConfirmation", NS) if subject is not None else []):
        if sc.get("Method") != "urn:oasis:names:tc:SAML:2.0:cm:bearer":
            continue
        data = sc.find("saml:SubjectConfirmationData", NS)
        if data is None:
            continue
        until = _time(data.get("NotOnOrAfter") or "")
        if data.get("Recipient") != acs or until is None or now - CLOCK_SKEW_S >= until:
            continue
        ok_bearer, in_resp = True, data.get("InResponseTo") or ""
        break
    if not ok_bearer:
        raise SamlError(f"нет подтверждения субъекта для этого адреса возврата ({acs}) или его срок истёк")
    if not in_resp:
        raise SamlError("вход, начатый поставщиком (без InResponseTo), не принимается — входите кнопкой на "
                        "странице архива")
    if assertion.find("saml:AuthnStatement", NS) is None:
        raise SamlError("в утверждении нет сведений о входе (AuthnStatement)")
    aid = assertion.get("ID") or ""
    if not aid or not remember_once(svc, "saml_assertion", aid, max(noa - now, 0) + CLOCK_SKEW_S + 60):
        raise SamlError("это утверждение уже использовано (повтор ответа)")
    attrs = _attributes(assertion)
    name_id = _text(subject.find("saml:NameID", NS)) if subject is not None else ""
    uattr = str(svc.rt("saml", "username_attribute") or "").strip()
    username = ((attrs.get(uattr) or attrs.get(uattr.lower()) or [""])[0] if uattr else name_id).strip()[:200]
    if not username:
        raise SamlError("поставщик не передал имя пользователя" + (f" (атрибут {uattr})" if uattr else " (NameID)"))
    eattr = str(svc.rt("saml", "email_attribute") or "").strip()
    email = ""
    for n in ([eattr] if eattr else list(EMAIL_ATTRS)):
        got = attrs.get(n) or attrs.get(n.lower())
        if got:
            email = got[0]
            break
    if not email and "@" in name_id:
        email = name_id
    rattr = str(svc.rt("saml", "role_attribute") or "groups").strip()
    groups = attrs.get(rattr) or attrs.get(rattr.lower()) or []
    role = role_from(groups, svc.rt("saml", "admin_values"), svc.rt("saml", "operator_values"),
                     bool(svc.rt("saml", "employee_login")), email.strip().lower())
    return {"username": username, "email": email.strip().lower(), "role": role, "in_response_to": in_resp,
            "groups": groups[:50], "assertion_id": aid}


def park(svc, info: Dict) -> str:
    """Проверенный вход ждёт браузер (POST от поставщика приходит без cookie) — одноразовый номер."""
    ticket = secrets.token_urlsafe(32)
    remember_once(svc, "saml_ticket", ticket, TICKET_TTL_S, info)
    return ticket


def claim(svc, ticket: str, cookie: str) -> Dict:
    """Браузер вернулся с номером: вход начат в этом же браузере → данные пользователя."""
    info = take_once(svc, "saml_ticket", ticket or "")
    if info is None:
        raise SamlError("вход устарел или уже использован — начните заново")
    saved = _unsign(svc, cookie)
    if saved is None:
        raise SamlError("вход начат не в этом браузере или устарел (нет cookie состояния)")
    if not hmac.compare_digest(str(saved.get("i") or ""), str(info.get("in_response_to") or "")):
        raise SamlError("ответ поставщика — на чужой запрос входа (InResponseTo не совпадает)")
    return info


def ensure_user(svc, info: Dict) -> Optional[Dict]:
    return _sso_ensure_user(svc, info, "saml", error=SamlError)


def check(svc, request=None) -> Dict:
    """Проверка из «Настроек»: описание поставщика, сертификаты, что указать у поставщика."""
    clear_cache()
    out: Dict[str, Any] = {"sp_entity_id": sp_entity_id(svc, request), "acs_url": acs_url(svc, request),
                           "metadata_url": _base(svc, request) + "/api/auth/saml/metadata",
                           "library": available()}
    if not available():
        return {**out, "ok": False, "error": "не установлен пакет signxml (pip install signxml)"}
    try:
        conf = idp(svc, refresh=True)
        certs = [cert_info(p) for p in conf["certs"]]
        soon = min((c["days_left"] for c in certs), default=999)
        warn = ("сертификат подписи поставщика истёк" if soon < 0 else
                f"сертификат подписи поставщика истекает через {soon} дн." if soon < 30 else "")
        return {**out, "ok": True, "entity_id": conf["entity_id"], "sso_url": conf["sso_url"], "certs": certs,
                "cert_warning": warn}
    except SamlError as exc:
        return {**out, "ok": False, "error": str(exc)}
