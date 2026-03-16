#!/usr/bin/env python3
"""IDE Agent OpenTelemetry Hook — pure OpenTelemetry SDK.

Captures hook events from Cursor IDE, GitHub Copilot, Claude Code, and
compatible hook runners as OpenTelemetry spans and logs using GenAI
semantic conventions.

Supports:
- Multi-IDE detection (Cursor, GitHub Copilot, Claude Code, Antigravity)
- Session-level trace hierarchy (session -> generation -> events)
- Structured OTel Logs for MCP, shell, and tool events (trace-correlated)
- Cross-process trace context via file-based state
- Generation-based batching with flush on Stop
- Privacy masking and opt-in text capture
- Any OTLP-compatible backend

Usage:
    echo '{"hook_event_name":"sessionStart","session_id":"abc"}' | python3 otel_hook.py
"""
import glob
import contextlib
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Bootstrap: auto-provision .venv and add its site-packages to sys.path.
# Works with any python3 — Cursor's system Python, Homebrew, pyenv, etc.
# First run: venv + pip install happens in background; tracing activates next
# invocation.
# ---------------------------------------------------------------------------


def _resolve_hook_home() -> str:
    """Return the writable directory used for hook state, config, and the bootstrap venv.

    Resolution order:
    1. ``IDE_OTEL_HOOK_HOME`` environment variable (explicit override).
    2. ``$XDG_DATA_HOME/opentelemetry-hooks`` (defaults to
       ``~/.local/share/opentelemetry-hooks`` when ``XDG_DATA_HOME`` is unset)
       when the hook is running from an installed package — i.e. ``__file__``
       lives inside a *site-packages* directory.
    3. The directory that contains ``__file__`` — legacy behaviour for a
       source-checkout or a directly-copied script.
    """
    explicit = os.environ.get("IDE_OTEL_HOOK_HOME", "").strip()
    if explicit:
        return os.path.abspath(explicit)

    # Detect installed-package mode by comparing __file__ against the known
    # site-packages directories reported by sysconfig / site.
    this_file = os.path.abspath(__file__)
    in_site_packages = False
    try:
        import sysconfig
        purelib = sysconfig.get_path("purelib") or ""
        platlib = sysconfig.get_path("platlib") or ""
        for sp in (purelib, platlib):
            if sp and this_file.startswith(os.path.abspath(sp) + os.sep):
                in_site_packages = True
                break
    except Exception:
        pass
    if not in_site_packages:
        try:
            import site
            for sp in (site.getsitepackages() if hasattr(site, "getsitepackages") else []):
                if sp and this_file.startswith(os.path.abspath(sp) + os.sep):
                    in_site_packages = True
                    break
        except Exception:
            pass

    if in_site_packages:
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        if not xdg_data:
            xdg_data = os.path.join(os.path.expanduser("~"), ".local", "share")
        return os.path.join(xdg_data, "opentelemetry-hooks")

    return os.path.dirname(this_file)


_HOOK_DIR = _resolve_hook_home()
_VENV_DIR = os.path.join(_HOOK_DIR, ".venv")
_SETUP_LOCK = os.path.join(_HOOK_DIR, ".state", "setup.lock")


def _auto_provision_venv() -> None:
    """Create .venv and install opentelemetry-sdk + exporter in the background if missing."""
    venv_python = os.path.join(_VENV_DIR, "bin", "python")
    if os.path.isfile(venv_python):
        return
    lock_dir = os.path.dirname(_SETUP_LOCK)
    os.makedirs(lock_dir, exist_ok=True)
    if os.path.exists(_SETUP_LOCK):
        return
    try:
        with open(_SETUP_LOCK, "w") as f:
            f.write(str(os.getpid()))
        setup_script = (
            f'{sys.executable} -m venv "{_VENV_DIR}" && '
            f'"{_VENV_DIR}/bin/pip" install --quiet --upgrade pip && '
            f'"{_VENV_DIR}/bin/pip" install --quiet '
            f'opentelemetry-sdk '
            f'opentelemetry-exporter-otlp-proto-grpc '
            f'opentelemetry-exporter-otlp-proto-http && '
            f'rm -f "{_SETUP_LOCK}"'
        )
        subprocess.Popen(
            setup_script, shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        try:
            os.remove(_SETUP_LOCK)
        except OSError:
            pass


_auto_provision_venv()

_VENV_SP = glob.glob(os.path.join(_VENV_DIR, "lib", "python*", "site-packages"))
for _sp in _VENV_SP:
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

try:
    from opentelemetry import trace
    from opentelemetry.trace import (
        NonRecordingSpan,
        SpanContext,
        SpanKind,
        Status,
        StatusCode,
        TraceFlags,
        TraceState,
        use_span,
    )
except Exception:
    trace = None
    NonRecordingSpan = None
    SpanContext = None
    SpanKind = None
    Status = None
    StatusCode = None
    TraceFlags = None
    TraceState = None
    use_span = None

try:
    from opentelemetry.sdk.trace.export import SpanExportResult
except ImportError:
    class SpanExportResult:  # minimal shim for SDK-unavailable environments
        SUCCESS = 0
        FAILURE = 1


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_TRACING_INITIALIZED = False
_LOGS_INITIALIZED = False
_OTEL_LOG_HANDLER = None  # OTel LoggingHandler for OTLP log export
_LOGGER = logging.getLogger("otel_hook")
_CONFIG_DEFAULT = os.path.join(_HOOK_DIR, "otel_config.json")
_STATE_DIR = os.path.join(_HOOK_DIR, ".state")
_SESSION_DIR = os.path.join(_STATE_DIR, "sessions")
_BATCH_DIR = os.path.join(_STATE_DIR, "batches")
_LOCAL_SPANS_DIR = os.path.join(_STATE_DIR, "local_spans")
_LOCAL_TRACE_DIR = _LOCAL_SPANS_DIR  # backward-compatible alias
_LOCK_DIR = os.path.join(_STATE_DIR, "locks")
_CLEANUP_MARKER = os.path.join(_STATE_DIR, "last_cleanup")

# MDM (Managed Device Management) configuration
_MDM_DOMAIN = "dev.o11y.opentelemetry-hook"  # macOS managed preferences domain
_MDM_REGISTRY_PATH = r"SOFTWARE\Policies\OpenTelemetryHook"  # Windows registry path

# Privacy patterns
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")
_HOME_RE = re.compile(r"/Users/[^/\s]+")


# ---------------------------------------------------------------------------
# Event name canonicalization (all IDE variants -> PascalCase)
# ---------------------------------------------------------------------------
_CANONICAL_EVENT = {
    # Cursor camelCase
    "sessionStart": "SessionStart",
    "sessionEnd": "SessionEnd",
    "beforeSubmitPrompt": "UserPromptSubmit",
    "preToolUse": "PreToolUse",
    "postToolUse": "PostToolUse",
    "postToolUseFailure": "PostToolUseFailure",
    "beforeShellExecution": "BeforeShellExecution",
    "afterShellExecution": "AfterShellExecution",
    "beforeMCPExecution": "BeforeMCPExecution",
    "afterMCPExecution": "AfterMCPExecution",
    "beforeReadFile": "BeforeReadFile",
    "afterFileEdit": "AfterFileEdit",
    "subagentStart": "SubagentStart",
    "subagentStop": "SubagentStop",
    "stop": "Stop",
    # Copilot camelCase
    "userPromptSubmitted": "UserPromptSubmit",
    "errorOccurred": "ErrorOccurred",
}

# Common camelCase -> snake_case aliases used by compatible hook runners.
# Claude Code's documented hook payloads are already snake_case, but generic
# runners and workflow adapters that forward Claude- or Antigravity-style
# events may supply camelCase fields instead.
_INPUT_ALIASES = {
    "sessionId": "session_id",
    "conversationId": "conversation_id",
    "generationId": "generation_id",
    "transcriptPath": "transcript_path",
    "providerName": "provider_name",
    "requestModel": "request_model",
    "responseModel": "response_model",
    "modelName": "model_name",
    "toolName": "tool_name",
    "toolInput": "tool_input",
    "toolOutput": "tool_output",
    "toolType": "tool_type",
    "toolDefinitions": "tool_definitions",
    "toolUseId": "tool_use_id",
    "toolId": "tool_id",
    "agentId": "agent_id",
    "agentName": "agent_name",
    "agentVersion": "agent_version",
    "agentDescription": "agent_description",
    "agentType": "agent_type",
    "subagentType": "subagent_type",
    "responseFormat": "response_format",
    "outputType": "output_type",
    "choiceCount": "choice_count",
    "systemInstructions": "system_instructions",
    "systemPrompt": "system_prompt",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "workspacePath": "workspace_path",
    "filePath": "file_path",
    "exitCode": "exit_code",
    "durationMs": "duration_ms",
    "loopCount": "loop_count",
    "stopHookActive": "stop_hook_active",
    "isInterrupt": "is_interrupt",
    "hookEventType": "hook_event_type",
}

# Canonical ide.name values accepted directly from IDE_OTEL_IDE_NAME or
# self-reported payload metadata before alias fallback.
_CANONICAL_IDE_NAMES = {"cursor", "copilot", "claude", "antigravity", "opencode"}
_IDE_NAME_ALIASES = {
    "github copilot": "copilot",
    "github copilot chat": "copilot",
    "copilot chat": "copilot",
    "claude code": "claude",
    "anthropic claude code": "claude",
    "cursor ide": "cursor",
    "cursor cli": "cursor",
    "anti gravity": "antigravity",
    "open code": "opencode",
}

# Session boundary events
_SESSION_START_EVENTS = {"SessionStart"}
_SESSION_END_EVENTS = {"SessionEnd"}
_GENERATION_START_EVENTS = {"UserPromptSubmit"}
_GENERATION_END_EVENTS = {"Stop"}

# GenAI operation mapping (canonical PascalCase)
_OP_TOOL_EVENTS = {
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "BeforeShellExecution", "AfterShellExecution",
    "BeforeMCPExecution", "AfterMCPExecution",
    "BeforeReadFile", "AfterFileEdit",
}
_OP_AGENT_EVENTS = {
    "SessionStart", "SessionEnd",
    "SubagentStart", "SubagentStop",
}

# Per-event attribute extraction map (canonical names)
_EVENT_ATTR_MAP = {
    # Common
    "UserPromptSubmit": {
        "prompt": "ide.prompt", "composer_mode": "ide.composer_mode",
        "model": "gen_ai.request.model",
    },
    "SessionStart": {
        "source": "ide.session_source", "composer_mode": "ide.composer_mode",
        "model": "gen_ai.request.model", "agent_type": "ide.agent_type",
    },
    "SessionEnd": {"status": "ide.status", "reason": "ide.session_end_reason"},
    "PreToolUse": {"tool_name": "ide.tool_name", "tool_id": "ide.tool_id", "tool_use_id": "ide.tool_use_id"},
    "PostToolUse": {"tool_name": "ide.tool_name", "tool_id": "ide.tool_id", "tool_use_id": "ide.tool_use_id", "duration_ms": "ide.duration_ms"},
    "PostToolUseFailure": {"tool_name": "ide.tool_name", "tool_id": "ide.tool_id", "tool_use_id": "ide.tool_use_id", "error": "ide.error", "is_interrupt": "ide.is_interrupt"},
    "SubagentStart": {"subagent_type": "ide.subagent_type", "subagent_task": "ide.subagent_task", "agent_id": "ide.agent_id", "agent_type": "ide.agent_type"},
    "SubagentStop": {"subagent_type": "ide.subagent_type", "subagent_task": "ide.subagent_task", "status": "ide.status", "agent_id": "ide.agent_id", "agent_type": "ide.agent_type"},
    "Stop": {"status": "ide.status", "loop_count": "ide.loop_count", "stop_hook_active": "ide.stop_hook_active"},
    # Cursor-specific
    "BeforeShellExecution": {"command": "ide.command", "cwd": "ide.cwd"},
    "AfterShellExecution": {"command": "ide.command", "cwd": "ide.cwd", "exit_code": "ide.exit_code", "duration_ms": "ide.duration_ms"},
    "BeforeMCPExecution": {"mcp_server": "ide.mcp_server", "command": "ide.mcp_server", "mcp_tool": "ide.mcp_tool", "tool_name": "ide.mcp_tool"},
    "AfterMCPExecution": {"mcp_server": "ide.mcp_server", "command": "ide.mcp_server", "mcp_tool": "ide.mcp_tool", "tool_name": "ide.mcp_tool", "duration_ms": "ide.duration_ms", "duration": "ide.duration_ms"},
    "BeforeReadFile": {"file_path": "ide.file_path"},
    "AfterFileEdit": {"file_path": "ide.file_path", "edits": "ide.edits"},
    # Copilot-specific
    "ErrorOccurred": {"error": "ide.error", "is_interrupt": "ide.is_interrupt"},
}


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------
def _load_input() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _safe_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _stringify(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True)
    return str(value)


def _set_if_present(span, attr: str, value) -> None:
    if value is not None:
        span.set_attribute(attr, value)


def _first_present(data: dict, keys: tuple):
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _lower_or_none(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    return lowered or None


def _normalize_genai_output_type(value) -> Optional[str]:
    normalized = _lower_or_none(value)
    if normalized in {"json_object", "json_schema"}:
        return "json"
    if normalized in {"text", "json", "image", "speech"}:
        return normalized
    return None


def _infer_genai_provider(data: dict) -> Optional[str]:
    explicit = _lower_or_none(_first_present(data, ("provider_name", "provider", "model_provider", "vendor")))
    if explicit is not None:
        if explicit == "xai":
            return "x_ai"
        return explicit

    model = _lower_or_none(_first_present(data, ("response_model", "request_model", "model", "model_name")))
    if not model:
        return None
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "text-embedding", "dall-e", "whisper")):
        return "openai"
    if model.startswith("gemini"):
        return "gcp.gemini"
    if model.startswith("mistral"):
        return "mistral_ai"
    if model.startswith("deepseek"):
        return "deepseek"
    if model.startswith("command") or model.startswith(("embed-", "rerank-")):
        return "cohere"
    if model.startswith("grok"):
        return "x_ai"
    if model.startswith("groq"):
        return "groq"
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


