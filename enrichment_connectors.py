"""Connector-based memory enrichment for hook event batches.

This module keeps event-memory extraction isolated from otel_hook.py so
additional enrichers can be added without growing the main hook runtime.
"""

from __future__ import annotations

from collections.abc import Callable

MemoryFacts = dict[str, list[str]]
MemoryConnector = Callable[[str, dict], MemoryFacts]
MemorySummary = dict[str, object]


def _empty_facts() -> MemoryFacts:
    return {"files": [], "tools": [], "entities": [], "commands": []}


def file_connector(_event_name: str, data: dict) -> MemoryFacts:
    facts = _empty_facts()
    file_path = data.get("file_path")
    if isinstance(file_path, str) and file_path.strip():
        facts["files"].append(file_path.strip())

    edits = data.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if isinstance(entry, dict):
                p = entry.get("file_path") or entry.get("path")
                if isinstance(p, str) and p.strip():
                    facts["files"].append(p.strip())
    elif isinstance(edits, dict):
        p = edits.get("file_path") or edits.get("path")
        if isinstance(p, str) and p.strip():
            facts["files"].append(p.strip())
    return facts


def tool_connector(_event_name: str, data: dict) -> MemoryFacts:
    facts = _empty_facts()
    tool_name = data.get("tool_name")
    if isinstance(tool_name, str) and tool_name.strip():
        facts["tools"].append(tool_name.strip())
    return facts


def entity_connector(_event_name: str, data: dict) -> MemoryFacts:
    facts = _empty_facts()
    for key in ("agent_id", "agent_name", "subagent_type", "mcp_server", "mcp_tool"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            facts["entities"].append(value.strip())
    return facts


def command_connector(_event_name: str, data: dict) -> MemoryFacts:
    facts = _empty_facts()
    command = data.get("command")
    if isinstance(command, str) and command.strip():
        facts["commands"].append(command.strip())
    return facts


DEFAULT_MEMORY_CONNECTORS: tuple[MemoryConnector, ...] = (
    file_connector,
    tool_connector,
    entity_connector,
    command_connector,
)


def extract_event_memory_facts(
    event_name: str,
    data: dict,
    connectors: tuple[MemoryConnector, ...] = DEFAULT_MEMORY_CONNECTORS,
) -> MemoryFacts:
    merged = _empty_facts()
    for connector in connectors:
        facts = connector(event_name, data)
        for key in merged:
            merged[key].extend(facts.get(key, []))
    return merged


def aggregate_generation_memory(
    batch: list,
    connectors: tuple[MemoryConnector, ...] = DEFAULT_MEMORY_CONNECTORS,
) -> MemorySummary:
    files: list[str] = []
    tools: list[str] = []
    entities: list[str] = []
    commands: list[str] = []
    seen = {
        "files": set(),
        "tools": set(),
        "entities": set(),
        "commands": set(),
    }
    tool_counts: dict[str, int] = {}

    for entry in batch:
        event_name = entry.get("event") or "unknown"
        data = entry.get("data") or {}
        facts = extract_event_memory_facts(event_name, data, connectors=connectors)

        for value in facts["files"]:
            if value not in seen["files"]:
                seen["files"].add(value)
                files.append(value)
        for value in facts["tools"]:
            tool_counts[value] = tool_counts.get(value, 0) + 1
            if value not in seen["tools"]:
                seen["tools"].add(value)
                tools.append(value)
        for value in facts["entities"]:
            if value not in seen["entities"]:
                seen["entities"].add(value)
                entities.append(value)
        for value in facts["commands"]:
            if value not in seen["commands"]:
                seen["commands"].add(value)
                commands.append(value)

    return {
        "files": files,
        "tools": tools,
        "entities": entities,
        "commands": commands,
        "tool_counts": tool_counts,
    }


def merge_memory_summaries(target: dict, source: MemorySummary) -> dict:
    for key in ("files", "tools", "entities", "commands"):
        existing = target.setdefault(key, [])
        seen = set(existing)
        for value in source.get(key, []):
            if value not in seen:
                seen.add(value)
                existing.append(value)
    counts = target.setdefault("tool_counts", {})
    for tool, count in source.get("tool_counts", {}).items():
        counts[tool] = counts.get(tool, 0) + count
    return target
