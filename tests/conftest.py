"""Shared fixtures for the test suite (pytest auto-loads this file).

Everything here exists to keep the tests hermetic — no network, no Anthropic
API calls, no embedding-model download:

- `FakeTokenizer` stands in for the bge tokenizer (1 word = 1 token), so
  chunking's token-budget math runs without loading the model.
- `FakeEmbedder` returns a caller-chosen vector, so retrieval runs without
  embedding anything. The COLLECTION side stores vectors we choose per chunk,
  which makes similarity ranking deterministic and readable in the tests.
- `make_collection` builds an in-memory Chroma collection (EphemeralClient:
  same query engine as the persisted store, nothing written to disk) with
  telemetry disabled so no test ever touches the network.
- Chunk-record builders mirror the ingest<->retrieve metadata contract
  (technique chunks carry source="ATT&CK" + attack_id + part/part_index/
  part_total; runbook chunks carry source=<filename> + chunk_index +
  part_total). Tests build tiny synthetic corpora out of these instead of
  running real ingestion.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import chromadb
import numpy as np
import pytest
from chromadb.config import Settings

from triage.schema import Citation, Severity, SourceType, TriageVerdict, Verdict


@pytest.fixture(autouse=True)
def _no_chroma_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable chromadb telemetry for every test via the environment.

    chromadb's ``Settings`` reads this env var on every construction, so the
    no-network rule holds even for code paths that build their own default
    ``Settings`` internally (e.g. ``query.load_collection``'s
    PersistentClient). Doing it by env rather than passing
    ``Settings(anonymized_telemetry=False)`` everywhere also avoids a chromadb
    gotcha: it caches one client "system" per store path and REFUSES to open
    the same path twice with unequal settings — a test that creates a store
    with explicit settings would make the production code's default-settings
    open of that same path blow up.
    """
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "False")


class FakeTokenizer:
    """Whitespace tokenizer: one word = one token, and words ARE the ids.

    Implements exactly the three operations chunk.py uses
    (``__call__ -> {"input_ids": [...]}``, ``encode``, ``decode``) so the
    token-budget splitting logic runs for real — only the tokenization itself
    is simplified. Using words as their own "ids" makes decode a trivial
    join and keeps hard-split output human-readable in failure messages.
    """

    def __call__(
        self, text: str, add_special_tokens: bool = True
    ) -> dict[str, list[str]]:
        return {"input_ids": self.encode(text, add_special_tokens)}

    def encode(self, text: str, add_special_tokens: bool = True) -> list[str]:
        return text.split()

    def decode(self, ids: list[str]) -> str:
        return " ".join(ids)


@pytest.fixture
def fake_tokenizer() -> FakeTokenizer:
    return FakeTokenizer()


class FakeEmbedder:
    """Embedder stand-in: always returns the vector it was built with.

    retrieve() only calls ``encode(...)[0].tolist()``; the test chooses the
    query vector directly, which is the point — similarity outcomes are set
    up by construction, not computed by a model.
    """

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        # The real SentenceTransformer exposes its tokenizer as an attribute;
        # query.triage_alert reads it for the embed-window guard
        # (rewrite.ensure_embeddable), so the fake carries one too.
        self.tokenizer = FakeTokenizer()

    def encode(self, texts: list[str], **_: Any) -> list[np.ndarray]:
        return [np.array(self.vector, dtype=np.float32) for _ in texts]


@dataclass(frozen=True)
class ChunkRecord:
    """One synthetic stored chunk: id + text + metadata + its embedding."""

    id: str
    text: str
    metadata: dict[str, str | int | float | bool]
    embedding: list[float]