def _normalize_ide_name(value: Optional[str]) -> Optional[str]:
    """Normalize IDE names to canonical identifiers using case-insensitive lookup."""
    if not isinstance(value, str):
        return None

    def _resolve_candidate(candidate: str) -> Optional[str]:
        if candidate in _CANONICAL_IDE_NAMES:
            return candidate
        return _IDE_NAME_ALIASES.get(candidate)

    normalized = re.sub(r"[-_\s]+", " ", value.strip().lower())
    resolved = _resolve_candidate(normalized)
    if resolved:
        return resolved

    if normalized.endswith((" cli", " ide")):
        return _resolve_candidate(normalized.rsplit(" ", 1)[0])

    return None


# ---------------------------------------------------------------------------
# State helpers (atomic writes + cleanup)
# ---------------------------------------------------------------------------
def _state_ttl_seconds() -> int:
    try:
        return int(os.getenv("IDE_OTEL_STATE_TTL_SECONDS", "86400"))
    except (TypeError, ValueError):
        return 86400


def _state_cleanup_interval_seconds() -> int:
    try:
        return int(os.getenv("IDE_OTEL_STATE_CLEANUP_INTERVAL_SECONDS", "3600"))
    except (TypeError, ValueError):
        return 3600


def _state_lock_timeout_seconds() -> float:
    try:
        return float(os.getenv("IDE_OTEL_STATE_LOCK_TIMEOUT_SECONDS", "2"))
    except (TypeError, ValueError):
        return 2.0


@contextlib.contextmanager
def _acquire_lock(lock_path: str):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    timeout = _state_lock_timeout_seconds()
    start = time.time()
    fd = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_path)
                if age > max(5.0, timeout * 5):
                    os.remove(lock_path)
            except OSError:
                pass
            if time.time() - start > timeout:
                break
            time.sleep(0.01)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.remove(lock_path)
            except OSError:
                pass


def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    os.replace(tmp_path, path)


def _cleanup_state() -> None:
    ttl = _state_ttl_seconds()
    if ttl <= 0:
        return
    interval = _state_cleanup_interval_seconds()
    now = time.time()
    try:
        if os.path.exists(_CLEANUP_MARKER):
            age = now - os.path.getmtime(_CLEANUP_MARKER)
            if age < interval:
                return
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_CLEANUP_MARKER, "w", encoding="utf-8") as fh:
            fh.write(str(now))
    except OSError:
        return

    cutoff = now - ttl
    for directory in (_SESSION_DIR, _BATCH_DIR):
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                continue


def _flush_stale_sessions(tracer) -> None:
    """Emit ``ide.session`` root spans for stale sessions that were never closed.

    When an IDE crashes or fails to send ``SessionEnd``, the session context
    file lingers on disk.  This function finds sessions older than the
    configured TTL and emits the missing root span before removing them, so
    that the trace tree remains complete.
    """
    ttl = _state_ttl_seconds()
    if ttl <= 0:
        return
    if not os.path.isdir(_SESSION_DIR):
        return

    cutoff = time.time() - ttl
    for name in os.listdir(_SESSION_DIR):
        path = os.path.join(_SESSION_DIR, name)
        try:
            if not os.path.isfile(path) or os.path.getmtime(path) >= cutoff:
                continue
            with open(path, "r", encoding="utf-8") as fh:
                ctx = json.load(fh)
            if not ctx:
                os.remove(path)
                continue
            session_key = name.removesuffix(".json")
            ide = ctx.get("ide", "unknown")
            _flush_session(tracer, session_key, ctx, ide)
            os.remove(path)
            _LOGGER.info("Flushed stale session %s", session_key)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Logging — JSON structured format with trace context & extra attributes
