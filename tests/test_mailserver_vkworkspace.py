"""Подключение VK WorkSpace: учебный API biz.mail.ru v1 и сервер OAuth o2.mail.ru.

Учебный сервер повторяет документированное (https://biz.mail.ru/developer/): токен доступа по refresh token
(``/token``; ошибки — HTTP 200 с ``{"error","error_code","error_description"}``), заголовок ``Authorization: Bearer``,
списки страницами ``{"paging": {...}, "data": [...]}``, ошибки клиента — HTTP 400 ``{"code","message","data"}``,
без токена — 403 ``{"message": "Authentication credentials were not provided."}``, пользователи домена (статусы
active / blocked / soft-blocked / purging / deleted) и их псевдонимы. Проверяется весь путь: подключение и проверка,
сверка, действия раздела «Ящики на сервере», создание и что секреты не попадают в журнал. Входа в ящики без
паролей сотрудников у VK WorkSpace нет — ящики копируются по IMAP с паролями приложений.
"""
import copy

import pytest

from mailserver_fake import FakeHttp, connection
from mailarchiver import models
from mailarchiver.errors import ValidationError
from mailarchiver.mailservers import service as mss
from mailarchiver.mailservers import store as msstore
from mailarchiver.mailservers.base import AlreadyExists, NotSupported, ServerError
from mailarchiver.mailservers.drivers import vkworkspace as vk
from mailarchiver.mailservers.drivers.vkworkspace import VKWorkSpaceDriver
from mailarchiver.mailservers.http import TOKENS, ApiLog
from mailarchiver.models import JobType
from mailarchiver.queue.jobs import HANDLERS, JobContext
from mailarchiver.serverops import base, create
from mailarchiver.serverops import targets as tg

ADMIN = {"username": "tester", "role": "admin"}
DOMAIN = "company.ru"
DOMAIN_ID = 42
CLIENT_ID = "biz-client-7"
REFRESH = "b42019a80b7d76b388b504cc4366e98425fcbd3e37363830"
NO_AUTH = {"message": "Authentication credentials were not provided."}


def vuser(uid, username, first, last, status="active"):
    return {"id": uid, "username": username, "email": f"{username}@{DOMAIN}", "nick": None, "firstname": first,
            "lastname": last, "is_spammer": False, "groups": [{"id": 1341, "name": "programmers"}],
            "avatars": {"90x90": f"//filin.mail.ru/pic?width=90&height=90&email={username}@{DOMAIN}"},
            "created_at": "2013-01-09T12:03:28Z", "status": status, "status_at": "2013-01-09T12:03:28Z",
            "contact": None}


class BizApi:
    """Состояние учебного домена VK WorkSpace."""

    def __init__(self):
        self.users = {u["id"]: u for u in (
            vuser(101, "ivanov", "Иван", "Иванов"),
            vuser(102, "petrova", "Анна", "Петрова"),
            vuser(103, "boss", "Главный", "Админ"),
            vuser(104, "sales", "Отдел", "Продаж", status="soft-blocked"),
            vuser(105, "gone", "Удалённый", "Сотрудник", status="deleted"),
            vuser(106, "leaving", "Уходящий", "Сотрудник", status="purging"),
        )}
        self.aliases = {101: [{"id": 501, "username": "ivan", "email": f"ivan@{DOMAIN}",
                               "created_at": "2013-11-22T14:31:39Z"}]}
        self.alias_seq = 600
        self.passwords = {}
        self.issued = []
        self.revoked = set()
        self.page = 2
        self.hidden = set()          # не показываются в списке (для проверки ответа «уже есть» от сервера)


