# PyInstaller spec — build the desktop app into one standalone .exe.
#
# A .spec IS the build configuration (a Python file PyInstaller execs), checked
# into git so the build is reproducible and reviewable instead of living in a
# long command line. Build it with:
#
#     python -m PyInstaller packaging/alert-triage-desktop.spec
#
# WHAT PYINSTALLER DOES: it does not "compile" Python. It walks the import graph
# from the entry script, collects those modules plus the CPython interpreter and
# the needed native libraries (here: the Qt DLLs and platform plugins), and
# writes them next to a small bootloader. Running the .exe unpacks/loads that
# bundle and runs the entry script — which is why no Python install is needed on
# the target machine, and why the result is tens of MB rather than a few KB.
#
# ONEFILE (chosen): everything is packed INTO the .exe, so the artifact is a
# single double-clickable file. Trade-off: each launch unpacks the bundle to a
# temp directory first, so startup is a second or two slower than the onedir
# layout (a folder of exe + DLLs). For a hand-distributed demo app the single
# file is worth the startup cost; switch to onedir (add a COLLECT step) if
# launch speed ever matters more than tidiness.

from PyInstaller.building.api import EXE, PYZ
from PyInstaller.building.build_main import Analysis

# The pipeline has no business inside a GUI client. These excludes are a
# DELIBERATE ASSERTION of CLAUDE.md's thin-client rule: the desktop app talks to
# the API over HTTP and imports only PySide6 + triage.apiclient (stdlib), so
# dropping the heavy pipeline/web stacks must not break it. If a future import
# ever made the app reach into the pipeline, this build would fail loudly —
# which is exactly when you want to hear about it, not after shipping a 2 GB exe.
_PIPELINE_AND_SERVER = [
    "torch",
    "sentence_transformers",
    "transformers",
    "chromadb",
    "anthropic",
    "numpy",
    "scipy",
    "sklearn",
    "fastapi",
    "uvicorn",
    "starlette",
    "streamlit",
    "pandas",
    "pyarrow",
    "altair",
    "matplotlib",
    "IPython",
    "pytest",
]

# Qt ships far more than a form and some labels. PySide6-Addons alone (WebEngine,
# 3D, Charts, Multimedia, ...) is ~170 MB installed; none of it is imported here,
# so excluding it keeps the artifact to the Qt core/GUI/widgets we actually use.
_UNUSED_QT = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
]

a = Analysis(
    ["desktop_entry.py"],
    # Resolve `import triage` from the repo root (this spec lives in packaging/).
    pathex=[".."],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_PIPELINE_AND_SERVER + _UNUSED_QT,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="alert-triage-desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX compression is off: it meaningfully raises the odds of an antivirus /
    # Smart App Control false positive on an already-unsigned binary, to save
    # some MB we don't need to save.
    upx=False,
    runtime_tmpdir=None,
    # console=False -> a GUI app: no black console window behind the UI.
    # The cost is that stray stdout/stderr goes nowhere, so the app must report
    # errors IN the window (it does — see TriageWindow._show_error).
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
