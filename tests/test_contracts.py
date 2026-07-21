"""Provider contract, privacy, lifecycle, and diagnostics regressions."""

import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

import otel_hook


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "contracts"


class RecordingSpan:
    def __init__(self):
        self.attributes = {}
        self.status = None

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_status(self, status):
        self.status = status


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    state = tmp_path / "state"
    sessions = state / "sessions"
    batches = state / "batches"
    locks = state / "locks"
    local_spans = state / "local_spans"
    for path in (sessions, batches, locks, local_spans):
        path.mkdir(parents=True)
    monkeypatch.setattr(otel_hook, "_STATE_DIR", str(state))
    monkeypatch.setattr(otel_hook, "_SESSION_DIR", str(sessions))
    monkeypatch.setattr(otel_hook, "_BATCH_DIR", str(batches))
    monkeypatch.setattr(otel_hook, "_LOCK_DIR", str(locks))
    monkeypatch.setattr(otel_hook, "_LOCAL_SPANS_DIR", str(local_spans))
    monkeypatch.setattr(otel_hook, "_DELIVERY_HEALTH_PATH", str(state / "delivery_health.json"))
    return state


@pytest.mark.parametrize(
    "fixture_path",
    sorted(path for path in FIXTURE_DIR.glob("*.json") if path.name != "capabilities.json"),
    ids=lambda path: path.stem,
)
def test_sanitized_provider_contract_fixture(fixture_path, monkeypatch):
    monkeypatch.delenv("IDE_OTEL_CAPTURE_TEXT", raising=False)
    monkeypatch.delenv("IDE_OTEL_CAPTURE_CONVERSATION_CONTENT", raising=False)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    adapter = otel_hook._event_adapter_for(fixture["provider"])
    for case in fixture["cases"]:
        data = otel_hook._normalize_input_data(case["raw"])
        original = otel_hook._get_event_name(data)
        canonical = adapter.normalize(original, None, data)
        expected = case["expected"]
        assert canonical.provider == fixture["provider"]
        assert canonical.event_name == expected["event_name"]
        assert canonical.session_id == expected["session_id"]
        if "generation_id" in expected:
            assert canonical.generation_id == expected["generation_id"]
        if "turn_id" in expected:
            assert canonical.turn_id == expected["turn_id"]
        if "conversation" in expected:
            actual = [
                {"kind": item.kind, "role": item.role, "length": item.length}
                for item in canonical.conversation
            ]
            assert actual == expected["conversation"]
            assert all(item.text is None for item in canonical.conversation)
            assert all(len(item.sha256) == 64 for item in canonical.conversation)
        if "relationship" in expected:
            task = expected["relationship"]["task"]
            assert canonical.relationship.task is None
            assert canonical.relationship.task_length == len(task)
            assert canonical.relationship.task_sha256 == hashlib.sha256(task.encode()).hexdigest()


