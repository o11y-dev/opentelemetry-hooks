"""Capture contract: preserve provider evidence without bypassing privacy policy."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import otel_hook as hook
from otel_hooks.capture import CapturePolicy
from otel_hooks.config import environment_settings


def policy(mode="metadata_only", **kwargs):
    return CapturePolicy.from_config({"capture": {"mode": mode, **kwargs}})


@pytest.fixture
def isolated_runtime(monkeypatch, tmp_path):
    with patch.dict(os.environ, {}, clear=True):
        for name in ("_SESSION_DIR", "_BATCH_DIR", "_LOCK_DIR", "_LOCAL_SPANS_DIR", "_STATE_DIR"):
            monkeypatch.setattr(hook, name, str(tmp_path / name))
        monkeypatch.setattr(hook, "_CAPTURE_POLICY", CapturePolicy())
        monkeypatch.setattr(hook, "_detect_ide_from_process_tree", lambda: None)
        monkeypatch.setattr(hook, "_configure_logging", lambda: None)
        yield tmp_path


def test_structured_keys_win_without_exporting_arbitrary_json():
    result = environment_settings({
        "IDE_OTEL_ENABLE_LOGS": True, "logs": {"enabled": False},
        "batch": {"on_stop": True}, "OTEL_SERVICE_NAME": "example", "PATH": "bad",
    })
    assert result == {"IDE_OTEL_ENABLE_LOGS": False, "IDE_OTEL_BATCH_ON_STOP": True, "OTEL_SERVICE_NAME": "example"}


def test_enable_logs_alias_uses_default_metadata_policy(isolated_runtime):
    hook._apply_config_env({"IDE_OTEL_ENABLE_LOGS": True})
    assert hook._CAPTURE_POLICY.mode == "metadata_only"
    assert hook._CAPTURE_POLICY.filter({"prompt": "SECRET", "session_id": "s"}) == {"session_id": "s"}


def test_managed_privacy_beats_environment_and_local_settings(isolated_runtime):
    os.environ["IDE_OTEL_ENABLE_LOGS"] = "true"
    os.environ["IDE_OTEL_CAPTURE_TEXT"] = "true"
    hook._apply_config_env({
        "capture": {"mode": "full"}, "logs": {"enabled": True},
        "_managed": {"capture": {"mode": "metadata_only"}, "logs": {"enabled": False}},
    })
    assert hook._CAPTURE_POLICY.mode == "metadata_only"
    assert os.environ["IDE_OTEL_ENABLE_LOGS"] == "False"
    assert hook._CAPTURE_POLICY.filter({"prompt": "SECRET"}) == {}


@pytest.mark.parametrize("mode", ["metadata_only", "selected_content"])
def test_unknown_content_cannot_escape_through_nested_metadata_or_numeric_fields(mode):
    data = {
        "sessionId": "s", "duration_ms": 123,
        "metadata": {"prompt": "SECRET", "requestModel": "gpt-example"},
        "toolInput": {"command": "SECRET", "pin": 12345},
        "cwd": "/SECRET", "error": {"message": "SECRET"},
        "duration": {"payload": "SECRET"}, "input_tokens": "SECRET",
    }
    assert policy(mode).filter(data) == {
        "sessionId": "s", "duration_ms": 123, "metadata": {"requestModel": "gpt-example"},
    }


def test_selected_content_and_exclusions_apply_to_original_keys():
    data = {"toolInput": {"Command": "echo hello", "apiKey": "SECRET"}, "prompt": "SECRET"}
    captured = policy("selected_content", include=["tool_input"], exclude=["tool_input.api_key"]).filter(data)
    assert captured == {"toolInput": {"Command": "echo hello"}}
    assert policy("full").attributes(captured) == {"hook.tool_input.command": "echo hello"}


def test_full_logs_are_lossless_but_spans_are_bounded_typed_and_never_json_blobs():
    data = {"NewField": {"nested": [1, None, {"responseText": "abcdef"}]}, "flags": [True, False]}
    capture = policy("full", max_string_length=3)
    assert capture.filter(data) == data
    attrs = capture.attributes(data)
    assert attrs["hook.new_field.nested.0"] == 1
    assert attrs["hook.new_field.nested.2.response_text"] == "abc"
    assert attrs["hook.flags"] == [True, False]
    assert not any(isinstance(value, dict) for value in attrs.values())
    assert not any(isinstance(value, str) and value.startswith(("{", "[")) for value in attrs.values())


def test_collisions_are_reported_not_silently_overwritten():
    attrs = policy("full").attributes({"ToolID": 1, "tool_id": 2, "tool-id": 3})
    assert "hook.tool_id" not in attrs
    assert attrs["hook.capture.collisions"] == 1


def test_limits_and_mapping():
    attrs = policy("full", max_attributes=2, mappings={"input_tokens": "observed.input_tokens"}).attributes(
        {"inputTokens": 5, "model": "example", "z": "too much"},
    )
    assert attrs == {"observed.input_tokens": 5, "hook.model": "example", "hook.capture.dropped_fields": 1}


@pytest.mark.parametrize("settings", [{"mode": "typo"}, {"mode": "full", "max_attributes": 0},
                                     {"mode": "full", "include": "*"}, {"mode": "full", "mappings": {"x": "UPPER"}}])
def test_invalid_capture_settings_fail_closed(settings):
    with pytest.raises(ValueError):
        CapturePolicy.from_config({"capture": settings})


def test_policy_change_isolates_old_queue_but_preserves_reflect_directory(isolated_runtime):
    original_spans = hook._LOCAL_SPANS_DIR
    hook._apply_config_env({"capture": {"mode": "full"}})
    old_batches = hook._BATCH_DIR
    hook._apply_config_env({"capture": {"mode": "metadata_only"}})
    assert hook._BATCH_DIR != old_batches
    assert hook._LOCAL_SPANS_DIR == original_spans


def test_primary_log_body_and_trace_correlation_without_legacy_fanout(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(hook, "_LOGS_INITIALIZED", True)
    monkeypatch.setattr(hook, "_get_otel_logger", lambda name: logger)
    body = {"toolInput": {"command": "echo hello"}, "unknown": [None, True, 42]}
    hook._emit_event_log("PreToolUse", {"_hook_attributes": {}, "_hook_log_body": body, "session_id": "s"})
    assert logger.info.call_args.args == (body,)
    assert logger.info.call_args.kwargs["extra"]["gen_ai.client.session_id"] == "s"


@pytest.mark.parametrize("provider", ["cursor", "claude", "codex", "copilot", "gemini", "windsurf", "antigravity", "opencode"])
def test_metadata_filter_precedes_buffer_writes(isolated_runtime, monkeypatch, provider):
    monkeypatch.setattr(hook, "_load_config", lambda: {
        "capture": {"mode": "metadata_only"}, "batch": {"on_stop": True}, "logs": {"enabled": True},
    })
    monkeypatch.setattr(hook, "_detect_ide", lambda data: provider)
    monkeypatch.setattr(hook, "_load_input", lambda: {
        "hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Shell", "tool_use_id": "call",
        "tool_input": {"command": "SECRET", "cwd": "/SECRET"}, "metadata": {"other": "SECRET"},
        "_hook_log_body": {"injected": "SECRET"}, "_hook_attributes": {"injected": "SECRET"},
    })
    assert hook.main() == 0
    stored = "\n".join(path.read_text() for path in isolated_runtime.rglob("*.json*"))
    assert stored
    assert "SECRET" not in stored
    assert '"tool_name": "Shell"' in stored


def test_cursor_and_opencode_preserve_real_invocation_ids():
    cursor = hook.CursorEventAdapter().parse("subagentStart", {"subagent_id": "child"})
    assert cursor.relationship.agent_id == "child"
    opencode = hook.OpenCodeEventAdapter().parse("PreToolUse", {"tool_id": "call"})
    assert opencode.lifecycle_data["tool_use_id"] == "call"


def test_sdk_exports_raw_json_body_with_active_span_context(monkeypatch):
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry import trace
    import logging

    exporter = InMemoryLogRecordExporter()
    logs = LoggerProvider()
    logs.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = LoggingHandler(logger_provider=logs)
    logger = logging.Logger("capture-test", level=logging.INFO)
    logger.addHandler(handler)
    monkeypatch.setattr(hook, "_LOGS_INITIALIZED", True)
    monkeypatch.setattr(hook, "_get_otel_logger", lambda name: logger)
    provider = TracerProvider()
    body = {"camelCase": {"numbers": [1, 2], "null": None, "boolean": True}}
    with provider.get_tracer("test").start_as_current_span("hook") as span:
        hook._emit_event_log("Stop", {"_hook_attributes": {}, "_hook_log_body": body})
        span_id = trace.get_current_span().get_span_context().span_id
    record = exporter.get_finished_logs()[0].log_record
    assert record.body == body
    assert record.span_id == span_id == span.get_span_context().span_id
    logs.shutdown()
    provider.shutdown()


_SDK_REPLAY_RUNNER = '''
import json, os
import otel_hook
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk._logs.export import LogExportResult
def traces(self, spans):
    return SpanExportResult.SUCCESS
def logs(self, records):
    with open(os.environ['FIXTURE_LOG_FILE'], 'a') as f:
        for item in records:
            record = item.log_record
            f.write(json.dumps({'body': record.body, 'attributes': dict(record.attributes),
                                'trace_id': record.trace_id, 'span_id': record.span_id}) + '\\n')
    return LogExportResult.SUCCESS
OTLPSpanExporter.export = traces
OTLPLogExporter.export = logs
otel_hook.cli()
'''


@pytest.mark.parametrize("mode,logs_enabled,selection", [
    ("full", True, {}), ("full", False, {}), ("metadata_only", True, {}),
    ("full", True, {"span_include": ["tool_input.command"]}),
    ("full", False, {"span_include": []}),
])
def test_batched_lifecycle_replay_with_real_sdk(tmp_path, mode, logs_enabled, selection):
    """Separate hook processes, actual SDK export, no collector/network required."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "capture": {"mode": mode, **selection}, "logs": {"enabled": logs_enabled},
        "batch": {"on_stop": True, "disable_sdk_batch": True}, "local_spans": True,
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example.test:4318",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    }))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "IDE_OTEL_"))}
    env.update(IDE_OTEL_HOOK_HOME=str(tmp_path), IDE_OTEL_CONFIG=str(config),
               FIXTURE_LOG_FILE=str(tmp_path / "exported_logs.jsonl"))
    events = [
        {"hook_event_name": "SessionStart", "session_id": "s", "extraStart": "start"},
        {"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Bash", "tool_use_id": "call",
         "tool_input": {"command": "SECRET", "nested": [None, True, {"CamelCase": 42}]}},
        {"hook_event_name": "PostToolUse", "session_id": "s", "tool_name": "Bash", "tool_use_id": "call",
         "tool_response": {"output": "SECRET"}},
        {"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": "SECRET"},
        {"hook_event_name": "SessionEnd", "session_id": "s", "extraEnd": "end"},
    ]
    for event in events:
        result = subprocess.run(
            [sys.executable, "-c", _SDK_REPLAY_RUNNER, "--claude"], input=json.dumps(event), env=env,
            capture_output=True, text=True, timeout=15, cwd=Path(hook.__file__).parent,
        )
        assert result.returncode == 0, result.stderr
        assert "SECRET" not in result.stdout
    spans = [json.loads(line) for path in (tmp_path / ".state" / "local_spans").glob("*.jsonl")
             for line in path.read_text().splitlines()]
    assert spans
    serialized = json.dumps(spans)
    if mode == "metadata_only":
        assert "SECRET" not in serialized
    elif selection:
        assert "hook.tool_input.nested.2.camel_case" not in serialized
        assert "gen_ai.client.command" in serialized
        assert "gen_ai.client.tool_use_id" in serialized
        assert ("hook.tool_input.command" in serialized) == bool(selection["span_include"])
    else:
        assert "hook.tool_input.nested.2.camel_case" in serialized
    assert "_hook_log_body" not in serialized
    assert "gen_ai.client.response.length" in serialized
    assert "gen_ai.client.response.sha256" in serialized
    log_file = tmp_path / "exported_logs.jsonl"
    if not logs_enabled:
        assert not log_file.exists()
        return
    logs = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert len(logs) == len(events)
    bodies = {record["body"]["hook_event_name"]: record["body"] for record in logs}
    expected = CapturePolicy(mode=mode)
    for event in events:
        assert bodies[event["hook_event_name"]] == expected.filter(event)
    assert all(record["trace_id"] and record["span_id"] for record in logs)


