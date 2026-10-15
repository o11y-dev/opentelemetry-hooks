"""Canonical hook field mappings shared by provider-independent telemetry."""

_EVENT_ATTR_MAP = {
    # Common
    "UserPromptSubmit": {
        "composer_mode": "gen_ai.client.composer_mode",
        "model": "gen_ai.request.model",
    },
    "SessionStart": {
        "source": "gen_ai.client.session_source", "composer_mode": "gen_ai.client.composer_mode",
        "model": "gen_ai.request.model", "agent_type": "gen_ai.client.agent_type",
    },
    "SessionEnd": {"status": "gen_ai.client.status", "reason": "gen_ai.client.session_end_reason"},
    "PreToolUse": {"tool_name": "gen_ai.client.tool_name", "tool_id": "gen_ai.client.tool_id", "tool_use_id": "gen_ai.client.tool_use_id"},
    "PostToolUse": {"tool_name": "gen_ai.client.tool_name", "tool_id": "gen_ai.client.tool_id", "tool_use_id": "gen_ai.client.tool_use_id", "duration_ms": "gen_ai.client.duration_ms"},
    "PostToolUseFailure": {"tool_name": "gen_ai.client.tool_name", "tool_id": "gen_ai.client.tool_id", "tool_use_id": "gen_ai.client.tool_use_id", "is_interrupt": "gen_ai.client.is_interrupt"},
    "PermissionRequest": {"tool_name": "gen_ai.client.tool_name", "tool_id": "gen_ai.client.tool_id", "tool_use_id": "gen_ai.client.tool_use_id", "turn_id": "gen_ai.client.turn_id"},
    "SubagentStart": {"subagent_type": "gen_ai.client.subagent_type", "agent_id": "gen_ai.client.agent_id", "parent_agent_id": "gen_ai.client.parent_agent_id", "agent_id_source": "gen_ai.client.agent_id_source", "agent_type": "gen_ai.client.agent_type"},
    "SubagentStop": {"subagent_type": "gen_ai.client.subagent_type", "status": "gen_ai.client.status", "agent_id": "gen_ai.client.agent_id", "parent_agent_id": "gen_ai.client.parent_agent_id", "agent_id_source": "gen_ai.client.agent_id_source", "agent_type": "gen_ai.client.agent_type"},
    "Stop": {"status": "gen_ai.client.status", "loop_count": "gen_ai.client.loop_count", "stop_hook_active": "gen_ai.client.stop_hook_active"},
    # Cursor-specific
    "BeforeShellExecution": {"command": "gen_ai.client.command", "cwd": "gen_ai.client.cwd"},
    "AfterShellExecution": {"command": "gen_ai.client.command", "cwd": "gen_ai.client.cwd", "exit_code": "gen_ai.client.exit_code", "duration_ms": "gen_ai.client.duration_ms"},
    "BeforeMCPExecution": {"mcp_server_name": "gen_ai.client.mcp_server", "mcp_server": "gen_ai.client.mcp_server", "mcp_tool": "gen_ai.client.mcp_tool", "tool_name": "gen_ai.client.mcp_tool", "tool_use_id": "gen_ai.client.tool_use_id"},
    "AfterMCPExecution": {"mcp_server_name": "gen_ai.client.mcp_server", "mcp_server": "gen_ai.client.mcp_server", "mcp_tool": "gen_ai.client.mcp_tool", "tool_name": "gen_ai.client.mcp_tool", "tool_use_id": "gen_ai.client.tool_use_id", "duration_ms": "gen_ai.client.duration_ms", "duration": "gen_ai.client.duration_ms", "status": "gen_ai.client.status"},
    "BeforeReadFile": {"file_path": "gen_ai.client.file_path"},
    "AfterFileEdit": {"file_path": "gen_ai.client.file_path", "edits": "gen_ai.client.edits"},
    # Copilot-specific
    "ErrorOccurred": {"error_type": "error.type", "error_code": "error.code", "is_interrupt": "gen_ai.client.is_interrupt"},
}

# Metadata-only observations shared by current harness contracts. Never flatten
# arbitrary event payloads: new hooks can carry prompt, config and MCP content.
_OBSERVATION_FIELDS = {
    "PreCompact": ("trigger", "context_usage_percent", "context_tokens", "context_window_size", "message_count", "messages_to_compact", "is_first_compaction"),
    "PostCompact": ("trigger",),
    "Notification": ("notification_type",),
    "PermissionReplied": ("permission_request_id", "permission_response"),
    "InstructionsLoaded": ("file_path", "load_reason"),
    "ConfigChange": ("source", "file_path"),
    "CwdChanged": ("cwd", "old_cwd", "new_cwd"),
    "DirectoryAdded": ("directory",),
    "WorktreeRemove": ("worktree_path",),
    "WorktreeReady": ("workspace_path", "root_workspace_path"),
    "TranscriptReady": ("transcript_path",),
    "TaskCreated": ("task_id",),
    "TaskCompleted": ("task_id",),
    "TeammateIdle": ("teammate_name", "agent_id"),
    "Elicitation": ("mcp_server_name", "mode", "elicitation_id"),
    "ElicitationResult": ("mcp_server_name", "mode", "action", "elicitation_id"),
    "PreModelSwitch": ("from_model", "to_model", "requested_model", "source", "context_tokens"),
    "PostModelSwitch": ("from_model", "to_model", "requested_model", "source", "context_tokens"),
    "AfterReadFile": ("file_path",),
    "BeforeFileEdit": ("file_path",),
    "AfterAgentThought": ("duration_ms",),
    "MessageDisplay": ("message_id", "index", "final"),
    "UserPromptExpansion": ("expansion_type", "command_name", "command_source"),
    "Interrupt": ("is_interrupt", "status"),
    "StopFailure": ("error_type", "status"),
}
for _observation_event, _observation_fields in _OBSERVATION_FIELDS.items():
    _EVENT_ATTR_MAP.setdefault(_observation_event, {}).update({
        field: f"gen_ai.client.{field}" for field in _observation_fields
    })



# Provider relationship identifiers retain their meaning; conversation IDs are
# not agent IDs, and a triggering tool call is not the subagent's invocation.
for event in ("SubagentStart", "SubagentStop"):
    _EVENT_ATTR_MAP[event].update({
        "parent_conversation_id": "gen_ai.client.parent_conversation_id",
        "tool_call_id": "gen_ai.client.parent_tool_call_id",
        "stop_hook_active": "gen_ai.client.stop_hook_active",
    })
_EVENT_ATTR_MAP["PostToolUseFailure"]["duration_ms"] = "gen_ai.client.duration_ms"
