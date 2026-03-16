#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# OpenTelemetry Hook — Quick Setup
#
# Merges the OTel hook into your existing .cursor/hooks.json (or creates one).
# Safe to run multiple times — skips hooks that are already registered.
#
# Usage:
#   bash .cursor/hooks/opentelemetry-hook/setup.sh
#
# Or from a fresh clone:
#   git clone <repo> && cd <repo>
#   bash .cursor/hooks/opentelemetry-hook/setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HOOK_DIR/../../.." && pwd)"
HOOKS_JSON="$REPO_ROOT/.cursor/hooks.json"
OTEL_CONFIG="$HOOK_DIR/otel_config.json"
OTEL_EXAMPLE="$HOOK_DIR/otel_config.example.json"
# Default to the local script so that copied-source checkouts always use
# the repo-local otel_hook.py and otel_config.json, regardless of any
# globally installed otel-hook on PATH.
# To explicitly opt into the system-installed otel-hook console command
# (e.g. for a pip-installed package deployment), set OTEL_HOOK_USE_GLOBAL=1.
if [ "${OTEL_HOOK_USE_GLOBAL:-}" = "1" ] && command -v otel-hook &>/dev/null; then
  HOOK_CMD="otel-hook"
else
  HOOK_CMD="python3 .cursor/hooks/opentelemetry-hook/otel_hook.py"
fi

# All Cursor hook events the OTel hook should register for
HOOK_EVENTS=(
  sessionStart sessionEnd
  subagentStart subagentStop
  preToolUse postToolUse postToolUseFailure
  beforeShellExecution afterShellExecution
  beforeMCPExecution afterMCPExecution
  beforeReadFile afterFileEdit
  beforeSubmitPrompt stop
)

echo "🔭 OpenTelemetry Hook Setup"
echo "─────────────────────────────"

# ── Step 1: Create otel_config.json from example if missing ──
if [ ! -f "$OTEL_CONFIG" ]; then
  if [ -f "$OTEL_EXAMPLE" ]; then
    cp "$OTEL_EXAMPLE" "$OTEL_CONFIG"
    echo "✅ Created otel_config.json from example template"
    echo "   → Edit $OTEL_CONFIG with your OTLP endpoint and auth"
  else
    echo "⚠️  No otel_config.example.json found — you'll need to create otel_config.json manually"
  fi
else
  echo "✅ otel_config.json already exists"
fi

# ── Step 2: Check for python3 ──
if ! command -v python3 &>/dev/null; then
  echo "❌ python3 not found. Install Python 3.8+ and re-run."
  exit 1
fi
echo "✅ python3 found: $(python3 --version 2>&1)"

# ── Step 3: Create or merge hooks.json ──
if [ ! -f "$HOOKS_JSON" ]; then
  # No hooks.json exists — create fresh
  echo "📝 Creating new .cursor/hooks.json ..."
  mkdir -p "$(dirname "$HOOKS_JSON")"

  # Build JSON with python3 (available since we checked above)
  python3 -c "
import json, sys
events = sys.argv[1:]
hooks = {}
for event in events:
    hooks[event] = [{'command': '$HOOK_CMD'}]
doc = {'version': 1, 'hooks': hooks}
print(json.dumps(doc, indent=2))
" "${HOOK_EVENTS[@]}" > "$HOOKS_JSON"

  echo "✅ Created $HOOKS_JSON with all OTel hook events"

else
  # hooks.json exists — merge in missing events
  echo "📝 Merging OTel hooks into existing .cursor/hooks.json ..."

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
    # Check if this command is already registered
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
    print(f'✅ Added OTel hook to {len(added)} events: {', '.join(added)}')
if skipped:
    print(f'⏭️  Already registered in {len(skipped)} events (no changes)')
if not added:
    print('✅ All hook events already registered — nothing to do')
" "$HOOKS_JSON" "$HOOK_CMD" "${HOOK_EVENTS[@]}"
fi

# ── Step 4: Kick off venv provisioning ──
echo ""
echo "🚀 Bootstrapping Python venv (runs in background) ..."
echo '{}' | python3 "$HOOK_DIR/otel_hook.py" > /dev/null 2>&1 || true

echo ""
echo "─────────────────────────────"
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit your OTLP config:  $OTEL_CONFIG"
echo "  2. Restart Cursor IDE to activate hooks"
echo "  3. Check logs:  tail -f $HOOK_DIR/otel_hook.log"
echo ""
echo "For Jaeger local dev, set in otel_config.json:"
echo '  {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"}'
echo ""
