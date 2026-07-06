"""alert-triage-rag core package.

Pipeline modules for the RAG triage assistant. Entry points are run as
modules from the repo root so relative corpus/db paths resolve:

    python -m triage.ingest          # corpus -> Chroma (run once / on change)
    python -m triage.query "..."     # alert text -> grounded verdict (JSON)
"""
