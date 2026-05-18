"""Meaningful tests for otel_hook.py — focused on core logic, not too many."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import enrichment_connectors
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


class TestMemoryAggregation:
    def test_extract_event_memory_facts(self):
        facts = otel_hook._extract_event_memory_facts("AfterFileEdit", {
            "file_path": "src/main.py",
            "tool_name": "edit",
            "agent_name": "planner",
            "command": "pytest -q",
        })
        assert facts["files"] == ["src/main.py"]
        assert facts["tools"] == ["edit"]
        assert "planner" in facts["entities"]
        assert facts["commands"] == ["pytest -q"]

    def test_aggregate_generation_memory_dedupes_and_counts(self):
        batch = [
            {"event": "PreToolUse", "data": {"tool_name": "read", "file_path": "README.md"}},
            {"event": "PostToolUse", "data": {"tool_name": "read", "file_path": "README.md"}},
            {"event": "AfterFileEdit", "data": {"file_path": "otel_hook.py", "tool_name": "edit"}},
        ]
        summary = otel_hook._aggregate_generation_memory(batch)
        assert summary["files"] == ["README.md", "otel_hook.py"]
        assert summary["tools"] == ["read", "edit"]
        assert summary["tool_counts"] == {"read": 2, "edit": 1}

    def test_connector_aggregation_and_merge(self):
        batch = [
            {"event": "PreToolUse", "data": {"tool_name": "read", "file_path": "README.md"}},
            {"event": "AfterShellExecution", "data": {"command": "pytest -q"}},
        ]
        summary = enrichment_connectors.aggregate_generation_memory(batch)
        session = {"files": ["existing.md"], "tool_counts": {"read": 1}}
        enrichment_connectors.merge_memory_summaries(session, summary)
        assert session["files"] == ["existing.md", "README.md"]
        assert session["commands"] == ["pytest -q"]
        assert session["tool_counts"]["read"] == 2

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
        ("SessionStart", "SessionStart"),
        ("PreToolUse", "PreToolUse"),
    ])
    def test_known_events(self, raw, canonical):
        assert otel_hook._normalize_event(raw) == canonical

    def test_unknown_event_passthrough(self):
        assert otel_hook._normalize_event("customEvent") == "customEvent"


class TestNormalizeInputData:
    def test_adds_snake_case_aliases_for_camel_case_payloads(self):
        data = otel_hook._normalize_input_data({
            "sessionId": "sess-1",
            "requestModel": "claude-3-7-sonnet",
            "responseModel": "claude-3-7-sonnet",
            "toolName": "Bash",
            "toolInput": {"command": "pwd"},
            "providerName": "anthropic",
            "responseFormat": "json_schema",
            "choiceCount": 2,
            "systemInstructions": [{"type": "text", "content": "You are a planner."}],
            "cacheCreationInputTokens": 3,
            "cacheReadInputTokens": 2,
            "agentName": "planner",
            "hookEventType": "PreToolUse",
            "traceId": "a" * 32,
            "spanId": "b" * 16,
            "parentSpanId": "c" * 16,
            "traceFlags": "01",
            "traceState": "vendor=value",
        })
        assert data["session_id"] == "sess-1"
        assert data["request_model"] == "claude-3-7-sonnet"
        assert data["response_model"] == "claude-3-7-sonnet"
        assert data["tool_name"] == "Bash"
        assert data["tool_input"] == {"command": "pwd"}
        assert data["provider_name"] == "anthropic"
        assert data["response_format"] == "json_schema"
        assert data["choice_count"] == 2
        assert data["system_instructions"] == [{"type": "text", "content": "You are a planner."}]
        assert data["cache_creation_input_tokens"] == 3
        assert data["cache_read_input_tokens"] == 2
        assert data["agent_name"] == "planner"
        assert data["hook_event_type"] == "PreToolUse"
        assert data["trace_id"] == "a" * 32
        assert data["span_id"] == "b" * 16
        assert data["parent_span_id"] == "c" * 16
        assert data["trace_flags"] == "01"
        assert data["tracestate"] == "vendor=value"

    def test_returns_original_dict_when_no_aliases_are_needed(self):
        data = {"session_id": "sess-1"}
        assert otel_hook._normalize_input_data(data) is data

    def test_normalizes_session_id_when_only_camel_case_exists(self):
        data = otel_hook._normalize_input_data({"sessionId": "camel-session"})
        assert data["session_id"] == "camel-session"
        assert data["sessionId"] == "camel-session"

    def test_keeps_existing_snake_case_values(self):
        data = otel_hook._normalize_input_data({
            "sessionId": "camel-session",
            "session_id": "snake-session",
        })
        assert data["session_id"] == "snake-session"


class TestGetEventName:
    def test_hook_event_name(self):
        assert otel_hook._get_event_name({"hook_event_name": "sessionStart"}) == "sessionStart"

    def test_hook_event_type_after_normalization(self):
        data = otel_hook._normalize_input_data({"hookEventType": "PreToolUse"})
        assert otel_hook._get_event_name(data) == "PreToolUse"

    def test_hook_event_type(self):
        assert otel_hook._get_event_name({"hook_event_type": "PreToolUse"}) == "PreToolUse"

    def test_event_field(self):
        assert otel_hook._get_event_name({"event": "preToolUse"}) == "preToolUse"

    def test_prompt_fallback(self):
        assert otel_hook._get_event_name({"prompt": "hello"}) == "beforeSubmitPrompt"

    def test_empty_fallback(self):
        assert otel_hook._get_event_name({}) == "stop"


# ── IDE detection ─────────────────────────────────────────────────────────


class TestDetectIDE:
    def test_env_override_antigravity(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_IDE_NAME", "antigravity")
        assert otel_hook._detect_ide({"session_id": "sess-1"}) == "antigravity"

    def test_env_override_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_IDE_NAME", "AntiGravity")
        assert otel_hook._detect_ide({"session_id": "sess-1"}) == "antigravity"

    def test_claude_via_transcript_path(self):
        assert otel_hook._detect_ide({"session_id": "sess-1", "transcript_path": "/tmp/transcript.jsonl"}) == "claude"

    def test_claude_via_permission_mode(self):
        assert otel_hook._detect_ide({"session_id": "sess-1", "permission_mode": "acceptEdits"}) == "claude"

    def test_claude_via_notification_type(self):
        assert otel_hook._detect_ide({"session_id": "sess-1", "notification_type": "needs_permission"}) == "claude"

    def test_claude_code_self_reported_name(self):
        assert otel_hook._detect_ide({"ide_name": "Claude Code"}) == "claude"

    def test_anthropic_claude_code_self_reported_name(self):
        assert otel_hook._detect_ide({"client": "Anthropic Claude Code"}) == "claude"

    def test_claude_code_cli_self_reported_name(self):
        assert otel_hook._detect_ide({"client": "Claude Code CLI"}) == "claude"

    def test_cursor_via_conversation_id(self):
        assert otel_hook._detect_ide({"conversation_id": "abc"}) == "cursor"

    def test_cursor_via_generation_id(self):
        assert otel_hook._detect_ide({"generation_id": "gen-1"}) == "cursor"

    def test_cursor_ide_self_reported_name(self):
        assert otel_hook._detect_ide({"client": "Cursor IDE"}) == "cursor"

    def test_cursor_cli_self_reported_name(self):
        assert otel_hook._detect_ide({"client": "Cursor CLI"}) == "cursor"

    def test_cursor_via_indicators(self):
        assert otel_hook._detect_ide({"composer_mode": "agent"}) == "cursor"

    def test_copilot_via_session_id_only(self):
        # No cursor-specific fields → copilot
        assert otel_hook._detect_ide({"session_id": "sess-1"}) == "copilot"

    def test_github_copilot_chat_env_override(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_IDE_NAME", "GitHub Copilot Chat")
        assert otel_hook._detect_ide({"session_id": "sess-1"}) == "copilot"

    def test_github_copilot_self_reported_name(self):
        assert otel_hook._detect_ide({"ide_name": "GitHub Copilot"}) == "copilot"

    def test_github_copilot_cli_self_reported_name(self):
        assert otel_hook._detect_ide({"source_app": "GitHub Copilot CLI"}) == "copilot"

    def test_github_copilot_hyphenated_cli_env_override(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_IDE_NAME", "github-copilot-cli")
        assert otel_hook._detect_ide({"session_id": "sess-1"}) == "copilot"

    def test_opencode_env_override(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_IDE_NAME", "OpenCode")
        assert otel_hook._detect_ide({"session_id": "sess-1"}) == "opencode"

    def test_opencode_self_reported_name(self):
        assert otel_hook._detect_ide({"source_app": "OpenCode"}) == "opencode"

    def test_opencode_cli_self_reported_name(self):
        assert otel_hook._detect_ide({"source_app": "OpenCode CLI"}) == "opencode"

    def test_codex_env_override(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_IDE_NAME", "OpenAI Codex")
        assert otel_hook._detect_ide({"session_id": "sess-1"}) == "codex"

    def test_codex_cli_self_reported_name(self):
        assert otel_hook._detect_ide({"source_app": "Codex CLI"}) == "codex"

    def test_codex_via_turn_id(self):
        assert otel_hook._detect_ide({"hook_event_name": "PreToolUse", "session_id": "sess-1", "turn_id": "turn-1"}) == "codex"

    def test_antigravity_spaced_self_reported_name(self):
        assert otel_hook._detect_ide({"source_app": "Anti Gravity"}) == "antigravity"

    def test_antigravity_cli_self_reported_name(self):
        assert otel_hook._detect_ide({"source_app": "Anti Gravity CLI"}) == "antigravity"

    def test_self_reported_name_beats_claude_heuristics(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "1")
        assert otel_hook._detect_ide({"client": "Cursor IDE", "session_id": "sess-1"}) == "cursor"

    def test_empty_defaults_cursor(self):
        # Default when no IDE signals present
        with mock.patch("os.getcwd", return_value="/tmp/test"):
            with mock.patch("os.path.exists", return_value=False):
                assert otel_hook._detect_ide({}) == "cursor"


class TestDetectAgentEngine:
    def test_detects_claude_from_self_reported_payload(self):
        assert otel_hook._detect_agent_engine({"client": "Claude Code"}) == "claude"

    def test_detects_claude_from_heuristics(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "1")
        assert otel_hook._detect_agent_engine({"session_id": "sess-1"}) == "claude"

    def test_detects_claude_when_outer_cursor_ide_present(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "1")
        assert otel_hook._detect_agent_engine({"client": "Cursor IDE", "session_id": "sess-1"}) == "claude"

    def test_returns_none_without_engine_signal(self):
        assert otel_hook._detect_agent_engine({"session_id": "sess-1"}) is None

    def test_detects_gemini_from_corroborated_semantic_fields(self):
        assert otel_hook._detect_agent_engine({
            "gen_ai.client.name": "gemini",
            "service.name": "gemini-cli",
        }) == "gemini"

    def test_ignores_single_semantic_field_without_corroboration(self):
        assert otel_hook._detect_agent_engine({
            "gen_ai.system": "vscode",
            "session_id": "sess-1",
        }) is None


# ── OpenCode plugin payload handling ──────────────────────────────────────


class TestOpenCodePluginPayloads:
    """Verify that payloads emitted by plugin/opencode.ts are handled correctly.

    The TypeScript plugin sets source_app="OpenCode" and hook_event_name=<PascalCase>
    on every payload it sends to otel-hook. These tests confirm that the Python
    hook correctly detects the IDE and normalises the event name for each event
    type the plugin emits.
    """

    # ── IDE detection via source_app ──────────────────────────────────────

    def test_session_start_detected_as_opencode(self):
        payload = {"hook_event_name": "SessionStart", "source_app": "OpenCode", "session_id": "abc123", "cwd": "/home/user/project"}
        assert otel_hook._detect_ide(payload) == "opencode"

    def test_session_end_detected_as_opencode(self):
        payload = {"hook_event_name": "SessionEnd", "source_app": "OpenCode", "session_id": "abc123"}
        assert otel_hook._detect_ide(payload) == "opencode"

    def test_pre_tool_use_detected_as_opencode(self):
        payload = {"hook_event_name": "PreToolUse", "source_app": "OpenCode", "session_id": "abc123", "tool_name": "bash", "tool_id": "call-1"}
        assert otel_hook._detect_ide(payload) == "opencode"

    def test_post_tool_use_detected_as_opencode(self):
        payload = {"hook_event_name": "PostToolUse", "source_app": "OpenCode", "session_id": "abc123", "tool_name": "bash", "tool_id": "call-1", "tool_output": "ok"}
        assert otel_hook._detect_ide(payload) == "opencode"

    def test_stop_from_session_idle_detected_as_opencode(self):
        payload = {"hook_event_name": "Stop", "source_app": "OpenCode", "session_id": "abc123", "status": "idle"}
        assert otel_hook._detect_ide(payload) == "opencode"

    def test_user_prompt_submit_detected_as_opencode(self):
        payload = {"hook_event_name": "UserPromptSubmit", "source_app": "OpenCode", "session_id": "abc123", "prompt": "list files"}
        assert otel_hook._detect_ide(payload) == "opencode"

    def test_session_end_error_detected_as_opencode(self):
        payload = {"hook_event_name": "SessionEnd", "source_app": "OpenCode", "session_id": "abc123", "status": "error"}
        assert otel_hook._detect_ide(payload) == "opencode"

    def test_after_file_edit_detected_as_opencode(self):
        # file.edited carries no session_id — only file_path and source_app.
        payload = {"hook_event_name": "AfterFileEdit", "source_app": "OpenCode", "file_path": "/home/user/project/main.py"}
        assert otel_hook._detect_ide(payload) == "opencode"

    def test_post_tool_use_failure_detected_as_opencode(self):
        payload = {"hook_event_name": "PostToolUseFailure", "source_app": "OpenCode", "session_id": "abc123",
                   "tool_name": "bash", "tool_id": "call-1", "exit_code": 1, "error": "exit 1"}
        assert otel_hook._detect_ide(payload) == "opencode"

    # ── Event name normalisation ──────────────────────────────────────────

    @pytest.mark.parametrize("event_name", [
        "SessionStart",
        "SessionEnd",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "UserPromptSubmit",
        "AfterFileEdit",
    ])
    def test_plugin_events_already_canonical(self, event_name):
        # Plugin emits PascalCase names that are already canonical — no mapping needed.
        assert otel_hook._normalize_event(event_name) == event_name

    def test_get_event_name_from_plugin_payload(self):
        payload = {"hook_event_name": "PreToolUse", "source_app": "OpenCode", "session_id": "s1", "tool_name": "bash"}
        assert otel_hook._get_event_name(payload) == "PreToolUse"

    def test_get_event_name_stop_from_session_idle(self):
        payload = {"hook_event_name": "Stop", "source_app": "OpenCode", "session_id": "s1", "status": "idle"}
        assert otel_hook._get_event_name(payload) == "Stop"

    def test_get_event_name_user_prompt_submit(self):
        payload = {"hook_event_name": "UserPromptSubmit", "source_app": "OpenCode", "session_id": "s1", "prompt": "hello"}
        assert otel_hook._get_event_name(payload) == "UserPromptSubmit"

    def test_get_event_name_session_end_error(self):
        payload = {"hook_event_name": "SessionEnd", "source_app": "OpenCode", "session_id": "s1", "status": "error"}
        assert otel_hook._get_event_name(payload) == "SessionEnd"

    def test_get_event_name_after_file_edit(self):
        payload = {"hook_event_name": "AfterFileEdit", "source_app": "OpenCode", "file_path": "/src/main.py"}
        assert otel_hook._get_event_name(payload) == "AfterFileEdit"

    def test_get_event_name_post_tool_use_failure(self):
        payload = {"hook_event_name": "PostToolUseFailure", "source_app": "OpenCode",
                   "session_id": "s1", "tool_name": "bash", "exit_code": 1, "error": "exit 1"}
        assert otel_hook._get_event_name(payload) == "PostToolUseFailure"

    # ── source_app takes priority over PascalCase auto-detection ─────────

    def test_source_app_beats_pascal_case_detection(self):
        # PascalCase hook_event_name would normally signal Claude Code (level 4),
        # but source_app="OpenCode" is a level-1 self-reported field and wins.
        payload = {"hook_event_name": "PreToolUse", "source_app": "OpenCode", "session_id": "s1"}
        assert otel_hook._detect_ide(payload) == "opencode"

    # ── IDE_OTEL_IDE_NAME env var still overrides source_app ─────────────

    def test_env_var_overrides_source_app(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_IDE_NAME", "cursor")
        payload = {"hook_event_name": "PreToolUse", "source_app": "OpenCode", "session_id": "s1"}
        assert otel_hook._detect_ide(payload) == "cursor"


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


# ── MDM configuration ────────────────────────────────────────────────────


class TestLoadMdmConfig:
    def test_returns_empty_on_linux(self):
        with mock.patch("sys.platform", "linux"):
            assert otel_hook._load_mdm_config() == {}

    def test_dispatches_to_macos(self):
        with mock.patch("sys.platform", "darwin"):
            with mock.patch.object(otel_hook, "_load_mdm_config_macos", return_value={"k": "v"}) as m:
                assert otel_hook._load_mdm_config() == {"k": "v"}
                m.assert_called_once()

    def test_dispatches_to_windows(self):
        with mock.patch("sys.platform", "win32"):
            with mock.patch.object(otel_hook, "_load_mdm_config_windows", return_value={"k": "v"}) as m:
                assert otel_hook._load_mdm_config() == {"k": "v"}
                m.assert_called_once()


class TestLoadMdmConfigMacOS:
    def test_reads_system_managed_plist(self, tmp_path):
        import plistlib
        plist_data = {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://mdm.example.com:4317"}
        plist_file = tmp_path / "managed.plist"
        with open(plist_file, "wb") as fh:
            plistlib.dump(plist_data, fh)
        domain = otel_hook._MDM_DOMAIN
        system_path = f"/Library/Managed Preferences/{domain}.plist"
        real_open = open
        def fake_open(p, *a, **kw):
            if p == system_path:
                return real_open(str(plist_file), *a, **kw)
            return real_open(p, *a, **kw)
        with mock.patch("os.path.exists", side_effect=lambda p: p == system_path):
            with mock.patch("builtins.open", side_effect=fake_open):
                result = otel_hook._load_mdm_config_macos()
        assert result == plist_data

    def test_falls_back_to_user_managed_plist(self, tmp_path):
        import plistlib
        plist_data = {"OTEL_SERVICE_NAME": "mdm-agent"}
        plist_file = tmp_path / "user.plist"
        with open(plist_file, "wb") as fh:
            plistlib.dump(plist_data, fh)
        domain = otel_hook._MDM_DOMAIN
        system_path = f"/Library/Managed Preferences/{domain}.plist"
        user_path = os.path.expanduser(f"~/Library/Managed Preferences/{domain}.plist")
        real_open = open
        def fake_exists(p):
            if p == system_path:
                return False
            if p == user_path:
                return True
            return False
        def fake_open(p, *a, **kw):
            if p == user_path:
                return real_open(str(plist_file), *a, **kw)
            return real_open(p, *a, **kw)
        with mock.patch("os.path.exists", side_effect=fake_exists):
            with mock.patch("builtins.open", side_effect=fake_open):
                result = otel_hook._load_mdm_config_macos()
        assert result == plist_data

    def test_returns_empty_when_no_plist(self):
        with mock.patch("os.path.exists", return_value=False):
            assert otel_hook._load_mdm_config_macos() == {}

    def test_returns_empty_on_read_error(self):
        with mock.patch("os.path.exists", return_value=True):
            with mock.patch("builtins.open", side_effect=OSError("permission denied")):
                assert otel_hook._load_mdm_config_macos() == {}


class TestLoadMdmConfigWindows:
    def test_returns_empty_when_no_winreg(self):
        with mock.patch.dict("sys.modules", {"winreg": None}):
            # On non-Windows, winreg import fails; function should return {}
            result = otel_hook._load_mdm_config_windows()
            assert result == {}

    def test_reads_registry_values(self):
        fake_winreg = mock.MagicMock()
        fake_winreg.HKEY_LOCAL_MACHINE = 0x80000002
        fake_winreg.HKEY_CURRENT_USER = 0x80000001
        fake_key = mock.MagicMock()
        fake_winreg.OpenKey.return_value.__enter__ = mock.Mock(return_value=fake_key)
        fake_winreg.OpenKey.return_value.__exit__ = mock.Mock(return_value=False)
        def enum_side_effect(key, idx):
            if key is fake_key and idx == 0:
                return ("OTEL_SERVICE_NAME", "mdm-service", 1)
            if key is fake_key and idx == 1:
                return ("IDE_OTEL_CAPTURE_TEXT", "false", 1)
            raise OSError("no more values")
        fake_winreg.EnumValue.side_effect = enum_side_effect
        with mock.patch.dict("sys.modules", {"winreg": fake_winreg}):
            # Patching sys.modules is sufficient for the local import in _load_mdm_config_windows().
            result = otel_hook._load_mdm_config_windows()
        assert result.get("OTEL_SERVICE_NAME") == "mdm-service"
        assert result.get("IDE_OTEL_CAPTURE_TEXT") == "false"

    def test_returns_empty_on_registry_not_found(self):
        fake_winreg = mock.MagicMock()
        fake_winreg.HKEY_LOCAL_MACHINE = 0x80000002
        fake_winreg.HKEY_CURRENT_USER = 0x80000001
        fake_winreg.OpenKey.side_effect = OSError("key not found")
        with mock.patch.dict("sys.modules", {"winreg": fake_winreg}):
            result = otel_hook._load_mdm_config_windows()
        assert result == {}


class TestMdmConfigPrecedence:
    def test_mdm_overrides_json_config(self, tmp_path):
        config_file = tmp_path / "otel_config.json"
        config_file.write_text(json.dumps({
            "OTEL_SERVICE_NAME": "json-service",
            "IDE_OTEL_CAPTURE_TEXT": "true",
        }))
        mdm_config = {"OTEL_SERVICE_NAME": "mdm-enforced-service"}
        with mock.patch.object(otel_hook, "_CONFIG_DEFAULT", str(config_file)):
            with mock.patch.object(otel_hook, "_load_mdm_config", return_value=mdm_config):
                with mock.patch.dict(os.environ, {}, clear=True):
                    result = otel_hook._load_config()
        assert result["OTEL_SERVICE_NAME"] == "mdm-enforced-service"
        assert result["IDE_OTEL_CAPTURE_TEXT"] == "true"

    def test_env_overrides_mdm(self, tmp_path):
        config_file = tmp_path / "otel_config.json"
        config_file.write_text(json.dumps({"OTEL_SERVICE_NAME": "json-service"}))
        mdm_config = {"OTEL_SERVICE_NAME": "mdm-service"}
        with mock.patch.object(otel_hook, "_CONFIG_DEFAULT", str(config_file)):
            with mock.patch.object(otel_hook, "_load_mdm_config", return_value=mdm_config):
                with mock.patch.dict(os.environ, {"OTEL_SERVICE_NAME": "env-service"}, clear=False):
                    result = otel_hook._load_config()
                    otel_hook._apply_config_env(result)
                    assert os.environ["OTEL_SERVICE_NAME"] == "env-service"

    def test_mdm_empty_no_effect(self, tmp_path):
        config_file = tmp_path / "otel_config.json"
        config_file.write_text(json.dumps({"OTEL_SERVICE_NAME": "json-service"}))
        with mock.patch.object(otel_hook, "_CONFIG_DEFAULT", str(config_file)):
            with mock.patch.object(otel_hook, "_load_mdm_config", return_value={}):
                with mock.patch.dict(os.environ, {}, clear=True):
                    result = otel_hook._load_config()
        assert result["OTEL_SERVICE_NAME"] == "json-service"

    def test_apply_config_skips_underscore_keys(self):
        config = {"_comment_1": "=== Section ===", "OTEL_SERVICE_NAME": "test-svc"}
        with mock.patch.dict(os.environ, {}, clear=True):
            otel_hook._apply_config_env(config)
            assert "_comment_1" not in os.environ
            assert os.environ["OTEL_SERVICE_NAME"] == "test-svc"


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

    def test_camel_case_alias_after_normalization(self):
        data = otel_hook._normalize_input_data({"sessionId": "s1"})
        assert otel_hook._session_key(data) == "s1"


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
        for evt in ("SessionStart", "SessionEnd", "SubagentStart", "PreCompact", "PostCompact"):
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


class TestGenAISemconv:
    @staticmethod
    def _attrs(span):
        return {
            args[0]: args[1]
            for args, _kwargs in (call for call in span.set_attribute.call_args_list)
        }

    def test_infers_provider_and_sets_v137_attributes(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_CAPTURE_TEXT", "true")
        span = mock.MagicMock()

        otel_hook._apply_genai_semconv(span, "SubagentStart", {
            "model": "claude-3-7-sonnet",
            "response_model": "claude-3-7-sonnet",
            "response_format": "json_schema",
            "choice_count": "2",
            "agent_id": "agent-1",
            "agent_name": "planner",
            "agent_version": "2026.03",
            "agent_description": "Plans coding steps",
            "system_prompt": "You are a planner.",
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "cache_creation_input_tokens": 3,
                "cached_input_tokens": 2,
            },
        }, "claude")

        attrs = self._attrs(span)
        assert attrs["gen_ai.provider.name"] == "anthropic"
        assert attrs["gen_ai.system"] == "claude"
        assert attrs["gen_ai.output.type"] == "json"
        assert attrs["gen_ai.request.choice.count"] == 2
        assert attrs["gen_ai.agent.id"] == "agent-1"
        assert attrs["gen_ai.agent.name"] == "planner"
        assert attrs["gen_ai.agent.version"] == "2026.03"
        assert attrs["gen_ai.agent.description"] == "Plans coding steps"
        assert attrs["gen_ai.usage.input_tokens"] == 11
        assert attrs["gen_ai.usage.output_tokens"] == 7
        assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 3
        assert attrs["gen_ai.usage.cache_read.input_tokens"] == 2
        assert attrs["gen_ai.system_instructions"] == "You are a planner."

    def test_explicit_provider_overrides_model_inference(self):
        span = mock.MagicMock()

        otel_hook._apply_genai_semconv(span, "UserPromptSubmit", {
            "provider_name": "openai",
            "model": "claude-3-7-sonnet",
        }, "cursor")

        attrs = self._attrs(span)
        assert attrs["gen_ai.provider.name"] == "openai"
        assert attrs["gen_ai.system"] == "cursor"

    def test_nested_agent_engine_becomes_genai_system(self):
        span = mock.MagicMock()

        otel_hook._apply_genai_semconv(
            span,
            "SubagentStop",
            {"gen_ai.client.name": "gemini", "service.name": "gemini-cli"},
            "claude",
            session_ctx={"agent_engine": "gemini"},
        )

        attrs = self._attrs(span)
        assert attrs["gen_ai.system"] == "gemini"

    def test_single_semantic_field_does_not_change_genai_system(self):
        span = mock.MagicMock()

        otel_hook._apply_genai_semconv(
            span,
            "UserPromptSubmit",
            {"gen_ai.system": "vscode"},
            "cursor",
            session_ctx={},
        )

        attrs = self._attrs(span)
        assert attrs["gen_ai.system"] == "cursor"


class TestClientIdentityAttributes:
    @staticmethod
    def _attrs(span):
        return {
            args[0]: args[1]
            for args, _kwargs in (call for call in span.set_attribute.call_args_list)
        }

    def test_sets_nested_agent_engine_when_distinct_from_outer_ide(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "1")
        span = mock.MagicMock()

        otel_hook._set_client_identity_attributes(span, "cursor", data={"session_id": "sess-1"})

        attrs = self._attrs(span)
        assert attrs["gen_ai.client.name"] == "claude"
        assert attrs["gen_ai.client.wrapper"] == "cursor"
        assert attrs["gen_ai.client.agent_engine"] == "claude"

    def test_omits_agent_engine_when_same_as_outer_ide(self):
        span = mock.MagicMock()

        otel_hook._set_client_identity_attributes(span, "claude", data={"client": "Claude Code"})

        attrs = self._attrs(span)
        assert attrs["gen_ai.client.name"] == "claude"
        assert "gen_ai.client.agent_engine" not in attrs

    def test_promotes_gemini_over_outer_claude(self):
        span = mock.MagicMock()

        otel_hook._set_client_identity_attributes(
            span,
            "claude",
            data={"gen_ai.client.name": "gemini", "service.name": "gemini-cli"},
        )

        attrs = self._attrs(span)
        assert attrs["gen_ai.client.name"] == "gemini"
        assert attrs["gen_ai.client.wrapper"] == "claude"
        assert attrs["gen_ai.client.agent_engine"] == "gemini"

    def test_preserves_outer_agent_when_single_semantic_field_disagrees(self):
        span = mock.MagicMock()

        otel_hook._set_client_identity_attributes(span, "cursor", data={"gen_ai.system": "vscode"})

        attrs = self._attrs(span)
        assert attrs["gen_ai.client.name"] == "cursor"
        assert "gen_ai.client.wrapper" not in attrs
        assert "gen_ai.client.agent_engine" not in attrs


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


# ── Local trace persistence ────────────────────────────────────────────────


class TestLocalTracePersistence:
    def _make_mock_span(self, name="gen_ai.client.generation", session_key="sess-1", trace_id=0xABCD, span_id=0x1234):
        span = mock.MagicMock()
        span.name = name
        ctx = mock.MagicMock()
        ctx.trace_id = trace_id
        ctx.span_id = span_id
        span.context = ctx
        span.parent = None
        span.start_time = 1_000_000_000
        span.end_time = 2_000_000_000
        span.attributes = {"gen_ai.client.session.key": session_key}
        span.status = mock.MagicMock()
        span.status.status_code.name = "OK"
        return span

    def test_file_span_exporter_writes_jsonl(self, tmp_path):
        out_file = str(tmp_path / "spans.jsonl")
        exporter = otel_hook._FileSpanExporter(out_file)
        span = self._make_mock_span()
        with mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
            exporter.export([span])
        assert os.path.exists(out_file)
        with open(out_file) as f:
            rec = json.loads(f.read().strip())
        assert rec["name"] == "gen_ai.client.generation"
        assert rec["attributes"]["gen_ai.client.session.key"] == "sess-1"
        assert rec["status"] == "OK"

    def test_file_span_exporter_appends_multiple_spans(self, tmp_path):
        out_file = str(tmp_path / "spans.jsonl")
        exporter = otel_hook._FileSpanExporter(out_file)
        with mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
            exporter.export([self._make_mock_span("span-1")])
            exporter.export([self._make_mock_span("span-2")])
        with open(out_file) as f:
            lines = f.read().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["name"] == "span-1"
        assert json.loads(lines[1])["name"] == "span-2"

    def test_file_span_exporter_shutdown_is_noop(self, tmp_path):
        exporter = otel_hook._FileSpanExporter(str(tmp_path / "spans.jsonl"))
        exporter.shutdown()  # must not raise

    def test_file_span_exporter_force_flush_returns_true(self, tmp_path):
        exporter = otel_hook._FileSpanExporter(str(tmp_path / "spans.jsonl"))
        assert exporter.force_flush() is True

    def test_uses_batch_fallback_when_local_flag_unset(self, monkeypatch):
        monkeypatch.delenv("IDE_OTEL_LOCAL_SPANS", raising=False)
        monkeypatch.delenv("IDE_OTEL_LOCAL_TRACE_SAVING", raising=False)
        monkeypatch.setenv("IDE_OTEL_BATCH_ON_STOP", "true")
        # Local trace saving should still be enabled via the batch fallback mechanism.
        assert otel_hook._local_spans_enabled() is True


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

    def test_create_uses_upstream_trace_context_when_present(self, tmp_path):
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(tmp_path)):
            with mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
                ctx = otel_hook._create_session_context(
                    "sess-upstream",
                    {"traceparent": f"00-{'a' * 32}-{'b' * 16}-00", "tracestate": "vendor=value"},
                    "cursor",
                )
                assert ctx["trace_id"] == "a" * 32
                assert ctx["upstream_parent_span_id"] == "b" * 16
                assert ctx["trace_flags"] == "00"
                assert ctx["tracestate"] == "vendor=value"
                assert ctx["context_origin"] == "upstream"

    def test_binds_existing_synthetic_session_to_first_upstream_context(self, tmp_path):
        with mock.patch.object(otel_hook, "_SESSION_DIR", str(tmp_path)):
            with mock.patch.object(otel_hook, "_LOCK_DIR", str(tmp_path / "locks")):
                ctx = otel_hook._create_session_context("sess-bind", {}, "cursor")
                assert ctx["context_origin"] == "synthetic"
                updated = otel_hook._maybe_bind_session_to_upstream_context(
                    "sess-bind",
                    ctx,
                    {"trace_id": "d" * 32, "span_id": "e" * 16},
                )
                assert updated["trace_id"] == "d" * 32
                assert updated["upstream_parent_span_id"] == "e" * 16
                assert updated["context_origin"] == "upstream"
                loaded = otel_hook._load_session_context("sess-bind")
                assert loaded["trace_id"] == "d" * 32
                assert loaded["upstream_parent_span_id"] == "e" * 16


class TestUpstreamTraceContext:
    def test_parses_traceparent_payload(self):
        ctx = otel_hook._resolve_upstream_trace_context({
            "traceparent": f"00-{'1' * 32}-{'2' * 16}-00",
            "tracestate": "vendor=value",
        })
        assert ctx == {
            "trace_id": "1" * 32,
            "parent_span_id": "2" * 16,
            "trace_flags": "00",
            "tracestate": "vendor=value",
        }

    def test_parses_future_version_traceparent_with_extra_fields(self):
        ctx = otel_hook._resolve_upstream_trace_context({
            "traceparent": f"01-{'1' * 32}-{'2' * 16}-00-extra-fields-allowed",
        })
        assert ctx == {
            "trace_id": "1" * 32,
            "parent_span_id": "2" * 16,
            "trace_flags": "00",
        }

    def test_rejects_version_00_traceparent_with_extra_fields(self):
        assert otel_hook._resolve_upstream_trace_context({
            "traceparent": f"00-{'1' * 32}-{'2' * 16}-00-extra-fields-not-allowed",
        }) is None

    def test_explicit_ids_prefer_current_span_id_as_parent(self):
        ctx = otel_hook._resolve_upstream_trace_context({
            "trace_id": "3" * 32,
            "span_id": "4" * 16,
            "parent_span_id": "5" * 16,
            "trace_flags": "01",
        })
        assert ctx == {
            "trace_id": "3" * 32,
            "parent_span_id": "4" * 16,
            "trace_flags": "01",
        }

    def test_reads_trace_context_from_env(self, monkeypatch):
        monkeypatch.setenv("TRACEPARENT", f"00-{'6' * 32}-{'7' * 16}-01")
        monkeypatch.setenv("TRACESTATE", "foo=bar")
        ctx = otel_hook._resolve_upstream_trace_context({})
        assert ctx == {
            "trace_id": "6" * 32,
            "parent_span_id": "7" * 16,
            "trace_flags": "01",
            "tracestate": "foo=bar",
        }

    def test_rejects_zero_trace_context(self):
        assert otel_hook._resolve_upstream_trace_context({
            "trace_id": "0" * 32,
            "span_id": "1" * 16,
        }) is None


class TestTraceContextSelection:
    def test_session_trace_context_prefers_upstream_parent(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            otel_hook,
            "_make_trace_context",
            lambda tid, sid, flags="01", tracestate=None: calls.append((tid, sid, flags, tracestate)) or "ctx",
        )
        session_ctx = {
            "trace_id": "8" * 32,
            "upstream_parent_span_id": "9" * 16,
            "phantom_parent_id": "a" * 16,
            "trace_flags": "00",
            "tracestate": "vendor=value",
        }
        assert otel_hook._session_trace_context(session_ctx) == "ctx"
        assert calls == [("8" * 32, "9" * 16, "00", "vendor=value")]

    def test_event_context_overrides_session_fallback(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            otel_hook,
            "_make_trace_context",
            lambda tid, sid, flags="01", tracestate=None: calls.append((tid, sid, flags, tracestate)) or "ctx",
        )
        session_ctx = {
            "trace_id": "b" * 32,
            "upstream_parent_span_id": "c" * 16,
            "phantom_parent_id": "d" * 16,
        }
        assert otel_hook._event_parent_trace_context(
            {"trace_id": "e" * 32, "span_id": "f" * 16, "trace_flags": "00"},
            session_ctx,
        ) == "ctx"
        assert calls == [("e" * 32, "f" * 16, "00", None)]


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
        assert tracer.start_span.call_args[0][0] == "gen_ai.client.session"
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
        otel_hook._flatten(out, "gen_ai.client.metadata", {"key": "val", "nested": {"a": 1}})
        assert out["gen_ai.client.metadata.key"] == "val"
        assert out["gen_ai.client.metadata.nested.a"] == 1

    def test_skips_none(self):
        out = {}
        otel_hook._flatten(out, "prefix", {"a": None, "b": 2})
        assert "prefix.a" not in out
        assert out["prefix.b"] == 2


# ── Main function integration ─────────────────────────────────────────────


class TestMainFlow:
    def test_outputs_continue_true(self, monkeypatch):
        """Main outputs legacy continue response by default."""
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"hook_event_name":"stop"}'))
        # Prevent actual tracing init
        monkeypatch.setattr(otel_hook, "_init_tracing", lambda ide, **kwargs: False)
        monkeypatch.setattr(otel_hook, "_configure_logging", lambda: None)
        monkeypatch.setattr(otel_hook, "_cleanup_state", lambda: None)
        monkeypatch.setattr(otel_hook, "_load_config", lambda: {})
        monkeypatch.delenv("IDE_OTEL_BATCH_ON_STOP", raising=False)
        monkeypatch.delenv("IDE_OTEL_LOCAL_SPANS", raising=False)
        monkeypatch.delenv("IDE_OTEL_LOCAL_TRACE_SAVING", raising=False)

        captured = []
        monkeypatch.setattr("builtins.print", lambda s: captured.append(s))
        result = otel_hook.main()
        assert result == 0
        assert json.loads(captured[0]) == {"continue": True}

    def test_codex_session_start_suppresses_stdout(self, monkeypatch):
        monkeypatch.setattr(
            "sys.stdin",
            __import__("io").StringIO('{"hook_event_name":"SessionStart","session_id":"s1","source_app":"Codex"}'),
        )
        monkeypatch.setattr(otel_hook, "_init_tracing", lambda ide, **kwargs: False)
        monkeypatch.setattr(otel_hook, "_configure_logging", lambda: None)
        monkeypatch.setattr(otel_hook, "_cleanup_state", lambda: None)
        monkeypatch.setattr(otel_hook, "_load_config", lambda: {})
        captured = []
        monkeypatch.setattr("builtins.print", lambda s: captured.append(s))

        result = otel_hook.main()

        assert result == 0
        assert captured == []

    def test_codex_user_prompt_submit_suppresses_stdout(self, monkeypatch):
        monkeypatch.setattr(
            "sys.stdin",
            __import__("io").StringIO('{"hook_event_name":"UserPromptSubmit","session_id":"s1","source_app":"Codex"}'),
        )
        monkeypatch.setattr(otel_hook, "_init_tracing", lambda ide, **kwargs: False)
        monkeypatch.setattr(otel_hook, "_configure_logging", lambda: None)
        monkeypatch.setattr(otel_hook, "_cleanup_state", lambda: None)
        monkeypatch.setattr(otel_hook, "_load_config", lambda: {})
        captured = []
        monkeypatch.setattr("builtins.print", lambda s: captured.append(s))

        result = otel_hook.main()

        assert result == 0
        assert captured == []

    def test_codex_stop_keeps_json_stdout(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"hook_event_name":"Stop","session_id":"s1","source_app":"Codex"}'))
        monkeypatch.setattr(otel_hook, "_init_tracing", lambda ide, **kwargs: False)
        monkeypatch.setattr(otel_hook, "_configure_logging", lambda: None)
        monkeypatch.setattr(otel_hook, "_cleanup_state", lambda: None)
        monkeypatch.setattr(otel_hook, "_load_config", lambda: {})
        monkeypatch.delenv("IDE_OTEL_LOCAL_SPANS", raising=False)
        monkeypatch.delenv("IDE_OTEL_LOCAL_TRACE_SAVING", raising=False)
        monkeypatch.delenv("IDE_OTEL_BATCH_ON_STOP", raising=False)
        captured = []
        monkeypatch.setattr("builtins.print", lambda s: captured.append(s))

        result = otel_hook.main()

        assert result == 0
        assert json.loads(captured[0]) == {"continue": True}

    def test_codex_session_start_governance_uses_adapter_payload(self, monkeypatch):
        monkeypatch.delenv("IDE_OTEL_LOCAL_SPANS", raising=False)
        monkeypatch.delenv("IDE_OTEL_LOCAL_TRACE_SAVING", raising=False)
        monkeypatch.delenv("IDE_OTEL_BATCH_ON_STOP", raising=False)
        response = otel_hook._stdout_response(
            "SessionStart",
            "codex",
            {"session_id": "s1"},
            governance=otel_hook.GovernanceResponse(
                system_message="workspace policy loaded",
                hook_specific_output={
                    "hookEventName": "SessionStart",
                    "additionalContext": "Load repository guardrails before editing.",
                },
            ),
        )

        assert json.loads(response) == {
            "continue": True,
            "systemMessage": "workspace policy loaded",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "Load repository guardrails before editing.",
            },
        }

    def test_continue_response_opt_in_local_spans_flag(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_LOCAL_SPANS", "true")
        assert json.loads(otel_hook._continue_response_json()) == {
            "continue": True, "local_spans": True
        }

    def test_local_spans_uses_batch_fallback(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_BATCH_ON_STOP", "true")
        monkeypatch.delenv("IDE_OTEL_LOCAL_SPANS", raising=False)
        monkeypatch.delenv("IDE_OTEL_LOCAL_TRACE_SAVING", raising=False)
        assert otel_hook._local_spans_enabled() is True

    def test_main_enables_file_exporter_when_local_spans_enabled(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_LOCAL_SPANS", "true")
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"hook_event_name":"Stop"}'))
        monkeypatch.setattr(otel_hook, "_configure_logging", lambda: None)
        monkeypatch.setattr(otel_hook, "_cleanup_state", lambda: None)
        monkeypatch.setattr(otel_hook, "_load_config", lambda: {})
        monkeypatch.setattr(otel_hook, "_init_tracing", lambda ide, **kwargs: True)
        monkeypatch.setattr(otel_hook, "_flush_stale_sessions", lambda tracer: None)
        monkeypatch.setattr(otel_hook, "_force_flush_provider", lambda **kw: None)
        mock_span_cm = mock.MagicMock()
        mock_span_cm.__enter__ = mock.MagicMock(return_value=mock_span_cm)
        mock_span_cm.__exit__ = mock.MagicMock(return_value=False)
        mock_tracer = mock.MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_span_cm
        mock_trace = mock.MagicMock()
        mock_trace.get_tracer.return_value = mock_tracer
        monkeypatch.setattr(otel_hook, "trace", mock_trace)
        calls = []
        monkeypatch.setattr(otel_hook, "_enable_file_exporter", lambda path: calls.append(path))
        captured = []
        monkeypatch.setattr("builtins.print", lambda *a, **kw: captured.append(a[0] if a else ""))
        result = otel_hook.main()
        assert result == 0
        assert len(calls) == 1 and calls[0].endswith(".jsonl")

    def test_empty_input(self, monkeypatch):
        """Empty stdin should not crash."""
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
        monkeypatch.setattr(otel_hook, "_init_tracing", lambda ide, **kwargs: False)
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
        monkeypatch.setattr(otel_hook, "_init_tracing", lambda ide, **kwargs: False)
        monkeypatch.setattr(otel_hook, "_configure_logging", lambda: None)
        monkeypatch.setattr(otel_hook, "_cleanup_state", lambda: None)
        monkeypatch.setattr(otel_hook, "_load_config", lambda: {})

        captured = []
        monkeypatch.setattr("builtins.print", lambda s: captured.append(s))
        result = otel_hook.main()
        assert result == 0

    def test_batch_buffered_event_skips_tracing_init(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_BATCH_ON_STOP", "true")
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"hook_event_name":"preToolUse","session_id":"s1"}'))
        monkeypatch.setattr(otel_hook, "_configure_logging", lambda: None)
        monkeypatch.setattr(otel_hook, "_cleanup_state", lambda: None)
        monkeypatch.setattr(otel_hook, "_load_config", lambda: {})
        monkeypatch.setattr(otel_hook, "_load_session_context", lambda _sk: {"current_generation": "g1"})
        appended = []
        monkeypatch.setattr(otel_hook, "_append_batch_event", lambda key, evt, data: appended.append((key, evt)))
        called = {"init": 0}
        monkeypatch.setattr(otel_hook, "_init_tracing", lambda ide, **kwargs: called.__setitem__("init", called["init"] + 1) or True)
        captured = []
        monkeypatch.setattr("builtins.print", lambda s: captured.append(s))
        result = otel_hook.main()
        assert result == 0
        assert appended == [("g1", "PreToolUse")]
        assert called["init"] == 0


class TestFastJsonLoads:
    def test_fast_json_loads_orjson_path(self, monkeypatch):
        class FakeOrjson:
            @staticmethod
            def loads(raw):
                return {"x": 1} if raw == '{"x":1}' else {}

        monkeypatch.setattr(otel_hook, "_ORJSON", FakeOrjson)
        assert otel_hook._fast_json_loads('{"x":1}') == {"x": 1}


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


# ── File-only TracerProvider mode ────────────────────────────────────────


class TestFileOnlyTracerProvider:
    def test_no_otlp_exporter_when_no_endpoint_and_local_spans(self, monkeypatch):
        """When no OTLP endpoint is set and local spans are enabled,
        _init_sdk_tracer_provider should create a bare provider without OTLP."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.setenv("IDE_OTEL_LOCAL_SPANS", "true")
        set_provider_calls = []
        monkeypatch.setattr(
            otel_hook.trace, "set_tracer_provider",
            lambda p: set_provider_calls.append(p),
        )
        result = otel_hook._init_sdk_tracer_provider({}, False)
        assert result is True
        assert len(set_provider_calls) == 1
        provider = set_provider_calls[0]
        # No span processors should have been added (no OTLP exporter)
        assert len(provider._active_span_processor._span_processors) == 0

    def test_otlp_exporter_created_when_endpoint_set(self, monkeypatch):
        """When an OTLP endpoint is configured, the normal exporter path runs."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        monkeypatch.setenv("IDE_OTEL_LOCAL_SPANS", "true")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
        set_provider_calls = []
        monkeypatch.setattr(
            otel_hook.trace, "set_tracer_provider",
            lambda p: set_provider_calls.append(p),
        )
        result = otel_hook._init_sdk_tracer_provider({}, False)
        assert result is True
        assert len(set_provider_calls) == 1
        provider = set_provider_calls[0]
        # At least one span processor should have been added (OTLP exporter)
        assert len(provider._active_span_processor._span_processors) >= 1


# ── Hook home resolution ──────────────────────────────────────────────────


class TestResolveHookHome:
    def test_explicit_env_var_takes_precedence(self, tmp_path, monkeypatch):
        """IDE_OTEL_HOOK_HOME overrides every other heuristic."""
        explicit = str(tmp_path / "my-hook-home")
        monkeypatch.setenv("IDE_OTEL_HOOK_HOME", explicit)
        result = otel_hook._resolve_hook_home()
        assert result == os.path.abspath(explicit)

    def test_explicit_env_var_resolves_to_abspath(self, monkeypatch):
        """Relative IDE_OTEL_HOOK_HOME values are converted to absolute paths."""
        monkeypatch.setenv("IDE_OTEL_HOOK_HOME", "relative/path")
        result = otel_hook._resolve_hook_home()
        assert os.path.isabs(result)

    def test_site_packages_returns_xdg_data_home(self, monkeypatch, tmp_path):
        """When __file__ lives inside a real site-packages dir, use XDG_DATA_HOME."""
        monkeypatch.delenv("IDE_OTEL_HOOK_HOME", raising=False)
        # Create a fake site-packages directory and put the module "inside" it.
        sp_dir = tmp_path / "lib" / "python3.x" / "site-packages"
        sp_dir.mkdir(parents=True)
        fake_file = str(sp_dir / "otel_hook.py")
        monkeypatch.setattr(otel_hook, "__file__", fake_file)
        import sysconfig as _sysconfig
        monkeypatch.setattr(_sysconfig, "get_path", lambda name, *a, **kw: str(sp_dir) if name in ("purelib", "platlib") else None)
        xdg = str(tmp_path / "xdg-data")
        monkeypatch.setenv("XDG_DATA_HOME", xdg)
        result = otel_hook._resolve_hook_home()
        assert result == os.path.join(xdg, "opentelemetry-hooks")

    def test_site_packages_defaults_to_dotlocal_share(self, monkeypatch, tmp_path):
        """When XDG_DATA_HOME is unset and running from site-packages, use ~/.local/share."""
        monkeypatch.delenv("IDE_OTEL_HOOK_HOME", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        sp_dir = tmp_path / "lib" / "site-packages"
        sp_dir.mkdir(parents=True)
        fake_file = str(sp_dir / "otel_hook.py")
        monkeypatch.setattr(otel_hook, "__file__", fake_file)
        import sysconfig as _sysconfig
        monkeypatch.setattr(_sysconfig, "get_path", lambda name, *a, **kw: str(sp_dir) if name in ("purelib", "platlib") else None)
        expected = os.path.join(os.path.expanduser("~"), ".local", "share", "opentelemetry-hooks")
        result = otel_hook._resolve_hook_home()
        assert result == expected

    def test_non_installed_returns_script_directory(self, monkeypatch, tmp_path):
        """When not in site-packages (source checkout), return __file__'s directory."""
        monkeypatch.delenv("IDE_OTEL_HOOK_HOME", raising=False)
        script_dir = tmp_path / "hooks" / "opentelemetry-hook"
        script_dir.mkdir(parents=True)
        fake_file = str(script_dir / "otel_hook.py")
        monkeypatch.setattr(otel_hook, "__file__", fake_file)
        import sysconfig as _sysconfig
        # Report a completely different directory as site-packages.
        other_sp = str(tmp_path / "other" / "site-packages")
        monkeypatch.setattr(_sysconfig, "get_path", lambda name, *a, **kw: other_sp if name in ("purelib", "platlib") else None)
        result = otel_hook._resolve_hook_home()
        assert result == str(script_dir)


