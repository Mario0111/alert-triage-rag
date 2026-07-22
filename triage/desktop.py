"""Native desktop app for triage — a Qt (PySide6) THIN CLIENT of the HTTP API.

Like the Streamlit UI, this imports NO part of the pipeline (CLAUDE.md's
single-integration-surface rule): it POSTs to ``/triage`` via `apiclient` and
renders the JSON envelope. Unlike the Streamlit UI it is a real native-window
application — its own window, native widgets, no browser — talking to a backend
you run separately (`triage serve` or `docker compose up`).

Qt concepts this file leans on (teaching notes):

- **The event loop and the one GUI thread.** ``QApplication.exec()`` runs an
  event loop on the main thread; every widget update must happen on that thread.
  A triage call blocks for SECONDS (local embedding + a Claude round-trip), so
  doing it on the GUI thread would FREEZE the window (no repaint, "not
  responding"). The request therefore runs on a worker ``QThread``
  (`_TriageWorker`), and the result comes back via a **signal**.

- **Signals and slots across threads.** Qt objects communicate by signals
  (events a widget emits) connected to slots (handlers). A signal emitted from
  the worker thread and connected to a main-thread slot is delivered as a
  QUEUED event on the GUI thread — Qt marshals it across the thread boundary for
  us, so `_on_success`/`_on_failure` safely touch widgets even though the work
  ran elsewhere. This is the thread-safe alternative to touching widgets from
  the worker directly (which crashes).

- **Widgets vs layouts.** Widgets (`QPushButton`, `QLineEdit`) are the controls;
  layouts (`QVBoxLayout`) arrange them and handle resizing. The citation panels
  are a small custom `_Collapsible` (a `QToolButton` toggling a content frame),
  the native equivalent of the Streamlit expanders.
"""

from __future__ import annotations

import sys
import urllib.error
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import apiclient

_VERDICT_LABELS = {
    "true_positive": "🔴 True positive",
    "false_positive": "🟢 False positive",
    "benign": "🟢 Benign",
    "needs_investigation": "🟡 Needs investigation",
}
_SEVERITY_LABELS = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "critical": "Critical",
}


class _TriageWorker(QThread):
    """Run one triage request off the GUI thread; report back via signals.

    ``succeeded`` carries the response envelope; ``failed`` carries a ready-made
    analyst-facing message. Exactly one fires per run.
    """

    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, api_url: str, alert: str, top_k: int) -> None:
        super().__init__()
        self._api_url = api_url
        self._alert = alert
        self._top_k = top_k

    def run(self) -> None:
        try:
            envelope = apiclient.post_triage(self._api_url, self._alert, self._top_k)
        except urllib.error.URLError as exc:
            # HTTPError (422/502) and connection failures both land here; the
            # shared client turns either into one message.
            self.failed.emit(apiclient.error_message(exc, self._api_url))
        except Exception as exc:  # defensive: a worker thread must never die silently
            self.failed.emit(f"Unexpected error: {exc}")
        else:
            self.succeeded.emit(envelope)


class _Collapsible(QWidget):
    """A section with a clickable header that shows/hides its content.

    Qt ships no collapsible box, so this is the small standard pattern: a
    checkable ``QToolButton`` whose toggle flips an arrow and its content
    widget's visibility. It is the native counterpart of a Streamlit expander.
    """

    def __init__(self, title: str, expanded: bool = False) -> None:
        super().__init__()
        self._button = QToolButton()
        self._button.setText(title)
        self._button.setCheckable(True)
        self._button.setChecked(expanded)
        self._button.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self._button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._button.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._button.toggled.connect(self._on_toggled)

        self._content = QWidget()
        self._content.setVisible(expanded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._button)
        layout.addWidget(self._content)

    def _on_toggled(self, checked: bool) -> None:
        self._button.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self._content.setVisible(checked)

    def set_content(self, widget: QWidget) -> None:
        inner = QVBoxLayout(self._content)
        inner.setContentsMargins(24, 4, 4, 8)
        inner.addWidget(widget)