@pytest.fixture()
def biz(monkeypatch):
    TOKENS._tokens.clear()
    vk._IDS.clear()
    g = BizApi()
    fake = FakeHttp()
    monkeypatch.setattr(VKWorkSpaceDriver, "API_URL", fake.url + "/api/v1")
    monkeypatch.setattr(VKWorkSpaceDriver, "OAUTH_URL", fake.url + "/o2")

    @fake.route("POST", r"/o2/token")
    def token(req):
        form = req.form()
        if form.get("client_id") != CLIENT_ID:
            return 200, {"error": "invalid client", "error_code": 1, "error_description": "Неверный клиент"}
        if form.get("grant_type") != "refresh_token" or form.get("refresh_token") != REFRESH:
            return 200, {"error": "token not found", "error_code": 6, "error_description": "Неправильный токен"}
        tok = f"vk-acc-{len(g.issued) + 1}"
        g.issued.append(tok)
        return 200, {"expires_in": 3600, "access_token": tok}

    def guard(req):
        auth = req.headers.get("authorization") or ""
        tok = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if tok not in g.issued or tok in g.revoked or req.q("access_token"):
            return 403, NO_AUTH
        return None

    def page(req, items, path):
        limit = min(int(req.q("limit", "25")), 100, g.page)
        offset = int(req.q("offset", "0"))
        paging = {"results_count": len(items)}
        if offset > 0:
            paging["previous"] = f"https://biz.mail.ru/api/{path}?limit={limit}&offset={max(0, offset - limit)}"
        if offset + limit < len(items):
            paging["next"] = f"https://biz.mail.ru/api/{path}?limit={limit}&offset={offset + limit}"
        return 200, {"paging": paging, "data": items[offset:offset + limit]}

    def error(code, message, data=None):
        body = {"code": code, "message": message}
        if data is not None:
            body["data"] = data
        return 400, body

    def user_of(req):
        if int(req.group(1)) != DOMAIN_ID:
            return None, (404, {"message": "Не найдено."})
        u = g.users.get(int(req.group(2)))
        if u is not None and req.method == "GET" and u["id"] in g.hidden:
            u = None
        return u, (None if u else (404, {"message": "Не найдено."}))

    @fake.route("GET", r"/api/v1/domains")
    def domains(req):
        return guard(req) or page(req, [{"id": 11, "name": "other.ru", "status": "confirmed"},
                                        {"id": DOMAIN_ID, "name": DOMAIN, "status": "confirmed"}], "domains")

    @fake.route("GET", r"/api/v1/domains/(\d+)/users")
    def users(req):
        bad = guard(req)
        if bad:
            return bad
        items = [copy.deepcopy(u) for u in g.users.values() if u["id"] not in g.hidden]
        return page(req, items, f"domains/{req.group(1)}/users")

    @fake.route("GET", r"/api/v1/domains/(\d+)/users/(\d+)")
    def one(req):
        u, err = user_of(req)
        return guard(req) or err or (200, copy.deepcopy(u))

    def edit(req):
        u, err = user_of(req)
        if guard(req) or err:
            return guard(req) or err
        body = req.json()
        if ("firstname" in body) != ("lastname" in body):
            return error(0, "Ошибка валидации данных.", {"lastname": ["Имя и фамилия задаются вместе"]})
        if u["status"] in ("purging", "deleted"):
            return error(147, "Невозможно отредактировать пользователя.")
        for key, value in body.items():
            if key == "status":
                if value not in ("active", "blocked", "soft-blocked"):
                    return error(0, "Ошибка валидации данных.", {"status": ["Недопустимое значение"]})
                u["status"] = value
            elif key == "password":
                if len(value) < 6:
                    return error(145, "Невозможно сменить пароль пользователя.")
                g.passwords[u["id"]] = value
            elif key in ("firstname", "lastname"):
                u[key] = value
            else:
                return error(0, "Ошибка валидации данных.", {key: ["Неизвестное поле"]})
        return 200, copy.deepcopy(u)

    fake.route("PATCH", r"/api/v1/domains/(\d+)/users/(\d+)")(edit)
    fake.route("PUT", r"/api/v1/domains/(\d+)/users/(\d+)")(edit)

    @fake.route("POST", r"/api/v1/domains/(\d+)/users")
    def create_user(req):
        bad = guard(req)
        if bad:
            return bad
        body = req.json()
        if not body.get("username") or not body.get("password"):
            return error(0, "Ошибка валидации данных.", {"username": ["Обязательное поле."]})
        if ("firstname" in body) != ("lastname" in body):
            return error(0, "Ошибка валидации данных.", {"lastname": ["Имя и фамилия задаются вместе"]})
        if any(u["username"] == body["username"] for u in g.users.values()):
            return error(0, "Ошибка валидации данных.", {"username": ["Такой ящик уже существует"]})
        uid = max(g.users) + 1
        u = vuser(uid, body["username"], body.get("firstname", ""), body.get("lastname", ""))
        g.users[uid] = u
        g.passwords[uid] = body["password"]
        return 201, copy.deepcopy(u)

    @fake.route("DELETE", r"/api/v1/domains/(\d+)/users/(\d+)")
    def delete(req):
        u, err = user_of(req)
        if guard(req) or err:
            return guard(req) or err
        if u["username"] == "boss":
            return error(135, "Невозможно удалить ящик администратора домена.")
        u["status"] = "purging"
        return 200, copy.deepcopy(u)

    @fake.route("GET", r"/api/v1/domains/(\d+)/users/(\d+)/aliases")
    def aliases(req):
        u, err = user_of(req)
        return guard(req) or err or (200, copy.deepcopy(g.aliases.get(u["id"], [])))

    @fake.route("POST", r"/api/v1/domains/(\d+)/users/(\d+)/aliases")
    def add_alias(req):
        u, err = user_of(req)
        if guard(req) or err:
            return guard(req) or err
        name = req.json().get("username") or ""
        have = g.aliases.setdefault(u["id"], [])
        if len(have) >= 5:
            return error(143, "Превышен лимит алиасов для этого пользователя")
        taken = {x["username"] for x in g.users.values()} | {a["username"] for v in g.aliases.values() for a in v}
        if name in taken:
            return error(0, "Ошибка валидации данных.", {"alias": ["Такое имя уже занято в домене"]})
        g.alias_seq += 1
        alias = {"id": g.alias_seq, "username": name, "email": f"{name}@{DOMAIN}", "created_at": "2026-10-26T10:00:00Z"}
        have.append(alias)
        return 201, alias

    @fake.route("DELETE", r"/api/v1/domains/(\d+)/users/(\d+)/aliases/(\d+)")
    def del_alias(req):
        u, err = user_of(req)
        if guard(req) or err:
            return guard(req) or err
        have = g.aliases.get(u["id"], [])
        aid = int(req.match.group(3))
        if not any(a["id"] == aid for a in have):
            return 404, {"message": "Не найдено."}
        g.aliases[u["id"]] = [a for a in have if a["id"] != aid]
        return 204, b""

    g.fake = fake
    yield g
    fake.close()
    TOKENS._tokens.clear()
    vk._IDS.clear()


