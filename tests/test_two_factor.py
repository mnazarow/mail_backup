# -*- coding: utf-8 -*-
"""Двухфакторный вход (TOTP, RFC 6238) — версия 1.3.0."""
import base64

import pytest
from fastapi.testclient import TestClient

from mailarchiver import totp

HDR = {"X-Requested-With": "fetch"}
PW = "СложныйПароль1"


def test_rfc6238_vectors():
    sec = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    for t, want in {59: "94287082", 1111111109: "07081804", 1234567890: "89005924",
                    2000000000: "69279037"}.items():
        assert totp.code_at(sec, t // 30, digits=8) == want


def test_verify_window_and_replay():
    sec = totp.new_secret()
    now = 1_700_000_000
    step = int(now // 30)
    assert totp.verify(sec, totp.code_at(sec, step), now=now) == step
    assert totp.verify(sec, totp.code_at(sec, step - 1), now=now) == step - 1      # часы отстают
    assert totp.verify(sec, totp.code_at(sec, step - 3), now=now) is None          # слишком старый
    assert totp.verify(sec, totp.code_at(sec, step), now=now, last_step=step) is None  # повтор
    assert totp.verify(sec, "12 34 5", now=now) is None


def test_qr_code_is_valid_svg():
    from mailarchiver import qrcode
    uri = totp.provisioning_uri(totp.new_secret(), "admin")
    grid = qrcode.encode(uri)
    n = len(grid)
    assert (n - 17) % 4 == 0 and 21 <= n <= 177
    # три поисковых узора по углам
    for (x0, y0) in ((0, 0), (n - 7, 0), (0, n - 7)):
        assert all(grid[y0][x0 + i] for i in range(7)) and all(grid[y0 + i][x0] for i in range(7))
    svg = qrcode.to_svg(uri)
    assert svg.startswith("<svg") and "<script" not in svg


def _enable(client, svc):
    client.post("/api/setup", json={"username": "adm", "password": PW})
    client.post("/api/login", json={"username": "adm", "password": PW})
    uid = client.get("/api/me").json()["id"]
    setup = client.post("/api/me/2fa/setup").json()
    assert setup["qr_svg"].startswith("<svg") and setup["uri"].startswith("otpauth://totp/")
    secret = svc.db.totp_state(uid)["pending"]
    r = client.post("/api/me/2fa/enable", json={"code": totp.code_at(secret, totp.current_step())})
    assert r.status_code == 200, r.text
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10
    return uid, secret, codes


def _fresh(app):
    return TestClient(app, headers=HDR)


def test_login_requires_code_after_enabling(client):
    svc = client.app.state.services
    uid, secret, _codes = _enable(client, svc)
    c = _fresh(client.app)
    r = c.post("/api/login", json={"username": "adm", "password": PW})
    body = r.json()
    assert r.status_code == 200 and body["otp_required"] is True and "user" not in body
    assert c.get("/api/me").status_code == 401, "до ввода кода сессии быть не должно"
    code = totp.code_at(secret, totp.current_step() + 1)
    ok = c.post("/api/login/otp", json={"challenge": body["challenge"], "code": code})
    assert ok.status_code == 200 and ok.json()["user"]["username"] == "adm"
    assert c.get("/api/me").json()["totp_enabled"] is True
    # тот же код второй раз не проходит (перехваченный код бесполезен)
    c2 = _fresh(client.app)
    ch = c2.post("/api/login", json={"username": "adm", "password": PW}).json()["challenge"]
    assert c2.post("/api/login/otp", json={"challenge": ch, "code": code}).status_code == 400


def test_wrong_codes_are_rate_limited(client):
    svc = client.app.state.services
    _enable(client, svc)
    c = _fresh(client.app)
    msgs = []
    for _ in range(4):
        r = c.post("/api/login", json={"username": "adm", "password": PW}).json()
        if "challenge" not in r:
            msgs.append(r.get("message", ""))
            break
        for _ in range(3):
            msgs.append(c.post("/api/login/otp", json={"challenge": r["challenge"], "code": "000000"}).json()["message"])
    assert any("Слишком много" in m for m in msgs), msgs
    # и повторный ввод ВЕРНОГО пароля счётчик не сбрасывает
    again = c.post("/api/login", json={"username": "adm", "password": PW}).json()
    assert "Слишком много" in again.get("message", "")


def test_ticket_is_single_use_and_attempt_limited(client):
    svc = client.app.state.services
    _uid, secret, _codes = _enable(client, svc)
    c = _fresh(client.app)
    ch = c.post("/api/login", json={"username": "adm", "password": PW}).json()["challenge"]
    ok = c.post("/api/login/otp", json={"challenge": ch, "code": totp.code_at(secret, totp.current_step() + 1)})
    assert ok.status_code == 200
    # тот же билет второй раз не принимается, даже с новым верным кодом
    again = _fresh(client.app).post("/api/login/otp", json={"challenge": ch,
                                                             "code": totp.code_at(secret, totp.current_step() + 1)})
    assert again.status_code == 400 and "Войдите заново" in again.json()["message"]
    # по одному билету — не больше трёх попыток, дальше и верный код не поможет
    c2 = _fresh(client.app)
    ch = c2.post("/api/login", json={"username": "adm", "password": PW}).json()["challenge"]
    msgs = [c2.post("/api/login/otp", json={"challenge": ch, "code": "111111"}).json()["message"] for _ in range(3)]
    assert "исчерпаны" in msgs[-1]
    r = c2.post("/api/login/otp", json={"challenge": ch, "code": totp.code_at(secret, totp.current_step())})
    assert r.status_code == 400 and "Войдите заново" in r.json()["message"]


def test_password_change_revokes_ticket(client):
    svc = client.app.state.services
    uid, secret, _codes = _enable(client, svc)
    from mailarchiver.security import hash_password
    c = _fresh(client.app)
    ch = c.post("/api/login", json={"username": "adm", "password": PW}).json()["challenge"]
    # смена пароля в обход set_user_password — срабатывает привязка билета к паролю
    svc.db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password("НовыйПароль123"), uid))
    r = c.post("/api/login/otp", json={"challenge": ch, "code": totp.code_at(secret, totp.current_step())})
    assert r.status_code == 400 and "Пароль учётной записи изменился" in r.json()["message"]
    # обычная смена пароля гасит выданные билеты сразу
    ch = c.post("/api/login", json={"username": "adm", "password": "НовыйПароль123"}).json()["challenge"]
    svc.db.set_user_password(uid, hash_password(PW))
    r = c.post("/api/login/otp", json={"challenge": ch, "code": totp.code_at(secret, totp.current_step())})
    assert r.status_code == 400 and "Войдите заново" in r.json()["message"]


