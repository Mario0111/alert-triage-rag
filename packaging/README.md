# packaging/

Everything needed to turn this repo into distributable Windows artifacts.
Two independent products live here:

| Artifact | Built from | Size | Needs on the target machine |
| --- | --- | --- | --- |
| **Windows installer** (`alert-triage-rag-<version>-setup.exe`) | `build_payload.ps1` + `alert-triage.iss` | ~700 MB–1 GB | nothing |
| **Standalone thin-client .exe** (`alert-triage-desktop.exe`) | `alert-triage-desktop.spec` | ~47 MB | Docker Desktop, or a reachable API |

They are not competing versions of the same thing. The `.exe` is a *frozen*
thin client whose backend is the published container image; the installer ships
a *real Python installation* and needs no Docker at all. Keep both.

---

## Building the installer

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_payload.ps1
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\alert-triage.iss
```

Output: `dist\alert-triage-rag-<version>-setup.exe`.

Step 1 takes roughly 10–20 minutes on a good connection (~2 GB of downloads) and
step 2 another 5–15 minutes (LZMA2 compression of ~1.7 GB). Both are
re-runnable: downloads are cached under `build\installer\cache\`, so a failure
part-way through does not start over. Add `-Clean` to force a fresh payload.

Everything lands under `build/` and `dist/`, which are gitignored. The *source*
of the build is the two checked-in files plus this README — nothing about the
process lives only in someone's shell history.

### Prerequisites

Inno Setup 6:

```bash
winget install --id JRSoftware.InnoSetup --accept-source-agreements --accept-package-agreements
```

Nothing else. In particular you do **not** need a matching Python on the build
machine: `build_payload.ps1` downloads its own 3.11 and uses that interpreter
for every `pip` call.

---

## What the payload actually is, and why

The installer's payload is **a real Python installation with the app
`pip install`ed into it**, copied verbatim to the user's machine. No freezing,
no import-graph analysis, no repacking. What you test on the build machine is
byte-for-byte what ships.

That is a deliberate rejection of the obvious alternative — PyInstaller — for
the *pipeline*. Freezing torch is a well-known multi-day exercise, and, worse,
its failure mode is a **runtime** failure: a missed hidden import surfaces when
a user clicks a button, not when the build runs. Shipping an installed tree
moves every such failure to build time. (The thin client stays frozen because
it imports nothing heavier than Qt — see `alert-triage-desktop.spec`.)

### Why not just ship a venv

`python -m venv` produces a folder that looks shippable and is not. A Windows
venv contains a redirector `Scripts\python.exe`, `Lib\site-packages`, and
`pyvenv.cfg` — but **not the standard library**. `pyvenv.cfg` records
`home = <the base install>`, and at startup the venv interpreter loads `Lib\`,
`python3xx.dll` and every `.pyd` from there. Copy the venv to a machine with no
Python and it is a brick, which is precisely the machine an installer exists to
serve.

### What is shipped instead

The **python.org Windows embeddable package** (`python-3.11.9-embed-amd64.zip`,
~10 MB): `python.exe`, `pythonw.exe`, `python311.dll`, the stdlib as
`python311.zip`, the C extension modules (`_ssl.pyd`, `_sqlite3.pyd`, …) and
`vcruntime140.dll`. A complete, **relocatable** CPython with no registry keys,
no installer and no PATH entry — the artifact python.org publishes for exactly
this use. Two things about it need handling, and `build_payload.ps1` does both:

1. **No `pip`** (`ensurepip` is stripped) → bootstrapped once from
   `get-pip.py`. It must be the payload's own 3.11 that runs every subsequent
   `pip`, because wheels are tagged per interpreter version (`cp311` vs
   `cp314`). Installing with the build machine's Python would fetch wheels the
   payload cannot load — which is also why `pip install --target` from the host
   is not an option for the heavy packages.

2. **`python311._pth`.** Its mere presence puts the interpreter in an isolated
   path mode: `PYTHONPATH`, `PYTHONHOME` and the registry are ignored, and
   `sys.path` is exactly the file's contents. That isolation is a feature here —
   the installed app cannot be broken by another Python on the user's machine.
   But `site` is **not** imported unless the file says `import site`, and with
   `site` off there is no `Lib\site-packages` on `sys.path`, no `.pth` file
   processing (torch and setuptools both rely on it), and `sitecustomize.py`
   never runs. The script rewrites the file to:

   ```
   python311.zip
   .
   Lib\site-packages

   import site
   ```

### Layout of the built payload

```
build/payload/                    <- mirrors {app} on the target machine
  triage.cmd                      CLI shim  (pipeline tier only)
  python/
    python.exe pythonw.exe python311.dll python311.zip python311._pth ...
    Lib/site-packages/
      sitecustomize.py            points HF_HOME at ../../../models/hf
      triage/  PySide6/  torch/  chromadb/  ...
  models/hf/                      the baked bge-small-en-v1.5 cache

