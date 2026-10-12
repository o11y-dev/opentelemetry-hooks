"""Platform contracts plus installed-command replay on native CI runners."""

import io
import json
import os
import shutil
import subprocess
import sys
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

import otel_hook
from otel_hooks import bootstrap


def test_windows_home_and_venv_layout(monkeypatch, tmp_path):
    import sysconfig
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("IDE_OTEL_HOOK_HOME", raising=False)
    monkeypatch.setattr(otel_hook, "__file__", str(tmp_path / "site-packages" / "otel_hook.py"))
    monkeypatch.setattr(sysconfig, "get_path", lambda *a, **kw: str(tmp_path / "site-packages"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local Data"))
    assert otel_hook._resolve_hook_home() == str(tmp_path / "Local Data" / "opentelemetry-hooks")
    monkeypatch.delenv("LOCALAPPDATA")
    assert otel_hook._resolve_hook_home() == os.path.join(os.path.expanduser("~"), "AppData", "Local", "opentelemetry-hooks")
    assert otel_hook._venv_site_packages_path(str(tmp_path)) == str(tmp_path / "Lib" / "site-packages")
    assert bootstrap.venv_python(str(tmp_path)) == str(tmp_path / "Scripts" / "python.exe")
    (tmp_path / ".venv" / "Lib" / "site-packages").mkdir(parents=True)
    assert otel_hook._select_bootstrap_venv_dir(str(tmp_path)).endswith(
        f".venv-py{sys.version_info.major}.{sys.version_info.minor}"
    )


@pytest.mark.parametrize("failure", [False, True])
def test_bootstrap_uses_argv_and_always_releases_lock(monkeypatch, tmp_path, failure):
    venv = tmp_path / "env with spaces & unicode-é"
    lock = tmp_path / "setup.lock"
    lock.touch()
    run = Mock(side_effect=subprocess.CalledProcessError(1, "fixture") if failure else None)
    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            bootstrap.provision(str(venv), str(lock))
    else:
        bootstrap.provision(str(venv), str(lock))
        assert run.call_args_list[1].args[0][:4] == [bootstrap.venv_python(str(venv)), "-m", "pip", "install"]
    assert run.call_args_list[0].args[0] == [sys.executable, "-m", "venv", str(venv)]
    assert not lock.exists()


def test_windows_skips_unix_process_probe_and_reads_utf8(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    probe = Mock(side_effect=AssertionError("Windows must not invoke ps"))
    monkeypatch.setattr(subprocess, "check_output", probe)
    assert otel_hook._detect_ide_from_process_tree() is None
    raw = json.dumps({"prompt": "résumé 日本語"}, ensure_ascii=False).encode("utf-8-sig")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="cp1252"))
    assert otel_hook._load_input() == {"prompt": "résumé 日本語"}


def test_windows_registrations_upgrade_and_uninstall(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    executable = r"C:\Users\Fixture O'Brien\Scripts\otel-hook.exe"
    monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: executable)
    monkeypatch.setattr(otel_hook, "_find_repo_root", lambda _: str(tmp_path))
    copilot_path = tmp_path / ".github" / "hooks" / "otel-hooks.json"
    otel_hook._write_json_file(str(copilot_path), {"hooks": {"sessionStart": [
        {"type": "command", "bash": "otel-hook --copilot"},
        {"type": "command", "powershell": "Write-Output unrelated"},
    ]}})
    otel_hook.setup_copilot(str(tmp_path))
    first = copilot_path.read_bytes()
    otel_hook.setup_copilot(str(tmp_path))
    assert copilot_path.read_bytes() == first
    entries = json.loads(first)["hooks"]["sessionStart"]
    assert len(entries) == 2
    assert entries[0]["powershell"] == "& 'C:\\Users\\Fixture O''Brien\\Scripts\\otel-hook.exe' --copilot --event sessionStart"
    assert entries[0]["bash"] == "otel-hook --copilot"
    result = CliRunner().invoke(otel_hook.cli, ["diagnose", "--agent", "copilot", "--no-global", "--cwd", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "events registered" in result.output
    result = CliRunner().invoke(otel_hook.cli, ["uninstall", "--agent", "copilot", "--no-global", "--cwd", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(copilot_path.read_bytes())["hooks"] == {"sessionStart": [entries[1]]}

    otel_hook.setup_claude(global_=False, cwd=str(tmp_path))
    settings_path = tmp_path / ".claude" / "settings.json"
    first = settings_path.read_bytes()
    otel_hook.setup_claude(global_=False, cwd=str(tmp_path))
    assert settings_path.read_bytes() == first
    for entries in json.loads(first)["hooks"].values():
        for entry in entries:
            for hook in entry["hooks"]:
                assert hook["command"] == executable
                assert hook["args"] == ["--claude"]
    otel_hook.setup_codex(global_=False, cwd=str(tmp_path))
    config = json.loads((tmp_path / ".codex" / "hooks.json").read_bytes())
    for entries in config["hooks"].values():
        for entry in entries:
            for hook in entry["hooks"]:
                assert hook["commandWindows"] == f'"{executable}" --codex'


def test_posix_executable_is_shell_quoted(monkeypatch):
    import shlex
    monkeypatch.setattr(sys, "platform", "linux")
    path = "/fixture user's tools/otel-hook"
    monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: path)
    assert shlex.split(otel_hook._hook_cmd_for_agent("cursor")) == [path, "--cursor"]


@pytest.mark.parametrize("path", [r"C:\Users\Fixture User\repo\file.py", "C:/Users/Fixture User/repo/file.py"])
def test_windows_username_masking(path):
    assert "Fixture" not in otel_hook._mask_text(path)
    assert "User/" not in otel_hook._mask_text(path)
    assert "User\\" not in otel_hook._mask_text(path)
    assert "[REDACTED]" in otel_hook._mask_text(path)


def test_installed_cli_replays_unicode_session_outside_checkout(tmp_path):
    command = shutil.which("otel-hook")
    assert command, "Install the package before running platform tests"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "IDE_OTEL_", "CODEX_"))}
    home = tmp_path / "hook home é"
    home.mkdir()
    config = home / "config.json"
    config.write_text("{}", encoding="utf-8")
    env.update(IDE_OTEL_HOOK_HOME=str(home), IDE_OTEL_CONFIG=str(config),
               IDE_OTEL_LOCAL_SPANS="true", IDE_OTEL_BATCH_ON_STOP="true",
               IDE_OTEL_ENABLE_LOGS="false")
    for name, fields in [
        ("SessionStart", {}), ("UserPromptSubmit", {"prompt": "résumé 日本語"}),
        ("Stop", {}), ("SessionEnd", {}),
    ]:
        result = subprocess.run(
            [command, "--claude"], cwd=tmp_path, env=env, capture_output=True, timeout=15,
            input=json.dumps({"hook_event_name": name, "session_id": "fixture", **fields}, ensure_ascii=False).encode("utf-8"),
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        assert json.loads(result.stdout)["continue"] is True
    spans = [json.loads(line) for path in (home / ".state" / "local_spans").glob("*.jsonl")
             for line in path.read_text(encoding="utf-8").splitlines()]
    assert spans, "Installed CLI must persist real spans"
    assert len({span["trace_id"] for span in spans}) == 1
    assert "résumé" not in json.dumps(spans, ensure_ascii=False)


@pytest.mark.skipif(sys.platform != "win32", reason="Executes native Windows shells")
@pytest.mark.parametrize("agent,shell", [("copilot", "powershell.exe"), ("codex", "cmd.exe")])
def test_native_windows_shell_executes_registered_command(agent, shell, tmp_path):
    command = otel_hook._hook_cmd_for_agent(agent) + " --help"
    # cmd takes a raw command line; list2cmdline would backslash-escape its quotes.
    args = f'cmd.exe /d /s /c "{command}"' if shell == "cmd.exe" else [shell, "-NoProfile", "-Command", command]
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert b"Usage:" in result.stdout
