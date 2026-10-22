"""Pure hook capture policy and bounded, typed span attribute extraction.

Filter before provider parsing and persistence. Log bodies keep original key
spelling and JSON types; only generated attribute names are normalized.
"""
import fnmatch
import hashlib
import json
import math
import re
from dataclasses import dataclass, field


def attribute_name(value: str) -> str:
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


# Exact metadata paths, not a heuristic that treats all numbers or *_id keys as
# safe. Unknown fields are available through selected_content or full capture.
_METADATA = set("""
hook_event_name event_name event type session_id conversation_id generation_id
turn_id trajectory_id execution_id event_id hook_event_id callback_id version
model model_id model_name request_model response_model provider_name provider model_provider vendor status
reason final_status trigger composer_mode tool_name tool_type tool_id tool_use_id
tool_call_id call_id subagent_id agent_id parent_agent_id parent_conversation_id parent_tool_call_id
agent_type subagent_type agent_engine client_name client_version agent_name agent_version agent_resource_id
permission_mode permission_request_id permission_response elicitation_id action mode
mcp_server mcp_server_name mcp_tool server_name tool duration duration_ms exit_code
is_interrupt stop_hook_active loop_count response_id message_id index final choice_count output_type response_format finish_reasons stop_sequences
task_id source notification_type error_type error_code from_model to_model
requested_model context_tokens context_usage_percent context_window_size
message_count messages_to_compact is_first_compaction load_reason expansion_type
command_name command_source timestamp timestamp_ms timestamp_ns prompt_cache_warm
cache_ttl estimated_cache_write_usd background_tasks session_crons
trace_id span_id parent_span_id trace_flags traceparent tracestate
native_trace_id native_span_id native_parent_span_id native_source
input_tokens prompt_tokens output_tokens completion_tokens total_tokens
cache_read_tokens cache_write_tokens cache_read_input_tokens
cache_creation_input_tokens cache_write_input_tokens cache_creation_tokens cached_input_tokens
reasoning_tokens cached_tokens usage_semantics is_partial truncated temperature top_p top_k max_tokens frequency_penalty presence_penalty seed
""".split())
_NUMERIC = set("""
duration duration_ms exit_code loop_count index context_tokens context_usage_percent
context_window_size message_count messages_to_compact timestamp_ms timestamp_ns
estimated_cache_write_usd input_tokens prompt_tokens output_tokens completion_tokens
total_tokens cache_read_tokens cache_write_tokens cache_read_input_tokens
cache_creation_input_tokens cache_write_input_tokens cache_creation_tokens cached_input_tokens reasoning_tokens cached_tokens temperature
top_p top_k max_tokens frequency_penalty presence_penalty seed
""".split())
_BOOLEAN = {"is_partial", "truncated", "is_interrupt", "stop_hook_active", "final", "is_first_compaction", "prompt_cache_warm"}
_MISSING = object()


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) or path.startswith(p + ".") for p in patterns)