class TestFindExampleConfig:
    def test_finds_file_next_to_module(self, monkeypatch, tmp_path):
        """When otel_config.example.json is next to __file__, it is returned."""
        example = tmp_path / "otel_config.example.json"
        example.write_text("{}")
        monkeypatch.setattr(otel_hook, "__file__", str(tmp_path / "otel_hook.py"))
        result = otel_hook._find_example_config()
        assert result == str(example)

    def test_finds_file_in_sys_prefix_share(self, monkeypatch, tmp_path):
        """Falls back to {sys.prefix}/share/opentelemetry-hooks/ when not beside __file__."""
        # Point __file__ to a directory that has NO example config beside it.
        monkeypatch.setattr(otel_hook, "__file__", str(tmp_path / "no_example" / "otel_hook.py"))
        share_dir = tmp_path / "share" / "opentelemetry-hooks"
        share_dir.mkdir(parents=True)
        example = share_dir / "otel_config.example.json"
        example.write_text("{}")
        monkeypatch.setattr(sys, "prefix", str(tmp_path))
        monkeypatch.setattr(sys, "exec_prefix", str(tmp_path))
        result = otel_hook._find_example_config()
        assert result == str(example)

    def test_returns_empty_when_not_found(self, monkeypatch, tmp_path):
        """Returns '' when the example config cannot be located anywhere."""
        monkeypatch.setattr(otel_hook, "__file__", str(tmp_path / "no_example" / "otel_hook.py"))
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "fake_prefix"))
        monkeypatch.setattr(sys, "exec_prefix", str(tmp_path / "fake_exec_prefix"))
        result = otel_hook._find_example_config()
        assert result == ""


