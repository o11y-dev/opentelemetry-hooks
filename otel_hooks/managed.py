"""SDK-free managed registration, detection and duplicate suppression.

Machine settings are separate from the current-user ``setup --global`` command.
Only entries bearing this package's explicit registration marker are owned here.
"""
import copy
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

from .config import claude_settings_paths, _load_json_config


def registration_path(agent: str, scope: str) -> str:
    if scope == "user":
        return os.path.expanduser("~/.claude/settings.json" if agent == "claude" else "~/.cursor/hooks.json")
    if agent == "claude":
        return claude_settings_paths()[0]
    if sys.platform == "darwin":
        return "/Library/Application Support/Cursor/hooks.json"
    if sys.platform == "win32":
        return os.path.join(os.getenv("ProgramData", r"C:\ProgramData"), "Cursor", "hooks.json")
    return "/etc/cursor/hooks.json"


def _events(agent: str) -> list[str]:
    from .cli import _CLAUDE_EVENTS, _CURSOR_EVENTS
    return _CLAUDE_EVENTS if agent == "claude" else _CURSOR_EVENTS


def _marked(hook: dict, scope: str) -> bool:
    command = hook.get("command", "")
    args = hook.get("args", [])
    text = command + " " + " ".join(str(a) for a in args)
    marker = "--managed" if scope == "machine" else "--registration-scope user"
    return ("otel-hook" in text or "otel_hook" in text) and marker in text


def _commands(doc: dict, agent: str):
    for entries in doc.get("hooks", {}).values():
        for entry in entries:
            yield from entry.get("hooks", []) if agent == "claude" else [entry]


def _target_exists(hook: dict) -> bool:
    args = hook.get("args", [])
    if "-File" in args:
        target = args[args.index("-File") + 1]
    elif args:
        target = hook.get("command", "")
    else:
        tokens = shlex.split(hook.get("command", ""), posix=sys.platform != "win32")
        if "-File" in tokens:
            target = tokens[tokens.index("-File") + 1].strip('"')
        else:
            target = tokens[0].strip('"') if tokens else ""
    return os.path.isabs(target) and os.path.isfile(target)


def _validate(doc: dict, agent: str) -> None:
    hooks = doc.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    for entries in hooks.values():
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ValueError("hook entries must be arrays of objects")
        for entry in entries:
            children = entry.get("hooks", []) if agent == "claude" else [entry]
            if not isinstance(children, list) or any(not isinstance(h, dict) for h in children):
                raise ValueError("nested hook entries must be arrays of objects")


def _write(path: str, doc: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = (target.stat().st_mode & 0o777) if target.exists() else 0o644
    # Write complete JSON beside the destination, then atomically replace it.
    fd, temporary = tempfile.mkstemp(prefix=".otel-hook-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove(doc: dict, agent: str, scope: str) -> None:
    hooks = doc.get("hooks", {})
    for event in list(hooks):
        retained = []
        for entry in hooks[event]:
            if agent == "claude":
                remaining = [h for h in entry.get("hooks", []) if not _marked(h, scope)]
                if len(remaining) == len(entry.get("hooks", [])):
                    retained.append(entry)
                elif remaining:
                    retained.append({**entry, "hooks": remaining})
            elif not _marked(entry, scope):
                retained.append(entry)
        if retained:
            hooks[event] = retained
        else:
            del hooks[event]


def command(executable: str, agent: str, scope: str) -> dict:
    args = ["--" + agent, "--managed"] if scope == "machine" else ["--" + agent, "--registration-scope", "user"]
    if sys.platform == "win32":
        if executable.lower().endswith(".ps1"):
            prefix = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", executable]
            if agent == "claude":
                return {"type": "command", "command": "powershell.exe", "args": prefix + args, "timeout": 10}
            value = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + executable + '" ' + " ".join(args)
            return {"command": value, "timeout": 10}
        if agent == "claude":
            return {"type": "command", "command": executable, "args": args, "timeout": 10}
        value = '"' + executable + '" ' + " ".join(args)
    else:
        value = shlex.quote(executable) + " " + " ".join(args)
    return {**({"type": "command"} if agent == "claude" else {}), "command": value, "timeout": 10}


def register(agent: str, scope: str, executable: str) -> dict:
    if not os.path.isabs(executable) or not os.path.isfile(executable):
        raise ValueError("registration requires an existing absolute executable path")
    if os.path.basename(executable).lower() not in {"otel-hook", "otel-hook.exe", "otel-hook.ps1"}:
        raise ValueError("executable must be the installed otel-hook command")
    if scope == "user" and active(agent, "machine"):
        raise ValueError("machine registration is active; do not add a duplicate user registration")
    path = registration_path(agent, scope)
    doc = _load_json_config(path)
    _validate(doc, agent)
    original = copy.deepcopy(doc)
    _remove(doc, agent, scope)
    hooks = doc.setdefault("hooks", {})
    if agent == "cursor":
        doc.setdefault("version", 1)
    for event in _events(agent):
        hook = command(executable, agent, scope)
        entry = {"hooks": [hook]} if agent == "claude" else hook
        if agent == "claude" and event in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
            entry["matcher"] = "*"
        hooks.setdefault(event, []).append(entry)
    if doc != original:
        _write(path, doc)
    return inspect(agent, scope, executable)


def unregister(agent: str, scope: str) -> None:
    path = registration_path(agent, scope)
    doc = _load_json_config(path)
    _validate(doc, agent)
    original = copy.deepcopy(doc)
    _remove(doc, agent, scope)
    if doc != original:
        _write(path, doc)


def active(agent: str, scope: str, event: str | None = None) -> bool:
    if agent not in {"claude", "cursor"}:
        return False
    try:
        doc = _load_json_config(registration_path(agent, scope))
        _validate(doc, agent)
        events = _events(agent)
        if event is not None:
            events = [name for name in events if name.casefold() == event.casefold()]
            if not events:
                return False
        # Suppression is per callback; detection requires the complete event set.
        for name in events:
            entries = doc.get("hooks", {}).get(name, [])
            commands = [h for e in entries for h in (e.get("hooks", []) if agent == "claude" else [e])]
            if not any(_marked(h, scope) and _target_exists(h) for h in commands):
                return False
        return True
    except (OSError, ValueError, TypeError, IndexError):
        return False


def should_skip(agent: str, scope: str | None, event: str | None = None) -> bool:
    if scope != "machine" and active(agent, "machine", event):
        return True
    return scope is None and active(agent, "user", event)


def inspect(agent: str, scope: str, executable: str | None = None) -> dict:
    path = registration_path(agent, scope)
    doc = _load_json_config(path)
    _validate(doc, agent)
    issues = []
    if not active(agent, scope):
        issues.append("missing_or_incomplete_registration")
    if executable:
        expected = command(executable, agent, scope)
        for event in _events(agent):
            entries = doc.get("hooks", {}).get(event, [])
            commands = [h for e in entries for h in (e.get("hooks", []) if agent == "claude" else [e])]
            if sum(h == expected for h in commands) != 1 or sum(_marked(h, scope) for h in commands) != 1:
                issues.append("unexpected_command_or_duplicate")
                break
        if not os.path.isfile(executable):
            issues.append("missing_executable")
    return {"agent": agent, "scope": scope, "path": path, "issues": issues,
            "status": "degraded" if issues else "installed", "backend_delivery": "unverified"}
