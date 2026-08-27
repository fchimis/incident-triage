from pathlib import Path

from incident_triage.client import MockGeminiClient
from incident_triage.evaluation import evaluate, load_cases
from incident_triage.pipeline import TriagePipeline


def test_evaluation_runs_end_to_end():
    cases = load_cases(Path(__file__).parent.parent / "data" / "golden_incidents.jsonl")
    assert len(cases) >= 10
    report = evaluate(TriagePipeline(client=MockGeminiClient()), cases)

    # With the mock, we expect:
    #   - every run to produce a validly-shaped result
    #   - category accuracy to be non-trivial (the mock uses keyword rules
    #     that mirror the golden set on purpose)
    assert report.json_validity_rate == 1.0
    assert report.category_accuracy >= 0.6, report.to_dict()

    # Deferrals should include at least the security case and the empty case.
    assert report.deferral_rate > 0
