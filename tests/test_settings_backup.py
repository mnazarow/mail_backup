"""Резервная копия настроек и восстановление (1.26.0), подпись снимков базы, учебное разворачивание снимка."""
import base64
import json
import os

import pytest
from fastapi.testclient import TestClient

from mailarchiver import models
from mailarchiver import settings_backup as sb
from mailarchiver.replica import snapshots
from mailarchiver.security import hash_password

HDR = {"X-Requested-With": "fetch"}
PW = "Sw0rdfish!1"
BACKUP_PW = "correct horse battery"


@pytest.fixture()
def servers(tmp_path, monkeypatch):
    """Два сервера (два каталога данных): «источник» и «новый». ``make(name)`` → вошедший администратор."""
    from mailarchiver.web.app import create_app
    opened = []

    def make(name, admin="admin"):
        d = tmp_path / name
        d.mkdir()
        monkeypatch.setenv("MAILARCHIVER_DATA", str(d))
        monkeypatch.delenv("MAILARCHIVER_CONFIG", raising=False)
        c = TestClient(create_app(), headers=HDR)
        c.__enter__()
        opened.append(c)
        assert c.post("/api/setup", json={"username": admin, "password": PW}).status_code == 200
        assert c.post("/api/login", json={"username": admin, "password": PW}).status_code == 200
        return c
    yield make
    for c in reversed(opened):
        c.__exit__(None, None, None)


def _fill_source(c):
    """Всего понемногу: параметры, подключение, сотрудники, ящики, расписания, доступ, пользователи, почта."""
    from mailarchiver import apitokens
    from mailarchiver.mailservers import store as ms
    svc = c.app.state.services
    r = c.put("/api/settings", json={"values": {
        "retention.keep_days": 400, "notifications.smtp_host": "smtp.x.ru",
        "notifications.smtp_password": "SmtpSecret-9", "ldap.url": "ldaps://dc.x.ru",
        "ldap.bind_password": "LdapSecret-3"}})
    assert r.status_code == 200, r.text
    sid = ms.create_server(svc.db, type="zimbra", name="Почта офиса", settings={"url": "https://z.x.ru"},
                           secrets={"password": "ZimbraSecret-5"})
    emp = svc.db.create_employee(full_name="Иванов Иван", email="ivanov@x.ru", external_id="T-1")
    boss = svc.db.create_employee(full_name="Петров Пётр", email="petrov@x.ru")
    a = svc.db.create_account(models.Account(name="Иванов", host="imap.x.ru", username="ivanov@x.ru",
                                             password="MailPw-1", folder_exclude=["Спам"]))
    b = svc.db.create_account(models.Account(name="Общий", host="z.x.ru", username="info@x.ru",
                                             auth_type=models.AuthType.SERVER, server_id=sid))
    svc.db.link_employee_account(emp, a)
    svc.db.create_schedule(a, "cron", "backup", "0 2 * * *")
    svc.db.create_schedule(None, "cron", "server_action", "0 3 * * *", options={"action": "purge"})
    from mailarchiver import delegation
    delegation.grant(svc, "admin", b, boss, "full")
    oper = svc.db.create_user("oper", hash_password("OperPass-77"), "operator")
    admin_user = svc.db.get_user_by_name("admin")
    tok = apitokens.create(svc, dict(admin_user), name="Zabbix", role="operator", days=0, user_id=oper)
    box = svc.db.secret
    svc.db.execute("INSERT INTO webmail_identities(account_id, display_name, signature, smtp_password) "
                   "VALUES(?,?,?,?)", (a, "Иван Иванов", "<p>С уважением</p>", box.encrypt("SmtpOwn-2")))
    svc.db.execute("INSERT INTO webmail_rules(account_id, name, conditions, actions) VALUES(?,?,?,?)",
                   (a, "Счета", '[{"field":"subject","op":"contains","value":"счёт"}]',
                    '[{"type":"move","folder":"Счета"}]'))
    svc.db.execute("INSERT INTO webmail_tags(account_id, name, color, keyword) VALUES(?,?,?,?)",
                   (a, "Срочно", "#ff0000", "$urgent"))
    svc.db.execute("INSERT INTO webmail_templates(owner, name, subject, body) VALUES(?,?,?,?)",
                   (f"mailbox:emp:{emp}", "Ответ клиенту", "Re:", "<p>Спасибо</p>"))
    svc.db.execute("INSERT INTO webmail_templates(owner, name, subject, body) VALUES(?,?,?,?)",
                   ("admin:Admin", "Для админа", "", ""))
    svc.db.execute("INSERT INTO webmail_book(account_id, name, email) VALUES(?,?,?)", (a, "Клиент", "client@ext.ru"))
    svc.db.execute("INSERT INTO webmail_smime_keys(account_id, cert_pem, key_enc) VALUES(?,?,?)",
                   (a, "CERT", box.encrypt("PRIVATE-KEY-PEM")))
    return {"emp": emp, "a": a, "b": b, "sid": sid, "token": tok["token"]}


