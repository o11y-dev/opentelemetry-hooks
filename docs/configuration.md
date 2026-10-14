# Configuration

`otel_config.json` uses structured, unprefixed hook settings and standard
`OTEL_*` exporter settings. No additional environment-variable prefix is used.
The complete starting configuration is [otel_config.example.json](../otel_config.example.json).

## Precedence

For ordinary runtime settings: environment > managed configuration > local JSON
> runtime defaults. Within each JSON object, structured keys override equivalent
legacy flat keys. Nested managed objects merge into local objects.

Managed structured `capture` and `logs` settings are the final authority, including
when legacy environment flags conflict. `IDE_OTEL_SKIP_MDM` skips legacy managed
settings only; it cannot skip these structured restrictions.

Every configuration uses the same capture and logging path. Omitting
`capture.mode` selects `metadata_only`; logs default off. The
`IDE_OTEL_ENABLE_LOGS=true` alias enables policy-filtered hook logs, not full
content capture.

## Capture policy

```json
{
  "capture": {
    "mode": "selected_content",
    "include": ["cwd", "file_path", "tool_input.command"],
    "exclude": ["tool_input.credentials"],
    "mappings": {"tool_input.command": "command.observed"},
    "max_attributes": 64,
    "max_string_length": 4000,
    "max_depth": 16,
    "max_array_length": 128,
    "max_attribute_bytes": 32768
  },
  "logs": {"enabled": true}
}
```

- `metadata_only`: recognizes specific metadata fields; unknown fields and content
  are omitted. It does not treat arbitrary numeric fields as safe. Field names
  identify categories, not a guarantee that a provider never places sensitive text
  in those categories.
- `selected_content`: includes that metadata plus the selected paths.
- `full`: retains all callback fields, subject to explicit exclusions.

Paths use normalized lowercase snake case: `toolInput.Command` becomes
`tool_input.command`. `include` and `exclude` accept shell-style patterns, and a
selected parent path covers its descendants. Exclusions win. The filter preserves
original key spelling and JSON value types in the log body.

### Span-only allowlist

`capture.span_include` selects additional recursively extracted span fields from
the already permitted payload. It uses the same normalized source paths and
patterns as `include`, before attribute mappings and extraction limits:

```json
{
  "capture": {
    "mode": "full",
    "span_include": [
      "cwd", "file_path", "tool_input.command", "tool_input.file_path",
      "tool_input.path", "tool_input.description", "tool_response.exit_code",
      "metadata.model", "metadata.request_model", "metadata.response_model",
      "metadata.temperature", "metadata.max_tokens"
    ],
    "max_attributes": 64
  },
  "logs": {"enabled": true}
}
```

The selection above follows workspace, tool and model field groups handled by
the legacy hook, using the current single pipeline. Canonical IDs, model/usage, status,
duration and permitted conversation attributes remain available independently
of this allowlist. Full original JSON logs are unchanged by `span_include`.

- Omit `span_include` to extract all fields permitted by the capture policy.
- Use `[]` for canonical span attributes only, without recursive extras.
- Use a parent path or wildcard to include its descendants. Arrays support
  numeric paths, such as `tool_calls.*.id` or `flags.0`.
- Nonmatching fields do not consume extraction limits or increment
  `hook.capture.dropped_fields`. Limits still apply to the selected fields.

This is a span-size control, not a privacy boundary. `capture.mode`, `include`
and `exclude` still govern content for both signals; an allowlist cannot restore
content removed by that policy. Managed `span_include` replaces the local list,
and changing it isolates pending state like any other capture policy change.

The default and gateway examples use metadata-only capture with logs disabled.
The [Reflect example](../examples/otel_config.reflect.example.json) enables full
capture and full JSON logs with `span_include: []`: canonical span attributes
only. Canonical command, tool file/path, model and ID attributes do not need
extraction mappings. The example requires the local Reflect gateway and updated
hook package; it does not update Reflect's setup or semantic normalization.

### Agent capture examples

Start with the shared Reflect example, then optionally replace its entire
`capture` object with one of these small snippets. Keep its exporter, batch,
local span and logging settings. These are copyable configuration sections,
not automatically loaded profiles or a configuration inheritance mechanism.

