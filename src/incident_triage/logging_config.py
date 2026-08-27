"""Minimal structured-logging shim.

The pipeline emits events like ``triage_started`` and ``triage_completed``
with a stable set of keys (``trace_id``, ``incident_id``, ``prompt_version``,
``model``). We format them as JSON so that in production a Cloud Logging
sink can parse them into structured payloads without any glue code, and in
development they still read as one line per event.

We deliberately avoid pulling in a heavyweight structlog dep. This is 40
lines and it does the job.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k in _STDLIB_KEYS or k.startswith("_"):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except TypeError:
                payload[k] = repr(v)
        return json.dumps(payload, default=str)


_STDLIB_KEYS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()) | {
    "message", "asctime"
}


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)


class _BoundLogger:
    """A logger adapter that stamps a fixed context onto every event."""

    def __init__(self, base: logging.Logger, context: dict[str, Any]) -> None:
        self._base = base
        self._context = context

    def _emit(self, level: int, event: str, extra: dict[str, Any] | None) -> None:
        merged = {**self._context, **(extra or {})}
        self._base.log(level, event, extra=merged)

    def info(self, event: str, extra: dict[str, Any] | None = None) -> None:
        self._emit(logging.INFO, event, extra)

    def warning(self, event: str, extra: dict[str, Any] | None = None) -> None:
        self._emit(logging.WARNING, event, extra)

    def error(self, event: str, extra: dict[str, Any] | None = None) -> None:
        self._emit(logging.ERROR, event, extra)


def bind_context(**ctx: Any) -> _BoundLogger:
    return _BoundLogger(logging.getLogger("incident_triage"), ctx)
