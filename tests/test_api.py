"""The FastAPI service (triage/api.py) driven through a real HTTP client.

TestClient makes actual HTTP requests that are handed straight to the ASGI app
in-process — request parsing, validation, routing, status codes and JSON
serialization are all exercised for real; no socket is opened. Hermetic like
the rest of the suite: the happy-path app gets a fake pipeline (in-memory
Chroma + FakeEmbedder + scripted Anthropic client) by monkeypatching the
loader, so retrieval and grounding run for real over HTTP with no network, no
API calls, and no model download. Startup-failure tests use the REAL loader
against broken stores, which fails before anything heavy would load.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import uuid4

import anthropic
import chromadb
import pytest
from fastapi.testclient import TestClient
from sentence_transformers import SentenceTransformer

from tests.conftest import (
    ChunkRecord,
    FakeAnthropicClient,
    FakeEmbedder,
    FakeMessages,
    FakeParseResponse,
    make_verdict,
    runbook_chunk,
    technique_chunk,
)
from triage import api
from triage.fingerprint import StaleStoreError
from triage.schema import SourceType, TriageVerdict


def make_app(
    monkeypatch: pytest.MonkeyPatch,
    collection: chromadb.Collection,
    responses: list[object],
) -> tuple[TestClient, FakeMessages]:
    """Build a test app whose lifespan loads a fake pipeline.

    Patching the loader (not the endpoint) keeps everything downstream real:
    the lifespan still runs, the dependency still resolves from app.state, and
    triage_alert executes the actual retrieve -> ground -> validate path.
    """
    messages = FakeMessages(queue=list(responses))
    pipeline = api.Pipeline(
        embedder=cast(SentenceTransformer, FakeEmbedder([1.0, 0.0, 0.0])),
        collection=collection,
        client=cast(anthropic.Anthropic, FakeAnthropicClient(messages)),
    )
    monkeypatch.setattr(api, "_load_pipeline", lambda settings: pipeline)
    settings = api.ApiSettings(
        db_dir=Path("unused-when-loader-is-patched"),
        no_rewrite=True,  # the rewrite stage would need its own Claude call
    )
    return TestClient(api.create_app(settings)), messages


@pytest.fixture
def corpus() -> list[ChunkRecord]:
    # One technique near the test query vector, one runbook further away but
    # inside top-k: both come back as citable sources, neither backfilled.
    return [
        technique_chunk(
            "T1110", "description", 0, 1, "Adversaries may guess passwords.",
            embedding=[1.0, 0.0, 0.0], name="Brute Force",
        ),
        runbook_chunk(
            "rb-brute-force.md", 0, 1, "Check auth logs.",
            embedding=[0.0, 1.0, 0.0],
        ),
    ]


# --- the healthy paths ---------------------------------------------------------


def test_health_reports_ok_once_startup_succeeded(
    monkeypatch: pytest.MonkeyPatch,
    make_collection: Callable[[list[ChunkRecord]], chromadb.Collection],
    corpus: list[ChunkRecord],
) -> None:
    client, _ = make_app(monkeypatch, make_collection(corpus), responses=[])
    with client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_triage_returns_a_grounded_verdict_over_http(
    monkeypatch: pytest.MonkeyPatch,
    make_collection: Callable[[list[ChunkRecord]], chromadb.Collection],
    corpus: list[ChunkRecord],
) -> None:
    verdict = make_verdict([("T1110", SourceType.ATTACK)])
    client, messages = make_app(
        monkeypatch,
        make_collection(corpus),
        responses=[FakeParseResponse(parsed_output=verdict)],
    )

    with client:
        response = client.post(
            "/triage", json={"alert": "many failed ssh logins from one source"}
        )

    assert response.status_code == 200
    body_json = response.json()
    # The response is the envelope: the verdict (still exactly the schema.py
    # contract, parseable back into TriageVerdict) plus the retrieved sources.
    returned = TriageVerdict.model_validate(body_json["verdict"])
    assert returned == verdict
    assert returned.citations[0].chunk_id == "T1110"
    # The model was grounded on the ORIGINAL alert text from the request body.
    prompt = messages.calls[0]["messages"][0]["content"]
    assert "many failed ssh logins from one source" in prompt
    # Retrieval really ran against the collection: both stored documents were
    # offered as citable sources.
    assert '<source id="T1110"' in prompt
    assert '<source id="rb-brute-force.md"' in prompt
    # And that same retrieval provenance is now exposed to the client (the UI
    # citation panel): both documents, correctly typed, neither backfilled,
    # each carrying its full grounding text.
    retrieved = {src["id"]: src for src in body_json["retrieved"]}
    assert set(retrieved) == {"T1110", "rb-brute-force.md"}
    assert retrieved["T1110"]["source_type"] == SourceType.ATTACK.value
    assert retrieved["rb-brute-force.md"]["source_type"] == SourceType.RUNBOOK.value
    assert all(not src["backfilled"] for src in retrieved.values())
    assert "Adversaries may guess passwords." in retrieved["T1110"]["text"]


# --- input validation -> 422 ----------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="missing-alert"),
        pytest.param({"alert": ""}, id="empty-alert"),
        pytest.param({"alert": "x", "top_k": 0}, id="top_k-below-minimum"),
        pytest.param({"alert": "x", "verdict": "benign"}, id="unknown-field"),
    ],
)
def test_invalid_request_bodies_are_rejected_as_422(
    monkeypatch: pytest.MonkeyPatch,
    make_collection: Callable[[list[ChunkRecord]], chromadb.Collection],
    corpus: list[ChunkRecord],
    body: dict[str, object],
) -> None:
    client, messages = make_app(monkeypatch, make_collection(corpus), responses=[])
    with client:
        response = client.post("/triage", json=body)
    # 422: well-formed HTTP, body fails Pydantic validation — rejected by
    # FastAPI before the endpoint runs, so the pipeline is never touched.
    assert response.status_code == 422
    assert messages.calls == []


# --- upstream (Claude) failures -> 502 -------------------------------------------


def test_model_refusal_maps_to_502(
    monkeypatch: pytest.MonkeyPatch,
    make_collection: Callable[[list[ChunkRecord]], chromadb.Collection],
    corpus: list[ChunkRecord],
) -> None:
    client, _ = make_app(
        monkeypatch,
        make_collection(corpus),
        responses=[FakeParseResponse(parsed_output=None, stop_reason="refusal")],
    )
    with client:
        response = client.post("/triage", json={"alert": "some alert"})
    # 502 Bad Gateway: the service worked; its upstream produced nothing this
    # service will vouch for. The pipeline's loud error is passed through.
    assert response.status_code == 502
    assert "refused" in response.json()["detail"]


def test_persistent_grounding_failure_maps_to_502(
    monkeypatch: pytest.MonkeyPatch,
    make_collection: Callable[[list[ChunkRecord]], chromadb.Collection],
    corpus: list[ChunkRecord],
) -> None:
    hallucinated = make_verdict([("T9999", SourceType.ATTACK)])
    client, messages = make_app(
        monkeypatch,
        make_collection(corpus),
        responses=[
            FakeParseResponse(parsed_output=hallucinated),
            FakeParseResponse(parsed_output=hallucinated),
        ],
    )
    with client:
        response = client.post("/triage", json={"alert": "some alert"})
    assert response.status_code == 502
    assert "failed validation" in response.json()["detail"]
    # The bounded feedback retry still happened inside the service.
    assert len(messages.calls) == 2


# --- startup failures: the server must refuse to come up -------------------------


def test_missing_store_aborts_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The key check precedes the store check; satisfy it hermetically.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    app = api.create_app(api.ApiSettings(db_dir=tmp_path / "never_ingested"))
    # No HTTP status here BY DESIGN: entering the client runs the lifespan,
    # and a missing store kills startup — the server never binds a port, so
    # there is no server to answer anything. Fail loudly, not serve-503s.
    with pytest.raises(FileNotFoundError, match="triage ingest"):
        with TestClient(app):
            pass


def test_stale_store_aborts_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    db_dir = tmp_path / "chroma_db"
    name = f"stale-{uuid4().hex}"
    # Default settings: same reasoning as test_fingerprint's stale-store test.
    chromadb.PersistentClient(path=str(db_dir)).create_collection(
        name=name, metadata={"hnsw:space": "cosine"}
    )

    app = api.create_app(api.ApiSettings(db_dir=db_dir, collection_name=name))
    with pytest.raises(StaleStoreError, match="triage ingest"):
        with TestClient(app):
            pass


def test_missing_api_key_aborts_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = api.create_app(api.ApiSettings(db_dir=tmp_path))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        with TestClient(app):
            pass
