"""Agent registration and diagnostics CLI; runtime loads only when needed."""
import glob
import json
import os
import re
import shlex
import shutil
import sys
import time
from typing import Optional

import click

from otel_hooks.parsers.common import _safe_bool
from otel_hooks.parsers.events import _CODEX_EVENTS, _COPILOT_EVENTS, _WINDSURF_EVENT_ALIASES
from otel_hooks.workspace import _find_repo_root


# ---------------------------------------------------------------------------
# Setup CLI: per-agent event lists
# ---------------------------------------------------------------------------
_CURSOR_EVENTS = [
    "sessionStart", "sessionEnd", "subagentStart", "subagentStop",
    "preToolUse", "postToolUse", "postToolUseFailure",
    "beforeShellExecution", "afterShellExecution",
    "beforeMCPExecution", "afterMCPExecution",
    "beforeReadFile", "afterFileEdit",
    "beforeSubmitPrompt", "stop",
    "preCompact", "beforeTabFileRead", "afterTabFileEdit",
    "afterAgentResponse", "afterAgentThought", "workspaceOpen",
]

_CLAUDE_EVENTS = [
    "SessionStart", "SessionEnd", "SubagentStart", "SubagentStop",
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "UserPromptSubmit", "PreCompact", "PostCompact", "Stop",
    "PermissionRequest", "PermissionDenied", "Notification", "StopFailure",
    "Setup", "InstructionsLoaded", "UserPromptExpansion", "MessageDisplay",
    "PostToolBatch", "TaskCreated", "TaskCompleted", "TeammateIdle",
    "ConfigChange", "CwdChanged", "DirectoryAdded", "WorktreeRemove",
    "PreModelSwitch", "PostModelSwitch", "Elicitation", "ElicitationResult",
]
# WorktreeCreate replaces git worktree creation; FileChanged installs watchers.
# Neither belongs in an automatically installed telemetry observer.
_CLAUDE_MATCHER_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}


_GEMINI_EVENTS = [
    "SessionStart", "SessionEnd", "BeforeAgent", "AfterAgent",
    "BeforeModel", "AfterModel", "BeforeTool", "AfterTool",
    "BeforeToolSelection", "Notification", "PreCompress",
]
_GEMINI_MATCHER_EVENTS = {"BeforeAgent", "AfterAgent", "BeforeModel", "AfterModel", "BeforeTool", "AfterTool"}

_CODEX_MATCHERS = {
    "SessionStart": "startup|resume|clear|compact",
    "PreToolUse": "*",
    "PermissionRequest": "*",
    "PostToolUse": "*",
}

# TranscriptReady is accepted, but registration would cause the harness to write
# full conversation transcripts independently of our content-capture setting.
_WINDSURF_EVENTS = [event for event in _WINDSURF_EVENT_ALIASES if event != "post_cascade_response_with_transcript"]

# OpenCode plugin — source filename (in plugin/) and destination filename (in plugins/).
_OPENCODE_PLUGIN_SOURCE_FILENAME = "opencode.ts"
_OPENCODE_PLUGIN_FILENAME = "otel-hook.ts"

# ---------------------------------------------------------------------------
# Setup CLI: helpers
# ---------------------------------------------------------------------------

def _resolve_hook_cmd() -> str:
    return shutil.which("otel-hook") or "otel-hook"


def _hook_cmd_for_agent(agent: str) -> str:
    executable = _resolve_hook_cmd()
    if sys.platform == "win32":
        if agent == "copilot":
            executable = "& '" + executable.replace("'", "''") + "'"
        else:
            executable = f'"{executable}"'
    else:
        executable = shlex.quote(executable)
    return f"{executable} --{agent}"


def _is_copilot_hook(hook: dict) -> bool:
    return any(
        marker in hook.get(shell, "")
        for shell in ("bash", "powershell") for marker in ("otel-hook", "otel_hook")
    )


