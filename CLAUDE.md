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

Core modules live in the `triage/` package; entry points are run from the
repo root as `python -m triage.ingest` / `python -m triage.query`.

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
- Hand-written runbooks (markdown, in `corpus/runbooks/`) -> same
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

## Stack (fixed — do not substitute)

- Python 3.11+
- Embeddings: `bge-small-en-v1.5` via `sentence-transformers`, LOCAL. No
  embedding API calls.
- Vector store: `chromadb`, persisted to `./chroma_db`.
- Generation: Anthropic API (`anthropic` SDK), Claude.
- Output validation: `pydantic` v2.
- Interface: CLI first. Streamlit only if/when explicitly asked.

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