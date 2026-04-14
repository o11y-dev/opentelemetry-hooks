#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# OpenTelemetry Hook — Quick Setup
#
# Registers the OTel hook with Cursor IDE, GitHub Copilot, Claude Code,
# and/or OpenCode.
# Safe to run multiple times — skips hooks that are already registered.
#
# Usage:
#   bash setup.sh                    # Auto-detect and set up all found IDEs
#   bash setup.sh --cursor           # Cursor project-level (.cursor/hooks.json)
#   bash setup.sh --cursor --global  # Cursor global (~/.cursor/hooks.json)
#   bash setup.sh --copilot          # GitHub Copilot (.github/hooks/otel-hooks.json)
#   bash setup.sh --claude           # Claude Code only
#   bash setup.sh --claude --global  # Claude Code global (~/.claude/settings.json)
#   bash setup.sh --opencode         # OpenCode project-level (.opencode/plugins/)
#   bash setup.sh --opencode --global # OpenCode global (~/.config/opencode/plugins/)
#   bash setup.sh --reinstall        # pipx install --force . then register hooks
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"

# Prefer the system-installed otel-hook command (pip/pipx deployment) when it is
# on PATH; fall back to the local script for source-checkout / copied-source use.
if command -v otel-hook &>/dev/null; then
  HOOK_CMD="$(command -v otel-hook)"
else
  HOOK_CMD="python3 $HOOK_DIR/otel_hook.py"
fi

# ─── Event names per IDE ─────────────────────────────────────────────────────
CURSOR_EVENTS=(
  sessionStart sessionEnd
  subagentStart subagentStop
  preToolUse postToolUse postToolUseFailure
  beforeShellExecution afterShellExecution
  beforeMCPExecution afterMCPExecution
  beforeReadFile afterFileEdit
  beforeSubmitPrompt stop
)

CLAUDE_EVENTS=(
  SessionStart SessionEnd
  SubagentStart SubagentStop
  PreToolUse PostToolUse PostToolUseFailure
  UserPromptSubmit Stop
)

GEMINI_EVENTS=(
  SessionStart SessionEnd
  BeforeAgent AfterAgent
  BeforeModel AfterModel
  BeforeTool AfterTool
)

COPILOT_EVENTS=(
  sessionStart sessionEnd
  userPromptSubmitted
  preToolUse postToolUse
  errorOccurred
)

REPO_MARKERS=(.git .github .cursor .claude .opencode .gemini)

# Events that require a matcher (Claude Code tool-related hooks)
CLAUDE_MATCHER_EVENTS="PreToolUse PostToolUse PostToolUseFailure"
GEMINI_MATCHER_EVENTS="BeforeAgent AfterAgent BeforeModel AfterModel BeforeTool AfterTool"

# ─── Parse arguments ─────────────────────────────────────────────────────────
DO_CURSOR=""
DO_COPILOT=""
DO_CLAUDE=""
DO_OPENCODE=""
DO_GEMINI=""
CURSOR_GLOBAL=""
CLAUDE_GLOBAL=""
OPENCODE_GLOBAL=""
GEMINI_GLOBAL=""
WANT_GLOBAL=""
DO_REINSTALL=""
DO_CLEAN=""
DO_UNINSTALL=""
DO_DIAGNOSE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cursor)    DO_CURSOR=1; shift ;;
    --copilot)  DO_COPILOT=1; shift ;;
    --claude)    DO_CLAUDE=1; shift ;;
    --opencode)  DO_OPENCODE=1; shift ;;
    --gemini)    DO_GEMINI=1; shift ;;
    --global)    WANT_GLOBAL=1; shift ;;
    --reinstall) DO_REINSTALL=1; shift ;;
    --clean)     DO_CLEAN=1; shift ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    --diagnose)  DO_DIAGNOSE=1; shift ;;
    *)           echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Apply --global only to the IDEs that were explicitly selected.
# Requiring an explicit IDE flag avoids accidentally installing global hooks
# for IDEs the user did not intend to configure.
if [[ -n "$WANT_GLOBAL" ]]; then
  if [[ -z "$DO_CURSOR" && -z "$DO_COPILOT" && -z "$DO_CLAUDE" && -z "$DO_OPENCODE" && -z "$DO_GEMINI" ]]; then
    echo "Error: --global requires an explicit IDE flag (--cursor, --copilot, --claude, --gemini, or --opencode)."
    exit 1
  fi
  if [[ -n "$DO_COPILOT" ]]; then
    echo "Error: GitHub Copilot hooks are repository-scoped; --copilot does not support --global."
    exit 1
  fi
  [[ -n "$DO_CURSOR" ]]   && CURSOR_GLOBAL=1
  [[ -n "$DO_CLAUDE" ]]   && CLAUDE_GLOBAL=1
  [[ -n "$DO_OPENCODE" ]] && OPENCODE_GLOBAL=1
  [[ -n "$DO_GEMINI" ]]   && GEMINI_GLOBAL=1
fi

