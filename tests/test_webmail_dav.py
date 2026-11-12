"""Календарь и контакты сервера (CalDAV, CardDAV — 1.20.0): поиск коллекций, контакты, события, приглашения, API."""
import base64
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import test_webmail as base

from mailarchiver import webmail_dav as wdav
from mailarchiver import webmail_tools as wt
from mailarchiver.errors import ValidationError

world, box, api = base.world, base.box, base.api
smtp = base.smtp

_ms = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" ' \
      'xmlns:a="urn:ietf:params:xml:ns:carddav">{}</d:multistatus>'


def _resp(href, props):
    return f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>" \
           f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"


def _ics(uid, start, summary):
    return (f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:{start:%Y%m%dT%H%M%SZ}\r\n"
            f"DTEND:{start + timedelta(hours=1):%Y%m%dT%H%M%SZ}\r\nSUMMARY:{summary}\r\nLOCATION:Переговорная 3\r\n"
            f"END:VEVENT\r\nEND:VCALENDAR\r\n")


class Dav(BaseHTTPRequestHandler):
    puts = {}
    soon = datetime.now(timezone.utc) + timedelta(days=2)

    def _auth(self):
        ok = self.headers.get("Authorization") == "Basic " + base64.b64encode(b"ivanov@x.ru:pw").decode()
        if not ok:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="dav"')
            self.end_headers()
        return ok

    def _xml(self, body, code=207):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PROPFIND(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.startswith("/.well-known/"):
            self.send_response(301)
            self.send_header("Location", "/dav/")
            self.end_headers()
            return
        if not self._auth():
            return
        p = self.path
        if p == "/dav/":
            self._xml(_ms.format(_resp("/dav/", "<d:resourcetype><d:collection/></d:resourcetype>"
                                               "<d:current-user-principal><d:href>/dav/principals/ivanov/</d:href>"
                                               "</d:current-user-principal>")))
        elif p == "/dav/principals/ivanov/":
            self._xml(_ms.format(_resp(p, "<c:calendar-home-set><d:href>/dav/cal/</d:href></c:calendar-home-set>"
                                          "<a:addressbook-home-set><d:href>/dav/book/</d:href></a:addressbook-home-set>")))
        elif p == "/dav/cal/":
            self._xml(_ms.format(_resp(p, "<d:resourcetype><d:collection/></d:resourcetype>")
                                 + _resp("/dav/cal/work/", "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                                                           "<d:displayname>Работа</d:displayname>")))
        elif p == "/dav/book/":
            self._xml(_ms.format(_resp("/dav/book/contacts/", "<d:resourcetype><d:collection/><a:addressbook/>"
                                                              "</d:resourcetype><d:displayname>Контакты</d:displayname>")))
        else:
            self._xml(_ms.format(""), 404)

    def do_REPORT(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self._auth():
            return
        if self.path == "/dav/book/contacts/":
            cards = ["BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Пётр Петров\r\nEMAIL:petrov@x.ru\r\nEND:VCARD",
                     "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Клиент ООО Ромашка\r\nEMAIL:client@romashka.ru\r\nEND:VCARD"]
            self._xml(_ms.format("".join(_resp(f"/dav/book/contacts/{i}.vcf", f"<a:address-data>{c}</a:address-data>")
                                         for i, c in enumerate(cards))))
        elif self.path == "/dav/cal/work/":
            evs = [_ics("e1", Dav.soon, "Планёрка"), _ics("e0", datetime.now(timezone.utc) - timedelta(days=40), "Старое")]
            evs += list(Dav.puts.values())
            self._xml(_ms.format("".join(_resp(f"/dav/cal/work/{i}.ics", f"<c:calendar-data>{e}</c:calendar-data>")
                                         for i, e in enumerate(evs))))
        else:
            self._xml(_ms.format(""), 404)

    def do_PUT(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        if not self._auth():
            return
        Dav.puts[self.path] = body
        self.send_response(201)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def dav():
    Dav.puts = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Dav)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_discover_contacts_events(box, dav, monkeypatch):
    svc, world, acc = box
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    cfg = wdav.configure(svc, acc, dav, actor="adm")
    assert cfg["calendar"].endswith("/dav/cal/work/") and cfg["addressbook"].endswith("/dav/book/contacts/")
    assert [c["name"] for c in cfg["found"]["calendars"]] == ["Работа"]
    res = wdav.sync_contacts(svc, acc)
    assert res["added"] == 2
    emails = {c["email"] for c in wt.list_book(svc, acc.id)}
    assert {"petrov@x.ru", "client@romashka.ru"} <= emails
    evs = wdav.events(svc, acc)
    assert [e["summary"] for e in evs] == ["Планёрка"] and evs[0]["location"] == "Переговорная 3"
    start = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
    wdav.add_event(svc, acc, summary="Встреча, важно", start=start.isoformat(), location="Офис", actor="adm")
    put = list(Dav.puts.values())[0]
    assert "SUMMARY:Встреча\\, важно" in put and "LOCATION:Офис" in put
    assert "Встреча, важно" in [e["summary"] for e in wdav.events(svc, acc)]
    invite = "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\n" + _ics("inv1", start, "Приглашение").split("\r\n", 1)[1]
    assert wdav.add_invitation(svc, acc, invite, "TENTATIVE")
    stored = [v for k, v in Dav.puts.items() if k.endswith("inv1.ics")][0]
    assert "METHOD:" not in stored and "STATUS:TENTATIVE" in stored
    with pytest.raises(ValidationError):
        wdav.add_event(svc, acc, summary="", start=start.isoformat())


def test_errors_and_https(box, dav, monkeypatch):
    svc, world, acc = box
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with pytest.raises(ValidationError) as ei:
        wdav.discover(acc, "http://dav.example.com/")
    assert "https" in ei.value.message
    acc.password = "bad"
    with pytest.raises(ValidationError) as ei:
        wdav.discover(acc, dav)
    assert "логин и пароль" in ei.value.message
    with pytest.raises(ValidationError):
        wdav.events(svc, acc)                                    # календарь не подключён


def test_api_dav(api, dav, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    admin, mb, svc, a, b = api
    assert mb.put("/api/webmail/dav", json={"account_id": a, "url": dav}).status_code == 403
    r = admin.put("/api/webmail/dav", json={"account_id": a, "url": dav})
    assert r.status_code == 200 and r.json()["calendar"]
    assert mb.get(f"/api/webmail/dav?account_id={a}").json()["can_configure"] is False
    assert [e["summary"] for e in mb.get(f"/api/webmail/calendar?account_id={a}").json()["items"]] == ["Планёрка"]
    assert mb.post("/api/webmail/dav/contacts", json={"account_id": a}).json()["added"] == 2
    start = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert mb.post("/api/webmail/calendar", json={"account_id": a, "summary": "Звонок", "start": start}).status_code == 200
    assert mb.get(f"/api/webmail/calendar?account_id={b}").status_code == 403
