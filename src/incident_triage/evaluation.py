"""Offline evaluation harness.

Load a JSONL of incidents with expected labels, run the pipeline, and
compute a small set of metrics that answer the question posed in the
challenge: *is this thing actually good enough?*

Metrics returned per run
------------------------
* ``category_accuracy`` — micro-averaged accuracy on category.
* ``category_f1_per_class`` — F1 per category, useful to catch cases where
  the model is good overall but blind to (say) `security`.
* ``priority_within_one`` — fraction of predictions within one priority band
  of the label. Priority is subjective; strict match under-reports quality.
* ``deferral_rate`` — how often the pipeline set ``needs_human_review``.
* ``correct_deferrals`` — of deferred cases, how many were also cases we
  expected to defer.
* ``json_validity_rate`` — fraction of runs that produced a schema-valid
  ``TriageResult`` without falling back.
* ``avg_latency_ms`` — mean end-to-end latency.

The harness is small on purpose: for real use we would layer LLM-as-judge
scoring on top of these deterministic metrics — see README.
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .pipeline import TriagePipeline, is_fallback
from .schemas import Priority, TriageResult

PRIORITY_INDEX = {p.value: i for i, p in enumerate([Priority.P1, Priority.P2, Priority.P3, Priority.P4])}


@dataclass
class EvalCase:
    incident_id: str
    description: str
    expected_category: str
    expected_priority: str
    expected_review: bool


@dataclass
class CaseResult:
    case: EvalCase
    result: TriageResult
    category_ok: bool
    priority_delta: int  # 0 = exact match, 1 = one band off, etc.
    review_match: bool


@dataclass
class EvalReport:
    n: int
    category_accuracy: float
    priority_within_one: float
    deferral_rate: float
    correct_deferrals: float
    json_validity_rate: float
    avg_latency_ms: float
    category_f1_per_class: dict[str, float] = field(default_factory=dict)
    cases: list[CaseResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "category_accuracy": round(self.category_accuracy, 4),
            "priority_within_one": round(self.priority_within_one, 4),
            "deferral_rate": round(self.deferral_rate, 4),
            "correct_deferrals": round(self.correct_deferrals, 4),
            "json_validity_rate": round(self.json_validity_rate, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "category_f1_per_class": {k: round(v, 4) for k, v in self.category_f1_per_class.items()},
        }


def load_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            exp = row["expected"]
            cases.append(EvalCase(
                incident_id=row["incident_id"],
                description=row["description"],
                expected_category=exp["category"],
                expected_priority=exp["priority"],
                expected_review=exp["needs_human_review"],
            ))
    return cases


def evaluate(pipeline: TriagePipeline, cases: Iterable[EvalCase]) -> EvalReport:
    results: list[CaseResult] = []
    valid_count = 0
    total_latency = 0

    for case in cases:
        inc = {"incident_id": case.incident_id, "description": case.description}
        r = pipeline.run(inc)
        # Count as "valid" any result that isn't the deterministic fallback.
        if not is_fallback(r):
            valid_count += 1

        cat_ok = r.category.value == case.expected_category
        prio_delta = abs(
            PRIORITY_INDEX[r.priority.value] - PRIORITY_INDEX[case.expected_priority]
        )
        review_match = r.needs_human_review == case.expected_review
        results.append(CaseResult(case, r, cat_ok, prio_delta, review_match))
        total_latency += (r.latency_ms or 0)

    n = len(results)
    if n == 0:
        return EvalReport(0, 0, 0, 0, 0, 0, 0)

    cat_acc = sum(cr.category_ok for cr in results) / n
    prio_ok = sum(cr.priority_delta <= 1 for cr in results) / n

    deferred = [cr for cr in results if cr.result.needs_human_review]
    deferral_rate = len(deferred) / n
    correct_deferrals = (
        sum(1 for cr in deferred if cr.case.expected_review) / len(deferred)
        if deferred else 1.0
    )

    return EvalReport(
        n=n,
        category_accuracy=cat_acc,
        priority_within_one=prio_ok,
        deferral_rate=deferral_rate,
        correct_deferrals=correct_deferrals,
        json_validity_rate=valid_count / n,
        avg_latency_ms=total_latency / n,
        category_f1_per_class=_per_class_f1(results),
        cases=results,
    )


def _per_class_f1(results: list[CaseResult]) -> dict[str, float]:
    tp: dict[str, int] = defaultdict(int)
    fp: dict[str, int] = defaultdict(int)
    fn: dict[str, int] = defaultdict(int)
    for cr in results:
        pred = cr.result.category.value
        gold = cr.case.expected_category
        if pred == gold:
            tp[gold] += 1
        else:
            fp[pred] += 1
            fn[gold] += 1
    classes = set(tp) | set(fp) | set(fn)
    f1 = {}
    for c in classes:
        p = tp[c] / (tp[c] + fp[c]) if (tp[c] + fp[c]) else 0.0
        r = tp[c] / (tp[c] + fn[c]) if (tp[c] + fn[c]) else 0.0
        f1[c] = 2 * p * r / (p + r) if (p + r) else 0.0
    return f1
