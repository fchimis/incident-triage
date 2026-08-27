"""Thin wrapper so `python scripts/run_demo.py` still works.

Preferred entry point is `uv run triage` (see pyproject.toml).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `src/` importable when the script is run directly.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from incident_triage.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
