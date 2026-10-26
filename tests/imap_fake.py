"""
Учебный почтовый сервер IMAP в памяти — вместо клиента imapclient.

Подменяет ``mailarchiver.imap.client.IMAPClient``: весь код MailArchiver выше
(копирование ящика, групповые действия на сервере) работает настоящий, а
«сервер» — словарь папок с письмами. Умеет то, чем пользуются копирование и
действия с ящиками: LIST (с флагами SPECIAL-USE), SELECT/EXAMINE, STATUS,
SEARCH (ALL, UID, SINCE, BEFORE, UNSEEN, DELETED), FETCH (размер, дата, флаги,
тело, заголовки), STORE флагов, EXPUNGE и UID EXPUNGE, MOVE, COPY, CREATE,
RENAME, DELETE, подписку и квоты. Расширения (UIDPLUS, MOVE, QUOTA)
включаются и выключаются в ``capabilities``.
"""
from __future__ import annotations

import datetime as _dt
import re
import types
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from imapclient.exceptions import IMAPClientError, LoginError


def raw_message(subject: str, *, mid: Optional[str] = None, date: str = "Mon, 01 Jan 2024 10:00:00 +0000",
                body: str = "текст") -> bytes:
    mid = mid or f"<{abs(hash(subject + date))}@test>"
    return (f"From: a@company.ru\r\nTo: b@company.ru\r\nSubject: {subject}\r\nMessage-ID: {mid}\r\n"
            f"Date: {date}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n").encode("utf-8")


@dataclass
class Msg:
    raw: bytes
    date: _dt.datetime
    flags: Set[bytes] = field(default_factory=set)


@dataclass
class Folder:
    flags: List[bytes] = field(default_factory=list)
    uidvalidity: int = 1000
    uidnext: int = 1
    messages: Dict[int, Msg] = field(default_factory=dict)


class ImapWorld:
    """Состояние учебного сервера: ящики (логин → папки)."""

    def __init__(self) -> None:
        self.boxes: Dict[str, Dict[str, Folder]] = {}
        self.passwords: Dict[str, str] = {}
        self.capabilities: Set[str] = {"IMAP4REV1", "UIDPLUS", "MOVE", "QUOTA", "SPECIAL-USE"}
        self.subscribed: Dict[str, Set[str]] = {}
        self.quota: Dict[str, tuple] = {}            # логин → (занято КБ, предел КБ)
        self.delimiter = "/"
        self.commands: List[str] = []
        #: команда → текст отказа (например {"DELETE": "NO [CANNOT] busy"})
        self.fail: Dict[str, str] = {}
        #: токены XOAUTH2: токен → адреса ящиков, в которые он пускает ("*" — во все)
        self.tokens: Dict[str, Set[str]] = {}
        #: администраторы почты: логин → пароль (SASL PLAIN с authzid и вход «ящик<разделитель>администратор»)
        self.admins: Dict[str, str] = {}
        self.master_separator = "*"
        #: мастер-пароль Kerio Connect (X-MASTERAUTH / X-SETUSER); пусто — сервер так не пускает
        self.kerio_master = ""
        #: как вошли последний раз: (способ, логин, в чей ящик)
        self.last_login: tuple = ()

    def mailbox(self, login: str, password: str = "pw", folders: Optional[Dict[str, List[bytes]]] = None) -> None:
        self.passwords[login] = password
        box = {"INBOX": Folder()}
        for name, flags in (folders or {}).items():
            box[name] = Folder(flags=list(flags))
        self.boxes[login] = box
        self.subscribed[login] = set()

    def add(self, login: str, folder: str, raw: bytes, *, days_ago: int = 0, flags=()) -> int:
        f = self.boxes[login][folder]
        uid = f.uidnext
        f.uidnext += 1
        when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_ago)
        f.messages[uid] = Msg(raw=raw, date=when, flags=set(flags))
        return uid

    def uids(self, login: str, folder: str) -> List[int]:
        return sorted(self.boxes[login][folder].messages)

    def client_class(self):
        world = self

        class Client(FakeIMAPClient):
            pass
        Client.world = world
        return Client