def test_invalid_policy_still_returns_passive_hook_response(isolated_runtime, monkeypatch, capsys):
    monkeypatch.setattr(hook, "_load_config", lambda: {"capture": {"mode": "typo"}})
    monkeypatch.setattr(hook, "_load_input", lambda: {"hook_event_name": "beforeReadFile", "file_path": "SECRET"})
    monkeypatch.setattr(hook, "_detect_ide", lambda data: "cursor")
    assert hook.main() == 0
    assert json.loads(capsys.readouterr().out)["permission"] == "allow"
    assert not list(isolated_runtime.rglob("*.json*"))


def test_managed_selected_content_does_not_inherit_local_allowlist(isolated_runtime):
    hook._apply_config_env({
        "capture": {"mode": "full", "include": ["prompt"]},
        "_managed": {"capture": {"mode": "selected_content"}},
    })
    assert hook._CAPTURE_POLICY.filter({"prompt": "SECRET"}) == {}


def test_managed_legacy_value_beats_local_structured_value(isolated_runtime):
    hook._apply_config_env({"batch": {"on_stop": True}, "_managed": {"IDE_OTEL_BATCH_ON_STOP": False}})
    assert not hook._batch_enabled()


def test_mapping_can_override_canonical_semantics(isolated_runtime):
    hook._apply_config_env({"capture": {"mode": "full", "mappings": {"uncached_input": "gen_ai.usage.input_tokens"}}})
    span = Mock()
    data = {"input_tokens": 100, "uncached_input": 25}
    data["_hook_attributes"] = hook._CAPTURE_POLICY.attributes(data)
    hook._populate_span(span, "Stop", data, "cursor")
    attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert attrs["gen_ai.usage.input_tokens"] == 25


