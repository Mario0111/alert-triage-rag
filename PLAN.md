# PLAN.md

Working document for the alert-triage-rag build. Tracks goal, sequence,
status, and decisions. Update as pieces close. CLAUDE.md holds the fixed
rules; this holds the moving state.

Status key: [ ] not started · [~] in progress · [x] done

## Goal

A working RAG triage assistant, demoable via CLI, that takes an alert
description and returns a structured, grounded verdict (JSON) with
citations back to MITRE ATT&CK techniques and internal runbooks. Target:
running end-to-end within a weekend or two.

## Division of labor (see CLAUDE.md for the rule)

- Author-owned (I write, Claude stays off): chunk.py, retrieve.py, the
  grounding prompt in query.py.
- Paired (Claude proposes, I decide): schema.py.
- Claude-owned (Claude writes, I review): scaffold, ingest plumbing,
  Chroma boilerplate, STIX parsing, CLI arg handling, config files.

## Build sequence

### Phase 0 — Scaffold  [ ]
Claude-owned. One session. Structure, requirements.txt, .gitignore,
README skeleton, stubbed chunk.py + retrieve.py, ingest.py plumbing,
STIX parsing helper. Schema fields proposed as comments only.
Done when: repo imports cleanly, tree matches the plan, punch list of
author-owned files is clear.

### Phase 1 — Schema  [ ]
Paired. Lock the Pydantic output contract before anything depends on it.
Fields to settle: technique_ids, severity, verdict, investigation_steps,
sources (+ confidence?). Done when schema.py validates a hand-written
example verdict.

### Phase 2 — Corpus  [ ]
Author-owned data work.
- [ ] Pull MITRE ATT&CK Enterprise (STIX bundle / JSON) into corpus/attack/
- [ ] Write 5-6 runbooks in corpus/runbooks/ (real triage logic for alert
      types I actually understand — phishing, suspicious PowerShell,
      brute-force/cred stuffing, lateral movement, data exfil, etc.)
Done when: corpus is on disk and STIX helper parses it without errors.

### Phase 3 — Chunking  [~]
Write chunk.py. ATT&CK -> description and detection embedded as *separate*
chunks (each split further past the 512-token window), all sharing the
technique's attack_id for reassembly. Runbooks -> same token-budgeted
splitter, chunks tagged with the runbook filename + chunk_index for
reassembly. No overlap anywhere: retrieval-time merge is the context
mechanism. Done when: ingest.py runs and populates Chroma; chunk count
looks sane; spot-check that a document's chunks all carry its merge key.
Token-aware split DONE for both corpus types: pieces budgeted by the real
bge tokenizer (header/label headroom measured per document, [CLS]/[SEP] +
margin reserved) with a token-boundary hard-split backstop. Verified for
techniques: 697 -> 1607 chunks, max 508 tokens, 0 over 512. Char proxy
retained as the fallback when no tokenizer is passed (tests / quick runs).
Remaining: run the full ingest end-to-end into Chroma to confirm counts.

### Phase 4 — Retrieval  [ ]
Author-owned. Write retrieve.py. Embed query, over-fetch top-k' cosine
against Chroma, merge sibling chunks by document key (attack_id for
techniques, runbook filename for runbooks), trim to k, return chunks +
their source metadata. Done when: a test alert returns relevant,
reassembled documents I can eyeball as correct.

### Phase 5 — Grounding + generation  [ ]
Author-owned prompt; Claude can wire the API call around it. Assemble
retrieved chunks into a grounded prompt, call Claude, parse + validate
against schema, one retry on validation failure. Done when: a test alert
produces valid JSON with honest citations.

### Phase 6 — CLI  [ ]
Claude-owned, I review. `python query.py "alert description"` -> pretty
-printed verdict. Done when: runnable from a clean checkout per README.

### Phase 7 — Polish (optional, later)  [ ]
- [ ] Thin Streamlit UI for screen-sharing in interviews
- [ ] Demo screenshot/gif in README
- [ ] Writeup: design decisions, why no LangChain, why local embeddings,
      the local-routine + frontier-for-hard-cases production note

## Decisions log

(Append as decisions get made — this is the "why" record for interviews.)

- Stack locked: bge-small-en-v1.5 (local) / Chroma / Claude API / Pydantic
  / no LangChain. Rationale in CLAUDE.md + writeup.
- Chunk ATT&CK per-technique so citations map 1:1 to a technique_id.
- Superseded the above: ATT&CK is now chunked **per field** (description /
  detection as separate chunks, split again past 512 tokens), each tagged
  with attack_id. Reason: bge-small-en-v1.5 truncates at 512 tokens and 74%
  of one-chunk-per-technique chunks exceeded it, so detection text (which
  alert queries echo) was never embedded. retrieve.py restores the 1:1
  technique->citation mapping by merging chunks that share an attack_id.
- ingest.py now rebuilds the Chroma collection from scratch each run (drops
  it first) so changing the chunk-id scheme can't leave orphaned documents.
- Runbooks: chose retrieval-time reassembly over chunk overlap. Runbook
  chunks are token-split like techniques and merged back by filename in
  retrieve.py, so no overlap is needed (and overlap would duplicate text at
  the seams of the merged document). Trade-off accepted: a hit on any chunk
  returns the whole runbook — perfect at today's 2-3 chunks/runbook, worth
  revisiting if runbooks ever grow much longer.

## Open questions / parking lot

- Add a `confidence` field to the schema, or leave it out as false
  precision?
- Worth deduping overlapping ATT&CK + runbook hits at retrieval time, or
  let the prompt handle redundancy?
- k=5 a good default, or tune after seeing real retrievals?