"""Regressions based on the first-party contracts linked in docs/hook-coverage.md."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import otel_hook as hook
from otel_hooks import enrichment
from otel_hooks import identity
from otel_hooks import cli as hook_cli
from tests.test_contracts import isolated_state, RecordingSpan  # noqa: F401


@pytest.fixture(autouse=True)
def default_content_privacy(monkeypatch):
    monkeypatch.setattr(hook, "_CAPTURE_POLICY", hook.CapturePolicy())
    monkeypatch.delenv("IDE_OTEL_CAPTURE_TEXT", raising=False)
    monkeypatch.delenv("IDE_OTEL_CAPTURE_CONVERSATION_CONTENT", raising=False)


def event(provider, name, **payload):
    return hook._event_adapter_for(provider).normalize(name, None, payload)


@pytest.mark.parametrize("provider,name,canonical", [
    ("cursor", "preCompact", "PreCompact"),
    ("cursor", "beforeTabFileRead", "BeforeReadFile"),
    ("cursor", "afterTabFileEdit", "AfterFileEdit"),
    ("cursor", "afterAgentResponse", "AfterAgentResponse"),
    ("cursor", "afterAgentThought", "AfterAgentThought"),
    ("cursor", "workspaceOpen", "WorkspaceOpen"),
    ("gemini", "BeforeAgent", "UserPromptSubmit"),
    ("gemini", "AfterAgent", "Stop"),
    ("gemini", "BeforeModel", "BeforeModel"),
    ("gemini", "AfterModel", "AfterModel"),
    ("gemini", "BeforeToolSelection", "BeforeToolSelection"),
    ("gemini", "PreCompress", "PreCompact"),
    ("copilot", "agentStop", "Stop"),
    ("copilot", "postToolUseFailure", "PostToolUseFailure"),
    ("copilot", "subagentStart", "SubagentStart"),
    ("copilot", "subagentStop", "SubagentStop"),
    ("copilot", "preCompact", "PreCompact"),
    ("copilot", "notification", "Notification"),
    ("copilot", "permissionRequest", "PermissionRequest"),
    ("copilot", "userPromptTransformed", "UserPromptTransformed"),
    ("codex", "Interrupt", "Interrupt"),
    ("claude", "StopFailure", "StopFailure"),
])
def test_current_event_contract(provider, name, canonical):
    assert event(provider, name, session_id="fixture").event_name == canonical


@pytest.mark.usefixtures("isolated_state")
def test_gemini_model_chunks_do_not_end_turn_or_create_subagents():
    session = "gemini-turn"
    hook._create_session_context(session, {}, "gemini")
    names = ["BeforeAgent", "BeforeModel", "BeforeToolSelection", "AfterModel", "AfterModel", "AfterTool"]
    generations = []
    for name in names:
        item = event("gemini", name, session_id=session, prompt="private prompt",
                     llm_request={"model": "gemini-fixture", "messages": ["private prompt"]},
                     llm_response={"text": "private chunk"})
        generations.append(hook._buffer_session_event(session, item.event_name, item.to_lifecycle_data(), "gemini").generation_key)
    assert len(set(generations)) == 1
    ctx = hook._load_session_context(session)
    assert ctx["generation_count"] == 1
    assert not ctx.get("agent_invocations")
    final = event("gemini", "AfterAgent", session_id=session, prompt_response="final", prompt="private prompt")
    decision = hook._buffer_generation_end(session, final.event_name, final.to_lifecycle_data(), "gemini")
    assert decision.generation_key == generations[0]
    assert final.conversation[0].length == 5
    assert "private" not in json.dumps(final.to_lifecycle_data())


@pytest.mark.parametrize("provider,name,payload", [
    ("cursor", "afterAgentResponse", {"text": "private response"}),
    ("cursor", "afterAgentThought", {"text": "private thought"}),
    ("claude", "StopFailure", {"error": "rate_limit", "error_details": "private detail", "lastAssistantMessage": "private error"}),
    ("claude", "TaskCreated", {"task_subject": "private task", "task_description": "private details"}),
    ("claude", "MessageDisplay", {"delta": "private chunk", "message_id": "m1", "index": 0, "final": False}),
    ("claude", "UserPromptExpansion", {"prompt": "private prompt", "command_args": "private args"}),
    ("copilot", "subagentStop", {"response": "private response"}),
    ("copilot", "userPromptTransformed", {"prompt": "private prompt", "transformedPrompt": "private expanded"}),
    ("gemini", "Notification", {"message": "private notification", "details": {"secret": "private"}}),
])
def test_new_content_facts_are_private_by_default(monkeypatch, provider, name, payload):
    monkeypatch.delenv("IDE_OTEL_CAPTURE_TEXT", raising=False)
    monkeypatch.delenv("IDE_OTEL_CAPTURE_CONVERSATION_CONTENT", raising=False)
    item = event(provider, name, **payload)
    assert item.conversation and item.conversation[0].sha256
    assert "private" not in json.dumps(item.to_lifecycle_data())


def test_new_response_text_capture_opt_in(monkeypatch):
    monkeypatch.setattr(hook, "_CAPTURE_POLICY", hook.CapturePolicy(mode="full"))
    item = event("cursor", "afterAgentResponse", text="fixture response")
    span = RecordingSpan()
    enrichment._apply_conversation_attributes(span, item.event_name, item.to_lifecycle_data(), capture_policy=hook._CAPTURE_POLICY)
    assert span.attributes["gen_ai.client.response.text"] == "fixture response"


@pytest.mark.usefixtures("isolated_state")
def test_copilot_payload_without_event_name_uses_registration(monkeypatch):
    monkeypatch.setenv("IDE_OTEL_BATCH_ON_STOP", "true")
    monkeypatch.setattr(hook, "_load_config", lambda: {"capture": {"mode": "full"}})
    monkeypatch.setattr(hook, "_configure_logging", lambda: None)
    monkeypatch.setattr(hook, "_cleanup_state", lambda: None)
    # Restore global CLI selection after CliRunner, as real invocations are separate processes.
    monkeypatch.setattr(hook, "_CLI_HOOK_EVENT", None)
    monkeypatch.setattr(hook, "_CLI_HOOK_SOURCE", None)
    runner = CliRunner()
    for name, payload in [("sessionStart", {}), ("userPromptSubmitted", {"prompt": "fixture"}),
                          ("preToolUse", {"toolName": "bash", "toolArgs": '{"command":"pwd"}'})]:
        result = runner.invoke(hook_cli.cli, ["--copilot", "--event", name], input=json.dumps({"sessionId": "copilot-fixture", **payload}))
        assert result.exit_code == 0, result.output
    ctx = hook._load_session_context("copilot-fixture")
    batch = hook._load_batch_events(ctx["current_generation"])
    assert [item["event"] for item in batch] == ["UserPromptSubmit", "PreToolUse"]
    assert batch[-1]["data"]["tool_input"] == {"command": "pwd"}


def test_native_windsurf_identity_and_fields(monkeypatch):
    monkeypatch.setattr(hook, "_CLI_HOOK_EVENT", None)
    raw = {"agent_action_name": "pre_user_prompt", "trajectory_id": "trajectory", "execution_id": "turn",
           "tool_info": {"user_prompt": "private prompt"}}
    item = event("windsurf", hook._get_event_name(raw), **raw)
    assert (item.session_id, item.generation_id, item.event_name) == ("trajectory", "turn", "UserPromptSubmit")
    assert "private" not in json.dumps(item.to_lifecycle_data())
    assert identity._resolve_client_name("windsurf", item.to_lifecycle_data()) == "windsurf"
    assert identity._detect_payload_client_name(raw) == "windsurf"
    tool = event("windsurf", "pre_mcp_tool_use", trajectory_id="trajectory", execution_id="turn",
                 tool_info={"mcp_server_name": "fixture", "mcp_tool_name": "lookup", "mcp_tool_arguments": {"id": 1}})
    assert tool.original_tool_name == "lookup"
    assert tool.to_lifecycle_data()["mcp_server_name"] == "fixture"


@pytest.mark.parametrize("provider,name,expected", [("claude", "StopFailure", "error"), ("codex", "Interrupt", "interrupted")])
@pytest.mark.usefixtures("isolated_state")
def test_terminal_callbacks_close_current_generation(provider, name, expected):
    hook._create_session_context("terminal", {}, provider)
    prompt = event(provider, "UserPromptSubmit", session_id="terminal", prompt="fixture")
    start = hook._buffer_session_event("terminal", prompt.event_name, prompt.to_lifecycle_data(), provider)
    end = event(provider, name, session_id="terminal", error="rate_limit" if name == "StopFailure" else None)
    assert end.event_name in hook._GENERATION_END_EVENTS
    decision = hook._buffer_generation_end("terminal", end.event_name, end.to_lifecycle_data(), provider)
    assert decision.generation_key == start.generation_key
    assert decision.data["status"] == expected


def test_provider_specific_passive_responses():
    assert hook._stdout_response("BeforeToolSelection", "gemini", {}) == "{}"
    for name in ("Interrupt", "SessionEnd", "PreCompact", "PostCompact", "SubagentStart", "SubagentStop"):
        assert hook._stdout_response(name, "codex", {}) is None


@pytest.mark.parametrize("name", ["preToolUse", "subagentStart", "beforeShellExecution",
                                 "beforeMCPExecution", "beforeReadFile", "beforeTabFileRead"])
def test_cursor_permission_hooks_return_valid_observer_response(monkeypatch, name):
    monkeypatch.setenv("IDE_OTEL_LOCAL_SPANS", "true")
    item = event("cursor", name, conversation_id="fixture")
    assert json.loads(hook._stdout_response(item.event_name, "cursor", item.to_lifecycle_data())) == {"permission": "allow"}
    # Internal governance must not turn an explicit denial into approval.
    denial = hook.GovernanceResponse(continue_=False, stop_reason="fixture policy")
    assert json.loads(hook._stdout_response(item.event_name, "cursor", {}, denial)) == {
        "permission": "deny", "user_message": "fixture policy",
    }


def test_permission_reply_is_observation_not_generation_end():
    item = event("opencode", "PermissionReplied", session_id="fixture", event_id="p1",
                 permission_request_id="p1", permission_response="reject")
    assert item.event_name not in hook._GENERATION_END_EVENTS | hook._SESSION_END_EVENTS
    assert item.to_lifecycle_data()["permission_response"] == "reject"


def test_cursor_conversation_join_preserves_native_identifier_without_inventing_trace_context():
    # Synthetic contract pair, not evidence of delivery from a live enterprise export.
    native_log = {"cursor.event.id": "export-event-1", "cursor.conversation.id": "conversation-1",
                  "cursor.request.id": "request-1"}
    item = event("cursor", "beforeSubmitPrompt", conversation_id="conversation-1",
                 generation_id="generation-1", prompt="fixture")
    span = RecordingSpan()
    enrichment._apply_genai_semconv(span, item.event_name, item.to_lifecycle_data(), "cursor", capture_policy=hook._CAPTURE_POLICY)
    assert span.attributes["gen_ai.conversation.id"] == native_log["cursor.conversation.id"]
    assert item.generation_id != native_log["cursor.request.id"]
    assert item.native.trace_id is None


@pytest.mark.parametrize("already_registered", [False, True])
def test_codex_upgrade_preserves_unrelated_matchers(tmp_path, monkeypatch, already_registered):
    monkeypatch.setattr(hook_cli, "_resolve_hook_cmd", lambda: "otel-hook")
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    entries = [{"matcher": "startup|resume|clear", "hooks": [
        {"type": "command", "command": "otel-hook"}, {"type": "command", "command": "echo custom"}]}]
    if already_registered:
        entries.append({"matcher": "startup|resume|clear|compact", "hooks": [{"type": "command", "command": "otel-hook"}]})
    path.write_text(json.dumps({"hooks": {"SessionStart": entries}}))
    hook_cli.setup_codex(global_=False, cwd=str(tmp_path))
    first = path.read_text()
    hook_cli.setup_codex(global_=False, cwd=str(tmp_path))
    assert path.read_text() == first
    config = json.loads(first)["hooks"]
    assert config["SessionStart"][0]["hooks"] == [{"type": "command", "command": "echo custom"}]
    assert config["SessionStart"][0]["matcher"] == "startup|resume|clear"
    assert len(config["SessionStart"]) == 2
    for name in ("Interrupt", "SessionEnd"):
        assert config[name][0]["hooks"][0]["timeout"] == 3


def test_observers_do_not_take_over_worktrees_or_enable_transcripts(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_cli, "_resolve_hook_cmd", lambda: "otel-hook")
    hook_cli.setup_claude(global_=False, cwd=str(tmp_path))
    config = json.loads((tmp_path / ".claude/settings.json").read_text())["hooks"]
    assert "StopFailure" in config and "InstructionsLoaded" in config
    assert "WorktreeCreate" not in config and "FileChanged" not in config
    hook_cli.setup_windsurf(global_=False, cwd=str(tmp_path))
    config = json.loads((tmp_path / ".windsurf/hooks.json").read_text())["hooks"]
    assert "post_cascade_response" in config
    assert "post_cascade_response_with_transcript" not in config


def test_windsurf_uses_active_devin_config(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_cli, "_resolve_hook_cmd", lambda: "otel-hook")
    path = tmp_path / ".devin/hooks.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"hooks": {"pre_read_code": [{"command": "echo custom"}]}}))
    hook_cli.setup_windsurf(global_=False, cwd=str(tmp_path))
    assert hook_cli._agent_config_paths(False, str(tmp_path))["windsurf"] == str(path)
    assert json.loads(path.read_text())["hooks"]["pre_read_code"][0]["command"] == "echo custom"
    assert not (tmp_path / ".windsurf/hooks.json").exists()


@pytest.mark.parametrize("provider", ["cursor", "claude", "codex", "copilot"])
def test_packaged_examples_match_setup_contract(provider):
    path = Path(__file__).parents[1] / "examples" / f"{provider}-hooks.example.json"
    assert set(json.loads(path.read_text())["hooks"]) == set(getattr(hook_cli, f"_{provider.upper()}_EVENTS"))


@pytest.mark.parametrize("legacy_command", [False, True])
def test_shell_copilot_registration_survives_clean_and_upgrade(tmp_path, legacy_command):
    import os
    import shutil
    from tests.test_setup_sh import _make_hook_dir, _run_setup

    hook_dir = _make_hook_dir(str(tmp_path))
    python_dir = str(Path(shutil.which("python3")).parent)
    env = {"HOME": str(tmp_path), "COPILOT_HOME": str(tmp_path / "custom-copilot"),
           "PATH": f"{python_dir}:/usr/bin:/bin"}
    path = tmp_path / "custom-copilot/hooks/otel-hooks.json"
    for index, args in enumerate((["--copilot", "--global"], ["--copilot", "--global"], ["--copilot", "--global", "--clean"])):
        result = _run_setup(hook_dir, args=args, env=env)
        assert result.returncode == 0, result.stderr
        hooks = json.loads(path.read_text())["hooks"]
        assert set(hooks) == set(hook._COPILOT_EVENTS)
        assert all(len(entries) == 1 for entries in hooks.values())
        if index == 0 and legacy_command:
            for entries in hooks.values():
                entries[0]["bash"] = entries[0]["bash"].split(" --event ")[0]
            path.write_text(json.dumps({"version": 1, "hooks": hooks}))
    result = _run_setup(hook_dir, args=["--copilot", "--global", "--uninstall"], env=env)
    assert result.returncode == 0, result.stderr
    assert not json.loads(path.read_text())["hooks"]
    assert not os.path.exists(tmp_path / ".github/hooks/otel-hooks.json")


def test_shell_codex_upgrade_enforces_short_timeouts(tmp_path):
    import shutil
    from tests.test_setup_sh import _make_hook_dir, _run_setup

    hook_dir = _make_hook_dir(str(tmp_path))
    env = {"PATH": f"{Path(shutil.which('python3')).parent}:/usr/bin:/bin"}
    path = tmp_path / ".codex/hooks.json"
    for index in range(2):
        result = _run_setup(hook_dir, args=["--codex", "--global"], env=env)
        assert result.returncode == 0, result.stderr
        doc = json.loads(path.read_text())
        for name in ("SessionEnd", "Interrupt"):
            assert doc["hooks"][name][0]["hooks"][0]["timeout"] == 3
            doc["hooks"][name][0]["hooks"][0]["timeout"] = 30
        if index == 0:
            path.write_text(json.dumps(doc))


def test_antigravity_keeps_compatibility_normalization():
    item = event("antigravity", "AfterModel", output="fixture", llm_response={"metadata": "fixture"})
    assert item.event_name == "Stop"
    assert item.to_lifecycle_data()["llm_response"] == {"metadata": "fixture"}
    assert item.conversation[0].length == 7
