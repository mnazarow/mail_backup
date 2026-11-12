"""Разбор кода 11.11.2026: что было найдено и исправлено.

* вход: неверный пароль к несуществующему логину отвечает так же долго, как к существующему;
* ответ 500 — с защитными заголовками; /health не ходит в базу из цикла событий;
* аудит: постранично, с поиском, без «limit=-1 — вся таблица»;
* сотрудник: залп одновременных «скопировать» даёт одно задание; «Проверить» — не чаще предела;
* удаление ящика — пачками, без многосекундной блокировки базы;
* номер версии в файлах Docker сверяется при сборке выпуска;
* install.sh берёт Python 3.10+ рядом со старым системным python3.
"""
import os
import shutil
import subprocess
import sys

import pytest

from mailarchiver import models
from mailarchiver.errors import ValidationError
from mailarchiver.models import JobType

PW = "СложныйПароль1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def admin(client):
    assert client.post("/api/setup", json={"username": "adm", "password": PW}).status_code == 200
    assert client.post("/api/login", json={"username": "adm", "password": PW}).status_code == 200
    return client


def test_unknown_login_costs_a_password_check(client, monkeypatch):
    from mailarchiver.web import auth
    assert client.post("/api/setup", json={"username": "adm", "password": PW}).status_code == 200
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda pw, stored: calls.append(stored) or real(pw, stored))
    client.cookies.clear()
    assert client.post("/api/login", json={"username": "nobody", "password": "x"}).status_code in (400, 401)
    assert calls and calls[0].startswith("pbkdf2$")                      # проверка «вхолостую» всё равно была
    calls.clear()
    assert client.post("/api/login", json={"username": "adm", "password": "x"}).status_code in (400, 401)
    assert len(calls) == 1                                                # для существующего — одна, настоящая


def test_server_error_has_security_headers(client):
    from fastapi.testclient import TestClient

    def boom():
        raise RuntimeError("сбой")
    client.app.add_api_route("/api/test-boom", boom)
    raw = TestClient(client.app, raise_server_exceptions=False, headers={"X-Requested-With": "fetch"})
    r = raw.get("/api/test-boom")
    assert r.status_code == 500 and r.json()["code"] == "internal"
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_health_reads_session_without_extending_it(admin):
    svc = admin.app.state.services
    before = {r["token"]: r["last_seen"] for r in svc.db.query("SELECT token, last_seen FROM sessions")}
    data = admin.get("/health").json()
    assert data["status"] == "ok" and data["version"]
    after = {r["token"]: r["last_seen"] for r in svc.db.query("SELECT token, last_seen FROM sessions")}
    assert before == after


def test_audit_is_paged_and_searchable(admin):
    svc = admin.app.state.services
    for i in range(30):
        svc.db.add_audit("adm", "account_delete" if i % 3 == 0 else "settings", f"ящик №{i}")
    page = admin.get("/api/audit?limit=10").json()
    assert len(page) == 10 and page[0]["id"] > page[-1]["id"]
    nxt = admin.get(f"/api/audit?limit=10&before_id={page[-1]['id']}").json()
    assert len(nxt) == 10 and nxt[0]["id"] < page[-1]["id"]
    found = admin.get("/api/audit?q=account_delete&limit=1000").json()
    assert len(found) == 10 and {a["action"] for a in found} == {"account_delete"}
    assert admin.get("/api/audit?q=№1_").json() == []                     # «_» — обычный знак, не шаблон
    assert len(admin.get("/api/audit?limit=-1").json()) == 1              # не «вся таблица»


def _mailbox_user(svc, aid):
    from mailarchiver.web.auth import mailbox_user
    return mailbox_user(svc, svc.db.get_account(aid))


def test_burst_of_mailbox_requests_gives_one_job(services):
    from mailarchiver.web import api
    svc = services
    aid = svc.db.create_account(models.Account(name="Иванов", host="h", username="i@x.ru", password="p"))
    user = _mailbox_user(svc, aid)
    api._mailbox_flood_guard(svc, user, JobType.BACKUP, aid)            # первый запрос разрешён…
    with pytest.raises(ValidationError):                                 # …второй — пока первый не поставил задание
        api._mailbox_flood_guard(svc, user, JobType.BACKUP, aid)
    api._mailbox_flood_guard(svc, user, JobType.EXPORT, aid)            # другое задание — можно
    jid = svc.db.enqueue_job(JobType.BACKUP, aid, {}, 5, 1, "i@x.ru")
    with pytest.raises(ValidationError):                                 # теперь мешает уже само задание
        api._mailbox_flood_guard(svc, user, JobType.BACKUP, aid)
    svc.db.finish_job(jid, "success")
    api._mailbox_flood_guard(svc, user, JobType.BACKUP, aid)            # закончилось — снова можно


