"""In-memory hook deduplication and invocation correlation.

Callers own persistence and pass the active state lifetime and capture settings.
"""
from dataclasses import dataclass
import uuid
from typing import Optional, Tuple

from otel_hooks.parsers.common import _first_present, _hash_text, _normalize_mcp_event_data, _stringify
from otel_hooks.parsers.events import _GENERATION_END_EVENTS, _MCP_EVENTS


_SESSION_EVENT_LIMIT = 512
_SESSION_INVOCATION_LIMIT = 128
_AGENT_INVOCATION_LIMIT = 128


@dataclass(slots=True)
class BufferedEventDecision:
    generation_key: Optional[str]
    data: dict
    duplicate: bool = False
    correlated: bool = False


def _event_tool_key(data: dict) -> Optional[str]:
    value = data.get("mcp_tool") or data.get("tool_name")
    if not isinstance(value, str) or not value:
        return None
    return value


def _prune_session_event_state(session_ctx: dict, now_ns: int, ttl_seconds: int) -> None:
    cutoff_ns = now_ns - max(1, ttl_seconds) * 1_000_000_000
    seen = session_ctx.get("seen_events")
    if isinstance(seen, list):
        session_ctx["seen_events"] = [
            entry for entry in seen[-_SESSION_EVENT_LIMIT:]
            if isinstance(entry, dict) and entry.get("seen_at_ns", now_ns) >= cutoff_ns
        ]
    invocations = session_ctx.get("tool_invocations")
    if isinstance(invocations, list):
        session_ctx["tool_invocations"] = [
            entry for entry in invocations[-_SESSION_INVOCATION_LIMIT:]
            if isinstance(entry, dict) and entry.get("updated_at_ns", now_ns) >= cutoff_ns
        ]
    agents = session_ctx.get("agent_invocations")
    if isinstance(agents, list):
        session_ctx["agent_invocations"] = [
            entry for entry in agents[-_AGENT_INVOCATION_LIMIT:]
            if isinstance(entry, dict) and entry.get("updated_at_ns", now_ns) >= cutoff_ns
        ]


def _mark_event_seen(
    session_ctx: dict,
    key: str,
    now_ns: int,
    generation_key: Optional[str],
    *,
    ttl_seconds: int,
    window_seconds: Optional[float] = None,
) -> bool:
    """Return True for a duplicate and remember new observations in bounded state."""
    _prune_session_event_state(session_ctx, now_ns, ttl_seconds)
    seen = session_ctx.setdefault("seen_events", [])
    for entry in reversed(seen):
        if entry.get("key") != key:
            continue
        if window_seconds is None:
            return True
        age_ns = now_ns - entry.get("seen_at_ns", now_ns)
        if age_ns <= int(window_seconds * 1_000_000_000):
            return True
        break
    seen.append({"key": key, "seen_at_ns": now_ns, "generation_key": generation_key})
    del seen[:-_SESSION_EVENT_LIMIT]
    return False


class SessionEventDeduplicator:
    """Own bounded callback idempotency independently of invocation matching."""

    def __init__(
        self, session_ctx: dict, generation_key: Optional[str], now_ns: int, *, ttl_seconds: int,
    ) -> None:
        self.session_ctx = session_ctx
        self.generation_key = generation_key
        self.now_ns = now_ns
        self.ttl_seconds = ttl_seconds

    def seen(self, key: str, *, window_seconds: Optional[float] = None) -> bool:
        return _mark_event_seen(
            self.session_ctx,
            key,
            self.now_ns,
            self.generation_key,
            ttl_seconds=self.ttl_seconds,
            window_seconds=window_seconds,
        )

    def is_duplicate(
        self,
        event_name: str,
        data: dict,
        invocation_id: Optional[str],
    ) -> bool:
        """Apply event-specific keys without owning correlation state."""
        if invocation_id:
            return self.seen(f"tool:{event_name}:{invocation_id}")
        if event_name in _GENERATION_END_EVENTS and self.generation_key:
            return self.seen(f"generation-end:{event_name}:{self.generation_key}")
        if event_name not in _MCP_EVENTS:
            event_id = data.get("_hook_event_id")
            if event_id and event_name in {
                "ErrorOccurred",
                "PostToolUseFailure",
                "PreCompact",
                "PostCompact",
                "PermissionRequest",
            }:
                window = None if data.get("_hook_event_id_source") == "provider" else 1.0
                return self.seen(
                    f"event:{event_name}:{self.generation_key}:{event_id}",
                    window_seconds=window,
                )
            return False
        event_id = data.get('_hook_event_id')
        if event_id and (data.get('_hook_event_id_source') == 'provider' or data.get('_hook_observed_time_ns') is not None):
            return self.seen(f'mcp-callback:{event_name}:{event_id}')
        # Equal content without callback/invocation identity cannot prove replay.
        return False