class TestLoadConfigWithFindExampleConfig:
    def test_copies_example_and_creates_dirs_when_missing(self, monkeypatch, tmp_path):
        """_load_config copies example config when the target config is absent."""
        example = tmp_path / "otel_config.example.json"
        example.write_text(json.dumps({"OTEL_SERVICE_NAME": "example-svc"}))
        config_path = tmp_path / "subdir" / "otel_config.json"

        monkeypatch.setattr(otel_hook, "_CONFIG_DEFAULT", str(config_path))
        monkeypatch.setattr(otel_hook, "_find_example_config", lambda: str(example))
        monkeypatch.setattr(otel_hook, "_load_mdm_config", lambda: {})
        monkeypatch.delenv("IDE_OTEL_CONFIG", raising=False)

        result = otel_hook._load_config()
        assert result.get("OTEL_SERVICE_NAME") == "example-svc"
        assert config_path.exists()

    def test_missing_example_returns_empty_config(self, monkeypatch, tmp_path):
        """When no example config can be found, _load_config returns {}."""
        config_path = tmp_path / "otel_config.json"

        monkeypatch.setattr(otel_hook, "_CONFIG_DEFAULT", str(config_path))
        monkeypatch.setattr(otel_hook, "_find_example_config", lambda: "")
        monkeypatch.setattr(otel_hook, "_load_mdm_config", lambda: {})
        monkeypatch.delenv("IDE_OTEL_CONFIG", raising=False)

        result = otel_hook._load_config()
        assert result == {}


