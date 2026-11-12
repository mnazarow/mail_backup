"""
Почтовый клиент (1.20.0): подпись и шифрование S/MIME, расшифровка, проверки перед
отправкой (конфиденциальные данные, внешние адресаты, открытый список рассылки) и
вложение писем из архива.

**S/MIME.** Сертификат ящика с закрытым ключом загружается файлом PKCS#12 (.p12/.pfx).
Закрытый ключ хранится в базе зашифрованным ключом сервиса (как пароли ящиков); подпись
и расшифровка делаются на сервере архива — так же, как сервер хранит пароль ящика для
IMAP. Сертификаты получателей собираются сами из писем с верной подписью (и могут быть
загружены файлом .cer/.pem): зашифровать письмо можно, только если сертификат есть у
каждого получателя. Отправленное зашифрованное письмо шифруется и для самого отправителя —
чтобы копию в «Отправленных» можно было прочитать.

**Проверки перед отправкой.** Номера банковских карт (с проверкой по алгоритму Луна),
СНИЛС (с контрольным числом), паспорт РФ и ИНН рядом со словом «паспорт»/«ИНН» — в теме,
тексте и вложениях (текст вложений — :mod:`mailarchiver.attachtext`); адресаты вне
организации; много адресатов в «Кому» и «Копии». Режим — ``webmail.dlp_mode``:
``off`` — не проверять, ``warn`` — предупредить (отправить можно, подтвердив; подтверждение
— в аудите), ``block_external`` — конфиденциальные данные за пределы организации не
отправлять, внутри — предупредить.
"""
from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import ValidationError
from .logging_setup import get_logger
from .models import Account
from .util import utcnow_iso

log = get_logger("webmail.secure")

SCHEMA = """
CREATE TABLE IF NOT EXISTS webmail_smime_keys (
    account_id  INTEGER PRIMARY KEY,
    cert_pem    TEXT NOT NULL,
    chain_pem   TEXT NOT NULL DEFAULT '',
    key_enc     TEXT NOT NULL,             -- закрытый ключ (PEM), зашифрованный ключом сервиса
    name        TEXT NOT NULL DEFAULT '',
    emails      TEXT NOT NULL DEFAULT '',
    issuer      TEXT NOT NULL DEFAULT '',
    valid_until TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL DEFAULT '',
    uploaded_by TEXT NOT NULL DEFAULT ''
);
-- сертификаты получателей: из писем с верной подписью и загруженные вручную
CREATE TABLE IF NOT EXISTS webmail_smime_certs (
    email       TEXT PRIMARY KEY,
    cert_pem    TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    issuer      TEXT NOT NULL DEFAULT '',
    valid_until TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',  -- signed | upload
    updated_at  TEXT NOT NULL DEFAULT ''
);
"""

DLP_MODES = ("off", "warn", "block_external")


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


# ===========================================================================
#  S/MIME: сертификаты
# ===========================================================================
def _crypto():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12
    return x509, hashes, serialization, pkcs7, pkcs12


def _info(cert_pem: bytes) -> Dict:
    from .webmail_more import _cert_info
    return _cert_info(cert_pem)


def upload_key(svc, account: Account, data: bytes, password: str, actor: str) -> Dict:
    """Сертификат ящика с закрытым ключом из файла PKCS#12."""
    x509, _hashes, serialization, _pkcs7, pkcs12 = _crypto()
    if not data or len(data) > 256 * 1024:
        raise ValidationError("Файл сертификата пуст или слишком большой.", hint="Нужен файл .p12 или .pfx.")
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, (password or "").encode("utf-8") or None)
    except (ValueError, TypeError) as exc:
        raise ValidationError("Файл не открылся: неверный пароль или это не файл .p12/.pfx.") from exc
    if key is None or cert is None:
        raise ValidationError("В файле нет закрытого ключа и сертификата — нужен файл с ключом (.p12/.pfx).")
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    if not isinstance(key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)):
        raise ValidationError("Поддерживаются ключи RSA и ECDSA.")
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    info = _info(cert_pem)
    if info.get("expired"):
        raise ValidationError(f"Срок действия сертификата истёк ({info.get('valid_until', '')[:10]}).")
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode("ascii")
    chain = "".join(c.public_bytes(serialization.Encoding.PEM).decode("ascii") for c in (extra or []))
    svc.db.execute("INSERT OR REPLACE INTO webmail_smime_keys(account_id, cert_pem, chain_pem, key_enc, name, emails, "
                   "issuer, valid_until, uploaded_at, uploaded_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (int(account.id), cert_pem.decode("ascii"), chain, svc.db.secret.encrypt(key_pem),
                    info.get("name", ""), ",".join(info.get("emails") or []), info.get("issuer", ""),
                    info.get("valid_until", ""), utcnow_iso(), actor))
    for email in info.get("emails") or []:
        _remember(svc, email, cert_pem, info, "own")
    svc.db.add_audit(actor, "webmail_smime_key", f"ящик «{account.name}»: загружен сертификат {info.get('name', '')} "
                     f"({', '.join(info.get('emails') or [])}) до {info.get('valid_until', '')[:10]}")
    return key_info(svc, account)


