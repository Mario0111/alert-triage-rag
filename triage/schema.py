"""Output contract for the triage verdict (Pydantic v2).

This is the output contract for the whole system (see CLAUDE.md): generation
code in `query.py` conforms to it, and citations are non-negotiable. Don't
quietly change these fields — downstream prompting and validation depend on them.

Design decisions (settled deliberately; revisit together if needed):
  - Citations are a FLAT list of structured `Citation` objects, not attached
    per-claim. A flat list keyed by `chunk_id` is enough to make every verdict
    traceable without complicating the prompt.
  - `severity` is assigned by Claude as part of the grounded verdict, rather
    than derived mechanically from ATT&CK.
  - Grounding is ENFORCED, not just requested:
      * at least one citation is required (a verdict with no source is a bug);
      * every id in `mitre_techniques` must appear in an ATT&CK citation, so a
        technique can't be claimed without a traceable source.
  - Unknown fields are rejected (`extra="forbid"`) so the contract fails loudly.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Verdict(str, Enum):
    """The triage disposition for an alert."""

    TRUE_POSITIVE = "true_positive"
    FALSE_POSITIVE = "false_positive"
    BENIGN = "benign"
    NEEDS_INVESTIGATION = "needs_investigation"


class Severity(str, Enum):
    """Severity of the alert if it represents real malicious activity."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SourceType(str, Enum):
    """Which part of the corpus a citation points back to."""

    ATTACK = "attack"
    RUNBOOK = "runbook"


class Citation(BaseModel):
    """A single reference back to a retrieved source chunk.

    Every verdict must be backed by these so an analyst can trace each claim to
    its origin in the ATT&CK corpus or a runbook.
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(
        description="Id of the retrieved chunk this citation references."
    )
    source_type: SourceType = Field(
        description="Whether the chunk came from ATT&CK or a runbook."
    )
    ref: str = Field(
        description=(
            "Human-meaningful reference: the ATT&CK technique id (e.g. "
            "'T1059.001') for attack sources, or the runbook filename for "
            "runbook sources."
        )
    )
    quote: str | None = Field(
        default=None,
        description="Optional supporting snippet quoted from the source chunk.",
    )


class TriageVerdict(BaseModel):
    """The structured, grounded triage verdict returned for an alert.

    This is what `query.py` validates Claude's JSON response against.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict = Field(description="The triage disposition.")
    severity: Severity = Field(
        description="Severity assuming the activity is malicious."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Model confidence in the verdict, from 0.0 to 1.0.",
    )
    summary: str = Field(
        description="Short natural-language rationale for the analyst."
    )
    mitre_techniques: list[str] = Field(
        default_factory=list,
        description=(
            "ATT&CK technique ids relevant to the alert (e.g. ['T1059.001']). "
            "Each must be backed by an ATT&CK citation."
        ),
    )
    recommended_actions: list[str] = Field(
        default_factory=list,
        description="Concrete next steps, ideally traceable to a runbook.",
    )
    citations: list[Citation] = Field(
        min_length=1,
        description="Sources backing the verdict. At least one is required.",
    )

    @model_validator(mode="after")
    def _techniques_must_be_cited(self) -> TriageVerdict:
        """Reject any referenced technique that lacks a backing ATT&CK citation."""
        cited_attack_ids = {
            c.ref for c in self.citations if c.source_type is SourceType.ATTACK
        }
        uncited = [t for t in self.mitre_techniques if t not in cited_attack_ids]
        if uncited:
            raise ValueError(
                "mitre_techniques not backed by an ATT&CK citation: "
                f"{uncited}. Every claimed technique needs a traceable source."
            )
        return self
