"""Учебный сервер LDAP для тестов: Bind, Search (фильтры &, |, !, =, =*, подстроки, вложенные группы AD), Unbind."""
import socketserver
import threading

from mailarchiver.ldapauth import (IN_CHAIN, ber_int, ber_str, children, read_tlv, seq, tlv, to_int)


class Directory:
    def __init__(self):
        self.entries = {}       # dn → {attr(lower): [values]}
        self.passwords = {}     # dn → пароль
        self.binds = []

    def add(self, dn, password=None, **attrs):
        self.entries[dn] = {k.lower(): (v if isinstance(v, list) else [v]) for k, v in attrs.items()}
        if password is not None:
            self.passwords[dn] = password

    def groups_in_chain(self, dn):
        seen, stack = set(), list(self.entries.get(dn, {}).get("memberof", []))
        while stack:
            g = stack.pop()
            if g.lower() in seen:
                continue
            seen.add(g.lower())
            stack += self.entries.get(g, {}).get("memberof", [])
        return seen


def _match(d, dn, attrs, flt):
    tag, val, _ = read_tlv(flt)
    if tag in (0xA0, 0xA1):
        items = [tlv(t, v) for t, v in children(val)]
        res = [_match(d, dn, attrs, i) for i in items]
        return all(res) if tag == 0xA0 else any(res)
    if tag == 0xA2:
        return not _match(d, dn, attrs, val)
    if tag == 0x87:
        return val.decode().lower() in attrs or val.decode().lower() == "objectclass"
    if tag == 0xA3:
        (_, a), (_, v) = children(val)
        return v.decode().lower() in [x.lower() for x in attrs.get(a.decode().lower(), [])]
    if tag == 0xA4:
        (_, a), (_, subs) = children(val)
        vals = attrs.get(a.decode().lower(), [])
        parts = children(subs)
        for x in vals:
            x = x.lower()
            ok = True
            for t, s in parts:
                s = s.decode().lower()
                if t == 0x80 and not x.startswith(s):
                    ok = False
                elif t == 0x82 and not x.endswith(s):
                    ok = False
                elif t == 0x81 and s not in x:
                    ok = False
            if ok:
                return True
        return False
    if tag == 0xA9:
        parts = {t: v.decode() for t, v in children(val)}
        if parts.get(0x81) == IN_CHAIN and parts.get(0x82, "").lower() == "memberof":
            return parts[0x83].lower() in d.groups_in_chain(dn)
        return False
    return False


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        d = self.server.directory
        buf = b""
        while True:
            try:
                chunk = self.request.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while True:
                try:
                    _t, body, end = read_tlv(buf)
                except Exception:  # noqa: BLE001
                    break
                buf = buf[end:]
                parts = children(body)
                mid = to_int(parts[0][1])
                tag, val = parts[1]
                if tag == 0x42:
                    return
                if tag == 0x60:
                    items = children(val)
                    dn, pw = items[1][1].decode(), items[2][1].decode()
                    d.binds.append(dn)
                    code = 0 if (not dn and not pw) or d.passwords.get(dn) == pw else 49
                    self._send(mid, seq(ber_int(code, 0x0A), ber_str(""), ber_str("" if not code else "invalid"),
                                        tag=0x61))
                elif tag == 0x77:
                    self._send(mid, seq(ber_int(2, 0x0A), ber_str(""), ber_str("no tls"), tag=0x78))
                elif tag == 0x63:
                    items = children(val)
                    base = items[0][1].decode().lower()
                    flt = tlv(items[6][0], items[6][1])
                    for dn, attrs in d.entries.items():
                        if not dn.lower().endswith(base):
                            continue
                        if _match(d, dn, attrs, flt):
                            pa = [seq(ber_str(k), tlv(0x31, b"".join(ber_str(v) for v in vs))) for k, vs in attrs.items()]
                            self._send(mid, seq(ber_str(dn), seq(*pa), tag=0x64))
                    self._send(mid, seq(ber_int(0, 0x0A), ber_str(""), ber_str(""), tag=0x65))

    def _send(self, mid, op):
        self.request.sendall(seq(ber_int(mid), op))


class LdapServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, directory):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.directory = directory
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()
