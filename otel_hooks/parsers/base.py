"""Base hook parsing; independent of the telemetry runtime."""
from typing import Optional
import json
import hashlib
from .events import (
    _INPUT_ALIASES,
    _GENERATION_START_EVENTS,
    _TRACE_ID_RE,
    _SPAN_ID_RE,
)
from .common import (
    _stringify,
    _first_present,
    _normalize_input_data,
    _bound_tool_response_payload,
    _hash_text,
    _conversation_content_enabled,
    _session_key,
    _generation_key_from_data,
    _normalize_mcp_event_data,
)
from .models import (
    ConversationRecord,
    AgentRelationship,
    WorkspaceIdentity,
    NativeTelemetryContext,
    CanonicalHookEvent,
)


class BaseParser:
    """Translate one provider payload into the canonical hook event model."""

    provider = "unknown"
    event_aliases: dict[str, str] = {}
    response_fields = ("response", "last_assistant_message", "assistant_response")
    stop_message_fields = ("stop_message",)
    # Text facts are bounded and hash-only unless conversation capture is enabled.
    event_text_fields = {
        "AfterAgentResponse": ("response", "assistant", ("text",)),
        "AfterAgentThought": ("reasoning", "assistant", ("text",)),
        "MessageDisplay": ("response.chunk", "assistant", ("delta",)),
        "Notification": ("notification", "system", ("message",)),
        "UserPromptTransformed": ("prompt.transformed", "user", ("transformedPrompt", "transformed_prompt", "prompt")),
        "UserPromptExpansion": ("prompt.expanded", "user", ("prompt", "command_args")),
        "StopFailure": ("error", "error", ("last_assistant_message", "error_details", "error")),
        "SubagentStop": ("response", "assistant", ("response", "last_assistant_message")),
        "TaskCreated": ("task", "system", ("task_subject", "task_description")),
        "TaskCompleted": ("task", "system", ("task_subject", "task_description")),
        "PreCompact": ("compaction.instructions", "user", ("custom_instructions",)),
        "PostCompact": ("compaction.summary", "system", ("compact_summary",)),
    }

    def parse(self, event_name: str, data: dict, session_ctx: Optional[dict] = None) -> CanonicalHookEvent:
        """Parse one callback without initializing telemetry or writing state."""
        return self.normalize(event_name, None, data, session_ctx=session_ctx)

    def canonical_event_name(self, original_event_name: str) -> str:
        """Translate the provider callback name into the shared event vocabulary."""
        return self.event_aliases.get(original_event_name, original_event_name)

    def normalize(
        self,
        original_event_name: str,
        event_name: Optional[str],
        data: dict,
        session_ctx: Optional[dict] = None,
    ) -> CanonicalHookEvent:
        event_name = event_name or self.canonical_event_name(original_event_name)
        normalized = dict(_normalize_input_data(data))
        self._normalize_provider_fields(event_name, normalized)
        conversation = tuple(self._conversation_records(event_name, normalized))
        event_id, event_id_source = self._event_identity(event_name, normalized, conversation, session_ctx)
        normalized["_hook_event_id"] = event_id
        normalized["_hook_event_id_source"] = event_id_source
        normalized["_hook_original_event"] = original_event_name
        normalized["_hook_provider_adapter"] = self.provider
        normalized["_canonical_event_name"] = event_name

        task = _first_present(normalized, ("delegation_task", "subagent_task", "task"))
        if task is not None and not isinstance(task, str):
            task = _stringify(task)
        task_text = task if isinstance(task, str) else None
        relationship = AgentRelationship(
            agent_id=self._text_value(normalized.get("agent_id")),
            parent_agent_id=self._text_value(normalized.get("parent_agent_id")),
            task=task_text if task_text and _conversation_content_enabled() else None,
            task_length=len(task_text) if task_text else 0,
            task_sha256=_hash_text(task_text) if task_text else "",
            status=self._text_value(normalized.get("status")),
        )
        workspace = WorkspaceIdentity(
            workspace_path=self._text_value(normalized.get("workspace_path")),
            cwd=self._text_value(normalized.get("cwd")),
        )
        native = NativeTelemetryContext(
            source=self._text_value(normalized.get("native_source")) or (
                self.provider if self._native_trace_id(normalized.get("native_trace_id")) else None
            ),
            trace_id=self._native_trace_id(normalized.get("native_trace_id")),
            span_id=self._native_span_id(normalized.get("native_span_id")),
            parent_span_id=self._native_span_id(normalized.get("native_parent_span_id")),
        )
        self._store_privacy_safe_conversation(event_name, normalized, conversation)
        if not _conversation_content_enabled():
            for source, target in _INPUT_ALIASES.items():
                if target not in normalized:
                    normalized.pop(source, None)
        _bound_tool_response_payload(normalized)
        return CanonicalHookEvent(
            provider=self.provider,
            original_event_name=original_event_name,
            event_name=event_name,
            event_id=event_id,
            event_id_source=event_id_source,
            original_tool_name=self._text_value(normalized.get("tool_name")),
            session_id=_session_key(normalized),
            generation_id=_generation_key_from_data(normalized),
            turn_id=self._text_value(normalized.get("turn_id")),
            conversation=conversation,
            relationship=relationship,
            workspace=workspace,
            native=native,
            lifecycle_data=normalized,
        )

    def _normalize_provider_fields(self, event_name: str, data: dict) -> None:
        """Normalize shared encoded MCP syntax before lifecycle processing."""
        data.update(_normalize_mcp_event_data(data, event_name, self.provider))

    def _conversation_records(self, event_name: str, data: dict):
        if event_name in self.event_text_fields:
            kind, role, fields = self.event_text_fields[event_name]
            text = self._first_text(data, fields)
            if text:
                yield ConversationRecord.from_text(kind, role, text)
        if event_name == "UserPromptSubmit":
            prompt = self._first_text(data, ("prompt", "user_prompt", "input"))
            if prompt:
                yield ConversationRecord.from_text("prompt", "user", prompt)
        if event_name == "Stop":
            response = self._first_text(data, self.response_fields)
            if response:
                yield ConversationRecord.from_text("response", "assistant", response)
            stop_message = self._first_text(data, self.stop_message_fields)
            if stop_message and stop_message != response:
                yield ConversationRecord.from_text("stop_message", "system", stop_message)
        if event_name in {"ErrorOccurred", "PostToolUseFailure"} or data.get("error") is not None:
            error = self._first_text(data, ("error_message", "error"))
            if error:
                yield ConversationRecord.from_text("error", "error", error)
        if event_name in {"SubagentStart", "SubagentStop"}:
            task = self._first_text(data, ("delegation_task", "subagent_task", "task"))
            if task:
                yield ConversationRecord.from_text("delegation.task", "system", task)

    def _store_privacy_safe_conversation(
        self,
        event_name: str,
        data: dict,
        conversation: tuple[ConversationRecord, ...],
    ) -> None:
        if conversation:
            capture = _conversation_content_enabled()
            data["_conversation_records"] = [
                {
                    "kind": record.kind,
                    "role": record.role,
                    "length": record.length,
                    "sha256": record.sha256,
                    **({"text": record.text} if capture and record.text is not None else {}),
                }
                for record in conversation
            ]
        if _conversation_content_enabled():
            return

        if event_name in self.event_text_fields:
            for key in self.event_text_fields[event_name][2]:
                data.pop(key, None)
        if event_name == "Notification":
            data.pop("details", None)
        if event_name in {"Elicitation", "ElicitationResult"}:
            for key in ("message", "requested_schema", "requestedSchema", "content"):
                data.pop(key, None)

        if event_name == "UserPromptSubmit":
            for key in ("prompt", "user_prompt", "input"):
                data.pop(key, None)
        if event_name == "Stop":
            for key in (*self.response_fields, *self.stop_message_fields):
                data.pop(key, None)
        if event_name in {"ErrorOccurred", "PostToolUseFailure", "AfterMCPExecution"} or data.get("error") is not None:
            error = data.get("error")
            if isinstance(error, dict):
                data.setdefault("error_type", _first_present(error, ("type", "error_type", "name")))
                data.setdefault("error_code", _first_present(error, ("code", "error_code")))
            error_record = next((item for item in conversation if item.kind == "error"), None)
            if error_record:
                data["error_length"] = error_record.length
                data["error_sha256"] = error_record.sha256
                data.setdefault("status", "error")
            data.pop("error", None)
            data.pop("error_message", None)
        if event_name in {"SubagentStart", "SubagentStop"}:
            task_record = next((item for item in conversation if item.kind == "delegation.task"), None)
            if task_record:
                data["delegation_task_length"] = task_record.length
                data["delegation_task_sha256"] = task_record.sha256
            for key in ("delegation_task", "subagent_task", "task"):
                data.pop(key, None)

    def _event_identity(
        self,
        event_name: str,
        data: dict,
        conversation: tuple[ConversationRecord, ...],
        session_ctx: Optional[dict],
    ) -> tuple[str, str]:
        provided = _first_present(data, ("event_id", "hook_event_id", "callback_id"))
        if provided is not None and str(provided).strip():
            return str(provided).strip(), "provider"
        task = _first_present(data, ("delegation_task", "subagent_task", "task"))
        error = _first_present(data, ("error_message", "error"))
        payload = {
            "provider": self.provider,
            "event": event_name,
            "session": _session_key(data),
            "generation": _generation_key_from_data(data) or (
                None if event_name in _GENERATION_START_EVENTS else (session_ctx or {}).get("current_generation")
            ),
            "turn": data.get("turn_id"),
            "tool_use_id": data.get("tool_use_id"),
            "tool_name": data.get("tool_name"),
            "agent_id": data.get("agent_id"),
            "agent_type": _first_present(data, ("subagent_type", "agent_type", "agent_name")),
            "task_sha256": _hash_text(_stringify(task)) if task is not None else None,
            "error_sha256": _hash_text(_stringify(error)) if error is not None else None,
            "status": data.get("status"),
            "timestamp": _first_present(data, ("timestamp_ns", "timestamp", "event_time")),
            "conversation": [(item.kind, item.sha256) for item in conversation],
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return f"hook:{digest}", "hook"

    @staticmethod
    def _first_text(data: dict, keys: tuple[str, ...]) -> Optional[str]:
        value = _first_present(data, keys)
        if value is None:
            return None
        return value if isinstance(value, str) else _stringify(value)

    @staticmethod
    def _text_value(value) -> Optional[str]:
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()

    @staticmethod
    def _native_trace_id(value) -> Optional[str]:
        value = str(value).strip().lower() if value is not None else ""
        return value if _TRACE_ID_RE.fullmatch(value) and int(value, 16) else None

    @staticmethod
    def _native_span_id(value) -> Optional[str]:
        value = str(value).strip().lower() if value is not None else ""
        return value if _SPAN_ID_RE.fullmatch(value) and int(value, 16) else None


# Compatibility name for integrations using the original adapter API.
ProviderEventAdapter = BaseParser
