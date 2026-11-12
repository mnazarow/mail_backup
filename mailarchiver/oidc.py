"""
Единый вход через OpenID Connect (1.22.0): Keycloak, Microsoft Entra ID (Azure AD), ADFS, Google Workspace,
Яндекс 360 для бизнеса, Authentik, Okta и другие поставщики удостоверений.

Поток — Authorization Code с PKCE (S256):

1. ``/api/auth/oidc/start`` — случайные ``state``, ``nonce`` и ``code_verifier`` кладутся в подписанный cookie
   (10 минут, только этот путь), браузер уходит на страницу входа поставщика;
2. ``/api/auth/oidc/callback`` — ``state`` сверяется с cookie, код меняется на токены (с ``code_verifier`` и
   секретом клиента), ``id_token`` проверяется: подпись по ключам поставщика (JWKS; RS256/384/512,
   PS256/384/512, ES256/384, EdDSA), издатель, получатель (``aud``/``azp``), срок, ``nonce``;
3. роль — по значениям утверждения (``oidc.role_claim``, по умолчанию ``groups``; путь через точку, например
   ``realm_access.roles``): совпало с «группами администраторов» — администратор, «операторов» — оператор;
   иначе — по желанию вход сотрудника в архив своих ящиков по адресу ``email``.

Учётная запись архива заводится сама (``users.auth_source = 'oidc'``); локальный пользователь с тем же
логином через единый вход не входит. Второй фактор — у поставщика (локальный 2FA к таким входам не
применяется). Адреса поставщика — только https (кроме этой машины).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .logging_setup import get_logger
from .sso import SsoError, ensure_user as _sso_ensure_user, role_from, values as _sso_values

log = get_logger("oidc")

COOKIE = "ma_oidc"
COOKIE_PATH = "/api/auth/oidc"
STATE_TTL_S = 600
CLOCK_SKEW_S = 120
TIMEOUT = 15
MAX_RESPONSE = 1024 * 1024
_cache: Dict[str, Tuple[float, Any]] = {}
_cache_lock = threading.Lock()
DISCOVERY_TTL_S = 3600


class OidcError(SsoError):
    """Вход не удался — текст для журнала и аудита (пользователю — общий ответ)."""


def enabled(svc) -> bool:
    return bool(svc.rt("oidc", "enabled")) and bool(str(svc.rt("oidc", "issuer") or "").strip()) \
        and bool(str(svc.rt("oidc", "client_id") or "").strip())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_dec(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _local(host: str) -> bool:
    return (host or "").lower() in ("localhost", "127.0.0.1", "::1", "[::1]")


def _check_url(url: str, what: str) -> str:
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise OidcError(f"{what}: нужен адрес https://")
    if parts.scheme == "http" and not _local(parts.hostname):
        raise OidcError(f"{what}: только https:// (кроме этой машины)")
    return url


def _http(svc, url: str, data: Optional[Dict[str, str]] = None, auth: Optional[Tuple[str, str]] = None) -> Any:
    _check_url(url, "адрес поставщика")
    headers = {"Accept": "application/json", "User-Agent": "MailArchiver"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if auth is not None:
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"{urllib.parse.quote(auth[0], safe='')}:{urllib.parse.quote(auth[1], safe='')}".encode()).decode()
    ctx = ssl.create_default_context()
    if not bool(svc.rt("oidc", "verify_ssl") if svc.rt("oidc", "verify_ssl") is not None else True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):        # перенаправления с секретом клиента не нужны
            return None
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read(MAX_RESPONSE)
    except urllib.error.HTTPError as exc:
        text = exc.read(2000).decode("utf-8", "replace") if exc.fp else ""
        raise OidcError(f"{urllib.parse.urlsplit(url).netloc}: HTTP {exc.code} {text[:300]}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise OidcError(f"нет связи с {urllib.parse.urlsplit(url).netloc}: {getattr(exc, 'reason', exc)}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise OidcError("поставщик прислал не JSON") from None


def _cached(key: str, ttl: float, fn):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def discovery(svc) -> Dict:
    issuer = str(svc.rt("oidc", "issuer") or "").strip().rstrip("/")
    _check_url(issuer, "издатель (issuer)")

    def load():
        doc = _http(svc, issuer + "/.well-known/openid-configuration")
        if str(doc.get("issuer") or "").rstrip("/") != issuer:
            raise OidcError(f"издатель в описании поставщика «{doc.get('issuer')}» не совпадает с настройкой «{issuer}»")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not doc.get(k):
                raise OidcError(f"в описании поставщика нет {k}")
            _check_url(doc[k], k)
        return doc
    return _cached("disc:" + issuer, DISCOVERY_TTL_S, load)


def _jwks(svc, uri: str, refresh: bool = False) -> List[Dict]:
    if refresh:
        with _cache_lock:
            _cache.pop("jwks:" + uri, None)
    return list(_cached("jwks:" + uri, DISCOVERY_TTL_S, lambda: _http(svc, uri)).get("keys") or [])


# ---------------------------------------------------------------------------
#  Проверка id_token
# ---------------------------------------------------------------------------
_HASHES = {"256": "SHA256", "384": "SHA384", "512": "SHA512"}
ALLOWED_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "EdDSA")


def _public_key(jwk: Dict):
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
    kty = jwk.get("kty")
    if kty == "RSA":
        n = int.from_bytes(_b64url_dec(jwk["n"]), "big")
        e = int.from_bytes(_b64url_dec(jwk["e"]), "big")
        return rsa.RSAPublicNumbers(e, n).public_key()
    if kty == "EC":
        curve = {"P-256": ec.SECP256R1(), "P-384": ec.SECP384R1()}.get(jwk.get("crv"))
        if curve is None:
            raise OidcError(f"кривая {jwk.get('crv')} не поддерживается")
        x = int.from_bytes(_b64url_dec(jwk["x"]), "big")
        y = int.from_bytes(_b64url_dec(jwk["y"]), "big")
        return ec.EllipticCurvePublicNumbers(x, y, curve).public_key()
    if kty == "OKP" and jwk.get("crv") == "Ed25519":
        return ed25519.Ed25519PublicKey.from_public_bytes(_b64url_dec(jwk["x"]))
    raise OidcError(f"тип ключа {kty} не поддерживается")


def _verify_sig(alg: str, key, signing_input: bytes, sig: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, utils
    try:
        if alg.startswith("RS"):
            key.verify(sig, signing_input, padding.PKCS1v15(), getattr(hashes, _HASHES[alg[2:]])())
        elif alg.startswith("PS"):
            h = getattr(hashes, _HASHES[alg[2:]])()
            key.verify(sig, signing_input, padding.PSS(mgf=padding.MGF1(h), salt_length=h.digest_size), h)
        elif alg.startswith("ES"):
            half = len(sig) // 2
            der = utils.encode_dss_signature(int.from_bytes(sig[:half], "big"), int.from_bytes(sig[half:], "big"))
            key.verify(der, signing_input, ec.ECDSA(getattr(hashes, _HASHES[alg[2:]])()))
        elif alg == "EdDSA":
            key.verify(sig, signing_input)
        else:
            raise OidcError(f"алгоритм {alg} не поддерживается")
    except InvalidSignature:
        raise OidcError("подпись id_token неверна") from None


def verify_id_token(svc, token: str, nonce: str, now: Optional[float] = None) -> Dict:
    """Проверить id_token и вернуть его утверждения (claims)."""
    try:
        head_b, body_b, sig_b = token.split(".")
        header = json.loads(_b64url_dec(head_b))
        claims = json.loads(_b64url_dec(body_b))
        sig = _b64url_dec(sig_b)
    except (ValueError, TypeError):
        raise OidcError("id_token повреждён") from None
    alg = str(header.get("alg") or "")
    if alg not in ALLOWED_ALGS:
        raise OidcError(f"алгоритм подписи id_token «{alg}» не принимается")
    doc = discovery(svc)
    kid = header.get("kid")

    def pick(keys):
        cand = [k for k in keys if (not kid or k.get("kid") == kid) and k.get("use", "sig") == "sig"]
        return cand[0] if cand else None
    jwk = pick(_jwks(svc, doc["jwks_uri"])) or pick(_jwks(svc, doc["jwks_uri"], refresh=True))
    if jwk is None:
        raise OidcError(f"ключа {kid or ''} нет в JWKS поставщика")
    _verify_sig(alg, _public_key(jwk), f"{head_b}.{body_b}".encode("ascii"), sig)
    now = time.time() if now is None else now
    issuer = str(svc.rt("oidc", "issuer") or "").strip().rstrip("/")
    if str(claims.get("iss") or "").rstrip("/") != issuer:
        raise OidcError(f"издатель id_token «{claims.get('iss')}» — не «{issuer}»")
    client_id = str(svc.rt("oidc", "client_id") or "").strip()
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if client_id not in auds:
        raise OidcError("id_token выдан другому приложению (aud)")
    if len(auds) > 1 and claims.get("azp") not in (None, client_id):
        raise OidcError("id_token выдан другому приложению (azp)")
    try:
        exp = float(claims["exp"])
    except (KeyError, TypeError, ValueError):
        raise OidcError("в id_token нет срока (exp)") from None
    if exp < now - CLOCK_SKEW_S:
        raise OidcError("срок id_token истёк")
    if float(claims.get("iat") or now) > now + CLOCK_SKEW_S:
        raise OidcError("id_token выдан в будущем — часы сервера архива или поставщика спешат")
    if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce or ""):
        raise OidcError("nonce id_token не совпадает (повтор чужого входа?)")
    return claims


# ---------------------------------------------------------------------------
#  Поток входа
# ---------------------------------------------------------------------------
def redirect_uri(svc, request) -> str:
    fixed = str(svc.rt("oidc", "redirect_uri") or "").strip()
    if fixed:
        return fixed
    public = str(svc.rt("server", "public_url") or "").strip().rstrip("/")
    if public:
        return public + "/api/auth/oidc/callback"
    return str(request.url_for("oidc_callback"))


def _sign(svc, payload: Dict) -> str:
    from .security import sign_value
    raw = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return sign_value(raw, svc.cfg.secret_key())


def _unsign(svc, value: str) -> Optional[Dict]:
    from .security import unsign_value
    raw = unsign_value(value or "", svc.cfg.secret_key())
    if not raw:
        return None
    try:
        data = json.loads(_b64url_dec(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or time.time() - float(data.get("t") or 0) > STATE_TTL_S:
        return None
    return data


def start(svc, request) -> Tuple[str, str]:
    """→ (адрес страницы входа поставщика, значение cookie с state/nonce/verifier)."""
    doc = discovery(svc)
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    params = {"response_type": "code", "client_id": str(svc.rt("oidc", "client_id") or "").strip(),
              "redirect_uri": redirect_uri(svc, request),
              "scope": str(svc.rt("oidc", "scopes") or "openid email profile").strip() or "openid",
              "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    if "openid" not in params["scope"].split():
        params["scope"] = "openid " + params["scope"]
    sep = "&" if "?" in doc["authorization_endpoint"] else "?"
    url = doc["authorization_endpoint"] + sep + urllib.parse.urlencode(params)
    return url, _sign(svc, {"s": state, "n": nonce, "v": verifier, "t": time.time()})


def _claim(claims: Dict, path: str):
    cur: Any = claims
    for part in (path or "").split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _values(v) -> List[str]:
    return _sso_values(v)


def role_of(svc, claims: Dict) -> str:
    email = claims.get("email") if claims.get("email_verified", True) else ""
    return role_from(_values(_claim(claims, str(svc.rt("oidc", "role_claim") or "groups"))),
                     svc.rt("oidc", "admin_values"), svc.rt("oidc", "operator_values"),
                     bool(svc.rt("oidc", "employee_login")), str(email or ""))


def finish(svc, query: Dict[str, str], cookie: str, request) -> Dict:
    """Код от поставщика → проверенные утверждения пользователя и роль."""
    if query.get("error"):
        raise OidcError(f"поставщик отказал: {query.get('error')} {query.get('error_description', '')}".strip())
    saved = _unsign(svc, cookie)
    if saved is None:
        raise OidcError("вход не начат здесь или устарел (нет cookie состояния) — начните заново")
    if not hmac.compare_digest(str(query.get("state") or ""), str(saved.get("s") or "")):
        raise OidcError("state не совпадает (подделка ответа?)")
    code = str(query.get("code") or "")
    if not code:
        raise OidcError("поставщик не вернул код")
    doc = discovery(svc)
    client_id = str(svc.rt("oidc", "client_id") or "").strip()
    secret = str(svc.rt("oidc", "client_secret") or "")
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(svc, request),
            "code_verifier": saved["v"], "client_id": client_id}
    methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if secret and "client_secret_basic" in methods:
        tok = _http(svc, doc["token_endpoint"], data, auth=(client_id, secret))
    else:
        if secret:
            data["client_secret"] = secret
        tok = _http(svc, doc["token_endpoint"], data)
    id_token = tok.get("id_token")
    if not id_token:
        raise OidcError("поставщик не вернул id_token (нужна область openid)")
    claims = verify_id_token(svc, id_token, saved["n"])
    if bool(svc.rt("oidc", "use_userinfo")) and doc.get("userinfo_endpoint") and tok.get("access_token"):
        try:
            extra = _http_bearer(svc, doc["userinfo_endpoint"], tok["access_token"])
            if extra.get("sub") == claims.get("sub"):
                claims = {**extra, **claims}
        except OidcError as exc:
            log.info("userinfo не прочитан: %s", exc)
    ucl = str(svc.rt("oidc", "username_claim") or "preferred_username")
    username = str(_claim(claims, ucl) or claims.get("email") or claims.get("sub") or "").strip()[:200]
    if not username:
        raise OidcError(f"в данных пользователя нет {ucl}")
    return {"username": username, "email": str(claims.get("email") or "").strip().lower(),
            "name": str(claims.get("name") or ""), "role": role_of(svc, claims), "sub": claims.get("sub")}


def _http_bearer(svc, url: str, token: str) -> Dict:
    _check_url(url, "userinfo")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - адрес проверен выше
            return json.loads(resp.read(MAX_RESPONSE).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OidcError(f"userinfo: {exc}") from None


def check(svc) -> Dict:
    """Проверка из «Настроек»: описание поставщика и его ключи."""
    clear_cache()
    try:
        doc = discovery(svc)
        keys = _jwks(svc, doc["jwks_uri"])
        return {"ok": True, "issuer": doc["issuer"], "authorization_endpoint": doc["authorization_endpoint"],
                "keys": len(keys), "algs": doc.get("id_token_signing_alg_values_supported") or [],
                "pkce": "S256" in (doc.get("code_challenge_methods_supported") or ["S256"])}
    except OidcError as exc:
        return {"ok": False, "error": str(exc)}


def ensure_user(svc, info: Dict) -> Optional[Dict]:
    """Учётная запись архива для пользователя поставщика. None — прав на архив нет (см. :mod:`sso`)."""
    return _sso_ensure_user(svc, info, "oidc", error=OidcError)