class SubagentInvocationCorrelator:
    """Correlate concurrent subagent callbacks before applying deduplication."""

    def __init__(
        self,
        session_ctx: dict,
        generation_key: Optional[str],
        now_ns: int,
        *,
        ttl_seconds: int,
    ) -> None:
        self.session_ctx = session_ctx
        self.generation_key = generation_key
        self.now_ns = now_ns
        self.deduplicator = SessionEventDeduplicator(
            session_ctx, generation_key, now_ns, ttl_seconds=ttl_seconds,
        )

    @property
    def invocations(self) -> list:
        return self.session_ctx.setdefault("agent_invocations", [])

    def prepare(self, event_name: str, data: dict) -> tuple[dict, bool]:
        if event_name not in {"SubagentStart", "SubagentStop"}:
            return data, False
        prepared = dict(data)
        callback_id = prepared.get("_hook_event_id")
        if prepared.get("_hook_event_id_source") == "provider" and callback_id:
            if self.deduplicator.seen(f"subagent-callback:{event_name}:{callback_id}"):
                return prepared, True

        agent_id = prepared.get("agent_id")
        if agent_id and self.deduplicator.seen(f"subagent:{event_name}:{agent_id}"):
            return prepared, True
        if event_name == "SubagentStart":
            return self._start(prepared, agent_id), False
        return self._stop(prepared, agent_id)

    def _start(self, prepared: dict, agent_id: Optional[str]) -> dict:
        if not agent_id:
            agent_id = f"hook:{uuid.uuid4()}"
            prepared["agent_id"] = agent_id
            prepared["agent_id_source"] = "hook"
        else:
            prepared["agent_id_source"] = "provider"
        parent_id = prepared.get("parent_agent_id") or self.session_ctx.get("root_agent_id")
        if parent_id:
            prepared["parent_agent_id"] = parent_id
        agent_type = _first_present(prepared, ("subagent_type", "agent_type", "agent_name"))
        task = _first_present(prepared, ("delegation_task", "subagent_task", "task"))
        self.invocations.append({
            "agent_id": agent_id,
            "agent_id_source": prepared.get("agent_id_source"),
            "parent_agent_id": parent_id,
            "agent_type": agent_type,
            "task": _stringify(task) if task is not None else None,
            "generation_key": self.generation_key,
            "state": "open",
            "started_at_ns": self.now_ns,
            "updated_at_ns": self.now_ns,
        })
        del self.invocations[:-_AGENT_INVOCATION_LIMIT]
        return prepared

    def _matches(self, invocation: dict, agent_id: Optional[str], agent_type, task) -> bool:
        return bool(
            invocation.get("state") == "open"
            and (not agent_id or invocation.get("agent_id") == agent_id)
            and (not agent_type or invocation.get("agent_type") in (None, agent_type))
            and (task is None or invocation.get("task") in (None, _stringify(task)))
        )

    def _stop(self, prepared: dict, agent_id: Optional[str]) -> tuple[dict, bool]:
        agent_type = _first_present(prepared, ("subagent_type", "agent_type", "agent_name"))
        task = _first_present(prepared, ("delegation_task", "subagent_task", "task"))
        candidates = [
            item for item in self.invocations if self._matches(item, agent_id, agent_type, task)
        ]
        if not candidates:
            recent_cutoff = self.now_ns - 1_000_000_000
            recently_closed = any(
                item.get("state") == "closed"
                and item.get("updated_at_ns", 0) >= recent_cutoff
                and (not agent_id or item.get("agent_id") == agent_id)
                and (not agent_type or item.get("agent_type") in (None, agent_type))
                and (task is None or item.get("task") in (None, _stringify(task)))
                for item in self.invocations
            )
            if recently_closed:
                return prepared, True
            event_id = prepared.get("_hook_event_id")
            if event_id:
                return prepared, self.deduplicator.seen(
                    f"subagent-unmatched-stop:{self.generation_key}:{event_id}",
                    window_seconds=1.0,
                )
            return prepared, False

        invocation = candidates[0]
        prepared.setdefault("agent_id", invocation.get("agent_id"))
        prepared.setdefault("agent_id_source", invocation.get("agent_id_source"))
        prepared.setdefault("parent_agent_id", invocation.get("parent_agent_id"))
        if task is None and invocation.get("task") is not None:
            prepared["delegation_task"] = invocation["task"]
        invocation["state"] = "closed"
        invocation["updated_at_ns"] = self.now_ns
        invocation["status"] = prepared.get("status") or "success"
        return prepared, False


