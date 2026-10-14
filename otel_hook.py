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
import contextlib
import glob
import hashlib
import importlib
import importlib.util
import importlib.metadata
import json
import logging
import os
import platform
import random
import re
import shutil
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

import click
from otel_hooks import bootstrap
from otel_hooks.attributes import _EVENT_ATTR_MAP, _OBSERVATION_FIELDS as _OBSERVATION_FIELDS
from otel_hooks.capture import CapturePolicy
from otel_hooks.config import (
    environment_settings, merge_settings,
    _coerce_env_value as _coerce_env_value,
    _headers_to_env as _headers_to_env,
    _load_mdm_config_macos as _load_mdm_config_macos,
    _load_mdm_config_windows as _load_mdm_config_windows,
    _load_json_config as _load_json_config,
    _parse_resource_attributes as _parse_resource_attributes,
    _parse_otlp_headers as _parse_otlp_headers,
)


# Compatibility re-exports; parsing itself lives in the SDK-independent package.
from otel_hooks.parsers.events import (
    _CANONICAL_EVENT as _CANONICAL_EVENT,
    _COPILOT_EVENTS as _COPILOT_EVENTS,
    _WINDSURF_EVENT_ALIASES as _WINDSURF_EVENT_ALIASES,
    _INPUT_ALIASES as _INPUT_ALIASES,
    _GENERATION_START_EVENTS as _GENERATION_START_EVENTS,
    _TRACE_ID_RE as _TRACE_ID_RE,
    _SPAN_ID_RE as _SPAN_ID_RE,
    _MCP_EVENTS as _MCP_EVENTS,
)
from otel_hooks.parsers.common import (
    _EMAIL_RE as _EMAIL_RE,
    _TOKEN_RE as _TOKEN_RE,
    _HOME_RE as _HOME_RE,
    _safe_bool as _safe_bool,
    _stringify as _stringify,
    _first_present as _first_present,
    _normalize_input_data as _normalize_input_data,
    _bound_tool_response_payload as _bound_tool_response_payload,
    _hash_text as _hash_text,
    _mask_text as _mask_text,
    _conversation_content_enabled as _conversation_content_enabled,
    _text_max_chars as _text_max_chars,
    _session_key as _session_key,
    _generation_key_from_data as _generation_key_from_data,
    _parse_encoded_mcp_tool_name as _parse_encoded_mcp_tool_name,
    _normalize_mcp_event_data as _normalize_mcp_event_data,
)
from otel_hooks.parsers.models import (
    ConversationRecord as ConversationRecord,
    AgentRelationship as AgentRelationship,
    WorkspaceIdentity as WorkspaceIdentity,
    NativeTelemetryContext as NativeTelemetryContext,
    CanonicalHookEvent as CanonicalHookEvent,
)
from otel_hooks.parsers.base import (
    ProviderEventAdapter as ProviderEventAdapter,
)
from otel_hooks.parsers.cursor import (
    CursorEventAdapter as CursorEventAdapter,
)
from otel_hooks.parsers.windsurf import (
    WindsurfEventAdapter as WindsurfEventAdapter,
)
from otel_hooks.parsers.claude import (
    ClaudeEventAdapter as ClaudeEventAdapter,
)
from otel_hooks.parsers.codex import (
    CodexEventAdapter as CodexEventAdapter,
)
from otel_hooks.parsers.gemini import (
    GeminiEventAdapter as GeminiEventAdapter,
)
from otel_hooks.parsers.antigravity import (
    AntigravityEventAdapter as AntigravityEventAdapter,
)
from otel_hooks.parsers.copilot import (
    CopilotEventAdapter as CopilotEventAdapter,
)
from otel_hooks.parsers.opencode import (
    OpenCodeEventAdapter as OpenCodeEventAdapter,
)
from otel_hooks.parsers import (
    _DEFAULT_EVENT_ADAPTER as _DEFAULT_EVENT_ADAPTER,
    _PROVIDER_EVENT_ADAPTERS as _PROVIDER_EVENT_ADAPTERS,
    _event_adapter_for as _event_adapter_for,
)
from enrichment_connectors import (
    aggregate_generation_memory,
    extract_event_memory_facts,
    merge_memory_summaries,
    normalize_memory_summary,
)

# Whether to attach OS/host attributes to every span in addition to resource attributes.
# Defaults to False to avoid duplicate data and hot-path overhead.
_CONSOLE_EXPORTER_REGISTERED = False

_ATTACH_OS_ATTRIBUTES_PER_SPAN = os.getenv("IDE_HOOK_ATTACH_OS_PER_SPAN") == "1"

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
    2. ``%LOCALAPPDATA%/opentelemetry-hooks`` on Windows, or
       ``$XDG_DATA_HOME/opentelemetry-hooks`` on POSIX (defaults to
       ``~/.local/share/opentelemetry-hooks`` when ``XDG_DATA_HOME`` is unset)
       when the hook is running from an installed package — i.e. ``__file__``
       lives inside a *site-packages* directory.
    3. The directory that contains ``__file__`` — legacy behaviour for a
       source checkout (keep its companion modules).
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
        if sys.platform == "win32":
            local_data = os.environ.get("LOCALAPPDATA", "").strip() or os.path.join(
                os.path.expanduser("~"), "AppData", "Local",
            )
            return os.path.join(local_data, "opentelemetry-hooks")
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        if not xdg_data:
            xdg_data = os.path.join(os.path.expanduser("~"), ".local", "share")
        return os.path.join(xdg_data, "opentelemetry-hooks")

    return os.path.dirname(this_file)


_HOOK_DIR = _resolve_hook_home()


def _venv_site_packages_path(venv_dir: str) -> str:
    """Return the site-packages path compatible with this Python runtime."""
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Lib", "site-packages")
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return os.path.join(venv_dir, "lib", version, "site-packages")


def _select_bootstrap_venv_dir(hook_dir: str) -> str:
    """Reuse a compatible legacy venv or isolate bootstrap deps by Python ABI."""
    legacy = os.path.join(hook_dir, ".venv")
    # Windows venv layouts do not encode the Python ABI; use a versioned venv.
    if sys.platform != "win32" and os.path.isdir(_venv_site_packages_path(legacy)):
        return legacy
    suffix = f".venv-py{sys.version_info.major}.{sys.version_info.minor}"
    return os.path.join(hook_dir, suffix)


def _runtime_has_otel_dependencies() -> bool:
    """Return whether the invoking environment already owns the hook SDK deps."""
    required = (
        "opentelemetry.sdk.trace.export",
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    )
    try:
        for module in required:
            importlib.import_module(module)
        return True
    except (ImportError, AttributeError, ValueError):
        return False


_VENV_DIR = _select_bootstrap_venv_dir(_HOOK_DIR)
_SETUP_LOCK = os.path.join(
    _HOOK_DIR,
    ".state",
    f"setup-py{sys.version_info.major}.{sys.version_info.minor}.lock",
)


def _auto_provision_venv() -> None:
    """Create .venv and install opentelemetry-sdk + exporter in the background if missing."""
    venv_python = bootstrap.venv_python(_VENV_DIR)
    if os.path.isfile(venv_python) and os.path.isfile(os.path.join(_VENV_DIR, bootstrap.READY_MARKER)):
        return
    lock_dir = os.path.dirname(_SETUP_LOCK)
    os.makedirs(lock_dir, exist_ok=True)
    if os.path.exists(_SETUP_LOCK):
        return
    try:
        with open(_SETUP_LOCK, "x") as f:
            f.write(str(os.getpid()))
        process_options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if sys.platform == "win32" else {"start_new_session": True}
        )
        subprocess.Popen(
            [sys.executable, bootstrap.__file__, _VENV_DIR, _SETUP_LOCK],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **process_options,
        )
    except FileExistsError:
        # Another invocation owns provisioning; do not remove its lock.
        return
    except Exception:
        try:
            os.remove(_SETUP_LOCK)
        except OSError:
            pass


_RUNTIME_HAS_OTEL_DEPENDENCIES = _runtime_has_otel_dependencies()
if not _RUNTIME_HAS_OTEL_DEPENDENCIES:
    _auto_provision_venv()
    _venv_site_packages = _venv_site_packages_path(_VENV_DIR)
    if os.path.isdir(_venv_site_packages) and _venv_site_packages not in sys.path:
        sys.path.insert(0, _venv_site_packages)

class _TraceShim:
    """Small shim so tests can monkeypatch trace methods before lazy load."""

    def set_tracer_provider(self, _provider) -> None:
        if _REAL_TRACE is not None:
            return _REAL_TRACE.set_tracer_provider(_provider)
        return None

    def get_tracer_provider(self):
        if _REAL_TRACE is not None:
            return _REAL_TRACE.get_tracer_provider()
        return None

    def get_tracer(self, _name: str):
        if _REAL_TRACE is not None:
            return _REAL_TRACE.get_tracer(_name)
        return None

    def get_current_span(self):
        if _REAL_TRACE is not None:
            return _REAL_TRACE.get_current_span()
        return None

    def set_span_in_context(self, _span):
        if _REAL_TRACE is not None:
            return _REAL_TRACE.set_span_in_context(_span)
        return None


# Lazy-loaded OpenTelemetry symbols (loaded only when tracing/logging is needed).
trace = _TraceShim()
_OTEL_MODULES_LOADED = False
_REAL_TRACE = None
NonRecordingSpan = None
Link = None
SpanContext = None
SpanKind = None
Status = None
StatusCode = None
TraceFlags = None
TraceState = None
use_span = None


class SpanExportResult:  # minimal shim for SDK-unavailable environments
    SUCCESS = 0
    FAILURE = 1


def _load_otel_modules() -> bool:
    """Load OpenTelemetry modules lazily; return False when unavailable."""
    global _OTEL_MODULES_LOADED
    global _REAL_TRACE
    global NonRecordingSpan, Link, SpanContext, SpanKind, Status, StatusCode, TraceFlags, TraceState, use_span
    global SpanExportResult
    if _OTEL_MODULES_LOADED:
        return True

    otel_spec = importlib.util.find_spec("opentelemetry")
    if otel_spec is None:
        return False
    sdk_spec = importlib.util.find_spec("opentelemetry.sdk.trace.export")
    if sdk_spec is None:
        return False
    otel_api = importlib.import_module("opentelemetry")
    _REAL_TRACE = otel_api.trace
    otel_trace = importlib.import_module("opentelemetry.trace")
    NonRecordingSpan = otel_trace.NonRecordingSpan
    Link = otel_trace.Link
    SpanContext = otel_trace.SpanContext
    SpanKind = otel_trace.SpanKind
    Status = otel_trace.Status
    StatusCode = otel_trace.StatusCode
    TraceFlags = otel_trace.TraceFlags
    TraceState = otel_trace.TraceState
    use_span = otel_trace.use_span
    sdk_export = importlib.import_module("opentelemetry.sdk.trace.export")
    SpanExportResult = sdk_export.SpanExportResult
    _OTEL_MODULES_LOADED = True
    return True


_ORJSON = None
if importlib.util.find_spec("orjson") is not None:
    _ORJSON = importlib.import_module("orjson")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CAPTURE_POLICY = CapturePolicy()
_TRACING_INITIALIZED = False
_LOGS_INITIALIZED = False
_EXPORT_DEADLINE: Optional[float] = None
_EXPORT_FAILURES: set[str] = set()
_FILE_EXPORTER_PATHS: set[str] = set()   # paths that already have a FileSpanExporter attached
_CONSOLE_EXPORTER_REGISTERED = False  # True once a ConsoleSpanExporter has been attached
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
_DELIVERY_HEALTH_PATH = os.path.join(_STATE_DIR, "delivery_health.json")
_SESSION_EVENT_LIMIT = 512
_SESSION_INVOCATION_LIMIT = 128
_AGENT_INVOCATION_LIMIT = 128
_HOOK_SCHEMA_VERSION = "1"
_TELEMETRY_SOURCE = "hook"

# MDM (Managed Device Management) configuration
_MDM_DOMAIN = "dev.o11y.opentelemetry-hook"  # macOS managed preferences domain
_MDM_REGISTRY_PATH = r"SOFTWARE\Policies\OpenTelemetryHook"  # Windows registry path

# Privacy patterns
_HEX_DIGITS = frozenset("0123456789abcdef")


# ---------------------------------------------------------------------------
# Event name canonicalization (all IDE variants -> PascalCase)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Setup CLI: per-agent event lists
# ---------------------------------------------------------------------------
_CURSOR_EVENTS = [
    "sessionStart", "sessionEnd", "subagentStart", "subagentStop",
    "preToolUse", "postToolUse", "postToolUseFailure",
    "beforeShellExecution", "afterShellExecution",
    "beforeMCPExecution", "afterMCPExecution",
    "beforeReadFile", "afterFileEdit",
    "beforeSubmitPrompt", "stop",
    "preCompact", "beforeTabFileRead", "afterTabFileEdit",
    "afterAgentResponse", "afterAgentThought", "workspaceOpen",
]

_CLAUDE_EVENTS = [
    "SessionStart", "SessionEnd", "SubagentStart", "SubagentStop",
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "UserPromptSubmit", "PreCompact", "PostCompact", "Stop",
    "PermissionRequest", "PermissionDenied", "Notification", "StopFailure",
    "Setup", "InstructionsLoaded", "UserPromptExpansion", "MessageDisplay",
    "PostToolBatch", "TaskCreated", "TaskCompleted", "TeammateIdle",
    "ConfigChange", "CwdChanged", "DirectoryAdded", "WorktreeRemove",
    "PreModelSwitch", "PostModelSwitch", "Elicitation", "ElicitationResult",
]
# WorktreeCreate replaces git worktree creation; FileChanged installs watchers.
# Neither belongs in an automatically installed telemetry observer.
_CLAUDE_MATCHER_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}


_GEMINI_EVENTS = [
    "SessionStart", "SessionEnd", "BeforeAgent", "AfterAgent",
    "BeforeModel", "AfterModel", "BeforeTool", "AfterTool",
    "BeforeToolSelection", "Notification", "PreCompress",
]
_GEMINI_MATCHER_EVENTS = {"BeforeAgent", "AfterAgent", "BeforeModel", "AfterModel", "BeforeTool", "AfterTool"}

_CODEX_EVENTS = [
    "SessionStart", "PreToolUse", "PermissionRequest", "PostToolUse",
    "UserPromptSubmit", "Stop",
    "SessionEnd", "PreCompact", "PostCompact", "SubagentStart", "SubagentStop", "Interrupt",
]
_CODEX_MATCHERS = {
    "SessionStart": "startup|resume|clear|compact",
    "PreToolUse": "*",
    "PermissionRequest": "*",
    "PostToolUse": "*",
}

# TranscriptReady is accepted, but registration would cause the harness to write
# full conversation transcripts independently of our content-capture setting.
_WINDSURF_EVENTS = [event for event in _WINDSURF_EVENT_ALIASES if event != "post_cascade_response_with_transcript"]

# OpenCode plugin — source filename (in plugin/) and destination filename (in plugins/).
_OPENCODE_PLUGIN_SOURCE_FILENAME = "opencode.ts"
_OPENCODE_PLUGIN_FILENAME = "otel-hook.ts"

# Common camelCase -> snake_case aliases used by compatible hook runners.
# Claude Code's documented hook payloads are already snake_case, but generic
# runners and workflow adapters that forward Claude- or Antigravity-style
# events may supply camelCase fields instead.

# Canonical gen_ai.client.name values accepted directly from IDE_OTEL_IDE_NAME or
# self-reported payload metadata before alias fallback.
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
_MANAGED_HOOK_SOURCE_ENV = "IDE_OTEL_HOOK_SOURCE"
_CLI_HOOK_SOURCE: Optional[str] = None
_CLI_HOOK_EVENT: Optional[str] = None

# Session boundary events
_SESSION_START_EVENTS = {"SessionStart"}
_SESSION_END_EVENTS = {"SessionEnd"}
_GENERATION_END_EVENTS = {"Stop", "StopFailure", "Interrupt"}

# GenAI operation mapping (canonical PascalCase)
_OP_TOOL_EVENTS = {
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "PermissionRequest",
    "BeforeShellExecution", "AfterShellExecution",
    "BeforeMCPExecution", "AfterMCPExecution",
    "BeforeReadFile", "AfterFileEdit", "AfterReadFile", "BeforeFileEdit",
}
_OP_AGENT_EVENTS = {
    "SessionStart", "SessionEnd",
    "SubagentStart", "SubagentStop",
    "PreCompact", "PostCompact",
}

# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------
def _load_input() -> dict:
    if sys.stdin.isatty():
        print("IDE Agent OpenTelemetry Hook — pure OpenTelemetry SDK.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Usage:", file=sys.stderr)
        print("    echo '{\"hook_event_name\":\"sessionStart\",\"session_id\":\"abc\"}' | python3 otel_hook.py", file=sys.stderr)
        print("", file=sys.stderr)
        print("This hook is intended to be called by an IDE (Cursor, Copilot, Claude Code) with JSON on stdin.", file=sys.stderr)
        raise SystemExit(0)
    # Hook protocols use UTF-8 even when Windows pipes default to a legacy code page.
    raw = sys.stdin.buffer.read().decode("utf-8-sig") if hasattr(sys.stdin, "buffer") else sys.stdin.read()
    if not raw.strip():
        return {}
    return _fast_json_loads(raw)


def _fast_json_loads(raw: str):
    if _ORJSON is not None:
        try:
            return _ORJSON.loads(raw)
        except Exception:
            return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _set_if_present(span, attr: str, value) -> None:
    if value is not None:
        span.set_attribute(attr, value)


