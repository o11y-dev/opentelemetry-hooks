#!/bin/bash
# Run as root. Secrets are delivered by managed preferences, never CLI arguments.
set -euo pipefail
umask 022
ROOT='/Library/Application Support/OpenTelemetryHook'
ACTION="${1:-}"
WHEELS="${2:-}"
LOCK="${3:-}"
VERSION="${4:-}"
PYTHON="${O11Y_DEPLOY_PYTHON:-/usr/local/bin/python3}"
AGENTS="${O11Y_DEPLOY_AGENTS:-claude,cursor}"

[[ "$(uname -s)" == Darwin && "$EUID" == 0 ]] || { echo 'Run as root on macOS' >&2; exit 1; }
case "$ACTION" in install|remediate|detect|rollback|uninstall) ;; *) echo 'Usage: deploy-jamf.sh install|remediate|detect|rollback|uninstall [wheelhouse lock version]' >&2; exit 2;; esac
IFS=',' read -r -a AGENT_LIST <<< "$AGENTS"
for agent in "${AGENT_LIST[@]}"; do
  [[ "$agent" == claude || "$agent" == cursor ]] || { echo 'Unsupported machine agent' >&2; exit 2; }
done
HOOK="$ROOT/current/bin/otel-hook"
LAUNCHER="$ROOT/bin/otel-hook"

register_all() {
  for agent in "${AGENT_LIST[@]}"; do
    "$HOOK" managed register --agent "$agent" --scope machine --executable "$LAUNCHER"
  done
}

detect_all() {
  [[ -x "$HOOK" && -f "$ROOT/current/.ready" && -n "$VERSION" ]] || exit 1
  for agent in "${AGENT_LIST[@]}"; do
    "$HOOK" managed status --agent "$agent" --scope machine --executable "$LAUNCHER" --expected-version "$VERSION"
  done
}

case "$ACTION" in
  detect) detect_all; exit 0 ;;
  uninstall)
    [[ -x "$HOOK" ]] || { echo 'Runtime missing; restore the approved bundle before unregistering' >&2; exit 1; }
    for agent in "${AGENT_LIST[@]}"; do "$HOOK" managed unregister --agent "$agent" --scope machine; done
    rm -f "$ROOT/current" "$LAUNCHER"
    echo 'Unregistered. Versioned runtimes, managed credentials and user queues retained for explicit cleanup.'
    exit 0 ;;
  rollback)
    [[ -L "$ROOT/previous" && -x "$ROOT/previous/bin/otel-hook" && -f "$ROOT/previous/.ready" ]] || exit 1
    # Python atomically replaces the symlink; no partially installed venv is activated.
    "$ROOT/previous/bin/python" - "$ROOT" <<'PY'
import os, sys
root = sys.argv[1]
current, previous = os.readlink(root + '/current'), os.readlink(root + '/previous')
for temporary in ('.switch', '.previous-switch'):
    try: os.unlink(root + '/' + temporary)
    except FileNotFoundError: pass
os.symlink(previous, root + '/.switch')
os.replace(root + '/.switch', root + '/current')
os.symlink(current, root + '/.previous-switch')
os.replace(root + '/.previous-switch', root + '/previous')
PY
    register_all
    echo 'Runtime rolled back. Restore the matching managed policy separately; user state retained.'
    exit 0 ;;
esac

[[ -d "$WHEELS" && -f "$LOCK" && "$VERSION" =~ ^[A-Za-z0-9.+_-]+$ && "$PYTHON" == /* && -x "$PYTHON" ]] || { echo 'Supply wheelhouse, requirements.lock, approved version and an absolute installed Python path' >&2; exit 2; }
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ required"'
LOCK_HASH="$(/usr/bin/shasum -a 256 "$LOCK" | /usr/bin/awk '{print $1}')"
TARGET="$ROOT/versions/$VERSION-${LOCK_HASH:0:12}"
mkdir -p "$ROOT/versions" "$ROOT/bin"
chmod 755 "$ROOT" "$ROOT/versions" "$ROOT/bin"
if [[ ! -f "$TARGET/.ready" ]]; then
  # A failed stage is never selected. Retrying completes the same fixed venv path.
  "$PYTHON" -m venv "$TARGET"
  "$TARGET/bin/python" -m pip install --no-index --find-links "$WHEELS" --require-hashes -r "$LOCK"
  "$TARGET/bin/python" - "$VERSION" <<'PY'
import importlib.metadata, sys
from otel_hooks.bootstrap import runtime_has_otel_dependencies
assert importlib.metadata.version('opentelemetry-hooks') == sys.argv[1]
assert runtime_has_otel_dependencies()
PY
  touch "$TARGET/.ready"
fi
cat > "$ROOT/bin/.otel-hook-new" <<'SH'
#!/bin/sh
HOOK='/Library/Application Support/OpenTelemetryHook/current/bin/otel-hook'
if [ -x "$HOOK" ]; then exec "$HOOK" "$@"; fi
printf '{}\n'
SH
chown -R root:wheel "$TARGET" "$ROOT/bin"
chmod 755 "$ROOT/bin/.otel-hook-new"
mv -f "$ROOT/bin/.otel-hook-new" "$LAUNCHER"
"$TARGET/bin/python" - "$ROOT" "$TARGET" <<'PY'
import os, sys
root, target = sys.argv[1:]
for temporary in ('.switch', '.previous-switch'):
    try: os.unlink(root + '/' + temporary)
    except FileNotFoundError: pass
if os.path.islink(root + '/current') and os.readlink(root + '/current') != target:
    os.symlink(os.readlink(root + '/current'), root + '/.previous-switch')
    os.replace(root + '/.previous-switch', root + '/previous')
os.symlink(target, root + '/.switch')
os.replace(root + '/.switch', root + '/current')
PY
register_all
detect_all