@dataclass(frozen=True)
class CapturePolicy:
    mode: str = "metadata_only"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    span_include: tuple[str, ...] | None = None
    mappings: dict[str, str] = field(default_factory=dict)
    max_attributes: int = 64
    max_string_length: int = 4000
    max_depth: int = 16
    max_array_length: int = 128
    max_attribute_bytes: int = 32768
    max_log_body_bytes: int | None = None
    max_export_attempts: int = 3
    mask: bool = False
    logs_enabled: bool = False
    user_identity: bool = False

    @property
    def content_enabled(self) -> bool:
        """Content must still pass filter(); this only controls retention afterward."""
        return self.mode != "metadata_only"

    @classmethod
    def from_config(cls, settings: dict):
        config = settings.get("capture", {})
        if not isinstance(config, dict):
            raise ValueError("capture must be a JSON object")
        logs = settings.get("logs", {})
        if not isinstance(logs, dict) or ("enabled" in logs and type(logs["enabled"]) is not bool):
            raise ValueError("logs must be an object with a boolean enabled setting")
        mode = config.get("mode", "metadata_only")
        if mode not in {"metadata_only", "selected_content", "full"}:
            raise ValueError(f"invalid capture.mode: {mode!r}")
        kwargs = {"mode": mode, "logs_enabled": logs.get('enabled', False)}
        for key in ('mask', 'user_identity'):
            if key in config and type(config[key]) is not bool:
                raise ValueError(f'capture.{key} must be a boolean')
            kwargs[key] = config.get(key, False)
        for key in ("include", "exclude", "span_include"):
            if key == "span_include" and key not in config:
                continue
            value = config.get(key, [])
            if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
                raise ValueError(f"capture.{key} must be an array of field paths")
            kwargs[key] = tuple(value)
        mappings = config.get("mappings", {})
        if not isinstance(mappings, dict) or not all(
            isinstance(k, str) and k and isinstance(v, str) and all(v.split("."))
            and v == ".".join(attribute_name(p) for p in v.split("."))
            for k, v in mappings.items()
        ):
            raise ValueError("capture.mappings must map paths to lowercase attribute names")
        # Canonical identity, status and accounting semantics are runtime-owned.
        protected = ('gen_ai.', 'error.', 'o11y.', 'otel.')
        if any(v.startswith(protected) or v in {'trace_id', 'span_id', 'parent_span_id'} for v in mappings.values()):
            raise ValueError("capture.mappings cannot overwrite protected telemetry fields")
        limit = logs.get('max_body_bytes')
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("logs.max_body_bytes must be a positive integer")
        kwargs['max_log_body_bytes'] = limit
        batch = settings.get('batch', {})
        if not isinstance(batch, dict):
            raise ValueError('batch must be a JSON object')
        attempts = batch.get('max_export_attempts', 3)
        if type(attempts) is not int or attempts < 1:
            raise ValueError('batch.max_export_attempts must be a positive integer')
        kwargs['max_export_attempts'] = attempts
        kwargs["mappings"] = dict(mappings)
        for key in ("max_attributes", "max_string_length", "max_depth", "max_array_length", "max_attribute_bytes"):
            value = config.get(key, cls.__dataclass_fields__[key].default)
            if type(value) is not int or value < 1:
                raise ValueError(f"capture.{key} must be a positive integer")
            kwargs[key] = value
        return cls(**kwargs)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps({'schema_version': 3, **self.__dict__}, sort_keys=True).encode()).hexdigest()[:16]

    def filter(self, payload: dict, *, span_only: bool = False) -> dict:
        """Select permitted payload fields, or strictly select extra span fields."""
        def visit(value, path: str):
            if _matches(path, self.exclude):
                return _MISSING
            # Full logs are deliberately lossless, including deeply nested JSON.
            # Depth and size limits apply to span extraction, not log bodies.
            selected = (
                _matches(path, self.span_include or ()) if span_only else
                self.mode == "full" or (self.mode == "selected_content" and _matches(path, self.include))
            )
            if isinstance(value, dict):
                result = {}
                for key, child in value.items():
                    if key.startswith("_"):
                        # Provider private keys are data, but cannot become runtime state.
                        if not selected:
                            continue
                    child_path = f"{path}.{attribute_name(key)}" if path else attribute_name(key)
                    item = visit(child, child_path)
                    if item is not _MISSING:
                        result[key] = item
                return result if result or selected else _MISSING
            if isinstance(value, list):
                if not span_only and not selected and path in {
                    'finish_reasons', 'stop_sequences', 'metadata.finish_reasons', 'metadata.stop_sequences',
                } and all(isinstance(item, str) for item in value):
                    return [item for i, item in enumerate(value) if not _matches(f'{path}.{i}', self.exclude)]
                items = [visit(item, f"{path}.{i}") for i, item in enumerate(value)]
                if selected or any(item is not _MISSING for item in items):
                    return [None if item is _MISSING else item for item in items]
                return _MISSING
            if selected:
                return value
            if span_only:
                return _MISSING
            parts = path.split(".")
            # Known usage/request metadata containers have the same scalar schema.
            token_details = {"prompt_tokens_details", "completion_tokens_details", "input_tokens_details", "output_tokens_details"}
            permitted = len(parts) == 1 or (len(parts) == 2 and parts[0] in {"usage", "metadata", "model_params", *token_details}) or (len(parts) == 3 and parts[0] == "usage" and parts[1] in token_details)
            name = parts[-1]
            if not permitted or name not in _METADATA:
                return _MISSING
            if name in _NUMERIC:
                return value if type(value) in (int, float) and math.isfinite(value) else _MISSING
            if name in _BOOLEAN:
                return value if isinstance(value, bool) else _MISSING
            return value if isinstance(value, (str, bool, int, float)) or value is None else _MISSING

        result = visit(payload, "")
        return result if isinstance(result, dict) else {}

    def attributes(self, payload: dict) -> dict:
        """Flatten without serializing objects; canonical runtime fields win later.

        Homogeneous scalar arrays remain typed arrays. Mixed/nested arrays use
        numeric path components. Null leaves are omitted (unsupported on spans).
        Colliding normalized paths are all omitted, never silently overwritten.
        """
        if self.span_include == ():
            return {}
        if self.span_include is not None:
            payload = self.filter(payload, span_only=True)
        result, collisions = {}, set()
        used_bytes = 0
        dropped = 0

        def put(path, value):
            nonlocal used_bytes, dropped
            key = self.mappings.get(path, f"hook.{path}")
            if key in result or key in collisions:
                if key in result:
                    removed = result.pop(key)
                    used_bytes -= len(key.encode()) + len(json.dumps(removed, ensure_ascii=False).encode())
                collisions.add(key)
                dropped += 1
                return
            size = len(key.encode()) + len(json.dumps(value, ensure_ascii=False).encode())
            if len(result) >= self.max_attributes or used_bytes + size > self.max_attribute_bytes:
                dropped += 1
                return
            result[key] = value
            used_bytes += size

        def scalar(value):
            return type(value) in (str, bool, int, float) and not (isinstance(value, float) and not math.isfinite(value))

        def visit(value, path, depth):
            nonlocal dropped
            if depth > self.max_depth:
                dropped += 1
                return
            if isinstance(value, dict):
                for key in sorted(value):
                    component = attribute_name(key)
                    if component:
                        visit(value[key], f"{path}.{component}" if path else component, depth + 1)
                    else:
                        dropped += 1
            elif isinstance(value, list):
                items = value[:self.max_array_length]
                dropped += len(value) - len(items)
                if items and all(scalar(v) and type(v) is type(items[0]) for v in items):
                    dropped += sum(isinstance(v, str) and len(v) > self.max_string_length for v in items)
                    put(path, [v[:self.max_string_length] if isinstance(v, str) else v for v in items])
                else:
                    for i, item in enumerate(items):
                        visit(item, f"{path}.{i}", depth + 1)
            elif scalar(value):
                if isinstance(value, str) and len(value) > self.max_string_length:
                    value = value[:self.max_string_length]
                    dropped += 1
                put(path, value)

        visit(payload, "", 0)
        if dropped:
            result["hook.capture.dropped_fields"] = dropped
        if collisions:
            result["hook.capture.collisions"] = len(collisions)
        return result


