# Telemetry reference

Spans contain canonical attributes and bounded recursive `hook.*` fields.
Optional logs carry the permitted original callback JSON. All configurations use
this one pipeline; see [configuration](configuration.md).

## Span Attributes

### Common (All Spans)

| Attribute | Description |
|-----------|-------------|
| `gen_ai.client.hook.event` | Canonical event name (PascalCase) |
| `gen_ai.client.hook.event_id` | Provider event ID or deterministic hook callback identity |
| `gen_ai.client.telemetry_source` / `gen_ai.client.hook_schema_version` | Explicit hook provenance and contract version |
| `gen_ai.client.name` | Canonical harness/engine identity (`codex`, `cursor`, `copilot`, `claude`, `opencode`, etc.) |
| `gen_ai.client.agent_engine` | Inner agent engine when it differs from the outer harness (for example Cursor running Claude Code) |
| `gen_ai.client.session_id` | Session identifier |
| `gen_ai.client.generation_id` | Generation identifier (Cursor) |
| `gen_ai.client.workspace` | Workspace / working directory |
| `gen_ai.client.cwd` / `gen_ai.client.repository_root` | Event working directory and resolved repository root |
| `vcs.repository.name` / `vcs.ref.head.name` | Repository and Git branch identity |
| `gen_ai.client.repository.remote.sha256` | SHA-256 of the credential-free normalized Git remote |
| `gen_ai.client.native_trace_id` / `gen_ai.client.native_span_id` | Valid explicit native source identifiers; generic trace fields are reserved for upstream parenting |
| `gen_ai.client.timestamp` | Event timestamp (ISO 8601) |
| `gen_ai.system` | Deprecated legacy GenAI system/provider attribute retained for backward compatibility |
| `gen_ai.operation.name` | `chat`, `execute_tool`, or `invoke_agent` |
| `telemetry.distro.name` / `telemetry.distro.version` | Hook package provenance on the OTel resource and in the local JSON span's `resource` object. The version is resolved from installed package metadata rather than duplicated in hook logic; neither field overwrites agent `service.name` or `service.version` |

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
| `PreToolUse` / `PostToolUse` | `gen_ai.client.tool_name`, `gen_ai.client.tool_id`, `gen_ai.client.tool_use_id`, `gen_ai.client.duration_ms`, and explicit MCP identity when encoded |
| `PostToolUseFailure` | `gen_ai.client.tool_name`, `gen_ai.client.tool_use_id`, `gen_ai.client.status=error`, privacy-safe error length/hash, and explicit MCP identity when encoded |
| `BeforeShellExecution` / `AfterShellExecution` | `gen_ai.client.command`, `gen_ai.client.cwd`, `gen_ai.client.exit_code` |
| `BeforeMCPExecution` / `AfterMCPExecution` | `gen_ai.client.mcp_server`, `gen_ai.client.mcp_tool` |
| `BeforeReadFile` / `AfterFileEdit` | `gen_ai.client.file_path`, `gen_ai.client.edits` |
| `SubagentStart` / `SubagentStop` | `gen_ai.client.subagent_type`, `gen_ai.client.agent_id`, `gen_ai.client.parent_agent_id`, ID source, delegation length/hash, and status |
| `Stop` | `gen_ai.client.status`, `gen_ai.client.loop_count` |
| `ErrorOccurred` | `error.type`, `error.code`, `gen_ai.client.error.length`, `gen_ai.client.error.sha256`, and `gen_ai.client.is_interrupt` |

Real error and failure callbacks set the OpenTelemetry span status to `ERROR` without placing raw error content in the status description. Intentional callbacks marked `is_interrupt=true` retain `UNSET` status and use `gen_ai.client.status=interrupted`.

## Hook logs

`logs.enabled` defaults to false. When enabled, each emitted callback has one log
correlated to its span through OTLP trace/span IDs and explicit correlation
attributes. Its body contains the policy-permitted original JSON with original
key spelling and types. Full mode without exclusions sends the full parsed JSON.

Shell, MCP, tool and conversation callbacks use this same record format.
Canonical span attributes retain their semantics; recursively extracted fields
cover additional permitted callback data. There are no separate per-tool or
per-conversation log emitters. Consumer queries should use the log body for
provider fields and `gen_ai.client.hook.event` for the event category.

Logs also carry available hook event, session, conversation, generation and turn
IDs. Retry can duplicate logs; use `gen_ai.client.hook.event_id` to deduplicate.

### Endpoint Derivation

The logs endpoint is derived automatically:

1. If `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` is set, it's used directly
2. Otherwise, `/v1/traces` is replaced with `/v1/logs` in `OTEL_EXPORTER_OTLP_ENDPOINT`
3. For gRPC, the same endpoint serves all signals

Example: `https://ingress.us1.coralogix.com:443/v1/traces` → `https://ingress.us1.coralogix.com:443/v1/logs`
