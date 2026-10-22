# OpenTelemetry Hooks

Collect AI coding-agent hook callbacks as OpenTelemetry spans and optional logs.
Use one configuration across Cursor, Claude Code, Codex, Gemini CLI, GitHub
Copilot, Windsurf and OpenCode, then send OTLP to your collector or backend.

This project is **beta**. It observes the callbacks a harness exposes. Hooks do
not guarantee complete model-call, token, billing or transcript coverage.
[Provider coverage and limitations](docs/hook-coverage.md) describe the supported
contracts; fixture coverage does not imply every harness version was tested live.

## Install and register

Requires Python 3.12+. Install with the package manager you use for command-line tools:

```bash
uv tool install opentelemetry-hooks
# Alternatively:
pipx install opentelemetry-hooks
```

Register the harnesses you use:

```bash
otel-hook setup --agent cursor --agent claude --agent codex
otel-hook doctor
```

`otel-hook setup` auto-detects installed harnesses when no `--agent` is supplied.
Registration is global by default. For a project, use `--no-global --cwd /path/to/repo`.
Restart the harness after changing its hooks, and approve hooks where the harness
requires trust. Installing the package alone does not register callbacks.

| Harness | `--agent` | Global registration |
|---|---|---|
| Cursor IDE / CLI | `cursor` | `~/.cursor/hooks.json` |
| Claude Code | `claude` | `~/.claude/settings.json` |
| Codex | `codex` | `~/.codex/hooks.json` and `config.toml` |
| Gemini CLI | `gemini` | `~/.gemini/settings.json` |
| Copilot CLI | `copilot` | `~/.copilot/hooks/otel-hooks.json` |
| Windsurf | `windsurf` | `~/.codeium/windsurf/hooks.json` |
| OpenCode | `opencode` | `~/.config/opencode/plugins/otel-hook.ts` |

Copilot repository/cloud hooks use `.github/hooks/otel-hooks.json` with
`--no-global`. Antigravity and compatible runners require manual integration;
see [examples](examples/). Native Windows is supported; use Python 3.12+ and
ensure `otel-hook` is on the harness's PATH. No Bash wrapper is required.

## Configure once

The hook reads `otel_config.json` from its data directory:

- macOS/Linux installed package: `$XDG_DATA_HOME/opentelemetry-hooks`, defaulting
  to `~/.local/share/opentelemetry-hooks`.
- Windows: `%LOCALAPPDATA%/opentelemetry-hooks`.
- Source checkout: the repository directory.

The [example configuration](otel_config.example.json) is copied on first use if
no configuration exists. This is a suitable starting point for a local collector:

```json
{
  "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
  "OTEL_SERVICE_NAME": "coding-agent",
  "batch": {"on_stop": true},
  "local_spans": true,
  "capture": {"mode": "metadata_only"},
  "logs": {"enabled": false},
  "diagnostics": {"level": "WARNING"}
}
```

Use an HTTPS endpoint and your collector/backend's authentication requirements
for remote export. Standard `OTEL_*` environment variables remain supported,
including `OTEL_EXPORTER_OTLP_HEADERS` and `OTEL_RESOURCE_ATTRIBUTES`.
Coralogix, Datadog, Tempo, Jaeger and other OTLP destinations can consume these
signals through an appropriately configured collector; backend-specific native
agent analytics are separate integrations.

