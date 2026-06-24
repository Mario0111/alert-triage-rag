"""Output contract for the triage verdict (Pydantic v2).

This file is the output contract for the whole system (see CLAUDE.md): the
generation code conforms to it, and citations are non-negotiable. The actual
model is intentionally NOT written yet — we decide the fields together in the
next pairing step. The comment block below is a *proposal* to react to, not a
final design.
"""

# ── PROPOSED FIELDS (for discussion — TODO(pairing): turn into the model) ──
#
# Candidate top-level model: TriageVerdict
#
#   - verdict: an enum of the triage outcome. Proposed values:
#       "true_positive" | "false_positive" | "benign" | "needs_investigation"
#       (Rationale: covers the common SOC/MDR dispositions without overfitting.)
#
#   - severity: an enum, e.g. "low" | "medium" | "high" | "critical".
#       (Open question: derive from ATT&CK, or have Claude assign it?)
#
#   - confidence: float in [0, 1] — how sure the model is in the verdict.
#
#   - summary: short natural-language rationale for the analyst.
#
#   - mitre_techniques: list[str] of cited ATT&CK ids (e.g. ["T1059.001"]).
#       (Must come from retrieved chunks — no hallucinated ids.)
#
#   - recommended_actions: list[str] of next steps, ideally traceable to
#       runbook chunks.
#
#   - citations: list of source references backing the verdict. THIS IS
#       NON-NEGOTIABLE — every verdict must reference the source chunks it used.
#       Proposed shape per citation: { chunk_id, source_type, ref } where
#       source_type ∈ {"attack", "runbook"} and ref is the ATT&CK id or runbook
#       filename. (Open question: separate Citation model vs. inline.)
#
# Open questions to settle in pairing:
#   - Should citations be a flat list, or attached per-claim?
#   - Validation: enforce mitre_techniques ⊆ citations' ATT&CK ids?
#   - Do we want a raw `model` / token-usage field for debugging?
#
# ──────────────────────────────────────────────────────────────────────────

# TODO(pairing): define the Pydantic v2 model(s) (e.g. `class TriageVerdict
# (BaseModel): ...`) once the fields above are agreed. Generation code in
# query.py validates against whatever this module exports.
