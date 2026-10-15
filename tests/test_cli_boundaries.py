"""The CLI stays independent of runtime setup and preserves executable boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from otel_hooks import cli as hook_cli


def isolated_environment(tmp_path):
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("OTEL_", "IDE_OTEL_", "CODEX_"))
    }
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    env.update(
        IDE_OTEL_HOOK_HOME=str(tmp_path / "hook-home"),
        IDE_OTEL_CONFIG=str(config),
        IDE_OTEL_LOCAL_SPANS="false",
    )
    return env


@pytest.mark.parametrize("command", ["help", "setup", "diagnose", "uninstall"])
def test_management_commands_do_not_load_runtime_or_sdk(tmp_path, command):
    args = ["--help"] if command == "help" else [
        command, "--agent", "cursor", "--no-global", "--cwd", str(tmp_path),
    ]
    runner = '''
import sys
class NoRuntimeImports:
    def find_spec(self, fullname, path=None, target=None):
        assert fullname != "otel_hook", "management command imported the hook runtime"
        assert not fullname.startswith("opentelemetry"), "management command imported the SDK"
sys.meta_path.insert(0, NoRuntimeImports())
from otel_hooks.cli import run_cli
run_cli()
'''
    result = subprocess.run(
        [sys.executable, "-c", runner, *args], env=isolated_environment(tmp_path),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "hook-home").exists()
    if command == "setup":
        config = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
        assert "sessionStart" in config["hooks"]


@pytest.mark.parametrize("installed", [False, True], ids=["checkout", "installed-prefix"])
def test_plugin_lookup_respects_cli_module_layout(tmp_path, monkeypatch, installed):
    if installed:
        module = tmp_path / "lib" / "python3.12" / "site-packages" / "otel_hooks" / "cli.py"
        plugin = tmp_path / "share" / "opentelemetry-hooks" / "plugin" / "opencode.ts"
    else:
        module = tmp_path / "otel_hooks" / "cli.py"
        plugin = tmp_path / "plugin" / "opencode.ts"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("// fixture plugin", encoding="utf-8")
    monkeypatch.setattr(hook_cli, "__file__", str(module))
    assert hook_cli._find_opencode_plugin_source() == str(plugin)


def test_script_stdin_uses_existing_runtime(tmp_path):
    import otel_hook

    # Running the source entrypoint must not import a second copy as otel_hook.
    runner = '''
import runpy, sys
class NoRuntimeReimport:
    def find_spec(self, fullname, path=None, target=None):
        assert fullname != "otel_hook", "source entrypoint loaded a second runtime"
sys.meta_path.insert(0, NoRuntimeReimport())
script = sys.argv[1]
sys.argv = [script, "--cursor"]
runpy.run_path(script, run_name="__main__")
'''
    result = subprocess.run(
        [sys.executable, "-c", runner, str(Path(otel_hook.__file__).resolve())],
        env=isolated_environment(tmp_path),
        input=json.dumps({"hook_event_name": "SessionStart", "session_id": "cli-script"}),
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["continue"] is True


@patch.dict(os.environ, {}, clear=True)
def test_doctor_uses_current_runtime_policy_and_source(monkeypatch, tmp_path):
    import otel_hook

    monkeypatch.setattr(otel_hook, "_CAPTURE_POLICY", otel_hook.CapturePolicy())
    monkeypatch.setattr(otel_hook, "_CLI_HOOK_SOURCE", None)
    monkeypatch.setattr(otel_hook, "_CLI_HOOK_EVENT", None)
    for name in ("_HOOK_DIR", "_STATE_DIR", "_SESSION_DIR", "_BATCH_DIR", "_LOCK_DIR",
                 "_LOCAL_SPANS_DIR", "_DELIVERY_HEALTH_PATH"):
        monkeypatch.setattr(otel_hook, name, str(tmp_path / name))
    config = {"capture": {"mode": "full"}, "logs": {"enabled": True}}
    monkeypatch.setattr(otel_hook, "_load_config", lambda: config)
    monkeypatch.setattr(hook_cli, "_find_repo_root", lambda _cwd: str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for mode in ("full", "metadata_only"):
        config["capture"]["mode"] = mode
        result = CliRunner().invoke(hook_cli.cli, [
            "--codex", "doctor", "--agent", "codex", "--no-global", "--json",
        ])
        assert result.exit_code in (0, 1), result.output
        report = json.loads(result.output)
        assert report["detected_agent"] == "codex"
        assert report["privacy"]["capture_mode"] == mode
        assert report["privacy"]["hook_log_body"] == mode