def test_correlated_cursor_callbacks_keep_original_evidence_in_batch(isolated_runtime):
    hook._create_session_context("s", {"session_id": "s"}, "cursor")
    hook._buffer_session_event("s", "PreToolUse", {
        "session_id": "s", "tool_name": "mcp__server__search", "tool_use_id": "call",
        "_hook_attributes": {},
    }, "cursor")
    original = {"mcp_server_name": "server", "tool_name": "search", "extra": "evidence"}
    decision = hook._buffer_session_event("s", "AfterMCPExecution", {
        **original, "session_id": "s", "_hook_attributes": {"hook.extra": "evidence"}, "_hook_log_body": original,
    }, "cursor")
    assert decision.correlated
    entries = hook._load_batch_events(decision.generation_key)
    event = next(entry for entry in entries if entry["event"] == "AfterMCPExecution")
    assert event["data"]["_hook_log_body"] == original
    assert event["data"]["_mcp_correlated_evidence"] is True


def test_stop_without_prompt_is_retained_in_explicit_capture_mode(isolated_runtime):
    hook._create_session_context("s", {"session_id": "s"}, "cursor")
    decision = hook._buffer_generation_end("s", "Stop", {
        "session_id": "s", "_hook_attributes": {"hook.model": "example"},
    }, "cursor")
    assert decision.generation_key
    assert hook._load_batch_events(decision.generation_key)[0]["event"] == "Stop"


