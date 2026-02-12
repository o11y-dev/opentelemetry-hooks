"""Meaningful tests for otel_hook.py — focused on core logic, not too many."""

import hashlib
import json
import os
import sys
import tempfile
import time
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import otel_hook


# ── Helper functions ──────────────────────────────────────────────────────


class TestSafeBool:
    @pytest.mark.parametrize("value,expected", [
        ("true", True), ("True", True), ("TRUE", True),
        ("1", True), ("yes", True), ("on", True),
        ("false", False), ("0", False), ("no", False),
        ("off", False), ("", False), ("random", False),
    ])
    def test_safe_bool(self, value, expected):
        assert otel_hook._safe_bool(value) is expected


class TestStringify:
    def test_dict(self):
        assert otel_hook._stringify({"a": 1}) == '{"a": 1}'

    def test_list(self):
        assert otel_hook._stringify([1, 2]) == "[1, 2]"

    def test_scalar(self):
        assert otel_hook._stringify(42) == "42"
        assert otel_hook._stringify("hello") == "hello"


class TestFirstPresent:
    def test_returns_first_match(self):
        data = {"b": 2, "c": 3}
        assert otel_hook._first_present(data, ("a", "b", "c")) == 2

    def test_returns_none_when_missing(self):
        assert otel_hook._first_present({}, ("x", "y")) is None

    def test_skips_none_values(self):
        data = {"a": None, "b": "val"}
        assert otel_hook._first_present(data, ("a", "b")) == "val"


class TestIntOrNone:
    def test_valid(self):
        assert otel_hook._int_or_none(42) == 42
        assert otel_hook._int_or_none("7") == 7

    def test_none(self):
        assert otel_hook._int_or_none(None) is None

    def test_invalid(self):
        assert otel_hook._int_or_none("abc") is None


class TestFloatOrNone:
    def test_valid(self):
        assert otel_hook._float_or_none(3.14) == 3.14
        assert otel_hook._float_or_none("2.5") == 2.5

    def test_none(self):
        assert otel_hook._float_or_none(None) is None

    def test_invalid(self):
        assert otel_hook._float_or_none("xyz") is None


# ── Event normalization ───────────────────────────────────────────────────


class TestEventNormalization:
    @pytest.mark.parametrize("raw,canonical", [
        ("sessionStart", "SessionStart"),
        ("sessionEnd", "SessionEnd"),
        ("beforeSubmitPrompt", "UserPromptSubmit"),
        ("preToolUse", "PreToolUse"),
        ("postToolUse", "PostToolUse"),
        ("stop", "Stop"),
        ("userPromptSubmitted", "UserPromptSubmit"),
        ("errorOccurred", "ErrorOccurred"),
    ])
    def test_known_events(self, raw, canonical):
        assert otel_hook._normalize_event(raw) == canonical

    def test_unknown_event_passthrough(self):
        assert otel_hook._normalize_event("customEvent") == "customEvent"


class TestGetEventName:
    def test_hook_event_name(self):
        assert otel_hook._get_event_name({"hook_event_name": "sessionStart"}) == "sessionStart"

    def test_event_field(self):
        assert otel_hook._get_event_name({"event": "preToolUse"}) == "preToolUse"

    def test_prompt_fallback(self):
        assert otel_hook._get_event_name({"prompt": "hello"}) == "beforeSubmitPrompt"

    def test_empty_fallback(self):
        assert otel_hook._get_event_name({}) == "stop"


# ── IDE detection ─────────────────────────────────────────────────────────


class TestDetectIDE:
    def test_cursor_via_conversation_id(self):
        assert otel_hook._detect_ide({"conversation_id": "abc"}) == "cursor"

    def test_cursor_via_generation_id(self):
        assert otel_hook._detect_ide({"generation_id": "gen-1"}) == "cursor"

    def test_cursor_via_indicators(self):
        assert otel_hook._detect_ide({"composer_mode": "agent"}) == "cursor"

    def test_copilot_via_session_id_only(self):
        # No cursor-specific fields → copilot
        assert otel_hook._detect_ide({"session_id": "sess-1"}) == "copilot"

    def test_empty_defaults_cursor(self):
        # Default when no IDE signals present
        with mock.patch("os.getcwd", return_value="/tmp/test"):
            with mock.patch("os.path.exists", return_value=False):
                assert otel_hook._detect_ide({}) == "cursor"


# ── Privacy functions ─────────────────────────────────────────────────────


