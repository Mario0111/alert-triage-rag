"""The native Qt desktop app (triage/desktop.py), rendered headless.

Hermetic and GUI-free-of-network: PySide6's ``offscreen`` platform builds real
widgets with no display, and the render methods are called directly with a
faked envelope — no event loop, no worker thread, no HTTP. PySide6 is the
optional [desktop] extra, so ``importorskip`` skips this module where it isn't
installed (e.g. a plain [dev] checkout); the shared HTTP logic is covered
without Qt in test_apiclient.py.
"""

from __future__ import annotations

import os
from typing import Any, cast

import pytest

pytest.importorskip("PySide6")
# Must be set before the first QApplication is created: render into a virtual
# screen so the test needs no display (works in CI and headless shells).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QToolButton,
)

from triage import desktop  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return cast(QApplication, QApplication.instance() or QApplication([]))


def _envelope() -> dict[str, Any]:
    """A valid envelope: one cited technique, one backfilled (uncited) runbook."""
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


def _all_text(window: desktop.TriageWindow) -> str:
    """Concatenate every label and collapsible-header text in the window."""
    labels = [w.text() for w in window.findChildren(QLabel)]
    buttons = [b.text() for b in window.findChildren(QToolButton)]
    return "\n".join(labels + buttons)


def test_window_renders_verdict_and_source_panels(qapp: QApplication) -> None:
    window = desktop.TriageWindow()
    window._render_result(_envelope())

    text = _all_text(window)
    assert "True positive" in text
    assert "Severity: High" in text
    assert "Credential brute force" in text
    # Both retrieved sources appear as collapsible section headers, with the
    # backfilled runbook marked as such.
    assert "Brute Force (T1110)" in text
    assert "rb-brute-force.md" in text
    assert "backfilled" in text
    # Full source text is rendered (proving the panel shows grounding, not just
    # the model's short quote).
    assert any(
        "Adversaries may guess passwords." in w.text()
        for w in window.findChildren(QLabel)
    )


def test_empty_alert_shows_error_without_requesting(qapp: QApplication) -> None:
    window = desktop.TriageWindow()
    # No worker should be created for an empty alert.
    window._on_submit()
    assert window._worker is None
    assert "Enter an alert description" in window._status.text()


def test_error_is_shown_in_status(qapp: QApplication) -> None:
    window = desktop.TriageWindow()
    window._on_failure("API returned 502: boom")
    assert "502" in window._status.text()
    # A failure re-enables the submit button (not left stuck on "Triaging…").
    assert window._submit.isEnabled()
    assert window._submit.text() == "Triage alert"