def test_large_payload_cannot_evict_canonical_correlation(isolated_runtime):
    from opentelemetry.sdk.trace import TracerProvider

    hook._apply_config_env({"capture": {"mode": "full"}})
    provider = TracerProvider()
    data = {
        "session_id": "s", "generation_id": "g", "turn_id": "t", "_hook_event_id": "event",
        "tool_use_id": "call", "model": "example", "input_tokens": 123,
        "metadata": {f"field_{i}": i for i in range(250)},
        "tool_input": {f"field_{i}": i for i in range(250)},
    }
    data["_hook_attributes"] = hook._CAPTURE_POLICY.attributes(data)
    with provider.get_tracer("test").start_as_current_span("hook") as span:
        hook._populate_span(span, "PreToolUse", data, "codex")
    assert span.attributes["gen_ai.client.hook.event_id"] == "event"
    assert span.attributes["gen_ai.client.session_id"] == "s"
    assert span.attributes["gen_ai.client.generation_id"] == "g"
    assert span.attributes["gen_ai.client.turn_id"] == "t"
    assert span.attributes["gen_ai.client.tool_use_id"] == "call"
    assert span.attributes["gen_ai.request.model"] == "example"
    assert span.attributes["gen_ai.usage.input_tokens"] == 123
    provider.shutdown()