class TestPrivacy:
    def test_hash_text_deterministic(self):
        expected = hashlib.sha256(b"hello").hexdigest()
        assert otel_hook._hash_text("hello") == expected

    def test_mask_email(self):
        assert "[REDACTED_EMAIL]" in otel_hook._mask_text("contact user@example.com please")

    def test_mask_home_path(self):
        assert "/Users/[REDACTED]" in otel_hook._mask_text("/Users/johndoe/project")

    def test_mask_token(self):
        long_token = "A" * 30
        result = otel_hook._mask_text(f"token: {long_token}")
        assert "[REDACTED_TOKEN]" in result


# ── Config parsing ────────────────────────────────────────────────────────


class TestConfigParsing:
    def test_parse_resource_attributes(self):
        result = otel_hook._parse_resource_attributes("service.name=test,env=prod")
        assert result == {"service.name": "test", "env": "prod"}

    def test_parse_resource_attributes_empty(self):
        assert otel_hook._parse_resource_attributes("") == {}
        assert otel_hook._parse_resource_attributes(None) == {}

    def test_parse_otlp_headers(self):
        result = otel_hook._parse_otlp_headers("authorization=Bearer%20token123")
        assert result == {"authorization": "Bearer token123"}

    def test_parse_otlp_headers_empty(self):
        assert otel_hook._parse_otlp_headers("") == {}

    def test_coerce_env_value_dict_headers(self):
        result = otel_hook._coerce_env_value(
            "OTEL_EXPORTER_OTLP_HEADERS", {"auth": "Bearer tok"}
        )
        assert result == "auth=Bearer tok"

    def test_coerce_env_value_bool(self):
        assert otel_hook._coerce_env_value("key", True) == "True"

    def test_coerce_env_value_none(self):
        assert otel_hook._coerce_env_value("key", None) == ""


# ── Session & generation key extraction ───────────────────────────────────


class TestSessionKey:
    def test_session_id(self):
        assert otel_hook._session_key({"session_id": "s1"}) == "s1"

    def test_conversation_id(self):
        assert otel_hook._session_key({"conversation_id": "c1"}) == "c1"

    def test_prefers_session_id(self):
        assert otel_hook._session_key({"session_id": "s1", "conversation_id": "c1"}) == "s1"

    def test_none_when_missing(self):
        assert otel_hook._session_key({}) is None


class TestGenerationKey:
    def test_from_data(self):
        assert otel_hook._generation_key_from_data({"generation_id": "g1"}) == "g1"

    def test_none_when_missing(self):
        assert otel_hook._generation_key_from_data({}) is None


# ── GenAI operation mapping ───────────────────────────────────────────────


class TestGenAIOperation:
    def test_tool_events(self):
        for evt in ("PreToolUse", "PostToolUse", "BeforeShellExecution", "AfterMCPExecution"):
            assert otel_hook._genai_operation(evt) == "execute_tool"

    def test_agent_events(self):
        for evt in ("SessionStart", "SessionEnd", "SubagentStart"):
            assert otel_hook._genai_operation(evt) == "invoke_agent"

    def test_chat_default(self):
        assert otel_hook._genai_operation("UserPromptSubmit") == "chat"
        assert otel_hook._genai_operation("Stop") == "chat"


# ── GenAI messages ────────────────────────────────────────────────────────


class TestGenAIMessages:
    def test_both_present(self):
        inp, out = otel_hook._genai_messages("hi", "hello")
        assert json.loads(inp)[0]["role"] == "user"
        assert json.loads(out)[0]["role"] == "assistant"

    def test_none_inputs(self):
        inp, out = otel_hook._genai_messages(None, None)
        assert inp is None and out is None


# ── Log endpoint derivation ───────────────────────────────────────────────


class TestDeriveLogsEndpoint:
    def test_explicit_override(self):
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://logs:4317"}):
            assert otel_hook._derive_logs_endpoint() == "http://logs:4317"

    def test_replace_traces_with_logs(self):
        env = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://host.com:443/v1/traces",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            # Clear the explicit override if set
            os.environ.pop("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", None)
            assert otel_hook._derive_logs_endpoint() == "https://host.com:443/v1/logs"

    def test_grpc_passthrough(self):
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", None)
            assert otel_hook._derive_logs_endpoint() == "http://localhost:4317"


# ── Batch buffer I/O ─────────────────────────────────────────────────────


