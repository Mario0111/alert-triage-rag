"""The installed ``triage`` command: one executable, git-style subcommands.

``[project.scripts]`` in pyproject.toml points the ``triage`` console script at
`main` below. A single top-level command with subcommands (``triage ingest``,
``triage query``, ``triage serve``) was chosen over separate per-verb
executables: one name on the user's PATH, ``triage --help`` enumerates every
verb, and new verbs slot in without minting new binaries — exactly how
``serve`` landed in Stage 2's API phase.

This module owns only dispatch. The flags and handlers live next to the code
they configure — each module's ``add_arguments``/``run`` pair — and are shared
with the original ``python -m triage.<verb>`` entry points, which keep working
identically.
"""

from __future__ import annotations

import argparse

from . import desktop_launch, ingest, query, serve, ui_launch


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

    serve_parser = subparsers.add_parser(
        "serve",
        help="Run the triage HTTP API (POST /triage, GET /health) via uvicorn.",
        description=serve.__doc__.splitlines()[0],
    )
    serve.add_arguments(serve_parser)
    serve_parser.set_defaults(func=serve.run)

    ui_parser = subparsers.add_parser(
        "ui",
        help="Launch the Streamlit UI, a thin browser client of the API.",
        description=ui_launch.__doc__.splitlines()[0],
    )
    ui_launch.add_arguments(ui_parser)
    ui_parser.set_defaults(func=ui_launch.run)

    desktop_parser = subparsers.add_parser(
        "desktop",
        help="Launch the native desktop UI (Qt), a thin client of the API.",
        description=desktop_launch.__doc__.splitlines()[0],
    )
    desktop_launch.add_arguments(desktop_parser)
    desktop_parser.set_defaults(func=desktop_launch.run)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