def _restore(c, data, *, password="", mode="add", dry=True, sections=None):
    form = {"password": password, "mode": mode, "dry_run": "true" if dry else "false"}
    if sections:
        form["sections"] = ",".join(sections)
    return c.post("/api/settings-backup/restore", files={"file": ("copy.json", data)}, data=form)


def test_plain_backup_has_no_secrets_and_restores_structure(servers):
    src = servers("src")
    _fill_source(src)
    r = src.post("/api/settings-backup/export", json={})
    assert r.status_code == 200 and "mailarchiver-settings-" in r.headers["content-disposition"]
    data = r.content
    for secret in (b"MailPw-1", b"SmtpSecret-9", b"LdapSecret-3", b"ZimbraSecret-5", b"SmtpOwn-2", b"PRIVATE-KEY",
                   b"pbkdf2$", b"OperPass"):
        assert secret not in data, secret
    doc = json.loads(data)
    assert doc["encrypted"] is False and "users" not in doc["sections"] and "config" not in doc["sections"]
    assert "webmail_smime_keys" not in doc["sections"]["webmail"]
    assert all(s["job_type"] != "server_action" for s in doc["sections"]["accounts"]["schedules"])
    audit = src.app.state.services.db.query_one("SELECT detail FROM audit WHERE action='settings_backup'")
    assert "без пароля" in audit["detail"]

    dst = servers("dst")
    svc = dst.app.state.services
    rep = _restore(dst, data).json()
    assert rep["dry_run"] and rep["tables"]["accounts"]["added"] == 2 and not svc.db.list_accounts()  # проверка
    rep = _restore(dst, data, dry=False).json()
    assert rep["totals"]["added"] >= 8 and rep["auto_backup"].startswith("before-restore-")
    accs = {a.username: a for a in svc.db.list_accounts()}
    assert accs["ivanov@x.ru"].password == "" and accs["ivanov@x.ru"].folder_exclude == ["Спам"]
    assert accs["info@x.ru"].server_id and accs["ivanov@x.ru"].employee_id
    assert svc.rt("retention", "keep_days") == 400 and not svc.rt("notifications", "smtp_password")
    assert [u["username"] for u in svc.db.list_users()] == ["admin"]            # без пароля — без пользователей
    # шаблон сотрудника — у его новой карточки, правило и метка — у ящика
    emp = svc.db.scalar("SELECT id FROM employees WHERE external_id='T-1'")
    assert svc.db.scalar("SELECT name FROM webmail_templates WHERE owner=?", (f"mailbox:emp:{emp}",)) == \
        "Ответ клиенту"
    assert svc.db.scalar("SELECT owner FROM webmail_templates WHERE name='Для админа'") == "admin:Admin"
    assert svc.db.scalar("SELECT COUNT(*) FROM webmail_rules") == 1
    assert svc.db.scalar("SELECT value FROM webmail_kv WHERE key='tags_seeded'") == "1"
    assert svc.db.scalar("SELECT rights FROM mailbox_delegations") == "full"
    assert svc.db.scalar("SELECT COUNT(*) FROM schedules WHERE job_type='backup'") == 1
    # повторно — всё на месте
    again = _restore(dst, data, mode="update").json()
    assert again["totals"]["added"] == 0 and again["totals"]["updated"] == 0, again["tables"]


