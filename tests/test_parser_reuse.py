"""Parsing shortcuts preserve original evidence and the capture boundary."""
from copy import deepcopy
import hashlib
import json
import os
from unittest.mock import Mock, patch

import pytest

import otel_hook as hook
from otel_hooks.capture import CapturePolicy


@pytest.fixture
def prepare_callback(monkeypatch):
    """Exercise main's preparation path without state writes or SDK setup."""
    adapter_for = hook._event_adapter_for
    with patch.dict(os.environ, {"IDE_OTEL_ENABLE_LOGS": "true"}, clear=True):
        monkeypatch.setattr(hook, "_load_config", lambda: {})
        monkeypatch.setattr(hook, "_apply_config_env", lambda config: None)
        monkeypatch.setattr(hook, "_configure_logging", lambda: None)
        monkeypatch.setattr(hook, "_cleanup_state", lambda: None)
        monkeypatch.setattr(hook, "_load_session_context", lambda key: {})
        monkeypatch.setattr(hook, "_maybe_bind_session_to_upstream_context", lambda key, ctx, data: ctx)
        monkeypatch.setattr(hook, "_maybe_enrich_session_context", lambda key, ctx, data: ctx)
        monkeypatch.setattr(hook, "_batch_enabled", lambda: False)
        monkeypatch.setattr(hook, "_init_tracing", lambda *args, **kwargs: False)
        # _apply_config_env is bypassed, so keep policy and deadline test-local.
        monkeypatch.setattr(hook, "_CAPTURE_POLICY", CapturePolicy())
        monkeypatch.setattr(hook, "_EXPORT_DEADLINE", None)

        def run(payload, capture, provider="claude"):
            original = deepcopy(payload)
            monkeypatch.setattr(hook, "_CAPTURE_POLICY", capture)
            adapter = adapter_for(provider)
            parse = Mock(wraps=adapter.parse)
            monkeypatch.setattr(adapter, "parse", parse)
            monkeypatch.setattr(hook, "_event_adapter_for", lambda ide: adapter)
            monkeypatch.setattr(hook, "_detect_ide", lambda data: provider)
            monkeypatch.setattr(hook, "_load_input", lambda: payload)
            response = Mock()
            monkeypatch.setattr(hook, "_emit_stdout_response", response)
            assert hook.main() == 0
            prepared = response.call_args.args[2]
            assert payload == original
            assert prepared["_hook_log_body"] == capture.filter(original)
            return prepared, parse.call_count

        yield run


@pytest.mark.parametrize("mode", ["metadata_only", "selected_content", "full"])
def test_unchanged_metadata_reuses_parse_in_every_mode(prepare_callback, mode):
    data, calls = prepare_callback(
        {"hook_event_name": "PreToolUse", "sessionId": "s", "toolName": "Bash", "eventId": "callback"},
        CapturePolicy(mode=mode),
    )
    assert calls == 1
    assert data["_hook_event_id"] == "callback"
    assert data["_hook_event_id_source"] == "provider"
    assert data["session_id"] == "s"


@pytest.mark.parametrize("provider,event,fields", [
    ("cursor", "beforeSubmitPrompt", {"prompt": "hello"}),
    ("claude", "PostToolBatch", {"tool_calls": [{"tool_name": "Bash", "tool_input": {"command": "echo hello"}}]}),
    ("codex", "UserPromptSubmit", {"prompt": "hello"}),
    ("copilot", "postToolUse", {"toolArgs": '{"command":"echo hello"}', "toolResult": {"textResultForLlm": "hello"}}),
    ("gemini", "BeforeAgent", {"prompt": "hello", "llm_request": {"model": "example", "messages": ["hello"]}}),
    ("windsurf", "pre_user_prompt", {"tool_info": {"user_prompt": "hello", "cwd": "/workspace"}}),
    ("antigravity", "AfterAgentThought", {"text": "hello"}),
    ("opencode", "PreToolUse", {"tool_id": "call", "tool_input": {"command": "echo hello"}}),
])
def test_full_capture_reuses_parse_without_mutating_provider_payload(prepare_callback, provider, event, fields):
    payload = {"hook_event_name": event, "session_id": "s", **fields}
    data, calls = prepare_callback(payload, CapturePolicy(mode="full"), provider)
    assert calls == 1
    assert data["_hook_provider_adapter"] == provider


def test_filtered_conversation_keeps_original_hash_and_callback_identity(prepare_callback):
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "secret é",
               "future_field": {"revision": 1}}
    identities = set()
    for capture, expected_calls, has_text in [
        (CapturePolicy(mode="full"), 1, True),
        (CapturePolicy(mode="metadata_only"), 2, False),
        (CapturePolicy(mode="selected_content"), 2, False),
        (CapturePolicy(mode="selected_content", include=("prompt",)), 2, True),
        (CapturePolicy(mode="full", exclude=("prompt",)), 2, False),
    ]:
        data, calls = prepare_callback(payload, capture)
        identities.add(data["_hook_event_id"])
        assert calls == expected_calls
        record, = data["_conversation_records"]
        assert record["length"] == len(payload["prompt"])
        assert record["sha256"] == hashlib.sha256(payload["prompt"].encode()).hexdigest()
        assert ("text" in record) is has_text
        if not has_text:
            assert "secret" not in json.dumps(data)
    assert len(identities) == 1
    changed, _ = prepare_callback({**payload, "future_field": {"revision": 2}}, CapturePolicy())
    assert changed["_hook_event_id"] not in identities


def test_private_state_injection_requires_separate_observation(prepare_callback):
    payload = {"hook_event_name": "PreToolUse", "session_id": "s", "event_id": "provider-id",
               "_hook_event_id": "injected", "_hook_attributes": {"injected": True},
               "_conversation_records": [{"kind": "prompt", "text": "injected"}]}
    data, calls = prepare_callback(payload, CapturePolicy(mode="full", span_include=()))
    assert calls == 2
    assert data["_hook_event_id"] == "provider-id"
    assert data["_hook_attributes"] == {}
    assert data["_conversation_records"] == []


def test_alias_expansion_cannot_hide_boolean_integer_difference(prepare_callback):
    payload = {"hook_event_name": "PreToolUse", "session_id": "s",
               "traceState": 1, "tracestate": True}
    capture = CapturePolicy(mode="full", exclude=("tracestate",))
    # Ordinary dict equality after normalization would incorrectly permit reuse.
    normalized = hook._normalize_input_data(capture.filter(payload))
    assert normalized == hook._normalize_input_data(payload)
    assert type(normalized["tracestate"]) is int
    data, calls = prepare_callback(payload, capture)
    assert calls == 2
    assert type(data["tracestate"]) is int
