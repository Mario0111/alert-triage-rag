"""Tiny HTTP client for the triage API, shared by every thin-client interface.

The native desktop app (desktop.py) — and any future thin client, such as the
SIEM webhook — talks to ``POST /triage`` (CLAUDE.md's single-integration-surface
rule). The client-side logic they share — build the request, call the endpoint,
turn transport and HTTP errors into a human message — lives here so it is
written and tested once, independent of any GUI toolkit. That independence is
what lets the test suite cover this path with no GUI installed at all.

Deliberately stdlib ``urllib`` only: no ``requests``/``httpx`` runtime
dependency (the same minimal-deps choice the compose healthcheck makes), so this
module imports nothing heavier than the standard library and both GUIs stay
free to depend on it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

# Default points at a local `triage serve` (127.0.0.1:8000). Clients override it
# via the TRIAGE_API_URL env var (which their --api-url flag sets), so the same
# UI reaches a local server, a `docker compose` service, or a remote host with
# no code change.
DEFAULT_API_URL = "http://127.0.0.1:8000"

# A real triage call embeds the alert locally AND calls Claude, so it can take
# many seconds. Be patient rather than timing out a legitimately-working call.
_REQUEST_TIMEOUT_S = 120


def api_base_url() -> str:
    """Resolve the API base URL: ``TRIAGE_API_URL`` env, else the local default."""
    return os.environ.get("TRIAGE_API_URL", DEFAULT_API_URL).rstrip("/")


def post_triage(api_url: str, alert: str, top_k: int) -> dict[str, Any]:
    """POST one alert to ``/triage`` and return the parsed response envelope.

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
            body, so `error_message` can surface the ``detail`` field.
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
    failures as JSON with a ``detail`` field; fall back to the raw reason when
    the body is not the shape we expect.
    """
    try:
        body = json.load(exc)
    except (json.JSONDecodeError, ValueError):
        return exc.reason or "unknown error"
    detail = body.get("detail", body)
    return detail if isinstance(detail, str) else json.dumps(detail)


def error_message(exc: urllib.error.URLError, api_url: str) -> str:
    """Turn a request failure into one analyst-facing line.

    ``HTTPError`` is a subclass of ``URLError``, so a caller can catch the
    single base type and hand it here; this branches on which it actually is:
    an HTTP status (the service answered with 422/502) versus no response at all
    (the service is down or unreachable).
    """
    if isinstance(exc, urllib.error.HTTPError):
        return f"API returned {exc.code}: {_http_error_detail(exc)}"
    return (
        f"Could not reach the API at {api_url} ({exc.reason}). "
        "Is `triage serve` (or the api container) running?"
    )