New JSON settings have no IDE prefix. Existing `IDE_OTEL_*` environment variables
and flat JSON aliases remain supported for runtime settings. Capture policy uses
`capture.mode`; retired content and log fan-out flags do not restore old behavior.
`IDE_OTEL_CONFIG` selects a different file; `IDE_OTEL_HOOK_HOME` selects the data
directory. Review the [upgrade steps](#upgrade-and-rollback) before replacing an
existing installation.

## Choose what to capture

| Mode | Hook fields retained |
|---|---|
| `metadata_only` | Recognized IDs, lifecycle, model, timing, status, usage and conversation length/hash facts |
| `selected_content` | Metadata plus explicitly included field paths |
| `full` | All callback fields, except explicit exclusions |

Spans contain canonical attributes and recursively extracted `hook.*`
attributes. Generated names use lowercase snake case. Values stay typed;
objects become dotted paths. **The full hook JSON is never a span attribute.**
Extraction has configurable count, depth, array, string and byte limits.

Use `capture.span_include` to allowlist additional span fields while retaining
full JSON logs. Canonical attributes remain available; `[]` disables recursive
extras, and omitting the setting permits all captured fields. The main Reflect
example selects shared evidence across agents with explicit limits and mappings;
[agent capture examples](docs/configuration.md#agent-capture-examples) provide smaller
selections. See the
[span allowlist](docs/configuration.md#span-only-allowlist) and
[Reflect configuration](examples/otel_config.reflect.example.json).

For selected content:

```json
{
  "capture": {
    "mode": "selected_content",
    "include": ["cwd", "file_path", "tool_input.command"],
    "exclude": ["tool_input.credentials"],
    "mappings": {"tool_input.command": "command.observed"}
  },
  "logs": {"enabled": true}
}
```

When logs are enabled, one correlated hook log carries the
permitted original payload. In `full` mode with no exclusions, its body is the
**full parsed hook JSON**, preserving key spelling and JSON types. It is sent
through OpenTelemetry, never printed to protocol stdout. This replaces separate
shell, MCP, tool and conversation log records. All configurations use this same
path; omitting `capture.mode` selects `metadata_only`, and logs default off.

Full capture can contain prompts, code, paths, tool output, personal information
and credentials. It is an explicit choice, not a redaction guarantee. Logs do not
use the span extraction limits, so full payloads may exceed backend log limits.
Metadata can still identify sessions, tools and models. Review your destination's
access and retention settings before enabling content capture.

See [configuration and precedence](docs/configuration.md) for field paths,
limits, managed policy and compatibility details.

## Enterprise rollout

Use the [managed deployment guide](docs/managed-deployment.md) and
[Jamf/Intune templates](examples/mdm/README.md) for offline version-pinned staging,
machine registration, detection/remediation and removal. `setup --global` is
current-user scope; machine installation uses the separate `managed` commands.
Claude can share native managed telemetry configuration with the hook. Installation
health and verified backend delivery are separate checks.

Start with a controlled pilot. The project is beta; validate the harness versions,
workstation policies and destination limits used by your organization.

1. Pin and distribute a tested package version with Python 3.12+.
2. Deliver the endpoint, credentials and capture configuration through your
   workstation-management system. Keep credentials out of repository examples.
3. Register the required harnesses in the intended global or project scope.
4. Pilot actual sessions and verify local capture **and** backend delivery.
5. Roll out gradually; rerun setup when upgrading to register added callbacks.

macOS managed preferences use `dev.o11y.opentelemetry-hook`. Windows uses
`SOFTWARE\Policies\OpenTelemetryHook`; machine settings take priority over user
settings. Managed structured `capture` and `logs` settings take precedence over
local settings and legacy environment flags. See [managed policy](docs/configuration.md#managed-policy).

The hook uses local session state to correlate separate processes into traces.
With `batch.on_stop`, events are buffered until a generation ends; pending
traces are retained when export fails. Logs are best effort and may be duplicated
on trace retries. Session lifecycle callbacks are emitted when the session is
flushed. Abrupt termination or unavailable callbacks can leave incomplete traces.

Changing capture policy isolates pending state from the previous policy to avoid
replaying previously permitted content. It can split an active session's trace.
Old files and already exported data are not retroactively deleted.

## Upgrade and rollback

Back up the configuration, managed policy and hook registrations before upgrading.
This version changes capture defaults and consumer contracts: omitted capture
mode means metadata-only, hook logs default off, and retired content/log switches
do not restore old behavior. Migrate to structured settings and update queries
that relied on separate log records or old flattened fields.

Pilot the approved version and rerun setup. To roll back, restore the tested
package version and matching configuration through the owning package manager,
then register and verify hooks again. Exported data is not retracted, and pending
state needs review before replay. See [upgrade and rollback steps](docs/upgrades.md),
including Reflect-managed installations.

## Reflect and other consumers

Local exported spans remain under `.state/local_spans/*.jsonl`. Reflect performs
skill extraction, analysis and semantic interpretation; the hook collects evidence.

The [shared Reflect profile](examples/otel_config.reflect.example.json) selects
bounded evidence and mappings across agents, with full JSON logs when enabled.
The reviewed Reflect version requires setup/dependency and consumer updates to preserve all
previous behavior. Read the [compatibility review](docs/reflect-compatibility.md)
before integration; successful ingestion alone does not prove equivalent analytics.

A stricter capture policy can remove paths or content a consumer needs. Select
those fields explicitly. Provider counters remain observations: deduplicate native
and hook telemetry, and account for provider-specific cache-token semantics.

## Verify and troubleshoot

```bash
otel-hook doctor --json
otel-hook diagnose
otel-hook setup --agent codex
```

Check registration, endpoint configuration, pending batches and delivery health.
An installed command, a green doctor result or a local span file alone does not
prove that the backend accepted telemetry. Generate a real tool call in each
pilot harness and find its session ID in the destination.

For a manual local replay with a configured collector:

```bash
printf '%s\n' '{"hook_event_name":"SessionStart","session_id":"test-123"}' | otel-hook --claude
printf '%s\n' '{"hook_event_name":"Stop","session_id":"test-123"}' | otel-hook --claude
printf '%s\n' '{"hook_event_name":"SessionEnd","session_id":"test-123"}' | otel-hook --claude
```

Hook stdout belongs to the harness response protocol. Use diagnostic logging for
troubleshooting. Check the harness's sandbox/network permissions when local
capture works but export fails.

To remove a registration:

```bash
otel-hook uninstall --agent cursor
```

## Support and security

Use [GitHub issues](https://github.com/o11y-dev/opentelemetry-hooks/issues) for bugs
and feature requests, with versions and a sanitized reproduction. See
[security reporting](SECURITY.md) for sensitive findings; do not post real hook
payloads or credentials publicly. Support is community-based, with no published
response-time or long-term support commitment. The deploying organization owns
collector operation, access control and data retention.

## Development

Edit the checkout, not installed package copies. Reinstall with the package
manager that owns `otel-hook`, then replay hooks using a temporary hook home.

```bash
uv run --no-project --python 3.12 --with . --with-requirements requirements-dev.txt python -m pytest tests/ -q
bun test tests/opencode.test.ts
```

`otel_hook.py` coordinates hook execution, session storage and telemetry export.
The `otel_hooks/` package separates provider parsing, span enrichment, lifecycle
correlation, agent identity and workspace discovery. Capture policy is passed
explicitly to enrichment and correlation. Setup, diagnostics and uninstall live
in `otel_hooks/cli.py`; source-checkout provisioning lives in `bootstrap.py`.
See [contribution guide](CONTRIBUTING.md), [telemetry reference](docs/telemetry-reference.md),
[Coralogix/OTel compatibility](docs/coralogix-compatibility.md), and [CHANGELOG.md](CHANGELOG.md).

Released under the [MIT license](LICENSE).
