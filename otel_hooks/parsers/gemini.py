"""Gemini hook parsing; independent of the telemetry runtime."""
from .base import BaseParser


class GeminiEventAdapter(BaseParser):
    provider = "gemini"
    event_aliases = {
        "BeforeAgent": "UserPromptSubmit", "AfterAgent": "Stop",
        "BeforeTool": "PreToolUse", "AfterTool": "PostToolUse",
        "PreCompress": "PreCompact",
    }
    response_fields = ("prompt_response", "response", "output", "last_assistant_message")

    def _normalize_provider_fields(self, event_name: str, data: dict) -> None:
        # Model hooks can fire many times per turn (AfterModel even per chunk).
        # Preserve their identities without opening/closing generations or
        # persisting nested model messages outside the content privacy contract.
        request = data.pop("llm_request", None)
        if isinstance(request, dict) and request.get("model"):
            data.setdefault("request_model", request["model"])
        data.pop("llm_response", None)
        if event_name == "Stop":
            data.pop("prompt", None)  # repeated original request, already captured at BeforeAgent
        if event_name == "PostToolUse" and isinstance(data.get("tool_response"), dict):
            if data["tool_response"].get("error"):
                data.setdefault("error", data["tool_response"]["error"])
                data.setdefault("status", "error")
        super()._normalize_provider_fields(event_name, data)
