"""The ``triage serve`` subcommand: run the API under uvicorn.

uvicorn is the ASGI *server*: it owns the TCP socket, parses HTTP, and calls
the FastAPI *application* (see api.py) once per request. The split mirrors
WSGI's server/app separation (gunicorn/Django), updated for async: any ASGI
server can run any ASGI app. FastAPI ships no server of its own.

`uvicorn.run` is handed the app OBJECT, not an import string ("triage.api:app").
That's deliberate: the app is built by a factory from parsed CLI flags, so
there IS no module-level app to name. The trade-off is that string-only
uvicorn features (--reload, --workers) are unavailable — acceptable here:
reload is a dev nicety, and multiple workers would each duplicate the ~100 MB
embedding model; scaling out is Phase 10+ territory (run more containers).

Defaults bind 127.0.0.1: the service is reachable only from the local machine
until the operator deliberately exposes it (``--host 0.0.0.0``) — it fronts a
paid API and has no auth of its own, so closed-by-default is the right posture.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from . import paths
from .api import ApiSettings, create_app
from .ingest import DEFAULT_COLLECTION, DEFAULT_EMBED_MODEL
from .query import DEFAULT_GEN_MODEL
from .rewrite import DEFAULT_REWRITE_MODEL


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the serve arguments to ``parser``.

    Shared between the standalone module entry point and the ``triage serve``
    subcommand (see cli.py). Store/model flags mirror ``triage query`` — the
    server is the same pipeline behind a socket — minus the per-alert ones
    (the alert and top_k arrive in each request body instead).
    """
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind. Default is local-only; use 0.0.0.0 to expose "
        "the service on the network (deliberate act — no built-in auth).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="TCP port to listen on.",
    )
    parser.add_argument(
        "--db-dir",
        type=Path,
        default=paths.chroma_dir(),
        help="Directory of the persisted Chroma database.",
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help="Chroma collection name.",
    )
    parser.add_argument(
        "--embed-model",
        default=DEFAULT_EMBED_MODEL,
        help="sentence-transformers model id (runs locally).",
    )
    parser.add_argument(
        "--gen-model",
        default=DEFAULT_GEN_MODEL,
        help="Claude model id for generation.",
    )
    parser.add_argument(
        "--rewrite-model",
        default=DEFAULT_REWRITE_MODEL,
        help="Claude model id for the pre-retrieval query rewrite.",
    )
    parser.add_argument(
        "--no-rewrite",
        action="store_true",
        help="Embed raw alert text directly, skipping the rewrite step.",
    )


def run(args: argparse.Namespace) -> None:
    """Build the app from parsed arguments and serve it (subcommand handler)."""
    app = create_app(
        ApiSettings(
            db_dir=args.db_dir,
            collection_name=args.collection,
            embed_model=args.embed_model,
            gen_model=args.gen_model,
            rewrite_model=args.rewrite_model,
            no_rewrite=args.no_rewrite,
        )
    )
    uvicorn.run(app, host=args.host, port=args.port)


def main(argv: list[str] | None = None) -> None:
    """Standalone entry point (`python -m triage.serve`)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