def delete_key(svc, account: Account, actor: str) -> None:
    svc.db.execute("DELETE FROM webmail_smime_keys WHERE account_id=?", (int(account.id),))
    svc.db.add_audit(actor, "webmail_smime_key", f"ящик «{account.name}»: сертификат удалён")


def key_info(svc, account: Account) -> Dict:
    row = svc.db.query_one("SELECT * FROM webmail_smime_keys WHERE account_id=?", (int(account.id),))
    if row is None:
        return {"present": False}
    expired = bool(row["valid_until"]) and row["valid_until"] < datetime.now(timezone.utc).isoformat()
    return {"present": True, "name": row["name"], "emails": [e for e in row["emails"].split(",") if e],
            "issuer": row["issuer"], "valid_until": row["valid_until"], "expired": expired,
            "uploaded_at": row["uploaded_at"], "uploaded_by": row["uploaded_by"]}


def _load_key(svc, account: Account):
    x509, _h, serialization, _p7, _p12 = _crypto()
    row = svc.db.query_one("SELECT * FROM webmail_smime_keys WHERE account_id=?", (int(account.id),))
    if row is None:
        raise ValidationError("У ящика нет сертификата S/MIME.",
                              hint="Загрузите свой сертификат с ключом (.p12/.pfx): Почта → ⚙ Настройки ящика → S/MIME.")
    cert = x509.load_pem_x509_certificate(row["cert_pem"].encode("ascii"))
    key = serialization.load_pem_private_key(svc.db.secret.decrypt(row["key_enc"]).encode("ascii"), password=None)
    chain = []
    if row["chain_pem"]:
        try:
            chain = x509.load_pem_x509_certificates(row["chain_pem"].encode("ascii"))
        except (ValueError, AttributeError):
            chain = []
    return cert, key, chain


def _remember(svc, email: str, cert_pem: bytes, info: Dict, source: str) -> None:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return
    old = svc.db.query_one("SELECT valid_until, source FROM webmail_smime_certs WHERE email=?", (email,))
    # загруженный вручную сертификат письмом с подписью не заменяется на более старый
    if old is not None and (old["valid_until"] or "") > (info.get("valid_until") or "") and old["source"] == "upload":
        return
    svc.db.execute("INSERT OR REPLACE INTO webmail_smime_certs(email, cert_pem, name, issuer, valid_until, source, "
                   "updated_at) VALUES(?,?,?,?,?,?,?)",
                   (email, cert_pem.decode("ascii") if isinstance(cert_pem, bytes) else cert_pem,
                    info.get("name", ""), info.get("issuer", ""), info.get("valid_until", ""), source, utcnow_iso()))


def remember_signer(svc, cert_pem: bytes) -> None:
    """Сертификат из письма с верной подписью — для шифрования писем этому адресату."""
    info = _info(cert_pem)
    if not info or info.get("expired"):
        return
    pem = cert_pem.split(b"-----END CERTIFICATE-----")[0] + b"-----END CERTIFICATE-----\n"
    for email in info.get("emails") or []:
        try:
            _remember(svc, email, pem, info, "signed")
        except Exception as exc:  # noqa: BLE001 - сбор сертификатов не должен мешать просмотру письма
            log.debug("Сертификат %s не сохранён: %s", email, exc)


def import_cert(svc, data: bytes, actor: str) -> Dict:
    """Сертификат получателя из файла .cer/.crt/.pem (DER или PEM)."""
    x509, _h, serialization, _p7, _p12 = _crypto()
    try:
        cert = x509.load_pem_x509_certificate(data) if b"-----BEGIN" in data else x509.load_der_x509_certificate(data)
    except ValueError as exc:
        raise ValidationError("Это не сертификат (.cer, .crt или .pem).") from exc
    pem = cert.public_bytes(serialization.Encoding.PEM)
    info = _info(pem)
    if not info.get("emails"):
        raise ValidationError("В сертификате нет адреса почты — для S/MIME он не подходит.")
    if info.get("expired"):
        raise ValidationError(f"Срок действия сертификата истёк ({info.get('valid_until', '')[:10]}).")
    for email in info["emails"]:
        _remember(svc, email, pem, info, "upload")
    svc.db.add_audit(actor, "webmail_smime_cert", f"сертификат получателя {', '.join(info['emails'])}")
    return {"emails": info["emails"], "name": info.get("name", ""), "valid_until": info.get("valid_until", "")}


