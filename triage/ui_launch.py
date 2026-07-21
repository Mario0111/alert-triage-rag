"""The ``triage ui`` subcommand: launch the Streamlit UI (a client of the API).

Streamlit is not a library you call — it is a RUNNER: ``streamlit run <script>``
boots a web server and re-executes the script per interaction (see ui.py). So
this subcommand does NOT import the UI module; it shells out to ``streamlit
run`` pointed at the packaged ui.py, passing the chosen API URL through the
environment.

Two deliberate choices, both interview material:

- **Lazy streamlit import.** streamlit is an OPTIONAL extra (``pip install
  "alert-triage-rag[ui]"``): heavy (pandas/pyarrow/tornado) and never needed by
  the CLI, the API, or the SIEM webhook. Importing it at module top would make
  a plain ``triage --help`` pull it in and would crash every install that
  skipped the extra — because cli.py imports this module to register the verb.
  So the presence check happens inside ``run``, and its absence produces a
  clear "install the [ui] extra" message instead of an ImportError traceback.

- **``python -m streamlit``, not the streamlit.exe shim.** Console-script .exe
  shims are blocked by Windows Smart App Control on the dev machine; invoking
  the module through the current interpreter sidesteps that and is portable.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the ui arguments to ``parser`` (shared with ``triage ui``)."""
    parser.add_argument(
        "--api-url",
        default=None,
        help="Base URL of the triage API the UI calls. Overrides the "
        "TRIAGE_API_URL env var; if neither is set the UI defaults to "
        "http://127.0.0.1:8000 (a local `triage serve`).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface for the Streamlit server to bind. Default is "
        "local-only; use 0.0.0.0 inside a container (as docker-compose does).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8501,
        help="Port for the Streamlit server (Streamlit's own default is 8501).",
    )


def run(args: argparse.Namespace) -> None:
    """Launch Streamlit against the packaged ui.py (subcommand handler)."""
    if importlib.util.find_spec("streamlit") is None:
        raise SystemExit(
            "The `ui` command needs Streamlit, which is an optional extra.\n"
            'Install it with:  pip install "alert-triage-rag[ui]"'
        )

    ui_script = Path(__file__).with_name("ui.py")

    # The UI reads TRIAGE_API_URL; translate the flag into it (flag > existing
    # env > ui.py's built-in default). Copy the env so this process's own
    # environment is left untouched.
    env = dict(os.environ)
    if args.api_url:
        env["TRIAGE_API_URL"] = args.api_url

    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(ui_script),
        "--server.address",
        args.host,
        "--server.port",
        str(args.port),
        # Headless: don't try to open a browser and don't show the first-run
        # email prompt — correct for a server/container launch.
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    # Hand control to streamlit; its exit code becomes ours.
    raise SystemExit(subprocess.call(command, env=env))


def main(argv: list[str] | None = None) -> None:
    """Standalone entry point (`python -m triage.ui_launch`)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
