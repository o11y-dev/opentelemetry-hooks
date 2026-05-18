"""Tests for setup.sh command-selection logic, --global flag scoping, and --reinstall.

Verifies that setup.sh prefers the system-installed otel-hook command when it
is available on PATH, and falls back to the local script otherwise.

Also verifies that --global is scoped to only the explicitly selected IDEs,
and that --reinstall calls `pipx install --force .` before registering hooks.
"""

import json
import os
import shutil
import subprocess
from typing import Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP_SH = os.path.join(REPO_ROOT, "setup.sh")
OTEL_HOOK_PY = os.path.join(REPO_ROOT, "otel_hook.py")
OTEL_CONFIG_EXAMPLE = os.path.join(REPO_ROOT, "otel_config.example.json")
PLUGIN_SRC = os.path.join(REPO_ROOT, "plugin", "opencode.ts")
COPILOT_EXAMPLE = os.path.join(REPO_ROOT, "examples", "copilot-hooks.example.json")


def _make_hook_dir(tmp_root: str) -> str:
    """Create a minimal .cursor/hooks/opentelemetry-hook/ directory tree
    inside *tmp_root* and return its absolute path."""
    hook_dir = os.path.join(tmp_root, ".cursor", "hooks", "opentelemetry-hook")
    os.makedirs(hook_dir, exist_ok=True)
    # Copy the files that setup.sh needs to find next to itself.
    shutil.copy(SETUP_SH, os.path.join(hook_dir, "setup.sh"))
    shutil.copy(OTEL_HOOK_PY, os.path.join(hook_dir, "otel_hook.py"))
    shutil.copy(OTEL_CONFIG_EXAMPLE, os.path.join(hook_dir, "otel_config.example.json"))
    # Copy the OpenCode plugin source so --opencode tests can find it.
    if os.path.exists(PLUGIN_SRC):
        plugin_dir = os.path.join(hook_dir, "plugin")
        os.makedirs(plugin_dir, exist_ok=True)
        shutil.copy(PLUGIN_SRC, os.path.join(plugin_dir, "opencode.ts"))
    return hook_dir


def _make_repo_root(tmp_root: str) -> str:
    """Create a minimal repo-root layout inside *tmp_root* and return it."""
    shutil.copy(SETUP_SH, os.path.join(tmp_root, "setup.sh"))
    shutil.copy(OTEL_HOOK_PY, os.path.join(tmp_root, "otel_hook.py"))
    shutil.copy(OTEL_CONFIG_EXAMPLE, os.path.join(tmp_root, "otel_config.example.json"))
    if os.path.exists(PLUGIN_SRC):
        plugin_dir = os.path.join(tmp_root, "plugin")
        os.makedirs(plugin_dir, exist_ok=True)
        shutil.copy(PLUGIN_SRC, os.path.join(plugin_dir, "opencode.ts"))
    return tmp_root


def _make_clean_dir(tmp_root: str) -> str:
    """Create a clean directory (no .cursor/.claude/.gemini/.github) with setup.sh inside.

    Unlike _make_hook_dir, this places setup.sh directly in a plain subdirectory
    so that auto-detect cannot find any IDE markers in the directory tree.
    Returns the directory path containing setup.sh.
    """
    clean_dir = os.path.join(tmp_root, "clean-setup")
    os.makedirs(clean_dir, exist_ok=True)
    shutil.copy(SETUP_SH, os.path.join(clean_dir, "setup.sh"))
    shutil.copy(OTEL_HOOK_PY, os.path.join(clean_dir, "otel_hook.py"))
    shutil.copy(OTEL_CONFIG_EXAMPLE, os.path.join(clean_dir, "otel_config.example.json"))
    if os.path.exists(PLUGIN_SRC):
        plugin_dir = os.path.join(clean_dir, "plugin")
        os.makedirs(plugin_dir, exist_ok=True)
        shutil.copy(PLUGIN_SRC, os.path.join(plugin_dir, "opencode.ts"))
    return clean_dir


