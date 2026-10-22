"""JSON settings and the compatibility boundary for legacy IDE_OTEL keys.

The runtime still reads legacy environment variables. Only this module translates
the public JSON format; arbitrary JSON keys are never exported to the environment.
"""
import json
import logging
import os
import sys
import urllib.parse

_MDM_DOMAIN = "dev.o11y.opentelemetry-hook"
_MDM_REGISTRY_PATH = r"SOFTWARE\Policies\OpenTelemetryHook"
_LOGGER = logging.getLogger("otel_hook")

# Only telemetry configuration is imported from another application's settings.
_SHARED_KEYS = {"OTEL_RESOURCE_ATTRIBUTES", "OTEL_SERVICE_NAME"}


def claude_settings_paths() -> list[str]:
    if sys.platform == "darwin":
        machine = "/Library/Application Support/ClaudeCode/managed-settings.json"
    elif sys.platform == "win32":
        machine = os.path.join(os.getenv("ProgramFiles", r"C:\Program Files"), "ClaudeCode", "managed-settings.json")
    else:
        machine = "/etc/claude-code/managed-settings.json"
    home = os.path.expanduser("~/.claude")
    return [machine, os.path.join(home, "remote-settings.json"), os.path.join(home, "settings.json")]


def shared_claude_settings(*, managed_only: bool = False) -> tuple[dict, dict]:
    """Read the same env block as Claude, without executing it or importing hooks."""
    result, sources = {}, {}
    paths = claude_settings_paths()
    for path in paths[:2] if managed_only else paths:
        doc = _load_json_config(path)
        env = doc.get("env", {})
        if not isinstance(env, dict):
            raise ValueError("Claude telemetry env must be an object")
        for key, value in env.items():
            if key not in _SHARED_KEYS and not key.startswith("OTEL_EXPORTER_OTLP"):
                continue
            if key not in result and isinstance(value, str) and value:
                result[key], sources[key] = value, path
    return result, sources


def export_enforced(managed: dict) -> bool:
    value = managed.get("enforce_export", False)
    if type(value) is not bool:
        raise ValueError("managed enforce_export must be a boolean")
    return value


def enforce_export_environment(managed: dict) -> None:
    """Clear alternate per-signal destinations/auth before applying trusted values."""
    if not export_enforced(managed):
        return
    endpoint = managed.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    protocol = managed.get("OTEL_EXPORTER_OTLP_PROTOCOL")
    if not isinstance(endpoint, str) or not endpoint or protocol not in {"grpc", "http/protobuf"}:
        raise ValueError("enforced export requires a managed endpoint and grpc or http/protobuf protocol")
    for key in list(os.environ):
        if key.startswith("OTEL_EXPORTER_OTLP"):
            del os.environ[key]
    for key, value in managed.items():
        if key.startswith("OTEL_EXPORTER_OTLP"):
            os.environ[key] = _coerce_env_value(key, value)


ALIASES = {
    "batch.on_stop": "BATCH_ON_STOP",
    "batch.disable_sdk_batch": "DISABLE_BATCH",
    "local_spans": "LOCAL_SPANS",
    "logs.enabled": "ENABLE_LOGS",
    "capture.user_identity": "CAPTURE_USER_IDENTITY",
    "capture.mask": "MASK_PROMPTS",
    "capture.max_string_length": "TEXT_MAX_CHARS",
    "diagnostics.level": "LOG_LEVEL",
    "diagnostics.file": "LOG_FILE",
    "diagnostics.events": "LOG_EVENTS",
    "diagnostics.console": "DEBUG_CONSOLE",
    "state.ttl_seconds": "STATE_TTL_SECONDS",
    "state.cleanup_interval_seconds": "STATE_CLEANUP_INTERVAL_SECONDS",
    "state.lock_timeout_seconds": "STATE_LOCK_TIMEOUT_SECONDS",
    "state.stale_flush_max_sessions": "STALE_FLUSH_MAX_SESSIONS",
    "service_name": "SERVICE_NAME",
    "app_name": "APP_NAME",
    "subsystem_name": "SUBSYSTEM_NAME",
}


def _headers_to_env(value: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in value.items() if v is not None)


def _coerce_env_value(key: str, value) -> str:
    if isinstance(value, dict) and key.endswith("_HEADERS"):
        return _headers_to_env(value)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def environment_settings(config: dict) -> dict:
    """New structured keys win over legacy flat keys within the same file."""
    result = {
        key: value for key, value in config.items()
        if key.startswith(("IDE_OTEL_", "OTEL_")) and value is not None
    }
    for path, suffix in ALIASES.items():
        value = config
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None:
            result[f"IDE_OTEL_{suffix}"] = value
    return result


def merge_settings(base: dict, override: dict) -> dict:
    """Merge objects, replacing lists and scalar values."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            value = merge_settings(result[key], value)
        result[key] = value
    return result


def _load_mdm_config_macos() -> dict:
    """Load managed preferences from macOS MDM profile."""
    try:
        import plistlib
        managed_path = f"/Library/Managed Preferences/{_MDM_DOMAIN}.plist"
        if os.path.exists(managed_path):
            with open(managed_path, "rb") as fh:
                result = plistlib.load(fh)
                if not isinstance(result, dict):
                    raise ValueError("managed preferences must be an object")
                return result
        # Fall back to current-user managed preferences
        user_managed = os.path.expanduser(
            f"~/Library/Managed Preferences/{_MDM_DOMAIN}.plist"
        )
        if os.path.exists(user_managed):
            with open(user_managed, "rb") as fh:
                result = plistlib.load(fh)
                if not isinstance(result, dict):
                    raise ValueError("managed preferences must be an object")
                return result
    except Exception as exc:
        raise ValueError("unable to read macOS managed preferences") from exc
    return {}


def _load_mdm_config_windows() -> dict:
    """Load managed configuration from Windows registry (HKLM)."""
    try:
        import winreg
        result = {}
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, _MDM_REGISTRY_PATH) as key:
                    idx = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(key, idx)
                            if name and value is not None:
                                result[name] = str(value)
                            idx += 1
                        except PermissionError as exc:
                            raise ValueError("unable to enumerate Windows managed policy") from exc
                        except OSError:
                            break
            except PermissionError as exc:
                raise ValueError("unable to read Windows managed policy") from exc
            except OSError:
                continue
        for name in ("capture", "logs", "claude", "enforce_export"):
            if name in result:
                try:
                    result[name] = json.loads(result[name])
                except json.JSONDecodeError:
                    # Keep the invalid value: validation disables telemetry
                    # instead of silently removing a managed restriction.
                    pass
        return result
    except ImportError:
        pass
    except Exception as exc:
        raise ValueError("unable to read Windows managed policy") from exc
    return {}


def _load_json_config(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            result = json.load(fh)
        if not isinstance(result, dict):
            raise ValueError("hook configuration must be a JSON object")
        return result
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("unable to read hook configuration") from exc


def _parse_resource_attributes(value: str) -> dict:
    attrs = {}
    for pair in (value or "").split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k.strip():
            attrs[k.strip()] = v.strip()
    return attrs


def _parse_otlp_headers(value: str) -> dict:
    headers = {}
    for pair in (value or "").split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, val = pair.split("=", 1)
        key = key.strip()
        if key:
            headers[key] = urllib.parse.unquote(val.strip())
    return headers
