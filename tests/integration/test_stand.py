"""Интеграционные тесты с настоящими серверами (1.22.0): Dovecot (IMAP, IDLE, LMTP, ManageSieve, master users),
Postfix (SMTP, журналирование always_bcc), OpenLDAP (memberOf), Radicale (CalDAV, CardDAV).

Запуск: ``sudo scripts/integration/stand.sh start`` и ``MA_IT=1 python -m pytest tests/integration``
(переменные — из ``$MA_STAND_DIR/env``, по умолчанию /tmp/ma-stand/env). Без стенда тесты пропускаются.
"""
import imaplib
import os
import smtplib
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest


def _env():
    path = os.path.join(os.environ.get("MA_STAND_DIR", "/tmp/ma-stand"), "env")
    out = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            k, _, v = line.strip().partition("=")
            if k:
                out[k] = v
    out.update({k: v for k, v in os.environ.items() if k.startswith("MA_IT")})
    return out


ENV = _env()
pytestmark = pytest.mark.skipif(os.environ.get("MA_IT") != "1" or "MA_IT_IMAP" not in ENV,
                                reason="нет стенда: sudo scripts/integration/stand.sh start, затем MA_IT=1")
IMAP_HOST, IMAP_PORT = (ENV.get("MA_IT_IMAP", "127.0.0.1:1143").split(":") + ["1143"])[:2]
SMTP_HOST, SMTP_PORT = (ENV.get("MA_IT_SMTP", "127.0.0.1:2526").split(":") + ["2526"])[:2]
PASS = ENV.get("MA_IT_PASS", "")


def _unique():
    return uuid.uuid4().hex[:10]


def _deliver(to, subject, body="Текст", frm="client@ext.test", attach=None, extra=None):
    """Письмо через настоящий Postfix: SMTP → LMTP → Dovecot (с фильтрами Sieve получателя)."""
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, to, subject
    m["Message-ID"] = f"<{_unique()}@ext.test>"
    for k, v in (extra or {}).items():
        m[k] = v
    m.set_content(body)
    if attach:
        name, data, mt, st = attach
        m.add_attachment(data, maintype=mt, subtype=st, filename=name)
    with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT), timeout=20) as s:
        s.send_message(m)
    return m


def _wait(predicate, timeout=20.0, what="условие"):
    end = time.time() + timeout
    while time.time() < end:
        res = predicate()
        if res:
            return res
        time.sleep(0.3)
    raise AssertionError(f"не дождались: {what}")


def _imap_count(user, folder="INBOX", subject=None):
    c = imaplib.IMAP4(IMAP_HOST, int(IMAP_PORT))
    try:
        c.login(user, PASS)
        typ, data = c.select(f'"{folder}"')
        if typ != "OK":
            return 0
        if subject:                                      # ищем по латинскому хвосту темы (_unique)
            typ, data = c.search(None, "SUBJECT", subject.split()[-1])
            return len(data[0].split()) if data and data[0] else 0
        return int(data[0])
    finally:
        c.logout()


@pytest.fixture()
def stand(services):
    from mailarchiver import models
    svc = services
    login = "ivanov@corp.test"
    acc_id = svc.db.create_account(models.Account(name="Иванов", host=IMAP_HOST, port=int(IMAP_PORT),
                                                  security=models.Security.PLAIN, username=login, password=PASS))
    return svc, svc.db.get_account(acc_id)


def _backup(svc, acc):
    from mailarchiver.models import JobType
    from mailarchiver.queue.jobs import JobContext, handle_backup
    job = svc.db.enqueue_job(JobType.BACKUP, acc.id, {}, 5, 1, "it")
    return handle_backup(JobContext(svc, job, JobType.BACKUP, acc.id, {}))