# ── OS / host detection ──────────────────────────────────────────────────


class TestGetOsInfo:
    def test_returns_all_keys(self):
        otel_hook._OS_INFO = None  # reset cache
        info = otel_hook._get_os_info()
        assert "os.type" in info
        assert "os.name" in info
        assert "os.version" in info
        assert "host.arch" in info

    def test_os_type_is_lowercase(self):
        otel_hook._OS_INFO = None
        info = otel_hook._get_os_info()
        assert info["os.type"] == info["os.type"].lower()

    def test_cached_after_first_call(self):
        otel_hook._OS_INFO = None
        first = otel_hook._get_os_info()
        second = otel_hook._get_os_info()
        assert first is second

    def test_darwin_shows_macos(self):
        otel_hook._OS_INFO = None
        with mock.patch("otel_hook.platform") as mp:
            mp.system.return_value = "Darwin"
            mp.release.return_value = "25.3.0"
            mp.machine.return_value = "arm64"
            info = otel_hook._get_os_info()
            assert info["os.type"] == "darwin"
            assert info["os.name"] == "macOS"
        otel_hook._OS_INFO = None  # reset


# ── Client version detection ─────────────────────────────────────────────


class TestDetectClientVersion:
    def test_from_payload(self):
        assert otel_hook._detect_client_version({"client_version": "1.2.3"}, "claude") == "1.2.3"

    def test_from_ide_version_alias(self):
        assert otel_hook._detect_client_version({"ide_version": "0.9.0"}, "cursor") == "0.9.0"

    def test_from_claude_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_VERSION", "1.0.42")
        assert otel_hook._detect_client_version({}, "claude") == "1.0.42"

    def test_from_cursor_env(self, monkeypatch):
        monkeypatch.setenv("CURSOR_VERSION", "0.45.0")
        assert otel_hook._detect_client_version({}, "cursor") == "0.45.0"

    def test_from_generic_env(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_CLIENT_VERSION", "2.0.0")
        assert otel_hook._detect_client_version({}, "opencode") == "2.0.0"

    def test_payload_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_VERSION", "1.0.42")
        assert otel_hook._detect_client_version({"client_version": "2.0.0"}, "claude") == "2.0.0"

    def test_returns_none_when_unavailable(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_VERSION", raising=False)
        monkeypatch.delenv("IDE_OTEL_CLIENT_VERSION", raising=False)
        assert otel_hook._detect_client_version({}, "claude") is None

    def test_codex_from_env(self, monkeypatch):
        monkeypatch.setenv("CODEX_VERSION", "1.5.0")
        monkeypatch.setattr(otel_hook, "_CODEX_VERSION_DETECTED", False)
        monkeypatch.setattr(otel_hook, "_CODEX_VERSION_CACHE", None)
        assert otel_hook._detect_client_version({}, "codex") == "1.5.0"

    def test_codex_subprocess_cached(self, monkeypatch):
        """codex --version subprocess must only be called once per process."""
        monkeypatch.delenv("CODEX_VERSION", raising=False)
        monkeypatch.delenv("IDE_OTEL_CLIENT_VERSION", raising=False)
        monkeypatch.setattr(otel_hook, "_CODEX_VERSION_DETECTED", False)
        monkeypatch.setattr(otel_hook, "_CODEX_VERSION_CACHE", None)
        call_count = []

        import subprocess as _sp
        real_run = _sp.run

        def fake_run(args, **kwargs):
            if args == ["codex", "--version"]:
                call_count.append(1)
                class R:
                    returncode = 0
                    stdout = "2.0.0\n"
                return R()
            return real_run(args, **kwargs)

        monkeypatch.setattr(_sp, "run", fake_run)
        assert otel_hook._detect_client_version({}, "codex") == "2.0.0"
        assert otel_hook._detect_client_version({}, "codex") == "2.0.0"
        assert len(call_count) == 1, "subprocess must only be invoked once"


class _FakeSpan:
    """Minimal span stub for testing attribute helpers."""
    def __init__(self):
        self.attrs = {}

    def set_attribute(self, key, value):
        self.attrs[key] = value


class TestSetCodexToolAttrs:
    def test_default_emits_digest_not_content(self, monkeypatch):
        monkeypatch.delenv("IDE_OTEL_CAPTURE_TOOL_INPUT_CONTENT", raising=False)
        span = _FakeSpan()
        data = {"tool_input": {"command": "ls", "path": "/tmp"}, "tool_response": {"output": "file.txt"}}
        otel_hook._set_codex_tool_attrs(span, "PreToolUse", data)
        # command is always safe to expose
        assert span.attrs.get("gen_ai.client.command") == "ls"
        # content should NOT be flattened by default
        assert "gen_ai.client.tool.input.command" not in span.attrs
        assert "gen_ai.client.tool.response.output" not in span.attrs
        # length + digest should be present
        assert "gen_ai.client.tool.input.length" in span.attrs
        assert "gen_ai.client.tool.input.sha256" in span.attrs
        assert "gen_ai.client.tool.response.length" in span.attrs
        assert "gen_ai.client.tool.response.sha256" in span.attrs

    def test_opt_in_flattens_content(self, monkeypatch):
        monkeypatch.setenv("IDE_OTEL_CAPTURE_TOOL_INPUT_CONTENT", "1")
        span = _FakeSpan()
        data = {"tool_input": {"command": "ls", "path": "/tmp"}, "tool_response": {"output": "file.txt"}}
        otel_hook._set_codex_tool_attrs(span, "PreToolUse", data)
        assert span.attrs.get("gen_ai.client.tool.input.command") == "ls"
        assert span.attrs.get("gen_ai.client.tool.response.output") == "file.txt"

    def test_permission_request_handled(self, monkeypatch):
        monkeypatch.delenv("IDE_OTEL_CAPTURE_TOOL_INPUT_CONTENT", raising=False)
        span = _FakeSpan()
        data = {"tool_input": {"description": "need access"}}
        otel_hook._set_codex_tool_attrs(span, "PermissionRequest", data)
        assert span.attrs.get("gen_ai.client.approval.description") == "need access"

    def test_permission_request_in_tool_events(self):
        assert "PermissionRequest" in otel_hook._TOOL_EVENTS


# ── New IDE name aliases ─────────────────────────────────────────────────


class TestNewIdeAliases:
    def test_windsurf(self):
        assert otel_hook._normalize_ide_name("windsurf") == "windsurf"
        assert otel_hook._normalize_ide_name("Windsurf IDE") == "windsurf"

    def test_vscode(self):
        assert otel_hook._normalize_ide_name("vscode") == "vscode"
        assert otel_hook._normalize_ide_name("Visual Studio Code") == "vscode"

    def test_zed(self):
        assert otel_hook._normalize_ide_name("zed") == "zed"
        assert otel_hook._normalize_ide_name("Zed Editor") == "zed"

    def test_claude_cli_alias(self):
        assert otel_hook._normalize_ide_name("Claude CLI") == "claude"


# ── Exporter deduplication guards ────────────────────────────────────────────


class TestEnableFileExporterIdempotency:
    """_enable_file_exporter must attach the processor only once per path."""

    def test_first_call_registers_and_subsequent_calls_skip(self, monkeypatch, tmp_path):
        path = str(tmp_path / "spans.jsonl")
        monkeypatch.setattr(otel_hook, "_FILE_EXPORTER_PATHS", set())

        try:
            from opentelemetry.sdk.trace import TracerProvider
        except ImportError:
            pytest.skip("opentelemetry SDK not available")

        mock_provider = mock.MagicMock(spec=TracerProvider)
        mock_trace = mock.MagicMock()
        mock_trace.get_tracer_provider.return_value = mock_provider
        monkeypatch.setattr(otel_hook, "trace", mock_trace)

        otel_hook._enable_file_exporter(path)
        otel_hook._enable_file_exporter(path)
        otel_hook._enable_file_exporter(path)

        assert mock_provider.add_span_processor.call_count == 1
        assert path in otel_hook._FILE_EXPORTER_PATHS

    def test_different_paths_each_get_one_exporter(self, monkeypatch, tmp_path):
        path_a = str(tmp_path / "a.jsonl")
        path_b = str(tmp_path / "b.jsonl")
        monkeypatch.setattr(otel_hook, "_FILE_EXPORTER_PATHS", set())

        try:
            from opentelemetry.sdk.trace import TracerProvider
        except ImportError:
            pytest.skip("opentelemetry SDK not available")

        mock_provider = mock.MagicMock(spec=TracerProvider)
        mock_trace = mock.MagicMock()
        mock_trace.get_tracer_provider.return_value = mock_provider
        monkeypatch.setattr(otel_hook, "trace", mock_trace)

        otel_hook._enable_file_exporter(path_a)
        otel_hook._enable_file_exporter(path_a)
        otel_hook._enable_file_exporter(path_b)
        otel_hook._enable_file_exporter(path_b)

        assert mock_provider.add_span_processor.call_count == 2
        assert path_a in otel_hook._FILE_EXPORTER_PATHS
        assert path_b in otel_hook._FILE_EXPORTER_PATHS


class TestEnableConsoleExporterIdempotency:
    """_enable_console_exporter must attach the processor only once."""

    def test_first_call_registers_and_subsequent_calls_skip(self, monkeypatch):
        monkeypatch.setattr(otel_hook, "_CONSOLE_EXPORTER_REGISTERED", False)

        try:
            from opentelemetry.sdk.trace import TracerProvider
        except ImportError:
            pytest.skip("opentelemetry SDK not available")

        mock_provider = mock.MagicMock(spec=TracerProvider)
        mock_trace = mock.MagicMock()
        mock_trace.get_tracer_provider.return_value = mock_provider
        monkeypatch.setattr(otel_hook, "trace", mock_trace)

        otel_hook._enable_console_exporter()
        otel_hook._enable_console_exporter()
        otel_hook._enable_console_exporter()

        assert mock_provider.add_span_processor.call_count == 1
        assert otel_hook._CONSOLE_EXPORTER_REGISTERED is True


class TestModelAttributionFallback:
    """Tests for the model attribution fallback chain in _apply_genai_semconv.

    Priority: data["model"] → session_ctx["last_known_model"] → batch_model → env vars.
    """

    @staticmethod
    def _attrs(span):
        return {
            args[0]: args[1]
            for args, _kwargs in (call for call in span.set_attribute.call_args_list)
        }

    def test_model_from_data_field(self):
        """Model is taken directly from the event data when present."""
        span = mock.MagicMock()
        otel_hook._apply_genai_semconv(span, "SubagentStop", {"model": "claude-3-7-sonnet"}, "claude")
        attrs = self._attrs(span)
        assert attrs.get("gen_ai.request.model") == "claude-3-7-sonnet"

    def test_model_falls_back_to_last_known_model(self):
        """When event data has no model, fall back to session_ctx['last_known_model']."""
        span = mock.MagicMock()
        session_ctx = {"last_known_model": "claude-3-5-haiku"}
        otel_hook._apply_genai_semconv(span, "SubagentStop", {}, "claude", session_ctx=session_ctx)
        attrs = self._attrs(span)
        assert attrs.get("gen_ai.request.model") == "claude-3-5-haiku"

    def test_model_falls_back_to_batch_model(self):
        """When neither data nor session_ctx has a model, fall back to batch_model."""
        span = mock.MagicMock()
        otel_hook._apply_genai_semconv(span, "SubagentStop", {}, "claude",
                                       session_ctx={}, batch_model="claude-opus-4")
        attrs = self._attrs(span)
        assert attrs.get("gen_ai.request.model") == "claude-opus-4"

    def test_model_falls_back_to_claude_env(self, monkeypatch):
        """When all other sources are absent, fall back to CLAUDE_MODEL env var."""
        monkeypatch.setenv("CLAUDE_MODEL", "claude-3-haiku-20240307")
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        span = mock.MagicMock()
        otel_hook._apply_genai_semconv(span, "SubagentStop", {}, "claude", session_ctx={})
        attrs = self._attrs(span)
        assert attrs.get("gen_ai.request.model") == "claude-3-haiku-20240307"

    def test_model_falls_back_to_anthropic_env(self, monkeypatch):
        """When CLAUDE_MODEL is absent, fall back to ANTHROPIC_MODEL env var."""
        monkeypatch.delenv("CLAUDE_MODEL", raising=False)
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-3-sonnet-20240229")
        span = mock.MagicMock()
        otel_hook._apply_genai_semconv(span, "SubagentStop", {}, "claude", session_ctx={})
        attrs = self._attrs(span)
        assert attrs.get("gen_ai.request.model") == "claude-3-sonnet-20240229"

    def test_data_model_overrides_session_ctx(self):
        """Model in data takes precedence over session_ctx['last_known_model']."""
        span = mock.MagicMock()
        session_ctx = {"last_known_model": "old-model"}
        otel_hook._apply_genai_semconv(span, "SubagentStop", {"model": "new-model"}, "claude",
                                       session_ctx=session_ctx)
        attrs = self._attrs(span)
        assert attrs.get("gen_ai.request.model") == "new-model"

    def test_session_ctx_overrides_batch_model(self):
        """session_ctx['last_known_model'] takes precedence over batch_model."""
        span = mock.MagicMock()
        session_ctx = {"last_known_model": "session-model"}
        otel_hook._apply_genai_semconv(span, "SubagentStop", {}, "claude",
                                       session_ctx=session_ctx, batch_model="batch-model")
        attrs = self._attrs(span)
        assert attrs.get("gen_ai.request.model") == "session-model"

    def test_no_model_attribute_when_all_sources_absent(self, monkeypatch):
        """When all fallback sources are absent, gen_ai.request.model must not be set."""
        monkeypatch.delenv("CLAUDE_MODEL", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        span = mock.MagicMock()
        otel_hook._apply_genai_semconv(span, "SubagentStop", {}, "claude", session_ctx={})
        attrs = self._attrs(span)
        assert "gen_ai.request.model" not in attrs

    def test_claude_event_without_model_uses_last_known(self):
        """Simulates a Claude payload (no model in event) resolved via last_known_model."""
        span = mock.MagicMock()
        # Simulate a SessionEnd payload that has no model field
        session_ctx = {
            "session_id": "sess-abc",
            "last_known_model": "claude-3-7-sonnet-20250219",
        }
        otel_hook._apply_genai_semconv(
            span,
            "SessionEnd",
            {"session_id": "sess-abc", "duration_ms": 5000},
             "claude",
            session_ctx=session_ctx,
        )
        attrs = self._attrs(span)
        assert attrs.get("gen_ai.request.model") == "claude-3-7-sonnet-20250219"


# ---------------------------------------------------------------------------
# setup_opencode
# ---------------------------------------------------------------------------

class TestSetupOpencode:
    """Tests for setup_opencode() — installs the TypeScript plugin file."""

    def _plugin_source(self):
        """Return the path to the real plugin/opencode.ts source file."""
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugin", otel_hook._OPENCODE_PLUGIN_SOURCE_FILENAME,
        )

    def test_global_install_creates_plugin_file(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".config" / "opencode" / "plugins"
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(otel_hook, "_find_opencode_plugin_source", self._plugin_source)
        otel_hook.setup_opencode(global_=True)
        dest = config_dir / "otel-hook.ts"
        assert dest.is_file()
        assert dest.read_text() == Path(self._plugin_source()).read_text()

    def test_project_install_creates_plugin_in_opencode_dir(self, tmp_path, monkeypatch):
        # Set up a fake git repo root
        (tmp_path / ".git").mkdir()
        plugins_dir = tmp_path / ".opencode" / "plugins"
        monkeypatch.setattr(otel_hook, "_find_opencode_plugin_source", self._plugin_source)
        otel_hook.setup_opencode(global_=False, cwd=str(tmp_path))
        dest = plugins_dir / "otel-hook.ts"
        assert dest.is_file()

    def test_idempotent_when_content_matches(self, tmp_path, monkeypatch, capsys):
        config_dir = tmp_path / ".config" / "opencode" / "plugins"
        config_dir.mkdir(parents=True)
        src = self._plugin_source()
        dest = config_dir / "otel-hook.ts"
        import shutil as _shutil
        _shutil.copy2(src, dest)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(otel_hook, "_find_opencode_plugin_source", lambda: src)
        otel_hook.setup_opencode(global_=True)
        captured = capsys.readouterr()
        assert "Already up to date" in captured.out

    def test_updates_when_content_differs(self, tmp_path, monkeypatch, capsys):
        config_dir = tmp_path / ".config" / "opencode" / "plugins"
        config_dir.mkdir(parents=True)
        dest = config_dir / "otel-hook.ts"
        dest.write_text("// old content")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(otel_hook, "_find_opencode_plugin_source", self._plugin_source)
        otel_hook.setup_opencode(global_=True)
        assert dest.read_text() == Path(self._plugin_source()).read_text()
        captured = capsys.readouterr()
        assert "Updated" in captured.out

    def test_raises_when_source_not_found(self, monkeypatch):
        monkeypatch.setattr(otel_hook, "_find_opencode_plugin_source", lambda: None)
        import click
        with pytest.raises(click.ClickException, match="plugin/opencode.ts"):
            otel_hook.setup_opencode(global_=True)

    def test_detect_available_agents_includes_opencode_when_config_exists(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(otel_hook.shutil, "which", lambda cmd: None)
        found = otel_hook._detect_available_agents()
        assert "opencode" in found

    def test_detect_available_agents_includes_opencode_when_binary_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(otel_hook.shutil, "which", lambda cmd: "/usr/local/bin/opencode" if cmd == "opencode" else None)
        found = otel_hook._detect_available_agents()
        assert "opencode" in found

    def test_setup_agent_dispatches_to_opencode(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(otel_hook, "setup_opencode", lambda global_, cwd: called.append((global_, cwd)))
        otel_hook.setup_agent("opencode", global_=True, cwd="/tmp")
        assert called == [(True, "/tmp")]


# ---------------------------------------------------------------------------
# setup_codex
# ---------------------------------------------------------------------------

class TestSetupCodex:
    def test_project_install_creates_hooks_and_config(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        otel_hook.setup_codex(global_=False, cwd=str(tmp_path))
        hooks_path = tmp_path / ".codex" / "hooks.json"
        config_path = tmp_path / ".codex" / "config.toml"
        assert hooks_path.is_file()
        assert config_path.is_file()
        doc = json.loads(hooks_path.read_text())
        assert set(otel_hook._CODEX_EVENTS).issubset(doc["hooks"])
        text = config_path.read_text()
        assert "hooks = true" in text
        assert "codex_hooks" not in text

    def test_global_install_uses_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        otel_hook.setup_codex(global_=True)
        assert (tmp_path / ".codex" / "hooks.json").is_file()

    def test_detect_available_agents_includes_codex_when_config_exists(self, tmp_path, monkeypatch):
        (tmp_path / ".codex").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(otel_hook.shutil, "which", lambda cmd: None)
        found = otel_hook._detect_available_agents()
        assert "codex" in found

    def test_setup_agent_dispatches_to_codex(self, monkeypatch):
        called = []
        monkeypatch.setattr(otel_hook, "setup_codex", lambda global_, cwd: called.append((global_, cwd)))
        otel_hook.setup_agent("codex", global_=False, cwd="/tmp/project")
        assert called == [(False, "/tmp/project")]