# Auto-detect if no IDE flags given (applies both for setup and for operational commands)
if [[ -z "$DO_CURSOR" && -z "$DO_COPILOT" && -z "$DO_CLAUDE" && -z "$DO_OPENCODE" && -z "$DO_GEMINI" ]]; then
  # Check for a .cursor workspace directory in the current or parent directories,
  # or fallback to cursor being installed on PATH or in $HOME.
  CURSOR_DIR_FOUND=""
  SEARCH_DIR="$PWD"
  while :; do
    if [ -d "$SEARCH_DIR/.cursor" ]; then
      CURSOR_DIR_FOUND=1
      break
    fi
    # Stop if we've reached the filesystem root or cannot ascend further
    if [ "$SEARCH_DIR" = "/" ]; then
      break
    fi
    PARENT_DIR="$(dirname "$SEARCH_DIR")"
    if [ "$PARENT_DIR" = "$SEARCH_DIR" ]; then
      break
    fi
    SEARCH_DIR="$PARENT_DIR"
  done

  if command -v cursor &>/dev/null || [ -d "$HOME/.cursor" ] || [ -n "$CURSOR_DIR_FOUND" ]; then
    DO_CURSOR=1
  fi
  # Check if claude is installed
  if command -v claude &>/dev/null || [ -d "$HOME/.claude" ]; then
    DO_CLAUDE=1
    CLAUDE_GLOBAL=1
  fi
  # Check if opencode is installed
  OPENCODE_CONFIG_DIR="${OPENCODE_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/opencode}"
  if command -v opencode &>/dev/null || [ -d "$OPENCODE_CONFIG_DIR" ]; then
    DO_OPENCODE=1
    OPENCODE_GLOBAL=1
  fi
  # Check if gemini-cli is installed
  if command -v gemini &>/dev/null || [ -d "$HOME/.gemini" ]; then
    DO_GEMINI=1
    GEMINI_GLOBAL=1
  fi
  if [[ -z "$DO_CURSOR" && -z "$DO_COPILOT" && -z "$DO_CLAUDE" && -z "$DO_OPENCODE" && -z "$DO_GEMINI" ]]; then
    if [[ -n "$DO_CLEAN" || -n "$DO_UNINSTALL" || -n "$DO_DIAGNOSE" ]]; then
      echo "No supported IDE detected. Use --cursor, --copilot, --claude, --gemini, or --opencode to target a specific IDE."
    else
      echo "No supported IDE detected. Use --cursor, --copilot, --claude, --gemini, or --opencode to force setup."
    fi
    exit 1
  fi
fi

echo "🔭 OpenTelemetry Hook Setup"
echo "─────────────────────────────"

# ─── Step 1: Check for python3 ──────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "❌ python3 not found. Install Python 3.12+ and re-run."
  exit 1
fi
PYTHON3_VERSION="$(python3 --version 2>&1)"
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "❌ Unsupported python3 version: $PYTHON3_VERSION. Install Python 3.12+ and re-run."
  exit 1
fi
echo "✅ python3 found: $PYTHON3_VERSION"
echo "✅ hook command: $HOOK_CMD"
echo ""

# ─── Optional: reinstall package via pipx ───────────────────────────────────
if [[ -n "$DO_REINSTALL" ]]; then
  if ! command -v pipx &>/dev/null; then
    echo "❌ pipx not found. Install pipx and re-run with --reinstall."
    exit 1
  fi
  echo "📦 Reinstalling package: pipx install --force \"$HOOK_DIR\""
  pipx install --force "$HOOK_DIR"
  echo "✅ Package reinstalled"
  echo ""
  # Refresh HOOK_CMD in case otel-hook just became available on PATH
  if command -v otel-hook &>/dev/null; then
    HOOK_CMD="$(command -v otel-hook)"
  fi
fi

find_repo_root_from() {
  local current="$1"
  local parent
  local marker

  while [[ -n "$current" && "$current" != "/" ]]; do
    for marker in "${REPO_MARKERS[@]}"; do
      if [[ -d "$current/$marker" ]]; then
        printf '%s\n' "$current"
        return 0
      fi
    done
    parent="$(dirname "$current")"
    if [[ "$parent" == "$current" ]]; then
      break
    fi
    current="$parent"
  done

  return 1
}

find_repo_root() {
  local candidate
  local repo_root=""
  local has_git=""

  if command -v git >/dev/null 2>&1; then
    has_git=1
  fi

  for candidate in "$PWD" "$HOOK_DIR"; do
    if [[ -n "$has_git" ]]; then
      repo_root="$(git -C "$candidate" rev-parse --show-toplevel 2>/dev/null || true)"
      if [[ -n "$repo_root" ]]; then
        printf '%s\n' "$repo_root"
        return 0
      fi
    fi
  done

  for candidate in "$PWD" "$HOOK_DIR"; do
    repo_root="$(find_repo_root_from "$candidate" 2>/dev/null || true)"
    if [[ -n "$repo_root" ]]; then
      printf '%s\n' "$repo_root"
      return 0
    fi
  done

  printf '%s\n' "$PWD"
}

# ─── Cursor IDE cleanup ─────────────────────────────────────────────────────
diagnose_cursor() {
  local hooks_json
  if [[ -n "$CURSOR_GLOBAL" ]]; then
    hooks_json="$HOME/.cursor/hooks.json"
    echo "🔍 Cursor IDE (global: $hooks_json)"
  else
    local repo_root
    repo_root="$(find_repo_root)"
    hooks_json="$repo_root/.cursor/hooks.json"
    echo "🔍 Cursor IDE (project: $hooks_json)"
  fi

  if [ ! -f "$hooks_json" ]; then
    echo "  ⏭️  Hooks file not found"
    return 0
  fi

  python3 -c "
import json, sys, os, shlex

def cmd_has_valid_path(cmd):
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = [cmd]
    abs_paths = [p for p in parts if p.startswith('/')]
    if not abs_paths:
        return True
    return any(os.path.exists(p) or p.startswith('/usr/local') for p in abs_paths)

hooks_path = sys.argv[1]
with open(hooks_path, 'r') as f:
    try:
        doc = json.load(f)
    except json.JSONDecodeError:
        print('  ❌ Error: hooks.json is not valid JSON')
        sys.exit(1)

hooks = doc.get('hooks', {})
registered_count = 0
stale_count = 0

for event, entries in hooks.items():
    for h in entries:
        cmd = h.get('command', '')
        if 'otel_hook' in cmd or 'otel-hook' in cmd:
            registered_count += 1
            if not cmd_has_valid_path(cmd):
                stale_count += 1

if registered_count > 0:
    print(f'  ✅ {registered_count} OTel hook entries registered ({stale_count} stale)')
else:
    print('  ⏭️  No OTel hook entries found')
" "$hooks_json"
}