def test_backup_and_webmail_against_dovecot(stand):
    """Копия настоящего ящика Dovecot; клиент: флаги, перенос, удаление — на сервере."""
    from mailarchiver import webmail as wm
    svc, acc = stand
    subj = f"Договор {_unique()}"
    _deliver("ivanov@corp.test", subj, attach=("счёт.txt", "Поставка арматуры".encode(), "text", "plain"))
    _wait(lambda: _imap_count("ivanov@corp.test", subject=subj), what="доставка через Postfix и LMTP")
    res = _backup(svc, acc)
    assert res["final_status"] in ("success", "partial"), res
    row = svc.db.query_one("SELECT id, folder FROM messages WHERE account_id=? AND subject=?", (acc.id, subj))
    assert row is not None and row["folder"] == "INBOX"
    folders = {f.name: f for f in wm.refresh_folders(svc, acc)}
    assert {"INBOX", "Sent", "Drafts", "Trash", "Archive"} <= set(folders)
    wm.set_flags(svc, acc, [row["id"]], add=["\\Flagged"])
    wm.move(svc, acc, [row["id"]], "Archive", actor="it")
    assert _imap_count("ivanov@corp.test", "Archive", subject=subj) == 1
    assert _imap_count("ivanov@corp.test", "INBOX", subject=subj) == 0
    moved = svc.db.query_one("SELECT id FROM messages WHERE account_id=? AND subject=? AND folder='Archive' "
                             "AND gone=0", (acc.id, subj))
    if moved is None:                                  # перенос виден архиву после проверки папки
        wm.sync_folder(svc, acc, "Archive")
        moved = svc.db.query_one("SELECT id FROM messages WHERE account_id=? AND subject=? AND folder='Archive'",
                                 (acc.id, subj))
    wm.delete(svc, acc, [moved["id"]], permanent=True, actor="it")
    assert _imap_count("ivanov@corp.test", "Archive", subject=subj) == 0


def test_send_through_postfix_and_sent_copy(stand):
    """Отправка из клиента: настоящий SMTP Postfix → ящик получателя; копия в «Отправленные» Dovecot."""
    from mailarchiver import webmail as wm
    svc, acc = stand
    wm.save_identity(svc, acc, {"smtp_host": SMTP_HOST, "smtp_port": int(SMTP_PORT), "smtp_security": "none",
                                "smtp_auth": "none", "save_sent": "always"}, admin=True)
    _backup(svc, acc)
    wm.refresh_folders(svc, acc)
    subj = f"Отчёт {_unique()}"
    d = wm.create_draft(svc, acc, "admin:it", {"to_addrs": "Пётр <petrov@corp.test>, , sidorova@corp.test", "subject": subj,
                                                "body": "<p>Добрый день</p>"})
    res = wm.send(svc, acc, d["id"], "admin:it")
    assert res["ok"] and res["recipients"] == 2 and not res["warnings"], res
    _wait(lambda: _imap_count("petrov@corp.test", subject=subj), what="письмо у получателя")
    _wait(lambda: _imap_count("sidorova@corp.test", subject=subj), what="письмо у второго получателя")
    assert _imap_count("ivanov@corp.test", "Sent", subject=subj) == 1


def test_idle_sees_new_mail(stand):
    """IMAP IDLE на настоящем Dovecot: новое письмо видно без ручной проверки."""
    from mailarchiver import webmail_more as wx
    svc, acc = stand
    _backup(svc, acc)
    v0 = wx.version(acc.id)
    wx.want_push(svc, acc)
    try:
        _wait(lambda: wx.push_status(svc, acc.id).get("push") == "idle", what="IDLE запущен")
        _deliver("ivanov@corp.test", f"IDLE {_unique()}")
        _wait(lambda: wx.version(acc.id) > v0, timeout=30, what="IDLE сообщил о новом письме")
    finally:
        wx.stop_push(svc)


def test_sieve_rules_on_server(stand):
    """Правила клиента — сценарием Sieve на настоящем ManageSieve; Dovecot раскладывает письма сам."""
    from mailarchiver import webmail as wm
    from mailarchiver import webmail_more as wx
    from mailarchiver import webmail_tools as wt
    svc, acc = stand
    _backup(svc, acc)
    wm.refresh_folders(svc, acc)
    word = _unique()
    wt.save_rule(svc, acc, {"name": "Тендеры", "conditions": [{"field": "subject", "op": "contains", "value": word}],
                            "actions": [{"type": "move", "folder": "Archive"}]}, actor="it")
    host, port = ENV["MA_IT_SIEVE"].split(":")
    st = wx.sieve_save(svc, acc, {"enabled": True, "host": host, "port": int(port), "security": "none"}, actor="it",
                       force=True)
    assert st["synced_at"] and not st["error"], st
    subj = f"Тендер {word}"
    _deliver("ivanov@corp.test", subj)
    _wait(lambda: _imap_count("ivanov@corp.test", "Archive", subject=subj), what="письмо разложено Sieve в Archive")
    assert _imap_count("ivanov@corp.test", "INBOX", subject=subj) == 0
    wx.sieve_save(svc, acc, {"enabled": False}, actor="it")