| Agent | Capture example | Additional source evidence |
|---|---|---|
| Cursor | [cursor](../examples/capture/cursor.example.json) | Tool-response exit code |
| Claude Code | [claude](../examples/capture/claude.example.json) | Tool-response exit code and batch tool-call IDs |
| Codex | [codex](../examples/capture/codex.example.json) | Tool-response exit code |
| Gemini CLI | [gemini](../examples/capture/gemini.example.json) | Tool-response exit code and original `llm_request.model` |
| Windsurf | [windsurf](../examples/capture/windsurf.example.json) | Native `tool_info.mcp_result.exit_code`, mapped to `hook.tool_response.exit_code` |
| Copilot | [copilot](../examples/capture/copilot.example.json) | Canonical fields only; JSON-encoded `toolArgs` stays in the log body |
| OpenCode | [opencode](../examples/capture/opencode.example.json) | Top-level failure exit code emitted by the plugin |
| Antigravity | [antigravity](../examples/capture/antigravity.example.json) | Tool-response exit code when supplied by a manual compatibility runner |

These are starting selections based on the former tool/model field groups,
not an exhaustive inventory or a promise that every callback supplies them.
Cursor, Codex and the compatibility runner share the same tool-response shape,
so their snippets deliberately match. Gemini's original model field is optional
source evidence alongside the canonical model. Copilot arguments are parsed for
canonical command and file/path attributes without selecting the encoded string
as a span attribute. Antigravity still requires a manual runner.

All snippets keep full JSON logs when used with the Reflect example. Edit
`span_include`, limits and mappings for additional fields found in your logs;
use `exclude` when a field must be removed from both signals. For multiple agents
sharing one config, combine the needed paths and mappings, or use
`IDE_OTEL_CONFIG` in each hook command to select separate complete config files.
The snippet files alone do not configure an exporter or enable logs.

Separate-process SDK replays check both the shared baseline and each agent's
capture snippet against representative callbacks, preserving complete correlated
logs. They do not verify a live agent installation or Reflect ingestion.

### Attribute names and limits

Span extraction uses a `hook.` prefix by default, such as `hook.model_params.0.id`.
Homogeneous scalar arrays remain arrays. Mixed/nested arrays use numeric path
components. Null leaves are omitted because span attributes cannot represent
nulls. Names that collide after normalization are omitted together and reported
by `hook.capture.collisions`. Truncation/omission is reported through
`hook.capture.dropped_fields`; these diagnostic fields are outside the extraction
count budget. Keys are traversed deterministically.

`mappings` maps normalized source paths to complete lowercase attribute names.
It can rename fields or explicitly override an existing canonical attribute.
Mappings do not perform arithmetic, infer missing data or change log bodies.
Generated fields are attached before canonical attributes so SDK attribute-count
limits prioritize canonical correlation fields. Explicit mappings are applied
last. SDK and backend limits may still remove attributes.

Recursive extraction handles metadata and tool input/response objects once. Map fields
explicitly when a consumer requires an old flattened attribute name. Stable
canonical identities, model/usage fields and conversation facts remain.

Extraction limits apply to recursively generated attributes; canonical attributes
retain their existing field-specific limits. These are not a maximum size for an
entire exported span. The hook never attaches the entire callback JSON as a span
attribute.

Filtering occurs before local lifecycle state and batch writes. Pure provider
parsers also inspect the original input transiently to preserve event identity
and conversation length/hash facts; omitted conversation content is never stored
in those facts. Explicit content exclusions still permit these length/hash facts.
The unfiltered input is not persisted by the capture path. Incoming
private top-level fields cannot impersonate the hook's internal state.

The capture-policy fingerprint separates queues and session state. A policy change
can split a running session; old queues are not replayed under the new policy.
Old state and already exported telemetry remain on disk/in the backend. Local
exported spans keep the existing `.state/local_spans` directory for consumers.

## Hook logs and diagnostics

`logs.enabled` controls one OTLP log record per
emitted callback. Its body is the policy-filtered original payload. Full mode
with no exclusions sends the full parsed JSON, including unknown fields and
nested objects; whitespace and object serialization order are not a wire-format
contract. It uses the active span's trace/span IDs. Protocol stdout remains
reserved for the harness response.

This replaces legacy tool/conversation log fan-out. Canonical span attributes
still carry tool, MCP, model and permitted conversation evidence. Log attributes
carry hook identity and session/generation/turn correlation; query the body for
provider fields. Record counts and old log-attribute queries require migration. Correlated Cursor
MCP callbacks are retained in batches with
`gen_ai.client.mcp.correlated_evidence=true`; they are callback evidence, not
additional tool invocations. A terminal callback can establish an incomplete
session/generation when its start callback was unavailable.

