"""Comprehensive tests for otel_hook.py — the IDE Agent OpenTelemetry Hook.

Tests cover helper functions, event normalisation, IDE detection, privacy
controls, config loading, session/batch state management, GenAI semantic
conventions, OTel log emission, OTLP header/resource-attribute parsing,
logs endpoint derivation, and end-to-end main() stdin→stdout flow.
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import textwrap
import time
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Import helpers — avoid side-effects from _auto_provision_venv
# ---------------------------------------------------------------------------
# The module runs _auto_provision_venv() at import time which tries to create
# a .venv directory.  We mock it away so tests stay self-contained.
# We also need the OTel SDK on the test-runner's Python (installed via
# requirements-dev.txt).

_HOOK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HOOK_DIR)

with mock.patch.dict(os.environ, {}, clear=False):
    import otel_hook


# ===================================================================
# Helper function tests
# ===================================================================

class TestSafeBool:
    @pytest.mark.parametrize("value,expected", [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("", False),
        ("  true  ", True),
        ("  false  ", False),
    ])
    def test_safe_bool_values(self, value, expected):
        assert otel_hook._safe_bool(value) is expected


class TestStringify:
    def test_dict(self):
        assert otel_hook._stringify({"a": 1}) == '{"a": 1}'

    def test_list(self):
        assert otel_hook._stringify([1, 2]) == "[1, 2]"

    def test_string(self):
        assert otel_hook._stringify("hello") == "hello"

    def test_int(self):
        assert otel_hook._stringify(42) == "42"

    def test_none(self):
        assert otel_hook._stringify(None) == "None"


class TestFirstPresent:
    def test_returns_first_match(self):
        data = {"a": None, "b": 2, "c": 3}
        assert otel_hook._first_present(data, ("a", "b", "c")) == 2

    def test_returns_none_when_no_match(self):
        assert otel_hook._first_present({}, ("x", "y")) is None

    def test_returns_first_non_none(self):
        data = {"x": None, "y": "val"}
        assert otel_hook._first_present(data, ("x", "y")) == "val"

    def test_returns_first_even_if_falsy(self):
        """0 is falsy but not None — it should still be returned."""
        data = {"a": 0, "b": 1}
        assert otel_hook._first_present(data, ("a", "b")) == 0


class TestIntOrNone:
    @pytest.mark.parametrize("value,expected", [
        (42, 42),
        ("42", 42),
        (None, None),
        ("abc", None),
        (3.9, 3),
    ])
    def test_int_or_none(self, value, expected):
        assert otel_hook._int_or_none(value) == expected


class TestFloatOrNone:
    @pytest.mark.parametrize("value,expected", [
        (1.5, 1.5),
        ("1.5", 1.5),
        (None, None),
        ("abc", None),
        (3, 3.0),
    ])
    def test_float_or_none(self, value, expected):
        assert otel_hook._float_or_none(value) == expected


class TestSetIfPresent:
    def test_sets_when_value_present(self):
        span = mock.MagicMock()
        otel_hook._set_if_present(span, "key", "value")
        span.set_attribute.assert_called_once_with("key", "value")

    def test_skips_when_none(self):
        span = mock.MagicMock()
        otel_hook._set_if_present(span, "key", None)
        span.set_attribute.assert_not_called()


class TestFlatten:
    def test_simple(self):
        out = {}
        otel_hook._flatten(out, "p", {"a": 1, "b": "x"})
        assert out == {"p.a": 1, "p.b": "x"}

    def test_nested(self):
        out = {}
        otel_hook._flatten(out, "p", {"a": {"b": 2}})
        assert out == {"p.a.b": 2}

    def test_list(self):
        out = {}
        otel_hook._flatten(out, "p", {"tags": [1, 2, 3]})
        assert out == {"p.tags": "[1, 2, 3]"}

    def test_none_skipped(self):
        out = {}
        otel_hook._flatten(out, "p", {"a": None, "b": 1})
        assert out == {"p.b": 1}


# ===================================================================
# Event name canonicalization
# ===================================================================

class TestEventNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("sessionStart", "SessionStart"),
        ("sessionEnd", "SessionEnd"),
        ("beforeSubmitPrompt", "UserPromptSubmit"),
        ("preToolUse", "PreToolUse"),
        ("postToolUse", "PostToolUse"),
        ("postToolUseFailure", "PostToolUseFailure"),
        ("stop", "Stop"),
        ("userPromptSubmitted", "UserPromptSubmit"),
        ("errorOccurred", "ErrorOccurred"),
        ("beforeShellExecution", "BeforeShellExecution"),
        ("afterShellExecution", "AfterShellExecution"),
        ("beforeMCPExecution", "BeforeMCPExecution"),
        ("afterMCPExecution", "AfterMCPExecution"),
        ("beforeReadFile", "BeforeReadFile"),
        ("afterFileEdit", "AfterFileEdit"),
        ("subagentStart", "SubagentStart"),
        ("subagentStop", "SubagentStop"),
    ])
    def test_canonical_mapping(self, raw, expected):
        assert otel_hook._normalize_event(raw) == expected

    def test_unknown_event_passes_through(self):
        assert otel_hook._normalize_event("customEvent") == "customEvent"


class TestGetEventName:
    def test_hook_event_name(self):
        assert otel_hook._get_event_name({"hook_event_name": "sessionStart"}) == "sessionStart"

    def test_event_key(self):
        assert otel_hook._get_event_name({"event": "stop"}) == "stop"

    def test_hook_key(self):
        assert otel_hook._get_event_name({"hook": "preToolUse"}) == "preToolUse"

    def test_prompt_fallback(self):
        assert otel_hook._get_event_name({"prompt": "hello"}) == "beforeSubmitPrompt"

    def test_empty_falls_to_stop(self):
        assert otel_hook._get_event_name({}) == "stop"

    def test_whitespace_event_name_ignored(self):
        assert otel_hook._get_event_name({"hook_event_name": "  ", "event": "stop"}) == "stop"


# ===================================================================
# IDE detection
# ===================================================================

class TestDetectIde:
    def test_cursor_with_conversation_id(self):
        assert otel_hook._detect_ide({"conversation_id": "abc"}) == "cursor"

    def test_cursor_with_generation_id(self):
        assert otel_hook._detect_ide({"generation_id": "gen-1"}) == "cursor"

    def test_cursor_with_composer_mode(self):
        assert otel_hook._detect_ide({"composer_mode": "agent"}) == "cursor"

    def test_cursor_with_agent_type(self):
        assert otel_hook._detect_ide({"agent_type": "main"}) == "cursor"

    def test_copilot_with_only_session_id(self):
        # No Cursor-specific fields → falls through to copilot
        assert otel_hook._detect_ide({"session_id": "sess-1"}) == "copilot"

    def test_default_to_cursor(self):
        assert otel_hook._detect_ide({}) == "cursor"

    def test_cursor_indicators_cwd(self):
        assert otel_hook._detect_ide({"cwd": "/tmp/project"}) == "cursor"


# ===================================================================
# Session key
# ===================================================================

class TestSessionKey:
    def test_session_id(self):
        assert otel_hook._session_key({"session_id": "s1"}) == "s1"

    def test_conversation_id(self):
        assert otel_hook._session_key({"conversation_id": "c1"}) == "c1"

    def test_prefers_session_id(self):
        assert otel_hook._session_key({"session_id": "s1", "conversation_id": "c1"}) == "s1"

    def test_none_when_missing(self):
        assert otel_hook._session_key({}) is None

    def test_none_when_empty(self):
        assert otel_hook._session_key({"session_id": "  "}) is None


# ===================================================================
# Privacy / masking
# ===================================================================

class TestHashText:
    def test_deterministic(self):
        assert otel_hook._hash_text("hello") == hashlib.sha256(b"hello").hexdigest()

    def test_different_inputs(self):
        assert otel_hook._hash_text("a") != otel_hook._hash_text("b")


class TestMaskText:
    def test_email(self):
        assert "[REDACTED_EMAIL]" in otel_hook._mask_text("user@example.com")

    def test_long_token(self):
        token = "A" * 30
        assert "[REDACTED_TOKEN]" in otel_hook._mask_text(f"Bearer {token}")

    def test_home_path(self):
        assert "/Users/[REDACTED]" in otel_hook._mask_text("/Users/johndoe/project")

    def test_preserves_short_text(self):
        text = "hello world"
        assert otel_hook._mask_text(text) == text


class TestMaybeAttachText:
    def test_empty_text_noop(self):
        span = mock.MagicMock()
        otel_hook._maybe_attach_text(span, "prompt", "")
        span.set_attribute.assert_not_called()

    @mock.patch.dict(os.environ, {"IDE_OTEL_CAPTURE_TEXT": ""}, clear=False)
    def test_sets_length_and_hash_only(self):
        span = mock.MagicMock()
        otel_hook._maybe_attach_text(span, "prompt", "hello")
        calls = {c[0][0] for c in span.set_attribute.call_args_list}
        assert "ide.prompt.length" in calls
        assert "ide.prompt.sha256" in calls
        assert "ide.prompt.text" not in calls

    @mock.patch.dict(os.environ, {"IDE_OTEL_CAPTURE_TEXT": "true", "IDE_OTEL_MASK_PROMPTS": ""}, clear=False)
    def test_captures_text_when_enabled(self):
        span = mock.MagicMock()
        otel_hook._maybe_attach_text(span, "prompt", "hello")
        calls = {c[0][0]: c[0][1] for c in span.set_attribute.call_args_list}
        assert calls["ide.prompt.text"] == "hello"

    @mock.patch.dict(os.environ, {
        "IDE_OTEL_CAPTURE_TEXT": "true",
        "IDE_OTEL_MASK_PROMPTS": "true",
    }, clear=False)
    def test_masks_text_when_enabled(self):
        span = mock.MagicMock()
        otel_hook._maybe_attach_text(span, "prompt", "email: user@test.com")
        calls = {c[0][0]: c[0][1] for c in span.set_attribute.call_args_list}
        assert "[REDACTED_EMAIL]" in calls["ide.prompt.text"]

    @mock.patch.dict(os.environ, {
        "IDE_OTEL_CAPTURE_TEXT": "true",
        "IDE_OTEL_TEXT_MAX_CHARS": "5",
    }, clear=False)
    def test_truncates_text(self):
        span = mock.MagicMock()
        otel_hook._maybe_attach_text(span, "prompt", "a" * 100)
        calls = {c[0][0]: c[0][1] for c in span.set_attribute.call_args_list}
        assert len(calls["ide.prompt.text"]) == 5


# ===================================================================
# Config helpers
# ===================================================================

class TestCoerceEnvValue:
    def test_string(self):
        assert otel_hook._coerce_env_value("K", "val") == "val"

    def test_bool(self):
        assert otel_hook._coerce_env_value("K", True) == "True"

    def test_int(self):
        assert otel_hook._coerce_env_value("K", 42) == "42"

    def test_none(self):
        assert otel_hook._coerce_env_value("K", None) == ""

    def test_dict_headers(self):
        result = otel_hook._coerce_env_value(
            "OTEL_EXPORTER_OTLP_HEADERS",
            {"Authorization": "Bearer tok"},
        )
        assert "Authorization=Bearer tok" in result

    def test_dict_non_headers(self):
        result = otel_hook._coerce_env_value("OTHER_KEY", {"a": 1})
        assert json.loads(result) == {"a": 1}

    def test_list(self):
        result = otel_hook._coerce_env_value("K", [1, 2])
        assert json.loads(result) == [1, 2]


class TestHeadersToEnv:
    def test_simple(self):
        assert otel_hook._headers_to_env({"a": "1", "b": "2"}) == "a=1,b=2"

    def test_skips_none(self):
        assert otel_hook._headers_to_env({"a": "1", "b": None}) == "a=1"


class TestParseOtlpHeaders:
    def test_simple(self):
        result = otel_hook._parse_otlp_headers("key1=val1,key2=val2")
        assert result == {"key1": "val1", "key2": "val2"}

    def test_url_encoded(self):
        result = otel_hook._parse_otlp_headers("authorization=Bearer%20token123")
        assert result == {"authorization": "Bearer token123"}

    def test_empty(self):
        assert otel_hook._parse_otlp_headers("") == {}
        assert otel_hook._parse_otlp_headers(None) == {}

    def test_malformed_ignored(self):
        result = otel_hook._parse_otlp_headers("good=val,badentry")
        assert result == {"good": "val"}


class TestParseResourceAttributes:
    def test_simple(self):
        result = otel_hook._parse_resource_attributes("service.name=test,env=prod")
        assert result == {"service.name": "test", "env": "prod"}

    def test_empty(self):
        assert otel_hook._parse_resource_attributes("") == {}
        assert otel_hook._parse_resource_attributes(None) == {}

    def test_malformed_ignored(self):
        result = otel_hook._parse_resource_attributes("good=val,badentry")
        assert result == {"good": "val"}


class TestApplyConfigEnv:
    def test_sets_env_vars(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            # Remove key if present
            os.environ.pop("TEST_CONFIG_KEY", None)
            otel_hook._apply_config_env({"TEST_CONFIG_KEY": "hello"})
            assert os.environ["TEST_CONFIG_KEY"] == "hello"
            del os.environ["TEST_CONFIG_KEY"]

    def test_does_not_overwrite_existing(self):
        with mock.patch.dict(os.environ, {"EXISTING_KEY": "original"}, clear=False):
            otel_hook._apply_config_env({"EXISTING_KEY": "new_value"})
            assert os.environ["EXISTING_KEY"] == "original"

    def test_skips_none_values(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            otel_hook._apply_config_env({"NULL_KEY": None})
            assert "NULL_KEY" not in os.environ


class TestLoadConfig:
    def test_loads_json_config(self, tmp_path):
        cfg = {"OTEL_SERVICE_NAME": "test-svc"}
        cfg_path = tmp_path / "otel_config.json"
        cfg_path.write_text(json.dumps(cfg))
        with mock.patch.dict(os.environ, {"IDE_OTEL_CONFIG": str(cfg_path)}, clear=False):
            result = otel_hook._load_config()
        assert result == cfg

    def test_returns_empty_on_missing_file(self, tmp_path):
        with mock.patch.dict(os.environ, {"IDE_OTEL_CONFIG": str(tmp_path / "missing.json")}, clear=False):
            with mock.patch.object(otel_hook, "_HOOK_DIR", str(tmp_path)):
                result = otel_hook._load_config()
        assert result == {}

    def test_returns_empty_on_bad_json(self, tmp_path):
        cfg_path = tmp_path / "otel_config.json"
        cfg_path.write_text("not json!!!")
        with mock.patch.dict(os.environ, {"IDE_OTEL_CONFIG": str(cfg_path)}, clear=False):
            result = otel_hook._load_config()
        assert result == {}


# ===================================================================
# Logs endpoint derivation
# ===================================================================

class TestDeriveLogsEndpoint:
    def test_explicit_override(self):
        with mock.patch.dict(os.environ, {
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://custom:4317/v1/logs",
        }, clear=False):
            assert otel_hook._derive_logs_endpoint() == "http://custom:4317/v1/logs"

    def test_replaces_traces_with_logs(self):
        with mock.patch.dict(os.environ, {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://ingress.us1.coralogix.com:443/v1/traces",
        }, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", None)
            assert otel_hook._derive_logs_endpoint() == "https://ingress.us1.coralogix.com:443/v1/logs"

    def test_grpc_passthrough(self):
        with mock.patch.dict(os.environ, {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
        }, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", None)
            assert otel_hook._derive_logs_endpoint() == "http://localhost:4317"

    def test_empty_returns_none(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", None)
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            assert otel_hook._derive_logs_endpoint() is None


# ===================================================================
# GenAI semantic conventions
# ===================================================================

class TestGenaiOperation:
    def test_tool_events(self):
        for evt in ("PreToolUse", "PostToolUse", "PostToolUseFailure",
                     "BeforeShellExecution", "AfterShellExecution",
                     "BeforeMCPExecution", "AfterMCPExecution",
                     "BeforeReadFile", "AfterFileEdit"):
            assert otel_hook._genai_operation(evt) == "execute_tool"

    def test_agent_events(self):
        for evt in ("SessionStart", "SessionEnd", "SubagentStart", "SubagentStop"):
            assert otel_hook._genai_operation(evt) == "invoke_agent"

    def test_other_events(self):
        assert otel_hook._genai_operation("UserPromptSubmit") == "chat"
        assert otel_hook._genai_operation("Stop") == "chat"


class TestGenaiMessages:
    def test_prompt_only(self):
        inp, out = otel_hook._genai_messages("hello", None)
        parsed = json.loads(inp)
        assert parsed[0]["role"] == "user"
        assert parsed[0]["parts"][0]["content"] == "hello"
        assert out is None

    def test_response_only(self):
        inp, out = otel_hook._genai_messages(None, "world")
        assert inp is None
        parsed = json.loads(out)
        assert parsed[0]["role"] == "assistant"

    def test_both(self):
        inp, out = otel_hook._genai_messages("q", "a")
        assert inp is not None
        assert out is not None

    def test_none(self):
        inp, out = otel_hook._genai_messages(None, None)
        assert inp is None
        assert out is None


class TestApplyGenaiSemconv:
    def test_basic_attributes(self):
        span = mock.MagicMock()
        data = {
            "session_id": "sess-1",
            "model": "gpt-4",
            "input_tokens": 100,
            "output_tokens": 50,
        }
        otel_hook._apply_genai_semconv(span, "UserPromptSubmit", data, "cursor")
        attrs = {c[0][0]: c[0][1] for c in span.set_attribute.call_args_list}
        assert attrs["gen_ai.system"] == "cursor"
        assert attrs["gen_ai.operation.name"] == "chat"
        assert attrs["gen_ai.request.model"] == "gpt-4"
        assert attrs["gen_ai.usage.input_tokens"] == 100
        assert attrs["gen_ai.usage.output_tokens"] == 50

    def test_nested_usage(self):
        span = mock.MagicMock()
        data = {
            "session_id": "sess-1",
            "usage": {"input_tokens": 200, "output_tokens": 80, "total_tokens": 280},
        }
        otel_hook._apply_genai_semconv(span, "Stop", data, "copilot")
        attrs = {c[0][0]: c[0][1] for c in span.set_attribute.call_args_list}
        assert attrs["gen_ai.usage.input_tokens"] == 200
        assert attrs["gen_ai.usage.output_tokens"] == 80
        assert attrs["ide.usage.total_tokens"] == 280

    def test_conversation_id_from_conversation_id(self):
        span = mock.MagicMock()
        data = {"conversation_id": "conv-abc"}
        otel_hook._apply_genai_semconv(span, "SessionStart", data, "cursor")
        attrs = {c[0][0]: c[0][1] for c in span.set_attribute.call_args_list}
        assert attrs["gen_ai.conversation.id"] == "conv-abc"


# ===================================================================
# Session / batch state management
# ===================================================================

class TestSessionContext:
    def test_create_and_load(self, tmp_path):
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(tmp_path / "sessions")), \
             mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
            ctx = otel_hook._create_session_context("sess-1", {}, "cursor")
            assert "trace_id" in ctx
            assert len(ctx["trace_id"]) == 32
            assert ctx["generation_count"] == 0

            loaded = otel_hook._load_session_context("sess-1")
            assert loaded["trace_id"] == ctx["trace_id"]

    def test_load_missing_returns_none(self, tmp_path):
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(tmp_path / "sessions")):
            assert otel_hook._load_session_context("nonexistent") is None

    def test_load_none_returns_none(self):
        assert otel_hook._load_session_context(None) is None

    def test_clear(self, tmp_path):
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(tmp_path / "sessions")), \
             mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
            otel_hook._create_session_context("sess-2", {}, "cursor")
            otel_hook._clear_session_context("sess-2")
            assert otel_hook._load_session_context("sess-2") is None

    def test_advance_generation(self, tmp_path):
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(tmp_path / "sessions")), \
             mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
            ctx = otel_hook._create_session_context("sess-3", {}, "cursor")
            gen1 = otel_hook._advance_generation("sess-3", ctx)
            assert gen1 == "sess-3_gen_1"
            assert ctx["generation_count"] == 1

            gen2 = otel_hook._advance_generation("sess-3", ctx)
            assert gen2 == "sess-3_gen_2"
            assert ctx["generation_count"] == 2


class TestBatchBuffer:
    def test_append_and_load(self, tmp_path):
        with mock.patch.object(otel_hook, "_BATCH_DIR", str(tmp_path / "batches")), \
             mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
            otel_hook._append_batch_event("gen-1", "PreToolUse", {"tool_name": "editor"})
            otel_hook._append_batch_event("gen-1", "PostToolUse", {"tool_name": "editor", "duration_ms": 100})

            events = otel_hook._load_batch_events("gen-1")
            assert len(events) == 2
            assert events[0]["event"] == "PreToolUse"
            assert events[1]["event"] == "PostToolUse"

    def test_load_empty(self, tmp_path):
        with mock.patch.object(otel_hook, "_BATCH_DIR", str(tmp_path / "batches")):
            assert otel_hook._load_batch_events("nonexistent") == []

    def test_clear(self, tmp_path):
        with mock.patch.object(otel_hook, "_BATCH_DIR", str(tmp_path / "batches")), \
             mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
            otel_hook._append_batch_event("gen-2", "Stop", {})
            otel_hook._clear_batch_events("gen-2")
            assert otel_hook._load_batch_events("gen-2") == []


class TestAtomicWriteJson:
    def test_writes_valid_json(self, tmp_path):
        path = str(tmp_path / "test.json")
        payload = {"key": "value", "num": 42}
        otel_hook._atomic_write_json(path, payload)
        with open(path) as f:
            assert json.load(f) == payload

    def test_overwrites_existing(self, tmp_path):
        path = str(tmp_path / "test.json")
        otel_hook._atomic_write_json(path, {"v": 1})
        otel_hook._atomic_write_json(path, {"v": 2})
        with open(path) as f:
            assert json.load(f)["v"] == 2


class TestResolveGenerationKey:
    def test_from_data(self):
        assert otel_hook._resolve_generation_key(
            {"generation_id": "gen-abc"}, None
        ) == "gen-abc"

    def test_from_session_ctx(self):
        assert otel_hook._resolve_generation_key(
            {}, {"current_generation": "sess_gen_1"}
        ) == "sess_gen_1"

    def test_none(self):
        assert otel_hook._resolve_generation_key({}, None) is None
        assert otel_hook._resolve_generation_key({}, {}) is None


# ===================================================================
# State cleanup
# ===================================================================

class TestCleanupState:
    def test_removes_old_files(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        old_file = sessions_dir / "old.json"
        old_file.write_text("{}")
        # Set mtime to 2 days ago
        old_time = time.time() - 200000
        os.utime(str(old_file), (old_time, old_time))

        new_file = sessions_dir / "new.json"
        new_file.write_text("{}")

        with mock.patch.object(otel_hook, "_STATE_DIR", str(tmp_path)), \
             mock.patch.object(otel_hook, "_SESSION_DIR", str(sessions_dir)), \
             mock.patch.object(otel_hook, "_BATCH_DIR", str(tmp_path / "batches")), \
             mock.patch.object(otel_hook, "_CLEANUP_MARKER", str(tmp_path / "last_cleanup")), \
             mock.patch.dict(os.environ, {
                 "IDE_OTEL_STATE_TTL_SECONDS": "86400",
                 "IDE_OTEL_STATE_CLEANUP_INTERVAL_SECONDS": "0",
             }, clear=False):
            otel_hook._cleanup_state()
            assert not old_file.exists()
            assert new_file.exists()


# ===================================================================
# OTel log emission
# ===================================================================

class TestEmitEventLog:
    """Verify the dispatcher routes events to the correct log emitter."""

    @mock.patch.object(otel_hook, "_LOGS_INITIALIZED", False)
    def test_noop_when_logs_disabled(self):
        # Should not raise
        otel_hook._emit_event_log("BeforeMCPExecution", {})

    @mock.patch.object(otel_hook, "_LOGS_INITIALIZED", True)
    @mock.patch.object(otel_hook, "_emit_mcp_log")
    def test_routes_mcp_events(self, mock_mcp):
        otel_hook._emit_event_log("BeforeMCPExecution", {"command": "srv"})
        mock_mcp.assert_called_once()

    @mock.patch.object(otel_hook, "_LOGS_INITIALIZED", True)
    @mock.patch.object(otel_hook, "_emit_shell_log")
    def test_routes_shell_events(self, mock_shell):
        otel_hook._emit_event_log("AfterShellExecution", {"command": "ls"})
        mock_shell.assert_called_once()

    @mock.patch.object(otel_hook, "_LOGS_INITIALIZED", True)
    @mock.patch.object(otel_hook, "_emit_tool_log")
    def test_routes_tool_events(self, mock_tool):
        otel_hook._emit_event_log("PreToolUse", {"tool_name": "editor"})
        mock_tool.assert_called_once()


class TestFmtDuration:
    def test_none(self):
        assert otel_hook._fmt_duration(None) == "n/a"

    def test_value(self):
        assert otel_hook._fmt_duration(123) == "123ms"


# ===================================================================
# Logging format
# ===================================================================

class TestJsonFormatter:
    def test_format_output(self):
        import logging
        formatter = otel_hook._JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello %s", args=("world",), exc_info=None,
        )
        record.trace_id = "abc123"
        record.span_id = "def456"
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["message"] == "hello world"
        assert parsed["level"] == "INFO"
        assert parsed["trace_id"] == "abc123"
        assert parsed["span_id"] == "def456"

    def test_extra_attributes(self):
        import logging
        formatter = otel_hook._JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        record.trace_id = "0"
        record.span_id = "0"
        record.custom_attr = "custom_value"
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "attributes" in parsed
        assert parsed["attributes"]["custom_attr"] == "custom_value"


# ===================================================================
# Lock mechanism
# ===================================================================

class TestAcquireLock:
    def test_basic_lock_unlock(self, tmp_path):
        lock_path = str(tmp_path / "test.lock")
        with otel_hook._acquire_lock(lock_path):
            # Lock file should exist during context
            assert os.path.exists(lock_path)
        # Lock file should be removed after context
        assert not os.path.exists(lock_path)


# ===================================================================
# Populate span
# ===================================================================

class TestPopulateSpan:
    @mock.patch.object(otel_hook, "_LOGS_INITIALIZED", False)
    def test_basic_attributes(self):
        span = mock.MagicMock()
        data = {
            "session_id": "s1",
            "generation_id": "g1",
            "cwd": "/tmp/project",
        }
        otel_hook._populate_span(span, "UserPromptSubmit", data, "cursor")
        attrs = {c[0][0]: c[0][1] for c in span.set_attribute.call_args_list}
        assert attrs["ide.hook.event"] == "UserPromptSubmit"
        assert attrs["ide.name"] == "cursor"
        assert attrs["ide.session_id"] == "s1"
        assert attrs["ide.generation_id"] == "g1"
        assert attrs["gen_ai.system"] == "cursor"

    @mock.patch.object(otel_hook, "_LOGS_INITIALIZED", False)
    def test_tool_event_attributes(self):
        span = mock.MagicMock()
        data = {
            "tool_name": "file_editor",
            "tool_id": "t-123",
            "duration_ms": 42,
        }
        otel_hook._populate_span(span, "PostToolUse", data, "cursor")
        attrs = {c[0][0]: c[0][1] for c in span.set_attribute.call_args_list}
        assert attrs["ide.tool_name"] == "file_editor"
        assert attrs["ide.tool_id"] == "t-123"
        assert attrs["ide.duration_ms"] == 42

    @mock.patch.object(otel_hook, "_LOGS_INITIALIZED", False)
    def test_metadata_flattening(self):
        span = mock.MagicMock()
        data = {"metadata": {"nested": {"key": "val"}}}
        otel_hook._populate_span(span, "Stop", data, "cursor")
        attrs = {c[0][0]: c[0][1] for c in span.set_attribute.call_args_list}
        assert attrs["ide.metadata.nested.key"] == "val"


# ===================================================================
# End-to-end main() tests (stdin→stdout)
# ===================================================================

class TestMainEndToEnd:
    """Test main() by mocking stdin and verifying stdout JSON output."""

    def _run_main(self, input_data: dict, env_overrides: dict = None):
        """Run main() with given input data, returns (return_code, stdout_json)."""
        env = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
            "IDE_OTEL_BATCH_ON_STOP": "false",
            "IDE_OTEL_ENABLE_LOGS": "false",
        }
        if env_overrides:
            env.update(env_overrides)

        input_json = json.dumps(input_data)

        with mock.patch("sys.stdin", __class__=type(sys.stdin)):
            with mock.patch("sys.stdin.read", return_value=input_json):
                with mock.patch("builtins.print") as mock_print:
                    with mock.patch.dict(os.environ, env, clear=False):
                        # Reset tracing state
                        otel_hook._TRACING_INITIALIZED = False
                        otel_hook._LOGS_INITIALIZED = False
                        # Mock config to avoid file reads
                        with mock.patch.object(otel_hook, "_load_config", return_value={}):
                            with mock.patch.object(otel_hook, "_configure_logging"):
                                with mock.patch.object(otel_hook, "_cleanup_state"):
                                    rc = otel_hook.main()

        # Extract printed JSON
        if mock_print.call_args_list:
            last_call = mock_print.call_args_list[-1]
            output = json.loads(last_call[0][0])
        else:
            output = None
        return rc, output

    def test_session_start_returns_continue(self):
        rc, output = self._run_main({
            "hook_event_name": "sessionStart",
            "session_id": "test-session",
        })
        assert rc == 0
        assert output == {"continue": True}

    def test_stop_event_returns_continue(self):
        rc, output = self._run_main({
            "hook_event_name": "stop",
            "session_id": "test-session",
        })
        assert rc == 0
        assert output == {"continue": True}

    def test_copilot_event_returns_continue(self):
        rc, output = self._run_main({
            "hook_event_name": "userPromptSubmitted",
            "session_id": "copilot-session",
        })
        assert rc == 0
        assert output == {"continue": True}

    def test_empty_input_returns_continue(self):
        """Even with empty input, hook should always return continue."""
        rc, output = self._run_main({})
        assert rc == 0
        assert output == {"continue": True}

    def test_tool_event_returns_continue(self):
        rc, output = self._run_main({
            "hook_event_name": "preToolUse",
            "session_id": "sess-1",
            "tool_name": "code_editor",
            "tool_id": "t-1",
        })
        assert rc == 0
        assert output == {"continue": True}


class TestMainBatchMode:
    """Test main() in batch mode — events are buffered and flushed on Stop."""

    def _run_main_batch(self, input_data: dict, tmp_path, env_overrides: dict = None):
        env = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
            "IDE_OTEL_BATCH_ON_STOP": "true",
            "IDE_OTEL_ENABLE_LOGS": "false",
        }
        if env_overrides:
            env.update(env_overrides)
        input_json = json.dumps(input_data)

        with mock.patch("sys.stdin.read", return_value=input_json), \
             mock.patch("builtins.print") as mock_print, \
             mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(otel_hook, "_load_config", return_value={}), \
             mock.patch.object(otel_hook, "_configure_logging"), \
             mock.patch.object(otel_hook, "_cleanup_state"), \
             mock.patch.object(otel_hook, "_SESSION_DIR", str(tmp_path / "sessions")), \
             mock.patch.object(otel_hook, "_BATCH_DIR", str(tmp_path / "batches")), \
             mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
            otel_hook._TRACING_INITIALIZED = False
            otel_hook._LOGS_INITIALIZED = False
            rc = otel_hook.main()

        if mock_print.call_args_list:
            output = json.loads(mock_print.call_args_list[-1][0][0])
        else:
            output = None
        return rc, output

    def test_session_start_creates_context(self, tmp_path):
        rc, output = self._run_main_batch({
            "hook_event_name": "sessionStart",
            "session_id": "batch-sess",
        }, tmp_path)
        assert rc == 0
        assert output == {"continue": True}
        # Session context should have been created
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(tmp_path / "sessions")):
            ctx = otel_hook._load_session_context("batch-sess")
            assert ctx is not None
            assert "trace_id" in ctx

    def test_prompt_buffers_event(self, tmp_path):
        """After SessionStart, a UserPromptSubmit should buffer an event."""
        # First create the session
        self._run_main_batch({
            "hook_event_name": "sessionStart",
            "session_id": "batch-sess-2",
        }, tmp_path)
        # Then submit a prompt
        self._run_main_batch({
            "hook_event_name": "beforeSubmitPrompt",
            "session_id": "batch-sess-2",
            "prompt": "write tests",
        }, tmp_path)
        # Check batch file was written
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(tmp_path / "sessions")), \
             mock.patch.object(otel_hook, "_BATCH_DIR", str(tmp_path / "batches")):
            ctx = otel_hook._load_session_context("batch-sess-2")
            gen_key = ctx.get("current_generation")
            assert gen_key is not None
            events = otel_hook._load_batch_events(gen_key)
            assert len(events) == 1
            assert events[0]["event"] == "UserPromptSubmit"


# ===================================================================
# State TTL / config helpers
# ===================================================================

class TestStateTtlHelpers:
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IDE_OTEL_STATE_TTL_SECONDS", None)
            os.environ.pop("IDE_OTEL_STATE_CLEANUP_INTERVAL_SECONDS", None)
            os.environ.pop("IDE_OTEL_STATE_LOCK_TIMEOUT_SECONDS", None)
            assert otel_hook._state_ttl_seconds() == 86400
            assert otel_hook._state_cleanup_interval_seconds() == 3600
            assert otel_hook._state_lock_timeout_seconds() == 2.0

    def test_custom_values(self):
        with mock.patch.dict(os.environ, {
            "IDE_OTEL_STATE_TTL_SECONDS": "3600",
            "IDE_OTEL_STATE_CLEANUP_INTERVAL_SECONDS": "600",
            "IDE_OTEL_STATE_LOCK_TIMEOUT_SECONDS": "5",
        }, clear=False):
            assert otel_hook._state_ttl_seconds() == 3600
            assert otel_hook._state_cleanup_interval_seconds() == 600
            assert otel_hook._state_lock_timeout_seconds() == 5.0

    def test_invalid_values(self):
        with mock.patch.dict(os.environ, {
            "IDE_OTEL_STATE_TTL_SECONDS": "abc",
            "IDE_OTEL_STATE_CLEANUP_INTERVAL_SECONDS": "",
            "IDE_OTEL_STATE_LOCK_TIMEOUT_SECONDS": "bad",
        }, clear=False):
            assert otel_hook._state_ttl_seconds() == 86400
            assert otel_hook._state_cleanup_interval_seconds() == 3600
            assert otel_hook._state_lock_timeout_seconds() == 2.0


# ===================================================================
# Batch enabled
# ===================================================================

class TestBatchEnabled:
    def test_true(self):
        with mock.patch.dict(os.environ, {"IDE_OTEL_BATCH_ON_STOP": "true"}, clear=False):
            assert otel_hook._batch_enabled() is True

    def test_false(self):
        with mock.patch.dict(os.environ, {"IDE_OTEL_BATCH_ON_STOP": "false"}, clear=False):
            assert otel_hook._batch_enabled() is False

    def test_empty(self):
        with mock.patch.dict(os.environ, {"IDE_OTEL_BATCH_ON_STOP": ""}, clear=False):
            assert otel_hook._batch_enabled() is False


# ===================================================================
# Log level resolution
# ===================================================================

class TestResolveLogLevel:
    def test_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("IDE_OTEL_LOG_LEVEL", "LOG_LEVEL", "LOGLEVEL"):
                os.environ.pop(key, None)
            import logging
            assert otel_hook._resolve_log_level() == logging.WARNING

    def test_debug(self):
        import logging
        with mock.patch.dict(os.environ, {"IDE_OTEL_LOG_LEVEL": "DEBUG"}, clear=False):
            assert otel_hook._resolve_log_level() == logging.DEBUG

    def test_info(self):
        import logging
        with mock.patch.dict(os.environ, {"IDE_OTEL_LOG_LEVEL": "info"}, clear=False):
            assert otel_hook._resolve_log_level() == logging.INFO


# ===================================================================
# Load input
# ===================================================================

class TestLoadInput:
    def test_valid_json(self):
        with mock.patch("sys.stdin.read", return_value='{"key": "value"}'):
            assert otel_hook._load_input() == {"key": "value"}

    def test_empty_input(self):
        with mock.patch("sys.stdin.read", return_value=""):
            assert otel_hook._load_input() == {}

    def test_whitespace_input(self):
        with mock.patch("sys.stdin.read", return_value="   "):
            assert otel_hook._load_input() == {}

    def test_invalid_json(self):
        with mock.patch("sys.stdin.read", return_value="not json"):
            assert otel_hook._load_input() == {}


# ===================================================================
# Generation key from data
# ===================================================================

class TestGenerationKeyFromData:
    def test_present(self):
        assert otel_hook._generation_key_from_data({"generation_id": "gen-1"}) == "gen-1"

    def test_missing(self):
        assert otel_hook._generation_key_from_data({}) is None

    def test_empty_string(self):
        assert otel_hook._generation_key_from_data({"generation_id": "  "}) is None


# ===================================================================
# Session path sanitization
# ===================================================================

class TestSessionPath:
    def test_sanitizes_special_chars(self):
        path = otel_hook._session_path("session/with:special chars!")
        basename = os.path.basename(path)
        # Should not contain special characters except underscore, dot, hyphen
        assert re.match(r"^[A-Za-z0-9_.-]+\.json$", basename)

    def test_normal_session_id(self):
        path = otel_hook._session_path("abc-123")
        assert path.endswith("abc-123.json")
