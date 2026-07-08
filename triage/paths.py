"""Where the app keeps its data: one module, one answer.

An installed CLI cannot assume a repo checkout, so repo-relative paths like
``./chroma_db`` stop working the moment the app is installed with pipx and run
from an arbitrary directory. The fix is the OS convention for per-user
application data, resolved by `platformdirs`:

    Windows:  %LOCALAPPDATA%\\alert-triage-rag
    Linux:    ~/.local/share/alert-triage-rag
    macOS:    ~/Library/Application Support/alert-triage-rag

Precedence, most explicit wins (each layer is an override, not a replacement):

    1. CLI flags (``--db-dir``, ``--attack-file``, ``--runbooks-dir``)
    2. The ``TRIAGE_DATA_DIR`` environment variable (dev-mode override)
    3. The platformdirs per-user default

Dev mode: pointing ``TRIAGE_DATA_DIR`` at a repo checkout reproduces the
original repo layout exactly — ``<repo>/chroma_db`` and
``<repo>/corpus/attack/enterprise-attack.json`` — because the subpaths below
were chosen to match it.

The runbooks are deliberately NOT under the data dir: they ship inside the
installed package (see ``[tool.setuptools.package-data]`` in pyproject.toml)
because they are part of the product, not state the app accumulates. Only
mutable/fetched data (the Chroma store, the downloaded ATT&CK bundle) lives in
the data dir.
"""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path

import platformdirs

APP_NAME = "alert-triage-rag"
ENV_DATA_DIR = "TRIAGE_DATA_DIR"


def data_dir() -> Path:
    """Resolve the root data directory (env override, else per-user default).

    Returns:
        ``TRIAGE_DATA_DIR`` if set (dev mode / tests), otherwise the
        platformdirs per-user data directory for this app. Not created here —
        callers that write are responsible for ``mkdir``, callers that read
        should fail loudly on absence.
    """
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override)
    # appauthor=False: without it Windows nests an author directory
    # (%LOCALAPPDATA%\Author\app) — noise for a single-author tool.
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def chroma_dir() -> Path:
    """Default location of the persisted Chroma database."""
    return data_dir() / "chroma_db"


def attack_file() -> Path:
    """Default location of the (downloaded) ATT&CK Enterprise STIX bundle."""
    return data_dir() / "corpus" / "attack" / "enterprise-attack.json"


def packaged_runbooks_dir() -> Path:
    """Locate the runbooks shipped inside the installed package.

    ``importlib.resources.files`` resolves package-relative data wherever the
    package actually lives: a normal site-packages install, an editable
    install (resolves straight into the repo), or a plain checkout.

    Returns:
        The directory containing the packaged ``*.md`` runbooks.

    Raises:
        FileNotFoundError: If the package data is missing — a broken install
            (e.g. the wheel was built without the package-data table).
    """
    # The runbooks are real files on disk in every install mode we support
    # (wheel installs are unpacked; we don't run from zip), so a plain Path is
    # safe and keeps ingest.py's directory-based API unchanged.
    runbooks = Path(str(files("triage").joinpath("corpus", "runbooks")))
    if not runbooks.is_dir():
        raise FileNotFoundError(
            f"Packaged runbooks not found at {runbooks}. The install is "
            "broken: the wheel was built without its package data "
            "(check [tool.setuptools.package-data] in pyproject.toml)."
        )
    return runbooks
