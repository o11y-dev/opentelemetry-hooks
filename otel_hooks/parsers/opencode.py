"""Opencode hook parsing; independent of the telemetry runtime."""
from .base import BaseParser


class OpenCodeEventAdapter(BaseParser):
    provider = "opencode"

    def _normalize_provider_fields(self, event_name: str, data: dict) -> None:
        if data.get("tool_id") and not data.get("tool_use_id"):
            data["tool_use_id"] = data["tool_id"]
        super()._normalize_provider_fields(event_name, data)
