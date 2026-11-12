"""
Вход через LDAP / Active Directory (1.20.0) — без внешних библиотек.

Администраторы и операторы архива входят своим доменным логином и паролем; роль
определяется группой каталога: член ``ldap.admin_group`` — администратор, член
``ldap.operator_group`` — оператор (вложенные группы Active Directory учитываются),
иначе вход запрещён. Сотрудник (``ldap.employee_login``) может войти доменным паролем
в архив своих ящиков — ящик находится по адресу почты из каталога.

Порядок проверки: служебная учётная запись (``ldap.bind_dn``) ищет пользователя по
``ldap.user_filter``, затем выполняется вход найденным DN с введённым паролем. Пароль
по сети уходит только по защищённому соединению: ``ldaps://`` или ``ldap://`` со StartTLS;
без шифрования — только к адресу этой же машины.

При каждом входе роль сверяется с каталогом: убрали из группы — следующий вход не
пройдёт, а учётная запись архива отключается (открытые сеансы завершаются).

Протокол — LDAPv3 (RFC 4511): BER-кодирование запросов Bind, Search, ExtendedRequest
(StartTLS), разбор ответов. Фильтры — строковые (RFC 4515): &, |, !, =, =*, подстроки
и расширенное сравнение (``memberOf:1.2.840.113556.1.4.1941:=`` — вложенные группы AD).
"""
from __future__ import annotations

import socket
import ssl
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .errors import ValidationError
from .logging_setup import get_logger

log = get_logger("ldap")

#: OID «членство в группе с учётом вложенности» (LDAP_MATCHING_RULE_IN_CHAIN, Active Directory).
IN_CHAIN = "1.2.840.113556.1.4.1941"
STARTTLS_OID = "1.3.6.1.4.1.1466.20037"
#: Можно подменить в тестах: функция (host, port, timeout) → socket.
CONNECT = None


class LdapError(Exception):
    def __init__(self, message: str, code: int = -1) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
#  BER
# ---------------------------------------------------------------------------
def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    out = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(out)]) + out


def tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def ber_int(n: int, tag: int = 0x02) -> bytes:
    if n == 0:
        return tlv(tag, b"\x00")
    raw = n.to_bytes((n.bit_length() + 8) // 8, "big", signed=True)
    return tlv(tag, raw)


def ber_str(s, tag: int = 0x04) -> bytes:
    return tlv(tag, s.encode("utf-8") if isinstance(s, str) else bytes(s))


def ber_bool(v: bool, tag: int = 0x01) -> bytes:
    return tlv(tag, b"\xff" if v else b"\x00")


def seq(*items: bytes, tag: int = 0x30) -> bytes:
    return tlv(tag, b"".join(items))


def read_tlv(data: bytes, pos: int = 0) -> Tuple[int, bytes, int]:
    """(тег, значение, позиция после элемента)."""
    if pos + 2 > len(data):
        raise LdapError("обрыв ответа сервера LDAP")
    tag = data[pos]
    n = data[pos + 1]
    pos += 2
    if n & 0x80:
        k = n & 0x7F
        if k == 0 or k > 4 or pos + k > len(data):
            raise LdapError("неверная длина в ответе LDAP")
        n = int.from_bytes(data[pos:pos + k], "big")
        pos += k
    if pos + n > len(data):
        raise LdapError("обрыв ответа сервера LDAP")
    return tag, data[pos:pos + n], pos + n


def children(value: bytes) -> List[Tuple[int, bytes]]:
    out = []
    pos = 0
    while pos < len(value):
        tag, val, pos = read_tlv(value, pos)
        out.append((tag, val))
    return out


def to_int(value: bytes) -> int:
    return int.from_bytes(value, "big", signed=True) if value else 0


# ---------------------------------------------------------------------------
#  Фильтры (RFC 4515)
# ---------------------------------------------------------------------------
def escape(value: str) -> str:
    """Экранировать значение для фильтра: логин пользователя не должен менять смысл фильтра."""
    out = []
    for ch in str(value or ""):
        if ch in "*()\\\x00":
            out.append("\\%02x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def _unescape(value: str) -> bytes:
    out = bytearray()
    i = 0
    raw = value.encode("utf-8")
    while i < len(raw):
        if raw[i] == 0x5C and i + 2 < len(raw) + 1:
            try:
                out.append(int(raw[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(raw[i])
        i += 1
    return bytes(out)


def parse_filter(text: str) -> bytes:
    """Строковый фильтр → BER."""
    text = (text or "").strip()
    if not text.startswith("("):
        text = f"({text})"
    data, pos = _parse(text, 0)
    if pos != len(text):
        raise ValidationError(f"Ошибка в фильтре LDAP: лишнее после позиции {pos}.")
    return data


def _parse(text: str, pos: int) -> Tuple[bytes, int]:
    if pos >= len(text) or text[pos] != "(":
        raise ValidationError("Ошибка в фильтре LDAP: ожидалась «(».")
    pos += 1
    op = text[pos:pos + 1]
    if op in ("&", "|"):
        pos += 1
        items = []
        while pos < len(text) and text[pos] == "(":
            item, pos = _parse(text, pos)
            items.append(item)
        if pos >= len(text) or text[pos] != ")":
            raise ValidationError("Ошибка в фильтре LDAP: не закрыта скобка.")
        return tlv(0xA0 if op == "&" else 0xA1, b"".join(items)), pos + 1
    if op == "!":
        item, pos = _parse(text, pos + 1)
        if pos >= len(text) or text[pos] != ")":
            raise ValidationError("Ошибка в фильтре LDAP: не закрыта скобка.")
        return tlv(0xA2, item), pos + 1
    end = pos
    depth = 0
    while end < len(text) and not (text[end] == ")" and depth == 0):
        end += 1
    item = text[pos:end]
    if end >= len(text):
        raise ValidationError("Ошибка в фильтре LDAP: не закрыта скобка.")
    return _simple(item), end + 1


def _simple(item: str) -> bytes:
    for marker, tag in ((">=", 0xA5), ("<=", 0xA6), ("~=", 0xA8)):
        if marker in item:
            attr, value = item.split(marker, 1)
            return tlv(tag, ber_str(attr) + ber_str(_unescape(value)))
    if "=" not in item:
        raise ValidationError(f"Ошибка в фильтре LDAP: «{item}».")
    attr, value = item.split("=", 1)
    if attr.endswith(":"):                                  # расширенное сравнение: type:rule:=value
        parts = attr[:-1].split(":")
        typ = parts[0]
        rule = parts[-1] if len(parts) > 1 else ""
        dn = "dn" in [p.lower() for p in parts[1:]]
        body = (ber_str(rule, 0x81) if rule and rule.lower() != "dn" else b"") + \
            (ber_str(typ, 0x82) if typ else b"") + ber_str(_unescape(value), 0x83) + \
            (ber_bool(True, 0x84) if dn else b"")
        return tlv(0xA9, body)
    if value == "*":
        return ber_str(attr, 0x87)
    if "*" in value:
        pieces = value.split("*")
        subs = []
        if pieces[0]:
            subs.append(ber_str(_unescape(pieces[0]), 0x80))
        for mid in pieces[1:-1]:
            if mid:
                subs.append(ber_str(_unescape(mid), 0x81))
        if pieces[-1]:
            subs.append(ber_str(_unescape(pieces[-1]), 0x82))
        return tlv(0xA4, ber_str(attr) + seq(*subs))
    return tlv(0xA3, ber_str(attr) + ber_str(_unescape(value)))


# ---------------------------------------------------------------------------
#  Соединение
# ---------------------------------------------------------------------------
class Connection:
    def __init__(self, url: str, *, starttls: bool, verify: bool, timeout: float) -> None:
        parts = urlsplit((url or "").strip())
        if parts.scheme not in ("ldap", "ldaps") or not parts.hostname:
            raise ValidationError("Адрес сервера LDAP — вида ldaps://dc.company.local или ldap://dc.company.local.")
        self.host = parts.hostname
        self.port = parts.port or (636 if parts.scheme == "ldaps" else 389)
        self.timeout = timeout
        self.msg_id = 0
        self.ctx = ssl.create_default_context()
        if not verify:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        connect = CONNECT or (lambda h, p, t: socket.create_connection((h, p), timeout=t))
        self.sock = connect(self.host, self.port, timeout)
        self.sock.settimeout(timeout)
        self.secure = False
        if parts.scheme == "ldaps":
            self.sock = self.ctx.wrap_socket(self.sock, server_hostname=self.host)
            self.secure = True
        elif starttls:
            self._starttls()
        self.buf = b""

    def _starttls(self) -> None:
        self.buf = b""
        resp = self.request(seq(ber_str(STARTTLS_OID, 0x80), tag=0x77), 0x78)
        code, msg = _result(resp)
        if code != 0:
            raise LdapError(f"сервер не включил шифрование (StartTLS): {msg or code}", code)
        self.sock = self.ctx.wrap_socket(self.sock, server_hostname=self.host)
        self.secure = True

    def close(self) -> None:
        try:
            self.msg_id += 1
            self.sock.sendall(seq(ber_int(self.msg_id), tlv(0x42, b"")))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def _recv_message(self) -> bytes:
        while True:
            if len(self.buf) >= 2:
                try:
                    _tag, _val, end = read_tlv(self.buf, 0)
                    msg, self.buf = self.buf[:end], self.buf[end:]
                    return msg
                except LdapError:
                    pass
            chunk = self.sock.recv(65536)
            if not chunk:
                raise LdapError("сервер LDAP закрыл соединение")
            self.buf += chunk
            if len(self.buf) > 16 * 1024 * 1024:
                raise LdapError("слишком большой ответ сервера LDAP")

    def send(self, op: bytes) -> int:
        self.msg_id += 1
        self.sock.sendall(seq(ber_int(self.msg_id), op))
        return self.msg_id

    def request(self, op: bytes, expect: int) -> bytes:
        mid = self.send(op)
        while True:
            _t, body, _e = read_tlv(self._recv_message())
            parts = children(body)
            if len(parts) < 2 or to_int(parts[0][1]) != mid:
                continue
            tag, val = parts[1]
            if tag == expect:
                return val
            raise LdapError(f"неожиданный ответ сервера LDAP (тег {tag:#x})")

    def bind(self, dn: str, password: str) -> None:
        if password and not self.secure and self.host not in ("127.0.0.1", "::1", "localhost"):
            raise LdapError("пароль не отправляется по незашифрованному соединению: используйте ldaps:// или StartTLS")
        resp = self.request(seq(ber_int(3), ber_str(dn), ber_str(password, 0x80), tag=0x60), 0x61)
        code, msg = _result(resp)
        if code != 0:
            raise LdapError(msg or f"код {code}", code)

    def search(self, base: str, flt: str, attrs: List[str], size_limit: int = 5) -> List[Tuple[str, Dict[str, List[str]]]]:
        op = seq(ber_str(base), ber_int(2, 0x0A), ber_int(0, 0x0A), ber_int(size_limit), ber_int(int(self.timeout)),
                 ber_bool(False), parse_filter(flt), seq(*[ber_str(a) for a in attrs]), tag=0x63)
        mid = self.send(op)
        out: List[Tuple[str, Dict[str, List[str]]]] = []
        while True:
            _t, body, _e = read_tlv(self._recv_message())
            parts = children(body)
            if len(parts) < 2 or to_int(parts[0][1]) != mid:
                continue
            tag, val = parts[1]
            if tag == 0x64:                                 # SearchResultEntry
                items = children(val)
                dn = items[0][1].decode("utf-8", "replace")
                attrs_out: Dict[str, List[str]] = {}
                for _tg, pa in children(items[1][1]) if len(items) > 1 else []:
                    pv = children(pa)
                    name = pv[0][1].decode("utf-8", "replace").lower()
                    attrs_out[name] = [v.decode("utf-8", "replace") for _x, v in children(pv[1][1])] if len(pv) > 1 else []
                out.append((dn, attrs_out))
            elif tag == 0x73:                               # ссылка на другой сервер — не идём
                continue
            elif tag == 0x65:                               # SearchResultDone
                code, msg = _result(val)
                if code not in (0, 4):                      # 4 — превышен sizeLimit
                    raise LdapError(msg or f"поиск: код {code}", code)
                return out
            else:
                raise LdapError(f"неожиданный ответ сервера LDAP (тег {tag:#x})")


def _result(value: bytes) -> Tuple[int, str]:
    parts = children(value)
    code = to_int(parts[0][1]) if parts else -1
    msg = parts[2][1].decode("utf-8", "replace") if len(parts) > 2 else ""
    return code, msg


# ---------------------------------------------------------------------------
#  Вход
# ---------------------------------------------------------------------------
def enabled(svc) -> bool:
    return bool(svc.rt("ldap", "enabled")) and bool(str(svc.rt("ldap", "url") or "").strip())


def _connect(svc) -> Connection:
    return Connection(str(svc.rt("ldap", "url") or ""), starttls=bool(svc.rt("ldap", "starttls")),
                      verify=bool(svc.rt("ldap", "verify_ssl")), timeout=float(svc.rt("ldap", "timeout_s") or 10))


def _is_member(conn: Connection, svc, user_dn: str, member_of: List[str], group: str) -> bool:
    group = (group or "").strip()
    if not group:
        return False
    if group.lower() in {g.lower() for g in member_of}:
        return True
    if not bool(svc.rt("ldap", "nested_groups")):
        return False
    try:
        found = conn.search(user_dn, f"(memberOf:{IN_CHAIN}:={escape(group)})", ["1.1"], size_limit=1)
    except LdapError as exc:
        log.debug("Проверка вложенных групп не удалась: %s", exc)
        return False
    return bool(found)


def lookup(svc, username: str, password: Optional[str]) -> Dict:
    """Найти пользователя в каталоге и (если передан пароль) проверить его.

    → {"dn", "mail", "name", "groups", "role": admin|operator|mailbox|""}; LdapError — сбой каталога.
    Неверный пароль или нет такого пользователя — {"ok": False}.
    """
    username = (username or "").strip()
    if not username or (password is not None and not password):
        return {"ok": False}
    conn = _connect(svc)
    try:
        bind_dn = str(svc.rt("ldap", "bind_dn") or "")
        conn.bind(bind_dn, str(svc.rt("ldap", "bind_password") or "") if bind_dn else "")
        login = username.split("\\", 1)[-1]
        flt = str(svc.rt("ldap", "user_filter") or "(sAMAccountName={login})").replace("{login}", escape(login)) \
            .replace("{username}", escape(username))
        attrs = ["mail", "displayName", "cn", "memberOf", "userPrincipalName"]
        found = conn.search(str(svc.rt("ldap", "base_dn") or ""), flt, attrs, size_limit=2)
        if len(found) != 1:
            return {"ok": False, "reason": "не найден" if not found else "найдено несколько записей"}
        dn, a = found[0]
        if password is not None:
            user_conn = _connect(svc)
            try:
                user_conn.bind(dn, password)
            except LdapError as exc:
                if exc.code == 49:                          # invalidCredentials
                    return {"ok": False, "reason": "неверный пароль"}
                raise
            finally:
                user_conn.close()
        groups = a.get("memberof", [])
        role = ""
        if _is_member(conn, svc, dn, groups, str(svc.rt("ldap", "admin_group") or "")):
            role = "admin"
        elif _is_member(conn, svc, dn, groups, str(svc.rt("ldap", "operator_group") or "")):
            role = "operator"
        mail = (a.get("mail") or a.get("userprincipalname") or [""])[0].strip().lower()
        if not role and bool(svc.rt("ldap", "employee_login")) and mail:
            role = "mailbox"
        return {"ok": True, "dn": dn, "mail": mail, "name": (a.get("displayname") or a.get("cn") or [""])[0],
                "groups": groups, "role": role}
    finally:
        conn.close()


def mailbox_for(svc, mail: str):
    """Ящик сотрудника по адресу почты из каталога (логин ящика или адрес сотрудника)."""
    if not mail:
        return None
    for acc in svc.db.list_accounts():
        if (acc.username or "").strip().lower() == mail and acc.enabled:
            return acc
    row = svc.db.query_one("SELECT a.id FROM employees e JOIN accounts a ON a.employee_id = e.id WHERE "
                           "lower(e.email)=? AND a.enabled=1 ORDER BY a.id LIMIT 1", (mail,))
    return svc.db.get_account(int(row["id"])) if row is not None else None
