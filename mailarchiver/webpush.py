"""
Уведомления о новой почте на компьютер и телефон сотрудника (Web Push, 1.22.0).

Сотрудник включает их в «Почта → ⚙ → 🔔 Уведомления»: браузер подписывается у своей службы доставки
(Google, Mozilla, Apple, Microsoft) и отдаёт архиву адрес подписки и ключи. Когда во «Входящих» его ящика
появляется непрочитанное письмо, архив шифрует короткое сообщение «от кого и тема» ключом этого браузера
(RFC 8291, aes128gcm) и отправляет на адрес подписки, подписав запрос своим ключом VAPID (RFC 8292).
Служба доставки содержимого не видит; браузер показывает уведомление, даже если «Почта» закрыта.

Новая почта замечается так же, как в открытом клиенте: соединение IMAP IDLE с ящиком (если сервер умеет
и включено «Новая почта сразу»), иначе проверка «Входящих» раз в ``webmail.web_push_poll_min`` минут.
Подписка принадлежит сотруднику, вошедшему паролем ящика; кому что слать, считается на каждой отправке:
уволенный сотрудник или снятый администратором доступ к общему ящику прекращают уведомления сразу.

Только для сотрудников: администратор чужие письма видит в архиве, уведомления о них ему не нужны.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .errors import ValidationError
from .logging_setup import get_logger
from .util import utcnow_iso

log = get_logger("webpush")

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint         TEXT NOT NULL UNIQUE,      -- адрес подписки у службы доставки
    p256dh           TEXT NOT NULL,             -- открытый ключ браузера (base64url, P-256)
    auth             TEXT NOT NULL,             -- секрет подписки (base64url, 16 байт)
    login_account_id INTEGER NOT NULL,          -- ящик, паролем которого вошёл сотрудник
    account_ids      TEXT NOT NULL DEFAULT '[]',-- о каких ящиках уведомлять
    device           TEXT NOT NULL DEFAULT '',  -- браузер (из User-Agent), чтобы отличать устройства
    created_at       TEXT NOT NULL DEFAULT '',
    last_ok_at       TEXT NOT NULL DEFAULT '',
    last_error       TEXT NOT NULL DEFAULT '',
    failures         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_login ON push_subscriptions(login_account_id);
CREATE TABLE IF NOT EXISTS push_watermarks (
    account_id INTEGER PRIMARY KEY,
    last_id    INTEGER NOT NULL DEFAULT 0       -- письма с номером больше — ещё не разосланы
);
"""

#: Службы доставки браузеров (суффиксы имён): на другие адреса архив запросы не шлёт.
DEFAULT_HOSTS = ["fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com"]
TIMEOUT = 10
#: Подписка, на которую столько раз подряд не удалось отправить, удаляется.
MAX_FAILURES = 10
#: Уведомлений об отдельных письмах ящика за один раз; остальные — одним «ещё N писем».
MAX_PER_RUN = 3
#: Письма старше этого не объявляются (например, ящик долго не проверялся).
MAX_AGE = timedelta(hours=24)
#: Подписок на одного сотрудника (устройств).
MAX_DEVICES = 20
TTL_S = 24 * 3600

_KEY_LOCK = threading.Lock()
_ACC_LOCKS: Dict[int, threading.Lock] = {}
_ACC_LOCKS_GUARD = threading.Lock()
_LAST_POLL: Dict[int, float] = {}


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    text = (text or "").strip().replace("+", "-").replace("/", "_").rstrip("=")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def enabled(svc) -> bool:
    return bool(svc.rt("webmail", "enabled")) and bool(svc.rt("webmail", "web_push"))