def connect(svc, **extra):
    values = {"type": "vkworkspace", "name": "Компания", "domain": DOMAIN, "client_id": CLIENT_ID,
              "refresh_token": REFRESH, "allow_changes": True}
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


def transcripts(svc, sid):
    return "\n".join(msstore.get_log(svc.db, row["id"])["transcript"] for row in msstore.list_log(svc.db, sid))


def drv_for(**settings):
    conn = connection("vkworkspace", dict({"domain": DOMAIN, "client_id": CLIENT_ID}, **settings),
                      {"refresh_token": REFRESH})
    return VKWorkSpaceDriver(conn, ApiLog())


@pytest.fixture()
def vkw(services, biz):
    sid = connect(services)
    res = mss.sync(services, sid, user="tester")
    return type("V", (), {"svc": services, "g": biz, "sid": sid, "sync": res,
                          "iv": f"ms:{sid}:ivanov@{DOMAIN}", "pe": f"ms:{sid}:petrova@{DOMAIN}"})


# ---------------------------------------------------------------------------
#  Подключение и проверка
# ---------------------------------------------------------------------------
def test_check_finds_domain_and_counts_accounts(services, biz):
    sid = connect(services)
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out
    details = "\n".join(out["status"]["details"])
    assert f"Домен: {DOMAIN} (№{DOMAIN_ID}), состояние: confirmed" in details and "Учётных записей: 6" in details
    assert "imap.mail.ru" in details and not out["status"]["problems"]
    text = transcripts(services, sid)
    assert REFRESH not in text and "vk-acc-1" not in text
    # токен — только в заголовке, не в адресе
    assert all("access_token" not in c.query for c in biz.fake.calls)


def test_wrong_refresh_token_is_explained_and_not_logged(services, biz):
    sid = connect(services, refresh_token="stale-Refresh-Token-1")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "сервер токенов отказал" in out["error"] and "token not found" in out["error"], out
    assert "заново войти" in out["hint"]
    text = transcripts(services, sid)
    assert "stale-Refresh-Token-1" not in text and REFRESH not in text


