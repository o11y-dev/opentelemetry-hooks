"""Canonical hook field mappings shared by provider-independent telemetry."""

_EVENT_ATTR_MAP = {
    # Common
    "UserPromptSubmit": {
        "composer_mode": "o11y.client.composer_mode",
    },
    "SessionStart": {
        "source": "o11y.client.session_source", "composer_mode": "o11y.client.composer_mode",
        "agent_type": "o11y.client.agent_type",
    },
    "SessionEnd": {"status": "o11y.client.status", "reason": "o11y.client.session_end_reason"},
    "PreToolUse": {"tool_name": "o11y.client.tool_name", "tool_id": "o11y.client.tool_id", "tool_use_id": "o11y.client.tool_use_id"},
    "PostToolUse": {"tool_name": "o11y.client.tool_name", "tool_id": "o11y.client.tool_id", "tool_use_id": "o11y.client.tool_use_id", "duration_ms": "o11y.client.duration_ms"},
    "PostToolUseFailure": {"tool_name": "o11y.client.tool_name", "tool_id": "o11y.client.tool_id", "tool_use_id": "o11y.client.tool_use_id", "is_interrupt": "o11y.client.is_interrupt"},
    "PermissionRequest": {"tool_name": "o11y.client.tool_name", "tool_id": "o11y.client.tool_id", "tool_use_id": "o11y.client.tool_use_id", "turn_id": "o11y.client.turn_id"},
    "SubagentStart": {"subagent_type": "o11y.client.subagent_type", "agent_id": "o11y.client.agent_id", "parent_agent_id": "o11y.client.parent_agent_id", "agent_id_source": "o11y.client.agent_id_source", "agent_type": "o11y.client.agent_type"},
    "SubagentStop": {"subagent_type": "o11y.client.subagent_type", "status": "o11y.client.status", "agent_id": "o11y.client.agent_id", "parent_agent_id": "o11y.client.parent_agent_id", "agent_id_source": "o11y.client.agent_id_source", "agent_type": "o11y.client.agent_type"},
    "Stop": {"status": "o11y.client.status", "loop_count": "o11y.client.loop_count", "stop_hook_active": "o11y.client.stop_hook_active"},
    # Cursor-specific
    "BeforeShellExecution": {"command": "o11y.client.command", "cwd": "o11y.client.cwd"},
    "AfterShellExecution": {"command": "o11y.client.command", "cwd": "o11y.client.cwd", "exit_code": "o11y.client.exit_code", "duration_ms": "o11y.client.duration_ms"},
    "BeforeMCPExecution": {"mcp_server_name": "o11y.client.mcp_server", "mcp_server": "o11y.client.mcp_server", "mcp_tool": "o11y.client.mcp_tool", "tool_name": "o11y.client.mcp_tool", "tool_use_id": "o11y.client.tool_use_id"},
    "AfterMCPExecution": {"mcp_server_name": "o11y.client.mcp_server", "mcp_server": "o11y.client.mcp_server", "mcp_tool": "o11y.client.mcp_tool", "tool_name": "o11y.client.mcp_tool", "tool_use_id": "o11y.client.tool_use_id", "duration_ms": "o11y.client.duration_ms", "duration": "o11y.client.duration_ms", "status": "o11y.client.status"},
    "BeforeReadFile": {"file_path": "o11y.client.file_path"},
    "AfterFileEdit": {"file_path": "o11y.client.file_path", "edits": "o11y.client.edits"},
    # Copilot-specific
    "ErrorOccurred": {"error_type": "error.type", "error_code": "o11y.error.code", "is_interrupt": "o11y.client.is_interrupt"},
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
        field: f"o11y.client.{field}" for field in _observation_fields
    })



# Provider relationship identifiers retain their meaning; conversation IDs are
# not agent IDs, and a triggering tool call is not the subagent's invocation.
for event in ("SubagentStart", "SubagentStop"):
    _EVENT_ATTR_MAP[event].update({
        "parent_conversation_id": "o11y.client.parent_conversation_id",
        "tool_call_id": "o11y.client.parent_tool_call_id",
        "stop_hook_active": "o11y.client.stop_hook_active",
    })
_EVENT_ATTR_MAP["PostToolUseFailure"]["duration_ms"] = "o11y.client.duration_ms"