def list_certs(svc, q: str = "") -> List[Dict]:
    rows = svc.db.query("SELECT email, name, issuer, valid_until, source, updated_at FROM webmail_smime_certs "
                        "ORDER BY email LIMIT 2000")
    q = (q or "").strip().lower()
    now = datetime.now(timezone.utc).isoformat()
    return [{"email": r["email"], "name": r["name"], "issuer": r["issuer"], "valid_until": r["valid_until"],
             "source": r["source"], "updated_at": r["updated_at"], "expired": bool(r["valid_until"]) and
             r["valid_until"] < now} for r in rows if not q or q in r["email"] or q in (r["name"] or "").lower()]


def delete_cert(svc, email: str, actor: str) -> None:
    svc.db.execute("DELETE FROM webmail_smime_certs WHERE email=?", ((email or "").strip().lower(),))
    svc.db.add_audit(actor, "webmail_smime_cert", f"удалён сертификат получателя {email}")


def certs_for(svc, emails: Iterable[str]) -> Tuple[Dict[str, bytes], List[str]]:
    """Сертификаты получателей: (найденные действующие, адреса без сертификата)."""
    found: Dict[str, bytes] = {}
    missing: List[str] = []
    now = datetime.now(timezone.utc).isoformat()
    for email in dict.fromkeys(e.strip().lower() for e in emails if e):
        row = svc.db.query_one("SELECT cert_pem, valid_until FROM webmail_smime_certs WHERE email=?", (email,))
        if row is None or (row["valid_until"] and row["valid_until"] < now):
            missing.append(email)
        else:
            found[email] = row["cert_pem"].encode("ascii")
    return found, missing


def recipients_state(svc, account: Account, emails: Iterable[str]) -> Dict:
    """Для окна письма: можно ли подписать и зашифровать письмо этим адресатам."""
    found, missing = certs_for(svc, emails)
    key = key_info(svc, account)
    return {"can_sign": bool(key.get("present")) and not key.get("expired"), "key": key,
            "missing": missing, "have": sorted(found)}


# ===========================================================================
#  S/MIME: подпись, шифрование, расшифровка
# ===========================================================================
def _split(msg: EmailMessage) -> Tuple[bytes, bytes]:
    """Письмо → (внешние заголовки, MIME-содержимое с заголовками Content-*)."""
    inner = EmailMessage(policy=policy.SMTP)
    outer = EmailMessage(policy=policy.SMTP)
    for k, v in msg.items():
        low = k.lower()
        if low.startswith("content-"):
            inner[k] = v
        elif low != "mime-version":
            outer[k] = v
    inner.set_payload(msg.get_payload())
    head = outer.as_bytes().split(b"\r\n\r\n", 1)[0] + b"\r\n"
    return head, inner.as_bytes()


def _crlf(data: bytes) -> bytes:
    return re.sub(rb"\r?\n", b"\r\n", data)


