#!/usr/bin/env python3
"""Hook runtime for Cursor, Claude Code, Codex, Copilot, Gemini, Windsurf,
OpenCode and Antigravity.

Coordinates capture policy, cross-process session traces, generation batching,
OpenTelemetry export and passive harness responses. Provider parsing, span
attributes, identity, correlation, bootstrap and CLI setup live in otel_hooks.
Optional correlated logs retain the capture-permitted original hook JSON.

Usage:
    echo '{"hook_event_name":"sessionStart","session_id":"abc"}' | python3 otel_hook.py
"""
import contextlib
import importlib
import importlib.util
import importlib.metadata
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional

from otel_hooks import bootstrap, enrichment
from otel_hooks.streaming import plan as plan_streaming
from otel_hooks.semconv import completed_data, operation_attributes, scope_role, span_name as operation_span_name
from otel_hooks.timing import PersistedIdGenerator, export_identity, new_span_id, boundaries, matching_terminal, branch as event_branch
from otel_hooks.lifecycle import BufferedEventDecision, ToolInvocationCorrelator, enrich_buffered_event
from otel_hooks.workspace import _collect_repository_attributes, _resolve_repository_context
from otel_hooks.identity import (
    _detect_payload_client_name, _get_event_name, _get_os_info, _normalize_ide_name,
    _resolve_client_name, _resolved_agent_engine, _set_client_identity_attributes,
)
from otel_hooks.enrichment import (
    _HOOK_SCHEMA_VERSION, _TELEMETRY_SOURCE, _apply_enrichment_attributes,
    _apply_memory_summary_attrs, _set_if_present,
)
from otel_hooks.attributes import _OBSERVATION_FIELDS as _OBSERVATION_FIELDS
from otel_hooks.capture import CapturePolicy, bound_runtime_payload
from otel_hooks.config import (
    environment_settings, merge_settings, shared_claude_settings,
    export_enforced, enforce_export_environment,
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
    _CODEX_EVENTS,
    _COPILOT_EVENTS as _COPILOT_EVENTS,
    _WINDSURF_EVENT_ALIASES as _WINDSURF_EVENT_ALIASES,
    _INPUT_ALIASES as _INPUT_ALIASES,
    _GENERATION_START_EVENTS as _GENERATION_START_EVENTS,
    _GENERATION_END_EVENTS as _GENERATION_END_EVENTS,
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
    _hash_text as _hash_text,
    _lower_or_none,
    _mask_text as _mask_text,
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
    get_parser as _get_parser,
)
from otel_hooks.serialization import (
    _JsonFormatter as _JsonFormatter,
    _span_to_dict as _span_to_dict,
)
from enrichment_connectors import (
    aggregate_generation_memory,
    merge_memory_summaries,
    normalize_memory_summary,
)

# Whether to attach OS/host attributes to every span in addition to resource attributes.
# Defaults to False to avoid duplicate data and hot-path overhead.
_ATTACH_OS_ATTRIBUTES_PER_SPAN = os.getenv("IDE_HOOK_ATTACH_OS_PER_SPAN") == "1"

# ---------------------------------------------------------------------------
# Bootstrap: auto-provision .venv and add its site-packages to sys.path.
# Source checkouts reuse installed SDK dependencies or provision a compatible venv.
# First run: venv + pip install happens in background; tracing activates next
# invocation.
# ---------------------------------------------------------------------------


_HOOK_DIR = bootstrap.resolve_hook_home(__file__)
_VENV_DIR = bootstrap.select_bootstrap_venv_dir(_HOOK_DIR)
_SETUP_LOCK = os.path.join(
    _HOOK_DIR,
    ".state",
    f"setup-py{sys.version_info.major}.{sys.version_info.minor}.lock",
)
_RUNTIME_HAS_OTEL_DEPENDENCIES = bootstrap.runtime_has_otel_dependencies()
if not _RUNTIME_HAS_OTEL_DEPENDENCIES:
    bootstrap.auto_provision_venv(_VENV_DIR, _SETUP_LOCK)
    _venv_site_packages = bootstrap.venv_site_packages_path(_VENV_DIR)
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

    def get_tracer(self, _name: str, version=None):
        if _REAL_TRACE is not None:
            return _REAL_TRACE.get_tracer(_name, version)
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
_LOCK_DIR = os.path.join(_STATE_DIR, "locks")
_CLEANUP_MARKER = os.path.join(_STATE_DIR, "last_cleanup")
_DELIVERY_HEALTH_PATH = os.path.join(_STATE_DIR, "delivery_health.json")

# MDM (Managed Device Management) configuration
_MDM_DOMAIN = "dev.o11y.opentelemetry-hook"  # macOS managed preferences domain
_MDM_REGISTRY_PATH = r"SOFTWARE\Policies\OpenTelemetryHook"  # Windows registry path

# Privacy patterns
_HEX_DIGITS = frozenset("0123456789abcdef")


# ---------------------------------------------------------------------------
# Event name canonicalization (all IDE variants -> PascalCase)
# ---------------------------------------------------------------------------

