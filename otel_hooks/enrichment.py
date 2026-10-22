"""Canonical span attributes from capture-permitted hook evidence.

This module has no exporter, session-state or SDK initialization responsibilities.
The runtime supplies the effective capture policy for every span.
"""
from datetime import datetime, timezone
import json
import os
from typing import Optional, Tuple

from .telemetry import AttributeBudget, usage_attributes
from .attributes import _EVENT_ATTR_MAP
from .capture import CapturePolicy
from .identity import _detect_client_version, _get_os_info, _resolve_client_name, _set_client_identity_attributes
from .parsers.base import ProviderEventAdapter
from .parsers.common import (
    _first_present, _generation_key_from_data, _hash_text, _lower_or_none,
    _mask_text, _safe_bool, _session_key, _stringify, _text_max_chars,
)
from .parsers.models import ConversationRecord
from .workspace import _workspace_observability_attributes


_HOOK_SCHEMA_VERSION = "2"

_TELEMETRY_SOURCE = "hook"

_OP_TOOL_EVENTS = {
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "PermissionRequest",
    "BeforeShellExecution", "AfterShellExecution",
    "BeforeMCPExecution", "AfterMCPExecution",
    "BeforeReadFile", "AfterFileEdit", "AfterReadFile", "BeforeFileEdit",
}

_OP_AGENT_EVENTS = {
    "SessionStart", "SessionEnd",
    "SubagentStart", "SubagentStop",
    "PreCompact", "PostCompact",
}


def _set_if_present(span, attr: str, value) -> None:
    if value is not None:
        span.set_attribute(attr, value)


def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iter_enrichment_sources(data: Optional[dict], include_tool_payloads: bool = False):
    if not isinstance(data, dict):
        return
    yield data
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        yield metadata
    user = data.get("user")
    if isinstance(user, dict):
        yield user
    if include_tool_payloads:
        for key in ("tool_input", "tool_response"):
            value = data.get(key)
            if isinstance(value, dict):
                yield value


def _coerce_attribute_value(value):
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _collect_user_identity_attributes(data: Optional[dict]) -> dict:
    if not _safe_bool(os.getenv("IDE_OTEL_CAPTURE_USER_IDENTITY", "")):
        return {}
    attrs = {}
    user_id = None
    user_email = None
    user = data.get("user") if isinstance(data, dict) else None
    if isinstance(user, dict):
        user_id = _first_present(user, ("user_id", "id", "login", "username", "user_name", "actor_id"))
        user_email = _first_present(user, ("user_email", "email"))
    sources = []
    if isinstance(data, dict):
        sources.append(data)
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            sources.append(metadata)
    for source in sources:
        if user_id is None:
            user_id = _first_present(source, ("user_id", "login", "username", "user_name", "actor_id"))
        if user_email is None:
            user_email = _first_present(source, ("user_email", "email"))
        if user_id is not None and user_email is not None:
            break
    user_id = _coerce_attribute_value(user_id)
    if user_id is not None:
        attrs["user.id"] = str(user_id)
    user_email = _coerce_attribute_value(user_email)
    if isinstance(user_email, str):
        if _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")):
            user_email = _mask_text(user_email)
        attrs["user.email"] = user_email
    return attrs


def _collect_governance_attributes(data: Optional[dict]) -> dict:
    if not isinstance(data, dict):
        return {}
    attrs = {}
    attr_map = (
        (("permission_mode",), "gen_ai.client.permission_mode"),
        (("permission_decision", "approval_decision"), "gen_ai.client.approval.decision"),
        (("approval_policy",), "gen_ai.client.approval.policy"),
        (("approval_required", "requires_approval"), "gen_ai.client.approval.required"),
        (("sandbox_mode",), "gen_ai.client.sandbox.mode"),
        (("sandbox_policy",), "gen_ai.client.sandbox.policy"),
        (("sandbox_enabled", "is_sandboxed", "sandboxed"), "gen_ai.client.sandbox.enabled"),
        (("tool_choice",), "gen_ai.client.tool_choice"),
        (("tool_decision",), "gen_ai.client.tool_decision"),
    )
    for source in _iter_enrichment_sources(data, include_tool_payloads=True):
        for keys, attr_name in attr_map:
            if attr_name in attrs:
                continue
            value = _coerce_attribute_value(_first_present(source, keys))
            if value is not None:
                attrs[attr_name] = value
    return attrs


