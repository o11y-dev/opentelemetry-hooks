"""Tests for setup.sh command-selection logic.

Verifies that setup.sh prefers the system-installed otel-hook command when it
is available on PATH, and falls back to the local script otherwise.
"""

import json
import os
import shutil
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP_SH = os.path.join(REPO_ROOT, "setup.sh")
OTEL_HOOK_PY = os.path.join(REPO_ROOT, "otel_hook.py")
OTEL_CONFIG_EXAMPLE = os.path.join(REPO_ROOT, "otel_config.example.json")


def _make_hook_dir(tmp_root: str) -> str:
    """Create a minimal .cursor/hooks/opentelemetry-hook/ directory tree
    inside *tmp_root* and return its absolute path."""
    hook_dir = os.path.join(tmp_root, ".cursor", "hooks", "opentelemetry-hook")
    os.makedirs(hook_dir, exist_ok=True)
    # Copy the files that setup.sh needs to find next to itself.
    shutil.copy(SETUP_SH, os.path.join(hook_dir, "setup.sh"))
    shutil.copy(OTEL_HOOK_PY, os.path.join(hook_dir, "otel_hook.py"))
    shutil.copy(OTEL_CONFIG_EXAMPLE, os.path.join(hook_dir, "otel_config.example.json"))
    return hook_dir


def _run_setup(hook_dir: str, env=None) -> subprocess.CompletedProcess:
    """Run setup.sh from *hook_dir*, capturing stdout/stderr.

    Individual tests control PATH via *env* to determine whether otel-hook
    is visible.  Tests that check the global-preferred behavior inject a fake
    otel-hook on the PATH; tests that check the fallback use a minimal PATH
    built from the real python3 location so that no accidental otel-hook
    leaks in from the developer's environment.
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    return subprocess.run(
        ["bash", os.path.join(hook_dir, "setup.sh")],
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


class TestSetupShCommandSelection:
    """setup.sh must prefer the global otel-hook command when available."""

    def test_default_uses_global_when_otel_hook_available(self, tmp_path):
        """When otel-hook is on PATH, setup.sh writes 'otel-hook' as the command."""
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
        assert cmd == "otel-hook", (
            f"Expected 'otel-hook' when it is on PATH, got: {cmd!r}"
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
