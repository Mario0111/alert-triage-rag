"""The installed ``triage`` command: one executable, git-style subcommands.

``[project.scripts]`` in pyproject.toml points the ``triage`` console script at
`main` below. A single top-level command with subcommands (``triage ingest``,
``triage query``, ``triage serve``, ``triage desktop``) was chosen over separate
per-verb executables: one name on the user's PATH, ``triage --help`` enumerates
every verb, and new verbs slot in without minting new binaries.

**Subcommand modules are imported lazily**, and that is load-bearing, not a
micro-optimisation. ``ingest``/``query``/``serve`` import torch, chromadb and
sentence-transformers at module top (~2 GB of dependencies, a couple of seconds
to load). If this file imported them eagerly — as it once did — then *every*
invocation, including ``triage --help`` and tab-completion, would pay that cost,
and ``import triage.cli`` would be impossible on an install that ships the GUI
without the pipeline (the installer's thin-client tier). Instead this module
knows only a REGISTRY of ``verb -> (module name, one-line summary)``: the
top-level parser is built from those strings alone, and the module for a verb is
imported only once that verb is actually dispatched. ``triage desktop`` stays
cheap for the same reason — ``desktop_launch`` itself lazy-imports PySide6.

This module owns only dispatch. Each subcommand's flags and handler live next to
the code they configure, as an ``add_arguments``/``run`` pair, and are shared
with the ``python -m triage.<verb>`` entry points, which keep working
identically.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class _Command:
    """One subcommand, described without importing its (heavy) module.

    ``module`` is a dotted import path resolved lazily in `main`; ``summary`` is
    the one-line help shown by ``triage --help``. Holding a string rather than
    the module object is the whole point — it keeps torch out of the
    help/dispatch path until a pipeline verb is actually run.
    """

    module: str
    summary: str


# The registry IS the top-level CLI. Order here is the order in ``--help``.
_COMMANDS: dict[str, _Command] = {
    "ingest": _Command(
        "triage.ingest",
        "Build the vector store: fetch/load corpus, chunk, embed, persist.",
    ),
    "query": _Command(
        "triage.query",
        "Triage one alert: retrieve sources and generate a grounded verdict.",
    ),
    "serve": _Command(
        "triage.serve",
        "Run the triage HTTP API (POST /triage, GET /health) via uvicorn.",
    ),
    "desktop": _Command(
        "triage.desktop_launch",
        "Launch the native desktop UI (Qt), a thin client of the API.",
    ),
}

_DESCRIPTION = (
    "RAG triage assistant for SOC alerts: retrieves MITRE ATT&CK techniques "
    "and internal runbooks, then produces a grounded, citable verdict."
)


def _build_top_parser() -> argparse.ArgumentParser:
    """Build the top-level parser from the registry alone (no heavy imports).

    The subparsers registered here are deliberately *empty* — they carry a name
    and a help line but none of the verb's own arguments, because populating
    those would require importing the module. This parser therefore renders
    ``triage --help``, ``triage`` (no verb), and ``triage <unknown>`` without
    touching the pipeline. It never parses a verb's own flags — `main` routes a
    real verb to a freshly built parser instead.
    """
    parser = argparse.ArgumentParser(prog="triage", description=_DESCRIPTION)
    subparsers = parser.add_subparsers(title="commands", dest="command", required=True)
    for name, command in _COMMANDS.items():
        subparsers.add_parser(name, help=command.summary, add_help=False)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``triage`` console script."""
    argv = list(sys.argv[1:] if argv is None else argv)

    # The command is the first positional token. Anything before it can only be
    # a top-level option — and the only one is -h/--help — so if the first token
    # is an option, no command was given: hand the whole thing to the
    # lightweight parser for help or a clean error. Same for an unknown verb.
    # None of these paths import a subcommand module.
    if not argv or argv[0].startswith("-") or argv[0] not in _COMMANDS:
        _build_top_parser().parse_args(argv)
        return  # unreachable: parse_args exits on --help and on errors

    command = argv[0]
    try:
        module = importlib.import_module(_COMMANDS[command].module)
    except ImportError as exc:
        # Lazy imports make a partial install REACHABLE, so it has to be
        # explained rather than crash. The installer's thin-client tier ships
        # the GUI without the pipeline, so `triage --help` and `triage desktop`
        # work there while `ingest`/`query`/`serve` cannot — without this the
        # user would get a bare ModuleNotFoundError naming some transitive
        # dependency they have never heard of. Mirrors the "[desktop] extra"
        # message desktop_launch.py gives for a missing PySide6.
        raise SystemExit(
            f"The `{command}` command needs the triage pipeline, which is not "
            f"installed in this environment ({exc.name} is missing).\n"
            "This is expected on a thin-client install, which ships only the "
            "desktop app and talks to a remote or containerised API.\n"
            "Install the full application, or use `triage desktop --api-url "
            "<url>` to point the app at a running API."
        ) from exc

    # Now that the module is loaded, build its real parser from the same
    # add_arguments() the `python -m triage.<verb>` entry point uses, and
    # dispatch. The description is the first line of the module's docstring.
    doc = (module.__doc__ or "").strip()
    subparser = argparse.ArgumentParser(
        prog=f"triage {command}",
        description=doc.splitlines()[0] if doc else None,
    )
    module.add_arguments(subparser)
    args = subparser.parse_args(argv[1:])
    module.run(args)


if __name__ == "__main__":
    main()