def test_distributed_code_guessing_is_locked_per_account(client, monkeypatch):
    """Перебор кода с разных адресов упирается в лимит на учётную запись;
    резервный код при этом продолжает работать."""
    svc = client.app.state.services
    _uid, secret, codes = _enable(client, svc)
    from mailarchiver.web import auth
    ip = {"v": "10.0.0.1"}
    monkeypatch.setattr(auth, "client_ip", lambda request: ip["v"])
    msgs = []
    for n in range(6):
        ip["v"] = f"10.0.1.{n}"
        c = _fresh(client.app)
        ch = c.post("/api/login", json={"username": "adm", "password": PW}).json()["challenge"]
        for _ in range(2):
            msgs.append(c.post("/api/login/otp", json={"challenge": ch, "code": "000000"}).json()["message"])
    locked = [m for m in msgs if "заблокирован" in m]
    assert locked and msgs.index(locked[0]) == 10, msgs     # проверено ровно 10 кодов
    ip["v"] = "10.0.2.1"
    c = _fresh(client.app)
    ch = c.post("/api/login", json={"username": "adm", "password": PW}).json()["challenge"]
    r = c.post("/api/login/otp", json={"challenge": ch, "code": totp.code_at(secret, totp.current_step())})
    assert r.status_code == 400 and "заблокирован" in r.json()["message"]
    r = c.post("/api/login/otp", json={"challenge": ch, "code": codes[1]})
    assert r.status_code == 200, r.text