def payload_facts(data: dict) -> dict:
    """Measure original canonical payloads without retaining their content."""
    from .parsers.common import _hash_text, _stringify
    facts = {}
    for label in ('tool_input', 'tool_output', 'tool_response', 'mcp_input', 'mcp_output'):
        if data.get(label) is not None:
            text = _stringify(data[label])
            facts[label] = {'length': len(text), 'bytes': len(text.encode('utf-8')),
                            'sha256': _hash_text(text), 'hash_basis': 'legacy_stringify_utf8',
                            'serialization_version': '1'}
            canonical = (json.dumps(data[label], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                         if isinstance(data[label], (dict, list)) else text)
            facts[label]['bytes'] = len(canonical.encode('utf-8'))
            facts[label]['fingerprint.sha256'] = _hash_text(canonical)
            facts[label]['fingerprint.basis'] = 'sorted_compact_json_utf8' if isinstance(data[label], (dict, list)) else 'utf8_text'
    return facts


def bound_runtime_payload(data: dict, policy: CapturePolicy) -> dict:
    """Bound parsed canonical previews before persistence; log bodies have their own limit.

    Observation fingerprints are computed before this operation. JSON structures
    remain valid; omitted fields and shortened previews are separately marked.
    """
    from .parsers.common import _mask_text, _safe_bool
    import os

    facts = data.get('_payload_facts') or payload_facts(data)
    used, dropped = 0, 0
    masked = policy.mask or _safe_bool(os.getenv('IDE_OTEL_MASK_PROMPTS', ''))

    def visit(value, depth):
        nonlocal used, dropped
        if depth > policy.max_depth:
            dropped += 1
            return None
        if isinstance(value, dict):
            result = {}
            # Facts and identifiers precede content in the persistence budget.
            items = sorted(value.items(), key=lambda item: (
                0 if type(item[1]) in (int, float, bool) or item[0].endswith(('id', 'sha256', 'length', 'bytes')) else 1))
            for key, child in items:
                if used >= policy.max_attribute_bytes:
                    dropped += 1
                    continue
                used += len(key.encode('utf-8'))
                if isinstance(child, str) and (key.endswith(('id', 'sha256')) or key.startswith('_hook_') or key == '_canonical_event_name'):
                    size = len(child.encode('utf-8'))
                    if used + size <= policy.max_attribute_bytes:
                        result[key] = child
                        used += size
                    else:
                        dropped += 1
                else:
                    result[key] = visit(child, depth + 1)
            return result
        if isinstance(value, list):
            dropped += max(0, len(value) - policy.max_array_length)
            return [visit(item, depth + 1) for item in value[:policy.max_array_length]]
        if isinstance(value, str):
            if len(value) > policy.max_string_length:
                dropped += 1
            value = value[:policy.max_string_length]
            if masked:
                value = _mask_text(value)
            # Bound by remaining UTF-8 bytes without splitting a code point.
            value = value.encode('utf-8')[:max(0, policy.max_attribute_bytes - used)].decode('utf-8', errors='ignore')
            used += len(value.encode('utf-8'))
        else:
            used += len(json.dumps(value).encode('utf-8'))
        return value

    exempt = {'_hook_log_body', '_hook_attributes', '_conversation_records', '_payload_facts'}
    result = visit({key: value for key, value in data.items() if key not in exempt}, 0)
    result.update({key: data[key] for key in exempt if key in data and key != '_conversation_records'})
    # Conversation facts are always retained; only the preview consumes a budget.
    if '_conversation_records' in data:
        result['_conversation_records'] = []
        for record in data['_conversation_records']:
            item = {key: value for key, value in record.items() if key != 'text'}
            if 'text' in record:
                item['text'] = visit(record['text'], 1)
                item['preview_truncated'] = item['text'] != record['text']
            result['_conversation_records'].append(item)
    result['_payload_facts'] = facts
    if dropped:
        result['_canonical_dropped_fields'] = dropped
    return result