# Common camelCase -> snake_case aliases used by compatible hook runners.
# Claude Code's documented hook payloads are already snake_case, but generic
# runners and workflow adapters that forward Claude- or Antigravity-style
# events may supply camelCase fields instead.

# Canonical o11y.client.name values accepted directly from IDE_OTEL_IDE_NAME or
# self-reported payload metadata before alias fallback.
_MANAGED_HOOK_SOURCE_ENV = "IDE_OTEL_HOOK_SOURCE"
_CLI_HOOK_SOURCE: Optional[str] = None
_CLI_HOOK_EVENT: Optional[str] = None
_CLI_REGISTRATION_SCOPE: Optional[str] = None
_POLICY_SOURCES: dict = {}

# Session boundary events
_SESSION_START_EVENTS = {"SessionStart"}
_SESSION_END_EVENTS = {"SessionEnd"}

# GenAI operation mapping (canonical PascalCase)

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


def _first_env(keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
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
                if name.endswith('.quarantine.json'):
                    continue
                if name.endswith('.identity.json') and os.path.exists(path.removesuffix('.identity.json')):
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
    """Emit ``o11y.client.session`` root spans for stale sessions that were never closed.

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
    shared_options = merge_settings(config, mdm).get("claude", {})
    if not isinstance(shared_options, dict):
        raise ValueError("claude configuration must be an object")
    sources = {}
    if shared_options.get("shared_config") is True and _CLI_HOOK_SOURCE == "claude":
        shared, sources = shared_claude_settings(managed_only=export_enforced(mdm))
        # Explicit hook settings override imported defaults. Enforced MDM settings
        # use only the shared source and administrator values, never local JSON.
        config = merge_settings(shared, config)
        if export_enforced(mdm):
            mdm = merge_settings(shared, mdm)
    if _safe_bool(os.getenv("IDE_OTEL_SKIP_MDM", "")):
        mdm = {key: value for key, value in mdm.items()
               if key in {"capture", "logs"} or export_enforced(mdm)}
    if mdm:
        config = merge_settings(config, mdm)
        config["_managed"] = mdm
    if mdm or sources:
        config["_policy_sources"] = {
            "managed": bool(mdm), "export_enforced": export_enforced(mdm),
            "claude_settings": sorted(set(sources.values())),
        }
    return config


def _apply_config_env(config: dict) -> None:
    global _CAPTURE_POLICY, _POLICY_SOURCES
    _POLICY_SOURCES = config.get("_policy_sources", {})
    managed = config.get("_managed", {})
    settings = environment_settings(config)
    settings.update(environment_settings(managed))
    for key, value in settings.items():
        os.environ.setdefault(key, _coerce_env_value(key, value))
    enforce_export_environment(managed)
    # Managed capture/log restrictions cannot be loosened by local environment.
    restrictions = {key: value for key, value in managed.items() if key in {"capture", "logs"}}
    if isinstance(restrictions.get("capture"), dict) and "mode" in restrictions["capture"]:
        # A managed selected-content mode with no include list permits metadata
        # only; do not inherit a user's content allowlist.
        restrictions["capture"] = {"include": [], **restrictions["capture"]}
    for key, value in environment_settings(restrictions).items():
        os.environ[key] = _coerce_env_value(key, value)
    effective = merge_settings(config, restrictions)
    _CAPTURE_POLICY = replace(
        CapturePolicy.from_config(effective),
        mask=_safe_bool(os.getenv('IDE_OTEL_MASK_PROMPTS', 'false')),
        logs_enabled=_safe_bool(os.getenv('IDE_OTEL_ENABLE_LOGS', 'false')),
        user_identity=_safe_bool(os.getenv('IDE_OTEL_CAPTURE_USER_IDENTITY', 'false')),
    )
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
        sdk_provider = SDKTracerProvider(resource=Resource.create(resource_attrs), id_generator=PersistedIdGenerator(), shutdown_on_exit=_EXPORT_DEADLINE is None)
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

    sdk_provider = SDKTracerProvider(resource=Resource.create(resource_attrs), id_generator=PersistedIdGenerator(), shutdown_on_exit=_EXPORT_DEADLINE is None)
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

    # Retain original observation/event times, including sub-microsecond precision.
    from otel_hooks.serialization import CallbackLogTimeMixin

    class CallbackLoggingHandler(CallbackLogTimeMixin, LoggingHandler):
        def emit(self, record):
            logger_provider.get_logger(record.name, _hook_distro_attributes().get('telemetry.distro.version')).emit(self._translate(record))

    _OTEL_LOG_HANDLER = CallbackLoggingHandler(level=logging.DEBUG, logger_provider=logger_provider)
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
                            if attributes.get("o11y.client.session_id") != self._expected_session_key:
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
    resource_attrs.setdefault("o11y.client.name", resolved_client_name)
    if ide != resolved_client_name:
        resource_attrs.setdefault("o11y.client.wrapper", ide)
    resource_attrs.update(_hook_distro_attributes())
    resource_attrs.setdefault("o11y.client.telemetry_source", _TELEMETRY_SOURCE)
    resource_attrs["o11y.client.hook_schema_version"] = _HOOK_SCHEMA_VERSION

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


def _event_adapter_for(ide: str) -> ProviderEventAdapter:
    return _get_parser(ide, capture_policy=_CAPTURE_POLICY)


def _apply_operation_status(span, event_name: str, data: dict) -> None:
    """Set OTel status for real failures without leaking error descriptions."""
    if _operation_failed(event_name, data) and Status is not None and StatusCode is not None and hasattr(span, "set_status"):
        error = data.get('error')
        error_type = data.get('error_type') or (error.get('type') if isinstance(error, dict) else None)
        span.set_attribute('error.type', error_type if isinstance(error_type, str) and error_type else '_OTHER')
        span.set_status(Status(StatusCode.ERROR))


def _operation_failed(event_name: str, data: dict) -> bool:
    if data.get('is_interrupt'):
        return False
    status = _lower_or_none(data.get('status') or data.get('mcp_status'))
    return bool(data.get('_hook_failure_observed') or event_name in {'ErrorOccurred', 'PostToolUseFailure', 'StopFailure'}
                or data.get('error') is not None
                or (type(data.get('error_length')) in (int, float) and data['error_length'] > 0)
                or status in {'error', 'failed', 'failure'}
                or (type(data.get('exit_code')) is int and data['exit_code'] != 0))


# ---------------------------------------------------------------------------
# Correlated hook log emission
# ---------------------------------------------------------------------------
def _get_otel_logger(name: str) -> logging.Logger:
    """Get or create a Python logger with the OTel log bridge handler attached."""
    logger = logging.getLogger(f"otel_hook.{name}")
    logger.propagate = False
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
        "o11y.client.hook.event": event_name,
        "o11y.client.telemetry_source": _TELEMETRY_SOURCE,
    }
    for source, target in (("_hook_event_id", "hook.event_id"), ("session_id", "session_id"),
                           ("conversation_id", "conversation_id"), ("turn_id", "turn_id"),
                           ("generation_id", "generation_id")):
        if data.get(source) is not None:
            attrs[f"o11y.client.{target}"] = data[source]
    _inject_trace_context(attrs)
    if data.get('_hook_provider_adapter'):
        attrs['o11y.client.hook.provider_adapter'] = data['_hook_provider_adapter']
    invocation = data.get('tool_use_id') or data.get('tool_call_id') or data.get('call_id')
    if invocation is not None:
        attrs['gen_ai.tool.call.id'] = invocation
    for key in ('event', 'observed'):
        value = data.get('_hook_' + ('event_time_ns' if key == 'event' else 'observed_time_ns'))
        if type(value) is int:
            attrs['o11y.time.' + key + '_ns'] = value
    attrs['o11y.client.hook_schema_version'] = _HOOK_SCHEMA_VERSION
    attrs['o11y.capture.mode'] = _CAPTURE_POLICY.mode
    attrs['o11y.capture.policy_id'] = _CAPTURE_POLICY.fingerprint
    for key in ('tool_use_id', 'tool_call_id', 'agent_id'):
        if data.get(key) is not None:
            attrs['o11y.client.' + key] = data[key]
    record = logging.LogRecord('otel_hook.hooks', logging.ERROR if _operation_failed(event_name, data) else logging.INFO,
                               '', 0, body, (), None)
    if data.get('_hook_event_time_ns') is not None:
        record.created = data['_hook_event_time_ns'] / 1e9
    record.__dict__.update(attrs)
    _get_otel_logger('hooks').handle(record)


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
        "session_span_id": new_span_id(),
        "phantom_parent_id": f"{random.getrandbits(64):016x}",
        "trace_id": f"{random.getrandbits(128):032x}",
        "start_time_ns": data.get("_hook_event_time_ns") or data.get("_hook_observed_time_ns") or time.time_ns(),
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
    if isinstance(data.get('conversation_id'), str) and data['conversation_id']:
        ctx['conversation_id'] = data['conversation_id']

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
            "o11y.client.repository.remote.sha256",
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
    if not session_key or not session_ctx or (session_ctx.get("context_origin") == "upstream" or session_ctx.get("identity_committed")):
        return session_ctx
    upstream_ctx = _resolve_upstream_trace_context(data)
    if upstream_ctx is None:
        return session_ctx

    def mutate(latest: dict) -> None:
        if latest.get("context_origin") == "upstream" or latest.get("identity_committed"):
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


def _batch_path(key: str) -> str:
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
    return os.path.join(_BATCH_DIR, f"{safe_key}.jsonl")


def _append_batch_event(key: str, event_name: str, data: dict) -> None:
    os.makedirs(_BATCH_DIR, exist_ok=True)
    observed = data.get('_hook_observed_time_ns') or time.time_ns()
    record = {"event": event_name, "timestamp_ns": data.get('_hook_event_time_ns') or observed,
              "observed_time_ns": observed, "span_id": new_span_id(), "data": data}
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


def _buffer_session_event(session_key: str, event_name: str, data: dict, ide: str) -> BufferedEventDecision:
    """Atomically prepare and append a generation-owned event."""
    now_ns = time.time_ns()

    def mutate(session_ctx: dict) -> BufferedEventDecision:
        if session_ctx.get("finalizing"):
            return BufferedEventDecision(None, data, duplicate=True)
        explicit_gen = _generation_key_from_data(data)
        if event_name in _GENERATION_START_EVENTS:
            event_id = data.get("_hook_event_id")
            if (
                session_ctx.get("current_generation")
                and event_id
                and session_ctx.get("active_prompt_event_id") == event_id
            ):
                return BufferedEventDecision(
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

        decision = ToolInvocationCorrelator(
            session_ctx, ide, gen_key, now_ns,
            ttl_seconds=_state_ttl_seconds(), capture_content=_CAPTURE_POLICY.content_enabled,
        ).prepare(
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
            return BufferedEventDecision(gen_key, data)
    return decision


def _buffer_generation_end(session_key: str, event_name: str, data: dict, ide: str) -> BufferedEventDecision:
    """Append a terminal callback, creating an incomplete generation if needed."""
    now_ns = time.time_ns()

    def mutate(session_ctx: dict) -> BufferedEventDecision:
        if session_ctx.get("finalizing"):
            return BufferedEventDecision(None, data, duplicate=True)
        gen_key = _generation_key_from_data(data) or session_ctx.get("current_generation")
        if not gen_key:
            pending = session_ctx.get("pending_generations") or []
            gen_key = pending[-1] if pending else None
        if not gen_key:
            gen_key = _new_generation_key(session_key, session_ctx)
        decision = ToolInvocationCorrelator(
            session_ctx, ide, gen_key, now_ns,
            ttl_seconds=_state_ttl_seconds(), capture_content=_CAPTURE_POLICY.content_enabled,
        ).prepare(
            event_name,
            data,
        )
        if not decision.duplicate:
            _append_batch_event(gen_key, event_name, decision.data)
        session_ctx["last_seen_at_ns"] = now_ns
        return decision

    _ctx, decision = _update_session_context(session_key, mutate)
    return decision or BufferedEventDecision(None, data)


def _prepare_streaming_session_event(
    session_key: str,
    event_name: str,
    data: dict,
    ide: str,
) -> BufferedEventDecision:
    """Apply the same persisted dedupe/correlation policy without writing a batch."""
    now_ns = time.time_ns()

    def mutate(session_ctx: dict) -> BufferedEventDecision:
        if session_ctx.get("finalizing"):
            return BufferedEventDecision(None, data, duplicate=True)
        explicit_gen = _generation_key_from_data(data)
        if event_name in _GENERATION_START_EVENTS:
            event_id = data.get("_hook_event_id")
            if (
                session_ctx.get("current_generation")
                and event_id
                and session_ctx.get("active_prompt_event_id") == event_id
            ):
                return BufferedEventDecision(
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
        decision = ToolInvocationCorrelator(
            session_ctx, ide, gen_key, now_ns,
            ttl_seconds=_state_ttl_seconds(), capture_content=_CAPTURE_POLICY.content_enabled,
        ).prepare(
            event_name,
            data,
        )
        session_ctx["last_seen_at_ns"] = now_ns
        return decision

    _ctx, decision = _update_session_context(session_key, mutate)
    return decision or BufferedEventDecision(None, data)


def _emit_streaming_event(tracer, session_key, event_name, data, ide, generation_key):
    """Persist scope and callback identities before retryable SDK export."""
    data.setdefault('_hook_observed_time_ns', time.time_ns())
    current, records = _update_session_context(
        session_key, lambda latest: (list(latest.get('stream_outbox', [])) if data.get('_replay_only')
                                     else plan_streaming(latest, event_name, data, generation_key)),
    )
    if not current or records is None:
        return False
    for record in records:
        role = record.get('role', 'callback')
        parent = _make_trace_context(record['trace_id'], record['parent_span_id'],
                                     current.get('trace_flags', '01'), current.get('tracestate'))
        with export_identity(record['trace_id'], record['span_id']):
            span = tracer.start_span(record['name'], context=parent, start_time=record['start'],
                                     kind=SpanKind.INTERNAL,
                                     attributes=_initial_span_attributes(role, record['event'], record['data']),
                                     links=_event_span_links(record['data'], current))
        span.set_attribute('o11y.timing.basis', record['basis'])
        span.set_attribute('o11y.timing.inferred', record['basis'] != 'matching_callback')
        if record.get('missing'):
            span.set_attribute('o11y.timing.missing_end', True)
        with _span_context(span):
            if record.get('scope_only'):
                enrichment.populate_span(span, record['event'], record['data'], ide,
                                         session_ctx=current, capture_policy=_CAPTURE_POLICY, span_role=role)
                _apply_operation_status(span, record['event'], record['data'])
            else:
                _populate_span(span, record['event'], record['data'], ide, session_ctx=current)
                _remember_agent_span_context(session_key, current, record['event'], record['data'], span)
        span.set_attribute('o11y.span.role', role)
        if record['end'] < record['start']:
            span.set_attribute('o11y.timing.end_before_start', True)
        span.end(end_time=record['end'])
    success = _force_flush_provider(authoritative_signal='traces')
    if success:
        exported = {record['span_id'] for record in records}
        _update_session_context(session_key, lambda latest: latest.__setitem__(
            'stream_outbox', [r for r in latest.get('stream_outbox', []) if r['span_id'] not in exported]))
    else:
        def failed(latest):
            retained = []
            attempted = {record['span_id'] for record in records}
            for record in latest.get('stream_outbox', []):
                if record['span_id'] in attempted:
                    record['attempts'] = record.get('attempts', 0) + 1
                if record.get('attempts', 0) >= _CAPTURE_POLICY.max_export_attempts:
                    _atomic_write_json(os.path.join(_BATCH_DIR, record['span_id'] + '.quarantine.json'), {
                        'record': record, 'trace_id': latest['trace_id'],
                        'policy_id': _CAPTURE_POLICY.fingerprint, 'reason': 'export_attempts_exhausted',
                    })
                    _LOGGER.error('Streaming export attempts exhausted; policy-filtered record quarantined')
                else:
                    retained.append(record)
            latest['stream_outbox'] = retained
        _update_session_context(session_key, failed)
    return success


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
                item for item in invocations if item.get("generation_key") != gen_key or item.get("state") == "open"
            ]
        agents = session_ctx.get("agent_invocations")
        if isinstance(agents, list):
            session_ctx["agent_invocations"] = [
                item for item in agents if item.get("generation_key") != gen_key or item.get("state") == "open"
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
        session_ctx.get("session_span_id") or session_ctx.get("upstream_parent_span_id") or session_ctx.get("phantom_parent_id", "0"),
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
            "o11y.client.native_source": (
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
        return [Link(context, attributes={"o11y.client.link.type": "delegation"})]
    except (TypeError, ValueError):
        return []


def _event_span_links(data: dict, session_ctx: Optional[dict]) -> list:
    links = _native_span_links(data)
    links.extend(_agent_relationship_links(data, session_ctx))
    upstream = _resolve_upstream_trace_context(data)
    if upstream and session_ctx and session_ctx.get('identity_committed') and upstream['trace_id'] != session_ctx.get('trace_id'):
        if SpanContext is not None and Link is not None:
            context = SpanContext(trace_id=int(upstream['trace_id'], 16), span_id=int(upstream['parent_span_id'], 16),
                                  is_remote=True, trace_flags=TraceFlags(int(upstream.get('trace_flags', '01'), 16)),
                                  trace_state=TraceState())
            links.append(Link(context, attributes={'o11y.relationship': 'late_upstream'}))
    return links


def _remember_agent_span_context(
    session_key: Optional[str],
    session_ctx: Optional[dict],
    event_name: str,
    data: dict,
    span,
) -> None:
    is_agent = event_name == 'SubagentStart' and data.get('agent_id')
    is_tool = event_name == 'PreToolUse' and data.get('tool_use_id')
    if not (is_agent or is_tool) or not session_key or not session_ctx:
        return
    collection, identity_key = ('agent_invocations', 'agent_id') if is_agent else ('tool_invocations', 'tool_use_id')
    try:
        context = span.get_span_context()
        if context is None or not context.is_valid:
            return
        trace_id = f"{context.trace_id:032x}"
        span_id = f"{context.span_id:016x}"
    except Exception:
        return

    def mutate(latest: dict) -> None:
        for invocation in reversed(latest.get(collection, [])):
            if invocation.get(identity_key) == data.get(identity_key):
                invocation["start_trace_id"] = trace_id
                invocation["start_span_id"] = span_id
                break

    _update_session_context(session_key, mutate)
    for invocation in reversed(session_ctx.get(collection, [])):
        if invocation.get(identity_key) == data.get(identity_key):
            invocation["start_trace_id"] = trace_id
            invocation["start_span_id"] = span_id
            break


# ---------------------------------------------------------------------------
# Span population
# ---------------------------------------------------------------------------
def _initial_span_attributes(role, event, data, model=None):
    """Make known semantic identity available when the SDK samples a span."""
    attrs = operation_attributes(role, event, data)
    model = _first_present(data, ('request_model', 'model', 'model_name')) or model
    provider = enrichment._infer_genai_provider(data if not model else {**data, 'request_model': model})
    if provider:
        attrs['gen_ai.provider.name'] = provider
    if isinstance(model, str) and model:
        attrs['gen_ai.request.model'] = model
    conversation = data.get('conversation_id') or data.get('session_id')
    if isinstance(conversation, str) and conversation:
        attrs['gen_ai.conversation.id'] = conversation
    return attrs


def _populate_span(span, event_name: str, data: dict, ide: str, session_ctx: Optional[dict] = None, batch_model: Optional[str] = None, *, span_role: str = 'callback') -> None:
    """Enrich the active span, emit its correlated hook log, then set status."""
    enrichment.populate_span(
        span, event_name, data, ide, session_ctx=session_ctx, batch_model=batch_model,
        capture_policy=_CAPTURE_POLICY, attach_os_attributes=_ATTACH_OS_ATTRIBUTES_PER_SPAN,
        span_role=span_role,
    )
    _emit_event_log(event_name, data)
    _apply_operation_status(span, event_name, data)


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
        entry["data"] = enrich_buffered_event(
            entry.get("data") or {},
            session_ctx,
        )

    identity_path = _batch_path(gen_key) + '.identity.json'
    try:
        with open(identity_path, encoding='utf-8') as fh:
            identity = json.load(fh)
    except FileNotFoundError:
        identity = {'span_id': new_span_id(), 'trace_id': (session_ctx or {}).get('trace_id') or uuid.uuid4().hex,
                    'callbacks': {}}
    for entry in batch:
        key = entry.get('span_id') or (entry.get('data') or {}).get('_hook_event_id')
        if key is None:
            key = str(batch.index(entry))
        entry['span_id'] = identity['callbacks'].setdefault(key, entry.get('span_id') or new_span_id())
    _atomic_write_json(identity_path, identity)
    if session_ctx is not None:
        session_ctx['identity_committed'] = True
        owner = _session_key(batch[0].get('data') or {})
        if owner:
            _update_session_context(owner, lambda latest: latest.__setitem__('identity_committed', True))
    intervals = boundaries(batch)
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
    memory_summary = aggregate_generation_memory(batch, repo_root=repo_root)
    first_event_data = batch[0].get("data") if batch else {}

    span_kind = SpanKind.INTERNAL if SpanKind is not None else None
    with export_identity(identity['trace_id'], identity['span_id']):
        gen_span = tracer.start_span(
            operation_span_name('generation', 'UserPromptSubmit', first_event_data), kind=span_kind,
            attributes=_initial_span_attributes('generation', 'UserPromptSubmit', first_event_data, batch_model),
            context=parent_ctx, start_time=first_ts,
        )
    gen_ctx = trace.set_span_in_context(gen_span)
    agent_parents = {item['agent_id']: item['start_span_id']
                     for item in (session_ctx or {}).get('agent_invocations', [])
                     if item.get('agent_id') and item.get('start_span_id')}
    tool_parents = {(item.get('agent_id'), item['tool_use_id']): item['start_span_id']
                    for item in (session_ctx or {}).get('tool_invocations', [])
                    if item.get('tool_use_id') and item.get('start_span_id')}
    with _span_context(gen_span):
        gen_span.set_attribute("o11y.span.role", "generation")
        gen_span.set_attribute("o11y.client.generation_id", gen_key)
        _set_if_present(
            gen_span,
            "o11y.client.session_id",
            _session_key(first_event_data) or (session_ctx or {}).get("session_id"),
        )
        gen_span.set_attribute("o11y.client.event.count", len(batch))
        _set_if_present(gen_span, "o11y.client.agent_id", (session_ctx or {}).get("root_agent_id"))
        _set_if_present(gen_span, "gen_ai.conversation.id", first_event_data.get('conversation_id') or
                        (session_ctx or {}).get('conversation_id') or _session_key(first_event_data) or (session_ctx or {}).get("session_id"))
        _set_if_present(gen_span, "gen_ai.request.model", batch_model)
        _apply_memory_summary_attrs(gen_span, "o11y.client.memory", memory_summary)
        _set_client_identity_attributes(gen_span, ide, data=first_event_data, session_ctx=session_ctx)
        _apply_enrichment_attributes(gen_span, first_event_data, session_ctx=session_ctx)
        _log_with_span(_LOGGER, logging.INFO, gen_span, "Generation span: gen_key=%s events=%d", gen_key, len(batch))

        for idx, entry in enumerate(batch):
            evt = entry.get("event") or "unknown"
            evt_data = entry.get("data") or {}
            ts = entry.get("timestamp_ns") or time.time_ns()
            next_ts, timing_basis, missing_end = intervals[idx]

            callback_parent = gen_ctx
            agent_id = evt_data.get('agent_id') or evt_data.get('subagent_id')
            tool_id = evt_data.get('tool_use_id') or evt_data.get('tool_call_id') or evt_data.get('call_id')
            parent_id = agent_parents.get(agent_id) if evt != 'SubagentStart' else agent_parents.get(evt_data.get('parent_agent_id'))
            if tool_id and evt in {'PostToolUse', 'PostToolUseFailure', 'AfterMCPExecution', 'AfterShellExecution'}:
                parent_id = tool_parents.get((agent_id, tool_id)) or parent_id
            if parent_id:
                callback_parent = _make_trace_context(identity['trace_id'], parent_id)
            if evt == 'SubagentStart' and agent_id:
                agent_parents[agent_id] = entry['span_id']
            if evt in {'PreToolUse', 'BeforeMCPExecution', 'BeforeShellExecution'} and tool_id:
                tool_parents[(agent_id, tool_id)] = entry['span_id']
            with export_identity(identity['trace_id'], entry['span_id']):
                role = ('callback' if evt_data.get('_mcp_correlated_evidence') else scope_role(evt))
                span = tracer.start_span(
                    operation_span_name(role, evt, evt_data), kind=span_kind,
                    attributes=_initial_span_attributes(role, evt, evt_data, batch_model),
                    context=callback_parent, start_time=ts,
                    links=_event_span_links(evt_data, session_ctx),
                )
            span.set_attribute('o11y.timing.basis', timing_basis)
            span.set_attribute('o11y.timing.inferred', timing_basis != 'matching_callback')
            if missing_end:
                span.set_attribute('o11y.timing.missing_end', True)
            with _span_context(span):
                terminal = matching_terminal(batch, idx) if role in {'tool', 'subagent'} else None
                scope_data = completed_data(evt_data, terminal['data'], terminal['event']) if terminal else evt_data
                _populate_span(span, evt, scope_data, ide, session_ctx=session_ctx, batch_model=batch_model, span_role=role)
                _remember_agent_span_context(
                    _session_key(evt_data),
                    session_ctx,
                    evt,
                    evt_data,
                    span,
                )
            span.set_attribute('o11y.span.role', role)
            span.end(end_time=next_ts)

        terminal = next((entry for entry in reversed(batch) if entry.get('event') in {'Stop', 'StopFailure', 'Interrupt'}
                         and event_branch(entry.get('data') or {}) == event_branch(first_event_data)), None)
        if terminal:
            _apply_operation_status(gen_span, terminal['event'], terminal['data'])
        gen_span.end(end_time=last_ts)

    success = not flush or _force_flush_provider(authoritative_signal="traces")
    if success:
        if session_ctx is not None:
            session_memory = session_ctx.setdefault("memory", {})
            merge_memory_summaries(session_memory, memory_summary, repo_root=repo_root)
        _clear_batch_events(gen_key)
        try:
            os.remove(identity_path)
        except FileNotFoundError:
            pass
    else:
        identity['attempts'] = identity.get('attempts', 0) + 1
        if identity['attempts'] >= _CAPTURE_POLICY.max_export_attempts:
            _atomic_write_json(identity_path + '.quarantine.json', {
                'identity': identity, 'events': batch, 'session_context': session_ctx,
                'policy_id': _CAPTURE_POLICY.fingerprint, 'reason': 'export_attempts_exhausted',
            })
            _clear_batch_events(gen_key)
            os.remove(identity_path)
            _LOGGER.error('Generation export attempts exhausted; policy-filtered records quarantined')
        else:
            _atomic_write_json(identity_path, identity)
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
    end_ns = session_ctx.get("end_time_ns") or time.time_ns()
    session_batch = sorted(
        _load_batch_events(f"{session_key}_session"),
        key=lambda e: e.get("timestamp_ns") or 0,
    )
    first_event_data = session_batch[0].get("data") if session_batch else {}

    parent_ctx = _session_trace_context(session_ctx)

    span_kind = SpanKind.INTERNAL if SpanKind is not None else None
    # Children already reference this persisted session ID. The session itself
    # is parented only to a validated upstream context, never to itself.
    parent_ctx = (_make_trace_context(session_ctx['trace_id'], session_ctx['upstream_parent_span_id'],
                                    session_ctx.get('trace_flags', '01'), session_ctx.get('tracestate'))
                  if session_ctx.get('upstream_parent_span_id') else None)
    with export_identity(session_ctx['trace_id'], session_ctx.get('session_span_id') or new_span_id()):
        session_span = tracer.start_span(
            "o11y.client.session", kind=span_kind,
            context=parent_ctx, start_time=start_ns,
        )
    with _span_context(session_span):
        for entry in session_batch:
            data = entry.get("data") or {}
            event = entry["event"]
            timestamp = entry.get("timestamp_ns") or end_ns
            with export_identity(session_ctx['trace_id'], entry.get('span_id') or new_span_id()):
                hook_span = tracer.start_span(
                    f"o11y.client.hook.{event}", kind=span_kind,
                    context=trace.set_span_in_context(session_span), start_time=timestamp,
                )
            with _span_context(hook_span):
                _populate_span(hook_span, event, data, ide, session_ctx=session_ctx)
            hook_span.end(end_time=timestamp)
        session_span.set_attribute("o11y.span.role", "session")
        session_span.set_attribute("o11y.client.session_id", session_key)
        _set_if_present(session_span, "o11y.client.agent_id", session_ctx.get("root_agent_id"))
        session_span.set_attribute("gen_ai.conversation.id", session_ctx.get("conversation_id") or session_ctx.get("session_id") or session_key)
        _set_if_present(
            session_span,
            "o11y.client.agent_id_source",
            session_ctx.get("root_agent_id_source"),
        )
        _set_client_identity_attributes(session_span, ide, data=first_event_data, session_ctx=session_ctx)
        session_span.set_attribute("o11y.client.generation_count", session_ctx.get("generation_count", 0))
        session_span.set_attribute("o11y.client.session.duration_ms", (end_ns - start_ns) // 1_000_000)
        _apply_enrichment_attributes(session_span, first_event_data, session_ctx=session_ctx)
        _apply_memory_summary_attrs(session_span, "o11y.client.memory", session_ctx.get("memory", {}))
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
            lambda latest: latest.update({'finalizing': True, 'end_time_ns': latest.get('end_time_ns') or time.time_ns()}),
        )
        if not current_ctx:
            return True
        session_ctx = current_ctx
        if any(session_ctx.get(key) for key in ('stream_scopes', 'stream_outbox', 'stream_generations')):
            # Maintenance closes missing scope callbacks, without fabricating a
            # callback log or changing the original observed timestamps.
            if not _emit_streaming_event(tracer, session_key, 'SessionEnd', {
                'session_id': session_key, '_hook_observed_time_ns': session_ctx['end_time_ns'],
                '_scope_closure_only': True, '_hook_timeout': True,
            }, ide, None):
                return False
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
        event = _event_adapter_for(ide).canonical_event_name(_get_event_name(data, override=_CLI_HOOK_EVENT))
        _emit_stdout_response(event, ide, data)
        return 0
    _configure_logging()

    input_data = _load_input()
    if not isinstance(input_data, dict):
        input_data = {}
    observed_time_ns = time.time_ns()
    original_data = data = _normalize_input_data(input_data)
    raw_event = _get_event_name(data, override=_CLI_HOOK_EVENT)
    ide = _detect_ide(data)
    from otel_hooks.managed import should_skip
    if should_skip(ide, _CLI_REGISTRATION_SCOPE, raw_event):
        event = _event_adapter_for(ide).canonical_event_name(raw_event)
        _emit_stdout_response(event, ide, data)
        return 0
    captured_payload = _CAPTURE_POLICY.filter(input_data)
    # Never trust incoming private keys as runtime-generated state.
    parser_input = {k: v for k, v in captured_payload.items() if not k.startswith("_")}
    # Filtering preserves leaf types. Compare before aliases are added, which
    # could otherwise hide changed values through Python's True == 1 equality.
    input_unchanged = parser_input == input_data
    data = _normalize_input_data(parser_input)
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
    observed = canonical_event if input_unchanged else adapter.parse(
        raw_event, original_data, session_ctx=initial_session_ctx,
    )
    if canonical_event.event_id_source != "provider":
        # Content equality is not callback identity. Generate once at observation,
        # then persist it with the record for replay.
        data['_hook_event_id'] = 'hook:' + str(uuid.uuid4())
    if _operation_failed(event_name, original_data):
        data['_hook_failure_observed'] = True
    data['_hook_observed_time_ns'] = observed_time_ns
    event_ns = input_data.get('timestamp_ns')
    if type(event_ns) is int and event_ns > 0:
        data['_hook_event_time_ns'] = event_ns
    else:
        data['_hook_event_time_ns'] = observed_time_ns
    data['_payload_facts'] = {**data.get('_payload_facts', {}), **observed.to_lifecycle_data().get('_payload_facts', {})}
    records = data.setdefault("_conversation_records", [])
    retained = {record["kind"] for record in records}
    records.extend({"kind": record.kind, "role": record.role, "length": record.length, "bytes": record.bytes, "sha256": record.sha256}
                   for record in observed.conversation if record.kind not in retained)
    data["_hook_attributes"] = _CAPTURE_POLICY.attributes(captured_payload)
    if _safe_bool(os.getenv("IDE_OTEL_ENABLE_LOGS", "false")):
        log_size = len(json.dumps(captured_payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        limit = _CAPTURE_POLICY.max_log_body_bytes
        if limit is not None and log_size > limit:
            data['_hook_log_body'] = {'o11y.body.omitted_reason': 'max_body_bytes', 'o11y.body.bytes': log_size}
        else:
            data['_hook_log_body'] = captured_payload
    data = bound_runtime_payload(data, _CAPTURE_POLICY)
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

    try:
        initialized = _init_tracing(
            ide,
            client_name=_resolve_client_name(ide, data=data, agent_engine=agent_engine, session_ctx=session_ctx),
            resource_attributes=_collect_repository_attributes(data, session_ctx=session_ctx),
        )
    except Exception as exc:
        # Invalid exporters must not consume the harness protocol response.
        _LOGGER.warning('Telemetry initialization failed (%s)', type(exc).__name__)
        initialized = False
    if not initialized:
        _emit_stdout_response(event_name, ide, data)
        return 0

    tracer = trace.get_tracer("ide-hooks", _hook_distro_attributes().get("telemetry.distro.version"))
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
                f"o11y.client.hook.{event_name}", kind=SpanKind.INTERNAL,
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
                if not (session_ctx or {}).get('stream_outbox'):
                    _emit_stdout_response(event_name, ide, streaming_decision.data)
                    return 0
                streaming_decision.data['_replay_only'] = True
            data = streaming_decision.data
            session_ctx = _load_session_context(sk) or session_ctx
            if streaming_decision.correlated:
                data = dict(data)
                data["_mcp_correlated_evidence"] = True

        if sk:
            if not _emit_streaming_event(tracer, sk, event_name, data, ide,
                                         streaming_decision.generation_key if streaming_decision else None):
                _emit_stdout_response(event_name, ide, data)
                return 0
        else:
            with tracer.start_as_current_span(
                f"o11y.client.hook.{event_name}", kind=SpanKind.INTERNAL,
                context=parent_ctx, links=_event_span_links(data, session_ctx),
            ) as span:
                span.set_attribute('o11y.timing.basis', 'hook_execution')
                _populate_span(span, event_name, data, ide, session_ctx=session_ctx)

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


if __name__ == "__main__":
    from otel_hooks.cli import run_cli
    run_cli(runtime=sys.modules[__name__])