def test_parallel_volley_checks_limited_codes(client):
    """Залп параллельных запросов с одним билетом проверяет не больше кодов, чем
    разрешено на билет."""
    from concurrent.futures import ThreadPoolExecutor
    svc = client.app.state.services
    _enable(client, svc)
    c = _fresh(client.app)
    ch = c.post("/api/login", json={"username": "adm", "password": PW}).json()["challenge"]

    def shot(_):
        return _fresh(client.app).post("/api/login/otp", json={"challenge": ch, "code": "000000"}).status_code

    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(shot, range(40)))
    assert set(codes) == {400}
    checked = svc.db.scalar("SELECT COUNT(*) FROM login_attempts WHERE kind='otp'")
    assert checked <= 3, checked


def test_recovery_code_works_without_readable_secret(client):
    svc = client.app.state.services
    uid, secret, codes = _enable(client, svc)
    svc.db.execute("UPDATE users SET totp_secret_enc='испорчено' WHERE id=?", (uid,))
    c = _fresh(client.app)
    ch = c.post("/api/login", json={"username": "adm", "password": PW}).json()["challenge"]
    r = c.post("/api/login/otp", json={"challenge": ch, "code": totp.code_at(secret, totp.current_step())})
    assert r.status_code == 400 and "не расшифровывается" in r.json()["message"]
    r = c.post("/api/login/otp", json={"challenge": ch, "code": codes[2]})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("code", ["１２３４５６", "12345²", "1" * 100000], ids=["fullwidth", "superscript", "huge"])
def test_odd_codes_are_rejected_cleanly(client, code):
    svc = client.app.state.services
    _enable(client, svc)
    c = _fresh(client.app)
    ch = c.post("/api/login", json={"username": "adm", "password": PW}).json()["challenge"]
    r = c.post("/api/login/otp", json={"challenge": ch, "code": code})
    if len(code) > 128:
        assert r.status_code == 422 and "слишком длинное" in r.json()["message"]
    else:
        assert r.status_code == 400 and "Неверный код" in r.json()["message"]


def test_code_endpoints_count_failures(client):
    """Подбор кода через /me/2fa/recovery (с открытой сессией) тоже ограничен."""
    svc = client.app.state.services
    _enable(client, svc)
    msgs = [client.post("/api/me/2fa/recovery", json={"code": "000000"}).json()["message"] for _ in range(12)]
    assert "заблокирован" in msgs[-1], msgs
    # пароль при отключении 2FA — с тем же учётом неудач, что и при входе
    msgs = [client.post("/api/me/2fa/disable", json={"password": "неверный", "code": "000000"}).json()["message"]
            for _ in range(7)]
    assert "Слишком много" in msgs[-1], msgs


def test_recovery_code_works_once(client):
    svc = client.app.state.services
    _uid, _secret, codes = _enable(client, svc)
    c = _fresh(client.app)
    ch = c.post("/api/login", json={"username": "adm", "password": PW}).json()["challenge"]
    r = c.post("/api/login/otp", json={"challenge": ch, "code": codes[0].upper()})
    assert r.status_code == 200 and r.json()["user"]["recovery_left"] == 9
    c2 = _fresh(client.app)
    ch = c2.post("/api/login", json={"username": "adm", "password": PW}).json()["challenge"]
    assert c2.post("/api/login/otp", json={"challenge": ch, "code": codes[0]}).status_code == 400


