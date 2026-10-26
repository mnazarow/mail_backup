"""
HTTP для драйверов почтовых серверов — без сторонних библиотек.

* :class:`HttpClient` — запросы к API сервера: JSON и текст, тайм-ауты,
  проверка сертификата (обычная или по отпечатку SHA-256 — для своего сервера с
  самоподписанным сертификатом), повтор при «слишком много запросов» (429) и —
  только для чтения — при временных отказах 502/503/504 (по ``Retry-After``,
  не дольше :data:`MAX_RETRY_WAIT_S`).
  Без шифрования — только на эту же машину (127.0.0.1) или если администратор
  явно разрешил это в подключении.
* :class:`ApiLog` — журнал вызовов одной сессии (что запрошено, что ответил
  сервер, сколько заняло); секреты (заголовки авторизации, пароли, токены —
  и в записи XML, JSON, URL; элементы <Password> и ключи Password словарей)
  в журнал не попадают.
* OAuth2: токен по учётным данным приложения (client credentials) и по
  подписанному JWT (сервисный аккаунт Google, сертификат приложения Microsoft) —
  с кэшем до истечения.
"""
from __future__ import annotations

import base64
import hashlib
import html
import http.client
import json
import re
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..logging_setup import get_logger
from .base import NotFound, ServerAuthError, ServerError, ServerUnreachable

log = get_logger("mailservers.http")

#: Сколько раз повторять запрос при 429/502/503/504 и сколько ждать всего.
MAX_RETRIES = 3
MAX_RETRY_WAIT_S = 30.0
DEFAULT_TIMEOUT_S = 30.0
#: Предел ответа, который читаем в память (большие отчёты — потоково не нужны).
MAX_BODY = 64 * 1024 * 1024
#: Поля, значения которых в журнале заменяются на «••••»: точные имена и окончания имён.
SECRET_NAMES = {"pass", "passwd", "authorization", "assertion", "subject_token", "x-token", "x-api-key", "key"}
SECRET_SUFFIXES = ("password", "secret", "token", "apikey", "api_key", "_key", "authtoken")


def _secret_name(name: Any) -> bool:
    low = str(name).lower()
    return low in SECRET_NAMES or low.endswith(SECRET_SUFFIXES)


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


