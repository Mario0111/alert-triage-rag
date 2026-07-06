"""Query rewriting: raw alert/log text -> retrieval-optimized description.

Pre-retrieval stage (see query.py). SOC alerts arrive in many shapes — analyst
prose, raw SIEM/EDR events, JSON logs, bare command lines — but the corpus this
system searches is prose (ATT&CK descriptions/detections, runbooks). A raw log
is a poor semantic match for that corpus, and anything past the embedder's
512-token window is silently truncated at query time. Rewriting the alert into
a short behavioral description solves both: the query lands in the same
vocabulary as the corpus, and it always fits the window.

Two invariants the pipeline relies on:
- The rewrite is used ONLY as the embedding/search text. The verdict stage in
  query.py grounds Claude on the ORIGINAL alert, so no evidence is laundered
  away by the paraphrase.
- The rewritten text is still checked against the embed window before use
  (`ensure_embeddable`) — the prompt bounds the length, the guard enforces it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anthropic

from .chunk import EMBED_MAX_TOKENS

if TYPE_CHECKING:
    from transformers import PreTrainedTokenizerBase

# The rewrite is a cheap, low-reasoning task; Haiku keeps the expensive model
# for the verdict stage.
DEFAULT_REWRITE_MODEL = "claude-haiku-4-5"

# Hard cap on the rewrite response. The prompt asks for <=120 words; this is
# the enforcement backstop, not the target.
_MAX_OUTPUT_TOKENS = 1024

REWRITE_SYSTEM_PROMPT = """\
You are a query rewriter inside a SOC alert triage system. The user message is
a security alert in some raw form: an analyst's note, a SIEM/EDR event, a JSON
log, or a command line.

Rewrite it as a short natural-language description of the OBSERVED BEHAVIOR,
using the vocabulary a security analyst or MITRE ATT&CK detection guidance
would use. Your output is embedded and used only for semantic search over
ATT&CK technique descriptions/detections and internal runbooks; it is never
shown to anyone as a finding.

Rules:
- Describe the behavior in plain prose: which process or tool did what,
  spawned by what, toward what target (e.g. "Microsoft Word spawned PowerShell
  with an encoded command that made an outbound HTTP connection").
- PRESERVE concrete, searchable entities: process/tool names, parent-child
  process relationships, protocols, port numbers, registry paths, file paths,
  scheduled task or service names, cloud API actions, account/privilege types.
- DROP tokenizer noise: hashes, GUIDs, timestamps, IP addresses, hostnames,
  usernames, and base64/encoded payloads. Describe their intent instead of
  quoting them ("an encoded PowerShell command", "a rare external domain").
- Do not speculate about attribution, severity, or a verdict; describe only
  what the alert shows.
- If the input is already a concise natural-language description, return it
  near-verbatim rather than rewriting aggressively.
- Output ONLY the rewritten description: plain prose, no preamble, no
  markdown, no JSON, at most 120 words.
"""


def rewrite_alert(
    alert_text: str,
    model: str = DEFAULT_REWRITE_MODEL,
    client: anthropic.Anthropic | None = None,
) -> str:
    """Rewrite raw alert text into a retrieval-optimized description.

    Args:
        alert_text: The alert as the analyst provided it (prose or raw log).
        model: Claude model id for the rewrite (cheap/fast tier by default).
        client: Optional pre-built Anthropic client (reads ``ANTHROPIC_API_KEY``
            from the environment when omitted).

    Returns:
        A short prose description of the alert's behavior, suitable as the
        embedding query.

    Raises:
        ValueError: If ``alert_text`` is empty.
        RuntimeError: If the model refuses, truncates, or returns no text.
        anthropic.APIError: Propagated as-is on API/network failure (fail
            loudly; retries for 429/5xx are handled inside the SDK).
    """
    if not alert_text.strip():
        raise ValueError("alert_text is empty")

    if client is None:
        client = anthropic.Anthropic()

    response = client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        system=REWRITE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": alert_text}],
    )

    if response.stop_reason == "refusal":
        raise RuntimeError(
            "Rewrite model refused the alert text; rerun with --no-rewrite to "
            "embed the raw alert instead."
        )
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Rewrite exceeded {_MAX_OUTPUT_TOKENS} output tokens — the model "
            "ignored the length bound; not using a truncated query."
        )

    rewritten = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    if not rewritten:
        raise RuntimeError("Rewrite model returned no text")
    return rewritten


def ensure_embeddable(text: str, tokenizer: "PreTrainedTokenizerBase") -> None:
    """Fail loudly if ``text`` would be silently truncated by the embedder.

    ``SentenceTransformer.encode`` drops everything past the model's window
    without warning — the same failure mode the corpus-side chunking guards
    against. With rewriting on, the prompt's length bound makes this a cheap
    assertion; with ``--no-rewrite`` it is the only defense.

    Args:
        text: The query text about to be embedded.
        tokenizer: The embedder's tokenizer (counts include [CLS]/[SEP]).

    Raises:
        ValueError: If the text exceeds the embedder's token window.
    """
    n_tokens = len(tokenizer(text)["input_ids"])
    if n_tokens > EMBED_MAX_TOKENS:
        raise ValueError(
            f"Query text is {n_tokens} tokens but the embedder truncates at "
            f"{EMBED_MAX_TOKENS}; the tail of the alert would be silently "
            "ignored. Shorten the alert text or enable rewriting."
        )
