"""Claude hook parsing; independent of the telemetry runtime."""
import os
from .common import (
    _safe_bool,
    _stringify,
    _hash_text,
    _mask_text,
    _text_max_chars,
)
from .base import BaseParser


class ClaudeEventAdapter(BaseParser):
    provider = "claude"
    response_fields = ("last_assistant_message", "response", "assistant_response")

    def _normalize_provider_fields(self, event_name: str, data: dict) -> None:
        if event_name == "PostToolBatch":
            # Batch results duplicate individual tool callbacks. Retain only
            # identity and content facts unless tool-content capture is enabled.
            calls = []
            capture = _safe_bool(os.getenv("IDE_OTEL_CAPTURE_TOOL_INPUT_CONTENT", ""))
            for call in data.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                safe_call = {key: call[key] for key in ("tool_name", "tool_use_id") if key in call}
                for key in ("tool_input", "tool_response"):
                    if key not in call:
                        continue
                    text = _stringify(call[key])
                    safe_call[f"{key}_length"] = len(text)
                    safe_call[f"{key}_sha256"] = _hash_text(text)
                    if capture:
                        if _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")):
                            text = _mask_text(text)
                        safe_call[key] = text[:_text_max_chars()]
                calls.append(safe_call)
            data["tool_calls"] = calls
        if event_name == "StopFailure":
            data.setdefault("error_type", data.get("error"))
            data.setdefault("status", "error")
        super()._normalize_provider_fields(event_name, data)
