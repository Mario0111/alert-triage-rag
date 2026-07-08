# alert-triage-rag

A retrieval-augmented triage assistant for SOC/MDR alerts. An analyst describes
an alert in natural language; the system retrieves relevant **MITRE ATT&CK**
techniques and internal **runbook** steps, then produces a structured, grounded
triage verdict (JSON) with **citations** back to the source material.

Built deliberately *without* orchestration frameworks (no LangChain /
LlamaIndex) — retrieval and prompting are hand-written so every step is
explainable.

## Architecture

Two phases:

1. **Ingestion** (`triage/ingest.py`) — load corpus → chunk → embed locally →
   persist to Chroma. Run once, or whenever the corpus changes.
2. **Query** (`triage/query.py`) — embed alert text → retrieve top-k chunks →
   build a grounded prompt → call Claude → validate JSON against the schema.

```
corpus/attack/*.json ─┐
                      ├─ ingest ──► ./chroma_db ──► query ──► triage verdict (JSON)
corpus/runbooks/*.md ─┘                               ▲
                                                alert description
```

**Corpus**
- MITRE ATT&CK Enterprise (STIX/JSON), chunked **per technique field** —
  description and detection are embedded as separate chunks (each split
  further if it exceeds the embedder's 512-token window), all tagged with the
  same `attack_id`.
- Hand-written runbooks (`corpus/runbooks/*.md`), split with the same
  token-budgeted splitter and tagged with the runbook filename.
- Retrieval merges sibling chunks of the same document (by `attack_id` /
  filename) back into one complete, citable unit, so a verdict cites a whole
  technique or runbook rather than a fragment.
- A runbook is always among the sources: if none places in the similarity
  top-k, the nearest one is appended — flagged as `backfilled` so the model
  judges its relevance instead of assuming it.

**Stack:** Python 3.11+ · `sentence-transformers` (`bge-small-en-v1.5`, local) ·
`chromadb` · `anthropic` (Claude) · `pydantic` v2. CLI-first.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Anthropic API key for the query phase
export ANTHROPIC_API_KEY=sk-ant-...   # Windows (PowerShell): $env:ANTHROPIC_API_KEY="..."
```

### Get the ATT&CK corpus

The MITRE ATT&CK Enterprise STIX/JSON bundle is large (~51 MB), public, and
regenerable, so it is **not** committed to this repo. Download it once
(pinned to **Enterprise ATT&CK v19.1**, the version this project was built
against):

```bash
curl -sSL -o corpus/attack/enterprise-attack.json \
  https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/enterprise-attack/enterprise-attack-19.1.json
```

For the latest release instead, drop the version suffix
(`enterprise-attack.json`). Then write runbooks under `corpus/runbooks/`.

## Usage

**Ingest** (build the vector store):

```bash
python -m triage.ingest
# options: --attack-file --runbooks-dir --db-dir --collection --embed-model --batch-size
```

Each run rebuilds the collection from scratch (it drops any existing one
first), so it is always safe to re-ingest after a corpus or chunking change
without leaving stale documents behind.

**Query** (triage an alert):

```bash
python -m triage.query "Multiple failed logons followed by a successful logon from a new country, then a PowerShell download cradle on the host."
# options: --top-k --db-dir --collection --gen-model
```

Both commands are run from the repo root (the default `corpus/` and
`./chroma_db` paths are resolved relative to it).

Output is a structured triage verdict (JSON) with citations back to the ATT&CK
techniques and runbook steps used.

## Demo

<!-- TODO: add a screenshot of a real triage run here -->
_Demo screenshot coming soon._

## Project layout

```
triage/               core package (run entry points with `python -m`)
  ingest.py           ingestion pipeline (plumbing)
  stix.py             ATT&CK STIX/JSON → flat Technique records
  chunk.py            chunking strategy (per-technique + runbooks)
  retrieve.py         top-k retrieval + sibling-chunk merge
  rewrite.py          alert → retrieval-optimized query rewrite
  query.py            query pipeline + grounding prompt
  schema.py           Pydantic output contract for the verdict
corpus/
  attack/             ATT&CK STIX bundle (downloaded, not committed)
  runbooks/           hand-written runbooks (markdown)
```
