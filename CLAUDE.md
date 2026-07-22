# CLAUDE.md

## Project: alert-triage-rag

A retrieval-augmented triage assistant for SOC/MDR alerts. An analyst
describes an alert in natural language; the system retrieves relevant
MITRE ATT&CK techniques and internal runbook steps, then produces a
structured, grounded triage verdict (JSON) with citations back to source
material.

This is a portfolio project. The author is the one being interviewed on
it, so clarity and explainability beat cleverness everywhere.

## Architecture

Two phases:

Core modules live in the `triage/` package. Installed (pipx/pip), the app
is the `triage` command (`ingest` / `query` subcommands, entry point in
`triage/cli.py`); from a checkout, `python -m triage.ingest` /
`python -m triage.query` still work with the same flags. Data locations
resolve in `triage/paths.py` (CLI flags > `TRIAGE_DATA_DIR` env override >
platformdirs per-user default).

1. **Ingestion** (`triage/ingest.py`): load corpus -> chunk -> embed ->
   persist to Chroma. Run once / on corpus change.
2. **Query** (`triage/query.py`): embed alert text -> retrieve top-k chunks ->
   build grounded prompt -> call Claude -> validate JSON against schema.

Corpus:
- MITRE ATT&CK Enterprise (STIX/JSON) -> chunked **per technique field**:
  description and detection are embedded as *separate* chunks (each split
  further if it exceeds the embedder's 512-token window), every chunk tagged
  with the same `attack_id`. This keeps detection text — which alert queries
  often echo — from being silently truncated off the tail of one oversized
  chunk.
- Hand-written runbooks (markdown, in `triage/corpus/runbooks/` — inside
  the package so they ship in the wheel) -> same
  token-budgeted splitting, every chunk tagged with the runbook filename
  (`source`) and its `chunk_index`.
- Both types are reassembled at retrieval: `triage/retrieve.py` merges sibling
  chunks of the same document (key: `attack_id` for techniques, `source`
  for runbooks) back into one complete, citable unit. The "one citable
  document per result" guarantee lives in **retrieval**, not in the chunk
  shape — which is also why chunks carry no overlap: merge, not overlap,
  is the context mechanism.
- Retrieval also guarantees a runbook candidate: when the similarity
  top-k contains no runbook, the single nearest runbook is appended,
  marked `backfilled`, and the grounding prompt tells the model to judge
  its relevance rather than assume it (backfill, not a quota slot —
  naturally-matching runbooks keep their earned ranks).

## Stage 2 — from pipeline to app (current)

The core RAG functionality (Stage 1) is done. Stage 2 turns it into a
distributable application with a real DevOps story:

1. Installable CLI: `pyproject.toml` + console entry points, per-user
   data directory for the Chroma store (no more repo-relative paths).
2. Tests + CI (GitHub Actions: ruff, mypy, pytest).
3. FastAPI service exposing `POST /triage` — the verdict schema in
   `triage/schema.py` doubles as the response model.
4. Docker + compose + tagged releases (wheel + image to GHCR).
5. Native desktop UI (PySide6/Qt, explicitly approved) as a client of the
   API, packaged to a standalone executable with PyInstaller. A Streamlit
   web UI was built first and then REMOVED by author decision — the
   deliverable is a real application, not a web app.
6. SIEM homelab integration: Wazuh + honeypot feeding real alerts into
   the same `POST /triage` endpoint.

Key architectural rule for Stage 2: **the FastAPI service is the single
integration surface.** CLI, desktop UI, and SIEM webhook are all thin clients of
the same `triage/` core. Don't let any interface grow its own triage
logic. Sequence, status, and decisions live in PLAN.md.

## Stack (fixed — do not substitute)

- Python 3.11+
- Embeddings: `bge-small-en-v1.5` via `sentence-transformers`, LOCAL. No
  embedding API calls.
- Vector store: `chromadb`, persisted to the per-user data dir
  (`TRIAGE_DATA_DIR=<repo>` in dev reproduces the old `./chroma_db`).
- Generation: Anthropic API (`anthropic` SDK), Claude.
- Output validation: `pydantic` v2.
- Interface: installable CLI (entry points). For Stage 2: `fastapi` +
  `uvicorn` for the service layer, `PySide6` (Qt) for the native desktop
  UI (all explicitly approved). `platformdirs` for per-user data paths.
  GUI deps are OPTIONAL extras (`[desktop]`), never runtime deps.

## Hard constraints

- **No LangChain, LlamaIndex, or similar orchestration frameworks.**
  Retrieval and prompting are hand-written. This is deliberate; the
  author must be able to explain every step.
- Keep dependencies minimal. If a stdlib solution exists, use it.
- The Pydantic schema in `triage/schema.py` is the output contract. Generation
  code conforms to it; don't quietly change its fields.
- Citations are non-negotiable: every verdict must reference the source
  chunks it used. A verdict with no traceable source is a bug.

## Division of labor (important)

Some files are author-owned and you should NOT write or auto-complete
their core logic unless explicitly asked. When working near them, you may
set up structure and TODOs, but leave the reasoning to the author:

- `triage/chunk.py` — the per-technique chunking strategy. Author writes this.
- `triage/retrieve.py` — the top-k retrieval logic. Author writes this.
- the grounding prompt inside `triage/query.py` — author writes this.

You CAN freely own: project scaffold, `triage/ingest.py` plumbing, Chroma
persistence boilerplate, STIX parsing glue, CLI argument handling,
`requirements.txt`, `.gitignore`, README skeleton.

Stage 2 additions — Claude can freely own: `pyproject.toml` and packaging,
data-directory handling, tests, CI workflows, Dockerfile/compose, the
FastAPI plumbing, the desktop UI + its packaging. Paired (Claude proposes, author
decides): the SIEM alert normalizer (it shapes what the retrieval pipeline
sees, so it touches author-owned reasoning).

When a task touches an author-owned file, stop and flag it rather than
filling it in.

## Conventions

- Type hints everywhere. Docstrings on public functions.
- Fail loudly: validate inputs, raise clear errors, no silent except.
- Small, single-purpose modules over one large script.
- Comments explain *why*, not *what*.

## Working style

Plan, then act, then verify. Before non-trivial changes, briefly state
the plan and wait if the path is ambiguous. Prefer showing a diff or the
specific files you'll touch over large unexplained rewrites.

**Teaching mode (applies to all Stage 2 work).** The author has little
packaging/DevOps experience and is doing Stage 2 partly to learn it. For
every new tool, file, or concept you introduce (pyproject.toml, entry
points, pipx, platformdirs, CI workflows, Docker layers, ...), explain
what it is, why it's needed here, and what would break without it —
before or alongside the change, not as an afterthought. Prefer small,
explained steps over large finished drops. The author must be able to
explain every piece of this project in an interview; a change the author
can't explain is as bad as a broken one.