"""Managed installs preserve unrelated settings and enforce the configured boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from click.testing import CliRunner
import pytest

from otel_hooks import config, managed
from otel_hooks.cli import cli


@pytest.fixture
def registrations(tmp_path, monkeypatch):
    monkeypatch.setattr(managed, "registration_path", lambda agent, scope: str(tmp_path / scope / (agent + ".json")))
    executable = tmp_path / "installed runtime-é" / ("otel-hook.exe" if sys.platform == "win32" else "otel-hook")
    executable.parent.mkdir()
    executable.touch()
    return executable


@pytest.mark.parametrize("agent", ["claude", "cursor"])
def test_registration_upgrade_and_removal_preserve_unrelated_hooks(registrations, agent):
    path = Path(managed.registration_path(agent, "machine"))
    path.parent.mkdir(parents=True)
    other = {"type": "command", "command": "other-observer"}
    entries = [{"matcher": "*", "hooks": [other]}] if agent == "claude" else [other]
    original = {"env": {"OTEL_EXPORTER_OTLP_HEADERS": "SECRET"}, "hooks": {"PreToolUse": entries}, "other": [1]}
    path.write_text(json.dumps(original), encoding="utf-8")
    assert managed.register(agent, "machine", str(registrations))["status"] == "installed"
    first = path.read_bytes()
    assert managed.register(agent, "machine", str(registrations))["status"] == "installed"
    assert path.read_bytes() == first
    upgraded = registrations.parent.parent / "new version" / registrations.name
    upgraded.parent.mkdir()
    upgraded.touch()
    assert managed.register(agent, "machine", str(upgraded))["issues"] == []
    assert managed.inspect(agent, "machine", str(registrations))["issues"] == ["unexpected_command_or_duplicate"]
    managed.unregister(agent, "machine")
    managed.unregister(agent, "machine")
    final = json.loads(path.read_text())
    if agent == "cursor":
        final.pop("version")
    assert final == original


def test_machine_registration_suppresses_legacy_user_and_project_commands(registrations):
    managed.register("claude", "user", str(registrations))
    assert managed.should_skip("claude", None)
    assert not managed.should_skip("claude", "user")
    managed.register("claude", "machine", str(registrations))
    assert managed.should_skip("claude", None)
    assert managed.should_skip("claude", "user")
    assert not managed.should_skip("claude", "machine")
    with pytest.raises(ValueError, match="machine registration"):
        managed.register("claude", "user", str(registrations))
    managed.unregister("claude", "machine")
    assert not managed.should_skip("claude", "user")


def test_partial_or_invalid_registration_does_not_suppress_callbacks(registrations):
    path = Path(managed.registration_path("cursor", "machine"))
    path.parent.mkdir()
    path.write_text(json.dumps({"hooks": {"sessionStart": [managed.command(str(registrations), "cursor", "machine")]}}))
    assert not managed.should_skip("cursor", None)
    assert managed.should_skip("cursor", None, "sessionStart")
    assert not managed.should_skip("cursor", None, "preToolUse")
    path.write_text('{"hooks":null}')
    before = path.read_bytes()
    assert not managed.should_skip("cursor", None)
    with pytest.raises(ValueError):
        managed.register("cursor", "machine", str(registrations))
    assert path.read_bytes() == before


def test_missing_runtime_does_not_suppress_existing_observers(registrations):
    managed.register("cursor", "machine", str(registrations))
    registrations.unlink()
    assert not managed.should_skip("cursor", None)


def test_duplicate_callback_returns_passive_response_without_state(registrations, monkeypatch, capsys):
    import otel_hook as hook
    managed.register("cursor", "machine", str(registrations))
    with patch.dict(os.environ, {}, clear=True):
        monkeypatch.setattr(hook, "_load_config", lambda: {})
        monkeypatch.setattr(hook, "_load_input", lambda: {"hook_event_name": "beforeSubmitPrompt", "session_id": "s"})
        monkeypatch.setattr(hook, "_configure_logging", lambda: None)
        monkeypatch.setattr(hook, "_CLI_HOOK_SOURCE", "cursor")
        monkeypatch.setattr(hook, "_CLI_REGISTRATION_SCOPE", None)
        monkeypatch.setattr(hook, "_CAPTURE_POLICY", hook.CapturePolicy())
        state = registrations.parent.parent / "state"
        monkeypatch.setattr(hook, "_STATE_DIR", str(state))
        assert hook.main() == 0
        assert json.loads(capsys.readouterr().out)["continue"] is True
        assert not state.exists()


def test_failed_atomic_save_retains_existing_settings(registrations, monkeypatch):
    path = Path(managed.registration_path("cursor", "machine"))
    path.parent.mkdir()
    path.write_text('{"other":42}')
    def failed(*args):
        raise OSError("interrupted")
    monkeypatch.setattr(managed.os, "replace", failed)
    with pytest.raises(OSError):
        managed.register("cursor", "machine", str(registrations))
    assert json.loads(path.read_text()) == {"other": 42}
    assert list(path.parent.iterdir()) == [path]


def test_claude_shared_settings_per_key_precedence_and_allowlist(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("machine.json", "remote.json", "user.json")]
    values = [
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://managed.example", "OTEL_EXPORTER_OTLP_HEADERS": ""},
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://remote.example", "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=secret"},
        {"OTEL_SERVICE_NAME": "agent", "PATH": "malicious", "PYTHONPATH": "malicious"},
    ]
    for path, value in zip(paths, values):
        path.write_text(json.dumps({"env": value, "hooks": {"not": "imported"}}))
    monkeypatch.setattr(config, "claude_settings_paths", lambda: [str(p) for p in paths])
    settings, sources = config.shared_claude_settings()
    assert settings == {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://managed.example",
                        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=secret", "OTEL_SERVICE_NAME": "agent"}
    assert sources["OTEL_EXPORTER_OTLP_HEADERS"] == str(paths[1])
    protected, _ = config.shared_claude_settings(managed_only=True)
    assert "OTEL_SERVICE_NAME" not in protected  # User settings are not an enforced source.
    paths[0].write_text("{invalid")
    with pytest.raises(ValueError, match="unable to read"):
        config.shared_claude_settings()


def test_enforced_export_clears_environment_and_local_signal_overrides(monkeypatch, tmp_path):
    import otel_hook as hook
    with patch.dict(os.environ, {}, clear=True):
        monkeypatch.setattr(hook, "_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(hook, "_CAPTURE_POLICY", hook.CapturePolicy())
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://wrong.example")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS", "Authorization=wrong-secret")
        monkeypatch.setenv("IDE_OTEL_ENABLE_LOGS", "true")
        hook._apply_config_env({
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://local.example",
            "capture": {"mode": "full"}, "logs": {"enabled": True},
            "_managed": {"enforce_export": True, "OTEL_EXPORTER_OTLP_ENDPOINT": "https://managed.example",
                         "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
                         "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=managed-secret",
                         "capture": {"mode": "metadata_only"}, "logs": {"enabled": False}},
        })
        assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://managed.example"
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" not in os.environ
        assert "OTEL_EXPORTER_OTLP_LOGS_HEADERS" not in os.environ
        assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=managed-secret"
        assert hook._CAPTURE_POLICY.mode == "metadata_only"
        assert not hook._CAPTURE_POLICY.logs_enabled


def test_enforcement_and_shared_settings_survive_skip_flag(tmp_path, monkeypatch):
    import otel_hook as hook
    path = tmp_path / "config.json"
    path.write_text('{"OTEL_EXPORTER_OTLP_ENDPOINT":"https://local.example"}')
    with patch.dict(os.environ, {"IDE_OTEL_SKIP_MDM": "true"}, clear=True):
        monkeypatch.setattr(hook, "_CONFIG_DEFAULT", str(path))
        monkeypatch.setattr(hook, "_CLI_HOOK_SOURCE", "claude")
        monkeypatch.setattr(hook, "_load_mdm_config", lambda: {
            "enforce_export": True, "claude": {"shared_config": True},
            "capture": {"mode": "metadata_only"}, "logs": {"enabled": False},
        })
        monkeypatch.setattr(hook, "shared_claude_settings", lambda **kwargs: ({
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://shared.example",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        }, {"OTEL_EXPORTER_OTLP_ENDPOINT": "/managed/settings.json"}))
        settings = hook._load_config()
        assert settings["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://shared.example"
        assert settings["_policy_sources"]["export_enforced"]
        assert settings["_policy_sources"]["claude_settings"] == ["/managed/settings.json"]


@pytest.mark.parametrize("value", ["true", None, 1])
def test_invalid_enforcement_fails_closed(value):
    with pytest.raises(ValueError):
        config.enforce_export_environment({"enforce_export": value})
    with pytest.raises(ValueError):
        config.enforce_export_environment({"enforce_export": True})


def test_registry_machine_policy_overrides_and_decodes_structured_values(monkeypatch):
    from unittest.mock import MagicMock
    registry = MagicMock()
    registry.HKEY_CURRENT_USER, registry.HKEY_LOCAL_MACHINE = 1, 2
    values = {1: {"enforce_export": "false", "capture": '{"mode":"full"}'},
              2: {"enforce_export": "true", "capture": '{"mode":"metadata_only"}',
                  "claude": '{"shared_config":true}'}}
    def open_key(hive, path):
        key = MagicMock()
        key.__enter__.return_value = hive
        return key
    def enum_value(hive, index):
        pairs = list(values[hive].items())
        if index >= len(pairs):
            raise OSError()
        return (*pairs[index], 1)
    registry.OpenKey.side_effect, registry.EnumValue.side_effect = open_key, enum_value
    monkeypatch.setitem(sys.modules, "winreg", registry)
    result = config._load_mdm_config_windows()
    assert result == {"enforce_export": True, "capture": {"mode": "metadata_only"}, "claude": {"shared_config": True}}


def test_managed_status_reports_version_scope_and_no_secrets(registrations):
    managed.register("cursor", "machine", str(registrations))
    result = CliRunner().invoke(cli, ["managed", "status", "--agent", "cursor", "--scope", "machine",
                                     "--executable", str(registrations), "--expected-version", "wrong-version"])
    assert result.exit_code == 1
    report = json.loads(result.output)
    assert "unexpected_version" in report["issues"]
    assert report["scope"] == "machine"
    assert report["backend_delivery"] == "unverified"


@pytest.mark.parametrize("action", ["register", "status", "unregister"])
def test_managed_commands_are_sdk_independent(tmp_path, action):
    runner = '''
import sys
class NoRuntime:
    def find_spec(self, fullname, path=None, target=None):
        assert fullname != "otel_hook" and not fullname.startswith("opentelemetry")
sys.meta_path.insert(0, NoRuntime())
from otel_hooks import managed
managed.registration_path = lambda a, s: sys.argv.pop(1)
from otel_hooks.cli import run_cli
run_cli()
'''
    # Run through Click while all target paths are isolated from the actual host.
    path = str(tmp_path / "settings.json")
    runner = runner.replace('managed.registration_path = lambda a, s: sys.argv.pop(1)',
                            'target = sys.argv.pop(1)\nmanaged.registration_path = lambda a, s: target')
    exe = tmp_path / ("otel-hook.exe" if sys.platform == "win32" else "otel-hook")
    exe.touch()
    args = [sys.executable, "-c", runner, path, "managed", action, "--agent", "cursor", "--scope", "machine"]
    if action == "register":
        args.extend(["--executable", str(exe)])
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    assert result.returncode in ({0, 1} if action == "status" else {0}), result.stderr


def test_windows_managed_launcher_uses_native_exec_and_quotes_paths(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    path = r"C:\Program Files\OpenTelemetryHook\otel-hook.ps1"
    hook = managed.command(path, "claude", "machine")
    assert hook["command"] == "powershell.exe"
    assert hook["args"][-3:] == [path, "--claude", "--managed"]
    assert managed._marked(hook, "machine")
    assert '"' + path + '"' in managed.command(path, "cursor", "machine")["command"]
