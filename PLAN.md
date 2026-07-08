# PLAN.md

Working document for the alert-triage-rag build. Tracks goal, sequence,
status, and decisions. Update as pieces close. CLAUDE.md holds the fixed
rules; this holds the moving state.

Status key: [ ] not started · [~] in progress · [x] done

## Goal

Stage 1 (done): a working RAG triage assistant, demoable via CLI, that
takes an alert description and returns a structured, grounded verdict
(JSON) with citations back to MITRE ATT&CK techniques and internal
runbooks.

Stage 2 (current): turn the pipeline into a distributable app with a
DevOps story — installable CLI, tests + CI, FastAPI service, Docker +
releases, Streamlit UI, and finally a SIEM homelab (Wazuh + honeypot)
feeding real alerts into the triage endpoint. Each phase is independently
shippable so the git history tells the story. Stage 2 runs in teaching
mode (see CLAUDE.md): every new tool/concept gets explained as it lands.

## Division of labor (see CLAUDE.md for the rule)

- Author-owned (I write, Claude stays off): chunk.py, retrieve.py, the
  grounding prompt in query.py.
- Paired (Claude proposes, I decide): schema.py.
- Claude-owned (Claude writes, I review): scaffold, ingest plumbing,
  Chroma boilerplate, STIX parsing, CLI arg handling, config files.

## Build sequence

### Phase 0 — Scaffold  [x]
Claude-owned. One session. Structure, requirements.txt, .gitignore,
README skeleton, stubbed chunk.py + retrieve.py, ingest.py plumbing,
STIX parsing helper. Schema fields proposed as comments only.
Done when: repo imports cleanly, tree matches the plan, punch list of
author-owned files is clear.

### Phase 1 — Schema  [x]
Paired. Lock the Pydantic output contract before anything depends on it.
Fields to settle: technique_ids, severity, verdict, investigation_steps,
sources (+ confidence?). Done when schema.py validates a hand-written
example verdict.

### Phase 2 — Corpus  [x]
Author-owned data work.
- [x] Pull MITRE ATT&CK Enterprise (STIX bundle / JSON) into corpus/attack/
- [x] Write 5-6 runbooks in corpus/runbooks/ (real triage logic for alert
      types I actually understand — phishing, suspicious PowerShell,
      brute-force/cred stuffing, lateral movement, data exfil, etc.)
Done when: corpus is on disk and STIX helper parses it without errors.

### Phase 3 — Chunking  [x]
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

### Phase 4 — Retrieval  [x]
Author-owned. Write retrieve.py. Embed query, over-fetch top-k' cosine
against Chroma, merge sibling chunks by document key (attack_id for
techniques, runbook filename for runbooks), trim to k, return chunks +
their source metadata. Done when: a test alert returns relevant,
reassembled documents I can eyeball as correct.

### Phase 5 — Grounding + generation  [x]
Author-owned prompt; Claude can wire the API call around it. Assemble
retrieved chunks into a grounded prompt, call Claude, parse + validate
against schema, one retry on validation failure. Done when: a test alert
produces valid JSON with honest citations.

### Phase 6 — CLI  [x]
Claude-owned, I review. `python -m triage.query "alert description"` ->
pretty-printed verdict. Done when: runnable from a clean checkout per
README.

## Build sequence — Stage 2 (app + DevOps + SIEM)

### Phase 7 — Installable package  [x]
Claude-owned, teaching mode. Convert to a real installable app:
- [x] pyproject.toml (setuptools backend; pinned deps migrated from
      requirements.txt with their why-comments; `[project.scripts]` ->
      one `triage` command with `ingest` / `query` argparse subcommands
      in triage/cli.py; runbooks ship as package data).
- [x] Per-user data dir via platformdirs (triage/paths.py): Chroma store
      + fetched ATT&CK bundle live under %LOCALAPPDATA%\alert-triage-rag
      (Linux/macOS equivalents via platformdirs). Precedence: CLI flags >
      TRIAGE_DATA_DIR env var > platformdirs default; TRIAGE_DATA_DIR=
      <repo root> reproduces the old ./chroma_db + corpus/attack layout
      exactly (dev mode). `triage ingest` auto-downloads the ATT&CK
      bundle (pinned v19.1 URL) when missing; --refresh-attack forces.
      Author-owned logic untouched: retrieve.py takes an opened
      collection (no paths), query.py changes were confined to CLI
      plumbing, grounding prompt untouched.
- [x] README: pipx install + clean-machine quickstart + data-locations
      table + dev setup (editable install + TRIAGE_DATA_DIR).
Verified: fresh venv (the pipx-equivalent; pipx not installed on the dev
machine), `pip install <repo>` -> `triage ingest` -> `triage query "..."`
run from outside the repo, no checkout assumptions.

