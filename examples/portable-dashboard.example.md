---
description: Portable, vendor-neutral dashboard blueprint for opentelemetry-hooks telemetry.
---

Use your backend's intrinsic span fields for **span name**, **span status**, **duration**, and **trace ID**. The attribute keys below are emitted by this repo as OpenTelemetry attributes, so you can translate the same view into Grafana, Jaeger, Honeycomb, Datadog, or any other OTLP-compatible backend without changing field names.

## Suggested base filter

- `service.name = <your OTEL_SERVICE_NAME>` (commonly `ide-agent`)

## Lean dashboard panels

| Panel | Filter | Group by | Measure |
| --- | --- | --- | --- |
| Sessions by agent | `span.name = o11y.client.session` | `o11y.client.name` | count |
| Generations by model | `o11y.span.role = generation` | `gen_ai.request.model` | count |
| Hook failures | `span.name starts with o11y.client.hook.` and (`status.code = ERROR` or `o11y.client.error` exists) | `o11y.client.name` | error count / total hook spans |
| Tool activity | `o11y.span.role = tool` and `gen_ai.operation.name = execute_tool` | `gen_ai.tool.name` | invocation count; p95 duration for matching callback boundaries |
| Shell + MCP activity | `o11y.client.command` exists or `o11y.client.mcp_server` exists | `o11y.client.command`, `o11y.client.mcp_server`, `o11y.client.mcp_tool` | count |
| Token usage facts | `gen_ai.usage.input_tokens` exists or `gen_ai.usage.output_tokens` exists | `o11y.client.name`, `gen_ai.request.model`, `gen_ai.provider.name` | reported input/output counts per observation |

## Useful drill-downs

- **Single session trace**: filter `o11y.client.session_id = <session key>`
- **Prompt lifecycle**: select `o11y.span.role = generation` and prompt/stop callback observations in that generation
- **File edits**: filter `span.name = o11y.client.hook.AfterFileEdit`
- **Wrapper vs inner engine**: compare `o11y.client.name`, `o11y.client.wrapper`, and `o11y.client.agent_engine`

## Portability notes

- Prefer `service.name`, `o11y.client.name`, `gen_ai.provider.name`, `gen_ai.request.model`, and `gen_ai.usage.*` as reusable dimensions.
- Treat `o11y.client.session_id`, file paths, cwd, and tool input/output hashes as **trace drill-down fields**, not long-lived metric dimensions.
- Sum token counts only after selecting one authoritative source and an explicit per-call or delta accounting policy. Do not add callback/scope/native copies or cumulative observations together.
- Translate `count`, `sum`, and `p95 duration` into your backend's native visualization or query language; the field names above stay the same.