@pytest.mark.parametrize("provider,event,fields,expected", [
    ("cursor", "AfterShellExecution", {"command": "pwd", "stdout": "/workspace", "exit_code": 0}, {
        "gen_ai.client.command": "pwd", "hook.stdout": "/workspace", "gen_ai.client.exit_code": 0,
    }),
    ("cursor", "AfterMCPExecution", {"mcp_server_name": "search", "tool_name": "lookup", "result_json": {"hits": 3}}, {
        "gen_ai.client.mcp_server": "search", "gen_ai.client.mcp_tool": "lookup", "hook.result_json.hits": 3,
    }),
    ("claude", "Stop", {"last_assistant_message": "Done"}, {
        "gen_ai.client.response.text": "Done", "hook.last_assistant_message": "Done",
    }),
])
@pytest.mark.parametrize("logs_enabled", [False, True])
def test_shell_mcp_and_conversation_evidence_stays_on_spans(
    isolated_runtime, monkeypatch, provider, event, fields, expected, logs_enabled,
):
    hook._apply_config_env({"capture": {"mode": "full"}, "logs": {"enabled": logs_enabled}})
    payload = {"hook_event_name": event, "session_id": "s", **fields}
    captured = hook._CAPTURE_POLICY.filter(payload)
    data = hook._event_adapter_for(provider).parse(event, captured).to_lifecycle_data()
    data["_hook_attributes"] = hook._CAPTURE_POLICY.attributes(captured)
    if logs_enabled:
        data["_hook_log_body"] = captured
    logger, span = Mock(), Mock()
    monkeypatch.setattr(hook, "_LOGS_INITIALIZED", logs_enabled)
    monkeypatch.setattr(hook, "_get_otel_logger", lambda name: logger)
    hook._populate_span(span, event, data, provider)
    attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert expected.items() <= attrs.items()
    assert payload not in attrs.values()
    assert logger.info.call_count == int(logs_enabled)
    if logs_enabled:
        assert logger.info.call_args.args == (payload,)


def test_selected_array_fields_preserve_indices_without_retaining_siblings():
    data = {"toolCalls": [{"id": "a", "secret": "PRIVATE"}, {"id": "b", "secret": "PRIVATE"}]}
    selected = policy("selected_content", include=["tool_calls.1.id"])
    assert selected.filter(data) == {"toolCalls": [None, {"id": "b"}]}
    assert selected.attributes(selected.filter(data)) == {"hook.tool_calls.1.id": "b"}


def test_parent_relationship_ids_are_metadata():
    data = {"parentConversationId": "session-parent", "parentToolCallId": "call-parent", "subagentId": "agent"}
    assert policy().filter(data) == data