def _native_telemetry_attributes(data: dict) -> dict:
    """Return native-agent identity only from the explicit native contract."""
    trace_id = ProviderEventAdapter._native_trace_id(data.get("native_trace_id"))
    span_id = ProviderEventAdapter._native_span_id(data.get("native_span_id"))
    parent_span_id = ProviderEventAdapter._native_span_id(data.get("native_parent_span_id"))
    attrs = {}
    if trace_id:
        attrs["gen_ai.client.native_trace_id"] = trace_id
    if span_id:
        attrs["gen_ai.client.native_span_id"] = span_id
    if parent_span_id:
        attrs["gen_ai.client.native_parent_span_id"] = parent_span_id
    if attrs:
        attrs["gen_ai.client.native_source"] = (
            data.get("native_source") or data.get("_hook_provider_adapter", "unknown")
        )
    return attrs


def _collect_event_enrichment_attributes(data: Optional[dict] = None, session_ctx: Optional[dict] = None) -> dict:
    data = data or {}
    attrs = _workspace_observability_attributes(data, session_ctx=session_ctx)
    attrs.update(_collect_user_identity_attributes(data))
    attrs.update(_collect_governance_attributes(data))
    attrs.update(_native_telemetry_attributes(data))
    attrs["gen_ai.client.telemetry_source"] = _TELEMETRY_SOURCE
    attrs["gen_ai.client.hook_schema_version"] = _HOOK_SCHEMA_VERSION
    if data.get("_hook_event_id"):
        attrs["gen_ai.client.hook.event_id"] = data["_hook_event_id"]
    if _session_key(data):
        attrs["gen_ai.client.session_id"] = _session_key(data)
    if _generation_key_from_data(data):
        attrs["gen_ai.client.generation_id"] = _generation_key_from_data(data)
    if data.get("turn_id"):
        attrs["gen_ai.client.turn_id"] = data["turn_id"]
    return attrs


def _apply_enrichment_attributes(span, data: Optional[dict] = None, session_ctx: Optional[dict] = None) -> None:
    for key, value in _collect_event_enrichment_attributes(data, session_ctx=session_ctx).items():
        _set_if_present(span, key, value)


def _normalize_genai_output_type(value) -> Optional[str]:
    normalized = _lower_or_none(value)
    if normalized in {"json_object", "json_schema"}:
        return "json"
    if normalized in {"text", "json", "image", "speech"}:
        return normalized
    return None


def _infer_genai_provider(data: dict) -> Optional[str]:
    explicit = _lower_or_none(_first_present(data, ("provider_name", "provider", "model_provider", "vendor")))
    if explicit is not None:
        if explicit == "xai":
            return "x_ai"
        return explicit

    model = _lower_or_none(_first_present(data, ("response_model", "request_model", "model", "model_name")))
    if not model:
        return None
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "text-embedding", "dall-e", "whisper")):
        return "openai"
    if model.startswith("gemini"):
        return "gcp.gemini"
    if model.startswith("mistral"):
        return "mistral_ai"
    if model.startswith("deepseek"):
        return "deepseek"
    if model.startswith("command") or model.startswith(("embed-", "rerank-")):
        return "cohere"
    if model.startswith("grok"):
        return "x_ai"
    if model.startswith("groq"):
        return "groq"
    return None


def _mcp_identity(data: dict) -> Tuple[Optional[str], Optional[str]]:
    """Return the already-normalized MCP server and tool identity."""
    return data.get("mcp_server"), data.get("mcp_tool")