uninstall_cursor() {
  local hooks_json
  if [[ -n "$CURSOR_GLOBAL" ]]; then
    hooks_json="$HOME/.cursor/hooks.json"
    echo "🗑️  Cursor IDE (global: $hooks_json)"
  else
    local repo_root
    repo_root="$(find_repo_root)"
    hooks_json="$repo_root/.cursor/hooks.json"
    echo "🗑️  Cursor IDE (project: $hooks_json)"
  fi

  if [ ! -f "$hooks_json" ]; then
    echo "  ⏭️  Hooks file not found — nothing to uninstall"
    return 0
  fi

  python3 -c "
import json, sys, os

hooks_path = sys.argv[1]
with open(hooks_path, 'r') as f:
    try:
        doc = json.load(f)
    except json.JSONDecodeError:
        print('  ❌ Error: hooks.json is not valid JSON')
        sys.exit(1)

hooks = doc.get('hooks', {})
removed_count = 0

for event, entries in list(hooks.items()):
    surviving_hooks = []
    for h in entries:
        cmd = h.get('command', '')
        if 'otel_hook' in cmd or 'otel-hook' in cmd:
            removed_count += 1
        else:
            surviving_hooks.append(h)
    
    hooks[event] = surviving_hooks
    if not hooks[event]:
        del hooks[event]

if removed_count > 0:
    with open(hooks_path, 'w') as f:
        json.dump(doc, f, indent=2)
        f.write('\n')
    print(f'  ✅ Uninstalled {removed_count} hook entries')
else:
    print('  ⏭️  No OTel hook entries found to uninstall')
" "$hooks_json"
}

clean_cursor() {
  local hooks_json
  if [[ -n "$CURSOR_GLOBAL" ]]; then
    hooks_json="$HOME/.cursor/hooks.json"
    echo "🧹 Cursor IDE (global: $hooks_json)"
  else
    local repo_root
    repo_root="$(find_repo_root)"
    hooks_json="$repo_root/.cursor/hooks.json"
    echo "🧹 Cursor IDE (project: $hooks_json)"
  fi

  if [ ! -f "$hooks_json" ]; then
    echo "  ⏭️  Hooks file not found — nothing to clean"
    return 0
  fi

  python3 -c "
import json, sys, os, shlex

def cmd_has_valid_path(cmd):
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = [cmd]
    abs_paths = [p for p in parts if p.startswith('/')]
    if not abs_paths:
        return True
    return any(os.path.exists(p) or p.startswith('/usr/local') for p in abs_paths)

hooks_path = sys.argv[1]
with open(hooks_path, 'r') as f:
    try:
        doc = json.load(f)
    except json.JSONDecodeError:
        print('  ❌ Error: hooks.json is not valid JSON')
        sys.exit(1)

hooks = doc.get('hooks', {})
removed_count = 0
total_count = 0

for event, entries in list(hooks.items()):
    surviving_hooks = []
    for h in entries:
        cmd = h.get('command', '')
        total_count += 1
        if cmd_has_valid_path(cmd):
            surviving_hooks.append(h)
        else:
            removed_count += 1

    hooks[event] = surviving_hooks
    if not hooks[event]:
        del hooks[event]

if removed_count > 0:
    with open(hooks_path, 'w') as f:
        json.dump(doc, f, indent=2)
        f.write('\n')
    print(f'  ✅ Removed {removed_count} stale hook entries (out of {total_count} total)')
else:
    print(f'  ✅ No stale hook entries found (out of {total_count} total)')
" "$hooks_json"
}

# ─── Cursor IDE setup ───────────────────────────────────────────────────────
setup_cursor() {
  local hooks_json

  if [[ -n "$CURSOR_GLOBAL" ]]; then
    hooks_json="$HOME/.cursor/hooks.json"
    echo "📦 Cursor IDE (global: $hooks_json)"
  else
    local repo_root
    repo_root="$(find_repo_root)"
    hooks_json="$repo_root/.cursor/hooks.json"
    echo "📦 Cursor IDE (project: $hooks_json)"
  fi

  if [ ! -f "$hooks_json" ]; then
    echo "  📝 Creating new .cursor/hooks.json ..."
    mkdir -p "$(dirname "$hooks_json")"

    python3 -c "
import json, sys
hook_cmd = sys.argv[1]
events = sys.argv[2:]
hooks = {}
for event in events:
    hooks[event] = [{'command': hook_cmd}]
doc = {'version': 1, 'hooks': hooks}
print(json.dumps(doc, indent=2))
" "$HOOK_CMD" "${CURSOR_EVENTS[@]}" > "$hooks_json"

    echo "  ✅ Created $hooks_json with all OTel hook events"
  else
    echo "  📝 Merging OTel hooks into existing .cursor/hooks.json ..."

    python3 -c "
import json, sys

hooks_path = sys.argv[1]
hook_cmd = sys.argv[2]
events = sys.argv[3:]

with open(hooks_path, 'r') as f:
    doc = json.load(f)

hooks = doc.setdefault('hooks', {})
added = []
updated = []
skipped = []

for event in events:
    event_hooks = hooks.setdefault(event, [])
    matches = [h for h in event_hooks if h.get('command') == hook_cmd]
    if matches:
        changed = False
        for hook in matches:
            env = hook.get('env')
            if isinstance(env, dict) and 'IDE_OTEL_IDE_NAME' in env:
                env = dict(env)
                env.pop('IDE_OTEL_IDE_NAME', None)
                if env:
                    hook['env'] = env
                else:
                    hook.pop('env', None)
                changed = True
        if changed:
            updated.append(event)
        else:
            skipped.append(event)
    else:
        event_hooks.append({'command': hook_cmd})
        added.append(event)
 
with open(hooks_path, 'w') as f:
    json.dump(doc, f, indent=2)
    f.write('\n')

if added:
    print(f'  ✅ Added OTel hook to {len(added)} events: {\", \".join(added)}')
if updated:
    print(f'  ✅ Removed IDE_OTEL_IDE_NAME from {len(updated)} existing events: {\", \".join(updated)}')
if skipped:
    print(f'  ⏭️  Already registered in {len(skipped)} events (no changes)')
if not added and not updated:
    print('  ✅ All hook events already registered — nothing to do')
" "$hooks_json" "$HOOK_CMD" "${CURSOR_EVENTS[@]}"
  fi
}

