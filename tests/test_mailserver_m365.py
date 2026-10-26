"""Подключение Microsoft 365: учебный Microsoft Graph (tests/mailserver_fake.py) и IMAP с токеном приложения.

Проверяется весь путь: подключение и проверка, сверка (пользователи, занятое место из отчёта, последний вход),
«Ящики на сервере» (ключи ms:…), действия раздела через драйвер, удаление только при полном архиве,
создание учётных записей и то, что секреты не попадают в журнал подключения.
"""
import pytest

import m365_fake
from imap_fake import ImapWorld, raw_message
from m365_fake import SECRET, TENANT
from mailarchiver import models
from mailarchiver.errors import ValidationError
from mailarchiver.imap import client as client_mod
from mailarchiver.mailservers import service as mss
from mailarchiver.mailservers import store as msstore
from mailarchiver.mailservers.drivers.m365 import RULE_NAME, M365Driver
from mailarchiver.mailservers.http import TOKENS
from mailarchiver.models import AuthType, JobType
from mailarchiver.queue.jobs import HANDLERS, JobContext
from mailarchiver.serverops import base, create
from mailarchiver.serverops import targets as tg

ADMIN = {"username": "tester", "role": "admin"}


@pytest.fixture()
def graph(monkeypatch):
    TOKENS._tokens.clear()
    g, fake = m365_fake.start()
    monkeypatch.setattr(M365Driver, "GRAPH_URL", fake.url + "/v1.0")
    monkeypatch.setattr(M365Driver, "LOGIN_URL", fake.url)
    yield g
    fake.close()
    TOKENS._tokens.clear()


def connect(svc, **extra):
    values = {"type": "m365", "name": "Контосо", "tenant_id": TENANT, "client_id": "app-1", "auth": "secret",
              "client_secret": SECRET, "allow_changes": True}
    values.update(extra)
    return mss.save(svc, "tester", values)


def run_job(svc, job_type, params):
    job_id = svc.db.enqueue_job(job_type, None, params, 5, 1, "tester")
    ctx = JobContext(svc, job_id, job_type, None, params)
    return HANDLERS[job_type](ctx)


def act(svc, action, keys, **options):
    res = run_job(svc, JobType.SERVER_ACTION, {"action": action, "keys": keys, "options": options,
                                               "requested_by": "tester", "op_id": None})
    return res, {i["key"]: i for i in res["items"]}


@pytest.fixture()
def m365(services, graph):
    sid = connect(services)
    res = mss.sync(services, sid, user="tester")
    return type("M", (), {"svc": services, "g": graph, "sid": sid, "sync": res,
                          "iv": f"ms:{sid}:ivanov@contoso.ru", "pe": f"ms:{sid}:petrova@contoso.ru"})


def test_check_reads_org_and_licenses(services, graph):
    sid = connect(services)
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out
    st = out["status"]
    assert st["seats_total"] == 10 and st["seats_used"] == 3
    assert any("Контосо" in d for d in st["details"])
    assert not st["problems"]
    conn = msstore.get_server(services.db, sid)
    assert conn.last_check_ok is True


def test_wrong_secret_is_explained_and_not_logged(services, graph):
    sid = connect(services, client_secret="wrong-Secret-9")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "сервер токенов отказал" in out["error"] and "Invalid client secret" in out["error"]
    for row in msstore.list_log(services.db, sid):
        text = msstore.get_log(services.db, row["id"])["transcript"]
        assert "wrong-Secret-9" not in text and SECRET not in text


