# -*- coding: utf-8 -*-
"""Роль «Оператор»: следит за копированием и запускает его, но не читает письма,
не меняет настроек и ничего не удаляет.

Главная проверка — матрица по ВСЕМ точкам API: всё, что не разрешено оператору
явно, отвечает 403. Новая точка API попадёт под эту проверку автоматически.
"""
import json
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mailarchiver import roles

HDR = {"X-Requested-With": "fetch"}
PW = "СложныйПароль1"

#: Что оператору разрешено (кроме точек «о себе» ниже). Держим список в тесте
#: отдельно от кода: расширение прав оператора должно быть осознанным.
OPERATOR_ALLOWED = {
    ("GET", "/api/state"), ("GET", "/api/live"), ("GET", "/api/help"), ("GET", "/api/stats"),
    ("GET", "/api/accounts"), ("GET", "/api/accounts/{account_id}"), ("GET", "/api/accounts/{account_id}/runs"),
    ("POST", "/api/accounts/{account_id}/backup"), ("POST", "/api/accounts/{account_id}/verify"),
    ("POST", "/api/accounts/{account_id}/test"), ("POST", "/api/accounts/{account_id}/folders/diagnose"),
    ("POST", "/api/accounts/check-logins"), ("POST", "/api/accounts/backup-all"),
    ("POST", "/api/accounts/export-list"),
    ("GET", "/api/accounts/bulk/actions"), ("POST", "/api/accounts/bulk"),
    ("GET", "/api/accounts/bulk/history"), ("GET", "/api/accounts/bulk/history/{op_id}"),
    ("POST", "/api/accounts/bulk/history/{op_id}/cancel"),
    ("GET", "/api/jobs"), ("GET", "/api/jobs/{job_id}"), ("GET", "/api/jobs/{job_id}/events"),
    ("POST", "/api/jobs/{job_id}/cancel"), ("POST", "/api/jobs/{job_id}/retry"),
    ("GET", "/api/schedules"), ("GET", "/api/logs"), ("GET", "/api/analytics/system"),
    ("GET", "/api/monitoring/summary"),
}
#: Вход, выход и свой профиль — доступны любому.
SELF_ROUTES = {
    ("GET", "/api/needs-setup"), ("POST", "/api/setup"), ("POST", "/api/login"), ("POST", "/api/login/otp"),
    ("POST", "/api/logout"), ("GET", "/api/me"), ("GET", "/api/me/2fa"), ("POST", "/api/me/2fa/setup"),
    ("POST", "/api/me/2fa/enable"), ("POST", "/api/me/2fa/disable"), ("POST", "/api/me/2fa/recovery"),
}


@pytest.fixture()
def setup(client, monkeypatch):
    svc = client.app.state.services
    svc.queue.stop()      # задания должны оставаться в очереди, а не выполняться
    client.post("/api/setup", json={"username": "adm", "password": PW})
    client.post("/api/login", json={"username": "adm", "password": PW})
    aid = client.post("/api/accounts", json={"name": "Иванов", "host": "h", "port": 993,
                                             "username": "ivanov@x.ru", "password": "p"}).json()["id"]
    r = client.post("/api/users", json={"username": "op", "password": PW, "role": "operator"})
    assert r.status_code == 200, r.text
    op = TestClient(client.app, headers=HDR)
    assert op.post("/api/login", json={"username": "op", "password": PW}).status_code == 200
    assert op.get("/api/me").json()["role"] == "operator"
    return client, op, aid, svc


def _path_for(route):
    return re.sub(r"\{[^}]+\}", "1", route.path)


