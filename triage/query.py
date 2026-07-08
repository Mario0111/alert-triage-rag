"""Query pipeline: alert text -> rewrite -> retrieve -> ground -> Claude -> validated JSON.

The generation stage enforces grounding in three layers (decisions settled with
the author; each is interview material):

1. **Structure at generation time.** `client.messages.parse()` sends the
   `TriageVerdict` schema as a structured-output constraint: the API's grammar
   makes malformed JSON, unknown fields, and invalid enum values unsampleable.
   Constraints the API cannot express (confidence bounds, min one citation,
   the techniques-must-be-cited validator) are stripped from the wire schema
   by the SDK and enforced client-side by Pydantic.

2. **Grounding beyond the schema.** `schema.py` can only check a verdict's
   internal consistency; it cannot know which sources were retrieved. So this
   module cross-checks every citation against the actual `RetrievedChunk`s —
   a citation to a source that was never retrieved is a hallucination, and
   per CLAUDE.md an untraceable citation makes the verdict a bug.

3. **One feedback retry, then fail loudly.** A validation failure (schema or
   grounding) is fed back to the model once, with the exact problems and the
   list of valid source ids. The retry is printed, not silent; a second
   failure raises. Bounded self-correction, not a framework loop.

The rewrite shapes ONLY the retrieval query; the ORIGINAL alert is what the
verdict is grounded on (see rewrite.py) — no evidence is laundered away by the
paraphrase.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import anthropic
import chromadb
from pydantic import ValidationError
from sentence_transformers import SentenceTransformer

from .ingest import DEFAULT_COLLECTION, DEFAULT_DB_DIR, DEFAULT_EMBED_MODEL
from .retrieve import RetrievedChunk, retrieve
from .rewrite import DEFAULT_REWRITE_MODEL, ensure_embeddable, rewrite_alert
from .schema import SourceType, TriageVerdict

# Generation model (see CLAUDE.md: Anthropic API, Claude). The verdict is the
# reasoning-heavy stage, so it gets the capable model; the rewrite runs on
# Haiku (see rewrite.py).
DEFAULT_GEN_MODEL = "claude-opus-4-8"
DEFAULT_TOP_K = 5

# Generous ceiling: adaptive thinking tokens count against max_tokens, and a
# truncated verdict is useless — the JSON must complete.
_MAX_OUTPUT_TOKENS = 16000

# Initial attempt + one feedback retry. The only realistic failure with
# structured outputs is a hallucinated/misattributed citation, which the
# feedback usually fixes; a second failure is a real problem to surface.
_MAX_ATTEMPTS = 2

TRIAGE_SYSTEM_PROMPT = """\
You are a triage assistant inside a SOC alert pipeline. You receive one
security alert and a set of retrieved reference sources: MITRE ATT&CK
techniques and internal runbooks. Produce a grounded triage verdict.

Grounding rules (non-negotiable):
- Base every claim on the provided <source> blocks and the <alert> itself.
  If the sources do not support a confident conclusion, return the verdict
  "needs_investigation" rather than guessing.
- Every source you rely on must appear in `citations`. Set `chunk_id` AND
  `ref` to the source's exact `id` attribute, and `source_type` to its exact
  `type` attribute ("attack" or "runbook"). Never cite an id that is not
  among the provided sources.
- Only list a technique id in `mitre_techniques` if you also cite that
  technique's ATT&CK source.
- When a `quote` would help an analyst verify a claim, copy it VERBATIM from
  the source text.

Runbook guarantee:
- At least one runbook is always provided. A source marked
  backfilled="true" did NOT match the alert by similarity — it is merely the
  nearest available runbook, included so a triage procedure is always on
  hand. Judge for yourself whether it applies to this alert: if it does not,
  do not cite it and do not follow its disposition; if it does, treat it
  like any other source.

Verdict semantics:
- `verdict`: your triage disposition for this alert.
- `severity`: how bad the activity would be IF it is malicious — not how
  confident you are that it is.
- `confidence`: 0.0-1.0 confidence in the disposition.
- `summary`: 2-4 sentences an analyst can act on, referencing the evidence.
- `recommended_actions`: concrete next steps, preferring steps traceable to a
  cited runbook.
