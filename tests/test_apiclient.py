"""The shared HTTP client (triage/apiclient.py), used by both GUIs.

Hermetic: the one outside call is ``urllib.request.urlopen``, faked here, so the
request building, envelope decode, and error-message mapping are all exercised
without a server, streamlit, or Qt.
"""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from typing import Any

import pytest

from triage import apiclient


class _FakeResponse:
    """Minimal stand-in for urlopen's return: a context manager you can read."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, *_: Any) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: Any) -> None:
        return None


def test_post_triage_sends_alert_and_returns_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float = 0) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["method"] = request.method
        return _FakeResponse(b'{"verdict": {}, "retrieved": []}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = apiclient.post_triage("http://host:8000", "many failed logins", 7)

    assert result == {"verdict": {}, "retrieved": []}
    assert captured["url"] == "http://host:8000/triage"
    assert captured["method"] == "POST"
    assert captured["body"] == {"alert": "many failed logins", "top_k": 7}


def test_error_message_reports_http_status_and_detail() -> None:
    exc = urllib.error.HTTPError(
        url="http://host:8000/triage",
        code=502,
        msg="Bad Gateway",
        hdrs=Message(),
        fp=io.BytesIO(b'{"detail": "Anthropic API failure: boom"}'),
    )
    message = apiclient.error_message(exc, "http://host:8000")
    assert "502" in message
    assert "boom" in message


def test_error_message_reports_unreachable_api() -> None:
    exc = urllib.error.URLError("Connection refused")
    message = apiclient.error_message(exc, "http://host:8000")
    assert "Could not reach the API at http://host:8000" in message
    assert "Connection refused" in message


def test_http_error_detail_falls_back_on_non_json_body() -> None:
    exc = urllib.error.HTTPError(
        url="http://host:8000/triage",
        code=500,
        msg="Internal Server Error",
        hdrs=Message(),
        fp=io.BytesIO(b"<html>not json</html>"),
    )
    # No JSON detail to extract → the message still names the status, not a crash.
    message = apiclient.error_message(exc, "http://host:8000")
    assert "500" in message
