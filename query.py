"""Query pipeline: alert text -> rewrite -> retrieve -> ground -> Claude -> validated JSON.

This file is partly author-owned. The plumbing (CLI, loading the embedder and
the persisted Chroma collection, calling retrieval) is wired here. The GROUNDING
PROMPT and the Claude call that consumes it are author-owned (see CLAUDE.md) and
left as a clearly marked TODO. Final JSON validation hooks into `schema.py`,
which is defined in a later pairing step.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

from ingest import DEFAULT_COLLECTION, DEFAULT_DB_DIR, DEFAULT_EMBED_MODEL
from retrieve import RetrievedChunk, retrieve
from rewrite import DEFAULT_REWRITE_MODEL, ensure_embeddable, rewrite_alert

# Generation model (see CLAUDE.md: Anthropic API, Claude). Opus 4.8 is the
# current default; the author may tune this when wiring the call.
DEFAULT_GEN_MODEL = "claude-opus-4-8"
DEFAULT_TOP_K = 5


def load_collection(
    db_dir: Path, collection_name: str
) -> chromadb.Collection:
    """Open the persisted Chroma collection produced by ingestion.

    Args:
        db_dir: Directory of the persisted Chroma database.
        collection_name: Name of the collection to open.

    Returns:
        The Chroma collection, ready to query.

    Raises:
        FileNotFoundError: If the database directory does not exist (i.e.
            ingestion has not been run yet).
    """
    if not db_dir.is_dir():
        raise FileNotFoundError(
            f"Chroma database not found at {db_dir}. Run ingest.py first."
        )
    client = chromadb.PersistentClient(path=str(db_dir))
    return client.get_collection(name=collection_name)


def build_grounding_prompt(alert_text: str, chunks: list[RetrievedChunk]) -> str:
    """Build the grounded prompt for Claude — AUTHOR-OWNED.

    This is the core of the system's explainability: how retrieved ATT&CK
    techniques and runbook steps are framed for Claude, and how the model is
    instructed to cite its sources and conform to the output schema. The author
    writes this (see CLAUDE.md).

    Args:
        alert_text: The analyst's natural-language alert description.
        chunks: The retrieved, citable source chunks.

    Returns:
        The grounded prompt string to send to Claude.
    """
    # TODO(author): write the grounding prompt. Must instruct Claude to cite the
    # source chunks it used and to emit JSON conforming to schema.py.
    raise NotImplementedError("The grounding prompt is author-owned; see CLAUDE.md")


def triage(
    alert_text: str,
    db_dir: Path,
    collection_name: str,
    embed_model: str,
    gen_model: str,
    rewrite_model: str,
    top_k: int,
    no_rewrite: bool = False,
) -> None:
    """Run the query pipeline for a single alert.

    Wires rewriting and retrieval together with generation. The generation +
    validation stage is intentionally not implemented here because it depends
    on the author-owned grounding prompt (`build_grounding_prompt`) and on
    `schema.py`.

    Args:
        alert_text: The alert as the analyst provided it (prose or raw log).
        db_dir: Directory of the persisted Chroma database.
        collection_name: Collection to query.
        embed_model: Local sentence-transformers model id.
        gen_model: Claude model id for generation.
        rewrite_model: Claude model id for the pre-retrieval query rewrite.
        top_k: Number of chunks to retrieve.
        no_rewrite: Embed the raw alert text directly, skipping the rewrite
            (escape hatch / A-B comparison; the token-window guard still runs).
    """
    embedder = SentenceTransformer(embed_model)
    collection = load_collection(db_dir, collection_name)

    # The rewrite shapes ONLY the search text; the original alert stays the
    # evidence for the grounding prompt below.
    if no_rewrite:
        search_text = alert_text
    else:
        search_text = rewrite_alert(alert_text, model=rewrite_model)
        print(f"Search query (rewritten): {search_text}")
    ensure_embeddable(search_text, embedder.tokenizer)

    chunks = retrieve(search_text, collection, embedder, k=top_k)

    # --- AUTHOR-OWNED region (grounding prompt + Claude call) -----------------
    # Once build_grounding_prompt is implemented and schema.py is defined:
    #   1. prompt = build_grounding_prompt(alert_text, chunks)
    #   2. call the Anthropic SDK (model=gen_model) with that prompt
    #      (use anthropic.Anthropic(); see the claude-api docs for output_config
    #       / structured outputs against schema.py's model)
    #   3. validate the response against the Pydantic schema in schema.py
    #   4. ensure every verdict cites the chunks it used (citations are
    #      non-negotiable, see CLAUDE.md)
    raise NotImplementedError(
        "Generation + validation depends on the author-owned grounding prompt "
        "and schema.py; wire it during the pairing step."
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the query script."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "alert",
        help="The alert description in natural language (quote it).",
    )
    parser.add_argument(
        "--db-dir",
        default=DEFAULT_DB_DIR,
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
        help="Embed the raw alert text directly, skipping the rewrite step.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help="Number of chunks to retrieve.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    args = parse_args(argv)
    triage(
        alert_text=args.alert,
        db_dir=Path(args.db_dir),
        collection_name=args.collection,
        embed_model=args.embed_model,
        gen_model=args.gen_model,
        rewrite_model=args.rewrite_model,
        top_k=args.top_k,
        no_rewrite=args.no_rewrite,
    )


if __name__ == "__main__":
    main()