def test_master_user_login(services):
    """Вход в ящик через администратора почты (Dovecot master user, логин «ящик*мастер»)."""
    from mailarchiver import models
    svc = services
    if not ENV.get("MA_IT_MASTER"):
        pytest.skip("нет master user")
    muser, mpass = ENV["MA_IT_MASTER"].split(":", 1)
    for k, v in {"enabled": True, "host": IMAP_HOST, "user": muser, "password": mpass, "mode": "master",
                 "separator": "*"}.items():
        svc.set_rt("mailadmin", k, v)
    acc_id = svc.db.create_account(models.Account(name="Петров", host=IMAP_HOST, port=int(IMAP_PORT),
                                                  security=models.Security.PLAIN, username="petrov@corp.test",
                                                  password="", auth_type=models.AuthType.MASTER
                                                  if hasattr(models.AuthType, "MASTER") else models.AuthType.PASSWORD))
    acc = svc.db.get_account(acc_id)
    if acc.auth_type == models.AuthType.PASSWORD:
        pytest.skip("вид входа «через администратора» называется иначе")
    _deliver("petrov@corp.test", f"Master {_unique()}")
    res = _backup(svc, acc)
    assert res["final_status"] in ("success", "partial"), res


def test_journal_from_postfix(services):
    """Журналирование: Postfix (always_bcc) отправляет копию каждого письма в MailArchiver."""
    from mailarchiver import journal, models
    svc = services
    a = svc.db.create_account(models.Account(name="Иванов", host="h", username="ivanov@corp.test", password="p"))
    port = int(ENV.get("MA_IT_JOURNAL_PORT", "2525"))
    for k, v in {"enabled": True, "port": port, "address": "journal@archive.test",
                 "allowed_ips": ["127.0.0.1", "::1"]}.items():
        svc.set_rt("journal", k, v)
    try:
        st = journal.ensure(svc)
        assert st["running"], st
        subj = f"Журнал {_unique()}"
        _deliver("ivanov@corp.test", subj, frm="client@ext.test")
        os.system("postqueue -f >/dev/null 2>&1")              # отложенные копии — сразу
        _wait(lambda: svc.db.query_one("SELECT id FROM messages WHERE account_id=? AND folder='Журнал' AND subject=?",
                                       (a, subj)), timeout=40, what="копия письма в журнале")
        row = svc.db.query_one("SELECT id FROM messages WHERE account_id=? AND subject=?", (a, subj))
        rec = journal.receipt(svc, row["id"])
        assert rec["ip"] == "127.0.0.1" and rec["helo"]
    finally:
        journal.stop()


def _ldap(svc, **extra):
    vals = {"enabled": True, "url": ENV["MA_IT_LDAP"], "starttls": False, "bind_dn": "uid=svc,ou=people,dc=test",
            "bind_password": "svc-pass", "base_dn": "ou=people,dc=test", "user_filter": "(uid={login})",
            "admin_group": "cn=archive-admins,ou=groups,dc=test",
            "operator_group": "cn=archive-operators,ou=groups,dc=test", "nested_groups": False,
            "employee_filter": "(&(objectClass=inetOrgPerson)(mail=*))"}
    vals.update(extra)
    for k, v in vals.items():
        svc.set_rt("ldap", k, v)


