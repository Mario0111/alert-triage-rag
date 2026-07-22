<#
.SYNOPSIS
    Build the shippable payload for the Windows installer, and compute the
    component manifest that splits it into installable tiers.

.DESCRIPTION
    WHAT A "PAYLOAD" IS HERE
    ------------------------
    packaging/alert-triage-desktop.spec builds a FROZEN executable: PyInstaller
    walks the import graph, packs the interpreter and modules into one .exe, and
    the .exe unpacks itself at launch. That works for the 47 MB thin client. It
    is a poor fit for the full pipeline: freezing torch is the classic multi-day
    failure mode, and its failures are RUNTIME failures (a missing hidden import
    surfaces when a user clicks a button, not when the build runs).

    So this installer ships something duller and far more predictable: a real
    Python installation with the app `pip install`ed into it, copied verbatim
    onto the user's machine. Nothing is repacked, rewritten, or analysed. What
    you test here is byte-for-byte what ships.

    WHY NOT A VENV
    --------------
    The obvious way to make "a folder with the app installed in it" is
    `python -m venv`. It does not survive being copied to another machine. A
    Windows venv contains a redirector `Scripts\python.exe`, `Lib\site-packages`,
    and `pyvenv.cfg` - but NOT the standard library. `pyvenv.cfg` records
    `home = <the base install>`, and the venv interpreter loads `Lib\`,
    `python3xx.dll` and every `.pyd` from there at startup. Ship the venv alone
    and it is a brick; the target machine has no base Python (that is the whole
    point of an installer).

    WHAT WE USE INSTEAD
    -------------------
    The python.org WINDOWS EMBEDDABLE PACKAGE: a ~10 MB zip holding python.exe,
    pythonw.exe, python311.dll, the stdlib as python311.zip, the C extensions
    (_ssl.pyd, _sqlite3.pyd, ...) and vcruntime140.dll. It is a complete,
    RELOCATABLE CPython - no registry keys, no installer, no PATH entry. It is
    the artifact python.org publishes for exactly this job, and it is what the
    well-known "portable" Windows AI apps ship, torch and all.

    It has two sharp edges, both handled below:

      1. No pip (ensurepip is stripped) -> bootstrapped once with get-pip.py.
         Note this MUST be run by the payload's own 3.11: wheels are tagged per
         interpreter version (cp311 vs cp314), so installing with the host
         Python - 3.14 on this machine - would fetch wheels the payload cannot
         load. That is also why `pip install --target` from the host is not an
         option for the heavy packages.

      2. `python311._pth`. Its presence switches the interpreter to an isolated
         path mode: PYTHONPATH, PYTHONHOME and the registry are ignored (good -
         the installed app is immune to the user's other Pythons) and sys.path
         is EXACTLY the file's contents. Crucially, `site` is not imported
         unless the file says so, and with `site` off there is no
         `Lib\site-packages` on sys.path and `sitecustomize.py` never runs -
         which would silently defeat the bundled-model wiring. The file is
         rewritten below.

    WHAT THIS SCRIPT PRODUCES
    -------------------------
        build/installer/payload/          the tree Inno Setup packs
          triage.cmd                      CLI shim (see note below)
          python/                         the embeddable CPython + site-packages
          models/hf/                      the baked bge-small-en-v1.5 cache
        build/installer/components.iss    generated [Files] lines, #include'd
                                          by packaging/alert-triage.iss
        build/installer/components.json   the same split, human-readable

    Everything lands under build/, which is already gitignored: this is build
    output, not source. The SOURCE of the build is this script plus the .iss.

.PARAMETER PythonVersion
    Which embeddable CPython to ship. 3.11 deliberately matches the Dockerfile
    and pyproject's requires-python floor: it is the version with the widest
    wheel coverage for this dependency set, and it is a real CI matrix job.

.PARAMETER NumpyPin
    Force a specific numpy after the install, or 'none' to take whatever torch
    resolves.

    WHY THIS EXISTS AND WHY IT IS NOT IN pyproject.toml: Windows Smart App
    Control blocks the DLLs shipped in numpy 2.5.x, so on an SAC-enforced
    machine `import numpy` fails and the whole pipeline with it. That is a
    WINDOWS PACKAGING constraint, not a property of the project - the Linux
    container takes whatever torch resolves and is fine. Pinning it in
    pyproject.toml would impose one platform's quirk on every install forever;
    pinning it in the Windows packaging script keeps the blast radius exactly
    where the problem is.

.PARAMETER Clean
    Delete an existing payload first. Off by default so a re-run after a failed
    download does not re-fetch ~2 GB over a flaky link.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build_payload.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build_payload.ps1 -Clean -NumpyPin none
#>

[CmdletBinding()]
param(
    [string] $PythonVersion = '3.11.9',
    [string] $NumpyPin      = '2.4.6',
    [string] $PayloadDir    = '',
    [switch] $Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Invoke-WebRequest's progress bar costs more wall-clock than the download on
# large files in Windows PowerShell 5.1.
$ProgressPreference = 'SilentlyContinue'

# --- Layout -----------------------------------------------------------------
$RepoRoot   = Split-Path -Parent $PSScriptRoot
$BuildRoot  = Join-Path $RepoRoot 'build\installer'
$CacheDir   = Join-Path $BuildRoot 'cache'      # downloads, kept across runs
# DELIBERATELY SHORT, and not under $BuildRoot: this is the only directory
# whose path length matters. Windows' MAX_PATH is 260 characters, and the
# deepest file in the payload is a vendored third-party licence text inside
# torch's dist-info, 196 characters of relative path on its own. Every
# character of prefix here is a character stolen from the user's install
# directory. See the pre-flight check further down.
$Payload    = if ($PayloadDir) { $PayloadDir } else { Join-Path $RepoRoot 'build\payload' }
$PyDir      = Join-Path $Payload  'python'
$SitePkgs   = Join-Path $PyDir    'Lib\site-packages'
$ModelHome  = Join-Path $Payload  'models\hf'
$WheelDir   = Join-Path $BuildRoot 'wheel'
$ProbeDir   = Join-Path $BuildRoot 'probe'      # throwaway component probes

$PyTag      = 'python' + ($PythonVersion.Split('.')[0..1] -join '')  # python311
$PyExe      = Join-Path $PyDir 'python.exe'

$EmbedUrl   = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$EmbedZip   = Join-Path $CacheDir "python-$PythonVersion-embed-amd64.zip"
$GetPipUrl  = 'https://bootstrap.pypa.io/get-pip.py'
$GetPipPy   = Join-Path $CacheDir 'get-pip.py'

# Mirrors the Dockerfile's hardening. The link on this machine has proven
# flaky enough to exhaust pip's default 5 retries mid-wheel.
$PipNet     = @('--retries', '10', '--timeout', '120')

$EmbedModel = 'BAAI/bge-small-en-v1.5'   # must match ingest.DEFAULT_EMBED_MODEL

$script:StepNo = 0
function Write-Step([string] $Message) {
    $script:StepNo++
    Write-Host ''
    Write-Host ("[{0}] {1}" -f $script:StepNo, $Message) -ForegroundColor Cyan
}

# Native executables do not raise on failure - PowerShell only sets
# $LASTEXITCODE - so every external call is checked. Fail loudly (CLAUDE.md):
# a half-installed payload that compiles into an installer is far worse than a
# build that stops here.
function Invoke-Checked([string] $Exe, [string[]] $Arguments) {
    Write-Host "    > $Exe $($Arguments -join ' ')" -ForegroundColor DarkGray
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Exe $($Arguments -join ' ')"
    }
}

function Get-Cached([string] $Url, [string] $Destination) {
    if (Test-Path $Destination) {
        Write-Host "    cached: $Destination" -ForegroundColor DarkGray
        return
    }
    Write-Host "    downloading $Url" -ForegroundColor DarkGray
    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
}

function Get-DirSizeMB([string] $Path) {
    if (-not (Test-Path $Path)) { return 0 }
    $bytes = (Get-ChildItem -LiteralPath $Path -Recurse -File -Force |
              Measure-Object -Property Length -Sum).Sum
    if ($null -eq $bytes) { return 0 }
    return [math]::Round($bytes / 1MB, 1)
}

# ---------------------------------------------------------------------------
Write-Host "alert-triage-rag installer payload" -ForegroundColor Green
Write-Host "  repo        : $RepoRoot"
Write-Host "  payload     : $Payload"
Write-Host "  python      : $PythonVersion (embeddable, amd64)"
Write-Host "  numpy pin   : $NumpyPin"

if ($Clean -and (Test-Path $Payload)) {
    Write-Step 'Cleaning previous payload'
    Remove-Item -LiteralPath $Payload -Recurse -Force
}
foreach ($d in @($CacheDir, $Payload, $WheelDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

# --- 1. The interpreter -----------------------------------------------------
Write-Step "Fetching the embeddable CPython $PythonVersion"
Get-Cached -Url $EmbedUrl -Destination $EmbedZip
# Printed so it can be pinned later if this build is ever hardened against a
# compromised mirror. Not enforced yet - see packaging/README.md.
Write-Host ("    sha256: " + (Get-FileHash -LiteralPath $EmbedZip -Algorithm SHA256).Hash) -ForegroundColor DarkGray

if (-not (Test-Path $PyExe)) {
    Write-Step 'Unpacking the interpreter'
    Expand-Archive -LiteralPath $EmbedZip -DestinationPath $PyDir -Force
}

Write-Step "Enabling site-packages and sitecustomize in $PyTag._pth"
# THE EDIT THAT MAKES THE PAYLOAD WORK. Shipped as:
#     python311.zip
#     .
#     # import site
# Rewritten to add Lib\site-packages and to turn `site` back on. Without the
# `import site` line the stdlib site module never runs, which means: no .pth
# file processing (torch and setuptools both rely on it), no site-packages on
# sys.path (so `import triage` fails), and no sitecustomize import (so the
# bundled model is never found). Isolated mode is KEPT - sys.path stays exactly
# these entries, so the installed app cannot be broken by another Python on the
# machine setting PYTHONPATH.
$PthFile = Join-Path $PyDir "$PyTag._pth"
@(
    "$PyTag.zip"
    '.'
    'Lib\site-packages'
    ''
    '# Enabled deliberately by packaging/build_payload.ps1 - see that file.'
    'import site'
) | Set-Content -LiteralPath $PthFile -Encoding ASCII
Get-Content -LiteralPath $PthFile | ForEach-Object { Write-Host "    | $_" -ForegroundColor DarkGray }

if (-not (Test-Path $SitePkgs)) { New-Item -ItemType Directory -Path $SitePkgs -Force | Out-Null }

# --- 2. pip -----------------------------------------------------------------
Write-Step 'Bootstrapping pip into the payload interpreter'
Get-Cached -Url $GetPipUrl -Destination $GetPipPy
if (Test-Path (Join-Path $SitePkgs 'pip')) {
    Write-Host '    pip already present' -ForegroundColor DarkGray
} else {
    Invoke-Checked $PyExe @($GetPipPy, '--no-warn-script-location')
}
Invoke-Checked $PyExe @('-m', 'pip', '--version')

# --- 3. The wheel -----------------------------------------------------------
Write-Step 'Building the alert-triage-rag wheel'
# Built by the PAYLOAD interpreter via `pip wheel`, not by the host - so this
# script needs nothing installed on the host machine at all. pip creates an
# isolated build environment from [build-system] in pyproject.toml (the same
# PEP 517 path the Dockerfile's builder stage and the release workflow take),
# so the wheel here is the same artifact those produce. --no-deps because we
# only want OUR wheel out of this step; the dependencies are installed below.
Get-ChildItem -LiteralPath $WheelDir -Filter '*.whl' -ErrorAction SilentlyContinue |
    Remove-Item -Force
Invoke-Checked $PyExe (@('-m', 'pip', 'wheel', '--no-deps', '--wheel-dir', $WheelDir) + $PipNet + @($RepoRoot))
$Wheel = (Get-ChildItem -LiteralPath $WheelDir -Filter 'alert_triage_rag-*.whl' | Select-Object -First 1).FullName
if (-not $Wheel) { throw "No wheel was produced in $WheelDir" }
Write-Host "    wheel: $(Split-Path -Leaf $Wheel)" -ForegroundColor DarkGray

# --- 4. Dependencies --------------------------------------------------------
Write-Step 'Installing CPU-only torch'
# Same cost control as the Dockerfile and CI, for the same reason: the default
# resolution can pull a CUDA build carrying gigabytes of GPU libraries this app
# never touches (it runs a 130 MB embedding model on the CPU). Installing torch
# FIRST from PyTorch's cpu index means the app install below sees it satisfied
# and leaves it alone.
Invoke-Checked $PyExe (@('-m', 'pip', 'install') + $PipNet +
    @('--index-url', 'https://download.pytorch.org/whl/cpu', 'torch'))

Write-Step 'Installing the app with the [desktop] extra'
# ONE payload contains everything: pipeline + GUI. The tiers are not separate
# builds - they are subsets of this tree, selected at install time by the
# component manifest computed in step 7. Building three payloads would mean
# ~4 GB of installer with PySide6 packed three times.
# --no-warn-script-location: the Scripts\ shims are deleted below anyway.
Invoke-Checked $PyExe (@('-m', 'pip', 'install') + $PipNet +
    @('--no-warn-script-location', ($Wheel + '[desktop]')))

# Then reinstall OUR wheel unconditionally. Without this, a re-run after a code
# edit is a silent no-op: pip compares versions, sees 0.4.0 == 0.4.0 and skips
# the wheel ("already installed with the same version"), so the payload would
# quietly keep stale code while every log line said success. --no-deps because
# the resolve above already placed every dependency.
Invoke-Checked $PyExe (@('-m', 'pip', 'install') + $PipNet +
    @('--no-warn-script-location', '--force-reinstall', '--no-deps', $Wheel))

if ($NumpyPin -and $NumpyPin -ne 'none') {
    Write-Step "Pinning numpy==$NumpyPin (Smart App Control workaround - see the -NumpyPin help)"
    Invoke-Checked $PyExe (@('-m', 'pip', 'install') + $PipNet + @("numpy==$NumpyPin"))
}

Write-Step 'Verifying the payload interpreter can import the pipeline'
# Fail HERE, at build time, rather than on a user's machine. This is the whole
# argument for shipping an installed tree instead of a freeze: the check is a
# real import in the real interpreter, not a static import-graph guess.
#
# Written to a FILE rather than passed with `python -c`: Windows PowerShell
# splits a multi-line string into one argument per line when handing it to a
# native executable, so the interpreter receives a truncated program. A script
# file also leaves the check on disk to re-run by hand while debugging.
$VerifyPy = Join-Path $BuildRoot 'verify_payload.py'
@'
"""Import everything the shipped tiers rely on, in the payload interpreter."""
import importlib.metadata as md

import chromadb  # noqa: F401
import fastapi  # noqa: F401
import numpy
import torch
from PySide6 import QtWidgets  # noqa: F401
from sentence_transformers import SentenceTransformer  # noqa: F401

import triage.cli  # noqa: F401
import triage.desktop_launch  # noqa: F401

print("    alert-triage-rag", md.version("alert-triage-rag"))
print("    torch", torch.__version__, "| numpy", numpy.__version__)
'@ | Set-Content -LiteralPath $VerifyPy -Encoding ASCII
Invoke-Checked $PyExe @($VerifyPy)

# --- 5. The model -----------------------------------------------------------
Write-Step "Baking $EmbedModel into the payload"
# DECISION (mirrors the Dockerfile, PLAN.md Phase 10): bundle the model rather
# than download it on first run. ~130 MB buys a first launch with zero network
# dependency, and - the real reason - the model id is an ENFORCED staleness
# fingerprint field, so which model this install embeds with must never be
# ambiguous.
if (-not (Test-Path $ModelHome)) { New-Item -ItemType Directory -Path $ModelHome -Force | Out-Null }
# sitecustomize.py sets these for the SHIPPED app; here they are set explicitly
# because the bake is the one moment we must be ONLINE. (sitecustomize uses
# setdefault, so these win even after it is copied in.)
$env:HF_HOME = $ModelHome
$env:HF_HUB_OFFLINE = '0'
Invoke-Checked $PyExe @('-c', "from sentence_transformers import SentenceTransformer; SentenceTransformer('$EmbedModel')")
Remove-Item Env:\HF_HUB_OFFLINE
Write-Host ("    model cache: {0} MB" -f (Get-DirSizeMB $ModelHome)) -ForegroundColor DarkGray

# --- 6. App-side files ------------------------------------------------------
Write-Step 'Installing sitecustomize.py and the CLI shim'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'sitecustomize.py') -Destination $SitePkgs -Force

# WHY A .cmd AND NOT THE triage.exe pip GENERATED: `[project.scripts]` makes pip
# write a tiny launcher .exe into Scripts\. Windows Smart App Control blocks
# those console-script shims outright (it is why this project's own test runs
# use `python -m pytest`). A .cmd is a plain text file - nothing to block - and
# it invokes the interpreter directly, which is the same call path the
# `python -m triage.cli` entry point already supports. `%~dp0` is the directory
# of the .cmd itself, so this works wherever the user installs.
@'
@echo off
rem alert-triage-rag CLI shim. Installed at the root of the app directory,
rem which is what the optional "Add to PATH" task registers.
rem `python -m triage.cli` is used instead of the Scripts\triage.exe console
rem shim because Windows Smart App Control blocks those.
"%~dp0python\python.exe" -m triage.cli %*
'@ | Set-Content -LiteralPath (Join-Path $Payload 'triage.cmd') -Encoding ASCII

# Drop the generated console shims. Nothing in the shipped app calls them (the
# GUI shortcut targets pythonw.exe directly, triage.cmd covers the CLI, and the
# autostarted backend is spawned as `sys.executable -m triage.serve`), they are
# blocked by Smart App Control anyway, and leaving them would put a mixture of
# pipeline-only and GUI-only executables in one directory that the component
# split could not cleanly divide.
$ScriptsDir = Join-Path $PyDir 'Scripts'
if (Test-Path $ScriptsDir) { Remove-Item -LiteralPath $ScriptsDir -Recurse -Force }

# --- 7. MAX_PATH pre-flight -------------------------------------------------
Write-Step 'Checking payload paths against MAX_PATH'
# WHY THIS CHECK EXISTS: Windows' classic path limit is 260 characters. Both
# ISCC (reading each file to compress it) and Setup (writing it out) hit it.
# The OS-wide LongPathsEnabled policy can lift the limit, but it needs an
# administrator to set it and we cannot assume anything about a user's machine
# - which is the entire premise of a no-prerequisites installer. So the payload
# must simply fit.
#
# It very nearly does not: torch's dist-info carries a tree of vendored
# third-party licence texts nested ten directories deep. Those are NOT
# candidates for deletion - they are how a redistribution satisfies the BSD
# notice requirements of the libraries torch bundles - so the budget has to
# come out of the prefix instead. That is why $Payload above is `build\payload`
# rather than `build\installer\payload`, and why the .iss uses SourceDir=..
# instead of writing `packaging\..\` into every one of 3000 Source lines.
#
# Without this check the symptom is ISCC stopping partway through compression
# with "The system cannot find the path specified" and naming a file that
# plainly exists - which is a genuinely baffling half hour.
$MaxPath = 259
$worst = Get-ChildItem -LiteralPath $Payload -Recurse -File -Force |
         Sort-Object { $_.FullName.Length } -Descending |
         Select-Object -First 1
$over = @(Get-ChildItem -LiteralPath $Payload -Recurse -File -Force |
          Where-Object { $_.FullName.Length -gt $MaxPath })
Write-Host ("    longest source path: {0} chars ({1} to spare)" -f
    $worst.FullName.Length, ($MaxPath - $worst.FullName.Length)) -ForegroundColor DarkGray
# The install-time budget is what the USER's chosen directory has to live
# within, so report it as the number that actually constrains them.
$relLongest = $worst.FullName.Length - $Payload.Length
$script:MaxAppDirLen = $MaxPath - $relLongest
Write-Host ("    leaves {0} chars for the install directory (default is ~53)" -f
    $script:MaxAppDirLen) -ForegroundColor DarkGray
if ($over.Count -gt 0) {
    $over | Select-Object -First 5 | ForEach-Object { Write-Host "    $($_.FullName)" }
    throw ("$($over.Count) payload file(s) exceed $MaxPath characters, so ISCC " +
           'cannot compress them. Move the checkout to a shorter path, or pass ' +
           'a shorter -PayloadDir.')
}

# --- 8. The component manifest ----------------------------------------------
Write-Step 'Computing the component manifest'
# THE RULE (PLAN.md): the split is COMPUTED, never hand-written. A hand-listed
# "these packages are the pipeline" would be wrong the first time a dependency
# gained or dropped a transitive dep, and nothing would tell us.
#
# Three sets, all produced by pip itself, all with the payload's own 3.11:
#   CORE  = `pip install --no-deps --target ...` of our wheel  -> triage/ + dist-info
#   GUI   = `pip install --target ...` of PySide6              -> PySide6*, shiboken6*
#   FULL  = what is actually in the payload's site-packages
# and therefore
#   PIPELINE = FULL - CORE - GUI - TOOLCHAIN
#
# The probes use --target into a throwaway directory, so this costs one small
# download instead of building a second 2 GB tree.
if (Test-Path $ProbeDir) { Remove-Item -LiteralPath $ProbeDir -Recurse -Force }
$ProbeCore = Join-Path $ProbeDir 'core'
$ProbeGui  = Join-Path $ProbeDir 'gui'

Invoke-Checked $PyExe (@('-m', 'pip', 'install') + $PipNet +
    @('--no-deps', '--target', $ProbeCore, $Wheel))
# Read the pinned PySide6 requirement out of pyproject.toml rather than
# repeating it here - one source of truth, and the probe cannot drift from the
# version actually installed above.
# Via a file for the same reason as verify_payload.py above: PowerShell breaks
# a multi-line `-c` program apart on its way to a native executable.
$ReadExtraPy = Join-Path $BuildRoot 'read_desktop_extra.py'
@"
import sys, tomllib
with open(sys.argv[1], 'rb') as fh:
    data = tomllib.load(fh)
print(data['project']['optional-dependencies']['desktop'][0])
"@ | Set-Content -LiteralPath $ReadExtraPy -Encoding ASCII
$PySideReq = (& $PyExe $ReadExtraPy (Join-Path $RepoRoot 'pyproject.toml')) | Out-String
if ($LASTEXITCODE -ne 0) { throw 'Could not read the [desktop] extra from pyproject.toml' }
$PySideReq = $PySideReq.Trim()
Write-Host "    gui probe: $PySideReq" -ForegroundColor DarkGray
Invoke-Checked $PyExe (@('-m', 'pip', 'install') + $PipNet + @('--target', $ProbeGui, $PySideReq))

function Get-TopLevelNames([string] $Path) {
    # Directory noise pip leaves behind that is not a distribution.
    $skip = @('__pycache__', 'bin', 'Scripts')
    Get-ChildItem -LiteralPath $Path -Force |
        Where-Object { $skip -notcontains $_.Name } |
        ForEach-Object { $_.Name }
}

$FullSet = @(Get-TopLevelNames $SitePkgs)
$CoreSet = @(Get-TopLevelNames $ProbeCore)
$GuiSet  = @(Get-TopLevelNames $ProbeGui)

# The ONLY hand-written list, and deliberately so: these are the packaging
# toolchain, not dependencies of anything the app imports. They appear in the
# payload because pip put them there, so they belong to no probe and would
# otherwise be misfiled as pipeline. Keeping pip in the payload lets an
# operator repair or extend an install in place.
$Toolchain = @('pip', 'setuptools', 'wheel', 'pkg_resources', '_distutils_hack',
               'sitecustomize.py', 'distutils-precedence.pth')

function Test-Toolchain([string] $Name, [string[]] $Tools) {
    # Distributions appear twice in site-packages: as the importable package
    # (`pip`) and as its metadata directory (`pip-25.3.dist-info`). Match both,
    # exactly - no prefix guessing, so `pipdeptree` could never be swept up.
    foreach ($tool in $Tools) {
        if ($Name -eq $tool -or $Name -like "$tool-*.dist-info") { return $true }
    }
    return $false
}

$assign = [ordered]@{}
foreach ($name in $FullSet) {
    if ($CoreSet -contains $name) { $assign[$name] = 'core' }
    elseif ($GuiSet -contains $name) { $assign[$name] = 'gui' }
    elseif (Test-Toolchain $name $Toolchain) { $assign[$name] = 'core' }
    else { $assign[$name] = 'pipeline' }
}

$counts = $assign.Values | Group-Object | Sort-Object Name
foreach ($c in $counts) { Write-Host ("    {0,-9} {1,4} entries" -f $c.Name, $c.Count) }

# --- 8. Emit the Inno Setup fragment ---------------------------------------
Write-Step 'Writing version.iss / components.iss / components.json'

# The version the installer advertises comes from the INSTALLED package, read
# back out of the payload with importlib.metadata - the same call
# fingerprint.app_version() makes at runtime. Taking it from there rather than
# re-parsing pyproject.toml means the installer literally cannot disagree with
# the code it installs. (Reading it from the payload also sidesteps the stale
# alert_triage_rag.egg-info in the repo root, which shadows the real metadata
# whenever cwd is the repo.)
$AppVersion = & $PyExe -c "import importlib.metadata as md; print(md.version('alert-triage-rag'))"
if ($LASTEXITCODE -ne 0) { throw 'Could not read the installed alert-triage-rag version' }
$AppVersion = $AppVersion.Trim()
Write-Host "    version: $AppVersion" -ForegroundColor DarkGray
@(
    '; GENERATED by packaging/build_payload.ps1 - do not edit.'
    ';'
    '; AppVersion is read from the payload with importlib.metadata, i.e. the'
    '; same source fingerprint.app_version() uses, so the installer and the'
    '; code it installs can never disagree.'
    ';'
    '; Payload is emitted rather than hard-coded in the .iss so that the'
    '; -PayloadDir build option actually reaches the compiler.'
    ';'
    '; MaxAppDirLen is MEASURED, not guessed: 259 minus the longest relative'
    '; path in the payload. The wizard refuses a longer install folder, which'
    '; turns a MAX_PATH truncation into a sentence the user can act on.'
    "#define AppVersion `"$AppVersion`""
    "#define Payload `"$Payload`""
    "#define MaxAppDirLen $($script:MaxAppDirLen)"
) | Set-Content -LiteralPath (Join-Path $BuildRoot 'version.iss') -Encoding ASCII

$rel = 'python\Lib\site-packages'
$lines = New-Object System.Collections.Generic.List[string]
$lines.Add('; GENERATED by packaging/build_payload.ps1 - do not edit.')
$lines.Add('; One [Files] entry per top-level site-packages item, tagged with the')
$lines.Add('; component that owns it. #include''d from packaging/alert-triage.iss.')
$lines.Add('')
foreach ($name in $assign.Keys) {
    $component = $assign[$name]
    $source = Join-Path (Join-Path $Payload $rel) $name
    if (Test-Path -LiteralPath $source -PathType Container) {
        $lines.Add("Source: `"{#Payload}\$rel\$name\*`"; DestDir: `"{app}\$rel\$name`"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: $component")
    } else {
        $lines.Add("Source: `"{#Payload}\$rel\$name`"; DestDir: `"{app}\$rel`"; Flags: ignoreversion; Components: $component")
    }
}
# ASCII, not UTF8: Set-Content -Encoding UTF8 in Windows PowerShell 5.1 writes a
# BYTE ORDER MARK, and a BOM in the middle of a file (this one is #include'd
# into [Files]) is a stray character, not a header. Distribution names are
# ASCII by packaging rules, so nothing is lost.
$IssOut = Join-Path $BuildRoot 'components.iss'
$lines | Set-Content -LiteralPath $IssOut -Encoding ASCII

$assign.GetEnumerator() |
    ForEach-Object { [pscustomobject]@{ name = $_.Key; component = $_.Value } } |
    ConvertTo-Json -Depth 3 |
    Set-Content -LiteralPath (Join-Path $BuildRoot 'components.json') -Encoding ASCII

# --- 9. Report --------------------------------------------------------------
Write-Step 'Payload summary'
$sizes = [ordered]@{
    'interpreter + stdlib' = (Get-DirSizeMB $PyDir) - (Get-DirSizeMB $SitePkgs)
    'site-packages'        = (Get-DirSizeMB $SitePkgs)
    'bundled model'        = (Get-DirSizeMB $ModelHome)
    'TOTAL'                = (Get-DirSizeMB $Payload)
}
foreach ($k in $sizes.Keys) { Write-Host ("    {0,-22} {1,8} MB" -f $k, $sizes[$k]) }

Write-Host ''
Write-Host 'Payload built. Next:' -ForegroundColor Green
Write-Host '    & "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\alert-triage.iss'
