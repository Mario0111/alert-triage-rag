"""The FastAPI service: the single HTTP integration surface for triage.

Per CLAUDE.md's Stage 2 rule, everything that wants a verdict — the CLI, the
native desktop app, and the SIEM webhook later — talks to this one service, and
this service is a THIN wrapper: `POST /triage` calls the exact same
`query.triage_alert` core the CLI uses. No interface grows its own triage
logic.

Web-service concepts this module leans on (teaching notes):

- **ASGI.** FastAPI apps are ASGI applications: a standard calling convention
  between an async-capable web server and a Python app (the async successor to
  WSGI, which was one-request-per-thread only). The app object below is just
  "a thing a server can call per request"; it does not listen on a port itself
  — that's the server's job (uvicorn, see serve.py).

- **App factory + lifespan.** `create_app` builds the app around explicit
  settings instead of module-level globals, so tests can build throwaway apps
  against fake pipelines. The `lifespan` async context manager is FastAPI's
  startup/shutdown hook: everything before its ``yield`` runs ONCE when the
  server starts — that's where the expensive objects (the ~100 MB embedding
  model, the Chroma collection, the Anthropic client) are loaded and parked on
  ``app.state``. Without it, each request would pay seconds of model loading.
  A startup failure (missing/stale store, no API key) aborts the server
  before it ever binds a port: fail loudly beats a zombie service answering
  503 forever, and the operator gets the "re-run triage ingest" message
  immediately instead of finding it in a request log.

- **Dependency injection.** The endpoint declares the pipeline as a parameter
  (``Annotated[Pipeline, Depends(get_pipeline)]``) rather than reaching for a
  global. FastAPI resolves it per request; tests could override it via
  ``app.dependency_overrides`` without monkeypatching.

- **Pydantic models at the boundary.** The request body is validated against
  `TriageRequest` BEFORE the endpoint runs — a missing/empty alert never
  reaches the pipeline and is rejected as 422 Unprocessable Entity, FastAPI's
  standard "the request was well-formed HTTP but the body fails validation"
  status. The response is a `TriageResponse` ENVELOPE: `schema.TriageVerdict`
  unchanged (still the single output contract, so it cannot drift), plus a
  `retrieved` list carrying the sources retrieval surfaced — full text, source
  type, and the ``backfilled`` marking — which the verdict's own citations
  (only what the model chose to cite) cannot express. Splitting the retrieval
  PROVENANCE from the model's OUTPUT contract is why this wraps TriageVerdict
  rather than adding fields to it. All models feed the OpenAPI docs at
  ``/docs``.

- **Status mapping.** 200: verdict produced. 422: bad input (Pydantic).
  502 Bad Gateway: this service is fine, but its upstream (the Anthropic API)
  failed or returned nothing usable — an API error, a refusal, a truncation,
  or a verdict still failing validation after the feedback retry. 502's
  meaning is exactly "invalid response from an upstream server".

- **Sync endpoint on purpose.** ``def triage_endpoint`` (not ``async def``):
  the pipeline blocks for seconds (local embedding + Claude call). FastAPI
  runs plain-``def`` endpoints in a worker threadpool, keeping the event loop
  free; an ``async def`` running blocking code would freeze every concurrent
  request (including /health) for the duration.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import anthropic
import chromadb
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sentence_transformers import SentenceTransformer

from .fingerprint import app_version
from .ingest import DEFAULT_COLLECTION, DEFAULT_EMBED_MODEL
from .query import (
    DEFAULT_GEN_MODEL,
    DEFAULT_TOP_K,
    _source_kind,
    load_collection,
    triage_alert,
)
from .retrieve import RetrievedChunk
from .rewrite import DEFAULT_REWRITE_MODEL
from .schema import SourceType, TriageVerdict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass(frozen=True)
class ApiSettings:
    """Server-side configuration, fixed for the lifetime of the process.

    Model choices and store location are deliberately NOT per-request: clients
    say WHAT to triage, the operator decides HOW (which store, which models) —
    the same split the CLI flags express.
    """

    db_dir: Path
    collection_name: str = DEFAULT_COLLECTION
    embed_model: str = DEFAULT_EMBED_MODEL
    gen_model: str = DEFAULT_GEN_MODEL
    rewrite_model: str = DEFAULT_REWRITE_MODEL
    no_rewrite: bool = False


@dataclass
class Pipeline:
    """The heavy per-process objects, loaded once at startup (see lifespan)."""

    embedder: SentenceTransformer
    collection: chromadb.Collection
    client: anthropic.Anthropic


class TriageRequest(BaseModel):
    """Body of ``POST /triage``: the alert, plus the one client-side knob."""

    model_config = ConfigDict(extra="forbid")

    alert: str = Field(
        min_length=1,
        description="The alert to triage, in natural language or raw log form.",
    )
    top_k: int = Field(
        default=DEFAULT_TOP_K,
        ge=1,
        le=25,
        description="How many source documents to retrieve as grounding.",
    )


class RetrievedSource(BaseModel):
    """One retrieved document, as exposed to API clients (the UI citation panel).

    This is the retrieval provenance the verdict itself cannot carry: the FULL
    reassembled source text, and the ``backfilled`` marking that says a runbook
    was appended by the retrieval guarantee rather than matched by similarity.
    A client links a verdict `Citation` back to its source by
    ``id == Citation.chunk_id``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description="Citable id: the ATT&CK technique id or the runbook filename."
    )
    source_type: SourceType = Field(
        description="Whether this document came from ATT&CK or a runbook."
    )
    name: str = Field(
        description="Human-friendly label: technique name or runbook filename."
    )
    backfilled: bool = Field(
        description=(
            "True when this runbook was appended by the retrieval guarantee "
            "(it did NOT place in the similarity top-k) — the UI marks it so an "
            "analyst weighs its relevance rather than assuming it."
        )
    )
    score: float = Field(
        description="Cosine distance of the best-matching chunk (lower is nearer)."
    )
    text: str = Field(
        description="The full reassembled document text used as grounding."
    )


