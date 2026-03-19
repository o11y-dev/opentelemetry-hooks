#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# OpenTelemetry Hook — Quick Setup
#
# Registers the OTel hook with Cursor IDE and/or Claude Code.
# Safe to run multiple times — skips hooks that are already registered.
#
# Usage:
#   bash setup.sh                   # Auto-detect and set up all found IDEs
#   bash setup.sh --cursor          # Cursor only
#   bash setup.sh --claude          # Claude Code only
#   bash setup.sh --claude --global # Claude Code global (~/.claude/settings.json)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"

# Prefer the system-installed otel-hook command (pip/pipx deployment) when it is
# on PATH; fall back to the local script for source-checkout / copied-source use.
if command -v otel-hook &>/dev/null; then
  HOOK_CMD="otel-hook"
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

# Events that require a matcher (Claude Code tool-related hooks)
CLAUDE_MATCHER_EVENTS="PreToolUse PostToolUse PostToolUseFailure"

# ─── Parse arguments ─────────────────────────────────────────────────────────
DO_CURSOR=""
DO_CLAUDE=""
CLAUDE_GLOBAL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cursor)  DO_CURSOR=1; shift ;;
    --claude)  DO_CLAUDE=1; shift ;;
    --global)  CLAUDE_GLOBAL=1; shift ;;
    *)         echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Auto-detect if no flags given
if [[ -z "$DO_CURSOR" && -z "$DO_CLAUDE" ]]; then
  # Check if .cursor exists in any parent or if cursor is installed
  if command -v cursor &>/dev/null || [ -d "$HOME/.cursor" ]; then
    DO_CURSOR=1
  fi
  # Check if claude is installed
  if command -v claude &>/dev/null || [ -d "$HOME/.claude" ]; then
    DO_CLAUDE=1
    CLAUDE_GLOBAL=1
  fi
  if [[ -z "$DO_CURSOR" && -z "$DO_CLAUDE" ]]; then
    echo "No supported IDE detected. Use --cursor or --claude to force setup."
    exit 1
  fi
fi

echo "🔭 OpenTelemetry Hook Setup"
echo "─────────────────────────────"

# ─── Step 1: Check for python3 ──────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "❌ python3 not found. Install Python 3.8+ and re-run."
  exit 1
fi
echo "✅ python3 found: $(python3 --version 2>&1)"
echo "✅ hook command: $HOOK_CMD"
echo ""

# ─── Cursor IDE setup ───────────────────────────────────────────────────────
setup_cursor() {
  local repo_root
  repo_root="$(cd "$HOOK_DIR/../../.." 2>/dev/null && pwd || echo "$HOOK_DIR")"
  local hooks_json="$repo_root/.cursor/hooks.json"

  echo "📦 Cursor IDE"

  if [ ! -f "$hooks_json" ]; then
    echo "  📝 Creating new .cursor/hooks.json ..."
    mkdir -p "$(dirname "$hooks_json")"

    python3 -c "
import json, sys
events = sys.argv[1:]
hooks = {}
for event in events:
    hooks[event] = [{'command': '$HOOK_CMD'}]
doc = {'version': 1, 'hooks': hooks}
print(json.dumps(doc, indent=2))
" "${CURSOR_EVENTS[@]}" > "$hooks_json"

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
skipped = []

for event in events:
    event_hooks = hooks.setdefault(event, [])
    already = any(h.get('command') == hook_cmd for h in event_hooks)
    if already:
        skipped.append(event)
    else:
        event_hooks.append({'command': hook_cmd})
        added.append(event)

with open(hooks_path, 'w') as f:
    json.dump(doc, f, indent=2)
    f.write('\n')

if added:
    print(f'  ✅ Added OTel hook to {len(added)} events: {\", \".join(added)}')
if skipped:
    print(f'  ⏭️  Already registered in {len(skipped)} events (no changes)')
if not added:
    print('  ✅ All hook events already registered — nothing to do')
" "$hooks_json" "$HOOK_CMD" "${CURSOR_EVENTS[@]}"
  fi
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
    repo_root="$(cd "$HOOK_DIR/../../.." 2>/dev/null && pwd || echo "$HOOK_DIR")"
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
skipped = []

for event in events:
    event_list = hooks.setdefault(event, [])

    # Check if otel-hook is already registered for this event
    already = False
    for entry in event_list:
        for h in entry.get('hooks', []):
            if h.get('command') == hook_cmd:
                already = True
                break
        if already:
            break

    if already:
        skipped.append(event)
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
if skipped:
    print(f'  ⏭️  Already registered in {len(skipped)} events (no changes)')
if not added:
    print('  ✅ All hook events already registered — nothing to do')
" "$settings_json" "$HOOK_CMD" "${CLAUDE_EVENTS[@]}"
}

# ─── Run setup for selected IDEs ────────────────────────────────────────────
if [[ -n "$DO_CURSOR" ]]; then
  setup_cursor
  echo ""
fi

if [[ -n "$DO_CLAUDE" ]]; then
  setup_claude
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
fi
if [[ -n "$DO_CLAUDE" ]]; then
  echo "  2. Restart Claude Code to activate hooks"
fi
echo "  3. Check logs:  tail -f ~/.local/share/opentelemetry-hooks/otel_hook.log"
echo ""