# ─── Gemini CLI cleanup ─────────────────────────────────────────────────────
diagnose_gemini() {
  local settings_json
  if [[ -n "$GEMINI_GLOBAL" ]]; then
    settings_json="$HOME/.gemini/settings.json"
    echo "🔍 Gemini CLI (global: $settings_json)"
  else
    local repo_root
    repo_root="$(find_repo_root)"
    settings_json="$repo_root/.gemini/settings.json"
    echo "🔍 Gemini CLI (project: $settings_json)"
  fi

  if [ ! -f "$settings_json" ]; then
    echo "  ⏭️  Settings file not found"
    return 0
  fi

  python3 -c "
import json, sys, os, shlex

def cmd_has_valid_path(cmd):
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = [cmd]
    abs_paths = [p for p in parts if p.startswith('/')]
    if not abs_paths:
        return True
    return any(os.path.exists(p) or p.startswith('/usr/local') for p in abs_paths)

settings_path = sys.argv[1]
with open(settings_path, 'r') as f:
    try:
        settings = json.load(f)
    except json.JSONDecodeError:
        print('  ❌ Error: Settings file is not valid JSON')
        sys.exit(1)

hooks = settings.get('hooks', {})
registered_count = 0
stale_count = 0

for event, entries in hooks.items():
    for entry in entries:
        for h in entry.get('hooks', []):
            cmd = h.get('command', '')
            if 'otel_hook' in cmd or 'otel-hook' in cmd:
                registered_count += 1
                if not cmd_has_valid_path(cmd):
                    stale_count += 1

if registered_count > 0:
    print(f'  ✅ {registered_count} OTel hook entries registered ({stale_count} stale)')
else:
    print('  ⏭️  No OTel hook entries found')
" "$settings_json"
}

uninstall_gemini() {
  local settings_json
  if [[ -n "$GEMINI_GLOBAL" ]]; then
    settings_json="$HOME/.gemini/settings.json"
    echo "🗑️  Gemini CLI (global: $settings_json)"
  else
    local repo_root
    repo_root="$(find_repo_root)"
    settings_json="$repo_root/.gemini/settings.json"
    echo "🗑️  Gemini CLI (project: $settings_json)"
  fi

  if [ ! -f "$settings_json" ]; then
    echo "  ⏭️  Settings file not found — nothing to uninstall"
    return 0
  fi

  python3 -c "
import json, sys, os

settings_path = sys.argv[1]
with open(settings_path, 'r') as f:
    try:
        settings = json.load(f)
    except json.JSONDecodeError:
        print('  ❌ Error: Settings file is not valid JSON')
        sys.exit(1)

hooks = settings.get('hooks', {})
removed_count = 0

for event, entries in list(hooks.items()):
    live = []
    for entry in entries:
        surviving_hooks = []
        for h in entry.get('hooks', []):
            cmd = h.get('command', '')
            if 'otel_hook' in cmd or 'otel-hook' in cmd:
                removed_count += 1
            else:
                surviving_hooks.append(h)
        
        if surviving_hooks:
            entry['hooks'] = surviving_hooks
            live.append(entry)
    
    hooks[event] = live
    if not hooks[event]:
        del hooks[event]

if removed_count > 0:
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2)
        f.write('\n')
    print(f'  ✅ Uninstalled {removed_count} hook entries')
else:
    print('  ⏭️  No OTel hook entries found to uninstall')
" "$settings_json"
}

clean_gemini() {
  local settings_json
  if [[ -n "$GEMINI_GLOBAL" ]]; then
    settings_json="$HOME/.gemini/settings.json"
    echo "🧹 Gemini CLI (global: $settings_json)"
  else
    local repo_root
    repo_root="$(find_repo_root)"
    settings_json="$repo_root/.gemini/settings.json"
    echo "🧹 Gemini CLI (project: $settings_json)"
  fi

  if [ ! -f "$settings_json" ]; then
    echo "  ⏭️  Settings file not found — nothing to clean"
    return 0
  fi

  python3 -c "
import json, sys, os, shlex

def cmd_has_valid_path(cmd):
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = [cmd]
    abs_paths = [p for p in parts if p.startswith('/')]
    if not abs_paths:
        return True
    return any(os.path.exists(p) or p.startswith('/usr/local') for p in abs_paths)

settings_path = sys.argv[1]
with open(settings_path, 'r') as f:
    try:
        settings = json.load(f)
    except json.JSONDecodeError:
        print('  ❌ Error: Settings file is not valid JSON')
        sys.exit(1)

hooks = settings.get('hooks', {})
removed_count = 0
total_count = 0

for event, entries in list(hooks.items()):
    live = []
    for entry in entries:
        surviving_hooks = []
        for h in entry.get('hooks', []):
            cmd = h.get('command', '')
            total_count += 1
            if cmd_has_valid_path(cmd):
                surviving_hooks.append(h)
            else:
                removed_count += 1

        if surviving_hooks:
            entry['hooks'] = surviving_hooks
            live.append(entry)

    hooks[event] = live
    if not hooks[event]:
        del hooks[event]

if removed_count > 0:
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2)
        f.write('\n')
    print(f'  ✅ Removed {removed_count} stale hook entries (out of {total_count} total)')
else:
    print(f'  ✅ No stale hook entries found (out of {total_count} total)')
" "$settings_json"
}