# ---------------------------------------------------------------------------
#  Ключ VAPID сервера
# ---------------------------------------------------------------------------
def _private_key(svc):
    """Ключ P-256, которым архив подписывает запросы к службам доставки (создаётся один раз, хранится
    в базе зашифрованным)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    with _KEY_LOCK:
        stored = svc.db.get_meta("webpush_vapid")
        if stored:
            try:
                pem = svc.db.secret.decrypt(stored).encode("ascii")
                return serialization.load_pem_private_key(pem, password=None)
            except Exception:  # noqa: BLE001 - ключ шифрования базы сменили: прежние подписки не годятся
                log.warning("Ключ VAPID не расшифровался — создаётся новый; сотрудникам нужно заново включить "
                            "уведомления.")
                svc.db.execute("DELETE FROM push_subscriptions")
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode("ascii")
        svc.db.set_meta("webpush_vapid", svc.db.secret.encrypt(pem))
        return key


def _raw_public(pub) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key(svc) -> str:
    """Открытый ключ VAPID (base64url, 65 байт) — applicationServerKey для браузера."""
    return _b64e(_raw_public(_private_key(svc).public_key()))


def _contact(svc) -> str:
    c = str(svc.rt("webmail", "web_push_contact") or "").strip()
    if c:
        return c if c.startswith(("mailto:", "https://")) else "mailto:" + c
    url = str(svc.rt("server", "public_url") or "").strip().rstrip("/")
    if url.startswith("https://"):
        return url
    return "mailto:postmaster@localhost"


def _vapid_header(svc, endpoint: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    key = _private_key(svc)
    parts = urllib.parse.urlsplit(endpoint)
    head = _b64e(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": _contact(svc)}
    body = _b64e(json.dumps(claims, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode("ascii"), ec.ECDSA(hashes.SHA256())))
    sig = _b64e(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{body}.{sig}, k={_b64e(_raw_public(key.public_key()))}"


# ---------------------------------------------------------------------------
#  Шифрование содержимого (RFC 8291 / RFC 8188, aes128gcm)
# ---------------------------------------------------------------------------
def _hmac(key: bytes, data: bytes) -> bytes:
    import hmac
    import hashlib
    return hmac.new(key, data, hashlib.sha256).digest()


def encrypt(p256dh: str, auth: str, plaintext: bytes, *, salt: Optional[bytes] = None,
            server_key=None) -> bytes:
    """Тело запроса aes128gcm для подписки (``p256dh``, ``auth``): одна запись, без дополнения."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ua_raw = _b64d(p256dh)
    auth_secret = _b64d(auth)
    if len(ua_raw) != 65 or ua_raw[0] != 4 or len(auth_secret) < 16:
        raise ValidationError("Подписка браузера повреждена: неверные ключи.")
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_raw)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_raw = _raw_public(as_key.public_key())
    shared = as_key.exchange(ec.ECDH(), ua_pub)
    prk_key = _hmac(auth_secret, shared)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_raw + as_raw + b"\x01")
    salt = salt or os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    record = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    rs = 4096
    return salt + struct.pack(">IB", rs, len(as_raw)) + as_raw + record


# ---------------------------------------------------------------------------
#  Подписки
# ---------------------------------------------------------------------------
def _hosts(svc) -> List[str]:
    hosts = svc.rt("webmail", "web_push_hosts")
    if isinstance(hosts, str):
        hosts = [h for h in hosts.replace(";", ",").split(",")]
    hosts = [str(h).strip().lower().lstrip(".") for h in (hosts or DEFAULT_HOSTS) if str(h).strip()]
    return hosts or list(DEFAULT_HOSTS)


def _loopback(host: str) -> bool:
    return (host or "").lower() in ("localhost", "127.0.0.1", "::1")


def check_endpoint(svc, endpoint: str) -> str:
    """Адрес подписки — https-адрес известной службы доставки (иначе архив стал бы слать запросы куда
    угодно по указке браузера)."""
    endpoint = (endpoint or "").strip()
    parts = urllib.parse.urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if len(endpoint) > 1000 or not host:
        raise ValidationError("Неверный адрес подписки на уведомления.")
    if parts.scheme != "https" and not (parts.scheme == "http" and _loopback(host)):
        raise ValidationError("Адрес подписки на уведомления — только https://.")
    if not any(host == h or host.endswith("." + h) for h in _hosts(svc)):
        raise ValidationError(f"Служба уведомлений «{host}» не входит в разрешённые («Настройки → Почтовый "
                              f"клиент → Службы уведомлений браузеров»).", code="push_host")
    return endpoint