def test_sync_users_usage_and_last_login(m365):
    rows = {r["email"]: r for r in msstore.list_accounts(m365.svc.db, m365.sid)}
    assert set(rows) == {"ivanov@contoso.ru", "petrova@contoso.ru", "noreply@contoso.ru"}   # гостей нет
    iv = rows["ivanov@contoso.ru"]
    assert iv["aliases"] == ["ivan@contoso.ru"] and iv["login_blocked"] is False
    assert iv["used_bytes"] == 5368709120 and iv["quota_kb"] == 107374182400 // 1024 and iv["messages"] == 1200
    assert iv["last_login"] == "2026-10-20T08:00:00Z" and iv["department"] == "Продажи"
    assert rows["petrova@contoso.ru"]["last_login"] == ""           # «никогда» — не дата
    assert rows["noreply@contoso.ru"]["login_blocked"] is True and rows["noreply@contoso.ru"]["kind"] == "unlicensed"
    assert m365.sync["incomplete"] is False and not m365.sync["problems"]
    # страницы — по ссылке @odata.nextLink
    assert len(m365.g.fake.calls_to("GET", r"/v1.0/users")) >= 2
    # ни токен, ни секрет, ни ссылка на файл отчёта (она сама — доступ) не попали в журнал подключения
    for row in msstore.list_log(m365.svc.db, m365.sid):
        text = msstore.get_log(m365.svc.db, row["id"])["transcript"]
        assert SECRET not in text and "tok-graph" not in text
        assert "report.csv" not in text and "/download/" not in text
    assert any("ссылка на файл отчёта" in msstore.get_log(m365.svc.db, row["id"])["transcript"]
               for row in msstore.list_log(m365.svc.db, m365.sid))


def test_report_link_not_in_errors(services, graph):
    """Файл отчёта не скачался — в заметке журнала нет его адреса (ссылки с доступом)."""
    fake = graph.fake
    fake.routes.insert(0, ("GET", __import__("re").compile(r"^/download/report\.csv$"),
                           lambda req: (404, {"error": {"code": "NotFound", "message": "gone"}})))
    sid = connect(services)
    mss.sync(services, sid, user="tester")
    text = "\n".join(msstore.get_log(services.db, row["id"])["transcript"] for row in msstore.list_log(services.db, sid))
    assert "отчёт об использовании ящиков не прочитан" in text and "report.csv" not in text


def test_last_login_kept_when_one_account_is_reread(m365):
    """Одна учётная запись читается без signInActivity — последний вход из сверки не стирается."""
    svc = m365.svc
    conn = msstore.get_server(svc.db, m365.sid)
    with mss.session(svc, conn) as drv:
        raw = drv.get_account("ivanov@contoso.ru")
    assert raw is not None and "last_login" not in raw
    mss.refresh_account(svc, conn, "ivanov@contoso.ru")
    assert msstore.get_account(svc.db, m365.sid, "ivanov@contoso.ru")["last_login"] == "2026-10-20T08:00:00Z"
    _res, items = act(svc, "ax_contact", [m365.iv], source="fixed", department="ИТ", overwrite=True)
    assert items[m365.iv]["status"] == "success", items
    assert msstore.get_account(svc.db, m365.sid, "ivanov@contoso.ru")["last_login"] == "2026-10-20T08:00:00Z"
    # сверка с последним входом — по-прежнему обновляет его, в том числе на «никогда»
    m365.g.users["u1"]["signInActivity"] = {"lastSignInDateTime": "2026-10-26T09:00:00Z"}
    mss.sync(svc, m365.sid, user="tester")
    assert msstore.get_account(svc.db, m365.sid, "ivanov@contoso.ru")["last_login"] == "2026-10-26T09:00:00Z"
    m365.g.users["u1"]["signInActivity"] = {"lastSignInDateTime": "0001-01-01T00:00:00Z"}
    mss.sync(svc, m365.sid, user="tester")
    assert msstore.get_account(svc.db, m365.sid, "ivanov@contoso.ru")["last_login"] == ""
    # без P1 последний вход не читается — прежнее значение остаётся, а не становится «никогда»
    msstore.update_fields(svc.db, m365.sid, "ivanov@contoso.ru", last_login="2026-10-01T00:00:00Z")
    m365.g.no_p1 = True
    mss.sync(svc, m365.sid, user="tester")
    assert msstore.get_account(svc.db, m365.sid, "ivanov@contoso.ru")["last_login"] == "2026-10-01T00:00:00Z"


