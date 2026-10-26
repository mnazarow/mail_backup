"""Подключение «Postfix + Dovecot»: учебный doveadm HTTP API поверх FakeHttp и IMAP с мастер-пользователем Dovecot.

Учебный doveadm ведёт себя как настоящий: ``POST /doveadm/v1`` с JSON-массивом ``[[команда, параметры, метка]]``,
ответ — ``[["doveadmResponse", строки, метка]]`` или ошибка внутри ответа с кодом 200
(``["error", {"type": "exitCode", "exitCode": 67}, метка]``, ``unknownMethod`` для неизвестной команды), все
значения — строки, квота — в килобайтах, ``-`` — без предела; ``user`` отвечает строками {field, value} или
объектом {пользователь: {…}} (как в примере документации 2.3). Проверяется: проверка подключения и ключ (API key и
Basic), сверка обходом всех пользователей, квота и запасной путь через vsize, автоответ и пересылка сценарием Sieve
с подключением прежнего сценария пользователя, пропуск того, чего doveadm не умеет, и вход по IMAP мастер-пользователем
(SASL PLAIN с authzid и «ящик*мастер»).
"""
import base64
import json

import pytest

from imap_fake import ImapWorld, raw_message
from mailserver_fake import FakeHttp, connection
from mailarchiver.errors import ValidationError
from mailarchiver.imap import client as client_mod
from mailarchiver.mailservers import service as mss
from mailarchiver.mailservers import store as msstore
from mailarchiver.mailservers.base import ServerError
from mailarchiver.mailservers.drivers.dovecot import SIEVE_NAME, DovecotDriver, sieve_state
from mailarchiver.mailservers.http import ApiLog
from mailarchiver.models import AuthType, JobType
from mailarchiver.queue.jobs import HANDLERS, JobContext
from mailarchiver.serverops import base, create

ADMIN = {"username": "tester", "role": "admin"}
API_KEY = "doveadm-Secret-Key-42"
DOVEADM_PW = "Doveadm-Pass-77"
MASTER = "archmaster"
MASTER_PW = "Master-Pass-99"
ALL_COMMANDS = ("reload", "who", "kick", "authCacheFlush", "user", "mailboxStatus", "quotaGet", "quotaRecalc",
                "sieveList", "sieveGet", "sievePut", "sieveActivate", "sieveDeactivate", "sieveDelete")
ROUNDCUBE = 'require ["fileinto"];\nif header :contains "subject" "отчёт" { fileinto "Отчёты"; }\n'


