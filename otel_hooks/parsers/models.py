"""Models hook parsing; independent of the telemetry runtime."""
from dataclasses import dataclass
from typing import Optional
from .common import (
    _hash_text,
    _conversation_content_enabled,
)


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    """One privacy-controlled conversation fact extracted from a hook callback."""

    kind: str
    role: str
    text: Optional[str] = None
    length: int = 0
    sha256: str = ""

    @classmethod
    def from_text(cls, kind: str, role: str, text: str) -> "ConversationRecord":
        """Build a content fact without retaining raw text unless capture is enabled."""
        return cls(
            kind=kind,
            role=role,
            text=text if _conversation_content_enabled() else None,
            length=len(text),
            sha256=_hash_text(text),
        )


@dataclass(frozen=True, slots=True)
class AgentRelationship:
    """Provider-neutral agent/delegation identity carried by an event."""

    agent_id: Optional[str] = None
    parent_agent_id: Optional[str] = None
    task: Optional[str] = None
    task_length: int = 0
    task_sha256: str = ""
    status: Optional[str] = None


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    """Provider-supplied workspace identity before Git enrichment."""

    workspace_path: Optional[str] = None
    cwd: Optional[str] = None


@dataclass(frozen=True, slots=True)
class NativeTelemetryContext:
    """Validated trace identifiers supplied by the native agent, when present."""

    source: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class CanonicalHookEvent:
    """Canonical event contract shared by every provider adapter."""

    provider: str
    original_event_name: str
    event_name: str
    event_id: str
    event_id_source: str
    original_tool_name: Optional[str]
    session_id: Optional[str]
    generation_id: Optional[str]
    turn_id: Optional[str]
    conversation: tuple[ConversationRecord, ...]
    relationship: AgentRelationship
    workspace: WorkspaceIdentity
    native: NativeTelemetryContext
    lifecycle_data: dict

    def to_lifecycle_data(self) -> dict:
        """Return the normalized, privacy-safe payload used by lifecycle services."""
        return dict(self.lifecycle_data)
