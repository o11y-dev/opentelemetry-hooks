# opentelemetry-hooks — Robustness Spec

## Context

This spec captures findings from a diagnostic session against a live install and defines the
changes needed to make `otel-hook` reliable across IDEs, resilient to re-installs, and
consistent in the attributes it emits.

---

## Bug 1 — Test artifacts leak into `~/.claude/settings.json`

### Root cause

`tests/test_setup_sh.py::TestSetupShCommandSelection` has two tests that do NOT use
`_minimal_env`:

```python
def test_default_uses_global_when_otel_hook_available(self, tmp_path):
    env_override = {"PATH": f"{fake_bin}:{old_path}"}
    result = _run_setup(hook_dir, env=env_override)   # HOME = real ~
```

`_run_setup` does `full_env = os.environ.copy()` and only calls
`full_env.setdefault("HOME", …)` — since `HOME` is already set from the real environment,
`setdefault` is a no-op and `setup.sh` writes to the developer's real
`~/.claude/settings.json` and `~/.cursor/hooks.json`.

After two `pytest` runs the global settings looked like this (5 entries per event, 4 dead):

```
SessionStart:
  /usr/local/bin/otel-hook                          ← real
  /tmp/.../pytest-1/test_default.../fakebin/otel-hook  ← stale
  python3 /tmp/.../pytest-1/test_falls_back.../otel_hook.py  ← stale
  /tmp/.../pytest-2/test_default.../fakebin/otel-hook  ← stale
  python3 /tmp/.../pytest-2/test_falls_back.../otel_hook.py  ← stale
```

Every hook event runs 5× per invocation; 4 of those execs immediately fail (path not found).

### Fix — test isolation

In `_run_setup`, always override `HOME`:

```python
def _run_setup(hook_dir, args=None, env=None):
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    # ALWAYS use the derived tmp root as HOME so setup.sh never touches the real ~/.claude
    full_env["HOME"] = os.path.dirname(os.path.dirname(os.path.dirname(hook_dir)))
    ...
```

Changing `setdefault` → direct assignment ensures every test run is isolated regardless of
which env dict is passed.

### Fix — one-time cleanup of existing polluted settings

Add a `--clean` subcommand to `setup.sh` (see Bug 3) that removes stale entries whose
command path no longer exists on disk. Users with polluted settings can run:

```bash
bash setup.sh --clean --claude --global
```

---

## Bug 2 — `gen_ai.request.model` missing from Claude Code spans

### Root cause

The hook reads model from the event payload:

```python
# otel_hook.py:1997
_set_if_present(span, "gen_ai.request.model",
    _first_present(data, ("request_model", "model", "model_name")))
```

Claude Code's hook payloads **do not include a model field** in most events:

| Event | Claude Code payload fields | Cursor payload fields |
|---|---|---|
| `UserPromptSubmit` | `session_id`, `transcript`, `cwd` | `session_id`, `model`, `prompt`, … |
| `PreToolUse` | `session_id`, `tool_name`, `tool_input` | `session_id`, `model`, `tool_name`, … |
| `Stop` | `session_id`, `stop_reason`, `usage: {input_tokens, output_tokens}` | `session_id`, `model`, `usage`, … |

Cursor injects `model` into every hook payload. Claude Code only exposes it in `Stop` as
of recent releases, but earlier/current versions omit it entirely. Result: all Claude Code
spans have `gen_ai.request.model = null`.

### Verification

```bash
# Check current Claude Code Stop payload:
echo '{"hook_event_name":"Stop","session_id":"test","stop_reason":"end_turn","usage":{"input_tokens":100,"output_tokens":50}}' \
  | otel-hook
# Emitted span will have no gen_ai.request.model
```

### Fix A — Carry model through session state

When the hook receives a `Stop` event that includes `model`, cache it in the session state
file alongside `trace_id` and `phantom_parent_id`. When populating any span, fall back to
the cached model if the current payload doesn't provide one:

```python
# In _create_session_context / _write_session_context
if model := _first_present(data, ("request_model", "model", "model_name")):
    session_ctx["last_known_model"] = model

# In _populate_span / _apply_genai_semconv
if not model:
    model = (session_ctx or {}).get("last_known_model")
_set_if_present(span, "gen_ai.request.model", model)
```

This is retroactive: once ANY event in a session carries model, all subsequent spans in
the same session get it too.

### Fix B — Also propagate from generation batch

During `_flush_generation`, scan all batched events for the first non-null model and set
it on the parent `gen_ai.client.generation` span and any child spans that lack it:

```python
# In _flush_generation, before emitting child spans:
batch_model = next(
    (_first_present(e["data"], ("request_model", "model", "model_name"))
     for e in batch
     if _first_present(e["data"], ("request_model", "model", "model_name"))),
    None
)
# Use batch_model as fallback when populating each child span
```

### Fix C — Read model from CLAUDE_MODEL env var (if set)

Claude Code sets `CLAUDE_MODEL` in some versions. Add as a last-resort fallback:

```python
model = model or os.getenv("CLAUDE_MODEL") or os.getenv("ANTHROPIC_MODEL")
```

---

## Bug 3 — No install cleanup / idempotency tooling

### Current state

`setup.sh` deduplicates by **exact command path match**. This is correct for idempotent
adds, but there is no way to:
- Remove stale entries (dead paths)
- Remove all hooks for a given IDE (uninstall)
- Inspect what is currently registered (diagnose)

### Fix — Add `--clean`, `--uninstall`, `--diagnose` flags

```bash
# Show what's currently registered (no changes)
bash setup.sh --diagnose --claude --global

# Remove entries whose command path no longer exists on disk
bash setup.sh --clean --claude --global

# Remove ALL otel-hook entries for this IDE
bash setup.sh --uninstall --claude --global
```

`--clean` implementation (Python inline in `setup.sh`):
```python
for event, entries in list(hooks.items()):
    live = []
    removed = []
    for entry in entries:
        surviving_hooks = [
            h for h in entry.get("hooks", [])
            if os.path.exists(h.get("command", "")) or h.get("command","").startswith("/usr/local")
        ]
        if surviving_hooks:
            entry["hooks"] = surviving_hooks
            live.append(entry)
        else:
            removed.append(entry)
    hooks[event] = live
    # remove event key if empty
    if not hooks[event]:
        del hooks[event]
```

> Note: `/usr/local/bin/otel-hook` is a symlink so `os.path.exists` resolves it correctly.
> The guard `startswith("/usr/local")` is an extra safety net for system-installed binaries.

---

## Bug 4 — `gen_ai.usage.input_tokens` missing when model is missing

### Root cause

Tokens DO exist in the Claude Code `Stop` payload under `usage.input_tokens` /
`usage.output_tokens`. The hook handles this (lines 2022–2034). However, when the
batch-flush path is used, the `Stop` event data is stored in the batch file and replayed
during `_flush_generation`. If the session state file was lost or TTL-expired between
`Stop` firing and the flush, the `gen_key` lookup fails and the generation is never
flushed — so zero token spans reach the exporter.

### Fix — Defensive flush on `SessionEnd`

If `SessionEnd` fires and there is still an unflushed `current_generation` in session state,
flush it before emitting the session root span:

```python
# In the SessionEnd branch of main():
if event_name in _SESSION_END_EVENTS:
    if sk and session_ctx:
        # Flush any dangling generation
        pending_gen = session_ctx.get("current_generation")
        if pending_gen:
            _flush_generation(tracer, pending_gen, session_ctx, ide)
        _flush_session(tracer, sk, session_ctx, ide)
        _clear_session_context(sk)
```

---

## Spec — Attribute completeness guarantee

Every span MUST emit these attributes when the information is knowable:

| Attribute | Source | Fallback chain |
|---|---|---|
| `gen_ai.client.session_id` | `session_id` / `conversation_id` in payload | — |
| `gen_ai.request.model` | payload `model` / `request_model` | session state `last_known_model` → generation batch scan → `CLAUDE_MODEL` env |
| `gen_ai.usage.input_tokens` | payload `usage.input_tokens` or top-level `input_tokens` | — |
| `gen_ai.usage.output_tokens` | payload `usage.output_tokens` or top-level `output_tokens` | — |
| `gen_ai.client.hook.event` | normalized event name | — |
| `gen_ai.client.name` | `IDE_OTEL_IDE_NAME` env → parent process detection → payload heuristics | `"unknown"` |
| `gen_ai.system` | same as `gen_ai.client.name` | — |
| `gen_ai.operation.name` | derived from event type | `"unknown"` |
| `gen_ai.client.workspace` | `cwd` in payload | `os.getcwd()` |
| `gen_ai.client.timestamp` | wall clock at span creation | — |

`gen_ai.request.model` is the most important. A span without it cannot be attributed to
a model and is useless for cost/latency analysis.

---

## Spec — Install robustness requirements

`setup.sh` must satisfy:

1. **Idempotent** — running twice produces the same result as running once (already mostly
   true, but must hold after a `--clean` pass too).

2. **Never touch real config in tests** — all tests must set `HOME` to a temp directory.
   `_run_setup` must enforce this unconditionally.

3. **Self-healing** — `--clean` removes stale entries (paths that don't exist on disk).

4. **Diagnosable** — `--diagnose` prints a table of currently registered hooks and whether
   each command exists on disk, without modifying anything.

5. **Reversible** — `--uninstall` removes all hooks registered by `setup.sh` for the
   given IDE, leaving other settings intact.

6. **Detects re-registration from a different path** — if `otel-hook` was previously
   registered at path A and is now at path B (e.g. after a `pipx reinstall`), warn and
   offer to update.

---

## Spec — Hook event payload contract (per IDE)

The hook must handle both payload shapes without configuration:

### Claude Code payload shape
```json
{
  "hook_event_name": "Stop",
  "session_id": "uuid",
  "stop_reason": "end_turn",
  "usage": {
    "input_tokens": 12345,
    "output_tokens": 678,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 9000
  }
}
```
No `model` field. Hook must recover model from session state or env.

### Cursor payload shape
```json
{
  "hook_event_name": "stop",
  "sessionId": "uuid",
  "requestModel": "claude-4.6-opus-high",
  "responseModel": "claude-4.6-opus-high",
  "usage": { "inputTokens": 12345, "outputTokens": 678 }
}
```
Model always present. Key names are camelCase (normalized by `_normalize_input_data`).

### GitHub Copilot payload shape
```json
{
  "hook_event_name": "userPromptSubmitted",
  "session_id": "uuid",
  "model": "gpt-4o"
}
```

The normalization layer (`_normalize_input_data`) handles camelCase → snake_case already.
The remaining gap is the Claude Code no-model case, covered by the Fix A/B/C above.

---

## Recommended implementation order

1. **Fix test isolation** (`_run_setup` HOME override) — prevents future pollution, low risk
2. **Clean current global settings** — one-time `--clean` pass or manual edit
3. **Fix model attribution** — Fix A (session state carry-forward) + Fix B (batch scan)
4. **Defensive SessionEnd flush** — prevents lost token data
5. **Add `--clean` / `--diagnose` / `--uninstall`** — operational tooling
6. **Fix C (env var fallback)** — nice-to-have, low priority

---

## Current state of `~/.claude/settings.json` (as of 2026-03-27)

Each of the 9 Claude hook events has 5 registrations:
- 1 live: `/usr/local/bin/otel-hook`
- 4 dead: pytest temp paths under `/private/var/folders/.../pytest-{1,2}/test_{default,falls_back}.../`

Immediate remediation: manually edit `~/.claude/settings.json` to keep only the first
entry per event (the `/usr/local/bin/otel-hook` one), or run `setup.sh --clean --claude --global`
once that flag is implemented.