def _device(user_agent: str) -> str:
    ua = user_agent or ""
    browser = next((n for k, n in (("Edg/", "Edge"), ("YaBrowser", "Яндекс Браузер"), ("OPR/", "Opera"),
                                   ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
                    if k in ua), "браузер")
    system = next((n for k, n in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                                  ("Windows", "Windows"), ("Mac OS X", "macOS"), ("Linux", "Linux")) if k in ua), "")
    return f"{browser}, {system}" if system else browser


def scope_of(svc, login_account_id: int) -> List[int]:
    """Ящики, о которых сотруднику можно сообщать сейчас (свои и открытые ему)."""
    if svc.db.get_account(int(login_account_id)) is None:
        return []
    ids, _ = svc.db.mailbox_scope(int(login_account_id))
    return [int(i) for i in ids]


def subscribe(svc, user: dict, endpoint: str, p256dh: str, auth: str, account_ids: Optional[List[int]] = None,
              user_agent: str = "") -> Dict:
    if not enabled(svc):
        raise ValidationError("Уведомления на устройства выключены администратором.", code="push_off")
    endpoint = check_endpoint(svc, endpoint)
    encrypt(p256dh, auth, b"check")                     # ключи браузера годятся
    login = int(user["account_id"])
    allowed = scope_of(svc, login)
    ids = [int(i) for i in (account_ids if account_ids is not None else allowed) if int(i) in allowed][:50]
    if not ids:
        raise ValidationError("Выберите хотя бы один ящик.")
    n = int(svc.db.scalar("SELECT COUNT(*) FROM push_subscriptions WHERE login_account_id=? AND endpoint<>?",
                          (login, endpoint)) or 0)
    if n >= MAX_DEVICES:
        raise ValidationError(f"Уведомления включены уже на {n} устройствах — выключите их на старых.")
    svc.db.execute("INSERT INTO push_subscriptions(endpoint, p256dh, auth, login_account_id, account_ids, device, "
                   "created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, "
                   "auth=excluded.auth, login_account_id=excluded.login_account_id, account_ids=excluded.account_ids, "
                   "device=excluded.device, failures=0, last_error=''",
                   (endpoint, p256dh.strip(), auth.strip(), login, json.dumps(ids), _device(user_agent)[:80],
                    utcnow_iso()))
    for aid in ids:
        _init_watermark(svc, aid)
    svc.db.add_audit(str(user.get("username") or ""), "webpush_subscribe",
                     f"уведомления о новой почте включены на устройстве ({_device(user_agent)}): ящиков {len(ids)}")
    return state(svc, user, endpoint)


def unsubscribe(svc, user: dict, endpoint: str) -> Dict:
    logins = _logins(svc, user)
    marks = ",".join("?" * len(logins))
    cur = svc.db.execute(f"DELETE FROM push_subscriptions WHERE endpoint=? AND login_account_id IN ({marks})",
                         (endpoint or "", *logins))
    if int(cur.rowcount or 0):
        svc.db.add_audit(str(user.get("username") or ""), "webpush_subscribe",
                         "уведомления о новой почте выключены на устройстве")
    return {"ok": True, "subscribed": False}


def _logins(svc, user: dict) -> List[int]:
    """Ящики, вход паролем которых — тот же сотрудник: подписку, сделанную из другого его ящика, тоже видно
    и можно снять."""
    login = int(user["account_id"])
    own = {int(r["id"]) for r in svc.db.query("SELECT a2.id FROM accounts a JOIN accounts a2 "
                                              "ON a2.employee_id = a.employee_id WHERE a.id=? AND a.employee_id "
                                              "IS NOT NULL", (login,))}
    return sorted(own | {login})