# ─── Gemini CLI setup ───────────────────────────────────────────────────────
setup_gemini() {
  local settings_json

  if [[ -n "$GEMINI_GLOBAL" ]]; then
    settings_json="$HOME/.gemini/settings.json"
    echo "📦 Gemini CLI (global: $settings_json)"
  else
    local repo_root
    repo_root="$(find_repo_root)"
    settings_json="$repo_root/.gemini/settings.json"
    echo "📦 Gemini CLI (project: $settings_json)"
  fi

  mkdir -p "$(dirname "$settings_json")"

  python3 -c "
import json, sys, os

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]
events = sys.argv[3:]
matcher_events = set('$GEMINI_MATCHER_EVENTS'.split())

# Load existing settings or start fresh
if os.path.exists(settings_path):
    with open(settings_path, 'r') as f:
        try:
            settings = json.load(f)
        except json.JSONDecodeError:
            settings = {}
else:
    settings = {}

hooks = settings.setdefault('hooks', {})
added = []
updated = []
skipped = []

for event in events:
    event_list = hooks.setdefault(event, [])

    # Check for existing otel-hook entries with any path
    others = []
    exact = []
    for entry in event_list:
        for h in entry.get('hooks', []):
            cmd = h.get('command', '')
            if 'otel_hook' in cmd or 'otel-hook' in cmd:
                if cmd == hook_cmd:
                    exact.append(h)
                else:
                    others.append(h)

    if exact:
        skipped.append(event)
        continue

    if others:
        for hook in others:
            hook['command'] = hook_cmd
        updated.append(event)
        continue

    # Build the hook entry
    hook_entry = {
        'hooks': [
            {'type': 'command', 'command': hook_cmd, 'name': 'otel-hook'}
        ]
    }

    # Add matcher for events that require it
    if event in matcher_events:
        hook_entry['matcher'] = '*'

    event_list.append(hook_entry)
    added.append(event)

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')

if added:
    print(f'  ✅ Added OTel hook to {len(added)} events: {\", \".join(added)}')
if updated:
    print(f'  ✅ Updated OTel hook path in {len(updated)} events: {\", \".join(updated)}')
if skipped:
    print(f'  ⏭️  Already registered in {len(skipped)} events (no changes)')
" "$settings_json" "$HOOK_CMD" "${GEMINI_EVENTS[@]}"
}

# ─── Claude Code cleanup ────────────────────────────────────────────────────
diagnose_claude() {
  local settings_json
  if [[ -n "$CLAUDE_GLOBAL" ]]; then
    settings_json="$HOME/.claude/settings.json"
    echo "🔍 Claude Code (global: $settings_json)"
  else
    local repo_root
    repo_root="$(find_repo_root)"
    settings_json="$repo_root/.claude/settings.json"
    echo "🔍 Claude Code (project: $settings_json)"
  fi

  if [ ! -f "$settings_json" ]; then
    echo "  ⏭️  Settings file not found"
    return 0
  fi

  python3 -c "
import json, sys, os, shlex

def cmd_has_valid_path(cmd):
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = [cmd]
    abs_paths = [p for p in parts if p.startswith('/')]
    if not abs_paths:
        return True
    return any(os.path.exists(p) or p.startswith('/usr/local') for p in abs_paths)

settings_path = sys.argv[1]
with open(settings_path, 'r') as f:
    try:
        settings = json.load(f)
    except json.JSONDecodeError:
        print('  ❌ Error: Settings file is not valid JSON')
        sys.exit(1)

hooks = settings.get('hooks', {})
registered_count = 0
stale_count = 0

for event, entries in hooks.items():
    for entry in entries:
        for h in entry.get('hooks', []):
            cmd = h.get('command', '')
            if 'otel_hook' in cmd or 'otel-hook' in cmd:
                registered_count += 1
                if not cmd_has_valid_path(cmd):
                    stale_count += 1

if registered_count > 0:
    print(f'  ✅ {registered_count} OTel hook entries registered ({stale_count} stale)')
else:
    print('  ⏭️  No OTel hook entries found')
" "$settings_json"
}

uninstall_claude() {
  local settings_json
  if [[ -n "$CLAUDE_GLOBAL" ]]; then
    settings_json="$HOME/.claude/settings.json"
    echo "🗑️  Claude Code (global: $settings_json)"
  else
    local repo_root
    repo_root="$(find_repo_root)"
    settings_json="$repo_root/.claude/settings.json"
    echo "🗑️  Claude Code (project: $settings_json)"
  fi

  if [ ! -f "$settings_json" ]; then
    echo "  ⏭️  Settings file not found — nothing to uninstall"
    return 0
  fi

  python3 -c "
import json, sys, os

settings_path = sys.argv[1]
with open(settings_path, 'r') as f:
    try:
        settings = json.load(f)
    except json.JSONDecodeError:
        print('  ❌ Error: Settings file is not valid JSON')
        sys.exit(1)

hooks = settings.get('hooks', {})
removed_count = 0

for event, entries in list(hooks.items()):
    live = []
    for entry in entries:
        surviving_hooks = []
        for h in entry.get('hooks', []):
            cmd = h.get('command', '')
            if 'otel_hook' in cmd or 'otel-hook' in cmd:
                removed_count += 1
            else:
                surviving_hooks.append(h)
        
        if surviving_hooks:
            entry['hooks'] = surviving_hooks
            live.append(entry)
    
    hooks[event] = live
    if not hooks[event]:
        del hooks[event]

if removed_count > 0:
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2)
        f.write('\n')
    print(f'  ✅ Uninstalled {removed_count} hook entries')
else:
    print('  ⏭️  No OTel hook entries found to uninstall')
" "$settings_json"
}

