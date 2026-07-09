"""Ingestion pipeline: corpus -> chunk -> embed -> persist to Chroma.

Run once, or whenever the corpus changes. This module is the plumbing that wires
the pieces together; the actual chunking lives in `chunk.py` (author-owned).

    ATT&CK bundle (auto-downloaded)  --stix.parse_techniques-->  Technique
                                     --chunk.chunk_techniques-->  Chunk
    packaged runbooks (*.md)         --chunk.chunk_runbook----->  Chunk
                                     --bge-small-en-v1.5-------->  embeddings
                                     --chromadb----------------->  data dir

All default locations resolve through `paths.py` (per-user data directory,
``TRIAGE_DATA_DIR`` override for dev mode). The ATT&CK bundle is fetched into
the data dir on first run — an installed app can't assume a repo checkout with
a pre-downloaded corpus. Embeddings are computed locally (no API calls) and
the collection uses cosine distance, which is what bge-small-en-v1.5 is
trained for.
"""

from __future__ import annotations

import argparse
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import chromadb
from chromadb.errors import NotFoundError
from sentence_transformers import SentenceTransformer

from . import paths, stix
from .chunk import Chunk, chunk_runbook, chunk_techniques
from .fingerprint import FINGERPRINT_KEY, build_fingerprint

if TYPE_CHECKING:
    from transformers import PreTrainedTokenizerBase

# Fixed stack (see CLAUDE.md). bge-small-en-v1.5, run locally.
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_COLLECTION = "alert_triage"

# Pinned to Enterprise ATT&CK v19.1 — the release this project's chunking and
# runbooks were written against. A moving "latest" URL could silently change
# the corpus under the pipeline; refreshes should be a deliberate act
# (--refresh-attack after bumping this pin).
ATTACK_BUNDLE_URL = (
    "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/"
    "master/enterprise-attack/enterprise-attack-19.1.json"
)


def fetch_attack_bundle(dest: Path, url: str = ATTACK_BUNDLE_URL) -> None:
    """Download the ATT&CK Enterprise STIX bundle (~51 MB) to ``dest``.

    Streams to a ``.part`` file and renames only on success, so an interrupted
    download can never leave a truncated file that a later run mistakes for a
    valid corpus (stix.parse_techniques would fail loudly on it, but failing
    at download time with the real cause is clearer).

    Args:
        dest: Final path for the bundle; parent directories are created.
        url: Source URL (pinned release by default).

    Raises:
        RuntimeError: On any network/HTTP failure, with the URL in the message.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    print(f"Downloading ATT&CK bundle (~51 MB)\n  from {url}\n  to   {dest}")
    try:
        with urllib.request.urlopen(url) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
    except urllib.error.URLError as exc:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"Failed to download the ATT&CK bundle from {url}: {exc}. "
            "Check the network, or place the bundle manually and pass "
            "--attack-file."
        ) from exc
    partial.replace(dest)
    print(f"  done ({dest.stat().st_size / 1_000_000:.0f} MB).")


def load_technique_chunks(
    attack_file: Path, tokenizer: PreTrainedTokenizerBase
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
    runbooks_dir: Path, tokenizer: PreTrainedTokenizerBase
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
    refresh_attack: bool = False,
) -> None:
    """Run the full ingestion pipeline.

    Args:
        attack_file: Path to the ATT&CK STIX/JSON bundle. Downloaded from the
            pinned release URL when missing.
        runbooks_dir: Directory of markdown runbooks.
        db_dir: Where to persist the Chroma database.
        collection_name: Name of the Chroma collection to write.
        embed_model: sentence-transformers model id (local).
        batch_size: Embedding/persist batch size.
        refresh_attack: Re-download the ATT&CK bundle even if present.

    Raises:
        RuntimeError: If the ATT&CK bundle download fails.
        FileNotFoundError: If the runbooks directory is missing.
    """
    if refresh_attack or not attack_file.is_file():
        fetch_attack_bundle(attack_file)
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
        metadata={
            "hnsw:space": "cosine",  # bge-small-en-v1.5 is a cosine model
            # Staleness fingerprint (see fingerprint.py): records the code +
            # corpus this store was built from, so query-side loading can
            # refuse to serve from an index the current code no longer matches.
            FINGERPRINT_KEY: build_fingerprint(
                embed_model=embed_model,
                runbooks_dir=runbooks_dir,
                attack_source=ATTACK_BUNDLE_URL,
            ),
        },
    )
    embed_and_persist(chunks, embedder, collection, batch_size)

    print(f"Done. {len(chunks)} chunks ingested into '{collection_name}'.")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the ingest arguments to ``parser``.

    Shared between the standalone module entry point and the ``triage ingest``
    subcommand (see cli.py), so both expose exactly the same flags. Defaults
    are resolved here, at parser-build time, so ``TRIAGE_DATA_DIR`` set in the
    invoking shell is honored.
    """
    parser.add_argument(
        "--attack-file",
        type=Path,
        default=paths.attack_file(),
        help="Path to the ATT&CK Enterprise STIX/JSON bundle "
        "(auto-downloaded here when missing).",
    )
    parser.add_argument(
        "--refresh-attack",
        action="store_true",
        help="Re-download the ATT&CK bundle even if it is already present.",
    )
    parser.add_argument(
        "--runbooks-dir",
        type=Path,
        default=paths.packaged_runbooks_dir(),
        help="Directory of markdown runbooks (defaults to the runbooks "
        "shipped inside the package).",
    )
    parser.add_argument(
        "--db-dir",
        type=Path,
        default=paths.chroma_dir(),
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


def run(args: argparse.Namespace) -> None:
    """Execute ingestion from parsed arguments (the subcommand handler)."""
    ingest(
        attack_file=args.attack_file,
        runbooks_dir=args.runbooks_dir,
        db_dir=args.db_dir,
        collection_name=args.collection,
        embed_model=args.embed_model,
        batch_size=args.batch_size,
        refresh_attack=args.refresh_attack,
    )


def main(argv: list[str] | None = None) -> None:
    """Standalone entry point (`python -m triage.ingest`)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
