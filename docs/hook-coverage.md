# Harness hook coverage

Research date: 2026-10-11. Counts describe installer registrations,
not features proven live in each installed harness version.

| Harness / first-party reference | Before | This change |
|---|---|---|
| [Claude Code](https://code.claude.com/docs/en/hooks) | 11 events | 31: adds permissions/denials, notifications, API failure stops, setup, instructions, prompt expansion, display chunks, tool batches, task/team events, config/workspace changes, model switches and MCP elicitation |
| [Codex](https://learn.chatgpt.com/docs/hooks) | 6 events | 12: session end, pre/post compaction, subagent start/stop and interruption; SessionStart also matches compact |
| [Cursor](https://cursor.com/docs/hooks) | 15 events | 21: preCompact, Tab file read/edit, afterAgentResponse, afterAgentThought and workspaceOpen |
| [Gemini CLI](https://geminicli.com/docs/hooks/reference/) | 8 events, incorrect turn mapping | 11: BeforeToolSelection, Notification and PreCompress; BeforeAgent/AfterAgent define the turn, model callbacks remain observations |
| [Copilot CLI/cloud](https://docs.github.com/en/copilot/reference/hooks-reference) | 6 repo events; native payload event name missing | 14 events with explicit CLI event arguments, normalized toolArgs/toolResult, and user-level CLI installation |
| [Windsurf / Cascade](https://docs.devin.ai/desktop/cascade/hooks) | 15 Cursor names in settings.json | 11 native events in hooks.json; nested tool_info plus trajectory/execution IDs normalized |
| [OpenCode](https://opencode.ai/docs/plugins/) | 8 callbacks | 11: session.compacted, permission.asked and permission.replied; fixes idle/error session IDs and message.updated metadata |
| Antigravity / compatible runners | Manual adapter | Preserved; no verified native registration contract established in this audit |

OpenCode's session and message payloads were cross-checked against its
[published SDK types](https://github.com/anomalyco/opencode-sdk-go/blob/main/event.go).
The bridge retains event/session metadata when a bus callback lacks message text;
it does not pretend to capture complete conversations from `message.updated`.
Permission reply fields follow the [current event schema](https://github.com/anomalyco/opencode/blob/dev/packages/schema/src/v1/permission.ts);
the bridge also accepts the older SDK's `permissionID` / `response` spelling.

## Cursor native export and hooks

[Cursor Enterprise export](https://cursor.com/docs/enterprise/opentelemetry-export)
provides metrics and logs alongside our hook traces. It is configured separately;
this package does not ingest it or enable its content-export settings.

The [wire reference](https://cursor.com/docs/enterprise/opentelemetry-export/wire)
defines these integration boundaries:

- Native logs have no trace/span IDs. Compare `cursor.conversation.id` with our
  `gen_ai.conversation.id` for the same conversation; validate with paired live
  records before relying on this join. Do not equate request IDs with generation IDs.
- `cursor.hook.execution_complete` reports hook duration and outcome, including
  failures/timeouts. It can help detect failures before our process exports.
- Native request logs carry token counts; estimated cost can receive billing
  corrections linked by `cursor.usage_event.id`.
- Dedupe native logs using `cursor.event.id`. Metrics are delta sums, lack session
  IDs and have at-most-once delivery. Avoid adding native and hook tool counts.

These are downstream integration inputs, not new callback registrations. Keep
native-source provenance separate from `gen_ai.client.telemetry_source=hook`.

## Coverage recheck

The linked command-hook references were rechecked on 2026-10-12: Cursor 21/21,
Codex 12/12, Gemini 11/11 and Copilot 14/14 event names are registered. Claude
registers 31/33 and Cascade 11/12, with the exclusions below. Event-name coverage
does not guarantee every field or execution surface is supported.

OpenCode is a selected bridge, not full event-bus coverage. It now observes
permission replies as well as requests. `session.status`, `session.updated`,
`session.diff`, message-part/removal events, command, watcher, installation, LSP,
server and TUI events remain outside this bridge. Adding them needs a concrete
telemetry use case and payload/privacy contracts; `shell.env` mutates execution
environment and is not an observation callback.

Cursor cloud agents expose fewer callbacks than desktop: local user hooks are
unavailable, and hooks begin only after the environment becomes writable.
Cloud lifecycle, dedicated MCP, Tab and workspace callbacks have documented
restrictions; self-hosted workers have separate session-boundary behavior.
See [Cursor cloud hook coverage](https://cursor.com/docs/hooks#cloud-agent-support).

The recheck also corrected Cursor permission response schemas (including Tab
reads) and removed an incorrect seconds-to-milliseconds conversion for MCP
durations. Earlier exported durations are not rewritten.

## Intentional limits

- Claude `WorktreeCreate` takes over worktree creation; automatic telemetry setup must not register it. `FileChanged` requires a deliberate file-watcher scope and is also omitted. Permission hooks observe requests without returning approval decisions.
- Windsurf's transcript callback is understood but not registered: enabling it makes the harness write full transcripts independently of this package's content setting. The ordinary response callback supplies the turn end. The legacy `.windsurf/hooks.json` path remains compatible; an existing active `.devin/hooks.json` takes precedence.
- Gemini model callbacks can repeat within a turn or per streaming chunk. Nested `llm_request` messages and `llm_response` chunks are omitted from persisted events; this change captures model identity and callback occurrence, not new token totals.
- Copilot's cloud agent cannot fire every interactive CLI event. Registering the full CLI vocabulary does not imply cloud notifications or interactive permission prompts.
- Codex SessionEnd and Interrupt have a three-second maximum. Their registrations use that limit and skip stale-session maintenance, but heavily loaded machines and unavailable exporters can still prevent delivery.
- New content facts retain hashes and lengths by default; explicit content capture follows existing gates. MessageDisplay records chunk facts and preserves message ID/index/final metadata without replacing rendered text.

## Upgrading existing installations

These changes correct registrations and event meaning; they do not introduce a timed
removal of compatibility interfaces. Install the updated package, then rerun setup
in the scope where the old hooks were installed.

| Harness | Previous behavior | Migration |
|---|---|---|
| Windsurf | Cursor-style event names written to `settings.json` | Run `otel-hook setup --agent windsurf` (add `--no-global` for project scope). Setup writes native events to `hooks.json`; it does **not** remove entries from the old `settings.json`. Review that file and remove only obsolete `otel-hook` registrations, preserving other settings. Legacy compatible-runner payload aliases remain accepted. |
| Gemini CLI | Model callbacks opened/closed turns; agent callbacks appeared as subagents | Rerun Gemini setup for added events. Update queries that relied on those old mappings: `BeforeAgent` / `AfterAgent` now delimit turns and model callbacks remain observations. Historical telemetry is not rewritten; Antigravity retains its separate compatibility mapping. |
| Copilot | CLI setup was repository-only; native payloads could lack event identity | Use `otel-hook setup --agent copilot --no-global` to upgrade repository hooks, or omit `--no-global` for user-level CLI hooks. Setup adds `--copilot --event <name>`. `bash setup.sh --copilot` and direct Python `setup_copilot()` calls retain their repository default. |
| Codex | No end/interruption registrations; SessionStart matcher omitted compaction | Rerun Codex setup. Managed legacy matchers migrate while unrelated commands remain intact; SessionEnd and Interrupt use three-second timeouts. Stale-session cleanup remains a fallback. |
| OpenCode | Errors closed the session; idle/message callbacks read incorrect payload fields | Rerun OpenCode setup to replace the installed plugin. Update queries expecting every error to imply SessionEnd; recoverable errors now emit ErrorOccurred. |

The `IDE_OTEL_*` settings, `--agent` option, identity aliases and existing telemetry
names remain supported. A terminology change alone does not require new keys or
defaults. Prefer managed source flags for new registrations and the narrower
`IDE_OTEL_CAPTURE_CONVERSATION_CONTENT` setting for conversation capture; legacy
source detection and `IDE_OTEL_CAPTURE_TEXT` remain available for compatibility.

## Validation and rollout

`tests/test_current_hooks.py` covers native payloads, setup upgrades, generation boundaries,
content gates and passive responses. `tests/opencode.test.ts` executes native plugin callbacks
with a mocked transport, including failure isolation. Existing tests cover the branch's ABI,
bounded export and response-size fixes. These are contract tests, not live harness certification.

Reinstall the package from this checkout, then rerun setup for each desired harness to
update registrations. Setup preserves unrelated commands and settings. A package reinstall
alone does not install newly added hooks or replace an already installed OpenCode plugin.
Contract tests and local-file replay do not verify remote exporter delivery or
shutdown timing when a collector is unavailable.
