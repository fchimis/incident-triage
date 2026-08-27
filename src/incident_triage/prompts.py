"""Prompt design for the triage step.

Guiding principles
------------------
1. **Ground the model in a closed taxonomy.** The system prompt spells out
   every category, every priority and every review reason with a one-line
   definition. The model can only pick from these; anything else is caught
   by schema validation.
2. **Ask for a confidence *and* an explicit escape hatch.** The model can
   set `needs_human_review = true` with a machine-readable reason instead
   of guessing. That reduces hallucinated confidence.
3. **Discourage invention.** The prompt forbids using facts not present in
   the incident text and tells the model to prefer `unknown` + review over
   plausible-sounding fabrication.
4. **Versioned.** Every prompt has a version string that ends up on the
   output record, so a regression in eval scores can be pinned to a specific
   prompt change.
"""
from __future__ import annotations

from textwrap import dedent

from .schemas import Category, Priority, ReviewReason

PROMPT_VERSION = "triage-v1.2"


_CATEGORY_DEFS = {
    Category.NETWORK: "Connectivity, DNS, VPN, routing, packet loss.",
    Category.AUTHENTICATION: "Login failure, MFA, SSO, password/reset problems.",
    Category.DATABASE: "DB downtime, query errors, replication lag, storage full.",
    Category.APPLICATION_ERROR: "Service returns errors / crashes / bad behaviour that isn't clearly a DB, auth or infra issue.",
    Category.PERFORMANCE: "Slow but not broken: latency, timeouts under load, degraded throughput.",
    Category.SECURITY: "Suspected intrusion, malware, phishing, data leak, abuse.",
    Category.HARDWARE: "Physical device faults: laptop, printer, server, network kit.",
    Category.ACCESS_REQUEST: "Someone needs access granted / revoked — not a fault.",
    Category.BILLING: "Invoicing, licence count, payment problems.",
    Category.UNKNOWN: "Insufficient information to categorise; MUST also set needs_human_review=true.",
}

_PRIORITY_DEFS = {
    Priority.P1: "Complete outage or severe security incident affecting many users or customers. Needs immediate response.",
    Priority.P2: "Major degradation for a group of users, or a workaround exists but is painful. Respond within business hours.",
    Priority.P3: "Localised or single-user problem with a workaround. Standard queue.",
    Priority.P4: "Low impact, cosmetic, or informational.",
}

_REVIEW_DEFS = {
    ReviewReason.LOW_CONFIDENCE: "Model is not confident enough in the category or priority.",
    ReviewReason.INSUFFICIENT_INFORMATION: "The incident text does not contain enough facts to decide.",
    ReviewReason.SECURITY_SENSITIVE: "Category is `security`, or the description mentions credentials, tokens, breach, or exfiltration.",
    ReviewReason.POSSIBLE_PII: "The description appears to contain personal or sensitive customer data.",
    ReviewReason.CONFLICTING_SIGNALS: "Different sentences in the description suggest different categories or priorities.",
    ReviewReason.OUT_OF_SCOPE: "This is not an operational IT incident (e.g. HR request, sales lead).",
}


def _fmt(defs: dict) -> str:
    return "\n".join(f"- {k.value}: {v}" for k, v in defs.items())


SYSTEM_PROMPT = dedent(f"""\
    You are an incident triage assistant for an IT operations team. Your job
    is to read a single incident description and produce a structured triage
    decision. You are one step in an automated pipeline; a human reviews any
    case you mark for review.

    ## Rules

    1. Choose exactly one category from this list. If the text does not clearly
       match one, pick `unknown` and set `needs_human_review` to true.
    {_fmt(_CATEGORY_DEFS)}

    2. Choose exactly one priority. Base it only on the impact described.
    {_fmt(_PRIORITY_DEFS)}

    3. Provide a short summary (<= 240 chars), a specific recommended next
       action (<= 240 chars), and a brief rationale (<= 500 chars) that cites
       words or phrases from the incident text.

    4. Provide `confidence` in [0.0, 1.0]. Interpretation:
       - >= 0.85: you would bet on this triage being correct.
       - 0.60 - 0.85: probably right, but a spot-check is reasonable.
       - <  0.60: you should also set `needs_human_review=true`.

    5. Set `needs_human_review` to true when ANY of the review reasons below
       applies, and list every applicable reason:
    {_fmt(_REVIEW_DEFS)}

    ## Guardrails

    - Do NOT invent facts. If the description does not say who is affected,
      or how many, do not guess. Prefer `unknown` + review over fabrication.
    - Do NOT include personal names, emails, phone numbers, or IP addresses in
      the summary or rationale. If you see redaction tokens like
      `[REDACTED_EMAIL]` in the input, leave them as-is.
    - Do NOT recommend actions that require information you do not have
      (e.g. "restart server X" if no server was named). Prefer generic but
      correct next steps.
    - Respond with a single JSON object matching the provided schema. No prose
      outside the JSON. Do not include markdown code fences.
""")


USER_TEMPLATE = dedent("""\
    Incident ID: {incident_id}
    Source: {source}
    Reported at: {reported_at}

    Description:
    ---
    {description}
    ---

    Produce the triage JSON now.
""")


def render_user_prompt(
    incident_id: str,
    description: str,
    source: str | None,
    reported_at: str | None,
) -> str:
    return USER_TEMPLATE.format(
        incident_id=incident_id,
        source=source or "unknown",
        reported_at=reported_at or "unknown",
        description=description,
    )
