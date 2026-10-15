"""Reusable hook logic must be safe to import without starting the runtime."""
import os
import subprocess
import sys


def test_support_modules_import_without_sdk_or_runtime_side_effects(tmp_path):
    hook_home = tmp_path / "hook-home"
    script = """
import importlib
import subprocess
import sys

def unexpected_process(*args, **kwargs):
    raise AssertionError("support-module import started a subprocess")

subprocess.Popen = unexpected_process
subprocess.run = unexpected_process
for name in ("bootstrap", "workspace", "identity", "enrichment", "lifecycle"):
    importlib.import_module(f"otel_hooks.{name}")
assert "otel_hook" not in sys.modules
assert not any(name.startswith("opentelemetry.") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "IDE_OTEL_HOOK_HOME": str(hook_home)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not hook_home.exists()
