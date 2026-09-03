"""Общие фикстуры для тестов MailArchiver."""
import os
import tempfile

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


@pytest.fixture()
def client(data_dir):
    """TestClient с полным жизненным циклом (очередь и планировщик стартуют)."""
    from fastapi.testclient import TestClient
    from mailarchiver.web.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c
