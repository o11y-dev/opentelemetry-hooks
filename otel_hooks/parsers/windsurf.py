"""Windsurf hook parsing; independent of the telemetry runtime."""
from .events import _WINDSURF_EVENT_ALIASES
from .base import BaseParser
from .cursor import CursorEventAdapter


class WindsurfEventAdapter(BaseParser):
    provider = "windsurf"
    # Keep accepting legacy compatible-runner payloads, but install native names.
    event_aliases = {**CursorEventAdapter.event_aliases, **_WINDSURF_EVENT_ALIASES}

    def _normalize_provider_fields(self, event_name: str, data: dict) -> None:
        for source, target in (("trajectory_id", "session_id"), ("execution_id", "generation_id")):
            if data.get(source):
                data.setdefault(target, data[source])
        info = data.pop("tool_info", None)
        if isinstance(info, dict):
            fields = {
                "file_path": "file_path", "command_line": "command", "cwd": "cwd",
                "mcp_server_name": "mcp_server_name", "mcp_tool_name": "tool_name",
                "mcp_tool_arguments": "tool_input", "mcp_result": "tool_response", "user_prompt": "prompt",
                "response": "response", "transcript_path": "transcript_path",
                "worktree_path": "workspace_path", "root_workspace_path": "root_workspace_path",
            }
            for source, target in fields.items():
                if source in info:
                    data.setdefault(target, info[source])
        super()._normalize_provider_fields(event_name, data)