build/installer/                  <- everything that is not packed
  cache/                          downloaded python-embed zip + get-pip.py
  wheel/                          the built alert_triage_rag wheel
  probe/                          throwaway component-diff installs
  version.iss  components.iss  components.json   generated, #include'd
```

The payload lives at `build/payload`, **not** `build/installer/payload`, and
that is not tidiness — see MAX_PATH below.

---

## The three tiers

| Tier | Components | Installed | What it is |
| --- | --- | --- | --- |
| **Desktop app** | core + gui + pipeline | 2022 MB | The full thing. Native Qt window, local pipeline, bundled model, no Docker. |
| **Command line only** | core + pipeline | 1398 MB | Same pipeline, no GUI. Drops `PySide6/` and `shiboken6/`. |
| **Thin client** | core + gui | 682 MB | GUI only. Talks to a remote or containerised API via `--api-url` / `TRIAGE_API_URL`. |

The installer itself is **519 MB** (lzma2/max over a 2022 MB payload).

**Size reality, measured rather than assumed.** Subtracting the tiers gives the
cost of each component:

| Component | Size | Share |
| --- | --- | --- |
| `pipeline` (torch, transformers, chromadb, onnxruntime, scipy, sklearn, model) | 1340 MB | 66% |
| `gui` (PySide6 + shiboken6) | 624 MB | 31% |
| `core` (embeddable CPython, `triage`, pip/setuptools) | 58 MB | 3% |

The pipeline is the single biggest cost, but **Qt is not small either** — the
planning estimate that "CLI only" would save ~10% was wrong by a wide margin,
because PySide6-Addons and PySide6-Essentials unpack to roughly four times
their wheel sizes. Dropping the GUI saves 31%; dropping the pipeline saves 66%.
Only the thin client avoids shipping a machine-learning stack at all.

### One payload, three installs

The tiers are **not three builds**. `build_payload.ps1` produces one full
payload, and Inno Setup omits file groups at install time. Building three
payloads would pack PySide6 twice and torch twice for a ~4 GB installer.

In Inno's vocabulary a **Component** is a selectable chunk of files and a
**Type** is a named preset of components. `[Types]` gives the three tiers,
`[Components]` gives `core` / `gui` / `pipeline`, and every `[Files]` line
carries a `Components:` tag saying who owns it.

### The component manifest is computed, never written by hand

A hand-maintained "these packages are the pipeline" list is wrong the first
time a transitive dependency appears or disappears, and nothing tells you.
So `build_payload.ps1` asks pip:

```
CORE = pip install --no-deps --target <tmp> <our wheel>   ->  triage/ + dist-info
GUI  = pip install          --target <tmp> PySide6==...   ->  PySide6*, shiboken6*
FULL = what is actually in the payload's site-packages

pipeline = FULL - CORE - GUI - TOOLCHAIN
```

The probes use `--target` into throwaway directories, so this costs one small
download rather than a second 2 GB tree. The PySide6 requirement is read out of
`pyproject.toml`, so the probe cannot drift from the version actually
installed. `TOOLCHAIN` (`pip`, `setuptools`, `wheel`, `pkg_resources`,
`_distutils_hack`, `sitecustomize.py`) is the only hand-written set, and it is
build tooling rather than a dependency of anything the app imports.

The result is written to `build/installer/components.iss`, one `[Files]` line
per top-level `site-packages` entry, `#include`d by `alert-triage.iss`.
`build/installer/components.json` holds the same split in readable form — read
it when a tier installs the wrong thing.

---

## Assets: bundle the model, fetch the corpus

| Asset | Decision | Why |
| --- | --- | --- |
| `bge-small-en-v1.5` (~130 MB) | **bundled** | First launch works offline, and the model id is an *enforced* staleness-fingerprint field, so which model an install embeds with must never be ambiguous. Same call the Dockerfile makes. |
| MITRE ATT&CK bundle (~51 MB) | **fetched on first run** | `triage ingest` already downloads it from a pinned v19.1 URL. Bundling it would add 51 MB to freeze a corpus the user can refresh. |
| Chroma store | **built on first run** | It is derived data, and shipping one would bake a fingerprint that every version bump immediately stales. |

The bake prints a warning that Hugging Face's cache "does not support symlinks"
on this machine. That is expected on Windows without Developer Mode, and it is
convenient here: `huggingface_hub` falls back to copying, so the cache is plain
files that Inno Setup packs and the installer writes verbatim. The cost is that
the blob and the snapshot are two copies — measured 128 MB total, which is the
same order as the Docker image's baked layer.