@pytest.mark.parametrize("selection,expected", [
    ({}, {"hook.tool_input.command": "pwd", "hook.tool_input.other": 2, "hook.model": "example"}),
    ({"span_include": []}, {}),
    ({"span_include": ["tool_input.command"]}, {"hook.tool_input.command": "pwd"}),
    ({"span_include": ["tool_input"]}, {"hook.tool_input.command": "pwd", "hook.tool_input.other": 2}),
    ({"span_include": ["tool_input.*"]}, {"hook.tool_input.command": "pwd", "hook.tool_input.other": 2}),
])
def test_span_allowlist_only_selects_extra_attributes(selection, expected):
    capture = policy("full", **selection)
    payload = {"toolInput": {"Command": "pwd", "other": 2}, "model": "example"}
    assert capture.filter(payload) == payload
    assert capture.attributes(capture.filter(payload)) == expected


def test_span_allowlist_precedes_limits_and_mappings():
    capture = policy("full", span_include=["tool_input.command"], max_attributes=1,
                     mappings={"tool_input.command": "command.observed"})
    data = {"a": {str(i): "x" * 5000 for i in range(100)}, "tool_input": {"command": "pwd"}}
    assert capture.attributes(capture.filter(data)) == {"command.observed": "pwd"}


def test_span_allowlist_respects_shared_privacy_exclusions():
    capture = policy("full", span_include=["tool_input"], exclude=["tool_input.secret"])
    payload = {"tool_input": {"command": "pwd", "secret": "PRIVATE"}, "extra": "log-only"}
    permitted = capture.filter(payload)
    assert permitted == {"tool_input": {"command": "pwd"}, "extra": "log-only"}
    assert capture.attributes(permitted) == {"hook.tool_input.command": "pwd"}


def test_span_allowlist_cannot_enable_content_removed_by_capture_mode():
    capture = policy("metadata_only", span_include=["tool_input.command"])
    assert capture.attributes(capture.filter({"tool_input": {"command": "PRIVATE"}})) == {}


def test_span_allowlist_selects_array_leaves_without_changing_log_body():
    capture = policy("full", span_include=["results.*.id", "flags.1"])
    payload = {"results": [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}], "flags": [True, False]}
    assert capture.filter(payload) == payload
    assert capture.attributes(payload) == {"hook.results.0.id": "a", "hook.results.1.id": "b", "hook.flags.1": False}


@pytest.mark.parametrize("value", [None, "*", [1], [""]])
def test_span_allowlist_rejects_invalid_configuration(value):
    with pytest.raises(ValueError, match="capture.span_include"):
        policy("full", span_include=value)


def test_managed_span_allowlist_overrides_local_and_isolates_pending_state(isolated_runtime):
    hook._apply_config_env({"capture": {"mode": "full", "span_include": ["*"]}})
    old_batches = hook._BATCH_DIR
    hook._apply_config_env({"capture": {"mode": "full", "span_include": ["*"]},
                           "_managed": {"capture": {"span_include": []}}})
    assert hook._BATCH_DIR != old_batches
    assert hook._CAPTURE_POLICY.attributes({"field": 42}) == {}
    assert hook._CAPTURE_POLICY.filter({"field": 42}) == {"field": 42}


