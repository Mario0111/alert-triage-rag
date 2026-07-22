"""The ``triage desktop`` subcommand: launch the native Qt UI (a client of the API).

A Qt app is an ordinary Python program (no separate server process to launch),
so this handler just imports it and calls ``main()`` — in-process, no subprocess.

The PySide6 import is LAZY: PySide6 is a heavy OPTIONAL extra (``pip install
"alert-triage-rag[desktop]"``), never needed by the CLI, API, or SIEM. cli.py
imports this module to register the verb, so a top-level ``import PySide6`` would
make a plain ``triage --help`` pull in Qt and would crash every install that
skipped the extra. The presence check happens inside ``run`` instead, producing
a clear "install the [desktop] extra" message.
"""

from __future__ import annotations

import argparse
import importlib.util
import os


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the desktop arguments to ``parser`` (shared with ``triage desktop``)."""
    parser.add_argument(
        "--api-url",
        default=None,
        help="Base URL of the triage API the app calls. Overrides the "
        "TRIAGE_API_URL env var; if neither is set the app defaults to "
        "http://127.0.0.1:8000 (a local `triage serve`). It can also be "
        "changed live in the app's API-endpoint field.",
    )
    parser.add_argument(
        "--no-autostart",
        action="store_true",
        help="Do not start a backend automatically. By default the app brings "
        "the API up if nothing is already answering (a local `triage serve` "
        "from a source checkout, or the Docker container when packaged) and "
        "shuts down only what it started.",
    )


def run(args: argparse.Namespace) -> None:
    """Launch the Qt desktop app (subcommand handler)."""
    if importlib.util.find_spec("PySide6") is None:
        raise SystemExit(
            "The `desktop` command needs PySide6, which is an optional extra.\n"
            'Install it with:  pip install "alert-triage-rag[desktop]"'
        )

    # The app reads TRIAGE_API_URL (via apiclient); translate the flag into it
    # before the app builds its window. Set on this process's own environment —
    # the app runs in-process, so there is no child to pass a copy to.
    if args.api_url:
        os.environ["TRIAGE_API_URL"] = args.api_url

    from . import desktop  # lazy: imports PySide6 only when the verb is used

    raise SystemExit(desktop.main(autostart=not args.no_autostart))


def main(argv: list[str] | None = None) -> None:
    """Standalone entry point (`python -m triage.desktop_launch`)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
