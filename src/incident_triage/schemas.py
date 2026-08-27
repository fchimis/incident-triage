"""Typed schemas for incident triage.

Why Pydantic + closed enums:
    - We refuse to trust free-form LLM text as a category or priority. Instead,
      the model must choose from a fixed vocabulary. Anything else is a hard
      validation failure that the pipeline can catch and either retry or route
      to a human.
    - The same schema is used to (a) instruct Gemini via response_schema, (b)
      validate parsed output, and (c) serialise for downstream storage. One
      source of truth.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Closed vocabularies. Kept small on purpose: fewer buckets means a higher
# per-class F1 on the golden set, and it is easier to explain to operations
# teams what each label means. New categories should be added deliberately
# and retested, not invented ad hoc by the model.
# ---------------------------------------------------------------------------


class Category(StrEnum):
    NETWORK = "network"
    AUTHENTICATION = "authentication"
    DATABASE = "database"
    APPLICATION_ERROR = "application_error"
    PERFORMANCE = "performance"
    SECURITY = "security"
    HARDWARE = "hardware"
    ACCESS_REQUEST = "access_request"
    BILLING = "billing"
    UNKNOWN = "unknown"


class Priority(StrEnum):
    """P1 highest (customer-visible outage) to P4 lowest (cosmetic / FYI)."""
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class ReviewReason(StrEnum):
    """Explicit reasons the model can raise to defer to a human. Making
    these enum-typed rather than free text stops the model from inventing
    novel-sounding excuses and lets us count them on a dashboard."""
    LOW_CONFIDENCE = "low_confidence"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    SECURITY_SENSITIVE = "security_sensitive"
    POSSIBLE_PII = "possible_pii"
    CONFLICTING_SIGNALS = "conflicting_signals"
    OUT_OF_SCOPE = "out_of_scope"


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


class IncidentInput(BaseModel):
    """The raw incident we receive. `description` is the only required field;
    everything else is optional metadata the source system might attach."""
    incident_id: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=8000)
    source: str | None = Field(None, max_length=64)
    reporter: str | None = Field(None, max_length=128)
    reported_at: datetime | None = None

    @field_validator("description")
    @classmethod
    def _strip_and_check(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("description is empty after stripping whitespace")
        return v


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class TriageResult(BaseModel):
    """Structured triage decision. `needs_human_review` is derived from the
    model's confidence and any explicit review reasons; the pipeline may
    also force it True (for example when redaction found sensitive tokens)."""
    incident_id: str
    category: Category
    summary: str = Field(..., min_length=1, max_length=280)
    priority: Priority
    next_action: str = Field(..., min_length=1, max_length=280)
    confidence: float = Field(..., ge=0.0, le=1.0)
    needs_human_review: bool
    review_reasons: list[ReviewReason] = Field(default_factory=list)
    rationale: str = Field(..., min_length=1, max_length=600)

    # Metadata that the pipeline (not the model) fills in.
    model_name: str | None = None
    prompt_version: str | None = None
    processed_at: datetime | None = None
    latency_ms: int | None = None

    @field_validator("summary", "next_action", "rationale")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


def default_processed_at() -> datetime:
    return datetime.now(UTC)
