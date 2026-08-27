"""Pytest configuration.

Only job here is to make sure the src-layout package is importable when the
tests run through `uv run pytest` or plain `pytest` from the repo root.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