def test_protected_backup_moves_everything_to_another_server(servers):
    src = servers("src")
    made = _fill_source(src)
    data = src.post("/api/settings-backup/export", json={"password": BACKUP_PW}).content
    for secret in (b"MailPw-1", b"SmtpSecret-9", b"ivanov@x.ru", b"pbkdf2$"):
        assert secret not in data, secret                                    # зашифрован целиком
    doc = json.loads(data)
    assert doc["encrypted"] and doc["kdf"]["name"] == "scrypt" and "sections" not in doc

    dst = servers("dst", admin="boss")
    svc = dst.app.state.services
    r = _restore(dst, data)
    assert r.status_code == 400 and r.json()["code"] == "password_required"
    r = _restore(dst, data, password="wrong password!")
    assert r.status_code == 400 and r.json()["code"] == "password_wrong"
    rep = _restore(dst, data, password=BACKUP_PW, dry=False).json()
    assert rep["secrets"] and rep["has_config"] is False                     # config.yaml в тестах нет
    accs = {a.username: a for a in svc.db.list_accounts()}
    assert accs["ivanov@x.ru"].password == "MailPw-1"                         # перешифрован своим ключом
    assert svc.rt("notifications", "smtp_password") == "SmtpSecret-9" and svc.rt("ldap", "bind_password")
    from mailarchiver.mailservers import store as ms
    assert ms.list_servers(svc.db)[0].secrets["password"] == "ZimbraSecret-5"
    assert svc.db.secret.decrypt(svc.db.scalar("SELECT smtp_password FROM webmail_identities")) == "SmtpOwn-2"
    assert svc.db.secret.decrypt(svc.db.scalar("SELECT key_enc FROM webmail_smime_keys")) == "PRIVATE-KEY-PEM"
    # пользователи и токены переехали: оператор входит своим паролем, токен работает
    assert {u["username"] for u in svc.db.list_users()} == {"boss", "admin", "oper"}
    other = TestClient(dst.app, headers=HDR)
    assert other.post("/api/login", json={"username": "oper", "password": "OperPass-77"}).status_code == 200
    tok = TestClient(dst.app, headers=dict(HDR, Authorization="Bearer " + made["token"]))
    assert tok.get("/api/accounts").status_code == 200
    # с токеном копию настроек не сделать и не восстановить
    assert tok.post("/api/settings-backup/export", json={}).status_code == 403
    assert tok.get("/api/settings-backup").status_code == 403


def test_add_and_update_modes_and_undo(servers):
    c = servers("one")
    made = _fill_source(c)
    svc = c.app.state.services
    data = c.post("/api/settings-backup/export", json={"password": BACKUP_PW}).content
    # после копии всё поменяли
    assert c.put("/api/settings", json={"values": {"retention.keep_days": 30}}).status_code == 200
    svc.db.execute("UPDATE accounts SET name='Переименован', enabled=0 WHERE id=?", (made["a"],))
    svc.db.execute("UPDATE schedules SET cron_expr='30 4 * * *' WHERE account_id=?", (made["a"],))
    svc.db.execute("UPDATE users SET role='admin' WHERE username='oper'")
    svc.db.execute("UPDATE users SET password_hash=? WHERE username='admin'", (hash_password("Changed-pass-1"),))
    svc.db.create_session("opertok", int(svc.db.get_user_by_name("oper")["id"]), "2099-01-01T00:00:00+00:00", "", "")
    # «только недостающее» ничего существующего не трогает
    rep = _restore(c, data, password=BACKUP_PW, dry=False).json()
    assert rep["totals"]["added"] == 0 and rep["totals"]["updated"] == 0
    assert rep["tables"]["settings"]["kept"] >= 1 and rep["tables"]["accounts"]["kept"] == 2
    assert svc.db.get_account(made["a"]).name == "Переименован" and svc.rt("retention", "keep_days") == 30
    # «обновить» возвращает значения из копии
    rep = _restore(c, data, password=BACKUP_PW, mode="update").json()
    assert rep["tables"]["accounts"]["updated"] == 1 and rep["tables"]["schedules"]["updated"] == 1
    rep = _restore(c, data, password=BACKUP_PW, mode="update", dry=False).json()
    acc = svc.db.get_account(made["a"])
    assert acc.name == "Иванов" and acc.enabled and svc.rt("retention", "keep_days") == 400
    assert svc.db.scalar("SELECT cron_expr FROM schedules WHERE account_id=?", (made["a"],)) == "0 2 * * *"
    assert svc.db.scalar("SELECT COUNT(*) FROM schedules WHERE account_id=?", (made["a"],)) == 1
    assert svc.db.get_user_by_name("oper")["role"] == "operator"
    assert not svc.db.query_one("SELECT 1 FROM sessions WHERE token='opertok'")   # права изменились — сеанс закрыт
    # свою учётную запись восстановление не меняет (пароль остался новым)
    assert any("не изменена" in n for n in rep["tables"]["users"]["notes"])
    me = TestClient(c.app, headers=HDR)
    assert me.post("/api/login", json={"username": "admin", "password": "Changed-pass-1"}).status_code == 200
    audit = svc.db.query_one("SELECT detail FROM audit WHERE action='settings_restore' ORDER BY id DESC LIMIT 1")
    assert "обновить" in audit["detail"]
    # вернуть настройки, какими они были перед восстановлением
    autos = c.get("/api/settings-backup").json()["auto"]
    assert len(autos) == 2
    r = c.post("/api/settings-backup/restore", data={"auto": autos[1]["name"], "mode": "update", "dry_run": "false"})
    assert r.status_code == 200, r.text
    assert svc.rt("retention", "keep_days") == 30 and svc.db.get_account(made["a"]).name == "Переименован"
    assert c.post("/api/settings-backup/restore", data={"auto": "../secret.key"}).status_code == 400


