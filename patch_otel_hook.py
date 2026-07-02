import re

with open("otel_hook.py", "r") as f:
    content = f.read()

# 1. Update _REPO_MARKERS
content = content.replace(
    '_REPO_MARKERS = (".git", ".github", ".cursor", ".claude", ".gemini", ".codex", ".opencode")',
    '_REPO_MARKERS = (".git", ".github", ".cursor", ".claude", ".gemini", ".codex", ".opencode", ".windsurf")'
)

# 2. Update detect_agents
windsurf_detect = """    if os.path.isdir(os.path.join(home, ".codeium", "windsurf")) or shutil.which("windsurf"):
        found.append("windsurf")"""
if 'found.append("opencode")' in content and 'found.append("windsurf")' not in content:
    content = content.replace(
        '    if shutil.which("opencode") or os.path.isdir(os.path.join(home, ".config", "opencode")):\n        found.append("opencode")',
        '    if shutil.which("opencode") or os.path.isdir(os.path.join(home, ".config", "opencode")):\n        found.append("opencode")\n' + windsurf_detect
    )

# 3. Add setup_windsurf
setup_windsurf = """def setup_windsurf(global_: bool = True, cwd: str = ".") -> None:
    \"\"\"Register otel-hook in Windsurf's settings.json.\"\"\"
    hook_cmd = _resolve_hook_cmd()
    if global_:
        hooks_path = os.path.join(os.path.expanduser("~"), ".codeium", "windsurf", "settings.json")
    else:
        repo = _find_repo_root(cwd)
        hooks_path = os.path.join(repo, ".windsurf", "settings.json")

    doc = _load_json_file(hooks_path)
    # Windsurf uses settings.json, but protocol is same as cursor
    doc.setdefault("version", 1)
    hooks = doc.setdefault("hooks", {})
    added, updated, skipped = [], [], []

    for event in _CURSOR_EVENTS:
        event_hooks = hooks.setdefault(event, [])
        matches = [h for h in event_hooks if "otel-hook" in h.get("command", "") or "otel_hook" in h.get("command", "")]
        if matches:
            changed = False
            for hook in matches:
                env = hook.get("env")
                if isinstance(env, dict) and "IDE_OTEL_IDE_NAME" in env:
                    env = dict(env)
                    env.pop("IDE_OTEL_IDE_NAME", None)
                    if env:
                        hook["env"] = env
                    else:
                        hook.pop("env", None)
                    changed = True
            (updated if changed else skipped).append(event)
        else:
            event_hooks.append({"command": hook_cmd})
            added.append(event)

    _write_json_file(hooks_path, doc)
    _log_setup_result("windsurf", hooks_path, added, updated, skipped)

"""
if 'def setup_windsurf' not in content:
    content = content.replace(
        'def setup_claude(',
        setup_windsurf + '\ndef setup_claude('
    )

# 4. Update setup_agent dispatcher
if 'elif agent == "windsurf":' not in content:
    content = content.replace(
        '    if agent == "cursor":\n        setup_cursor(global_=global_, cwd=cwd)',
        '    if agent == "cursor":\n        setup_cursor(global_=global_, cwd=cwd)\n    elif agent == "windsurf":\n        setup_windsurf(global_=global_, cwd=cwd)'
    )

# 5. Update Choices
content = content.replace(
    '["cursor", "claude", "copilot", "gemini", "codex", "opencode"]',
    '["cursor", "windsurf", "claude", "copilot", "gemini", "codex", "opencode"]'
)

# 6. Update help messages
content = content.replace(
    '--agent cursor|claude',
    '--agent cursor|windsurf|claude'
)

# 7. Update _detect_hooks
if '"windsurf":' not in content:
    content = content.replace(
        '"cursor": os.path.join(home, ".cursor", "hooks.json") if global_ else os.path.join(_find_repo_root(cwd), ".cursor", "hooks.json"),',
        '"cursor": os.path.join(home, ".cursor", "hooks.json") if global_ else os.path.join(_find_repo_root(cwd), ".cursor", "hooks.json"),\n        "windsurf": os.path.join(home, ".codeium", "windsurf", "settings.json") if global_ else os.path.join(_find_repo_root(cwd), ".windsurf", "settings.json"),'
    )

# 8. Update remove cmd
if 'elif agent == "windsurf":' not in content:
    content = content.replace(
        '        elif agent == "cursor":\n            path = os.path.join(home, ".cursor", "hooks.json") if global_ else os.path.join(_find_repo_root(cwd), ".cursor", "hooks.json")',
        '        elif agent == "cursor":\n            path = os.path.join(home, ".cursor", "hooks.json") if global_ else os.path.join(_find_repo_root(cwd), ".cursor", "hooks.json")\n        elif agent == "windsurf":\n            path = os.path.join(home, ".codeium", "windsurf", "settings.json") if global_ else os.path.join(_find_repo_root(cwd), ".windsurf", "settings.json")'
    )

with open("otel_hook.py", "w") as f:
    f.write(content)
