"""Query-path grounding (triage/query.py) with a mocked Anthropic client.

No API calls: FakeAnthropicClient returns scripted responses, so the tests
drive exactly the paths that matter — the grounding cross-check that schema.py
cannot perform (citations must point at RETRIEVED sources), the single visible
feedback retry, and the loud failure after it.

typing.cast note: generate_verdict is annotated to take anthropic.Anthropic;
cast() tells mypy "trust me, this fake plays that role" without any runtime
effect — the standard way to hand a test double to a typed signature.
"""

from __future__ import annotations

from typing import cast

import anthropic
import pytest
from pydantic import ValidationError

from tests.conftest import (
    FakeAnthropicClient,
    FakeMessages,
    FakeParseResponse,
    make_verdict,
)
from triage.query import build_grounding_prompt, generate_verdict
from triage.retrieve import RetrievedChunk
from triage.schema import SourceType, TriageVerdict

TECHNIQUE = RetrievedChunk(
    id="T1110",
    text="Brute Force (T1110)\n\nDescription (1/1):\nAdversaries may guess.",
    metadata={"source": "ATT&CK", "attack_id": "T1110", "name": "Brute Force"},
    score=0.1,
)
RUNBOOK = RetrievedChunk(
    id="rb-brute-force.md",
    text="Runbook: Brute Force [rb-brute-force.md]\n\nPart (1/1):\nSteps.",
    metadata={"source": "rb-brute-force.md"},
    score=0.9,
    backfilled=True,
)
CHUNKS = [TECHNIQUE, RUNBOOK]


def make_client(responses: list[object]) -> tuple[anthropic.Anthropic, FakeMessages]:
    messages = FakeMessages(queue=list(responses))
    return cast(anthropic.Anthropic, FakeAnthropicClient(messages)), messages


def schema_validation_error() -> ValidationError:
    """A REAL pydantic error to script the fake with, not a hand-built fake."""
    try:
        TriageVerdict.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError("empty payload unexpectedly validated")


# --- build_grounding_prompt ---------------------------------------------------


def test_prompt_refuses_empty_sources() -> None:
    with pytest.raises(ValueError, match="No retrieved sources"):
        build_grounding_prompt("alert", [])


def test_prompt_exposes_citation_contract() -> None:
    prompt = build_grounding_prompt("Suspicious logins observed.", CHUNKS)
    # The source ids in the prompt ARE the citation contract: whatever the
    # model may cite must appear verbatim as an id attribute.
    assert '<source id="T1110"' in prompt
    assert '<source id="rb-brute-force.md"' in prompt
    # The backfilled runbook must be disclosed as such, and only it.
    assert 'backfilled="true"' in prompt
    assert prompt.count('backfilled="true"') == 1
    # The ORIGINAL alert text is the grounding evidence.
    assert "<alert>\nSuspicious logins observed.\n</alert>" in prompt


# --- generate_verdict: happy path ----------------------------------------------


def test_grounded_verdict_returned_first_try() -> None:
    verdict = make_verdict([("T1110", SourceType.ATTACK)])
    client, messages = make_client([FakeParseResponse(parsed_output=verdict)])

    result = generate_verdict("alert", CHUNKS, client=client)

    assert result is verdict
    assert len(messages.calls) == 1
    # The schema itself is sent as the structured-output constraint.
    assert messages.calls[0]["output_format"] is TriageVerdict


# --- generate_verdict: the grounding cross-check + one feedback retry ----------


def hallucinated() -> TriageVerdict:
    # Schema-valid (technique cited, consistent refs) but T9999 was never
    # retrieved — exactly the failure only query.py's cross-check can catch.
    return make_verdict([("T9999", SourceType.ATTACK)])


def test_hallucinated_citation_triggers_feedback_retry_then_succeeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = make_verdict([("T1110", SourceType.ATTACK)])
    client, messages = make_client(
        [
            FakeParseResponse(parsed_output=hallucinated()),
            FakeParseResponse(parsed_output=good),
        ]
    )

    result = generate_verdict("alert", CHUNKS, client=client)

    assert result is good
    assert len(messages.calls) == 2
    # The retry prompt must carry the diagnosis and the allowed ids.
    retry_content = messages.calls[1]["messages"][0]["content"]
    assert "failed validation" in retry_content
    assert "T9999" in retry_content
    assert "T1110" in retry_content and "rb-brute-force.md" in retry_content
    # The retry is announced on stdout — never silent (capsys captures it).
    assert "retrying once" in capsys.readouterr().out


def test_second_grounding_failure_raises(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, messages = make_client(
        [
            FakeParseResponse(parsed_output=hallucinated()),
            FakeParseResponse(parsed_output=hallucinated()),
        ]
    )

    with pytest.raises(ValueError, match="after a feedback retry"):
        generate_verdict("alert", CHUNKS, client=client)
    # Exactly one retry: bounded self-correction, not a loop.
    assert len(messages.calls) == 2


def test_misattributed_source_type_is_a_grounding_error() -> None:
    # Cites a real retrieved id, but claims the runbook is an ATT&CK source.
    wrong_kind = make_verdict(
        [("rb-brute-force.md", SourceType.ATTACK)], mitre_techniques=[]
    )
    client, messages = make_client(
        [
            FakeParseResponse(parsed_output=wrong_kind),
            FakeParseResponse(parsed_output=wrong_kind),
        ]
    )

    with pytest.raises(ValueError, match="source_type"):
        generate_verdict("alert", CHUNKS, client=client)
    assert len(messages.calls) == 2


def test_schema_validation_error_is_fed_back_once() -> None:
    good = make_verdict([("T1110", SourceType.ATTACK)])
    client, messages = make_client(
        [schema_validation_error(), FakeParseResponse(parsed_output=good)]
    )

    result = generate_verdict("alert", CHUNKS, client=client)

    assert result is good
    assert "schema validation failed" in messages.calls[1]["messages"][0]["content"]


# --- generate_verdict: hard failures never retry --------------------------------


@pytest.mark.parametrize(
    ("response", "match"),
    [
        pytest.param(
            FakeParseResponse(parsed_output=None, stop_reason="refusal"),
            "refused",
            id="refusal",
        ),
        pytest.param(
            FakeParseResponse(parsed_output=None, stop_reason="max_tokens"),
            "truncated",
            id="truncation",
        ),
        pytest.param(
            FakeParseResponse(parsed_output=None),
            "no parseable verdict",
            id="no-output",
        ),
    ],
)
def test_hard_failures_raise_immediately(
    response: FakeParseResponse, match: str
) -> None:
    client, messages = make_client([response])

    with pytest.raises(RuntimeError, match=match):
        generate_verdict("alert", CHUNKS, client=client)
    # Refusal/truncation are not validation problems; feedback can't fix
    # them, so there must be no second call.
    assert len(messages.calls) == 1
