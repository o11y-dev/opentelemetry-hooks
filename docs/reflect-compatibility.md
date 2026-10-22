# Reflect integration with semantic schema 3

Reflect consumers must adopt the [OTel semantic contract](semantic-conventions.md).
No telemetry alias mode preserves prior readers. The hook project does not
modify Reflect's source, dependency constraints or installed configuration.

The [shared capture profile](../examples/otel_config.reflect.example.json)
selects additional original hook fields and enables policy-permitted JSON logs.
The [minimal profile](../examples/otel_config.reflect.minimal.example.json)
emits canonical fields and logs with no additional recursive span fields.
Both use the same semantic contract; neither restores legacy telemetry names.

| Evidence | Consumer field |
|---|---|
| Actual operation | `gen_ai.operation.name` on invocation scopes |
| Tool and provider call ID | `gen_ai.tool.name`, `gen_ai.tool.call.id` |
| Policy-permitted object arguments/result | JSON-serialized `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result` |
| Scalar or non-object tool evidence | `o11y.client.tool_input.*`, `tool_output.*`, `tool_response.*`, or original callback log body |
| Prompt/response previews and original facts | `o11y.client.prompt.*`, `o11y.client.response.*` |
| Callback/session/generation/agent run identity | `o11y.client.*` |
| Span role and boundary evidence | `o11y.span.role`, `o11y.timing.*` |
| Native model-call telemetry | Its own source identity and optional trace links; not a duplicate hook invocation |

Update dependency constraints and setup together. Read structured `capture`,
`logs`, `batch` and `local_spans` settings. Classify invocation scopes before
callback observations, and preserve original provider IDs when matching results.
Do not count callbacks, scope spans, logs and native spans as independent calls.
Use the original permitted log body for provider fields without standard mappings.

The shared profile permits 96 total span attributes, 4,000 characters per
extracted string, depth 16, arrays up to 128 elements and 32 KiB of attributes.
It no longer maps preview aliases into runtime-owned fields. Its remaining
`process.exit.code` mappings refer to reported exit-code facts. Extraction
collisions and omissions are explicit. Full logs are governed by the capture
policy and optional log-body limit, independently from span preview bounds.

An earlier review of Reflect checkout `34e010e` (0.9.8) and 16 ingestion replays
predated schema 3. Those results do not validate the current contract. The current
hook tests replay supported harnesses through both profiles and verify SDK spans
and correlated log bodies; they do not claim a deployed Reflect upgrade.

During integration, ingest retained local span files into a fresh database with
the candidate Reflect version. Verify tool rows, object/scalar content handling,
exact payload facts, shell classification, MCP identity, subagent parentage,
conversation facts and log correlation. Reinstall and rerun hook setup in the
intended scope before live harness validation.