def test_challenge_is_bound_and_expires(client, monkeypatch):
    svc = client.app.state.services
    _uid, secret, _codes = _enable(client, svc)
    from mailarchiver.web import auth
    c = _fresh(client.app)
    ch = c.post("/api/login", json={"username": "adm", "password": PW}).json()["challenge"]
    # подделанный билет
    assert c.post("/api/login/otp", json={"challenge": ch[:-2] + "00", "code": "123456"}).status_code == 400
    # истёкший билет
    monkeypatch.setattr(auth.time, "time", lambda: 10 ** 10)
    r = c.post("/api/login/otp", json={"challenge": ch, "code": totp.code_at(secret, totp.current_step())})
    assert r.status_code == 400 and "истекло" in r.json()["message"]


def test_disable_needs_password_and_code(client):
    svc = client.app.state.services
    uid, secret, _codes = _enable(client, svc)
    code = totp.code_at(secret, totp.current_step() + 1)
    assert client.post("/api/me/2fa/disable", json={"password": "неверный", "code": code}).status_code == 400
    assert svc.db.totp_state(uid)["enabled"]
    assert client.post("/api/me/2fa/disable", json={"password": PW, "code": code}).status_code == 200
    assert not svc.db.totp_state(uid)["enabled"]
    # после отключения вход снова по паролю
    c = _fresh(client.app)
    assert c.post("/api/login", json={"username": "adm", "password": PW}).json()["ok"] is True


def test_require_2fa_blocks_until_enrolled(client):
    svc = client.app.state.services
    client.post("/api/setup", json={"username": "adm", "password": PW})
    client.post("/api/login", json={"username": "adm", "password": PW})
    assert client.put("/api/settings", json={"values": {"security.require_2fa": True}}).status_code == 200
    blocked = client.get("/api/state")
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "2fa_required"
    me = client.get("/api/me").json()
    assert me["must_enroll_2fa"] is True
    # включаем — и всё открывается
    uid = me["id"]
    client.post("/api/me/2fa/setup")
    secret = svc.db.totp_state(uid)["pending"]
    assert client.post("/api/me/2fa/enable", json={"code": totp.code_at(secret, totp.current_step())}).status_code == 200
    assert client.get("/api/state").status_code == 200
    # при обязательной 2FA отключить её нельзя
    code = totp.code_at(secret, totp.current_step() + 1)
    assert client.post("/api/me/2fa/disable", json={"password": PW, "code": code}).status_code == 400


def test_admin_can_reset_other_users_2fa(client):
    svc = client.app.state.services
    uid, _secret, _codes = _enable(client, svc)
    client.post("/api/users", json={"username": "second", "password": "ВторойПароль1", "role": "admin"})
    second = [u for u in client.get("/api/users").json() if u["username"] == "second"][0]["id"]
    svc.db.enable_totp(second, totp.new_secret(), [], 0)
    assert [u for u in client.get("/api/users").json() if u["id"] == second][0]["totp_enabled"] is True
    assert client.post(f"/api/users/{uid}/2fa/reset").status_code == 400          # себе — нельзя
    assert client.post(f"/api/users/{second}/2fa/reset").status_code == 200
    assert not svc.db.totp_state(second)["enabled"]


def test_cli_reset_2fa(data_dir, capsys):
    from mailarchiver.__main__ import main
    from mailarchiver.config import load_config
    from mailarchiver.service import Services
    svc = Services(load_config())
    svc.setup()
    uid = svc.db.create_user("adm", "x", role="admin")
    svc.db.enable_totp(uid, totp.new_secret(), [], 0)
    assert main(["reset-2fa", "-u", "adm"]) == 0
    assert not svc.db.totp_state(uid)["enabled"]
    assert "отключён" in capsys.readouterr().out
    # Повторный вызов честно говорит, что 2FA уже не включена.
    assert main(["reset-2fa", "-u", "ADM"]) == 0
    assert "не был включён" in capsys.readouterr().out
    assert main(["reset-2fa", "-u", "нет-такого"]) == 1
