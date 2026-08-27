from typing import Any

import pytest
from pydantic import ValidationError

from incident_triage.client import GeminiError, MockGeminiClient, _with_retries
from incident_triage.pipeline import FALLBACK_RATIONALE, TriagePipeline, is_fallback
from incident_triage.schemas import Category, Priority, ReviewReason


def _run(payload_desc: str, forced: dict[str, Any] | None = None):
    client = MockGeminiClient(forced_response=forced)
    return TriagePipeline(client=client).run(
        {"incident_id": "T1", "description": payload_desc}
    )


def test_network_outage_high_priority():
    r = _run("Everyone is down, cannot access anything, complete outage.")
    assert r.category is Category.NETWORK
    assert r.priority is Priority.P1
    assert r.needs_human_review is False


def test_mock_prefers_specific_database_signal_over_generic_down_word():
    r = _run("The Postgres database is down for checkout.")
    assert r.category is Category.DATABASE
    assert r.priority is Priority.P2


def test_security_always_deferred():
    r = _run("We think there is a possible phishing attempt from IT support.")
    assert r.category is Category.SECURITY
    assert r.needs_human_review is True
    assert ReviewReason.SECURITY_SENSITIVE in r.review_reasons


def test_security_forced_to_review_even_when_model_says_no():
    forced = {
        "category": "security",
        "summary": "Possible credential leak.",
        "priority": "P1",
        "next_action": "Escalate to security-on-call.",
        "confidence": 0.9,
        "needs_human_review": False,
        "review_reasons": [],
        "rationale": "Input mentions leaked credentials.",
    }
    r = _run("Credentials may have leaked in a shared channel.", forced=forced)
    assert r.needs_human_review is True
    assert ReviewReason.SECURITY_SENSITIVE in r.review_reasons


def test_review_reason_forces_human_review_boolean():
    forced = {
        "category": "performance",
        "summary": "Latency spike.",
        "priority": "P3",
        "next_action": "Check recent latency metrics.",
        "confidence": 0.81,
        "needs_human_review": False,
        "review_reasons": ["conflicting_signals"],
        "rationale": "Input mentions latency and intermittent errors.",
    }
    r = _run("The service is slow but also throws intermittent 500s.", forced=forced)
    assert r.needs_human_review is True
    assert ReviewReason.CONFLICTING_SIGNALS in r.review_reasons


def test_pii_forces_review():
    r = _run("Please email me at bob@example.com for the outage details.")
    assert r.needs_human_review is True
    assert ReviewReason.POSSIBLE_PII in r.review_reasons


def test_unknown_deferred():
    r = _run("something happened idk")
    # The mock's fallback branch — no keyword match.
    assert r.category is Category.UNKNOWN
    assert r.needs_human_review is True


def test_low_confidence_deferred_even_when_model_says_no():
    forced = {
        "category": "performance",
        "summary": "s",
        "priority": "P3",
        "next_action": "a",
        "confidence": 0.4,           # below floor
        "needs_human_review": False,  # model lied about confidence
        "review_reasons": [],
        "rationale": "r",
    }
    r = _run("Some slow thing.", forced=forced)
    assert r.needs_human_review is True
    assert ReviewReason.LOW_CONFIDENCE in r.review_reasons


def test_invalid_incident_input_rejected():
    with pytest.raises(ValidationError):
        TriagePipeline(client=MockGeminiClient()).run(
            {"incident_id": "", "description": "x"}
        )


def test_pipeline_falls_back_when_model_returns_junk():
    class JunkClient:
        model_name = "junk"

        def generate_json(self, *_a, **_k):
            return {"nope": "no useful fields"}

    r = TriagePipeline(client=JunkClient()).run(
        {"incident_id": "J1", "description": "the DB is down"}
    )
    assert r.category is Category.UNKNOWN
    assert r.needs_human_review is True
    assert is_fallback(r)
    assert r.rationale == FALLBACK_RATIONALE


def test_retry_helper_backs_off_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise GeminiError("transient")
        return {"ok": True}

    sleeps = []
    out = _with_retries(
        flaky, max_retries=3, base_delay_s=0.01, max_delay_s=0.1,
        sleep=sleeps.append, rng=lambda: 0.5,
    )
    assert out == {"ok": True}
    assert calls["n"] == 2
    assert sleeps  # a delay happened


def test_retry_helper_raises_after_exhaustion():
    def always_fail():
        raise GeminiError("nope")

    with pytest.raises(GeminiError):
        _with_retries(
            always_fail, max_retries=2, base_delay_s=0.01, max_delay_s=0.05,
            sleep=lambda _s: None, rng=lambda: 0.5,
        )


def test_retry_helper_rejects_negative_max_retries():
    with pytest.raises(ValueError):
        _with_retries(
            lambda: {"ok": True}, max_retries=-1, base_delay_s=0.01, max_delay_s=0.1,
        )