def test_unknown_domain_lists_domains_of_token(services, biz):
    sid = connect(services, domain="absent.ru")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "домена absent.ru нет" in out["error"] and "other.ru, company.ru" in out["error"]
    with pytest.raises(ValidationError, match="домен указан неверно"):
        connect(services, domain="user@company.ru")


def test_revoked_cached_token_is_replaced_once(services, biz):
    sid = connect(services)
    assert mss.check(services, sid, user="tester")["ok"]
    biz.revoked.add("vk-acc-1")                         # отозван раньше срока — в кэше он ещё есть
    res = mss.sync(services, sid, user="tester")
    assert res["accounts"] == 4 and biz.issued == ["vk-acc-1", "vk-acc-2"]
    biz.revoked.add("vk-acc-2")
    TOKENS._tokens.clear()
    biz.issued.append("vk-acc-3")                       # сервер токенов выдаёт уже отозванный — повтор не помогает
    biz.revoked.add("vk-acc-4")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "доступ запрещён (403)" in out["error"]


# ---------------------------------------------------------------------------
#  Сверка
# ---------------------------------------------------------------------------
def test_sync_normalizes_accounts(vkw):
    rows = {r["email"]: r for r in msstore.list_accounts(vkw.svc.db, vkw.sid)}
    # удалённые и помеченные на удаление — не в сверке
    assert set(rows) == {f"ivanov@{DOMAIN}", f"petrova@{DOMAIN}", f"boss@{DOMAIN}", f"sales@{DOMAIN}"}
    iv = rows[f"ivanov@{DOMAIN}"]
    assert iv["aliases"] == [f"ivan@{DOMAIN}"] and iv["login_blocked"] is False and iv["native_id"] == "101"
    assert iv["first_name"] == "Иван" and iv["last_name"] == "Иванов" and iv["info"]["groups"] == ["programmers"]
    assert iv["quota_kb"] is None and iv["used_bytes"] is None and iv["last_login"] == ""
    sales = rows[f"sales@{DOMAIN}"]
    assert sales["login_blocked"] is True and sales["info"]["status"] == "soft-blocked"
    assert vkw.sync["incomplete"] is False and not vkw.sync["problems"]
    # страницы — по paging.next (по 2 записи)
    assert len(vkw.g.fake.calls_to("GET", rf"/api/v1/domains/{DOMAIN_ID}/users")) == 3


def test_sync_without_aliases(services, biz):
    sid = connect(services, read_aliases=False, domain_id=DOMAIN_ID)
    mss.sync(services, sid, user="tester")
    assert not biz.fake.calls_to("GET", r"/api/v1/domains/\d+/users/\d+/aliases")
    assert not biz.fake.calls_to("GET", r"/api/v1/domains")             # номер домена — из настройки
    assert msstore.get_account(services.db, sid, f"ivanov@{DOMAIN}")["aliases"] == []


def test_get_account_reuses_known_id(vkw):
    calls_before = len(vkw.g.fake.calls_to("GET", rf"/api/v1/domains/{DOMAIN_ID}/users"))
    acc = drv_for().get_account(f"ivanov@{DOMAIN}")
    assert acc["native_id"] == "101" and acc["aliases"] == [f"ivan@{DOMAIN}"]
    assert len(vkw.g.fake.calls_to("GET", rf"/api/v1/domains/{DOMAIN_ID}/users")) == calls_before
    assert vkw.g.fake.calls_to("GET", rf"/api/v1/domains/{DOMAIN_ID}/users/101")
    assert drv_for().get_account(f"leaving@{DOMAIN}") is None
    assert drv_for().get_account(f"nobody@{DOMAIN}") is None


# ---------------------------------------------------------------------------
#  Действия раздела «Ящики на сервере»
# ---------------------------------------------------------------------------
def test_block_soft_and_full_then_unblock(vkw):
    svc, g = vkw.svc, vkw.g
    _res, items = act(svc, "ax_block", [vkw.pe], final_copy=False, keep_receiving=True)
    assert items[vkw.pe]["status"] == "success", items
    assert g.users[102]["status"] == "soft-blocked" and "письма продолжают приходить" in items[vkw.pe]["detail"]
    assert msstore.get_account(svc.db, vkw.sid, f"petrova@{DOMAIN}")["login_blocked"] is True
    _res, items = act(svc, "ax_unblock", [vkw.pe])
    assert items[vkw.pe]["status"] == "success" and g.users[102]["status"] == "active"
    _res, items = act(svc, "ax_block", [vkw.pe], final_copy=False, keep_receiving=False)
    assert g.users[102]["status"] == "blocked" and "полная блокировка" in items[vkw.pe]["detail"]


