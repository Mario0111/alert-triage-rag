"""The ``triage`` dispatcher (triage/cli.py): lazy, torch-free at the top level.

Two things are checked here, and the first can only be proven in a SUBPROCESS.
The pytest session that runs this file has already imported torch/chromadb
transitively (test_api, test_query, ...), so ``sys.modules`` in-process says
nothing about what ``triage.cli`` pulls in. A fresh interpreter is the only
honest measurement of "does importing/using the CLI drag in the pipeline",
which is the entire reason cli.py imports its subcommands lazily (so the
installer's thin-client tier can ship a working ``triage`` without torch).

The second (dispatch routes to the right module and passes args through) is a
plain in-process call with the heavy module faked, so it stays hermetic.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from triage import cli

# The subprocess probes below check for these in a fresh interpreter's
# sys.modules: sentence_transformers pulls torch, chromadb is the other
# heavyweight, and anthropic — lighter — still has no business being imported to
# print help. Their absence is what makes the thin-client `triage` possible.


def _run(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter and capture its output."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
    )


def test_help_lists_every_verb_without_importing_the_pipeline() -> None:
    """``triage --help`` must print all verbs and import nothing heavy."""
    result = _run(
        """
        import sys
        from triage import cli
        try:
            cli.main(["--help"])
        except SystemExit:
            pass  # --help exits 0; we care about what got imported
        heavy = [m for m in ("torch", "sentence_transformers", "chromadb",
                             "anthropic") if m in sys.modules]
        print("HEAVY:" + ",".join(heavy))
        """
    )
    assert result.returncode == 0, result.stderr
    # Every registered verb is advertised in the help text.
    for verb in ("ingest", "query", "serve", "desktop"):
        assert verb in result.stdout, f"{verb!r} missing from --help:\n{result.stdout}"
    # ...and none of the pipeline modules were dragged in to produce it.
    heavy_line = next(
        ln for ln in result.stdout.splitlines() if ln.startswith("HEAVY:")
    )
    assert heavy_line == "HEAVY:", f"top-level CLI imported the pipeline: {heavy_line}"


def test_bare_import_of_cli_is_torch_free() -> None:
    """``import triage.cli`` alone must not import the pipeline.

    This is the property the installer's thin-client tier depends on: the GUI
    ships without torch, and the console entry point resolves to ``cli.main``.
    """
    result = _run(
        """
        import sys
        import triage.cli  # noqa: F401
        heavy = [m for m in ("torch", "sentence_transformers", "chromadb",
                             "anthropic") if m in sys.modules]
        print("HEAVY:" + ",".join(heavy))
        """
    )
    assert result.returncode == 0, result.stderr
    heavy_line = next(
        ln for ln in result.stdout.splitlines() if ln.startswith("HEAVY:")
    )
    assert heavy_line == "HEAVY:", f"importing triage.cli imported: {heavy_line}"


def test_no_command_errors_out() -> None:
    """``triage`` with no verb exits non-zero (a required subcommand)."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code != 0


def test_unknown_command_errors_out() -> None:
    """An unknown verb is rejected by the lightweight top parser."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["frobnicate"])
    assert excinfo.value.code != 0


def test_missing_pipeline_gives_an_explanation_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline verb on a thin install must explain itself.

    Lazy dispatch makes ``triage ingest`` REACHABLE on an install that has no
    torch, so the ImportError has to be translated. Simulated by making the
    import of the verb's module fail the way a missing dependency would.
    """

    def explode(name: str) -> object:
        raise ModuleNotFoundError("No module named 'torch'", name="torch")

    monkeypatch.setattr(cli.importlib, "import_module", explode)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["ingest"])

    message = str(excinfo.value)
    assert "torch" in message
    assert "thin-client" in message
    # Points at the way forward rather than just naming the failure.
    assert "--api-url" in message


def test_dispatch_routes_to_the_named_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """A known verb imports its module and calls its ``run`` with parsed args.

    The module is faked via the import machinery, so this stays hermetic — no
    real ingest module (and no torch) is loaded. It verifies the wiring: the
    registry name is imported, ``add_arguments`` shapes the parser, and ``run``
    receives the resulting namespace.
    """
    import argparse
    import types

    seen: dict[str, object] = {}

    fake = types.ModuleType("triage.ingest")
    fake.__doc__ = "Fake ingest module.\nSecond line ignored."

    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--batch-size", type=int, default=32)

    def run(args: argparse.Namespace) -> None:
        seen["batch_size"] = args.batch_size

    fake.add_arguments = add_arguments  # type: ignore[attr-defined]
    fake.run = run  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "triage.ingest", fake)

    cli.main(["ingest", "--batch-size", "8"])

    assert seen == {"batch_size": 8}
