"""Общие фикстуры для тестов MailArchiver."""
import pytest


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "madata"
    d.mkdir()
    monkeypatch.setenv("MAILARCHIVER_DATA", str(d))
    monkeypatch.delenv("MAILARCHIVER_CONFIG", raising=False)
    return str(d)


@pytest.fixture()
def cfg(data_dir):
    from mailarchiver.config import load_config
    return load_config()


@pytest.fixture()
def services(cfg):
    from mailarchiver.service import Services
    svc = Services(cfg)
    svc.setup()
    # НЕ запускаем очередь/планировщик — тестируем компоненты изолированно
    return svc


#: Заголовок, который интерфейс шлёт на каждый запрос к API (см. web/proxy.py:
#: запросы, меняющие состояние, без Origin/Referer принимаются только с ним).
API_HEADERS = {"X-Requested-With": "fetch"}


@pytest.fixture()
def client(data_dir):
    """TestClient с полным жизненным циклом (очередь и планировщик стартуют)."""
    from fastapi.testclient import TestClient
    from mailarchiver.web.app import create_app
    app = create_app()
    with TestClient(app, headers=API_HEADERS) as c:
        yield c
