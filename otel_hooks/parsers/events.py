"""Events hook parsing; independent of the telemetry runtime."""
import re


_CANONICAL_EVENT = {
    # Cursor camelCase
    "sessionStart": "SessionStart",
    "sessionEnd": "SessionEnd",
    "beforeSubmitPrompt": "UserPromptSubmit",
    "preToolUse": "PreToolUse",
    "postToolUse": "PostToolUse",
    "postToolUseFailure": "PostToolUseFailure",
    "beforeShellExecution": "BeforeShellExecution",
    "afterShellExecution": "AfterShellExecution",
    "beforeMCPExecution": "BeforeMCPExecution",
    "afterMCPExecution": "AfterMCPExecution",
    "beforeReadFile": "BeforeReadFile",
    "afterFileEdit": "AfterFileEdit",
    "subagentStart": "SubagentStart",
    "subagentStop": "SubagentStop",
    "stop": "Stop",
    "preCompact": "PreCompact",
    "beforeTabFileRead": "BeforeReadFile",
    "afterTabFileEdit": "AfterFileEdit",
    "afterAgentResponse": "AfterAgentResponse",
    "afterAgentThought": "AfterAgentThought",
    "workspaceOpen": "WorkspaceOpen",
    # Copilot camelCase
    "userPromptSubmitted": "UserPromptSubmit",
    "errorOccurred": "ErrorOccurred",
    "agentStop": "Stop",
    "permissionRequest": "PermissionRequest",
    "notification": "Notification",
    "userPromptTransformed": "UserPromptTransformed",
    # Gemini CLI
    "BeforeModel": "UserPromptSubmit",
    "AfterModel": "Stop",
    "BeforeTool": "PreToolUse",
    "AfterTool": "PostToolUse",
    "BeforeAgent": "SubagentStart",
    "AfterAgent": "SubagentStop",
    # Codex
    "PermissionRequest": "PermissionRequest",
}


_COPILOT_EVENTS = [
    "sessionStart", "sessionEnd", "userPromptSubmitted",
    "preToolUse", "postToolUse", "errorOccurred",
    "postToolUseFailure", "agentStop", "subagentStart", "subagentStop",
    "preCompact", "permissionRequest", "notification", "userPromptTransformed",
]


_WINDSURF_EVENT_ALIASES = {
    "pre_read_code": "BeforeReadFile", "post_read_code": "AfterReadFile",
    "pre_write_code": "BeforeFileEdit", "post_write_code": "AfterFileEdit",
    "pre_run_command": "BeforeShellExecution", "post_run_command": "AfterShellExecution",
    "pre_mcp_tool_use": "BeforeMCPExecution", "post_mcp_tool_use": "AfterMCPExecution",
    "pre_user_prompt": "UserPromptSubmit", "post_cascade_response": "Stop",
    "post_cascade_response_with_transcript": "TranscriptReady",
    "post_setup_worktree": "WorktreeReady",
}


_INPUT_ALIASES = {
    "sessionId": "session_id",
    "conversationId": "conversation_id",
    "generationId": "generation_id",
    "transcriptPath": "transcript_path",
    "providerName": "provider_name",
    "requestModel": "request_model",
    "responseModel": "response_model",
    "modelName": "model_name",
    "toolName": "tool_name",
    "toolInput": "tool_input",
    "toolOutput": "tool_output",
    "toolType": "tool_type",
    "toolDefinitions": "tool_definitions",
    "toolUseId": "tool_use_id",
    "toolId": "tool_id",
    "mcpServerName": "mcp_server_name",
    "mcpServer": "mcp_server",
    "mcpTool": "mcp_tool",
    "turnId": "turn_id",
    "toolResponse": "tool_response",
    "lastAssistantMessage": "last_assistant_message",
    "stopMessage": "stop_message",
    "errorMessage": "error_message",
    "errorType": "error_type",
    "errorCode": "error_code",
    "eventId": "event_id",
    "hookEventId": "hook_event_id",
    "agentId": "agent_id",
    "parentAgentId": "parent_agent_id",
    "agentName": "agent_name",
    "agentVersion": "agent_version",
    "agentDescription": "agent_description",
    "agentType": "agent_type",
    "subagentType": "subagent_type",
    "subagentTask": "subagent_task",
    "delegationTask": "delegation_task",
    "responseFormat": "response_format",
    "outputType": "output_type",
    "choiceCount": "choice_count",
    "systemInstructions": "system_instructions",
    "systemPrompt": "system_prompt",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "workspacePath": "workspace_path",
    "filePath": "file_path",
    "userId": "user_id",
    "userEmail": "user_email",
    "userName": "user_name",
    "permissionMode": "permission_mode",
    "permissionDecision": "permission_decision",
    "approvalPolicy": "approval_policy",
    "approvalDecision": "approval_decision",
    "approvalRequired": "approval_required",
    "sandboxMode": "sandbox_mode",
    "sandboxPolicy": "sandbox_policy",
    "sandboxEnabled": "sandbox_enabled",
    "isSandboxed": "is_sandboxed",
    "toolChoice": "tool_choice",
    "toolDecision": "tool_decision",
    "exitCode": "exit_code",
    "durationMs": "duration_ms",
    "loopCount": "loop_count",
    "stopHookActive": "stop_hook_active",
    "isInterrupt": "is_interrupt",
    "hookEventType": "hook_event_type",
    "clientVersion": "client_version",
    "ideVersion": "ide_version",
    "appVersion": "app_version",
    "ideName": "ide_name",
    "sourceApp": "source_app",
    "traceId": "trace_id",
    "spanId": "span_id",
    "parentSpanId": "parent_span_id",
    "nativeTraceId": "native_trace_id",
    "nativeSpanId": "native_span_id",
    "nativeParentSpanId": "native_parent_span_id",
    "nativeSource": "native_source",
    "traceFlags": "trace_flags",
    "traceState": "tracestate",
}


_GENERATION_START_EVENTS = {"UserPromptSubmit"}


_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")


_MCP_EVENTS = {"BeforeMCPExecution", "AfterMCPExecution"}
