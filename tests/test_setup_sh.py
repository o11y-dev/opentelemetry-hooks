"""Tests for setup.sh command-selection logic.

Verifies that setup.sh defaults to the local script and only uses the
system-installed otel-hook command when OTEL_HOOK_USE_GLOBAL=1 is set.
"""

import json
import os
import shutil
import subprocess
import tempfile

import pytest

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


def _run_setup(hook_dir: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run setup.sh from *hook_dir*, capturing stdout/stderr."""
    full_env = os.environ.copy()
    # Remove any real otel-hook from the effective PATH so tests are
    # reproducible on machines that happen to have it installed.
    full_env.pop("OTEL_HOOK_USE_GLOBAL", None)
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
    """setup.sh must default to the local script path."""

    def test_default_uses_local_script(self, tmp_path):
        """Without OTEL_HOOK_USE_GLOBAL, setup.sh writes the local script command."""
        hook_dir = _make_hook_dir(str(tmp_path))
        result = _run_setup(hook_dir)
        assert result.returncode == 0, result.stderr

        cmds = _hooks_json_commands(str(tmp_path))
        assert len(cmds) == 1
        cmd = cmds[0]
        assert "otel_hook.py" in cmd, (
            f"Expected local otel_hook.py script in command, got: {cmd!r}"
        )
        assert cmd != "otel-hook", (
            "setup.sh should NOT use the global otel-hook by default"
        )

    def test_global_flag_not_set_ignores_path_otel_hook(self, tmp_path):
        """Even if a fake otel-hook is on PATH, the local script is used when
        OTEL_HOOK_USE_GLOBAL is unset."""
        hook_dir = _make_hook_dir(str(tmp_path))

        # Create a fake otel-hook on a temp PATH
        fake_bin = str(tmp_path / "fakebin")
        os.makedirs(fake_bin)
        fake_otel = os.path.join(fake_bin, "otel-hook")
        with open(fake_otel, "w") as f:
            f.write("#!/bin/sh\necho fake\n")
        os.chmod(fake_otel, 0o755)

        old_path = os.environ.get("PATH", "")
        env_override = {"PATH": f"{fake_bin}:{old_path}"}
        # Explicitly unset the opt-in flag
        env_override["OTEL_HOOK_USE_GLOBAL"] = ""

        result = _run_setup(hook_dir, env=env_override)
        assert result.returncode == 0, result.stderr

        cmds = _hooks_json_commands(str(tmp_path))
        assert len(cmds) == 1
        cmd = cmds[0]
        assert "otel_hook.py" in cmd, (
            f"Expected local script, got: {cmd!r} (fake otel-hook was on PATH)"
        )

    def test_use_global_flag_selects_otel_hook(self, tmp_path):
        """With OTEL_HOOK_USE_GLOBAL=1 and a fake otel-hook on PATH, setup.sh
        writes 'otel-hook' as the command."""
        hook_dir = _make_hook_dir(str(tmp_path))

        fake_bin = str(tmp_path / "fakebin")
        os.makedirs(fake_bin)
        fake_otel = os.path.join(fake_bin, "otel-hook")
        with open(fake_otel, "w") as f:
            f.write("#!/bin/sh\necho fake\n")
        os.chmod(fake_otel, 0o755)

        old_path = os.environ.get("PATH", "")
        env_override = {
            "PATH": f"{fake_bin}:{old_path}",
            "OTEL_HOOK_USE_GLOBAL": "1",
        }

        result = _run_setup(hook_dir, env=env_override)
        assert result.returncode == 0, result.stderr

        cmds = _hooks_json_commands(str(tmp_path))
        assert len(cmds) == 1
        cmd = cmds[0]
        assert cmd == "otel-hook", (
            f"Expected 'otel-hook' with OTEL_HOOK_USE_GLOBAL=1, got: {cmd!r}"
        )

    def test_use_global_flag_without_otel_hook_falls_back_to_local(self, tmp_path):
        """With OTEL_HOOK_USE_GLOBAL=1 but no otel-hook on PATH, setup.sh
        falls back to the local script."""
        hook_dir = _make_hook_dir(str(tmp_path))

        # Use a PATH that definitely doesn't have otel-hook
        env_override = {
            "PATH": "/usr/bin:/bin",
            "OTEL_HOOK_USE_GLOBAL": "1",
        }

        result = _run_setup(hook_dir, env=env_override)
        assert result.returncode == 0, result.stderr

        cmds = _hooks_json_commands(str(tmp_path))
        assert len(cmds) == 1
        cmd = cmds[0]
        assert "otel_hook.py" in cmd, (
            f"Expected local script fallback when otel-hook not found, got: {cmd!r}"
        )