def test_copies_are_checked(servers, tmp_path):
    a = servers("a")
    _fill_source(a)
    local, _ = sb.build(a.app.state.services, local=True)
    b = servers("b")
    r = _restore(b, local)
    assert r.status_code == 400 and "другим ключом secret.key" in r.json()["message"]
    data = a.post("/api/settings-backup/export", json={"password": BACKUP_PW}).content
    doc = json.loads(data)
    raw = bytearray(base64.b64decode(doc["data"]))
    raw[40] ^= 1
    doc["data"] = base64.b64encode(bytes(raw)).decode()
    assert _restore(b, json.dumps(doc).encode(), password=BACKUP_PW).json()["code"] == "password_wrong"
    doc = json.loads(data)
    doc["kdf"]["n"] = 2 ** 30                                                # не даём занять всю память
    assert "недопустимые параметры" in _restore(b, json.dumps(doc).encode(), password=BACKUP_PW).json()["message"]
    assert "не копия" in _restore(b, b'{"hello": 1}').json()["message"]
    assert "не JSON" in _restore(b, b"\x1f\x8b binary").json()["message"]
    r = a.post("/api/settings-backup/export", json={"password": "short"})
    assert r.status_code == 400 and "не короче" in r.json()["message"]


def test_secret_bound_to_address_is_dropped(servers):
    """Копия без пароля меняет адрес LDAP — сохранённый здесь пароль на новый адрес не уходит."""
    src = servers("src")
    assert src.put("/api/settings", json={"values": {"ldap.url": "ldaps://new.x.ru",
                                                     "replica.enabled": True}}).status_code == 200
    data = src.post("/api/settings-backup/export", json={"sections": ["settings"]}).content
    dst = servers("dst")
    svc = dst.app.state.services
    assert dst.put("/api/settings", json={"values": {"ldap.url": "ldaps://old.x.ru",
                                                     "ldap.bind_password": "Keep-me-9"}}).status_code == 200
    rep = _restore(dst, data, mode="update", dry=False).json()
    notes = " ".join(rep["tables"]["settings"]["notes"])
    assert "ldap.bind_password" in notes and "Копия вне сервера не включена" in notes
    assert svc.rt("ldap", "url") == "ldaps://new.x.ru" and not svc.rt("ldap", "bind_password")
    assert not svc.rt("replica", "enabled")


def test_restore_keeps_an_admin(servers):
    c = servers("one")
    svc = c.app.state.services
    svc.db.create_user("second", hash_password("Second-pass-1"), "admin")
    data, _ = sb.build(svc, password=BACKUP_PW)
    svc.db.execute("UPDATE users SET disabled=1 WHERE username='admin'")     # в копии admin выключен
    opened = sb.open_backup(data, password=BACKUP_PW)
    for row in opened["sections"]["users"]["users"]:
        row["disabled"] = 1
    with pytest.raises(sb.ValidationError, match="ни одного включённого администратора"):
        sb.restore(svc, opened, mode="update", actor="cli", auto_backup=False)
    assert not svc.db.get_user_by_name("second")["disabled"]                 # ничего не изменено