def _api_routes(routes):
    """Все точки API приложения. Новые FastAPI держат подключённые роутеры
    обёртками (_IncludedRouter) — обходим и их."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif getattr(route, "original_router", None) is not None:
            yield from _api_routes(route.original_router.routes)


def test_operator_denied_everything_not_allowed(setup):
    """Все точки API, кроме разрешённых, отвечают оператору 403."""
    admin, op, _aid, _svc = setup
    routes = [r for r in _api_routes(admin.app.routes) if r.path.startswith("/api")]
    # обход увидел всё, что есть в схеме OpenAPI (страховка от смены устройства роутеров)
    schema = {(m.upper(), path) for path, ops in admin.app.openapi()["paths"].items() for m in ops
              if path.startswith("/api")}
    assert schema <= {(m, r.path) for r in routes for m in r.methods}
    checked = 0
    for route in routes:
        for method in sorted(route.methods - {"HEAD"}):
            key = (method, route.path)
            if key in OPERATOR_ALLOWED or key in SELF_ROUTES:
                continue
            r = op.request(method, _path_for(route), json={})
            assert r.status_code == 403, (key, r.status_code, r.text[:200])
            checked += 1
    assert checked > 60      # письма, выгрузки, настройки, пользователи, удаление…


def test_allowed_list_matches_code():
    """Белый список в auth.py не шире того, что разрешено этим тестом."""
    from mailarchiver.web.auth import OPERATOR_ROUTES
    assert set(OPERATOR_ROUTES) <= OPERATOR_ALLOWED


def test_operator_reads_state_accounts_logs(setup):
    admin, op, aid, svc = setup
    for url in ("/api/state", "/api/live", "/api/accounts", f"/api/accounts/{aid}", f"/api/accounts/{aid}/runs",
                "/api/jobs", "/api/schedules", "/api/stats", "/api/logs", "/api/analytics/system",
                "/api/monitoring/summary", "/api/help", "/api/me/2fa"):
        r = op.get(url)
        assert r.status_code == 200, (url, r.text[:200])
    assert op.post("/api/monitoring/summary").status_code == 403       # отправить сводку — администратор
    r = op.post("/api/accounts/export-list", json={"format": "csv"})
    assert r.status_code == 200 and "ivanov@x.ru" in r.content.decode("utf-8-sig")


def test_operator_starts_backups_and_checks(setup, monkeypatch):
    admin, op, aid, svc = setup
    assert op.post(f"/api/accounts/{aid}/backup").status_code == 200
    assert op.post(f"/api/accounts/{aid}/backup", json={"rebuild": "missing"}).status_code == 200
    r = op.post(f"/api/accounts/{aid}/backup", json={"rebuild": "full"})
    assert r.status_code == 403
    assert op.post(f"/api/accounts/{aid}/verify").status_code == 200
    assert op.post("/api/accounts/check-logins", json={}).status_code == 200
    r = op.post("/api/accounts/backup-all", json={"sequential": True})
    assert r.status_code == 200 and r.json()["job_id"]
    import mailarchiver.web.api as api_mod
    monkeypatch.setattr(api_mod, "probe_account", lambda acc, opts: {"ok": True, "folders": []})
    monkeypatch.setattr(api_mod, "diagnose_folders", lambda *a, **k: {"ok": True, "folders": [], "broken_folders": []})
    assert op.post(f"/api/accounts/{aid}/test").json()["ok"] is True
    assert op.post(f"/api/accounts/{aid}/folders/diagnose").json()["ok"] is True
    rebuilds = [j for j in svc.db.active_jobs() if json.loads(j["params"] or "{}").get("rebuild") == "full"]
    assert not rebuilds
    audit = [r["user"] for r in svc.db.list_audit(200)]
    assert "op" in audit


def test_operator_job_details_and_management(setup):
    admin, op, aid, svc = setup
    plain = svc.db.enqueue_job("backup", aid, {}, created_by="adm")
    full = svc.db.enqueue_job("backup", aid, {"rebuild": "full"}, created_by="adm")
    final = svc.db.enqueue_job("backup", aid, {"final": True}, created_by="system")
    export = svc.db.enqueue_job("export", aid, {"engine": "eml"}, created_by="adm")
    listing = {j["id"]: j for j in op.get("/api/jobs").json()}
    assert listing[plain]["operator_can_manage"] is True
    assert listing[full]["operator_can_manage"] is False
    assert listing[final]["operator_can_manage"] is False
    assert listing[export]["operator_can_manage"] is False
    # отмена — только копирования и проверок
    assert op.post(f"/api/jobs/{full}/cancel").status_code == 403
    assert op.post(f"/api/jobs/{final}/cancel").status_code == 403
    assert op.post(f"/api/jobs/{export}/cancel").status_code == 403
    assert not svc.db.get_job(export)["cancel_requested"]
    assert op.post(f"/api/jobs/{plain}/cancel").status_code == 200
    # подробности выгрузки: без параметров и событий
    svc.db.finish_job(export, "success", {"summary": "Выгружено 3 письма", "path": "/data/exports/x.zip",
                                          "samples": [{"subject": "Секрет"}]})
    svc.db.add_job_event(export, "INFO", "Письмо «Секрет» выгружено")
    d = op.get(f"/api/jobs/{export}").json()
    assert d["restricted"] is True and d["params"] == {}
    assert d["result"] == {"summary": "Выгружено 3 письма"}
    assert op.get(f"/api/jobs/{export}/events").json() == []
    assert admin.get(f"/api/jobs/{export}").json()["result"]["path"] == "/data/exports/x.zip"
    # повтор (пока по ящику стоит другое такое же копирование — не повторяется)
    svc.db.finish_job(plain, "failed", {"summary": "нет связи"}, error="нет связи")
    assert op.post(f"/api/jobs/{plain}/retry").status_code == 400          # в очереди ещё «последняя копия»
    svc.db.finish_job(final, "success", {})
    assert op.post(f"/api/jobs/{plain}/retry").status_code == 200
    svc.db.finish_job(full, "failed", {}, error="x")
    assert op.post(f"/api/jobs/{full}/retry").status_code == 403
    assert op.post(f"/api/jobs/{export}/retry").status_code == 403


def test_operator_bulk_whitelist(setup):
    admin, op, aid, svc = setup
    meta = op.get("/api/accounts/bulk/actions").json()
    keys = {a["key"] for a in meta["actions"]}
    assert keys == set(roles.OPERATOR_BULK_ACTIONS)
    assert "create_defaults" not in meta
    assert all(a["group"] in {g["key"] for g in meta["groups"]} for a in meta["actions"])
    for action in ("delete", "purge", "enable", "disable", "rebuild_full", "export", "restore", "notes",
                   "retention_set", "quarantine_delete", "schedule_set", "set_server"):
        r = op.post("/api/accounts/bulk", json={"action": action, "ids": [aid], "preview": True})
        assert r.status_code == 403, action
    assert op.post("/api/accounts/bulk-create", json={"text": "a@b.ru"}).status_code == 403
    r = op.post("/api/accounts/bulk", json={"action": "backup", "ids": [aid], "preview": False})
    assert r.status_code == 200 and r.json()["counts"]["ok"] == 1
    op_backup = r.json()["op_id"]
    # операция администратора (заметки) оператору не видна
    r = admin.post("/api/accounts/bulk", json={"action": "notes", "ids": [aid], "preview": False,
                                               "params": {"op": "replace", "text": "служебная заметка"}})
    assert r.status_code == 200, r.text
    op_notes = r.json()["op_id"]
    hist = {i["id"] for i in op.get("/api/accounts/bulk/history").json()["items"]}
    assert op_backup in hist and op_notes not in hist
    assert op.get(f"/api/accounts/bulk/history/{op_notes}").status_code == 404
    assert op.post(f"/api/accounts/bulk/history/{op_notes}/cancel").status_code == 404
    assert op.get(f"/api/accounts/bulk/history/{op_backup}").status_code == 200


def test_operator_bulk_cancel_skips_foreign_jobs(setup):
    admin, op, aid, svc = setup
    backup = svc.db.enqueue_job("backup", aid, {}, created_by="adm")
    export = svc.db.enqueue_job("export", aid, {}, created_by="adm")
    r = op.post("/api/accounts/bulk", json={"action": "cancel_jobs", "ids": [aid], "preview": False,
                                            "params": {"which": "all"}})
    assert r.status_code == 200, r.text
    assert svc.db.get_job(backup)["cancel_requested"] or svc.db.get_job(backup)["status"] == "cancelled"
    assert not svc.db.get_job(export)["cancel_requested"]
    assert svc.db.get_job(export)["status"] == "queued"


def test_operator_banners(setup):
    admin, op, _aid, svc = setup
    svc.store.encryption_blocked = "Нет файла ключа /srv/secret/storage.key"
    try:
        assert "/srv/secret" in admin.get("/api/state").json()["encryption_blocked"]
        text = op.get("/api/state").json()["encryption_blocked"]
        assert text and "/srv/secret" not in text
    finally:
        svc.store.encryption_blocked = ""


def test_ws_snapshot_keeps_logs_for_staff():
    from mailarchiver.web.ws import _personal_snapshot
    snap = {"active_jobs": [], "job_counts": {}, "logs": [{"message": "x"}]}
    assert "logs" in _personal_snapshot(snap, {"role": "operator"})
    assert "logs" in _personal_snapshot(snap, {"role": "admin"})
    assert "logs" not in _personal_snapshot(snap, {"role": "mailbox", "account_id": 1})


def test_user_roles_management(setup):
    admin, op, _aid, svc = setup
    users = {u["username"]: u for u in admin.get("/api/users").json()}
    assert users["op"]["role"] == "operator" and users["adm"]["role"] == "admin"
    r = admin.post("/api/users", json={"username": "mb", "password": PW, "role": "mailbox"})
    assert r.status_code == 400
    # оператор ролями не управляет
    assert op.post(f"/api/users/{users['adm']['id']}/role", json={"role": "operator"}).status_code == 403
    # свою роль администратор не меняет — так всегда остаётся хотя бы один администратор
    r = admin.post(f"/api/users/{users['adm']['id']}/role", json={"role": "operator"})
    assert r.status_code == 400
    assert admin.post(f"/api/users/{users['op']['id']}/role", json={"role": "boss"}).status_code == 400
    # повышение: сеансы пользователя завершаются
    r = admin.post(f"/api/users/{users['op']['id']}/role", json={"role": "admin"})
    assert r.status_code == 200 and r.json()["changed"] is True
    assert op.get("/api/me").status_code == 401
    assert svc.db.get_user_by_id(users["op"]["id"])["role"] == "admin"
    assert any(a["action"] == "user_role" for a in admin.get("/api/audit").json())


def test_operator_role_change_applies_immediately(setup):
    """Права берутся из базы при каждом запросе, а не из сеанса."""
    admin, op, aid, svc = setup
    uid = svc.db.get_user_by_name("op")["id"]
    svc.db.set_user_role(uid, "admin")          # без завершения сеансов
    assert op.get("/api/settings").status_code == 200
    svc.db.set_user_role(uid, "operator")
    assert op.get("/api/settings").status_code == 403


def test_require_2fa_applies_to_operator(setup):
    admin, op, _aid, svc = setup
    assert admin.put("/api/settings", json={"values": {"security.require_2fa": True}}).status_code == 200
    r = op.get("/api/accounts")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "2fa_required"
    assert op.get("/api/me").json()["must_enroll_2fa"] is True
    assert op.get("/api/me/2fa").status_code == 200          # включить 2FA оператор может сам


def test_operator_may_manage_rules():
    def row(job_type, params=None):
        return {"type": job_type, "params": json.dumps(params or {})}
    assert roles.operator_may_manage(row("backup"))
    assert roles.operator_may_manage(row("backup", {"rebuild": "missing"}))
    assert not roles.operator_may_manage(row("backup", {"rebuild": "full"}))
    assert not roles.operator_may_manage(row("backup", {"disable_after": True}))
    assert not roles.operator_may_manage(row("backup", {"final": True}))
    for t in ("backup_all", "check_logins", "folders_check", "verify"):
        assert roles.operator_may_manage(row(t))
    for t in ("export", "restore", "import_pst", "retention", "cleanup", "quarantine_check", "quarantine_rescue",
              "analyze", "dedup_report", "storage_convert", "replicate", "search_reindex", "sync_employees"):
        assert not roles.operator_may_manage(row(t)), t


def test_operator_cannot_queue_disabled_or_duplicate_backups(setup):
    admin, op, aid, svc = setup
    assert op.post(f"/api/accounts/{aid}/backup").status_code == 200
    r = op.post(f"/api/accounts/{aid}/backup")
    assert r.status_code == 400 and "уже выполняется или стоит в очереди" in r.json()["message"]
    assert op.post(f"/api/accounts/{aid}/verify").status_code == 200
    assert op.post(f"/api/accounts/{aid}/verify").status_code == 400
    svc.db.execute("UPDATE accounts SET enabled=0 WHERE id=?", (aid,))
    for j in svc.db.active_jobs():
        svc.db.finish_job(j["id"], "failed", {}, error="x")
    r = op.post(f"/api/accounts/{aid}/backup")
    assert r.status_code == 400 and "выключено" in r.json()["message"]
    failed = [j["id"] for j in svc.db.list_jobs(job_type="backup")][0]
    assert op.post(f"/api/jobs/{failed}/retry").status_code == 400
    # администратора эти ограничения не касаются
    assert admin.post(f"/api/accounts/{aid}/backup").status_code == 200


def test_operator_sees_no_error_details_of_restricted_jobs(setup):
    from mailarchiver.web.ws import _personal_snapshot
    admin, op, aid, svc = setup
    export = svc.db.enqueue_job("export", aid, {}, created_by="adm")
    svc.db.update_job_progress(export, 1, 3, "Папка «Личное/Банк»: письмо «Счёт»")
    listing = {j["id"]: j for j in op.get("/api/jobs").json()}
    assert listing[export]["progress_message"] == ""
    live = op.get("/api/live").json()
    assert all(j["progress_message"] == "" for j in live["active_jobs"] if j["id"] == export)
    assert admin.get("/api/jobs").json()[0]["progress_message"].startswith("Папка")
    snap = {"active_jobs": [{"id": 1, "type": "export", "progress_message": "секрет", "error": "x"}]}
    red = _personal_snapshot(snap, {"role": "operator"})["active_jobs"][0]
    assert red["progress_message"] == "" and "секрет" not in red["error"]
    svc.db.finish_job(export, "failed", {"summary": "Не выгружено"}, error="Permission denied: /x/Иванов.zip")
    d = op.get(f"/api/jobs/{export}").json()
    assert "Иванов" not in d["error"] and d["error"]


def test_operator_does_not_see_admin_notes(setup):
    admin, op, aid, svc = setup
    svc.db.execute("UPDATE accounts SET notes=? WHERE id=?", ("пароль у бухгалтерии", aid))
    assert "notes" not in op.get(f"/api/accounts/{aid}").json()
    assert all("notes" not in a for a in op.get("/api/accounts").json())
    assert all("notes" not in a for a in op.get("/api/state").json()["accounts"])
    assert admin.get(f"/api/accounts/{aid}").json()["notes"] == "пароль у бухгалтерии"
    csv_text = op.post("/api/accounts/export-list", json={"format": "csv"}).content.decode("utf-8-sig")
    assert "бухгалтерии" not in csv_text and "Заметка" not in csv_text
    assert "бухгалтерии" in admin.post("/api/accounts/export-list", json={"format": "csv"}).content.decode("utf-8-sig")


def test_last_admin_is_always_kept(setup):
    admin, op, _aid, svc = setup
    adm = svc.db.get_user_by_name("adm")["id"]
    opid = svc.db.get_user_by_name("op")["id"]
    # единственного администратора не понизить, не отключить и не удалить — даже в обход проверки «себя»
    assert svc.db.set_user_role(adm, "operator") is False
    assert svc.db.set_user_disabled(adm, True) is False
    assert svc.db.delete_user(adm) is False
    assert svc.db.get_user_by_id(adm)["role"] == "admin"
    # с другим администратором — можно
    assert svc.db.set_user_role(opid, "admin") is True
    assert svc.db.set_user_role(adm, "operator") is True
    assert svc.db.set_user_role(opid, "operator") is False       # теперь он последний
    assert svc.db.set_user_disabled(opid, True) is False
    assert svc.db.set_user_role(adm, "admin") is True


def test_api_default_role_is_operator(setup):
    admin, _op, _aid, svc = setup
    assert admin.post("/api/users", json={"username": "noroles", "password": PW}).status_code == 200
    assert svc.db.get_user_by_name("noroles")["role"] == "operator"


def test_unknown_role_gets_nothing(setup):
    admin, op, aid, svc = setup
    svc.db.execute("UPDATE users SET role='viewer' WHERE username='op'")
    assert op.get("/api/accounts").status_code == 403
    assert op.get(f"/api/accounts/{aid}/messages").status_code == 403
    assert op.get("/api/settings").status_code == 403


def test_summary_preview_for_operator_has_no_security_line(setup, monkeypatch):
    admin, op, _aid, svc = setup
    svc.db.record_login_attempt("someone", False, "10.0.0.1")
    assert "Безопасность за неделю" in admin.get("/api/monitoring/summary").json()["body"]
    assert "Безопасность за неделю" not in op.get("/api/monitoring/summary").json()["body"]