def state(svc, user: dict, endpoint: str = "") -> Dict:
    on = enabled(svc)
    out = {"enabled": on, "public_key": public_key(svc) if on else "", "subscribed": False, "account_ids": [],
           "preview": bool(svc.rt("webmail", "web_push_preview")), "devices": []}
    logins = _logins(svc, user)
    marks = ",".join("?" * len(logins))
    rows = svc.db.query(f"SELECT * FROM push_subscriptions WHERE login_account_id IN ({marks}) ORDER BY id",
                        tuple(logins))
    for r in rows:
        mine = bool(endpoint) and r["endpoint"] == endpoint
        if mine:
            out["subscribed"] = True
            out["account_ids"] = json.loads(r["account_ids"] or "[]")
        out["devices"].append({"id": int(r["id"]), "device": r["device"], "created_at": r["created_at"],
                               "last_ok_at": r["last_ok_at"], "last_error": r["last_error"], "this": mine})
    return out


def forget_device(svc, user: dict, sub_id: int) -> Dict:
    logins = _logins(svc, user)
    marks = ",".join("?" * len(logins))
    svc.db.execute(f"DELETE FROM push_subscriptions WHERE id=? AND login_account_id IN ({marks})",
                   (int(sub_id), *logins))
    return {"ok": True}


# ---------------------------------------------------------------------------
#  Отправка
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _post(svc, endpoint: str, body: bytes, *, ttl: int = TTL_S, urgency: str = "normal") -> Tuple[int, str]:
    """→ (код ответа, текст ошибки)."""
    check_endpoint(svc, endpoint)
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Type": "application/octet-stream", "Content-Encoding": "aes128gcm", "TTL": str(int(ttl)),
        "Urgency": urgency, "Authorization": _vapid_header(svc, endpoint), "User-Agent": "MailArchiver"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return int(resp.status), ""
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read(300).decode("utf-8", "replace") if exc.fp else ""
        except Exception:  # noqa: BLE001
            pass
        return int(exc.code), f"HTTP {exc.code} {text}".strip()[:300]
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"нет связи со службой уведомлений: {getattr(exc, 'reason', exc)}"[:300]


def send(svc, sub, message: Dict) -> bool:
    """Отправить одно уведомление подписке ``sub`` (строка таблицы). Устаревшая подписка удаляется."""
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(payload) > 3000:         # запись aes128gcm — до 4 КБ; длинный текст укорачиваем, JSON не режем
        message = dict(message, body=str(message.get("body", ""))[:200], title=str(message.get("title", ""))[:100])
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    try:
        body = encrypt(sub["p256dh"], sub["auth"], payload)
        code, err = _post(svc, sub["endpoint"], body)
    except ValidationError as exc:
        code, err = 0, exc.message
    if 200 <= code < 300:
        svc.db.execute("UPDATE push_subscriptions SET last_ok_at=?, last_error='', failures=0 WHERE id=?",
                       (utcnow_iso(), int(sub["id"])))
        return True
    if code in (404, 410):          # браузер отписался или подписка истекла
        svc.db.execute("DELETE FROM push_subscriptions WHERE id=?", (int(sub["id"]),))
        return False
    svc.db.execute("UPDATE push_subscriptions SET last_error=?, failures=failures+1 WHERE id=?",
                   (err or f"HTTP {code}", int(sub["id"])))
    svc.db.execute("DELETE FROM push_subscriptions WHERE id=? AND failures>=?", (int(sub["id"]), MAX_FAILURES))
    log.info("Уведомление на устройство не ушло (%s): %s", sub["device"], err)
    return False