def _store_mcp_result(invocation: dict, data: dict, *, capture_content: bool) -> None:
    duration = _first_present(data, ("duration_ms", "duration"))
    if duration is not None:
        invocation["duration_ms"] = duration
    if data.get("status") is not None:
        invocation["mcp_status"] = data.get("status")
    if data.get("error") is not None:
        error_text = _stringify(data.get("error"))
        invocation["error_length"] = len(error_text)
        invocation["error_sha256"] = _hash_text(error_text)
        if capture_content:
            invocation["error"] = error_text
        invocation["mcp_status"] = "error"
    result = _first_present(data, ("result_json", "mcp_output", "tool_output", "output"))
    if result is not None:
        text = _stringify(result)
        invocation["mcp_result_length"] = len(text)
        invocation["mcp_result_sha256"] = _hash_text(text)
        invocation["result_json"] = result


def _merge_invocation_data(data: dict, invocation: dict) -> dict:
    """Fill absent canonical fields without replacing callback evidence."""
    merged = dict(data)
    for key in (
        "tool_use_id",
        "mcp_server",
        "mcp_tool",
        "duration_ms",
        "mcp_status",
        "error",
        "error_length",
        "error_sha256",
        "mcp_result_length",
        "mcp_result_sha256",
        "result_json",
    ):
        value = invocation.get(key)
        if value is not None and merged.get(key) is None:
            merged[key] = value
    if merged.get("status") is None and invocation.get("mcp_status") is not None:
        merged["status"] = invocation["mcp_status"]
    return merged


def _matching_open_tools(
    session_ctx: dict,
    data: dict,
    generation_key: Optional[str],
    *,
    mcp_only: bool = False,
    after_mcp: bool = False,
) -> list:
    tool_key = _event_tool_key(data)
    server = data.get("mcp_server")
    candidates = []
    for invocation in session_ctx.get("tool_invocations", []):
        if invocation.get("state") != "open":
            continue
        if mcp_only and not invocation.get("is_mcp"):
            continue
        if tool_key and invocation.get("tool_key") != tool_key:
            continue
        if generation_key and invocation.get("generation_key") != generation_key:
            continue
        if data.get("turn_id") and invocation.get("turn_id") != data.get("turn_id"):
            continue
        if server and invocation.get("mcp_server") not in (None, server):
            continue
        if after_mcp and not invocation.get("mcp_before_seen"):
            continue
        if after_mcp and invocation.get("mcp_after_seen"):
            continue
        if not after_mcp and invocation.get("mcp_before_seen"):
            continue
        candidates.append(invocation)
    return candidates


