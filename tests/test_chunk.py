"""Chunking behavior (author-owned logic in triage/chunk.py — exercised, not owned).

Two splitting paths exist by design: the real-token path (tokenizer passed,
used by ingestion) and the char-proxy fallback (no tokenizer — quick runs and
these tests). Both are covered; the token path runs against the whitespace
FakeTokenizer so the budget arithmetic is executed without loading bge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from tests.conftest import FakeTokenizer
from triage.chunk import (
    EMBED_MAX_TOKENS,
    TECHNIQUE_FIELD_CHUNK_CHARS,
    chunk_runbook,
    chunk_techniques,
)
from triage.stix import Technique

if TYPE_CHECKING:
    from transformers import PreTrainedTokenizerBase


def as_tokenizer(fake: FakeTokenizer) -> PreTrainedTokenizerBase:
    """mypy-only cast so the fake fits chunk_techniques' typed signature."""
    return cast("PreTrainedTokenizerBase", fake)


def make_technique(
    attack_id: str = "T1059",
    description: str = "Adversaries may abuse command interpreters.",
    detection: str = "Monitor process creation events.",
) -> Technique:
    return Technique(
        attack_id=attack_id,
        name="Command and Scripting Interpreter",
        description=description,
        detection=detection,
        platforms=("Windows", "Linux"),
        tactics=("execution",),
    )


def long_prose(n_sentences: int, words_per_sentence: int = 12) -> str:
    """Deterministic multi-sentence filler that the sentence splitter can cut."""
    sentence = " ".join(f"word{i}" for i in range(words_per_sentence)) + "."
    return " ".join(sentence for _ in range(n_sentences))


# --- per-field strategy -----------------------------------------------------


def test_description_and_detection_become_separate_chunks() -> None:
    chunks = chunk_techniques([make_technique()])
    parts = {c.metadata["part"] for c in chunks}
    assert parts == {"description", "detection"}
    # The whole point of per-field chunking: every piece of one technique
    # carries the same attack_id, the reassembly key retrieval merges on.
    assert {c.metadata["attack_id"] for c in chunks} == {"T1059"}


def test_empty_detection_yields_no_detection_chunk() -> None:
    chunks = chunk_techniques([make_technique(detection="   ")])
    assert [c.metadata["part"] for c in chunks] == ["description"]


def test_chunk_ids_are_deterministic_across_runs() -> None:
    # Chroma upserts are keyed on the id: a re-ingest must REPLACE chunks,
    # not duplicate them, so two runs over the same corpus must agree.
    first = [c.id for c in chunk_techniques([make_technique()])]
    second = [c.id for c in chunk_techniques([make_technique()])]
    assert first == second
    assert first[0] == "Technique:T1059:description:0"


def test_every_chunk_is_self_identifying() -> None:
    chunks = chunk_techniques([make_technique()])
    for chunk in chunks:
        header, sep, body = chunk.text.partition("\n\n")
        # retrieve._split_stored_text depends on this exact layout.
        assert sep, "chunk text must contain the header/body separator"
        assert "T1059" in header
        assert body


# --- char-proxy fallback splitting (no tokenizer) ----------------------------


def test_long_field_splits_into_indexed_siblings() -> None:
    technique = make_technique(
        description=long_prose(n_sentences=80), detection=""
    )
    chunks = chunk_techniques([technique])

    assert len(chunks) > 1
    # part_index must be a contiguous 0..N-1 sequence and every sibling must
    # agree on part_total — retrieval reconstructs missing siblings from
    # exactly these two fields.
    indices = [c.metadata["part_index"] for c in chunks]
    assert indices == list(range(len(chunks)))
    assert {c.metadata["part_total"] for c in chunks} == {len(chunks)}
    assert {c.id for c in chunks} == {
        f"Technique:T1059:description:{i}" for i in range(len(chunks))
    }


def test_char_proxy_respects_the_char_budget() -> None:
    technique = make_technique(
        description=long_prose(n_sentences=80), detection=""
    )
    for chunk in chunk_techniques([technique]):
        body = chunk.text.partition("\n\n")[2]
        # The proxy budgets the FIELD text; header + label ride on top, so
        # assert on the piece after its label line, not the whole chunk.
        piece = body.partition("\n")[2]
        assert len(piece) <= TECHNIQUE_FIELD_CHUNK_CHARS


# --- token-budget splitting (fake tokenizer) ---------------------------------


def test_token_split_keeps_every_chunk_inside_the_window(
    fake_tokenizer: FakeTokenizer,
) -> None:
    # 1200 one-token words: must split, and every ASSEMBLED chunk (header +
    # label + piece) must fit the embedder window with room for [CLS]/[SEP].
    technique = make_technique(
        description=long_prose(n_sentences=100), detection=""
    )
    chunks = chunk_techniques([technique], tokenizer=as_tokenizer(fake_tokenizer))

    assert len(chunks) > 1
    for chunk in chunks:
        n_tokens = len(fake_tokenizer.encode(chunk.text))
        assert n_tokens <= EMBED_MAX_TOKENS - 2


def test_oversized_single_sentence_is_hard_split(
    fake_tokenizer: FakeTokenizer,
) -> None:
    # No sentence boundary anywhere: packing can't help, the token-boundary
    # backstop has to cut mid-"sentence" rather than emit an over-budget chunk.
    one_giant_sentence = " ".join(f"word{i}" for i in range(700))
    technique = make_technique(description=one_giant_sentence, detection="")
    chunks = chunk_techniques([technique], tokenizer=as_tokenizer(fake_tokenizer))

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(fake_tokenizer.encode(chunk.text)) <= EMBED_MAX_TOKENS - 2


# --- runbooks -----------------------------------------------------------------


def test_runbook_chunks_carry_source_and_index() -> None:
    text = "# Brute Force Triage\n\n" + long_prose(n_sentences=80)
    chunks = chunk_runbook(text, source="rb-brute-force.md")

    assert len(chunks) > 1
    assert {c.metadata["source"] for c in chunks} == {"rb-brute-force.md"}
    indices = [c.metadata["chunk_index"] for c in chunks]
    assert indices == list(range(len(chunks)))
    assert {c.metadata["part_total"] for c in chunks} == {len(chunks)}
    assert chunks[0].id == "Runbook:rb-brute-force.md:0"


def test_runbook_header_uses_h1_title() -> None:
    chunks = chunk_runbook(
        "# Brute Force Triage\n\nSome steps.", source="rb.md"
    )
    header = chunks[0].text.partition("\n\n")[0]
    assert "Brute Force Triage" in header
    assert "[rb.md]" in header


def test_runbook_header_falls_back_to_filename_without_h1() -> None:
    chunks = chunk_runbook("No heading here. Just prose.", source="rb.md")
    assert "rb.md" in chunks[0].text.partition("\n\n")[0]
