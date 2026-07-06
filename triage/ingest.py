"""Ingestion pipeline: corpus -> chunk -> embed -> persist to Chroma.

Run once, or whenever the corpus changes. This module is the plumbing that wires
the pieces together; the actual chunking lives in `chunk.py` (author-owned).
Once those stubs are implemented, this script runs end to end:

    corpus/attack/*.json  --stix.parse_techniques-->  Technique
                          --chunk.chunk_techniques-->  Chunk
    corpus/runbooks/*.md  --chunk.chunk_runbook----->  Chunk
                          --bge-small-en-v1.5-------->  embeddings
                          --chromadb----------------->  ./chroma_db

Embeddings are computed locally (no API calls) and the collection uses cosine
distance, which is what bge-small-en-v1.5 is trained for.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

import chromadb
from chromadb.errors import NotFoundError
from sentence_transformers import SentenceTransformer

import stix
from chunk import Chunk, chunk_runbook, chunk_techniques

if TYPE_CHECKING:
    from transformers import PreTrainedTokenizerBase

# Fixed stack (see CLAUDE.md). bge-small-en-v1.5, run locally.
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DB_DIR = "./chroma_db"
DEFAULT_COLLECTION = "alert_triage"
DEFAULT_ATTACK_FILE = "corpus/attack/enterprise-attack.json"
DEFAULT_RUNBOOKS_DIR = "corpus/runbooks"


def load_technique_chunks(
    attack_file: Path, tokenizer: "PreTrainedTokenizerBase"
) -> list[Chunk]:
    """Parse the ATT&CK STIX bundle and chunk it per technique field.

    Args:
        attack_file: Path to the ATT&CK Enterprise STIX/JSON bundle.
        tokenizer: The local embedder's tokenizer, forwarded to
            `chunk.chunk_techniques` so it can size chunks by real token count.

    Returns:
        Technique chunks (see `chunk.chunk_techniques`).
    """
    techniques = stix.parse_techniques(attack_file)
    return chunk_techniques(techniques, tokenizer)


def load_runbook_chunks(
    runbooks_dir: Path, tokenizer: "PreTrainedTokenizerBase"
) -> list[Chunk]:
    """Load every markdown runbook and chunk each one.

    Args:
        runbooks_dir: Directory containing hand-written ``*.md`` runbooks.
        tokenizer: The local embedder's tokenizer, forwarded to
            `chunk.chunk_runbook` so it can size chunks by real token count.

    Returns:
        All runbook chunks, flattened across files. Empty if there are no
        runbooks yet (the author writes these over time).
    """
    chunks: list[Chunk] = []
    for md_path in sorted(runbooks_dir.glob("*.md")):
        text = md_path.read_text(encoding="utf-8")
        if not text.strip():
            # An empty runbook file is almost certainly a mistake — say so.
            raise ValueError(f"Runbook {md_path} is empty")
        chunks.extend(chunk_runbook(text, source=md_path.name, tokenizer=tokenizer))
    return chunks


def embed_and_persist(
    chunks: list[Chunk],
    embedder: SentenceTransformer,
    collection: chromadb.Collection,
    batch_size: int,
) -> None:
    """Embed chunk texts locally and upsert them into the Chroma collection.

    Embeddings are L2-normalized so the collection's cosine space behaves as
    expected for bge-small-en-v1.5. Writes with ``upsert`` keyed on the chunk id
    (idempotent within a run); orphan-free re-ingests are guaranteed by the
    caller rebuilding the collection from scratch, not by upsert alone.

    Args:
        chunks: Chunks to persist.
        embedder: Loaded local embedding model.
        collection: Target Chroma collection.
        batch_size: Number of chunks to embed/persist per batch.
    """
    if not chunks:
        raise ValueError("No chunks to ingest; check that the corpus is populated")

    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        embeddings = embedder.encode(
            [c.text for c in batch],
            normalize_embeddings=True,  # cosine space expects unit vectors
            show_progress_bar=False,
        )
        collection.upsert(
            ids=[c.id for c in batch],
            embeddings=[e.tolist() for e in embeddings],
            documents=[c.text for c in batch],
            metadatas=[c.metadata for c in batch],
        )


def ingest(
    attack_file: Path,
    runbooks_dir: Path,
    db_dir: Path,
    collection_name: str,
    embed_model: str,
    batch_size: int,
) -> None:
    """Run the full ingestion pipeline.

    Args:
        attack_file: Path to the ATT&CK STIX/JSON bundle.
        runbooks_dir: Directory of markdown runbooks.
        db_dir: Where to persist the Chroma database.
        collection_name: Name of the Chroma collection to write.
        embed_model: sentence-transformers model id (local).
        batch_size: Embedding/persist batch size.

    Raises:
        FileNotFoundError: If the ATT&CK file or runbooks directory is missing.
    """
    if not attack_file.is_file():
        raise FileNotFoundError(
            f"ATT&CK bundle not found: {attack_file}. "
            "Download the Enterprise STIX/JSON into corpus/attack/."
        )
    if not runbooks_dir.is_dir():
        raise FileNotFoundError(f"Runbooks directory not found: {runbooks_dir}")

    print(f"Loading embedding model {embed_model} (local)...")
    embedder = SentenceTransformer(embed_model)

    print(f"Parsing + chunking ATT&CK techniques from {attack_file}...")
    technique_chunks = load_technique_chunks(attack_file, embedder.tokenizer)
    print(f"  {len(technique_chunks)} technique chunks")

    print(f"Loading + chunking runbooks from {runbooks_dir}...")
    runbook_chunks = load_runbook_chunks(runbooks_dir, embedder.tokenizer)
    print(f"  {len(runbook_chunks)} runbook chunks")

    chunks = technique_chunks + runbook_chunks

    print(f"Persisting to Chroma at {db_dir} (collection '{collection_name}')...")
    client = chromadb.PersistentClient(path=str(db_dir))
    # Full rebuild: drop any existing collection first. Ingestion is a
    # from-scratch build, not an incremental upsert, so this guarantees a
    # re-ingest can't leave orphaned documents behind when chunk ids change
    # (e.g. the technique split-per-field scheme produces different ids than an
    # older one-chunk-per-technique run).
    try:
        client.delete_collection(name=collection_name)
    except NotFoundError:
        pass  # first run / nothing to drop
    collection = client.create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},  # bge-small-en-v1.5 is a cosine model
    )
    embed_and_persist(chunks, embedder, collection, batch_size)

    print(f"Done. {len(chunks)} chunks ingested into '{collection_name}'.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the ingestion script."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--attack-file",
        default=DEFAULT_ATTACK_FILE,
        help="Path to the ATT&CK Enterprise STIX/JSON bundle.",
    )
    parser.add_argument(
        "--runbooks-dir",
        default=DEFAULT_RUNBOOKS_DIR,
        help="Directory of markdown runbooks.",
    )
    parser.add_argument(
        "--db-dir",
        default=DEFAULT_DB_DIR,
        help="Directory to persist the Chroma database.",
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
        "--batch-size",
        type=int,
        default=64,
        help="Embedding/persist batch size.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    args = parse_args(argv)
    ingest(
        attack_file=Path(args.attack_file),
        runbooks_dir=Path(args.runbooks_dir),
        db_dir=Path(args.db_dir),
        collection_name=args.collection,
        embed_model=args.embed_model,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()