def technique_chunk(
    attack_id: str,
    part: str,
    part_index: int,
    part_total: int,
    body: str,
    embedding: list[float],
    name: str = "Some Technique",
) -> ChunkRecord:
    """Build a stored technique chunk exactly as ingest would persist it.

    The text layout (header, blank line, labeled body) matters: retrieval's
    `_split_stored_text` requires the ``\\n\\n`` separator that
    chunk._assemble guarantees.
    """
    header = f"{name} ({attack_id})\nPLATFORMS: Windows\nTACTICS  : execution"
    return ChunkRecord(
        id=f"Technique:{attack_id}:{part}:{part_index}",
        text=(
            f"{header}\n\n{part.capitalize()} "
            f"({part_index + 1}/{part_total}):\n{body}"
        ),
        metadata={
            "attack_id": attack_id,
            "name": name,
            "platforms": "Windows",
            "tactics": "execution",
            "source": "ATT&CK",
            "part": part,
            "part_index": part_index,
            "part_total": part_total,
        },
        embedding=embedding,
    )


def runbook_chunk(
    source: str,
    chunk_index: int,
    part_total: int,
    body: str,
    embedding: list[float],
) -> ChunkRecord:
    """Build a stored runbook chunk exactly as ingest would persist it."""
    header = f"Runbook: {source} [{source}]"
    return ChunkRecord(
        id=f"Runbook:{source}:{chunk_index}",
        text=(
            f"{header}\n\nPart ({chunk_index + 1}/{part_total}):\n{body}"
        ),
        metadata={
            "source": source,
            "chunk_index": chunk_index,
            "part_total": part_total,
        },
        embedding=embedding,
    )


@pytest.fixture
def make_collection() -> Callable[[list[ChunkRecord]], chromadb.Collection]:
    """Factory fixture: synthetic chunks -> in-memory cosine collection.

    A factory (fixture returning a function) rather than a ready collection,
    because each test wants a different corpus. anonymized_telemetry=False
    keeps chromadb from calling home — the suite's no-network rule is real.
    """

    def _make(records: list[ChunkRecord]) -> chromadb.Collection:
        client = chromadb.EphemeralClient(
            settings=Settings(anonymized_telemetry=False)
        )
        # EphemeralClient is cached per process — every call returns the SAME
        # in-memory store, so a fixed collection name would collide across
        # tests. A unique name per call restores test isolation.
        collection = client.create_collection(
            name=f"test-{uuid4().hex}", metadata={"hnsw:space": "cosine"}
        )
        if records:
            # The annotation matches one arm of chromadb's accepted union
            # exactly; a bare list[list[float]] fails because list is
            # invariant in its element type.
            vectors: list[Sequence[float] | Sequence[int]] = [
                r.embedding for r in records
            ]
            collection.add(
                ids=[r.id for r in records],
                embeddings=vectors,
                documents=[r.text for r in records],
                metadatas=[r.metadata for r in records],
            )
        return collection

    return _make


@dataclass
class FakeMessages:
    """Scripted stand-in for client.messages: returns queued responses.

    ``parse`` pops the next queued item — an exception instance is raised
    (to simulate the SDK surfacing a Pydantic ValidationError), anything
    else is returned. Every call's kwargs are recorded so tests can assert
    on what the model was actually sent (e.g. the retry feedback).
    """

    queue: list[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class FakeAnthropicClient:
    messages: FakeMessages


@dataclass
class FakeParseResponse:
    """The two attributes generate_verdict reads off a parse() response."""

    parsed_output: TriageVerdict | None
    stop_reason: str = "end_turn"


def make_verdict(
    citations: list[tuple[str, SourceType]],
    mitre_techniques: list[str] | None = None,
) -> TriageVerdict:
    """Build a schema-valid verdict citing the given (id, source_type) pairs.

    Schema-valid is the interesting part: grounding failures in query.py are
    only reachable with verdicts that already PASS schema.py, e.g. a citation
    to a plausible-but-never-retrieved source id.
    """
    return TriageVerdict(
        verdict=Verdict.TRUE_POSITIVE,
        severity=Severity.HIGH,
        confidence=0.9,
        summary="Test verdict.",
        mitre_techniques=(
            mitre_techniques
            if mitre_techniques is not None
            else [cid for cid, kind in citations if kind is SourceType.ATTACK]
        ),
        recommended_actions=["isolate host"],
        citations=[
            Citation(chunk_id=cid, source_type=kind, ref=cid)
            for cid, kind in citations
        ],
    )
