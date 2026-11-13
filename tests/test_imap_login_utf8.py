"""Вход в ящик, когда в пароле русские буквы (журнал 12.11.2026: «'ascii' codec can't encode characters»).

imaplib передаёт аргументы LOGIN в ASCII, и такой ящик не копировался вовсе. Проверяется настоящими IMAPClient и
imaplib против маленького сервера на сокете: пароль уходит в UTF-8 — AUTHENTICATE PLAIN, а если сервер его не
объявляет, — LOGIN с паролем литералом.
"""
import base64
import socket
import threading

import pytest

from mailarchiver import models
from mailarchiver.errors import ImapAuthError
from mailarchiver.imap import client as client_mod

USER = "vechkitov@corp.test"
PASSWORD = "1Вла-2026!"


class _Server:
    """IMAP-сервер на одно соединение за раз: CAPABILITY, LOGIN (и с литералом), AUTHENTICATE PLAIN, LOGOUT."""

    def __init__(self, caps: str):
        self.caps = caps
        self.seen = []                       # (способ, логин, пароль)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        f = conn.makefile("rb")

        def send(text):
            conn.sendall(text.encode("utf-8") + b"\r\n")

        send(f"* OK [CAPABILITY IMAP4rev1 {self.caps}] test ready")
        while True:
            line = f.readline()
            if not line:
                return
            tag, _, rest = line.rstrip(b"\r\n").partition(b" ")
            tag = tag.decode()
            cmd, _, args = rest.partition(b" ")
            cmd = cmd.upper()
            if cmd == b"CAPABILITY":
                send(f"* CAPABILITY IMAP4rev1 {self.caps}")
                send(f"{tag} OK done")
            elif cmd == b"LOGIN":
                user, _, pw = args.partition(b" ")
                user = user.strip(b'"')
                if pw.startswith(b"{") and pw.endswith(b"}"):
                    send("+ go ahead")
                    data = f.read(int(pw[1:-1]))
                    f.readline()                  # конец строки команды после литерала
                    self.seen.append(("literal", user.decode(), data.decode("utf-8")))
                    ok = data == PASSWORD.encode("utf-8")
                else:
                    pw = pw.strip(b'"')
                    self.seen.append(("login", user.decode(), pw.decode()))
                    ok = pw == b"ascii-pass"
                send(f"{tag} OK logged in" if ok and user == USER.encode() else
                     f"{tag} NO [AUTHENTICATIONFAILED] Authentication failed.")
            elif cmd == b"AUTHENTICATE":
                send("+ ")
                data = base64.b64decode(f.readline().strip())
                _authz, user, pw = data.split(b"\0")
                self.seen.append(("plain", user.decode("utf-8"), pw.decode("utf-8")))
                ok = user == USER.encode() and pw == PASSWORD.encode("utf-8")
                send(f"{tag} OK authenticated" if ok else f"{tag} NO [AUTHENTICATIONFAILED] Authentication failed.")
            elif cmd == b"LOGOUT":
                send("* BYE bye")
                send(f"{tag} OK done")
                conn.close()
                return
            else:
                send(f"{tag} OK done")

    def close(self):
        self.sock.close()


@pytest.fixture()
def server_factory():
    made = []

    def make(caps):
        srv = _Server(caps)
        made.append(srv)
        return srv
    yield make
    for srv in made:
        srv.close()


def _connect(port, password, user=USER):
    acc = models.Account(id=1, name="Вечкитов Владимир Алексеевич", host="127.0.0.1", port=port,
                         security=models.Security.PLAIN, username=user, password=password)
    conn = client_mod.ImapConnection(acc, client_mod.ConnectOptions(connect_timeout_s=5, socket_timeout_s=5))
    conn.connect()
    return conn


def test_russian_password_goes_as_authenticate_plain(server_factory):
    srv = server_factory("AUTH=PLAIN")
    conn = _connect(srv.port, PASSWORD)
    conn.close()
    assert srv.seen == [("plain", USER, PASSWORD)]


