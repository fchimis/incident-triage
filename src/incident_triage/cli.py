"""CLI entrypoint wired to `[project.scripts]` in pyproject.toml.

    uv sync
    uv run triage                # runs the demo on the golden set
    uv run triage --eval         # prints the eval report as JSON
    GEMINI_API_KEY=... uv run triage --live --eval
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .client import GeminiError, MockGeminiClient, RealGeminiClient
from .evaluation import evaluate, load_cases
from .logging_config import configure_logging
from .pipeline import TriagePipeline


def _default_data_path() -> Path:
    # Walk up from this file to find the repo `data/` folder (works for both
    # `uv run triage` and `python -m incident_triage.cli`).
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / "data" / "golden_incidents.jsonl"
        if candidate.exists():
            return candidate
    return Path.cwd() / "data" / "golden_incidents.jsonl"


def _load_dotenv() -> None:
    """Load `.env` from the repo root if python-dotenv is installed. Silent
    no-op otherwise — the env var may already be exported by the shell."""
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    # Walk up from cli.py looking for the first `.env` we find.
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / ".env"
        if candidate.exists():
            load_dotenv(candidate)
            return


def _build_client(live: bool):
    if not live:
        return MockGeminiClient()
    _load_dotenv()
    try:
        return RealGeminiClient()
    except GeminiError as e:
        print(f"[warn] cannot use real Gemini ({e}); falling back to mock.",
              file=sys.stderr)
        return MockGeminiClient()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="triage", description="Incident triage demo")
    parser.add_argument("--live", action="store_true", help="Use real Gemini API")
    parser.add_argument("--eval", action="store_true",
                        help="Run the golden-set evaluation instead of the demo")
    parser.add_argument("--quiet", action="store_true", help="Suppress structured logs")
    parser.add_argument("--data", type=Path, default=None,
                        help="Path to a JSONL of eval cases (default: data/golden_incidents.jsonl)")
    parser.add_argument("--limit", type=int, default=4,
                        help="How many cases to show in demo mode (default 4)")
    args = parser.parse_args(argv)

    configure_logging(logging.WARNING if args.quiet else logging.INFO)
    # google-genai + httpx are chatty at INFO — mute them so the demo
    # output stays about *our* pipeline, not the SDK's plumbing.
    for noisy in ("google_genai", "httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    client = _build_client(args.live)
    pipeline = TriagePipeline(client=client)
    cases = load_cases(args.data or _default_data_path())

    try:
        if args.eval:
            report = evaluate(pipeline, cases)
            print(json.dumps(report.to_dict(), indent=2))
            return 0

        for case in cases[: args.limit]:
            result = pipeline.run({
                "incident_id": case.incident_id,
                "description": case.description,
            })
            print("=" * 70)
            print(f"{case.incident_id}: {case.description[:80]}...")
            print(result.model_dump_json(indent=2, exclude_none=True))
        return 0
    except Exception as e:  # noqa: BLE001
        # Fatal errors from the SDK (bad API key, deprecated model, quota)
        # should not spam a 40-line traceback in a demo.
        print(f"\n[error] {type(e).__name__}: {e}", file=sys.stderr)
        if args.live:
            print(
                "\nHint: set GEMINI_MODEL in your .env if Google renamed the "
                "default model (e.g. GEMINI_MODEL=gemini-3.6-flash), or check "
                "GEMINI_API_KEY.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
