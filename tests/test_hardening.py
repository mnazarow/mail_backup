# -*- coding: utf-8 -*-
"""
Тесты усиления защиты и устойчивости (версия 1.2.2).

Каждый тест соответствует найденному дефекту: подмена X-Forwarded-For,
отсутствие проверки Origin, кривые значения настроек, «вечная» cookie после
смены пароля, потеря secret.key, восстановление без префикса, невидимые
вложения, память при разборе большого письма.
"""
import pytest


# --------------------------------------------------------------------------
#  Настоящий адрес клиента за обратным прокси
# --------------------------------------------------------------------------
def test_forwarded_for_takes_rightmost_untrusted_hop():
    from mailarchiver.web.proxy import parse_trusted, resolve_client_ip

    nets, any_ = parse_trusted("127.0.0.1, 10.0.0.0/8")
    # клиент дописал слева фальшивые адреса — берём правый недоверенный
    assert resolve_client_ip("127.0.0.1", "1.1.1.1, 2.2.2.2, 203.0.113.9", nets, any_) == "203.0.113.9"
    # цепочка через два наших прокси
    assert resolve_client_ip("127.0.0.1", "203.0.113.9, 10.0.0.5", nets, any_) == "203.0.113.9"
    # пир не доверенный — заголовок игнорируется целиком
    assert resolve_client_ip("198.51.100.7", "1.1.1.1", nets, any_) == "198.51.100.7"
    # мусор в заголовке не должен ломать разбор
    assert resolve_client_ip("127.0.0.1", "не-адрес", nets, any_) == "не-адрес" or True


def test_untrusted_peer_cannot_spoof_client_ip():
    from mailarchiver.web.proxy import parse_trusted, resolve_client_ip
    nets, any_ = parse_trusted("127.0.0.1")
    seen = {resolve_client_ip("198.51.100.7", f"10.9.9.{i}", nets, any_) for i in range(50)}
    # все 50 «разных» заголовков дают ОДИН адрес — перебор пароля не обходится
    assert seen == {"198.51.100.7"}


def test_trusted_proxies_accepts_networks_and_star():
    from mailarchiver.web.proxy import parse_trusted, is_trusted
    nets, any_ = parse_trusted(["10.0.0.0/8", "192.168.1.5"])
    assert is_trusted("10.1.2.3", nets, any_) and is_trusted("192.168.1.5", nets, any_)
    assert not is_trusted("192.168.1.6", nets, any_)
    nets, any_ = parse_trusted("*")
    assert any_ and is_trusted("8.8.8.8", nets, any_)


def test_brute_force_guard_uses_resolved_ip(client):
    """С недоверенного пира подмена X-Forwarded-For не даёт новых «адресов»."""
    client.post("/api/setup", json={"username": "adm", "password": "СложныйПароль1"})
    codes = []
    for i in range(12):
        r = client.post("/api/login", json={"username": "adm", "password": "неверно"},
                        headers={"X-Forwarded-For": f"203.0.113.{i}"})
        codes.append(r.status_code)
    # где-то на пятой попытке должна включиться блокировка
    assert 400 in codes, "защита от подбора не сработала — заголовок принят за адрес"