def _first_env(keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
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


def _iter_enrichment_sources(data: Optional[dict], include_tool_payloads: bool = False):
    if not isinstance(data, dict):
        return
    yield data
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        yield metadata
    user = data.get("user")
    if isinstance(user, dict):
        yield user
    if include_tool_payloads:
        for key in ("tool_input", "tool_response"):
            value = data.get(key)
            if isinstance(value, dict):
                yield value


def _coerce_attribute_value(value):
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _path_search_root(path: str) -> Optional[str]:
    if not isinstance(path, str) or not path.strip():
        return None
    expanded = os.path.abspath(path.strip())
    if os.path.isdir(expanded):
        return expanded
    return os.path.dirname(expanded) or expanded


def _candidate_repo_paths(data: Optional[dict]) -> list[str]:
    if not isinstance(data, dict):
        return []
    candidates: list[str] = []
    cwd = data.get("cwd") or data.get("workspace_path")
    if isinstance(cwd, str) and cwd.strip():
        candidates.append(cwd.strip())

    def _append_path(value) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        raw = value.strip()
        if os.path.isabs(raw):
            candidates.append(raw)
            return
        if isinstance(cwd, str) and cwd.strip() and os.path.isabs(cwd.strip()):
            candidates.append(os.path.join(cwd.strip(), raw))

    _append_path(data.get("file_path"))
    edits = data.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if isinstance(entry, dict):
                _append_path(entry.get("file_path") or entry.get("path"))
    elif isinstance(edits, dict):
        _append_path(edits.get("file_path") or edits.get("path"))
    return candidates


def _git_command_output(args: list[str], cwd: str) -> Optional[str]:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _parse_repository_remote(remote_url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(remote_url, str) or not remote_url.strip():
        return None, None
    normalized = remote_url.strip()
    is_network_remote = False
    if "://" in normalized:
        is_network_remote = True
        path = urllib.parse.urlparse(normalized).path or ""
    elif "@" in normalized and ":" in normalized.split("@", 1)[1]:
        is_network_remote = True
        path = normalized.split(":", 1)[1]
    else:
        path = normalized
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if not parts:
        return None, None
    owner = parts[-2] if is_network_remote and len(parts) >= 2 else None
    return owner, parts[-1]


def _normalize_repository_remote(remote_url: Optional[str]) -> Optional[str]:
    """Return a credential-free host/path identity suitable for deterministic hashing."""
    if not isinstance(remote_url, str) or not remote_url.strip():
        return None
    value = remote_url.strip()
    host = ""
    path = ""
    if "://" in value:
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").lower()
        try:
            if parsed.port:
                host = f"{host}:{parsed.port}"
        except ValueError:
            return None
        path = parsed.path
    elif "@" in value and ":" in value.split("@", 1)[1]:
        host_path = value.split("@", 1)[1]
        host, path = host_path.split(":", 1)
        host = host.lower()
    else:
        return None
    path = "/".join(part for part in path.strip().strip("/").split("/") if part)
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        return None
    return f"{host}/{path}"


def _resolve_repository_context(data: Optional[dict] = None, session_ctx: Optional[dict] = None) -> dict:
    repo_ctx: dict[str, str] = {}
    if session_ctx:
        for key in (
            "repo_root",
            "workspace_path",
            "vcs.repository.owner",
            "vcs.repository.name",
            "vcs.ref.head.name",
            "vcs.ref.head.type",
            "gen_ai.client.repository.remote.sha256",
        ):
            value = session_ctx.get(key)
            if isinstance(value, str) and value.strip():
                repo_ctx[key] = value.strip()
    explicit_workspace = _first_present(data or {}, ("workspace_path",))
    if isinstance(explicit_workspace, str) and explicit_workspace.strip():
        repo_ctx["workspace_path"] = os.path.abspath(os.path.expanduser(explicit_workspace.strip()))
    if session_ctx and session_ctx.get("repository_context_resolved") and repo_ctx.get("repo_root"):
        return repo_ctx
    if "repo_root" not in repo_ctx:
        for candidate in _candidate_repo_paths(data):
            search_root = _path_search_root(candidate)
            if not search_root:
                continue
            repo_root = _find_repo_root(search_root)
            if repo_root and any(os.path.exists(os.path.join(repo_root, marker)) for marker in _REPO_MARKERS):
                repo_ctx["repo_root"] = repo_root
                break
    repo_root = repo_ctx.get("repo_root")
    if not repo_root:
        return repo_ctx
    if not os.path.exists(os.path.join(repo_root, ".git")):
        return repo_ctx
    git_root = _git_command_output(["git", "rev-parse", "--show-toplevel"], cwd=repo_root)
    if git_root:
        repo_ctx["repo_root"] = git_root
        repo_ctx.setdefault("workspace_path", git_root)
        remote_url = None
        if (
            "vcs.repository.name" not in repo_ctx
            or "vcs.repository.owner" not in repo_ctx
            or "gen_ai.client.repository.remote.sha256" not in repo_ctx
        ):
            remote_url = _git_command_output(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=git_root,
            )
            owner, name = _parse_repository_remote(remote_url)
            if owner and "vcs.repository.owner" not in repo_ctx:
                repo_ctx["vcs.repository.owner"] = owner
            if name and "vcs.repository.name" not in repo_ctx:
                repo_ctx["vcs.repository.name"] = name
            normalized_remote = _normalize_repository_remote(remote_url)
            if normalized_remote:
                repo_ctx["gen_ai.client.repository.remote.sha256"] = _hash_text(normalized_remote)
        if "vcs.repository.name" not in repo_ctx:
            repo_name = os.path.basename(git_root.rstrip(os.sep))
            if repo_name:
                repo_ctx["vcs.repository.name"] = repo_name
        if "vcs.ref.head.name" not in repo_ctx:
            head_name = _git_command_output(
                ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=git_root,
            )
            if head_name:
                repo_ctx["vcs.ref.head.name"] = head_name
                repo_ctx["vcs.ref.head.type"] = "branch"
    return repo_ctx


def _collect_repository_attributes(data: Optional[dict] = None, session_ctx: Optional[dict] = None) -> dict:
    attrs: dict[str, str] = {}
    repo_ctx = _resolve_repository_context(data, session_ctx=session_ctx)
    for key in (
        "vcs.repository.owner",
        "vcs.repository.name",
        "vcs.ref.head.name",
        "vcs.ref.head.type",
        "gen_ai.client.repository.remote.sha256",
    ):
        value = repo_ctx.get(key)
        if value:
            attrs[key] = value
    return attrs


def _workspace_observability_attributes(data: Optional[dict], session_ctx: Optional[dict]) -> dict:
    data = data or {}
    repo_ctx = _resolve_repository_context(data, session_ctx=session_ctx)
    cwd = _first_present(data, ("cwd",))
    if not isinstance(cwd, str) or not cwd.strip():
        cwd = None
    workspace = _first_present(data, ("workspace_path",)) or repo_ctx.get("workspace_path")
    workspace = workspace or repo_ctx.get("repo_root") or cwd
    attrs = {}
    if workspace:
        attrs["gen_ai.client.workspace"] = os.path.abspath(os.path.expanduser(str(workspace)))
    if cwd:
        attrs["gen_ai.client.cwd"] = os.path.abspath(os.path.expanduser(cwd))
    if repo_ctx.get("repo_root"):
        attrs["gen_ai.client.repository_root"] = repo_ctx["repo_root"]
    for key in (
        "vcs.repository.owner",
        "vcs.repository.name",
        "vcs.ref.head.name",
        "vcs.ref.head.type",
        "gen_ai.client.repository.remote.sha256",
    ):
        if repo_ctx.get(key):
            attrs[key] = repo_ctx[key]
    return attrs


def _collect_user_identity_attributes(data: Optional[dict]) -> dict:
    if not _safe_bool(os.getenv("IDE_OTEL_CAPTURE_USER_IDENTITY", "")):
        return {}
    attrs = {}
    user_id = None
    user_email = None
    user = data.get("user") if isinstance(data, dict) else None
    if isinstance(user, dict):
        user_id = _first_present(user, ("user_id", "id", "login", "username", "user_name", "actor_id"))
        user_email = _first_present(user, ("user_email", "email"))
    sources = []
    if isinstance(data, dict):
        sources.append(data)
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            sources.append(metadata)
    for source in sources:
        if user_id is None:
            user_id = _first_present(source, ("user_id", "login", "username", "user_name", "actor_id"))
        if user_email is None:
            user_email = _first_present(source, ("user_email", "email"))
        if user_id is not None and user_email is not None:
            break
    user_id = _coerce_attribute_value(user_id)
    if user_id is not None:
        attrs["user.id"] = str(user_id)
    user_email = _coerce_attribute_value(user_email)
    if isinstance(user_email, str):
        if _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")):
            user_email = _mask_text(user_email)
        attrs["user.email"] = user_email
    return attrs


def _collect_governance_attributes(data: Optional[dict]) -> dict:
    if not isinstance(data, dict):
        return {}
    attrs = {}
    attr_map = (
        (("permission_mode",), "gen_ai.client.permission_mode"),
        (("permission_decision", "approval_decision"), "gen_ai.client.approval.decision"),
        (("approval_policy",), "gen_ai.client.approval.policy"),
        (("approval_required", "requires_approval"), "gen_ai.client.approval.required"),
        (("sandbox_mode",), "gen_ai.client.sandbox.mode"),
        (("sandbox_policy",), "gen_ai.client.sandbox.policy"),
        (("sandbox_enabled", "is_sandboxed", "sandboxed"), "gen_ai.client.sandbox.enabled"),
        (("tool_choice",), "gen_ai.client.tool_choice"),
        (("tool_decision",), "gen_ai.client.tool_decision"),
    )
    for source in _iter_enrichment_sources(data, include_tool_payloads=True):
        for keys, attr_name in attr_map:
            if attr_name in attrs:
                continue
            value = _coerce_attribute_value(_first_present(source, keys))
            if value is not None:
                attrs[attr_name] = value
    return attrs


def _collect_event_enrichment_attributes(data: Optional[dict] = None, session_ctx: Optional[dict] = None) -> dict:
    data = data or {}
    attrs = _workspace_observability_attributes(data, session_ctx=session_ctx)
    attrs.update(_collect_user_identity_attributes(data))
    attrs.update(_collect_governance_attributes(data))
    attrs.update(_native_telemetry_attributes(data))
    attrs["gen_ai.client.telemetry_source"] = _TELEMETRY_SOURCE
    attrs["gen_ai.client.hook_schema_version"] = _HOOK_SCHEMA_VERSION
    if data.get("_hook_event_id"):
        attrs["gen_ai.client.hook.event_id"] = data["_hook_event_id"]
    if _session_key(data):
        attrs["gen_ai.client.session_id"] = _session_key(data)
    if _generation_key_from_data(data):
        attrs["gen_ai.client.generation_id"] = _generation_key_from_data(data)
    if data.get("turn_id"):
        attrs["gen_ai.client.turn_id"] = data["turn_id"]
    return attrs


def _apply_enrichment_attributes(span, data: Optional[dict] = None, session_ctx: Optional[dict] = None) -> None:
    for key, value in _collect_event_enrichment_attributes(data, session_ctx=session_ctx).items():
        _set_if_present(span, key, value)


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


# ---------------------------------------------------------------------------
# OS / host detection (cached)
# ---------------------------------------------------------------------------
_OS_INFO: Optional[dict] = None


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


# ---------------------------------------------------------------------------
# Client (IDE) version detection
# ---------------------------------------------------------------------------
_CODEX_VERSION_CACHE: Optional[str] = None  # cached result; None means not yet detected or not found
_CODEX_VERSION_DETECTED: bool = False  # True once detection has been attempted


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
        or _get_event_name(data) in _CODEX_EVENTS
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


def _mcp_identity(data: dict) -> Tuple[Optional[str], Optional[str]]:
    """Return the already-normalized MCP server and tool identity."""
    return data.get("mcp_server"), data.get("mcp_tool")


def _mcp_observability_attributes(event_name: str, data: dict) -> dict:
    """Build content-safe MCP attributes shared by spans and logs."""
    normalized = _normalize_mcp_event_data(data, event_name)
    server, tool = _mcp_identity(normalized)
    if not server and not tool:
        return {}
    attrs = {}
    if server:
        attrs["gen_ai.client.mcp_server"] = server
    if tool:
        attrs["gen_ai.client.mcp_tool"] = tool
    invocation_id = normalized.get("tool_use_id")
    if invocation_id:
        attrs["gen_ai.client.tool_use_id"] = invocation_id
    duration = _first_present(normalized, ("duration_ms", "duration"))
    if duration is not None:
        attrs["gen_ai.client.mcp.duration_ms"] = duration
        attrs["gen_ai.client.duration_ms"] = duration
    status = normalized.get("status") or normalized.get("mcp_status")
    if event_name == "PostToolUseFailure" or normalized.get("error") is not None:
        status = "error"
    elif event_name in {"PostToolUse", "AfterMCPExecution"} and not status:
        status = "success"
    if status:
        attrs["gen_ai.client.status"] = status
    if normalized.get("error") is not None:
        error_text = _stringify(normalized["error"])
        attrs["gen_ai.client.error.length"] = len(error_text)
        attrs["gen_ai.client.error.sha256"] = _hash_text(error_text)
        if _conversation_content_enabled():
            if _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")):
                error_text = _mask_text(error_text)
            attrs["gen_ai.client.error.text"] = error_text[:_text_max_chars()]
    else:
        if normalized.get("error_length") is not None:
            attrs["gen_ai.client.error.length"] = normalized["error_length"]
        if normalized.get("error_sha256") is not None:
            attrs["gen_ai.client.error.sha256"] = normalized["error_sha256"]

    result = _first_present(normalized, ("result_json", "mcp_output", "tool_output", "output"))
    if result is not None:
        text = _stringify(result)
        attrs["gen_ai.client.mcp.output.length"] = len(text)
        attrs["gen_ai.client.mcp.output.sha256"] = _hash_text(text)
    else:
        if normalized.get("mcp_result_length") is not None:
            attrs["gen_ai.client.mcp.output.length"] = normalized["mcp_result_length"]
        if normalized.get("mcp_result_sha256") is not None:
            attrs["gen_ai.client.mcp.output.sha256"] = normalized["mcp_result_sha256"]
    return attrs


def _apply_mcp_attributes(span, event_name: str, data: dict) -> None:
    for key, value in _mcp_observability_attributes(event_name, data).items():
        _set_if_present(span, key, value)


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


def _stale_flush_session_limit() -> int:
    """Bound best-effort stale maintenance performed by an interactive hook."""
    try:
        return max(0, int(os.getenv("IDE_OTEL_STALE_FLUSH_MAX_SESSIONS", "1")))
    except (TypeError, ValueError):
        return 1


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
                raise TimeoutError(f"timed out acquiring state lock: {lock_path}")
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
    # Session files must survive until _flush_stale_sessions can emit their
    # pending generations and root span.  Only remove old orphan batches here.
    if os.path.isdir(_BATCH_DIR):
        for name in os.listdir(_BATCH_DIR):
            path = os.path.join(_BATCH_DIR, name)
            try:
                if not os.path.isfile(path) or os.path.getmtime(path) >= cutoff:
                    continue
                key = name.removesuffix(".jsonl")
                events = _load_batch_events(key)
                owner = next(
                    (_session_key(entry.get("data") or {}) for entry in events if _session_key(entry.get("data") or {})),
                    None,
                )
                if not owner or not os.path.exists(_session_path(owner)):
                    os.remove(path)
            except OSError:
                continue


def _flush_stale_sessions(tracer) -> None:
    """Emit ``gen_ai.client.session`` root spans for stale sessions that were never closed.

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

    session_limit = _stale_flush_session_limit()
    if session_limit <= 0:
        return

    cutoff = time.time() - ttl
    flushed_any = False
    paths = []
    for name in os.listdir(_SESSION_DIR):
        path = os.path.join(_SESSION_DIR, name)
        try:
            mtime = os.path.getmtime(path)
            if os.path.isfile(path) and mtime < cutoff:
                paths.append((mtime, path))
        except OSError:
            continue

    for _mtime, path in sorted(paths)[:session_limit]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                ctx = json.load(fh)
            if not ctx:
                os.remove(path)
                continue
            session_key = os.path.basename(path).removesuffix(".json")
            ide = ctx.get("ide", "unknown")
            if _local_spans_enabled():
                _enable_file_exporter(
                    _local_span_path(session_key),
                    expected_session_key=session_key,
                )
            if _finalize_session(tracer, session_key, ctx, ide):
                flushed_any = True
                _LOGGER.info("Flushed stale session %s", session_key)
        except Exception:
            continue
    if flushed_any:
        _LOGGER.info("Finished stale-session finalization")


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
            "gen_ai.client.mcp_server": "gitlab-mcp",
            "gen_ai.client.mcp_tool": "search_gitlab",
            "gen_ai.client.mcp.input": "{...}",
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
    if _safe_bool(os.getenv("IDE_OTEL_SKIP_MDM", "")):
        mdm = {key: value for key, value in mdm.items() if key in {"capture", "logs"}}
    if mdm:
        config = merge_settings(config, mdm)
        config["_managed"] = mdm
    return config


def _apply_config_env(config: dict) -> None:
    global _CAPTURE_POLICY
    managed = config.get("_managed", {})
    settings = environment_settings(config)
    settings.update(environment_settings(managed))
    for key, value in settings.items():
        os.environ.setdefault(key, _coerce_env_value(key, value))
    # Managed capture/log restrictions cannot be loosened by local environment.
    restrictions = {key: value for key, value in managed.items() if key in {"capture", "logs"}}
    if isinstance(restrictions.get("capture"), dict) and "mode" in restrictions["capture"]:
        # A managed selected-content mode with no include list permits metadata
        # only; do not inherit a user's content allowlist.
        restrictions["capture"] = {"include": [], **restrictions["capture"]}
    for key, value in environment_settings(restrictions).items():
        os.environ[key] = _coerce_env_value(key, value)
    effective = merge_settings(config, restrictions)
    _CAPTURE_POLICY = CapturePolicy.from_config(effective)
    for suffix in ("CAPTURE_TEXT", "CAPTURE_CONVERSATION_CONTENT", "CAPTURE_TOOL_INPUT_CONTENT", "CAPTURE_TOOL_DEFINITIONS"):
        os.environ[f"IDE_OTEL_{suffix}"] = str(_CAPTURE_POLICY.mode != "metadata_only")
    # Keep old queues separate so stricter policy never replays old content.
    # Local span paths stay stable for Reflect's existing watcher.
    global _SESSION_DIR, _BATCH_DIR, _LOCK_DIR
    policy_dir = os.path.join(_STATE_DIR, "capture", _CAPTURE_POLICY.fingerprint)
    _SESSION_DIR = os.path.join(policy_dir, "sessions")
    _BATCH_DIR = os.path.join(policy_dir, "batches")
    _LOCK_DIR = os.path.join(policy_dir, "locks")


def _sanitized_exporter_endpoint(endpoint: Optional[str]) -> Optional[str]:
    if not isinstance(endpoint, str) or not endpoint.strip():
        return None
    value = endpoint.strip()
    parsed = urllib.parse.urlsplit(value if "://" in value else f"grpc://{value}")
    if not parsed.hostname:
        return None
    scheme = parsed.scheme if "://" in value else "grpc"
    authority = parsed.hostname
    try:
        if parsed.port:
            authority = f"{authority}:{parsed.port}"
    except ValueError:
        return None
    return f"{scheme}://{authority}"


def _load_delivery_health() -> dict:
    try:
        with open(_DELIVERY_HEALTH_PATH, "r", encoding="utf-8") as fh:
            value = json.load(fh)
            return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _record_delivery_health(signal: str, success: bool, error: Optional[BaseException] = None) -> None:
    """Persist bounded, sanitized exporter health for ``otel-hook doctor``."""
    try:
        with _acquire_lock(os.path.join(_LOCK_DIR, "delivery_health.lock")):
            health = _load_delivery_health()
            health["schema_version"] = 1
            health["updated_at_ns"] = time.time_ns()
            signals = health.setdefault("signals", {})
            record = signals.setdefault(signal, {})
            key = "last_success_at_ns" if success else "last_failure_at_ns"
            record[key] = time.time_ns()
            record["endpoint"] = _sanitized_exporter_endpoint(
                _derive_logs_endpoint() if signal == "logs" else os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
            )
            if success:
                record.pop("last_error", None)
            elif error is not None:
                error_text = str(error)
                record["last_error"] = {
                    "type": type(error).__name__,
                    "message_length": len(error_text),
                    "message_sha256": _hash_text(error_text),
                }
            else:
                record["last_error"] = {"type": "ExportFailure"}
            _atomic_write_json(_DELIVERY_HEALTH_PATH, health)
    except (OSError, TimeoutError):
        pass


class _DiagnosticExporter:
    """Transparent exporter decorator that records delivery success/failure."""

    def __init__(self, delegate, signal: str) -> None:
        self._delegate = delegate
        self._signal = signal

    def export(self, items):
        try:
            result = self._delegate.export(items)
            success = result == SpanExportResult.SUCCESS or getattr(result, "name", "") == "SUCCESS"
            if not success:
                _EXPORT_FAILURES.add(self._signal)
            _record_delivery_health(self._signal, success)
            return result
        except Exception as exc:
            _EXPORT_FAILURES.add(self._signal)
            _record_delivery_health(self._signal, False, exc)
            raise

    def shutdown(self, *args, **kwargs):
        return self._delegate.shutdown(*args, **kwargs)

    def force_flush(self, *args, **kwargs):
        try:
            result = self._delegate.force_flush(*args, **kwargs)
            _record_delivery_health(self._signal, result is not False)
            return result
        except Exception as exc:
            _record_delivery_health(self._signal, False, exc)
            raise


# ---------------------------------------------------------------------------
# Tracing init — pure OpenTelemetry SDK
# ---------------------------------------------------------------------------
def _hook_distro_attributes() -> dict:
    attrs = {"telemetry.distro.name": "opentelemetry-hooks"}
    try:
        attrs["telemetry.distro.version"] = importlib.metadata.version("opentelemetry-hooks")
    except importlib.metadata.PackageNotFoundError:
        pass
    return attrs


def _init_sdk_tracer_provider(resource_attrs: dict, disable_batch: bool) -> bool:
    """Configure the OTel SDK TracerProvider with OTLP exporter.

    When no OTLP endpoint is configured and local spans are enabled,
    creates a bare TracerProvider (no OTLP exporter) so the file exporter
    can be attached later without wasted network calls.
    """
    if not _load_otel_modules():
        _LOGGER.error("opentelemetry-sdk not importable")
        return False

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
        sdk_provider = SDKTracerProvider(resource=Resource.create(resource_attrs), shutdown_on_exit=_EXPORT_DEADLINE is None)
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

    exporter = _DiagnosticExporter(exporter, "traces")

    sdk_provider = SDKTracerProvider(resource=Resource.create(resource_attrs), shutdown_on_exit=_EXPORT_DEADLINE is None)
    if disable_batch and _EXPORT_DEADLINE is None:
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

    exporter = _DiagnosticExporter(exporter, "logs")

    resource = Resource.create(resource_attrs)
    logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=_EXPORT_DEADLINE is None)
    if disable_batch and _EXPORT_DEADLINE is None:
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
    global _CONSOLE_EXPORTER_REGISTERED
    if _CONSOLE_EXPORTER_REGISTERED:
        return
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
    except ImportError as exc:
        _LOGGER.warning("Console exporter unavailable: %s", exc)
        return
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        _CONSOLE_EXPORTER_REGISTERED = True


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
    resource = getattr(span, "resource", None)
    resource_attributes = dict(getattr(resource, "attributes", {}) or {})
    parent_id = None
    parent_ctx = span.parent
    if parent_ctx is not None and getattr(parent_ctx, "span_id", 0) != 0:
        parent_id = format(parent_ctx.span_id, "016x")
    links = []
    for link in getattr(span, "links", ()) or ():
        link_context = getattr(link, "context", None)
        if link_context is None or not getattr(link_context, "is_valid", False):
            continue
        links.append({
            "trace_id": format(link_context.trace_id, "032x"),
            "span_id": format(link_context.span_id, "016x"),
            "attributes": dict(getattr(link, "attributes", {}) or {}),
        })
    return {
        "name": span.name,
        "trace_id": format(ctx.trace_id, "032x") if ctx else None,
        "span_id": format(ctx.span_id, "016x") if ctx else None,
        "parent_span_id": parent_id,
        "start_time_ns": span.start_time,
        "end_time_ns": span.end_time,
        "attributes": dict(span.attributes or {}),
        "resource": resource_attributes,
        "links": links,
        "status": span.status.status_code.name if span.status else None,
    }


class _FileSpanExporter:
    """OTel SpanExporter that appends spans as JSONL to a file."""

    def __init__(self, path: str, expected_session_key: Optional[str] = None) -> None:
        self._path = path
        self._expected_session_key = expected_session_key
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
                        if self._expected_session_key is not None:
                            attributes = dict(span.attributes or {})
                            if attributes.get("gen_ai.client.session_id") != self._expected_session_key:
                                continue
                        fh.write(json.dumps(_span_to_dict(span), ensure_ascii=True, default=str) + "\n")
            return SpanExportResult.SUCCESS
        except OSError as exc:
            _LOGGER.debug("file span exporter write failed: %s", exc)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _enable_file_exporter(path: str, expected_session_key: Optional[str] = None) -> None:
    """Add a file span exporter to the TracerProvider for local span persistence."""
    if path in _FILE_EXPORTER_PATHS:
        return
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except ImportError as exc:
        _LOGGER.warning("File exporter unavailable: %s", exc)
        return
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(
            SimpleSpanProcessor(
                _FileSpanExporter(path, expected_session_key=expected_session_key),
            )
        )
        _FILE_EXPORTER_PATHS.add(path)


def _flush_signal(provider, signal: str, timeout_millis: int) -> bool:
    """Bound SDK flush waits for short hooks, including SDKs ignoring timeout."""
    result = [False]

    def flush() -> None:
        try:
            result[0] = not hasattr(provider, "force_flush") or provider.force_flush(
                timeout_millis=timeout_millis,
            ) is not False
        except Exception as exc:
            _LOGGER.warning("%s force_flush failed: %s", signal, exc)

    if _EXPORT_DEADLINE is None:
        flush()
    else:
        remaining = min(timeout_millis / 1000, _EXPORT_DEADLINE - time.monotonic())
        if remaining <= 0:
            return False
        worker = threading.Thread(target=flush, daemon=True)
        worker.start()
        worker.join(remaining)
        if worker.is_alive():
            return False
    # SDK flush success only means the queue drained, not that export succeeded.
    return result[0] and signal not in _EXPORT_FAILURES


def _force_flush_provider(
    timeout_millis: int = 500,
    *,
    authoritative_signal: Optional[str] = None,
) -> bool:
    """Flush the SDK TracerProvider and LoggerProvider to push pending data.

    Short Codex hooks enforce the timeout independently of SDK behavior.
    Batch lifecycle callers use traces as the
    authoritative signal so a best-effort log failure cannot replay spans that
    were already exported successfully.
    """
    trace_success = _flush_signal(trace.get_tracer_provider(), "traces", timeout_millis)
    log_success = True
    if _LOGS_INITIALIZED:
        from opentelemetry._logs import get_logger_provider
        log_success = _flush_signal(get_logger_provider(), "logs", timeout_millis)
    if authoritative_signal == "traces":
        return trace_success
    if authoritative_signal == "logs":
        return log_success
    return trace_success and log_success


def _init_tracing(
    ide: str = "cursor",
    client_name: Optional[str] = None,
    resource_attributes: Optional[dict] = None,
) -> bool:
    global _TRACING_INITIALIZED
    if _TRACING_INITIALIZED:
        return True
    if not _load_otel_modules():
        _LOGGER.error("opentelemetry-sdk not installed; tracing disabled.")
        return False

    service_name = os.getenv("IDE_OTEL_SERVICE_NAME")
    if service_name and not os.getenv("OTEL_SERVICE_NAME"):
        os.environ["OTEL_SERVICE_NAME"] = service_name

    app_name = os.getenv("IDE_OTEL_APP_NAME") or os.getenv("OTEL_SERVICE_NAME") or "ide-agent"
    resolved_client_name = client_name or ide
    resource_attrs = _parse_resource_attributes(os.getenv("OTEL_RESOURCE_ATTRIBUTES", ""))
    if isinstance(resource_attributes, dict):
        for key, value in resource_attributes.items():
            if key and value is not None:
                resource_attrs.setdefault(key, value)
    resource_attrs.setdefault("service.name", app_name)
    resource_attrs.setdefault("gen_ai.client.name", resolved_client_name)
    resource_attrs.setdefault("gen_ai.system", resolved_client_name)
    if ide != resolved_client_name:
        resource_attrs.setdefault("gen_ai.client.wrapper", ide)
    resource_attrs.update(_hook_distro_attributes())
    resource_attrs.setdefault("gen_ai.client.telemetry_source", _TELEMETRY_SOURCE)
    resource_attrs.setdefault("gen_ai.client.hook_schema_version", _HOOK_SCHEMA_VERSION)

    # OS / host resource attributes (OTel semantic conventions)
    os_info = _get_os_info()
    for attr_key, attr_val in os_info.items():
        resource_attrs.setdefault(attr_key, attr_val)

    resource_attrs_str = ",".join(f"{k}={v}" for k, v in resource_attrs.items())
    os.environ["OTEL_RESOURCE_ATTRIBUTES"] = resource_attrs_str
    _LOGGER.debug("Set OTEL_RESOURCE_ATTRIBUTES: %s", resource_attrs_str)

    disable_batch = _safe_bool(os.getenv("IDE_OTEL_DISABLE_BATCH", ""))

    if not _init_sdk_tracer_provider(resource_attrs, disable_batch):
        return False

    # Init OTel Logs (LoggerProvider + OTLP log exporter)
    if _safe_bool(os.getenv("IDE_OTEL_ENABLE_LOGS", "false")):
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


def _attach_content_fact(span, label: str, text: str, *, capture: bool) -> None:
    if not text:
        return
    span.set_attribute(f"gen_ai.client.{label}.length", len(text))
    span.set_attribute(f"gen_ai.client.{label}.sha256", _hash_text(text))
    if not capture:
        return
    if _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")):
        text = _mask_text(text)
    span.set_attribute(f"gen_ai.client.{label}.text", text[:_text_max_chars()])


def _conversation_records_from_data(data: dict) -> tuple[ConversationRecord, ...]:
    records = []
    for item in data.get("_conversation_records") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        role = item.get("role")
        text = item.get("text")
        length = item.get("length")
        sha256 = item.get("sha256")
        if not isinstance(kind, str) or not isinstance(role, str):
            continue
        if isinstance(text, str) and text:
            records.append(ConversationRecord.from_text(kind, role, text))
        elif isinstance(length, int) and length >= 0 and isinstance(sha256, str) and sha256:
            records.append(ConversationRecord(kind, role, None, length, sha256))
    return tuple(records)


def _apply_conversation_attributes(span, event_name: str, data: dict) -> None:
    records = _conversation_records_from_data(data)
    if not records:
        records = tuple(ProviderEventAdapter()._conversation_records(event_name, data))
    if records:
        span.set_attribute("gen_ai.client.message.types", [record.kind for record in records])
        span.set_attribute("gen_ai.client.message.roles", [record.role for record in records])
    for record in records:
        span.set_attribute(f"gen_ai.client.{record.kind}.length", record.length)
        span.set_attribute(f"gen_ai.client.{record.kind}.sha256", record.sha256)
        if _conversation_content_enabled() and record.text is not None:
            _attach_content_fact(span, record.kind, record.text, capture=True)

    error = data.get("error")
    if isinstance(error, dict):
        _set_if_present(span, "error.type", _first_present(error, ("type", "error_type", "name")))
        _set_if_present(span, "error.code", _first_present(error, ("code", "error_code")))
    _set_if_present(span, "error.type", data.get("error_type"))
    _set_if_present(span, "error.code", data.get("error_code"))
    if event_name in {"ErrorOccurred", "PostToolUseFailure"} or error is not None:
        span.set_attribute(
            "gen_ai.client.status",
            "interrupted" if data.get("is_interrupt") else "error",
        )


def _apply_operation_status(span, event_name: str, data: dict) -> None:
    """Set OTel status for real failures without leaking error descriptions."""
    if data.get("is_interrupt"):
        return
    status = _lower_or_none(data.get("status") or data.get("mcp_status"))
    is_error = bool(
        event_name in {"ErrorOccurred", "PostToolUseFailure"}
        or data.get("error") is not None
        or status in {"error", "failed", "failure"}
    )
    if is_error and Status is not None and StatusCode is not None and hasattr(span, "set_status"):
        span.set_status(Status(StatusCode.ERROR))


def _maybe_attach_text(span, label: str, text: str) -> None:
    _attach_content_fact(
        span,
        label,
        text,
        capture=_safe_bool(os.getenv("IDE_OTEL_CAPTURE_TEXT", "")),
    )


# ---------------------------------------------------------------------------
# Correlated hook log emission
# ---------------------------------------------------------------------------
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


def _emit_event_log(event_name: str, data: dict) -> None:
    """Export one policy-filtered JSON body under the active hook span."""
    body = data.get("_hook_log_body")
    if not _LOGS_INITIALIZED or body is None:
        return
    attrs = {
        "gen_ai.client.hook.event": event_name,
        "gen_ai.client.telemetry_source": _TELEMETRY_SOURCE,
    }
    for source, target in (("_hook_event_id", "hook.event_id"), ("session_id", "session_id"),
                           ("conversation_id", "conversation_id"), ("turn_id", "turn_id"),
                           ("generation_id", "generation_id")):
        if data.get(source) is not None:
            attrs[f"gen_ai.client.{target}"] = data[source]
    _inject_trace_context(attrs)
    _get_otel_logger("hooks").info(body, extra=attrs)


# ---------------------------------------------------------------------------
# IDE detection and event normalization
# ---------------------------------------------------------------------------

def _detect_ide_from_process_tree() -> Optional[str]:
    """Walk the parent process chain to detect the outermost IDE.

    This is the most reliable detection method because it inspects the
    actual process hierarchy rather than relying on environment variables
    (which leak across embedded agents) or payload heuristics.

    Typical process chains:
      Cursor session:  otel-hook → node → Cursor Helper → Cursor.app
      Claude terminal: otel-hook → claude → zsh → iTerm/Terminal
      VS Code:         otel-hook → node → Code Helper → Code.app

    Not cached — each hook invocation is a fresh process so there is no
    reuse concern.  The walk costs ~10 ms (5–8 ps calls), negligible
    compared to Python startup.
    """
    # Managed registrations identify their source explicitly. Windows has no ps;
    # unmanaged hooks continue through the environment and payload fallbacks.
    if sys.platform == "win32":
        return None
    try:
        import subprocess as _sp
        pid = os.getpid()
        for _ in range(20):
            if not pid or pid <= 1:
                break
            out = _sp.check_output(
                ["ps", "-p", str(pid), "-o", "ppid=,comm="],
                text=True, timeout=2,
            ).strip()
            if not out:
                break
            parts = out.split(None, 1)
            ppid = int(parts[0])
            comm = parts[1].lower() if len(parts) > 1 else ""

            if "cursor" in comm:
                return "cursor"
            if "opencode" in comm:
                return "opencode"
            if comm.endswith("codex") or comm.endswith("/codex") or "codex" in comm:
                return "codex"
            if ("code helper" in comm or comm.endswith("/code") or
                    "visual studio code" in comm):
                return "copilot"
            if comm.endswith("claude") or comm.endswith("/claude"):
                return "claude"

            pid = ppid
    except Exception:
        # Best-effort IDE detection: failures should not break the hook.
        logging.debug("Failed to detect IDE from process tree; returning None.", exc_info=True)
    return None


def _detect_ide(data: dict) -> str:
    """Detect which IDE is calling this hook.

    Detection order (highest to lowest confidence):
    1. Managed hook source flag stamped by `otel-hook setup` (for example --codex)
    2. Managed hook source env from legacy setup-generated configs
    3. Explicit override via IDE_OTEL_IDE_NAME env var
    4. Process tree inspection fallback for legacy/unmanaged hooks
    5. Self-reported payload fields (ide_name, ide, client, source_app)
    6. Heuristic fallback (Claude env/payload/event casing, Cursor fields)
    7. Default: cursor
    """
    # Level 1: explicit source flag stamped into hook command by setup_*.
    cli_source = _normalize_ide_name(_CLI_HOOK_SOURCE)
    if cli_source:
        return cli_source

    # Level 2: env source stamped by older setup_* versions.
    managed_source = _normalize_ide_name(os.getenv(_MANAGED_HOOK_SOURCE_ENV))
    if managed_source:
        return managed_source

    # Level 3: Explicit override from hook config env block.
    override = _normalize_ide_name(os.getenv("IDE_OTEL_IDE_NAME"))
    if override:
        return override

    # Level 4: Process tree — fallback for legacy hooks that predate the source flag.
    ptree_ide = _detect_ide_from_process_tree()
    if ptree_ide:
        return ptree_ide

    # Level 5: Self-reported payload fields.
    payload_client = _detect_payload_client_name(data, include_session_fallback=False)
    if payload_client:
        return payload_client

    # Level 6: Heuristic fallback — Claude-specific signals.
    if os.getenv("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"

    # Level 7: Copilot (session_id without other indicators).
    if data.get("session_id"):
        return "copilot"

    # Default to cursor (most common case)
    return "cursor"


def _get_event_name(data: dict) -> str:
    """Extract raw event name from hook input."""
    if _CLI_HOOK_EVENT:
        return _CLI_HOOK_EVENT
    for key in ("hook_event_name", "hook_event_type", "event", "hook", "agent_action_name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if "prompt" in data:
        return "beforeSubmitPrompt"
    return "stop"


def _normalize_event(event_name: str) -> str:
    """Normalize event name to canonical PascalCase."""
    return _CANONICAL_EVENT.get(event_name, event_name)


# ---------------------------------------------------------------------------
# Session context (cross-process, session-level trace linking)
# ---------------------------------------------------------------------------
def _session_path(session_key: str) -> str:
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_key)
    return os.path.join(_SESSION_DIR, f"{safe_key}.json")


def _new_session_context(data: dict, ide: str) -> dict:
    """Build a new session context without writing it."""
    upstream_ctx = _resolve_upstream_trace_context(data)
    ctx = {
        "phantom_parent_id": f"{random.getrandbits(64):016x}",
        "trace_id": f"{random.getrandbits(128):032x}",
        "start_time_ns": time.time_ns(),
        "ide": ide,
        "generation_count": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "context_origin": "synthetic",
        "root_agent_id": data.get("agent_id") or f"hook:{uuid.uuid4()}",
        "root_agent_id_source": "provider" if data.get("agent_id") else "hook",
    }
    if upstream_ctx is not None:
        ctx["trace_id"] = upstream_ctx["trace_id"]
        ctx["upstream_parent_span_id"] = upstream_ctx["parent_span_id"]
        ctx["trace_flags"] = upstream_ctx.get("trace_flags", "01")
        if upstream_ctx.get("tracestate"):
            ctx["tracestate"] = upstream_ctx["tracestate"]
        ctx["context_origin"] = "upstream"
    if model := _first_present(data, ("request_model", "model", "model_name")):
        ctx["last_known_model"] = model

    repository_context = _resolve_repository_context(data)
    ctx.update(repository_context)
    ctx["repository_context_resolved"] = bool(repository_context.get("repo_root"))

    agent_engine = _resolved_agent_engine(data, ide=ide)
    if agent_engine and agent_engine != ide:
        ctx["agent_engine"] = agent_engine
        ctx["agent_engine_confirmed"] = True
    return ctx


def _session_lock_path(session_key: str) -> str:
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_key)
    return os.path.join(_LOCK_DIR, f"{safe_key}.lock")


def _create_session_context(session_key: str, data: dict, ide: str) -> dict:
    """Idempotently create and persist a session context."""
    os.makedirs(_SESSION_DIR, exist_ok=True)
    lock_path = _session_lock_path(session_key)
    with _acquire_lock(lock_path):
        existing = _load_session_context(session_key)
        if existing:
            return existing
        ctx = _new_session_context(data, ide)
        ctx["session_id"] = session_key
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
    lock_path = _session_lock_path(session_key)
    with _acquire_lock(lock_path):
        _atomic_write_json(_session_path(session_key), ctx)


def _update_session_context(session_key: str, mutator):
    """Atomically reload, mutate, and persist one session record."""
    os.makedirs(_SESSION_DIR, exist_ok=True)
    with _acquire_lock(_session_lock_path(session_key)):
        ctx = _load_session_context(session_key)
        if not ctx:
            return None, None
        result = mutator(ctx)
        _atomic_write_json(_session_path(session_key), ctx)
        return ctx, result


def _maybe_enrich_session_context(session_key: Optional[str], session_ctx: Optional[dict], data: dict) -> Optional[dict]:
    if not session_key or not session_ctx:
        return session_ctx

    def mutate(latest: dict) -> None:
        repo_ctx = _resolve_repository_context(data, session_ctx=latest)
        for key in (
            "repo_root",
            "workspace_path",
            "vcs.repository.owner",
            "vcs.repository.name",
            "vcs.ref.head.name",
            "vcs.ref.head.type",
            "gen_ai.client.repository.remote.sha256",
        ):
            value = repo_ctx.get(key)
            if value:
                latest[key] = value
        if repo_ctx.get("repo_root"):
            latest["repository_context_resolved"] = True
        repo_root = latest.get("repo_root")
        if repo_root and isinstance(latest.get("memory"), dict):
            normalize_memory_summary(latest["memory"], repo_root=repo_root)

    latest, _result = _update_session_context(session_key, mutate)
    return latest or session_ctx


def _maybe_bind_session_to_upstream_context(session_key: Optional[str], session_ctx: Optional[dict], data: dict) -> Optional[dict]:
    if not session_key or not session_ctx or session_ctx.get("context_origin") == "upstream":
        return session_ctx
    upstream_ctx = _resolve_upstream_trace_context(data)
    if upstream_ctx is None:
        return session_ctx

    def mutate(latest: dict) -> None:
        if latest.get("context_origin") == "upstream":
            return
        latest["trace_id"] = upstream_ctx["trace_id"]
        latest["upstream_parent_span_id"] = upstream_ctx["parent_span_id"]
        latest["trace_flags"] = upstream_ctx.get("trace_flags", "01")
        if upstream_ctx.get("tracestate"):
            latest["tracestate"] = upstream_ctx["tracestate"]
        else:
            latest.pop("tracestate", None)
        latest["context_origin"] = "upstream"

    latest, _result = _update_session_context(session_key, mutate)
    return latest or session_ctx


def _clear_session_context(session_key: Optional[str]) -> None:
    if not session_key:
        return
    try:
        path = _session_path(session_key)
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _remember_pending_generation(session_ctx: dict, gen_key: str, *, make_current: bool = True) -> str:
    pending = session_ctx.setdefault("pending_generations", [])
    if gen_key not in pending:
        pending.append(gen_key)
        session_ctx["generation_count"] = session_ctx.get("generation_count", 0) + 1
    if make_current:
        session_ctx["current_generation"] = gen_key
    return gen_key


def _new_generation_key(session_key: str, session_ctx: dict) -> str:
    count = session_ctx.get("generation_count", 0) + 1
    gen_key = f"{session_key}_gen_{count}"
    session_ctx["generation_count"] = count
    pending = session_ctx.setdefault("pending_generations", [])
    if gen_key not in pending:
        pending.append(gen_key)
    session_ctx["current_generation"] = gen_key
    return gen_key


def _advance_generation(session_key: str, session_ctx: dict) -> str:
    """Atomically start a new generation within the session."""
    _ctx, gen_key = _update_session_context(
        session_key,
        lambda latest: _new_generation_key(session_key, latest),
    )
    if gen_key:
        return gen_key
    return _new_generation_key(session_key, session_ctx)


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


@dataclass(slots=True)
class GovernanceResponse:
    """Governance-oriented response fields that adapters can project to stdout later."""

    continue_: Optional[bool] = None
    stop_reason: Optional[str] = None
    system_message: Optional[str] = None
    suppress_output: Optional[bool] = None
    hook_specific_output: Optional[dict] = None


class HookResponseAdapter:
    """Runner-specific adapter for stdout response envelopes."""

    def build_payload(
        self,
        event_name: str,
        data: dict,
        governance: Optional[GovernanceResponse] = None,
    ) -> Optional[dict]:
        payload = {"continue": True}
        if _local_spans_configured():
            payload["local_spans"] = _local_spans_enabled()
        return self._merge_governance(payload, governance)

    @staticmethod
    def _merge_governance(payload: Optional[dict], governance: Optional[GovernanceResponse]) -> Optional[dict]:
        if governance is None:
            return payload
        merged = dict(payload or {})
        if governance.continue_ is not None:
            merged["continue"] = governance.continue_
        if governance.stop_reason is not None:
            merged["stopReason"] = governance.stop_reason
        if governance.system_message is not None:
            merged["systemMessage"] = governance.system_message
        if governance.suppress_output is not None:
            merged["suppressOutput"] = governance.suppress_output
        if governance.hook_specific_output is not None:
            merged["hookSpecificOutput"] = governance.hook_specific_output
        return merged


class CursorHookResponseAdapter(HookResponseAdapter):
    """Cursor permission hooks require a decision, not a continue envelope."""

    _PERMISSION_EVENTS = {"PreToolUse", "SubagentStart", "BeforeShellExecution", "BeforeMCPExecution", "BeforeReadFile"}

    def build_payload(self, event_name, data, governance=None):
        if event_name in self._PERMISSION_EVENTS:
            payload = {"permission": "deny" if governance and governance.continue_ is False else "allow"}
            if governance and governance.stop_reason:
                payload["user_message"] = governance.stop_reason
            return payload
        return super().build_payload(event_name, data, governance)


class CodexHookResponseAdapter(HookResponseAdapter):
    """Codex has event-specific stdout contracts that differ from the default envelope."""

    # Only Stop uses the default continuation envelope; other events are passive.
    _JSON_RESPONSE_EVENTS = frozenset({"Stop"})
    _SILENT_SUCCESS_EVENTS = frozenset(set(_CODEX_EVENTS).difference(_JSON_RESPONSE_EVENTS))

    def build_payload(
        self,
        event_name: str,
        data: dict,
        governance: Optional[GovernanceResponse] = None,
    ) -> Optional[dict]:
        if governance is None:
            if event_name in self._JSON_RESPONSE_EVENTS:
                return {"continue": True}
            if event_name in self._SILENT_SUCCESS_EVENTS:
                return None
            return super().build_payload(event_name, data, governance)

        base_payload = {"continue": True} if event_name in self._JSON_RESPONSE_EVENTS else {}
        return self._merge_governance(base_payload, governance)


_DEFAULT_HOOK_RESPONSE_ADAPTER = HookResponseAdapter()


class GeminiHookResponseAdapter(HookResponseAdapter):
    def build_payload(self, event_name, data, governance=None):
        if event_name == "BeforeToolSelection":
            return {}  # This event accepts only toolConfig, not continue/decision.
        return super().build_payload(event_name, data, governance)


_HOOK_RESPONSE_ADAPTERS = {
    "cursor": CursorHookResponseAdapter(),
    "codex": CodexHookResponseAdapter(),
    "gemini": GeminiHookResponseAdapter(),
}


def _response_adapter_for(ide: str) -> HookResponseAdapter:
    return _HOOK_RESPONSE_ADAPTERS.get(ide, _DEFAULT_HOOK_RESPONSE_ADAPTER)


def _stdout_response(
    event_name: str,
    ide: str,
    data: dict,
    governance: Optional[GovernanceResponse] = None,
) -> Optional[str]:
    payload = _response_adapter_for(ide).build_payload(event_name, data, governance)
    if payload is None:
        return None
    return json.dumps(payload)


def _emit_stdout_response(
    event_name: str,
    ide: str,
    data: dict,
    governance: Optional[GovernanceResponse] = None,
) -> None:
    response = _stdout_response(event_name, ide, data, governance)
    if response is not None:
        print(response)


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


@dataclass(slots=True)
class _BufferedEventDecision:
    generation_key: Optional[str]
    data: dict
    duplicate: bool = False
    correlated: bool = False


def _event_tool_key(data: dict) -> Optional[str]:
    _server, mcp_tool = _mcp_identity(data)
    value = mcp_tool or data.get("tool_name")
    if not isinstance(value, str) or not value:
        return None
    return value


def _prune_session_event_state(session_ctx: dict, now_ns: int) -> None:
    cutoff_ns = now_ns - max(1, _state_ttl_seconds()) * 1_000_000_000
    seen = session_ctx.get("seen_events")
    if isinstance(seen, list):
        session_ctx["seen_events"] = [
            entry for entry in seen[-_SESSION_EVENT_LIMIT:]
            if isinstance(entry, dict) and entry.get("seen_at_ns", now_ns) >= cutoff_ns
        ]
    invocations = session_ctx.get("tool_invocations")
    if isinstance(invocations, list):
        session_ctx["tool_invocations"] = [
            entry for entry in invocations[-_SESSION_INVOCATION_LIMIT:]
            if isinstance(entry, dict) and entry.get("updated_at_ns", now_ns) >= cutoff_ns
        ]
    agents = session_ctx.get("agent_invocations")
    if isinstance(agents, list):
        session_ctx["agent_invocations"] = [
            entry for entry in agents[-_AGENT_INVOCATION_LIMIT:]
            if isinstance(entry, dict) and entry.get("updated_at_ns", now_ns) >= cutoff_ns
        ]


def _mark_event_seen(
    session_ctx: dict,
    key: str,
    now_ns: int,
    generation_key: Optional[str],
    *,
    window_seconds: Optional[float] = None,
) -> bool:
    """Return True for a duplicate and remember new observations in bounded state."""
    _prune_session_event_state(session_ctx, now_ns)
    seen = session_ctx.setdefault("seen_events", [])
    for entry in reversed(seen):
        if entry.get("key") != key:
            continue
        if window_seconds is None:
            return True
        age_ns = now_ns - entry.get("seen_at_ns", now_ns)
        if age_ns <= int(window_seconds * 1_000_000_000):
            return True
        break
    seen.append({"key": key, "seen_at_ns": now_ns, "generation_key": generation_key})
    del seen[:-_SESSION_EVENT_LIMIT]
    return False


class SessionEventDeduplicator:
    """Own bounded callback idempotency independently of invocation matching."""

    def __init__(self, session_ctx: dict, generation_key: Optional[str], now_ns: int) -> None:
        self.session_ctx = session_ctx
        self.generation_key = generation_key
        self.now_ns = now_ns

    def seen(self, key: str, *, window_seconds: Optional[float] = None) -> bool:
        return _mark_event_seen(
            self.session_ctx,
            key,
            self.now_ns,
            self.generation_key,
            window_seconds=window_seconds,
        )

    def is_duplicate(
        self,
        event_name: str,
        data: dict,
        invocation_id: Optional[str],
    ) -> bool:
        """Apply event-specific keys without owning correlation state."""
        if invocation_id:
            return self.seen(f"tool:{event_name}:{invocation_id}")
        if event_name in _GENERATION_END_EVENTS and self.generation_key:
            return self.seen(f"generation-end:{event_name}:{self.generation_key}")
        if event_name not in _MCP_EVENTS:
            event_id = data.get("_hook_event_id")
            if event_id and event_name in {
                "ErrorOccurred",
                "PostToolUseFailure",
                "PreCompact",
                "PostCompact",
                "PermissionRequest",
            }:
                window = None if data.get("_hook_event_id_source") == "provider" else 1.0
                return self.seen(
                    f"event:{event_name}:{self.generation_key}:{event_id}",
                    window_seconds=window,
                )
            return False
        return self.seen(
            _no_id_mcp_fingerprint(event_name, data, self.generation_key),
            window_seconds=0.25,
        )


class SubagentInvocationCorrelator:
    """Correlate concurrent subagent callbacks before applying deduplication."""

    def __init__(
        self,
        session_ctx: dict,
        generation_key: Optional[str],
        now_ns: int,
    ) -> None:
        self.session_ctx = session_ctx
        self.generation_key = generation_key
        self.now_ns = now_ns
        self.deduplicator = SessionEventDeduplicator(session_ctx, generation_key, now_ns)

    @property
    def invocations(self) -> list:
        return self.session_ctx.setdefault("agent_invocations", [])

    def prepare(self, event_name: str, data: dict) -> tuple[dict, bool]:
        if event_name not in {"SubagentStart", "SubagentStop"}:
            return data, False
        prepared = dict(data)
        callback_id = prepared.get("_hook_event_id")
        if prepared.get("_hook_event_id_source") == "provider" and callback_id:
            if self.deduplicator.seen(f"subagent-callback:{event_name}:{callback_id}"):
                return prepared, True

        agent_id = prepared.get("agent_id")
        if agent_id and self.deduplicator.seen(f"subagent:{event_name}:{agent_id}"):
            return prepared, True
        if event_name == "SubagentStart":
            return self._start(prepared, agent_id), False
        return self._stop(prepared, agent_id)

    def _start(self, prepared: dict, agent_id: Optional[str]) -> dict:
        if not agent_id:
            agent_id = f"hook:{uuid.uuid4()}"
            prepared["agent_id"] = agent_id
            prepared["agent_id_source"] = "hook"
        else:
            prepared["agent_id_source"] = "provider"
        parent_id = prepared.get("parent_agent_id") or self.session_ctx.get("root_agent_id")
        if parent_id:
            prepared["parent_agent_id"] = parent_id
        agent_type = _first_present(prepared, ("subagent_type", "agent_type", "agent_name"))
        task = _first_present(prepared, ("delegation_task", "subagent_task", "task"))
        self.invocations.append({
            "agent_id": agent_id,
            "agent_id_source": prepared.get("agent_id_source"),
            "parent_agent_id": parent_id,
            "agent_type": agent_type,
            "task": _stringify(task) if task is not None else None,
            "generation_key": self.generation_key,
            "state": "open",
            "started_at_ns": self.now_ns,
            "updated_at_ns": self.now_ns,
        })
        del self.invocations[:-_AGENT_INVOCATION_LIMIT]
        return prepared

    def _matches(self, invocation: dict, agent_id: Optional[str], agent_type, task) -> bool:
        return bool(
            invocation.get("state") == "open"
            and (not agent_id or invocation.get("agent_id") == agent_id)
            and (not agent_type or invocation.get("agent_type") in (None, agent_type))
            and (task is None or invocation.get("task") in (None, _stringify(task)))
        )

    def _stop(self, prepared: dict, agent_id: Optional[str]) -> tuple[dict, bool]:
        agent_type = _first_present(prepared, ("subagent_type", "agent_type", "agent_name"))
        task = _first_present(prepared, ("delegation_task", "subagent_task", "task"))
        candidates = [
            item for item in self.invocations if self._matches(item, agent_id, agent_type, task)
        ]
        if not candidates:
            recent_cutoff = self.now_ns - 1_000_000_000
            recently_closed = any(
                item.get("state") == "closed"
                and item.get("updated_at_ns", 0) >= recent_cutoff
                and (not agent_id or item.get("agent_id") == agent_id)
                and (not agent_type or item.get("agent_type") in (None, agent_type))
                and (task is None or item.get("task") in (None, _stringify(task)))
                for item in self.invocations
            )
            if recently_closed:
                return prepared, True
            event_id = prepared.get("_hook_event_id")
            if event_id:
                return prepared, self.deduplicator.seen(
                    f"subagent-unmatched-stop:{self.generation_key}:{event_id}",
                    window_seconds=1.0,
                )
            return prepared, False

        invocation = candidates[0]
        prepared.setdefault("agent_id", invocation.get("agent_id"))
        prepared.setdefault("agent_id_source", invocation.get("agent_id_source"))
        prepared.setdefault("parent_agent_id", invocation.get("parent_agent_id"))
        if task is None and invocation.get("task") is not None:
            prepared["delegation_task"] = invocation["task"]
        invocation["state"] = "closed"
        invocation["updated_at_ns"] = self.now_ns
        invocation["status"] = prepared.get("status") or "success"
        return prepared, False
def _no_id_mcp_fingerprint(event_name: str, data: dict, generation_key: Optional[str]) -> str:
    server, tool = _mcp_identity(data)
    result = _first_present(data, ("result_json", "mcp_output", "tool_output", "output"))
    result_hash = _hash_text(_stringify(result)) if result is not None else ""
    error = data.get("error")
    error_hash = _hash_text(str(error)) if error is not None else ""
    supplied_time = _first_present(data, ("timestamp_ns", "timestamp", "event_time"))
    payload = "|".join(str(value or "") for value in (
        event_name,
        server,
        tool,
        generation_key,
        data.get("duration_ms") or data.get("duration"),
        data.get("status"),
        result_hash,
        error_hash,
        supplied_time,
    ))
    return f"mcp-fingerprint:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _store_mcp_result(invocation: dict, data: dict) -> None:
    duration = _first_present(data, ("duration_ms", "duration"))
    if duration is not None:
        invocation["duration_ms"] = duration
    if data.get("status") is not None:
        invocation["mcp_status"] = data.get("status")
    if data.get("error") is not None:
        error_text = _stringify(data.get("error"))
        invocation["error_length"] = len(error_text)
        invocation["error_sha256"] = _hash_text(error_text)
        if _conversation_content_enabled():
            invocation["error"] = error_text
        invocation["mcp_status"] = "error"
    result = _first_present(data, ("result_json", "mcp_output", "tool_output", "output"))
    if result is not None:
        text = _stringify(result)
        invocation["mcp_result_length"] = len(text)
        invocation["mcp_result_sha256"] = _hash_text(text)
        invocation["result_json"] = result


def _merge_invocation_data(data: dict, invocation: dict) -> dict:
    merged = dict(data)
    for key in (
        "tool_use_id",
        "mcp_server",
        "mcp_tool",
        "duration_ms",
        "mcp_status",
        "error",
        "error_length",
        "error_sha256",
        "mcp_result_length",
        "mcp_result_sha256",
        "result_json",
    ):
        value = invocation.get(key)
        if value is not None and merged.get(key) is None:
            merged[key] = value
    if merged.get("status") is None and invocation.get("mcp_status") is not None:
        merged["status"] = invocation["mcp_status"]
    return merged


def _matching_open_tools(
    session_ctx: dict,
    data: dict,
    generation_key: Optional[str],
    *,
    mcp_only: bool = False,
    after_mcp: bool = False,
) -> list:
    tool_key = _event_tool_key(data)
    server, _tool = _mcp_identity(data)
    candidates = []
    for invocation in session_ctx.get("tool_invocations", []):
        if invocation.get("state") != "open":
            continue
        if mcp_only and not invocation.get("is_mcp"):
            continue
        if tool_key and invocation.get("tool_key") != tool_key:
            continue
        if generation_key and invocation.get("generation_key") != generation_key:
            continue
        if data.get("turn_id") and invocation.get("turn_id") != data.get("turn_id"):
            continue
        if server and invocation.get("mcp_server") not in (None, server):
            continue
        if after_mcp and not invocation.get("mcp_before_seen"):
            continue
        if after_mcp and invocation.get("mcp_after_seen"):
            continue
        if not after_mcp and invocation.get("mcp_before_seen"):
            continue
        candidates.append(invocation)
    return candidates


class ToolInvocationCorrelator:
    """Own bounded, session-local tool, permission, and MCP correlation state."""

    def __init__(
        self,
        session_ctx: dict,
        ide: str,
        generation_key: Optional[str],
        now_ns: int,
    ) -> None:
        self.session_ctx = session_ctx
        self.ide = ide
        self.generation_key = generation_key
        self.now_ns = now_ns
        self.deduplicator = SessionEventDeduplicator(session_ctx, generation_key, now_ns)

    @property
    def invocations(self) -> list:
        """Return the current session-owned list after any pruning replacement."""
        return self.session_ctx.setdefault("tool_invocations", [])

    def prepare(self, event_name: str, data: dict) -> _BufferedEventDecision:
        prepared = _normalize_mcp_event_data(data, event_name, self.ide)
        if event_name in {"SubagentStart", "SubagentStop"}:
            prepared, duplicate = SubagentInvocationCorrelator(
                self.session_ctx,
                self.generation_key,
                self.now_ns,
            ).prepare(event_name, prepared)
            return self._decision(prepared, duplicate=duplicate)

        invocation_id = prepared.get("tool_use_id")
        if self._is_duplicate(event_name, prepared, invocation_id):
            return self._decision(prepared, duplicate=True)

        if event_name == "PreToolUse" and invocation_id:
            self._record_open_tool(prepared, invocation_id)
        elif event_name == "PermissionRequest" and not invocation_id:
            prepared, duplicate = self._correlate_permission(event_name, prepared)
            if duplicate:
                return self._decision(prepared, duplicate=True, correlated=True)
        elif event_name in _MCP_EVENTS and self.ide == "cursor" and not invocation_id:
            correlated = self._correlate_cursor_mcp(event_name, prepared)
            if correlated is not None:
                return self._decision(correlated, correlated=True)
        elif event_name in {"PostToolUse", "PostToolUseFailure"} and invocation_id:
            prepared = self._close_tool(event_name, prepared, invocation_id)

        return self._decision(prepared)

    def _decision(
        self,
        data: dict,
        *,
        duplicate: bool = False,
        correlated: bool = False,
    ) -> _BufferedEventDecision:
        return _BufferedEventDecision(self.generation_key, data, duplicate=duplicate, correlated=correlated)

    def _is_duplicate(self, event_name: str, data: dict, invocation_id: Optional[str]) -> bool:
        return self.deduplicator.is_duplicate(event_name, data, invocation_id)

    def _record_open_tool(self, data: dict, invocation_id: str) -> None:
        if any(item.get("tool_use_id") == invocation_id for item in self.invocations):
            return
        server, tool = _mcp_identity(data)
        self.invocations.append({
            "tool_use_id": invocation_id,
            "tool_name": data.get("tool_name"),
            "tool_key": _event_tool_key(data),
            "mcp_server": server,
            "mcp_tool": tool,
            "is_mcp": bool(server or tool),
            "generation_key": self.generation_key,
            "turn_id": data.get("turn_id"),
            "state": "open",
            "started_at_ns": self.now_ns,
            "updated_at_ns": self.now_ns,
        })
        del self.invocations[:-_SESSION_INVOCATION_LIMIT]

    def _correlate_permission(self, event_name: str, data: dict) -> Tuple[dict, bool]:
        candidates = _matching_open_tools(self.session_ctx, data, self.generation_key)
        if len(candidates) != 1:
            return data, False
        prepared = dict(data)
        invocation_id = candidates[0]["tool_use_id"]
        prepared["tool_use_id"] = invocation_id
        duplicate = _mark_event_seen(
            self.session_ctx,
            f"tool:{event_name}:{invocation_id}",
            self.now_ns,
            self.generation_key,
        )
        return prepared, duplicate

    def _correlate_cursor_mcp(self, event_name: str, data: dict) -> Optional[dict]:
        candidates = _matching_open_tools(
            self.session_ctx,
            data,
            self.generation_key,
            mcp_only=True,
            after_mcp=event_name == "AfterMCPExecution",
        )
        if event_name == "AfterMCPExecution" and not candidates:
            candidates = _matching_open_tools(
                self.session_ctx,
                data,
                self.generation_key,
                mcp_only=True,
            )
        if not candidates:
            return None

        invocation = candidates[0]
        prepared = dict(data)
        prepared["tool_use_id"] = invocation["tool_use_id"]
        server, tool = _mcp_identity(prepared)
        invocation["mcp_server"] = server or invocation.get("mcp_server")
        invocation["mcp_tool"] = tool or invocation.get("mcp_tool")
        invocation["is_mcp"] = True
        invocation["updated_at_ns"] = self.now_ns
        if event_name == "BeforeMCPExecution":
            invocation["mcp_before_seen"] = True
        else:
            invocation["mcp_after_seen"] = True
            _store_mcp_result(invocation, prepared)
            invocation.setdefault("mcp_status", "success")
        return prepared
    def _close_tool(self, event_name: str, data: dict, invocation_id: str) -> dict:
        invocation = next(
            (item for item in self.invocations if item.get("tool_use_id") == invocation_id),
            None,
        )
        if invocation is None:
            return data
        prepared = _merge_invocation_data(data, invocation)
        invocation["state"] = "closed"
        invocation["updated_at_ns"] = self.now_ns
        if event_name == "PostToolUseFailure":
            invocation["mcp_status"] = "error"
        else:
            invocation.setdefault("mcp_status", "success")
        return prepared


# Backward-compatible name retained for callers that imported the original class.
MCPInvocationCorrelator = ToolInvocationCorrelator


def _buffer_session_event(session_key: str, event_name: str, data: dict, ide: str) -> _BufferedEventDecision:
    """Atomically prepare and append a generation-owned event."""
    now_ns = time.time_ns()

    def mutate(session_ctx: dict) -> _BufferedEventDecision:
        if session_ctx.get("finalizing"):
            return _BufferedEventDecision(None, data, duplicate=True)
        explicit_gen = _generation_key_from_data(data)
        if event_name in _GENERATION_START_EVENTS:
            event_id = data.get("_hook_event_id")
            if (
                session_ctx.get("current_generation")
                and event_id
                and session_ctx.get("active_prompt_event_id") == event_id
            ):
                return _BufferedEventDecision(
                    session_ctx.get("current_generation"),
                    data,
                    duplicate=True,
                )
            gen_key = (
                _remember_pending_generation(session_ctx, explicit_gen)
                if explicit_gen
                else _new_generation_key(session_key, session_ctx)
            )
            session_ctx["active_prompt_event_id"] = event_id
        elif explicit_gen:
            gen_key = _remember_pending_generation(session_ctx, explicit_gen)
        else:
            gen_key = session_ctx.get("current_generation")
            if not gen_key:
                gen_key = _new_generation_key(session_key, session_ctx)

        decision = ToolInvocationCorrelator(session_ctx, ide, gen_key, now_ns).prepare(
            event_name,
            data,
        )
        if not decision.duplicate:
            if decision.correlated:
                decision.data["_mcp_correlated_evidence"] = True
            _append_batch_event(gen_key, event_name, decision.data)
        session_ctx["last_seen_at_ns"] = now_ns
        return decision

    _ctx, decision = _update_session_context(session_key, mutate)
    if decision is None:
        session_ctx = _create_session_context(session_key, data, ide)
        _ctx, decision = _update_session_context(session_key, mutate)
        if decision is None:
            gen_key = _resolve_generation_key(data, session_ctx)
            return _BufferedEventDecision(gen_key, data)
    return decision


def _buffer_generation_end(session_key: str, event_name: str, data: dict, ide: str) -> _BufferedEventDecision:
    """Append a terminal callback, creating an incomplete generation if needed."""
    now_ns = time.time_ns()

    def mutate(session_ctx: dict) -> _BufferedEventDecision:
        if session_ctx.get("finalizing"):
            return _BufferedEventDecision(None, data, duplicate=True)
        gen_key = _generation_key_from_data(data) or session_ctx.get("current_generation")
        if not gen_key:
            pending = session_ctx.get("pending_generations") or []
            gen_key = pending[-1] if pending else None
        if not gen_key:
            gen_key = _new_generation_key(session_key, session_ctx)
        decision = ToolInvocationCorrelator(session_ctx, ide, gen_key, now_ns).prepare(
            event_name,
            data,
        )
        if not decision.duplicate:
            _append_batch_event(gen_key, event_name, decision.data)
        session_ctx["last_seen_at_ns"] = now_ns
        return decision

    _ctx, decision = _update_session_context(session_key, mutate)
    return decision or _BufferedEventDecision(None, data)


def _prepare_streaming_session_event(
    session_key: str,
    event_name: str,
    data: dict,
    ide: str,
) -> _BufferedEventDecision:
    """Apply the same persisted dedupe/correlation policy without writing a batch."""
    now_ns = time.time_ns()

    def mutate(session_ctx: dict) -> _BufferedEventDecision:
        if session_ctx.get("finalizing"):
            return _BufferedEventDecision(None, data, duplicate=True)
        explicit_gen = _generation_key_from_data(data)
        if event_name in _GENERATION_START_EVENTS:
            event_id = data.get("_hook_event_id")
            if (
                session_ctx.get("current_generation")
                and event_id
                and session_ctx.get("active_prompt_event_id") == event_id
            ):
                return _BufferedEventDecision(
                    session_ctx.get("current_generation"),
                    data,
                    duplicate=True,
                )
            gen_key = (
                _remember_pending_generation(session_ctx, explicit_gen)
                if explicit_gen
                else _new_generation_key(session_key, session_ctx)
            )
            session_ctx["active_prompt_event_id"] = event_id
        elif event_name in _GENERATION_END_EVENTS:
            gen_key = explicit_gen or session_ctx.get("current_generation")
            if not gen_key:
                gen_key = _new_generation_key(session_key, session_ctx)
        elif explicit_gen:
            gen_key = _remember_pending_generation(session_ctx, explicit_gen)
        else:
            gen_key = session_ctx.get("current_generation")
            if not gen_key:
                gen_key = _new_generation_key(session_key, session_ctx)
        decision = ToolInvocationCorrelator(session_ctx, ide, gen_key, now_ns).prepare(
            event_name,
            data,
        )
        session_ctx["last_seen_at_ns"] = now_ns
        return decision

    _ctx, decision = _update_session_context(session_key, mutate)
    return decision or _BufferedEventDecision(None, data)


def _complete_generation_state(session_key: str, gen_key: str, memory: Optional[dict] = None) -> None:
    def mutate(session_ctx: dict) -> None:
        pending = session_ctx.get("pending_generations")
        if isinstance(pending, list):
            session_ctx["pending_generations"] = [key for key in pending if key != gen_key]
        if session_ctx.get("current_generation") == gen_key:
            session_ctx.pop("current_generation", None)
            session_ctx.pop("active_prompt_event_id", None)
        invocations = session_ctx.get("tool_invocations")
        if isinstance(invocations, list):
            session_ctx["tool_invocations"] = [
                item for item in invocations if item.get("generation_key") != gen_key
            ]
        agents = session_ctx.get("agent_invocations")
        if isinstance(agents, list):
            session_ctx["agent_invocations"] = [
                item for item in agents if item.get("generation_key") != gen_key
            ]
        seen = session_ctx.get("seen_events")
        if isinstance(seen, list):
            session_ctx["seen_events"] = [
                item for item in seen if item.get("generation_key") != gen_key
            ]
        if isinstance(memory, dict):
            session_ctx["memory"] = memory

    _update_session_context(session_key, mutate)


def _pending_batch_keys_for_session(session_key: str, session_ctx: Optional[dict]) -> list:
    """Enumerate registered and discovered generation batches owned by a session."""
    keys = []
    if session_ctx:
        for key in session_ctx.get("pending_generations") or []:
            if key and key not in keys:
                keys.append(key)
        current = session_ctx.get("current_generation")
        if current and current not in keys:
            keys.append(current)

    if not os.path.isdir(_BATCH_DIR):
        return keys
    session_batch_key = f"{session_key}_session"
    for name in os.listdir(_BATCH_DIR):
        if not name.endswith(".jsonl"):
            continue
        key = name[:-6]
        if key == session_batch_key or key in keys:
            continue
        events = _load_batch_events(key)
        if any(_session_key(entry.get("data") or {}) == session_key for entry in events):
            keys.append(key)
    return keys


def _enrich_buffered_event(data: dict, event_name: str, session_ctx: Optional[dict], ide: str) -> dict:
    enriched = _normalize_mcp_event_data(data, event_name, ide)
    invocation_id = enriched.get("tool_use_id")
    if not invocation_id or not session_ctx:
        return enriched
    invocation = next(
        (
            item for item in session_ctx.get("tool_invocations", [])
            if item.get("tool_use_id") == invocation_id
        ),
        None,
    )
    return _merge_invocation_data(enriched, invocation) if invocation else enriched


# ---------------------------------------------------------------------------
# Trace context helpers
# ---------------------------------------------------------------------------
def _parse_traceparent(value: Optional[str]) -> Optional[dict]:
    if not isinstance(value, str):
        return None
    parts = value.strip().lower().split("-")
    if len(parts) < 4:
        return None
    version, trace_id, span_id, trace_flags = parts[:4]
    if version == "ff":
        return None
    if len(version) != 2 or len(trace_flags) != 2:
        return None
    if version == "00" and len(parts) != 4:
        return None
    if version != "00" and len(parts) > 4 and any(part == "" for part in parts[4:]):
        return None
    if any(ch not in _HEX_DIGITS for ch in version) or any(ch not in _HEX_DIGITS for ch in trace_flags):
        return None
    if not _TRACE_ID_RE.match(trace_id) or not _SPAN_ID_RE.match(span_id):
        return None
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return {
        "trace_id": trace_id,
        "parent_span_id": span_id,
        "trace_flags": trace_flags,
    }


def _resolve_upstream_trace_context(data: Optional[dict]) -> Optional[dict]:
    data = data or {}
    tracestate = _first_present(data, ("tracestate",)) or _first_env(
        ("IDE_OTEL_TRACESTATE", "TRACESTATE", "OTEL_TRACESTATE")
    )
    traceparent = _first_present(data, ("traceparent",)) or _first_env(
        ("IDE_OTEL_TRACEPARENT", "TRACEPARENT", "OTEL_TRACEPARENT")
    )
    parsed = _parse_traceparent(traceparent)
    if parsed is not None:
        if isinstance(tracestate, str) and tracestate.strip():
            parsed["tracestate"] = tracestate.strip()
        return parsed

    trace_id = _lower_or_none(_first_present(data, ("trace_id",)) or _first_env(
        ("IDE_OTEL_TRACE_ID", "TRACE_ID", "OTEL_TRACE_ID")
    ))
    span_id = _lower_or_none(_first_present(data, ("span_id",)) or _first_env(
        ("IDE_OTEL_SPAN_ID", "SPAN_ID", "OTEL_SPAN_ID")
    ))
    parent_span_id = _lower_or_none(_first_present(data, ("parent_span_id",)) or _first_env(
        ("IDE_OTEL_PARENT_SPAN_ID", "PARENT_SPAN_ID", "OTEL_PARENT_SPAN_ID")
    ))
    trace_flags = _lower_or_none(_first_present(data, ("trace_flags",)) or _first_env(
        ("IDE_OTEL_TRACE_FLAGS", "TRACE_FLAGS", "OTEL_TRACE_FLAGS")
    )) or "01"
    effective_parent_span_id = span_id or parent_span_id
    if trace_id is None or effective_parent_span_id is None:
        return None
    if not _TRACE_ID_RE.match(trace_id) or trace_id == "0" * 32:
        return None
    if not _SPAN_ID_RE.match(effective_parent_span_id) or effective_parent_span_id == "0" * 16:
        return None
    if not re.fullmatch(r"[0-9a-f]{2}", trace_flags):
        trace_flags = "01"
    ctx = {
        "trace_id": trace_id,
        "parent_span_id": effective_parent_span_id,
        "trace_flags": trace_flags,
    }
    if isinstance(tracestate, str) and tracestate.strip():
        ctx["tracestate"] = tracestate.strip()
    return ctx


def _make_trace_context(
    trace_id_hex: str,
    span_id_hex: str,
    trace_flags_hex: str = "01",
    tracestate_header: Optional[str] = None,
):
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
    trace_flags = TraceFlags(TraceFlags.SAMPLED)
    try:
        trace_flags = TraceFlags(int(trace_flags_hex, 16) & 0xFF)
    except (ValueError, TypeError):
        pass
    trace_state = TraceState()
    if tracestate_header and hasattr(TraceState, "from_header"):
        try:
            parsed_state = TraceState.from_header([tracestate_header])
            if parsed_state is not None:
                trace_state = parsed_state
        except Exception:
            pass
    ctx = SpanContext(
        trace_id=tid, span_id=sid, is_remote=True,
        trace_flags=trace_flags, trace_state=trace_state,
    )
    return trace.set_span_in_context(NonRecordingSpan(ctx))


def _session_trace_context(session_ctx: Optional[dict]):
    if not session_ctx:
        return None
    return _make_trace_context(
        session_ctx.get("trace_id", "0"),
        session_ctx.get("upstream_parent_span_id") or session_ctx.get("phantom_parent_id", "0"),
        session_ctx.get("trace_flags", "01"),
        session_ctx.get("tracestate"),
    )


def _event_parent_trace_context(data: dict, session_ctx: Optional[dict]):
    upstream_ctx = _resolve_upstream_trace_context(data)
    if upstream_ctx is not None:
        return _make_trace_context(
            upstream_ctx["trace_id"],
            upstream_ctx["parent_span_id"],
            upstream_ctx.get("trace_flags", "01"),
            upstream_ctx.get("tracestate"),
        )
    return _session_trace_context(session_ctx)


def _native_telemetry_attributes(data: dict) -> dict:
    """Return native-agent identity only from the explicit native contract."""
    trace_id = ProviderEventAdapter._native_trace_id(data.get("native_trace_id"))
    span_id = ProviderEventAdapter._native_span_id(data.get("native_span_id"))
    parent_span_id = ProviderEventAdapter._native_span_id(data.get("native_parent_span_id"))
    attrs = {}
    if trace_id:
        attrs["gen_ai.client.native_trace_id"] = trace_id
    if span_id:
        attrs["gen_ai.client.native_span_id"] = span_id
    if parent_span_id:
        attrs["gen_ai.client.native_parent_span_id"] = parent_span_id
    if attrs:
        attrs["gen_ai.client.native_source"] = (
            data.get("native_source") or data.get("_hook_provider_adapter", "unknown")
        )
    return attrs


def _native_span_links(data: dict) -> list:
    """Create a link to a validated native span without inventing identifiers."""
    if SpanContext is None or Link is None or TraceFlags is None or TraceState is None:
        return []
    trace_id = ProviderEventAdapter._native_trace_id(data.get("native_trace_id"))
    span_id = ProviderEventAdapter._native_span_id(
        data.get("native_span_id") or data.get("native_parent_span_id")
    )
    if not trace_id or not span_id:
        return []
    try:
        native_context = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )
        return [Link(native_context, attributes={
            "gen_ai.client.native_source": (
                data.get("native_source") or data.get("_hook_provider_adapter", "unknown")
            )
        })]
    except (TypeError, ValueError):
        return []


def _agent_relationship_links(data: dict, session_ctx: Optional[dict]) -> list:
    if SpanContext is None or Link is None or TraceFlags is None or TraceState is None or not session_ctx:
        return []
    target_agent_id = None
    event_name = data.get("_canonical_event_name")
    if event_name == "SubagentStop":
        target_agent_id = data.get("agent_id")
    elif event_name == "SubagentStart":
        target_agent_id = data.get("parent_agent_id")
    if not target_agent_id:
        return []
    invocation = next(
        (
            item for item in session_ctx.get("agent_invocations", [])
            if item.get("agent_id") == target_agent_id
            and item.get("start_trace_id")
            and item.get("start_span_id")
        ),
        None,
    )
    if not invocation:
        return []
    try:
        context = SpanContext(
            trace_id=int(invocation["start_trace_id"], 16),
            span_id=int(invocation["start_span_id"], 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )
        return [Link(context, attributes={"gen_ai.client.link.type": "delegation"})]
    except (TypeError, ValueError):
        return []


def _event_span_links(data: dict, session_ctx: Optional[dict]) -> list:
    links = _native_span_links(data)
    links.extend(_agent_relationship_links(data, session_ctx))
    return links


def _remember_agent_span_context(
    session_key: Optional[str],
    session_ctx: Optional[dict],
    event_name: str,
    data: dict,
    span,
) -> None:
    if event_name != "SubagentStart" or not session_key or not session_ctx or not data.get("agent_id"):
        return
    try:
        context = span.get_span_context()
        if context is None or not context.is_valid:
            return
        trace_id = f"{context.trace_id:032x}"
        span_id = f"{context.span_id:016x}"
    except Exception:
        return

    def mutate(latest: dict) -> None:
        for invocation in reversed(latest.get("agent_invocations", [])):
            if invocation.get("agent_id") == data.get("agent_id"):
                invocation["start_trace_id"] = trace_id
                invocation["start_span_id"] = span_id
                break

    _update_session_context(session_key, mutate)
    for invocation in reversed(session_ctx.get("agent_invocations", [])):
        if invocation.get("agent_id") == data.get("agent_id"):
            invocation["start_trace_id"] = trace_id
            invocation["start_span_id"] = span_id
            break


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


def _apply_genai_semconv(span, event_name: str, data: dict, ide: str, session_ctx: Optional[dict] = None, batch_model: Optional[str] = None) -> None:
    provider = _infer_genai_provider(data)
    if provider is not None:
        span.set_attribute("gen_ai.provider.name", provider)
    span.set_attribute("gen_ai.system", _resolve_client_name(ide, data=data, session_ctx=session_ctx))
    span.set_attribute("gen_ai.operation.name", _genai_operation(event_name))
    _set_if_present(span, "gen_ai.conversation.id", data.get("conversation_id") or data.get("session_id"))
    _set_if_present(
        span,
        "gen_ai.agent.id",
        _first_present(data, ("agent_id",)) or (session_ctx or {}).get("root_agent_id"),
    )
    _set_if_present(span, "gen_ai.agent.name", _first_present(data, ("agent_name", "subagent_type", "agent_type")))
    _set_if_present(span, "gen_ai.agent.version", _first_present(data, ("agent_version",)))
    _set_if_present(span, "gen_ai.agent.description", _first_present(data, ("agent_description",)))

    # Model attribution with fallback chain (Fix A, B, C)
    model = _first_present(data, ("request_model", "model", "model_name"))
    if not model and session_ctx:
        model = session_ctx.get("last_known_model")
    if not model:
        model = batch_model
    if not model:
        model = os.getenv("CLAUDE_MODEL") or os.getenv("ANTHROPIC_MODEL")

    _set_if_present(span, "gen_ai.request.model", model)
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
        _set_if_present(span, "gen_ai.usage.total_tokens", _int_or_none(_first_present(usage, ("total_tokens",))))

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
    if _conversation_content_enabled():
        records = _conversation_records_from_data(data)
        prompt = next((record.text for record in records if record.kind == "prompt"), None)
        response = next((record.text for record in records if record.kind == "response"), None)
        prompt = prompt or (data.get("prompt") if isinstance(data.get("prompt"), str) else None)
        response = response or (data.get("response") if isinstance(data.get("response"), str) else None)
        if _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")):
            prompt = _mask_text(prompt) if prompt else None
            response = _mask_text(response) if response else None
        max_chars = _text_max_chars()
        prompt = prompt[:max_chars] if prompt else None
        response = response[:max_chars] if response else None
        inp_msg, out_msg = _genai_messages(prompt, response)
        _set_if_present(span, "gen_ai.input.messages", inp_msg)
        _set_if_present(span, "gen_ai.output.messages", out_msg)
    if _safe_bool(os.getenv("IDE_OTEL_CAPTURE_TEXT", "")):
        system_instructions = _first_present(data, ("system_instructions", "system_prompt"))
        if system_instructions is not None:
            _set_if_present(span, "gen_ai.system_instructions", _stringify(system_instructions))


# ---------------------------------------------------------------------------
# Span population
# ---------------------------------------------------------------------------
def _populate_span(span, event_name: str, data: dict, ide: str, session_ctx: Optional[dict] = None, batch_model: Optional[str] = None) -> None:
    """Attach all attributes to a span and emit OTel log records."""
    data = _normalize_mcp_event_data(data, event_name, ide)
    # Add bounded provider fields first so canonical attributes retain priority
    # when the SDK/backend imposes a smaller attribute count limit.
    for key, value in (data.get("_hook_attributes") or {}).items():
        span.set_attribute(key, value)
    _set_client_identity_attributes(span, ide, data=data, session_ctx=session_ctx)
    _apply_enrichment_attributes(span, data, session_ctx=session_ctx)
    for key, value in _workspace_observability_attributes(data, session_ctx).items():
        _set_if_present(span, key, value)
    for key, value in _native_telemetry_attributes(data).items():
        _set_if_present(span, key, value)

    # Optionally attach OS / host attributes on every span.
    # These are already present as resource attributes via OTEL_RESOURCE_ATTRIBUTES,
    # so we gate per-span duplication behind a flag to avoid hot-path overhead.
    if _ATTACH_OS_ATTRIBUTES_PER_SPAN:
        os_info = _get_os_info()
        for attr_key, attr_val in os_info.items():
            span.set_attribute(attr_key, attr_val)

    # Client version
    client_version = _detect_client_version(data, ide)
    _set_if_present(span, "gen_ai.client.version", client_version)

    # Emit structured OTel log record (MCP, shell, tool — correlated with this span)
    _emit_event_log(event_name, data)
    span.set_attribute("gen_ai.client.timestamp", datetime.now(timezone.utc).isoformat())

    # GenAI semantic conventions
    _apply_genai_semconv(span, event_name, data, ide, session_ctx=session_ctx, batch_model=batch_model)

    # Event-specific attributes
    mapping = _EVENT_ATTR_MAP.get(event_name, {})
    for key, attr in mapping.items():
        _set_if_present(span, attr, data.get(key))
    _apply_mcp_attributes(span, event_name, data)
    _set_tool_payload_attributes(span, event_name, data)
    _apply_conversation_attributes(span, event_name, data)
    _apply_operation_status(span, event_name, data)

    for label in ("tool_input", "tool_output", "mcp_input", "mcp_output"):
        value = data.get(label)
        if value is not None and not isinstance(value, dict):
            _maybe_attach_text(span, label, _stringify(value))

    # Attach correlation last so large content cannot evict it from bounded
    # SDK attribute storage. Explicit configured mappings still take precedence.
    span.set_attribute("gen_ai.client.hook.event", event_name)
    span.set_attribute("gen_ai.client.telemetry_source", _TELEMETRY_SOURCE)
    span.set_attribute("gen_ai.client.hook_schema_version", _HOOK_SCHEMA_VERSION)
    _set_if_present(span, "gen_ai.client.hook.event_id", data.get("_hook_event_id"))
    _set_if_present(span, "gen_ai.client.hook.event_id_source", data.get("_hook_event_id_source"))
    _set_if_present(span, "gen_ai.client.hook.original_event", data.get("_hook_original_event"))
    _set_if_present(span, "gen_ai.client.hook.provider_adapter", data.get("_hook_provider_adapter"))
    _set_if_present(
        span,
        "gen_ai.client.mcp.correlated_evidence",
        data.get("_mcp_correlated_evidence"),
    )
    _set_if_present(span, "gen_ai.client.session_id", data.get("session_id") or data.get("conversation_id"))
    _set_if_present(span, "gen_ai.client.generation_id", data.get("generation_id"))
    _set_if_present(span, "gen_ai.client.turn_id", data.get("turn_id"))

    # Explicit extraction mappings can override a canonical value. This lets
    # consumers choose their field semantics without provider-specific switches.
    for key in _CAPTURE_POLICY.mappings.values():
        if key in (data.get("_hook_attributes") or {}):
            span.set_attribute(key, data["_hook_attributes"][key])


def _set_tool_payload_attributes(span, event_name: str, data: dict) -> None:
    """Attach canonical facts from payloads already permitted by capture policy."""
    if event_name not in _OP_TOOL_EVENTS:
        return
    tool_input = data.get("tool_input")
    if isinstance(tool_input, dict):
        for source, target in (("command", "command"), ("description", "approval.description"),
                               ("file_path", "tool.input.file_path"), ("path", "tool.input.path")):
            value = tool_input.get(source)
            if isinstance(value, str):
                span.set_attribute(f"gen_ai.client.{target}", value[:_text_max_chars()])
    for source, target in (("tool_input", "input"), ("tool_response", "response")):
        value = data.get(source)
        length = _int_or_none(data.get(f"{source}_length"))
        digest = data.get(f"{source}_sha256")
        if value is not None and (length is None or not digest):
            text = _stringify(value)
            length, digest = len(text), _hash_text(text)
        _set_if_present(span, f"gen_ai.client.tool.{target}.length", length)
        _set_if_present(span, f"gen_ai.client.tool.{target}.sha256", digest)


# ---------------------------------------------------------------------------
# Memory enrichment (generation/session summaries)
# ---------------------------------------------------------------------------
def _extract_event_memory_facts(event_name: str, data: dict) -> dict:
    """Backward-compatible wrapper for connector-based enrichment."""
    return extract_event_memory_facts(event_name, data)


def _aggregate_generation_memory(batch: list, repo_root: Optional[str] = None) -> dict:
    """Backward-compatible wrapper for connector-based enrichment."""
    return aggregate_generation_memory(batch, repo_root=repo_root)


def _apply_memory_summary_attrs(span, prefix: str, summary: dict) -> None:
    """Attach memory summary attributes to a span."""
    files = summary.get("files") or []
    tools = summary.get("tools") or []
    entities = summary.get("entities") or []
    commands = summary.get("commands") or []
    tool_counts = summary.get("tool_counts") or {}

    span.set_attribute(f"{prefix}.files_touched_count", len(files))
    span.set_attribute(f"{prefix}.tools_used_count", len(tools))
    span.set_attribute(f"{prefix}.entities_count", len(entities))
    span.set_attribute(f"{prefix}.commands_count", len(commands))

    if files:
        span.set_attribute(f"{prefix}.files_touched", files)
    if tools:
        span.set_attribute(f"{prefix}.tools_used", tools)
    if entities:
        span.set_attribute(f"{prefix}.entities", entities)
    if commands:
        span.set_attribute(f"{prefix}.commands", commands)
    if tool_counts:
        span.set_attribute(f"{prefix}.tool_counts", json.dumps(tool_counts, ensure_ascii=True, sort_keys=True))


# ---------------------------------------------------------------------------
# Flush helpers (session-level batching)
# ---------------------------------------------------------------------------
def _flush_generation_unlocked(tracer, gen_key: str, session_ctx: Optional[dict], ide: str, flush: bool = True) -> bool:
    """Flush buffered generation events as a subtree under the session trace."""
    batch = sorted(
        _load_batch_events(gen_key),
        key=lambda e: e.get("timestamp_ns") or 0,
    )
    if not batch:
        _clear_batch_events(gen_key)
        return True

    for entry in batch:
        entry["data"] = _enrich_buffered_event(
            entry.get("data") or {},
            entry.get("event") or "unknown",
            session_ctx,
            ide,
        )

    first_ts = batch[0].get("timestamp_ns") or time.time_ns()
    last_ts = batch[-1].get("timestamp_ns") or time.time_ns()

    # Use session trace context if available, so this generation shares the trace_id
    parent_ctx = _session_trace_context(session_ctx)

    # Scan batch for model (Fix B)
    batch_model = next(
        (_first_present(e["data"], ("request_model", "model", "model_name"))
         for e in batch
         if _first_present(e["data"], ("request_model", "model", "model_name"))),
        None
    )
    repo_root = (session_ctx or {}).get("repo_root")
    memory_summary = _aggregate_generation_memory(batch, repo_root=repo_root)
    first_event_data = batch[0].get("data") if batch else {}

    span_kind = SpanKind.INTERNAL if SpanKind is not None else None
    gen_span = tracer.start_span(
        "gen_ai.client.generation", kind=span_kind,
        context=parent_ctx, start_time=first_ts,
    )
    gen_ctx = trace.set_span_in_context(gen_span)
    with _span_context(gen_span):
        gen_span.set_attribute("gen_ai.client.generation_id", gen_key)
        _set_if_present(
            gen_span,
            "gen_ai.client.session_id",
            _session_key(first_event_data) or (session_ctx or {}).get("session_id"),
        )
        gen_span.set_attribute("gen_ai.client.event.count", len(batch))
        _set_if_present(gen_span, "gen_ai.agent.id", (session_ctx or {}).get("root_agent_id"))
        _set_if_present(gen_span, "gen_ai.request.model", batch_model)
        _apply_memory_summary_attrs(gen_span, "gen_ai.client.memory", memory_summary)
        _set_client_identity_attributes(gen_span, ide, data=first_event_data, session_ctx=session_ctx)
        _apply_enrichment_attributes(gen_span, first_event_data, session_ctx=session_ctx)
        _log_with_span(_LOGGER, logging.INFO, gen_span, "Generation span: gen_key=%s events=%d", gen_key, len(batch))

        for idx, entry in enumerate(batch):
            evt = entry.get("event") or "unknown"
            evt_data = entry.get("data") or {}
            ts = entry.get("timestamp_ns") or time.time_ns()
            next_ts = batch[idx + 1].get("timestamp_ns") if idx + 1 < len(batch) else ts + 1_000_000

            span = tracer.start_span(
                f"gen_ai.client.hook.{evt}", kind=span_kind,
                context=gen_ctx, start_time=ts,
                links=_event_span_links(evt_data, session_ctx),
            )
            with _span_context(span):
                _populate_span(span, evt, evt_data, ide, session_ctx=session_ctx, batch_model=batch_model)
                _remember_agent_span_context(
                    _session_key(evt_data),
                    session_ctx,
                    evt,
                    evt_data,
                    span,
                )
            span.end(end_time=next_ts)

        gen_span.end(end_time=last_ts)

    if session_ctx is not None:
        session_memory = session_ctx.setdefault("memory", {})
        merge_memory_summaries(session_memory, memory_summary, repo_root=repo_root)

    success = not flush or _force_flush_provider(authoritative_signal="traces")
    if success:
        _clear_batch_events(gen_key)
    _LOGGER.info("Flushed generation %s (%d events)", gen_key, len(batch))
    return success


def _flush_generation(tracer, gen_key: str, session_ctx: Optional[dict], ide: str, flush: bool = True) -> bool:
    """Serialize generation flushes so duplicate Stop/SessionEnd callbacks are idempotent."""
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", gen_key)
    lock_path = os.path.join(_LOCK_DIR, f"flush_{safe_key}.lock")
    with _acquire_lock(lock_path):
        return _flush_generation_unlocked(tracer, gen_key, session_ctx, ide, flush=flush)


def _flush_session(tracer, session_key: str, session_ctx: dict, ide: str, flush: bool = True) -> bool:
    """Emit the root session span covering the full session duration."""
    start_ns = session_ctx.get("start_time_ns") or time.time_ns()
    end_ns = time.time_ns()
    session_batch = sorted(
        _load_batch_events(f"{session_key}_session"),
        key=lambda e: e.get("timestamp_ns") or 0,
    )
    first_event_data = session_batch[0].get("data") if session_batch else {}

    parent_ctx = _session_trace_context(session_ctx)

    span_kind = SpanKind.INTERNAL if SpanKind is not None else None
    session_span = tracer.start_span(
        "gen_ai.client.session", kind=span_kind,
        context=parent_ctx, start_time=start_ns,
    )
    with _span_context(session_span):
        for entry in session_batch:
            data = entry.get("data") or {}
            event = entry["event"]
            timestamp = entry.get("timestamp_ns") or end_ns
            hook_span = tracer.start_span(
                f"gen_ai.client.hook.{event}", kind=span_kind,
                context=trace.set_span_in_context(session_span), start_time=timestamp,
            )
            with _span_context(hook_span):
                _populate_span(hook_span, event, data, ide, session_ctx=session_ctx)
            hook_span.end(end_time=timestamp)
        session_span.set_attribute("gen_ai.client.session_id", session_key)
        _set_if_present(session_span, "gen_ai.agent.id", session_ctx.get("root_agent_id"))
        _set_if_present(
            session_span,
            "gen_ai.client.agent_id_source",
            session_ctx.get("root_agent_id_source"),
        )
        _set_client_identity_attributes(session_span, ide, data=first_event_data, session_ctx=session_ctx)
        session_span.set_attribute("gen_ai.client.generation_count", session_ctx.get("generation_count", 0))
        session_span.set_attribute("gen_ai.client.session.duration_ms", (end_ns - start_ns) // 1_000_000)
        _apply_enrichment_attributes(session_span, first_event_data, session_ctx=session_ctx)
        _apply_memory_summary_attrs(session_span, "gen_ai.client.memory", session_ctx.get("memory", {}))
        _log_with_span(_LOGGER, logging.INFO, session_span, "Session span: session=%s", session_key)
        session_span.end(end_time=end_ns)

    success = not flush or _force_flush_provider(authoritative_signal="traces")
    trace_id = session_ctx.get("trace_id", "unknown")
    _LOGGER.info("Flushed session %s (trace_id=%s)", session_key, trace_id)
    return success


def _finalize_session(
    tracer,
    session_key: str,
    session_ctx: Optional[dict],
    ide: str,
    *,
    flush: bool = True,
) -> bool:
    """Flush every session-owned batch, emit one root, then clean all state."""
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_key)
    finalize_lock = os.path.join(_LOCK_DIR, f"finalize_{safe_key}.lock")
    with _acquire_lock(finalize_lock):
        current_ctx, _result = _update_session_context(
            session_key,
            lambda latest: latest.__setitem__("finalizing", True),
        )
        if not current_ctx:
            return True
        session_ctx = current_ctx
        pending = _pending_batch_keys_for_session(session_key, session_ctx)
        session_ctx["generation_count"] = max(session_ctx.get("generation_count", 0), len(pending))
        for gen_key in pending:
            if not _flush_generation(tracer, gen_key, session_ctx, ide, flush=flush):
                return False
        if not _flush_session(tracer, session_key, session_ctx, ide, flush=flush):
            return False
        _clear_batch_events(f"{session_key}_session")
        _clear_session_context(session_key)
        return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    global _EXPORT_DEADLINE
    started = time.monotonic()
    if _safe_bool(os.getenv("IDE_OTEL_LOG_EVENTS", "")):
        # Very early log to stderr to show the hook is alive
        sys.stderr.write(f"Hook starting (pid={os.getpid()})\n")
    try:
        _apply_config_env(_load_config())
    except ValueError as exc:
        # A bad capture policy must neither export unfiltered data nor prevent
        # the harness from continuing its work.
        _LOGGER.error("Hook telemetry disabled: %s", exc)
        data = _normalize_input_data(_load_input())
        ide = _detect_ide(data)
        event = _event_adapter_for(ide).canonical_event_name(_get_event_name(data))
        _emit_stdout_response(event, ide, data)
        return 0
    _configure_logging()

    input_data = _load_input()
    if not isinstance(input_data, dict):
        input_data = {}
    data = _normalize_input_data(input_data)
    raw_event = _get_event_name(data)
    ide = _detect_ide(data)
    captured_payload = _CAPTURE_POLICY.filter(input_data)
    # Never trust incoming private keys as runtime-generated state.
    data = _normalize_input_data({k: v for k, v in captured_payload.items() if not k.startswith("_")})
    short_hook = ide == "codex" and raw_event in {"SessionEnd", "Interrupt"}
    # Leave one second of Codex's three-second budget for startup and local I/O.
    _EXPORT_DEADLINE = started + 2.0 if short_hook else None
    if not short_hook:
        _cleanup_state()
    initial_session_key = _session_key(data)
    initial_session_ctx = _load_session_context(initial_session_key)
    adapter = _event_adapter_for(ide)
    canonical_event = adapter.parse(
        raw_event,
        data,
        session_ctx=initial_session_ctx,
    )
    event_name = canonical_event.event_name
    data = canonical_event.to_lifecycle_data()
    # Pure parsing can retain identity and length/hash facts from omitted
    # conversation content, without persisting that content.
    observed = adapter.parse(raw_event, _normalize_input_data(input_data), session_ctx=initial_session_ctx)
    if canonical_event.event_id_source != "provider":
        # Unmapped fields can distinguish real callbacks (for example,
        # model_params-only thoughts). Include them in fallback identity.
        data["_hook_event_id"] = "hook:" + _hash_text(json.dumps(
            [observed.event_id, input_data], sort_keys=True, ensure_ascii=False,
        ))
    records = data.setdefault("_conversation_records", [])
    retained = {record["kind"] for record in records}
    records.extend({"kind": record.kind, "role": record.role, "length": record.length, "sha256": record.sha256}
                   for record in observed.conversation if record.kind not in retained)
    data["_hook_attributes"] = _CAPTURE_POLICY.attributes(captured_payload)
    if _safe_bool(os.getenv("IDE_OTEL_ENABLE_LOGS", "false")):
        data["_hook_log_body"] = captured_payload
    data = _normalize_mcp_event_data(data, event_name, ide)
    sk = _session_key(data)
    session_ctx = initial_session_ctx if sk == initial_session_key else _load_session_context(sk)
    if sk and session_ctx is None and event_name not in _SESSION_START_EVENTS:
        session_ctx = _create_session_context(sk, data, ide)
    session_ctx = _maybe_bind_session_to_upstream_context(sk, session_ctx, data)
    session_ctx = _maybe_enrich_session_context(sk, session_ctx, data)
    agent_engine = _resolved_agent_engine(data, session_ctx, ide=ide)

    if _safe_bool(os.getenv("IDE_OTEL_LOG_EVENTS", "")):
        _LOGGER.info(
            "Hook: %s (raw=%s) | ide=%s | python=%s",
            event_name, raw_event, ide, sys.executable,
        )

    # Fast path for buffered batch events (no span emission yet):
    # avoid loading OpenTelemetry SDK for events that only append to batch files.
    if _batch_enabled():
        if event_name in _SESSION_START_EVENTS:
            if sk:
                _create_session_context(sk, data, ide)
                if initial_session_ctx is None:
                    _append_batch_event(f"{sk}_session", event_name, data)
            _emit_stdout_response(event_name, ide, data)
            return 0

        if sk and event_name not in _GENERATION_END_EVENTS and event_name not in _SESSION_END_EVENTS:
            _buffer_session_event(sk, event_name, data, ide)
            _emit_stdout_response(event_name, ide, data)
            return 0

    if not _init_tracing(
        ide,
        client_name=_resolve_client_name(ide, data=data, agent_engine=agent_engine, session_ctx=session_ctx),
        resource_attributes=_collect_repository_attributes(data, session_ctx=session_ctx),
    ):
        _emit_stdout_response(event_name, ide, data)
        return 0

    tracer = trace.get_tracer("ide-hooks")
    # Codex only allows up to three seconds for these callbacks. Spend that
    # budget on the current session, leaving stale maintenance to other hooks.
    if not (ide == "codex" and event_name in {"SessionEnd", "Interrupt"}):
        _flush_stale_sessions(tracer)

    if _local_spans_enabled():
        _enable_file_exporter(
            _local_span_path(sk),
            expected_session_key=sk,
        )

    try:
        if sk and session_ctx:
            model = _first_present(data, ("request_model", "model", "model_name"))

            def update_observations(latest: dict) -> None:
                if agent_engine and agent_engine != ide:
                    latest["agent_engine"] = agent_engine
                    latest["agent_engine_confirmed"] = True
                if model:
                    latest["last_known_model"] = model

            session_ctx, _result = _update_session_context(sk, update_observations)

        # ── Batch mode: session-level trace hierarchy ──
        if _batch_enabled():

            # SessionStart: create session context
            if event_name in _SESSION_START_EVENTS:
                if sk:
                    session_ctx = _create_session_context(sk, data, ide)
                _emit_stdout_response(event_name, ide, data)
                return 0

            # Stop: flush generation
            if event_name in _GENERATION_END_EVENTS:
                decision = _buffer_generation_end(sk, event_name, data, ide) if sk else None
                gen_key = decision.generation_key if decision else None
                session_ctx = _load_session_context(sk)
                if gen_key and _flush_generation(tracer, gen_key, session_ctx, ide):
                    memory = session_ctx.get("memory") if session_ctx else None
                    _complete_generation_state(sk, gen_key, memory=memory)
                _emit_stdout_response(event_name, ide, data)
                return 0

            # SessionEnd: emit session root span, clean up
            if event_name in _SESSION_END_EVENTS:
                if sk:
                    with _acquire_lock(_session_lock_path(sk)):
                        key = f"{sk}_session"
                        if not any(entry.get("data", {}).get("_hook_event_id") == data.get("_hook_event_id")
                                   for entry in _load_batch_events(key)):
                            _append_batch_event(key, event_name, data)
                    _finalize_session(tracer, sk, session_ctx, ide)
                _emit_stdout_response(event_name, ide, data)
                return 0

            # Sessionless runners cannot use the persisted batch lifecycle.
            parent_ctx = _event_parent_trace_context(data, session_ctx)
            with tracer.start_as_current_span(
                f"gen_ai.client.hook.{event_name}", kind=SpanKind.INTERNAL,
                context=parent_ctx,
                links=_event_span_links(data, session_ctx),
            ) as span:
                _populate_span(span, event_name, data, ide, session_ctx=session_ctx)
                _remember_agent_span_context(sk, session_ctx, event_name, data, span)

            _emit_stdout_response(event_name, ide, data)
            return 0

        # ── Streaming mode: emit spans immediately ──
        parent_ctx = _event_parent_trace_context(data, session_ctx)

        # Create session context on SessionStart even in streaming mode
        if event_name in _SESSION_START_EVENTS and sk:
            duplicate_start = session_ctx is not None
            session_ctx = _create_session_context(sk, data, ide)
            parent_ctx = _event_parent_trace_context(data, session_ctx)
            if duplicate_start:
                _emit_stdout_response(event_name, ide, data)
                return 0

        if event_name in _SESSION_END_EVENTS and sk and not session_ctx:
            _emit_stdout_response(event_name, ide, data)
            return 0

        streaming_decision = None
        if sk and event_name not in _SESSION_START_EVENTS and event_name not in _SESSION_END_EVENTS:
            streaming_decision = _prepare_streaming_session_event(sk, event_name, data, ide)
            if streaming_decision.duplicate:
                _emit_stdout_response(event_name, ide, streaming_decision.data)
                return 0
            data = streaming_decision.data
            session_ctx = _load_session_context(sk) or session_ctx
            if streaming_decision.correlated:
                data = dict(data)
                data["_mcp_correlated_evidence"] = True

        with tracer.start_as_current_span(
            f"gen_ai.client.hook.{event_name}", kind=SpanKind.INTERNAL,
            context=parent_ctx,
            links=_event_span_links(data, session_ctx),
        ) as span:
            _populate_span(span, event_name, data, ide, session_ctx=session_ctx)
            _remember_agent_span_context(sk, session_ctx, event_name, data, span)

        if (
            event_name in _GENERATION_END_EVENTS
            and sk
            and streaming_decision
            and streaming_decision.generation_key
        ):
            _complete_generation_state(sk, streaming_decision.generation_key)

        # Clean up session on SessionEnd
        if event_name in _SESSION_END_EVENTS and sk:
            _finalize_session(tracer, sk, session_ctx, ide)

        # Flush in streaming mode to ensure spans are exported
        _force_flush_provider()

    except Exception as exc:
        if trace is not None and Status is not None:
            cur = trace.get_current_span()
            if cur is not None:
                cur.record_exception(exc)
                cur.set_status(Status(StatusCode.ERROR, str(exc)))
        _LOGGER.exception("Hook failure: %s", exc)

    _emit_stdout_response(event_name, ide, data)
    return 0


# ---------------------------------------------------------------------------
# Setup CLI: helpers
# ---------------------------------------------------------------------------

_REPO_MARKERS = (".git", ".github", ".cursor", ".claude", ".gemini", ".codex", ".opencode", ".windsurf")


def _find_repo_root(cwd: str) -> str:
    """Walk up from cwd to find the nearest repo/project root.

    Tries `git rev-parse --show-toplevel` first (handles worktrees where .git
    is a file rather than a directory), then falls back to marker detection.
    """
    # Fast path: ask git (works for worktrees too)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=os.path.abspath(cwd),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # git not installed, timed out, or environment error — non-fatal, use marker walk
        pass
    # Fallback: walk up looking for well-known markers (os.path.exists handles
    # both regular dirs and file-form .git used by worktrees/submodules)
    current = os.path.abspath(cwd)
    while True:
        for marker in _REPO_MARKERS:
            if os.path.exists(os.path.join(current, marker)):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(cwd)
        current = parent


def _resolve_hook_cmd() -> str:
    return shutil.which("otel-hook") or "otel-hook"


def _hook_cmd_for_agent(agent: str) -> str:
    executable = _resolve_hook_cmd()
    if sys.platform == "win32":
        if agent == "copilot":
            executable = "& '" + executable.replace("'", "''") + "'"
        else:
            executable = f'"{executable}"'
    else:
        executable = shlex.quote(executable)
    return f"{executable} --{agent}"


def _is_copilot_hook(hook: dict) -> bool:
    return any(
        marker in hook.get(shell, "")
        for shell in ("bash", "powershell") for marker in ("otel-hook", "otel_hook")
    )


def _load_json_file(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError as exc:
                raise click.ClickException(f"Invalid JSON in {path}: {exc}") from exc
    return {}


def _write_json_file(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _ensure_toml_bool(path: str, section: str, key: str, value: bool) -> None:
    """Set a boolean in a simple TOML section while preserving unrelated text."""
    desired = "true" if value else "false"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = []

    section_header = f"[{section}]"
    section_start = None
    section_end = len(lines)
    section_re = re.compile(r"^\s*\[.*\]\s*$")
    for index, line in enumerate(lines):
        if line.strip() == section_header:
            section_start = index
            section_end = len(lines)
            for probe in range(index + 1, len(lines)):
                if section_re.match(lines[probe]):
                    section_end = probe
                    break
            break

    new_line = f"{key} = {desired}\n"
    if section_start is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend([f"{section_header}\n", new_line])
    else:
        key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for index in range(section_start + 1, section_end):
            if key_re.match(lines[index]):
                if lines[index] == new_line:
                    return
                lines[index] = new_line
                break
        else:
            lines.insert(section_end, new_line)

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _remove_toml_key(path: str, section: str, key: str) -> None:
    """Remove a key from a simple TOML section while preserving unrelated text."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    section_header = f"[{section}]"
    section_start = None
    section_end = len(lines)
    section_re = re.compile(r"^\s*\[.*\]\s*$")
    for index, line in enumerate(lines):
        if line.strip() == section_header:
            section_start = index
            section_end = len(lines)
            for probe in range(index + 1, len(lines)):
                if section_re.match(lines[probe]):
                    section_end = probe
                    break
            break

    if section_start is None:
        return

    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    new_lines = []
    removed = False
    for index, line in enumerate(lines):
        if section_start < index < section_end and key_re.match(line):
            removed = True
            continue
        new_lines.append(line)

    if not removed:
        return

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def _detect_available_agents() -> list:
    """Return list of agent names whose home dirs or commands exist."""
    found = []
    home = os.path.expanduser("~")
    if os.path.isdir(os.path.join(home, ".cursor")) or shutil.which("cursor"):
        found.append("cursor")
    if os.path.isdir(os.path.join(home, ".claude")) or shutil.which("claude"):
        found.append("claude")
    # Copilot: detect via gh CLI or a .github dir in the current working directory
    if shutil.which("gh") or os.path.isdir(os.path.join(os.getcwd(), ".github")):
        found.append("copilot")
    if os.path.isdir(os.path.join(home, ".gemini")) or shutil.which("gemini"):
        found.append("gemini")
    if shutil.which("opencode") or os.path.isdir(os.path.join(home, ".config", "opencode")):
        found.append("opencode")
    if os.path.isdir(os.path.join(home, ".codeium", "windsurf")) or shutil.which("windsurf"):
        found.append("windsurf")
    if os.path.isdir(os.path.join(home, ".codex")) or shutil.which("codex"):
        found.append("codex")
    return found


def _find_opencode_plugin_source() -> Optional[str]:
    """Return the absolute path to the bundled plugin/opencode.ts source file.

    Looks next to otel_hook.py first (source checkout), then in the installed
    package data location (pipx / pip install).
    """
    # Source checkout: plugin/ lives next to otel_hook.py
    candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin", _OPENCODE_PLUGIN_SOURCE_FILENAME)
    if os.path.isfile(candidate):
        return candidate
    # Installed package data: setuptools places data files under <prefix>/share/
    # Check the venv that contains this very module (handles pipx venvs correctly),
    # then sys.prefix, then ~/.local as a last-resort fallback.
    import sys
    module_file = os.path.abspath(__file__)
    # Walk up from the module file to find a share/ sibling (handles any venv layout)
    check = os.path.dirname(module_file)
    for _ in range(6):  # at most 6 levels up
        share_candidate = os.path.join(check, "share", "opentelemetry-hooks", "plugin", _OPENCODE_PLUGIN_SOURCE_FILENAME)
        if os.path.isfile(share_candidate):
            return share_candidate
        parent = os.path.dirname(check)
        if parent == check:
            break
        check = parent
    # Fallbacks: sys.prefix, sys.real_prefix, ~/.local
    for prefix in (sys.prefix, getattr(sys, "real_prefix", None), os.path.expanduser("~/.local")):
        if not prefix:
            continue
        data_candidate = os.path.join(prefix, "share", "opentelemetry-hooks", "plugin", _OPENCODE_PLUGIN_SOURCE_FILENAME)
        if os.path.isfile(data_candidate):
            return data_candidate
    return None


# ---------------------------------------------------------------------------
# Setup CLI: per-agent setup functions (public API, importable)
# ---------------------------------------------------------------------------

def _configure_managed_hook_command(hook: dict, source: str) -> bool:
    """Stamp a setup-managed hook command with the source-agent CLI flag."""
    exec_form = sys.platform == "win32" and source == "claude"
    desired_cmd = _resolve_hook_cmd() if exec_form else _hook_cmd_for_agent(source)
    changed = False
    if hook.get("command") != desired_cmd:
        hook["command"] = desired_cmd
        changed = True
    if exec_form:
        if hook.get("args") != ["--claude"]:
            hook["args"] = ["--claude"]
            changed = True
        if "shell" in hook:
            del hook["shell"]
            changed = True
    elif source == "claude" and "args" in hook:
        # Converting a managed Windows exec registration back to shell form.
        del hook["args"]
        changed = True
    if source == "codex" and sys.platform == "win32" and hook.get("commandWindows") != desired_cmd:
        hook["commandWindows"] = desired_cmd
        changed = True
    env = hook.get("env")
    if isinstance(env, dict):
        next_env = dict(env)
        next_env.pop("IDE_OTEL_IDE_NAME", None)
        next_env.pop(_MANAGED_HOOK_SOURCE_ENV, None)
        if next_env:
            if next_env != env:
                hook["env"] = next_env
                changed = True
        else:
            hook.pop("env", None)
            changed = True
    return changed


def setup_cursor(global_: bool = True, cwd: str = ".") -> None:
    """Register otel-hook in Cursor's hooks.json."""
    hook_cmd = _resolve_hook_cmd()
    if global_:
        hooks_path = os.path.join(os.path.expanduser("~"), ".cursor", "hooks.json")
    else:
        repo = _find_repo_root(cwd)
        hooks_path = os.path.join(repo, ".cursor", "hooks.json")

    doc = _load_json_file(hooks_path)
    doc.setdefault("version", 1)
    hooks = doc.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _CURSOR_EVENTS:
        event_hooks = hooks.setdefault(event, [])
        matches = [h for h in event_hooks if "otel-hook" in h.get("command", "") or "otel_hook" in h.get("command", "")]
        if matches:
            changed = False
            for hook in matches:
                changed = _configure_managed_hook_command(hook, "cursor") or changed
            (updated if changed else skipped).append(event)
        else:
            hook = {"command": hook_cmd}
            _configure_managed_hook_command(hook, "cursor")
            event_hooks.append(hook)
            added.append(event)

    _write_json_file(hooks_path, doc)
    _log_setup_result("cursor", hooks_path, added, updated, skipped)


def setup_windsurf(global_: bool = True, cwd: str = ".") -> None:
    """Register native Cascade hooks (also supported by Devin Desktop)."""
    hook_cmd = _resolve_hook_cmd()
    if global_:
        hooks_path = os.path.join(os.path.expanduser("~"), ".codeium", "windsurf", "hooks.json")
    else:
        repo = _find_repo_root(cwd)
        hooks_path = os.path.join(repo, ".windsurf", "hooks.json")
        devin_path = os.path.join(repo, ".devin", "hooks.json")
        if _load_json_file(devin_path).get("hooks"):
            hooks_path = devin_path

    doc = _load_json_file(hooks_path)
    hooks = doc.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _WINDSURF_EVENTS:
        event_hooks = hooks.setdefault(event, [])
        matches = [h for h in event_hooks if "otel-hook" in h.get("command", "") or "otel_hook" in h.get("command", "")]
        if matches:
            changed = False
            for hook in matches:
                changed = _configure_managed_hook_command(hook, "windsurf") or changed
            (updated if changed else skipped).append(event)
        else:
            hook = {"command": hook_cmd}
            _configure_managed_hook_command(hook, "windsurf")
            event_hooks.append(hook)
            added.append(event)

    _write_json_file(hooks_path, doc)
    _log_setup_result("windsurf", hooks_path, added, updated, skipped)


def setup_claude(global_: bool = True, cwd: str = ".") -> None:
    """Register otel-hook in Claude Code's settings.json."""
    hook_cmd = _resolve_hook_cmd()
    if global_:
        settings_path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    else:
        repo = _find_repo_root(cwd)
        settings_path = os.path.join(repo, ".claude", "settings.json")

    settings = _load_json_file(settings_path)
    hooks = settings.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _CLAUDE_EVENTS:
        event_list = hooks.setdefault(event, [])
        others, exact = [], []
        desired_cmd = _hook_cmd_for_agent("claude")
        for entry in event_list:
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "otel_hook" in cmd or "otel-hook" in cmd:
                    (exact if cmd == desired_cmd else others).append(h)

        if exact:
            changed = False
            for hook in exact:
                changed = _configure_managed_hook_command(hook, "claude") or changed
            (updated if changed else skipped).append(event)
            continue

        if others:
            for hook in others:
                _configure_managed_hook_command(hook, "claude")
            updated.append(event)
            continue

        hook = {"type": "command", "command": hook_cmd}
        _configure_managed_hook_command(hook, "claude")
        hook_entry: dict = {"hooks": [hook]}
        if event in _CLAUDE_MATCHER_EVENTS:
            hook_entry["matcher"] = "*"
        event_list.append(hook_entry)
        added.append(event)

    _write_json_file(settings_path, settings)
    _log_setup_result("claude", settings_path, added, updated, skipped)


def setup_copilot(cwd: str = ".", global_: bool = False) -> None:
    """Register otel-hook in GitHub Copilot's otel-hooks.json."""
    hook_cmd = _hook_cmd_for_agent("copilot")
    command_key = "powershell" if sys.platform == "win32" else "bash"
    repo = _find_repo_root(cwd)
    hooks_path = os.path.join(repo, ".github", "hooks", "otel-hooks.json")
    if global_:
        copilot_home = os.getenv("COPILOT_HOME") or os.path.join(os.path.expanduser("~"), ".copilot")
        hooks_path = os.path.join(copilot_home, "hooks", "otel-hooks.json")

    doc = _load_json_file(hooks_path)
    doc.setdefault("version", 1)
    hooks = doc.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _COPILOT_EVENTS:
        # Native Copilot callbacks do not carry hook_event_name.
        event_cmd = f"{hook_cmd} --event {event}"
        event_hooks = hooks.setdefault(event, [])
        plain = [h for h in event_hooks if _is_copilot_hook(h)]
        if plain:
            changed = any("timeoutSec" not in h or h.get(command_key) != event_cmd for h in plain)
            for h in plain:
                h[command_key] = event_cmd
                h.setdefault("timeoutSec", 30)
            (updated if changed else skipped).append(event)
            continue

        event_hooks.append({"type": "command", command_key: event_cmd, "timeoutSec": 30})
        added.append(event)

    _write_json_file(hooks_path, doc)
    _log_setup_result("copilot", hooks_path, added, updated, skipped)


def setup_gemini(global_: bool = True, cwd: str = ".") -> None:
    """Register otel-hook in Gemini CLI's settings.json."""
    hook_cmd = _resolve_hook_cmd()
    if global_:
        settings_path = os.path.join(os.path.expanduser("~"), ".gemini", "settings.json")
    else:
        repo = _find_repo_root(cwd)
        settings_path = os.path.join(repo, ".gemini", "settings.json")

    settings = _load_json_file(settings_path)
    hooks = settings.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _GEMINI_EVENTS:
        event_list = hooks.setdefault(event, [])
        others, exact = [], []
        desired_cmd = _hook_cmd_for_agent("gemini")
        for entry in event_list:
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "otel_hook" in cmd or "otel-hook" in cmd:
                    (exact if cmd == desired_cmd else others).append(h)

        if exact:
            changed = False
            for hook in exact:
                changed = _configure_managed_hook_command(hook, "gemini") or changed
            (updated if changed else skipped).append(event)
            continue

        if others:
            for hook in others:
                _configure_managed_hook_command(hook, "gemini")
            updated.append(event)
            continue

        hook = {"type": "command", "command": hook_cmd, "name": "otel-hook"}
        _configure_managed_hook_command(hook, "gemini")
        hook_entry: dict = {"hooks": [hook]}
        if event in _GEMINI_MATCHER_EVENTS:
            hook_entry["matcher"] = "*"
        event_list.append(hook_entry)
        added.append(event)

    _write_json_file(settings_path, settings)
    _log_setup_result("gemini", settings_path, added, updated, skipped)


def setup_codex(global_: bool = True, cwd: str = ".") -> None:
    """Register otel-hook in Codex's hooks.json and enable Codex hooks."""
    hook_cmd = _resolve_hook_cmd()
    if global_:
        codex_dir = os.path.join(os.path.expanduser("~"), ".codex")
    else:
        repo = _find_repo_root(cwd)
        codex_dir = os.path.join(repo, ".codex")

    hooks_path = os.path.join(codex_dir, "hooks.json")
    config_path = os.path.join(codex_dir, "config.toml")
    _ensure_toml_bool(config_path, "features", "hooks", True)
    _remove_toml_key(config_path, "features", "codex_hooks")

    doc = _load_json_file(hooks_path)
    hooks = doc.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _CODEX_EVENTS:
        timeout = 3 if event in {"SessionEnd", "Interrupt"} else 30
        event_list = hooks.setdefault(event, [])
        matcher = _CODEX_MATCHERS.get(event)
        if event == "SessionStart":
            # Move only our legacy registration, even if the new one already exists.
            for entry in list(event_list):
                if entry.get("matcher") == "startup|resume|clear":
                    entry["hooks"] = [h for h in entry.get("hooks", []) if not any(token in h.get("command", "") for token in ("otel-hook", "otel_hook"))]
                    if not entry["hooks"]:
                        event_list.remove(entry)
        matches = []
        for entry in event_list:
            if matcher is not None and entry.get("matcher", "") != matcher:
                continue
            if matcher is None and "matcher" in entry:
                continue
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "otel_hook" in cmd or "otel-hook" in cmd:
                    matches.append(h)

        if matches:
            changed = False
            desired_cmd = _hook_cmd_for_agent("codex")
            for hook in matches:
                if hook.get("command") != desired_cmd:
                    hook["command"] = desired_cmd
                    changed = True
                if hook.get("type") != "command":
                    hook["type"] = "command"
                    changed = True
                if "timeout" not in hook or (event in {"SessionEnd", "Interrupt"} and hook["timeout"] != timeout):
                    hook["timeout"] = timeout
                    changed = True
                changed = _configure_managed_hook_command(hook, "codex") or changed
            (updated if changed else skipped).append(event)
            continue

        hook = {"type": "command", "command": hook_cmd, "timeout": timeout}
        _configure_managed_hook_command(hook, "codex")
        hook_entry: dict = {
            "hooks": [hook]
        }
        if matcher is not None:
            hook_entry["matcher"] = matcher
        event_list.append(hook_entry)
        added.append(event)

    _write_json_file(hooks_path, doc)
    if added or updated:
        click.echo(f"  ✓ [codex] Enabled hooks feature ({config_path})")
    _log_setup_result("codex", hooks_path, added, updated, skipped)


def setup_opencode(global_: bool = True, cwd: str = ".") -> None:
    """Install the otel-hook TypeScript plugin into OpenCode's plugins directory.

    Global install (default): ~/.config/opencode/plugins/otel-hook.ts
    Project install:           <repo>/.opencode/plugins/otel-hook.ts
    """
    src = _find_opencode_plugin_source()
    if src is None:
        raise click.ClickException(
            "Cannot find plugin/opencode.ts — ensure the opentelemetry-hooks package "
            "is installed correctly (pip install opentelemetry-hooks)."
        )

    home = os.path.expanduser("~")
    if global_:
        plugins_dir = os.path.join(home, ".config", "opencode", "plugins")
    else:
        repo = _find_repo_root(cwd)
        plugins_dir = os.path.join(repo, ".opencode", "plugins")

    dest = os.path.join(plugins_dir, _OPENCODE_PLUGIN_FILENAME)

    # Check if already installed and identical
    if os.path.isfile(dest):
        with open(src, encoding="utf-8") as f:
            src_content = f.read()
        with open(dest, encoding="utf-8") as f:
            dest_content = f.read()
        if src_content == dest_content:
            click.echo(f"  · [opencode] Already up to date ({dest})")
            return
        # Content differs — update
        os.makedirs(plugins_dir, exist_ok=True)
        shutil.copy2(src, dest)
        click.echo(f"  ✓ [opencode] Updated plugin ({dest})")
        return

    os.makedirs(plugins_dir, exist_ok=True)
    shutil.copy2(src, dest)
    click.echo(f"  ✓ [opencode] Installed plugin ({dest})")


def setup_agent(agent: str, global_: bool = True, cwd: str = ".") -> None:
    """Dispatcher: configure hooks for a single agent by name."""
    if agent == "cursor":
        setup_cursor(global_=global_, cwd=cwd)
    elif agent == "windsurf":
        setup_windsurf(global_=global_, cwd=cwd)
    elif agent == "claude":
        setup_claude(global_=global_, cwd=cwd)
    elif agent == "copilot":
        setup_copilot(cwd=cwd, global_=global_)
    elif agent == "gemini":
        setup_gemini(global_=global_, cwd=cwd)
    elif agent == "codex":
        setup_codex(global_=global_, cwd=cwd)
    elif agent == "opencode":
        setup_opencode(global_=global_, cwd=cwd)
    else:
        raise ValueError(f"Unknown agent: {agent}")


def _log_setup_result(agent: str, path: str, added: list, updated: list, skipped: list) -> None:
    if added:
        click.echo(f"  ✓ [{agent}] Added {len(added)} events ({path})")
    if updated:
        click.echo(f"  ✓ [{agent}] Updated {len(updated)} events")
    if not added and not updated:
        click.echo(f"  · [{agent}] Already up to date ({path})")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.group(invoke_without_command=True)
@click.option("--cursor", "hook_source", flag_value="cursor", default=None, help="Run hook as Cursor.")
@click.option("--windsurf", "hook_source", flag_value="windsurf", help="Run hook as Windsurf.")
@click.option("--claude", "hook_source", flag_value="claude", help="Run hook as Claude Code.")
@click.option("--copilot", "hook_source", flag_value="copilot", help="Run hook as GitHub Copilot.")
@click.option("--gemini", "hook_source", flag_value="gemini", help="Run hook as Gemini CLI.")
@click.option("--codex", "hook_source", flag_value="codex", help="Run hook as Codex CLI.")
@click.option("--opencode", "hook_source", flag_value="opencode", help="Run hook as OpenCode.")
@click.option("--event", "hook_event", default=None, help="Hook event name for runners that omit it from stdin.")
@click.pass_context
def cli(ctx: click.Context, hook_source: Optional[str], hook_event: Optional[str]) -> None:
    """otel-hook — OpenTelemetry hook runner and setup CLI for AI coding agents.

    When called with no subcommand and piped stdin, runs as the hook runner
    (IDE event JSON → OTel spans). Use subcommands to configure agent hooks.
    """
    global _CLI_HOOK_SOURCE, _CLI_HOOK_EVENT
    _CLI_HOOK_SOURCE = hook_source
    _CLI_HOOK_EVENT = hook_event
    if ctx.invoked_subcommand is None:
        if not sys.stdin.isatty():
            raise SystemExit(main())
        else:
            click.echo(ctx.get_help())


@cli.command("setup")
@click.option(
    "--agent", "agents",
    type=click.Choice(["cursor", "windsurf", "claude", "copilot", "gemini", "codex", "opencode"]),
    multiple=True,
    help="Agent to configure. Omit to auto-detect all available agents.",
)
@click.option("--global/--no-global", "global_", default=True, show_default=True,
              help="Install to global agent config (~/.claude, ~/.cursor, etc.)")
@click.option("--cwd", default=".", show_default=True,
              help="Project root for project-scoped installs.")
def setup_cmd(agents: tuple, global_: bool, cwd: str) -> None:
    """Register otel-hook in one or more AI agent configs."""
    targets = list(agents) or _detect_available_agents()
    if not targets:
        click.echo("No agents detected. Use --agent cursor|windsurf|claude|copilot|gemini|codex|opencode to specify one.", err=True)
        raise SystemExit(1)
    errors = []
    for agent in targets:
        try:
            setup_agent(agent, global_=global_, cwd=cwd)
        except Exception as exc:
            click.echo(f"  ✗ [{agent}] {exc}", err=True)
            errors.append(agent)
    if errors:
        raise SystemExit(1)


_SUPPORTED_AGENTS = ("cursor", "windsurf", "claude", "copilot", "gemini", "codex", "opencode")


def _agent_config_paths(global_: bool, cwd: str) -> dict[str, str]:
    home = os.path.expanduser("~")
    repo_root = _find_repo_root(cwd)
    return {
        "cursor": os.path.join(home, ".cursor", "hooks.json") if global_ else os.path.join(repo_root, ".cursor", "hooks.json"),
        "windsurf": os.path.join(home, ".codeium", "windsurf", "hooks.json") if global_ else (
            os.path.join(repo_root, ".devin", "hooks.json") if _load_json_file(os.path.join(repo_root, ".devin", "hooks.json")).get("hooks")
            else os.path.join(repo_root, ".windsurf", "hooks.json")
        ),
        "claude": os.path.join(home, ".claude", "settings.json") if global_ else os.path.join(repo_root, ".claude", "settings.json"),
        "copilot": os.path.join(os.getenv("COPILOT_HOME") or os.path.join(home, ".copilot"), "hooks", "otel-hooks.json") if global_ else os.path.join(repo_root, ".github", "hooks", "otel-hooks.json"),
        "gemini": os.path.join(home, ".gemini", "settings.json") if global_ else os.path.join(repo_root, ".gemini", "settings.json"),
        "codex": os.path.join(home, ".codex", "hooks.json") if global_ else os.path.join(repo_root, ".codex", "hooks.json"),
        "opencode": (
            os.path.join(home, ".config", "opencode", "plugins", _OPENCODE_PLUGIN_FILENAME)
            if global_
            else os.path.join(repo_root, ".opencode", "plugins", _OPENCODE_PLUGIN_FILENAME)
        ),
    }


def _registered_hook_events(agent: str, path: str) -> list[str]:
    if agent == "opencode":
        return ["plugin"] if os.path.isfile(path) else []
    if not os.path.exists(path):
        return []
    doc = _load_json_file(path)
    hooks = doc.get("hooks", {})
    enabled = set()
    for event_name, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else []:
            for field in ("command", "bash", "powershell"):
                command = entry.get(field, "")
                if "otel-hook" in command or "otel_hook" in command:
                    enabled.add(event_name)
            for nested in entry.get("hooks", []):
                command = nested.get("command", "")
                if "otel-hook" in command or "otel_hook" in command:
                    enabled.add(event_name)
    return sorted(enabled)


def _pending_state_summary() -> dict:
    def files(directory: str, pattern: str) -> list[str]:
        return glob.glob(os.path.join(directory, pattern)) if os.path.isdir(directory) else []

    sessions = files(_SESSION_DIR, "*.json")
    batches = files(_BATCH_DIR, "*.jsonl")
    locks = files(_LOCK_DIR, "*.lock")
    mtimes = [os.path.getmtime(path) for path in sessions + batches if os.path.exists(path)]
    oldest_age = max(0, int(time.time() - min(mtimes))) if mtimes else 0
    return {
        "sessions": len(sessions),
        "batches": len(batches),
        "locks": len(locks),
        "oldest_pending_age_seconds": oldest_age,
        "state_directory_writable": os.access(_STATE_DIR, os.W_OK) if os.path.exists(_STATE_DIR) else os.access(_HOOK_DIR, os.W_OK),
    }


def _doctor_report(agents: tuple, global_: bool, cwd: str) -> tuple[dict, int]:
    targets = list(agents) or list(_SUPPORTED_AGENTS)
    paths = _agent_config_paths(global_, cwd)
    registrations = []
    for agent in targets:
        path = paths[agent]
        enabled_events = _registered_hook_events(agent, path)
        registrations.append({
            "agent": agent,
            "path": path,
            "registered_events": len(enabled_events),
            "enabled_events": enabled_events,
        })

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    health = _load_delivery_health()
    signal_health = health.get("signals", {}) if isinstance(health, dict) else {}
    logs_enabled = _safe_bool(os.getenv("IDE_OTEL_ENABLE_LOGS", "false"))

    def signal_failed_recently(signal: str) -> bool:
        record = signal_health.get(signal, {})
        return bool(
            isinstance(record, dict)
            and record.get("last_failure_at_ns", 0)
            >= record.get("last_success_at_ns", 0)
            and record.get("last_failure_at_ns")
        )

    exporter_status = "disabled"
    if endpoint:
        exporter_status = "configured_unknown"
        traces = signal_health.get("traces", {})
        if signal_failed_recently("traces") or (logs_enabled and signal_failed_recently("logs")):
            exporter_status = "failing_recent"
        elif traces.get("last_success_at_ns"):
            exporter_status = "healthy_recent"
    elif _local_spans_enabled():
        exporter_status = "local_only"

    failures = []
    for signal, record in signal_health.items():
        if not isinstance(record, dict):
            continue
        if record.get("last_failure_at_ns", 0) >= record.get("last_success_at_ns", 0) and record.get("last_failure_at_ns"):
            failures.append({
                "signal": signal,
                "at_ns": record.get("last_failure_at_ns"),
                "endpoint": record.get("endpoint"),
                "error": record.get("last_error"),
            })

    distro = _hook_distro_attributes()
    report = {
        "schema_version": 1,
        "status": "healthy",
        "package": {
            "name": "opentelemetry-hooks",
            "version": distro.get("telemetry.distro.version", "source-checkout"),
            "executable": shutil.which("otel-hook"),
        },
        "detected_agent": _detect_ide({}),
        "hook_schema_version": _HOOK_SCHEMA_VERSION,
        "registrations": registrations,
        "privacy": {
            "capture_mode": _CAPTURE_POLICY.mode,
            "hook_log_body": (
                _CAPTURE_POLICY.mode if logs_enabled else "disabled"
            ),
            "conversation_content": _conversation_content_enabled(),
            "tool_input_content": _safe_bool(os.getenv("IDE_OTEL_CAPTURE_TOOL_INPUT_CONTENT", "")),
            "user_identity": _safe_bool(os.getenv("IDE_OTEL_CAPTURE_USER_IDENTITY", "")),
            "masking": _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")),
        },
        "exporter": {
            "status": exporter_status,
            "protocol": (os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL") or "grpc").lower(),
            "endpoint": _sanitized_exporter_endpoint(endpoint),
            "logs_enabled": logs_enabled,
            "headers_present": bool(os.getenv("OTEL_EXPORTER_OTLP_HEADERS")),
        },
        "state": _pending_state_summary(),
        "recent_delivery_failures": failures,
    }
    warnings = []
    if not any(item["registered_events"] for item in registrations):
        warnings.append("no requested agent hooks are registered")
    if exporter_status in {"disabled", "failing_recent"}:
        warnings.append("authoritative trace exporter or enabled log exporter is not healthy")
    if not report["state"]["state_directory_writable"]:
        warnings.append("hook state directory is not writable")
    if warnings:
        report["status"] = "degraded"
        report["warnings"] = warnings
        return report, 1
    return report, 0


def _doctor_human_lines(report: dict) -> list[str]:
    """Render complete or minimal doctor reports without raising another error."""
    status = report.get("status") or "unknown"
    lines = [f"otel-hook doctor: {status}"]
    error = report.get("error")
    if status == "error" or isinstance(error, dict):
        error_type = error.get("type") if isinstance(error, dict) else None
        lines.append(f"  error: {error_type or 'InternalError'}")
        return lines

    package = report.get("package") or {}
    lines.append(
        f"  package: {package.get('version', 'unknown')} "
        f"({package.get('executable') or 'not on PATH'})"
    )
    lines.append(f"  detected agent: {report.get('detected_agent') or 'unknown'}")
    for registration in report.get("registrations") or []:
        if not isinstance(registration, dict):
            continue
        lines.append(
            f"  [{registration.get('agent', 'unknown')}] "
            f"{registration.get('registered_events', 0)} events "
            f"({registration.get('path') or 'unknown path'})"
        )
    exporter = report.get("exporter") or {}
    lines.append(
        f"  exporter: {exporter.get('status', 'unknown')} "
        f"({exporter.get('endpoint') or 'no endpoint'})"
    )
    state = report.get("state") or {}
    lines.append(
        f"  state: {state.get('sessions', 0)} sessions, "
        f"{state.get('batches', 0)} batches"
    )
    for warning in report.get("warnings") or []:
        lines.append(f"  warning: {warning}")
    return lines


@cli.command("doctor")
@click.option(
    "--agent", "agents",
    type=click.Choice(list(_SUPPORTED_AGENTS)),
    multiple=True,
    help="Agent to inspect. Omit to inspect all.",
)
@click.option("--global/--no-global", "global_", default=True)
@click.option("--cwd", default=".")
@click.option("--json", "json_output", is_flag=True, help="Emit a stable machine-readable report.")
def doctor_cmd(agents: tuple, global_: bool, cwd: str, json_output: bool) -> None:
    """Inspect hook registration, privacy, exporter health, and pending state."""
    try:
        _apply_config_env(_load_config())
        report, exit_code = _doctor_report(agents, global_, cwd)
    except Exception as exc:
        report = {
            "schema_version": 1,
            "status": "error",
            "error": {"type": type(exc).__name__},
        }
        exit_code = 2
    if json_output:
        click.echo(json.dumps(report, ensure_ascii=True, sort_keys=True))
    else:
        for line in _doctor_human_lines(report):
            click.echo(line)
    if exit_code:
        raise SystemExit(exit_code)


@cli.command("diagnose")
@click.option(
    "--agent", "agents",
    type=click.Choice(["cursor", "windsurf", "claude", "copilot", "gemini", "codex", "opencode"]),
    multiple=True,
    help="Agent to check. Omit to check all.",
)
@click.option("--global/--no-global", "global_", default=True)
@click.option("--cwd", default=".")
def diagnose_cmd(agents: tuple, global_: bool, cwd: str) -> None:
    """Show hook registration status for each agent."""
    targets = list(agents) or list(_SUPPORTED_AGENTS)
    paths = _agent_config_paths(global_, cwd)

    for agent in targets:
        path = paths[agent]
        if agent == "opencode":
            if os.path.isfile(path):
                click.echo(f"  ✓ [opencode] Plugin installed ({path})")
            else:
                click.echo(f"  · [opencode] Not installed ({path})")
            continue
        if not os.path.exists(path):
            click.echo(f"  · [{agent}] Not found ({path})")
            continue
        doc = _load_json_file(path)
        hooks = doc.get("hooks", {})
        count = 0
        for entries in hooks.values():
            for entry in (entries if isinstance(entries, list) else []):
                # cursor: {"command": "otel-hook"}
                if "otel-hook" in entry.get("command", "") or "otel_hook" in entry.get("command", ""):
                    count += 1
                # copilot: {"bash": "otel-hook"}
                if _is_copilot_hook(entry):
                    count += 1
                # claude/gemini: {"hooks": [{"command": "otel-hook"}]}
                for h in entry.get("hooks", []):
                    if "otel-hook" in h.get("command", "") or "otel_hook" in h.get("command", ""):
                        count += 1
        status = f"{count} events registered" if count else "not registered"
        click.echo(f"  {'✓' if count else '·'} [{agent}] {status} ({path})")


@cli.command("uninstall")
@click.option(
    "--agent", "agents",
    type=click.Choice(["cursor", "windsurf", "claude", "copilot", "gemini", "codex", "opencode"]),
    multiple=True,
    required=True,
)
@click.option("--global/--no-global", "global_", default=True)
@click.option("--cwd", default=".")
def uninstall_cmd(agents: tuple, global_: bool, cwd: str) -> None:
    """Remove otel-hook entries from agent configs."""
    home = os.path.expanduser("~")
    for agent in agents:
        if agent == "opencode":
            path = (
                os.path.join(home, ".config", "opencode", "plugins", _OPENCODE_PLUGIN_FILENAME)
                if global_ else
                os.path.join(_find_repo_root(cwd), ".opencode", "plugins", _OPENCODE_PLUGIN_FILENAME)
            )
            if os.path.isfile(path):
                os.remove(path)
                click.echo(f"  ✓ [opencode] Removed plugin ({path})")
            else:
                click.echo(f"  · [opencode] Not installed ({path})")
            continue

        elif agent == "cursor":
            path = os.path.join(home, ".cursor", "hooks.json") if global_ else os.path.join(_find_repo_root(cwd), ".cursor", "hooks.json")
            doc = _load_json_file(path)
            hooks = doc.get("hooks", {})
            removed = 0
            for event in list(hooks.keys()):
                before = len(hooks[event])
                hooks[event] = [h for h in hooks[event] if "otel-hook" not in h.get("command", "") and "otel_hook" not in h.get("command", "")]
                removed += before - len(hooks[event])
                if not hooks[event]:
                    del hooks[event]
            if removed:
                _write_json_file(path, doc)
            click.echo(f"  {'✓' if removed else '·'} [cursor] Removed {removed} entries ({path})")

        elif agent == "claude":
            path = os.path.join(home, ".claude", "settings.json") if global_ else os.path.join(_find_repo_root(cwd), ".claude", "settings.json")
            settings = _load_json_file(path)
            hooks = settings.get("hooks", {})
            removed = 0
            for event in list(hooks.keys()):
                new_list = []
                for entry in hooks[event]:
                    surviving = [h for h in entry.get("hooks", []) if "otel-hook" not in h.get("command", "") and "otel_hook" not in h.get("command", "")]
                    if surviving:
                        entry["hooks"] = surviving
                        new_list.append(entry)
                    else:
                        removed += 1
                hooks[event] = new_list
                if not hooks[event]:
                    del hooks[event]
            if removed:
                _write_json_file(path, settings)
            click.echo(f"  {'✓' if removed else '·'} [claude] Removed {removed} entries ({path})")

        elif agent == "copilot":
            path = _agent_config_paths(global_, cwd)["copilot"]
            doc = _load_json_file(path)
            hooks = doc.get("hooks", {})
            removed = 0
            for event in list(hooks.keys()):
                before = len(hooks[event])
                hooks[event] = [h for h in hooks[event] if not _is_copilot_hook(h)]
                removed += before - len(hooks[event])
                if not hooks[event]:
                    del hooks[event]
            if removed:
                _write_json_file(path, doc)
            click.echo(f"  {'✓' if removed else '·'} [copilot] Removed {removed} entries ({path})")

        elif agent == "gemini":
            path = os.path.join(home, ".gemini", "settings.json") if global_ else os.path.join(_find_repo_root(cwd), ".gemini", "settings.json")
            settings = _load_json_file(path)
            hooks = settings.get("hooks", {})
            removed = 0
            for event in list(hooks.keys()):
                new_list = []
                for entry in hooks[event]:
                    surviving = [h for h in entry.get("hooks", []) if "otel-hook" not in h.get("command", "") and "otel_hook" not in h.get("command", "")]
                    if surviving:
                        entry["hooks"] = surviving
                        new_list.append(entry)
                    else:
                        removed += 1
                hooks[event] = new_list
                if not hooks[event]:
                    del hooks[event]
            if removed:
                _write_json_file(path, settings)
            click.echo(f"  {'✓' if removed else '·'} [gemini] Removed {removed} entries ({path})")

        elif agent == "codex":
            path = os.path.join(home, ".codex", "hooks.json") if global_ else os.path.join(_find_repo_root(cwd), ".codex", "hooks.json")
            doc = _load_json_file(path)
            hooks = doc.get("hooks", {})
            removed = 0
            for event in list(hooks.keys()):
                new_list = []
                for entry in hooks[event]:
                    before = len(entry.get("hooks", []))
                    surviving = [
                        h for h in entry.get("hooks", [])
                        if "otel-hook" not in h.get("command", "") and "otel_hook" not in h.get("command", "")
                    ]
                    removed += before - len(surviving)
                    if surviving:
                        entry["hooks"] = surviving
                        new_list.append(entry)
                hooks[event] = new_list
                if not hooks[event]:
                    del hooks[event]
            if removed:
                _write_json_file(path, doc)
            click.echo(f"  {'✓' if removed else '·'} [codex] Removed {removed} entries ({path})")


def run_cli() -> None:
    """Use UTF-8 for redirected Windows output before Click renders help/errors."""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    cli()


if __name__ == "__main__":
    run_cli()
