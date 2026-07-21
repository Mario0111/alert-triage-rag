"""The Streamlit UI (triage/ui.py) driven through Streamlit's AppTest harness.

Hermetic like the rest of the suite: no network, no API, no model download. The
UI's only outside contact is one ``urllib.request.urlopen`` call, so faking that
one function exercises the whole page — submit, render, and both failure paths —
without a server. ``AppTest`` runs the real script in-process and re-runs it on
each simulated interaction, exactly as a browser would.

streamlit is the optional [ui] extra, so a plain [dev] checkout does not have
it; ``importorskip`` skips this module there instead of erroring at collection.
CI installs [dev,ui], so it runs for real there.
"""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

UI_SCRIPT = str(Path(__file__).resolve().parents[1] / "triage" / "ui.py")


def _envelope() -> dict[str, Any]:
    """A valid TriageResponse envelope: verdict + two retrieved sources.

    One cited ATT&CK technique, one backfilled (uncited) runbook — so a single
    fixture exercises the cited/uncited and backfilled markings in the panels.
    """
    return {
        "verdict": {
            "verdict": "true_positive",
            "severity": "high",
            "confidence": 0.9,
            "summary": "Credential brute force followed by a successful login.",
            "mitre_techniques": ["T1110"],
            "recommended_actions": ["Isolate the host"],
            "citations": [
                {
                    "chunk_id": "T1110",
                    "source_type": "attack",
                    "ref": "T1110",
                    "quote": "Adversaries may use brute force.",
                }
            ],
        },
        "retrieved": [
            {
                "id": "T1110",
                "source_type": "attack",
                "name": "Brute Force",
                "backfilled": False,
                "score": 0.12,
                "text": "Brute Force (T1110)\n\nAdversaries may guess passwords.",
            },
            {
                "id": "rb-brute-force.md",
                "source_type": "runbook",
                "name": "rb-brute-force.md",
                "backfilled": True,
                "score": 0.71,
                "text": "Runbook: Brute Force\n\nCheck the auth logs.",
            },
        ],
    }


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


def _submit(at: AppTest, alert: str = "many failed ssh logins") -> AppTest:
    """Type an alert and click Triage, returning the run app for assertions."""
    at.text_area[0].set_value(alert)
    at.button[0].click().run()
    return at


def test_ui_renders_verdict_and_source_panels(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(_envelope()).encode()
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
    )

    at = AppTest.from_file(UI_SCRIPT).run()
    _submit(at)

    assert not at.exception
    # The verdict block: disposition + severity rendered as metrics.
    metric_values = [m.value for m in at.metric]
    assert any("True positive" in v for v in metric_values)
    assert any(v == "High" for v in metric_values)
    # The summary and the retrieved-sources header render top-level.
    page_markdown = " ".join(m.value for m in at.markdown)
    assert "Credential brute force" in page_markdown
    assert "Retrieved sources" in page_markdown


def test_ui_shows_api_error_detail_on_502(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_502(*_: Any, **__: Any) -> None:
        raise urllib.error.HTTPError(
            url="http://api:8000/triage",
            code=502,
            msg="Bad Gateway",
            hdrs=Message(),
            fp=io.BytesIO(b'{"detail": "Anthropic API failure: boom"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", _raise_502)

    at = AppTest.from_file(UI_SCRIPT).run()
    _submit(at)

    assert not at.exception
    errors = " ".join(e.value for e in at.error)
    assert "502" in errors
    assert "boom" in errors


def test_ui_reports_unreachable_api(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*_: Any, **__: Any) -> None:
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)

    at = AppTest.from_file(UI_SCRIPT).run()
    _submit(at)

    assert not at.exception
    errors = " ".join(e.value for e in at.error)
    assert "Could not reach the API" in errors


def test_ui_warns_on_empty_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    # No HTTP call should happen; make urlopen explode so the test fails loudly
    # if the empty-alert guard ever regresses and lets a request through.
    def _boom(*_: Any, **__: Any) -> None:
        raise AssertionError("urlopen must not be called for an empty alert")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    at = AppTest.from_file(UI_SCRIPT).run()
    at.button[0].click().run()

    assert not at.exception
    assert any("Enter an alert" in w.value for w in at.warning)
