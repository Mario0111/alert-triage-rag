"""Top-k retrieval — AUTHOR-OWNED.

How the alert text is turned into a query and how the top-k chunks are selected
is a core part of the system the author must be able to explain. The signature
and `RetrievedChunk` shape below define the contract; the body is left for the
author. `query.py` depends on this returning citable results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import chromadb
    from sentence_transformers import SentenceTransformer


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk returned by retrieval, with its provenance and match score.

    Attributes:
        id: The chunk / Chroma document id.
        text: The chunk text, to be shown and used as grounding.
        metadata: Provenance for citation (mirrors the persisted chunk metadata).
        score: Similarity/distance score for this match. Document the exact
            meaning (cosine distance vs. similarity) in the implementation so
            ranking is unambiguous.
    """

    id: str
    text: str
    metadata: dict[str, str | int | float | bool]
    score: float


def _merge_by_document(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Merge sibling chunks of the same source document into one result.

    Both corpus types are split into sub-window chunks at ingest (see
    `chunk.py`), so a hit on any one chunk should surface the WHOLE document,
    and siblings shouldn't consume multiple top-k slots. The merge key depends
    on the source type:

    - Techniques: group by ``attack_id``, reassemble in ``(part, part_index)``
      order (description parts, then detection parts).
    - Runbooks: group by ``source`` (the runbook filename), reassemble in
      ``chunk_index`` order.

    Collapsing each group to a single `RetrievedChunk` is what preserves the
    "complete, citable document" guarantee from CLAUDE.md at retrieval time
    instead of at chunk time.

    Args:
        chunks: Raw per-chunk matches from the Chroma query, best-scored first.

    Returns:
        Deduplicated results, one per source document, preserving best-match
        order.
    """
    # TODO(author): decide how to combine sibling scores (best? mean?), how to
    # order/join the reassembled text, and how ``part_total`` is used to tell
    # whether all of a document's pieces were recovered (non-retrieved siblings
    # can be fetched from Chroma by id, since chunk ids are deterministic).
    raise NotImplementedError("merge-by-document is author-owned; see CLAUDE.md")


def retrieve(
    alert_text: str,
    collection: "chromadb.Collection",
    embedder: "SentenceTransformer",
    k: int = 5,
) -> list[RetrievedChunk]:
    """Retrieve the top-k most relevant chunks for an alert description.

    Embeds the alert text with the same local model used at ingestion, queries
    the Chroma collection, and returns the top-k results ranked by relevance.

    Because both techniques and runbooks are split across multiple chunks (see
    `chunk.py`), the raw query is expected to OVER-FETCH (some k' > k) and then
    collapse sibling chunks via ``_merge_by_document`` before trimming to ``k``
    — otherwise two chunks of the same document could occupy two of the k
    slots.

    Args:
        alert_text: The analyst's natural-language alert description.
        collection: The persisted Chroma collection to search.
        embedder: The local sentence-transformers model (bge-small-en-v1.5).
        k: Number of distinct results to return.

    Returns:
        Up to ``k`` `RetrievedChunk`, best match first, one per technique.
    """
    # TODO(author): implement the top-k retrieval logic:
    #   1. embed alert_text (same model/normalization as ingestion),
    #   2. query Chroma for k' > k raw chunks,
    #   3. _merge_by_document(...) to reassemble techniques/runbooks,
    #   4. trim to k.
    raise NotImplementedError("retrieve is author-owned; see CLAUDE.md")
