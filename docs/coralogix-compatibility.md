# Coralogix and OpenTelemetry attribute compatibility

Audit date: 2026-10-22. This compares public hook source and documentation; it
does not inspect a customer's Coralogix tenant or an actual production payload.

OpenTelemetry semantic conventions are the implementation target. Coralogix is
comparison research, not a compatibility requirement. Vendor-specific attribute
aliases, span construction and dashboard behavior are outside the implementation
plan. Existing aliases are retained for consumers of our previous telemetry.

## Default contract

The default configuration uses standard `OTEL_*` exporter settings and emits
canonical attributes with metadata-only capture and logs disabled. No vendor
attribute prefix is required by OTLP, which defines transport and data encoding.
Attribute names and meanings come from semantic conventions, separately from
OTLP transport compliance.

Existing `gen_ai.client.*` attributes and span names remain available. The
current standard cache-write and tool identity attributes are additive. Defaults
do not automatically emit Coralogix's `cursor.*` aliases. The deprecated
`gen_ai.usage.prompt_tokens` / `completion_tokens` names are accepted as input
fields (`prompt_tokens` / `completion_tokens`); they are not emitted as span
aliases. Consumers use `gen_ai.usage.input_tokens` / `output_tokens`.

The conventions audited here are the development definitions at
[OpenTelemetry GenAI commit `9ffc7e0`](https://github.com/open-telemetry/semantic-conventions-genai/tree/9ffc7e00c639be7712e544e3e54331e885dccf84).
Their [registry](https://github.com/open-telemetry/semantic-conventions-genai/blob/9ffc7e00c639be7712e544e3e54331e885dccf84/docs/registry/attributes/gen-ai.md),
[Anthropic accounting](https://github.com/open-telemetry/semantic-conventions-genai/blob/9ffc7e00c639be7712e544e3e54331e885dccf84/docs/gen-ai/anthropic.md),
and [tool spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/9ffc7e00c639be7712e544e3e54331e885dccf84/docs/gen-ai/gen-ai-spans.md#execute-tool-span)
are the reference, rather than a vendor's field prefix. These development
conventions can change. We do not advertise blanket conformance to all GenAI
signals or to every future revision.

| Meaning | Default output | Compatibility |
|---|---|---|
| Input and output usage | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | Standard integer counts; input includes cache subsets |
| Cache reads | `gen_ai.usage.cache_read.input_tokens` | Current standard and Coralogix documented name |
| Cache writes | `gen_ai.usage.cache_write.input_tokens` | Current standard |
| Historical cache writes | `gen_ai.usage.cache_creation.input_tokens` | Retained for our existing consumers, with the same count as the standard field |
| Reasoning output | `gen_ai.usage.reasoning.output_tokens` | Standard subset of output, never added twice |
| Combined usage | `o11y.usage.total_tokens` and `gen_ai.usage.total_tokens` | Project extension and historical alias; combined total is not a current standard attribute |
| Model provider | `gen_ai.provider.name` | Explicit provider or model-based inference; unknown providers are omitted |
| Historical harness identity | `gen_ai.system` | Retained legacy harness value, such as `cursor`; it must not be interpreted as the model provider |
| Tool identity | `gen_ai.tool.name`, available `gen_ai.tool.type`, `gen_ai.tool.call.id` | Standard fields alongside existing `gen_ai.client.tool_name`, MCP and invocation fields |
| Context occupancy | `gen_ai.client.context_tokens` | Project extension; not converted to billed input usage |
| Sampling top K | `gen_ai.request.top_k` | Integer; not a floating-point value or an alias for top log probabilities |
| Hook facts and provenance | `gen_ai.client.*`, `o11y.*`, optional `hook.*` | Project extensions, even where a prefix resembles a standard namespace |

Cache write aliases describe the same subset: never sum them. Inclusive input
already includes cache reads and writes. With Anthropic's 300 new input, 600
cache reads, 100 cache writes and 200 output (including 50 reasoning), input is
1,000 and total is 1,200. With OpenAI's 1,000 inclusive input, cached input 600,
and output 200, total is also 1,200. Provider totals are used when either input
or output is unavailable; otherwise total is computed as input plus output.
Unknown counts are omitted and genuine zero counts remain integers.

OpenAI Chat Completions `prompt_tokens_details.cached_tokens` and
`completion_tokens_details.reasoning_tokens`, and Responses
`input_tokens_details.cached_tokens` and `output_tokens_details.reasoning_tokens`,
are supported at the callback root or under `usage`, including in default
metadata-only capture. Anthropic `cache_creation_input_tokens` and the normalized
`cache_write_input_tokens` spelling both map to the two cache-write output aliases.
This is a documented set of input formats, not an automatic translation of
arbitrary provider response bodies.

## What Coralogix's Cursor hook actually emits

The source audited is
[`cursor/extension/resources/hook.py` at `f00ad6f`](https://github.com/coralogix/ai-agent-instrumentation/blob/f00ad6f357a11f238180fd4f0a6fbb78cfb0e722/cursor/extension/resources/hook.py).
Its `build_attributes` function emits the following representative fields:

| Coralogix source fields | Our corresponding output |
|---|---|
| `cursor.conversation_id`, `cursor.generation_id` | `gen_ai.conversation.id`, `gen_ai.client.session_id`, `gen_ai.client.generation_id` |
| `gen_ai.system=cursor`, `gen_ai.request.model` | Retained legacy `gen_ai.system`, model attribute, separate `gen_ai.provider.name` |
| `gen_ai.tool.name`, `cursor.tool_use_id` | `gen_ai.tool.name`, `gen_ai.tool.call.id` and legacy invocation fields |
| `cursor.tool_input`, `cursor.tool_output`, `cursor.result_json` | Size/hash metadata by default; permitted content requires explicit capture policy |
| `cursor.shell_command`, `cursor.exit_code`, `cursor.cwd` | Permitted command capture, `gen_ai.client.exit_code`, `gen_ai.client.cwd` |
| `cursor.duration_ms`, `cursor.thinking_ms`, `cursor.session_duration_ms` | Available provider durations plus actual reconstructed span boundaries and timing basis |
| `cursor.context_tokens`, `cursor.context_window_size`, `cursor.context_usage_pct` | `gen_ai.client.context_tokens`, `context_window_size`, `context_usage_percent` |
| `cursor.subagent_id`, `cursor.subagent_type`, `cursor.parent_conversation_id` | Agent identity and explicit parent conversation relationship fields |
| `cursor.status`, `cursor.error`, `error.type` | Status, permitted error facts and `error.type`; raw errors require content capture |

There are **no `cursor.cache*` or `cursor.chache*` attributes in this hook** at
that commit. Its compaction branch copies `context_tokens` into
`gen_ai.usage.input_tokens`; our hooks deliberately keep context occupancy
separate from token consumption. Its spans are named `cursor.<hook_name>` and
use `SERVER`; their start/end surround export-time handling. A `duration_ms`
attribute does not change those span timestamps. Our local hook observations
and reconstructed tool/agent scopes use `INTERNAL` and explicit timing bases.

Coralogix's separate
[Cursor Usage & Cost integration](https://coralogix.com/docs/integrations/ai-observability/cursor/)
collects Admin API data and derives metrics such as `cursor_event_tokens_total`.
That is a different source from the local Cursor hook. Its
[AI cost documentation](https://coralogix.com/docs/user-guides/ai/cost/#send-cache-and-cost-data-to-coralogix)
still lists `gen_ai.usage.cache_creation.input_tokens`. This name already existed
in our telemetry and remains for our existing consumers; the current OTel
cache-write name is preferred for new queries. Dashboard recognition is a
separate backend concern and is not a requirement for OTel conformance.

## Conformance scope and remaining validation

Hook observations, sessions and generations are project spans. Their legacy
names and callback operation labels do not assert that each callback is an
independent GenAI inference request. Content JSON, provider identity, native
inference span kind/name and server attributes require evidence that hook
callbacks do not always provide. Native agent/SDK telemetry remains the source
for complete model-call coverage; do not invent missing facts or add callback,
scope and native usage together.

Before claiming full GenAI conformance for a harness, the plan requires:

- Validate a real native model call and hook tree against the pinned inference,
  agent, tool and MCP span definitions, including required attributes, operation
  boundaries, names, kinds and attributes available when sampling begins.
- Validate opt-in instructions/messages/tool content against the structured
  content schemas, including exclusions and truncation behavior.
- Verify each supported provider's usage shape, inclusive input/output bases,
  modality subsets and per-call versus cumulative accounting. Other response
  formats require explicit adapters and fixtures.
- Validate exported OTLP attributes and token accounting with a known-value
  collector canary; preserve native and hook source identities and establish
  the consumer's deduplication policy.

The automated checks in `tests/test_telemetry_v2.py` cover decoded OTLP integer
types, default-policy usage retention, old/new cache-write aliases, legacy tool
names, provider identity, real zeros and invalid counts. These checks establish
the supported metadata contract; they do not replace the live canaries above.
