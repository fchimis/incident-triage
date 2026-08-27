"""Incident triage package: turn free-text operational incidents into
validated, structured triage decisions using Gemini.

Public surface: `triage_incident` and the `TriageResult` schema.
"""
from .pipeline import TriagePipeline, triage_incident
from .schemas import (
    Category,
    IncidentInput,
    Priority,
    ReviewReason,
    TriageResult,
)

__all__ = [
    "IncidentInput",
    "TriageResult",
    "Category",
    "Priority",
    "ReviewReason",
    "triage_incident",
    "TriagePipeline",
]

__version__ = "0.1.0"