def test_sync_without_p1_and_with_concealed_report(services, graph):
    graph.no_p1 = True
    graph.concealed = True
    sid = connect(services)
    res = mss.sync(services, sid, user="tester")
    rows = {r["email"]: r for r in msstore.list_accounts(services.db, sid)}
    assert rows["ivanov@contoso.ru"]["last_login"] == "" and rows["ivanov@contoso.ru"]["used_bytes"] is None
    assert any("скрыты имена" in p for p in res["problems"])


def test_server_targets_and_matching(m365):
    svc = m365.svc
    linked = svc.db.create_account(models.Account(name="Иванов", host="outlook.office365.com",
                                                  username="ivanov@contoso.ru", password="pw"))
    items = {t.key: t for t in tg.load(svc)}
    assert items[m365.iv].mailbox is not None and items[m365.iv].mailbox.id == linked
    assert items[m365.iv].kind == "server" and items[m365.iv].conn.id == m365.sid
    assert items[m365.pe].mailbox is None
    row = tg.row_dict(svc, items[m365.iv], {}, {})
    assert row["server"]["type"] == "m365" and "block" in row["server"]["caps"]


def imap_world(monkeypatch):
    world = ImapWorld()
    world.mailbox("petrova@contoso.ru", "nopass")
    world.add("petrova@contoso.ru", "INBOX", raw_message("Отчёт за квартал"))
    world.tokens["tok-imap"] = {"*"}
    monkeypatch.setattr(client_mod, "IMAPClient", world.client_class())
    return world


def test_add_mailbox_then_copy_with_app_token(m365, monkeypatch):
    svc = m365.svc
    world = imap_world(monkeypatch)
    res = base.run(svc, ADMIN, "ax_add_mailboxes", [m365.pe], {}, preview=False)
    assert res["counts"]["ok"] == 1, res
    acc = next(a for a in svc.db.list_accounts() if a.username == "petrova@contoso.ru")
    assert acc.auth_type == AuthType.SERVER and acc.server_id == m365.sid
    assert acc.host == "outlook.office365.com" and acc.password == ""
    from mailarchiver.imap.client import ImapConnection
    conn = ImapConnection(acc, svc.connect_options())
    conn.connect()
    try:
        assert world.last_login == ("xoauth2", "petrova@contoso.ru", "petrova@contoso.ru")
    finally:
        conn.close()
    # тот же ящик второй раз не заводится
    again = base.run(svc, ADMIN, "ax_add_mailboxes", [m365.pe], {}, preview=True)
    assert again["counts"]["skip"] == 1 and "уже копируется" in again["results"][0]["detail"]


def test_capabilities_skip_with_reason(m365):
    svc = m365.svc
    pv = base.run(svc, ADMIN, "ax_quota", [m365.iv], {"quota_mb": 2048}, preview=True)
    assert pv["counts"]["skip"] == 1 and "квота ящика — этот сервер так не умеет" in pv["results"][0]["detail"]
    pv = base.run(svc, ADMIN, "ax_alias_add", [m365.iv], {"template": "{f}.{last}"}, preview=True)
    assert "псевдонимы" in pv["results"][0]["detail"]
    # действие только Axigen — без настроенного Axigen оно недоступно вовсе
    with pytest.raises(ValidationError):
        base.run(svc, ADMIN, "ax_account_type", [m365.iv], {}, preview=True)