def redact(value: Any) -> Any:
    """Копия данных без секретов (для журнала)."""
    if isinstance(value, dict):
        return {k: ("••••" if _secret_name(k) and v not in (None, "") else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


#: Значения секретных элементов XML (<Password>…</Password>) и ключей словарей CommuniGate (Password = "…";).
_XML_SECRET = re.compile(r"<((?:[\w-]+:)?\w*(?:[Pp]assword|[Ss]ecret|[Tt]oken))(\s[^>]*)?>(.*?)</\1>", re.S)
_CGP_SECRET = re.compile(r"(\b\w*(?:Password|Secret)\w*\s*=\s*)(\"(?:[^\"\\]|\\.)*\"|[^;}\s]+)")


def _forms(secret: str) -> List[str]:
    """Секрет и его записи в теле запроса: экранированная для XML/HTML, для JSON, для URL."""
    forms = {secret, html.escape(secret, quote=False), html.escape(secret, quote=True),
             json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret)[1:-1],
             urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}
    return sorted(forms, key=len, reverse=True)


def redact_markup(text: str) -> str:
    """Тексты XML и словарей CommuniGate — без значений паролей, секретов и токенов."""
    out = _XML_SECRET.sub(lambda m: f"<{m.group(1)}{m.group(2) or ''}>••••</{m.group(1)}>"
                          if m.group(3).strip() else m.group(0), text)
    return _CGP_SECRET.sub(lambda m: m.group(1) + "••••", out)


def redact_text(text: str, secrets: Sequence[str] = ()) -> str:
    out = text or ""
    for s in secrets:
        if s and len(s) >= 4:
            for form in _forms(s):
                out = out.replace(form, "••••")
    return out


@dataclass
class Call:
    method: str
    url: str
    status: int
    ms: int
    request: str = ""
    response: str = ""
    error: str = ""


class ApiLog:
    """Журнал вызовов одной сессии работы с сервером (пишется в базу по окончании)."""

    TEXT_LIMIT = 4000

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        self.calls: List[Call] = []
        self.secrets = [s for s in secrets if s]
        self.lock = threading.Lock()

    def add(self, call: Call) -> None:
        call.request = redact_text(call.request, self.secrets)[:self.TEXT_LIMIT]
        call.response = redact_text(call.response, self.secrets)[:self.TEXT_LIMIT]
        call.url = redact_text(call.url, self.secrets)
        with self.lock:
            self.calls.append(call)

    def note(self, text: str) -> None:
        self.add(Call("NOTE", "", 0, 0, response=text))

    def secret(self, value: str) -> None:
        """Ещё один секрет, полученный по ходу работы (токен сеанса), — в журнале не показывать."""
        if value and value not in self.secrets:
            self.secrets.append(value)

    def transcript(self) -> str:
        lines = []
        for c in self.calls:
            if c.method == "NOTE":
                lines.append(f"# {c.response}")
                continue
            head = f"{c.method} {c.url} → {c.status or '—'} ({c.ms} мс)"
            if c.error:
                head += f" ошибка: {c.error}"
            lines.append(head)
            if c.request:
                lines.append("  > " + c.request.replace("\n", "\n  > "))
            if c.response:
                lines.append("  < " + c.response.replace("\n", "\n  < "))
        return "\n".join(lines)

    @property
    def ok(self) -> bool:
        return not any(c.error for c in self.calls)


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ServerError(f"Сервер прислал не JSON: {self.text()[:200]!r}") from exc

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def cert_sha256(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def norm_fingerprint(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch in "0123456789abcdef")


class HttpClient:
    """Запросы к одному серверу API (``base_url`` — схема, адрес и, если надо, путь)."""

    def __init__(self, base_url: str, *, api: Optional[ApiLog] = None, verify_tls: bool = True,
                 cert_sha256: str = "", allow_plain: bool = False, timeout: float = DEFAULT_TIMEOUT_S,
                 headers: Optional[Dict[str, str]] = None, server_label: str = "сервер") -> None:
        parsed = urllib.parse.urlsplit(base_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ServerError(f"Адрес API указан неверно: «{base_url}».")
        if parsed.scheme == "http" and not allow_plain and not _is_loopback(parsed.hostname):
            raise ServerError(f"Подключение к {parsed.hostname} без шифрования (http) не разрешено: пароль или "
                              f"токен ушли бы по сети открытым текстом.",
                              hint="Укажите https-адрес или, только для доверенной сети, включите «Разрешить без "
                                   "шифрования» в подключении.")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.prefix = parsed.path.rstrip("/")
        self.api = api or ApiLog()
        self.verify_tls = verify_tls
        self.pin = norm_fingerprint(cert_sha256)
        self.timeout = timeout
        self.headers = dict(headers or {})
        self.server_label = server_label
        #: отпечаток сертификата, который прислал сервер (для «Доверять этому сертификату»)
        self.seen_fingerprint = ""

    # -- соединение ----------------------------------------------------------------
    def _connection(self):
        if self.scheme == "http":
            return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        ctx = ssl.create_default_context()
        if self.pin or not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=ctx)
        return conn

    def _check_pin(self, conn) -> None:
        if self.scheme != "https":
            return
        try:
            der = conn.sock.getpeercert(binary_form=True)
        except Exception:  # noqa: BLE001
            der = None
        if der:
            self.seen_fingerprint = cert_sha256(der)
        if self.pin and self.seen_fingerprint != self.pin:
            raise ServerUnreachable(
                f"Сертификат {self.host} не тот, которому разрешено доверять (SHA-256 "
                f"{self.seen_fingerprint or 'неизвестен'}).",
                hint="Если сертификат на сервере меняли — сверьте новый отпечаток и доверьтесь ему заново в "
                     "подключении; если нет — соединение могли перехватить.")

    # -- запрос ----------------------------------------------------------------------
    def url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            full = path
        else:
            full = f"{self.scheme}://{self.host}:{self.port}{self.prefix}{path if path.startswith('/') else '/' + path}"
        if params:
            q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            full += ("&" if "?" in full else "?") + q
        return full

    def request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, json_body: Any = None,
                data: Optional[bytes] = None, form: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None, expect: Sequence[int] = (200, 201, 202, 204),
                log_body: bool = True, timeout: Optional[float] = None, idempotent: Optional[bool] = None,
                log_url: Optional[str] = None) -> Response:
        """Запрос; ответ с кодом не из ``expect`` — исключение с понятным текстом.

        Повтор при 502/503/504 — только для запросов, которые можно безопасно повторить: GET, HEAD (или
        ``idempotent=True`` — например, чтение через POST у JSON-RPC). Ответ 502/504 не значит, что сервер ничего
        не сделал: повтор удаления или создания дал бы «не найдено» / «уже есть» про уже сделанное. 429 —
        запрос не принят — повторяется для любого метода. ``log_url`` — что записать в журнал вместо адреса
        (адрес сам — ссылка доступа).
        """
        url = self.url(path, params)
        if idempotent is None:
            idempotent = method.upper() in ("GET", "HEAD", "OPTIONS")
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname != self.host:
            raise ServerError(f"Запрос к чужому адресу ({parsed.hostname}) отклонён.")
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        hdrs = {"Accept": "application/json", "User-Agent": "MailArchiver"}
        hdrs.update(self.headers)
        hdrs.update(headers or {})
        shown_req = ""
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
            shown_req = json.dumps(redact(json_body), ensure_ascii=False)
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
            shown_req = urllib.parse.urlencode(redact(form))
        elif data is not None:
            shown_req = redact_markup(data[:2000].decode("utf-8", errors="replace"))
        if not log_body:
            shown_req = "(не показывается)" if shown_req else ""
        shown_url = log_url if log_url is not None else url
        waited = 0.0
        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            conn = self._connection()
            if timeout:
                conn.timeout = timeout
            try:
                conn.connect()
                self._check_pin(conn)
                conn.request(method, target, body=data, headers=hdrs)
                resp = conn.getresponse()
                body = resp.read(MAX_BODY + 1)
                status = resp.status
                headers_out = {k.lower(): v for k, v in resp.getheaders()}
            except ServerError:
                self.api.add(Call(method, shown_url, 0, int((time.monotonic() - started) * 1000), shown_req,
                                  error="сертификат не совпал"))
                raise
            except ssl.SSLError as exc:
                self.api.add(Call(method, shown_url, 0, int((time.monotonic() - started) * 1000), shown_req,
                                  error=str(exc)))
                raise ServerUnreachable(
                    f"{self.server_label}: защищённое соединение с {self.host}:{self.port} не установлено ({exc}).",
                    hint="Если у сервера самоподписанный сертификат — «Проверить подключение» покажет его "
                         "отпечаток, им можно разрешить доверие.", cause=exc) from exc
            except (OSError, socket.timeout, http.client.HTTPException) as exc:
                self.api.add(Call(method, shown_url, 0, int((time.monotonic() - started) * 1000), shown_req,
                                  error=str(exc) or type(exc).__name__))
                raise ServerUnreachable(
                    f"{self.server_label}: нет связи с {self.host}:{self.port} ({exc or type(exc).__name__}).",
                    hint="Проверьте адрес и порт, межсетевой экран и что служба API на сервере запущена.",
                    retryable=True, cause=exc) from exc
            finally:
                conn.close()
            ms = int((time.monotonic() - started) * 1000)
            if len(body) > MAX_BODY:
                raise ServerError(f"{self.server_label}: ответ больше {MAX_BODY // 1048576} МБ — не принят.")
            shown_resp = _shown(body) if log_body else "(не показывается)"
            self.api.add(Call(method, shown_url, status, ms, shown_req, shown_resp))
            if (status == 429 or (idempotent and status in (502, 503, 504))) and attempt <= MAX_RETRIES:
                wait = _retry_after(headers_out.get("retry-after"), attempt)
                if waited + wait <= MAX_RETRY_WAIT_S:
                    self.api.note(f"сервер попросил подождать: {status}, повтор через {wait:.0f} с")
                    time.sleep(wait)
                    waited += wait
                    continue
            response = Response(status, headers_out, body)
            if status in expect:
                return response
            raise error_for(response, self.server_label, method, parsed.path)

    def get_json(self, path: str, **kw) -> Any:
        return self.request("GET", path, **kw).json()

    def post_json(self, path: str, body: Any = None, **kw) -> Any:
        return self.request("POST", path, json_body=body if body is not None else {}, **kw).json()


def _shown(body: bytes) -> str:
    """Ответ для журнала: JSON — без значений секретных полей (token, password…), прочее — как есть (начало)."""
    head = body[:1].strip()
    if head in (b"{", b"[") and len(body) <= 1024 * 1024:
        try:
            return json.dumps(redact(json.loads(body.decode("utf-8-sig"))), ensure_ascii=False)[:3000]
        except (UnicodeDecodeError, ValueError):
            pass
    return redact_markup(body[:1024 * 1024].decode("utf-8", errors="replace"))[:3000]


def _retry_after(value: Optional[str], attempt: int) -> float:
    if value:
        try:
            return max(0.5, min(float(value), MAX_RETRY_WAIT_S))
        except ValueError:
            pass
    return min(2.0 ** attempt, 10.0)


def _message_from(body: Any) -> str:
    """Текст ошибки из ответа (форматы разных API)."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            parts = [str(err.get(k)) for k in ("code", "message") if err.get(k)]
            if parts:
                return ": ".join(parts)
        if isinstance(err, str):
            desc = body.get("error_description") or body.get("message") or ""
            return f"{err}: {desc}" if desc else err
        for key in ("message", "msg", "detail", "description", "errorMessage"):
            if body.get(key):
                value = body[key]
                return ", ".join(map(str, value)) if isinstance(value, list) else str(value)
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return _message_from(body[0])
    return ""


def error_for(resp: Response, label: str, method: str, path: str) -> ServerError:
    try:
        body = json.loads(resp.body.decode("utf-8-sig")) if resp.body else None
    except (UnicodeDecodeError, ValueError):
        body = None
    text = _message_from(body) or resp.text()[:300].strip() or f"код {resp.status}"
    if resp.status in (401, 403):
        return ServerAuthError(f"{label}: доступ запрещён ({resp.status}): {text}",
                               hint="Проверьте учётные данные подключения и выданные приложению права.")
    if resp.status == 404:
        return NotFound(f"{label}: не найдено ({method} {path}): {text}")
    if resp.status == 429:
        return ServerError(f"{label}: сервер ограничил частоту запросов: {text}",
                           hint="Задание можно повторить позже.", retryable=True)
    if resp.status >= 500:
        return ServerError(f"{label}: ошибка сервера ({resp.status}): {text}", retryable=True)
    return ServerError(f"{label}: запрос отклонён ({resp.status}): {text}")


# ---------------------------------------------------------------------------
#  OAuth2
# ---------------------------------------------------------------------------
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt_rs256(claims: Dict[str, Any], private_key_pem: str, headers: Optional[Dict[str, Any]] = None, *,
              alg: str = "RS256") -> str:
    """Подписанный JWT (RS256 или PS256) — для сервисного аккаунта Google и сертификата приложения Microsoft."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise ServerAuthError("Закрытый ключ не читается (нужен PEM без пароля).",
                              hint="Вставьте ключ целиком: от «-----BEGIN PRIVATE KEY-----» до «-----END …-----».",
                              cause=exc) from exc
    head = {"alg": alg, "typ": "JWT"}
    head.update(headers or {})
    signing_input = (b64url(json.dumps(head, separators=(",", ":")).encode()) + "." +
                     b64url(json.dumps(claims, separators=(",", ":")).encode()))
    if alg == "PS256":
        pad = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)
    else:
        pad = padding.PKCS1v15()
    try:
        signature = key.sign(signing_input.encode("ascii"), pad, hashes.SHA256())
    except (TypeError, ValueError) as exc:
        raise ServerAuthError("Закрытый ключ не подходит для подписи (нужен ключ RSA).", cause=exc) from exc
    return signing_input + "." + b64url(signature)


