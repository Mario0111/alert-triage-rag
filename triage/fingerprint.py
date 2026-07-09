"""Staleness fingerprint: does the stored index match the code that's running?

The Chroma store is built once by `triage ingest` and then survives app
upgrades untouched (the data dir is deliberately not part of the install). That
opens a silent failure mode: the installed code moves on — different chunking
budgets, a different embedding model, edited runbooks — while queries keep
running against an index built by the old code. Nothing crashes; retrieval
quality just quietly degrades, which for a system whose whole pitch is
*traceable* verdicts is the worst kind of bug.

The fix is a fingerprint written into the collection's metadata at ingest time
and checked every time the collection is opened for querying (CLI and API both
go through `query.load_collection`). On mismatch the store is declared stale
and the caller fails loudly with "re-run `triage ingest`" — a rebuild takes
minutes and restores the invariant.

What defines "stale" (author decision, PLAN.md Phase 9): a mismatch in ANY of

  - ``app_version``   — the installed package version. Coarse proxy for
    chunking-LOGIC changes the constants below can't see (e.g. a rewritten
    splitter). Cost accepted: every release forces one re-ingest.
  - ``embed_model``   — the sentence-transformers model id. Query vectors must
    live in the same embedding space as the stored vectors; a mismatch makes
    similarity scores meaningless without any visible error.
  - ``chunking``      — the token-budget constants in chunk.py that shape every
    chunk (and thereby the chunk-id scheme retrieve.py reconstructs).
    ``TECHNIQUE_FIELD_CHUNK_CHARS`` is deliberately excluded: it only drives
    the tokenizer-less fallback path, which real ingestion never takes.
  - ``attack_source`` — the pinned ATT&CK bundle URL. Bumping the pin means a
    new corpus even though no local file visibly changed.
  - ``runbooks``      — content hash of the runbooks the store was built from.
    Runbooks ship inside the wheel, so an app upgrade can swap them without
    touching anything else. Only enforced for the packaged runbooks: a store
    built from a custom ``--runbooks-dir`` (a dev escape hatch) is prefixed
    ``custom:`` and its hash is recorded but not compared — comparing it
    against the *packaged* runbooks would flag a mismatch that re-running
    ingest could never fix.

The fingerprint is stored as ONE json string under `FINGERPRINT_KEY`: Chroma
collection metadata only holds flat scalars, and a single value is compared
atomically and diffed field-by-field for the error message.
"""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import paths