@pytest.mark.parametrize("provider", ["cursor", "windsurf", "claude", "codex", "gemini", "antigravity", "copilot", "opencode"])
@pytest.mark.parametrize("agent_capture", [False, True], ids=["canonical", "agent-extras"])
def test_reflect_config_replays_supported_harnesses(tmp_path, provider, agent_capture):
    """Replay provider shapes through separate processes and real SDK signals."""
    example = next(path for path in (
        Path(hook.__file__).parent / "examples/otel_config.reflect.example.json",
        Path(sys.prefix) / "share/opentelemetry-hooks/examples/otel_config.reflect.example.json",
    ) if path.exists())
    config_data = json.loads(example.read_text())
    if agent_capture:
        config_data["capture"] = json.loads((example.parent / "capture" / f"{provider}.example.json").read_text())["capture"]
    # Substitute only the exporter transport; intercept SDK export, without a gateway.
    config_data.update(OTEL_EXPORTER_OTLP_ENDPOINT="http://collector.example.test:4318",
                       OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf")
    config_data["batch"]["disable_sdk_batch"] = True
    config = tmp_path / "config.json"
    config.write_text(json.dumps(config_data))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "IDE_OTEL_"))}
    env.update(IDE_OTEL_HOOK_HOME=str(tmp_path), IDE_OTEL_CONFIG=str(config),
               FIXTURE_LOG_FILE=str(tmp_path / "exported_logs.jsonl"))
    args = {"command": "echo fixture", "file_path": "/workspace/example.py", "path": "/workspace", "description": "fixture"}
    events = [
        {"hook_event_name": "UserPromptSubmit", "session_id": "fixture", "prompt": "fixture prompt"},
        {"hook_event_name": "PreToolUse", "session_id": "fixture", "tool_use_id": "call", "tool_name": "read",
         "tool_input": args, "unselected": {"secret": "LOG_ONLY"}},
        {"hook_event_name": "PostToolUse", "session_id": "fixture", "tool_use_id": "call", "tool_name": "read",
         "tool_response": {"exit_code": 0, "output": "fixture output"}},
        {"hook_event_name": "Stop", "session_id": "fixture", "response": "fixture response"},
    ]
    if provider == "copilot":
        for event, name in zip(events, ["userPromptSubmitted", "preToolUse", "postToolUse", "agentStop"]):
            event["hook_event_name"] = name
            event["sessionId"] = event.pop("session_id")
            if "tool_input" in event:
                event["toolArgs"] = json.dumps(event.pop("tool_input"))
            if "tool_name" in event:
                event["toolName"] = event.pop("tool_name")
            if "tool_response" in event:
                event["toolResult"] = {"textResultForLlm": "fixture output"}
    elif provider == "windsurf":
        events = [
            {"agent_action_name": "pre_user_prompt", "trajectory_id": "fixture", "execution_id": "turn",
             "tool_info": {"user_prompt": "fixture prompt"}},
            {"agent_action_name": "pre_mcp_tool_use", "trajectory_id": "fixture", "execution_id": "turn",
             "tool_info": {"mcp_server_name": "fixture", "mcp_tool_name": "read", "mcp_tool_arguments": args},
             "unselected": {"secret": "LOG_ONLY"}},
            {"agent_action_name": "post_mcp_tool_use", "trajectory_id": "fixture", "execution_id": "turn",
             "tool_info": {"mcp_server_name": "fixture", "mcp_tool_name": "read", "mcp_result": {"exit_code": 0}}},
            {"agent_action_name": "post_cascade_response", "trajectory_id": "fixture", "execution_id": "turn",
             "tool_info": {"response": "fixture response"}},
        ]
        events[1:1] = [
            {"agent_action_name": "pre_read_code", "trajectory_id": "fixture", "execution_id": "turn",
             "tool_info": {"file_path": args["file_path"]}},
            {"agent_action_name": "pre_run_command", "trajectory_id": "fixture", "execution_id": "turn",
             "tool_info": {"command_line": args["command"], "cwd": args["path"]}},
        ]
    elif provider == "gemini":
        for event, name in zip(events, ["BeforeAgent", "BeforeTool", "AfterTool", "AfterAgent"]):
            event["hook_event_name"] = name
        events[-1]["prompt_response"] = events[-1].pop("response")
        events.insert(1, {"hook_event_name": "BeforeModel", "session_id": "fixture",
                          "llm_request": {"model": "fixture-model", "contents": ["LOG_ONLY"]}})
    elif provider == "cursor":
        for event, name in zip(events, ["beforeSubmitPrompt", "preToolUse", "postToolUse", "stop"]):
            event["hook_event_name"] = name
            event["conversation_id"] = event.pop("session_id")
            event["generation_id"] = "turn"
    elif provider == "opencode":
        for event in events:
            if "tool_use_id" in event:
                event["tool_id"] = event.pop("tool_use_id")
        events[2].pop("tool_response")
        events[2].update(hook_event_name="PostToolUseFailure", exit_code=1, error="exit 1", tool_output="fixture output")
    elif provider == "claude":
        events.insert(-1, {"hook_event_name": "PostToolBatch", "session_id": "fixture",
                           "tool_calls": [{"tool_name": "read", "tool_use_id": "call"}]})
    for event in events:
        cli_args = [] if provider == "antigravity" else [f"--{provider}"]
        if provider == "antigravity":
            env["IDE_OTEL_IDE_NAME"] = provider  # manual compatibility runner
        payload = dict(event)
        # Copilot's registered command supplies the event name, outside stdin.
        if provider == "copilot":
            cli_args += ["--event", payload.pop("hook_event_name")]
        result = subprocess.run([sys.executable, "-c", _SDK_REPLAY_RUNNER, *cli_args],
                                input=json.dumps(payload), env=env, capture_output=True, text=True,
                                timeout=15, cwd=Path(hook.__file__).parent)
        assert result.returncode == 0, result.stderr
        assert "LOG_ONLY" not in result.stdout
    spans = [json.loads(line) for path in (tmp_path / ".state/local_spans").glob("*.jsonl")
             for line in path.read_text().splitlines()]
    assert spans
    attrs = {key: value for span in spans for key, value in span["attributes"].items()}
    assert attrs["gen_ai.client.tool.input.file_path"] == args["file_path"]
    assert attrs["gen_ai.client.tool.input.path"] == args["path"]
    assert attrs["gen_ai.client.command"] == args["command"]
    assert attrs["gen_ai.client.session_id"] == "fixture"
    assert "LOG_ONLY" not in json.dumps(spans)
    if provider == "windsurf":
        assert attrs["gen_ai.client.file_path"] == args["file_path"]
        assert attrs["gen_ai.client.cwd"] == args["path"]
        assert "hook.tool_info.command_line" not in attrs  # canonical command needs no extra field
    if not agent_capture or provider == "copilot":
        assert not any(key.startswith("hook.") for key in attrs)
    elif provider == "opencode":
        assert attrs["hook.exit_code"] == 1
    else:
        assert attrs["hook.tool_response.exit_code"] == 0
    if agent_capture and provider == "gemini":
        assert attrs["hook.llm_request.model"] == "fixture-model"
    if agent_capture and provider == "claude":
        assert attrs["hook.tool_calls.0.tool_use_id"] == "call"
    if provider == "copilot":
        assert "hook.tool_args" not in attrs
    logs = [json.loads(line) for line in (tmp_path / "exported_logs.jsonl").read_text().splitlines()]
    expected = [{k: v for k, v in event.items() if provider != "copilot" or k != "hook_event_name"} for event in events]
    assert len(logs) == len(events)
    assert all(body in [record["body"] for record in logs] for body in expected)
    assert all(record["trace_id"] and record["span_id"] for record in logs)