def test_russian_password_as_literal_without_auth_plain(server_factory):
    srv = server_factory("")
    conn = _connect(srv.port, PASSWORD)
    assert conn.client._imap.state == "AUTH"
    conn.close()
    assert srv.seen == [("literal", USER, PASSWORD)]


def test_ascii_password_still_plain_login(server_factory):
    srv = server_factory("AUTH=PLAIN")
    conn = _connect(srv.port, "ascii-pass")
    conn.close()
    assert srv.seen == [("login", USER, "ascii-pass")]


@pytest.mark.parametrize("caps", ["AUTH=PLAIN", ""])
def test_wrong_russian_password_hints_at_keyboard_layout(server_factory, caps):
    srv = server_factory(caps)
    with pytest.raises(ImapAuthError) as err:
        _connect(srv.port, "1Вла-неверный")
    assert "русской раскладке" in (err.value.hint or "")
    assert "AUTHENTICATIONFAILED" in err.value.message


def test_russian_login_without_auth_plain_is_explained(server_factory):
    srv = server_factory("")
    with pytest.raises(ImapAuthError) as err:
        _connect(srv.port, PASSWORD, user="вечкитов")
    assert "не латинские" in err.value.message and srv.seen == []


def test_unicode_encode_error_is_not_unexpected():
    exc = None
    try:
        "1Вла".encode("ascii")
    except UnicodeEncodeError as e:
        exc = e
    mapped = client_mod._map_exception(exc)
    assert "Неожиданная" not in mapped.message and "не латинские" in mapped.message


# ---------------------------------------------------------------------------
#  То же для отправки (SMTP): smtplib тоже кодирует AUTH в ASCII
# ---------------------------------------------------------------------------
class _SmtpServer:
    def __init__(self, mechs: str):
        self.mechs = mechs
        self.seen = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        f = conn.makefile("rb")

        def send(text):
            conn.sendall(text.encode() + b"\r\n")
        send("220 test ESMTP")
        while True:
            line = f.readline().rstrip(b"\r\n")
            if not line:
                return
            up = line.upper()
            if up.startswith(b"EHLO"):
                send("250-test")
                send(f"250 AUTH {self.mechs}")
            elif up.startswith(b"AUTH PLAIN "):
                _a, user, pw = base64.b64decode(line.split()[2]).split(b"\0")
                self.seen.append(("plain", user.decode(), pw.decode("utf-8")))
                send("235 ok" if pw == PASSWORD.encode() else "535 bad")
            elif up.startswith(b"AUTH LOGIN "):
                user = base64.b64decode(line.split()[2]).decode()
                send("334 UGFzc3dvcmQ6")
                pw = base64.b64decode(f.readline().strip()).decode("utf-8")
                self.seen.append(("login", user, pw))
                send("235 ok" if pw == PASSWORD else "535 bad")
            elif up.startswith(b"QUIT"):
                send("221 bye")
                conn.close()
                return
            else:
                send("250 ok")

    def close(self):
        self.sock.close()


@pytest.mark.parametrize("mechs,way", [("PLAIN LOGIN", "plain"), ("LOGIN", "login")])
def test_smtp_login_with_russian_password(mechs, way):
    import smtplib

    from mailarchiver.util import smtp_login
    srv = _SmtpServer(mechs)
    try:
        s = smtplib.SMTP("127.0.0.1", srv.port, timeout=5)
        smtp_login(s, USER, PASSWORD)
        s.quit()
        assert srv.seen == [(way, USER, PASSWORD)]
    finally:
        srv.close()


def test_smtp_wrong_russian_password_is_auth_error():
    import smtplib

    from mailarchiver.util import smtp_login
    srv = _SmtpServer("PLAIN")
    try:
        s = smtplib.SMTP("127.0.0.1", srv.port, timeout=5)
        with pytest.raises(smtplib.SMTPAuthenticationError):
            smtp_login(s, USER, "1Вла-неверный")
        s.close()
    finally:
        srv.close()