def test_mailbox_probe_limit(services):
    from mailarchiver.web import api
    svc = services
    aid = svc.db.create_account(models.Account(name="Иванов", host="h", username="i@x.ru", password="p"))
    user = _mailbox_user(svc, aid)
    with api._mailbox_probe_guard(svc, user, aid):
        with pytest.raises(ValidationError, match="уже идёт"):           # одна проверка за раз
            with api._mailbox_probe_guard(svc, user, aid):
                pass
    for _ in range(api.MAILBOX_PROBES_PER_WINDOW - 1):
        with api._mailbox_probe_guard(svc, user, aid):
            pass
    with pytest.raises(ValidationError, match="раз за последние"):
        with api._mailbox_probe_guard(svc, user, aid):
            pass
    staff = {"role": "admin", "username": "adm"}
    for _ in range(10):                                                  # администратора предел не касается
        with api._mailbox_probe_guard(svc, staff, aid):
            pass


def test_account_is_deleted_in_batches(services, monkeypatch):
    db = services.db
    a = db.create_account(models.Account(name="A", host="h", username="a@x.ru", password="p"))
    b = db.create_account(models.Account(name="B", host="h", username="b@x.ru", password="p"))
    for aid, n in ((a, 11), (b, 2)):
        for i in range(n):
            db.execute("INSERT INTO messages(account_id, folder, uid, uidvalidity, stored_path, size) "
                       "VALUES(?,?,?,?,?,?)", (aid, "INBOX", i + 1, 1, f"p{aid}_{i}", 10))
    statements = []
    real = db.execute

    def spy(sql, params=()):
        statements.append(sql.split()[0] + " " + sql.split()[2])
        return real(sql, params)
    monkeypatch.setattr(db, "DELETE_BATCH", 4)
    monkeypatch.setattr(db, "execute", spy)
    db.delete_account(a)
    assert statements.count("DELETE messages") == 3 and statements[-1] == "DELETE accounts"
    assert db.count_messages(a) == 0 and db.get_account(a) is None
    assert db.count_messages(b) == 2 and db.message_totals_mismatch() == []


def test_release_checks_docker_version(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_release", os.path.join(ROOT, "scripts", "make_release.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "docker").mkdir()
    (tmp_path / "VERSION").write_text("2.0.0\n")
    (tmp_path / "docker" / "Dockerfile").write_text('LABEL org.opencontainers.image.version="1.9.9"\n')
    (tmp_path / "docker" / "docker-compose.yml").write_text("services:\n  app:\n    image: mailarchiver:2.0.0\n")
    mod.ROOT = str(tmp_path)
    assert mod._versions() == {"VERSION": "2.0.0", "docker/Dockerfile": "1.9.9",
                               "docker/docker-compose.yml": "2.0.0"}


@pytest.mark.skipif(sys.platform == "win32", reason="bash")
def test_install_picks_newer_python_next_to_old_system_one(tmp_path):
    """RHEL 8/9, Debian 11: python3 — 3.6–3.9, рядом стоит python3.12 — его и берём; MA_PYTHON — без подбора."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "python3").write_text("#!/bin/sh\nexit 1\n")                # «старый»: проверка версии не проходит
    (fake / "python3.12").write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
    for f in fake.iterdir():
        f.chmod(0o755)
    func = subprocess.run(["sed", "-n", "/^pick_python(){/,/^}/p", os.path.join(ROOT, "scripts", "install.sh")],
                          capture_output=True, text=True, check=True).stdout
    assert func.strip()
    script = func + '\npick_python; echo "$PYTHON"\n'
    env = {"PATH": str(fake)}                                            # только подставные интерпретаторы
    bash = shutil.which("bash")
    out = subprocess.run([bash, "-c", script], capture_output=True, text=True, env=env, check=True).stdout
    assert out.strip() == "python3.12"
    env["MA_PYTHON"] = "/opt/py/bin/python3"
    out = subprocess.run([bash, "-c", script], capture_output=True, text=True, env=env, check=True).stdout
    assert out.strip() == "/opt/py/bin/python3"


def test_help_page_can_be_framed_only_by_the_app(client):
    """Раздел «Документация» показывает справку во фрейме: ей можно встраиваться в сам MailArchiver (тот же
    источник), но не в чужие сайты; остальные страницы встраивать нельзя вовсе."""
    r = client.get("/static/docs/index.html")
    assert r.status_code == 200
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert r.headers["content-security-policy"] == "frame-ancestors 'self'"
    root = client.get("/")
    assert root.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in root.headers["content-security-policy"]
    assert client.get("/static/js/app.js").headers["x-frame-options"] == "DENY"


def test_documentation_section_in_menu():
    """Пункт «Документация» в меню — всем ролям; у каждого раздела есть глава справки, и она есть в справке."""
    import re
    from mailarchiver.web.assets import bundle_text
    js = bundle_text()
    assert "{id:'docs', icon:'📖', title:'Документация', roles:'aom'}" in js and "docs:viewDocs" in js
    anchors = dict(re.findall(r"(\w+):'([a-z]+)'", js[js.index("const DOC_ANCHORS="):js.index("function docsHref")]))
    views = set(re.findall(r"\{id:'(\w+)'", js[js.index("const NAV = ["):js.index("const ROLE_LABELS")])) - {"docs"}
    assert views <= set(anchors)
    html = open(os.path.join(ROOT, "mailarchiver", "web", "static", "docs", "index.html"), encoding="utf-8").read()
    missing = [a for a in set(anchors.values()) if f'id="{a}"' not in html]
    assert not missing, missing
