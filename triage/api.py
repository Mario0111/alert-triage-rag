"""The FastAPI service: the single HTTP integration surface for triage.

Per CLAUDE.md's Stage 2 rule, everything that wants a verdict — the CLI today,
the Streamlit UI and the SIEM webhook later — talks to this one service, and
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
  status. The response is `schema.TriageVerdict` ITSELF (declared via the
  return annotation): the verdict contract and the API contract are the same
  object, so they cannot drift. Both models also feed the auto-generated
  OpenAPI docs at ``/docs``.

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
from .query import DEFAULT_GEN_MODEL, DEFAULT_TOP_K, load_collection, triage_alert
from .rewrite import DEFAULT_REWRITE_MODEL
from .schema import TriageVerdict

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
    ) -> TriageVerdict:
        """Triage one alert; the response IS `schema.TriageVerdict`."""
        try:
            return triage_alert(
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

    return app