def protect(svc, account: Account, msg: EmailMessage, rcpts: Sequence[str], sender: str, *,
            sign: bool, encrypt: bool) -> bytes:
    """Подписать и/или зашифровать письмо. → готовые байты письма."""
    if not sign and not encrypt:
        return msg.as_bytes()
    x509, hashes, serialization, pkcs7, _p12 = _crypto()
    head, entity = _split(msg)
    body = entity
    if sign:
        cert, key, chain = _load_key(svc, account)
        info = _info(cert.public_bytes(serialization.Encoding.PEM))
        if info.get("expired"):
            raise ValidationError("Срок действия сертификата ящика истёк — подписать письмо нельзя.")
        b = pkcs7.PKCS7SignatureBuilder().set_data(entity).add_signer(cert, key, hashes.SHA256())
        for c in chain:
            b = b.add_certificate(c)
        signed = _crlf(b.sign(serialization.Encoding.SMIME, [pkcs7.PKCS7Options.DetachedSignature]))
        body = signed
    if encrypt:
        if not hasattr(pkcs7, "PKCS7EnvelopeBuilder"):
            raise ValidationError("Шифрование S/MIME требует библиотеку cryptography версии 45 или новее.",
                                  hint="Обновите MailArchiver скриптом update.sh — он поставит нужную версию.")
        found, missing = certs_for(svc, rcpts)
        if missing:
            raise ValidationError("Нет сертификата получателя: " + ", ".join(missing[:10])
                                  + (" …" if len(missing) > 10 else "") + ".",
                                  hint="Сертификат появляется сам, когда получатель пришлёт письмо с подписью S/MIME, "
                                       "или его можно загрузить файлом (Почта → ⚙ → S/MIME).", code="smime_missing")
        own = svc.db.query_one("SELECT cert_pem FROM webmail_smime_keys WHERE account_id=?", (int(account.id),))
        pems = list(found.values()) + ([own["cert_pem"].encode("ascii")] if own is not None else [])
        if own is None:
            mine, _ = certs_for(svc, [sender])
            pems += list(mine.values())
        if body.startswith(b"MIME-Version:"):
            body = body.split(b"\r\n", 1)[1]
        env = pkcs7.PKCS7EnvelopeBuilder().set_data(body)
        seen = set()
        for pem in pems:
            cert = x509.load_pem_x509_certificate(pem)
            fp = cert.fingerprint(hashes.SHA256())
            if fp in seen:
                continue
            seen.add(fp)
            env = env.add_recipient(cert)
        body = _crlf(env.encrypt(serialization.Encoding.SMIME, []))
    if not body.startswith(b"MIME-Version:"):
        body = b"MIME-Version: 1.0\r\n" + body
    return head + body


def decrypt(svc, account: Account, raw: bytes) -> bytes:
    """Зашифрованное письмо → письмо с теми же заголовками и расшифрованным содержимым."""
    _x, _h, _s, pkcs7, _p12 = _crypto()
    if not hasattr(pkcs7, "pkcs7_decrypt_smime"):
        raise ValidationError("Расшифровка S/MIME требует библиотеку cryptography версии 44 или новее.")
    cert, key, _chain = _load_key(svc, account)
    try:
        inner = pkcs7.pkcs7_decrypt_smime(_crlf(raw), cert, key, [])
    except ValueError as exc:
        raise ValidationError("Письмо не расшифровывается ключом этого ящика: оно зашифровано для другого "
                              "сертификата (или повреждено).") from exc
    msg = message_from_bytes(raw[:256 * 1024], policy=policy.SMTP)
    outer = EmailMessage(policy=policy.SMTP)
    for k, v in msg.items():
        if not k.lower().startswith("content-") and k.lower() != "mime-version":
            outer[k] = v
    head = outer.as_bytes().split(b"\r\n\r\n", 1)[0] + b"\r\n"
    return head + b"MIME-Version: 1.0\r\n" + _crlf(inner)


# ===========================================================================
#  Проверки перед отправкой
# ===========================================================================
_CARD_RE = re.compile(r"(?<!\d)(?<!\d[.,])(?:\d[ \-]?){12,18}\d(?!\d|[.,]\d)")
_SNILS_RE = re.compile(r"(?<!\d)(\d{3})[- ]?(\d{3})[- ]?(\d{3})[ \-]+(\d{2})(?!\d)")
_PASSPORT_RE = re.compile(r"паспорт\w*[^\d\n]{0,40}?(\d{2}\s?\d{2})\s*(?:№|N|номер)?\s*(\d{6})(?!\d)", re.I)
_INN_RE = re.compile(r"\bИНН\b[^\d\n]{0,10}(\d{10}|\d{12})(?!\d)", re.I)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _snils_ok(num: str, check: str) -> bool:
    if int(num) <= 1001998:
        return False
    s = sum(int(d) * (9 - i) for i, d in enumerate(num))
    s = s % 101
    return (0 if s in (100, 101) else s) == int(check)