class Doveadm:
    """Состояние учебного Dovecot."""

    def __init__(self):
        # пользователь → папки {имя: (писем, vsize)}, квота (занято КБ, предел КБ или None)
        self.users = {
            "ivanov@company.ru": {"folders": {"INBOX": (120, 5_000_000), "Sent": (30, 1_000_000)},
                                  "quota": (6000, 1048576)},
            "petrova@company.ru": {"folders": {"INBOX": (10, 200_000)}, "quota": (190, None)},
            "boss@other.ru": {"folders": {"INBOX": (1, 1000)}, "quota": (1, None)},
            "vmail": {"folders": {"INBOX": (0, 0)}, "quota": (0, None)},
        }
        self.commands = set(ALL_COMMANDS)
        #: «user» — строками {field, value} (исходники) или объектом {пользователь: {…}} (документация 2.3)
        self.user_shape = "rows"
        #: неизвестная команда — ошибкой в ответе 200 (2.3) или кодом 404 (как пишет документация 2.4)
        self.unknown_as_404 = False
        #: команда → код завершения при обходе всех пользователей
        self.fail_all = {}
        self.scripts = {"ivanov@company.ru": {"roundcube": ROUNDCUBE}}
        self.active = {"ivanov@company.ru": "roundcube"}
        self.sessions = [{"username": "ivanov@company.ru", "connections": "2", "service": "imap",
                          "pids": "(4999 5001)", "ips": "(10.0.0.7)"}]
        self.requests = []

    # -- протокол ------------------------------------------------------------------------------
    @staticmethod
    def authorized(req):
        header = req.headers.get("authorization", "")
        return header in ("X-Dovecot-API " + base64.b64encode(API_KEY.encode()).decode(),
                          "Basic " + base64.b64encode(f"doveadm:{DOVEADM_PW}".encode()).decode())

    def listing(self, req):
        if not self.authorized(req):
            return 401, "Authentication required", {"WWW-Authenticate": "X-Dovecot-API"}
        return 200, [{"command": c, "parameters": [{"name": "user", "type": "string"}]} for c in sorted(self.commands)]

    def post(self, req):
        if not self.authorized(req):
            return 401, "Authentication required", {"WWW-Authenticate": 'Basic Realm="doveadm"'}
        try:
            body = req.json()
        except ValueError:
            return 400, "Invalid request"
        if not isinstance(body, list):
            return 400, "Invalid request"
        out = []
        for command, params, tag in body:
            self.requests.append((command, params))
            if command not in self.commands:
                if self.unknown_as_404:
                    return 404, "Unknown doveadm command"
                out.append(["error", {"type": "unknownMethod", "exitCode": 0}, tag])
                continue
            try:
                rows = getattr(self, "cmd_" + command)(params)
            except Exit as exc:
                out.append(["error", {"type": "exitCode", "exitCode": exc.code}, tag])
                continue
            out.append(["doveadmResponse", rows, tag])
        return 200, out

    def targets(self, params, command):
        if params.get("allUsers"):
            if command in self.fail_all:
                raise Exit(self.fail_all[command])
            return list(self.users), True
        user = params.get("user")
        if user not in self.users:
            raise Exit(67)
        return [user], False

    # -- команды -------------------------------------------------------------------------------
    def cmd_mailboxStatus(self, params):
        users, many = self.targets(params, "mailboxStatus")
        rows = []
        for user in users:
            for name, (messages, vsize) in self.users[user]["folders"].items():
                values = {"messages": str(messages), "vsize": str(vsize)}
                row = ({"username": user} if many else {}) | {"mailbox": name}
                row.update({f: values[f] for f in params["field"]})
                rows.append(row)
        return rows

    def cmd_quotaGet(self, params):
        users, many = self.targets(params, "quotaGet")
        rows = []
        for user in users:
            used, limit = self.users[user]["quota"]
            messages = sum(m for m, _v in self.users[user]["folders"].values())
            for kind, value, lim in (("STORAGE", used, limit), ("MESSAGE", messages, None)):
                row = {"username": user} if many else {}
                row.update({"root": "User quota", "type": kind, "value": str(value),
                            "limit": str(lim) if lim else "-", "percent": str(100 * value // lim) if lim else "0"})
                rows.append(row)
        return rows

    def cmd_user(self, params):
        user = params["userMask"][0]
        if user not in self.users:
            raise Exit(67)
        fields = {"uid": "5000", "gid": "5000", "home": f"/var/vmail/{user}", "mail": "maildir:~/Maildir"}
        if self.user_shape == "object":
            return {user: fields}
        return [{"field": k, "value": v} for k, v in fields.items()]

    def cmd_who(self, params):
        return self.sessions

    def cmd_sieveList(self, params):
        users, many = self.targets(params, "sieveList")
        rows = []
        for user in users:
            for name in self.scripts.get(user, {}):
                row = {"username": user} if many else {}
                row.update({"script": name, "active": "ACTIVE" if self.active.get(user) == name else ""})
                rows.append(row)
        return rows

    def cmd_sieveGet(self, params):
        script = self.scripts.get(params["user"], {}).get(params["scriptname"])
        if script is None:
            raise Exit(68)
        return [{"sieve script": script}]

    def cmd_sievePut(self, params):
        assert isinstance(params["file"], str) and isinstance(params["activate"], bool)
        if not params["file"].strip():
            raise Exit(65)
        self.scripts.setdefault(params["user"], {})[params["scriptname"]] = params["file"]
        if params["activate"]:
            self.active[params["user"]] = params["scriptname"]
        return []

    def cmd_sieveActivate(self, params):
        if params["scriptname"] not in self.scripts.get(params["user"], {}):
            raise Exit(68)
        self.active[params["user"]] = params["scriptname"]
        return []

    def cmd_sieveDeactivate(self, params):
        self.active.pop(params["user"], None)
        return []

    def cmd_sieveDelete(self, params):
        for name in params["scriptname"]:
            if self.active.get(params["user"]) == name and not params.get("ignoreActive"):
                raise Exit(65)
            self.scripts.get(params["user"], {}).pop(name)
        return []


class Exit(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@pytest.fixture()
def dv():
    d = Doveadm()
    fake = FakeHttp()
    fake.route("GET", r"/doveadm/v1")(d.listing)
    fake.route("POST", r"/doveadm/v1")(d.post)
    d.fake = fake
    yield d
    fake.close()


def connect(svc, dv, **extra):
    values = {"type": "dovecot", "name": "Почта Dovecot", "base_url": dv.fake.url, "auth": "api_key",
              "api_key": API_KEY, "master_user": MASTER, "master_password": MASTER_PW, "imap_host": "mail.company.ru",
              "allow_changes": True}
    values.update(extra)
    return mss.save(svc, "tester", values)


def run_job(svc, job_type, params):
    job_id = svc.db.enqueue_job(job_type, None, params, 5, 1, "tester")
    return HANDLERS[job_type](JobContext(svc, job_id, job_type, None, params))


def act(svc, action, keys, **options):
    res = run_job(svc, JobType.SERVER_ACTION, {"action": action, "keys": keys, "options": options,
                                               "requested_by": "tester", "op_id": None})
    return res, {i["key"]: i for i in res["items"]}


def transcripts(svc, sid):
    return "\n".join(msstore.get_log(svc.db, row["id"])["transcript"] for row in msstore.list_log(svc.db, sid))


def driver(dv, **settings):
    values = {"base_url": dv.fake.url, "auth": "api_key"}
    values.update(settings)
    return DovecotDriver(connection("dovecot", values, {"api_key": API_KEY, "doveadm_password": DOVEADM_PW,
                                                        "master_password": MASTER_PW}), ApiLog())


@pytest.fixture()
def dc(services, dv):
    sid = connect(services, dv, domains="company.ru")
    res = mss.sync(services, sid, user="tester")
    return type("D", (), {"svc": services, "dv": dv, "sid": sid, "sync": res,
                          "iv": f"ms:{sid}:ivanov@company.ru", "pe": f"ms:{sid}:petrova@company.ru"})


# ---------------------------------------------------------------------------
#  Подключение и проверка
# ---------------------------------------------------------------------------
def test_check_lists_commands_sessions_and_master(services, dv):
    sid = connect(services, dv)
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out
    st = out["status"]
    assert not st["problems"], st["problems"]
    details = " | ".join(st["details"])
    assert f"команд доступно {len(ALL_COMMANDS)}" in details and "модуль quota подключён" in details
    assert "пользователей 1, соединений 2" in details and "мастер-пользователь archmaster" in details
    text = transcripts(services, sid)
    assert API_KEY not in text and MASTER_PW not in text
    assert base64.b64encode(API_KEY.encode()).decode() not in text
    # одна команда в запросе — JSON-массив [команда, параметры, метка]
    sent = [json.loads(c.body) for c in dv.fake.calls_to("POST", r"/doveadm/v1")]
    assert all(isinstance(b, list) and len(b) == 1 and len(b[0]) == 3 for b in sent)


def test_check_without_plugins_and_master(services, dv):
    dv.commands -= {"quotaGet", "sieveList", "sieveGet", "sievePut", "sieveActivate", "sieveDeactivate",
                    "sieveDelete"}
    sid = connect(services, dv, master_user="", master_password="")
    conn = msstore.get_server(services.db, sid)
    msstore.update_server(services.db, sid, name=conn.name, settings=dict(conn.settings, master_user=""),
                          secrets={"api_key": API_KEY}, sync_cron=conn.sync_cron, enabled=True, allow_changes=True,
                          sync_enabled=True, sync_add_new=False, monitor=True)
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out                                  # только предупреждения
    problems = {p[0]: p for p in out["status"]["problems"]}
    assert set(problems) == {"quota", "sieve", "imap_master"}
    assert "Pigeonhole" in problems["sieve"][2] and all(p[1] == "warning" for p in problems.values())


def test_wrong_key_is_explained_and_not_logged(services, dv):
    sid = connect(services, dv, api_key="Wrong-Doveadm-Key")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "не принял ключ API" in out["error"] and "doveadm_api_key" in out["hint"]
    text = transcripts(services, sid)
    assert "Wrong-Doveadm-Key" not in text and base64.b64encode(b"Wrong-Doveadm-Key").decode() not in text
    # пароль doveadm (Basic с именем doveadm) — тоже вход
    sid2 = connect(services, dv, name="Basic", auth="password", doveadm_password=DOVEADM_PW)
    assert mss.check(services, sid2, user="tester")["ok"]
    assert DOVEADM_PW not in transcripts(services, sid2)


def test_settings_validation_and_plain_http(dv):
    with pytest.raises(ValidationError, match="ключ API"):
        DovecotDriver.validate_settings({"base_url": "http://10.0.0.5:8080", "auth": "api_key"}, {})
    with pytest.raises(ValidationError, match="пароль мастер-пользователя"):
        DovecotDriver.validate_settings({"base_url": "10.0.0.5:8080", "master_user": "archmaster"},
                                        {"api_key": API_KEY})
    with pytest.raises(ValidationError, match="Разделитель"):
        DovecotDriver.validate_settings({"base_url": "10.0.0.5:8080", "master_mode": "separator",
                                         "master_separator": "ab"}, {"api_key": API_KEY})
    # обычный http на другую машину — только с разрешением; без схемы — https
    drv = driver(dv, base_url="http://10.0.0.5:8080")
    with pytest.raises(ServerError, match="без шифрования"):
        _ = drv.http
    drv = driver(dv, base_url="http://10.0.0.5:8080/doveadm/v1/", allow_plain=True)
    assert drv.http.url("/doveadm/v1") == "http://10.0.0.5:8080/doveadm/v1"
    assert drv.imap_server() == ("10.0.0.5", 993, "ssl")
    assert driver(dv, base_url="mail.company.ru:8443").http.url("/") == "https://mail.company.ru:8443/"


# ---------------------------------------------------------------------------
#  Сверка
# ---------------------------------------------------------------------------
def test_sync_all_users_with_quota(services, dv):
    sid = connect(services, dv)
    res = mss.sync(services, sid, user="tester")
    rows = {r["email"]: r for r in msstore.list_accounts(services.db, sid)}
    assert set(rows) == {"ivanov@company.ru", "petrova@company.ru", "boss@other.ru"}
    iv = rows["ivanov@company.ru"]
    assert iv["used_bytes"] == 6000 * 1024 and iv["quota_kb"] == 1048576 and iv["messages"] == 150
    assert iv["info"]["usage_source"] == "quota" and iv["info"]["folders"] == 2 and iv["login_blocked"] is None
    assert iv["autoreply"] is False and iv["forward_to"] == [] and iv["aliases"] == []
    pe = rows["petrova@company.ru"]
    assert pe["quota_kb"] is None and pe["used_bytes"] == 190 * 1024           # «-» — без предела
    # пользователь без домена (системный) — пропущен с объяснением, сверка при этом полная
    assert any("без домена" in p and "vmail" in p for p in res["problems"]) and res["incomplete"] is False
    alls = [p for c, p in dv.requests if c == "mailboxStatus" and p.get("allUsers")]
    assert alls == [{"allUsers": True, "field": ["messages", "vsize"], "mailboxMask": ["*"]}]


def test_sync_only_domains_and_without_quota_plugin(services, dv):
    dv.commands.discard("quotaGet")
    sid = connect(services, dv, domains="company.ru")
    mss.sync(services, sid, user="tester")
    rows = {r["email"]: r for r in msstore.list_accounts(services.db, sid)}
    assert set(rows) == {"ivanov@company.ru", "petrova@company.ru"}
    iv = rows["ivanov@company.ru"]
    assert iv["used_bytes"] == 6_000_000 and iv["quota_kb"] is None and iv["info"]["usage_source"] == "vsize"
    assert "модуль quota не подключён" in transcripts(services, sid)


def test_sync_error_inside_200_is_explained(services, dv):
    dv.fail_all["mailboxStatus"] = 75
    sid = connect(services, dv)
    with pytest.raises(ServerError) as err:
        mss.sync(services, sid, user="tester")
    assert "временная ошибка (код 75)" in err.value.message and err.value.retryable
    dv.fail_all = {"mailboxStatus": 78}
    with pytest.raises(ServerError) as err:
        mss.sync(services, sid, user="tester")
    assert "ошибка в настройках Dovecot (код 78)" in err.value.message and "итерация" in err.value.hint
    # сценарии Sieve не прочитались — сверка идёт, автоответ неизвестен
    dv.fail_all = {"sieveList": 75}
    res = mss.sync(services, sid, user="tester")
    assert res["accounts"] == 3
    assert msstore.get_account(services.db, sid, "ivanov@company.ru")["autoreply"] is None


@pytest.mark.parametrize("shape", ["rows", "object"])
def test_get_account_both_user_shapes_and_missing_user(dc, shape):
    dc.dv.user_shape = shape
    _res, items = act(dc.svc, "ax_refresh", [dc.iv])
    assert items[dc.iv]["status"] == "success" and "занято" in items[dc.iv]["detail"], items
    acc = driver(dc.dv).get_account("ivanov@company.ru")
    assert acc["info"]["home"] == "/var/vmail/ivanov@company.ru" and acc["used_bytes"] == 6000 * 1024
    # пользователя больше нет (код 67 внутри ответа 200) — учётная запись помечается удалённой
    dc.dv.users.pop("petrova@company.ru")
    _res, items = act(dc.svc, "ax_refresh", [dc.pe])
    assert items[dc.pe]["status"] == "success" and "больше нет" in items[dc.pe]["detail"], items
    assert msstore.get_account(dc.svc.db, dc.sid, "petrova@company.ru")["deleted_at"]
    assert driver(dc.dv).get_account("nobody@company.ru") is None
    assert driver(dc.dv, domains="company.ru").get_account("boss@other.ru") is None


# ---------------------------------------------------------------------------
#  Автоответ и пересылка — сценарий Sieve
# ---------------------------------------------------------------------------
def test_autoreply_and_forwarding_keep_users_script(dc):
    svc, dv = dc.svc, dc.dv
    _res, items = act(svc, "srv_autoreply", [dc.iv], mode="on", subject="Нет на месте",
                      text="Я больше не работаю.\nПишите petrova@company.ru", start="", end="2026-12-31")
    assert items[dc.iv]["status"] == "success", items
    assert dv.active["ivanov@company.ru"] == SIEVE_NAME and "roundcube" in dv.scripts["ivanov@company.ru"]
    script = dv.scripts["ivanov@company.ru"][SIEVE_NAME]
    assert 'vacation :days 1 :subject "Нет на месте"' in script
    assert 'currentdate :value "le" "date" "2026-12-31"' in script
    assert script.rstrip().endswith('include :personal "roundcube";')       # фильтры пользователя работают дальше
    assert msstore.get_account(svc.db, dc.sid, "ivanov@company.ru")["autoreply"] is True
    _res, items = act(svc, "srv_forwarding", [dc.iv], mode="on", addresses="petrova@company.ru", keep_copy=True)
    assert items[dc.iv]["status"] == "success", items
    script = dv.scripts["ivanov@company.ru"][SIEVE_NAME]
    assert 'redirect :copy "petrova@company.ru";' in script and "vacation" in script
    assert sieve_state(script)["include"] == "roundcube"
    row = msstore.get_account(svc.db, dc.sid, "ivanov@company.ru")
    assert row["forward_to"] == ["petrova@company.ru"] and row["autoreply"] is True
    drv = driver(dv)
    assert drv.get_forwarding("ivanov@company.ru") == {"addresses": ["petrova@company.ru"], "keep_copy": True}
    assert drv.get_autoreply("ivanov@company.ru")["end"] == "2026-12-31"
    # сверка видит то же
    mss.sync(svc, dc.sid, user="tester")
    assert msstore.get_account(svc.db, dc.sid, "ivanov@company.ru")["forward_to"] == ["petrova@company.ru"]
    # всё выключили — сценарий MailArchiver удалён, активен снова прежний
    act(svc, "srv_forwarding", [dc.iv], mode="off", addresses="", keep_copy=True)
    _res, items = act(svc, "srv_autoreply", [dc.iv], mode="off")
    assert items[dc.iv]["status"] == "success", items
    assert SIEVE_NAME not in dv.scripts["ivanov@company.ru"] and dv.active["ivanov@company.ru"] == "roundcube"
    row = msstore.get_account(svc.db, dc.sid, "ivanov@company.ru")
    assert row["autoreply"] is False and row["forward_to"] == []


def test_forwarding_without_copy_and_user_without_scripts(dc):
    svc, dv = dc.svc, dc.dv
    _res, items = act(svc, "srv_forwarding", [dc.pe], mode="on", addresses="boss@other.ru", keep_copy=False)
    assert items[dc.pe]["status"] == "success", items
    script = dv.scripts["petrova@company.ru"][SIEVE_NAME]
    assert 'redirect "boss@other.ru";\nstop;' in script and "include :personal" not in script
    assert dv.active["petrova@company.ru"] == SIEVE_NAME
    # пользователь переключился на свой сценарий в почтовом клиенте — пересылка больше не действует
    dv.scripts["petrova@company.ru"]["sogo"] = "keep;\n"
    dv.active["petrova@company.ru"] = "sogo"
    _res, items = act(svc, "ax_refresh", [dc.pe])
    assert msstore.get_account(svc.db, dc.sid, "petrova@company.ru")["forward_to"] == []
    _res, items = act(svc, "srv_forwarding", [dc.pe], mode="on", addresses="boss@other.ru", keep_copy=True)
    assert dv.active["petrova@company.ru"] == SIEVE_NAME
    assert sieve_state(dv.scripts["petrova@company.ru"][SIEVE_NAME])["include"] == "sogo"
    act(svc, "srv_forwarding", [dc.pe], mode="off", addresses="", keep_copy=True)
    assert dv.active["petrova@company.ru"] == "sogo" and SIEVE_NAME not in dv.scripts["petrova@company.ru"]


@pytest.mark.parametrize("as_404", [False, True])
def test_no_sieve_plugin_skips_with_reason(dc, as_404):
    dc.dv.commands -= {"sieveList", "sieveGet", "sievePut", "sieveActivate", "sieveDeactivate", "sieveDelete"}
    dc.dv.unknown_as_404 = as_404
    _res, items = act(dc.svc, "srv_autoreply", [dc.iv], mode="on", subject="", text="Отпуск", start="", end="")
    item = items[dc.iv]
    assert item["status"] == "skipped", item
    assert ("Pigeonhole" in item["detail"]) if not as_404 else ("404" in item["detail"])
    assert "Автоответ и пересылка через doveadm недоступны" in item["detail"]


# ---------------------------------------------------------------------------
#  Чего doveadm не умеет; IMAP
# ---------------------------------------------------------------------------
def test_user_database_actions_are_skipped_with_reason(dc):
    svc = dc.svc
    for action, params, words in (("ax_block", {"final_copy": False}, "закрыть и открыть вход"),
                                  ("ax_password", {"mode": "random_hidden"}, "пароли"),
                                  ("ax_quota", {"quota_mb": 2048}, "квота ящика"),
                                  ("ax_alias_add", {"template": "{f}.{last}"}, "псевдонимы"),
                                  ("ax_contact", {"source": "fixed", "company": "ООО"}, "ФИО"),
                                  ("ax_delete", {}, "удаление")):
        pv = base.run(svc, ADMIN, action, [dc.iv], params, preview=True)
        assert pv["counts"]["skip"] == 1, (action, pv)
        assert words in pv["results"][0]["detail"] and "так не умеет" in pv["results"][0]["detail"], (action, pv)
    with pytest.raises(ValidationError, match="создавать учётные записи"):
        create.run(svc, ADMIN, {"server_id": dc.sid, "text": "new@company.ru", "password_mode": "random_file"},
                   preview=True)
    assert "kick" in " ".join(DovecotDriver.limits) and "authCacheFlush" in " ".join(DovecotDriver.limits)


@pytest.mark.parametrize("mode", ["plain_authzid", "separator"])
def test_imap_copy_with_master_user(services, dv, monkeypatch, mode):
    sid = connect(services, dv, master_mode=mode, master_separator="*")
    mss.sync(services, sid, user="tester")
    world = ImapWorld()
    world.mailbox("petrova@company.ru", "nopass")
    world.add("petrova@company.ru", "INBOX", raw_message("Квартальный отчёт"))
    world.admins[MASTER] = MASTER_PW
    world.master_separator = "*"
    monkeypatch.setattr(client_mod, "IMAPClient", world.client_class())
    res = base.run(services, ADMIN, "ax_add_mailboxes", [f"ms:{sid}:petrova@company.ru"], {}, preview=False)
    assert res["counts"]["ok"] == 1, res
    acc = next(a for a in services.db.list_accounts() if a.username == "petrova@company.ru")
    assert acc.auth_type == AuthType.SERVER and acc.host == "mail.company.ru" and acc.password == ""
    from mailarchiver.imap.client import ImapConnection
    conn = ImapConnection(acc, services.connect_options())
    conn.connect()
    try:
        assert world.last_login == (mode, MASTER, "petrova@company.ru")
    finally:
        conn.close()
    # IMAP-сервер ящика не тот, что в подключении, — пароль мастер-пользователя туда не уходит
    acc.host = "evil.example.org"
    services.db.update_account(acc, update_password=False, update_oauth_secret=False, update_oauth_token=False)
    with pytest.raises(ServerError, match="входят только на mail.company.ru"):
        mss.imap_auth_for(services, services.db.get_account(acc.id))


def test_only_read_commands_are_repeatable(dc, monkeypatch):
    from mailarchiver.mailservers.http import HttpClient
    seen = []
    real = HttpClient.request

    def spy(self, method, path, **kw):
        if kw.get("json_body"):
            seen.append((kw["json_body"][0][0], kw.get("idempotent")))
        return real(self, method, path, **kw)
    monkeypatch.setattr(HttpClient, "request", spy)
    _res, items = act(dc.svc, "srv_forwarding", [dc.iv], mode="on", addresses="petrova@company.ru", keep_copy=True)
    assert items[dc.iv]["status"] == "success", items
    flags = dict(seen)
    assert flags["sievePut"] is False                                     # изменение после 502/504 не повторяется
    assert all(flags[c] is True for c in ("sieveList", "sieveGet", "mailboxStatus", "quotaGet", "user"))
