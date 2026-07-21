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

    def set_attribute(self, key, value):
        self.attributes[key] = value


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
def test_sanitized_provider_contract_fixture(fixture_path):
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
            assert all(item.sha256 == hashlib.sha256(item.text.encode("utf-8")).hexdigest() for item in canonical.conversation)
        if "relationship" in expected:
            assert canonical.relationship.task == expected["relationship"]["task"]


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
    span = RecordingSpan()
    otel_hook._apply_conversation_attributes(span, event.event_name, event.data)
    assert span.attributes["gen_ai.client.prompt.length"] == 16
    assert span.attributes["gen_ai.client.prompt.sha256"] == hashlib.sha256(b"synthetic prompt").hexdigest()
    assert "gen_ai.client.prompt.text" not in span.attributes
    assert "prompt" not in event.data
    assert "text" not in event.data["_conversation_records"][0]


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
        otel_hook._apply_conversation_attributes(span, event.event_name, event.data)
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
    otel_hook._emit_conversation_logs(event.event_name, event.data)
    logger.error.assert_not_called()

    monkeypatch.setenv("IDE_OTEL_ENABLE_CONVERSATION_LOGS", "true")
    otel_hook._emit_conversation_logs(event.event_name, event.data)
    logger.error.assert_called_once()
    attrs = logger.error.call_args.kwargs["extra"]
    assert attrs["gen_ai.client.error.length"] == 17
    assert "gen_ai.client.error.text" not in attrs


def test_duplicate_prompt_and_subagent_callbacks_are_idempotent(isolated_state):
    session_id = "lifecycle-session"
    otel_hook._create_session_context(session_id, {"session_id": session_id}, "claude")
    prompt_payload = {"session_id": session_id, "prompt": "same prompt"}
    adapter = otel_hook.ClaudeEventAdapter()
    prompt = adapter.normalize("UserPromptSubmit", "UserPromptSubmit", prompt_payload)
    first_prompt = otel_hook._buffer_session_event(session_id, prompt.event_name, prompt.data, "claude")
    duplicate_prompt = otel_hook._buffer_session_event(session_id, prompt.event_name, prompt.data, "claude")
    assert not first_prompt.duplicate
    assert duplicate_prompt.duplicate
    assert duplicate_prompt.generation_key == first_prompt.generation_key

    otel_hook._complete_generation_state(session_id, first_prompt.generation_key)
    repeated_prompt = otel_hook._buffer_session_event(session_id, prompt.event_name, prompt.data, "claude")
    assert not repeated_prompt.duplicate
    assert repeated_prompt.generation_key != first_prompt.generation_key

    start = adapter.normalize(
        "SubagentStart",
        "SubagentStart",
        {"session_id": session_id, "subagent_type": "planner", "subagent_task": "inspect fixture"},
    )
    first_start = otel_hook._buffer_session_event(session_id, start.event_name, start.data, "claude")
    duplicate_start = otel_hook._buffer_session_event(session_id, start.event_name, start.data, "claude")
    assert not first_start.duplicate
    assert duplicate_start.duplicate
    assert first_start.data["agent_id"].startswith("hook:")

    stop = adapter.normalize(
        "SubagentStop",
        "SubagentStop",
        {"session_id": session_id, "subagent_type": "planner", "status": "success"},
    )
    stopped = otel_hook._buffer_session_event(session_id, stop.event_name, stop.data, "claude")
    assert stopped.data["agent_id"] == first_start.data["agent_id"]
    assert stopped.data["parent_agent_id"]


def test_error_and_compaction_callbacks_use_bounded_deduplication(isolated_state):
    session_id = "dedupe-session"
    otel_hook._create_session_context(session_id, {"session_id": session_id}, "claude")
    adapter = otel_hook.ClaudeEventAdapter()
    prompt = adapter.normalize(
        "UserPromptSubmit",
        "UserPromptSubmit",
        {"session_id": session_id, "prompt": "start generation"},
    )
    otel_hook._buffer_session_event(session_id, prompt.event_name, prompt.data, "claude")

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
        first = otel_hook._buffer_session_event(session_id, event.event_name, event.data, "claude")
        duplicate = otel_hook._buffer_session_event(session_id, event.event_name, event.data, "claude")
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
    attrs = otel_hook._native_telemetry_attributes({
        "trace_id": "1" * 32,
        "span_id": "2" * 16,
        "parent_span_id": "3" * 16,
        "_hook_provider_adapter": "gemini",
    })
    assert attrs["gen_ai.client.native_source"] == "gemini"
    assert attrs["gen_ai.client.native_trace_id"] == "1" * 32
    assert otel_hook._native_telemetry_attributes({"trace_id": "not-hex"}) == {}


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
