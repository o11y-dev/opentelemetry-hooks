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


class TestSetupShIdeIdentityEnv:
    def _minimal_env(self, tmp_path) -> dict:
        python3_bin = shutil.which("python3") or "/usr/bin/python3"
        python3_dir = os.path.dirname(python3_bin)
        return {
            "HOME": str(tmp_path),
            "PATH": f"{python3_dir}:/usr/bin:/bin",
        }

    def test_cursor_new_hooks_include_explicit_ide_env(self, tmp_path):
        hook_dir = _make_hook_dir(str(tmp_path))
        result = _run_setup(hook_dir, args=["--cursor"], env=self._minimal_env(tmp_path))
        assert result.returncode == 0, result.stderr

        doc = _hooks_json_doc(str(tmp_path))
        for event_hooks in doc["hooks"].values():
            assert event_hooks == [{
                "command": event_hooks[0]["command"],
                "env": {"IDE_OTEL_IDE_NAME": "cursor"},
            }]

    def test_cursor_merge_adds_explicit_ide_env_to_existing_command(self, tmp_path):
        hook_dir = _make_hook_dir(str(tmp_path))
        hooks_json = tmp_path / ".cursor" / "hooks.json"
        hooks_json.parent.mkdir(parents=True, exist_ok=True)
        hooks_json.write_text(json.dumps({
            "version": 1,
            "hooks": {
                "sessionStart": [{"command": "python3 /tmp/other.py"}],
                "preToolUse": [{"command": "python3 %s/otel_hook.py" % hook_dir}],
            },
        }))

        result = _run_setup(hook_dir, args=["--cursor"], env=self._minimal_env(tmp_path))
        assert result.returncode == 0, result.stderr

        doc = _hooks_json_doc(str(tmp_path))
        matching = [h for h in doc["hooks"]["preToolUse"] if h["command"] == "python3 %s/otel_hook.py" % hook_dir]
        assert matching == [{
            "command": "python3 %s/otel_hook.py" % hook_dir,
            "env": {"IDE_OTEL_IDE_NAME": "cursor"},
        }]

    def test_claude_new_hooks_include_explicit_ide_env(self, tmp_path):
        hook_dir = _make_hook_dir(str(tmp_path))
        result = _run_setup(hook_dir, args=["--claude"], env=self._minimal_env(tmp_path))
        assert result.returncode == 0, result.stderr

        doc = _claude_settings_doc(str(tmp_path))
        for event_entries in doc["hooks"].values():
            for event_entry in event_entries:
                for hook in event_entry["hooks"]:
                    if hook["command"].endswith("otel_hook.py"):
                        assert hook["env"] == {"IDE_OTEL_IDE_NAME": "claude"}

    def test_claude_merge_adds_explicit_ide_env_to_existing_command(self, tmp_path):
        hook_dir = _make_hook_dir(str(tmp_path))
        settings_json = tmp_path / ".claude" / "settings.json"
        settings_json.parent.mkdir(parents=True)
        settings_json.write_text(json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "python3 %s/otel_hook.py" % hook_dir}],
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
            "env": {"IDE_OTEL_IDE_NAME": "claude"},
        }]


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


class TestExplicitIdeIdentityExamples:
    def _minimal_env(self, tmp_path) -> dict:
        python3_bin = shutil.which("python3") or "/usr/bin/python3"
        python3_dir = os.path.dirname(python3_bin)
        return {
            "HOME": str(tmp_path),
            "PATH": f"{python3_dir}:/usr/bin:/bin",
        }

    def test_setup_opencode_installs_global_plugin_with_explicit_ide_env(self, tmp_path):
        hook_dir = _make_hook_dir(str(tmp_path))
        config_dir = str(tmp_path / "opencode-config")
        env = self._minimal_env(tmp_path)
        env["OPENCODE_CONFIG_DIR"] = config_dir

        result = _run_setup(hook_dir, args=["--opencode", "--global"], env=env)
        assert result.returncode == 0, result.stderr

        plugin_text = _opencode_plugin_text(str(tmp_path), global_install=True, config_dir=config_dir)
        assert "IDE_OTEL_IDE_NAME=opencode otel-hook" in plugin_text

    def test_copilot_example_wraps_commands_with_explicit_ide_env(self):
        with open(COPILOT_EXAMPLE) as f:
            doc = json.load(f)

        for event_hooks in doc["hooks"].values():
            for hook in event_hooks:
                assert hook["bash"].startswith("env IDE_OTEL_IDE_NAME=copilot ")