def send_test(svc, user: dict, endpoint: str) -> Dict:
    row = svc.db.query_one("SELECT * FROM push_subscriptions WHERE endpoint=?", (endpoint or "",))
    if row is None or int(row["login_account_id"]) not in _logins(svc, user):
        raise ValidationError("На этом устройстве уведомления не включены.", code="not_found")
    ok = send(svc, row, {"title": "MailArchiver", "body": "Уведомления о новой почте работают.",
                         "tag": "ma-test", "url": "/#/mail"})
    if not ok:
        fresh = svc.db.query_one("SELECT last_error FROM push_subscriptions WHERE id=?", (int(row["id"]),))
        raise ValidationError("Служба уведомлений не приняла сообщение: "
                              + (fresh["last_error"] if fresh else "подписка браузера больше не действует") + ".")
    return {"ok": True}


def _acc_lock(account_id: int) -> threading.Lock:
    with _ACC_LOCKS_GUARD:
        return _ACC_LOCKS.setdefault(int(account_id), threading.Lock())


def _init_watermark(svc, account_id: int) -> None:
    top = int(svc.db.scalar("SELECT COALESCE(MAX(id), 0) FROM messages WHERE account_id=?", (int(account_id),)) or 0)
    svc.db.execute("INSERT INTO push_watermarks(account_id, last_id) VALUES(?,?) ON CONFLICT(account_id) DO NOTHING",
                   (int(account_id), top))