clean_claude() {
  local settings_json
  if [[ -n "$CLAUDE_GLOBAL" ]]; then
    settings_json="$HOME/.claude/settings.json"
    echo "🧹 Claude Code (global: $settings_json)"
  else
    local repo_root
    repo_root="$(find_repo_root)"
    settings_json="$repo_root/.claude/settings.json"
    echo "🧹 Claude Code (project: $settings_json)"
  fi

  if [ ! -f "$settings_json" ]; then
    echo "  ⏭️  Settings file not found — nothing to clean"
    return 0
  fi

  python3 -c "
import json, sys, os, shlex

def cmd_has_valid_path(cmd):
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = [cmd]
    abs_paths = [p for p in parts if p.startswith('/')]
    if not abs_paths:
        return True
    return any(os.path.exists(p) or p.startswith('/usr/local') for p in abs_paths)

settings_path = sys.argv[1]
with open(settings_path, 'r') as f:
    try:
        settings = json.load(f)
    except json.JSONDecodeError:
        print('  ❌ Error: Settings file is not valid JSON')
        sys.exit(1)

hooks = settings.get('hooks', {})
removed_count = 0
total_count = 0

for event, entries in list(hooks.items()):
    live = []
    for entry in entries:
        surviving_hooks = []
        for h in entry.get('hooks', []):
            cmd = h.get('command', '')
            total_count += 1
            # Keep if command path(s) exist, or if it is the system-installed otel-hook (which may be a symlink)
            if cmd_has_valid_path(cmd):
                surviving_hooks.append(h)
            else:
                removed_count += 1

        if surviving_hooks:
            entry['hooks'] = surviving_hooks
            live.append(entry)

    hooks[event] = live
    if not hooks[event]:
        del hooks[event]

if removed_count > 0:
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2)
        f.write('\n')
    print(f'  ✅ Removed {removed_count} stale hook entries (out of {total_count} total)')
else:
    print(f'  ✅ No stale hook entries found (out of {total_count} total)')
" "$settings_json"
}

# ─── Claude Code setup ──────────────────────────────────────────────────────
setup_claude() {
  local settings_json

  if [[ -n "$CLAUDE_GLOBAL" ]]; then
    settings_json="$HOME/.claude/settings.json"
    echo "📦 Claude Code (global: $settings_json)"
  else
    # Project-level: .claude/settings.json in the repo root
    local repo_root
    repo_root="$(find_repo_root)"
    settings_json="$repo_root/.claude/settings.json"
    echo "📦 Claude Code (project: $settings_json)"
  fi

  mkdir -p "$(dirname "$settings_json")"

  python3 -c "
import json, sys, os

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]
events = sys.argv[3:]
matcher_events = set('$CLAUDE_MATCHER_EVENTS'.split())

# Load existing settings or start fresh
if os.path.exists(settings_path):
    with open(settings_path, 'r') as f:
        try:
            settings = json.load(f)
        except json.JSONDecodeError:
            settings = {}
else:
    settings = {}

hooks = settings.setdefault('hooks', {})
added = []
updated = []
skipped = []

for event in events:
    event_list = hooks.setdefault(event, [])

    # Check for existing otel-hook entries with any path
    others = []
    exact = []
    for entry in event_list:
        for h in entry.get('hooks', []):
            cmd = h.get('command', '')
            if 'otel_hook' in cmd or 'otel-hook' in cmd:
                if cmd == hook_cmd:
                    exact.append(h)
                else:
                    others.append(h)

    if exact:
        changed = False
        for hook in exact:
            env = hook.get('env')
            if isinstance(env, dict) and 'IDE_OTEL_IDE_NAME' in env:
                env = dict(env)
                env.pop('IDE_OTEL_IDE_NAME', None)
                if env:
                    hook['env'] = env
                else:
                    hook.pop('env', None)
                changed = True
        if changed:
            updated.append(event)
        else:
            skipped.append(event)
        continue

    if others:
        # Update existing otel-hook to new path (Fix #6)
        for hook in others:
            hook['command'] = hook_cmd
            # Also clean up legacy env if present
            env = hook.get('env')
            if isinstance(env, dict) and 'IDE_OTEL_IDE_NAME' in env:
                env = dict(env)
                env.pop('IDE_OTEL_IDE_NAME', None)
                if env:
                    hook['env'] = env
                else:
                    hook.pop('env', None)
        updated.append(event)
        continue

    # Build the hook entry in Claude Code format
    hook_entry = {
        'hooks': [
            {'type': 'command', 'command': hook_cmd}
        ]
    }

    # Add matcher for tool-related events
    if event in matcher_events:
        hook_entry['matcher'] = '*'

    event_list.append(hook_entry)
    added.append(event)

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')

if added:
    print(f'  ✅ Added OTel hook to {len(added)} events: {\", \".join(added)}')
if updated:
    print(f'  ✅ Removed IDE_OTEL_IDE_NAME from {len(updated)} existing events: {\", \".join(updated)}')
if skipped:
    print(f'  ⏭️  Already registered in {len(skipped)} events (no changes)')
if not added and not updated:
    print('  ✅ All hook events already registered — nothing to do')
" "$settings_json" "$HOOK_CMD" "${CLAUDE_EVENTS[@]}"
}

# ─── GitHub Copilot cleanup ───────────────────────────────────────────────────
diagnose_copilot() {
  local hooks_json
  local repo_root
  repo_root="$(find_repo_root)"
  hooks_json="$repo_root/.github/hooks/otel-hooks.json"

  echo "🔍 GitHub Copilot (repo: $hooks_json)"
  if [ ! -f "$hooks_json" ]; then
    echo "  ⏭️  Hooks file not found"
    return 0
  fi

  python3 -c "
import json, sys, os

hooks_path = sys.argv[1]
with open(hooks_path, 'r') as f:
    try:
        doc = json.load(f)
    except json.JSONDecodeError:
        print('  ❌ Error: otel-hooks.json is not valid JSON')
        sys.exit(1)

hooks = doc.get('hooks', {})
registered_count = 0
stale_count = 0

for event, entries in hooks.items():
    for h in entries:
        cmd = h.get('bash', '')
        if 'otel_hook' in cmd or 'otel-hook' in cmd:
            registered_count += 1
            # Check for command path in the bash string
            path_part = cmd.split(' ')[-1] if ' ' in cmd else cmd
            if not (os.path.exists(path_part) or path_part.startswith('/usr/local')):
                stale_count += 1

if registered_count > 0:
    print(f'  ✅ {registered_count} OTel hook entries registered ({stale_count} stale)')
else:
    print('  ⏭️  No OTel hook entries found')
" "$hooks_json"
}

