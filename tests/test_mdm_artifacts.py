"""Validate distributable deployment templates, guards and offline bundle integrity."""
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import zipfile

import pytest


def artifact_root():
    source = Path(__file__).resolve().parent.parent / "examples"
    return source if source.exists() else Path(sys.prefix) / "share" / "opentelemetry-hooks" / "examples"


@pytest.mark.parametrize("name", ["opentelemetry-hook", "claude-shared"])
def test_managed_profiles_have_typed_restrictions_and_matching_registry(name):
    root = artifact_root() / "mdm"
    profile = plistlib.loads((root / (name + ".mobileconfig")).read_bytes())
    payload = profile["PayloadContent"][0]
    assert payload["PayloadType"] == "dev.o11y.opentelemetry-hook"
    assert payload["enforce_export"] is True
    assert payload["capture"] == {"mode": "metadata_only"}
    assert payload["logs"] == {"enabled": False}
    registry = (root / (name + ".reg")).read_text()
    assert '"enforce_export"="true"' in registry
    assert 'HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\OpenTelemetryHook' in registry
    if name == "claude-shared":
        assert payload["claude"] == {"shared_config": True}
        assert "OTEL_EXPORTER_OTLP_HEADERS" not in payload
    else:
        assert payload["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=REPLACE_THROUGH_MDM"


def test_claude_example_covers_passive_events_with_missing_file_guard(tmp_path):
    from otel_hooks.cli import _CLAUDE_EVENTS
    doc = json.loads((artifact_root() / "claude-managed-settings.example.json").read_text())
    assert set(doc["hooks"]) == set(_CLAUDE_EVENTS)
    assert "REPLACE_THROUGH_MDM" in doc["env"]["OTEL_EXPORTER_OTLP_HEADERS"]
    for entries in doc["hooks"].values():
        for entry in entries:
            for hook in entry["hooks"]:
                command = hook["command"]
                assert "Authorization" not in command
                assert "--claude --managed" in command
    if sys.platform != "win32":
        command = doc["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        result = subprocess.run(["/bin/sh", "-c", command], input="{}", capture_output=True, text=True, timeout=5)
        assert result.returncode == 0 and json.loads(result.stdout) == {}


def test_shell_deployment_parses():
    if sys.platform == "win32":
        pytest.skip("bash is optional on Windows")
    result = subprocess.run(["bash", "-n", str(artifact_root() / "mdm" / "deploy-jamf.sh")], capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Validates Windows PowerShell 5.1 syntax on Windows CI")
def test_windows_deployment_parses(tmp_path):
    script = str(artifact_root() / "mdm" / "deploy-intune.ps1").replace("'", "''")
    command = "$tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseFile('" + script + "',[ref]$tokens,[ref]$errors) | Out-Null; if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 }"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    # Execute the actual launcher body with absent staging files and real flags.
    source = (artifact_root() / "mdm" / "deploy-intune.ps1").read_text()
    launcher = source.split("$launcherText = @'\n", 1)[1].split("\n'@", 1)[0]
    path = tmp_path / "otel-hook.ps1"
    path.write_text(launcher, encoding="utf-8")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path),
                             "--claude", "--managed"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and json.loads(result.stdout) == {}, result.stderr


def test_bundle_lock_uses_actual_wheel_hashes_and_rejects_duplicates(tmp_path):
    script = Path(__file__).resolve().parent.parent / "scripts" / "build_mdm_bundle.py"
    if not script.exists():
        pytest.skip("Bundle builder is shipped in the source distribution")
    spec = importlib.util.spec_from_file_location("build_mdm_bundle", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    wheel = tmp_path / "opentelemetry_hooks-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("opentelemetry_hooks-1.0.dist-info/METADATA", "Name: opentelemetry-hooks\nVersion: 1.0\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert module.lock_wheels(tmp_path) == f"opentelemetry-hooks==1.0 --hash=sha256:{digest}\n"
    (tmp_path / "duplicate.whl").write_bytes(wheel.read_bytes())
    with pytest.raises(ValueError, match="duplicate"):
        module.lock_wheels(tmp_path)