def test_capability_manifest_matches_provider_adapters():
    manifest = json.loads((FIXTURE_DIR / "capabilities.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert set(manifest["providers"]) == set(otel_hook._PROVIDER_EVENT_ADAPTERS)
    assert all("conversation" in capabilities for capabilities in manifest["providers"].values())


def test_conversation_content_is_hash_only_by_default(monkeypatch):
    monkeypatch.delenv("IDE_OTEL_CAPTURE_TEXT", raising=False)
    monkeypatch.delenv("IDE_OTEL_CAPTURE_CONVERSATION_CONTENT", raising=False)
    event = otel_hook.CodexEventAdapter().normalize(
        "UserPromptSubmit",
        "UserPromptSubmit",
        {"session_id": "session-1", "prompt": "synthetic prompt"},
    )
    lifecycle_data = event.to_lifecycle_data()
    span = RecordingSpan()
    otel_hook._apply_conversation_attributes(span, event.event_name, lifecycle_data)
    assert span.attributes["gen_ai.client.prompt.length"] == 16
    assert span.attributes["gen_ai.client.prompt.sha256"] == hashlib.sha256(b"synthetic prompt").hexdigest()
    assert "gen_ai.client.prompt.text" not in span.attributes
    assert event.conversation[0].text is None
    assert "prompt" not in lifecycle_data
    assert "text" not in lifecycle_data["_conversation_records"][0]


def test_conversation_content_new_gate_and_legacy_gate(monkeypatch):
    for env_name in ("IDE_OTEL_CAPTURE_CONVERSATION_CONTENT", "IDE_OTEL_CAPTURE_TEXT"):
        monkeypatch.delenv("IDE_OTEL_CAPTURE_CONVERSATION_CONTENT", raising=False)
        monkeypatch.delenv("IDE_OTEL_CAPTURE_TEXT", raising=False)
        monkeypatch.setenv(env_name, "true")
        event = otel_hook.ClaudeEventAdapter().normalize(
            "Stop",
            "Stop",
            {"session_id": "session-1", "last_assistant_message": "fixture response"},
        )
        span = RecordingSpan()
        otel_hook._apply_conversation_attributes(
            span,
            event.event_name,
            event.to_lifecycle_data(),
        )
        assert span.attributes["gen_ai.client.response.text"] == "fixture response"


def test_conversation_logs_are_explicit_opt_in(monkeypatch):
    event = otel_hook.CopilotEventAdapter().normalize(
        "errorOccurred",
        "ErrorOccurred",
        {"session_id": "session-1", "error": "synthetic failure"},
    )
    logger = mock.MagicMock()
    monkeypatch.setattr(otel_hook, "_LOGS_INITIALIZED", True)
    monkeypatch.setattr(otel_hook, "_get_otel_logger", lambda _name: logger)
    monkeypatch.setattr(otel_hook, "_inject_trace_context", lambda attrs: ("1", "2"))
    monkeypatch.delenv("IDE_OTEL_CAPTURE_TEXT", raising=False)
    monkeypatch.delenv("IDE_OTEL_CAPTURE_CONVERSATION_CONTENT", raising=False)
    monkeypatch.delenv("IDE_OTEL_ENABLE_CONVERSATION_LOGS", raising=False)
    otel_hook._emit_conversation_logs(event.event_name, event.to_lifecycle_data())
    logger.error.assert_not_called()

    monkeypatch.setenv("IDE_OTEL_ENABLE_CONVERSATION_LOGS", "true")
    otel_hook._emit_conversation_logs(event.event_name, event.to_lifecycle_data())
    logger.error.assert_called_once()
    attrs = logger.error.call_args.kwargs["extra"]
    assert attrs["gen_ai.client.error.length"] == 17
    assert "gen_ai.client.error.text" not in attrs


def test_duplicate_prompt_and_concurrent_subagents_are_handled_independently(isolated_state):
    session_id = "lifecycle-session"
    otel_hook._create_session_context(session_id, {"session_id": session_id}, "claude")
    prompt_payload = {"session_id": session_id, "prompt": "same prompt"}
    adapter = otel_hook.ClaudeEventAdapter()
    prompt = adapter.normalize("UserPromptSubmit", "UserPromptSubmit", prompt_payload)
    prompt_data = prompt.to_lifecycle_data()
    first_prompt = otel_hook._buffer_session_event(session_id, prompt.event_name, prompt_data, "claude")
    duplicate_prompt = otel_hook._buffer_session_event(session_id, prompt.event_name, prompt_data, "claude")
    assert not first_prompt.duplicate
    assert duplicate_prompt.duplicate
    assert duplicate_prompt.generation_key == first_prompt.generation_key

    otel_hook._complete_generation_state(session_id, first_prompt.generation_key)
    repeated_prompt = otel_hook._buffer_session_event(session_id, prompt.event_name, prompt_data, "claude")
    assert not repeated_prompt.duplicate
    assert repeated_prompt.generation_key != first_prompt.generation_key

    start = adapter.normalize(
        "SubagentStart",
        "SubagentStart",
        {"session_id": session_id, "subagent_type": "planner", "subagent_task": "inspect fixture"},
    )
    start_data = start.to_lifecycle_data()
    first_start = otel_hook._buffer_session_event(session_id, start.event_name, start_data, "claude")
    second_start = otel_hook._buffer_session_event(session_id, start.event_name, start_data, "claude")
    assert not first_start.duplicate
    assert not second_start.duplicate
    assert first_start.data["agent_id"].startswith("hook:")
    assert second_start.data["agent_id"].startswith("hook:")
    assert second_start.data["agent_id"] != first_start.data["agent_id"]

    stop = adapter.normalize(
        "SubagentStop",
        "SubagentStop",
        {"session_id": session_id, "subagent_type": "planner", "status": "success"},
    )
    stop_data = stop.to_lifecycle_data()
    first_stop = otel_hook._buffer_session_event(session_id, stop.event_name, stop_data, "claude")
    second_stop = otel_hook._buffer_session_event(session_id, stop.event_name, stop_data, "claude")
    duplicate_stop = otel_hook._buffer_session_event(session_id, stop.event_name, stop_data, "claude")
    assert first_stop.data["agent_id"] == first_start.data["agent_id"]
    assert second_stop.data["agent_id"] == second_start.data["agent_id"]
    assert first_stop.data["parent_agent_id"]
    assert duplicate_stop.duplicate


def test_provider_subagent_callback_id_is_idempotent(isolated_state):
    session_id = "provider-subagent-session"
    otel_hook._create_session_context(session_id, {"session_id": session_id}, "claude")
    event = otel_hook.ClaudeEventAdapter().normalize(
        "SubagentStart",
        "SubagentStart",
        {
            "session_id": session_id,
            "event_id": "provider-callback-1",
            "subagent_type": "planner",
        },
    )
    data = event.to_lifecycle_data()
    first = otel_hook._buffer_session_event(session_id, event.event_name, data, "claude")
    duplicate = otel_hook._buffer_session_event(session_id, event.event_name, data, "claude")
    assert not first.duplicate
    assert duplicate.duplicate


def test_error_and_compaction_callbacks_use_bounded_deduplication(isolated_state):
    session_id = "dedupe-session"
    otel_hook._create_session_context(session_id, {"session_id": session_id}, "claude")
    adapter = otel_hook.ClaudeEventAdapter()
    prompt = adapter.normalize(
        "UserPromptSubmit",
        "UserPromptSubmit",
        {"session_id": session_id, "prompt": "start generation"},
    )
    otel_hook._buffer_session_event(
        session_id,
        prompt.event_name,
        prompt.to_lifecycle_data(),
        "claude",
    )

    for event_name, payload in (
        ("ErrorOccurred", {"error": "synthetic failure"}),
        ("PreCompact", {"trigger": "manual"}),
        ("PostCompact", {"trigger": "manual"}),
    ):
        event = adapter.normalize(
            event_name,
            event_name,
            {"session_id": session_id, **payload},
        )
        event_data = event.to_lifecycle_data()
        first = otel_hook._buffer_session_event(session_id, event.event_name, event_data, "claude")
        duplicate = otel_hook._buffer_session_event(session_id, event.event_name, event_data, "claude")
        assert not first.duplicate
        assert duplicate.duplicate


def test_workspace_remote_normalization_removes_credentials():
    ssh = otel_hook._normalize_repository_remote("git@github.com:o11y-dev/opentelemetry-hooks.git")
    https = otel_hook._normalize_repository_remote(
        "https://token:secret@github.com/o11y-dev/opentelemetry-hooks.git?ignored=true"
    )
    assert ssh == "github.com/o11y-dev/opentelemetry-hooks"
    assert https == ssh
    assert "secret" not in https


def test_native_context_attributes_require_valid_ids():
    assert otel_hook._native_telemetry_attributes({
        "trace_id": "1" * 32,
        "span_id": "2" * 16,
        "_hook_provider_adapter": "gemini",
    }) == {}
    attrs = otel_hook._native_telemetry_attributes({
        "native_trace_id": "1" * 32,
        "native_span_id": "2" * 16,
        "native_parent_span_id": "3" * 16,
        "native_source": "gemini-native",
        "_hook_provider_adapter": "gemini",
    })
    assert attrs["gen_ai.client.native_source"] == "gemini-native"
    assert attrs["gen_ai.client.native_trace_id"] == "1" * 32
    assert otel_hook._native_telemetry_attributes({"native_trace_id": "not-hex"}) == {}


def test_adapter_separates_upstream_and_native_trace_contracts():
    adapter = otel_hook.CursorEventAdapter()
    upstream = adapter.normalize(
        "sessionStart",
        None,
        {
            "session_id": "session-1",
            "trace_id": "1" * 32,
            "span_id": "2" * 16,
        },
    )
    assert upstream.native.trace_id is None
    assert upstream.to_lifecycle_data()["trace_id"] == "1" * 32

    native = adapter.normalize(
        "sessionStart",
        None,
        {
            "session_id": "session-1",
            "nativeTraceId": "3" * 32,
            "nativeSpanId": "4" * 16,
            "nativeSource": "cursor-native",
        },
    )
    assert native.native.trace_id == "3" * 32
    assert native.native.span_id == "4" * 16
    assert native.native.source == "cursor-native"


def test_cursor_duration_unit_normalization_does_not_leak_to_windsurf():
    payload = {
        "session_id": "session-1",
        "mcp_server_name": "reflect",
        "tool_name": "reflect_context",
        "duration": 1.979,
    }
    cursor = otel_hook.CursorEventAdapter().normalize(
        "afterMCPExecution",
        None,
        payload,
    )
    windsurf = otel_hook.WindsurfEventAdapter().normalize(
        "afterMCPExecution",
        None,
        payload,
    )
    assert cursor.to_lifecycle_data()["duration_ms"] == 1979.0
    assert "duration_ms" not in windsurf.to_lifecycle_data()


def test_native_links_require_explicit_native_identifiers():
    assert otel_hook._load_otel_modules()
    assert otel_hook._native_span_links({
        "trace_id": "1" * 32,
        "span_id": "2" * 16,
    }) == []
    links = otel_hook._native_span_links({
        "native_trace_id": "1" * 32,
        "native_span_id": "2" * 16,
        "native_source": "gemini-native",
    })
    assert len(links) == 1
    assert links[0].attributes["gen_ai.client.native_source"] == "gemini-native"


def test_real_failure_sets_otel_error_but_interrupt_stays_unset():
    assert otel_hook._load_otel_modules()
    failed = RecordingSpan()
    otel_hook._apply_conversation_attributes(
        failed,
        "PostToolUseFailure",
        {"error_type": "FixtureError"},
    )
    otel_hook._apply_operation_status(
        failed,
        "PostToolUseFailure",
        {"error_type": "FixtureError"},
    )
    assert failed.attributes["gen_ai.client.status"] == "error"
    assert failed.status.status_code == otel_hook.StatusCode.ERROR
    assert not failed.status.description

    interrupted = RecordingSpan()
    data = {"is_interrupt": True, "error_type": "Interrupted"}
    otel_hook._apply_conversation_attributes(interrupted, "PostToolUseFailure", data)
    otel_hook._apply_operation_status(interrupted, "PostToolUseFailure", data)
    assert interrupted.attributes["gen_ai.client.status"] == "interrupted"
    assert interrupted.status is None


def test_trace_authoritative_flush_does_not_fail_for_log_only_failure(monkeypatch):
    trace_provider = mock.MagicMock()
    trace_provider.force_flush.return_value = True
    log_provider = mock.MagicMock()
    log_provider.force_flush.return_value = False
    monkeypatch.setattr(otel_hook, "_LOGS_INITIALIZED", True)
    monkeypatch.setattr(
        otel_hook,
        "trace",
        mock.MagicMock(get_tracer_provider=lambda: trace_provider),
    )
    monkeypatch.setattr("opentelemetry._logs.get_logger_provider", lambda: log_provider)

    assert not otel_hook._force_flush_provider()
    assert otel_hook._force_flush_provider(authoritative_signal="traces")


def test_workspace_identity_is_not_invented_from_hook_process_cwd(monkeypatch):
    monkeypatch.setattr(otel_hook, "_resolve_repository_context", lambda *_args, **_kwargs: {})
    assert otel_hook._workspace_observability_attributes({}, None) == {}


def test_doctor_reports_enabled_log_exporter_failure(monkeypatch, isolated_state):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.test:4318")
    monkeypatch.setenv("IDE_OTEL_ENABLE_LOGS", "true")
    monkeypatch.setattr(
        otel_hook,
        "_agent_config_paths",
        lambda _global, _cwd: {agent: "/fixture" for agent in otel_hook._SUPPORTED_AGENTS},
    )
    monkeypatch.setattr(otel_hook, "_registered_hook_events", lambda _agent, _path: ["Stop"])
    monkeypatch.setattr(otel_hook, "_detect_ide", lambda _data: "codex")
    otel_hook._record_delivery_health("traces", True)
    otel_hook._record_delivery_health("logs", False)

    report, exit_code = otel_hook._doctor_report(("codex",), True, ".")
    assert exit_code == 1
    assert report["exporter"]["status"] == "failing_recent"
    assert {failure["signal"] for failure in report["recent_delivery_failures"]} == {"logs"}


def test_subagent_stop_links_to_recorded_start_context():
    assert otel_hook._load_otel_modules()
    data = {
        "_canonical_event_name": "SubagentStop",
        "agent_id": "agent-1",
    }
    session_ctx = {
        "agent_invocations": [{
            "agent_id": "agent-1",
            "start_trace_id": "1" * 32,
            "start_span_id": "2" * 16,
        }],
    }
    links = otel_hook._agent_relationship_links(data, session_ctx)
    assert len(links) == 1
    assert f"{links[0].context.trace_id:032x}" == "1" * 32
    assert f"{links[0].context.span_id:016x}" == "2" * 16


def test_delivery_health_sanitizes_endpoint_and_error(monkeypatch, isolated_state):
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "https://user:password@collector.example.test:4318/v1/traces?token=secret",
    )
    otel_hook._record_delivery_health("traces", False, RuntimeError("private failure detail"))
    health = json.loads((isolated_state / "delivery_health.json").read_text(encoding="utf-8"))
    record = health["signals"]["traces"]
    assert record["endpoint"] == "https://collector.example.test:4318"
    assert record["last_error"]["type"] == "RuntimeError"
    assert "private failure detail" not in json.dumps(record)