def _run_setup_from_clean_dir(clean_dir: str, home_dir: str, args: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    """Run setup.sh from *clean_dir* (no IDE markers in the tree) with controlled HOME."""
    python3_bin = shutil.which("python3") or "/usr/bin/python3"
    python3_dir = os.path.dirname(python3_bin)
    full_env = {
        "HOME": home_dir,
        "PATH": f"{python3_dir}:/usr/bin:/bin",
    }
    if env:
        full_env.update(env)
    cmd = ["bash", os.path.join(clean_dir, "setup.sh")] + (args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=clean_dir,
        env=full_env,
    )


def _run_setup(hook_dir: str, args: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    """Run setup.sh from *hook_dir* with optional extra *args*, capturing stdout/stderr.

    Individual tests control PATH via *env* to determine whether otel-hook
    is visible.  Tests that check the global-preferred behavior inject a fake
    otel-hook on the PATH; tests that check the fallback use a minimal PATH
    built from the real python3 location so that no accidental otel-hook
    leaks in from the developer's environment.
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    # ALWAYS override HOME so setup.sh never touches the real ~/.claude or ~/.cursor.
    # Using setdefault was a bug: real HOME leaked through when env didn't include it,
    # causing test artifacts to accumulate in the developer's global settings.
    full_env["HOME"] = os.path.dirname(os.path.dirname(os.path.dirname(hook_dir)))

    cmd = ["bash", os.path.join(hook_dir, "setup.sh")] + (args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(hook_dir))),  # tmp_root
        env=full_env,
    )


def _hooks_json_commands(tmp_root: str) -> list[str]:
    """Return every distinct 'command' value found in .cursor/hooks.json."""
    hooks_json = os.path.join(tmp_root, ".cursor", "hooks.json")
    with open(hooks_json) as f:
        doc = json.load(f)
    commands = set()
    for event_hooks in doc.get("hooks", {}).values():
        for hook in event_hooks:
            if "command" in hook:
                commands.add(hook["command"])
    return list(commands)


def _hooks_json_doc(tmp_root: str) -> dict:
    hooks_json = os.path.join(tmp_root, ".cursor", "hooks.json")
    with open(hooks_json) as f:
        return json.load(f)


def _claude_settings_doc(tmp_root: str) -> dict:
    settings_json = os.path.join(tmp_root, ".claude", "settings.json")
    with open(settings_json) as f:
        return json.load(f)


def _gemini_settings_doc(tmp_root: str) -> dict:
    settings_json = os.path.join(tmp_root, ".gemini", "settings.json")
    with open(settings_json) as f:
        return json.load(f)


def _codex_hooks_doc(tmp_root: str) -> dict:
    hooks_json = os.path.join(tmp_root, ".codex", "hooks.json")
    with open(hooks_json) as f:
        return json.load(f)


def _copilot_hooks_doc(tmp_root: str) -> dict:
    hooks_json = os.path.join(tmp_root, ".github", "hooks", "otel-hooks.json")
    with open(hooks_json) as f:
        return json.load(f)


def _opencode_plugin_text(tmp_root: str, global_install: bool = False, config_dir: Optional[str] = None) -> str:
    if global_install:
        base_dir = config_dir or os.path.join(tmp_root, ".config", "opencode")
        plugin_path = os.path.join(base_dir, "plugins", "otel-hook.ts")
    else:
        plugin_path = os.path.join(tmp_root, ".opencode", "plugins", "otel-hook.ts")
    with open(plugin_path) as f:
        return f.read()


class TestSetupShCommandSelection:
    """setup.sh must prefer the global otel-hook command when available."""

    def test_default_uses_global_when_otel_hook_available(self, tmp_path):
        """When otel-hook is on PATH, setup.sh writes its absolute path as the command."""
        hook_dir = _make_hook_dir(str(tmp_path))

        fake_bin = str(tmp_path / "fakebin")
        os.makedirs(fake_bin)
        fake_otel = os.path.join(fake_bin, "otel-hook")
        with open(fake_otel, "w") as f:
            f.write("#!/bin/sh\necho fake\n")
        os.chmod(fake_otel, 0o755)

        old_path = os.environ.get("PATH", "")
        env_override = {"PATH": f"{fake_bin}:{old_path}"}

        result = _run_setup(hook_dir, env=env_override)
        assert result.returncode == 0, result.stderr

        cmds = _hooks_json_commands(str(tmp_path))
        assert len(cmds) == 1
        cmd = cmds[0]
        assert cmd == fake_otel, (
            f"Expected absolute path '{fake_otel}' when otel-hook is on PATH, got: {cmd!r}"
        )

    def test_falls_back_to_local_when_no_otel_hook(self, tmp_path):
        """When otel-hook is not on PATH, setup.sh falls back to the local script."""
        hook_dir = _make_hook_dir(str(tmp_path))

        # Build a minimal PATH containing python3 but definitely no otel-hook,
        # so the test is portable across macOS (/opt/homebrew/bin) and Linux.
        python3_bin = shutil.which("python3") or "/usr/bin/python3"
        python3_dir = os.path.dirname(python3_bin)
        env_override = {"PATH": f"{python3_dir}:/usr/bin:/bin"}

        result = _run_setup(hook_dir, env=env_override)
        assert result.returncode == 0, result.stderr

        cmds = _hooks_json_commands(str(tmp_path))
        assert len(cmds) == 1
        cmd = cmds[0]
        assert "otel_hook.py" in cmd, (
            f"Expected local script fallback when otel-hook not found, got: {cmd!r}"
        )


class TestSetupShGlobalScoping:
    """--global must only affect the explicitly selected IDE(s)."""

    def _minimal_env(self, tmp_path) -> dict:
        """Return an env dict with HOME set to tmp_path and a minimal PATH.

        Setting HOME isolates global-install paths (~/.cursor, ~/.claude, etc.)
        from the real home directory. A minimal PATH ensures no spurious
        otel-hook binary leaks in.
        """
        python3_bin = shutil.which("python3") or "/usr/bin/python3"
        python3_dir = os.path.dirname(python3_bin)
        return {
            "HOME": str(tmp_path),
            "PATH": f"{python3_dir}:/usr/bin:/bin",
        }

    def test_global_without_ide_flag_errors(self, tmp_path):
        """--global alone (no IDE flag) must exit non-zero with a clear error."""
        hook_dir = _make_hook_dir(str(tmp_path))
        result = _run_setup(hook_dir, args=["--global"], env=self._minimal_env(tmp_path))
        assert result.returncode != 0, "Expected non-zero exit when --global used without IDE flag"
        assert "--cursor" in result.stdout or "--cursor" in result.stderr, (
            "Expected error message to mention IDE flags"
        )

    def test_cursor_global_only_affects_cursor(self, tmp_path):
        """--cursor --global writes ~/.cursor/hooks.json but not ~/.claude/settings.json."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        result = _run_setup(hook_dir, args=["--cursor", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        # Global Cursor hooks.json must be created under the fake HOME
        global_cursor = os.path.join(str(tmp_path), ".cursor", "hooks.json")
        assert os.path.exists(global_cursor), (
            f"Expected {global_cursor} to be created by --cursor --global"
        )

        # Claude settings must NOT be touched
        global_claude = os.path.join(str(tmp_path), ".claude", "settings.json")
        assert not os.path.exists(global_claude), (
            "--cursor --global must not create Claude's settings.json"
        )

    def test_claude_global_only_affects_claude(self, tmp_path):
        """--claude --global writes ~/.claude/settings.json but not ~/.cursor/hooks.json."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        result = _run_setup(hook_dir, args=["--claude", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        # Global Claude settings must be created under the fake HOME
        global_claude = os.path.join(str(tmp_path), ".claude", "settings.json")
        assert os.path.exists(global_claude), (
            f"Expected {global_claude} to be created by --claude --global"
        )

        # Cursor global hooks.json must NOT be touched by a --claude --global run
        global_cursor_hooks = os.path.join(str(tmp_path), ".cursor", "hooks.json")
        assert not os.path.exists(global_cursor_hooks), (
            "--claude --global must not create Cursor's global hooks.json"
        )

    def test_multi_ide_global_affects_only_selected(self, tmp_path):
        """--cursor --claude --global installs globally for Cursor and Claude but not OpenCode."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        result = _run_setup(hook_dir, args=["--cursor", "--claude", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        # Both Cursor and Claude global files must exist
        global_cursor = os.path.join(str(tmp_path), ".cursor", "hooks.json")
        global_claude = os.path.join(str(tmp_path), ".claude", "settings.json")
        assert os.path.exists(global_cursor), "Expected ~/.cursor/hooks.json for --cursor --global"
        assert os.path.exists(global_claude), "Expected ~/.claude/settings.json for --claude --global"

        # OpenCode global plugin dir must NOT be created
        opencode_global_plugin = os.path.join(
            str(tmp_path), ".config", "opencode", "plugins", "otel-hook.ts"
        )
        assert not os.path.exists(opencode_global_plugin), (
            "--cursor --claude --global must not install OpenCode's global plugin"
        )

    def test_copilot_global_is_rejected(self, tmp_path):
        """--copilot --global must fail because Copilot hooks are repo-scoped."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        result = _run_setup(hook_dir, args=["--copilot", "--global"], env=env)
        assert result.returncode != 0, "Expected non-zero exit for --copilot --global"
        assert "repository-scoped" in result.stdout or "repository-scoped" in result.stderr

        copilot_hooks = os.path.join(str(tmp_path), ".github", "hooks", "otel-hooks.json")
        assert not os.path.exists(copilot_hooks), (
            "--copilot --global must not create a Copilot hooks file"
        )


class TestSetupShIdeDetectionConfig:
    def _minimal_env(self, tmp_path) -> dict:
        python3_bin = shutil.which("python3") or "/usr/bin/python3"
        python3_dir = os.path.dirname(python3_bin)
        return {
            "HOME": str(tmp_path),
            "PATH": f"{python3_dir}:/usr/bin:/bin",
        }

    def test_cursor_new_hooks_do_not_include_ide_override_env(self, tmp_path):
        hook_dir = _make_hook_dir(str(tmp_path))
        result = _run_setup(hook_dir, args=["--cursor"], env=self._minimal_env(tmp_path))
        assert result.returncode == 0, result.stderr

        doc = _hooks_json_doc(str(tmp_path))
        for event_hooks in doc["hooks"].values():
            assert len(event_hooks) == 1
            hook = event_hooks[0]
            assert "command" in hook and hook["command"]
            assert "env" not in hook

    def test_cursor_merge_removes_legacy_ide_override_env(self, tmp_path):
        hook_dir = _make_hook_dir(str(tmp_path))
        hooks_json = tmp_path / ".cursor" / "hooks.json"
        hooks_json.parent.mkdir(parents=True, exist_ok=True)
        hooks_json.write_text(json.dumps({
            "version": 1,
            "hooks": {
                "sessionStart": [{"command": "python3 /tmp/other.py"}],
                "preToolUse": [{"command": "python3 %s/otel_hook.py" % hook_dir, "env": {"IDE_OTEL_IDE_NAME": "cursor", "KEEP": "1"}}],
            },
        }))

        result = _run_setup(hook_dir, args=["--cursor"], env=self._minimal_env(tmp_path))
        assert result.returncode == 0, result.stderr

        doc = _hooks_json_doc(str(tmp_path))
        matching = [h for h in doc["hooks"]["preToolUse"] if h["command"] == "python3 %s/otel_hook.py" % hook_dir]
        assert matching == [{
            "command": "python3 %s/otel_hook.py" % hook_dir,
            "env": {"KEEP": "1"},
        }]

    def test_claude_new_hooks_do_not_include_ide_override_env(self, tmp_path):
        hook_dir = _make_hook_dir(str(tmp_path))
        result = _run_setup(hook_dir, args=["--claude"], env=self._minimal_env(tmp_path))
        assert result.returncode == 0, result.stderr

        doc = _claude_settings_doc(str(tmp_path))
        for event_entries in doc["hooks"].values():
            for event_entry in event_entries:
                for hook in event_entry["hooks"]:
                    if hook["command"].endswith("otel_hook.py"):
                        assert "env" not in hook

    def test_claude_merge_removes_legacy_ide_override_env(self, tmp_path):
        hook_dir = _make_hook_dir(str(tmp_path))
        settings_json = tmp_path / ".claude" / "settings.json"
        settings_json.parent.mkdir(parents=True)
        settings_json.write_text(json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "python3 %s/otel_hook.py" % hook_dir, "env": {"IDE_OTEL_IDE_NAME": "claude", "KEEP": "1"}}],
                }],
            },
        }))

        result = _run_setup(hook_dir, args=["--claude"], env=self._minimal_env(tmp_path))
        assert result.returncode == 0, result.stderr

        doc = _claude_settings_doc(str(tmp_path))
        matching = []
        for event_entry in doc["hooks"]["PreToolUse"]:
            for hook in event_entry["hooks"]:
                if hook["command"] == "python3 %s/otel_hook.py" % hook_dir:
                    matching.append(hook)
        assert matching == [{
            "type": "command",
            "command": "python3 %s/otel_hook.py" % hook_dir,
            "env": {"KEEP": "1"},
        }]

    def test_copilot_new_hooks_do_not_include_ide_override_env(self, tmp_path):
        hook_dir = _make_hook_dir(str(tmp_path))
        result = _run_setup(hook_dir, args=["--copilot"], env=self._minimal_env(tmp_path))
        assert result.returncode == 0, result.stderr

        doc = _copilot_hooks_doc(str(tmp_path))
        for event_hooks in doc["hooks"].values():
            assert len(event_hooks) == 1
            hook = event_hooks[0]
            assert hook["type"] == "command"
            assert hook["timeoutSec"] == 30
            assert "IDE_OTEL_IDE_NAME" not in hook["bash"]
            assert hook["bash"].endswith("otel_hook.py")

    def test_copilot_merge_removes_legacy_ide_override_env(self, tmp_path):
        hook_dir = _make_hook_dir(str(tmp_path))
        hooks_json = tmp_path / ".github" / "hooks" / "otel-hooks.json"
        hooks_json.parent.mkdir(parents=True, exist_ok=True)
        hooks_json.write_text(json.dumps({
            "version": 1,
            "hooks": {
                "sessionStart": [{"type": "command", "bash": "env IDE_OTEL_IDE_NAME=copilot python3 %s/otel_hook.py" % hook_dir}],
                "preToolUse": [{"type": "command", "bash": "python3 %s/otel_hook.py" % hook_dir, "timeoutSec": 45}],
            },
        }))

        result = _run_setup(hook_dir, args=["--copilot"], env=self._minimal_env(tmp_path))
        assert result.returncode == 0, result.stderr

        doc = _copilot_hooks_doc(str(tmp_path))
        session_start = doc["hooks"]["sessionStart"]
        assert session_start == [{
            "type": "command",
            "bash": "python3 %s/otel_hook.py" % hook_dir,
            "timeoutSec": 30,
        }]
        pre_tool_use = doc["hooks"]["preToolUse"]
        assert pre_tool_use == [{
            "type": "command",
            "bash": "python3 %s/otel_hook.py" % hook_dir,
            "timeoutSec": 45,
        }]

    def test_setup_from_repo_root_creates_project_configs(self, tmp_path):
        repo_root = _make_repo_root(str(tmp_path))
        env = self._minimal_env(tmp_path)

        for args, expected_path in (
            (["--cursor"], tmp_path / ".cursor" / "hooks.json"),
            (["--claude"], tmp_path / ".claude" / "settings.json"),
            (["--copilot"], tmp_path / ".github" / "hooks" / "otel-hooks.json"),
        ):
            result = subprocess.run(
                ["bash", os.path.join(repo_root, "setup.sh"), *args],
                capture_output=True,
                text=True,
                cwd=repo_root,
                env={**os.environ, **env},
            )
            assert result.returncode == 0, result.stderr
            assert expected_path.exists(), f"Expected {expected_path} to be created"


class TestSetupShReinstall:
    """--reinstall must call `pipx install --force .` before registering hooks."""

    def _minimal_env(self, tmp_path) -> dict:
        """Return an env dict with HOME set to tmp_path and a minimal PATH."""
        python3_bin = shutil.which("python3") or "/usr/bin/python3"
        python3_dir = os.path.dirname(python3_bin)
        return {
            "HOME": str(tmp_path),
            "PATH": f"{python3_dir}:/usr/bin:/bin",
        }

    def test_reinstall_without_pipx_errors(self, tmp_path):
        """--reinstall must exit non-zero with a clear error when pipx is not found."""
        hook_dir = _make_hook_dir(str(tmp_path))

        # Build a controlled fake PATH that contains only the minimal executables
        # setup.sh needs (bash + python3) but deliberately omits pipx.
        fake_bin = str(tmp_path / "no_pipx_bin")
        os.makedirs(fake_bin, exist_ok=True)
        python3_real = shutil.which("python3") or "/usr/bin/python3"
        bash_real = shutil.which("bash") or "/bin/bash"
        for name, real in [("python3", python3_real), ("bash", bash_real)]:
            link = os.path.join(fake_bin, name)
            if not os.path.exists(link):
                os.symlink(real, link)
        env = {
            "HOME": str(tmp_path),
            "PATH": fake_bin,
        }

        result = _run_setup(hook_dir, args=["--cursor", "--reinstall"], env=env)
        assert result.returncode != 0, "Expected non-zero exit when pipx is not on PATH"
        assert "pipx" in result.stdout or "pipx" in result.stderr, (
            "Expected error message to mention pipx"
        )

    def test_reinstall_calls_pipx_force(self, tmp_path):
        """--reinstall must invoke `pipx install --force <HOOK_DIR>` before hook registration."""
        hook_dir = _make_hook_dir(str(tmp_path))

        # Create a fake pipx that records its invocation and exits successfully.
        fake_bin = str(tmp_path / "fakebin")
        os.makedirs(fake_bin, exist_ok=True)
        invocation_log = str(tmp_path / "pipx_invocations.txt")
        fake_pipx = os.path.join(fake_bin, "pipx")
        with open(fake_pipx, "w") as f:
            f.write(
                f"#!/bin/sh\necho \"$@\" >> {invocation_log}\nexit 0\n"
            )
        os.chmod(fake_pipx, 0o755)

        python3_bin = shutil.which("python3") or "/usr/bin/python3"
        python3_dir = os.path.dirname(python3_bin)
        env = {
            "HOME": str(tmp_path),
            "PATH": f"{fake_bin}:{python3_dir}:/usr/bin:/bin",
        }

        result = _run_setup(hook_dir, args=["--cursor", "--reinstall"], env=env)
        assert result.returncode == 0, result.stderr

        # The fake pipx should have been called with 'install --force <hook_dir>'
        assert os.path.exists(invocation_log), "pipx was never invoked"
        with open(invocation_log) as f:
            invocations = f.read()
        assert "install --force" in invocations, (
            f"Expected 'install --force' in pipx invocations, got: {invocations!r}"
        )
        assert hook_dir in invocations, (
            f"Expected hook_dir path {hook_dir!r} in pipx invocations, got: {invocations!r}"
        )

    def test_reinstall_still_registers_hooks(self, tmp_path):
        """--reinstall must register hooks after a successful pipx reinstall."""
        hook_dir = _make_hook_dir(str(tmp_path))

        # Fake pipx that succeeds silently.
        fake_bin = str(tmp_path / "fakebin")
        os.makedirs(fake_bin, exist_ok=True)
        fake_pipx = os.path.join(fake_bin, "pipx")
        with open(fake_pipx, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(fake_pipx, 0o755)

        python3_bin = shutil.which("python3") or "/usr/bin/python3"
        python3_dir = os.path.dirname(python3_bin)
        env = {
            "HOME": str(tmp_path),
            "PATH": f"{fake_bin}:{python3_dir}:/usr/bin:/bin",
        }

        result = _run_setup(hook_dir, args=["--cursor", "--reinstall"], env=env)
        assert result.returncode == 0, result.stderr

        # Hooks must still be written
        cmds = _hooks_json_commands(str(tmp_path))
        assert len(cmds) == 1, f"Expected one distinct command in hooks.json, got: {cmds}"


class TestProcessDiscoveryExamples:
    def _minimal_env(self, tmp_path) -> dict:
        python3_bin = shutil.which("python3") or "/usr/bin/python3"
        python3_dir = os.path.dirname(python3_bin)
        return {
            "HOME": str(tmp_path),
            "PATH": f"{python3_dir}:/usr/bin:/bin",
        }

    def test_setup_opencode_installs_global_plugin_without_ide_override_env(self, tmp_path):
        hook_dir = _make_hook_dir(str(tmp_path))
        config_dir = str(tmp_path / "opencode-config")
        env = self._minimal_env(tmp_path)
        env["OPENCODE_CONFIG_DIR"] = config_dir

        result = _run_setup(hook_dir, args=["--opencode", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        plugin_text = _opencode_plugin_text(str(tmp_path), global_install=True, config_dir=config_dir)
        assert "IDE_OTEL_IDE_NAME" not in plugin_text
        assert "await $`otel-hook`" in plugin_text

    def test_copilot_example_uses_plain_hook_command(self):
        with open(COPILOT_EXAMPLE) as f:
            doc = json.load(f)

        for event_hooks in doc["hooks"].values():
            for hook in event_hooks:
                assert hook["bash"] == "{{SCRIPT_PATH}}"


class TestGeminiSetup:
    """Tests for Gemini CLI hook setup and operational commands."""

    def _minimal_env(self, tmp_path) -> dict:
        python3_bin = shutil.which("python3") or "/usr/bin/python3"
        python3_dir = os.path.dirname(python3_bin)
        return {
            "HOME": str(tmp_path),
            "PATH": f"{python3_dir}:/usr/bin:/bin",
        }

    def test_gemini_global_creates_settings_json(self, tmp_path):
        """setup.sh --gemini --global must create ~/.gemini/settings.json with correct shape."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        result = _run_setup(hook_dir, args=["--gemini", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        settings_path = tmp_path / ".gemini" / "settings.json"
        assert settings_path.exists(), "Expected ~/.gemini/settings.json to be created"

        doc = _gemini_settings_doc(str(tmp_path))
        assert "hooks" in doc, "settings.json must have a 'hooks' key"

        # Every event must have a list of hook entries
        for event, entries in doc["hooks"].items():
            assert isinstance(entries, list), f"hooks[{event!r}] must be a list"
            for entry in entries:
                assert "hooks" in entry, "Each hook entry must have a nested 'hooks' key"
                for h in entry["hooks"]:
                    assert "command" in h, "Each inner hook must have a 'command'"
                    assert "otel" in h["command"].lower() or "otel-hook" in h["command"]

    def test_gemini_matcher_only_for_matcher_events(self, tmp_path):
        """Gemini hook entries must include 'matcher' only for matcher events."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        result = _run_setup(hook_dir, args=["--gemini", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        doc = _gemini_settings_doc(str(tmp_path))
        # GEMINI_MATCHER_EVENTS includes agent/model/tool events but NOT SessionStart/SessionEnd
        matcher_events = {"BeforeAgent", "AfterAgent", "BeforeModel", "AfterModel", "BeforeTool", "AfterTool"}
        non_matcher_events = {"SessionStart", "SessionEnd"}

        for event, entries in doc["hooks"].items():
            for entry in entries:
                if event in matcher_events:
                    assert "matcher" in entry, f"Event {event!r} should have a matcher field"
                elif event in non_matcher_events:
                    assert "matcher" not in entry, f"Event {event!r} should NOT have a matcher field"

    def test_gemini_diagnose_composite_command(self, tmp_path):
        """--diagnose --gemini must correctly handle composite commands like 'python3 /path/otel_hook.py'."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        # First set up gemini
        result = _run_setup(hook_dir, args=["--gemini", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        # Verify the settings.json was written with a composite command (python3 + script path)
        doc = _gemini_settings_doc(str(tmp_path))
        all_cmds = [
            h["command"]
            for entries in doc["hooks"].values()
            for entry in entries
            for h in entry.get("hooks", [])
        ]
        assert all_cmds, "Expected at least one command in hooks"

        # Run diagnose — should report registered hooks, not spuriously mark them stale
        result = _run_setup(hook_dir, args=["--diagnose", "--gemini", "--global"], env=env)
        assert result.returncode == 0, result.stderr
        output = result.stdout + result.stderr
        assert "OTel hook entries registered" in output, (
            f"Expected diagnose to find registered hooks, got:\n{output}"
        )
        # Stale count should be 0 because the script path exists
        assert "(0 stale)" in output, (
            f"Expected 0 stale hooks for a freshly installed config, got:\n{output}"
        )

    def test_gemini_clean_keeps_valid_composite_commands(self, tmp_path):
        """--clean --gemini must keep hooks whose script path exists (composite commands)."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        # Set up gemini hooks
        result = _run_setup(hook_dir, args=["--gemini", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        doc_before = _gemini_settings_doc(str(tmp_path))
        hooks_before = sum(len(entries) for entries in doc_before["hooks"].values())

        # Run clean — should NOT remove valid hooks
        result = _run_setup(hook_dir, args=["--clean", "--gemini", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        doc_after = _gemini_settings_doc(str(tmp_path))
        hooks_after = sum(len(entries) for entries in doc_after["hooks"].values())
        assert hooks_after == hooks_before, (
            f"--clean removed valid composite-command hooks: before={hooks_before}, after={hooks_after}"
        )
        output = result.stdout + result.stderr
        assert "No stale hook entries found" in output, (
            f"Expected no stale entries to be cleaned, got:\n{output}"
        )

    def test_gemini_uninstall_removes_all_otel_hooks(self, tmp_path):
        """--uninstall --gemini must remove all OTel hook entries."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        result = _run_setup(hook_dir, args=["--gemini", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        result = _run_setup(hook_dir, args=["--uninstall", "--gemini", "--global"], env=env)
        assert result.returncode == 0, result.stderr
        output = result.stdout + result.stderr
        assert "Uninstalled" in output or "hook entries" in output.lower(), (
            f"Expected uninstall confirmation, got:\n{output}"
        )

        doc = _gemini_settings_doc(str(tmp_path))
        for event, entries in doc.get("hooks", {}).items():
            for entry in entries:
                for h in entry.get("hooks", []):
                    cmd = h.get("command", "")
                    assert "otel_hook" not in cmd and "otel-hook" not in cmd, (
                        f"OTel hook was not removed for event {event!r}: {cmd!r}"
                    )

    def test_codex_global_creates_hooks_and_config(self, tmp_path):
        """setup.sh --codex --global must create ~/.codex/hooks.json and enable hooks."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        result = _run_setup(hook_dir, args=["--codex", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        hooks_path = tmp_path / ".codex" / "hooks.json"
        config_path = tmp_path / ".codex" / "config.toml"
        assert hooks_path.exists()
        assert config_path.exists()
        text = config_path.read_text()
        assert "hooks = true" in text
        assert "codex_hooks" not in text

        doc = _codex_hooks_doc(str(tmp_path))
        assert "PermissionRequest" in doc["hooks"]
        assert doc["hooks"]["SessionStart"][0]["matcher"] == "startup|resume|clear"
        assert doc["hooks"]["PreToolUse"][0]["matcher"] == "*"
        assert "matcher" not in doc["hooks"]["UserPromptSubmit"][0]

    def test_codex_uninstall_removes_all_otel_hooks(self, tmp_path):
        """--uninstall --codex must remove all OTel hook entries."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        result = _run_setup(hook_dir, args=["--codex", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        result = _run_setup(hook_dir, args=["--uninstall", "--codex", "--global"], env=env)
        assert result.returncode == 0, result.stderr
        doc = _codex_hooks_doc(str(tmp_path))
        assert doc.get("hooks", {}) == {}

    def test_diagnose_auto_detects_gemini_when_dir_exists(self, tmp_path):
        """--diagnose without an IDE flag must auto-detect gemini when ~/.gemini exists."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        # Set up gemini first
        result = _run_setup(hook_dir, args=["--gemini", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        # Run --diagnose with NO IDE flag; ~/.gemini exists so gemini should be auto-detected
        result = _run_setup(hook_dir, args=["--diagnose"], env=env)
        assert result.returncode == 0, result.stderr
        output = result.stdout + result.stderr
        assert "Gemini CLI" in output, (
            f"Expected Gemini CLI to be auto-detected for --diagnose, got:\n{output}"
        )


class TestOperationalFlags:
    """Tests for --diagnose, --clean, --uninstall auto-detection and behavior."""

    def _minimal_env(self, tmp_path) -> dict:
        python3_bin = shutil.which("python3") or "/usr/bin/python3"
        python3_dir = os.path.dirname(python3_bin)
        return {
            "HOME": str(tmp_path),
            "PATH": f"{python3_dir}:/usr/bin:/bin",
        }

    def test_diagnose_without_ide_flag_errors_when_no_ide_detected(self, tmp_path):
        """--diagnose with no IDE flag and no IDE detected must exit non-zero."""
        # Use a clean directory (no .cursor/.claude/.gemini anywhere in path)
        # so that auto-detect finds nothing.
        clean_dir = _make_clean_dir(str(tmp_path))
        home_dir = str(tmp_path / "home")
        os.makedirs(home_dir, exist_ok=True)

        result = _run_setup_from_clean_dir(clean_dir, home_dir, args=["--diagnose"])
        assert result.returncode != 0, (
            f"Expected non-zero exit when no IDE detected, got:\n{result.stdout}"
        )
        output = result.stdout + result.stderr
        assert "No supported IDE detected" in output, (
            f"Expected 'No supported IDE detected' error, got:\n{output}"
        )

    def test_clean_without_ide_flag_errors_when_no_ide_detected(self, tmp_path):
        """--clean with no IDE flag and no IDE detected must exit non-zero."""
        clean_dir = _make_clean_dir(str(tmp_path))
        home_dir = str(tmp_path / "home")
        os.makedirs(home_dir, exist_ok=True)

        result = _run_setup_from_clean_dir(clean_dir, home_dir, args=["--clean"])
        assert result.returncode != 0, (
            f"Expected non-zero exit when no IDE detected, got:\n{result.stdout}"
        )
        output = result.stdout + result.stderr
        assert "No supported IDE detected" in output

    def test_uninstall_without_ide_flag_errors_when_no_ide_detected(self, tmp_path):
        """--uninstall with no IDE flag and no IDE detected must exit non-zero."""
        clean_dir = _make_clean_dir(str(tmp_path))
        home_dir = str(tmp_path / "home")
        os.makedirs(home_dir, exist_ok=True)

        result = _run_setup_from_clean_dir(clean_dir, home_dir, args=["--uninstall"])
        assert result.returncode != 0, (
            f"Expected non-zero exit when no IDE detected, got:\n{result.stdout}"
        )
        output = result.stdout + result.stderr
        assert "No supported IDE detected" in output

    def test_diagnose_cursor_reports_registered_hooks(self, tmp_path):
        """--diagnose --cursor must report that OTel hooks are registered."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        # Set up cursor first
        result = _run_setup(hook_dir, args=["--cursor"], env=env)
        assert result.returncode == 0, result.stderr

        result = _run_setup(hook_dir, args=["--diagnose", "--cursor"], env=env)
        assert result.returncode == 0, result.stderr
        output = result.stdout + result.stderr
        assert "OTel hook entries registered" in output, (
            f"Expected registered hooks report, got:\n{output}"
        )
        assert "(0 stale)" in output, (
            f"Expected 0 stale hooks, got:\n{output}"
        )

    def test_clean_cursor_keeps_valid_hooks(self, tmp_path):
        """--clean --cursor must preserve valid (non-stale) hook entries."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        result = _run_setup(hook_dir, args=["--cursor"], env=env)
        assert result.returncode == 0, result.stderr

        doc_before = _hooks_json_doc(str(tmp_path))
        total_before = sum(len(v) for v in doc_before["hooks"].values())

        result = _run_setup(hook_dir, args=["--clean", "--cursor"], env=env)
        assert result.returncode == 0, result.stderr

        doc_after = _hooks_json_doc(str(tmp_path))
        total_after = sum(len(v) for v in doc_after["hooks"].values())
        assert total_after == total_before, (
            f"--clean removed valid hooks: before={total_before}, after={total_after}"
        )
        output = result.stdout + result.stderr
        assert "No stale hook entries found" in output

    def test_uninstall_cursor_removes_otel_hooks(self, tmp_path):
        """--uninstall --cursor must remove all OTel hook entries."""
        hook_dir = _make_hook_dir(str(tmp_path))
        env = self._minimal_env(tmp_path)

        result = _run_setup(hook_dir, args=["--cursor"], env=env)
        assert result.returncode == 0, result.stderr

        result = _run_setup(hook_dir, args=["--uninstall", "--cursor"], env=env)
        assert result.returncode == 0, result.stderr
        output = result.stdout + result.stderr
        assert "Uninstalled" in output

        hooks_json = str(tmp_path / ".cursor" / "hooks.json")
        with open(hooks_json) as f:
            doc = json.load(f)
        for event, entries in doc.get("hooks", {}).items():
            for h in entries:
                cmd = h.get("command", "")
                assert "otel_hook" not in cmd and "otel-hook" not in cmd
