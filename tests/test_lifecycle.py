"""Lifecycle decisions use the settings supplied by their owning runtime."""
import hashlib

import pytest

from otel_hooks.lifecycle import SessionEventDeduplicator, ToolInvocationCorrelator


@pytest.mark.parametrize("ttl_seconds,duplicate", [(1, False), (5, True)])
def test_explicit_ttl_controls_deduplication_and_invocation_retention(ttl_seconds, duplicate):
    started_at = 1_000_000_000
    session = {
        "tool_invocations": [{"tool_use_id": "old-tool", "updated_at_ns": started_at}],
        "agent_invocations": [{"agent_id": "old-agent", "updated_at_ns": started_at}],
    }
    first = SessionEventDeduplicator(session, "generation", started_at, ttl_seconds=ttl_seconds)
    assert not first.seen("callback")

    later = SessionEventDeduplicator(session, "generation", 3_000_000_000, ttl_seconds=ttl_seconds)
    assert later.seen("callback") is duplicate
    assert bool(session["tool_invocations"]) is duplicate
    assert bool(session["agent_invocations"]) is duplicate
    assert len(session["seen_events"]) == 1


@pytest.mark.parametrize("capture_content", [False, True])
def test_explicit_capture_setting_controls_correlated_error_content(capture_content):
    session = {}
    pre = {"tool_name": "MCP:search", "tool_use_id": "call-1"}
    ToolInvocationCorrelator(
        session, "cursor", "generation", 1, ttl_seconds=86400, capture_content=capture_content,
    ).prepare("PreToolUse", pre)
    after = ToolInvocationCorrelator(
        session, "cursor", "generation", 2, ttl_seconds=86400, capture_content=capture_content,
    ).prepare("AfterMCPExecution", {
        "tool_name": "search", "mcp_server_name": "server", "duration": 0, "error": "callback failed",
    })
    assert after.correlated

    post = ToolInvocationCorrelator(
        session, "cursor", "generation", 3, ttl_seconds=86400, capture_content=capture_content,
    ).prepare("PostToolUseFailure", pre)
    assert post.data["error_length"] == len("callback failed")
    assert post.data["error_sha256"] == hashlib.sha256(b"callback failed").hexdigest()
    assert post.data["mcp_server"] == "server"
    assert post.data["duration_ms"] == 0.0
    assert post.data["status"] == "error"
    assert ("error" in post.data) is capture_content
    assert ("error" in session["tool_invocations"][0]) is capture_content
