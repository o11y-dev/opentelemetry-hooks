"""Agent, client-version and host identity without telemetry initialization."""
import os
import platform
import re
import subprocess
from typing import Optional

from .parsers.common import _first_present
from .parsers.events import _CODEX_EVENTS, _WINDSURF_EVENT_ALIASES


_CANONICAL_IDE_NAMES = {"cursor", "copilot", "claude", "antigravity", "opencode", "windsurf", "zed", "vscode", "gemini", "codex"}

_IDE_NAME_ALIASES = {
    "openai codex": "codex",
    "codex cli": "codex",
    "github copilot": "copilot",
    "github copilot chat": "copilot",
    "copilot chat": "copilot",
    "claude code": "claude",
    "anthropic claude code": "claude",
    "claude cli": "claude",
    "cursor ide": "cursor",
    "cursor cli": "cursor",
    "anti gravity": "antigravity",
    "windsurf ide": "windsurf",
    "codeium windsurf": "windsurf",
    "visual studio code": "vscode",
    "vs code": "vscode",
    "zed editor": "zed",
    "open code": "opencode",
    "gemini cli": "gemini",
    "google gemini": "gemini",
}

_IDE_NAME_NORM_PATTERN = re.compile(r"[-_\s]+")

_OS_INFO: Optional[dict] = None

_CODEX_VERSION_CACHE: Optional[str] = None  # cached result; None means not yet detected or not found

_CODEX_VERSION_DETECTED: bool = False  # True once detection has been attempted


def _get_os_info() -> dict:
    """Detect operating system, version, and architecture. Cached after first call."""
    global _OS_INFO
    if _OS_INFO is not None:
        return _OS_INFO
    sys_name = platform.system().lower()  # darwin, linux, windows
    os_type = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(sys_name, sys_name)
    os_name = platform.system()  # Darwin, Linux, Windows
    if os_type == "darwin":
        os_name = "macOS"
    os_version = platform.release()  # e.g. "25.3.0", "6.5.0-44-generic"
    arch = platform.machine()  # arm64, x86_64, aarch64
    _OS_INFO = {
        "os.type": os_type,
        "os.name": os_name,
        "os.version": os_version,
        "host.arch": arch,
    }
    return _OS_INFO