# These four constants shape every chunk the store holds; chunk.py is
# author-owned, so they are READ here (never redefined) — the fingerprint
# observes the chunking strategy, it doesn't own it.
from .chunk import (
    _FIELD_TOKEN_MARGIN,
    _MIN_FIELD_TOKENS,
    _SPECIAL_TOKENS,
    EMBED_MAX_TOKENS,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# The single metadata key the fingerprint lives under, next to "hnsw:space".
FINGERPRINT_KEY = "triage_fingerprint"

# Origin prefixes for the runbooks field (see module docstring).
_PACKAGED_PREFIX = "packaged:"
_CUSTOM_PREFIX = "custom:"


class StaleStoreError(RuntimeError):
    """The persisted Chroma store no longer matches the running code/corpus."""


def app_version() -> str:
    """Installed package version, or a stable placeholder outside an install.

    Both ingest and query resolve this the same way, so a plain checkout run
    via PYTHONPATH (no install at all) still compares equal to itself.
    """
    try:
        return version(paths.APP_NAME)
    except PackageNotFoundError:
        return "unversioned"


def _chunking_params() -> str:
    """The chunk-shaping constants, serialized in one comparable string."""
    return (
        f"embed_max_tokens={EMBED_MAX_TOKENS} "
        f"special_tokens={_SPECIAL_TOKENS} "
        f"field_token_margin={_FIELD_TOKEN_MARGIN} "
        f"min_field_tokens={_MIN_FIELD_TOKENS}"
    )


def _hash_runbooks(runbooks_dir: Path) -> str:
    """Order-independent content hash of every ``*.md`` runbook in a directory.

    Filenames participate in the hash (they are the citation ids), separated
    from content by NUL bytes so no concatenation of names/bodies can collide
    with a different split of the same bytes. Truncated to 16 hex chars —
    64 bits is far beyond what change-detection needs, and it keeps the stored
    fingerprint readable when debugging.
    """
    digest = hashlib.sha256()
    for md_path in sorted(runbooks_dir.glob("*.md")):
        digest.update(md_path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(md_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _runbooks_field(runbooks_dir: Path) -> str:
    """Hash the runbooks and tag whether they are the packaged set.

    The tag decides enforcement at check time: only a ``packaged:`` store is
    compared against the currently packaged runbooks (see module docstring).
    """
    try:
        is_packaged = (
            runbooks_dir.resolve() == paths.packaged_runbooks_dir().resolve()
        )
    except FileNotFoundError:
        # No packaged runbooks resolvable (broken install) — whatever dir the
        # caller supplied is by definition not the packaged one.
        is_packaged = False
    prefix = _PACKAGED_PREFIX if is_packaged else _CUSTOM_PREFIX
    return prefix + _hash_runbooks(runbooks_dir)


def _fingerprint_dict(
    embed_model: str, runbooks_dir: Path, attack_source: str
) -> dict[str, str]:
    """Assemble the five comparison fields for the CURRENT code + corpus."""
    return {
        "app_version": app_version(),
        "embed_model": embed_model,
        "chunking": _chunking_params(),
        "attack_source": attack_source,
        "runbooks": _runbooks_field(runbooks_dir),
    }


def build_fingerprint(
    embed_model: str, runbooks_dir: Path, attack_source: str
) -> str:
    """Serialize the current fingerprint for storage in collection metadata.

    Called by ingest when creating the collection.

    Args:
        embed_model: The sentence-transformers model id used to embed.
        runbooks_dir: The runbooks directory this ingest run is reading.
        attack_source: The pinned ATT&CK bundle URL (`ingest.ATTACK_BUNDLE_URL`;
            passed in rather than imported to keep this module import-cycle
            free — ingest already imports us).

    Returns:
        A json string to store under `FINGERPRINT_KEY`.
    """
    return json.dumps(
        _fingerprint_dict(embed_model, runbooks_dir, attack_source),
        sort_keys=True,
    )


def check_fingerprint(
    metadata: Mapping[str, Any] | None, embed_model: str, attack_source: str
) -> None:
    """Fail loudly if a collection's stored fingerprint doesn't match the code.

    Called by `query.load_collection` on every open (CLI query and API startup
    both funnel through it). The expected fingerprint is recomputed from the
    running code — same functions ingest used — so the comparison is
    "would `triage ingest` build this store today?".

    Args:
        metadata: The Chroma collection's metadata mapping (may be None).
        embed_model: The model id the caller is about to embed queries with.
        attack_source: The pinned ATT&CK bundle URL the current code fetches.

    Raises:
        StaleStoreError: If the fingerprint is missing (pre-fingerprint store),
            unreadable, or differs from the current code/corpus on any
            enforced field. The message names each mismatched field and ends
            with the remedy: re-run ``triage ingest``.
    """
    remedy = "Re-run `triage ingest` to rebuild the store."
    stored_raw = (metadata or {}).get(FINGERPRINT_KEY)
    if not isinstance(stored_raw, str):
        raise StaleStoreError(
            "The vector store has no staleness fingerprint — it was built by "
            f"an older version of this app. {remedy}"
        )
    try:
        stored = json.loads(stored_raw)
    except json.JSONDecodeError as exc:
        raise StaleStoreError(
            f"The vector store's staleness fingerprint is unreadable "
            f"({exc}). {remedy}"
        ) from exc

    expected = _fingerprint_dict(
        embed_model, paths.packaged_runbooks_dir(), attack_source
    )
    mismatches = []
    for field, want in expected.items():
        got = stored.get(field)
        if field == "runbooks" and isinstance(got, str) and got.startswith(
            _CUSTOM_PREFIX
        ):
            # Built from a custom --runbooks-dir: recorded, not enforced.
            continue
        if got != want:
            mismatches.append(f"  {field}: store has {got!r}, code has {want!r}")
    if mismatches:
        raise StaleStoreError(
            "The vector store is stale — it was built by different "
            "code/corpus than what is now running:\n"
            + "\n".join(mismatches)
            + f"\n{remedy}"
        )
