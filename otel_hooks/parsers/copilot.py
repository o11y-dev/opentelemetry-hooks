"""Copilot hook parsing; independent of the telemetry runtime."""
import json
from .events import (
    _CANONICAL_EVENT,
    _COPILOT_EVENTS,
)
from .base import BaseParser


class CopilotEventAdapter(BaseParser):
    provider = "copilot"
    event_aliases = {
        key: _CANONICAL_EVENT[key]
        for key in _COPILOT_EVENTS
    }

    def _normalize_provider_fields(self, event_name: str, data: dict) -> None:
        if "toolArgs" in data:
            args = data.pop("toolArgs")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (ValueError, TypeError):
                    pass
            data.setdefault("tool_input", args)
        result = data.pop("toolResult", data.pop("tool_result", None))
        if isinstance(result, dict):
            data.setdefault("tool_output", result.get("textResultForLlm", result.get("text_result_for_llm")))
        super()._normalize_provider_fields(event_name, data)