def test_cli_settings_backup_and_restore(servers, tmp_path, capsys, monkeypatch):
    from mailarchiver.__main__ import main
    src = servers("src")
    _fill_source(src)
    pw = tmp_path / "pw.txt"
    pw.write_text(BACKUP_PW + "\n")
    out = tmp_path / "copy.json"
    assert main(["settings-backup", "-o", str(out), "--password-file", str(pw)]) == 0
    assert "с паролем" in capsys.readouterr().out and oct(os.stat(out).st_mode & 0o777) == "0o600"
    assert main(["settings-backup", "-o", str(out), "--no-password"]) == 1    # не перезаписывает
    # на другом сервере: проверка, затем восстановление
    dst = servers("dst")
    svc = dst.app.state.services
    monkeypatch.setenv("MAILARCHIVER_DATA", svc.cfg.data_dir)
    assert main(["settings-restore", str(out), "--password-file", str(pw), "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "ничего не изменено" in text and "Ящики: добавить 2" in text
    assert not svc.db.list_accounts()
    assert main(["settings-restore", str(out), "--password-file", str(pw)]) == 0
    text = capsys.readouterr().out
    assert "Восстановлено" in text and "Перезапустите службу" in text and "before-restore-" in text
    assert {a.username for a in svc.db.list_accounts()} == {"ivanov@x.ru", "info@x.ru"}
    # config.yaml — только извлечь
    conf = tmp_path / "config.yaml"
    conf.write_text("server:\n  port: 8493\n")
    monkeypatch.setenv("MAILARCHIVER_DATA", src.app.state.services.cfg.data_dir)
    assert main(["-c", str(conf), "settings-backup", "-o", str(tmp_path / "c2.json"), "--password-file",
                 str(pw), "--sections", "config,settings"]) == 0
    capsys.readouterr()
    assert main(["settings-restore", str(tmp_path / "c2.json"), "--password-file", str(pw), "--extract-config",
                 str(tmp_path / "restored.yaml")]) == 0
    assert (tmp_path / "restored.yaml").read_text() == "server:\n  port: 8493\n"


# ---------------------------------------------------------------------------
#  Подпись снимков (1.26.0)
# ---------------------------------------------------------------------------
def test_snapshot_signature(services, tmp_path, capsys):
    from mailarchiver.__main__ import main
    from mailarchiver.security import SecretBox
    svc = services
    info = snapshots.make_snapshot(svc, keep=2)
    assert info["signed"] and os.path.exists(info["path"] + ".sig")
    assert snapshots.list_snapshots(svc.cfg)[0]["signed"]
    assert snapshots.verify_signature(svc.db.secret, info["path"])["status"] == "ok"
    assert snapshots.verify_signature(SecretBox(b"x" * 48), info["path"])["status"] == "other_key"
    # подложенный файл с тем же именем
    evil = tmp_path / info["name"]
    with open(info["path"], "rb") as fh:
        data = bytearray(fh.read())
    data[-20] ^= 1
    evil.write_bytes(bytes(data))
    (tmp_path / (info["name"] + ".sig")).write_bytes(open(info["path"] + ".sig", "rb").read())
    assert snapshots.verify_signature(svc.db.secret, str(evil))["status"] == "bad"
    assert main(["restore-snapshot", str(evil), "--force"]) == 1
    err = capsys.readouterr().err
    assert "Подпись не совпадает" in err and "База не изменена" in err
    # чужой ключ — можно указать файл ключа того сервера
    other = tmp_path / "other.key"
    other.write_text("ab" * 48)
    assert main(["restore-snapshot", info["path"], "--force", "--secret-key", str(other)]) == 1
    assert "другим ключом" in capsys.readouterr().err
    # старые снимки удаляются вместе с подписями; подпись без снимка — тоже
    for _ in range(3):
        snapshots.make_snapshot(svc, keep=2)
    names = os.listdir(snapshots.snapshot_dir(svc.cfg))
    assert len([n for n in names if n.endswith(".sig")]) == 2 and len(names) == 4


def test_replica_copies_signatures(services, tmp_path):
    from mailarchiver.replica.runner import ReplicaRunner, build_target, pull
    svc = services
    dest = tmp_path / "replica"
    dest.mkdir()
    svc.set_rt("replica", "target", "dir")
    svc.set_rt("replica", "dir_path", str(dest))
    assert ReplicaRunner(svc).run()["status"] == "success"
    remote = os.listdir(dest / "db")
    assert len(remote) == 2 and any(n.endswith(".sig") for n in remote)
    out = tmp_path / "restored"
    pull(build_target(svc), str(out), include_mail=False)
    got = os.listdir(out / "snapshots")
    assert len(got) == 2 and any(n.endswith(".sig") for n in got)


def test_drill_restores_snapshot_on_trial(services):
    from mailarchiver import drill
    svc = services
    svc.db.create_account(models.Account(name="Ящик", host="h", username="u@x", password="p"))
    info = snapshots.make_snapshot(svc)
    res = drill.run(svc)
    assert res["ok"], res
    assert "Снимок базы" in res["summary"] and "ящиков 1" in res["summary"]
    assert not [n for n in os.listdir(svc.cfg.tmp_dir) if n.startswith("drill-")]
    # повреждённый снимок — проблема и уведомление
    with open(info["path"], "r+b") as fh:
        fh.seek(-30, 2)
        fh.write(b"\0" * 10)
    res = drill.run(svc)
    assert not res["ok"] and res["bad"] == 1 and "Подпись не совпадает" in res["summary"]
    os.unlink(info["path"] + ".sig")                     # без подписи — снимок всё равно разворачивается
    res = drill.run(svc)
    assert not res["ok"] and ("не разворачивается" in res["summary"] or "не читается" in res["summary"])
    svc.set_rt("drill", "snapshot", False)
    res = drill.run(svc)
    assert res["ok"] and "Снимок базы" not in res["summary"]


def test_damaged_rows_are_skipped(servers):
    """Правленный вручную или повреждённый файл: плохие строки пропускаются с пояснением, остальное — на месте."""
    c = servers("one")
    svc = c.app.state.services
    svc.db.create_schedule(None, "cron", "backup_all", "0 1 * * *", options={"pause_seconds": 5})
    data = json.loads(c.post("/api/settings-backup/export", json={}).content)
    part = data["sections"]["accounts"]
    part["accounts"] = [{"host": {"x": 1}, "username": ["a"]}, "строка", {"name": "без ключа"},
                        {"name": "Новый", "host": "imap.y.ru", "username": "new@y.ru", "enabled": True}]
    part["schedules"] += [{"account": ["imap.x.ru", "nobody@x.ru"], "kind": "cron", "job_type": "backup",
                           "cron_expr": "0 2 * * *"},
                          {"account": None, "kind": "cron", "job_type": "backup", "cron_expr": "0 2 * * *"},
                          {"account": ["imap.y.ru", "new@y.ru"], "kind": "cron", "job_type": "backup",
                           "cron_expr": "* * *"},
                          {"account": ["imap.y.ru", "new@y.ru"], "kind": "cron", "job_type": "export",
                           "cron_expr": "0 2 * * *"}]
    data["sections"]["settings"]["retention.keep_days"] = "много"
    data["sections"]["settings"]["future.option"] = 1
    data["sections"]["settings"]["server.port"] = 1
    svc.db.execute("DELETE FROM schedules")
    r = _restore(c, json.dumps(data).encode(), dry=False)
    assert r.status_code == 200, r.text
    rep = r.json()["tables"]
    assert rep["accounts"]["added"] == 1 and rep["accounts"]["skipped"] == 3
    assert rep["schedules"]["added"] == 1 and rep["schedules"]["skipped"] == 4
    notes = " ".join(rep["settings"]["notes"])
    assert "этой версии неизвестен" in notes and "целым числом" in notes and "только в config.yaml" in notes
    acc = next(a for a in svc.db.list_accounts() if a.username == "new@y.ru")
    assert acc.enabled and svc.db.scalar("SELECT job_type FROM schedules") == "backup_all"