class TestBatchBuffer:
    def test_append_and_load(self, tmp_path):
        with mock.patch.object(otel_hook, "_BATCH_DIR", str(tmp_path)):
            with mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
                otel_hook._append_batch_event("test-gen", "PreToolUse", {"tool_name": "grep"})
                otel_hook._append_batch_event("test-gen", "PostToolUse", {"tool_name": "grep"})
                events = otel_hook._load_batch_events("test-gen")
                assert len(events) == 2
                assert events[0]["event"] == "PreToolUse"
                assert events[1]["event"] == "PostToolUse"

    def test_clear(self, tmp_path):
        with mock.patch.object(otel_hook, "_BATCH_DIR", str(tmp_path)):
            with mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
                otel_hook._append_batch_event("test-gen", "PreToolUse", {})
                otel_hook._clear_batch_events("test-gen")
                assert otel_hook._load_batch_events("test-gen") == []

    def test_load_empty(self, tmp_path):
        with mock.patch.object(otel_hook, "_BATCH_DIR", str(tmp_path)):
            assert otel_hook._load_batch_events("nonexistent") == []


# ── Session context persistence ───────────────────────────────────────────


class TestSessionContext:
    def test_create_and_load(self, tmp_path):
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(tmp_path)):
            with mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
                ctx = otel_hook._create_session_context("sess-1", {}, "cursor")
                assert "trace_id" in ctx
                assert len(ctx["trace_id"]) == 32
                loaded = otel_hook._load_session_context("sess-1")
                assert loaded["trace_id"] == ctx["trace_id"]

    def test_clear(self, tmp_path):
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(tmp_path)):
            with mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
                otel_hook._create_session_context("sess-2", {}, "cursor")
                otel_hook._clear_session_context("sess-2")
                assert otel_hook._load_session_context("sess-2") is None

    def test_load_missing_returns_none(self):
        assert otel_hook._load_session_context(None) is None
        assert otel_hook._load_session_context("nonexistent-session") is None


# ── Generation advance ────────────────────────────────────────────────────


class TestAdvanceGeneration:
    def test_increments_counter(self, tmp_path):
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(tmp_path)):
            with mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
                ctx = otel_hook._create_session_context("sess-adv", {}, "copilot")
                gen1 = otel_hook._advance_generation("sess-adv", ctx)
                assert gen1 == "sess-adv_gen_1"
                ctx = otel_hook._load_session_context("sess-adv")
                gen2 = otel_hook._advance_generation("sess-adv", ctx)
                assert gen2 == "sess-adv_gen_2"


# ── Flush stale sessions ─────────────────────────────────────────────────


class TestFlushStaleSessions:
    def test_flushes_stale_session_and_removes_file(self, tmp_path):
        """Stale session files should emit ide.session root span and be removed."""
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        ctx = {
            "trace_id": "a" * 32,
            "phantom_parent_id": "b" * 16,
            "start_time_ns": 1000000000,
            "ide": "cursor",
            "generation_count": 2,
        }
        sess_file = session_dir / "stale-sess.json"
        sess_file.write_text(json.dumps(ctx))
        # Make file appear old (mtime in the past)
        old_mtime = time.time() - 100_000
        os.utime(str(sess_file), (old_mtime, old_mtime))

        tracer = mock.MagicMock()
        mock_span = mock.MagicMock()
        tracer.start_span.return_value = mock_span
        mock_span.__enter__ = mock.MagicMock(return_value=mock_span)
        mock_span.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch.object(otel_hook, "_SESSION_DIR", str(session_dir)):
            with mock.patch.object(otel_hook, "_force_flush_provider"):
                otel_hook._flush_stale_sessions(tracer)

        # Session span should have been emitted
        tracer.start_span.assert_called_once()
        assert tracer.start_span.call_args[0][0] == "ide.session"
        # File should be removed
        assert not sess_file.exists()

    def test_skips_recent_sessions(self, tmp_path):
        """Sessions that are not yet stale should not be flushed."""
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        ctx = {
            "trace_id": "c" * 32,
            "phantom_parent_id": "d" * 16,
            "start_time_ns": time.time_ns(),
            "ide": "copilot",
            "generation_count": 0,
        }
        sess_file = session_dir / "recent-sess.json"
        sess_file.write_text(json.dumps(ctx))

        tracer = mock.MagicMock()
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(session_dir)):
            otel_hook._flush_stale_sessions(tracer)

        tracer.start_span.assert_not_called()
        assert sess_file.exists()

    def test_no_crash_on_missing_dir(self):
        """Should not crash when session directory does not exist."""
        tracer = mock.MagicMock()
        with mock.patch.object(otel_hook, "_SESSION_DIR", "/nonexistent/path"):
            otel_hook._flush_stale_sessions(tracer)
        tracer.start_span.assert_not_called()

    def test_skips_empty_json(self, tmp_path):
        """Empty JSON files should be removed without emitting a span."""
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        sess_file = session_dir / "empty-sess.json"
        sess_file.write_text("{}")
        old_mtime = time.time() - 100_000
        os.utime(str(sess_file), (old_mtime, old_mtime))

        tracer = mock.MagicMock()
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(session_dir)):
            otel_hook._flush_stale_sessions(tracer)

        tracer.start_span.assert_not_called()
        assert not sess_file.exists()

    def test_disabled_when_ttl_zero(self, tmp_path):
        """Should not flush anything when TTL is zero (disabled)."""
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        sess_file = session_dir / "sess.json"
        sess_file.write_text(json.dumps({"trace_id": "a" * 32}))
        old_mtime = time.time() - 100_000
        os.utime(str(sess_file), (old_mtime, old_mtime))

        tracer = mock.MagicMock()
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(session_dir)):
            with mock.patch.dict(os.environ, {"IDE_OTEL_STATE_TTL_SECONDS": "0"}):
                otel_hook._flush_stale_sessions(tracer)

        tracer.start_span.assert_not_called()
        assert sess_file.exists()