def test_config_does_not_translate_capture_policy_into_process_flags(isolated_runtime):
    for flag in ("TEXT", "CONVERSATION_CONTENT", "TOOL_INPUT_CONTENT", "TOOL_DEFINITIONS"):
        os.environ[f"IDE_OTEL_CAPTURE_{flag}"] = "legacy-value"
    before = dict(os.environ)
    hook._apply_config_env({"capture": {"mode": "full"}})
    assert hook._CAPTURE_POLICY.content_enabled
    for key, value in before.items():
        assert os.environ[key] == value
    hook._apply_config_env({"capture": {"mode": "metadata_only"}})
    assert not hook._CAPTURE_POLICY.content_enabled


def test_span_workspace_and_native_enrichment_is_computed_once(isolated_runtime, monkeypatch):
    workspace = Mock(return_value={"gen_ai.client.workspace_path": "/fixture"})
    native = Mock(return_value={"gen_ai.client.native.source": "fixture"})
    monkeypatch.setattr(hook, "_workspace_observability_attributes", workspace)
    monkeypatch.setattr(hook, "_native_telemetry_attributes", native)
    span = Mock()
    hook._populate_span(span, "SessionStart", {}, "claude")
    workspace.assert_called_once()
    native.assert_called_once()
    attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert attrs["gen_ai.client.workspace_path"] == "/fixture"
    assert attrs["gen_ai.client.native.source"] == "fixture"
