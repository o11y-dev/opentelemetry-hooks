"""Cursor hook parsing; independent of the telemetry runtime."""
from .events import _CANONICAL_EVENT
from .base import BaseParser


class CursorEventAdapter(BaseParser):
    provider = "cursor"
    event_aliases = {
        key: value
        for key, value in _CANONICAL_EVENT.items()
        if key[:1].islower() and key not in {"userPromptSubmitted", "errorOccurred"}
    }
