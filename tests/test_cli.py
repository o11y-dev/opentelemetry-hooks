"""Tests for the otel-hook Click CLI (setup / diagnose / uninstall subcommands)."""
import json
import os

import pytest
from click.testing import CliRunner

import otel_hook
from otel_hook import cli, setup_cursor, setup_windsurf, setup_claude, setup_copilot, setup_gemini, setup_codex


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _read(path):
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Backward compatibility: hook runner path
# ---------------------------------------------------------------------------

class TestHookRunnerBackwardCompat:
    def test_piped_stdin_runs_main(self, tmp_path, monkeypatch):
        """When called with no subcommand and piped stdin, main() is invoked."""
        runner = CliRunner()
        # CliRunner sets stdin to a non-tty pipe by default
        result = runner.invoke(cli, [], input='{"hook_event_name":"Stop","session_id":"test-bc"}')
        # main() exits 0 and prints the continue response
        assert result.exit_code == 0
        assert '"continue"' in result.output

    def test_tty_shows_help(self):
        """When called with no subcommand and no piped input, help is shown."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "setup" in result.output
        assert "diagnose" in result.output
        assert "uninstall" in result.output

    @pytest.mark.parametrize("flag,source", [
        ("--cursor", "cursor"),
        ("--windsurf", "windsurf"),
        ("--claude", "claude"),
        ("--copilot", "copilot"),
        ("--gemini", "gemini"),
        ("--codex", "codex"),
        ("--opencode", "opencode"),
    ])
    def test_agent_flag_sets_hook_source(self, monkeypatch, flag, source):
        runner = CliRunner()
        seen = []

        def fake_main():
            seen.append(otel_hook._CLI_HOOK_SOURCE)
            return 0

        monkeypatch.setattr(otel_hook, "main", fake_main)
        result = runner.invoke(cli, [flag], input='{"hook_event_name":"Stop","session_id":"s1"}')

        assert result.exit_code == 0
        assert seen == [source]


class TestDoctor:
    def test_json_report_is_machine_readable_and_sanitized(self, monkeypatch, tmp_path):
        config = tmp_path / "hooks.json"
        _write(str(config), {"hooks": {"SessionStart": [{"command": "otel-hook --claude"}]}})
        monkeypatch.setattr(
            otel_hook,
            "_agent_config_paths",
            lambda _global, _cwd: {agent: str(config) for agent in otel_hook._SUPPORTED_AGENTS},
        )
        monkeypatch.setattr(otel_hook, "_detect_ide", lambda _data: "claude")
        monkeypatch.setattr(
            otel_hook,
            "_pending_state_summary",
            lambda: {
                "sessions": 0,
                "batches": 0,
                "locks": 0,
                "oldest_pending_age_seconds": 0,
                "state_directory_writable": True,
            },
        )
        monkeypatch.setattr(otel_hook, "_load_delivery_health", lambda: {})
        monkeypatch.delenv("IDE_OTEL_CAPTURE_TEXT", raising=False)
        monkeypatch.delenv("IDE_OTEL_CAPTURE_CONVERSATION_CONTENT", raising=False)
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "https://user:secret@collector.example.test:4318/v1/traces?token=hidden",
        )

        result = CliRunner().invoke(cli, ["doctor", "--agent", "claude", "--json"])

        assert result.exit_code == 0
        report = json.loads(result.output)
        assert report["status"] == "healthy"
        assert report["detected_agent"] == "claude"
        assert report["exporter"]["endpoint"] == "https://collector.example.test:4318"
        assert "secret" not in result.output
        assert report["privacy"]["conversation_content"] is False
        assert report["registrations"][0]["enabled_events"] == ["SessionStart"]

    def test_diagnose_supports_windsurf(self, monkeypatch, tmp_path):
        config = tmp_path / "windsurf.json"
        _write(str(config), {"hooks": {"sessionStart": [{"command": "otel-hook --windsurf"}]}})
        monkeypatch.setattr(
            otel_hook,
            "_agent_config_paths",
            lambda _global, _cwd: {agent: str(config) for agent in otel_hook._SUPPORTED_AGENTS},
        )
        result = CliRunner().invoke(cli, ["diagnose", "--agent", "windsurf"])
        assert result.exit_code == 0
        assert "[windsurf] 1 events registered" in result.output


# ---------------------------------------------------------------------------
# setup_cursor
# ---------------------------------------------------------------------------

class TestSetupCursor:
    def test_creates_new_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_cursor(global_=False, cwd=str(tmp_path))
        hooks_path = tmp_path / ".cursor" / "hooks.json"
        assert hooks_path.exists()
        doc = _read(str(hooks_path))
        assert "hooks" in doc
        assert "sessionStart" in doc["hooks"]
        assert doc["hooks"]["sessionStart"] == [{"command": "otel-hook --cursor"}]

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_cursor(global_=False, cwd=str(tmp_path))
        content_before = _read(str(tmp_path / ".cursor" / "hooks.json"))
        setup_cursor(global_=False, cwd=str(tmp_path))
        content_after = _read(str(tmp_path / ".cursor" / "hooks.json"))
        assert content_before == content_after

    def test_merges_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        hooks_path = tmp_path / ".cursor" / "hooks.json"
        os.makedirs(str(hooks_path.parent))
        _write(str(hooks_path), {"version": 1, "hooks": {"sessionStart": [{"command": "some-other-hook"}]}})
        setup_cursor(global_=False, cwd=str(tmp_path))
        doc = _read(str(hooks_path))
        # Our entry is appended, existing entry is preserved
        cmds = [h["command"] for h in doc["hooks"]["sessionStart"]]
        assert "otel-hook --cursor" in cmds
        assert "some-other-hook" in cmds

    def test_migrates_legacy_env_to_source_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        hooks_path = tmp_path / ".cursor" / "hooks.json"
        os.makedirs(str(hooks_path.parent))
        _write(str(hooks_path), {
            "version": 1,
            "hooks": {
                "sessionStart": [{"command": "otel-hook", "env": {"IDE_OTEL_IDE_NAME": "cursor", "IDE_OTEL_HOOK_SOURCE": "cursor"}}]
            }
        })
        setup_cursor(global_=False, cwd=str(tmp_path))
        doc = _read(str(hooks_path))
        entry = doc["hooks"]["sessionStart"][0]
        assert entry["command"] == "otel-hook --cursor"
        assert "env" not in entry

    def test_global_uses_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        monkeypatch.setenv("HOME", str(tmp_path))
        setup_cursor(global_=True)
        hooks_path = tmp_path / ".cursor" / "hooks.json"
        assert hooks_path.exists()


# ---------------------------------------------------------------------------
# setup_windsurf
# ---------------------------------------------------------------------------

class TestSetupWindsurf:
    def test_creates_new_file_with_source_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_windsurf(global_=False, cwd=str(tmp_path))
        hooks_path = tmp_path / ".windsurf" / "settings.json"
        assert hooks_path.exists()
        doc = _read(str(hooks_path))
        assert "sessionStart" in doc["hooks"]
        assert doc["hooks"]["sessionStart"] == [{"command": "otel-hook --windsurf"}]

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_windsurf(global_=False, cwd=str(tmp_path))
        before = _read(str(tmp_path / ".windsurf" / "settings.json"))
        setup_windsurf(global_=False, cwd=str(tmp_path))
        after = _read(str(tmp_path / ".windsurf" / "settings.json"))
        assert before == after


# ---------------------------------------------------------------------------
# setup_claude
# ---------------------------------------------------------------------------

class TestSetupClaude:
    def test_creates_new_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_claude(global_=False, cwd=str(tmp_path))
        settings_path = tmp_path / ".claude" / "settings.json"
        assert settings_path.exists()
        doc = _read(str(settings_path))
        assert "hooks" in doc
        assert "SessionStart" in doc["hooks"]
        assert "PreCompact" in doc["hooks"]
        assert "PostCompact" in doc["hooks"]

    def test_preserves_existing_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        settings_path = tmp_path / ".claude" / "settings.json"
        os.makedirs(str(settings_path.parent))
        _write(str(settings_path), {"allowedTools": ["Bash"], "hooks": {}})
        setup_claude(global_=False, cwd=str(tmp_path))
        doc = _read(str(settings_path))
        assert doc["allowedTools"] == ["Bash"]

    def test_matcher_events_get_matcher(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_claude(global_=False, cwd=str(tmp_path))
        doc = _read(str(tmp_path / ".claude" / "settings.json"))
        for event in ["PreToolUse", "PostToolUse", "PostToolUseFailure"]:
            entries = doc["hooks"][event]
            assert any(e.get("matcher") == "*" for e in entries)

    def test_non_matcher_events_no_matcher(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_claude(global_=False, cwd=str(tmp_path))
        doc = _read(str(tmp_path / ".claude" / "settings.json"))
        for event in ["SessionStart", "SessionEnd", "PreCompact", "PostCompact", "Stop"]:
            entries = doc["hooks"][event]
            assert all("matcher" not in e for e in entries)

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_claude(global_=False, cwd=str(tmp_path))
        before = _read(str(tmp_path / ".claude" / "settings.json"))
        setup_claude(global_=False, cwd=str(tmp_path))
        after = _read(str(tmp_path / ".claude" / "settings.json"))
        assert before == after

    def test_migrates_legacy_env_to_source_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        settings_path = tmp_path / ".claude" / "settings.json"
        os.makedirs(str(settings_path.parent))
        _write(str(settings_path), {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "otel-hook", "env": {"IDE_OTEL_IDE_NAME": "claude", "IDE_OTEL_HOOK_SOURCE": "claude"}}]}]
            }
        })
        setup_claude(global_=False, cwd=str(tmp_path))
        doc = _read(str(settings_path))
        for h in doc["hooks"]["SessionStart"][0]["hooks"]:
            assert h["command"] == "otel-hook --claude"
            assert "env" not in h


# ---------------------------------------------------------------------------
# setup_copilot
# ---------------------------------------------------------------------------

class TestSetupCopilot:
    def test_creates_new_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_copilot(cwd=str(tmp_path))
        hooks_path = tmp_path / ".github" / "hooks" / "otel-hooks.json"
        assert hooks_path.exists()
        doc = _read(str(hooks_path))
        assert "hooks" in doc
        assert "sessionStart" in doc["hooks"]

    def test_uses_bash_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_copilot(cwd=str(tmp_path))
        doc = _read(str(tmp_path / ".github" / "hooks" / "otel-hooks.json"))
        for entries in doc["hooks"].values():
            for h in entries:
                assert "bash" in h
                assert h["bash"] == "otel-hook --copilot"

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_copilot(cwd=str(tmp_path))
        before = _read(str(tmp_path / ".github" / "hooks" / "otel-hooks.json"))
        setup_copilot(cwd=str(tmp_path))
        after = _read(str(tmp_path / ".github" / "hooks" / "otel-hooks.json"))
        assert before == after


# ---------------------------------------------------------------------------
# setup_gemini
# ---------------------------------------------------------------------------

class TestSetupGemini:
    def test_creates_new_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_gemini(global_=False, cwd=str(tmp_path))
        settings_path = tmp_path / ".gemini" / "settings.json"
        assert settings_path.exists()

    def test_matcher_events(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_gemini(global_=False, cwd=str(tmp_path))
        doc = _read(str(tmp_path / ".gemini" / "settings.json"))
        for event in ["BeforeTool", "AfterTool", "BeforeModel", "AfterModel"]:
            entries = doc["hooks"][event]
            assert any(e.get("matcher") == "*" for e in entries)

    def test_uses_source_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_gemini(global_=False, cwd=str(tmp_path))
        doc = _read(str(tmp_path / ".gemini" / "settings.json"))
        hook = doc["hooks"]["BeforeTool"][0]["hooks"][0]
        assert hook["command"] == "otel-hook --gemini"

    def test_preserves_existing_settings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        settings_path = tmp_path / ".gemini" / "settings.json"
        os.makedirs(str(settings_path.parent))
        _write(str(settings_path), {"theme": "dark", "hooks": {}})
        setup_gemini(global_=False, cwd=str(tmp_path))
        doc = _read(str(settings_path))
        assert doc["theme"] == "dark"


# ---------------------------------------------------------------------------
# setup_codex
# ---------------------------------------------------------------------------

class TestSetupCodex:
    def test_creates_hooks_and_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_codex(global_=False, cwd=str(tmp_path))
        hooks_path = tmp_path / ".codex" / "hooks.json"
        config_path = tmp_path / ".codex" / "config.toml"
        assert hooks_path.exists()
        assert config_path.exists()
        doc = _read(str(hooks_path))
        assert "PermissionRequest" in doc["hooks"]
        text = config_path.read_text()
        assert "hooks = true" in text
        assert "codex_hooks" not in text

    def test_matchers_only_where_supported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_codex(global_=False, cwd=str(tmp_path))
        doc = _read(str(tmp_path / ".codex" / "hooks.json"))
        assert doc["hooks"]["SessionStart"][0]["matcher"] == "startup|resume|clear"
        assert doc["hooks"]["PreToolUse"][0]["matcher"] == "*"
        assert "matcher" not in doc["hooks"]["UserPromptSubmit"][0]
        assert "matcher" not in doc["hooks"]["Stop"][0]

    def test_preserves_existing_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        config_path = tmp_path / ".codex" / "config.toml"
        os.makedirs(str(config_path.parent))
        config_path.write_text('model = "gpt-5.5"\n\n[features]\nmemories = true\n')
        setup_codex(global_=False, cwd=str(tmp_path))
        text = config_path.read_text()
        assert 'model = "gpt-5.5"' in text
        assert "memories = true" in text
        assert "hooks = true" in text
        assert "codex_hooks" not in text

    def test_migrates_deprecated_codex_hooks_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        config_path = tmp_path / ".codex" / "config.toml"
        os.makedirs(str(config_path.parent))
        config_path.write_text('[features]\ncodex_hooks = true\nmemories = true\n')
        setup_codex(global_=False, cwd=str(tmp_path))
        text = config_path.read_text()
        assert "hooks = true" in text
        assert "memories = true" in text
        assert "codex_hooks" not in text


# ---------------------------------------------------------------------------
# CLI setup command
# ---------------------------------------------------------------------------

class TestSetupCmd:
    def test_setup_cursor_via_cli(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        runner = CliRunner()
        result = runner.invoke(cli, ["setup", "--agent", "cursor", "--no-global", "--cwd", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / ".cursor" / "hooks.json").exists()

    def test_setup_multiple_agents(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        runner = CliRunner()
        result = runner.invoke(cli, [
            "setup", "--agent", "cursor", "--agent", "claude",
            "--no-global", "--cwd", str(tmp_path)
        ])
        assert result.exit_code == 0
        assert (tmp_path / ".cursor" / "hooks.json").exists()
        assert (tmp_path / ".claude" / "settings.json").exists()

    def test_copilot_skipped_with_global(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        runner = CliRunner()
        result = runner.invoke(cli, ["setup", "--agent", "copilot", "--global", "--cwd", str(tmp_path)])
        assert result.exit_code == 0
        assert "Skipping" in result.output

    def test_no_agents_detected_exits_1(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_detect_available_agents", lambda: [])
        runner = CliRunner()
        result = runner.invoke(cli, ["setup", "--cwd", str(tmp_path)])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# CLI diagnose command
# ---------------------------------------------------------------------------

class TestDiagnoseCmd:
    def test_not_found(self, tmp_path, monkeypatch):
        runner = CliRunner()
        result = runner.invoke(cli, ["diagnose", "--agent", "cursor", "--no-global", "--cwd", str(tmp_path)])
        assert result.exit_code == 0
        assert "Not found" in result.output

    def test_registered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_cursor(global_=False, cwd=str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli, ["diagnose", "--agent", "cursor", "--no-global", "--cwd", str(tmp_path)])
        assert result.exit_code == 0
        assert "events registered" in result.output

    def test_codex_registered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_codex(global_=False, cwd=str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli, ["diagnose", "--agent", "codex", "--no-global", "--cwd", str(tmp_path)])
        assert result.exit_code == 0
        assert "events registered" in result.output


# ---------------------------------------------------------------------------
# CLI uninstall command
# ---------------------------------------------------------------------------

class TestUninstallCmd:
    def test_uninstall_cursor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_cursor(global_=False, cwd=str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall", "--agent", "cursor", "--no-global", "--cwd", str(tmp_path)])
        assert result.exit_code == 0
        doc = _read(str(tmp_path / ".cursor" / "hooks.json"))
        for entries in doc.get("hooks", {}).values():
            for h in entries:
                assert "otel-hook" not in h.get("command", "")

    def test_uninstall_no_file_is_noop(self, tmp_path, monkeypatch):
        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall", "--agent", "cursor", "--no-global", "--cwd", str(tmp_path)])
        assert result.exit_code == 0

    def test_uninstall_codex(self, tmp_path, monkeypatch):
        monkeypatch.setattr(otel_hook, "_resolve_hook_cmd", lambda: "otel-hook")
        setup_codex(global_=False, cwd=str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall", "--agent", "codex", "--no-global", "--cwd", str(tmp_path)])
        assert result.exit_code == 0
        doc = _read(str(tmp_path / ".codex" / "hooks.json"))
        assert doc.get("hooks", {}) == {}
