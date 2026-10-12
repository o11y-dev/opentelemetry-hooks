"""Antigravity hook parsing; independent of the telemetry runtime."""
from .events import _CANONICAL_EVENT
from .base import BaseParser


class AntigravityEventAdapter(BaseParser):
    provider = "antigravity"
    # The compatibility runner has its own historic event contract.
    event_aliases = {
        key: value for key, value in _CANONICAL_EVENT.items() if key.startswith(("Before", "After"))
    }
    response_fields = ("response", "output", "last_assistant_message")
