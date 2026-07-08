"""The installed ``triage`` command: one executable, git-style subcommands.

``[project.scripts]`` in pyproject.toml points the ``triage`` console script at
`main` below. A single top-level command with subcommands (``triage ingest``,
``triage query``) was chosen over separate ``triage-ingest``/``triage-query``
executables: one name on the user's PATH, ``triage --help`` enumerates every
verb, and future verbs (``triage serve`` in Stage 2's API phase) slot in
without minting new binaries.

This module owns only dispatch. The flags and handlers live next to the code
they configure — ``ingest.add_arguments``/``ingest.run`` and
``query.add_arguments``/``query.run`` — and are shared with the original
``python -m triage.ingest`` / ``python -m triage.query`` entry points, which
keep working identically.
"""

from __future__ import annotations

import argparse

from . import ingest, query


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``triage`` console script."""
    parser = argparse.ArgumentParser(
        prog="triage",
        description=(
            "RAG triage assistant for SOC alerts: retrieves MITRE ATT&CK "
            "techniques and internal runbooks, then produces a grounded, "
            "citable verdict."
        ),
    )
    subparsers = parser.add_subparsers(
        title="commands", dest="command", required=True
    )

    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Build the vector store: fetch/load corpus, chunk, embed, persist.",
        description=ingest.__doc__.splitlines()[0],
    )
    ingest.add_arguments(ingest_parser)
    # set_defaults(func=...) is the standard argparse dispatch idiom: each
    # subparser records its handler, so main() never switch-cases on strings.
    ingest_parser.set_defaults(func=ingest.run)

    query_parser = subparsers.add_parser(
        "query",
        help="Triage one alert: retrieve sources and generate a grounded verdict.",
        description=query.__doc__.splitlines()[0],
    )
    query.add_arguments(query_parser)
    query_parser.set_defaults(func=query.run)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