uninstall_copilot() {
  local hooks_json
  local repo_root
  repo_root="$(find_repo_root)"
  hooks_json="$repo_root/.github/hooks/otel-hooks.json"

  echo "🗑️  GitHub Copilot (repo: $hooks_json)"
  if [ ! -f "$hooks_json" ]; then
    echo "  ⏭️  Hooks file not found — nothing to uninstall"
    return 0
  fi

  python3 -c "
import json, sys, os

hooks_path = sys.argv[1]
with open(hooks_path, 'r') as f:
    try:
        doc = json.load(f)
    except json.JSONDecodeError:
        print('  ❌ Error: otel-hooks.json is not valid JSON')
        sys.exit(1)

hooks = doc.get('hooks', {})
removed_count = 0

for event, entries in list(hooks.items()):
    surviving_hooks = []
    for h in entries:
        cmd = h.get('bash', '')
        if 'otel_hook' in cmd or 'otel-hook' in cmd:
            removed_count += 1
        else:
            surviving_hooks.append(h)
    
    hooks[event] = surviving_hooks
    if not hooks[event]:
        del hooks[event]

if removed_count > 0:
    with open(hooks_path, 'w') as f:
        json.dump(doc, f, indent=2)
        f.write('\n')
    print(f'  ✅ Uninstalled {removed_count} hook entries')
else:
    print('  ⏭️  No OTel hook entries found to uninstall')
" "$hooks_json"
}

clean_copilot() {
  local hooks_json
  local repo_root
  repo_root="$(find_repo_root)"
  hooks_json="$repo_root/.github/hooks/otel-hooks.json"

  echo "🧹 GitHub Copilot (repo: $hooks_json)"
  if [ ! -f "$hooks_json" ]; then
    echo "  ⏭️  Hooks file not found — nothing to clean"
    return 0
  fi

  python3 -c "
import json, sys, os

hooks_path = sys.argv[1]
with open(hooks_path, 'r') as f:
    try:
        doc = json.load(f)
    except json.JSONDecodeError:
        print('  ❌ Error: otel-hooks.json is not valid JSON')
        sys.exit(1)

hooks = doc.get('hooks', {})
removed_count = 0
total_count = 0

for event, entries in list(hooks.items()):
    surviving_hooks = []
    for h in entries:
        cmd = h.get('bash', '')
        total_count += 1
        path_part = cmd.split(' ')[-1] if ' ' in cmd else cmd
        if os.path.exists(path_part) or path_part.startswith('/usr/local'):
            surviving_hooks.append(h)
        else:
            removed_count += 1
    
    hooks[event] = surviving_hooks
    if not hooks[event]:
        del hooks[event]

if removed_count > 0:
    with open(hooks_path, 'w') as f:
        json.dump(doc, f, indent=2)
        f.write('\n')
    print(f'  ✅ Removed {removed_count} stale hook entries (out of {total_count} total)')
else:
    print(f'  ✅ No stale hook entries found (out of {total_count} total)')
" "$hooks_json"
}

# ─── GitHub Copilot setup ─────────────────────────────────────────────────────
setup_copilot() {
  local hooks_json
  local repo_root
  repo_root="$(find_repo_root)"
  hooks_json="$repo_root/.github/hooks/otel-hooks.json"

  echo "📦 GitHub Copilot (repo: $hooks_json)"
  mkdir -p "$(dirname "$hooks_json")"

  if [ ! -f "$hooks_json" ]; then
    echo "  📝 Creating new .github/hooks/otel-hooks.json ..."

    python3 -c "
import json, sys
hook_cmd = sys.argv[1]
events = sys.argv[2:]
hooks = {}
for event in events:
    hooks[event] = [{'type': 'command', 'bash': hook_cmd, 'timeoutSec': 30}]
doc = {'version': 1, 'hooks': hooks}
print(json.dumps(doc, indent=2))
" "$HOOK_CMD" "${COPILOT_EVENTS[@]}" > "$hooks_json"

    echo "  ✅ Created $hooks_json with all OTel hook events"
  else
    echo "  📝 Merging OTel hooks into existing .github/hooks/otel-hooks.json ..."

    python3 -c "
import json, sys

hooks_path = sys.argv[1]
hook_cmd = sys.argv[2]
events = sys.argv[3:]
wrapped_bash_cmds = [
    f'env IDE_OTEL_IDE_NAME=copilot {hook_cmd}',
    f'env IDE_OTEL_IDE_NAME=GitHub Copilot {hook_cmd}',
]

with open(hooks_path, 'r') as f:
    doc = json.load(f)

doc.setdefault('version', 1)
hooks = doc.setdefault('hooks', {})
added = []
updated = []
skipped = []

for event in events:
    event_hooks = hooks.setdefault(event, [])
    plain_matches = [h for h in event_hooks if h.get('type') == 'command' and h.get('bash') == hook_cmd]
    if plain_matches:
        changed = False
        for hook in plain_matches:
            if 'timeoutSec' not in hook:
                hook['timeoutSec'] = 30
                changed = True
        if changed:
            updated.append(event)
        else:
            skipped.append(event)
        continue

    legacy_matches = [h for h in event_hooks if h.get('type') == 'command' and h.get('bash') in wrapped_bash_cmds]
    if legacy_matches:
        for hook in legacy_matches:
            hook['type'] = 'command'
            hook['bash'] = hook_cmd
            hook.setdefault('timeoutSec', 30)
        updated.append(event)
        continue

    event_hooks.append({'type': 'command', 'bash': hook_cmd, 'timeoutSec': 30})
    added.append(event)

with open(hooks_path, 'w') as f:
    json.dump(doc, f, indent=2)
    f.write('\n')

if added:
    print(f'  ✅ Added OTel hook to {len(added)} events: {\", \".join(added)}')
if updated:
    print(f'  ✅ Updated {len(updated)} existing Copilot hook commands: {\", \".join(updated)}')
if skipped:
    print(f'  ⏭️  Already registered in {len(skipped)} events (no changes)')
if not added and not updated:
    print('  ✅ All hook events already registered — nothing to do')
" "$hooks_json" "$HOOK_CMD" "${COPILOT_EVENTS[@]}"
  fi
}