class TokenCache:
    """Токены доступа по ключу (подключение, область, от чьего имени) — до истечения минус запас."""

    MARGIN_S = 120

    def __init__(self) -> None:
        self._tokens: Dict[Tuple, Tuple[str, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: Tuple) -> Optional[str]:
        with self._lock:
            hit = self._tokens.get(key)
            if hit and hit[1] - self.MARGIN_S > time.time():
                return hit[0]
            self._tokens.pop(key, None)
            return None

    def put(self, key: Tuple, token: str, expires_in: Any) -> None:
        try:
            ttl = float(expires_in)
        except (TypeError, ValueError):
            ttl = 3600.0
        with self._lock:
            self._tokens[key] = (token, time.time() + ttl)
            if len(self._tokens) > 5000:
                now = time.time()
                for k in [k for k, (_t, exp) in self._tokens.items() if exp < now]:
                    self._tokens.pop(k, None)

    def drop(self, prefix: Tuple) -> None:
        with self._lock:
            for k in [k for k in self._tokens if k[:len(prefix)] == prefix]:
                self._tokens.pop(k, None)


TOKENS = TokenCache()


def token_request(client: HttpClient, path: str, form: Dict[str, Any], label: str) -> Dict[str, Any]:
    """POST формы на сервер токенов → ответ (access_token, expires_in); ошибка — понятным текстом."""
    resp = client.request("POST", path, form=form, expect=(200, 400, 401, 403), log_body=False)
    try:
        body = resp.json() or {}
    except ServerError:
        body = {}
    if resp.status != 200 or not body.get("access_token"):
        text = _message_from(body) or resp.text()[:300]
        raise ServerAuthError(f"{label}: сервер токенов отказал ({resp.status}): {text}",
                              hint="Проверьте идентификатор и секрет (ключ) приложения и что администратор "
                                   "выдал приложению права.")
    return body
