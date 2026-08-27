"""Thin wrapper around the Gemini generation call.

The wrapper is deliberately narrow:

* It exposes a single `generate_json` method that returns a raw dict.
* It handles retries with jittered backoff for transient errors.
* It does NOT parse into `TriageResult` — that's the pipeline's job so
  validation failures can trigger their own retry logic.
* It has a `MockGeminiClient` twin so tests and demos run without a real
  API key. The interface both classes share is the ``GeminiClient`` Protocol.
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class GeminiError(RuntimeError):
    """Raised when Gemini cannot produce a usable response after retries."""


class GeminiClient(Protocol):
    def generate_json(
        self, system_prompt: str, user_prompt: str, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        ...

    @property
    def model_name(self) -> str: ...


# ---------------------------------------------------------------------------
# Real Gemini client
# ---------------------------------------------------------------------------


class RealGeminiClient:
    """Live Gemini client. Optional install: `uv sync --extra live`."""

    # Default model — overridable via env var so the demo doesn't require a
    # code change if Google renames the current generation again.
    DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        max_retries: int = 3,
        base_delay_s: float = 1.0,
        max_delay_s: float = 10.0,
    ) -> None:
        try:
            from google import genai  # type: ignore
        except ImportError as e:  # pragma: no cover - depends on install
            raise GeminiError(
                "google-genai not installed. `uv sync --extra live` "
                "or use MockGeminiClient for offline runs."
            ) from e
        self._genai = genai
        self._model = model or self.DEFAULT_MODEL
        self._client = genai.Client(api_key=api_key or os.environ.get("GEMINI_API_KEY"))
        self.max_retries = max_retries
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s

    @property
    def model_name(self) -> str:
        return self._model

    def generate_json(
        self, system_prompt: str, user_prompt: str, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        from google.genai import types  # type: ignore

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=response_schema,
            # Low temperature: this is a classification task, we want stability
            # across identical inputs so the eval scores mean something.
            temperature=0.1,
            top_p=0.95,
        )

        from google.genai import errors as genai_errors  # type: ignore

        def _call() -> dict[str, Any]:
            try:
                resp = self._client.models.generate_content(
                    model=self._model,
                    contents=user_prompt,
                    config=config,
                )
            except genai_errors.APIError as e:
                # 5xx / 429 are transient — wrap as GeminiError so
                # `_with_retries` retries with backoff. 4xx (bad key, bad
                # model name, quota exhausted) is fatal — surface it plain.
                status = getattr(e, "code", None) or getattr(e, "status_code", None)
                if isinstance(status, int) and (status == 429 or 500 <= status < 600):
                    raise GeminiError(f"transient Gemini error {status}: {e}") from e
                raise
            raw = getattr(resp, "text", None) or ""
            if not raw.strip():
                raise GeminiError("empty response from Gemini")
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                raise GeminiError(f"non-JSON response: {e}: {raw[:200]!r}") from e

        return _with_retries(
            _call,
            max_retries=self.max_retries,
            base_delay_s=self.base_delay_s,
            max_delay_s=self.max_delay_s,
        )


# ---------------------------------------------------------------------------
# Mock client — deterministic, keyword-driven. Enough to exercise the pipeline.
# ---------------------------------------------------------------------------


class MockGeminiClient:
    """A small rule-based stand-in for Gemini so tests and the demo run
    offline. It returns realistic-looking JSON that exercises every branch:
    high-confidence categorisation, low-confidence deferral, PII sightings,
    and the `unknown` fallback.
    """
    model_name_value = "mock-gemini-2.5-flash"

    def __init__(self, forced_response: dict[str, Any] | None = None) -> None:
        self._forced = forced_response

    @property
    def model_name(self) -> str:
        return self.model_name_value

    def generate_json(
        self, system_prompt: str, user_prompt: str, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        if self._forced is not None:
            return dict(self._forced)

        description = _extract_description(user_prompt)
        text = description.lower()
        # Extract the incident_id from the templated prompt for the mock.
        incident_id = "UNKNOWN"
        for line in user_prompt.splitlines():
            if line.lower().startswith("incident id:"):
                incident_id = line.split(":", 1)[1].strip() or "UNKNOWN"
                break

        # Simple keyword rules — only for offline testing. More specific
        # categories run before broad outage words such as "down".
        rules = [
            (("login", "password", "mfa", "sso", "sign in"),
             "authentication", "P2",
             "Check IdP status and recent auth-service deploys.",
             0.82),
            (("database", "db ", "postgres", "mysql", "sql "),
             "database", "P2",
             "Check DB primary health and replication lag.",
             0.80),
            (("slow", "latency", "timeout", "performance"),
             "performance", "P3",
             "Pull the last hour of latency metrics and top-N slow endpoints.",
             0.72),
            (("phishing", "malware", "breach", "leaked", "exfiltration"),
             "security", "P1",
             "Escalate to security-on-call; do not reply to the reporter yet.",
             0.90),
            (("printer", "laptop", "monitor", "hardware", "keyboard"),
             "hardware", "P3",
             "File a hardware ticket with asset tag; offer a spare device.",
             0.75),
            (("invoice", "billing", "payment", "charged"),
             "billing", "P3",
             "Forward to finance ops with the invoice reference.",
             0.72),
            (("error", "exception", "500", "crash"),
             "application_error", "P2",
             "Check recent deploys and error-rate dashboard for the affected service.",
             0.68),
            ((
                "outage", "down", "cannot access", "unable to access",
                "unable to reach", "unreachable", "all offices", "all users",
            ),
             "network", "P1",
             "Investigate network / edge connectivity; page on-call SRE.",
             0.88),
            (("access request", "access to", "permission", "grant", "role"),
             "access_request", "P3",
             "Route to the access-management workflow.",
             0.70),
        ]

        for keywords, cat, prio, action, conf in rules:
            if any(k in text for k in keywords):
                needs_review = conf < 0.75 or cat == "security"
                reasons = []
                if conf < 0.75:
                    reasons.append("low_confidence")
                if cat == "security":
                    reasons.append("security_sensitive")
                if "[redacted_" in text:
                    reasons.append("possible_pii")
                    needs_review = True
                return {
                    "incident_id": incident_id,
                    "category": cat,
                    "summary": f"Auto-triaged as {cat}: {_first_sentence(description, 200)}",
                    "priority": prio,
                    "next_action": action,
                    "confidence": conf,
                    "needs_human_review": needs_review,
                    "review_reasons": reasons,
                    "rationale": f"Matched keywords consistent with {cat}.",
                }

        # Fallback: unknown.
        return {
            "incident_id": incident_id,
            "category": "unknown",
            "summary": "Insufficient information to categorise the incident.",
            "priority": "P3",
            "next_action": "Ask the reporter for the affected service, error message, and time of occurrence.",
            "confidence": 0.35,
            "needs_human_review": True,
            "review_reasons": ["insufficient_information", "low_confidence"],
            "rationale": "No strong keyword signal in the description.",
        }


def _extract_description(user_prompt: str) -> str:
    """Pull the description block out of the templated prompt for the mock."""
    marker = "Description:"
    if marker not in user_prompt:
        return user_prompt
    after_marker = user_prompt.split(marker, 1)[1]
    parts = after_marker.split("---")
    if len(parts) >= 3:
        return parts[1].strip()
    return after_marker.strip()


def _first_sentence(text: str, limit: int) -> str:
    text = text.strip().replace("\n", " ")
    for stop in [". ", "! ", "? "]:
        idx = text.find(stop)
        if 0 < idx < limit:
            return text[: idx + 1]
    return text[:limit]


# ---------------------------------------------------------------------------
# Retry helper — used by the real client; kept module-level so it's testable.
# ---------------------------------------------------------------------------


def _with_retries(
    fn: Callable[[], dict[str, Any]],
    *,
    max_retries: int,
    base_delay_s: float,
    max_delay_s: float,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
) -> dict[str, Any]:
    """Retry `fn` on `GeminiError` up to `max_retries` extra times with
    jittered exponential backoff. Any other exception is not caught: it
    propagates immediately so bugs don't hide behind the retry loop."""
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")

    for attempt in range(max_retries + 1):
        try:
            return fn()
        except GeminiError as exc:
            if attempt == max_retries:
                # Re-raise from within the except block so the traceback keeps
                # the original chain — no assert / no dangling reference.
                raise
            delay = min(base_delay_s * (2 ** attempt), max_delay_s) * (0.5 + rng())
            logger.warning(
                "gemini call failed (attempt %d/%d): %s — retrying in %.2fs",
                attempt + 1, max_retries + 1, exc, delay,
            )
            sleep(delay)
    # Unreachable: the loop either returns or re-raises, but keep an explicit
    # error here so the type checker is happy.
    raise GeminiError("retry loop exited without result")