def _mcp_observability_attributes(event_name: str, data: dict, *, capture_policy: CapturePolicy) -> dict:
    """Build content-safe MCP attributes from canonical lifecycle data."""
    server, tool = _mcp_identity(data)
    if not server and not tool:
        return {}
    attrs = {}
    if server:
        attrs["gen_ai.client.mcp_server"] = server
    if tool:
        attrs["gen_ai.client.mcp_tool"] = tool
    invocation_id = data.get("tool_use_id")
    if invocation_id:
        attrs["gen_ai.client.tool_use_id"] = invocation_id
    duration = _first_present(data, ("duration_ms", "duration"))
    if duration is not None:
        attrs["gen_ai.client.mcp.duration_ms"] = duration
        attrs["gen_ai.client.duration_ms"] = duration
    status = data.get("status") or data.get("mcp_status")
    if event_name == "PostToolUseFailure" or data.get("error") is not None:
        status = "error"
    elif event_name in {"PostToolUse", "AfterMCPExecution"} and not status:
        status = "success"
    if status:
        attrs["gen_ai.client.status"] = status
    if data.get("error") is not None:
        error_text = _stringify(data["error"])
        attrs["gen_ai.client.error.length"] = len(error_text)
        attrs["gen_ai.client.error.sha256"] = _hash_text(error_text)
        if capture_policy.content_enabled:
            if _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")):
                error_text = _mask_text(error_text)
            attrs["gen_ai.client.error.text"] = error_text[:_text_max_chars()]
    else:
        if data.get("error_length") is not None:
            attrs["gen_ai.client.error.length"] = data["error_length"]
        if data.get("error_sha256") is not None:
            attrs["gen_ai.client.error.sha256"] = data["error_sha256"]

    result = _first_present(data, ("result_json", "mcp_output", "tool_output", "output"))
    if result is not None:
        text = _stringify(result)
        attrs["gen_ai.client.mcp.output.length"] = len(text)
        attrs["gen_ai.client.mcp.output.sha256"] = _hash_text(text)
    else:
        if data.get("mcp_result_length") is not None:
            attrs["gen_ai.client.mcp.output.length"] = data["mcp_result_length"]
        if data.get("mcp_result_sha256") is not None:
            attrs["gen_ai.client.mcp.output.sha256"] = data["mcp_result_sha256"]
    return attrs


def _apply_mcp_attributes(span, event_name: str, data: dict, *, capture_policy: CapturePolicy) -> None:
    for key, value in _mcp_observability_attributes(event_name, data, capture_policy=capture_policy).items():
        _set_if_present(span, key, value)


def _attach_content_fact(span, label: str, text: str, *, capture: bool) -> None:
    if text is None:
        return
    span.set_attribute(f"gen_ai.client.{label}.bytes", len(text.encode("utf-8")))
    span.set_attribute(f"gen_ai.client.{label}.hash_basis", "utf8_text")
    span.set_attribute(f"gen_ai.client.{label}.serialization_version", "1")
    span.set_attribute(f"gen_ai.client.{label}.length", len(text))
    span.set_attribute(f"gen_ai.client.{label}.sha256", _hash_text(text))
    if capture:
        _attach_text(span, label, text)


def _attach_text(span, label: str, text: str) -> None:
    if _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")):
        text = _mask_text(text)
    span.set_attribute(f"gen_ai.client.{label}.text", text[:_text_max_chars()])


def _conversation_records_from_data(data: dict, *, capture_policy: CapturePolicy) -> tuple[ConversationRecord, ...]:
    records = []
    for item in data.get("_conversation_records") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        role = item.get("role")
        text = item.get("text")
        length = item.get("length")
        sha256 = item.get("sha256")
        if not isinstance(kind, str) or not isinstance(role, str):
            continue
        if isinstance(length, int) and length >= 0 and isinstance(sha256, str) and sha256:
            retained_text = text if isinstance(text, str) and capture_policy.content_enabled else None
            records.append(ConversationRecord(kind, role, retained_text, length, sha256, item.get("bytes")))
        elif isinstance(text, str) and text:
            records.append(ConversationRecord.from_text(kind, role, text, capture=capture_policy.content_enabled))
    return tuple(records)


