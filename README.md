# alert-triage-rag

A retrieval-augmented triage assistant for SOC/MDR alerts. An analyst describes
an alert in natural language; the system retrieves relevant **MITRE ATT&CK**
techniques and internal **runbook** steps, then produces a structured, grounded
triage verdict (JSON) with **citations** back to the source material.

Built deliberately *without* orchestration frameworks (no LangChain /
LlamaIndex)

## Architecture

Two phases:

1. **Ingestion** (`triage/ingest.py`) — load corpus → chunk → embed locally →
   persist to Chroma. Run once, or whenever the corpus changes.
2. **Query** (`triage/query.py`) — embed alert text → retrieve top-k chunks →
   build a grounded prompt → call Claude → validate JSON against the schema.

```
ATT&CK STIX bundle (auto-downloaded) ─┐
                                      ├─ ingest ──► Chroma (per-user data dir) ──► query ──► triage verdict (JSON)
packaged runbooks (triage/corpus/) ───┘                                              ▲
                                                                               alert description
```

**Corpus**
- MITRE ATT&CK Enterprise (STIX/JSON), chunked **per technique field** —
  description and detection are embedded as separate chunks (each split
  further if it exceeds the embedder's 512-token window), all tagged with the
  same `attack_id`.
- Hand-written runbooks (`triage/corpus/runbooks/*.md`, shipped inside the
  package), split with the same token-budgeted splitter and tagged with the
  runbook filename.
- Retrieval merges sibling chunks of the same document (by `attack_id` /
  filename) back into one complete, citable unit, so a verdict cites a whole
  technique or runbook rather than a fragment.
- A runbook is always among the sources: if none places in the similarity
  top-k, the nearest one is appended — flagged as `backfilled` so the model
  judges its relevance instead of assuming it.

**Stack:** Python 3.11+ · `sentence-transformers` (`bge-small-en-v1.5`, local) ·
`chromadb` · `anthropic` (Claude) · `pydantic` v2 · `fastapi` + `uvicorn`.
Installable CLI + HTTP service.

**Staleness guard:** ingestion stamps the store with a fingerprint (app
version, embedding model, chunking parameters, corpus identity). Query and
serve refuse a store whose fingerprint no longer matches the running code —
"re-run `triage ingest`" — so an app upgrade can never silently serve verdicts
from an index built by older code.

## Install

The recommended installer is [pipx](https://pipx.pypa.io/): it creates a
dedicated virtual environment for the app, installs it there, and puts just
the `triage` command on your PATH. You get an isolated install (this
project's pinned dependencies can never conflict with anything else on your
machine) without ever activating a venv yourself — the right tool for
end-user CLI apps, where `pip install` into a shared environment is the
classic way to break two projects at once.

```bash
# once per machine
pip install --user pipx
pipx ensurepath          # then open a new terminal

# install the app (from a clone, or straight from GitHub)
pipx install git+https://github.com/Mario0111/alert-triage-rag
# or, from a local checkout:
pipx install .
```

(Equivalent without pipx: create a fresh venv, activate it, `pip install .` —
that is exactly what pipx automates.)

## Quickstart (clean machine)

```bash
# 1. API key for the query phase (generation runs on Claude)
export ANTHROPIC_API_KEY=sk-ant-...   # Windows (PowerShell): $env:ANTHROPIC_API_KEY="..."

# 2. Build the vector store. First run downloads the MITRE ATT&CK Enterprise
#    bundle (~51 MB, pinned to v19.1) and the bge-small-en-v1.5 embedding
#    model (~130 MB), then embeds the corpus locally — expect a few minutes.
triage ingest

# 3. Triage an alert
triage query "Multiple failed logons followed by a successful logon from a new country, then a PowerShell download cradle on the host."
```

Output is a structured triage verdict (JSON) with citations back to the ATT&CK
techniques and runbook steps used.

## Usage

**`triage ingest`** — build the vector store. Fetches the ATT&CK bundle into
the data directory when missing (`--refresh-attack` re-downloads it, e.g.
after bumping the pinned release). Each run rebuilds the collection from
scratch (it drops any existing one first), so it is always safe to re-ingest
after a corpus or chunking change without leaving stale documents behind.

```bash
triage ingest
# options: --attack-file --refresh-attack --runbooks-dir --db-dir
#          --collection --embed-model --batch-size
```

**`triage query`** — triage one alert.

```bash
triage query "Scheduled task created remotely via schtasks from a workstation to a domain controller."
# options: --top-k --db-dir --collection --gen-model --rewrite-model --no-rewrite
```

**`triage serve`** — run the triage HTTP API. This is the single integration
surface: the upcoming UI and SIEM webhook are thin clients of the same
endpoint, backed by the same pipeline as `triage query`. The embedding model
and Chroma collection load once at startup; a missing or stale store aborts
startup with the `triage ingest` remedy instead of serving errors. Binds
`127.0.0.1` by default — expose it deliberately with `--host 0.0.0.0`.

```bash
triage serve                     # http://127.0.0.1:8000, interactive docs at /docs
# options: --host --port --db-dir --collection --embed-model --gen-model
#          --rewrite-model --no-rewrite
```

```bash
# POST an alert, get the verdict JSON (same schema as the CLI output):
curl -s http://127.0.0.1:8000/triage \
  -H "Content-Type: application/json" \
  -d '{"alert": "Multiple failed logons followed by a successful logon from a new country."}'
# Windows (PowerShell):
#   Invoke-RestMethod http://127.0.0.1:8000/triage -Method Post -ContentType "application/json" `
#     -Body '{"alert": "..."}'
```

Responses: `200` with the verdict; `422` if the request body fails validation;
`502` if the upstream model produced nothing the service can vouch for
(API failure, refusal, or a verdict that failed grounding validation).

## Run it with Docker

The container is the zero-Python path: the image ships the pinned dependencies,
CPU-only torch, and the `bge-small-en-v1.5` embedding model **baked in**, so a
cold start needs no downloads at all. The Chroma store lives on a named volume,
so it survives container and image upgrades.

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # PowerShell: $env:ANTHROPIC_API_KEY="..."

# 1. Build the store (one-off container; downloads the ATT&CK bundle into the
#    volume and embeds the corpus — a few minutes)
docker compose run --rm api ingest

# 2. Serve the API
docker compose up
```

`GET /health` and `POST /triage` then answer on `http://127.0.0.1:8000`, exactly
as with `triage serve` — the container runs the same CLI.

Notes:

- **The API key is passed by environment only.** `docker-compose.yml` names the
  variable but never holds a value, so the key is never in the image, the file,
  or git.
- **Ingest is a deliberate one-off, not automatic on startup.** After upgrading
  to a new image version the API will *refuse to start*, naming the
  fingerprint mismatch — the store was built by different code. Re-run step 1
  and `docker compose up` again. That refusal is the staleness guard working as
  designed, so the container does not auto-heal it silently.
- The published image can be used directly instead of building:
  `docker pull ghcr.io/mario0111/alert-triage-rag:latest`.
- `docker compose down` stops the service and keeps the store;
  `docker compose down -v` also deletes the volume (a full re-ingest afterwards).

## Data locations

The app never assumes a repo checkout. Mutable data (the Chroma store, the
downloaded ATT&CK bundle) lives in the per-user data directory (via
`platformdirs`); the runbooks ship read-only inside the package.

| Platform | Data directory |
|---|---|
| Windows | `%LOCALAPPDATA%\alert-triage-rag` |
| Linux | `~/.local/share/alert-triage-rag` |
| macOS | `~/Library/Application Support/alert-triage-rag` |

Override the root with the `TRIAGE_DATA_DIR` environment variable, or
individual locations with `--db-dir` / `--attack-file` (flags win over the
env var, which wins over the default).

## Development setup

From a checkout, install **editable** so code edits apply without
reinstalling, and point the data dir at the repo to reproduce the classic
layout (`./chroma_db`, `corpus/attack/`):

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .

export TRIAGE_DATA_DIR=$PWD     # Windows (PowerShell): $env:TRIAGE_DATA_DIR=$PWD
triage ingest                    # or: python -m triage.ingest
```

## Demo

<!-- TODO: add a screenshot of a real triage run here -->
_Demo screenshot coming soon._

## Project layout

```
pyproject.toml        packaging: metadata, pinned deps, `triage` entry point
Dockerfile            multi-stage image build (wheel -> slim runtime, model baked in)
.dockerignore         what never enters the build context
docker-compose.yml    API service + Chroma volume + /health healthcheck
.github/workflows/
  ci.yml              ruff + mypy + pytest on every push/PR
  release.yml         on a v* tag: wheel -> GitHub Release, image -> GHCR
triage/               core package
  cli.py              the `triage` command (argparse subcommand dispatch)
  paths.py            data-directory resolution (platformdirs + overrides)
  ingest.py           ingestion pipeline (plumbing, ATT&CK auto-fetch)
  stix.py             ATT&CK STIX/JSON → flat Technique records
  chunk.py            chunking strategy (per-technique + runbooks)
  retrieve.py         top-k retrieval + sibling-chunk merge
  rewrite.py          alert → retrieval-optimized query rewrite
  query.py            query pipeline + grounding prompt
  fingerprint.py      store staleness fingerprint (written at ingest, checked at load)
  api.py              FastAPI app: POST /triage, GET /health
  serve.py            `triage serve` (uvicorn runner)
  schema.py           Pydantic output contract for the verdict
  corpus/runbooks/    hand-written runbooks (markdown, ship in the wheel)
corpus/
  attack/             dev-mode ATT&CK bundle location (downloaded, not committed)
```
