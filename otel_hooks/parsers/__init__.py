"""Provider parser registry; imports no SDK and performs no runtime setup."""
from ..capture import CapturePolicy
from .models import (
    ConversationRecord as ConversationRecord,
    AgentRelationship as AgentRelationship,
    WorkspaceIdentity as WorkspaceIdentity,
    NativeTelemetryContext as NativeTelemetryContext,
    CanonicalHookEvent as CanonicalHookEvent,
)
from .base import ProviderEventAdapter as ProviderEventAdapter
from .cursor import CursorEventAdapter as CursorEventAdapter
from .windsurf import WindsurfEventAdapter as WindsurfEventAdapter
from .claude import ClaudeEventAdapter as ClaudeEventAdapter
from .codex import CodexEventAdapter as CodexEventAdapter
from .gemini import GeminiEventAdapter as GeminiEventAdapter
from .antigravity import AntigravityEventAdapter as AntigravityEventAdapter
from .copilot import CopilotEventAdapter as CopilotEventAdapter
from .opencode import OpenCodeEventAdapter as OpenCodeEventAdapter
from .base import BaseParser as BaseParser


_DEFAULT_EVENT_ADAPTER = ProviderEventAdapter()


_PROVIDER_EVENT_ADAPTERS = {
    "cursor": CursorEventAdapter(),
    "windsurf": WindsurfEventAdapter(),
    "claude": ClaudeEventAdapter(),
    "codex": CodexEventAdapter(),
    "gemini": GeminiEventAdapter(),
    "antigravity": AntigravityEventAdapter(),
    "copilot": CopilotEventAdapter(),
    "opencode": OpenCodeEventAdapter(),
}


def get_parser(ide: str, *, capture_policy: CapturePolicy | None = None) -> BaseParser:
    parser = _PROVIDER_EVENT_ADAPTERS.get(ide, _DEFAULT_EVENT_ADAPTER)
    return type(parser)(capture_policy=capture_policy) if capture_policy is not None else parser


# Public parser lookup; the private spelling remains available in otel_hook.
_event_adapter_for = get_parser
