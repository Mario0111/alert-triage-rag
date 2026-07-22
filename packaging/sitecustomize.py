"""Point the installed app at its BUNDLED embedding model, wherever it landed.

WHAT THIS FILE IS
-----------------
``sitecustomize`` is a hook CPython has had for decades: during startup the
stdlib ``site`` module tries ``import sitecustomize`` and, if a module by that
name is importable, runs it. It is the supported way to configure an
interpreter *before any user code runs* - earlier than a ``main()``, earlier
than the first ``import triage``. The installer drops this file into the
payload's ``Lib\\site-packages\\``, so it is imported by every process the
installed app ever starts.

WHY THIS PROJECT NEEDS IT
-------------------------
The installer bundles bge-small-en-v1.5 (~130 MB) so the app embeds offline
from the first launch - the same decision the Dockerfile makes and for the same
reason (PLAN.md Phase 10: the model id is an ENFORCED staleness-fingerprint
field, so "which model is in here" must never be ambiguous, and a download on
first run reintroduces exactly the invisible moving part the fingerprint work
exists to remove).

But sentence-transformers does not take a path - it resolves ``BAAI/bge-small
-en-v1.5`` through the Hugging Face cache, whose location is the ``HF_HOME``
environment variable. So something has to set ``HF_HOME`` before
``sentence_transformers`` is imported. The candidates were:

* A global (user or machine) environment variable - rejected: an installer has
  no business mutating the user's environment for a private detail, it leaks
  into every *other* Python on the machine, and uninstalling would have to
  remember to unset it.
* A wrapper ``.cmd`` that sets the variable and then launches Python - rejected:
  the Start Menu GUI shortcut targets ``pythonw.exe`` directly (a wrapper would
  flash a console window), and the backend the app autostarts is spawned by
  ``backend.py`` as ``sys.executable -m triage.serve``, which would bypass any
  wrapper anyway.
* This file - chosen: it travels *inside* the payload, so it cannot be missed
  by any entry point, and it touches nothing outside the current process.

RELATIVE TO ITSELF, NOT TO A BAKED-IN PATH
------------------------------------------
The path is computed from ``__file__`` rather than written at build time,
because the user chooses the install directory and can move the folder
afterwards. The payload layout is fixed by the installer, so the walk up is
stable::

    <install dir>/                      <- parents[3]
      models/hf/                        <- the bundled HF cache
      python/                           <- parents[2], the embeddable CPython
        Lib/                            <- parents[1]
          site-packages/                <- parents[0]
            sitecustomize.py            <- __file__

WHAT BREAKS WITHOUT IT
----------------------
``triage ingest`` and the API would ignore the bundled 130 MB, try to download
the model from Hugging Face on first use, and fail on a machine with no
network - or, worse, silently succeed and cache a *second* copy under
``%USERPROFILE%\\.cache\\huggingface``.

NOTE ON THE THIN-CLIENT TIER: it installs no model (and no torch), so
``models/hf`` does not exist there and this file deliberately does nothing.
That is why the directory is checked instead of assumed.

Both variables use ``setdefault``: an operator who exports ``HF_HOME`` or
``HF_HUB_OFFLINE`` on purpose (the build script does exactly that while baking
the model) keeps their value.
"""

import os
from pathlib import Path

_MODEL_HOME = Path(__file__).resolve().parents[3] / "models" / "hf"

if _MODEL_HOME.is_dir():
    os.environ.setdefault("HF_HOME", str(_MODEL_HOME))
    # Mirror of the Dockerfile's post-bake `ENV HF_HUB_OFFLINE=1`. Once the
    # model is bundled, a runtime fetch can only mean something went wrong
    # (wrong model id, corrupted cache), so make it fail loudly here rather
    # than quietly pull a different snapshot than the one that shipped.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
