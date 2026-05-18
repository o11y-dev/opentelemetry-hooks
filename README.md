# OpenTelemetry Hook for AI Coding Agents

[![PyPI](https://img.shields.io/pypi/v/opentelemetry-hooks)](https://pypi.org/project/opentelemetry-hooks/)
[![Release](https://img.shields.io/github/v/release/o11y-dev/opentelemetry-hooks?display_name=tag)](https://github.com/o11y-dev/opentelemetry-hooks/releases)
[![Tests](https://img.shields.io/github/actions/workflow/status/o11y-dev/opentelemetry-hooks/ci.yml?branch=main&label=tests)](https://github.com/o11y-dev/opentelemetry-hooks/actions/workflows/ci.yml)
[![OpenTelemetry GenAI SemConv](https://img.shields.io/badge/OpenTelemetry-GenAI%20SemConv-425CC7?logo=opentelemetry)](https://opentelemetry.io/docs/specs/semconv/gen-ai/)

> Observability for AI coding agents — any OTLP-compatible backend.

An open-source OpenTelemetry integration that captures AI coding agent activity as structured **traces and logs** and exports them to any OTLP-compatible backend. Works with **any AI coding agent** — today: **Antigravity**, **Claude Code**, **Codex**, **Cursor IDE / Cursor CLI**, **Gemini CLI**, **GitHub Copilot**, and **OpenCode** — using [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/).

Every hook event — prompt submissions, tool calls, shell commands, MCP interactions, file edits, subagent orchestration — becomes an OpenTelemetry span you can query, alert on, and visualize in Jaeger, Grafana, Datadog, Honeycomb, Coralogix, or any OTLP-compatible backend.

> **Note**: Claude Code and Codex have native OpenTelemetry support, but this repo can also be used as a hook target when you want the same hook-based pipeline across all agents. Avoid enabling native Codex OTel export and `otel-hook` for the same events unless you intentionally want duplicate telemetry.

## How It Works

The hook is a lightweight Python command that your IDE invokes on every agent event. The IDE pipes a JSON payload to stdin, the hook processes it, emits OpenTelemetry spans and logs, and returns `{"continue": true}` on stdout so the IDE proceeds normally. No sidecar, no daemon — just a command your IDE calls.

```
IDE Event → stdin (JSON) → otel-hook → OpenTelemetry SDK → OTLP Backend
                                 ↓
                          stdout: {"continue": true}
```

## Features

- **Multi-agent support**: One hook command, multiple agent integrations. The CLI and `setup.sh` can register Codex, Cursor, Claude Code, Gemini CLI, GitHub Copilot, and OpenCode. Antigravity and compatible hook runners can call the same hook command directly. Runtime detection prefers parent-process discovery first, then explicit overrides, then self-reported payload fields, and finally heuristics when needed.

- **Session-level Traces**: Groups all events within a session into a single trace with a 3-tier hierarchy:

```
gen_ai.client.session (root)
├── gen_ai.client.generation (gen-1)
│   ├── gen_ai.client.hook.UserPromptSubmit
│   ├── gen_ai.client.hook.PreToolUse
│   ├── gen_ai.client.hook.PostToolUse
│   └── gen_ai.client.hook.Stop
├── gen_ai.client.generation (gen-2)
│   ├── gen_ai.client.hook.UserPromptSubmit
│   ├── gen_ai.client.hook.PreToolUse
│   ├── gen_ai.client.hook.PostToolUse
│   └── gen_ai.client.hook.Stop
└── gen_ai.client.hook.SessionEnd
```

- **GenAI Semantic Conventions**: Emits OpenTelemetry GenAI attributes aligned with v1.37+ (`gen_ai.provider.name`, `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.*`, etc.) while preserving legacy `gen_ai.system` for backward compatibility.

- **All Hook Events**: Captures the full lifecycle — sessions, prompts, tool usage, shell commands, MCP calls, file operations, subagents, errors, and more.

- **Structured OTel Logs**: Emits trace-correlated log records for MCP calls, shell executions, and tool usage — with full I/O payloads, server output, and duration. Logs are exported via OTLP alongside spans.

- **Zero Setup**: Auto-provisions a Python virtual environment on first run. No manual install needed.

- **Privacy Controls**: Built-in masking of emails, tokens, and usernames. Text capture is opt-in.

- **JSON Config File**: All settings in `otel_config.json` — no environment variable exports needed.

## Supported Agents

| Agent | Setup Command | Scope | Config Written |
|---|---|---|---|
| Cursor IDE / CLI | `otel-hook setup --agent cursor` | Global by default; use `--no-global` for project scope | `~/.cursor/hooks.json` or `.cursor/hooks.json` |
| Claude Code | `otel-hook setup --agent claude` | Global by default; use `--no-global` for project scope | `~/.claude/settings.json` or `.claude/settings.json` |
| Gemini CLI | `otel-hook setup --agent gemini` | Global by default; use `--no-global` for project scope | `~/.gemini/settings.json` or `.gemini/settings.json` |
| GitHub Copilot coding agent | `otel-hook setup --agent copilot --no-global` | Repository only | `.github/hooks/otel-hooks.json` |
| OpenCode | `otel-hook setup --agent opencode` | Global by default; use `--no-global` for project scope | `~/.config/opencode/plugins/otel-hook.ts` or `.opencode/plugins/otel-hook.ts` |
| Antigravity / compatible runners | Manual hook command | Runner-defined | Runner workflow/config |

Run `otel-hook diagnose` to see what is currently registered, and `otel-hook uninstall --agent <agent>` to remove this hook from an agent config.

## Supported Events

| Canonical Name | Antigravity / Claude Code | Codex | Cursor IDE / CLI | Gemini CLI | GitHub Copilot | OpenCode (plugin) |
|---|---|---|---|---|---|---|
| `SessionStart` | `SessionStart` | `SessionStart` | `sessionStart` | `SessionStart` | `sessionStart` | `session.created` |
| `SessionEnd` | `SessionEnd` | — | `sessionEnd` | `SessionEnd` | `sessionEnd` | `session.deleted`, `session.error` |
| `UserPromptSubmit` | `UserPromptSubmit` | `UserPromptSubmit` | `beforeSubmitPrompt` | `BeforeModel` ¹ | `userPromptSubmitted` | `message.updated` (role=user) |
| `PreToolUse` | `PreToolUse` | `PreToolUse` | `preToolUse` | `BeforeTool` | `preToolUse` | `tool.execute.before` ² |
| `PermissionRequest` | — | `PermissionRequest` | — | — | — | — |
| `PostToolUse` | `PostToolUse` | `PostToolUse` | `postToolUse` | `AfterTool` | `postToolUse` | `tool.execute.after` (exit=0) |
| `PostToolUseFailure` | `PostToolUseFailure` | — | `postToolUseFailure` | — | — | `tool.execute.after` (exit≠0) |
| `Stop` | `Stop` | `Stop` | `stop` | `AfterModel` ¹ | — | `session.idle` |
| `SubagentStart` | `SubagentStart` | — | `subagentStart` | `BeforeAgent` | — | — ³ |
| `SubagentStop` | `SubagentStop` | — | `subagentStop` | `AfterAgent` | — | — ³ |
| `ErrorOccurred` | — | — | — | — | `errorOccurred` | — |
| `BeforeShellExecution` | — | — | `beforeShellExecution` | — | — | — ² |
| `AfterShellExecution` | — | — | `afterShellExecution` | — | — | — ² |
| `BeforeMCPExecution` | — | — | `beforeMCPExecution` | — | — | — ² |
| `AfterMCPExecution` | — | — | `afterMCPExecution` | — | — | — ² |
| `BeforeReadFile` | — | — | `beforeReadFile` | — | — | — ² |
| `AfterFileEdit` | — | — | `afterFileEdit` | — | — | `file.edited` |

¹ Gemini CLI uses `BeforeModel`/`AfterModel` where other agents use `UserPromptSubmit`/`Stop`; the hook normalizes both to canonical span names.<br>
² OpenCode routes bash, read, write, MCP, and all other tools through the universal `tool.execute.before/after` hooks, so these events are observable as `PreToolUse`/`PostToolUse` with the appropriate `tool_name`.<br>
³ Subagent invocations surface as `PreToolUse`/`PostToolUse` with `tool_name=task` — there are no dedicated subagent hook events in OpenCode.

## Installation

```bash
# Recommended: pipx keeps otel-hook on PATH in an isolated venv
pipx install opentelemetry-hooks

# Or with pip
pip install opentelemetry-hooks
```

To pin a specific version or install directly from a tag:

```bash
pipx install git+https://github.com/o11y-dev/opentelemetry-hooks.git@v0.13.0
```

Or install from a pre-built wheel from the [Releases](https://github.com/o11y-dev/opentelemetry-hooks/releases) page:

```bash
pipx install opentelemetry_hooks-*.whl
```

Once installed, run `otel-hook setup` to wire your agents.

## Quick Start

### One-Command Setup (pip/pipx install)

After installing the package, configure your agents with the built-in CLI:

```bash
# Auto-detect all installed agents and configure globally
otel-hook setup

# Configure a specific agent
otel-hook setup --agent claude
otel-hook setup --agent cursor
otel-hook setup --agent copilot --no-global   # project-scoped (run from repo root)
otel-hook setup --agent gemini
otel-hook setup --agent codex
otel-hook setup --agent opencode

# Project-scoped instead of global
otel-hook setup --agent cursor --no-global
otel-hook setup --agent codex --no-global

# Check registration status
otel-hook diagnose

# Remove hooks
otel-hook uninstall --agent claude
```

Setup is idempotent — safe to re-run. Then configure your OTLP endpoint:

```bash
vim ~/.local/share/opentelemetry-hooks/otel_config.json
```

### Python API (importable)

The setup functions are importable for programmatic use:

```python
from otel_hook import setup_claude, setup_cursor, setup_gemini

setup_claude(global_=True)   # ~/.claude/settings.json
setup_cursor(global_=True)   # ~/.cursor/hooks.json
setup_gemini(global_=True)   # ~/.gemini/settings.json
```

### Source Checkout Setup

If you're working from a source checkout rather than a pip install, use the
bundled `setup.sh`:

```bash
# Auto-detect installed/supported agents and configure them
bash setup.sh

# Configure one agent
bash setup.sh --cursor --global
bash setup.sh --claude --global
bash setup.sh --gemini --global
bash setup.sh --copilot       # repository-scoped
bash setup.sh --opencode --global
```

Then edit your endpoint config and restart the configured agent:

```bash
vim otel_config.json
```

### Clone Into an Existing Project

If your project doesn't have the hook yet, copy the entire hook directory and run setup:

```bash
# Clone the hook repo and copy the essential files into your project
git clone https://github.com/o11y-dev/opentelemetry-hooks.git /tmp/otel-hook-source
mkdir -p .cursor/hooks/opentelemetry-hook
cp /tmp/otel-hook-source/otel_hook.py .cursor/hooks/opentelemetry-hook/
cp /tmp/otel-hook-source/setup.sh .cursor/hooks/opentelemetry-hook/
cp /tmp/otel-hook-source/otel_config.example.json .cursor/hooks/opentelemetry-hook/
cp /tmp/otel-hook-source/.gitignore .cursor/hooks/opentelemetry-hook/
cp -r /tmp/otel-hook-source/examples .cursor/hooks/opentelemetry-hook/

# Run setup — creates/merges hooks.json automatically
bash .cursor/hooks/opentelemetry-hook/setup.sh
rm -rf /tmp/otel-hook-source
```

### Prerequisites

- Python 3.12+ (the setup script checks for this)
- An OTLP-compatible backend (Jaeger, Coralogix, Datadog, Grafana, Honeycomb, etc.)

### Other IDEs

#### Cursor CLI

Cursor CLI uses the same `.cursor/hooks.json` configuration and hook payload shape as Cursor IDE, so the Cursor IDE setup above in [Quick Start](#quick-start) also covers Cursor CLI. Its spans are recorded with the canonical `gen_ai.client.name=cursor`.

#### GitHub Copilot

```bash
# Repo-scoped hooks file (.github/hooks/otel-hooks.json)
bash setup.sh --copilot
```

`setup.sh --copilot` creates or merges `.github/hooks/otel-hooks.json` and points each event directly at `otel-hook` (or the local `otel_hook.py` fallback). Copilot is then detected from the process tree first, with `session_id`-based heuristics as a fallback.

GitHub Copilot hooks are repository-scoped, so `--copilot --global` is intentionally unsupported. Commit `.github/hooks/otel-hooks.json` to your default branch for the coding agent to pick it up.

If you prefer a manual install, copy the bundled example instead:

```bash
mkdir -p .github/hooks
cp examples/copilot-hooks.example.json .github/hooks/otel-hooks.json
```

Then replace `{{SCRIPT_PATH}}` with the hook command. For a copied-source checkout the default is `python3 .cursor/hooks/opentelemetry-hook/otel_hook.py`; use `otel-hook` only when the package is installed via pipx or pip.
See [GitHub Copilot hooks docs](https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-hooks).

#### Claude Code

```bash
mkdir -p .claude
cp examples/claude-hooks.example.json .claude/settings.json
```

Replace `{{SCRIPT_PATH}}` with the hook command, for example:

```bash
# source checkout / copied-source
python3 .cursor/hooks/opentelemetry-hook/otel_hook.py
# pip-installed package
otel-hook
```

The bundled Claude example and `setup.sh --claude` both invoke `otel-hook` directly without an IDE override env var.
Claude Code is auto-detected from the parent process tree first; hook metadata such as `session_id`, `transcript_path`, `permission_mode`, and `notification_type` is used as a fallback. The camelCase alias handling is mainly for compatible third-party hook runners and mixed payload formats.

#### Gemini CLI

```bash
# Global Gemini settings (~/.gemini/settings.json)
otel-hook setup --agent gemini

# Project-scoped Gemini settings (.gemini/settings.json)
otel-hook setup --agent gemini --no-global
```

For a source checkout, use:

```bash
bash setup.sh --gemini --global
```

Gemini CLI emits model, tool, and agent lifecycle events. The hook maps `BeforeModel` / `AfterModel` to prompt and stop spans, `BeforeTool` / `AfterTool` to tool spans, and `BeforeAgent` / `AfterAgent` to subagent spans.

#### Antigravity

Antigravity workflow and hook formats can vary, so the simplest integration is to invoke the hook command directly from your workflow/rule and pin the IDE name explicitly:

```bash
# source checkout / copied-source
env IDE_OTEL_IDE_NAME=antigravity python3 .cursor/hooks/opentelemetry-hook/otel_hook.py
# pip-installed package
env IDE_OTEL_IDE_NAME=antigravity otel-hook
```

An example Antigravity workflow is included in `examples/antigravity-workflow.example.md`:

```bash
mkdir -p .agent/workflows
cp .cursor/hooks/opentelemetry-hook/examples/antigravity-workflow.example.md .agent/workflows/opentelemetry-hook.md
```

Replace `{{SCRIPT_PATH}}` in the copied workflow with the hook command you want Antigravity to invoke. For a copied-source checkout use `python3 .cursor/hooks/opentelemetry-hook/otel_hook.py`; use `otel-hook` for a pip-installed package.

#### Codex

Codex hooks are configured in `~/.codex/hooks.json` or `<repo>/.codex/hooks.json` and require the `hooks` feature flag in the matching `config.toml`.

**Quick setup (recommended):**

```bash
# Global — available in every Codex session
bash setup.sh --codex --global

# Project-level — only active for this project
bash setup.sh --codex
```

The setup command enables:

```toml
[features]
hooks = true
```

**Manual install:**

```bash
mkdir -p .codex
cp examples/codex-hooks.example.json .codex/hooks.json
```

Then replace `{{SCRIPT_PATH}}` with `otel-hook` or the absolute source checkout command.

**Events captured:** `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, and `Stop`. Codex does not currently emit a hook-level `SessionEnd`, so unfinished sessions are closed by this hook's stale-session cleanup when needed.

#### OpenCode

A native TypeScript plugin is included at `plugin/opencode.ts`. It hooks into OpenCode's session and tool lifecycle events and pipes JSON payloads to `otel-hook` on stdin — the same pattern used by [rtk](https://github.com/rtk-ai/rtk).

**Quick setup (recommended):**

```bash
# Global — available in every OpenCode session
bash setup.sh --opencode --global

# Project-level — only active for this project
bash setup.sh --opencode
```

**Manual install:**

```bash
# Global
mkdir -p ~/.config/opencode/plugins
cp plugin/opencode.ts ~/.config/opencode/plugins/otel-hook.ts

# Project-level
mkdir -p .opencode/plugins
cp plugin/opencode.ts .opencode/plugins/otel-hook.ts
```

Restart OpenCode after installing. The bundled plugin — including the copy installed by `setup.sh --opencode` — invokes `otel-hook` directly. The runtime prefers parent-process discovery, while the plugin's `source_app: "OpenCode"` payload field remains a compatibility fallback. `OPENCODE_CONFIG_DIR` is respected if set.

**Events captured:** `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure` (detected via `metadata.exit`), `Stop`, `AfterFileEdit`. Bash, read, write, MCP, and subagent (`task`) tool calls all flow through the universal `tool.execute.before/after` hooks and appear as `PreToolUse`/`PostToolUse` with the appropriate `tool_name`.

#### Other compatible runners

For any hook runner not listed above, invoke `otel-hook` (or `python3 .../otel_hook.py`) and forward compatible hook JSON on stdin. Pass a self-reported client field such as `ide_name`, `client`, or `source_app` with the value matching your tool, or set `IDE_OTEL_IDE_NAME` in the environment. When your runner uses camelCase payload keys such as `sessionId`, `toolName`, `toolInput`, or `hookEventType`, the hook normalizes them automatically before exporting spans.

#### GitHub Copilot — Recommended Repositories

To make this hook automatically available to the GitHub Copilot coding agent across your organization's repositories, add it as a [recommended repository](https://docs.github.com/en/copilot/customizing-copilot/adding-repository-instructions-for-github-copilot):

1. Go to your organization settings → **Copilot** → **Coding agent** → **Recommended repositories**
2. Add `o11y-dev/opentelemetry-hooks` to the list
3. The Copilot coding agent will now be able to reference this repo for hook setup and configuration

### Configuration

Edit the hook config file. For pip/pipx installs this lives at `~/.local/share/opentelemetry-hooks/otel_config.json` unless `IDE_OTEL_HOOK_HOME` is set. For a source checkout or copied-source install, edit the local `otel_config.json` next to `otel_hook.py`.

```json
{
  "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
  "OTEL_SERVICE_NAME": "ide-agent",
  "IDE_OTEL_BATCH_ON_STOP": "true"
}
```

Then restart your agent or IDE.

## Configuration Reference

### OTLP Exporter

| Variable | Description | Default |
|----------|-------------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector endpoint | `http://localhost:4317` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc`, `http/protobuf`, or `http/json` | `grpc` |
| `OTEL_EXPORTER_OTLP_HEADERS` | Auth headers (URL-encoded `key=value` pairs) | — |
| `OTEL_SERVICE_NAME` | Service name in traces | `ide-agent` |

> **Note**: `OTEL_EXPORTER_OTLP_INSECURE` is only used by the OTLP **gRPC** exporter (`OTEL_EXPORTER_OTLP_PROTOCOL=grpc`). It defaults to `true` (plaintext); set to `false` for TLS-secured gRPC endpoints. For `http/protobuf` and `http/json` exporters, TLS is determined by the endpoint scheme (`https://` vs `http://`).

### Hook Behavior

| Variable | Description | Default |
|----------|-------------|---------|
| `IDE_OTEL_BATCH_ON_STOP` | Enable session-level batching (recommended) | `false` |
| `IDE_OTEL_IDE_NAME` | Force the detected IDE name (`codex`, `cursor`, `copilot`, `claude`, `gemini`, `antigravity`, `opencode`) for generic hook runners; common labels like `OpenAI Codex`, `Codex CLI`, `GitHub Copilot`, `Claude Code`, `Cursor IDE` / `Cursor CLI`, `Gemini CLI`, `Anti Gravity`, `OpenCode`, and their `... CLI` / `... IDE` variants normalize automatically | auto-detect |
| `IDE_OTEL_LOCAL_SPANS` | Save hook spans locally as JSONL files for agent analysis (`.state/local_spans/*.jsonl`) | unset |
| `IDE_OTEL_CAPTURE_TEXT` | Include prompt/response text in spans | `false` |
| `IDE_OTEL_MASK_PROMPTS` | Redact emails, tokens, usernames from text | `false` |
| `IDE_OTEL_TEXT_MAX_CHARS` | Max characters for captured text | `4000` |
| `IDE_OTEL_CAPTURE_TOOL_INPUT_CONTENT` | Include tool input content in logs | `false` |
| `IDE_OTEL_CAPTURE_TOOL_DEFINITIONS` | Include tool definitions in spans | `false` |

### OTel Logs

| Variable | Description | Default |
|----------|-------------|---------|
| `IDE_OTEL_ENABLE_LOGS` | Enable OTel Logs signal export (OTLP) | `true` |
| `IDE_OTEL_MCP_LOG_PAYLOAD` | Include full MCP input/output payloads in logs | `true` |
| `IDE_OTEL_LOG_ALL_EVENTS` | Emit OTel log records for all hook events (not just MCP/shell/tool) | `false` |
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | Override OTLP logs endpoint (auto-derived from traces endpoint if not set) | — |

### Resource Attributes

| Variable | Description | Default |
|----------|-------------|---------|
| `OTEL_RESOURCE_ATTRIBUTES` | Comma-separated `key=value` pairs | — |
| `IDE_OTEL_APP_NAME` | Application name | `ide-agent` |
| `IDE_OTEL_SUBSYSTEM_NAME` | Subsystem name (Coralogix) | `ide-hooks` |

### Logging & Debug

| Variable | Description | Default |
|----------|-------------|---------|
| `IDE_OTEL_LOG_LEVEL` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `WARNING` |
| `IDE_OTEL_LOG_FILE` | Log file path | `<hook-home>/otel_hook.log` |
| `IDE_OTEL_LOG_EVENTS` | Log each hook event to file | `false` |
| `IDE_OTEL_DEBUG_CONSOLE` | Print spans to stdout (for debugging) | `false` |

### Advanced (Rarely Needed)

These settings have sensible defaults and typically don't need to be changed:

| Variable | Description | Default |
|----------|-------------|---------|
| `OTEL_EXPORTER_OTLP_INSECURE` | **gRPC only**: `true` for plaintext, `false` for TLS | `true` |
| `IDE_OTEL_DISABLE_BATCH` | Disable OpenTelemetry batch span processor | `false` |
| `IDE_OTEL_STATE_TTL_SECONDS` | TTL for state files before cleanup | `86400` |
| `IDE_OTEL_STATE_CLEANUP_INTERVAL_SECONDS` | Minimum interval between cleanup runs | `3600` |
| `IDE_OTEL_STATE_LOCK_TIMEOUT_SECONDS` | Max time to wait for state file locks | `2` |
| `IDE_OTEL_HOOK_HOME` | Override the hook's writable home directory (config, state, venv, log) | See below |

> **`IDE_OTEL_HOOK_HOME`**: When `otel-hook` runs from an installed package (i.e. the module lives inside *site-packages*), the hook automatically uses `$XDG_DATA_HOME/opentelemetry-hooks` (defaulting to `~/.local/share/opentelemetry-hooks`) instead of the package directory, so all writable files are placed in a user-owned location. Set `IDE_OTEL_HOOK_HOME` to an absolute path to override this location explicitly (useful for project-local or shared deployments). When running from a source checkout or a directly-copied script, the directory that contains `otel_hook.py` is used as before.

## Hook Stdout Response

The hook writes a JSON response to stdout for the IDE/client.

- Default (backward compatible):

```json
{"continue": true}
```

- If `IDE_OTEL_LOCAL_SPANS` is explicitly set (`true` or `false`), the response includes:

```json
{"continue": true, "local_spans": true}
```

For the stdout response field, `local_spans` uses `IDE_OTEL_LOCAL_SPANS` when set; otherwise internal behavior falls back to `IDE_OTEL_BATCH_ON_STOP`.

## Local Trace Files (Agent-Friendly)

When local trace saving is enabled, each hook event is also written to JSONL in:

- `.cursor/hooks/opentelemetry-hook/.state/local_spans/<session_key>.jsonl`
- `.cursor/hooks/opentelemetry-hook/.state/local_spans/unscoped.jsonl` (if no session key exists)

Each line is a single JSON object, for example:

```json
{
  "timestamp_ns": 1771976482308258082,
  "event": "UserPromptSubmit",
  "ide": "copilot",
  "session_key": "agent-s1",
  "generation_key": null,
  "data": {
    "hook_event_name": "beforeSubmitPrompt",
    "session_id": "agent-s1",
    "prompt": "hello"
  }
}
```

## MDM / Managed Configuration

For enterprise deployments, configuration can be pushed to developer machines via MDM (Mobile Device Management) systems such as Jamf, Intune, or Group Policy. MDM-managed settings override `otel_config.json` values but can still be overridden by environment variables.

**Precedence** (highest to lowest):

1. Environment variables
2. MDM-managed configuration (macOS plist / Windows registry)
3. `otel_config.json` file
4. Built-in defaults

### macOS (Configuration Profile)

The hook reads managed preferences from the domain `dev.o11y.opentelemetry-hook`. Deploy a `.mobileconfig` profile via Jamf, Mosyle, or Apple Business Manager with the following payload:

```xml
<dict>
    <key>PayloadType</key>
    <string>dev.o11y.opentelemetry-hook</string>
    <key>OTEL_EXPORTER_OTLP_ENDPOINT</key>
    <string>https://otel-collector.corp.example.com:4317</string>
    <key>OTEL_EXPORTER_OTLP_PROTOCOL</key>
    <string>grpc</string>
    <key>OTEL_SERVICE_NAME</key>
    <string>corp-ide-agent</string>
    <key>IDE_OTEL_CAPTURE_TEXT</key>
    <string>false</string>
</dict>
```

The managed plist is read from:
- `/Library/Managed Preferences/dev.o11y.opentelemetry-hook.plist` (device-level)
- `~/Library/Managed Preferences/dev.o11y.opentelemetry-hook.plist` (user-level fallback)

### Windows (Registry / Group Policy)

The hook reads string values from the Windows registry under:

```
HKEY_LOCAL_MACHINE\SOFTWARE\Policies\OpenTelemetryHook
```

with a fallback to `HKEY_CURRENT_USER`. Deploy via Intune, Group Policy (ADMX), or any MDM that manages registry keys:

| Registry Value Name | Type | Example |
|---------------------|------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `REG_SZ` | `https://otel-collector.corp.example.com:4317` |
| `OTEL_SERVICE_NAME` | `REG_SZ` | `corp-ide-agent` |
| `IDE_OTEL_CAPTURE_TEXT` | `REG_SZ` | `false` |

Any key from the [Configuration Reference](#configuration-reference) can be set via MDM.

## Backend Examples

### Jaeger (Local Development)

```bash
docker run -d --name jaeger \
  -p 4317:4317 -p 4318:4318 -p 16686:16686 \
  jaegertracing/all-in-one:latest
```

```json
{
  "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
  "OTEL_SERVICE_NAME": "ide-agent",
  "IDE_OTEL_BATCH_ON_STOP": "true"
}
```

View traces at http://localhost:16686

### Jaeger + Local File Export

Send traces to Jaeger **and** save them as local JSONL files for agent analysis or offline inspection:

```bash
docker run -d --name jaeger \
  -p 4317:4317 -p 4318:4318 -p 16686:16686 \
  jaegertracing/all-in-one:latest
```

```json
{
  "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
  "OTEL_SERVICE_NAME": "ide-agent",
  "IDE_OTEL_BATCH_ON_STOP": "true",
  "IDE_OTEL_LOCAL_SPANS": "true"
}
```

Traces are exported to Jaeger at http://localhost:16686 and simultaneously written to `.state/local_spans/<session>.jsonl`.

### Local Files Only (No Backend)

Save spans as local JSONL files without sending to any remote backend. Useful for offline debugging, CI environments, or feeding traces back to an agent:

```json
{
  "OTEL_SERVICE_NAME": "ide-agent",
  "IDE_OTEL_BATCH_ON_STOP": "true",
  "IDE_OTEL_LOCAL_SPANS": "true"
}
```

Omit `OTEL_EXPORTER_OTLP_ENDPOINT` to skip remote export. Spans are written to `.state/local_spans/<session>.jsonl`. Each line is a JSON object with trace/span IDs, attributes, and timing — see [Local Trace Files](#local-trace-files-agent-friendly) for the format.

### Coralogix

```json
{
  "OTEL_EXPORTER_OTLP_ENDPOINT": "https://ingress.<region>.coralogix.com:443/v1/traces",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
  "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer%20<YOUR_API_KEY>",
  "OTEL_SERVICE_NAME": "ide-agent",
  "IDE_OTEL_BATCH_ON_STOP": "true"
}
```

Replace `<region>` with your Coralogix domain (e.g., `us1`, `eu1`, `ap1`).
If Coralogix requires `cx.application.name`, add it via `OTEL_RESOURCE_ATTRIBUTES`:

```json
{
  "OTEL_RESOURCE_ATTRIBUTES": "cx.application.name=ide-agent"
}
```

### Datadog

```json
{
  "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
  "OTEL_SERVICE_NAME": "ide-agent",
  "IDE_OTEL_BATCH_ON_STOP": "true"
}
```

Requires the [Datadog Agent](https://docs.datadoghq.com/opentelemetry/) with OTLP ingestion enabled.

### Grafana / Tempo

```json
{
  "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp-gateway-<zone>.grafana.net/otlp",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
  "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Basic%20<BASE64_CREDENTIALS>",
  "OTEL_SERVICE_NAME": "ide-agent",
  "IDE_OTEL_BATCH_ON_STOP": "true"
}
```

### Honeycomb

```json
{
  "OTEL_EXPORTER_OTLP_ENDPOINT": "https://api.honeycomb.io",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
  "OTEL_EXPORTER_OTLP_HEADERS": "x-honeycomb-team=<YOUR_API_KEY>",
  "OTEL_SERVICE_NAME": "ide-agent",
  "IDE_OTEL_BATCH_ON_STOP": "true"
}
```

## Span Attributes

### Common (All Spans)

| Attribute | Description |
|-----------|-------------|
| `gen_ai.client.hook.event` | Canonical event name (PascalCase) |
| `gen_ai.client.name` | Outer IDE or hook host (`codex`, `cursor`, `copilot`, `claude`, `opencode`, etc.) |
| `gen_ai.client.agent_engine` | Inner agent engine when it differs from the outer IDE (for example Cursor running Claude Code) |
| `gen_ai.client.session_id` | Session identifier |
| `gen_ai.client.generation_id` | Generation identifier (Cursor) |
| `gen_ai.client.workspace` | Workspace / working directory |
| `gen_ai.client.timestamp` | Event timestamp (ISO 8601) |
| `gen_ai.system` | Deprecated legacy GenAI system/provider attribute retained for backward compatibility |
| `gen_ai.operation.name` | `chat`, `execute_tool`, or `invoke_agent` |

### GenAI (When Available)

| Attribute | Description |
|-----------|-------------|
| `gen_ai.provider.name` | Canonical GenAI provider when inferred from payload/model metadata |
| `gen_ai.request.model` | Requested model name |
| `gen_ai.response.model` | Response model name |
| `gen_ai.conversation.id` | Session / conversation ID |
| `gen_ai.usage.input_tokens` | Input token count |
| `gen_ai.usage.output_tokens` | Output token count |
| `gen_ai.usage.cache_creation.input_tokens` | Cache-write input token count when provided |
| `gen_ai.usage.cache_read.input_tokens` | Cache-read input token count when provided |
| `gen_ai.request.temperature` | Temperature setting |
| `gen_ai.request.max_tokens` | Max tokens setting |
| `gen_ai.request.choice.count` | Requested number of choices/candidates |
| `gen_ai.output.type` | Requested output modality (`text`, `json`, `image`, `speech`) |
| `gen_ai.agent.id` / `gen_ai.agent.name` | Agent identity when the hook payload includes agent metadata |
| `gen_ai.response.finish_reasons` | Finish reasons array |
| `gen_ai.system_instructions` | System instructions (opt-in text capture) |
| `gen_ai.input.messages` | Input messages (opt-in) |
| `gen_ai.output.messages` | Output messages (opt-in) |

### Event-Specific

| Event | Key Attributes |
|-------|---------------|
| `UserPromptSubmit` | `gen_ai.client.composer_mode`, `gen_ai.request.model` |
| `PreToolUse` / `PostToolUse` | `gen_ai.client.tool_name`, `gen_ai.client.tool_id`, `gen_ai.client.duration_ms` |
| `PostToolUseFailure` | `gen_ai.client.tool_name`, `gen_ai.client.error` |
| `BeforeShellExecution` / `AfterShellExecution` | `gen_ai.client.command`, `gen_ai.client.cwd`, `gen_ai.client.exit_code` |
| `BeforeMCPExecution` / `AfterMCPExecution` | `gen_ai.client.mcp_server`, `gen_ai.client.mcp_tool` |
| `BeforeReadFile` / `AfterFileEdit` | `gen_ai.client.file_path`, `gen_ai.client.edits` |
| `SubagentStart` / `SubagentStop` | `gen_ai.client.subagent_type`, `gen_ai.client.agent_id` |
| `Stop` | `gen_ai.client.status`, `gen_ai.client.loop_count` |
| `ErrorOccurred` | `gen_ai.client.error`, `gen_ai.client.is_interrupt` |

## OTel Logs (MCP, Shell, Tool Events)

When `IDE_OTEL_ENABLE_LOGS=true` (default), the hook emits structured OpenTelemetry log records alongside traces. Log records are automatically correlated with the active span's trace context, so you can jump between traces and logs in your backend.

### What gets logged

| Event Type | Log Records | Payload Control |
|------------|-------------|----------------|
| **MCP calls** (`BeforeMCPExecution`, `AfterMCPExecution`) | Always when logs enabled | `IDE_OTEL_MCP_LOG_PAYLOAD` |
| **Shell execution** (`BeforeShellExecution`, `AfterShellExecution`) | Always when logs enabled | `IDE_OTEL_MCP_LOG_PAYLOAD` |
| **Tool usage** (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`) | Always when logs enabled | `IDE_OTEL_CAPTURE_TOOL_INPUT_CONTENT` |
| **All other events** | Only when `IDE_OTEL_LOG_ALL_EVENTS=true` | — |

### MCP Log Attributes

| Attribute | Description |
|-----------|-------------|
| `gen_ai.client.mcp_server` | MCP server name |
| `gen_ai.client.mcp_tool` | MCP tool name |
| `gen_ai.client.mcp.input` | Full input payload (opt-in) |
| `gen_ai.client.mcp.input.length` | Input payload size |
| `gen_ai.client.mcp.input.sha256` | Input payload hash |
| `gen_ai.client.mcp.output` | Full output payload (opt-in) |
| `gen_ai.client.mcp.output.length` | Output payload size |
| `gen_ai.client.mcp.output.sha256` | Output payload hash |
| `gen_ai.client.mcp.duration_ms` | MCP call duration |
| `gen_ai.client.mcp.stdout` | Server stdout (if available) |
| `gen_ai.client.mcp.stderr` | Server stderr (if available) |

### Endpoint Derivation

The logs endpoint is derived automatically:

1. If `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` is set, it's used directly
2. Otherwise, `/v1/traces` is replaced with `/v1/logs` in `OTEL_EXPORTER_OTLP_ENDPOINT`
3. For gRPC, the same endpoint serves all signals

Example: `https://ingress.us1.coralogix.com:443/v1/traces` → `https://ingress.us1.coralogix.com:443/v1/logs`

## Session-level Batching

When `IDE_OTEL_BATCH_ON_STOP=true` (recommended):

1. **SessionStart**: If the hook receives upstream trace context (`traceparent` / `TRACEPARENT`, or explicit `trace_id` + `span_id` / `parent_span_id`), it adopts that real `trace_id` and parent span for the session. Otherwise it pre-generates a synthetic `trace_id`. State is stored in `.state/sessions/`.
2. **Generation events**: Buffered to `.state/batches/<generation_id>.jsonl`.
3. **Stop**: Flushes the generation's events as a `gen_ai.client.generation` span with child event spans. All share the session's `trace_id`. Exported immediately to avoid data loss.
4. **SessionEnd**: Emits the root `gen_ai.client.session` span covering the full session duration. Cleans up state files.

For IDEs without a `generation_id` (Copilot), the hook auto-derives generation boundaries from `UserPromptSubmit` → `Stop` cycles using an internal counter.

When upstream context is present, hook spans keep the real `trace_id` and attach to the real upstream parent span. The hook still cannot force a specific emitted `span_id` for its own spans because the Python OpenTelemetry SDK assigns span IDs when spans are started.

## IDE Detection

The hook auto-detects which IDE is calling it:

| Signal | IDE |
|--------|-----|
| Parent process tree (`ps` parent-chain walk) | Preferred detection for supported IDEs such as Cursor, Copilot / VS Code, Claude Code, and OpenCode |
| `IDE_OTEL_IDE_NAME` env var | Explicit override for generic hook runners or manual debugging |
| Self-reported `ide_name`, `client`, or `source_app` values such as `OpenAI Codex`, `Codex CLI`, `GitHub Copilot`, `GitHub Copilot CLI`, `GitHub Copilot Chat`, `Claude Code`, `Claude Code CLI`, `Anthropic Claude Code`, `Cursor IDE`, `Cursor CLI`, `Anti Gravity`, `Anti Gravity CLI`, or `OpenCode` / `OpenCode CLI` (case-insensitive, hyphen/space-insensitive) | Normalized to the canonical `gen_ai.client.name` |
| `conversation_id` or `generation_id` in input | Cursor |
| `transcript_path`, `permission_mode`, or `notification_type` | Claude Code |
| `turn_id`, `tool_response`, or `last_assistant_message` | Codex |
| `session_id` only (no Cursor-specific fields) | GitHub Copilot |

Detection order is: (1) parent process tree, (2) explicit `IDE_OTEL_IDE_NAME`, (3) self-reported payload fields, then (4) heuristics. `setup.sh` now relies on process discovery for generated Cursor, Copilot, and Claude configs, while the env var remains available as an escape hatch for generic runners and debugging.

The hook still detects the outer wrapper IDE/process, but the emitted canonical client identity now prefers a distinct inner engine when one is present (for example Gemini running under Claude Code). In that case, spans/resources use the inner engine for `gen_ai.client.name` and `gen_ai.system`, keep `gen_ai.client.agent_engine` for compatibility, and record the wrapper as `gen_ai.client.wrapper`. When the hook can infer a provider from the payload, it also sets `gen_ai.provider.name` as the canonical provider attribute (v1.37+).

## File Structure

```
.cursor/
├── hooks.json                          # Active Cursor hooks config (created by setup.sh)
└── hooks/
    └── opentelemetry-hook/
        ├── setup.sh                            # One-command setup (creates/merges hooks.json)
        ├── otel_hook.py                        # Main hook implementation (exposed as `otel-hook` when installed)
        ├── otel_config.json                    # Your config (gitignored, auto-created)
        ├── otel_config.example.json            # Config template
        ├── README.md                           # This file
        ├── examples/
        │   ├── hooks.example.json              # Full Cursor hooks template
        │   ├── cursor-hooks.example.json       # Minimal Cursor hooks template
        │   ├── copilot-hooks.example.json      # GitHub Copilot hooks template
        │   ├── claude-hooks.example.json       # Claude Code hooks template
        │   ├── opencode-plugin.example.ts      # OpenCode plugin template
        │   └── antigravity-workflow.example.md # Antigravity workflow template
        ├── .gitignore                          # Excludes secrets, venv, state
        ├── .venv/                              # Python venv (auto-provisioned)
        └── .state/                             # Runtime state
            ├── sessions/                       # Session trace context
            └── batches/                        # Generation event buffers
```

## Privacy & Security

### What Gets Sent (by default)

- Event names and timing
- Tool/command names
- File paths
- Prompt/response **length and SHA-256 hash** (not content)

### Opt-in Content Capture

Set `IDE_OTEL_CAPTURE_TEXT=true` to include prompt/response text. Combine with `IDE_OTEL_MASK_PROMPTS=true` to redact:
- Email addresses
- Long tokens / API keys
- macOS usernames from paths

### Never Sent

- API keys or credentials (automatically filtered)
- File contents (unless tool_response capture is enabled)
- Raw code

## Troubleshooting

### Check the log

```bash
# pip/pipx install
tail -f ~/.local/share/opentelemetry-hooks/otel_hook.log

# source checkout / copied-source install
tail -f ./otel_hook.log
```

### Enable debug output

```json
{
  "IDE_OTEL_LOG_LEVEL": "DEBUG",
  "IDE_OTEL_DEBUG_CONSOLE": "true",
  "IDE_OTEL_LOG_EVENTS": "true"
}
```

### Test manually

```bash
echo '{"hook_event_name":"SessionStart","session_id":"test-123"}' | otel-hook
```

### Common issues

| Problem | Fix |
|---------|-----|
| `opentelemetry-sdk not installed` | Auto-provisioning may still be in progress; wait ~30s and retry, or run `.venv/bin/pip install opentelemetry-sdk opentelemetry-exporter-otlp` |
| `Missing API key` | Set `OTEL_EXPORTER_OTLP_HEADERS` with your auth token in config |
| `cx.application.name required` | Coralogix needs this — set automatically, or add to `OTEL_RESOURCE_ATTRIBUTES` |
| Orphan spans | Enable `IDE_OTEL_BATCH_ON_STOP=true` for session-level traces |
| No traces appearing | Check endpoint, protocol, and auth headers in config. Verify the backend is running and reachable. |
| Wrong IDE detected | Check the parent process chain and input payload first; for generic runners or debugging, set `IDE_OTEL_IDE_NAME` explicitly in the hook command |
| Traces going to the wrong backend | Verify `OTEL_EXPORTER_OTLP_ENDPOINT` points to the intended backend |

## Contributing

Contributions are welcome. To get started:

```bash
git clone https://github.com/o11y-dev/opentelemetry-hooks.git
cd opentelemetry-hooks
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

Please open an issue first if you plan a large change.

## Credits

- Built on pure [OpenTelemetry Python SDK](https://opentelemetry.io/docs/languages/python/)
- Uses [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- Supports [GitHub Copilot hooks](https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-hooks), Cursor IDE / CLI hook payloads, Claude Code hook payloads, and compatible runners such as OpenCode

## License

MIT
