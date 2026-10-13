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

    def _normalize_provider_fields(self, event_name: str, data: dict) -> None:
        if data.get("subagent_id") and not data.get("agent_id"):
            data["agent_id"] = data["subagent_id"]
        super()._normalize_provider_fields(event_name, data)
