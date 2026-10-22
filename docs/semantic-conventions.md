# OpenTelemetry semantic contract

Schema 3 targets the development GenAI conventions at
[`open-telemetry/semantic-conventions-genai` commit `9ffc7e0`](https://github.com/open-telemetry/semantic-conventions-genai/tree/9ffc7e00c639be7712e544e3e54331e885dccf84).
The [attribute registry](https://github.com/open-telemetry/semantic-conventions-genai/blob/9ffc7e00c639be7712e544e3e54331e885dccf84/docs/registry/attributes/gen-ai.md),
[tool span definition](https://github.com/open-telemetry/semantic-conventions-genai/blob/9ffc7e00c639be7712e544e3e54331e885dccf84/docs/gen-ai/gen-ai-spans.md#execute-tool-span),
and [internal agent spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/9ffc7e00c639be7712e544e3e54331e885dccf84/docs/gen-ai/gen-ai-agent-spans.md#invoke-agent-internal-span)
define names, types and meanings. This is a breaking telemetry contract with no
compatibility mode or vendor-specific aliases. OTLP exporter configuration is
separate from the semantic contract.

## Attributes

| Meaning | Emitted attribute |
|---|---|
| Model provider | `gen_ai.provider.name`; explicit provider wins over model inference |
| Requested/response model | `gen_ai.request.model`, `gen_ai.response.model` |
| Conversation | `gen_ai.conversation.id` |
| Inclusive input/output usage | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| Cache subsets | `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens` |
| Reasoning subset of output | `gen_ai.usage.reasoning.output_tokens` |
| Tool identity | `gen_ai.tool.name`, available `gen_ai.tool.type`, `gen_ai.tool.call.id` |
| Hosted agent resource | `gen_ai.agent.id`, only from explicit `agent_resource_id` |
| Available agent name/version | `gen_ai.agent.name`, `gen_ai.agent.version` |
| Structured content, opt-in | `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` |
| Hook identity, invocation IDs and timing | Project attributes under `o11y.*` |
| Combined input plus output | Project attribute `o11y.usage.total_tokens` |

`gen_ai.system`, `gen_ai.usage.cache_creation.input_tokens`,
`gen_ai.usage.total_tokens` and project-specific `gen_ai.client.*` attributes are
not emitted. There are no Coralogix `cursor.*` aliases. Capture mappings cannot
write into the reserved `gen_ai.*` namespace.

Harness names such as `cursor` and `claude` are `o11y.client.name`, not model
providers. Per-run and subagent invocation IDs use `o11y.client.agent_id`; they
are not hosted agent resource identifiers. Context-window occupancy stays
`o11y.client.context_tokens`, separate from billed input usage. Unknown facts
are omitted rather than fabricated.

Token counts are nonnegative integers; zeros are retained and booleans, strings
and fractional counts are rejected. Input includes cache-read and cache-write
subsets. Anthropic's 300 new input + 600 cached + 100 cache-write becomes 1,000
input tokens. With 200 output, including 50 reasoning, the combined project
total is 1,200. OpenAI's input is already inclusive and is not increased by its
cache-read subset. Both Responses and Chat Completions usage-detail objects are
supported. Provider API spellings such as `cache_creation_input_tokens` are
input fields to normalize, not output attribute aliases.

## Spans and observations

| Scope | Span name | Kind | `gen_ai.operation.name` |
|---|---|---|---|
| Tool invocation with known tool name | `execute_tool <tool>` | `INTERNAL` | `execute_tool` |
| Agent turn or subagent invocation | `invoke_agent [agent name]` | `INTERNAL` | `invoke_agent` |
| Callback observation | `o11y.client.hook.<event>` | `INTERNAL` | Omitted |
| Session container | `o11y.client.session` | `INTERNAL` | Omitted |
| Tool scope without a known tool name | `o11y.client.tool` | `INTERNAL` | Omitted |

`o11y.span.role` distinguishes `generation`, `subagent`, `tool`, `callback` and
`session` even when two agent invocations have the same span name. Streaming
callbacks are point observations with separate invocation scopes. Buffered
start callbacks delimit reconstructed scopes; their matching terminal callbacks
remain observations. Correlated MCP callback aliases do not create additional
tool invocations. Missing boundaries carry explicit inference metadata.

Available operation, tool, provider and model identity is supplied when an
invocation span is created so the SDK sampler can inspect it. Failures set
`ERROR` and a low-cardinality `error.type`; `_OTHER` is the documented fallback
when the hook reports failure without a specific type. Intentional interruption
does not imply failure. Successful tool-result content is omitted on failures.

Hook callbacks do not establish individual remote inference requests. They do
not produce synthetic `chat` client spans or fabricated server addresses. Native
agent/SDK instrumentation supplies those calls and protocol-level MCP spans.
Native and hook observations remain distinct sources and must not be added as
independent copies of the same token usage.

## Structured content and bounds

Python span attributes serialize structured values as valid JSON strings.
System instructions use an array of content parts; message attributes use arrays
of messages with roles and parts. Tool arguments/results must be objects;
serialized object strings are decoded. Other values remain project hook evidence
and are omitted from these standard attributes. Tool definitions emit the required
type and name; full original definitions remain available in permitted raw logs.
Capture selection and exclusions apply before enrichment and persistence.
Metadata-only capture emits no content. Incomplete locally truncated tool values,
invalid JSON and oversized structured attributes are omitted rather than exported
as broken JSON. Exact semantic identity strings are preserved or omitted, never
shortened into different model or tool names.

## Validation and rollout

The pinned registry fixture is
[`tests/fixtures/semconv/gen-ai-9ffc7e0.json`](../tests/fixtures/semconv/gen-ai-9ffc7e0.json).
The upstream structured-content JSON schemas are pinned alongside it. Acceptance
tests compare actual SDK/OTLP output against that registry and those schemas, validate
operation names/kinds and creation-time attributes, and assert removed aliases
are absent. See the [validation record](telemetry-v2-validation.md).

The schema version is part of the capture-policy fingerprint. Earlier-schema
queues are isolated; they are not rewritten or replayed into schema 3. Consumers
must update to the new fields and span roles. A pilot should verify exported
OTLP, known-value token accounting, parentage and collector rejection metrics
against the installed package and target harness before a wider rollout.

The [Coralogix comparison](coralogix-compatibility.md) is research evidence only;
its field names and dashboard behavior are not implementation requirements.