def _apply_conversation_attributes(span, event_name: str, data: dict, *, capture_policy: CapturePolicy) -> None:
    records = _conversation_records_from_data(data, capture_policy=capture_policy)
    if not records:
        records = tuple(ProviderEventAdapter(capture_policy=capture_policy)._conversation_records(event_name, data))
    if records:
        span.set_attribute("gen_ai.client.message.types", [record.kind for record in records])
        span.set_attribute("gen_ai.client.message.roles", [record.role for record in records])
    for record in records:
        _set_if_present(span, f"gen_ai.client.{record.kind}.bytes", record.bytes)
        span.set_attribute(f"gen_ai.client.{record.kind}.hash_basis", "utf8_text")
        span.set_attribute(f"gen_ai.client.{record.kind}.serialization_version", "1")
        if record.text is not None:
            span.set_attribute(f"gen_ai.client.{record.kind}.preview_truncated", record.length > min(_text_max_chars(), capture_policy.max_string_length))
        span.set_attribute(f"gen_ai.client.{record.kind}.length", record.length)
        span.set_attribute(f"gen_ai.client.{record.kind}.sha256", record.sha256)
        if capture_policy.content_enabled and record.text is not None:
            _attach_text(span, record.kind, record.text)

    # Both message formats use the same records and preserve the original facts
    # while masking and bounding only their captured text.
    if capture_policy.content_enabled:
        prompt = next((record.text for record in records if record.kind == "prompt"), None)
        response = next((record.text for record in records if record.kind == "response"), None)
        prompt = prompt or (data.get("prompt") if isinstance(data.get("prompt"), str) else None)
        response = response or (data.get("response") if isinstance(data.get("response"), str) else None)
        if _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")):
            prompt = _mask_text(prompt) if prompt else None
            response = _mask_text(response) if response else None
        max_chars = _text_max_chars()
        prompt = prompt[:max_chars] if prompt else None
        response = response[:max_chars] if response else None
        inp_msg, out_msg = _genai_messages(prompt, response)
        _set_if_present(span, "gen_ai.input.messages", inp_msg)
        _set_if_present(span, "gen_ai.output.messages", out_msg)

    error = data.get("error")
    if isinstance(error, dict):
        _set_if_present(span, "error.type", _first_present(error, ("type", "error_type", "name")))
        _set_if_present(span, "error.code", _first_present(error, ("code", "error_code")))
    _set_if_present(span, "error.type", data.get("error_type"))
    _set_if_present(span, "error.code", data.get("error_code"))
    if event_name in {"ErrorOccurred", "PostToolUseFailure"} or error is not None:
        span.set_attribute(
            "gen_ai.client.status",
            "interrupted" if data.get("is_interrupt") else "error",
        )


def _genai_operation(event_name: str) -> str:
    if event_name in _OP_TOOL_EVENTS:
        return "execute_tool"
    if event_name in _OP_AGENT_EVENTS:
        return "invoke_agent"
    if event_name in {"UserPromptSubmit", "Stop", "BeforeModel", "AfterModel", "BeforeAgent", "AfterAgent"}:
        return "chat"
    return None