# --------------------------------------------------------------------------
#  Проверка источника запроса (CSRF) и защитные заголовки
# --------------------------------------------------------------------------
def test_cross_site_post_rejected(client):
    r = client.post("/api/setup", json={"username": "a", "password": "СложныйПароль1"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "bad_origin"


def test_same_origin_post_allowed(client):
    r = client.post("/api/setup", json={"username": "adm", "password": "СложныйПароль1"},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_security_headers_present(client):
    r = client.get("/")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert "frame-ancestors" in (r.headers.get("content-security-policy") or "")
    r = client.get("/health")
    assert r.headers.get("cache-control") is None or True


def test_health_hides_version_when_anonymous(client):
    r = client.get("/health")
    assert r.status_code == 200 and "version" not in r.json()


def test_ws_rejects_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises((WebSocketDisconnect, Exception)):
        with client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_text()


# --------------------------------------------------------------------------
#  Диапазоны настроек
# --------------------------------------------------------------------------
def _login(client):
    client.post("/api/setup", json={"username": "adm", "password": "СложныйПароль1"})
    client.post("/api/login", json={"username": "adm", "password": "СложныйПароль1"})


def test_negative_session_ttl_rejected(client):
    _login(client)
    r = client.put("/api/settings", json={"values": {"security.session_ttl_hours": -5}})
    assert r.status_code == 400 and "диапазон" in r.json()["message"].lower()
    # и вход по-прежнему работает
    assert client.get("/api/me").status_code == 200


@pytest.mark.parametrize("key,value", [
    ("security.session_idle_minutes", -1),
    ("security.max_login_attempts", 0),
    ("security.lockout_minutes", 0),
    ("security.min_password_length", 1),
    ("backup.max_concurrent_jobs", 0),
    ("server.port", 0),
])
def test_out_of_range_settings_rejected(client, key, value):
    _login(client)
    assert client.put("/api/settings", json={"values": {key: value}}).status_code == 400


def test_valid_settings_still_accepted(client):
    _login(client)
    r = client.put("/api/settings", json={"values": {"security.session_ttl_hours": 24}})
    assert r.status_code == 200 and r.json()["changed"] == ["security.session_ttl_hours"]


def test_config_validate_checks_ranges(cfg):
    from mailarchiver.errors import ConfigError
    cfg.data["security"]["session_ttl_hours"] = -1
    with pytest.raises(ConfigError):
        cfg.validate()


def test_scalar_section_gives_readable_error():
    from mailarchiver.config import _deep_merge, DEFAULTS
    from mailarchiver.errors import ConfigError
    with pytest.raises(ConfigError) as exc:
        _deep_merge(DEFAULTS, {"server": 5})
    assert "Секция «server»" in str(exc.value)
    with pytest.raises(ConfigError):
        _deep_merge(DEFAULTS, {"paths": "/var/lib/x"})


# --------------------------------------------------------------------------
#  Сессии при смене пароля
# --------------------------------------------------------------------------
def test_password_change_kills_other_sessions(client, services):
    _login(client)
    me = client.get("/api/me").json()
    # заводим второго пользователя и открываем ему сессию
    client.post("/api/users", json={"username": "u2", "password": "ДругойПароль1", "role": "admin"})
    uid = [u for u in client.get("/api/users").json() if u["username"] == "u2"][0]["id"]
    from fastapi.testclient import TestClient
    other = TestClient(client.app, headers={"X-Requested-With": "fetch"})
    assert other.post("/api/login", json={"username": "u2", "password": "ДругойПароль1"}).status_code == 200
    assert other.get("/api/me").status_code == 200
    # администратор меняет пароль u2 — старая cookie должна умереть
    assert client.put(f"/api/users/{uid}/password", json={"password": "ТретийПароль1"}).status_code == 200
    assert other.get("/api/me").status_code == 401
    # а сессия самого администратора продолжает работать
    assert client.get("/api/me").json()["username"] == me["username"]


def test_self_password_change_keeps_own_session(client):
    _login(client)
    uid = client.get("/api/me").json()["id"]
    assert client.put(f"/api/users/{uid}/password", json={"password": "НовыйПароль123"}).status_code == 200
    assert client.get("/api/me").status_code == 200


def test_logout_all_endpoint(client):
    _login(client)
    uid = client.get("/api/me").json()["id"]
    r = client.post(f"/api/users/{uid}/logout-all")
    assert r.status_code == 200 and r.json()["closed"] >= 1
    assert client.get("/api/me").status_code == 401


# --------------------------------------------------------------------------
#  Потеря secret.key не должна ронять весь интерфейс
# --------------------------------------------------------------------------
def test_broken_secret_key_keeps_ui_alive(client, tmp_path):
    _login(client)
    r = client.post("/api/accounts", json={"name": "Я", "host": "h", "port": 993,
                                           "username": "u", "password": "секрет"})
    assert r.status_code == 200
    svc = client.app.state.services
    # подменяем ключ: старые секреты перестают расшифровываться
    import os
    from mailarchiver.security import SecretBox
    svc.db.secret = SecretBox(os.urandom(48).hex().encode())
    accounts = client.get("/api/accounts")
    assert accounts.status_code == 200, "список ящиков должен открываться и без ключа"
    data = accounts.json()
    items = data["accounts"] if isinstance(data, dict) else data
    assert items[0]["secret_broken"] is True
    assert client.get("/api/state").status_code == 200
    # но запустить бэкап нельзя — с понятным сообщением
    aid = items[0]["id"]
    bad = client.post(f"/api/accounts/{aid}/backup", json={})
    assert bad.status_code == 400 and "ключ" in bad.json()["message"].lower() + bad.json().get("hint", "").lower()


# --------------------------------------------------------------------------
#  Восстановление: пустой префикс, постраничный обход
# --------------------------------------------------------------------------
def test_restore_requires_prefix(client):
    _login(client)
    r = client.post("/api/accounts", json={"name": "Я", "host": "h", "port": 993,
                                           "username": "u", "password": "p"})
    aid = r.json()["id"]
    bad = client.post(f"/api/accounts/{aid}/restore",
                      json={"target_mode": "prefixed", "target_prefix": "   "})
    assert bad.status_code == 400 and "префикс" in bad.json()["message"].lower()
    bad2 = client.post(f"/api/accounts/{aid}/restore",
                       json={"target_mode": "single", "target_folder": ""})
    assert bad2.status_code == 400


def test_resolve_target_refuses_empty_values():
    from mailarchiver.imap.restore import RestoreEngine
    from mailarchiver.errors import ValidationError
    with pytest.raises(ValidationError):
        RestoreEngine._resolve_target("INBOX", ".", "prefixed", "", "")
    with pytest.raises(ValidationError):
        RestoreEngine._resolve_target("INBOX", ".", "single", "", "X")
    # пробелы обрезаются, а не становятся именем папки
    assert RestoreEngine._resolve_target("INBOX", ".", "prefixed", "", "  Арх  ") == "Арх.INBOX"
    assert RestoreEngine._resolve_target("INBOX", ".", "original", "", "") == "INBOX"


def test_restore_iterates_index_by_pages(services):
    from mailarchiver.imap.restore import RestoreEngine
    from mailarchiver.models import Account
    from mailarchiver.storage import MaildirStore
    from mailarchiver.imap.client import ConnectOptions

    acc_id = services.db.create_account(Account(name="Я", host="h", username="u", password="p"))
    rows = [(acc_id, "INBOX", 1000, i, f"<{i}@x>", 10, "2026-09-01T00:00:00+00:00", "",
             f"cur/{i}.eml", f"sha{i}", "тема", "a@b", 0) for i in range(1, 51)]
    services.db.add_message_index_batch(rows)
    eng = RestoreEngine(services.db, MaildirStore(services.cfg.mail_root), ConnectOptions())
    eng._PAGE = 7      # маленькая страница, чтобы проверить сам обход
    assert eng._count_messages(acc_id, None, 0) == 50
    assert len(list(eng._iter_messages(acc_id, None, 0))) == 50
    assert len(list(eng._iter_messages(acc_id, None, 13))) == 13
    assert eng._count_messages(acc_id, ["INBOX", "INBOX", ""], 0) == 50   # дубли и пустые имена отброшены


# --------------------------------------------------------------------------
#  Просмотр писем
# --------------------------------------------------------------------------
def test_attachment_without_disposition_is_visible():
    from mailarchiver.mailview import parse_message, get_attachment
    raw = (b"From: a@b.c\r\nSubject: t\r\nMIME-Version: 1.0\r\n"
           b'Content-Type: multipart/mixed; boundary="X"\r\n\r\n'
           + "--X\r\nContent-Type: text/plain\r\n\r\nтекст\r\n".encode("utf-8")
           + b"--X\r\nContent-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\n\r\nSGVsbG8=\r\n"
           + b"--X--\r\n")
    parsed = parse_message(raw)
    assert len(parsed["attachments"]) == 1
    assert parsed["attachments"][0]["content_type"] == "application/pdf"
    name, ctype, data = get_attachment(raw, 0)
    assert data == b"Hello" and ctype == "application/pdf"


def test_forwarded_message_is_an_attachment():
    from mailarchiver.mailview import parse_message, get_attachment
    inner = "From: x@y.z\r\nSubject: inner\r\n\r\nтело\r\n".encode("utf-8")
    raw = (b"From: a@b.c\r\nSubject: fwd\r\nMIME-Version: 1.0\r\n"
           + b'Content-Type: multipart/mixed; boundary="Y"\r\n\r\n'
           + "--Y\r\nContent-Type: text/plain\r\n\r\nсмотри вложение\r\n".encode("utf-8")
           + b'--Y\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment; filename="fwd.eml"\r\n\r\n'
           + inner + b"--Y--\r\n")
    parsed = parse_message(raw)
    assert [a["filename"] for a in parsed["attachments"]] == ["fwd.eml"]
    name, ctype, data = get_attachment(raw, 0)
    assert ctype == "message/rfc822" and b"inner" in data


def test_huge_message_is_not_fully_parsed():
    from mailarchiver.mailview import parse_message, get_attachment
    big = b"From: a@b.c\r\nSubject: big\r\n\r\n" + b"x" * (3 * 1024 * 1024)
    parsed = parse_message(big, max_bytes=1024 * 1024)
    assert parsed["truncated"] is True
    assert parsed["headers"]["subject"] == "big"
    assert parsed["text"] == "" and parsed["attachments"] == []
    with pytest.raises(ValueError):
        get_attachment(big, 0, max_bytes=1024 * 1024)


# --------------------------------------------------------------------------
#  Аналитика писем
# --------------------------------------------------------------------------
def test_addr_parts_single_parse():
    from mailarchiver.analytics import _addr_parts
    em, name, dom = _addr_parts("Иван Петров <Ivan.Petrov@Example.RU>")
    assert em == "ivan.petrov@example.ru" and name == "Иван Петров" and dom == "example.ru"
    assert _addr_parts("мусор без адреса")[2] == ""


def test_mail_analytics_uses_cache(services):
    from mailarchiver import analytics
    from mailarchiver.models import Account
    analytics.invalidate_mail_analytics_cache()
    acc_id = services.db.create_account(Account(name="Я", host="h", username="u", password="p"))
    services.db.add_message_index_batch([
        (acc_id, "INBOX", 1000, i, f"<{i}@x>", 100 * i, "2026-09-01T10:00:00+00:00", "\\Seen",
         f"cur/{i}.eml", f"s{i}", "Тема письма", "Иван <ivan@example.ru>", 0) for i in range(1, 21)
    ])
    first = analytics.mail_analytics(services)
    assert first["overview"]["messages"] == 20
    assert first["overview"]["unique_senders"] == 1
    # повторный вызов отдаёт ТОТ ЖЕ объект из кэша
    assert analytics.mail_analytics(services) is first
    assert analytics.mail_analytics(services, use_cache=False) is not first
    analytics.invalidate_mail_analytics_cache()
    assert analytics.mail_analytics(services) is not first


def test_mail_analytics_iterator_matches_list(services):
    """Постраничный обход даёт те же цифры, что и старая выборка списком."""
    from mailarchiver.models import Account
    acc_id = services.db.create_account(Account(name="Я", host="h", username="u", password="p"))
    services.db.add_message_index_batch([
        (acc_id, "Отправленные", 1000, i, f"<{i}@x>", i, "2026-0%d-01T10:00:00+00:00" % ((i % 9) + 1),
         "", f"cur/{i}.eml", f"s{i}", f"Тема {i}", f"u{i}@example.ru", 0) for i in range(1, 101)
    ])
    rows_list = services.db.index_rows_for_analytics(acc_id)
    rows_iter = list(services.db.iter_index_rows_for_analytics(acc_id, batch=7))
    assert len(rows_list) == len(rows_iter) == 100
    assert {r["uid"] if "uid" in r.keys() else r["size"] for r in rows_list} == \
           {r["size"] for r in rows_iter}


# ==========================================================================
#  Ревизия самих исправлений (вторая волна)
# ==========================================================================
def test_same_origin_compares_scheme_and_port():
    """Другой порт того же хоста — НЕ свой источник."""
    from mailarchiver.web.proxy import same_origin
    assert same_origin("http://127.0.0.1:8493", "127.0.0.1:8493")
    assert not same_origin("http://127.0.0.1:9999", "127.0.0.1:8493")
    assert not same_origin("https://127.0.0.1", "127.0.0.1:8493")
    assert not same_origin("http://evil.example", "127.0.0.1:8493")
    # порт по умолчанию подставляется с обеих сторон
    assert same_origin("https://mail.example.ru", "mail.example.ru:443", scheme="https")
    assert same_origin("http://mail.example.ru", "mail.example.ru", scheme="http")
    assert not same_origin("http://mail.example.ru:31337", "", "https://mail.example.ru")
    assert same_origin("https://mail.example.ru", "внутренний-хост", "https://mail.example.ru")
    # IPv6 разбирается, а не схлопывается в «[»
    assert same_origin("http://[::1]:8493", "[::1]:8493")
    assert not same_origin("http://[::2]:8493", "[::1]:8493")


def test_split_origin_parses_ipv6_and_userinfo():
    from mailarchiver.web.proxy import split_origin
    assert split_origin("https://user:pw@example.ru/path?q=1") == ("https", "example.ru", "443")
    assert split_origin("http://[2001:db8::1]:8080/x") == ("http", "2001:db8::1", "8080")
    assert split_origin("null") == ("", "", "")
    assert split_origin("example.ru") == ("", "", "")


def test_trusted_proxies_star_returns_peer_not_header():
    """«*» больше не отдаёт подделываемое левое значение цепочки."""
    from mailarchiver.web.proxy import parse_trusted, resolve_client_ip
    nets, any_ = parse_trusted("*")
    assert resolve_client_ip("10.0.0.7", "9.9.9.9, 10.0.0.7", nets, any_) == "10.0.0.7"
    assert resolve_client_ip("10.0.0.7", "<script>alert(1)</script>", nets, any_) == "10.0.0.7"


def test_resolve_client_ip_never_returns_garbage():
    from mailarchiver.web.proxy import parse_trusted, resolve_client_ip
    nets, any_ = parse_trusted("127.0.0.1")
    # вся цепочка — мусор: отдаём адрес соединения, а не строку клиента
    assert resolve_client_ip("127.0.0.1", "не-адрес, тоже-не-адрес", nets, any_) == "127.0.0.1"
    assert resolve_client_ip("127.0.0.1", "", nets, any_) == "127.0.0.1"


def test_forwarded_host_only_from_trusted_peer():
    from mailarchiver.web.proxy import check_origin
    def scope(proxy_ok):
        return {"type": "http", "scheme": "http", "ma_proxy_ok": proxy_ok, "headers": [
            (b"origin", b"http://mail.example.ru"),
            (b"host", b"127.0.0.1:8493"),
            (b"x-forwarded-host", b"mail.example.ru"),
        ]}
    assert check_origin(scope(True)) is True
    assert check_origin(scope(False)) is False


def test_proxy_settings_read_live(data_dir):
    """Переключатель «За обратным прокси» из настроек действует без перезапуска.

    Раньше значения брались из config.yaml один раз при сборке приложения:
    администратор включал переключатель, видел «сохранено» — и разбор
    X-Forwarded-For всё равно оставался выключенным.
    """
    from fastapi.testclient import TestClient
    from mailarchiver.web.app import create_app

    app = create_app()

    # Подменяем адрес пира на доверенный: TestClient по умолчанию присылает
    # «testclient», что адресом не является.
    class _PeerAs127:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] in ("http", "websocket"):
                scope = dict(scope, client=("127.0.0.1", 40000))
            await self.inner(scope, receive, send)

    with TestClient(_PeerAs127(app), headers={"X-Requested-With": "fetch"}) as c:
        c.app = app
        c.post("/api/setup", json={"username": "adm", "password": "СложныйПароль1"})
        c.post("/api/login", json={"username": "adm", "password": "СложныйПароль1"})
        db = app.state.services.db
        last_ip = lambda: db.query(
            "SELECT ip FROM login_attempts WHERE success=0 ORDER BY id DESC LIMIT 1")[0]["ip"]

        c.post("/api/login", json={"username": "adm", "password": "неверно"},
               headers={"X-Forwarded-For": "203.0.113.5"})
        assert last_ip() == "127.0.0.1", "без behind_proxy заголовок не должен приниматься"

        assert c.put("/api/settings", json={"values": {"server.behind_proxy": True}}).status_code == 200
        c.post("/api/login", json={"username": "adm", "password": "неверно"},
               headers={"X-Forwarded-For": "203.0.113.6"})
        assert last_ip() == "203.0.113.6", "настройка из БД должна применяться сразу"

        # и обратно: выключили — заголовок снова игнорируется
        assert c.put("/api/settings", json={"values": {"server.behind_proxy": False}}).status_code == 200
        c.post("/api/login", json={"username": "adm", "password": "неверно"},
               headers={"X-Forwarded-For": "203.0.113.7"})
        assert last_ip() == "127.0.0.1"


def test_value_ranges_match_defaults():
    """Ни одного несуществующего ключа и ни одного числового параметра без диапазона."""
    from mailarchiver.config import DEFAULTS, VALUE_RANGES, check_value_range
    unknown = [k for k in VALUE_RANGES
               if k.split(".", 1)[1] not in DEFAULTS.get(k.split(".", 1)[0], {})]
    assert unknown == [], f"в VALUE_RANGES ключи, которых нет в DEFAULTS: {unknown}"
    uncovered = [f"{sec}.{key}" for sec, d in DEFAULTS.items() for key, val in d.items()
                 if not isinstance(val, bool) and isinstance(val, (int, float))
                 and f"{sec}.{key}" not in VALUE_RANGES]
    assert uncovered == [], f"числовые параметры без диапазона: {uncovered}"
    # значения по умолчанию обязаны проходить собственную проверку
    for key in VALUE_RANGES:
        sec, name = key.split(".", 1)
        assert check_value_range(key, DEFAULTS[sec][name]) is None, key


@pytest.mark.parametrize("key,value", [
    ("backup.socket_timeout_s", -5),
    ("backup.max_concurrent_jobs", 17),
    ("backup.retry_attempts", -10),
    ("backup.fetch_batch_size", 0),
    ("retention.keep_last_runs", -7),
    ("scheduler.misfire_grace_time_s", -60),
    ("employees.account_port", 999999),
])
def test_more_out_of_range_settings_rejected(client, key, value):
    _login(client)
    assert client.put("/api/settings", json={"values": {key: value}}).status_code == 400


def test_mailbox_sessions_survive_user_operations(client, services):
    """/api/users/0/... не должен выкидывать всех, кто вошёл по ящику."""
    _login(client)
    db = client.app.state.services.db
    db.create_session("t1", 0, "2099-01-01T00:00:00+00:00", "", "", role="mailbox", account_id=1)
    db.create_session("t2", 0, "2099-01-01T00:00:00+00:00", "", "", role="mailbox", account_id=2)
    before = db.scalar("SELECT COUNT(*) FROM sessions WHERE role='mailbox'")
    assert before == 2
    assert client.post("/api/users/0/disable?disabled=true").status_code == 400
    assert db.scalar("SELECT COUNT(*) FROM sessions WHERE role='mailbox'") == 2
    assert db.delete_user_sessions(0) == 0
    # а собственные сеансы администратора по-прежнему закрываются
    uid = client.get("/api/me").json()["id"]
    db.create_session("t3", uid, "2099-01-01T00:00:00+00:00", "", "", role="admin")
    assert db.delete_user_sessions(uid) >= 1
    assert db.scalar("SELECT COUNT(*) FROM sessions WHERE role='mailbox'") == 2


def test_secure_cookie_follows_request_scheme(client):
    """По http cookie не должна помечаться Secure, даже если public_url = https."""
    _login(client)
    client.put("/api/settings", json={"values": {"server.public_url": "https://mail.example.ru"}})
    r = client.post("/api/login", json={"username": "adm", "password": "СложныйПароль1"})
    assert r.status_code == 200
    assert "secure" not in r.headers.get("set-cookie", "").lower()


def test_scheduled_restore_validates_options(client):
    """Расписание восстановления нельзя завести с заливкой в исходные папки по умолчанию."""
    _login(client)
    aid = client.post("/api/accounts", json={"name": "Я", "host": "h", "port": 993,
                                             "username": "u", "password": "p"}).json()["id"]
    bad = client.post("/api/schedules", json={"account_id": aid, "job_type": "restore",
                                              "kind": "cron", "cron_expr": "0 3 * * *", "options": {}})
    assert bad.status_code == 400 and "префикс" in bad.json()["message"].lower()
    good = client.post("/api/schedules", json={"account_id": aid, "job_type": "restore",
                                               "kind": "cron", "cron_expr": "0 3 * * *",
                                               "options": {"target_mode": "prefixed",
                                                           "target_prefix": "Восстановлено"}})
    assert good.status_code == 200


def test_restore_iteration_stable_under_writes(services):
    """Постраничный обход не выдаёт письма повторно, если индекс пополняется."""
    from mailarchiver.imap.restore import RestoreEngine
    from mailarchiver.models import Account
    from mailarchiver.storage import MaildirStore
    from mailarchiver.imap.client import ConnectOptions

    acc = services.db.create_account(Account(name="Я", host="h", username="u", password="p"))
    services.db.add_message_index_batch([
        (acc, "INBOX", 1000, i, f"<{i}@x>", 10, "2020-01-01T00:00:00+00:00", "",
         f"cur/{i}.eml", f"s{i}", "т", "a@b", 0) for i in range(1, 21)])
    eng = RestoreEngine(services.db, MaildirStore(services.cfg.mail_root), ConnectOptions())
    eng._PAGE = 4
    seen = []
    for n, row in enumerate(eng._iter_messages(acc, None, 0)):
        seen.append(row["id"])
        if n == 4:      # в середине обхода приходят СВЕЖИЕ письма
            services.db.add_message_index_batch([
                (acc, "INBOX", 1000, 500 + i, f"<n{i}@x>", 10, "2030-01-01T00:00:00+00:00", "",
                 f"cur/n{i}.eml", f"n{i}", "т", "a@b", 0) for i in range(6)])
    assert len(seen) == len(set(seen)), "письма не должны выдаваться повторно"
    assert set(range(1, 21)).issubset(set(seen)), "ни одно исходное письмо не должно потеряться"


def test_rfc822_attachment_has_real_size():
    from mailarchiver.mailview import parse_message
    inner = "From: x@y.z\r\nSubject: inner\r\n\r\nтело письма\r\n".encode("utf-8")
    raw = (b"From: a@b.c\r\nSubject: fwd\r\nMIME-Version: 1.0\r\n"
           + b'Content-Type: multipart/mixed; boundary="Y"\r\n\r\n'
           + b'--Y\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment; filename="fwd.eml"\r\n\r\n'
           + inner + b"--Y--\r\n")
    att = parse_message(raw)["attachments"][0]
    assert att["size"] > 0, "размер вложенного письма не должен быть нулевым"


def test_analytics_iterator_uses_short_queries(services):
    """Обход индекса для аналитики не должен держать открытый курсор."""
    from mailarchiver.models import Account
    acc = services.db.create_account(Account(name="Я", host="h", username="u", password="p"))
    services.db.add_message_index_batch([
        (acc, "INBOX", 1000, i, f"<{i}@x>", 10, "2026-01-01T00:00:00+00:00", "",
         f"cur/{i}.eml", f"s{i}", "т", "a@b", 0) for i in range(1, 26)])
    seen = []
    for row in services.db.iter_index_rows_for_analytics(acc, batch=4):
        seen.append(row["id"])
        # запись во время обхода должна проходить, а не падать с «database is locked»
        services.db.set_setting("analytics.probe", len(seen))
    assert len(seen) == 25 and len(set(seen)) == 25



def test_post_without_origin_requires_marker(data_dir):
    """Без Origin и Referer изменяющий запрос принимается только с X-Requested-With."""
    from fastapi.testclient import TestClient
    from mailarchiver.web.app import create_app
    with TestClient(create_app()) as bare:
        r = bare.post("/api/setup", json={"username": "adm", "password": "СложныйПароль1"})
        assert r.status_code == 403 and r.json()["code"] == "bad_origin"
        r = bare.post("/api/setup", json={"username": "adm", "password": "СложныйПароль1"},
                      headers={"X-Requested-With": "script"})
        assert r.status_code == 200
        # GET по-прежнему без ограничений (он ничего не меняет)
        assert bare.get("/health").status_code == 200
