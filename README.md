# OpenTelemetry Hook for AI Coding Agents

> Observability for your AI pair-programmer — know what your agent is doing, one trace at a time.

An open-source OpenTelemetry integration that captures all AI coding agent activity as structured **traces and logs** and exports them to any OTLP-compliant backend. Works with **Cursor IDE** and **GitHub Copilot** using [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/).

Every hook event — prompt submissions, tool calls, shell commands, MCP interactions, file edits, subagent orchestration — becomes an OpenTelemetry span you can query, alert on, and visualize in Jaeger, Grafana, Datadog, Honeycomb, Coralogix, or any OTLP-compatible backend.

> **Note**: Claude Code has [native OpenTelemetry support](https://docs.claude.com/en/docs/claude-code/monitoring-usage) built-in and does not need this hook.

## How It Works

The hook is a lightweight Python script that your IDE invokes on every agent event. The IDE pipes a JSON payload to stdin, the hook processes it, emits OpenTelemetry spans and logs, and returns `{"continue": true}` on stdout so the IDE proceeds normally. No sidecar, no daemon — just a script your IDE calls.

```
IDE Event → stdin (JSON) → otel_hook.py → OpenTelemetry SDK → OTLP Backend
                                 ↓
                          stdout: {"continue": true}
```

## Features

- **Dual-IDE Support**: One script, two IDEs — auto-detects Cursor or GitHub Copilot from hook input fields.

- **Session-level Traces**: Groups all events within a session into a single trace with a 3-tier hierarchy:

```
ide.session (root)
├── ide.generation (gen-1)
│   ├── ide.hook.UserPromptSubmit
│   ├── ide.hook.PreToolUse
│   ├── ide.hook.PostToolUse
│   └── ide.hook.Stop
├── ide.generation (gen-2)
│   ├── ide.hook.UserPromptSubmit
│   ├── ide.hook.PreToolUse
│   ├── ide.hook.PostToolUse
│   └── ide.hook.Stop
└── ide.hook.SessionEnd
```

- **GenAI Semantic Conventions**: Emits standard OpenTelemetry GenAI attributes (`gen_ai.system`, `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.*`, etc.)

- **All Hook Events**: Captures the full lifecycle — sessions, prompts, tool usage, shell commands, MCP calls, file operations, subagents, errors, and more.

- **Structured OTel Logs**: Emits trace-correlated log records for MCP calls, shell executions, and tool usage — with full I/O payloads, server output, and duration. Logs are exported via OTLP alongside spans.

- **Zero Setup**: Auto-provisions a Python virtual environment on first run. No manual install needed.

- **Privacy Controls**: Built-in masking of emails, tokens, and usernames. Text capture is opt-in.

- **JSON Config File**: All settings in `otel_config.json` — no environment variable exports needed.

## Supported Events

| Canonical Name | Cursor | Copilot |
|---|---|---|
| `SessionStart` | `sessionStart` | `sessionStart` |
| `SessionEnd` | `sessionEnd` | `sessionEnd` |
| `UserPromptSubmit` | `beforeSubmitPrompt` | `userPromptSubmitted` |
| `PreToolUse` | `preToolUse` | `preToolUse` |
| `PostToolUse` | `postToolUse` | `postToolUse` |
| `PostToolUseFailure` | `postToolUseFailure` | — |
| `Stop` | `stop` | — |
| `SubagentStart` | `subagentStart` | — |
| `SubagentStop` | `subagentStop` | — |
| `ErrorOccurred` | — | `errorOccurred` |
| `BeforeShellExecution` | `beforeShellExecution` | — |
| `AfterShellExecution` | `afterShellExecution` | — |
| `BeforeMCPExecution` | `beforeMCPExecution` | — |
| `AfterMCPExecution` | `afterMCPExecution` | — |
| `BeforeReadFile` | `beforeReadFile` | — |
| `AfterFileEdit` | `afterFileEdit` | — |

## Installation

### Download from GitHub Releases

Each tagged version (`v*`) produces a GitHub Release with pre-built packages:

1. Go to [Releases](https://github.com/o11y-dev/opentelemetry-hooks/releases)
2. Download the `.whl` or `.tar.gz` from the latest release
3. Install with pip:

```bash
pip install opentelemetry_hooks-*.whl
```

### Install directly from GitHub

```bash
pip install git+https://github.com/o11y-dev/opentelemetry-hooks.git@v0.1.0
```

Or install the latest from `main`:

```bash
pip install git+https://github.com/o11y-dev/opentelemetry-hooks.git
```

### Versioning

This project uses [semantic versioning](https://semver.org/). Versions are derived automatically from git tags using `setuptools-scm`.

To create a new release, go to **Actions → Release → Run workflow**, enter a version number (e.g. `0.2.0`), and click **Run workflow**. The workflow will run tests, create the git tag, build the package, and publish a GitHub Release with the artifacts.

Alternatively, push a tag manually:

```bash
git tag v0.2.0
git push origin v0.2.0
```

The CI pipeline will then build the package and create a GitHub Release automatically.

## Quick Start

### One-Command Setup (Cursor IDE)

```bash
bash .cursor/hooks/opentelemetry-hook/setup.sh
```

That's it. The script will:

1. Create or **merge into** your existing `.cursor/hooks.json` (safe to re-run)
2. Create `otel_config.json` from the example template (if missing)
3. Bootstrap the Python venv in the background (~30s on first run)

Then edit your endpoint config and restart Cursor:

```bash
# Edit the OTLP endpoint + auth
vim .cursor/hooks/opentelemetry-hook/otel_config.json
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

- Python 3.8+ (the setup script checks for this)
- An OTLP-compatible backend (Jaeger, Coralogix, Datadog, Grafana, Honeycomb, etc.)

### Other IDEs

#### GitHub Copilot

```bash
mkdir -p .github/hooks
cp .cursor/hooks/opentelemetry-hook/examples/copilot-hooks.example.json .github/hooks/otel-hooks.json
```

Replace `{{SCRIPT_PATH}}` with the path to the hook script (e.g. `python3 .cursor/hooks/opentelemetry-hook/otel_hook.py`).
See [GitHub Copilot hooks docs](https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-hooks).

#### GitHub Copilot — Recommended Repositories

To make this hook automatically available to the GitHub Copilot coding agent across your organization's repositories, add it as a [recommended repository](https://docs.github.com/en/copilot/customizing-copilot/adding-repository-instructions-for-github-copilot):

1. Go to your organization settings → **Copilot** → **Coding agent** → **Recommended repositories**
2. Add `o11y-dev/opentelemetry-hooks` to the list
3. The Copilot coding agent will now be able to reference this repo for hook setup and configuration

### Configuration

Edit `.cursor/hooks/opentelemetry-hook/otel_config.json`:

```json
{
  "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
  "OTEL_SERVICE_NAME": "ide-agent",
  "IDE_OTEL_BATCH_ON_STOP": "true"
}
```

Then restart your IDE.

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
| `IDE_OTEL_LOG_FILE` | Log file path | `.cursor/hooks/opentelemetry-hook/otel_hook.log` |
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
  "OTEL_EXPORTER_OTLP_ENDPOINT": null,
  "OTEL_SERVICE_NAME": "ide-agent",
  "IDE_OTEL_BATCH_ON_STOP": "true",
  "IDE_OTEL_LOCAL_SPANS": "true"
}
```

Spans are written to `.state/local_spans/<session>.jsonl`. Each line is a JSON object with trace/span IDs, attributes, and timing — see [Local Trace Files](#local-trace-files-agent-friendly) for the format.

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
| `ide.hook.event` | Canonical event name (PascalCase) |
| `ide.name` | Detected IDE (`cursor`, `copilot`) |
| `ide.session_id` | Session identifier |
| `ide.generation_id` | Generation identifier (Cursor) |
| `ide.workspace` | Workspace / working directory |
| `ide.timestamp` | Event timestamp (ISO 8601) |
| `gen_ai.system` | IDE name (resource attribute) |
| `gen_ai.operation.name` | `chat`, `execute_tool`, or `invoke_agent` |

### GenAI (When Available)

| Attribute | Description |
|-----------|-------------|
| `gen_ai.request.model` | Requested model name |
| `gen_ai.response.model` | Response model name |
| `gen_ai.conversation.id` | Session / conversation ID |
| `gen_ai.usage.input_tokens` | Input token count |
| `gen_ai.usage.output_tokens` | Output token count |
| `gen_ai.request.temperature` | Temperature setting |
| `gen_ai.request.max_tokens` | Max tokens setting |
| `gen_ai.response.finish_reasons` | Finish reasons array |
| `gen_ai.input.messages` | Input messages (opt-in) |
| `gen_ai.output.messages` | Output messages (opt-in) |

### Event-Specific

| Event | Key Attributes |
|-------|---------------|
| `UserPromptSubmit` | `ide.composer_mode`, `gen_ai.request.model` |
| `PreToolUse` / `PostToolUse` | `ide.tool_name`, `ide.tool_id`, `ide.duration_ms` |
| `PostToolUseFailure` | `ide.tool_name`, `ide.error` |
| `BeforeShellExecution` / `AfterShellExecution` | `ide.command`, `ide.cwd`, `ide.exit_code` |
| `BeforeMCPExecution` / `AfterMCPExecution` | `ide.mcp_server`, `ide.mcp_tool` |
| `BeforeReadFile` / `AfterFileEdit` | `ide.file_path`, `ide.edits` |
| `SubagentStart` / `SubagentStop` | `ide.subagent_type`, `ide.agent_id` |
| `Stop` | `ide.status`, `ide.loop_count` |
| `ErrorOccurred` | `ide.error`, `ide.is_interrupt` |

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
| `ide.mcp_server` | MCP server name |
| `ide.mcp_tool` | MCP tool name |
| `ide.mcp.input` | Full input payload (opt-in) |
| `ide.mcp.input.length` | Input payload size |
| `ide.mcp.input.sha256` | Input payload hash |
| `ide.mcp.output` | Full output payload (opt-in) |
| `ide.mcp.output.length` | Output payload size |
| `ide.mcp.output.sha256` | Output payload hash |
| `ide.mcp.duration_ms` | MCP call duration |
| `ide.mcp.stdout` | Server stdout (if available) |
| `ide.mcp.stderr` | Server stderr (if available) |

### Endpoint Derivation

The logs endpoint is derived automatically:

1. If `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` is set, it's used directly
2. Otherwise, `/v1/traces` is replaced with `/v1/logs` in `OTEL_EXPORTER_OTLP_ENDPOINT`
3. For gRPC, the same endpoint serves all signals

Example: `https://ingress.us1.coralogix.com:443/v1/traces` → `https://ingress.us1.coralogix.com:443/v1/logs`

## Session-level Batching

When `IDE_OTEL_BATCH_ON_STOP=true` (recommended):

1. **SessionStart**: Pre-generates a `trace_id` shared by all spans in the session. Stored in `.state/sessions/`.
2. **Generation events**: Buffered to `.state/batches/<generation_id>.jsonl`.
3. **Stop**: Flushes the generation's events as an `ide.generation` span with child event spans. All share the session's `trace_id`. Exported immediately to avoid data loss.
4. **SessionEnd**: Emits the root `ide.session` span covering the full session duration. Cleans up state files.

For IDEs without a `generation_id` (Copilot), the hook auto-derives generation boundaries from `UserPromptSubmit` → `Stop` cycles using an internal counter.

## IDE Detection

The hook auto-detects which IDE is calling it:

| Signal | IDE |
|--------|-----|
| `conversation_id` or `generation_id` in input | Cursor |
| `session_id` only (no Cursor-specific fields) | GitHub Copilot |

The detected IDE is stored as the `gen_ai.system` resource attribute and `ide.name` span attribute.

> **Note**: Claude Code has [native OpenTelemetry support](https://docs.claude.com/en/docs/claude-code/monitoring-usage) — use that instead of this hook.

## File Structure

```
.cursor/
├── hooks.json                          # Active Cursor hooks config (created by setup.sh)
└── hooks/
    └── opentelemetry-hook/
        ├── setup.sh                            # One-command setup (creates/merges hooks.json)
        ├── otel_hook.py                        # Main hook script (self-bootstraps venv)
        ├── otel_config.json                    # Your config (gitignored, auto-created)
        ├── otel_config.example.json            # Config template
        ├── README.md                           # This file
        ├── examples/
        │   ├── hooks.example.json              # Full Cursor hooks template
        │   ├── cursor-hooks.example.json       # Minimal Cursor hooks template
        │   └── copilot-hooks.example.json      # GitHub Copilot hooks template
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
tail -f .cursor/hooks/opentelemetry-hook/otel_hook.log
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
echo '{"hook_event_name":"SessionStart","session_id":"test-123"}' | python3 .cursor/hooks/opentelemetry-hook/otel_hook.py
```

### Common issues

| Problem | Fix |
|---------|-----|
| `opentelemetry-sdk not installed` | Auto-provisioning may still be in progress; wait ~30s and retry, or run `.venv/bin/pip install opentelemetry-sdk opentelemetry-exporter-otlp` |
| `Missing API key` | Set `OTEL_EXPORTER_OTLP_HEADERS` with your auth token in config |
| `cx.application.name required` | Coralogix needs this — set automatically, or add to `OTEL_RESOURCE_ATTRIBUTES` |
| Orphan spans | Enable `IDE_OTEL_BATCH_ON_STOP=true` for session-level traces |
| No traces appearing | Check endpoint, protocol, and auth headers in config. Verify the backend is running and reachable. |
| Wrong IDE detected | Check that your IDE provides the expected input fields |
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
- Supports [GitHub Copilot hooks](https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-hooks)

## License

MIT