class ToolInvocationCorrelator:
    """Own bounded, session-local tool, permission, and MCP correlation state."""

    def __init__(
        self,
        session_ctx: dict,
        ide: str,
        generation_key: Optional[str],
        now_ns: int,
        *,
        ttl_seconds: int,
        capture_content: bool,
    ) -> None:
        self.session_ctx = session_ctx
        self.ide = ide
        self.generation_key = generation_key
        self.now_ns = now_ns
        self.ttl_seconds = ttl_seconds
        self.capture_content = capture_content
        self.deduplicator = SessionEventDeduplicator(
            session_ctx, generation_key, now_ns, ttl_seconds=ttl_seconds,
        )

    @property
    def invocations(self) -> list:
        """Return the current session-owned list after any pruning replacement."""
        return self.session_ctx.setdefault("tool_invocations", [])

    def prepare(self, event_name: str, data: dict) -> BufferedEventDecision:
        # This boundary also accepts raw lifecycle events from direct callers.
        prepared = _normalize_mcp_event_data(data, event_name, self.ide)
        if event_name in {"SubagentStart", "SubagentStop"}:
            prepared, duplicate = SubagentInvocationCorrelator(
                self.session_ctx,
                self.generation_key,
                self.now_ns,
                ttl_seconds=self.ttl_seconds,
            ).prepare(event_name, prepared)
            return self._decision(prepared, duplicate=duplicate)

        invocation_id = prepared.get("tool_use_id")
        if self._is_duplicate(event_name, prepared, invocation_id):
            return self._decision(prepared, duplicate=True)

        if event_name == "PreToolUse" and invocation_id:
            self._record_open_tool(prepared, invocation_id)
        elif event_name == "PermissionRequest" and not invocation_id:
            prepared, duplicate = self._correlate_permission(event_name, prepared)
            if duplicate:
                return self._decision(prepared, duplicate=True, correlated=True)
        elif event_name in _MCP_EVENTS and self.ide == "cursor" and not invocation_id:
            correlated = self._correlate_cursor_mcp(event_name, prepared)
            if correlated is not None:
                return self._decision(correlated, correlated=True)
        elif event_name in {"PostToolUse", "PostToolUseFailure"} and invocation_id:
            prepared = self._close_tool(event_name, prepared, invocation_id)

        return self._decision(prepared)

    def _decision(
        self,
        data: dict,
        *,
        duplicate: bool = False,
        correlated: bool = False,
    ) -> BufferedEventDecision:
        return BufferedEventDecision(self.generation_key, data, duplicate=duplicate, correlated=correlated)

    def _is_duplicate(self, event_name: str, data: dict, invocation_id: Optional[str]) -> bool:
        return self.deduplicator.is_duplicate(event_name, data, invocation_id)

    def _record_open_tool(self, data: dict, invocation_id: str) -> None:
        if any(item.get("tool_use_id") == invocation_id for item in self.invocations):
            return
        server, tool = data.get("mcp_server"), data.get("mcp_tool")
        self.invocations.append({
            "tool_use_id": invocation_id,
            "agent_id": data.get("agent_id"),
            "tool_name": data.get("tool_name"),
            "tool_key": _event_tool_key(data),
            "mcp_server": server,
            "mcp_tool": tool,
            "is_mcp": bool(server or tool),
            "generation_key": self.generation_key,
            "turn_id": data.get("turn_id"),
            "state": "open",
            "started_at_ns": self.now_ns,
            "updated_at_ns": self.now_ns,
        })
        del self.invocations[:-_SESSION_INVOCATION_LIMIT]

    def _correlate_permission(self, event_name: str, data: dict) -> Tuple[dict, bool]:
        candidates = _matching_open_tools(self.session_ctx, data, self.generation_key)
        if len(candidates) != 1:
            return data, False
        prepared = dict(data)
        invocation_id = candidates[0]["tool_use_id"]
        prepared["tool_use_id"] = invocation_id
        duplicate = self.deduplicator.seen(f"tool:{event_name}:{invocation_id}")
        return prepared, duplicate

    def _correlate_cursor_mcp(self, event_name: str, data: dict) -> Optional[dict]:
        candidates = _matching_open_tools(
            self.session_ctx,
            data,
            self.generation_key,
            mcp_only=True,
            after_mcp=event_name == "AfterMCPExecution",
        )
        if event_name == "AfterMCPExecution" and not candidates:
            candidates = _matching_open_tools(
                self.session_ctx,
                data,
                self.generation_key,
                mcp_only=True,
            )
        if not candidates:
            return None

        invocation = candidates[0]
        prepared = dict(data)
        prepared["tool_use_id"] = invocation["tool_use_id"]
        server, tool = prepared.get("mcp_server"), prepared.get("mcp_tool")
        invocation["mcp_server"] = server or invocation.get("mcp_server")
        invocation["mcp_tool"] = tool or invocation.get("mcp_tool")
        invocation["is_mcp"] = True
        invocation["updated_at_ns"] = self.now_ns
        if event_name == "BeforeMCPExecution":
            invocation["mcp_before_seen"] = True
        else:
            invocation["mcp_after_seen"] = True
            _store_mcp_result(invocation, prepared, capture_content=self.capture_content)
            invocation.setdefault("mcp_status", "success")
        return prepared

    def _close_tool(self, event_name: str, data: dict, invocation_id: str) -> dict:
        invocation = next(
            (item for item in self.invocations if item.get("tool_use_id") == invocation_id),
            None,
        )
        if invocation is None:
            return data
        prepared = _merge_invocation_data(data, invocation)
        invocation["state"] = "closed"
        invocation["updated_at_ns"] = self.now_ns
        if event_name == "PostToolUseFailure":
            invocation["mcp_status"] = "error"
        else:
            invocation.setdefault("mcp_status", "success")
        return prepared


def enrich_buffered_event(data: dict, session_ctx: Optional[dict]) -> dict:
    """Merge invocation facts into events normalized before they were buffered."""
    invocation_id = data.get("tool_use_id")
    if not invocation_id or not session_ctx:
        return data
    invocation = next(
        (
            item for item in session_ctx.get("tool_invocations", [])
            if item.get("tool_use_id") == invocation_id
        ),
        None,
    )
    return _merge_invocation_data(data, invocation) if invocation else data
