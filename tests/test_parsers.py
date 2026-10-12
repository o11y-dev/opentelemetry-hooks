"""The parser package is usable without the hook runtime or third-party SDKs."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from otel_hooks.parsers import BaseParser, CanonicalHookEvent, get_parser


@pytest.mark.parametrize("fixture_path", sorted(
    path for path in (Path(__file__).parent / "fixtures" / "contracts").glob("*.json")
    if path.name != "capabilities.json"
), ids=lambda path: path.stem)
def test_public_parser_contract(fixture_path, monkeypatch):
    monkeypatch.delenv("IDE_OTEL_CAPTURE_TEXT", raising=False)
    monkeypatch.delenv("IDE_OTEL_CAPTURE_CONVERSATION_CONTENT", raising=False)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    parser = get_parser(fixture["provider"])
    assert isinstance(parser, BaseParser)
    for case in fixture["cases"]:
        raw = copy.deepcopy(case["raw"])
        event = parser.parse(raw["hook_event_name"], raw)
        assert isinstance(event, CanonicalHookEvent)
        assert event.provider == fixture["provider"]
        assert event.event_name == case["expected"]["event_name"]
        assert event.session_id == case["expected"]["session_id"]
        assert raw == case["raw"], "Parsing must not mutate caller payloads"
        assert all(record.text is None for record in event.conversation)


def test_standalone_import_has_no_sdk_or_runtime_side_effects(tmp_path):
    import otel_hooks
    package_root = Path(otel_hooks.__file__).parent.parent
    result = subprocess.run([
        sys.executable, "-S", "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from otel_hooks.parsers import get_parser; "
        "assert get_parser('claude').parse('SessionStart', {'session_id': 'fixture'}).session_id == 'fixture'; "
        "assert not any(name.startswith(('opentelemetry', 'click', 'otel_hook.')) or name == 'otel_hook' "
        "for name in sys.modules)",
        str(package_root),
    ], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())


def test_legacy_adapter_imports_and_unknown_provider():
    import otel_hook
    assert otel_hook.ProviderEventAdapter is BaseParser
    assert otel_hook.CanonicalHookEvent is CanonicalHookEvent
    assert otel_hook.ClaudeEventAdapter is type(get_parser("claude"))
    parser = get_parser("custom-runner")
    assert parser.parse("CustomEvent", {}) == parser.normalize("CustomEvent", None, {})