def _recent(internaldate: str) -> bool:
    try:
        dt = datetime.fromisoformat(str(internaldate or "").replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt <= MAX_AGE


def _subscribers(svc) -> Dict[int, List]:
    """{ящик: подписки, которым о нём можно сообщать}; подписки сотрудников без доступа удаляются."""
    out: Dict[int, List] = {}
    scopes: Dict[int, List[int]] = {}
    for sub in svc.db.query("SELECT * FROM push_subscriptions"):
        login = int(sub["login_account_id"])
        if login not in scopes:
            scopes[login] = scope_of(svc, login)
        allowed = scopes[login]
        if not allowed:
            svc.db.execute("DELETE FROM push_subscriptions WHERE id=?", (int(sub["id"]),))
            continue
        try:
            wanted = [int(i) for i in json.loads(sub["account_ids"] or "[]")]
        except (ValueError, TypeError):
            wanted = []
        for aid in wanted:
            if aid in allowed:
                out.setdefault(aid, []).append(sub)
    return out


def notify_account(svc, account_id: int, subs: Optional[List] = None) -> int:
    """Разослать уведомления о новых непрочитанных письмах «Входящих» ящика. → сколько писем объявлено."""
    if not enabled(svc):
        return 0
    if subs is None:
        subs = _subscribers(svc).get(int(account_id), [])
    with _acc_lock(account_id):
        _init_watermark(svc, account_id)
        last = int(svc.db.scalar("SELECT last_id FROM push_watermarks WHERE account_id=?", (int(account_id),)) or 0)
        rows = svc.db.query("SELECT id, folder, subject, from_addr, flags, internaldate, COALESCE(gone, 0) AS gone "
                            "FROM messages WHERE account_id=? AND id>? ORDER BY id LIMIT 500",
                            (int(account_id), last))
        if not rows:
            return 0
        top = int(rows[-1]["id"]) if len(rows) < 500 else int(svc.db.scalar(
            "SELECT MAX(id) FROM messages WHERE account_id=?", (int(account_id),)) or 0)
        svc.db.execute("UPDATE push_watermarks SET last_id=? WHERE account_id=?", (top, int(account_id)))
    if not subs:
        return 0
    acc = svc.db.get_account(int(account_id))
    if acc is None:
        return 0
    from .webmail_tools import _inbox
    try:
        inbox = _inbox(svc, acc).upper()
    except Exception:  # noqa: BLE001
        inbox = "INBOX"
    fresh = [r for r in rows if str(r["folder"] or "").upper() in (inbox, "INBOX") and not int(r["gone"] or 0)
             and "\\seen" not in str(r["flags"] or "").lower() and _recent(r["internaldate"])]
    if not fresh:
        return 0
    preview = bool(svc.rt("webmail", "web_push_preview"))
    messages = []
    for r in fresh[-MAX_PER_RUN:]:
        sender = _sender(svc, int(account_id), r["from_addr"])
        if preview:
            messages.append({"title": sender or "Новое письмо", "body": ((r["subject"] or "(без темы)")[:150]
                                                                        + f"\n{acc.name}")[:300],
                             "tag": f"ma-{account_id}-{int(r['id'])}",
                             "url": f"/#/mail?open={int(account_id)}.{int(r['id'])}"})
        else:
            messages.append({"title": "Новое письмо", "body": f"Ящик «{acc.name}»"[:200],
                             "tag": f"ma-{account_id}", "url": f"/#/mail?open={int(account_id)}.{int(r['id'])}"})
    if len(fresh) > MAX_PER_RUN:
        messages.append({"title": "Новая почта", "body": f"Ещё писем во «Входящих» ящика «{acc.name}»: "
                                                         f"{len(fresh) - MAX_PER_RUN}",
                         "tag": f"ma-{account_id}-more", "url": "/#/mail"})
    if not preview:
        messages = messages[-1:]
    with ThreadPoolExecutor(max_workers=min(8, len(subs))) as pool:
        for sub in subs:
            pool.submit(_send_all, svc, sub, messages)
    return len(fresh)


def _send_all(svc, sub, messages: List[Dict]) -> None:
    for m in messages:
        try:
            if not send(svc, sub, m):
                return
        except Exception:  # noqa: BLE001 - одно устройство не мешает другим
            log.exception("Ошибка отправки уведомления")
            return


def _sender(svc, account_id: int, from_addr: str) -> str:
    """Имя отправителя из адресной книги ящика, иначе адрес."""
    from email.utils import parseaddr
    name, addr = parseaddr(str(from_addr or ""))
    if not name and addr:
        try:
            row = svc.db.query_one("SELECT name FROM webmail_book WHERE account_id=? AND lower(email)=? AND name<>'' "
                                   "LIMIT 1", (int(account_id), addr.lower()))
            name = row["name"] if row is not None else ""
        except Exception:  # noqa: BLE001
            name = ""
    return (name or addr or "").strip()[:80]


def tick(svc) -> Dict:
    """Раз в минуту: держать IDLE с ящиками, на которые подписаны, или проверять их «Входящие»; разослать."""
    out = {"accounts": 0, "messages": 0}
    if not enabled(svc):
        return out
    subs = _subscribers(svc)
    if not subs:
        return out
    from . import webmail as wm
    from . import webmail_more as wx
    from .webmail_tools import _inbox, has_automation, process_new
    poll = max(1, int(svc.rt("webmail", "web_push_poll_min") or 5)) * 60
    mono = time.monotonic()
    for aid, items in sorted(subs.items()):
        acc = svc.db.get_account(aid)
        if acc is None or not acc.enabled or acc.login_status == "auth_error" or acc.secret_broken:
            continue
        out["accounts"] += 1
        idle = False
        if bool(svc.rt("webmail", "push")):
            try:
                idle = wx.want_push(svc, acc).get("push") in ("idle", "starting")
            except Exception:  # noqa: BLE001
                idle = False
        if not idle and mono - _LAST_POLL.get(aid, -1e9) >= poll - 5:
            _LAST_POLL[aid] = mono
            try:
                if not wm.backup_running(svc, aid):
                    wm.sync_folder(svc, acc, _inbox(svc, acc))
                    if has_automation(svc, aid):      # правила раньше уведомления: письмо могли убрать
                        process_new(svc, acc)
            except Exception as exc:  # noqa: BLE001
                log.info("«Входящие» ящика «%s» для уведомлений не проверены: %s", acc.name,
                         getattr(exc, "message", exc))
        try:
            out["messages"] += notify_account(svc, aid, items)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка рассылки уведомлений ящика #%s", aid)
    return out
