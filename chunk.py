"""Corpus chunking — AUTHOR-OWNED.

The per-technique chunking strategy is the heart of this project's retrieval
quality and the author must be able to explain it in an interview. The function
bodies below are intentionally left unimplemented. The `Chunk` dataclass and the
function signatures define the contract that `ingest.py` depends on; fill in the
reasoning, don't change the shape without updating `ingest.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from stix import Technique


@dataclass(frozen=True)
class Chunk:
    """One retrievable unit of text plus the provenance needed to cite it.

    Attributes:
        id: Stable, unique id used as the Chroma document id. Must be
            deterministic across ingests so re-ingesting replaces (not
            duplicates) a chunk.
        text: The text that gets embedded and later shown to the analyst /
            handed to Claude as grounding.
        metadata: Provenance for citations. Persisted alongside the embedding so
            every verdict can point back to its source (e.g. source type,
            ATT&CK id, runbook filename). Values must be Chroma-compatible
            scalars (str/int/float/bool).
    """

    id: str
    text: str
    metadata: dict[str, str | int | float | bool]


def chunk_techniques(techniques: list[Technique]) -> list[Chunk]:
    """Chunk ATT&CK techniques, ONE chunk per technique.

    Contract (see CLAUDE.md): id + name + description + detection for a single
    technique are kept together as one chunk, so retrieval returns a complete,
    citable technique rather than a fragment.

    Args:
        techniques: Flattened techniques from `stix.parse_techniques`.

    Returns:
        One `Chunk` per technique, each carrying enough metadata to cite the
        technique by its ATT&CK id.
    """
    # TODO(author): implement the per-technique chunking strategy.
    raise NotImplementedError("chunk_techniques is author-owned; see CLAUDE.md")


def chunk_runbook(text: str, source: str) -> list[Chunk]:
    """Chunk a single runbook markdown document with a generic splitter.

    Contract (see CLAUDE.md): runbooks use a generic character splitter (no
    per-technique structure). Each resulting chunk must carry enough metadata to
    cite the originating runbook.

    Args:
        text: Raw markdown content of one runbook.
        source: Identifier for the runbook (e.g. its filename), used in chunk
            ids and citation metadata.

    Returns:
        One or more `Chunk` covering the runbook's content.
    """
    # TODO(author): implement generic character splitting for runbooks.
    raise NotImplementedError("chunk_runbook is author-owned; see CLAUDE.md")
