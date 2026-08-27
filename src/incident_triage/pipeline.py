"""End-to-end pipeline: validated input in, validated `TriageResult` out.

Steps for each incident:

    1. Validate the raw dict against `IncidentInput`. Reject junk early.
    2. Redact obvious PII / secrets. Track whether any hits occurred.
    3. Render the versioned prompt.
    4. Call Gemini (or the mock) with a JSON response_schema.
    5. Parse into `TriageResult`. If parsing fails, retry once with a
       corrective nudge; if it still fails, escalate to human review.
    6. Apply safety overrides: if we redacted anything, force
       `possible_pii` + human review; if the model chose `unknown` or
       `security`, ditto; if confidence < threshold, ditto.
    7. Attach metadata (model, prompt version, latency) and log a
       structured record for observability.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .client import GeminiClient, GeminiError, MockGeminiClient
from .logging_config import bind_context
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, render_user_prompt
from .redaction import redact
from .schemas import (
    Category,
    IncidentInput,
    ReviewReason,
    TriageResult,
    default_processed_at,
)

logger = logging.getLogger(__name__)


# The JSON schema we pass to Gemini via response_schema. Kept in one place so
# it stays in sync with the Pydantic model. We hand-roll it (rather than
# using model_json_schema()) because Gemini's schema subset does not accept
# all the JSON-Schema keywords Pydantic emits.
GEMINI_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "incident_id": {"type": "string"},
        "category": {"type": "string", "enum": [c.value for c in Category]},
        "summary": {"type": "string"},
        "priority": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
        "next_action": {"type": "string"},
        "confidence": {"type": "number"},
        "needs_human_review": {"type": "boolean"},
        "review_reasons": {
            "type": "array",
            "items": {"type": "string", "enum": [r.value for r in ReviewReason]},
        },
        "rationale": {"type": "string"},
    },
    "required": [
        "incident_id", "category", "summary", "priority",
        "next_action", "confidence", "needs_human_review",
        "review_reasons", "rationale",
    ],
}


# If the model reports below this we always defer, regardless of what it says.
DEFAULT_CONFIDENCE_FLOOR = 0.60

# Rationale text on the deterministic fallback result. Exported so downstream
# code (e.g. the eval harness) can identify a fallback without string-sniffing
# the human-readable field.
FALLBACK_RATIONALE = (
    "Fallback result generated because the model response failed validation."
)


@dataclass
class TriagePipeline:
    client: GeminiClient
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR

    def run(self, incident: dict[str, Any] | IncidentInput) -> TriageResult:
        started = time.perf_counter()
        trace_id = str(uuid.uuid4())

        # 1. Input validation.
        if isinstance(incident, IncidentInput):
            inc = incident
        else:
            try:
                inc = IncidentInput.model_validate(incident)
            except ValidationError as e:
                logger.warning("input_rejected", extra={
                    "trace_id": trace_id, "errors": e.errors(),
                })
                raise

        ctx = bind_context(
            trace_id=trace_id,
            incident_id=inc.incident_id,
            prompt_version=PROMPT_VERSION,
            model=self.client.model_name,
        )
        ctx.info("triage_started")

        # 2. Redact before we send anything downstream.
        red = redact(inc.description)
        if red.any_hit:
            ctx.info("redaction_applied", extra={"counts": red.counts})

        # 3. Prompt.
        user_prompt = render_user_prompt(
            incident_id=inc.incident_id,
            description=red.redacted_text,
            source=inc.source,
            reported_at=inc.reported_at.isoformat() if inc.reported_at else None,
        )

        # 4 + 5. Call Gemini and parse; one corrective retry if invalid.
        raw = self._call_and_parse(user_prompt, ctx)

        if raw is None:
            result = self._fallback_result(inc.incident_id, reason=ReviewReason.LOW_CONFIDENCE)
        else:
            # Force the incident_id we know is correct; ignore whatever the
            # model echoed back. Prevents drift if the model mangles the id.
            raw["incident_id"] = inc.incident_id
            try:
                result = TriageResult.model_validate(raw)
            except ValidationError as e:
                ctx.warning("output_validation_failed", extra={"errors": e.errors()})
                result = self._fallback_result(inc.incident_id, reason=ReviewReason.LOW_CONFIDENCE)

        # 6. Safety overrides.
        forced_reasons: list[ReviewReason] = []
        if red.any_hit and ReviewReason.POSSIBLE_PII not in result.review_reasons:
            forced_reasons.append(ReviewReason.POSSIBLE_PII)
        if result.confidence < self.confidence_floor and ReviewReason.LOW_CONFIDENCE not in result.review_reasons:
            forced_reasons.append(ReviewReason.LOW_CONFIDENCE)
        if result.category is Category.UNKNOWN and ReviewReason.INSUFFICIENT_INFORMATION not in result.review_reasons:
            forced_reasons.append(ReviewReason.INSUFFICIENT_INFORMATION)
        if result.category is Category.SECURITY and ReviewReason.SECURITY_SENSITIVE not in result.review_reasons:
            forced_reasons.append(ReviewReason.SECURITY_SENSITIVE)
        if forced_reasons or result.review_reasons:
            result = result.model_copy(update={
                "needs_human_review": True,
                "review_reasons": list(result.review_reasons) + forced_reasons,
            })

        # 7. Metadata + observability.
        latency_ms = int((time.perf_counter() - started) * 1000)
        result = result.model_copy(update={
            "model_name": self.client.model_name,
            "prompt_version": PROMPT_VERSION,
            "processed_at": default_processed_at(),
            "latency_ms": latency_ms,
        })
        ctx.info("triage_completed", extra={
            "category": result.category.value,
            "priority": result.priority.value,
            "confidence": result.confidence,
            "needs_human_review": result.needs_human_review,
            "review_reasons": [r.value for r in result.review_reasons],
            "latency_ms": latency_ms,
        })
        return result

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _call_and_parse(self, user_prompt: str, ctx) -> dict[str, Any] | None:
        """Call Gemini, optionally retry once with a corrective nudge, return
        the raw dict if it has the shape we require or ``None`` if both
        attempts failed. Returning ``None`` (not ``{}``) makes the caller's
        control flow explicit — an empty dict is a valid but unhelpful value
        that we never want silently forwarded to Pydantic."""
        for attempt, prompt in enumerate((user_prompt, _corrective(user_prompt))):
            try:
                raw = self.client.generate_json(
                    SYSTEM_PROMPT, prompt, GEMINI_RESPONSE_SCHEMA
                )
            except GeminiError as e:
                event = "model_call_failed_retry" if attempt else "model_call_failed"
                ctx.warning(event, extra={"error": str(e)})
                continue
            if _looks_like_triage_json(raw):
                return raw
            ctx.warning("model_response_missing_fields", extra={"raw_keys": list(raw)})
        return None

    def _fallback_result(self, incident_id: str, reason: ReviewReason) -> TriageResult:
        return TriageResult(
            incident_id=incident_id,
            category=Category.UNKNOWN,
            summary="Automated triage unavailable; deferred to human.",
            priority="P3",
            next_action="Human review required: model output could not be validated.",
            confidence=0.0,
            needs_human_review=True,
            review_reasons=[reason, ReviewReason.INSUFFICIENT_INFORMATION],
            rationale=FALLBACK_RATIONALE,
        )


_REQUIRED_KEYS = frozenset({
    "category", "summary", "priority", "next_action", "confidence",
    "needs_human_review", "review_reasons", "rationale",
})


def _looks_like_triage_json(raw: object) -> bool:
    return isinstance(raw, dict) and _REQUIRED_KEYS.issubset(raw.keys())


def _corrective(user_prompt: str) -> str:
    """Prompt appended after a first invalid response, asking the model to
    return only the JSON object."""
    return user_prompt + (
        "\n\nYour previous response was not valid. Return ONLY the JSON "
        "object with every required field. Do not include explanation."
    )


def is_fallback(result: TriageResult) -> bool:
    """True if `result` was produced by the deterministic fallback path
    (model output could not be validated). Kept alongside FALLBACK_RATIONALE
    so downstream code doesn't have to string-sniff the rationale."""
    return result.rationale == FALLBACK_RATIONALE


# Convenience for one-shot callers, defaults to the mock so `python -m` works
# out of the box.
def triage_incident(
    incident: dict[str, Any], client: GeminiClient | None = None
) -> TriageResult:
    pipeline = TriagePipeline(client=client or MockGeminiClient())
    return pipeline.run(incident)