def test_ldap_openldap(services):
    """Настоящий OpenLDAP: поиск, пароль, роли по memberOf, справочник сотрудников, сверка пользователей."""
    from mailarchiver import ldapauth
    svc = services
    if not ENV.get("MA_IT_LDAP"):
        pytest.skip("нет LDAP")
    _ldap(svc)
    info = ldapauth.lookup(svc, "anna", PASS)
    assert info["ok"] and info["role"] == "admin" and info["mail"] == "anna@corp.test", info
    assert ldapauth.lookup(svc, "bob", PASS)["role"] == "operator"
    assert ldapauth.lookup(svc, "anna", "wrong")["ok"] is False
    assert ldapauth.lookup(svc, "ivanov", PASS)["role"] == ""
    rows, problems = ldapauth.employee_rows(svc)
    by = {r["email"]: r for r in rows}
    assert {"anna@corp.test", "bob@corp.test", "ivanov@corp.test"} <= set(by) and not problems
    assert by["ivanov@corp.test"]["phone"] == "+7 495 000-00-01" and by["ivanov@corp.test"]["external_id"] == "1001"
    svc.db.create_user("root", "x", "admin")                  # последнего администратора сверка не трогает
    uid = svc.db.create_user("bob", "x", "admin")
    svc.db.execute("UPDATE users SET auth_source='ldap' WHERE id=?", (uid,))
    svc.set_rt("ldap", "sync_users", True)
    res = ldapauth.sync_users(svc)
    assert res["checked"] == 1 and res["roles"] == 1 and svc.db.get_user_by_id(uid)["role"] == "operator"


def test_caldav_carddav_radicale(stand):
    """Настоящий Radicale: поиск календаря, события (добавить, изменить, удалить), контакты в обе стороны."""
    import base64
    import urllib.request
    from mailarchiver import webmail_dav as wdav
    from mailarchiver import webmail_tools as wt
    svc, acc = stand
    base = ENV.get("MA_IT_DAV")
    if not base:
        pytest.skip("нет Radicale")
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    auth = "Basic " + base64.b64encode(f"ivanov@corp.test:{PASS}".encode()).decode()
    for path, body in ((f"/ivanov@corp.test/cal-{_unique()}/",
                        '<?xml version="1.0"?><c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                        '<d:set><d:prop><d:displayname>Работа</d:displayname></d:prop></d:set></c:mkcalendar>'),):
        req = urllib.request.Request(base + path, data=body.encode(), method="MKCALENDAR",
                                     headers={"Authorization": auth, "Content-Type": "application/xml"})
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10).read()
    book = f"/ivanov@corp.test/book-{_unique()}/"
    req = urllib.request.Request(base + book, method="MKCOL", headers={"Authorization": auth,
                                 "Content-Type": "application/xml"}, data=(
        '<?xml version="1.0"?><d:mkcol xmlns:d="DAV:" xmlns:a="urn:ietf:params:xml:ns:carddav"><d:set><d:prop>'
        '<d:resourcetype><d:collection/><a:addressbook/></d:resourcetype><d:displayname>Контакты</d:displayname>'
        '</d:prop></d:set></d:mkcol>').encode())
    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10).read()
    cfg = wdav.configure(svc, acc, base + "/", actor="it")
    assert cfg["calendar"] and cfg["addressbook"], cfg
    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0, second=0)
    wdav.add_event(svc, acc, summary="Планёрка, важно", start=start.isoformat(), location="Зал 1", actor="it")
    ev = [e for e in wdav.events(svc, acc) if e["summary"] == "Планёрка, важно"][0]
    wdav.update_event(svc, acc, ev["href"], ev["etag"], summary="Планёрка перенесена",
                      start=(start + timedelta(hours=1)).isoformat(), actor="it")
    ev = [e for e in wdav.events(svc, acc) if e["summary"] == "Планёрка перенесена"][0]
    with pytest.raises(Exception):
        wdav.update_event(svc, acc, ev["href"], '"stale-etag"', summary="X", start=start.isoformat())
    wdav.delete_event(svc, acc, ev["href"], ev["etag"], actor="it")
    assert "Планёрка перенесена" not in [e["summary"] for e in wdav.events(svc, acc)]
    assert wdav.push_contact(svc, acc, name="Сидорова Анна", email="sidorova@corp.test", phone="+7 900")
    res = wdav.sync_contacts(svc, acc)
    assert res["total"] >= 1
    assert "sidorova@corp.test" in {c["email"] for c in wt.list_book(svc, acc.id)}