def _inn_ok(inn: str) -> bool:
    def ctrl(digits, weights):
        return sum(int(d) * w for d, w in zip(digits, weights)) % 11 % 10
    if len(inn) == 10:
        return ctrl(inn, (2, 4, 10, 3, 5, 9, 4, 6, 8)) == int(inn[9])
    if len(inn) == 12:
        return (ctrl(inn, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == int(inn[10])
                and ctrl(inn, (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == int(inn[11]))
    return False


def find_sensitive(text: str) -> Dict[str, int]:
    """Сколько конфиденциальных данных каждого вида в тексте."""
    out: Dict[str, int] = {}
    if not text:
        return out
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits) and len(set(digits)) > 1:
            out["card"] = out.get("card", 0) + 1
    for m in _SNILS_RE.finditer(text):
        if _snils_ok(m.group(1) + m.group(2) + m.group(3), m.group(4)):
            out["snils"] = out.get("snils", 0) + 1
    out_pass = len(_PASSPORT_RE.findall(text))
    if out_pass:
        out["passport"] = out_pass
    inn = sum(1 for m in _INN_RE.finditer(text) if _inn_ok(m.group(1)))
    if inn:
        out["inn"] = inn
    return out


SENSITIVE_LABELS = {"card": "номер банковской карты", "snils": "СНИЛС", "passport": "паспортные данные",
                    "inn": "ИНН"}


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[1].lower() if "@" in addr else ""


def presend_checks(svc, account: Account, draft, rcpts: Sequence[str], attachments: Sequence[Tuple[str, str, bytes]]
                   ) -> List[Dict]:
    """Предупреждения перед отправкой: [{level: warn|block, code, text}]."""
    from .attachtext import extract
    from .search import html_to_text
    from .webmail_more import _org, org_domains
    mode = str(svc.rt("webmail", "dlp_mode") or "warn").strip().lower()
    out: List[Dict] = []
    orgs = {_org(d) for d in org_domains(svc)}
    external = sorted({a for a in rcpts if _domain(a) and _org(_domain(a)) not in orgs})
    if external and bool(svc.rt("webmail", "warn_external")) and orgs:
        doms = sorted({_domain(a) for a in external})
        out.append({"level": "warn", "code": "external",
                    "text": f"Письмо уйдёт за пределы организации: {len(external)} адресат(ов) — "
                            + ", ".join(doms[:5]) + (" …" if len(doms) > 5 else "") + "."})
    try:
        many = int(svc.rt("webmail", "warn_recipients") or 0)
    except (TypeError, ValueError):
        many = 0
    visible = [a for _n, a in getaddresses([draft["to_addrs"] or "", draft["cc_addrs"] or ""]) if a]
    if many and len(visible) > many:
        out.append({"level": "warn", "code": "many",
                    "text": f"В «Кому» и «Копии» {len(visible)} адресатов — каждый увидит адреса всех остальных. "
                            f"Для рассылки используйте «Скрытую копию»."})
    if mode == "off":
        return out
    found: Dict[str, int] = {}
    where: List[str] = []
    for label, text in (("тема", draft["subject"] or ""), ("текст", html_to_text(draft["body"] or ""))):
        hit = find_sensitive(text)
        if hit:
            where.append(label)
            for k, v in hit.items():
                found[k] = found.get(k, 0) + v
    for name, ctype, data in attachments:
        try:
            text = extract(name, ctype, data, 256 * 1024)
        except Exception:  # noqa: BLE001
            text = ""
        hit = find_sensitive(text)
        if hit:
            where.append(f"вложение «{name}»")
            for k, v in hit.items():
                found[k] = found.get(k, 0) + v
    if found:
        what = ", ".join(f"{SENSITIVE_LABELS[k]} ({v})" for k, v in found.items())
        block = mode == "block_external" and bool(external)
        out.append({"level": "block" if block else "warn", "code": "sensitive",
                    "text": (f"В письме похоже на конфиденциальные данные: {what} — {', '.join(where[:5])}."
                             + (" Отправлять такие данные за пределы организации запрещено настройками."
                                if block else ""))})
    return out


# ===========================================================================
#  Письма из архива во вложения
# ===========================================================================
def attach_archived(svc, draft_id: int, owner: str, message_ids: Sequence[int], allowed_accounts: Optional[set]
                    ) -> List[Dict]:
    """Приложить к черновику письма из архива (файлами .eml). ``allowed_accounts`` — None: любые ящики."""
    from . import webmail as wm
    out = []
    for pk in list(dict.fromkeys(int(i) for i in message_ids))[:20]:
        row = svc.db.get_message(pk)
        if row is None or (allowed_accounts is not None and int(row["account_id"]) not in allowed_accounts):
            raise ValidationError(f"Письмо #{pk} не найдено в ваших ящиках.", code="not_found")
        raw = svc.store.read_message(int(row["account_id"]), row["stored_path"])
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", row["subject"] or "").strip()[:80] or f"Письмо {pk}"
        item = wm.add_attachment(svc, draft_id, owner, f"{name}.eml", "message/rfc822", io.BytesIO(raw))
        out.append(item)
    return out