def test_changes_need_permission_per_connection(services, graph):
    sid = connect(services, allow_changes=False)
    mss.sync(services, sid, user="tester")
    pv = base.run(services, ADMIN, "ax_block", [f"ms:{sid}:ivanov@contoso.ru"], {"final_copy": False}, preview=True)
    assert pv["counts"]["skip"] == 1 and "изменения на сервере «Контосо» не разрешены" in pv["results"][0]["detail"]
    # чтение — можно
    pv = base.run(services, ADMIN, "ax_refresh", [f"ms:{sid}:ivanov@contoso.ru"], {}, preview=True)
    assert pv["counts"]["ok"] == 1


def test_block_password_contact_unblock(m365):
    svc, g = m365.svc, m365.g
    _res, items = act(svc, "ax_block", [m365.pe], final_copy=False, keep_receiving=True)
    assert items[m365.pe]["status"] == "success", items
    assert g.users["u2"]["accountEnabled"] is False and "сеансы завершены" in items[m365.pe]["detail"]
    assert msstore.get_account(svc.db, m365.sid, "petrova@contoso.ru")["login_blocked"] is True
    # пароль — случайный, сложный, с требованием сменить
    _res, items = act(svc, "ax_password", [m365.iv], mode="random_hidden", must_change=True)
    assert items[m365.iv]["status"] == "success", items
    prof = g.settings["u1"]["password"]
    assert prof["forceChangePasswordNextSignIn"] is True and len(prof["password"]) == 20
    # общий пароль, который не подходит Microsoft 365, — пропуск с причиной
    pv = base.run(svc, ADMIN, "ax_password", [m365.iv], {"mode": "same", "password": "simplepass"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "три вида" in pv["results"][0]["detail"]
    # ФИО и отдел — из фиксированных значений
    _res, items = act(svc, "ax_contact", [m365.iv], source="fixed", company="ООО Контосо", department="ИТ",
                      office="", overwrite=True, name_order="last_first", phone=False)
    assert items[m365.iv]["status"] == "success", items
    assert g.users["u1"]["companyName"] == "ООО Контосо" and g.users["u1"]["department"] == "ИТ"
    # одна учётная запись Microsoft 365 места не сообщает — после перечитывания место из сверки остаётся
    after = msstore.get_account(svc.db, m365.sid, "ivanov@contoso.ru")
    assert after["used_bytes"] == 5368709120 and after["aliases"] == ["ivan@contoso.ru"]
    _res, items = act(svc, "ax_unblock", [m365.pe])
    assert items[m365.pe]["status"] == "success" and g.users["u2"]["accountEnabled"] is True


def test_autoreply_and_forwarding(m365):
    svc, g = m365.svc, m365.g
    _res, items = act(svc, "srv_autoreply", [m365.iv], mode="on", subject="Нет на месте",
                      text="Я больше не работаю.\nПишите petrova@contoso.ru", start="", end="")
    assert items[m365.iv]["status"] == "success", items
    auto = g.settings["u1"]["auto"]
    assert auto["status"] == "alwaysEnabled" and "petrova@contoso.ru" in auto["externalReplyMessage"]
    assert "<br>" in auto["externalReplyMessage"]
    assert msstore.get_account(svc.db, m365.sid, "ivanov@contoso.ru")["autoreply"] is True
    _res, items = act(svc, "srv_autoreply", [m365.iv], mode="on", subject="", text="Отпуск", start="2026-11-01",
                      end="2026-11-10")
    assert g.settings["u1"]["auto"]["status"] == "scheduled"
    assert g.settings["u1"]["auto"]["scheduledEndDateTime"]["dateTime"] == "2026-11-10T23:59:59"
    # пересылка — правилом во «Входящих»
    _res, items = act(svc, "srv_forwarding", [m365.iv], mode="on", addresses="petrova@contoso.ru", keep_copy=True)
    assert items[m365.iv]["status"] == "success", items
    rule = list(g.rules["u1"].values())[0]
    assert rule["displayName"] == RULE_NAME and rule["actions"]["forwardTo"][0]["emailAddress"]["address"] == \
        "petrova@contoso.ru"
    assert msstore.get_account(svc.db, m365.sid, "ivanov@contoso.ru")["forward_to"] == ["petrova@contoso.ru"]
    # без копии — письма в «Удалённые»; потом снова с копией: «delete» снимается (PATCH не трогает того, чего нет
    # в запросе), а перенаправление, добавленное в Outlook, убирается
    _res, items = act(svc, "srv_forwarding", [m365.iv], mode="on", addresses="petrova@contoso.ru", keep_copy=False)
    assert items[m365.iv]["status"] == "success" and "«Удалённые»" in items[m365.iv]["detail"], items
    rule = list(g.rules["u1"].values())[0]
    assert rule["actions"]["delete"] is True and len(g.rules["u1"]) == 1
    rule["actions"]["redirectTo"] = [{"emailAddress": {"address": "boss@other.ru"}}]
    _res, items = act(svc, "srv_forwarding", [m365.iv], mode="on", addresses="petrova@contoso.ru", keep_copy=True)
    assert items[m365.iv]["status"] == "success", items
    rule = list(g.rules["u1"].values())[0]
    assert rule["actions"]["delete"] is False and rule["actions"]["redirectTo"] == []
    with mss.session(svc, msstore.get_server(svc.db, m365.sid)) as drv:
        assert drv.get_forwarding("ivanov@contoso.ru") == {"addresses": ["petrova@contoso.ru"], "keep_copy": True}
    _res, items = act(svc, "srv_forwarding", [m365.iv], mode="off", addresses="", keep_copy=True)
    assert not g.rules["u1"]
    # проверка параметров — до запуска
    with pytest.raises(ValidationError):
        base.run(svc, ADMIN, "srv_forwarding", [m365.iv], {"mode": "on", "addresses": "не адрес"}, preview=True)
    with pytest.raises(ValidationError):
        base.run(svc, ADMIN, "srv_autoreply", [m365.iv], {"mode": "on", "text": ""}, preview=True)


def test_autoreply_not_for_mailboxes_without_server_account(m365):
    svc = m365.svc
    acc_id = svc.db.create_account(models.Account(name="Чужой", host="imap.other.ru", username="x@other.ru",
                                                  password="pw"))
    pv = base.run(svc, ADMIN, "srv_autoreply", [f"mb:{acc_id}"], {"mode": "off"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "подключённом почтовом сервере" in pv["results"][0]["detail"]


def test_delete_only_with_full_archive(m365, monkeypatch):
    svc, g = m365.svc, m365.g
    world = imap_world(monkeypatch)
    base.run(svc, ADMIN, "ax_add_mailboxes", [m365.pe], {}, preview=False)
    # вход не закрыт — удалять нельзя
    pv = base.run(svc, ADMIN, "ax_delete", [m365.pe], {}, preview=True)
    assert pv["counts"]["skip"] == 1 and "сначала «Заблокировать вход»" in pv["results"][0]["detail"]
    _res, items = act(svc, "ax_block", [m365.pe], final_copy=True)
    assert items[m365.pe]["status"] == "success" and "последняя копия сделана" in items[m365.pe]["detail"], items
    # письмо пришло после копии — в архиве его нет: учётная запись не удаляется
    real_fresh = __import__("mailarchiver.serverops.imap_ops", fromlist=["fresh_copy"]).fresh_copy

    def copy_then_new_mail(ctx, acc, emit):
        out = real_fresh(ctx, acc, emit)
        world.add("petrova@contoso.ru", "INBOX", raw_message("Пришло после копии"))
        return out
    monkeypatch.setattr("mailarchiver.serverops.imap_ops.fresh_copy", copy_then_new_mail)
    _res, items = act(svc, "ax_delete", [m365.pe])
    assert items[m365.pe]["status"] == "skipped" and "НЕ удалена" in items[m365.pe]["detail"], items
    assert "u2" in g.users
    acc = next(a for a in svc.db.list_accounts() if a.username == "petrova@contoso.ru")
    assert acc.hold_until != "9999-12-31"            # удержание снято обратно
    monkeypatch.setattr("mailarchiver.serverops.imap_ops.fresh_copy", real_fresh)
    _res, items = act(svc, "ax_delete", [m365.pe])
    assert items[m365.pe]["status"] == "success", items
    assert "u2" not in g.users and "30 дней" in items[m365.pe]["detail"]
    acc = svc.db.get_account(acc.id)
    assert acc.enabled is False and acc.hold_until == "9999-12-31"
    assert msstore.get_account(svc.db, m365.sid, "petrova@contoso.ru")["deleted_at"]
    # передать адрес коллеге в Microsoft 365 нельзя (псевдонимы Graph не меняет)
    pv = base.run(svc, ADMIN, "ax_forward", [m365.pe], {"to": "ivanov@contoso.ru"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "псевдонимы" in pv["results"][0]["detail"]


def test_delete_needs_password_free_copy(m365):
    svc = m365.svc
    svc.db.create_account(models.Account(name="Петрова", host="outlook.office365.com",
                                         username="petrova@contoso.ru", password="pw"))
    msstore.update_fields(svc.db, m365.sid, "petrova@contoso.ru", login_blocked=True)
    pv = base.run(svc, ADMIN, "ax_delete", [m365.pe], {}, preview=True)
    assert pv["counts"]["skip"] == 1 and "по паролю сотрудника" in pv["results"][0]["detail"]


def test_create_accounts_on_connection(m365, monkeypatch):
    svc, g = m365.svc, m365.g
    msstore.update_server  # noqa: B018 — подключение уже есть
    conn = msstore.get_server(svc.db, m365.sid)
    msstore.update_server(svc.db, m365.sid, name=conn.name, settings=dict(conn.settings, license_sku="O365_BUSINESS_PREMIUM"),
                          secrets=conn.secrets, sync_cron=conn.sync_cron, enabled=True, allow_changes=True,
                          sync_enabled=True, sync_add_new=False, monitor=True)
    params = {"server_id": m365.sid, "text": "new.user@contoso.ru; Новый; Сотрудник\nivanov@contoso.ru\nx@unknown.ru",
              "password_mode": "random_file", "must_change": True, "quota_mb": 0, "add_to_archive": True}
    pv = create.run(svc, ADMIN, params, preview=True)
    by = {i["email"]: i for i in pv["items"]}
    assert by["new.user@contoso.ru"]["status"] == "ok"
    assert by["ivanov@contoso.ru"]["status"] == "skip" and "уже есть" in by["ivanov@contoso.ru"]["text"]
    assert "домена unknown.ru нет" in by["x@unknown.ru"]["text"]
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 1)
    res = create.run(svc, ADMIN, params, preview=False)
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["ok"] == 1, out
    new = g.find("new.user@contoso.ru")
    assert new["assignedLicenses"] == [{"skuId": "sku-1"}] and new["usageLocation"] == "RU"
    assert g.settings[new["id"]]["password"]["forceChangePasswordNextSignIn"] is True
    row = msstore.get_account(svc.db, m365.sid, "new.user@contoso.ru")
    assert row is not None and row["first_name"] == "Новый"
    assert any(a.username == "new.user@contoso.ru" and a.auth_type == AuthType.SERVER for a in svc.db.list_accounts())
    # пароль — в файле для раздачи
    from mailarchiver.serverops import secrets
    assert secrets.count(svc.db, res["op_id"]) == 1


def _with_license(m365, sku="O365_BUSINESS_PREMIUM"):
    svc = m365.svc
    conn = msstore.get_server(svc.db, m365.sid)
    msstore.update_server(svc.db, m365.sid, name=conn.name, settings=dict(conn.settings, license_sku=sku),
                          secrets=conn.secrets, sync_cron=conn.sync_cron, enabled=True, allow_changes=True,
                          sync_enabled=True, sync_add_new=False, monitor=True)
    return msstore.get_server(svc.db, m365.sid)


def test_create_checks_license_before_creating(m365, monkeypatch):
    """Свободных лицензий нет или подписки нет — учётная запись не создаётся вовсе (а не «создана без лицензии»)."""
    svc, g = m365.svc, m365.g
    _with_license(m365)
    g.skus[0]["consumedUnits"] = 10
    params = {"server_id": m365.sid, "text": "new.user@contoso.ru; Новый; Сотрудник", "password_mode": "random_file",
              "must_change": True, "quota_mb": 0, "add_to_archive": False}
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 1)
    create.run(svc, ADMIN, params, preview=False)
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    item = out["items"][0]
    assert item["status"] == "failed" and "свободных лицензий" in item["detail"] and "не создана" in item["detail"]
    assert g.find("new.user@contoso.ru") is None and not g.fake.calls_to("POST", r"/v1.0/users")
    conn = _with_license(m365, "NO_SUCH_SKU")
    with mss.session(svc, conn) as drv:
        with pytest.raises(ValidationError, match="подписки «NO_SUCH_SKU» нет"):
            drv.create_account("x.user@contoso.ru", first_name="Х", last_name="У", password="Xx-12345678")
    assert g.find("x.user@contoso.ru") is None


def test_create_keeps_account_when_license_assignment_fails(m365, monkeypatch):
    """Учётная запись создана, лицензию назначить не удалось — создана (с предупреждением), а не ошибка."""
    svc, g = m365.svc, m365.g
    conn = _with_license(m365)
    g.assign_error = "License assignment cannot be done for user with invalid usage location."
    with mss.session(svc, conn) as drv:
        acc = drv.create_account("new.user@contoso.ru", first_name="Новый", last_name="Сотрудник",
                                 password="Xx-12345678")
    assert g.find("new.user@contoso.ru") is not None
    assert acc["email"] == "new.user@contoso.ru" and acc["native_id"] and acc["kind"] == "unlicensed"
    assert "создана без лицензии" in acc["warning"] and "invalid usage location" in acc["warning"]
    assert acc["info"]["license_error"] == acc["warning"]
    # в задании — создана (повтор не нужен), пароль остаётся в файле для раздачи
    params = {"server_id": m365.sid, "text": "second.user@contoso.ru; Второй; Сотрудник",
              "password_mode": "random_file", "must_change": True, "quota_mb": 0, "add_to_archive": False}
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 1)
    res = create.run(svc, ADMIN, params, preview=False)
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["ok"] == 1 and out["items"][0]["status"] == "success", out
    assert msstore.get_account(svc.db, m365.sid, "second.user@contoso.ru") is not None
    from mailarchiver.serverops import secrets
    assert secrets.count(svc.db, res["op_id"]) == 1


def test_report_by_api_when_no_imap(m365):
    svc = m365.svc
    pv = base.run(svc, ADMIN, "report", [m365.iv], {}, preview=True)
    assert pv["counts"]["ok"] == 1 and "через API Microsoft 365" in pv["results"][0]["detail"]
    _res, items = act(svc, "report", [m365.iv])
    assert items[m365.iv]["status"] == "success", items
    report = items[m365.iv]["report"]
    assert report["size"] == 5368709120 and report["source"].startswith("API Microsoft 365")
    assert report["percent"] == 5.0


def test_monitor_problems_for_connection(m365):
    from mailarchiver.mailservers import monitor
    found = monitor.problems(m365.svc)
    kinds = {p["kind"] for p in found}
    assert "unarchived" in kinds                     # учётные записи без копии в архиве
    m365.g.skus[0]["capabilityStatus"] = "Suspended"
    mss.check(m365.svc, m365.sid, user="tester")
    found = monitor.problems(m365.svc)
    assert any(p["kind"] == "server" and "Suspended" in p["text"] for p in found)
