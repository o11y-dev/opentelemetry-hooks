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
| [OpenCode](https://opencode.ai/docs/plugins/) | 8 callbacks | 10: session.compacted and permission.asked; fixes idle/error session IDs and message.updated metadata |
| Antigravity / compatible runners | Manual adapter | Preserved; no verified native registration contract established in this audit |

OpenCode's session and message payloads were cross-checked against its
[published SDK types](https://github.com/anomalyco/opencode-sdk-go/blob/main/event.go).
The bridge retains event/session metadata when a bus callback lacks message text;
it does not pretend to capture complete conversations from `message.updated`.

## Intentional limits

- Claude `WorktreeCreate` takes over worktree creation; automatic telemetry setup must not register it. `FileChanged` requires a deliberate file-watcher scope and is also omitted. Permission hooks observe requests without returning approval decisions.
- Windsurf's transcript callback is understood but not registered: enabling it makes the harness write full transcripts independently of this package's content setting. The ordinary response callback supplies the turn end. The legacy `.windsurf/hooks.json` path remains compatible; an existing active `.devin/hooks.json` takes precedence.
- Gemini model callbacks can repeat within a turn or per streaming chunk. Nested `llm_request` messages and `llm_response` chunks are omitted from persisted events; this change captures model identity and callback occurrence, not new token totals.
- Copilot's cloud agent cannot fire every interactive CLI event. Registering the full CLI vocabulary does not imply cloud notifications or interactive permission prompts.
- Codex SessionEnd and Interrupt have a three-second maximum. Their registrations use that limit and skip stale-session maintenance, but heavily loaded machines and unavailable exporters can still prevent delivery.
- New content facts retain hashes and lengths by default; explicit content capture follows existing gates. MessageDisplay records chunk facts and preserves message ID/index/final metadata without replacing rendered text.

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