### Phase 8 — Tests + CI  [x]
Claude-owned, teaching mode. pytest suite (chunk splitting, retrieval
merge, schema validation, mocked-Claude query path), ruff, mypy. GitHub
Actions workflow running all three on push/PR. Done when: CI is green on
a fresh clone and a deliberately broken test fails the build.
Delivered: [dev] extra (pytest/ruff/mypy, pinned) + [tool.*] configs in
pyproject.toml; 42 hermetic tests in tests/ (no network, no API calls, no
model download — fake tokenizer/embedder/Anthropic client, in-memory
Chroma via EphemeralClient with telemetry off); .github/workflows/ci.yml
(3.11 + 3.14 matrix, pip cache, CPU-only torch). Verified locally: ruff,
mypy, pytest all green from a TRIAGE_DATA_DIR-less shell; a deliberately
broken assertion failed the suite (exit 1) and was restored. No GitHub
remote yet, so the workflow is correct-on-push (YAML parse-validated);
its first real run happens when the repo is pushed.
Author follow-ups proposed by lint/type findings (NOT applied — see
decisions log): zip(strict=True) in retrieve.py, StrEnum in schema.py,
type-narrowing in retrieve.py/chunk.py.

### Phase 9 — FastAPI service  [ ]
Claude-owned plumbing; schema.py stays the contract. `POST /triage`
(alert text in -> validated verdict JSON out), `GET /health`. The service
is the single integration surface: CLI, UI, and SIEM all call the same
core. Done when: uvicorn serves a verdict with citations via curl.

### Phase 10 — Docker + releases  [ ]
Claude-owned, teaching mode. Multi-stage Dockerfile (torch layer cached),
docker-compose (API + UI + Chroma volume), embedding model baked/cached
so cold start is sane, API key via env only. Tagged releases: wheel +
image to GHCR via Actions. Done when: `docker compose up` on a clean
machine serves the API + UI.

### Phase 11 — Streamlit UI  [ ]
Claude-owned. Thin client of the API: alert text box, rendered verdict,
expandable citation panels showing retrieved chunks (incl. backfilled
runbook marking). Done when: demoable in a screen-share.