`sitecustomize.py` is what makes the bundled model findable. It is a CPython
startup hook — the stdlib `site` module imports it before any user code runs —
and it sets `HF_HOME` to a path computed **relative to its own `__file__`**, so
the app works wherever the user installs it. Read the file; the reasoning for
rejecting a global env var and a wrapper `.cmd` is written there.

---

## MAX_PATH: the constraint that shapes the build layout

Windows limits a full path to 260 characters. Both ISCC (reading each file to
compress it) and Setup (writing it out) hit that limit, and so does the running
app. The payload comes uncomfortably close, because **torch's `dist-info`
carries a tree of vendored third-party licence texts nested ten directories
deep** — 196 characters of relative path in the worst case:

```
python\Lib\site-packages\torch-2.13.0+cpu.dist-info\licenses\third_party\kineto\
libkineto\third_party\dynolog\third_party\prometheus-cpp\3rdparty\civetweb\src\
third_party\duktape-1.8.0\LICENSE.txt
```

Those files are **not** candidates for deletion. They are how a redistribution
satisfies the notice requirements of the BSD-licensed libraries torch bundles,
so the budget has to come out of the prefix instead. Two changes buy back 23
characters:

- `SourceDir=..` in `[Setup]`, so ISCC does not prepend `packaging\..\` to
  every one of ~3000 `Source` paths (13 chars)
- the payload staged at `build\payload` rather than `build\installer\payload`
  (10 chars)

`LongPathsEnabled` is not a way out. It is a per-machine policy needing an
administrator, and assuming anything about the user's machine contradicts the
whole premise of a no-prerequisites installer. (ISCC is not long-path aware
regardless — this machine has the policy enabled and the compile still failed.)

Two guards make the constraint visible instead of mysterious:

1. `build_payload.ps1` measures the longest payload path before generating the
   manifest and **fails the build** if anything exceeds 259 characters. Without
   it, the symptom is ISCC stopping partway through compression with "The
   system cannot find the path specified" while naming a file that plainly
   exists.
2. It also computes how many characters that leaves for the **install
   directory** and writes it into `version.iss` as `MaxAppDirLen`. The wizard
   refuses a longer folder with an explanation, rather than letting the user
   discover a half-installed app.

Currently: longest source path 259 (exactly at the limit), leaving **62
characters** for the install folder against a ~53-character default. If the
checkout ever moves somewhere deeper, the build stops and says so — pass
`-PayloadDir C:\atr` to build from a short path.

---

## Deliberate details in `alert-triage.iss`

- **Per-user install** (`PrivilegesRequired=lowest`) into
  `%LOCALAPPDATA%\Programs\Alert Triage RAG`. No UAC prompt, no machine-wide
  state to leave behind, and removable by the user who installed it. An
  unsigned installer asking for admin is the worst of both worlds.
- **`AppId` must never change.** It is the identity Windows keys the uninstall
  entry on; changing it makes an upgrade install *alongside* the old copy —
  two Apps & Features entries and two 1.6 GB payloads.
- **`SolidCompression=no`.** Solid mode compresses better but forces the
  installer to decompress the whole stream to extract anything, so a thin
  install would stream ~1.7 GB to write ~300 MB. Per-file compression keeps a
  partial install proportional to what it installs.
- **CLI shim is `triage.cmd`, not the pip-generated `triage.exe`.** Smart App
  Control blocks console-script shims (it is why this project's own test runs
  use `python -m pytest`). A `.cmd` is plain text, invokes the interpreter
  directly, and works from any install directory via `%~dp0`. The generated
  `Scripts\` directory is deleted from the payload.
- **`triage.cmd` and the PATH task ship on every tier.** They used to be
  pipeline-only, because `cli.py` imported `ingest`/`query`/`serve` at module
  level and so needed torch just to print help. `cli.py` now dispatches
  lazily — it holds a registry of verb → module *name* and imports a verb's
  module only when that verb runs (`import triage.cli` measured 0.09 s vs 7.36 s
  for `import triage.query`). On a thin install `triage --help` and
  `triage desktop --api-url <remote>` work, and the three pipeline verbs exit
  with an explanation instead of a `ModuleNotFoundError`.
- **Two GUI shortcuts, selected by `Components: gui and pipeline` vs
  `gui and not pipeline`.** With the pipeline present, `triage/backend.py`
  probes `/health` and, finding nothing, spawns
  `sys.executable -m triage.serve` — which is the bundled `pythonw.exe`, whose
  environment *has* the pipeline. No Docker. Without the pipeline that spawn
  would die on `import torch` and be misreported as a stale store, so the thin
  tier's shortcut passes `--no-autostart`.
- **No auto-ingest.** Building the store is an explicit Start Menu item and an
  *unticked* post-install checkbox — the same decision the Docker path makes.
  Auto-ingesting would bury a multi-minute network operation inside
  "installing" and, worse, would mute the staleness fingerprint: the loud
  "re-run `triage ingest`" refusal would become a silent self-heal.
- **PATH is appended with `{olddata};{app}`** and removed entry-by-entry on
  uninstall. Replacing rather than appending is the classic installer bug that
  erases a user's PATH.
- **The uninstall prompt uses `SuppressibleMsgBox`, not `MsgBox`.** This was a
  real bug, caught by running an unattended uninstall: during
  `/VERYSILENT /SUPPRESSMSGBOXES` nobody is there to answer, so Inno answers —
  and plain `MsgBox` returns the *affirmative* default, **ignoring
  `MB_DEFBUTTON2`**. The "default to No" intent was inverted in exactly the
  automated path, and the test uninstall deleted the vector store and the
  53 MB ATT&CK bundle. `SuppressibleMsgBox` takes the silent answer as an
  explicit argument (`IDNO`). Generalises to any `[Code]` prompt guarding a
  destructive action: the interactive default and the suppressed default are
  different mechanisms, and you must set both.
- **Uninstall offers to delete the data directory**, defaulting to No.
  `%LOCALAPPDATA%\alert-triage-rag` holds the ATT&CK bundle and the vector
  store (~700 MB) and deliberately lives outside `{app}` so it survives
  upgrades.

---

## Verifying a build

`ruff`, `mypy` and `pytest` cover the application. The installer needs its own
manual pass, because none of it is exercised by the test suite:

1. **Payload sanity** — `build_payload.ps1` already imports `torch`, `numpy`,
   `chromadb`, `fastapi`, `sentence_transformers`, `PySide6` and `triage.cli`
   in the payload interpreter and fails the build if any of them break. This is
   the check a freeze cannot give you.
2. Install **each of the three tiers** into a separate directory and confirm
   the file groups: thin has no `torch/` and no `models/`; CLI has no
   `PySide6/`; desktop has everything.
3. **Desktop tier**: launch the shortcut and confirm the app starts its own
   backend and returns a verdict. Do not settle for "Docker wasn't running" —
   check the process tree. The backend should be
   `<install>\python\pythonw.exe -m triage.serve`, and `docker ps -a` should be
   empty. (It cannot be otherwise: `backend.is_frozen()` is `False` for an
   installed payload, so the Docker branch is unreachable — but verify, don't
   assume.)
4. **CLI tier**: `triage ingest` then `triage query "..."`. Ingest is the real
   test of the bundled model: `HF_HUB_OFFLINE=1` means a successful model load
   *proves* `sitecustomize` found the bundle. Expect 1607 + 10 = 1617 chunks,
   the same numbers the Docker build produces.
5. **Thin tier**: confirm the shortcut passes `--no-autostart`, that there is
   only one shortcut, and that the window opens.
6. **Uninstall** each and confirm `{app}` is gone (including the `__pycache__`
   directories Python created at runtime), the PATH entry is removed *and the
   user's other entries survive*, the Apps & Features entry has disappeared,
   and — importantly — **the data directory is still there**.
7. **The silent paths**, because they take different code: a silent uninstall
   must not delete the data directory (see `SuppressibleMsgBox` below), and
   `/DIR=` with an over-long path must be refused rather than half-installed.

Everything above is scriptable with Inno's silent switches, which is how it was
verified:

```powershell
setup.exe /VERYSILENT /SUPPRESSMSGBOXES /TYPE=thin /DIR=C:\atr-thin /LOG=thin.log
C:\atr-thin\unins000.exe /VERYSILENT /SUPPRESSMSGBOXES
```

Use short install paths (`C:\atr-thin`) — the wizard's length check is there
for a reason.

---

## Known limitations

- **The installer is unsigned.** Other machines will show a SmartScreen
  "Windows protected your PC" warning ("More info" → "Run anyway"). Removing it
  needs an Authenticode code-signing certificate (an OV cert is a few hundred
  USD/year and still needs reputation to build; an EV cert clears SmartScreen
  immediately). Not pursued — no interview value for a portfolio demo, and the
  trade-off is worth being able to explain.
- **No custom icon.** Shortcuts show the Python logo (`pythonw.exe`'s icon).
  Add a `.ico` and set `SetupIconFile` / `IconFilename` when there is one.
- **Not built in CI.** Like the PyInstaller `.exe`, this needs a Windows runner
  and would publish an unsigned ~1 GB binary. Parked deliberately.
- **`-NumpyPin 2.4.6` is the default** because Windows Smart App Control blocks
  the DLLs in numpy 2.5.x. That is a Windows *packaging* constraint, not a
  project one — the Linux container takes whatever torch resolves — so it lives
  in this script and **never** in `pyproject.toml`. Pass `-NumpyPin none` to
  disable.
- **Upgrades reinstall in place** (same `AppId`), and every version bump stales
  the store by design, so an upgrade is always followed by one `triage ingest`.