# ---------------------------------------------------------------------------

# Standard Python LogRecord fields to exclude from the JSON "attributes" bucket
_LOG_RESERVED_ATTRS = frozenset({
    "args", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg",
    "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "taskName", "thread", "threadName",
    # Our own top-level keys (already emitted explicitly)
    "trace_id", "span_id",
})


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object.

    Schema::

        {
          "timestamp": "2026-02-10T09:56:42.546000+00:00",
          "level": "INFO",
          "logger": "otel_hook.mcp",
          "message": "MCP call: gitlab-mcp/search_gitlab",
          "trace_id": "795f9117681e7f5c010a851ada5c300a",
          "span_id": "5b718186951f167f",
          "attributes": {              // all extra= fields
            "ide.mcp_server": "gitlab-mcp",
            "ide.mcp_tool": "search_gitlab",
            "ide.mcp.input": "{...}",
            ...
          }
        }
    """

    def format(self, record: logging.LogRecord) -> str:
        # Build the base envelope
        obj = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", "0"),
            "span_id": getattr(record, "span_id", "0"),
        }

        # Collect all extra attributes (anything not in the reserved set)
        attrs = {}
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _LOG_RESERVED_ATTRS:
                continue
            # Skip None values and internal callables
            if value is None or callable(value):
                continue
            try:
                # Ensure JSON-serializable
                json.dumps(value)
                attrs[key] = value
            except (TypeError, ValueError):
                attrs[key] = str(value)
        if attrs:
            obj["attributes"] = attrs

        # Include exception info if present
        if record.exc_info and record.exc_info[1] is not None:
            obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(obj, ensure_ascii=False, default=str)


def _resolve_log_level() -> int:
    for key in ("IDE_OTEL_LOG_LEVEL", "LOG_LEVEL", "LOGLEVEL"):
        value = os.getenv(key)
        if value:
            return getattr(logging, value.upper(), logging.WARNING)
    return logging.WARNING


def _attach_trace_context(record: logging.LogRecord) -> None:
    trace_id = "0"
    span_id = "0"
    if trace is not None:
        try:
            span = trace.get_current_span()
            if span is not None:
                ctx = span.get_span_context()
                if ctx is not None and ctx.is_valid:
                    trace_id = f"{ctx.trace_id:032x}"
                    span_id = f"{ctx.span_id:016x}"
        except Exception:
            pass
    record.trace_id = trace_id
    record.span_id = span_id


class _TraceContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "trace_id", None) or not getattr(record, "span_id", None):
            _attach_trace_context(record)
        return True


def _configure_logging() -> None:
    if _LOGGER.handlers:
        return
    level = _resolve_log_level()
    log_path = os.getenv(
        "IDE_OTEL_LOG_FILE",
        os.path.join(_HOOK_DIR, "otel_hook.log"),
    )
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        handler = logging.FileHandler(log_path)
        handler.setFormatter(_JsonFormatter())
        handler.addFilter(_TraceContextFilter())
        _LOGGER.addHandler(handler)
        _LOGGER.setLevel(level)
        _LOGGER.propagate = False
        _attach_otel_sdk_logging(handler, level)
    except Exception:
        pass


def _log_with_span(logger: logging.Logger, level: int, span, message: str, *args) -> None:
    """Log with explicit trace/span ids from a span object."""
    try:
        ctx = span.get_span_context() if span is not None else None
        if ctx is not None and ctx.is_valid:
            logger.log(
                level, message, *args,
                extra={
                    "trace_id": f"{ctx.trace_id:032x}",
                    "span_id": f"{ctx.span_id:016x}",
                },
            )
            return
    except Exception:
        pass
    logger.log(level, message, *args)


def _attach_otel_sdk_logging(handler: logging.Handler, level: int) -> None:
    """Route OTel SDK/exporter logs into the hook log file."""
    for name in (
        "opentelemetry",
        "opentelemetry.sdk",
        "opentelemetry.exporter",
        "opentelemetry.exporter.otlp",
    ):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        if handler not in logger.handlers:
            logger.addHandler(handler)
        logger.propagate = False


@contextlib.contextmanager
def _span_context(span):
    if use_span is None:
        yield
        return
    with use_span(span, end_on_exit=False):
        yield


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _load_mdm_config() -> dict:
    """Read managed configuration pushed by MDM (macOS or Windows).

    macOS: reads managed preferences plist files directly via ``plistlib``.
    Windows: reads string values from HKLM registry under *_MDM_REGISTRY_PATH*.

    Returns a dict of key/value pairs (may be empty).  Never raises.
    """
    if sys.platform == "darwin":
        return _load_mdm_config_macos()
    if sys.platform == "win32":
        return _load_mdm_config_windows()
    return {}


def _load_mdm_config_macos() -> dict:
    """Load managed preferences from macOS MDM profile."""
    try:
        import plistlib
        managed_path = f"/Library/Managed Preferences/{_MDM_DOMAIN}.plist"
        if os.path.exists(managed_path):
            with open(managed_path, "rb") as fh:
                return plistlib.load(fh) or {}
        # Fall back to current-user managed preferences
        user_managed = os.path.expanduser(
            f"~/Library/Managed Preferences/{_MDM_DOMAIN}.plist"
        )
        if os.path.exists(user_managed):
            with open(user_managed, "rb") as fh:
                return plistlib.load(fh) or {}
    except Exception:
        _LOGGER.debug("MDM: unable to read macOS managed preferences")
    return {}


def _load_mdm_config_windows() -> dict:
    """Load managed configuration from Windows registry (HKLM)."""
    try:
        import winreg
        result = {}
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, _MDM_REGISTRY_PATH) as key:
                    idx = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(key, idx)
                            if name and value is not None:
                                result[name] = str(value)
                            idx += 1
                        except OSError:
                            break
            except OSError:
                continue
        return result
    except ImportError:
        pass
    except Exception:
        _LOGGER.debug("MDM: unable to read Windows registry")
    return {}


def _find_example_config() -> str:
    """Return the path to ``otel_config.example.json``, or ``''`` if not found.

    Search order:
    1. Next to ``__file__`` (source checkout / directly-copied script).
    2. ``{sys.prefix}/share/opentelemetry-hooks/`` (pip-installed package).
    """
    # Source-checkout or script-copy layout.
    candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otel_config.example.json")
    if os.path.exists(candidate):
        return candidate

    # pip-installed layout: data-files land under {prefix}/share/opentelemetry-hooks/
    seen: set = set()
    for prefix in [sys.prefix, sys.exec_prefix]:
        if prefix in seen:
            continue
        seen.add(prefix)
        p = os.path.join(prefix, "share", "opentelemetry-hooks", "otel_config.example.json")
        if os.path.exists(p):
            return p

    return ""


def _load_config() -> dict:
    path = os.getenv("IDE_OTEL_CONFIG", _CONFIG_DEFAULT)
    if not os.path.isabs(path):
        path = os.path.join(_HOOK_DIR, path)
    if not os.path.exists(path):
        example = _find_example_config()
        if example:
            try:
                import shutil
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copy2(example, path)
            except OSError:
                pass
        if not os.path.exists(path):
            config = {}
        else:
            config = _load_json_config(path)
    else:
        config = _load_json_config(path)
    # MDM settings override JSON config (IT admin policy takes precedence)
    mdm = _load_mdm_config()
    if mdm:
        config.update(mdm)
    return config


def _load_json_config(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _headers_to_env(value: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in value.items() if v is not None)


def _coerce_env_value(key: str, value) -> str:
    if isinstance(value, dict) and key == "OTEL_EXPORTER_OTLP_HEADERS":
        return _headers_to_env(value)
    if isinstance(value, (bool, int, float)):
        return str(value)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _apply_config_env(config: dict) -> None:
    for key, value in config.items():
        if not key or value is None or key in os.environ or key.startswith("_"):
            continue
        os.environ[key] = _coerce_env_value(key, value)


def _parse_resource_attributes(value: str) -> dict:
    attrs = {}
    for pair in (value or "").split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k.strip():
            attrs[k.strip()] = v.strip()
    return attrs


def _parse_otlp_headers(value: str) -> dict:
    headers = {}
    for pair in (value or "").split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, val = pair.split("=", 1)
        key = key.strip()
        if key:
            headers[key] = urllib.parse.unquote(val.strip())
    return headers


# ---------------------------------------------------------------------------
# Tracing init — pure OpenTelemetry SDK
# ---------------------------------------------------------------------------
def _init_sdk_tracer_provider(resource_attrs: dict, disable_batch: bool) -> bool:
    """Configure the OTel SDK TracerProvider with OTLP exporter.

    When no OTLP endpoint is configured and local spans are enabled,
    creates a bare TracerProvider (no OTLP exporter) so the file exporter
    can be attached later without wasted network calls.
    """
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError as exc:
        _LOGGER.error("opentelemetry-sdk not importable: %s", exc)
        return False

    protocol = (os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL") or "grpc").lower()
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    headers = _parse_otlp_headers(os.getenv("OTEL_EXPORTER_OTLP_HEADERS", ""))

    if not endpoint and _local_spans_enabled():
        sdk_provider = SDKTracerProvider(resource=Resource.create(resource_attrs))
        trace.set_tracer_provider(sdk_provider)
        _LOGGER.info("SDK TracerProvider ready (file-only mode, no OTLP endpoint)")
        return True

    exporter = None
    if protocol == "grpc":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            insecure = _safe_bool(os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "true"))
            exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers, insecure=insecure)
        except ImportError as exc:
            _LOGGER.warning("gRPC exporter unavailable: %s — falling back to http/protobuf", exc)
            protocol = "http/protobuf"

    if exporter is None:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
        except ImportError as exc:
            _LOGGER.error("No OTLP exporter available: %s", exc)
            return False

    sdk_provider = SDKTracerProvider(resource=Resource.create(resource_attrs))
    if disable_batch:
        sdk_provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        sdk_provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(sdk_provider)
    _LOGGER.info("SDK TracerProvider ready (protocol=%s endpoint=%s)", protocol, endpoint)
    return True


def _derive_logs_endpoint() -> Optional[str]:
    """Derive the OTLP logs endpoint from config.

    Priority:
    1. Explicit ``OTEL_EXPORTER_OTLP_LOGS_ENDPOINT``
    2. Replace ``/v1/traces`` → ``/v1/logs`` in the traces endpoint
    3. Fall back to the base OTLP endpoint (gRPC uses same host for all signals)
    """
    explicit = os.getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
    if explicit:
        return explicit
    base = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if base.endswith("/v1/traces"):
        return base.rsplit("/v1/traces", 1)[0] + "/v1/logs"
    return base or None


def _init_sdk_logger_provider(resource_attrs: dict, disable_batch: bool) -> bool:
    """Configure the OTel SDK LoggerProvider with OTLP log exporter."""
    global _OTEL_LOG_HANDLER, _LOGS_INITIALIZED
    try:
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry._logs import set_logger_provider
    except ImportError as exc:
        _LOGGER.warning("OTel Logs SDK not importable: %s", exc)
        return False

    protocol = (os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL") or "grpc").lower()
    endpoint = _derive_logs_endpoint()
    headers = _parse_otlp_headers(os.getenv("OTEL_EXPORTER_OTLP_HEADERS", ""))

    exporter = None
    if protocol == "grpc":
        try:
            from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
            insecure = _safe_bool(os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "true"))
            kwargs = {"headers": headers, "insecure": insecure}
            if endpoint:
                kwargs["endpoint"] = endpoint
            exporter = OTLPLogExporter(**kwargs)
        except ImportError as exc:
            _LOGGER.warning("gRPC log exporter unavailable: %s — falling back to http/protobuf", exc)
            protocol = "http/protobuf"

    if exporter is None:
        try:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            kwargs = {"headers": headers}
            if endpoint:
                kwargs["endpoint"] = endpoint
            exporter = OTLPLogExporter(**kwargs)
        except ImportError as exc:
            _LOGGER.error("No OTLP log exporter available: %s", exc)
            return False

    resource = Resource.create(resource_attrs)
    logger_provider = LoggerProvider(resource=resource)
    if disable_batch:
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    else:
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(logger_provider)

    # Python logging → OTel log bridge handler
    _OTEL_LOG_HANDLER = LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider)
    _LOGS_INITIALIZED = True
    _LOGGER.info("SDK LoggerProvider ready (protocol=%s endpoint=%s)", protocol, endpoint)
    return True


def _enable_console_exporter() -> None:
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
    except ImportError as exc:
        _LOGGER.warning("Console exporter unavailable: %s", exc)
        return
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))


def _enable_console_log_exporter() -> None:
    """Add a console exporter to the LoggerProvider for debugging."""
    try:
        from opentelemetry.sdk._logs import LoggerProvider as SDKLoggerProvider
        from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
        from opentelemetry._logs import get_logger_provider
        # Use the non-deprecated name if available (OTel SDK >= 1.39)
        try:
            from opentelemetry.sdk._logs.export import ConsoleLogRecordExporter as ConsoleExporter
        except ImportError:
            from opentelemetry.sdk._logs.export import ConsoleLogExporter as ConsoleExporter
    except ImportError:
        return
    provider = get_logger_provider()
    if isinstance(provider, SDKLoggerProvider):
        provider.add_log_record_processor(SimpleLogRecordProcessor(ConsoleExporter()))


def _span_to_dict(span) -> dict:
    """Serialize an OTel ReadableSpan to a JSON-compatible dict."""
    ctx = span.context
    parent_id = None
    parent_ctx = span.parent
    if parent_ctx is not None and getattr(parent_ctx, "span_id", 0) != 0:
        parent_id = format(parent_ctx.span_id, "016x")
    return {
        "name": span.name,
        "trace_id": format(ctx.trace_id, "032x") if ctx else None,
        "span_id": format(ctx.span_id, "016x") if ctx else None,
        "parent_span_id": parent_id,
        "start_time_ns": span.start_time,
        "end_time_ns": span.end_time,
        "attributes": dict(span.attributes or {}),
        "status": span.status.status_code.name if span.status else None,
    }


class _FileSpanExporter:
    """OTel SpanExporter that appends spans as JSONL to a file."""

    def __init__(self, path: str) -> None:
        self._path = path
        lock_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.basename(path))
        self._lock_path = os.path.join(_LOCK_DIR, f"file_exporter_{lock_name}.lock")

    def export(self, spans):
        try:
            dir_path = os.path.dirname(self._path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            with _acquire_lock(self._lock_path):
                with open(self._path, "a", encoding="utf-8") as fh:
                    for span in spans:
                        fh.write(json.dumps(_span_to_dict(span), ensure_ascii=True, default=str) + "\n")
            return SpanExportResult.SUCCESS
        except OSError as exc:
            _LOGGER.debug("file span exporter write failed: %s", exc)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _enable_file_exporter(path: str) -> None:
    """Add a file span exporter to the TracerProvider for local span persistence."""
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except ImportError as exc:
        _LOGGER.warning("File exporter unavailable: %s", exc)
        return
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(SimpleSpanProcessor(_FileSpanExporter(path)))


def _force_flush_provider(timeout_millis: int = 5000) -> None:
    """Flush the SDK TracerProvider and LoggerProvider to push pending data."""
    try:
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=timeout_millis)
    except Exception as exc:
        _LOGGER.warning("trace force_flush failed: %s", exc)
    try:
        from opentelemetry._logs import get_logger_provider
        log_provider = get_logger_provider()
        if hasattr(log_provider, "force_flush"):
            log_provider.force_flush(timeout_millis=timeout_millis)
    except Exception as exc:
        _LOGGER.warning("log force_flush failed: %s", exc)


def _init_tracing(ide: str = "cursor") -> bool:
    global _TRACING_INITIALIZED
    if _TRACING_INITIALIZED:
        return True
    if trace is None:
        _LOGGER.error("opentelemetry-sdk not installed; tracing disabled.")
        return False

    service_name = os.getenv("IDE_OTEL_SERVICE_NAME")
    if service_name and not os.getenv("OTEL_SERVICE_NAME"):
        os.environ["OTEL_SERVICE_NAME"] = service_name

    app_name = os.getenv("IDE_OTEL_APP_NAME") or os.getenv("OTEL_SERVICE_NAME") or "ide-agent"
    resource_attrs = _parse_resource_attributes(os.getenv("OTEL_RESOURCE_ATTRIBUTES", ""))
    resource_attrs.setdefault("service.name", app_name)
    resource_attrs.setdefault("gen_ai.system", ide)

    resource_attrs_str = ",".join(f"{k}={v}" for k, v in resource_attrs.items())
    os.environ["OTEL_RESOURCE_ATTRIBUTES"] = resource_attrs_str
    _LOGGER.debug("Set OTEL_RESOURCE_ATTRIBUTES: %s", resource_attrs_str)

    disable_batch = _safe_bool(os.getenv("IDE_OTEL_DISABLE_BATCH", ""))

    if not _init_sdk_tracer_provider(resource_attrs, disable_batch):
        return False

    # Init OTel Logs (LoggerProvider + OTLP log exporter)
    if _safe_bool(os.getenv("IDE_OTEL_ENABLE_LOGS", "true")):
        if not _init_sdk_logger_provider(resource_attrs, disable_batch):
            _LOGGER.warning("OTel Logs init failed — continuing with traces only")

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL")
    service = os.getenv("OTEL_SERVICE_NAME")
    headers = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
    _LOGGER.info(
        "OTEL ready: endpoint=%s protocol=%s service=%s ide=%s headers_present=%s logs=%s",
        endpoint, protocol, service, ide, "yes" if headers else "no",
        "yes" if _LOGS_INITIALIZED else "no",
    )
    if not endpoint:
        _LOGGER.warning("OTEL_EXPORTER_OTLP_ENDPOINT not set — exports may fail")
    if _safe_bool(os.getenv("IDE_OTEL_DEBUG_CONSOLE", "")):
        _enable_console_exporter()
        if _LOGS_INITIALIZED:
            _enable_console_log_exporter()

    _TRACING_INITIALIZED = True
    return True


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------
def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mask_text(text: str) -> str:
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _TOKEN_RE.sub("[REDACTED_TOKEN]", text)
    text = _HOME_RE.sub("/Users/[REDACTED]", text)
    return text


def _maybe_attach_text(span, label: str, text: str) -> None:
    if not text:
        return
    span.set_attribute(f"ide.{label}.length", len(text))
    span.set_attribute(f"ide.{label}.sha256", _hash_text(text))
    if not _safe_bool(os.getenv("IDE_OTEL_CAPTURE_TEXT", "")):
        return
    max_chars = int(os.getenv("IDE_OTEL_TEXT_MAX_CHARS", "4000"))
    if _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")):
        text = _mask_text(text)
    span.set_attribute(f"ide.{label}.text", text[:max_chars])


# ---------------------------------------------------------------------------
# OTel Log emission (MCP, shell, tool events)
# ---------------------------------------------------------------------------
_MCP_EVENTS = {"BeforeMCPExecution", "AfterMCPExecution"}
_SHELL_EVENTS = {"BeforeShellExecution", "AfterShellExecution"}
_TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}


def _get_otel_logger(name: str) -> logging.Logger:
    """Get or create a Python logger with the OTel log bridge handler attached."""
    logger = logging.getLogger(f"otel_hook.{name}")
    if _OTEL_LOG_HANDLER is not None and _OTEL_LOG_HANDLER not in logger.handlers:
        logger.addHandler(_OTEL_LOG_HANDLER)
        logger.setLevel(logging.DEBUG)
    return logger


def _inject_trace_context(attrs: dict) -> tuple:
    """Inject trace_id and span_id from the current active span into attrs.

    This ensures trace context appears as explicit log attributes in the OTLP
    export — not just as OTel log record metadata — so backends like Coralogix
    surface them as searchable, first-class fields.

    Returns (trace_id, span_id) strings for embedding in log message bodies.
    """
    tid, sid = "0", "0"
    if trace is None:
        return tid, sid
    try:
        span = trace.get_current_span()
        if span is not None:
            ctx = span.get_span_context()
            if ctx is not None and ctx.is_valid:
                tid = f"{ctx.trace_id:032x}"
                sid = f"{ctx.span_id:016x}"
                attrs["trace_id"] = tid
                attrs["span_id"] = sid
    except Exception:
        pass
    return tid, sid


def _fmt_duration(duration) -> str:
    """Format duration for log messages, handling None gracefully."""
    if duration is None:
        return "n/a"
    return f"{duration}ms"


def _emit_mcp_log(event_name: str, data: dict) -> None:
    """Emit a structured OTel log record for MCP events with full I/O payload.

    Cursor sends MCP events with these field names:
    - ``command``    → MCP server name  (e.g. "gitlab-mcp", "atlassian")
    - ``tool_name``  → MCP tool name    (e.g. "get_merge_requests", "jira_search")
    - ``tool_input`` → input payload    (dict)
    - ``result_json``→ output payload   (JSON string)
    - ``duration``   → duration in ms   (float)
    """
    if not _LOGS_INITIALIZED:
        return

    logger = _get_otel_logger("mcp")
    # Cursor uses "command" for server, "tool_name" for tool; fall back to mcp_server/mcp_tool
    server = _first_present(data, ("mcp_server", "command")) or "unknown"
    tool = _first_present(data, ("mcp_tool", "tool_name")) or "unknown"

    attrs = {
        "ide.mcp_server": server,
        "ide.mcp_tool": tool,
        "ide.hook.event": event_name,
    }
    _tid, _sid = _inject_trace_context(attrs)

    # Capture input payload
    capture_payload = _safe_bool(os.getenv("IDE_OTEL_MCP_LOG_PAYLOAD", "true"))
    mask = _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", ""))
    max_chars = int(os.getenv("IDE_OTEL_TEXT_MAX_CHARS", "4000"))

    for key in ("mcp_input", "tool_input", "input"):
        value = data.get(key)
        if value is not None:
            text = _stringify(value)
            attrs["ide.mcp.input.length"] = len(text)
            attrs["ide.mcp.input.sha256"] = _hash_text(text)
            if capture_payload:
                if mask:
                    text = _mask_text(text)
                attrs["ide.mcp.input"] = text[:max_chars]
            break

    # Capture output payload — Cursor uses "result_json" (JSON string)
    for key in ("mcp_output", "result_json", "tool_output", "output", "tool_response"):
        value = data.get(key)
        if value is not None:
            text = _stringify(value)
            attrs["ide.mcp.output.length"] = len(text)
            attrs["ide.mcp.output.sha256"] = _hash_text(text)
            if capture_payload:
                if mask:
                    text = _mask_text(text)
                attrs["ide.mcp.output"] = text[:max_chars]
            break

    # Duration — Cursor uses "duration" (float ms), fallback to "duration_ms"
    duration = _first_present(data, ("duration_ms", "duration"))
    if duration is not None:
        attrs["ide.mcp.duration_ms"] = duration

    # Server stdout/stderr (if the IDE provides it)
    for stream in ("stdout", "stderr", "mcp_stdout", "mcp_stderr"):
        value = data.get(stream)
        if value:
            stream_name = stream.replace("mcp_", "")
            text = str(value)
            attrs[f"ide.mcp.{stream_name}.length"] = len(text)
            if capture_payload:
                if mask:
                    text = _mask_text(text)
                attrs[f"ide.mcp.{stream_name}"] = text[:max_chars]

    # Emit the log record — span_id in body for backend visibility
    if event_name == "BeforeMCPExecution":
        logger.info("[%s] MCP call: %s/%s", _sid, server, tool, extra=attrs)
    else:
        logger.info("[%s] MCP result: %s/%s duration=%s", _sid, server, tool, _fmt_duration(duration), extra=attrs)


def _emit_shell_log(event_name: str, data: dict) -> None:
    """Emit a structured OTel log record for shell execution events."""
    if not _LOGS_INITIALIZED:
        return

    logger = _get_otel_logger("shell")
    command = data.get("command") or "unknown"
    cwd = data.get("cwd") or ""

    attrs = {
        "ide.hook.event": event_name,
        "ide.command": command,
        "ide.cwd": cwd,
    }
    _tid, _sid = _inject_trace_context(attrs)

    exit_code = data.get("exit_code")
    if exit_code is not None:
        attrs["ide.exit_code"] = exit_code
    duration = data.get("duration_ms")
    if duration is not None:
        attrs["ide.duration_ms"] = duration

    # Capture stdout/stderr
    capture_payload = _safe_bool(os.getenv("IDE_OTEL_MCP_LOG_PAYLOAD", "true"))
    mask = _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", ""))
    max_chars = int(os.getenv("IDE_OTEL_TEXT_MAX_CHARS", "4000"))
    for stream in ("stdout", "stderr", "output"):
        value = data.get(stream)
        if value:
            text = str(value)
            stream_name = "stdout" if stream == "output" else stream
            attrs[f"ide.shell.{stream_name}.length"] = len(text)
            if capture_payload:
                if mask:
                    text = _mask_text(text)
                attrs[f"ide.shell.{stream_name}"] = text[:max_chars]

    if event_name == "BeforeShellExecution":
        logger.info("[%s] Shell exec: %s", _sid, command, extra=attrs)
    else:
        logger.info("[%s] Shell result: %s exit=%s duration=%s", _sid, command, exit_code, _fmt_duration(duration), extra=attrs)


def _emit_tool_log(event_name: str, data: dict) -> None:
    """Emit a structured OTel log record for tool use events."""
    if not _LOGS_INITIALIZED:
        return

    logger = _get_otel_logger("tool")
    tool_name = data.get("tool_name") or "unknown"

    attrs = {
        "ide.hook.event": event_name,
        "ide.tool_name": tool_name,
    }
    _tid, _sid = _inject_trace_context(attrs)

    for key in ("tool_id", "tool_use_id"):
        value = data.get(key)
        if value is not None:
            attrs[f"ide.{key}"] = value

    duration = data.get("duration_ms")
    if duration is not None:
        attrs["ide.duration_ms"] = duration

    error = data.get("error")
    if error is not None:
        attrs["ide.error"] = str(error)

    # Capture tool input/output (subject to privacy controls)
    capture_payload = _safe_bool(os.getenv("IDE_OTEL_MCP_LOG_PAYLOAD", "true"))
    mask = _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", ""))
    max_chars = int(os.getenv("IDE_OTEL_TEXT_MAX_CHARS", "4000"))
    for key in ("tool_input", "input"):
        value = data.get(key)
        if value is not None:
            text = _stringify(value)
            attrs["ide.tool.input.length"] = len(text)
            if capture_payload and _safe_bool(os.getenv("IDE_OTEL_CAPTURE_TOOL_INPUT_CONTENT", "")):
                if mask:
                    text = _mask_text(text)
                attrs["ide.tool.input"] = text[:max_chars]
            break

    if event_name == "PostToolUseFailure":
        logger.warning("[%s] Tool failed: %s error=%s", _sid, tool_name, error, extra=attrs)
    elif event_name == "PreToolUse":
        logger.info("[%s] Tool call: %s", _sid, tool_name, extra=attrs)
    else:
        logger.info("[%s] Tool result: %s duration=%s", _sid, tool_name, _fmt_duration(duration), extra=attrs)


def _emit_event_log(event_name: str, data: dict) -> None:
    """Emit OTel log records for hook events (dispatcher)."""
    if not _LOGS_INITIALIZED:
        return
    if event_name in _MCP_EVENTS:
        _emit_mcp_log(event_name, data)
    elif event_name in _SHELL_EVENTS:
        _emit_shell_log(event_name, data)
    elif event_name in _TOOL_EVENTS:
        _emit_tool_log(event_name, data)
    elif _safe_bool(os.getenv("IDE_OTEL_LOG_ALL_EVENTS", "")):
        logger = _get_otel_logger("events")
        all_attrs = {"ide.hook.event": event_name}
        _tid, _sid = _inject_trace_context(all_attrs)
        logger.info("[%s] Hook event: %s", _sid, event_name, extra=all_attrs)


# ---------------------------------------------------------------------------
# IDE detection and event normalization
# ---------------------------------------------------------------------------
def _detect_ide(data: dict) -> str:
    """Detect which IDE is calling this hook based on input fields.

    IDE_OTEL_IDE_NAME can be used to force the IDE name for hook systems that
    do not expose enough identifying fields.
    """
    # IDE_OTEL_IDE_NAME wins first, then self-reported IDE/client fields,
    # before falling back to payload-shape detection.
    override = _normalize_ide_name(
        os.getenv("IDE_OTEL_IDE_NAME")
        or _first_present(data, ("ide_name", "ide", "client", "source_app"))
    )
    if override:
        return override

    # Cursor-specific fields (check these first before session_id)
    if data.get("conversation_id") or data.get("generation_id"):
        return "cursor"

    # Cursor also sends these fields that Copilot doesn't
    cursor_indicators = (
        "composer_mode", "agent_type", "subagent_type",
        "cwd", "workspace", "workspace_path"
    )
    if any(data.get(key) for key in cursor_indicators):
        return "cursor"

    # Check workspace context — if .cursor/ directory exists, likely Cursor
    try:
        cwd = data.get("cwd") or os.getcwd()
        if ".cursor" in cwd or os.path.exists(os.path.join(cwd, ".cursor")):
            return "cursor"
    except Exception:
        pass

    # Claude Code hook payloads include transcript-specific metadata and
    # Claude-only hook context such as permission/notification fields.
    if data.get("transcript_path") or data.get("permission_mode") or data.get("notification_type"):
        return "claude"

    # Copilot only sends session_id without other indicators
    if data.get("session_id"):
        return "copilot"

    # Default to cursor (most common case)
    return "cursor"


def _get_event_name(data: dict) -> str:
    """Extract raw event name from hook input."""
    for key in ("hook_event_name", "hook_event_type", "event", "hook"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if "prompt" in data:
        return "beforeSubmitPrompt"
    return "stop"


def _normalize_event(event_name: str) -> str:
    """Normalize event name to canonical PascalCase."""
    return _CANONICAL_EVENT.get(event_name, event_name)


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


# ---------------------------------------------------------------------------
# Session context (cross-process, session-level trace linking)
# ---------------------------------------------------------------------------
def _session_path(session_key: str) -> str:
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_key)
    return os.path.join(_SESSION_DIR, f"{safe_key}.json")


def _create_session_context(session_key: str, data: dict, ide: str) -> dict:
    """Create and persist a new session context with pre-generated trace IDs."""
    os.makedirs(_SESSION_DIR, exist_ok=True)
    ctx = {
        "trace_id": f"{random.getrandbits(128):032x}",
        "phantom_parent_id": f"{random.getrandbits(64):016x}",
        "start_time_ns": time.time_ns(),
        "ide": ide,
        "generation_count": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    lock_path = os.path.join(_LOCK_DIR, f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', session_key)}.lock")
    with _acquire_lock(lock_path):
        _atomic_write_json(_session_path(session_key), ctx)
    return ctx


def _load_session_context(session_key: Optional[str]) -> Optional[dict]:
    if not session_key:
        return None
    path = _session_path(session_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) or None
    except (OSError, json.JSONDecodeError):
        return None


def _write_session_context(session_key: str, ctx: dict) -> None:
    os.makedirs(_SESSION_DIR, exist_ok=True)
    lock_path = os.path.join(_LOCK_DIR, f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', session_key)}.lock")
    with _acquire_lock(lock_path):
        _atomic_write_json(_session_path(session_key), ctx)


def _clear_session_context(session_key: Optional[str]) -> None:
    if not session_key:
        return
    try:
        path = _session_path(session_key)
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _advance_generation(session_key: str, session_ctx: dict) -> str:
    """Start a new generation within the session. Returns the generation key."""
    count = session_ctx.get("generation_count", 0) + 1
    gen_key = f"{session_key}_gen_{count}"
    session_ctx["generation_count"] = count
    session_ctx["current_generation"] = gen_key
    _write_session_context(session_key, session_ctx)
    return gen_key


def _resolve_generation_key(data: dict, session_ctx: Optional[dict]) -> Optional[str]:
    """Resolve the generation key for this event.

    Cursor provides generation_id directly. Copilot derives it from the
    session context's current_generation counter.
    """
    gen_id = _generation_key_from_data(data)
    if gen_id:
        return gen_id
    if session_ctx:
        return session_ctx.get("current_generation")
    return None


# ---------------------------------------------------------------------------
# Batch buffer
# ---------------------------------------------------------------------------
def _batch_enabled() -> bool:
    return _safe_bool(os.getenv("IDE_OTEL_BATCH_ON_STOP", ""))


def _local_spans_configured() -> bool:
    return bool(os.getenv("IDE_OTEL_LOCAL_SPANS", "") or os.getenv("IDE_OTEL_LOCAL_TRACE_SAVING", ""))


def _local_spans_enabled() -> bool:
    """Return whether local spans are enabled for the current session."""
    if _local_spans_configured():
        val = os.getenv("IDE_OTEL_LOCAL_SPANS", "") or os.getenv("IDE_OTEL_LOCAL_TRACE_SAVING", "")
        return _safe_bool(val)
    return _batch_enabled()


def _continue_response_json() -> str:
    payload = {"continue": True}
    if _local_spans_configured():
        payload["local_spans"] = _local_spans_enabled()
    return json.dumps(payload)


def _local_span_path(session_key: Optional[str]) -> str:
    key = session_key or "unscoped"
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
    return os.path.join(_LOCAL_SPANS_DIR, f"{safe_key}.jsonl")


def _save_local_span_event(event_name: str, ide: str, data: dict) -> None:
    if not _local_spans_enabled():
        return
    session_key = _session_key(data)
    record = {
        "timestamp_ns": time.time_ns(),
        "event": event_name,
        "ide": ide,
        "session_key": session_key,
        "generation_key": _generation_key_from_data(data),
        "data": data,
    }
    lock_key = session_key or "unscoped"
    lock_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", lock_key)
    lock_path = os.path.join(_LOCK_DIR, f"local_spans_{lock_name}.lock")
    try:
        os.makedirs(_LOCAL_SPANS_DIR, exist_ok=True)
        with _acquire_lock(lock_path):
            with open(_local_span_path(session_key), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
    except OSError as exc:
        _LOGGER.debug("local spans save failed: %s", exc)


def _local_trace_saving_configured() -> bool:
    return _local_spans_configured()


def _local_trace_saving_enabled() -> bool:
    return _local_spans_enabled()


def _save_local_trace_event(event_name: str, ide: str, data: dict) -> None:
    _save_local_span_event(event_name, ide, data)


def _batch_path(key: str) -> str:
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
    return os.path.join(_BATCH_DIR, f"{safe_key}.jsonl")


def _append_batch_event(key: str, event_name: str, data: dict) -> None:
    os.makedirs(_BATCH_DIR, exist_ok=True)
    record = {"event": event_name, "timestamp_ns": time.time_ns(), "data": data}
    lock_path = os.path.join(_LOCK_DIR, f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', key)}.lock")
    with _acquire_lock(lock_path):
        with open(_batch_path(key), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=True) + "\n")


def _load_batch_events(key: str) -> list:
    path = _batch_path(key)
    if not os.path.exists(path):
        return []
    events = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return events


def _clear_batch_events(key: str) -> None:
    try:
        path = _batch_path(key)
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Trace context helpers
# ---------------------------------------------------------------------------
def _make_trace_context(trace_id_hex: str, span_id_hex: str):
    """Create an OTel context from hex trace/span IDs for cross-process linking."""
    if SpanContext is None:
        return None
    try:
        tid = int(trace_id_hex, 16)
        sid = int(span_id_hex, 16)
    except (ValueError, TypeError):
        return None
    if not tid or not sid:
        return None
    ctx = SpanContext(
        trace_id=tid, span_id=sid, is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED), trace_state=TraceState(),
    )
    return trace.set_span_in_context(NonRecordingSpan(ctx))


# ---------------------------------------------------------------------------
# GenAI semantic conventions
# ---------------------------------------------------------------------------
def _genai_operation(event_name: str) -> str:
    if event_name in _OP_TOOL_EVENTS:
        return "execute_tool"
    if event_name in _OP_AGENT_EVENTS:
        return "invoke_agent"
    return "chat"


def _genai_messages(
    prompt: Optional[str], response: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    inp = None
    out = None
    if prompt:
        inp = json.dumps([{"role": "user", "parts": [{"type": "text", "content": prompt}]}], ensure_ascii=True)
    if response:
        out = json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": response}]}], ensure_ascii=True)
    return inp, out


def _apply_genai_semconv(span, event_name: str, data: dict, ide: str) -> None:
    provider = _infer_genai_provider(data)
    if provider is not None:
        span.set_attribute("gen_ai.provider.name", provider)
    # Keep gen_ai.system aligned with the IDE identifier; provider identity is in gen_ai.provider.name.
    span.set_attribute("gen_ai.system", ide)
    span.set_attribute("gen_ai.operation.name", _genai_operation(event_name))
    _set_if_present(span, "gen_ai.conversation.id", data.get("conversation_id") or data.get("session_id"))
    _set_if_present(span, "gen_ai.agent.id", _first_present(data, ("agent_id",)))
    _set_if_present(span, "gen_ai.agent.name", _first_present(data, ("agent_name", "subagent_type", "agent_type")))
    _set_if_present(span, "gen_ai.agent.version", _first_present(data, ("agent_version",)))
    _set_if_present(span, "gen_ai.agent.description", _first_present(data, ("agent_description",)))

    # Model
    _set_if_present(span, "gen_ai.request.model", _first_present(data, ("request_model", "model", "model_name")))
    _set_if_present(span, "gen_ai.response.model", _first_present(data, ("response_model",)))
    _set_if_present(span, "gen_ai.request.choice.count", _int_or_none(_first_present(data, ("choice_count",))))
    _set_if_present(
        span,
        "gen_ai.output.type",
        _normalize_genai_output_type(_first_present(data, ("output_type", "response_format"))),
    )

    # Token usage (top-level)
    _set_if_present(span, "gen_ai.usage.input_tokens", _int_or_none(_first_present(data, ("input_tokens", "prompt_tokens"))))
    _set_if_present(span, "gen_ai.usage.output_tokens", _int_or_none(_first_present(data, ("output_tokens", "completion_tokens"))))
    _set_if_present(
        span,
        "gen_ai.usage.cache_creation.input_tokens",
        _int_or_none(_first_present(data, ("cache_creation_input_tokens",))),
    )
    _set_if_present(
        span,
        "gen_ai.usage.cache_read.input_tokens",
        _int_or_none(_first_present(data, ("cache_read_input_tokens",))),
    )

    # Token usage (nested)
    usage = data.get("usage")
    if isinstance(usage, dict):
        _set_if_present(span, "gen_ai.usage.input_tokens", _int_or_none(_first_present(usage, ("input_tokens", "prompt_tokens"))))
        _set_if_present(span, "gen_ai.usage.output_tokens", _int_or_none(_first_present(usage, ("output_tokens", "completion_tokens"))))
        _set_if_present(
            span,
            "gen_ai.usage.cache_creation.input_tokens",
            _int_or_none(_first_present(usage, ("cache_creation_input_tokens", "cache_creation_tokens"))),
        )
        _set_if_present(
            span,
            "gen_ai.usage.cache_read.input_tokens",
            _int_or_none(_first_present(usage, ("cache_read_input_tokens", "cached_input_tokens"))),
        )
        _set_if_present(span, "ide.usage.total_tokens", _int_or_none(_first_present(usage, ("total_tokens",))))

    # Request params
    for source in (data, data.get("metadata") or {}):
        _set_if_present(span, "gen_ai.request.temperature", _float_or_none(_first_present(source, ("temperature",))))
        _set_if_present(span, "gen_ai.request.top_p", _float_or_none(_first_present(source, ("top_p",))))
        _set_if_present(span, "gen_ai.request.top_k", _float_or_none(_first_present(source, ("top_k",))))
        _set_if_present(span, "gen_ai.request.max_tokens", _int_or_none(_first_present(source, ("max_tokens",))))
        _set_if_present(span, "gen_ai.request.frequency_penalty", _float_or_none(_first_present(source, ("frequency_penalty",))))
        _set_if_present(span, "gen_ai.request.presence_penalty", _float_or_none(_first_present(source, ("presence_penalty",))))
        _set_if_present(span, "gen_ai.request.seed", _int_or_none(_first_present(source, ("seed",))))
        _set_if_present(span, "gen_ai.response.id", _first_present(source, ("response_id",)))
        finish = _first_present(source, ("finish_reasons",))
        if finish is not None:
            span.set_attribute("gen_ai.response.finish_reasons", finish if isinstance(finish, list) else [str(finish)])
        stop_seq = _first_present(source, ("stop_sequences",))
        if stop_seq is not None:
            span.set_attribute("gen_ai.request.stop_sequences", stop_seq if isinstance(stop_seq, list) else [str(stop_seq)])

    # Tool definitions (opt-in)
    tool_defs = _first_present(data, ("tool_definitions", "tools", "tool_schema"))
    if tool_defs is not None and _safe_bool(os.getenv("IDE_OTEL_CAPTURE_TOOL_DEFINITIONS", "")):
        span.set_attribute("gen_ai.tool.definitions", _stringify(tool_defs))

    # GenAI messages (opt-in)
    if _safe_bool(os.getenv("IDE_OTEL_CAPTURE_TEXT", "")):
        prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else None
        response = data.get("response") if isinstance(data.get("response"), str) else None
        if _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")):
            prompt = _mask_text(prompt) if prompt else None
            response = _mask_text(response) if response else None
        inp_msg, out_msg = _genai_messages(prompt, response)
        _set_if_present(span, "gen_ai.input.messages", inp_msg)
        _set_if_present(span, "gen_ai.output.messages", out_msg)
        system_instructions = _first_present(data, ("system_instructions", "system_prompt"))
        if system_instructions is not None:
            _set_if_present(span, "gen_ai.system_instructions", _stringify(system_instructions))


# ---------------------------------------------------------------------------
# Span population
# ---------------------------------------------------------------------------
def _populate_span(span, event_name: str, data: dict, ide: str) -> None:
    """Attach all attributes to a span and emit OTel log records."""
    span.set_attribute("ide.hook.event", event_name)
    span.set_attribute("ide.name", ide)

    # Emit structured OTel log record (MCP, shell, tool — correlated with this span)
    _emit_event_log(event_name, data)
    _set_if_present(span, "ide.session_id", data.get("session_id") or data.get("conversation_id"))
    _set_if_present(span, "ide.generation_id", data.get("generation_id"))
    span.set_attribute("ide.timestamp", datetime.now(timezone.utc).isoformat())
    span.set_attribute("ide.workspace", data.get("cwd") or os.getcwd())

    # GenAI semantic conventions
    _apply_genai_semconv(span, event_name, data, ide)

    # Flatten metadata dict
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        flat = {}  # type: dict
        _flatten(flat, "ide.metadata", metadata)
        for k, v in flat.items():
            _set_if_present(span, k, v)

    # Event-specific attributes
    mapping = _EVENT_ATTR_MAP.get(event_name, {})
    for key, attr in mapping.items():
        _set_if_present(span, attr, data.get(key))

    # Text fields
    for label in ("prompt", "response"):
        value = data.get(label)
        if isinstance(value, str):
            _maybe_attach_text(span, label, value)

    for label in ("tool_input", "tool_output", "mcp_input", "mcp_output"):
        value = data.get(label)
        if value is not None and not isinstance(value, dict):
            _maybe_attach_text(span, label, _stringify(value))


def _flatten(out: dict, prefix: str, data: dict) -> None:
    for key, value in data.items():
        if value is None:
            continue
        name = f"{prefix}.{key}"
        if isinstance(value, dict):
            _flatten(out, name, value)
        elif isinstance(value, list):
            out[name] = json.dumps(value, ensure_ascii=True)
        else:
            out[name] = value


# ---------------------------------------------------------------------------
# Flush helpers (session-level batching)
# ---------------------------------------------------------------------------
def _flush_generation(tracer, gen_key: str, session_ctx: Optional[dict], ide: str) -> None:
    """Flush buffered generation events as a subtree under the session trace."""
    batch = sorted(
        _load_batch_events(gen_key),
        key=lambda e: e.get("timestamp_ns") or 0,
    )
    if not batch:
        _clear_batch_events(gen_key)
        return

    first_ts = batch[0].get("timestamp_ns") or time.time_ns()
    last_ts = batch[-1].get("timestamp_ns") or time.time_ns()

    # Use session trace context if available, so this generation shares the trace_id
    parent_ctx = None
    if session_ctx:
        parent_ctx = _make_trace_context(
            session_ctx.get("trace_id", "0"),
            session_ctx.get("phantom_parent_id", "0"),
        )

    gen_span = tracer.start_span(
        "ide.generation", kind=SpanKind.INTERNAL,
        context=parent_ctx, start_time=first_ts,
    )
    gen_ctx = trace.set_span_in_context(gen_span)
    with _span_context(gen_span):
        gen_span.set_attribute("ide.generation_id", gen_key)
        gen_span.set_attribute("ide.event.count", len(batch))
        gen_span.set_attribute("ide.name", ide)
        _log_with_span(_LOGGER, logging.INFO, gen_span, "Generation span: gen_key=%s events=%d", gen_key, len(batch))

        for idx, entry in enumerate(batch):
            evt = entry.get("event") or "unknown"
            evt_data = entry.get("data") or {}
            ts = entry.get("timestamp_ns") or time.time_ns()
            next_ts = batch[idx + 1].get("timestamp_ns") if idx + 1 < len(batch) else ts + 1_000_000

            span = tracer.start_span(
                f"ide.hook.{evt}", kind=SpanKind.INTERNAL,
                context=gen_ctx, start_time=ts,
            )
            with _span_context(span):
                _populate_span(span, evt, evt_data, ide)
            span.end(end_time=next_ts)

        gen_span.end(end_time=last_ts)
        _clear_batch_events(gen_key)

    _force_flush_provider()
    _LOGGER.info("Flushed generation %s (%d events)", gen_key, len(batch))


def _flush_session(tracer, session_key: str, session_ctx: dict, ide: str) -> None:
    """Emit the root session span covering the full session duration."""
    start_ns = session_ctx.get("start_time_ns") or time.time_ns()
    end_ns = time.time_ns()

    parent_ctx = _make_trace_context(
        session_ctx.get("trace_id", "0"),
        session_ctx.get("phantom_parent_id", "0"),
    )

    session_span = tracer.start_span(
        "ide.session", kind=SpanKind.INTERNAL,
        context=parent_ctx, start_time=start_ns,
    )
    with _span_context(session_span):
        session_span.set_attribute("ide.session_id", session_key)
        session_span.set_attribute("ide.name", ide)
        session_span.set_attribute("ide.generation_count", session_ctx.get("generation_count", 0))
        session_span.set_attribute("ide.session.duration_ms", (end_ns - start_ns) // 1_000_000)
        _log_with_span(_LOGGER, logging.INFO, session_span, "Session span: session=%s", session_key)
        session_span.end(end_time=end_ns)

    _force_flush_provider()
    trace_id = session_ctx.get("trace_id", "unknown")
    _LOGGER.info("Flushed session %s (trace_id=%s)", session_key, trace_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    _apply_config_env(_load_config())
    _configure_logging()
    _cleanup_state()

    input_data = _load_input()
    if not isinstance(input_data, dict):
        input_data = {}
    data = _normalize_input_data(input_data)
    raw_event = _get_event_name(data)
    event_name = _normalize_event(raw_event)
    ide = _detect_ide(data)

    if _safe_bool(os.getenv("IDE_OTEL_LOG_EVENTS", "")):
        _LOGGER.info(
            "Hook: %s (raw=%s) | ide=%s | python=%s",
            event_name, raw_event, ide, sys.executable,
        )

    if not _init_tracing(ide):
        print(_continue_response_json())
        return 0

    if _local_spans_enabled():
        _enable_file_exporter(_local_span_path(_session_key(data)))

    tracer = trace.get_tracer("ide-hooks")
    _flush_stale_sessions(tracer)

    try:
        sk = _session_key(data)
        session_ctx = _load_session_context(sk)

        # ── Batch mode: session-level trace hierarchy ──
        if _batch_enabled():

            # SessionStart: create session context
            if event_name in _SESSION_START_EVENTS:
                if sk:
                    session_ctx = _create_session_context(sk, data, ide)
                    _append_batch_event(f"{sk}_session", event_name, data)
                print(_continue_response_json())
                return 0

            # UserPromptSubmit: start a new generation
            if event_name in _GENERATION_START_EVENTS:
                gen_key = _generation_key_from_data(data)
                if not gen_key and sk and session_ctx:
                    gen_key = _advance_generation(sk, session_ctx)
                    session_ctx = _load_session_context(sk)
                if gen_key:
                    _append_batch_event(gen_key, event_name, data)
                print(_continue_response_json())
                return 0

            # Stop: flush generation
            if event_name in _GENERATION_END_EVENTS:
                gen_key = _resolve_generation_key(data, session_ctx)
                if gen_key:
                    _append_batch_event(gen_key, event_name, data)
                    _flush_generation(tracer, gen_key, session_ctx, ide)
                    # Clear current_generation in session state
                    if sk and session_ctx:
                        session_ctx.pop("current_generation", None)
                        _write_session_context(sk, session_ctx)
                print(_continue_response_json())
                return 0

            # SessionEnd: emit session root span, clean up
            if event_name in _SESSION_END_EVENTS:
                if sk and session_ctx:
                    _flush_session(tracer, sk, session_ctx, ide)
                    _clear_session_context(sk)
                print(_continue_response_json())
                return 0

            # All other events: buffer under current generation
            gen_key = _resolve_generation_key(data, session_ctx)
            if gen_key:
                _append_batch_event(gen_key, event_name, data)
            else:
                # No generation context — emit as standalone span
                parent_ctx = None
                if session_ctx:
                    parent_ctx = _make_trace_context(
                        session_ctx.get("trace_id", "0"),
                        session_ctx.get("phantom_parent_id", "0"),
                    )
                with tracer.start_as_current_span(
                    f"ide.hook.{event_name}", kind=SpanKind.INTERNAL,
                    context=parent_ctx,
                ) as span:
                    _populate_span(span, event_name, data, ide)

            print(_continue_response_json())
            return 0

        # ── Streaming mode: emit spans immediately ──
        parent_ctx = None
        if session_ctx:
            parent_ctx = _make_trace_context(
                session_ctx.get("trace_id", "0"),
                session_ctx.get("phantom_parent_id", "0"),
            )

        # Create session context on SessionStart even in streaming mode
        if event_name in _SESSION_START_EVENTS and sk and not session_ctx:
            session_ctx = _create_session_context(sk, data, ide)
            parent_ctx = _make_trace_context(
                session_ctx["trace_id"],
                session_ctx["phantom_parent_id"],
            )

        with tracer.start_as_current_span(
            f"ide.hook.{event_name}", kind=SpanKind.INTERNAL,
            context=parent_ctx,
        ) as span:
            _populate_span(span, event_name, data, ide)

        # Clean up session on SessionEnd
        if event_name in _SESSION_END_EVENTS and sk:
            if session_ctx:
                _flush_session(tracer, sk, session_ctx, ide)
            _clear_session_context(sk)

        # Flush in streaming mode to ensure spans are exported
        _force_flush_provider()

    except Exception as exc:
        if trace is not None and Status is not None:
            cur = trace.get_current_span()
            if cur is not None:
                cur.record_exception(exc)
                cur.set_status(Status(StatusCode.ERROR, str(exc)))
        _LOGGER.exception("Hook failure: %s", exc)

    print(_continue_response_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
