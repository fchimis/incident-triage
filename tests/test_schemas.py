from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from incident_triage.schemas import (
    Category,
    IncidentInput,
    Priority,
    ReviewReason,
    TriageResult,
)


def test_incident_input_strips_whitespace():
    inc = IncidentInput(incident_id="ABC", description="  hello  ")
    assert inc.description == "hello"


def test_incident_input_rejects_empty_description():
    with pytest.raises(ValidationError):
        IncidentInput(incident_id="ABC", description="   ")


def test_incident_input_rejects_missing_id():
    with pytest.raises(ValidationError):
        IncidentInput(description="something happened")  # type: ignore[call-arg]


def test_triage_result_confidence_bounds():
    with pytest.raises(ValidationError):
        TriageResult(
            incident_id="X",
            category=Category.NETWORK,
            summary="s",
            priority=Priority.P2,
            next_action="a",
            confidence=1.5,  # out of range
            needs_human_review=False,
            review_reasons=[],
            rationale="r",
        )


def test_triage_result_accepts_review_reasons():
    r = TriageResult(
        incident_id="X",
        category=Category.SECURITY,
        summary="s",
        priority=Priority.P1,
        next_action="a",
        confidence=0.9,
        needs_human_review=True,
        review_reasons=[ReviewReason.SECURITY_SENSITIVE],
        rationale="r",
        processed_at=datetime.now(UTC),
    )
    assert r.review_reasons == [ReviewReason.SECURITY_SENSITIVE]