def _genai_messages(
    prompt: Optional[str], response: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    inp = None
    out = None
    if prompt:
        inp = json.dumps([{"role": "user", "parts": [{"type": "text", "content": prompt}]}], ensure_ascii=True)
    if response:
        out = json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": response}]}], ensure_ascii=True)
    return inp, out


def _apply_genai_semconv(span, event_name: str, data: dict, ide: str, session_ctx: Optional[dict] = None, batch_model: Optional[str] = None, *, capture_policy: CapturePolicy) -> None:
    provider = _infer_genai_provider(data)
    if provider is not None:
        span.set_attribute("gen_ai.provider.name", provider)
    span.set_attribute("gen_ai.system", _resolve_client_name(ide, data=data, session_ctx=session_ctx))
    _set_if_present(span, "gen_ai.operation.name", _genai_operation(event_name))
    if event_name in _OP_TOOL_EVENTS:
        # Keep gen_ai.client.tool_name / mcp_tool for existing consumers and
        # also publish the standard identity used by GenAI backends.
        _set_if_present(span, "gen_ai.tool.name", _first_present(data, ("tool_name", "mcp_tool")))
        _set_if_present(span, "gen_ai.tool.type", data.get("tool_type"))
    _set_if_present(span, "gen_ai.conversation.id", data.get("conversation_id") or data.get("session_id"))
    _set_if_present(
        span,
        "gen_ai.agent.id",
        _first_present(data, ("agent_id",)) or (session_ctx or {}).get("root_agent_id"),
    )
    _set_if_present(span, "gen_ai.agent.name", _first_present(data, ("agent_name", "subagent_type", "agent_type")))
    _set_if_present(span, "gen_ai.agent.version", _first_present(data, ("agent_version",)))
    _set_if_present(span, "gen_ai.agent.description", _first_present(data, ("agent_description",)))

    # Model attribution with fallback chain (Fix A, B, C)
    model = _first_present(data, ("request_model", "model", "model_name"))
    if not model and session_ctx:
        model = session_ctx.get("last_known_model")
    if not model:
        model = batch_model
    if not model:
        model = os.getenv("CLAUDE_MODEL") or os.getenv("ANTHROPIC_MODEL")

    if model:
        model_basis = ('provider' if _first_present(data, ('request_model', 'model', 'model_name'))
                       else 'session' if session_ctx and session_ctx.get('last_known_model')
                       else 'generation' if batch_model else 'configuration')
        span.set_attribute('o11y.model.basis', model_basis)
    _set_if_present(span, "gen_ai.request.model", model)
    _set_if_present(span, "gen_ai.response.model", _first_present(data, ("response_model",)))
    _set_if_present(span, "gen_ai.request.choice.count", _int_or_none(_first_present(data, ("choice_count",))))
    _set_if_present(
        span,
        "gen_ai.output.type",
        _normalize_genai_output_type(_first_present(data, ("output_type", "response_format"))),
    )

    for key, value in usage_attributes(data, provider).items():
        span.set_attribute(key, value)

    # Request params
    for source in (data, data.get("metadata") or {}):
        _set_if_present(span, "gen_ai.request.temperature", _float_or_none(_first_present(source, ("temperature",))))
        _set_if_present(span, "gen_ai.request.top_p", _float_or_none(_first_present(source, ("top_p",))))
        top_k = _first_present(source, ("top_k",))
        if type(top_k) is int and top_k >= 0:
            span.set_attribute("gen_ai.request.top_k", top_k)
        _set_if_present(span, "gen_ai.request.max_tokens", _int_or_none(_first_present(source, ("max_tokens",))))
        _set_if_present(span, "gen_ai.request.frequency_penalty", _float_or_none(_first_present(source, ("frequency_penalty",))))
        _set_if_present(span, "gen_ai.request.presence_penalty", _float_or_none(_first_present(source, ("presence_penalty",))))
        _set_if_present(span, "gen_ai.request.seed", _int_or_none(_first_present(source, ("seed",))))
        _set_if_present(span, "gen_ai.response.id", _first_present(source, ("response_id",)))
        finish = _first_present(source, ("finish_reasons",))
        if finish is not None:
            span.set_attribute("gen_ai.response.finish_reasons", finish if isinstance(finish, list) else [str(finish)])
        stop_seq = _first_present(source, ("stop_sequences",))
        if stop_seq is not None:
            span.set_attribute("gen_ai.request.stop_sequences", stop_seq if isinstance(stop_seq, list) else [str(stop_seq)])

    # Tool definitions (opt-in)
    tool_defs = _first_present(data, ("tool_definitions", "tools", "tool_schema"))
    if tool_defs is not None and capture_policy.content_enabled:
        span.set_attribute("gen_ai.tool.definitions", _stringify(tool_defs))

    if capture_policy.content_enabled:
        system_instructions = _first_present(data, ("system_instructions", "system_prompt"))
        if system_instructions is not None:
            _set_if_present(span, "gen_ai.system_instructions", _stringify(system_instructions))


def _set_tool_payload_attributes(span, event_name: str, data: dict) -> None:
    """Attach canonical facts from payloads already permitted by capture policy."""
    if event_name not in _OP_TOOL_EVENTS:
        return
    tool_input = data.get("tool_input")
    if isinstance(tool_input, dict):
        for source, target in (("command", "command"), ("description", "approval.description"),
                               ("file_path", "tool.input.file_path"), ("path", "tool.input.path")):
            value = tool_input.get(source)
            if isinstance(value, str):
                span.set_attribute(f"gen_ai.client.{target}", value[:_text_max_chars()])
    for source, target in (("tool_input", "input"), ("tool_response", "response")):
        value = data.get(source)
        facts = (data.get("_payload_facts") or {}).get(source, {})
        length = _int_or_none(data.get(f"{source}_length", facts.get("length")))
        digest = data.get(f"{source}_sha256") or facts.get("sha256")
        if value is not None and (length is None or not digest):
            text = _stringify(value)
            length, digest = len(text), _hash_text(text)
        _set_if_present(span, f"gen_ai.client.tool.{target}.length", length)
        _set_if_present(span, f"gen_ai.client.tool.{target}.sha256", digest)


def _apply_memory_summary_attrs(span, prefix: str, summary: dict) -> None:
    """Attach memory summary attributes to a span."""
    files = summary.get("files") or []
    tools = summary.get("tools") or []
    entities = summary.get("entities") or []
    commands = summary.get("commands") or []
    tool_counts = summary.get("tool_counts") or {}

    span.set_attribute(f"{prefix}.files_touched_count", len(files))
    span.set_attribute(f"{prefix}.tools_used_count", len(tools))
    span.set_attribute(f"{prefix}.entities_count", len(entities))
    span.set_attribute(f"{prefix}.commands_count", len(commands))

    if files:
        span.set_attribute(f"{prefix}.files_touched", files)
    if tools:
        span.set_attribute(f"{prefix}.tools_used", tools)
    if entities:
        span.set_attribute(f"{prefix}.entities", entities)
    if commands:
        span.set_attribute(f"{prefix}.commands", commands)
    if tool_counts:
        span.set_attribute(f"{prefix}.tool_counts", json.dumps(tool_counts, ensure_ascii=True, sort_keys=True))



def populate_span(span, event_name: str, data: dict, ide: str, session_ctx: Optional[dict] = None, batch_model: Optional[str] = None, *, capture_policy: CapturePolicy, attach_os_attributes: bool = False) -> None:
    """Attach bounded hook fields and canonical attributes in priority order."""
    span = AttributeBudget(span, capture_policy)
    span.set_attribute('o11y.span.role', 'callback')
    _set_client_identity_attributes(span, ide, data=data, session_ctx=session_ctx)
    _apply_enrichment_attributes(span, data, session_ctx=session_ctx)

    # Optionally attach OS / host attributes on every span.
    # These are already present as resource attributes via OTEL_RESOURCE_ATTRIBUTES,
    # so we gate per-span duplication behind a flag to avoid hot-path overhead.
    if attach_os_attributes:
        os_info = _get_os_info()
        for attr_key, attr_val in os_info.items():
            span.set_attribute(attr_key, attr_val)

    # Client version
    client_version = _detect_client_version(data, ide)
    _set_if_present(span, "gen_ai.client.version", client_version)

    observed = data.get('_hook_observed_time_ns')
    span.set_attribute("gen_ai.client.timestamp", (
        datetime.fromtimestamp(observed / 1e9, timezone.utc).isoformat()
        if observed is not None else datetime.now(timezone.utc).isoformat()
    ))
    _set_if_present(span, 'o11y.time.observed_ns', observed)
    _set_if_present(span, 'o11y.time.event_ns', data.get('_hook_event_time_ns'))
    span.set_attribute('o11y.capture.mode', capture_policy.mode)
    span.set_attribute('o11y.capture.policy_id', capture_policy.fingerprint)

    # GenAI semantic conventions
    _apply_genai_semconv(span, event_name, data, ide, session_ctx=session_ctx, batch_model=batch_model, capture_policy=capture_policy)

    # Event-specific attributes
    mapping = _EVENT_ATTR_MAP.get(event_name, {})
    for key, attr in mapping.items():
        _set_if_present(span, attr, data.get(key))
    _apply_mcp_attributes(span, event_name, data, capture_policy=capture_policy)
    _set_tool_payload_attributes(span, event_name, data)
    _apply_conversation_attributes(span, event_name, data, capture_policy=capture_policy)

    for label in ("tool_input", "tool_output", "mcp_input", "mcp_output"):
        value = data.get(label)
        if value is not None and not isinstance(value, dict):
            if label not in (data.get("_payload_facts") or {}):
                _attach_content_fact(span, label, _stringify(value), capture=capture_policy.content_enabled)
            elif capture_policy.content_enabled:
                _attach_text(span, label, _stringify(value))

    # Attach correlation last so large content cannot evict it from bounded
    # SDK attribute storage. Protected mappings cannot replace these facts.
    span.set_attribute("gen_ai.client.hook.event", event_name)
    span.set_attribute("gen_ai.client.telemetry_source", _TELEMETRY_SOURCE)
    span.set_attribute("gen_ai.client.hook_schema_version", _HOOK_SCHEMA_VERSION)
    _set_if_present(span, "gen_ai.client.hook.event_id", data.get("_hook_event_id"))
    _set_if_present(span, "gen_ai.client.hook.event_id_source", data.get("_hook_event_id_source"))
    _set_if_present(span, "gen_ai.client.hook.original_event", data.get("_hook_original_event"))
    _set_if_present(span, "gen_ai.client.hook.provider_adapter", data.get("_hook_provider_adapter"))
    _set_if_present(
        span,
        "gen_ai.client.mcp.correlated_evidence",
        data.get("_mcp_correlated_evidence"),
    )
    _set_if_present(span, "gen_ai.client.session_id", data.get("session_id") or data.get("conversation_id"))
    _set_if_present(span, "gen_ai.client.generation_id", data.get("generation_id"))
    _set_if_present(span, "gen_ai.client.turn_id", data.get("turn_id"))

    for label, facts in (data.get('_payload_facts') or {}).items():
        for fact, value in facts.items():
            span.set_attribute(f'gen_ai.client.{label}.{fact}', value)
        alias = {'tool_input': 'tool.input', 'tool_response': 'tool.response'}.get(label)
        if alias:
            for fact in ('length', 'sha256'):
                _set_if_present(span, f'gen_ai.client.{alias}.{fact}', facts.get(fact))
    _set_if_present(span, 'o11y.capture.canonical_dropped_fields', data.get('_canonical_dropped_fields'))
    _set_if_present(span, 'gen_ai.tool.call.id', data.get('tool_use_id') or data.get('tool_call_id') or data.get('call_id'))
    if type(data.get('is_partial')) is bool:
        span.set_attribute('o11y.result.completeness', 'partial' if data['is_partial'] else 'complete')
    if type(data.get('truncated')) is bool:
        span.set_attribute('o11y.result.provider_truncated', data['truncated'])
    span.finish(data.get('_hook_attributes') or {})