Full raw bodies intentionally are not truncated by span extraction limits. Large
bodies may be rejected by a destination; use exclusions or a collector policy for
that destination. The original JSON log body is buffered only when logs are enabled.
Policy-permitted parsed fields and extracted attributes are buffered for spans.
Logs are best effort. A retained trace batch can replay its log on retry, so use
`gen_ai.client.hook.event_id` for deduplication.

Operational diagnostics use `diagnostics.level`, `diagnostics.file`,
`diagnostics.events` and `diagnostics.console`. They are separate from hook payload
logs. Full capture is not a masking feature: `capture.mask` only masks canonical
text fields, not the original JSON log or recursive extracted fields.
Use explicit exclusions or metadata/selected capture to control those fields.

## Runtime settings

| JSON setting | Environment alias | Purpose |
|---|---|---|
| `batch.on_stop` | `IDE_OTEL_BATCH_ON_STOP` | Buffer callbacks until generation end |
| `batch.disable_sdk_batch` | `IDE_OTEL_DISABLE_BATCH` | Use synchronous SDK export where hook deadlines permit |
| `local_spans` | `IDE_OTEL_LOCAL_SPANS` | Save exported spans for local consumers |
| `logs.enabled` | `IDE_OTEL_ENABLE_LOGS` | Enable the OTLP log signal |
| `capture.user_identity` | `IDE_OTEL_CAPTURE_USER_IDENTITY` | Canonical user identity attributes |
| `capture.mask` | `IDE_OTEL_MASK_PROMPTS` | Canonical text masking |
| `diagnostics.level` | `IDE_OTEL_LOG_LEVEL` | Diagnostic severity |
| `diagnostics.file` | `IDE_OTEL_LOG_FILE` | Diagnostic file destination |
| `state.ttl_seconds` | `IDE_OTEL_STATE_TTL_SECONDS` | Expire old active-policy state |
| `state.cleanup_interval_seconds` | `IDE_OTEL_STATE_CLEANUP_INTERVAL_SECONDS` | Throttle cleanup |
| `state.lock_timeout_seconds` | `IDE_OTEL_STATE_LOCK_TIMEOUT_SECONDS` | Local lock timeout |

`service_name`, `app_name` and `subsystem_name` are available as unprefixed
settings. Prefer standard `OTEL_SERVICE_NAME` for service identity.

`capture.mode`, `include` and `exclude` determine permitted content for both
spans and logs. There is no separate content-capture runtime. Old content-toggle
flags do not override this policy. The old conversation/payload/all-events log
switches are retired: `logs.enabled` is the single log switch.

Consumers that relied on separate log records or old flattened names must update
their configuration and queries. Use `capture.mappings` for attribute names and
normalize consumer-specific semantics in the consumer. No Reflect-specific
compatibility emitter is included.

## Managed policy

macOS reads `/Library/Managed Preferences/dev.o11y.opentelemetry-hook.plist`,
then the corresponding user managed-preferences file if the machine file is
absent. Distribute nested `capture` and `logs` dictionaries, for example:

```json
{"capture": {"mode": "metadata_only"}, "logs": {"enabled": false}}
```

Windows reads `SOFTWARE\Policies\OpenTelemetryHook` from HKCU then HKLM, so machine
values win. Use JSON object strings for registry values named `capture` and
`logs`. For standard/legacy scalar settings, use ordinary string values.

A managed mode fixes the mode for that device. Administrators should specify the
complete permitted include/exclude policy rather than relying on user defaults.
Full mode can send secrets present in callbacks. Restrict access to credentials
and raw payloads through your normal workstation and backend controls.

## Consumer semantics and source coverage

The hook records supplied evidence. Retrospective turn analyses,
costs, model-majority guesses, idle gaps and risk flags belong to consumers, not
new callback events. Supplied cache-read/write counters, model parameters, stop
status and duration remain queryable through extracted fields without inventing
missing values. Existing canonical token-counter semantics are not changed here.

Reflect can maintain its own configuration and normalize fields for its own
semantics. The hook does not add a Reflect-specific compatibility path. Neither
hook/native double counting nor provider-specific inclusive/uncached input-token
semantics can be solved by renaming a field alone.

Cursor subagent IDs normalize to canonical agent IDs; parent-conversation and
triggering-tool IDs remain distinct relationship attributes. Claude elicitation
IDs and failure duration are retained. OpenCode plugin invocation IDs normalize
to `tool_use_id`. Codex retains its tool/turn/session IDs and short callback export
deadline. See [provider contracts](hook-coverage.md) for first-party sources.
