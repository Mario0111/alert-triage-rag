"""alert-triage-rag core package.

Pipeline modules for the RAG triage assistant. Installed, the package exposes
a single ``triage`` console command (see cli.py):

    triage ingest            # corpus -> Chroma (run once / on corpus change)
    triage query "..."       # alert text -> grounded verdict (JSON)

From a repo checkout the module entry points still work and take the same
flags: ``python -m triage.ingest`` / ``python -m triage.query "..."``. Data
locations resolve through paths.py (per-user data dir; ``TRIAGE_DATA_DIR``
overrides for dev mode).
"""
