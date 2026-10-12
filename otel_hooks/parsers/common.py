"""Common hook parsing; independent of the telemetry runtime."""
from typing import Optional
from typing import Tuple
import os
import json
import re
import hashlib
from .events import (
    _INPUT_ALIASES,
    _MCP_EVENTS,
)


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


_HOME_RE = re.compile(r"/Users/[^/\s]+")

_WINDOWS_HOME_RE = re.compile(r"([A-Za-z]:[\\/]Users[\\/])[^\\/\r\n]+", re.IGNORECASE)


def _safe_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _stringify(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True)
    return str(value)


def _first_present(data: dict, keys: tuple):
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _normalize_input_data(data: dict) -> dict:
    """Add snake_case aliases for compatible hook payloads when needed."""
    normalized = None
    for source_key, target_key in _INPUT_ALIASES.items():
        if source_key in data and target_key not in data:
            if normalized is None:
                normalized = dict(data)
            normalized[target_key] = data[source_key]
    return normalized or data


def _bound_tool_response_payload(data: dict) -> None:
    """Keep buffered tool responses small while preserving exact size evidence."""
    value = data.get("tool_response")
    if value is None:
        return

    text = _stringify(value)
    data["tool_response_length"] = len(text)
    data["tool_response_sha256"] = _hash_text(text)

    # Provider aliases are normalized before this boundary. Keeping both copies
    # defeats the payload bound for camelCase callbacks.
    data.pop("toolResponse", None)

    max_chars = _text_max_chars()
    if len(text) <= max_chars:
        return

    data["tool_response_truncated"] = True
    retain_preview = _safe_bool(os.getenv("IDE_OTEL_CAPTURE_TOOL_INPUT_CONTENT", "")) or _safe_bool(
        os.getenv("IDE_OTEL_MCP_LOG_PAYLOAD", "true")
    )
    if retain_preview and max_chars > 0:
        data["tool_response"] = text[:max_chars]
    else:
        data.pop("tool_response", None)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mask_text(text: str) -> str:
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _TOKEN_RE.sub("[REDACTED_TOKEN]", text)
    text = _HOME_RE.sub("/Users/[REDACTED]", text)
    text = _WINDOWS_HOME_RE.sub(lambda match: match[1] + "[REDACTED]", text)
    return text


def _conversation_content_enabled() -> bool:
    """Return whether raw conversation/delegation content was explicitly enabled."""
    return _safe_bool(os.getenv("IDE_OTEL_CAPTURE_CONVERSATION_CONTENT", "")) or _safe_bool(
        os.getenv("IDE_OTEL_CAPTURE_TEXT", "")
    )


def _text_max_chars() -> int:
    try:
        return max(0, int(os.getenv("IDE_OTEL_TEXT_MAX_CHARS", "4000")))
    except (TypeError, ValueError):
        return 4000


def _session_key(data: dict) -> Optional[str]:
    """Extract session key: conversation_id (Cursor) or session_id (Copilot)."""
    for key in ("session_id", "conversation_id"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _generation_key_from_data(data: dict) -> Optional[str]:
    """Extract generation key from Cursor-specific fields."""
    val = data.get("generation_id")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _parse_encoded_mcp_tool_name(tool_name: object) -> Optional[Tuple[str, str]]:
    """Parse ``mcp__<server>__<tool>`` while preserving ``__`` inside the tool."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return None
    parts = tool_name.split("__", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def _normalize_mcp_event_data(data: dict, event_name: str, ide: Optional[str] = None) -> dict:
    """Add a provider-neutral MCP identity without changing the original tool name."""
    tool_name = data.get("tool_name")
    parsed = _parse_encoded_mcp_tool_name(tool_name)
    server = _first_present(data, ("mcp_server_name", "mcp_server"))
    tool = data.get("mcp_tool")

    if parsed:
        server = server or parsed[0]
        tool = tool or parsed[1]
    elif ide == "cursor" and isinstance(tool_name, str) and tool_name.startswith("MCP:"):
        tool = tool or tool_name.removeprefix("MCP:")
    elif event_name in _MCP_EVENTS:
        tool = tool or tool_name

    # ``command`` is a legacy fallback for dedicated MCP events.  A real
    # server field always wins, so executable paths never overwrite it.
    if not server and event_name in _MCP_EVENTS:
        server = data.get("command")

    updates = {}
    if server and data.get("mcp_server") != server:
        updates["mcp_server"] = server
    if tool and data.get("mcp_tool") != tool:
        updates["mcp_tool"] = tool
    if ide == "cursor" and event_name == "AfterMCPExecution" and data.get("duration_ms") is None:
        # Cursor's hook wire contract reports duration in milliseconds.
        duration = data.get("duration")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            updates["duration_ms"] = float(duration)
    if not updates:
        return data
    normalized = dict(data)
    normalized.update(updates)
    return normalized
