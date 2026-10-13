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
model model_id model_name request_model response_model provider_name status
reason final_status trigger composer_mode tool_name tool_type tool_id tool_use_id
tool_call_id call_id subagent_id agent_id parent_agent_id parent_conversation_id parent_tool_call_id
agent_type subagent_type agent_engine client_name client_version agent_version
permission_mode permission_request_id permission_response elicitation_id action mode
mcp_server mcp_server_name mcp_tool server_name tool duration duration_ms exit_code
is_interrupt stop_hook_active loop_count response_id message_id index final
task_id source notification_type error_type error_code from_model to_model
requested_model context_tokens context_usage_percent context_window_size
message_count messages_to_compact is_first_compaction load_reason expansion_type
command_name command_source timestamp timestamp_ms timestamp_ns prompt_cache_warm
cache_ttl estimated_cache_write_usd background_tasks session_crons
trace_id span_id parent_span_id trace_flags traceparent tracestate
native_trace_id native_span_id native_parent_span_id native_source
input_tokens prompt_tokens output_tokens completion_tokens total_tokens
cache_read_tokens cache_write_tokens cache_read_input_tokens
cache_creation_input_tokens cache_creation_tokens cached_input_tokens
temperature top_p top_k max_tokens frequency_penalty presence_penalty seed
""".split())
_NUMERIC = set("""
duration duration_ms exit_code loop_count index context_tokens context_usage_percent
context_window_size message_count messages_to_compact timestamp_ms timestamp_ns
estimated_cache_write_usd input_tokens prompt_tokens output_tokens completion_tokens
total_tokens cache_read_tokens cache_write_tokens cache_read_input_tokens
cache_creation_input_tokens cache_creation_tokens cached_input_tokens temperature
top_p top_k max_tokens frequency_penalty presence_penalty seed
""".split())
_BOOLEAN = {"is_interrupt", "stop_hook_active", "final", "is_first_compaction", "prompt_cache_warm"}
_MISSING = object()


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) or path.startswith(p + ".") for p in patterns)


@dataclass(frozen=True)
class CapturePolicy:
    mode: str = "metadata_only"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    mappings: dict[str, str] = field(default_factory=dict)
    max_attributes: int = 64
    max_string_length: int = 4000
    max_depth: int = 16
    max_array_length: int = 128
    max_attribute_bytes: int = 32768

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
        kwargs = {"mode": mode}
        for key in ("include", "exclude"):
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
        kwargs["mappings"] = dict(mappings)
        for key in ("max_attributes", "max_string_length", "max_depth", "max_array_length", "max_attribute_bytes"):
            value = config.get(key, cls.__dataclass_fields__[key].default)
            if type(value) is not int or value < 1:
                raise ValueError(f"capture.{key} must be a positive integer")
            kwargs[key] = value
        return cls(**kwargs)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True).encode()).hexdigest()[:16]

    def filter(self, payload: dict) -> dict:
        def visit(value, path: str):
            if _matches(path, self.exclude):
                return _MISSING
            # Full logs are deliberately lossless, including deeply nested JSON.
            # Depth and size limits apply to span extraction, not log bodies.
            selected = self.mode == "full" or (
                self.mode == "selected_content" and _matches(path, self.include)
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
                items = [visit(item, f"{path}.{i}") for i, item in enumerate(value)]
                if selected or any(item is not _MISSING for item in items):
                    return [None if item is _MISSING else item for item in items]
                return _MISSING
            if selected:
                return value
            parts = path.split(".")
            # Known usage/request metadata containers have the same scalar schema.
            permitted = len(parts) == 1 or (len(parts) == 2 and parts[0] in {"usage", "metadata", "model_params"})
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
        result, collisions = {}, set()
        used_bytes = 0
        dropped = 0

        def put(path, value):
            nonlocal used_bytes, dropped
            key = self.mappings.get(path, f"hook.{path}")
            if key in result or key in collisions:
                result.pop(key, None)
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
