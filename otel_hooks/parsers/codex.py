"""Codex hook parsing; independent of the telemetry runtime."""
from .base import BaseParser


class CodexEventAdapter(BaseParser):
    provider = "codex"

    def _normalize_provider_fields(self, event_name: str, data: dict) -> None:
        if event_name == "Interrupt":
            data["is_interrupt"] = True
            data.setdefault("status", "interrupted")
        super()._normalize_provider_fields(event_name, data)
