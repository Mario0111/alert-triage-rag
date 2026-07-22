"""PyInstaller entry point for the native desktop app.

Why this file exists instead of pointing PyInstaller at ``triage/desktop.py``:
an entry script is executed as ``__main__`` with NO package parent, so
desktop.py's ``from . import apiclient`` would fail with "attempted relative
import with no known parent package". Importing the module by its ABSOLUTE name
keeps the package context intact, so the relative import inside it resolves.

This is also the seam that keeps the bundled app THIN: it reaches only
``triage.desktop`` -> PySide6 + ``triage.apiclient`` (stdlib), never the
pipeline modules (torch, chromadb, anthropic, ...). See the excludes in
alert-triage-desktop.spec, which assert that.
"""

from triage.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
