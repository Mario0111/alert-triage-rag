"""Data-directory resolution (triage/paths.py).

monkeypatch is pytest's built-in fixture for temporarily changing the
environment: setenv/delenv apply for ONE test and are automatically undone
afterwards, so these tests can't leak TRIAGE_DATA_DIR into each other — or
depend on whatever the developer's shell happens to export.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from triage import paths


def test_env_override_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path))
    assert paths.data_dir() == tmp_path


def test_platformdirs_default_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(paths.ENV_DATA_DIR, raising=False)
    resolved = paths.data_dir()
    # Not asserting the exact OS-specific path — that would just re-test
    # platformdirs. What matters: it's an absolute per-user location named
    # after the app, not something repo-relative.
    assert resolved.is_absolute()
    assert resolved.name == paths.APP_NAME


def test_empty_env_var_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # TRIAGE_DATA_DIR="" (set but empty) must not resolve to Path(".") —
    # the truthiness check in data_dir() treats it as unset.
    monkeypatch.setenv(paths.ENV_DATA_DIR, "")
    assert paths.data_dir().name == paths.APP_NAME


def test_subpaths_mirror_the_repo_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The dev-mode contract: pointing TRIAGE_DATA_DIR at a checkout must
    # reproduce the old repo-relative layout exactly.
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path))
    assert paths.chroma_dir() == tmp_path / "chroma_db"
    assert (
        paths.attack_file()
        == tmp_path / "corpus" / "attack" / "enterprise-attack.json"
    )


def test_packaged_runbooks_resolve_and_contain_markdown() -> None:
    # Exercises the importlib.resources path AND the package-data wiring in
    # pyproject.toml: if the runbooks stopped shipping with the package,
    # this is the test that goes red.
    runbooks = paths.packaged_runbooks_dir()
    assert runbooks.is_dir()
    assert list(runbooks.glob("*.md")), "no packaged runbooks found"
