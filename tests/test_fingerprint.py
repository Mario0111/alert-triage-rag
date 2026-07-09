"""Staleness fingerprint (triage/fingerprint.py) and the load-time gate.

Hermetic like the rest of the suite: fingerprints are built against tmp_path
runbook directories or the packaged runbooks already on disk; the collection
side uses the in-memory EphemeralClient harness, and the one test of
`query.load_collection` writes a real (tiny, empty) PersistentClient store
into tmp_path — disk, but no network and no models.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import chromadb
import pytest

from triage import paths
from triage.fingerprint import (
    FINGERPRINT_KEY,
    StaleStoreError,
    build_fingerprint,
    check_fingerprint,
)
from triage.query import load_collection

ATTACK_URL = "https://example.test/enterprise-attack-19.1.json"


def make_runbooks(tmp_path: Path, content: str = "# RB\nsteps") -> Path:
    runbooks = tmp_path / "runbooks"
    runbooks.mkdir(exist_ok=True)
    (runbooks / "rb-test.md").write_text(content, encoding="utf-8")
    return runbooks


def packaged_fingerprint(embed_model: str = "test-model") -> str:
    """The fingerprint ingest would write for the packaged runbooks."""
    return build_fingerprint(
        embed_model=embed_model,
        runbooks_dir=paths.packaged_runbooks_dir(),
        attack_source=ATTACK_URL,
    )


# --- building --------------------------------------------------------------


def test_fingerprint_carries_the_five_agreed_fields(tmp_path: Path) -> None:
    fp = json.loads(
        build_fingerprint(
            embed_model="test-model",
            runbooks_dir=make_runbooks(tmp_path),
            attack_source=ATTACK_URL,
        )
    )
    # The field set IS the staleness decision (PLAN.md Phase 9) — a change
    # here must be deliberate, so the test pins it.
    assert set(fp) == {
        "app_version",
        "embed_model",
        "chunking",
        "attack_source",
        "runbooks",
    }
    assert fp["embed_model"] == "test-model"
    assert fp["attack_source"] == ATTACK_URL
    # chunk.py's real budget constants must be present, not placeholders.
    assert "embed_max_tokens=512" in fp["chunking"]


def test_runbook_content_change_changes_the_fingerprint(tmp_path: Path) -> None:
    runbooks = make_runbooks(tmp_path, content="# RB\noriginal")
    before = json.loads(
        build_fingerprint("m", runbooks_dir=runbooks, attack_source=ATTACK_URL)
    )
    (runbooks / "rb-test.md").write_text("# RB\nedited", encoding="utf-8")
    after = json.loads(
        build_fingerprint("m", runbooks_dir=runbooks, attack_source=ATTACK_URL)
    )
    assert before["runbooks"] != after["runbooks"]


def test_runbooks_origin_is_tagged(tmp_path: Path) -> None:
    custom = json.loads(
        build_fingerprint(
            "m", runbooks_dir=make_runbooks(tmp_path), attack_source=ATTACK_URL
        )
    )
    packaged = json.loads(packaged_fingerprint())
    assert custom["runbooks"].startswith("custom:")
    assert packaged["runbooks"].startswith("packaged:")


# --- checking ----------------------------------------------------------------


def test_matching_fingerprint_passes() -> None:
    metadata = {"hnsw:space": "cosine", FINGERPRINT_KEY: packaged_fingerprint()}
    check_fingerprint(metadata, embed_model="test-model", attack_source=ATTACK_URL)


def test_missing_fingerprint_is_stale() -> None:
    # A store built before this feature existed has no fingerprint at all;
    # both a metadata dict without the key and no metadata must fail.
    for metadata in ({"hnsw:space": "cosine"}, None):
        with pytest.raises(StaleStoreError, match="triage ingest"):
            check_fingerprint(
                metadata, embed_model="test-model", attack_source=ATTACK_URL
            )


def test_mismatch_names_the_differing_fields() -> None:
    metadata = {FINGERPRINT_KEY: packaged_fingerprint(embed_model="old-model")}
    with pytest.raises(StaleStoreError) as excinfo:
        check_fingerprint(
            metadata, embed_model="new-model", attack_source=ATTACK_URL
        )
    message = str(excinfo.value)
    # The analyst-facing diff: which field, both values, and the remedy.
    assert "embed_model" in message
    assert "old-model" in message and "new-model" in message
    assert "triage ingest" in message
    # Fields that DO match must not be reported as mismatches.
    assert "app_version" not in message


def test_app_version_mismatch_is_stale() -> None:
    stored = json.loads(packaged_fingerprint())
    stored["app_version"] = "0.0.0-older"
    metadata = {FINGERPRINT_KEY: json.dumps(stored)}
    with pytest.raises(StaleStoreError, match="app_version"):
        check_fingerprint(
            metadata, embed_model="test-model", attack_source=ATTACK_URL
        )


def test_custom_runbooks_hash_is_recorded_but_not_enforced(
    tmp_path: Path,
) -> None:
    # A store ingested from --runbooks-dir can never hash-match the packaged
    # runbooks; enforcing that would make it permanently "stale" with no
    # ingest command able to fix it. Everything else still must match.
    stored = build_fingerprint(
        embed_model="test-model",
        runbooks_dir=make_runbooks(tmp_path),
        attack_source=ATTACK_URL,
    )
    check_fingerprint(
        {FINGERPRINT_KEY: stored},
        embed_model="test-model",
        attack_source=ATTACK_URL,
    )


def test_unreadable_fingerprint_is_stale() -> None:
    with pytest.raises(StaleStoreError, match="unreadable"):
        check_fingerprint(
            {FINGERPRINT_KEY: "not json{"},
            embed_model="test-model",
            attack_source=ATTACK_URL,
        )


# --- the gate in query.load_collection ----------------------------------------


def test_load_collection_refuses_a_stale_store(tmp_path: Path) -> None:
    """End to end through the real loader: a pre-fingerprint store is refused.

    Builds an actual on-disk PersistentClient store (what ingest produces)
    whose collection lacks the fingerprint — exactly the state of any store
    built before this feature.
    """
    db_dir = tmp_path / "chroma_db"
    # Default settings on purpose: load_collection opens this same path with
    # default settings too, and chromadb refuses to reopen a path with
    # settings unequal to the first open (telemetry is off via the autouse
    # env fixture in conftest).
    client = chromadb.PersistentClient(path=str(db_dir))
    name = f"stale-{uuid4().hex}"
    client.create_collection(name=name, metadata={"hnsw:space": "cosine"})

    with pytest.raises(StaleStoreError, match="triage ingest"):
        load_collection(db_dir, name)


def test_load_collection_missing_dir_says_run_ingest(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="triage ingest"):
        load_collection(tmp_path / "never_ingested", "alert_triage")


def test_load_collection_missing_collection_says_run_ingest(
    tmp_path: Path,
) -> None:
    db_dir = tmp_path / "chroma_db"
    chromadb.PersistentClient(path=str(db_dir))
    with pytest.raises(FileNotFoundError, match="triage ingest"):
        load_collection(db_dir, "no_such_collection")
