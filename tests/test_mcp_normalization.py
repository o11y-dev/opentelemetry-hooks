"""MCP identity stays canonical across parsing, correlation, and emission."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

import otel_hook as hook
from otel_hooks.capture import CapturePolicy


@pytest.fixture
def mcp_runtime(monkeypatch):
    monkeypatch.setattr(hook, "_CAPTURE_POLICY", CapturePolicy(mode="full"))
    monkeypatch.setattr(hook, "_LOGS_INITIALIZED", True)
    monkeypatch.setattr(hook, "_collect_event_enrichment_attributes", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(hook, "_detect_client_version", lambda *_args: None)
    logger = Mock()
    monkeypatch.setattr(hook, "_get_otel_logger", lambda _name: logger)
    return logger


@pytest.mark.parametrize("provider,event,payload,server,tool", [
    ("claude", "PostToolUse", {"tool_name": "mcp__reflect__reflect__context"}, "reflect", "reflect__context"),
    ("cursor", "afterMCPExecution", {
        "tool_name": "MCP:search", "mcp_server_name": "real-server",
        "command": "/opt/mcp", "duration": 123,
    }, "real-server", "search"),
    ("windsurf", "post_mcp_tool_use", {"tool_info": {
        "mcp_server_name": "real-server", "mcp_tool_name": "search", "command_line": "/opt/mcp",
    }}, "real-server", "search"),
])
def test_parsed_identity_reaches_span_without_reinterpreting_raw_fields(
    mcp_runtime, monkeypatch, provider, event, payload, server, tool,
):
    original = deepcopy(payload)
    parsed = hook._event_adapter_for(provider).parse(event, payload)
    data = parsed.to_lifecycle_data()
    data["_hook_log_body"] = payload
    monkeypatch.setattr(hook, "_normalize_mcp_event_data", Mock(side_effect=AssertionError("already parsed")))

    span = Mock()
    hook._populate_span(span, parsed.event_name, data, provider)

    attrs = dict(call.args for call in span.set_attribute.call_args_list)
    assert attrs["gen_ai.client.mcp_server"] == server
    assert attrs["gen_ai.client.mcp_tool"] == tool
    if provider == "cursor":
        assert attrs["gen_ai.client.mcp.duration_ms"] == 123.0
    assert payload == original
    assert mcp_runtime.info.call_args.args == (original,)


@pytest.mark.parametrize("buffered", [False, True], ids=["streaming", "buffered"])
def test_cursor_correlation_preserves_duration_identity_and_raw_logs(mcp_runtime, monkeypatch, buffered):
    session = {}
    raw_events = [
        ("preToolUse", {"tool_name": "MCP:search", "tool_use_id": "call-1"}),
        ("beforeMCPExecution", {
            "tool_name": "search", "mcp_server_name": "real-server", "command": "/opt/mcp",
        }),
        ("afterMCPExecution", {
            "tool_name": "search", "mcp_server_name": "real-server", "duration": 1979,
            "result_json": {"result": "found"},
        }),
        ("postToolUse", {"tool_name": "MCP:search", "tool_use_id": "call-1"}),
    ]
    expected_raw = deepcopy(raw_events)
    events = []
    emitted = []

    def emit(event_name, data):
        span = Mock()
        hook._populate_span(span, event_name, data, "cursor")
        emitted.append(dict(call.args for call in span.set_attribute.call_args_list))

    for index, (event_name, payload) in enumerate(raw_events):
        parsed = hook._event_adapter_for("cursor").parse(event_name, payload)
        data = parsed.to_lifecycle_data()
        data["_hook_log_body"] = payload
        decision = hook.ToolInvocationCorrelator(session, "cursor", "generation", index + 1).prepare(
            parsed.event_name, data,
        )
        assert not decision.duplicate
        assert decision.data["tool_use_id"] == "call-1"
        if parsed.event_name in {"BeforeMCPExecution", "AfterMCPExecution"}:
            assert decision.correlated
        events.append((parsed.event_name, decision.data))
        if not buffered:
            emit(parsed.event_name, decision.data)

    # Flush consumes the fields stored at the ingress boundary, including
    # invocation evidence learned after the original PreToolUse was buffered.
    monkeypatch.setattr(hook, "_normalize_mcp_event_data", Mock(side_effect=AssertionError("already correlated")))
    if buffered:
        for event_name, data in events:
            emit(event_name, hook._enrich_buffered_event(data, session))

    post = emitted[-1]
    assert post["gen_ai.client.mcp_server"] == "real-server"
    assert post["gen_ai.client.mcp_tool"] == "search"
    assert post["gen_ai.client.tool_use_id"] == "call-1"
    assert post["gen_ai.client.mcp.duration_ms"] == 1979.0
    assert post["gen_ai.client.status"] == "success"
    assert post["gen_ai.client.mcp.output.sha256"] == hook._hash_text('{"result": "found"}')
    if buffered:
        assert {attrs["gen_ai.client.mcp_server"] for attrs in emitted} == {"real-server"}
    assert [call.args[0] for call in mcp_runtime.info.call_args_list] == [data for _, data in expected_raw]
    assert raw_events == expected_raw


def test_invocation_merge_preserves_callback_identity_and_failure(mcp_runtime):
    parsed = hook._event_adapter_for("claude").parse("PostToolUseFailure", {
        "tool_name": "mcp__encoded-server__query__all", "mcp_server_name": "callback-server",
        "tool_use_id": "call-1", "error": "failed", "duration_ms": 0,
    })
    data = parsed.to_lifecycle_data()
    merged = hook._merge_invocation_data(data, {
        "tool_use_id": "call-1", "mcp_server": "previous-server", "mcp_tool": "previous-tool",
        "duration_ms": 42, "mcp_status": "success",
    })

    attrs = hook._mcp_observability_attributes(parsed.event_name, merged)
    assert attrs["gen_ai.client.mcp_server"] == "callback-server"
    assert attrs["gen_ai.client.mcp_tool"] == "query__all"
    assert attrs["gen_ai.client.mcp.duration_ms"] == 0
    assert attrs["gen_ai.client.status"] == "error"
    assert attrs["gen_ai.client.error.sha256"] == hook._hash_text("failed")
    assert "mcp_status" not in data
