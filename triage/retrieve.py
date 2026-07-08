"""Top-k retrieval: embed alert -> over-fetch -> merge siblings -> complete documents.

The corpus is stored as sub-window fragments (see `chunk.py`), but the unit an
analyst cites is a whole technique or runbook. This module is where the
"one complete, citable document per result" guarantee from CLAUDE.md is
enforced. The design decisions, in pipeline order:

1. **Embed like ingestion, plus the BGE query instruction.** Same local model,
   same L2 normalization (the collection is cosine-space with unit vectors).
   bge-small-en-v1.5 was trained asymmetrically — queries prefixed with an
   instruction, passages bare — and ingestion stored passages bare, so the
   prefix goes on the query side only. It mostly helps terse queries
   ("mimikatz on DC"), and adding/removing it never requires re-ingesting.

2. **Over-fetch, iteratively.** Sibling chunks of one document cluster in
   embedding space, so k raw hits can merge into fewer than k documents.
   Start at k' = 3k and double until merging yields k documents or the whole
   collection has been fetched. The expensive step (embedding the alert) runs
   ONCE; the loop only re-runs a local HNSW index query with the same vector,
   so iterating is cheap and scales with corpus size.

3. **Merge by citable identity, score by best chunk.** Techniques group on
   ``attack_id``, runbooks on ``source`` (metadata is the ingest<->retrieve
   contract; chunk-id strings are storage keys, not an API). A merged document
   scores as its best sibling (min cosine distance): one strong passage is
   what makes a document relevant, and averaging would punish long documents
   for their weaker fragments (the classic MaxP argument).

4. **Complete every returned document.** A hit on one fragment surfaces the
   whole document: missing siblings are fetched directly by their
   deterministic ids (``Technique:{attack_id}:{part}:{idx}``,
   ``Runbook:{source}:{idx}``) — a key lookup, not a metadata scan, and it
   fails loudly if ingest and retrieve ever disagree about the id scheme.
   Without this, a citation could say "T1055" while the grounding text has
   holes, and citations stop being trustworthy.

5. **Reassemble without duplication.** Chunks carry no overlap precisely so
   pieces concatenate cleanly: one header (each stored chunk repeats it only
   because chunks embed independently), then the labeled bodies in explicit
   order — description before detection, mirroring how ATT&CK presents a
   technique and front-loading identification before detection reasoning.

6. **Guarantee a runbook candidate (backfill, not quota).** Runbooks encode
   the triage procedure, but a handful of them compete against hundreds of
   techniques in one similarity pool, so a stylistic mismatch between alert
   prose and runbook prose can crowd them out entirely. When the mixed top-k
   contains no runbook, the single nearest runbook is APPENDED and marked
   ``backfilled`` — never reserved a slot: alerts that match several runbooks
   keep all of them, and only the failure case changes shape. Whether the
   appended runbook actually applies is judged by the generation stage, which
   reads both texts; a cosine threshold can't make that call (observed
   runbook margins sit within embedding noise).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import chromadb
    from sentence_transformers import SentenceTransformer

# The retrieval instruction bge-small-en-v1.5 was trained with. Queries get it,
# passages don't — which matches what ingest.py persisted.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Initial raw-chunk over-fetch per requested result. 3x is a bound you can
# reason about (under-delivery needs the top 3k chunks concentrated in < k
# documents) and the iterative doubling below covers the rare miss.
_OVERFETCH_FACTOR = 3

# Explicit reassembly order for technique parts. A plain (part, part_index)
# tuple sort happens to work because "description" < "detection" alphabetically
# — but that's luck, not intent, and an unknown part should fail loudly rather
# than land somewhere arbitrary in the grounding text.
_PART_ORDER = {"description": 0, "detection": 1}

# Per-chunk bookkeeping that stops making sense once siblings are merged back
# into one document; everything else in the metadata is document-level
# provenance and is kept for citation.
_CHUNK_ONLY_KEYS = frozenset({"part", "part_index", "part_total", "chunk_index"})

Metadata = dict[str, str | int | float | bool]


@dataclass(frozen=True)
class RetrievedChunk:
    """One citable document returned by retrieval, reassembled from its chunks.

    Named "chunk" for historical symmetry with ingestion, but post-merge each
    instance is a complete document: a whole ATT&CK technique or a whole
    runbook.

    Attributes:
        id: The citable identifier — the ATT&CK id (e.g. ``"T1055"``) for
            techniques, the runbook filename for runbooks. Stable across runs
            regardless of which fragment matched.
        text: The full reassembled document text, used as grounding.
        metadata: Document-level provenance (mirrors the persisted chunk
            metadata minus per-chunk bookkeeping).
        score: Cosine DISTANCE of the document's best-matching chunk, exactly
            as Chroma returns it for an ``hnsw:space="cosine"`` collection:
            ``1 - cosine_similarity``, so LOWER is better. Passed through
            unconverted — one source of truth, no translation layer to get a
            sign wrong in.
        backfilled: True when this runbook did not place in the similarity
            top-k and was appended by the runbook guarantee (design note 6).
            The grounding prompt discloses this so the model judges the
            runbook's relevance instead of inferring it from mere presence.
    """

    id: str
    text: str
    metadata: Metadata
    score: float
    backfilled: bool = False


@dataclass
class _DocGroup:
    """Raw query hits grouped under one citable document, best-first."""

    kind: str  # "technique" | "runbook"
    key: str  # attack_id / runbook filename — becomes RetrievedChunk.id
    best_distance: float
    pieces: list[tuple[Metadata, str]] = field(default_factory=list)


def _merge_key(metadata: Metadata) -> tuple[str, str]:
    """Identify the citable document a chunk belongs to.

    The merge key must be the same unit as the citation, and it comes from
    metadata — the declared contract between ingest and retrieve — not from
    parsing chunk-id strings, which are an ingest-internal storage format.

    Args:
        metadata: A persisted chunk's metadata.

    Returns:
        ``("technique", attack_id)`` or ``("runbook", filename)``.

    Raises:
        KeyError: If the metadata lacks the keys the ingest contract promises.
    """
    if metadata["source"] == "ATT&CK":
        return "technique", str(metadata["attack_id"])
    return "runbook", str(metadata["source"])


def _group_hits(raw: dict) -> list[_DocGroup]:
    """Group raw Chroma hits by citable document, preserving best-first order.

    Chroma returns hits by ascending distance, so the first sibling seen for a
    document is its best chunk — insertion order into the dict IS best-score
    order, with no re-sort to disagree with the store over float ties.

    Args:
        raw: The dict returned by ``collection.query`` for a single query
            embedding (documents, metadatas and distances included).

    Returns:
        One `_DocGroup` per distinct document, best match first.
    """
    groups: dict[tuple[str, str], _DocGroup] = {}
    for text, meta, dist in zip(
        raw["documents"][0], raw["metadatas"][0], raw["distances"][0]
    ):
        kind, key = _merge_key(meta)
        group = groups.get((kind, key))
        if group is None:
            groups[(kind, key)] = _DocGroup(kind, key, dist, [(meta, text)])
        else:
            group.pieces.append((meta, text))
            # First-seen is already the minimum; kept explicit so the scoring
            # rule survives any future reordering of this loop.
            group.best_distance = min(group.best_distance, dist)
    return list(groups.values())


def _get_by_ids(
    collection: "chromadb.Collection", ids: list[str], *, required: bool
) -> dict[str, tuple[str, Metadata]]:
    """Fetch chunks by id, optionally failing loudly on any miss.

    Args:
        ids: Chunk ids to fetch (deterministic, reconstructed from metadata).
        required: When True, every id must exist — a miss means ingest and
            retrieve disagree about the id scheme or the corpus changed under
            us, which is a bug to surface, not to paper over.

    Returns:
        Mapping of id -> (stored text, metadata) for the ids that exist.

    Raises:
        LookupError: If ``required`` and any id is missing.
    """
    if not ids:
        return {}
    res = collection.get(ids=ids, include=["documents", "metadatas"])
    found = {
        cid: (doc, meta)
        for cid, doc, meta in zip(res["ids"], res["documents"], res["metadatas"])
    }
    if required:
        missing = sorted(set(ids) - set(found))
        if missing:
            raise LookupError(
                f"Chunk ids missing from the collection: {missing}. The id "
                "scheme in retrieve.py disagrees with what ingest.py persisted "
                "— re-run ingest.py or reconcile the schemes."
            )
    return found


def _split_stored_text(text: str) -> tuple[str, str]:
    """Split a stored chunk into (header, labeled body).

    `chunk._assemble` guarantees exactly one blank line between the
    self-identifying header and the part-labeled body; the header is repeated
    on every stored chunk only because chunks embed independently, so the
    merged document keeps a single copy.
    """
    header, sep, body = text.partition("\n\n")
    if not sep:
        raise ValueError(
            "Stored chunk text has no header/body separator; it was not "
            "produced by chunk.py's _assemble()."
        )
    return header, body


def _complete_technique(
    group: _DocGroup, collection: "chromadb.Collection"
) -> list[str]:
    """Recover ALL of a technique's chunks and return them in citation order.

    Retrieval may have surfaced any subset (e.g. only ``detection:1``). Each
    retrieved sibling reveals its own part's ``part_total``; for a part with no
    retrieved sibling at all, piece 0 is probed by deterministic id — a miss
    there is legitimate (detection is optional), but once a part is known to
    exist, every one of its pieces must be found.

    Args:
        group: The document's retrieved pieces.
        collection: The Chroma collection, for direct id lookups.

    Returns:
        Stored chunk texts ordered by (`_PART_ORDER`, part_index).

    Raises:
        ValueError: On a part name `_PART_ORDER` doesn't know — fail loudly
            rather than order the grounding text by alphabet accident.
        LookupError: If a known-to-exist sibling is missing (id-scheme drift).
    """
    held: dict[tuple[str, int], str] = {}
    totals: dict[str, int] = {}
    for meta, text in group.pieces:
        part = str(meta["part"])
        if part not in _PART_ORDER:
            raise ValueError(
                f"Unknown technique part {part!r} on {group.key}; add it to "
                "_PART_ORDER so its position in the merged document is chosen, "
                "not alphabetical."
            )
        held[(part, int(meta["part_index"]))] = text
        totals[part] = int(meta["part_total"])

    known_missing: list[str] = []
    probes: list[str] = []
    for part in _PART_ORDER:
        if part in totals:
            known_missing.extend(
                f"Technique:{group.key}:{part}:{idx}"
                for idx in range(totals[part])
                if (part, idx) not in held
            )
        else:
            probes.append(f"Technique:{group.key}:{part}:0")

    fetched = _get_by_ids(collection, known_missing, required=True)
    fetched.update(_get_by_ids(collection, probes, required=False))
    for text, meta in fetched.values():
        held[(str(meta["part"]), int(meta["part_index"]))] = text
        totals[str(meta["part"])] = int(meta["part_total"])

    # A successful probe revealed how many pieces its part has; fetch the rest.
    remainder = [
        f"Technique:{group.key}:{part}:{idx}"
        for part, total in totals.items()
        for idx in range(total)
        if (part, idx) not in held
    ]
    for text, meta in _get_by_ids(collection, remainder, required=True).values():
        held[(str(meta["part"]), int(meta["part_index"]))] = text

    ordered = sorted(held, key=lambda pi: (_PART_ORDER[pi[0]], pi[1]))
    return [held[key] for key in ordered]


def _complete_runbook(
    group: _DocGroup, collection: "chromadb.Collection"
) -> list[str]:
    """Recover ALL of a runbook's chunks and return them in ``chunk_index`` order.

    Simpler than techniques: a runbook is one linear sequence and every chunk's
    ``part_total`` covers the whole document, so any retrieved piece reveals
    the complete deterministic id set.

    Args:
        group: The document's retrieved pieces.
        collection: The Chroma collection, for direct id lookups.

    Returns:
        Stored chunk texts ordered by ``chunk_index``.

    Raises:
        LookupError: If a sibling id is missing (id-scheme drift).
    """
    held: dict[int, str] = {}
    total = 0
    for meta, text in group.pieces:
        held[int(meta["chunk_index"])] = text
        total = int(meta["part_total"])

    missing = [
        f"Runbook:{group.key}:{idx}" for idx in range(total) if idx not in held
    ]
    for text, meta in _get_by_ids(collection, missing, required=True).values():
        held[int(meta["chunk_index"])] = text

    return [held[idx] for idx in sorted(held)]


def _assemble_document(
    group: _DocGroup,
    collection: "chromadb.Collection",
    backfilled: bool = False,
) -> RetrievedChunk:
    """Turn a group of raw hits into one complete, citable result.

    Completes the document (fetching non-retrieved siblings), keeps a single
    header, and joins the part-labeled bodies in order. No de-duplication is
    needed at the seams because chunks were split with no overlap — merge, not
    overlap, is this project's context mechanism.

    Args:
        group: The document's retrieved pieces, with its best distance.
        collection: The Chroma collection, for direct id lookups.
        backfilled: Whether this document was appended by the runbook
            guarantee rather than placing in the similarity top-k.

    Returns:
        The reassembled document as a `RetrievedChunk`.
    """
    if group.kind == "technique":
        texts = _complete_technique(group, collection)
    else:
        texts = _complete_runbook(group, collection)

    header, first_body = _split_stored_text(texts[0])
    bodies = [first_body] + [_split_stored_text(t)[1] for t in texts[1:]]

    # Document-level provenance only; per-chunk indices are meaningless after
    # the merge and would be misleading in a citation.
    doc_meta: Metadata = {
        k: v for k, v in group.pieces[0][0].items() if k not in _CHUNK_ONLY_KEYS
    }

    return RetrievedChunk(
        id=group.key,
        text=header + "\n\n" + "\n\n".join(bodies),
        metadata=doc_meta,
        score=group.best_distance,
        backfilled=backfilled,
    )


def _nearest_runbook(
    query_vec: list[float], collection: "chromadb.Collection"
) -> RetrievedChunk | None:
    """Fetch the single nearest runbook document for the backfill guarantee.

    A single ``n_results=1`` hit under a runbook-only filter is enough:
    Chroma returns ascending distance and a document scores as its best
    chunk, so the top raw hit already identifies the best runbook —
    `_assemble_document` then completes it from siblings. No over-fetch loop.

    The filter reuses the ingest<->retrieve metadata contract: technique
    chunks carry ``source: "ATT&CK"``, so anything else is a runbook.

    Args:
        query_vec: The already-computed query embedding — reused, the alert
            is never embedded twice.
        collection: The Chroma collection.

    Returns:
        The nearest runbook, marked ``backfilled=True``; None when the
        collection contains no runbooks at all, in which case the guarantee
        cannot hold and the caller returns techniques only.
    """
    raw = collection.query(
        query_embeddings=[query_vec],
        n_results=1,
        where={"source": {"$ne": "ATT&CK"}},
        include=["documents", "metadatas", "distances"],
    )
    groups = _group_hits(raw)
    if not groups:
        return None
    return _assemble_document(groups[0], collection, backfilled=True)


def retrieve(
    alert_text: str,
    collection: "chromadb.Collection",
    embedder: "SentenceTransformer",
    k: int = 5,
) -> list[RetrievedChunk]:
    """Retrieve the top-k most relevant citable documents for an alert.

    Embeds the alert once (same model and normalization as ingestion, plus the
    BGE query instruction), then over-fetches raw chunks and merges siblings of
    the same document until k distinct documents are found or the collection is
    exhausted. Each returned document is complete — non-retrieved siblings are
    fetched by deterministic id — and scored by its best chunk's cosine
    distance (lower is better).

    Args:
        alert_text: The analyst's natural-language alert description (already
            rewritten/size-guarded by the caller, see `query.py`).
        collection: The persisted Chroma collection produced by `ingest.py`.
        embedder: The local sentence-transformers model (bge-small-en-v1.5).
        k: Number of distinct documents to return.

    Returns:
        Up to ``k`` `RetrievedChunk`, best match first, one per document.
        Fewer than ``k`` only when the whole corpus merges into fewer
        documents. When the top-k contains no runbook, the nearest runbook
        is appended as one extra result marked ``backfilled`` (design
        note 6), so callers may receive ``k + 1`` documents.

    Raises:
        ValueError: If ``k`` is not positive or the collection is empty.
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    total_chunks = collection.count()
    if total_chunks == 0:
        raise ValueError("Collection is empty — run ingest.py before querying.")

    query_vec = embedder.encode(
        [BGE_QUERY_PREFIX + alert_text],
        normalize_embeddings=True,  # the collection holds unit vectors
        show_progress_bar=False,
    )[0].tolist()

    # Embed once, then iterate only the local HNSW query: each pass asks for a
    # superset of the last, so regrouping from scratch is simpler than
    # incremental bookkeeping and costs nothing at this scale. The cap at
    # total_chunks is the termination guard — without it, a corpus that merges
    # into fewer than k documents would loop forever.
    n_results = min(_OVERFETCH_FACTOR * k, total_chunks)
    while True:
        raw = collection.query(
            query_embeddings=[query_vec],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
        groups = _group_hits(raw)
        if len(groups) >= k or n_results >= total_chunks:
            break
        n_results = min(n_results * 2, total_chunks)

    top = groups[:k]
    results = [_assemble_document(group, collection) for group in top]

    # The runbook guarantee (design note 6): backfill only on the failure
    # case, so naturally-matching runbooks — including several at once —
    # keep their earned ranks and existing behavior is untouched.
    if all(group.kind != "runbook" for group in top):
        runbook = _nearest_runbook(query_vec, collection)
        if runbook is not None:
            results.append(runbook)

    return results