def test_passwords(vkw):
    svc, g = vkw.svc, vkw.g
    _res, items = act(svc, "ax_password", [vkw.iv], mode="random_hidden", must_change=True)
    assert items[vkw.iv]["status"] == "success", items
    assert len(g.passwords[101]) == 20 and "смену пароля при входе VK WorkSpace не требует" in items[vkw.iv]["detail"]
    assert g.passwords[101] not in transcripts(svc, vkw.sid)
    pv = base.run(svc, ADMIN, "ax_password", [vkw.iv], {"mode": "same", "password": "simplepass1"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "спецсимвол" in pv["results"][0]["detail"]
    pv = base.run(svc, ADMIN, "ax_password", [vkw.iv], {"mode": "same", "password": "Пароль-2026"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "латиница" in pv["results"][0]["detail"]
    pv = base.run(svc, ADMIN, "ax_password", [vkw.iv], {"mode": "same", "password": "Company-2026"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "название домена" in pv["results"][0]["detail"]
    drv = drv_for()
    for _ in range(20):
        drv.check_password(drv.random_password())


def test_contact_names_change_together(vkw):
    svc, g = vkw.svc, vkw.g
    svc.db.create_employee(full_name="Сидорова Анна", email=f"petrova@{DOMAIN}", department="ИТ")
    _res, items = act(svc, "ax_contact", [vkw.pe], source="employees", overwrite=True, name_order="last_first")
    assert items[vkw.pe]["status"] == "success", items
    assert g.users[102]["firstname"] == "Анна" and g.users[102]["lastname"] == "Сидорова"
    assert msstore.get_account(svc.db, vkw.sid, f"petrova@{DOMAIN}")["last_name"] == "Сидорова"
    # отдел API не задаёт — записывать нечего
    pv = base.run(svc, ADMIN, "ax_contact", [vkw.iv], {"source": "fixed", "department": "ИТ", "overwrite": True},
                  preview=True)
    assert pv["counts"]["skip"] == 1 and "нечего записывать" in pv["results"][0]["detail"]


def test_aliases_add_remove_and_limit(vkw):
    svc, g = vkw.svc, vkw.g
    _res, items = act(svc, "ax_alias_add", [vkw.pe], template="{f}.{last}")
    assert items[vkw.pe]["status"] == "success", items
    assert [a["username"] for a in g.aliases[102]] == ["a.petrova"]
    assert msstore.get_account(svc.db, vkw.sid, f"petrova@{DOMAIN}")["aliases"] == [f"a.petrova@{DOMAIN}"]
    _res, items = act(svc, "ax_alias_remove", [vkw.iv], which="list", aliases="ivan")
    assert items[vkw.iv]["status"] == "success", items
    assert g.aliases[101] == [] and msstore.get_account(svc.db, vkw.sid, f"ivanov@{DOMAIN}")["aliases"] == []
    drv = drv_for()
    with pytest.raises(AlreadyExists, match="уже занят"):
        drv.add_alias(f"ivanov@{DOMAIN}", f"petrova@{DOMAIN}")
    with pytest.raises(ValidationError, match="только в домене учётной записи"):
        drv.add_alias(f"ivanov@{DOMAIN}", "ivan@other.ru")
    for i in range(5):
        drv.add_alias(f"ivanov@{DOMAIN}", f"iv{i}@{DOMAIN}")
    with pytest.raises(ServerError, match="не больше 5"):
        drv.add_alias(f"ivanov@{DOMAIN}", f"iv9@{DOMAIN}")


def test_capabilities_skip_with_reason(vkw):
    svc = vkw.svc
    for action, params, text in (
            ("ax_quota", {"quota_mb": 2048}, "квота ящика — этот сервер так не умеет"),
            ("srv_autoreply", {"mode": "off"}, "автоответ — этот сервер так не умеет"),
            ("srv_forwarding", {"mode": "off"}, "пересылка — этот сервер так не умеет"),
            ("ax_must_change", {"value": "yes"}, "смена пароля при входе — этот сервер так не умеет"),
            ("ax_add_mailboxes", {}, "копирование без паролей сотрудников не поддерживается")):
        pv = base.run(svc, ADMIN, action, [vkw.iv], params, preview=True)
        assert pv["counts"]["skip"] == 1 and text in pv["results"][0]["detail"], (action, pv["results"])


def test_password_mailbox_is_matched_but_not_password_free(vkw):
    svc = vkw.svc
    linked = svc.db.create_account(models.Account(name="Иванов", host="imap.mail.ru", username=f"ivanov@{DOMAIN}",
                                                  password="app-password-1"))
    items = {t.key: t for t in tg.load(svc)}
    assert items[vkw.iv].mailbox is not None and items[vkw.iv].mailbox.id == linked
    acc = svc.db.get_account(linked)
    acc.server_id = vkw.sid
    with pytest.raises(ServerError, match="без паролей сотрудников этот сервер не поддерживает"):
        mss.imap_auth_for(svc, acc)


# ---------------------------------------------------------------------------
#  Удаление и передача адреса коллеге
# ---------------------------------------------------------------------------
def test_no_delete_through_mailarchiver(vkw):
    """Удаления нет: сверить ящик с архивом без пароля сотрудника нечем — удаляют в панели администратора."""
    svc, g = vkw.svc, vkw.g
    msstore.update_fields(svc.db, vkw.sid, f"petrova@{DOMAIN}", login_blocked=True)
    svc.db.create_account(models.Account(name="Петрова", host="imap.mail.ru", username=f"petrova@{DOMAIN}",
                                         password="app-password-2"))
    pv = base.run(svc, ADMIN, "ax_delete", [vkw.pe], {}, preview=True)
    assert pv["counts"]["skip"] == 1 and "удаление — этот сервер так не умеет" in pv["results"][0]["detail"], pv
    with pytest.raises(NotSupported):
        drv_for().delete_account(f"petrova@{DOMAIN}")
    assert g.users[102]["status"] == "active" and not g.fake.calls_to("DELETE", r"/api/v1/domains/\d+/users/\d+")
    assert not VKWorkSpaceDriver.can("delete")
    assert any("удалить учётную запись через MailArchiver нельзя" in line for line in VKWorkSpaceDriver.limits)


def test_stale_known_id_of_deleted_account_prefers_live_one(vkw):
    """Номер из прошлой сверки — у удалённой учётной записи, а с тем же адресом завели новую: берётся новая."""
    g = vkw.g
    assert vk._IDS[(VKWorkSpaceDriver.API_URL, str(DOMAIN_ID))][f"petrova@{DOMAIN}"] == "102"
    g.users[102]["status"] = "deleted"
    g.users[107] = vuser(107, "petrova", "Анна", "Петрова")             # новая с тем же адресом — в конце списка
    acc = drv_for().get_account(f"petrova@{DOMAIN}")
    assert acc is not None and acc["native_id"] == "107" and acc["login_blocked"] is False
    drv_for().set_login(f"petrova@{DOMAIN}", False, keep_receiving=True)
    assert g.users[107]["status"] == "soft-blocked" and g.users[102]["status"] == "deleted"
    assert vk._IDS[(VKWorkSpaceDriver.API_URL, str(DOMAIN_ID))][f"petrova@{DOMAIN}"] == "107"
    # повторная сверка (удалённая — в списке раньше новой) номер не портит
    mss.sync(vkw.svc, vkw.sid, user="tester")
    assert vk._IDS[(VKWorkSpaceDriver.API_URL, str(DOMAIN_ID))][f"petrova@{DOMAIN}"] == "107"
    # действующей нет — удалённая находится как удалённая (создать заново нельзя, с понятной причиной)
    g.users.pop(107)
    assert drv_for().get_account(f"petrova@{DOMAIN}") is None
    with pytest.raises(AlreadyExists, match="помечена на удаление"):
        drv_for().create_account(f"petrova@{DOMAIN}", first_name="", last_name="", password="Xx-1234567")


def test_deleted_in_panel_then_address_to_colleague(vkw):
    svc, g = vkw.svc, vkw.g
    g.users[102]["status"] = "deleted"
    mss.sync(svc, vkw.sid, user="tester")
    assert msstore.get_account(svc.db, vkw.sid, f"petrova@{DOMAIN}")["deleted_at"]
    g.users.pop(102)                                   # удаление завершено — имя свободно
    _res, items = act(svc, "ax_forward", [vkw.pe], to=f"ivanov@{DOMAIN}", with_aliases=True)
    assert items[vkw.pe]["status"] == "success", items
    assert "petrova" in [a["username"] for a in g.aliases[101]]
    assert f"petrova@{DOMAIN}" in msstore.get_account(svc.db, vkw.sid, f"ivanov@{DOMAIN}")["aliases"]


# ---------------------------------------------------------------------------
#  Создание учётных записей
# ---------------------------------------------------------------------------
def test_create_accounts_on_connection(vkw, monkeypatch):
    svc, g = vkw.svc, vkw.g
    params = {"server_id": vkw.sid, "text": f"new.user@{DOMAIN}; Новый; Сотрудник\nivanov@{DOMAIN}\nx@unknown.ru",
              "password_mode": "random_file", "must_change": True, "quota_mb": 100, "add_to_archive": True}
    pv = create.run(svc, ADMIN, params, preview=True)
    by = {i["email"]: i for i in pv["items"]}
    assert by[f"new.user@{DOMAIN}"]["status"] == "ok"
    assert by[f"ivanov@{DOMAIN}"]["status"] == "skip" and "уже есть" in by[f"ivanov@{DOMAIN}"]["text"]
    assert "домена unknown.ru нет" in by["x@unknown.ru"]["text"]
    warnings = " ".join(pv["warnings"])
    assert "смену пароля при первом входе сервер не требует" in warnings
    assert "копирование без паролей сотрудников не поддерживается" in warnings and "квоту через API" in warnings
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 1)
    res = create.run(svc, ADMIN, params, preview=False)
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["ok"] == 1, out
    new = next(u for u in g.users.values() if u["username"] == "new.user")
    assert new["firstname"] == "Новый" and new["lastname"] == "Сотрудник" and len(g.passwords[new["id"]]) == 20
    row = msstore.get_account(svc.db, vkw.sid, f"new.user@{DOMAIN}")
    assert row is not None and row["native_id"] == str(new["id"])
    # ящик в архиве сам не заводится (нет входа без паролей)
    assert not any(a.username == f"new.user@{DOMAIN}" for a in svc.db.list_accounts())
    from mailarchiver.serverops import secrets
    assert secrets.count(svc.db, res["op_id"]) == 1


def test_create_refuses_existing_and_bad_input(biz):
    drv = drv_for()
    with pytest.raises(AlreadyExists, match="уже есть"):
        drv.create_account(f"ivanov@{DOMAIN}", first_name="И", last_name="И", password="Xx-1234567")
    with pytest.raises(AlreadyExists, match="помечена на удаление"):
        drv.create_account(f"leaving@{DOMAIN}", first_name="", last_name="", password="Xx-1234567")
    # в списке не видно, а сервер отвечает «уже существует» — тоже AlreadyExists
    biz.hidden.add(102)
    with pytest.raises(AlreadyExists, match="Такой ящик уже существует"):
        drv_for().create_account(f"petrova@{DOMAIN}", first_name="", last_name="", password="Xx-1234567")
    with pytest.raises(ValidationError, match="только вместе"):
        drv.create_account(f"half@{DOMAIN}", first_name="Имя", last_name="", password="Xx-1234567")
    with pytest.raises(ValidationError, match="другого"):
        drv.create_account("someone@other.ru", first_name="", last_name="", password="Xx-1234567")


def test_driver_contract():
    desc = VKWorkSpaceDriver.describe()
    assert desc["cloud"] and desc["imap_default"] == ["imap.mail.ru", 993, "ssl"]
    assert set(desc["caps"]) == {"read", "block", "password", "contact", "create", "aliases", "status"}
    assert "удаление" not in desc["summary"]
    assert {f["key"] for f in desc["fields"] if f["secret"]} == {"refresh_token"}
    assert any("пароль приложения" in s for s in desc["setup"])
    drv = drv_for()
    for call in (lambda: drv.imap_auth(f"ivanov@{DOMAIN}"), lambda: drv.set_quota(f"ivanov@{DOMAIN}", 1),
                 lambda: drv.set_autoreply(f"ivanov@{DOMAIN}", enabled=False),
                 lambda: drv.set_forwarding(f"ivanov@{DOMAIN}", addresses=[]),
                 lambda: drv.set_must_change(f"ivanov@{DOMAIN}", True)):
        with pytest.raises(NotSupported):
            call()
