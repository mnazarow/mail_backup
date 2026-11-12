"""1.22.0: файлы интерфейса по порядку, состояние проверки восстановления."""
import os
import re

from mailarchiver.models import JobType
from mailarchiver.web.assets import JS_FILES, STATIC_DIR, bundle_text


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})


def test_interface_files_in_order(client):
    """Страница подключает все файлы интерфейса по порядку; запуск — только в конце последнего."""
    html = client.get("/").text
    srcs = re.findall(r'<script src="/static/js/([\w.-]+)\?v=', html)
    assert srcs == ["theme.js", *JS_FILES]
    for name in JS_FILES:
        text = open(os.path.join(STATIC_DIR, "js", name), encoding="utf-8").read()
        assert '"use strict";' in text[:1200], name
        assert client.get(f"/static/js/{name}").status_code == 200
    assert re.findall(r"^boot\(\);", bundle_text(), re.M) == ["boot();"]
    assert bundle_text().rstrip().endswith("boot();")
    # отпечаток ?v= зависит от всех файлов (иначе браузер держал бы старые части интерфейса)
    assert len(set(re.findall(r"\?v=(\w+)", html))) == 1


def test_drill_status_endpoint(client):
    _login(client)
    svc = client.app.state.services
    assert client.get("/api/drill").json() == {"last": None, "active_job": None}
    job = svc.db.enqueue_job(JobType.RESTORE_DRILL, None, {}, 6, 1, "t")
    svc.db.finish_job(job, "success", {"summary": "Проверено писем: 5 в 1 ящиках; повреждённых или нечитаемых: 0."})
    last = client.get("/api/drill").json()["last"]
    assert last["id"] == job and last["status"] == "success" and last["summary"].startswith("Проверено писем: 5")
