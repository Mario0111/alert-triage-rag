"""Corpus chunking — AUTHOR-OWNED.

The per-technique chunking strategy is the heart of this project's retrieval
quality and the author must be able to explain it in an interview. The function
bodies below are intentionally left unimplemented. The `Chunk` dataclass and the
function signatures define the contract that `ingest.py` depends on; fill in the
reasoning, don't change the shape without updating `ingest.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .stix import Technique

if TYPE_CHECKING:
    from transformers import PreTrainedTokenizerBase


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


# bge-small-en-v1.5 embeds at most this many tokens; anything past it is silently
# truncated at embed time. Every technique chunk must fit inside this window.
EMBED_MAX_TOKENS = 512
# The tokenizer prepends [CLS] and appends [SEP] to every input; reserve them.
_SPECIAL_TOKENS = 2
# Small cushion for tokenization boundary effects: sub-words can merge or split at
# the header/field join differently than measuring the parts in isolation implies.
_FIELD_TOKEN_MARGIN = 4
# Floor so a pathologically long header can't drive the field budget to zero.
_MIN_FIELD_TOKENS = 32

# Char budget for the token-less fallback path (tests / quick local runs with no
# tokenizer). ~1600 chars is a rough stand-in for the 512-token window.
TECHNIQUE_FIELD_CHUNK_CHARS = 1600

# Sentence-ish boundary: a ., ! or ? followed by whitespace.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# First markdown H1 in a runbook, used as its human-readable title.
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _technique_header(technique: Technique) -> str:
    """Compact, self-identifying header prepended to each technique chunk."""
    platforms = ", ".join(technique.platforms) or "(none)"
    tactics = ", ".join(technique.tactics) or "(none)"
    return (
        f"{technique.name} ({technique.attack_id})\n"
        f"PLATFORMS: {platforms}\nTACTICS  : {tactics}"
    )


def _assemble(header: str, part: str, idx: int, total: int, piece: str) -> str:
    """Build a technique chunk's final text: header + part label + field piece.

    Single source of truth for the chunk-text layout, so the token-budget
    estimate in `_split_field` and the real assembly in `chunk_techniques` can't
    drift apart.
    """
    return f"{header}\n\n{part.capitalize()} ({idx + 1}/{total}):\n{piece}"


def _n_tokens(text: str, tokenizer: PreTrainedTokenizerBase) -> int:
    """Count content tokens, excluding the [CLS]/[SEP] the model adds itself."""
    return len(tokenizer(text, add_special_tokens=False)["input_ids"])


def _hard_split(
    text: str, tokenizer: PreTrainedTokenizerBase, budget: int
) -> list[str]:
    """Last-resort split of a single over-budget sentence on token boundaries.

    Sentence packing can't help when one sentence alone exceeds the budget, so
    cut it every ``budget`` tokens and decode each slice back to text. This is
    what guarantees no chunk exceeds the window, at the cost of a mid-sentence
    break for the rare very long sentence.
    """
    ids = tokenizer.encode(text, add_special_tokens=False)
    pieces = [
        tokenizer.decode(ids[start : start + budget]).strip()
        for start in range(0, len(ids), budget)
    ]
    return [piece for piece in pieces if piece]


def _split_by_tokens(
    text: str, tokenizer: PreTrainedTokenizerBase, budget: int
) -> list[str]:
    """Greedily pack sentences into pieces of at most ``budget`` content tokens."""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_RE.split(text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue

        # A single sentence bigger than the budget can't be packed with others —
        # flush what we have, then hard-split it on token boundaries.
        if _n_tokens(sentence, tokenizer) > budget:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(_hard_split(sentence, tokenizer, budget))
            continue

        candidate = sentence if not current else f"{current} {sentence}"
        if _n_tokens(candidate, tokenizer) <= budget:
            current = candidate
        else:
            pieces.append(current)
            current = sentence

    if current:
        pieces.append(current)
    return pieces


def _split_field(
    field_text: str,
    part: str,
    header: str,
    tokenizer: PreTrainedTokenizerBase | None,
) -> list[str]:
    """Split one technique field into pieces whose assembled chunk fits the window.

    With a tokenizer the budget is measured, not guessed: the header and part
    label are identical on every piece, so their token cost is measured once
    (worst-case two-digit ``i/N`` label) and subtracted from the 512-token
    window, along with the model's special tokens and a small margin. Without a
    tokenizer, falls back to the char proxy.
    """
    if tokenizer is None:
        return _generic_splitter(field_text, chunk_size=TECHNIQUE_FIELD_CHUNK_CHARS)

    # Overhead = everything in the assembled chunk except the field piece. The
    # two-digit (i/N) placeholder reserves room for the largest label we'd emit.
    overhead = _n_tokens(_assemble(header, part, 98, 99, ""), tokenizer)
    budget = EMBED_MAX_TOKENS - _SPECIAL_TOKENS - overhead - _FIELD_TOKEN_MARGIN
    return _split_by_tokens(field_text, tokenizer, max(budget, _MIN_FIELD_TOKENS))


def chunk_techniques(
    techniques: list[Technique],
    tokenizer: PreTrainedTokenizerBase | None = None,
) -> list[Chunk]:
    """Chunk ATT&CK techniques into sub-limit, attack_id-tagged chunks.

    Strategy (option 1): a technique's description and detection are embedded as
    SEPARATE chunks, so detection text — which an alert query often echoes — gets
    its own vector instead of being truncated off the tail of one oversized
    chunk. If a field itself exceeds the embedder's window it is split further,
    and every resulting chunk carries the same ``attack_id`` plus
    ``part`` / ``part_index`` / ``part_total`` so ``retrieve.py`` can merge the
    pieces back into one complete, citable technique.

    NOTE: this deviates from the "one chunk per technique" wording in CLAUDE.md;
    the completeness guarantee now lives in retrieval's merge-by-attack_id step.
    Keep CLAUDE.md and ingest.py in sync with this shape.

    Args:
        techniques: Flattened techniques from `stix.parse_techniques`.
        tokenizer: The embedder's tokenizer, injected by `ingest.py`, so the
            field splitter can budget by real 512-token windows instead of a
            char proxy. Optional: when ``None`` (e.g. quick local runs / tests)
            the splitter falls back to the character heuristic.

    Returns:
        One or more `Chunk` per technique (a description part, an optional
        detection part, each possibly split), all sharing the technique's
        ``attack_id`` for reassembly at retrieval time.
    """
    chunks: list[Chunk] = []
    for technique in techniques:
        header = _technique_header(technique)
        base_meta: dict[str, str | int | float | bool] = {
            "attack_id": technique.attack_id,
            "name": technique.name,
            "platforms": ", ".join(technique.platforms),
            "tactics": ", ".join(technique.tactics),
            "source": "ATT&CK",
        }

        # Split per field so no single embedded unit blows the token budget.
        # Detection is optional (some techniques have none).
        fields: list[tuple[str, str]] = [("description", technique.description)]
        if technique.detection.strip():
            fields.append(("detection", technique.detection))

        for part, field_text in fields:
            pieces = _split_field(field_text, part, header, tokenizer)
            total = len(pieces)
            for idx, piece in enumerate(pieces):
                # Header on every chunk keeps each unit self-identifying for both
                # embedding and grounding.
                text = _assemble(header, part, idx, total, piece)
                chunks.append(
                    Chunk(
                        id=f"Technique:{technique.attack_id}:{part}:{idx}",
                        text=text,
                        metadata={
                            **base_meta,
                            "part": part,
                            "part_index": idx,
                            "part_total": total,
                        },
                    )
                )
    return chunks



def _generic_splitter(text: str, chunk_size: int = 1024) -> list[str]:
    """Split text into chunks that end on sentence boundaries when possible."""

    sentences = re.split(r'(?<=[.!?])\s+', text.strip())

    chunks = []
    current_chunk = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        # If a single sentence is longer than the chunk size,
        # emit it as its own chunk.
        if len(sentence) > chunk_size:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""
            chunks.append(sentence)
            continue

        if not current_chunk:
            current_chunk = sentence
        elif len(current_chunk) + 1 + len(sentence) <= chunk_size:
            current_chunk += " " + sentence
        else:
            chunks.append(current_chunk)
            current_chunk = sentence

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _runbook_header(text: str, source: str) -> str:
    """Compact, self-identifying header prepended to each runbook chunk.

    Uses the runbook's markdown H1 as the human-readable title (falling back to
    the filename) so every chunk names its document — same reasoning as the
    technique header: each chunk is embedded and retrieved independently, and an
    anonymous mid-document fragment is a poor embedding target and confusing
    grounding.
    """
    match = _H1_RE.search(text)
    title = match.group(1).strip() if match else source
    return f"Runbook: {title} [{source}]"


def chunk_runbook(
    text: str,
    source: str,
    tokenizer: PreTrainedTokenizerBase | None = None,
) -> list[Chunk]:
    """Chunk a single runbook into sub-window, source-tagged chunks.

    Mirrors the technique strategy: split by real token budget (header and part
    label measured and subtracted from the 512-token window), every chunk tagged
    with the runbook's ``source`` plus ``chunk_index`` / ``part_total`` so
    ``retrieve.py`` can merge siblings back into the complete runbook. No
    overlap between pieces — reassembly at retrieval is the context mechanism,
    and overlapping text would duplicate sentences at every seam of the merged
    document.

    Args:
        text: Raw markdown content of one runbook.
        source: Identifier for the runbook (e.g. its filename), used in chunk
            ids, citation metadata, and as the merge key at retrieval time.
        tokenizer: The embedder's tokenizer, injected by `ingest.py`. Optional:
            when ``None`` (tests / quick runs) falls back to the char proxy.

    Returns:
        One or more `Chunk` covering the runbook's content, all sharing
        ``source`` for reassembly at retrieval time.
    """
    header = _runbook_header(text, source)

    if tokenizer is None:
        pieces = _generic_splitter(text, chunk_size=TECHNIQUE_FIELD_CHUNK_CHARS)
    else:
        # Same measured-headroom scheme as _split_field: everything except the
        # body piece, with a two-digit (i/N) placeholder for the largest label.
        overhead = _n_tokens(_assemble(header, "part", 98, 99, ""), tokenizer)
        budget = (
            EMBED_MAX_TOKENS - _SPECIAL_TOKENS - overhead - _FIELD_TOKEN_MARGIN
        )
        pieces = _split_by_tokens(text, tokenizer, max(budget, _MIN_FIELD_TOKENS))

    total = len(pieces)
    chunks: list[Chunk] = []
    for idx, piece in enumerate(pieces):
        chunks.append(
            Chunk(
                id=f"Runbook:{source}:{idx}",
                text=_assemble(header, "part", idx, total, piece),
                metadata={
                    "source": source,
                    "chunk_index": idx,
                    "part_total": total,
                },
            )
        )
    return chunks
