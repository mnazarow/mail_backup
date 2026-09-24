"""Сервер токенов OAuth2: новый refresh-токен сохраняется, сбои делятся на временные и настоящие."""
import io
import json
import urllib.error

import pytest

from mailarchiver.errors import ImapAuthError, ImapConnectionError
from mailarchiver.imap import oauth


class _Resp:
    def __init__(self, body: bytes, ctype: str = "application/json"):
        self._body = body
        self.headers = {"Content-Type": ctype}

    def read(self, *_a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch(monkeypatch, fn):
    monkeypatch.setattr(oauth.urllib.request, "urlopen", fn)


def test_new_refresh_token_is_returned(monkeypatch):
    _patch(monkeypatch, lambda req, timeout=0: _Resp(json.dumps(
        {"access_token": "AT", "expires_in": 1800, "refresh_token": "NEW"}).encode()))
    assert oauth.refresh_access_token("https://t/token", "c", "s", "OLD") == ("AT", 1800)
    assert oauth.refresh_access_token("https://t/token", "c", "s", "OLD",
                                      with_refresh=True) == ("AT", 1800, "NEW")


def test_server_errors_are_retryable_and_config_errors_are_not(monkeypatch):
    def http_error(code):
        def _raise(req, timeout=0):
            raise urllib.error.HTTPError("https://t/token", code, "x", {},
                                         io.BytesIO(b'{"error":"invalid_grant","refresh_token":"SECRET"}'))
        return _raise

    _patch(monkeypatch, http_error(503))
    with pytest.raises(ImapConnectionError) as info:
        oauth.refresh_access_token("https://t/token", "c", "s", "r")
    assert info.value.retryable and "503" in info.value.message

    _patch(monkeypatch, http_error(400))
    with pytest.raises(ImapAuthError) as info:
        oauth.refresh_access_token("https://t/token", "c", "s", "r")
    assert "invalid_grant" in info.value.hint and "SECRET" not in info.value.hint

    def timeout(req, timeout=0):
        raise urllib.error.URLError(TimeoutError("timed out"))
    _patch(monkeypatch, timeout)
    with pytest.raises(ImapConnectionError):
        oauth.refresh_access_token("https://t/token", "c", "s", "r")


def test_html_instead_of_json_is_explained(monkeypatch):
    _patch(monkeypatch, lambda req, timeout=0: _Resp(b"<html>proxy login</html>", "text/html"))
    with pytest.raises(ImapAuthError) as info:
        oauth.refresh_access_token("https://t/token", "c", "s", "r")
    assert "HTML" in info.value.message and "oauth_token_url" in info.value.hint


def test_http_token_url_is_refused():
    with pytest.raises(ImapAuthError):
        oauth.refresh_access_token("http://t/token", "c", "s", "r")