def _expand(spec: str, top: int) -> List[int]:
    out: List[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            a, b = part.split(":", 1)
            lo = int(a) if a != "*" else top
            hi = int(b) if b != "*" else top
            if lo > hi:
                lo, hi = hi, lo
            out.extend(range(lo, hi + 1))
        else:
            out.append(int(part) if part != "*" else top)
    return out


class FakeIMAPClient:
    world: ImapWorld = None

    def __init__(self, host, port=None, ssl=True, ssl_context=None, timeout=None):
        self.host = host
        self.login_name: Optional[str] = None
        self.current: Optional[str] = None
        self.readonly = True
        self.normalise_times = True
        self.folder_encode = True
        self._imap = types.SimpleNamespace(untagged_responses={}, sock=None)

    # -- вход ------------------------------------------------------------------
    def _cmd(self, name: str) -> None:
        self.world.commands.append(name)
        if name in self.world.fail:
            raise IMAPClientError(self.world.fail[name])

    def login(self, user, password):
        self._cmd("LOGIN")
        sep = self.world.master_separator
        if self.world.admins and sep and sep in user:
            box, admin = user.split(sep, 1)
            if self.world.admins.get(admin) == password and box in self.world.boxes:
                self.login_name = box
                self.world.last_login = ("separator", admin, box)
                return
            raise LoginError("[AUTHENTICATIONFAILED] Authentication failed.")
        if self.world.passwords.get(user) != password:
            raise LoginError("[AUTHENTICATIONFAILED] Authentication failed.")
        self.login_name = user
        self.world.last_login = ("login", user, user)

    def plain_login(self, user, password, authorization_identity=None):
        self._cmd("AUTHENTICATE PLAIN")
        if authorization_identity and authorization_identity != user:
            if self.world.admins.get(user) == password and authorization_identity in self.world.boxes:
                self.login_name = authorization_identity
                self.world.last_login = ("plain_authzid", user, authorization_identity)
                return
            raise LoginError("[AUTHENTICATIONFAILED] Authentication failed.")
        self.login(user, password)

    def kerio_master_login(self, mailbox, master_password):
        self._cmd("X-MASTERAUTH")
        if not self.world.kerio_master or master_password != self.world.kerio_master:
            raise LoginError("X-MASTERAUTH failed")
        self._cmd("X-SETUSER")
        if mailbox not in self.world.boxes:
            raise LoginError("X-SETUSER failed")
        self.login_name = mailbox
        self.world.last_login = ("kerio_master", "master", mailbox)

    def oauth2_login(self, user, token):
        self._cmd("AUTHENTICATE XOAUTH2")
        allowed = self.world.tokens.get(token)
        if allowed is not None and (user in allowed or "*" in allowed) and user in self.world.boxes:
            self.login_name = user
            self.world.last_login = ("xoauth2", user, user)
            return
        raise LoginError("AUTHENTICATE failed.")

    def starttls(self, ctx=None):
        return None

    def logout(self):
        return None

    def shutdown(self):
        return None

    # -- сведения ----------------------------------------------------------------
    @property
    def box(self) -> Dict[str, Folder]:
        return self.world.boxes[self.login_name]

    def capabilities(self):
        return [c.encode() for c in sorted(self.world.capabilities)]

    def has_capability(self, name):
        return name.upper() in self.world.capabilities

    def list_folders(self, directory="", pattern="*"):
        d = self.world.delimiter
        out = []
        for name, f in self.box.items():
            if pattern not in ("*", "") and not re.fullmatch(re.escape(pattern).replace(r"\*", ".*")
                                                             .replace(r"\%", f"[^{re.escape(d)}]*"), name):
                continue
            out.append((tuple(f.flags), d.encode(), name))
        return out

    def list_sub_folders(self, directory="", pattern="*"):
        d = self.world.delimiter
        return [((), d.encode(), n) for n in sorted(self.world.subscribed[self.login_name]) if n in self.box]

    def folder_exists(self, folder):
        return folder in self.box

    def folder_status(self, folder, what):
        if folder not in self.box:
            raise IMAPClientError("STATUS failed: no such mailbox")
        f = self.box[folder]
        return {b"MESSAGES": len(f.messages), b"UNSEEN": sum(1 for m in f.messages.values() if b"\\Seen" not in m.flags),
                b"UIDNEXT": f.uidnext, b"UIDVALIDITY": f.uidvalidity}

    def get_quota_root(self, mailbox):
        used, limit = self.world.quota.get(self.login_name, (0, 0))
        q = types.SimpleNamespace(quota_root="", resource="STORAGE", usage=used, limit=limit)
        return types.SimpleNamespace(mailbox=mailbox, quota_roots=[""]), ([q] if limit else [])

    # -- папки -------------------------------------------------------------------
    def select_folder(self, folder, readonly=False):
        self._cmd("EXAMINE" if readonly else "SELECT")
        if folder not in self.box or b"\\Noselect" in self.box[folder].flags:
            raise IMAPClientError(f"select failed: [NONEXISTENT] {folder}")
        self.current = folder
        self.readonly = readonly
        f = self.box[folder]
        return {b"UIDVALIDITY": f.uidvalidity, b"UIDNEXT": f.uidnext, b"EXISTS": len(f.messages)}

    def create_folder(self, folder):
        self._cmd("CREATE")
        if folder in self.box:
            raise IMAPClientError("create failed: [ALREADYEXISTS] Mailbox already exists")
        self.box[folder] = Folder(uidvalidity=2000 + len(self.box))

    def rename_folder(self, old, new):
        self._cmd("RENAME")
        if old not in self.box or new in self.box:
            raise IMAPClientError("rename failed")
        self.box[new] = self.box.pop(old)
        self.box[new].uidvalidity += 1

    def delete_folder(self, folder):
        self._cmd("DELETE")
        if folder not in self.box:
            raise IMAPClientError("delete failed: no such mailbox")
        if self.current == folder:
            raise IMAPClientError("delete failed: mailbox is selected")
        del self.box[folder]
        self.world.subscribed[self.login_name].discard(folder)

    def subscribe_folder(self, folder):
        self._cmd("SUBSCRIBE")
        self.world.subscribed[self.login_name].add(folder)

    # -- письма ------------------------------------------------------------------
    def _f(self) -> Folder:
        return self.box[self.current]

    def search(self, criteria):
        self._cmd("SEARCH")
        f = self._f()
        uids = set(f.messages)
        crit = list(criteria) if isinstance(criteria, (list, tuple)) else [criteria]
        i = 0
        top = max(f.messages or [0])
        while i < len(crit):
            c = crit[i]
            key = c.upper() if isinstance(c, str) else c
            if key == "ALL":
                pass
            elif key == "UID":
                uids &= set(_expand(crit[i + 1], max(top, f.uidnext - 1)))
                i += 1
            elif key in ("SINCE", "BEFORE"):
                when = crit[i + 1]
                if isinstance(when, _dt.datetime):
                    when = when.date()
                if key == "SINCE":
                    uids = {u for u in uids if f.messages[u].date.date() >= when}
                else:
                    uids = {u for u in uids if f.messages[u].date.date() < when}
                i += 1
            elif key == "UNSEEN":
                uids = {u for u in uids if b"\\Seen" not in f.messages[u].flags}
            elif key == "DELETED":
                uids = {u for u in uids if b"\\Deleted" in f.messages[u].flags}
            elif key == "HEADER":
                i += 2
            else:
                raise IMAPClientError(f"SEARCH failed: unknown criterion {c}")
            i += 1
        return sorted(uids)

    def fetch(self, uids, items):
        self._cmd("FETCH")
        f = self._f()
        if uids and isinstance(uids[0], str):
            uids = _expand(uids[0], max(f.messages or [0]))
        out = {}
        for uid in uids:
            m = f.messages.get(int(uid))
            if m is None:
                continue
            row = {}
            for it in items:
                it = it if isinstance(it, bytes) else str(it).encode()
                if it == b"RFC822.SIZE":
                    row[b"RFC822.SIZE"] = len(m.raw)
                elif it == b"INTERNALDATE":
                    row[b"INTERNALDATE"] = m.date
                elif it == b"FLAGS":
                    row[b"FLAGS"] = tuple(sorted(m.flags))
                elif it in (b"BODY.PEEK[]", b"RFC822"):
                    row[b"BODY[]"] = m.raw
                elif b"HEADER.FIELDS" in it:
                    head = m.raw.split(b"\r\n\r\n", 1)[0]
                    keep = [l for l in head.split(b"\r\n") if l.split(b":", 1)[0].upper() in
                            (b"MESSAGE-ID", b"DATE")]
                    row[b"BODY[HEADER.FIELDS (MESSAGE-ID DATE)]"] = b"\r\n".join(keep) + b"\r\n\r\n"
            out[int(uid)] = row
        return out

    def _store(self, uids, flags, add: bool):
        if self.readonly:
            raise IMAPClientError("STORE failed: mailbox is read-only")
        f = self._f()
        for uid in uids:
            m = f.messages.get(int(uid))
            if m is None:
                continue
            for flag in flags:
                flag = flag if isinstance(flag, bytes) else str(flag).encode()
                (m.flags.add if add else m.flags.discard)(flag)

    def add_flags(self, uids, flags, silent=False):
        self._cmd("STORE")
        self._store(uids, flags, True)

    def remove_flags(self, uids, flags, silent=False):
        self._cmd("STORE")
        self._store(uids, flags, False)

    def expunge(self, messages=None):
        if messages is not None:
            self._cmd("UID EXPUNGE")
            if "UIDPLUS" not in self.world.capabilities:
                raise IMAPClientError("EXPUNGE failed: UIDPLUS not supported")
        else:
            self._cmd("EXPUNGE")
        if self.readonly:
            raise IMAPClientError("EXPUNGE failed: read-only")
        f = self._f()
        wanted = None if messages is None else {int(u) for u in messages}
        for uid in list(f.messages):
            if b"\\Deleted" in f.messages[uid].flags and (wanted is None or uid in wanted):
                del f.messages[uid]

    def copy(self, uids, folder):
        self._cmd("COPY")
        if folder not in self.box:
            raise IMAPClientError("copy failed: [TRYCREATE] no such mailbox")
        src, dst = self._f(), self.box[folder]
        for uid in uids:
            m = src.messages.get(int(uid))
            if m is None:
                continue
            new = dst.uidnext
            dst.uidnext += 1
            dst.messages[new] = Msg(raw=m.raw, date=m.date, flags=set(m.flags) - {b"\\Deleted"})

    def move(self, uids, folder):
        if "MOVE" not in self.world.capabilities:
            raise IMAPClientError("MOVE not supported")
        self._cmd("MOVE")
        self.copy(uids, folder)
        f = self._f()
        for uid in uids:
            f.messages.pop(int(uid), None)

    def append(self, folder, msg, flags=(), msg_time=None):
        self._cmd("APPEND")
        f = self.box[folder]
        uid = f.uidnext
        f.uidnext += 1
        f.messages[uid] = Msg(raw=msg, date=msg_time or _dt.datetime.now(_dt.timezone.utc), flags=set(flags or ()))
