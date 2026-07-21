"""Streamlit UI for triage — a THIN CLIENT of the HTTP API, nothing more.

Per CLAUDE.md's Stage 2 rule the FastAPI service is the single integration
surface: this module imports NO part of the triage pipeline (no embedder, no
Chroma, no Anthropic client, no `triage.query`). It only builds an alert
request, POSTs it to ``/triage`` over HTTP, and renders the JSON envelope that
comes back. Run `triage serve` (or `docker compose up`) separately; this is a
different process that talks to it.

Streamlit concepts this file leans on (teaching notes — the author is
interviewed on every one):

- **Top-to-bottom rerun on every interaction.** Streamlit has no callbacks-and-
  widgets-persist model like a normal GUI. Instead it RE-RUNS THIS ENTIRE
  SCRIPT, top to bottom, every time the user touches any widget (types, clicks
  a button, opens an expander). Each widget call (`st.text_area`, `st.button`)
  both draws the widget AND returns its current value for this run. So the
  script is really "given the current widget state, what should the page look
  like?" recomputed from scratch each time.

- **`st.session_state` is the only thing that survives a rerun.** Because the
  script restarts, a plain local variable (`result = post_triage(...)`) is gone
  on the next interaction. If the verdict were only a local, then opening a
  citation expander — which triggers a rerun in which the button is NOT pressed
  — would wipe the whole verdict off the page. So the last API response is
  parked in ``st.session_state`` (a dict that persists across reruns for one
  browser session) and the page renders FROM it. Without this the UI would
  flicker its results away the moment you inspected a source.

- **Widgets vs layout containers.** `st.text_area`/`st.button`/`st.slider` are
  input WIDGETS (they return values). `st.expander`/`st.columns`/`st.sidebar`
  are layout CONTAINERS (they just group output). The citation panels are
  expanders: collapsed by default so the verdict stays scannable, expanded to
  reveal the full retrieved source text on demand.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

import streamlit as st

# Default points at a local `triage serve` (127.0.0.1:8000). In compose the
# launcher (`triage ui --api-url http://api:8000`) overrides it via this env
# var, so the UI reaches the API by its compose SERVICE NAME with no code
# change. Precedence mirrors the data-dir pattern: an explicit flag becomes
# this env var, which falls back to the local default.
DEFAULT_API_URL = "http://127.0.0.1:8000"

# A real triage call embeds the alert locally AND calls Claude, so it can take
# many seconds. Be patient rather than timing out a legitimately-working call.
_REQUEST_TIMEOUT_S = 120

# How the disposition string renders — purely cosmetic, keeps the verdict
# scannable at a glance.
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


def api_base_url() -> str:
    """Resolve the API base URL: ``TRIAGE_API_URL`` env, else the local default."""
    return os.environ.get("TRIAGE_API_URL", DEFAULT_API_URL).rstrip("/")


def post_triage(api_url: str, alert: str, top_k: int) -> dict[str, Any]:
    """POST one alert to ``/triage`` and return the parsed response envelope.

    Uses the stdlib ``urllib`` only — no ``requests``/``httpx`` runtime
    dependency, the same minimal-deps choice the compose healthcheck makes.

    Args:
        api_url: Base URL of the triage API (no trailing slash).
        alert: The analyst's alert text.
        top_k: How many source documents to request as grounding.

    Returns:
        The decoded ``TriageResponse`` JSON: ``{"verdict": {...},
        "retrieved": [...]}``.

    Raises:
        urllib.error.HTTPError: On a non-2xx response (e.g. 422 bad input, 502
            upstream-model failure). The error object still carries the JSON
            body, so the caller can surface the ``detail`` message.
        urllib.error.URLError: On a transport failure (API down / unreachable).
    """
    payload = json.dumps({"alert": alert, "top_k": top_k}).encode("utf-8")
    request = urllib.request.Request(
        f"{api_url}/triage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
        result: dict[str, Any] = json.load(response)
        return result


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """Pull the API's ``detail`` message out of an HTTPError body if present.

    FastAPI reports both its 422 validation errors and our 502 upstream
    failures as JSON with a ``detail`` field; fall back to the raw body when it
    is not the shape we expect.
    """
    try:
        body = json.load(exc)
    except (json.JSONDecodeError, ValueError):
        return exc.reason or "unknown error"
    detail = body.get("detail", body)
    return detail if isinstance(detail, str) else json.dumps(detail)


def render_verdict(verdict: dict[str, Any]) -> None:
    """Render the grounded verdict block (everything except the source panels)."""
    disposition = verdict.get("verdict", "")
    severity = verdict.get("severity", "")
    confidence = verdict.get("confidence", 0.0)

    col_verdict, col_severity, col_confidence = st.columns(3)
    col_verdict.metric("Verdict", _VERDICT_LABELS.get(disposition, disposition))
    col_severity.metric("Severity", _SEVERITY_LABELS.get(severity, severity))
    col_confidence.metric("Confidence", f"{confidence:.0%}")

    st.markdown("**Summary**")
    st.write(verdict.get("summary", ""))

    techniques = verdict.get("mitre_techniques") or []
    if techniques:
        st.markdown("**MITRE ATT&CK techniques**")
        st.write(", ".join(techniques))

    actions = verdict.get("recommended_actions") or []
    if actions:
        st.markdown("**Recommended actions**")
        for action in actions:
            st.markdown(f"- {action}")


def render_sources(verdict: dict[str, Any], retrieved: list[dict[str, Any]]) -> None:
    """Render one expandable panel per retrieved source.

    The panels are the whole point of the retrieval envelope (see api.py): each
    shows the FULL grounding text, whether the model actually CITED it (and its
    quote), and — the piece the verdict alone can't express — whether the
    source was BACKFILLED (a runbook appended by the guarantee, not matched by
    similarity), so the analyst weighs it accordingly.
    """
    # Index the verdict's citations by the source id they point at, so each
    # panel can show "the model cited this, with this quote".
    quotes_by_id = {
        c["chunk_id"]: c.get("quote") for c in verdict.get("citations", [])
    }

    st.markdown(f"**Retrieved sources** ({len(retrieved)})")
    for source in retrieved:
        source_id = source["id"]
        cited = source_id in quotes_by_id
        backfilled = source.get("backfilled", False)

        # The expander LABEL carries the at-a-glance status; the body carries
        # the detail. Cited sources open by default (they back the verdict);
        # uncited/backfilled ones stay collapsed.
        marks = ["✅ cited" if cited else "not cited"]
        if backfilled:
            marks.append("⚠️ backfilled")
        status = ", ".join(marks)
        label = (
            f"{source['source_type']} · {source['name']} "
            f"({source_id}) — {status}"
        )

        with st.expander(label, expanded=cited):
            if backfilled:
                st.info(
                    "Backfilled: this runbook did not match the alert by "
                    "similarity — it was appended so a triage procedure is "
                    "always on hand. Judge its relevance rather than assume it."
                )
            quote = quotes_by_id.get(source_id)
            if quote:
                st.markdown("**Quoted by the verdict:**")
                st.markdown(f"> {quote}")
            st.markdown("**Full retrieved source:**")
            st.text(source["text"])


def main() -> None:
    """Draw the whole page. Re-invoked top-to-bottom on every interaction."""
    st.set_page_config(page_title="Alert Triage RAG", page_icon="🛡️")
    st.title("🛡️ Alert Triage RAG")
    st.caption(
        "Describe a SOC alert; get a grounded verdict with citations back to "
        "MITRE ATT&CK techniques and internal runbooks."
    )

    api_url = api_base_url()
    st.sidebar.markdown("**API endpoint**")
    st.sidebar.code(api_url)
    st.sidebar.caption(
        "This UI is a thin client — it only calls this API over HTTP. Start it "
        "with `triage serve` or `docker compose up`."
    )

    alert_text = st.text_area(
        "Alert description",
        height=160,
        placeholder="e.g. Multiple failed SSH logins from a single external IP, "
        "followed by one success and an outbound connection to an unknown host.",
    )
    top_k = st.slider(
        "Sources to retrieve (top-k)", min_value=1, max_value=15, value=5
    )
    submitted = st.button("Triage alert", type="primary")

    if submitted:
        if not alert_text.strip():
            st.warning("Enter an alert description first.")
        else:
            # The spinner is honest: a real call is seconds of local embedding
            # plus a Claude round-trip.
            with st.spinner("Retrieving sources and generating a verdict…"):
                try:
                    st.session_state["result"] = post_triage(
                        api_url, alert_text.strip(), top_k
                    )
                    st.session_state.pop("error", None)
                except urllib.error.HTTPError as exc:
                    # The service answered, but with an error status. 422 = the
                    # request body failed validation; 502 = the upstream model
                    # failed us. Show the API's own detail message.
                    st.session_state["error"] = (
                        f"API returned {exc.code}: {_http_error_detail(exc)}"
                    )
                    st.session_state.pop("result", None)
                except urllib.error.URLError as exc:
                    # No response at all — the API is down or unreachable.
                    st.session_state["error"] = (
                        f"Could not reach the API at {api_url} ({exc.reason}). "
                        "Is `triage serve` (or the api container) running?"
                    )
                    st.session_state.pop("result", None)

    # Render FROM session_state, not from the local `submitted` branch, so the
    # verdict survives the reruns that opening a citation panel triggers.
    if error := st.session_state.get("error"):
        st.error(error)
    elif result := st.session_state.get("result"):
        st.divider()
        render_verdict(result["verdict"])
        st.divider()
        render_sources(result["verdict"], result["retrieved"])


# `streamlit run triage/ui.py` executes this module as __main__; the launcher
# (triage/ui_launch.py) is what points streamlit at this file.
if __name__ == "__main__":
    main()
else:
    # Streamlit imports the script as a module named for its path (not
    # "__main__"), so calling main() here is what actually draws the page when
    # launched via `streamlit run`.
    main()