### Phase 12 — SIEM homelab  [ ]
Paired — the normalizer shapes retrieval input. Wazuh manager VM +
telemetry sources feeding it; custom integratord script POSTs qualifying
alerts to /triage; verdicts logged/indexed back with citations.
Attack-signal sources (see decisions log): honeypot (Cowrie) for real
unscripted attacker traffic + the existing attack-simulation homelab /
Atomic Red Team for endpoint techniques the honeypot can't produce.
Normalizer: Wazuh alert JSON -> natural-language description (sits in
front of rewrite.py's query rewrite). Done when: a real honeypot hit
produces a grounded verdict end-to-end without a human typing anything.

### Phase 13 — Writeup + demo polish  [ ]
- [ ] Demo screenshot/gif in README
- [ ] Writeup: design decisions, why no LangChain, why local embeddings,
      the local-routine + frontier-for-hard-cases production note, the
      Stage 2 architecture (API as single integration surface)

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
- Stage 2 architecture: FastAPI service as the single integration surface.
  CLI, Streamlit UI, and the SIEM webhook are all thin clients of the same
  triage core — no interface grows its own triage logic. Chosen because
  the SIEM integration needs an HTTP endpoint anyway, and schema.py
  (Pydantic v2) doubles as the FastAPI response model for free.
- Packaging: pipx-installable CLI with entry points; Chroma store and
  fetched corpus move to a per-user data dir (platformdirs) so the app
  works without a repo checkout.
- Phase 7 packaging decisions:
  - Build backend: setuptools. It's the long-standing default with no
    exotic needs here (hatchling/flit would also work; nothing to gain,
    one more tool to explain).
  - One `triage` command with argparse subparsers over separate
    triage-ingest/triage-query binaries: one name on PATH, --help
    enumerates the verbs, and future verbs (`triage serve`, Phase 9)
    don't mint new executables. Flag definitions live in each module's
    add_arguments() so `python -m triage.ingest/query` (kept working)
    share them with the subcommands — one definition, two entry points.
  - requirements.txt deleted, not kept alongside pyproject.toml: two
    copies of the dependency list WILL drift. The pins + why-comments
    moved into [project.dependencies]. requirements.txt's remaining
    legitimate role is as a full transitive lockfile for reproducible
    deploys — revisit at Phase 10 (Docker), where pip freeze inside the
    image build serves that purpose.
  - Runbooks moved corpus/runbooks/ -> triage/corpus/runbooks/ and ship
    inside the wheel as package data (read via importlib.resources).
    Reason: setuptools can only bundle data files that live inside a
    package, and the runbooks are product (retrieval corpus), not user
    state. The ATT&CK bundle stays out of the wheel (51 MB, public,
    regenerable) and is fetched by `triage ingest` instead — pinned to
    the v19.1 release URL so the corpus can't change silently; refresh
    is a deliberate --refresh-attack.
  - Data precedence: CLI flag > TRIAGE_DATA_DIR env > platformdirs
    per-user dir. Subpaths under the data root deliberately mirror the
    old repo layout (chroma_db/, corpus/attack/) so pointing
    TRIAGE_DATA_DIR at a checkout IS dev mode — no second code path.
- Phase 8 test/CI decisions:
  - Tests are hermetic by construction: the tokenizer, embedder, and
    Anthropic client are faked; Chroma runs in-memory (EphemeralClient,
    telemetry disabled). Retrieval tests use hand-picked unit vectors in
    a 3-dim cosine space so ranking outcomes are forced, not computed.
    Gotcha found: EphemeralClient is cached per process, so collection
    names must be unique per test.
  - Division of labor held: tests exercise author-owned behavior; no
    bugs found in author-owned code. Lint/type findings whose fixes
    change behavior in author-owned/paired files were NOT applied but
    parked as scoped, commented config ignores + proposals to the author:
    (1) retrieve.py: zip(..., strict=True) on the two Chroma parallel-list
    zips (fail-loudly on length mismatch) — ruff B905;
    (2) schema.py: (str, Enum) -> StrEnum (changes str(member); safe here
    since only .value is used) — ruff UP042;
    (3) retrieve.py/chunk.py: real type-narrowing for chromadb's
    Optional/TypedDict results and transformers' decode() overloads,
    replacing the per-module mypy disable_error_code entries.
    Each ignore is commented in pyproject.toml and should be removed when
    the author applies or rejects the fix.
  - Post-push CI fix: mypy's python_version = "3.11" pin was removed. It
    applied 3.11 syntax rules to installed dependencies' type stubs too,
    and the 3.14 job's newer numpy ships PEP 695 `type`-statement stubs
    (3.12+ syntax) — instant syntax error in numpy's own files. Each job
    now checks its own interpreter's semantics; the 3.11 floor is still
    enforced by the matrix's real 3.11 job.
  - CI matrix is 3.11 (requires-python floor) + 3.14 (dev machine) only;
    intermediate versions add a full torch install each for little signal.
    CI installs CPU-only torch (~200 MB) from the pytorch cpu index before
    the package — the default Linux wheel is the CUDA build (~2 GB). Pip's
    download cache is keyed on pyproject.toml's hash.
  - Staleness detection (parking lot): decided to implement at the START
    of Phase 9, not in Phase 8 — it must land before the API serves
    verdicts from silently-stale stores, and Phase 8's EphemeralClient
    harness makes it cheap to test. Mechanism: ingest writes a fingerprint
    (app version + corpus hash + chunking params) into the collection
    metadata (already used for hnsw:space); query.load_collection checks
    it and fails with "re-run triage ingest". Paired work: the plumbing is
    Claude-owned, but WHICH fields define staleness (chunking params) is
    the author's call.
- SIEM: Wazuh. Free, single-VM friendly, ships rule->ATT&CK technique
  mappings (dovetails with the corpus), and its integratord hook makes
  "call a script with the alert JSON" a first-class feature.
- Attack signal for the homelab: honeypot (Cowrie SSH/Telnet) as the
  headline source — real, unscripted internet attacker traffic, which is
  a stronger demo than scripted attacks and exercises the brute-force
  (07), C2 (06), and low-signal (09) runbooks naturally. Complemented by
  the author's existing attack-simulation homelab / Atomic Red Team for
  endpoint techniques a honeypot can't produce (LSASS dumping, Office
  spawning interpreter, encoded PowerShell). Honeypot must be isolated
  (own VLAN/DMZ or a cheap VPS forwarding logs home) — never bridged to
  the home LAN.

## Open questions / parking lot

- Add a `confidence` field to the schema, or leave it out as false
  precision?
- Worth deduping overlapping ATT&CK + runbook hits at retrieval time, or
  let the prompt handle redundancy?
- k=5 a good default, or tune after seeing real retrievals?
- Honeypot placement: cheap VPS (real internet exposure, zero home-network
  risk, ~monthly cost) vs isolated VLAN at home (free, but internet
  exposure requires port-forwarding — more setup risk)? Decide at Phase 12.
- Honeypot alert volume: internet-facing SSH honeypots get hammered —
  need a Wazuh rule-level filter (and maybe dedup/rate-limit in the
  integratord script) so the triage API isn't called thousands of times a
  day. Which threshold?
- Publish to PyPI, or keep install as `pipx install` from the GitHub repo?
- Staleness detection: the Chroma store is built by one app version and
  survives updates (data dir is deliberately untouched by installs), so a
  code update that changes chunking/runbooks leaves a silently stale store
  unless the id scheme happens to drift (which retrieve.py catches loudly).
  Candidate fix: write a fingerprint (app version + corpus hash + chunking
  params) into the collection metadata at ingest, check it at query time
  and fail with "re-run triage ingest". DECIDED (Phase 8): implement at
  the start of Phase 9 — see the decisions-log entry for the mechanism
  and the author-owned part (which fields define staleness).
- Uninstall leaves the data dir behind (~700 MB with the bundle + store):
  normal CLI-app behavior, but README could gain an "uninstall fully"
  note (`pipx uninstall` + delete %LOCALAPPDATA%\alert-triage-rag).