def _wrapped_label(text: str, *, selectable: bool = False) -> QLabel:
    """A word-wrapped QLabel (optionally text-selectable), the workhorse output."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class TriageWindow(QMainWindow):
    """The application's main window: inputs on top, results below."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Alert Triage RAG")
        self._worker: _TriageWorker | None = None

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        title = QLabel("🛡️ Alert Triage RAG")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        outer.addWidget(title)
        outer.addWidget(
            _wrapped_label(
                "Describe a SOC alert; get a grounded verdict with citations "
                "back to MITRE ATT&CK techniques and internal runbooks."
            )
        )

        outer.addWidget(QLabel("API endpoint"))
        self._api_edit = QLineEdit(apiclient.api_base_url())
        outer.addWidget(self._api_edit)

        outer.addWidget(QLabel("Alert description"))
        self._alert_edit = QPlainTextEdit()
        self._alert_edit.setPlaceholderText(
            "e.g. Multiple failed SSH logins from a single external IP, followed "
            "by one success and an outbound connection to an unknown host."
        )
        self._alert_edit.setMinimumHeight(120)
        outer.addWidget(self._alert_edit)

        self._top_k = QSpinBox()
        self._top_k.setRange(1, 15)
        self._top_k.setValue(5)
        self._top_k.setPrefix("Sources to retrieve (top-k): ")
        outer.addWidget(self._top_k)

        self._submit = QPushButton("Triage alert")
        self._submit.clicked.connect(self._on_submit)
        outer.addWidget(self._submit)

        self._status = _wrapped_label("")
        self._status.setStyleSheet("color: #b00020;")
        outer.addWidget(self._status)

        # Scrollable results: an ATT&CK technique's detection text is long.
        self._results = QWidget()
        self._results_layout = QVBoxLayout(self._results)
        self._results_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._results)
        outer.addWidget(scroll, stretch=1)

    # --- actions -------------------------------------------------------------

    def _on_submit(self) -> None:
        alert = self._alert_edit.toPlainText().strip()
        if not alert:
            self._show_error("Enter an alert description first.")
            return
        # Don't launch a second request while one is in flight.
        if self._worker is not None and self._worker.isRunning():
            return

        self._clear_results()
        self._status.setText("")
        self._submit.setEnabled(False)
        self._submit.setText("Triaging…")

        worker = _TriageWorker(
            self._api_edit.text().strip().rstrip("/"),
            alert,
            self._top_k.value(),
        )
        worker.succeeded.connect(self._on_success)
        worker.failed.connect(self._on_failure)
        # Keep a reference: a GC'd QThread would be destroyed mid-run.
        self._worker = worker
        worker.start()

    def _on_success(self, envelope: dict[str, Any]) -> None:
        self._reset_submit()
        self._render_result(envelope)

    def _on_failure(self, message: str) -> None:
        self._reset_submit()
        self._show_error(message)

    def _reset_submit(self) -> None:
        self._submit.setEnabled(True)
        self._submit.setText("Triage alert")

    def _show_error(self, message: str) -> None:
        self._clear_results()
        self._status.setText(message)

    # --- rendering -----------------------------------------------------------

    def _clear_results(self) -> None:
        while self._results_layout.count():
            item = self._results_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _render_result(self, envelope: dict[str, Any]) -> None:
        self._clear_results()
        verdict = envelope.get("verdict", {})
        self._render_verdict(verdict)
        self._render_sources(verdict, envelope.get("retrieved", []))

    def _render_verdict(self, verdict: dict[str, Any]) -> None:
        disposition = verdict.get("verdict", "")
        severity = _SEVERITY_LABELS.get(verdict.get("severity", ""), "")
        confidence = verdict.get("confidence", 0.0)
        header = QLabel(
            f"{_VERDICT_LABELS.get(disposition, disposition)}    "
            f"Severity: {severity}    Confidence: {confidence:.0%}"
        )
        header.setStyleSheet("font-size: 15px; font-weight: bold;")
        self._results_layout.addWidget(header)

        self._results_layout.addWidget(
            _wrapped_label(verdict.get("summary", ""), selectable=True)
        )

        techniques = verdict.get("mitre_techniques") or []
        if techniques:
            self._results_layout.addWidget(
                _wrapped_label("MITRE ATT&CK techniques: " + ", ".join(techniques))
            )

        actions = verdict.get("recommended_actions") or []
        if actions:
            bullets = "\n".join(f"•  {a}" for a in actions)
            self._results_layout.addWidget(
                _wrapped_label("Recommended actions:\n" + bullets)
            )

    def _render_sources(
        self, verdict: dict[str, Any], retrieved: list[dict[str, Any]]
    ) -> None:
        quotes_by_id = {
            c["chunk_id"]: c.get("quote") for c in verdict.get("citations", [])
        }

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        self._results_layout.addWidget(divider)
        self._results_layout.addWidget(
            _wrapped_label(f"Retrieved sources ({len(retrieved)})")
        )

        for source in retrieved:
            source_id = source["id"]
            cited = source_id in quotes_by_id
            backfilled = source.get("backfilled", False)

            marks = ["✅ cited" if cited else "not cited"]
            if backfilled:
                marks.append("⚠️ backfilled")
            title = (
                f"{source['source_type']} · {source['name']} "
                f"({source_id}) — {', '.join(marks)}"
            )

            body = QWidget()
            body_layout = QVBoxLayout(body)
            body_layout.setContentsMargins(0, 0, 0, 0)
            if backfilled:
                note = _wrapped_label(
                    "Backfilled: this runbook did not match the alert by "
                    "similarity — it was appended so a triage procedure is "
                    "always on hand. Judge its relevance rather than assume it."
                )
                note.setStyleSheet("color: #8a6d00;")
                body_layout.addWidget(note)
            quote = quotes_by_id.get(source_id)
            if quote:
                q = _wrapped_label(
                    f"Quoted by the verdict:\n“{quote}”", selectable=True
                )
                q.setStyleSheet("font-style: italic;")
                body_layout.addWidget(q)
            body_layout.addWidget(_wrapped_label(source["text"], selectable=True))

            section = _Collapsible(title, expanded=cited)
            section.set_content(body)
            self._results_layout.addWidget(section)


def main() -> int:
    """Create the QApplication, show the window, and run the event loop."""
    app = QApplication.instance() or QApplication(sys.argv)
    window = TriageWindow()
    window.resize(760, 820)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
