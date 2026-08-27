"""Best-effort PII / secret redaction before the incident text ever leaves
our process.

Design notes
------------
* This is a defence-in-depth measure, not a substitute for Google Cloud DLP.
  In production the same input would also flow through DLP (see README).
* We prefer *tagging* the redacted spans (e.g. `[REDACTED_EMAIL]`) rather
  than deleting them, so the model still sees the shape of the sentence and
  can reason about it ("the user reported an issue with their [REDACTED_EMAIL]
  account").
* If any redaction fires, the pipeline sets `possible_pii` as a review reason
  so a human sees the *unredacted* record via a controlled UI.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Ordered: apply more specific patterns before generic ones. CARD runs before
# PHONE so a Luhn-valid PAN is not mis-tagged as a phone number.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")),
    # 13-19 digit runs, allowing spaces/dashes. Followed by Luhn check below.
    ("CARD", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("IPV4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # Loose phone pattern: at least 7 digits with common separators.
    ("PHONE", re.compile(r"(?:\+?\d{1,3}[\s\-.]?)?(?:\(?\d{2,4}\)?[\s\-.]?){2,4}\d{2,4}")),
    # JWT-ish
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")),
    # AWS-style access keys, GCP service account keys, generic API keys
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GENERIC_SECRET", re.compile(
        r"(?i)(?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*[\"']?([A-Za-z0-9_\-./+]{8,})[\"']?"
    )),
]


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0 and len(digits) >= 13


@dataclass(frozen=True)
class RedactionReport:
    redacted_text: str
    counts: dict[str, int]

    @property
    def any_hit(self) -> bool:
        return any(v > 0 for v in self.counts.values())


def redact(text: str) -> RedactionReport:
    """Return a copy of `text` with sensitive spans replaced by tokens, plus
    per-category counts. The function is deliberately conservative: it
    prefers false positives (over-redaction) to false negatives, since the
    downstream signal is only ``possible_pii`` review, not silent data loss.
    """
    if not text:
        return RedactionReport(redacted_text=text, counts={})

    counts: dict[str, int] = {}
    out = text
    for label, pattern in _PATTERNS:
        def _sub(match: re.Match[str], _label: str = label) -> str:
            raw = match.group(0)
            # Extra guard on CARD: only redact when digits pass Luhn, else
            # we'd eat every phone / order number in the corpus.
            if _label == "CARD":
                digits = re.sub(r"\D", "", raw)
                if not _luhn_ok(digits):
                    return raw
            counts[_label] = counts.get(_label, 0) + 1
            return f"[REDACTED_{_label}]"
        out = pattern.sub(_sub, out)
    return RedactionReport(redacted_text=out, counts=counts)
