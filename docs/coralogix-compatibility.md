# Coralogix comparison research

Audit date: 2026-10-22. This records public source behavior, not customer tenant
payloads. Our implementation follows the [OTel semantic contract](semantic-conventions.md).
Coralogix fields, span construction and dashboard behavior are research evidence.

The source reviewed is
[`cursor/extension/resources/hook.py` at `f00ad6f`](https://github.com/coralogix/ai-agent-instrumentation/blob/f00ad6f357a11f238180fd4f0a6fbb78cfb0e722/cursor/extension/resources/hook.py).

| Coralogix source behavior | Our semantic contract |
|---|---|
| `gen_ai.system=cursor` | Harness identity is `o11y.client.name`; the model provider is `gen_ai.provider.name` |
| `cursor.conversation_id`, `cursor.generation_id` | `gen_ai.conversation.id` and project generation metadata |
| `gen_ai.tool.name`, `cursor.tool_use_id` | Standard tool name and provider tool-call ID |
| `cursor.tool_input`, `cursor.tool_output`, `cursor.result_json` | Policy-permitted content; standard arguments/results follow the OTel object schemas |
| `context_tokens` copied into `gen_ai.usage.input_tokens` during compaction | Context occupancy stays in `o11y.client.context_tokens`, separate from consumption |
| `cursor.<hook_name>` spans with `SERVER` kind around export-time handling | Local observations and reconstructed `INTERNAL` invocations with observed timing bases |

There are no `cursor.cache*` or `cursor.chache*` attributes in this hook at the
reviewed commit. Coralogix's
[AI cost documentation](https://coralogix.com/docs/user-guides/ai/cost/#send-cache-and-cost-data-to-coralogix)
still documents `gen_ai.usage.cache_creation.input_tokens`. Our output uses only
the current OTel name, `gen_ai.usage.cache_write.input_tokens`.

The separate [Cursor Usage & Cost integration](https://coralogix.com/docs/integrations/ai-observability/cursor/)
collects Admin API data and derives metrics such as `cursor_event_tokens_total`.
Those API metrics are a different source from the local hook. Neither their
field names nor dashboard recognition are implementation requirements.
