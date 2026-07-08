"""The output contract (triage/schema.py): what passes, and what must not.

The negative cases matter more than the positive one here — schema.py is the
enforcement layer for "citations are non-negotiable", so each rule gets a
test proving it actually rejects. parametrize runs one test PER bad payload:
each case shows up individually in the report and one failure can't mask
another (a loop of asserts would stop at the first).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from triage.schema import TriageVerdict


def valid_payload() -> dict[str, Any]:
    """A minimal verdict that satisfies every rule; tests break ONE thing each."""
    return {
        "verdict": "true_positive",
        "severity": "high",
        "confidence": 0.85,
        "summary": "Encoded PowerShell spawned by Word; matches T1059.001.",
        "mitre_techniques": ["T1059.001"],
        "recommended_actions": ["Isolate the host."],
        "citations": [
            {
                "chunk_id": "T1059.001",
                "source_type": "attack",
                "ref": "T1059.001",
                "quote": "Adversaries may abuse PowerShell.",
            },
            {
                "chunk_id": "rb-powershell.md",
                "source_type": "runbook",
                "ref": "rb-powershell.md",
            },
        ],
    }


def test_valid_verdict_passes() -> None:
    verdict = TriageVerdict.model_validate(valid_payload())
    assert verdict.verdict.value == "true_positive"
    assert verdict.citations[0].ref == "T1059.001"


def break_payload(**changes: Any) -> dict[str, Any]:
    return {**valid_payload(), **changes}


@pytest.mark.parametrize(
    ("payload", "expected_in_error"),
    [
        pytest.param(
            break_payload(citations=[]),
            "citations",
            id="no-citations",
        ),
        pytest.param(
            break_payload(mitre_techniques=["T1059.001", "T1566"]),
            "T1566",
            id="uncited-technique",
        ),
        pytest.param(
            # The technique IS cited — but by a runbook-typed citation, which
            # must not count as an ATT&CK source.
            break_payload(
                mitre_techniques=["T1110"],
                citations=[
                    {
                        "chunk_id": "T1110",
                        "source_type": "runbook",
                        "ref": "T1110",
                    }
                ],
            ),
            "T1110",
            id="technique-cited-by-wrong-source-type",
        ),
        pytest.param(
            break_payload(confidence=1.5),
            "confidence",
            id="confidence-above-1",
        ),
        pytest.param(
            break_payload(confidence=-0.1),
            "confidence",
            id="confidence-below-0",
        ),
        pytest.param(
            break_payload(verdict="probably_bad"),
            "verdict",
            id="unknown-verdict-enum",
        ),
        pytest.param(
            break_payload(surprise_field="hello"),
            "surprise_field",
            id="unknown-field-forbidden",
        ),
    ],
)
def test_invalid_verdicts_are_rejected(
    payload: dict[str, Any], expected_in_error: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        TriageVerdict.model_validate(payload)
    # The error must NAME the problem — "fail loudly" includes being
    # debuggable from the message alone.
    assert expected_in_error in str(exc_info.value)
