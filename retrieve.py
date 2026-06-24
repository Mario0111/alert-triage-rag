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


def retrieve(
    alert_text: str,
    collection: "chromadb.Collection",
    embedder: "SentenceTransformer",
    k: int = 5,
) -> list[RetrievedChunk]:
    """Retrieve the top-k most relevant chunks for an alert description.

    Embeds the alert text with the same local model used at ingestion, queries
    the Chroma collection, and returns the top-k chunks ranked by relevance.

    Args:
        alert_text: The analyst's natural-language alert description.
        collection: The persisted Chroma collection to search.
        embedder: The local sentence-transformers model (bge-small-en-v1.5).
        k: Number of chunks to return.

    Returns:
        Up to ``k`` `RetrievedChunk`, best match first.
    """
    # TODO(author): implement the top-k retrieval logic.
    raise NotImplementedError("retrieve is author-owned; see CLAUDE.md")