"""


def _source_kind(chunk: RetrievedChunk) -> SourceType:
    """Map a retrieved document to the citation SourceType.

    Same discriminator as retrieval's merge key: technique chunks carry
    ``source: "ATT&CK"``; anything else is a runbook (source = filename).
    """
    return (
        SourceType.ATTACK
        if chunk.metadata.get("source") == "ATT&CK"
        else SourceType.RUNBOOK
    )


def build_grounding_prompt(alert_text: str, chunks: list[RetrievedChunk]) -> str:
    """Build the grounded user prompt: XML-tagged sources plus the raw alert.

    Each retrieved document becomes a ``<source>`` block whose ``id`` attribute
    is the citable identifier (`RetrievedChunk.id`: the ATT&CK id or runbook
    filename) — the citation contract is visible in the prompt itself, and the
    id maps 1:1 onto `Citation.chunk_id`. The ORIGINAL alert text goes in its
    own ``<alert>`` tag: the rewrite is retrieval-only, so the verdict is
    grounded on the un-paraphrased evidence.

    A runbook appended by the retrieval guarantee carries
    ``backfilled="true"`` on its block: presence in the prompt must not imply
    similarity-matched relevance, so the model is told which source to vet
    (see the system prompt's runbook-guarantee rule).

    Args:
        alert_text: The alert as the analyst provided it.
        chunks: The retrieved, citable source documents.

    Returns:
        The grounded prompt string (user message; role/rules live in
        `TRIAGE_SYSTEM_PROMPT`).

    Raises:
        ValueError: If ``chunks`` is empty — a verdict without sources is a
            bug, so don't even ask for one.
    """
    if not chunks:
        raise ValueError(
            "No retrieved sources to ground on; refusing to request an "
            "unciteable verdict."
        )

    blocks = []
    for chunk in chunks:
        kind = _source_kind(chunk)
        name = chunk.metadata.get("name", chunk.id)
        backfilled = ' backfilled="true"' if chunk.backfilled else ""
        blocks.append(
            f'<source id="{chunk.id}" type="{kind.value}" name="{name}"'
            f"{backfilled}>\n{chunk.text}\n</source>"
        )
    sources = "\n\n".join(blocks)
    return f"<sources>\n{sources}\n</sources>\n\n<alert>\n{alert_text}\n</alert>"


def _grounding_errors(
    verdict: TriageVerdict, chunks: list[RetrievedChunk]
) -> list[str]:
    """Check what the schema can't: citations must point at RETRIEVED sources.

    `schema.py` enforces internal consistency (min one citation, techniques
    backed by ATT&CK citations) but has no knowledge of the retrieval set. A
    citation whose id was never retrieved is a hallucination; a citation with
    the wrong source_type or a mismatched ref is misattribution. All are
    grounding failures.

    Args:
        verdict: The schema-valid verdict from the model.
        chunks: The sources that were actually provided in the prompt.

    Returns:
        Human-readable problems, empty when the verdict is fully grounded.
        Returned (not raised) so the caller can feed them back for the retry.
    """
    kinds = {chunk.id: _source_kind(chunk) for chunk in chunks}
    errors: list[str] = []
    for citation in verdict.citations:
        kind = kinds.get(citation.chunk_id)
        if kind is None:
            errors.append(
                f"citation chunk_id {citation.chunk_id!r} does not match any "
                "provided source id"
            )
            continue
        if citation.source_type is not kind:
            errors.append(
                f"citation {citation.chunk_id!r} has source_type "
                f"{citation.source_type.value!r} but that source's type is "
                f"{kind.value!r}"
            )
        if citation.ref != citation.chunk_id:
            errors.append(
                f"citation {citation.chunk_id!r} has ref {citation.ref!r}; "
                "ref must equal the source id"
            )
    return errors


def generate_verdict(
    alert_text: str,
    chunks: list[RetrievedChunk],
    model: str = DEFAULT_GEN_MODEL,
    client: anthropic.Anthropic | None = None,
) -> TriageVerdict:
    """Call Claude for a grounded verdict, validated against schema + retrieval.

    Structured outputs guarantee the JSON shape at generation time; Pydantic
    enforces the client-side constraints; `_grounding_errors` enforces that
    every citation traces to a retrieved source. Any validation failure is fed
    back to the model exactly once (visibly, on stdout); a second failure
    raises.

    Args:
        alert_text: The ORIGINAL alert text (not the rewrite).
        chunks: The retrieved, citable source documents.
        model: Claude model id for generation.
        client: Optional pre-built Anthropic client (reads ``ANTHROPIC_API_KEY``
            from the environment when omitted).

    Returns:
        A fully validated, fully grounded `TriageVerdict`.

    Raises:
        ValueError: If validation still fails after the feedback retry.
        RuntimeError: If the model refuses or truncates.
        anthropic.APIError: Propagated as-is on API/network failure.
    """
    if client is None:
        client = anthropic.Anthropic()

    prompt = build_grounding_prompt(alert_text, chunks)
    valid_ids = sorted(chunk.id for chunk in chunks)

    feedback: str | None = None
    problems: list[str] = []
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        content = prompt if feedback is None else f"{prompt}\n\n{feedback}"
        verdict: TriageVerdict | None = None
        try:
            response = client.messages.parse(
                model=model,
                max_tokens=_MAX_OUTPUT_TOKENS,
                system=TRIAGE_SYSTEM_PROMPT,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": content}],
                output_format=TriageVerdict,
            )
        except ValidationError as exc:
            # Grammar guarantees structure, so this is a client-side constraint
            # (confidence bounds, missing citation, uncited technique).
            problems = [f"schema validation failed: {exc}"]
        else:
            if response.stop_reason == "refusal":
                raise RuntimeError(
                    "Triage model refused the request; no verdict produced."
                )
            if response.stop_reason == "max_tokens":
                raise RuntimeError(
                    f"Verdict generation exceeded {_MAX_OUTPUT_TOKENS} output "
                    "tokens; not accepting a truncated verdict."
                )
            verdict = response.parsed_output
            if verdict is None:
                raise RuntimeError("Triage model returned no parseable verdict.")
            problems = _grounding_errors(verdict, chunks)
            if not problems:
                return verdict

        if attempt < _MAX_ATTEMPTS:
            # Visible, bounded self-correction — never a silent retry.
            print("Verdict failed validation; retrying once with feedback:")
            for problem in problems:
                print(f"  - {problem}")
            previous = (
                f"\n\nYour previous verdict was:\n{verdict.model_dump_json()}"
                if verdict is not None
                else ""
            )
            feedback = (
                "Your previous verdict failed validation:\n- "
                + "\n- ".join(problems)
                + previous
                + "\n\nProduce a corrected verdict. Cite ONLY these source "
                + "ids: " + ", ".join(valid_ids)
            )

    raise ValueError(
        "Verdict failed validation after a feedback retry: " + "; ".join(problems)
    )


def load_collection(
    db_dir: Path, collection_name: str
) -> chromadb.Collection:
    """Open the persisted Chroma collection produced by ingestion.

    Args:
        db_dir: Directory of the persisted Chroma database.
        collection_name: Name of the collection to open.

    Returns:
        The Chroma collection, ready to query.

    Raises:
        FileNotFoundError: If the database directory does not exist (i.e.
            ingestion has not been run yet).
    """
    if not db_dir.is_dir():
        raise FileNotFoundError(
            f"Chroma database not found at {db_dir}. Run ingest.py first."
        )
    client = chromadb.PersistentClient(path=str(db_dir))
    return client.get_collection(name=collection_name)


def triage(
    alert_text: str,
    db_dir: Path,
    collection_name: str,
    embed_model: str,
    gen_model: str,
    rewrite_model: str,
    top_k: int,
    no_rewrite: bool = False,
) -> TriageVerdict:
    """Run the full query pipeline for a single alert.

    rewrite -> retrieve -> grounded generation -> validated verdict, printed
    as JSON with the citations that make it traceable.

    Args:
        alert_text: The alert as the analyst provided it (prose or raw log).
        db_dir: Directory of the persisted Chroma database.
        collection_name: Collection to query.
        embed_model: Local sentence-transformers model id.
        gen_model: Claude model id for generation.
        rewrite_model: Claude model id for the pre-retrieval query rewrite.
        top_k: Number of documents to retrieve.
        no_rewrite: Embed the raw alert text directly, skipping the rewrite
            (escape hatch / A-B comparison; the token-window guard still runs).

    Returns:
        The validated, grounded verdict.
    """
    embedder = SentenceTransformer(embed_model)
    collection = load_collection(db_dir, collection_name)

    # The rewrite shapes ONLY the search text; the original alert stays the
    # evidence for the grounding prompt below.
    if no_rewrite:
        search_text = alert_text
    else:
        search_text = rewrite_alert(alert_text, model=rewrite_model)
        print(f"Search query (rewritten): {search_text}")
    ensure_embeddable(search_text, embedder.tokenizer)

    chunks = retrieve(search_text, collection, embedder, k=top_k)
    print(
        f"Retrieved {len(chunks)} sources: "
        + ", ".join(
            chunk.id + (" (backfilled)" if chunk.backfilled else "")
            for chunk in chunks
        )
    )

    verdict = generate_verdict(alert_text, chunks, model=gen_model)
    print(verdict.model_dump_json(indent=2))
    return verdict


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the query script."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "alert",
        help="The alert description in natural language (quote it).",
    )
    parser.add_argument(
        "--db-dir",
        default=DEFAULT_DB_DIR,
        help="Directory of the persisted Chroma database.",
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help="Chroma collection name.",
    )
    parser.add_argument(
        "--embed-model",
        default=DEFAULT_EMBED_MODEL,
        help="sentence-transformers model id (runs locally).",
    )
    parser.add_argument(
        "--gen-model",
        default=DEFAULT_GEN_MODEL,
        help="Claude model id for generation.",
    )
    parser.add_argument(
        "--rewrite-model",
        default=DEFAULT_REWRITE_MODEL,
        help="Claude model id for the pre-retrieval query rewrite.",
    )
    parser.add_argument(
        "--no-rewrite",
        action="store_true",
        help="Embed the raw alert text directly, skipping the rewrite step.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help="Number of documents to retrieve.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    args = parse_args(argv)
    triage(
        alert_text=args.alert,
        db_dir=Path(args.db_dir),
        collection_name=args.collection,
        embed_model=args.embed_model,
        gen_model=args.gen_model,
        rewrite_model=args.rewrite_model,
        top_k=args.top_k,
        no_rewrite=args.no_rewrite,
    )


if __name__ == "__main__":
    main()