def _detect_client_version(data: dict, ide: str) -> Optional[str]:
    """Extract client/IDE version from environment variables or input payload."""
    global _CODEX_VERSION_CACHE, _CODEX_VERSION_DETECTED
    # Check input payload first
    version = _first_present(data, ("client_version", "ide_version", "app_version"))
    if version:
        return str(version)
    # IDE-specific env vars
    if ide == "claude":
        v = os.getenv("CLAUDE_CODE_VERSION")
        if v:
            return v
    if ide == "cursor":
        v = os.getenv("CURSOR_VERSION")
        if v:
            return v
    if ide == "copilot":
        v = os.getenv("COPILOT_VERSION")
        if v:
            return v
    if ide == "codex":
        v = os.getenv("CODEX_VERSION")
        if v:
            return v
        if not _CODEX_VERSION_DETECTED:
            _CODEX_VERSION_DETECTED = True
            try:
                result = subprocess.run(
                    ["codex", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if result.returncode == 0:
                    _CODEX_VERSION_CACHE = result.stdout.strip()
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
        if _CODEX_VERSION_CACHE:
            return _CODEX_VERSION_CACHE
    # Generic fallback
    v = os.getenv("IDE_OTEL_CLIENT_VERSION")
    if v:
        return v
    return None


def _normalize_ide_name(value: Optional[str]) -> Optional[str]:
    """Normalize IDE names to canonical identifiers using case-insensitive lookup."""
    if not isinstance(value, str):
        return None
    normalized = _IDE_NAME_NORM_PATTERN.sub(" ", value.strip().lower())
    if normalized in _CANONICAL_IDE_NAMES:
        return normalized

    alias = _IDE_NAME_ALIASES.get(normalized)
    if alias:
        return alias

    if normalized.endswith((" cli", " ide")):
        normalized = normalized.rsplit(" ", 1)[0]
        if normalized in _CANONICAL_IDE_NAMES:
            return normalized
        return _IDE_NAME_ALIASES.get(normalized)

    return None


def _get_event_name(data: dict, override: Optional[str] = None) -> str:
    """Extract raw event name from hook input."""
    if override:
        return override
    for key in ("hook_event_name", "hook_event_type", "event", "hook", "agent_action_name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if "prompt" in data:
        return "beforeSubmitPrompt"
    return "stop"


def _detect_payload_client_name(data: dict, include_session_fallback: bool = False) -> Optional[str]:
    """Infer client identity from payload fields only, without wrapper env hints."""
    reported = _normalize_ide_name(_first_present(data, ("ide_name", "ide", "client", "source_app")))
    if reported:
        return reported

    semantic_signals = [
        _normalize_ide_name(_first_present(data, ("gen_ai.client.name",))),
        _normalize_ide_name(_first_present(data, ("gen_ai.system",))),
        _normalize_ide_name(_first_present(data, ("service.name", "service_name"))),
    ]
    semantic_counts: dict[str, int] = {}
    for signal in semantic_signals:
        if signal:
            semantic_counts[signal] = semantic_counts.get(signal, 0) + 1
    corroborated = [name for name, count in semantic_counts.items() if count >= 2]
    if len(corroborated) == 1:
        return corroborated[0]

    # Native Cascade IDs become session_id/generation_id in the adapter. Keep
    # that provenance ahead of Cursor's generation_id heuristic.
    if isinstance(data.get("agent_action_name"), str) and data["agent_action_name"] in _WINDSURF_EVENT_ALIASES and data.get("trajectory_id"):
        return "windsurf"

    if data.get("conversation_id") or data.get("generation_id"):
        return "cursor"

    if data.get("transcript_path") or data.get("permission_mode") or data.get("notification_type"):
        return "claude"

    if data.get("turn_id") or data.get("last_assistant_message") is not None or data.get("tool_response") is not None:
        return "codex"

    raw_event = _first_present(data, ("hook_event_name", "hook_event_type", "event"))
    if isinstance(raw_event, str) and raw_event and raw_event[0].isupper():
        return "claude"

    cursor_indicators = ("composer_mode", "agent_type", "cwd", "workspace", "workspace_path")
    if any(data.get(key) for key in cursor_indicators):
        return "cursor"

    if include_session_fallback and data.get("session_id"):
        return "copilot"

    return None


def _detect_agent_engine(data: dict) -> Optional[str]:
    """Detect an inner agent engine without consulting IDE_OTEL_IDE_NAME."""
    explicit = _normalize_ide_name(_first_present(
        data,
        (
            "agent_engine",
            "agentEngine",
            "engine",
            "engine_name",
        ),
    ))
    if explicit:
        return explicit

    payload_client = _detect_payload_client_name(data, include_session_fallback=False)
    if payload_client:
        return payload_client

    if os.getenv("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"

    return None


def _has_strong_engine_signal(data: dict, resolved_engine: str) -> bool:
    """True when payload has high-confidence evidence for the resolved engine."""
    explicit = _normalize_ide_name(_first_present(
        data,
        ("agent_engine", "agentEngine", "engine", "engine_name"),
    ))
    if explicit == resolved_engine:
        return True
    reported = _normalize_ide_name(_first_present(data, ("ide_name", "ide", "client", "source_app")))
    if reported == resolved_engine:
        return True
    semantic_signals = [
        _normalize_ide_name(_first_present(data, ("gen_ai.client.name",))),
        _normalize_ide_name(_first_present(data, ("gen_ai.system",))),
        _normalize_ide_name(_first_present(data, ("service.name", "service_name"))),
    ]
    if sum(1 for signal in semantic_signals if signal == resolved_engine) >= 2:
        return True
    if resolved_engine == "cursor" and (data.get("conversation_id") or data.get("generation_id")):
        return True
    if resolved_engine == "windsurf" and isinstance(data.get("agent_action_name"), str) and data["agent_action_name"] in _WINDSURF_EVENT_ALIASES and data.get("trajectory_id"):
        return True
    if resolved_engine == "codex" and (
        data.get("turn_id")
        or data.get("last_assistant_message") is not None
        or data.get("tool_response") is not None
        or (data.get("_hook_original_event") or _get_event_name(data)) in _CODEX_EVENTS
    ):
        return True
    return False


def _resolved_agent_engine(
    data: Optional[dict] = None,
    session_ctx: Optional[dict] = None,
    ide: Optional[str] = None,
) -> Optional[str]:
    payload = data or {}
    resolved = _detect_agent_engine(payload)
    if resolved and ide and resolved != ide and not _has_strong_engine_signal(payload, resolved):
        resolved = None
    if resolved:
        return resolved
    if ide and _has_strong_engine_signal(payload, ide):
        return None
    if session_ctx:
        session_engine = _normalize_ide_name(session_ctx.get("agent_engine"))
        if session_engine and session_ctx.get("agent_engine_confirmed", True):
            return session_engine
    return None


def _resolve_client_name(
    ide: str,
    data: Optional[dict] = None,
    agent_engine: Optional[str] = None,
    session_ctx: Optional[dict] = None,
) -> str:
    resolved_engine = agent_engine if agent_engine is not None else _resolved_agent_engine(data, session_ctx, ide=ide)
    if resolved_engine and resolved_engine != ide:
        return resolved_engine
    return ide


def _set_client_identity_attributes(
    span, ide: str, data: Optional[dict] = None, agent_engine: Optional[str] = None, session_ctx: Optional[dict] = None,
) -> Optional[str]:
    """Attach canonical client identity and preserve wrapper identity when nested.

    Returns the resolved inner engine when one is detected and differs from the
    outer IDE; otherwise returns ``None``.
    """
    resolved_engine = agent_engine if agent_engine is not None else _resolved_agent_engine(data, session_ctx, ide=ide)
    client_name = _resolve_client_name(ide, data=data, agent_engine=resolved_engine, session_ctx=session_ctx)
    span.set_attribute("gen_ai.client.name", client_name)
    if resolved_engine and resolved_engine != ide:
        span.set_attribute("gen_ai.client.wrapper", ide)
        span.set_attribute("gen_ai.client.agent_engine", resolved_engine)
        return resolved_engine
    return None
