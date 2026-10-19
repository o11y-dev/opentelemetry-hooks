# Telemetry reference

Spans contain canonical attributes and bounded recursive `hook.*` fields.
Optional logs carry the permitted original callback JSON. All configurations use
this one pipeline; see [configuration](configuration.md).

## Span Attributes

### Common (All Spans)

| Attribute | Description |
|-----------|-------------|
| `gen_ai.client.hook.event` | Canonical event name (PascalCase) |
| `gen_ai.client.hook.event_id` | Provider callback ID, or a UUID generated once on observation and retained on replay |
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
| `gen_ai.client.timestamp` | Original hook observation timestamp (ISO 8601), retained on replay |
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

## Schema version 2

`gen_ai.client.hook_schema_version` is the string `"2"`. `o11y.*` attributes are
project extensions, not OpenTelemetry semantic conventions. Unknown facts are
omitted; measured zero values are retained. Service and SDK attributes remain on
the shared Resource. `telemetry.distro.name/version` identify this hook package;
trace and log instrumentation scopes carry its installed package version.

| Attribute | Type / units | Meaning |
|---|---|---|
| `o11y.span.role` | string | `callback`, buffered `callback_scope`, or streaming/session `generation`, `tool`, `subagent`, `session`; distinguishes observations from operation scopes |
| `o11y.time.event_ns` | integer, Unix nanoseconds | Explicit callback `timestamp_ns`, otherwise original hook observation time |
| `o11y.time.observed_ns` | integer, Unix nanoseconds | Time this hook first received the callback |
| `o11y.timing.basis` | string | `matching_callback`, `next_callback`, `observation`, `hook_execution`, `generation_end`, `session_end`, `timeout`, or `state_limit` |
| `o11y.timing.inferred` | boolean | Boundary lacks a matching callback |
| `o11y.timing.missing_end` | boolean | An expected terminal callback was not observed |
| `o11y.timing.end_before_start` | boolean | Observed clock ordering is inverted; original endpoints are retained |
| `o11y.model.basis` | string | `provider`, `session`, `generation`, or `configuration` |
| `o11y.usage.basis` / `input_basis` / `output_basis` | string | Provider-reported accounting, or normalized inclusive input |
| `o11y.usage.semantics` | string | Explicit `per_call`, `cumulative`, `delta`, or `correction`; omitted if unspecified |
| `gen_ai.usage.total_tokens` | integer, tokens | Inclusive input plus output when both are available |
| `gen_ai.usage.reasoning.output_tokens` | integer, tokens | Reasoning subset of output; never added a second time |
| `gen_ai.tool.call.id` | string | Available invocation ID, distinct from the callback ID |
| `o11y.capture.mode` / `policy_id` | string | Effective capture mode and policy fingerprint |
| `o11y.capture.canonical_dropped_fields` | integer, fields | Canonical previews shortened or omitted before persistence |
| `o11y.capture.shortened_or_dropped_fields` | integer, fields | Canonical/extracted span fields shortened or omitted by the shared attribute budget |
| `o11y.result.completeness` | string | `partial` / `complete`, only for an explicit `is_partial` callback fact |
| `o11y.result.provider_truncated` | boolean | Explicit provider `truncated` fact, independent of local preview limits |

For conversation facts and canonical `tool_input`, `tool_output`, `tool_response`,
`mcp_input`, and `mcp_output` payloads, the `gen_ai.client.<label>` family includes:

- `length` (integer): original legacy text/serialization character count.
- `sha256` (string): original legacy fingerprint; existing queries retain its meaning.
- `bytes` (integer): UTF-8 bytes, using compact, sorted, Unicode JSON for objects/arrays.
- `hash_basis` and `serialization_version` (strings): text or legacy stringify basis and version `1`.
- Payload `fingerprint.sha256` / `fingerprint.basis`: new canonical UTF-8 fingerprint,
  using `sorted_compact_json_utf8` for objects/arrays and `utf8_text` for scalar text.
- Conversation `preview_truncated` (boolean): local text preview was shortened;
  this does not assert that the provider result was partial.

Legacy `gen_ai.client.tool.input/response.length/sha256` retain their original
pre-preview facts. Bytes are measured from parsed content, not original transport
whitespace. Excluded text can still contribute permitted size/hash facts. Missing
content does not become empty content.

Anthropic `input_tokens` is new input and is normalized by adding cache-read and
cache-write subsets. Generic `prompt_tokens` is already inclusive. Other provider
input counts stay provider-reported; unknown providers are not guessed. For 300
new + 600 cached + 100 cache-write input and 200 output including 50 reasoning,
inclusive input is 1,000 and total is 1,200. Hook observations and native spans
remain separate evidence: consumers must not sum repeated/cumulative usage across
callbacks or native telemetry without their own accounting policy. Use
`o11y.span.role` to distinguish scope spans from callback evidence; do not count
both as independent invocations.

### Relationships and boundaries

Buffered callback intervals use matching invocation callbacks when available,
then the next callback in the same session/agent branch. The final callback is an
instantaneous observation; it does not extend 1 ms beyond the generation. Agent
and invocation IDs select nested parents. Streaming callback spans represent
observations; separate generation, tool and subagent scope spans with reliable invocation IDs carry matching
start/end durations. Async scopes may outlive their enclosing generation. Missing
ends close with explicit inferred reasons; maintenance uses `timeout` and does
not fabricate callback logs. Observed endpoints are not clipped to fit parents. Unresolved streaming scope
and generation collections are capped at 128 entries each; overflow closes the
oldest with an explicit `state_limit` inference rather than losing its span ID.

Session and generation parent IDs are persisted before child export and reused
when those scope spans are emitted. Valid upstream context parents the session;
related native spans use links. Once an export identity is committed, a late
upstream trace cannot rewrite it and instead contributes a relationship link.

### Logs and delivery

One optional callback log uses the callback span's native OTLP trace/span IDs.
It includes callback and available invocation/session/turn/agent IDs, schema,
harness provenance and policy. Ordinary records use INFO and UNSET span status;
failure observations use ERROR for both. Intentional interruption is not an
error by itself. Runtime diagnostics use a separate logger and do not receive
callback bodies through logger propagation.

The Python log bridge preserves original event and observation nanoseconds;
export happens later and is not substituted for either time. Buffered retries
reuse callback, generation and session span identities and original event times.
Each flush performs one application-level attempt; SDK retries and the callback
deadline remain bounded. After `batch.max_export_attempts` failures (default 3),
records move to policy-local `*.quarantine.json` files with their identities and
failure reason. Quarantine is retained for operator investigation, not silently
dropped or automatically replayed under another policy.

Trace and log signals deliver independently. A successful trace export can clear
a queue even if logs fail. A failed trace export can replay logs; delivery is
at least once, not transactional. Per-record collector acknowledgements are not
available through the SDK exporter result: a collector's acknowledged partial
rejection must be monitored at the collector. Deduplicate replay by native span
ID and callback ID, never by content hash alone.