# ── Flatten helper ────────────────────────────────────────────────────────


class TestFlatten:
    def test_flat(self):
        out = {}
        otel_hook._flatten(out, "ide.metadata", {"key": "val", "nested": {"a": 1}})
        assert out["ide.metadata.key"] == "val"
        assert out["ide.metadata.nested.a"] == 1

    def test_skips_none(self):
        out = {}
        otel_hook._flatten(out, "prefix", {"a": None, "b": 2})
        assert "prefix.a" not in out
        assert out["prefix.b"] == 2


# ── Main function integration ─────────────────────────────────────────────


class TestMainFlow:
    def test_outputs_continue_true(self, monkeypatch):
        """Main always outputs {"continue": true} for the IDE to proceed."""
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"hook_event_name":"stop"}'))
        # Prevent actual tracing init
        monkeypatch.setattr(otel_hook, "_init_tracing", lambda ide: False)
        monkeypatch.setattr(otel_hook, "_configure_logging", lambda: None)
        monkeypatch.setattr(otel_hook, "_cleanup_state", lambda: None)
        monkeypatch.setattr(otel_hook, "_load_config", lambda: {})

        captured = []
        monkeypatch.setattr("builtins.print", lambda s: captured.append(s))
        result = otel_hook.main()
        assert result == 0
        assert json.loads(captured[0]) == {"continue": True}

    def test_empty_input(self, monkeypatch):
        """Empty stdin should not crash."""
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
        monkeypatch.setattr(otel_hook, "_init_tracing", lambda ide: False)
        monkeypatch.setattr(otel_hook, "_configure_logging", lambda: None)
        monkeypatch.setattr(otel_hook, "_cleanup_state", lambda: None)
        monkeypatch.setattr(otel_hook, "_load_config", lambda: {})

        captured = []
        monkeypatch.setattr("builtins.print", lambda s: captured.append(s))
        result = otel_hook.main()
        assert result == 0

    def test_malformed_json(self, monkeypatch):
        """Malformed JSON should not crash."""
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{bad json"))
        monkeypatch.setattr(otel_hook, "_init_tracing", lambda ide: False)
        monkeypatch.setattr(otel_hook, "_configure_logging", lambda: None)
        monkeypatch.setattr(otel_hook, "_cleanup_state", lambda: None)
        monkeypatch.setattr(otel_hook, "_load_config", lambda: {})

        captured = []
        monkeypatch.setattr("builtins.print", lambda s: captured.append(s))
        result = otel_hook.main()
        assert result == 0


# ── Atomic write ──────────────────────────────────────────────────────────


class TestAtomicWrite:
    def test_writes_json_file(self, tmp_path):
        path = str(tmp_path / "test.json")
        otel_hook._atomic_write_json(path, {"key": "value"})
        with open(path) as f:
            assert json.load(f) == {"key": "value"}


# ── Duration formatting ──────────────────────────────────────────────────


class TestFmtDuration:
    def test_with_value(self):
        assert otel_hook._fmt_duration(150) == "150ms"

    def test_none(self):
        assert otel_hook._fmt_duration(None) == "n/a"
