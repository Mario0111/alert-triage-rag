"""Retrieval merge + backfill (author-owned logic in triage/retrieve.py).

Each test builds a tiny synthetic collection with HAND-PICKED unit vectors,
so similarity outcomes are forced by construction: the collection is cosine
space, the query vector comes from FakeEmbedder, and distance is 1 - cos.
A chunk at [1,0,0] queried with [1,0,0] scores 0.0 (best); an orthogonal
chunk scores 1.0 (worst). No model, no randomness.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

import chromadb
import pytest

from tests.conftest import ChunkRecord, FakeEmbedder, runbook_chunk, technique_chunk
from triage.retrieve import retrieve

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

MakeCollection = Callable[[list[ChunkRecord]], chromadb.Collection]


def embedder(vector: list[float]) -> SentenceTransformer:
    """Present the fake to retrieve()'s typed signature.

    cast() is mypy-only (no runtime effect); the TYPE_CHECKING import keeps
    torch out of this module's runtime imports entirely.
    """
    return cast("SentenceTransformer", FakeEmbedder(vector))


# One technique split across three chunks (description in two pieces plus a
# detection part) and one two-piece runbook. Distances to the query [1,0,0]:
# desc:0 -> 0.0, det:0 -> 0.1, T1078 -> 0.3, desc:1 and both runbook chunks
# -> 1.0. So T1110's siblings deliberately do NOT all sit near the query:
# desc:1 can only appear in a result via completion-by-id, never via search.
CORPUS = [
    technique_chunk("T1110", "description", 0, 2, "alpha", [1.0, 0.0, 0.0]),
    technique_chunk("T1110", "description", 1, 2, "bravo", [0.0, 1.0, 0.0]),
    technique_chunk("T1110", "detection", 0, 1, "charlie", [0.9, 0.43589, 0.0]),
    technique_chunk("T1078", "description", 0, 1, "delta", [0.7, 0.71414, 0.0]),
    runbook_chunk("rb.md", 0, 2, "echo", [0.0, 0.0, 1.0]),
    runbook_chunk("rb.md", 1, 2, "foxtrot", [0.0, 0.19900, 0.98]),
]


def test_siblings_merge_into_one_document_per_citable_unit(
    make_collection: MakeCollection,
) -> None:
    collection = make_collection(CORPUS)
    results = retrieve("alert", collection, embedder([1.0, 0.0, 0.0]), k=2)

    similarity_results = [r for r in results if not r.backfilled]
    # Four technique chunks collapse into exactly two citable documents,
    # identified by attack_id (the citation unit), best match first.
    assert [r.id for r in similarity_results] == ["T1110", "T1078"]
    assert similarity_results[0].score < similarity_results[1].score


def test_merged_document_is_completed_and_ordered(
    make_collection: MakeCollection,
) -> None:
    collection = make_collection(CORPUS)
    results = retrieve("alert", collection, embedder([1.0, 0.0, 0.0]), k=1)

    doc = results[0]
    assert doc.id == "T1110"
    # description:1 sat at distance 1.0 — it was never a search hit, so its
    # presence proves the fetch-missing-siblings-by-id path ran. Order must
    # be description pieces in index order, then detection.
    positions = [doc.text.index(w) for w in ("alpha", "bravo", "charlie")]
    assert positions == sorted(positions)
    # The repeated per-chunk header collapses to a single copy.
    assert doc.text.count("(T1110)") == 1
    # Per-chunk bookkeeping must not leak into citation metadata.
    assert "part" not in doc.metadata
    assert "part_index" not in doc.metadata
    assert doc.metadata["attack_id"] == "T1110"


def test_runbook_backfilled_when_top_k_has_none(
    make_collection: MakeCollection,
) -> None:
    collection = make_collection(CORPUS)
    results = retrieve("alert", collection, embedder([1.0, 0.0, 0.0]), k=2)

    # Both runbook chunks sit at distance ~1.0, far outside the top-k — the
    # guarantee must APPEND the nearest runbook as a k+1'th, marked result.
    assert len(results) == 3
    backfilled = results[-1]
    assert backfilled.id == "rb.md"
    assert backfilled.backfilled is True
    # The backfilled runbook is completed like any other document.
    echo, foxtrot = backfilled.text.index("echo"), backfilled.text.index("foxtrot")
    assert echo < foxtrot


def test_runbook_that_earns_its_rank_is_not_backfilled(
    make_collection: MakeCollection,
) -> None:
    collection = make_collection(CORPUS)
    # Query the runbook corner of the space: rb.md places first on merit.
    results = retrieve("alert", collection, embedder([0.0, 0.0, 1.0]), k=2)

    assert results[0].id == "rb.md"
    # Backfill is a failure-case mechanism only — a naturally-ranked runbook
    # must keep its earned position and its unmarked status.
    assert all(r.backfilled is False for r in results)
    assert len(results) == 2


def test_overfetch_doubles_until_k_documents_emerge(
    make_collection: MakeCollection,
) -> None:
    # Six near chunks all belong to ONE runbook, so the initial over-fetch
    # (3k = 6 raw hits) yields a single document; only the doubling pass can
    # surface the second, far document.
    near = [
        runbook_chunk("big.md", i, 6, f"piece{i}", [1.0, 0.001 * i, 0.0])
        for i in range(6)
    ]
    far = [runbook_chunk("small.md", 0, 1, "tiny", [0.0, 0.0, 1.0])]
    collection = make_collection(near + far)

    results = retrieve("alert", collection, embedder([1.0, 0.0, 0.0]), k=2)
    assert [r.id for r in results] == ["big.md", "small.md"]


def test_returns_fewer_than_k_when_corpus_is_smaller(
    make_collection: MakeCollection,
) -> None:
    collection = make_collection(CORPUS)
    results = retrieve("alert", collection, embedder([0.0, 0.0, 1.0]), k=50)

    # The whole corpus merges into 3 documents; the loop must terminate and
    # return them all rather than spin looking for 50.
    assert {r.id for r in results} == {"T1110", "T1078", "rb.md"}


def test_rejects_nonpositive_k(make_collection: MakeCollection) -> None:
    collection = make_collection(CORPUS)
    with pytest.raises(ValueError, match="k must be positive"):
        retrieve("alert", collection, embedder([1.0, 0.0, 0.0]), k=0)


def test_rejects_empty_collection(make_collection: MakeCollection) -> None:
    collection = make_collection([])
    with pytest.raises(ValueError, match="empty"):
        retrieve("alert", collection, embedder([1.0, 0.0, 0.0]), k=5)