def _load_json_file(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError as exc:
                raise click.ClickException(f"Invalid JSON in {path}: {exc}") from exc
    return {}


def _write_json_file(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _ensure_toml_bool(path: str, section: str, key: str, value: bool) -> None:
    """Set a boolean in a simple TOML section while preserving unrelated text."""
    desired = "true" if value else "false"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = []

    section_header = f"[{section}]"
    section_start = None
    section_end = len(lines)
    section_re = re.compile(r"^\s*\[.*\]\s*$")
    for index, line in enumerate(lines):
        if line.strip() == section_header:
            section_start = index
            section_end = len(lines)
            for probe in range(index + 1, len(lines)):
                if section_re.match(lines[probe]):
                    section_end = probe
                    break
            break

    new_line = f"{key} = {desired}\n"
    if section_start is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend([f"{section_header}\n", new_line])
    else:
        key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for index in range(section_start + 1, section_end):
            if key_re.match(lines[index]):
                if lines[index] == new_line:
                    return
                lines[index] = new_line
                break
        else:
            lines.insert(section_end, new_line)

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _remove_toml_key(path: str, section: str, key: str) -> None:
    """Remove a key from a simple TOML section while preserving unrelated text."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    section_header = f"[{section}]"
    section_start = None
    section_end = len(lines)
    section_re = re.compile(r"^\s*\[.*\]\s*$")
    for index, line in enumerate(lines):
        if line.strip() == section_header:
            section_start = index
            section_end = len(lines)
            for probe in range(index + 1, len(lines)):
                if section_re.match(lines[probe]):
                    section_end = probe
                    break
            break

    if section_start is None:
        return

    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    new_lines = []
    removed = False
    for index, line in enumerate(lines):
        if section_start < index < section_end and key_re.match(line):
            removed = True
            continue
        new_lines.append(line)

    if not removed:
        return

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def _detect_available_agents() -> list:
    """Return list of agent names whose home dirs or commands exist."""
    found = []
    home = os.path.expanduser("~")
    if os.path.isdir(os.path.join(home, ".cursor")) or shutil.which("cursor"):
        found.append("cursor")
    if os.path.isdir(os.path.join(home, ".claude")) or shutil.which("claude"):
        found.append("claude")
    # Copilot: detect via gh CLI or a .github dir in the current working directory
    if shutil.which("gh") or os.path.isdir(os.path.join(os.getcwd(), ".github")):
        found.append("copilot")
    if os.path.isdir(os.path.join(home, ".gemini")) or shutil.which("gemini"):
        found.append("gemini")
    if shutil.which("opencode") or os.path.isdir(os.path.join(home, ".config", "opencode")):
        found.append("opencode")
    if os.path.isdir(os.path.join(home, ".codeium", "windsurf")) or shutil.which("windsurf"):
        found.append("windsurf")
    if os.path.isdir(os.path.join(home, ".codex")) or shutil.which("codex"):
        found.append("codex")
    return found


def _find_opencode_plugin_source() -> Optional[str]:
    """Return the absolute path to the bundled plugin/opencode.ts source file.

    Looks next to otel_hook.py first (source checkout), then in the installed
    package data location (pipx / pip install).
    """
    # Source checkout: plugin/ lives next to otel_hook.py
    candidate = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin", _OPENCODE_PLUGIN_SOURCE_FILENAME)
    if os.path.isfile(candidate):
        return candidate
    # Installed package data: setuptools places data files under <prefix>/share/
    # Check the venv that contains this very module (handles pipx venvs correctly),
    # then sys.prefix, then ~/.local as a last-resort fallback.
    module_file = os.path.abspath(__file__)
    # Walk up from the module file to find a share/ sibling (handles any venv layout)
    check = os.path.dirname(module_file)
    for _ in range(6):  # at most 6 levels up
        share_candidate = os.path.join(check, "share", "opentelemetry-hooks", "plugin", _OPENCODE_PLUGIN_SOURCE_FILENAME)
        if os.path.isfile(share_candidate):
            return share_candidate
        parent = os.path.dirname(check)
        if parent == check:
            break
        check = parent
    # Fallbacks: sys.prefix, sys.real_prefix, ~/.local
    for prefix in (sys.prefix, getattr(sys, "real_prefix", None), os.path.expanduser("~/.local")):
        if not prefix:
            continue
        data_candidate = os.path.join(prefix, "share", "opentelemetry-hooks", "plugin", _OPENCODE_PLUGIN_SOURCE_FILENAME)
        if os.path.isfile(data_candidate):
            return data_candidate
    return None


# ---------------------------------------------------------------------------
# Setup CLI: per-agent setup functions (public API, importable)
# ---------------------------------------------------------------------------

def _configure_managed_hook_command(hook: dict, source: str) -> bool:
    """Stamp a setup-managed hook command with the source-agent CLI flag."""
    exec_form = sys.platform == "win32" and source == "claude"
    desired_cmd = _resolve_hook_cmd() if exec_form else _hook_cmd_for_agent(source)
    changed = False
    if hook.get("command") != desired_cmd:
        hook["command"] = desired_cmd
        changed = True
    if exec_form:
        if hook.get("args") != ["--claude"]:
            hook["args"] = ["--claude"]
            changed = True
        if "shell" in hook:
            del hook["shell"]
            changed = True
    elif source == "claude" and "args" in hook:
        # Converting a managed Windows exec registration back to shell form.
        del hook["args"]
        changed = True
    if source == "codex" and sys.platform == "win32" and hook.get("commandWindows") != desired_cmd:
        hook["commandWindows"] = desired_cmd
        changed = True
    env = hook.get("env")
    if isinstance(env, dict):
        next_env = dict(env)
        next_env.pop("IDE_OTEL_IDE_NAME", None)
        next_env.pop("IDE_OTEL_HOOK_SOURCE", None)
        if next_env:
            if next_env != env:
                hook["env"] = next_env
                changed = True
        else:
            hook.pop("env", None)
            changed = True
    return changed


def setup_cursor(global_: bool = True, cwd: str = ".") -> None:
    """Register otel-hook in Cursor's hooks.json."""
    hook_cmd = _resolve_hook_cmd()
    if global_:
        hooks_path = os.path.join(os.path.expanduser("~"), ".cursor", "hooks.json")
    else:
        repo = _find_repo_root(cwd)
        hooks_path = os.path.join(repo, ".cursor", "hooks.json")

    doc = _load_json_file(hooks_path)
    doc.setdefault("version", 1)
    hooks = doc.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _CURSOR_EVENTS:
        event_hooks = hooks.setdefault(event, [])
        matches = [h for h in event_hooks if "otel-hook" in h.get("command", "") or "otel_hook" in h.get("command", "")]
        if matches:
            changed = False
            for hook in matches:
                changed = _configure_managed_hook_command(hook, "cursor") or changed
            (updated if changed else skipped).append(event)
        else:
            hook = {"command": hook_cmd}
            _configure_managed_hook_command(hook, "cursor")
            event_hooks.append(hook)
            added.append(event)

    _write_json_file(hooks_path, doc)
    _log_setup_result("cursor", hooks_path, added, updated, skipped)


def setup_windsurf(global_: bool = True, cwd: str = ".") -> None:
    """Register native Cascade hooks (also supported by Devin Desktop)."""
    hook_cmd = _resolve_hook_cmd()
    if global_:
        hooks_path = os.path.join(os.path.expanduser("~"), ".codeium", "windsurf", "hooks.json")
    else:
        repo = _find_repo_root(cwd)
        hooks_path = os.path.join(repo, ".windsurf", "hooks.json")
        devin_path = os.path.join(repo, ".devin", "hooks.json")
        if _load_json_file(devin_path).get("hooks"):
            hooks_path = devin_path

    doc = _load_json_file(hooks_path)
    hooks = doc.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _WINDSURF_EVENTS:
        event_hooks = hooks.setdefault(event, [])
        matches = [h for h in event_hooks if "otel-hook" in h.get("command", "") or "otel_hook" in h.get("command", "")]
        if matches:
            changed = False
            for hook in matches:
                changed = _configure_managed_hook_command(hook, "windsurf") or changed
            (updated if changed else skipped).append(event)
        else:
            hook = {"command": hook_cmd}
            _configure_managed_hook_command(hook, "windsurf")
            event_hooks.append(hook)
            added.append(event)

    _write_json_file(hooks_path, doc)
    _log_setup_result("windsurf", hooks_path, added, updated, skipped)


def setup_claude(global_: bool = True, cwd: str = ".") -> None:
    """Register otel-hook in Claude Code's settings.json."""
    hook_cmd = _resolve_hook_cmd()
    if global_:
        settings_path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    else:
        repo = _find_repo_root(cwd)
        settings_path = os.path.join(repo, ".claude", "settings.json")

    settings = _load_json_file(settings_path)
    hooks = settings.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _CLAUDE_EVENTS:
        event_list = hooks.setdefault(event, [])
        others, exact = [], []
        desired_cmd = _hook_cmd_for_agent("claude")
        for entry in event_list:
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "otel_hook" in cmd or "otel-hook" in cmd:
                    (exact if cmd == desired_cmd else others).append(h)

        if exact:
            changed = False
            for hook in exact:
                changed = _configure_managed_hook_command(hook, "claude") or changed
            (updated if changed else skipped).append(event)
            continue

        if others:
            for hook in others:
                _configure_managed_hook_command(hook, "claude")
            updated.append(event)
            continue

        hook = {"type": "command", "command": hook_cmd}
        _configure_managed_hook_command(hook, "claude")
        hook_entry: dict = {"hooks": [hook]}
        if event in _CLAUDE_MATCHER_EVENTS:
            hook_entry["matcher"] = "*"
        event_list.append(hook_entry)
        added.append(event)

    _write_json_file(settings_path, settings)
    _log_setup_result("claude", settings_path, added, updated, skipped)


def setup_copilot(cwd: str = ".", global_: bool = False) -> None:
    """Register otel-hook in GitHub Copilot's otel-hooks.json."""
    hook_cmd = _hook_cmd_for_agent("copilot")
    command_key = "powershell" if sys.platform == "win32" else "bash"
    repo = _find_repo_root(cwd)
    hooks_path = os.path.join(repo, ".github", "hooks", "otel-hooks.json")
    if global_:
        copilot_home = os.getenv("COPILOT_HOME") or os.path.join(os.path.expanduser("~"), ".copilot")
        hooks_path = os.path.join(copilot_home, "hooks", "otel-hooks.json")

    doc = _load_json_file(hooks_path)
    doc.setdefault("version", 1)
    hooks = doc.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _COPILOT_EVENTS:
        # Native Copilot callbacks do not carry hook_event_name.
        event_cmd = f"{hook_cmd} --event {event}"
        event_hooks = hooks.setdefault(event, [])
        plain = [h for h in event_hooks if _is_copilot_hook(h)]
        if plain:
            changed = any("timeoutSec" not in h or h.get(command_key) != event_cmd for h in plain)
            for h in plain:
                h[command_key] = event_cmd
                h.setdefault("timeoutSec", 30)
            (updated if changed else skipped).append(event)
            continue

        event_hooks.append({"type": "command", command_key: event_cmd, "timeoutSec": 30})
        added.append(event)

    _write_json_file(hooks_path, doc)
    _log_setup_result("copilot", hooks_path, added, updated, skipped)


def setup_gemini(global_: bool = True, cwd: str = ".") -> None:
    """Register otel-hook in Gemini CLI's settings.json."""
    hook_cmd = _resolve_hook_cmd()
    if global_:
        settings_path = os.path.join(os.path.expanduser("~"), ".gemini", "settings.json")
    else:
        repo = _find_repo_root(cwd)
        settings_path = os.path.join(repo, ".gemini", "settings.json")

    settings = _load_json_file(settings_path)
    hooks = settings.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _GEMINI_EVENTS:
        event_list = hooks.setdefault(event, [])
        others, exact = [], []
        desired_cmd = _hook_cmd_for_agent("gemini")
        for entry in event_list:
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "otel_hook" in cmd or "otel-hook" in cmd:
                    (exact if cmd == desired_cmd else others).append(h)

        if exact:
            changed = False
            for hook in exact:
                changed = _configure_managed_hook_command(hook, "gemini") or changed
            (updated if changed else skipped).append(event)
            continue

        if others:
            for hook in others:
                _configure_managed_hook_command(hook, "gemini")
            updated.append(event)
            continue

        hook = {"type": "command", "command": hook_cmd, "name": "otel-hook"}
        _configure_managed_hook_command(hook, "gemini")
        hook_entry: dict = {"hooks": [hook]}
        if event in _GEMINI_MATCHER_EVENTS:
            hook_entry["matcher"] = "*"
        event_list.append(hook_entry)
        added.append(event)

    _write_json_file(settings_path, settings)
    _log_setup_result("gemini", settings_path, added, updated, skipped)


def setup_codex(global_: bool = True, cwd: str = ".") -> None:
    """Register otel-hook in Codex's hooks.json and enable Codex hooks."""
    hook_cmd = _resolve_hook_cmd()
    if global_:
        codex_dir = os.path.join(os.path.expanduser("~"), ".codex")
    else:
        repo = _find_repo_root(cwd)
        codex_dir = os.path.join(repo, ".codex")

    hooks_path = os.path.join(codex_dir, "hooks.json")
    config_path = os.path.join(codex_dir, "config.toml")
    _ensure_toml_bool(config_path, "features", "hooks", True)
    _remove_toml_key(config_path, "features", "codex_hooks")

    doc = _load_json_file(hooks_path)
    hooks = doc.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _CODEX_EVENTS:
        timeout = 3 if event in {"SessionEnd", "Interrupt"} else 30
        event_list = hooks.setdefault(event, [])
        matcher = _CODEX_MATCHERS.get(event)
        if event == "SessionStart":
            # Move only our legacy registration, even if the new one already exists.
            for entry in list(event_list):
                if entry.get("matcher") == "startup|resume|clear":
                    entry["hooks"] = [h for h in entry.get("hooks", []) if not any(token in h.get("command", "") for token in ("otel-hook", "otel_hook"))]
                    if not entry["hooks"]:
                        event_list.remove(entry)
        matches = []
        for entry in event_list:
            if matcher is not None and entry.get("matcher", "") != matcher:
                continue
            if matcher is None and "matcher" in entry:
                continue
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "otel_hook" in cmd or "otel-hook" in cmd:
                    matches.append(h)

        if matches:
            changed = False
            desired_cmd = _hook_cmd_for_agent("codex")
            for hook in matches:
                if hook.get("command") != desired_cmd:
                    hook["command"] = desired_cmd
                    changed = True
                if hook.get("type") != "command":
                    hook["type"] = "command"
                    changed = True
                if "timeout" not in hook or (event in {"SessionEnd", "Interrupt"} and hook["timeout"] != timeout):
                    hook["timeout"] = timeout
                    changed = True
                changed = _configure_managed_hook_command(hook, "codex") or changed
            (updated if changed else skipped).append(event)
            continue

        hook = {"type": "command", "command": hook_cmd, "timeout": timeout}
        _configure_managed_hook_command(hook, "codex")
        hook_entry: dict = {
            "hooks": [hook]
        }
        if matcher is not None:
            hook_entry["matcher"] = matcher
        event_list.append(hook_entry)
        added.append(event)

    _write_json_file(hooks_path, doc)
    if added or updated:
        click.echo(f"  ✓ [codex] Enabled hooks feature ({config_path})")
    _log_setup_result("codex", hooks_path, added, updated, skipped)


def setup_opencode(global_: bool = True, cwd: str = ".") -> None:
    """Install the otel-hook TypeScript plugin into OpenCode's plugins directory.

    Global install (default): ~/.config/opencode/plugins/otel-hook.ts
    Project install:           <repo>/.opencode/plugins/otel-hook.ts
    """
    src = _find_opencode_plugin_source()
    if src is None:
        raise click.ClickException(
            "Cannot find plugin/opencode.ts — ensure the opentelemetry-hooks package "
            "is installed correctly (pip install opentelemetry-hooks)."
        )

    home = os.path.expanduser("~")
    if global_:
        plugins_dir = os.path.join(home, ".config", "opencode", "plugins")
    else:
        repo = _find_repo_root(cwd)
        plugins_dir = os.path.join(repo, ".opencode", "plugins")

    dest = os.path.join(plugins_dir, _OPENCODE_PLUGIN_FILENAME)

    # Check if already installed and identical
    if os.path.isfile(dest):
        with open(src, encoding="utf-8") as f:
            src_content = f.read()
        with open(dest, encoding="utf-8") as f:
            dest_content = f.read()
        if src_content == dest_content:
            click.echo(f"  · [opencode] Already up to date ({dest})")
            return
        # Content differs — update
        os.makedirs(plugins_dir, exist_ok=True)
        shutil.copy2(src, dest)
        click.echo(f"  ✓ [opencode] Updated plugin ({dest})")
        return

    os.makedirs(plugins_dir, exist_ok=True)
    shutil.copy2(src, dest)
    click.echo(f"  ✓ [opencode] Installed plugin ({dest})")


def setup_agent(agent: str, global_: bool = True, cwd: str = ".") -> None:
    """Dispatcher: configure hooks for a single agent by name."""
    if agent == "cursor":
        setup_cursor(global_=global_, cwd=cwd)
    elif agent == "windsurf":
        setup_windsurf(global_=global_, cwd=cwd)
    elif agent == "claude":
        setup_claude(global_=global_, cwd=cwd)
    elif agent == "copilot":
        setup_copilot(cwd=cwd, global_=global_)
    elif agent == "gemini":
        setup_gemini(global_=global_, cwd=cwd)
    elif agent == "codex":
        setup_codex(global_=global_, cwd=cwd)
    elif agent == "opencode":
        setup_opencode(global_=global_, cwd=cwd)
    else:
        raise ValueError(f"Unknown agent: {agent}")


def _log_setup_result(agent: str, path: str, added: list, updated: list, skipped: list) -> None:
    if added:
        click.echo(f"  ✓ [{agent}] Added {len(added)} events ({path})")
    if updated:
        click.echo(f"  ✓ [{agent}] Updated {len(updated)} events")
    if not added and not updated:
        click.echo(f"  · [{agent}] Already up to date ({path})")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _runtime(ctx: click.Context):
    """Load the hook runtime only for execution or health inspection."""
    root = ctx.find_root()
    if root.obj is None:
        import otel_hook
        root.obj = otel_hook
    runtime = root.obj
    runtime._CLI_HOOK_SOURCE = root.params.get("hook_source")
    runtime._CLI_HOOK_EVENT = root.params.get("hook_event")
    return runtime


@click.group(invoke_without_command=True)
@click.option("--cursor", "hook_source", flag_value="cursor", default=None, help="Run hook as Cursor.")
@click.option("--windsurf", "hook_source", flag_value="windsurf", help="Run hook as Windsurf.")
@click.option("--claude", "hook_source", flag_value="claude", help="Run hook as Claude Code.")
@click.option("--copilot", "hook_source", flag_value="copilot", help="Run hook as GitHub Copilot.")
@click.option("--gemini", "hook_source", flag_value="gemini", help="Run hook as Gemini CLI.")
@click.option("--codex", "hook_source", flag_value="codex", help="Run hook as Codex CLI.")
@click.option("--opencode", "hook_source", flag_value="opencode", help="Run hook as OpenCode.")
@click.option("--event", "hook_event", default=None, help="Hook event name for runners that omit it from stdin.")
@click.pass_context
def cli(ctx: click.Context, hook_source: Optional[str], hook_event: Optional[str]) -> None:
    """otel-hook — OpenTelemetry hook runner and setup CLI for AI coding agents.

    When called with no subcommand and piped stdin, runs as the hook runner
    (IDE event JSON → OTel spans). Use subcommands to configure agent hooks.
    """
    if ctx.invoked_subcommand is None:
        if not sys.stdin.isatty():
            raise SystemExit(_runtime(ctx).main())
        else:
            click.echo(ctx.get_help())


@cli.command("setup")
@click.option(
    "--agent", "agents",
    type=click.Choice(["cursor", "windsurf", "claude", "copilot", "gemini", "codex", "opencode"]),
    multiple=True,
    help="Agent to configure. Omit to auto-detect all available agents.",
)
@click.option("--global/--no-global", "global_", default=True, show_default=True,
              help="Install to global agent config (~/.claude, ~/.cursor, etc.)")
@click.option("--cwd", default=".", show_default=True,
              help="Project root for project-scoped installs.")
def setup_cmd(agents: tuple, global_: bool, cwd: str) -> None:
    """Register otel-hook in one or more AI agent configs."""
    targets = list(agents) or _detect_available_agents()
    if not targets:
        click.echo("No agents detected. Use --agent cursor|windsurf|claude|copilot|gemini|codex|opencode to specify one.", err=True)
        raise SystemExit(1)
    errors = []
    for agent in targets:
        try:
            setup_agent(agent, global_=global_, cwd=cwd)
        except Exception as exc:
            click.echo(f"  ✗ [{agent}] {exc}", err=True)
            errors.append(agent)
    if errors:
        raise SystemExit(1)


_SUPPORTED_AGENTS = ("cursor", "windsurf", "claude", "copilot", "gemini", "codex", "opencode")


def _agent_config_paths(global_: bool, cwd: str) -> dict[str, str]:
    home = os.path.expanduser("~")
    repo_root = _find_repo_root(cwd)
    return {
        "cursor": os.path.join(home, ".cursor", "hooks.json") if global_ else os.path.join(repo_root, ".cursor", "hooks.json"),
        "windsurf": os.path.join(home, ".codeium", "windsurf", "hooks.json") if global_ else (
            os.path.join(repo_root, ".devin", "hooks.json") if _load_json_file(os.path.join(repo_root, ".devin", "hooks.json")).get("hooks")
            else os.path.join(repo_root, ".windsurf", "hooks.json")
        ),
        "claude": os.path.join(home, ".claude", "settings.json") if global_ else os.path.join(repo_root, ".claude", "settings.json"),
        "copilot": os.path.join(os.getenv("COPILOT_HOME") or os.path.join(home, ".copilot"), "hooks", "otel-hooks.json") if global_ else os.path.join(repo_root, ".github", "hooks", "otel-hooks.json"),
        "gemini": os.path.join(home, ".gemini", "settings.json") if global_ else os.path.join(repo_root, ".gemini", "settings.json"),
        "codex": os.path.join(home, ".codex", "hooks.json") if global_ else os.path.join(repo_root, ".codex", "hooks.json"),
        "opencode": (
            os.path.join(home, ".config", "opencode", "plugins", _OPENCODE_PLUGIN_FILENAME)
            if global_
            else os.path.join(repo_root, ".opencode", "plugins", _OPENCODE_PLUGIN_FILENAME)
        ),
    }


def _registered_hook_events(agent: str, path: str) -> list[str]:
    if agent == "opencode":
        return ["plugin"] if os.path.isfile(path) else []
    if not os.path.exists(path):
        return []
    doc = _load_json_file(path)
    hooks = doc.get("hooks", {})
    enabled = set()
    for event_name, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else []:
            for field in ("command", "bash", "powershell"):
                command = entry.get(field, "")
                if "otel-hook" in command or "otel_hook" in command:
                    enabled.add(event_name)
            for nested in entry.get("hooks", []):
                command = nested.get("command", "")
                if "otel-hook" in command or "otel_hook" in command:
                    enabled.add(event_name)
    return sorted(enabled)


def _pending_state_summary(runtime) -> dict:
    def files(directory: str, pattern: str) -> list[str]:
        return glob.glob(os.path.join(directory, pattern)) if os.path.isdir(directory) else []

    sessions = files(runtime._SESSION_DIR, "*.json")
    batches = files(runtime._BATCH_DIR, "*.jsonl")
    locks = files(runtime._LOCK_DIR, "*.lock")
    mtimes = [os.path.getmtime(path) for path in sessions + batches if os.path.exists(path)]
    oldest_age = max(0, int(time.time() - min(mtimes))) if mtimes else 0
    return {
        "sessions": len(sessions),
        "batches": len(batches),
        "locks": len(locks),
        "oldest_pending_age_seconds": oldest_age,
        "state_directory_writable": os.access(runtime._STATE_DIR, os.W_OK) if os.path.exists(runtime._STATE_DIR) else os.access(runtime._HOOK_DIR, os.W_OK),
    }


def _doctor_report(runtime, agents: tuple, global_: bool, cwd: str) -> tuple[dict, int]:
    targets = list(agents) or list(_SUPPORTED_AGENTS)
    paths = _agent_config_paths(global_, cwd)
    registrations = []
    for agent in targets:
        path = paths[agent]
        enabled_events = _registered_hook_events(agent, path)
        registrations.append({
            "agent": agent,
            "path": path,
            "registered_events": len(enabled_events),
            "enabled_events": enabled_events,
        })

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    health = runtime._load_delivery_health()
    signal_health = health.get("signals", {}) if isinstance(health, dict) else {}
    logs_enabled = _safe_bool(os.getenv("IDE_OTEL_ENABLE_LOGS", "false"))

    def signal_failed_recently(signal: str) -> bool:
        record = signal_health.get(signal, {})
        return bool(
            isinstance(record, dict)
            and record.get("last_failure_at_ns", 0)
            >= record.get("last_success_at_ns", 0)
            and record.get("last_failure_at_ns")
        )

    exporter_status = "disabled"
    if endpoint:
        exporter_status = "configured_unknown"
        traces = signal_health.get("traces", {})
        if signal_failed_recently("traces") or (logs_enabled and signal_failed_recently("logs")):
            exporter_status = "failing_recent"
        elif traces.get("last_success_at_ns"):
            exporter_status = "healthy_recent"
    elif runtime._local_spans_enabled():
        exporter_status = "local_only"

    failures = []
    for signal, record in signal_health.items():
        if not isinstance(record, dict):
            continue
        if record.get("last_failure_at_ns", 0) >= record.get("last_success_at_ns", 0) and record.get("last_failure_at_ns"):
            failures.append({
                "signal": signal,
                "at_ns": record.get("last_failure_at_ns"),
                "endpoint": record.get("endpoint"),
                "error": record.get("last_error"),
            })

    distro = runtime._hook_distro_attributes()
    report = {
        "schema_version": 1,
        "status": "healthy",
        "package": {
            "name": "opentelemetry-hooks",
            "version": distro.get("telemetry.distro.version", "source-checkout"),
            "executable": shutil.which("otel-hook"),
        },
        "detected_agent": runtime._detect_ide({}),
        "hook_schema_version": runtime._HOOK_SCHEMA_VERSION,
        "registrations": registrations,
        "privacy": {
            "capture_mode": runtime._CAPTURE_POLICY.mode,
            "hook_log_body": (
                runtime._CAPTURE_POLICY.mode if logs_enabled else "disabled"
            ),
            "conversation_content": runtime._CAPTURE_POLICY.content_enabled,
            "tool_input_content": runtime._CAPTURE_POLICY.content_enabled,
            "user_identity": _safe_bool(os.getenv("IDE_OTEL_CAPTURE_USER_IDENTITY", "")),
            "masking": _safe_bool(os.getenv("IDE_OTEL_MASK_PROMPTS", "")),
        },
        "exporter": {
            "status": exporter_status,
            "protocol": (os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL") or "grpc").lower(),
            "endpoint": runtime._sanitized_exporter_endpoint(endpoint),
            "logs_enabled": logs_enabled,
            "headers_present": bool(os.getenv("OTEL_EXPORTER_OTLP_HEADERS")),
        },
        "state": _pending_state_summary(runtime),
        "recent_delivery_failures": failures,
    }
    warnings = []
    if not any(item["registered_events"] for item in registrations):
        warnings.append("no requested agent hooks are registered")
    if exporter_status in {"disabled", "failing_recent"}:
        warnings.append("authoritative trace exporter or enabled log exporter is not healthy")
    if not report["state"]["state_directory_writable"]:
        warnings.append("hook state directory is not writable")
    if warnings:
        report["status"] = "degraded"
        report["warnings"] = warnings
        return report, 1
    return report, 0


def _doctor_human_lines(report: dict) -> list[str]:
    """Render complete or minimal doctor reports without raising another error."""
    status = report.get("status") or "unknown"
    lines = [f"otel-hook doctor: {status}"]
    error = report.get("error")
    if status == "error" or isinstance(error, dict):
        error_type = error.get("type") if isinstance(error, dict) else None
        lines.append(f"  error: {error_type or 'InternalError'}")
        return lines

    package = report.get("package") or {}
    lines.append(
        f"  package: {package.get('version', 'unknown')} "
        f"({package.get('executable') or 'not on PATH'})"
    )
    lines.append(f"  detected agent: {report.get('detected_agent') or 'unknown'}")
    for registration in report.get("registrations") or []:
        if not isinstance(registration, dict):
            continue
        lines.append(
            f"  [{registration.get('agent', 'unknown')}] "
            f"{registration.get('registered_events', 0)} events "
            f"({registration.get('path') or 'unknown path'})"
        )
    exporter = report.get("exporter") or {}
    lines.append(
        f"  exporter: {exporter.get('status', 'unknown')} "
        f"({exporter.get('endpoint') or 'no endpoint'})"
    )
    state = report.get("state") or {}
    lines.append(
        f"  state: {state.get('sessions', 0)} sessions, "
        f"{state.get('batches', 0)} batches"
    )
    for warning in report.get("warnings") or []:
        lines.append(f"  warning: {warning}")
    return lines


@cli.command("doctor")
@click.option(
    "--agent", "agents",
    type=click.Choice(list(_SUPPORTED_AGENTS)),
    multiple=True,
    help="Agent to inspect. Omit to inspect all.",
)
@click.option("--global/--no-global", "global_", default=True)
@click.option("--cwd", default=".")
@click.option("--json", "json_output", is_flag=True, help="Emit a stable machine-readable report.")
@click.pass_context
def doctor_cmd(ctx: click.Context, agents: tuple, global_: bool, cwd: str, json_output: bool) -> None:
    """Inspect hook registration, privacy, exporter health, and pending state."""
    try:
        runtime = _runtime(ctx)
        runtime._apply_config_env(runtime._load_config())
        report, exit_code = _doctor_report(runtime, agents, global_, cwd)
    except Exception as exc:
        report = {
            "schema_version": 1,
            "status": "error",
            "error": {"type": type(exc).__name__},
        }
        exit_code = 2
    if json_output:
        click.echo(json.dumps(report, ensure_ascii=True, sort_keys=True))
    else:
        for line in _doctor_human_lines(report):
            click.echo(line)
    if exit_code:
        raise SystemExit(exit_code)


@cli.command("diagnose")
@click.option(
    "--agent", "agents",
    type=click.Choice(["cursor", "windsurf", "claude", "copilot", "gemini", "codex", "opencode"]),
    multiple=True,
    help="Agent to check. Omit to check all.",
)
@click.option("--global/--no-global", "global_", default=True)
@click.option("--cwd", default=".")
def diagnose_cmd(agents: tuple, global_: bool, cwd: str) -> None:
    """Show hook registration status for each agent."""
    targets = list(agents) or list(_SUPPORTED_AGENTS)
    paths = _agent_config_paths(global_, cwd)

    for agent in targets:
        path = paths[agent]
        if agent == "opencode":
            if os.path.isfile(path):
                click.echo(f"  ✓ [opencode] Plugin installed ({path})")
            else:
                click.echo(f"  · [opencode] Not installed ({path})")
            continue
        if not os.path.exists(path):
            click.echo(f"  · [{agent}] Not found ({path})")
            continue
        doc = _load_json_file(path)
        hooks = doc.get("hooks", {})
        count = 0
        for entries in hooks.values():
            for entry in (entries if isinstance(entries, list) else []):
                # cursor: {"command": "otel-hook"}
                if "otel-hook" in entry.get("command", "") or "otel_hook" in entry.get("command", ""):
                    count += 1
                # copilot: {"bash": "otel-hook"}
                if _is_copilot_hook(entry):
                    count += 1
                # claude/gemini: {"hooks": [{"command": "otel-hook"}]}
                for h in entry.get("hooks", []):
                    if "otel-hook" in h.get("command", "") or "otel_hook" in h.get("command", ""):
                        count += 1
        status = f"{count} events registered" if count else "not registered"
        click.echo(f"  {'✓' if count else '·'} [{agent}] {status} ({path})")


@cli.command("uninstall")
@click.option(
    "--agent", "agents",
    type=click.Choice(["cursor", "windsurf", "claude", "copilot", "gemini", "codex", "opencode"]),
    multiple=True,
    required=True,
)
@click.option("--global/--no-global", "global_", default=True)
@click.option("--cwd", default=".")
def uninstall_cmd(agents: tuple, global_: bool, cwd: str) -> None:
    """Remove otel-hook entries from agent configs."""
    home = os.path.expanduser("~")
    for agent in agents:
        if agent == "opencode":
            path = (
                os.path.join(home, ".config", "opencode", "plugins", _OPENCODE_PLUGIN_FILENAME)
                if global_ else
                os.path.join(_find_repo_root(cwd), ".opencode", "plugins", _OPENCODE_PLUGIN_FILENAME)
            )
            if os.path.isfile(path):
                os.remove(path)
                click.echo(f"  ✓ [opencode] Removed plugin ({path})")
            else:
                click.echo(f"  · [opencode] Not installed ({path})")
            continue

        elif agent == "cursor":
            path = os.path.join(home, ".cursor", "hooks.json") if global_ else os.path.join(_find_repo_root(cwd), ".cursor", "hooks.json")
            doc = _load_json_file(path)
            hooks = doc.get("hooks", {})
            removed = 0
            for event in list(hooks.keys()):
                before = len(hooks[event])
                hooks[event] = [h for h in hooks[event] if "otel-hook" not in h.get("command", "") and "otel_hook" not in h.get("command", "")]
                removed += before - len(hooks[event])
                if not hooks[event]:
                    del hooks[event]
            if removed:
                _write_json_file(path, doc)
            click.echo(f"  {'✓' if removed else '·'} [cursor] Removed {removed} entries ({path})")

        elif agent == "claude":
            path = os.path.join(home, ".claude", "settings.json") if global_ else os.path.join(_find_repo_root(cwd), ".claude", "settings.json")
            settings = _load_json_file(path)
            hooks = settings.get("hooks", {})
            removed = 0
            for event in list(hooks.keys()):
                new_list = []
                for entry in hooks[event]:
                    surviving = [h for h in entry.get("hooks", []) if "otel-hook" not in h.get("command", "") and "otel_hook" not in h.get("command", "")]
                    if surviving:
                        entry["hooks"] = surviving
                        new_list.append(entry)
                    else:
                        removed += 1
                hooks[event] = new_list
                if not hooks[event]:
                    del hooks[event]
            if removed:
                _write_json_file(path, settings)
            click.echo(f"  {'✓' if removed else '·'} [claude] Removed {removed} entries ({path})")

        elif agent == "copilot":
            path = _agent_config_paths(global_, cwd)["copilot"]
            doc = _load_json_file(path)
            hooks = doc.get("hooks", {})
            removed = 0
            for event in list(hooks.keys()):
                before = len(hooks[event])
                hooks[event] = [h for h in hooks[event] if not _is_copilot_hook(h)]
                removed += before - len(hooks[event])
                if not hooks[event]:
                    del hooks[event]
            if removed:
                _write_json_file(path, doc)
            click.echo(f"  {'✓' if removed else '·'} [copilot] Removed {removed} entries ({path})")

        elif agent == "gemini":
            path = os.path.join(home, ".gemini", "settings.json") if global_ else os.path.join(_find_repo_root(cwd), ".gemini", "settings.json")
            settings = _load_json_file(path)
            hooks = settings.get("hooks", {})
            removed = 0
            for event in list(hooks.keys()):
                new_list = []
                for entry in hooks[event]:
                    surviving = [h for h in entry.get("hooks", []) if "otel-hook" not in h.get("command", "") and "otel_hook" not in h.get("command", "")]
                    if surviving:
                        entry["hooks"] = surviving
                        new_list.append(entry)
                    else:
                        removed += 1
                hooks[event] = new_list
                if not hooks[event]:
                    del hooks[event]
            if removed:
                _write_json_file(path, settings)
            click.echo(f"  {'✓' if removed else '·'} [gemini] Removed {removed} entries ({path})")

        elif agent == "codex":
            path = os.path.join(home, ".codex", "hooks.json") if global_ else os.path.join(_find_repo_root(cwd), ".codex", "hooks.json")
            doc = _load_json_file(path)
            hooks = doc.get("hooks", {})
            removed = 0
            for event in list(hooks.keys()):
                new_list = []
                for entry in hooks[event]:
                    before = len(entry.get("hooks", []))
                    surviving = [
                        h for h in entry.get("hooks", [])
                        if "otel-hook" not in h.get("command", "") and "otel_hook" not in h.get("command", "")
                    ]
                    removed += before - len(surviving)
                    if surviving:
                        entry["hooks"] = surviving
                        new_list.append(entry)
                hooks[event] = new_list
                if not hooks[event]:
                    del hooks[event]
            if removed:
                _write_json_file(path, doc)
            click.echo(f"  {'✓' if removed else '·'} [codex] Removed {removed} entries ({path})")


def run_cli(runtime=None) -> None:
    """Use UTF-8 for redirected Windows output before Click renders help/errors."""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    cli(obj=runtime)

