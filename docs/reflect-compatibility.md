# Reflect compatibility with the new hook capture pipeline

One shared hook configuration can capture useful evidence across all eight
parsers. It cannot preserve every behavior of today's Reflect without consumer
changes. Use [the shared profile](../examples/otel_config.reflect.example.json)
for integration work, not the canonical-only
[minimal profile](../examples/otel_config.reflect.minimal.example.json).

## What the shared profile captures

| Evidence | Span representation |
|---|---|
| Harness, session, generation, turn, callback and tool identity | Canonical `gen_ai.client.*` attributes when available |
| Shell commands and tool file paths | Canonical attributes, plus scalar input preview aliases for existing readers |
| Tool arguments | Bounded recursive typed fields; selected names map to `gen_ai.client.tool.input.*` |
| Tool result text and exit codes | Known text leaves map to `gen_ai.client.tool.output`; exit codes map to `process.exit.code` |
| Prompts and responses | Canonical text/length/hash facts plus legacy scalar preview aliases |
| Model/request/usage metadata | Canonical values plus selected original `metadata`, `model_params`, `usage` and Gemini model evidence |
| Windsurf and Copilot shapes | Native `tool_info.*` paths and Copilot result-text fields are selected explicitly |
| Original callback | Complete parsed JSON in the correlated log body; never the entire hook JSON on a span |

The shared profile uses 96 extra attributes, 4,000 characters per extracted
string, depth 16, arrays up to 128 elements and a 32 KiB extraction budget.
The SDK span count limit is 256 to leave space for canonical enrichment.
These extraction budgets do not bound every canonical attribute or the full log
body. Backends can impose smaller limits. Inspect `hook.capture.dropped_fields`
and `hook.capture.collisions` when fields are missing.

Mappings are exact source-to-target renames, not fallback rules. Multiple aliases
mapped to one target are intended for different callback shapes. If both occur
in the same callback, extraction omits the colliding target and reports the
collision. Structured arguments remain typed fields; a JSON-encoded string such
as Copilot `toolArgs` is not decoded by recursive extraction.

## Current Reflect review

Reviewed local Reflect checkout `34e010e` (version 0.9.8). Its working tree was
clean. This is source and isolated ingestion evidence, not an installed-runtime
upgrade or production validation.

The check replayed the minimal and shared hook profiles through Reflect's actual
`migrate`, `ingest_local_spans_file` and `normalize_pending_raw_events` functions,
using a separate in-memory SQLite database for every provider/profile pair.
All 16 combinations normalized with zero failures. The representative callbacks
covered Cursor, Windsurf, Claude, Codex, Gemini, Copilot, OpenCode and the
Antigravity manual compatibility runner.

Successful ingestion did not mean identical analytics:

| Check | Minimal profile | Shared profile / remaining work |
|---|---|---|
| Session steps and prompt/response facts | Ingested for all eight parsers | Preserved |
| Tool output previews | Empty in all eight fixtures | Restored in seven fixtures; the Windsurf fixture supplied an exit code but no output text |
| Older Bash command counter | Canonical command was ignored for `PreToolUse` | Scalar `gen_ai.client.tool.input` mapping restored the targeted counter check |
| Typed subagent name | Unmapped `hook.tool_input.subagent_type` was ignored | Mapping to `gen_ai.client.tool.input.subagent_type` restored the targeted check |
| Skill name from tool arguments | Legacy helper expects a JSON input preview | Still requires Reflect to read typed skill fields or decode the correlated raw log |
| Exact tool output hash | New hook emits `gen_ai.client.tool.response.sha256` | Reflect reads `gen_ai.client.tool.output.sha256`; hashing a mapped text preview is not the original response fingerprint |
| Shell/tool classification | Canonical command takes priority over tool callback type | Reflect must preserve invocation/result correlation for shell tools |

The fixtures combine command and file arguments to exercise field coverage;
they are not recordings of complete live sessions. In the command-bearing
fixtures, Reflect classified invocation spans as shell steps before tool steps,
so most tool-table input hashes/previews were absent despite being present on
raw invocation spans. This needs a consumer fix, not more capture mappings.

## Required Reflect integration changes

1. **Upgrade dependency and setup together.** Both `pyproject.toml` and
   `instrumentation.py` constrain hooks to `>=0.14,<0.15`. Setup still writes
   `IDE_OTEL_CAPTURE_TEXT`; `_prompt_capture_enabled` reads that retired flag.
   Read/write structured `capture`, `logs`, `batch` and `local_spans` settings,
   and test capture-disabled setup against an existing full-capture config.
2. **Prefer canonical fields in consumers.** Read `gen_ai.client.command` for
   Bash/tool invocations; accept `gen_ai.client.tool.response.sha256` as the
   output fingerprint and `gen_ai.client.tool_output.text` for available output
   facts. Preserve invocation hashes when updating a tool result.
3. **Consume typed arguments and raw hook logs.** Skill/subagent/MCP extraction
   should use typed input fields and original callback shapes, including
   Copilot's encoded arguments. Avoid requiring JSON argument blobs on spans.
4. **Fix classification and correlation.** A shell command attribute must not
   discard the tool invocation's identity, preview or hash. Keep event/tool-call
   IDs authoritative when matching invocation/result observations.
5. **Migrate log-specific readers.** One correlated callback log replaces legacy
   shell/MCP/tool/conversation fan-out. Read original payload fields from its
   body, deduplicate by hook event identity, and avoid counting mirrored logs
   as additional tool calls.

Relevant Reflect code: `instrumentation.py`, `processing.py`,
`telemetry_facts.py`, `store/normalize.py`, `store/hook_facts.py`,
`tool_outcomes.py` and `dashboard_sessions.py`. These paths contain explicit
attribute-name readers; the reviewed code does not provide a general configured
semantic alias layer that would make the above changes disappear.

## Recheck during integration

Replay the hook capture tests with a retained temporary directory, then ingest
the resulting `.state/local_spans/*.jsonl` files using the candidate Reflect
checkout in fresh databases. Verify tool rows, previews, exact hashes, shell
counts, MCP identity, skill/subagent extraction, conversation facts and log
correlation, not just a zero-error normalization result. Include real recorded
payloads with both alias fields and large inputs to check collisions and budgets.

Keep the shared config as the default integration example. Agent snippets remain
optional smaller selections, not a prerequisite for using multiple agents.