class TriageResponse(BaseModel):
    """Body of ``POST /triage``: the grounded verdict plus its retrieved sources.

    The verdict stays `schema.TriageVerdict` unchanged (the output contract is
    the single source of truth); ``retrieved`` wraps it with the retrieval
    detail every client — CLI, UI, SIEM — can render. Kept as an envelope,
    rather than fields on TriageVerdict, so the model's OUTPUT contract and the
    service's retrieval PROVENANCE evolve independently.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: TriageVerdict
    retrieved: list[RetrievedSource]


def _to_source(chunk: RetrievedChunk) -> RetrievedSource:
    """Map a retrieval `RetrievedChunk` to the wire `RetrievedSource`.

    ``_source_kind`` (query.py) is the one canonical ATT&CK-vs-runbook
    discriminator, reused here so the response can never disagree with what the
    grounding prompt and citation checks used.
    """
    return RetrievedSource(
        id=chunk.id,
        source_type=_source_kind(chunk),
        name=str(chunk.metadata.get("name", chunk.id)),
        backfilled=chunk.backfilled,
        score=chunk.score,
        text=chunk.text,
    )


def _load_pipeline(settings: ApiSettings) -> Pipeline:
    """Load every expensive/failing dependency, cheapest check first.

    Order matters for failing fast: the API-key and store checks are instant
    and catch the common misconfigurations before the multi-second embedding
    model load. `load_collection` performs the staleness-fingerprint check, so
    a stale store can never serve a single verdict.

    Raises:
        RuntimeError: If ``ANTHROPIC_API_KEY`` is not set (env-only by design;
            the key never appears in flags or config files).
        FileNotFoundError: If the store or collection does not exist yet.
        StaleStoreError: If the store no longer matches the running code.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. The API key is read from the "
            "environment only; export it before running `triage serve`."
        )
    collection = load_collection(
        settings.db_dir, settings.collection_name, embed_model=settings.embed_model
    )
    embedder = SentenceTransformer(settings.embed_model)
    return Pipeline(
        embedder=embedder, collection=collection, client=anthropic.Anthropic()
    )


def get_pipeline(request: Request) -> Pipeline:
    """Dependency: hand the process-wide pipeline to an endpoint."""
    pipeline: Pipeline = request.app.state.pipeline
    return pipeline


def create_app(settings: ApiSettings) -> FastAPI:
    """Build the ASGI app around explicit settings (the app-factory pattern).

    Args:
        settings: Store location and model choices for this process.

    Returns:
        A FastAPI app exposing ``POST /triage`` and ``GET /health``, with the
        heavy pipeline loaded once via the lifespan hook.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Runs once at server startup. An exception here aborts startup —
        # uvicorn exits instead of serving requests it could never answer.
        app.state.pipeline = _load_pipeline(settings)
        yield
        # (Nothing to tear down: no open files/sockets of our own.)

    app = FastAPI(
        title="alert-triage-rag",
        description=(
            "RAG triage for SOC alerts: retrieves MITRE ATT&CK techniques and "
            "internal runbooks, returns a grounded verdict with citations."
        ),
        version=app_version(),
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe: the process is up and startup succeeded.

        Reaching this handler at all proves the lifespan completed, which
        includes the store/fingerprint check — so "up" also means "ready".
        """
        return {"status": "ok"}

    @app.post("/triage")
    def triage_endpoint(
        body: TriageRequest,
        pipeline: Annotated[Pipeline, Depends(get_pipeline)],
    ) -> TriageResponse:
        """Triage one alert; the response wraps the verdict with its sources."""
        try:
            result = triage_alert(
                body.alert,
                pipeline.embedder,
                pipeline.collection,
                gen_model=settings.gen_model,
                rewrite_model=settings.rewrite_model,
                top_k=body.top_k,
                no_rewrite=settings.no_rewrite,
                client=pipeline.client,
            )
        except anthropic.APIError as exc:
            raise HTTPException(
                status_code=502, detail=f"Anthropic API failure: {exc}"
            ) from exc
        except (RuntimeError, ValueError) as exc:
            # The pipeline's fail-loudly errors: refusal/truncation
            # (RuntimeError), or a verdict that still fails schema/grounding
            # validation after the feedback retry (ValueError). All mean the
            # upstream model produced nothing this service will vouch for.
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return TriageResponse(
            verdict=result.verdict,
            retrieved=[_to_source(chunk) for chunk in result.retrieved],
        )

    return app
