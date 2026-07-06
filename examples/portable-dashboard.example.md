---
description: Portable, vendor-neutral dashboard blueprint for opentelemetry-hooks telemetry.
---

Use your backend's intrinsic span fields for **span name**, **span status**, **duration**, and **trace ID**. The attribute keys below are emitted by this repo as OpenTelemetry attributes, so you can translate the same view into Grafana, Jaeger, Honeycomb, Datadog, or any other OTLP-compatible backend without changing field names.

## Suggested base filter

- `service.name = <your OTEL_SERVICE_NAME>` (commonly `ide-agent`)

## Lean dashboard panels

| Panel | Filter | Group by | Measure |
| --- | --- | --- | --- |
| Sessions by agent | `span.name = gen_ai.client.session` | `gen_ai.client.name` | count |
| Generations by model | `span.name = gen_ai.client.generation` | `gen_ai.request.model` | count |
| Hook failures | `span.name starts with gen_ai.client.hook.` and (`status.code = ERROR` or `gen_ai.client.error` exists) | `gen_ai.client.name` | error count / total hook spans |
| Tool activity | `span.name in {gen_ai.client.hook.PreToolUse, gen_ai.client.hook.PostToolUse, gen_ai.client.hook.PostToolUseFailure}` | `gen_ai.client.tool_name` | count or p95 duration |
| Shell + MCP activity | `gen_ai.client.command` exists or `gen_ai.client.mcp_server` exists | `gen_ai.client.command`, `gen_ai.client.mcp_server`, `gen_ai.client.mcp_tool` | count |
| Token usage | `gen_ai.usage.input_tokens` exists or `gen_ai.usage.output_tokens` exists | `gen_ai.client.name`, `gen_ai.request.model`, `gen_ai.provider.name` | sum input tokens, sum output tokens |

## Useful drill-downs

- **Single session trace**: filter `gen_ai.client.session.key = <session key>`
- **Prompt lifecycle**: filter `span.name in {gen_ai.client.hook.UserPromptSubmit, gen_ai.client.generation, gen_ai.client.hook.Stop}`
- **File edits**: filter `span.name = gen_ai.client.hook.AfterFileEdit`
- **Wrapper vs inner engine**: compare `gen_ai.client.name`, `gen_ai.client.wrapper`, and `gen_ai.client.agent_engine`

## Portability notes

- Prefer `service.name`, `gen_ai.client.name`, `gen_ai.provider.name`, `gen_ai.request.model`, and `gen_ai.usage.*` as reusable dimensions.
- Treat `gen_ai.client.session.key`, file paths, cwd, and tool input/output hashes as **trace drill-down fields**, not long-lived metric dimensions.
- Translate `count`, `sum`, and `p95 duration` into your backend's native visualization or query language; the field names above stay the same.