# ─── OpenCode setup ──────────────────────────────────────────────────────────
setup_opencode() {
  local plugin_dir
  local plugin_src="$HOOK_DIR/plugin/opencode.ts"

  if [ ! -f "$plugin_src" ]; then
    echo "  ❌ Plugin source not found: $plugin_src"
    echo "     Run setup.sh from the opentelemetry-hooks repo directory."
    return 1
  fi

  if [[ -n "$OPENCODE_GLOBAL" ]]; then
    # Respect OPENCODE_CONFIG_DIR if set (mirrors rtk's behavior)
    local config_dir="${OPENCODE_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/opencode}"
    plugin_dir="$config_dir/plugins"
    echo "📦 OpenCode (global: $plugin_dir/otel-hook.ts)"
  else
    # Derive project root from HOOK_DIR (matches Cursor/Claude behavior)
    local project_root="$HOOK_DIR"
    plugin_dir="$project_root/.opencode/plugins"
    echo "📦 OpenCode (project: $plugin_dir/otel-hook.ts)"
  fi

  mkdir -p "$plugin_dir"
  local dest="$plugin_dir/otel-hook.ts"

  if [ -f "$dest" ] && diff -q "$plugin_src" "$dest" &>/dev/null; then
    echo "  ✅ OpenCode plugin already up to date — nothing to do"
  else
    cp "$plugin_src" "$dest"
    echo "  ✅ Installed OpenCode plugin → $dest"
  fi
}

# ─── Run setup for selected IDEs ────────────────────────────────────────────
if [[ -n "$DO_DIAGNOSE" ]]; then
  echo "🔍 Diagnosing OTel hook registrations ..."
  [[ -n "$DO_CURSOR" ]] && diagnose_cursor
  [[ -n "$DO_CLAUDE" ]] && diagnose_claude
  [[ -n "$DO_COPILOT" ]] && diagnose_copilot
  [[ -n "$DO_GEMINI" ]] && diagnose_gemini
  exit 0
fi

if [[ -n "$DO_UNINSTALL" ]]; then
  echo "🗑️  Uninstalling OTel hooks ..."
  [[ -n "$DO_CURSOR" ]] && uninstall_cursor
  [[ -n "$DO_CLAUDE" ]] && uninstall_claude
  [[ -n "$DO_COPILOT" ]] && uninstall_copilot
  [[ -n "$DO_GEMINI" ]] && uninstall_gemini
  echo "✅ Uninstall complete!"
  exit 0
fi

if [[ -n "$DO_CLEAN" ]]; then
  echo "🧹 Cleaning stale OTel hook registrations ..."
  [[ -n "$DO_CURSOR" ]] && clean_cursor
  [[ -n "$DO_CLAUDE" ]] && clean_claude
  [[ -n "$DO_COPILOT" ]] && clean_copilot
  [[ -n "$DO_GEMINI" ]] && clean_gemini
  echo "✅ Cleaning complete!"
  exit 0
fi

if [[ -n "$DO_CURSOR" ]]; then
  setup_cursor
  echo ""
fi

if [[ -n "$DO_CLAUDE" ]]; then
  setup_claude
  echo ""
fi

if [[ -n "$DO_COPILOT" ]]; then
  setup_copilot
  echo ""
fi

if [[ -n "$DO_OPENCODE" ]]; then
  setup_opencode
  echo ""
fi

if [[ -n "$DO_GEMINI" ]]; then
  setup_gemini
  echo ""
fi

# ─── Kick off venv provisioning ─────────────────────────────────────────────
echo "🚀 Bootstrapping Python venv (runs in background) ..."
echo '{}' | python3 "$HOOK_DIR/otel_hook.py" > /dev/null 2>&1 || true

echo ""
echo "─────────────────────────────"
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Configure your OTLP endpoint in otel_config.json"
if [[ -n "$DO_CURSOR" ]]; then
  echo "  2. Restart Cursor IDE to activate hooks"
  if [[ -n "$CURSOR_GLOBAL" ]]; then
    echo "     (global hooks.json — applies to all projects)"
  fi
fi
if [[ -n "$DO_CLAUDE" ]]; then
  echo "  2. Restart Claude Code to activate hooks"
fi
if [[ -n "$DO_COPILOT" ]]; then
  echo "  2. Commit .github/hooks/otel-hooks.json to your default branch for Copilot to load it"
fi
if [[ -n "$DO_OPENCODE" ]]; then
  echo "  2. Restart OpenCode to activate the plugin"
fi
# Determine the hook home used for logging: prefer IDE_OTEL_HOOK_HOME, then
# fall back to the system default for otel-hook, or the local script dir.
LOG_HOME="${IDE_OTEL_HOOK_HOME:-}"
if [[ -z "$LOG_HOME" ]]; then
  if [[ "$HOOK_CMD" == *"/otel-hook" ]]; then
    LOG_HOME="$HOME/.local/share/opentelemetry-hooks"
  else
    LOG_HOME="$HOOK_DIR"
  fi
fi
echo "  3. Check logs:  tail -f \"$LOG_HOME/otel_hook.log\""
echo ""
