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


@pytest.mark.parametrize("provider", ["cursor", "claude", "codex", "copilot", "gemini", "windsurf", "opencode"])
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


@pytest.mark.parametrize("mode,logs_enabled", [("full", True), ("full", False), ("metadata_only", True)])
def test_batched_lifecycle_replay_with_real_sdk(tmp_path, mode, logs_enabled):
    """Separate hook processes, actual SDK export, no collector/network required."""
    runner = '''
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
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "capture": {"mode": mode}, "logs": {"enabled": logs_enabled},
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
            [sys.executable, "-c", runner, "--claude"], input=json.dumps(event), env=env,
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
