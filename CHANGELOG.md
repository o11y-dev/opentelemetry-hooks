# Changelog

## 0.14.0 (unreleased)

### Added
- Added a canonical hook event model with provider adapters for Cursor, Windsurf, Claude, Codex, Gemini, Antigravity, Copilot, and OpenCode.
- Added span-first prompt, response, stop-message, error, and delegation facts with length and SHA-256 metadata by default and explicit content capture through `IDE_OTEL_CAPTURE_CONVERSATION_CONTENT`.
- Added stable session-backed subagent identities, parent relationships, delegation status, and trace links when valid source contexts are available.
- Added workspace, repository root, credential-free Git remote hashing, branch identity, hook schema/source provenance, and native trace/span identifiers.
- Added `otel-hook doctor` with human and JSON reports for registrations, privacy controls, exporter health, pending state, and sanitized delivery failures.
- Added sanitized provider contract fixtures and a capability manifest for every supported agent family.

### Changed
- Extended session-backed idempotency to prompts, errors, subagents, compaction, and permission callbacks while retaining bounded state and lifecycle cleanup.
- Made spans the authoritative conversation signal. Optional trace-correlated conversation logs require `IDE_OTEL_ENABLE_CONVERSATION_LOGS=true`.
- Decorated OTLP exporters with bounded delivery-health recording that excludes payloads, headers, credentials, and raw error messages.

### Fixed
- Prevented raw prompt, error, and delegation content from bypassing privacy gates through direct event attribute mappings.
- Preserved native and hook telemetry as distinct sources while linking valid native contexts instead of deduplicating them.

## 0.13.6 (unreleased)

### Added
- Added explicit cross-agent MCP server/tool attributes for encoded Codex and Claude tool names and Cursor's `mcp_server_name` payloads.
- Added `telemetry.distro.name` and `telemetry.distro.version` hook provenance without changing agent service identity or version fields.
- Preserved OTel resource attributes in local JSON spans so installed hook provenance remains inspectable without an OTLP backend.

### Changed
- Encapsulated bounded tool deduplication and provider-specific invocation matching in a session-aware MCP correlator.
- Made session creation, callback deduplication, generic/dedicated Cursor MCP correlation, and pending-generation ownership session-scoped and atomic across hook processes.
- Made `Stop` flush only the current generation while preserving the session, and made `SessionEnd` or stale finalization flush every session-owned batch exactly once before cleanup.

### Fixed
- Prevented duplicate Cursor callbacks and dedicated MCP events from creating duplicate logical tool spans, while reusing the stable generic `tool_use_id` for correlated evidence.
- Correlated unambiguous Codex `PermissionRequest` callbacks with their open tool invocation and preserved Claude failure IDs and status.
- Prevented stale-session cleanup from deleting pending Codex batches before their generation and session spans can be exported.
- Suppressed duplicate generation `Stop` callbacks and preserved correlated Cursor MCP evidence as trace-correlated logs in streaming mode without creating duplicate logical tool spans.
- Routed stale-finalized session roots to their own local JSON files instead of the cleanup trigger's `unscoped.jsonl` file.

## 0.13.5 (2026-05-25)

### Fixed
- Removed project-directory (`.cursor`/cwd) checks from agent-engine inference and consolidated engine detection paths to avoid weak, environment-dependent relabeling.

## 0.13.4 (2026-05-20)

### Fixed
- Preserved native Cursor client attribution for Cursor-style payloads when leaked Claude-specific hints would otherwise misclassify the event, while still recording any distinct wrapper via `gen_ai.client.wrapper`

## 0.13.3 (2026-05-19)

### Fixed
- Made passive Codex hooks stay silent for non-`Stop` events like `PostToolUse`, while keeping `Stop` on the minimal valid JSON response and suppressing the custom `local_spans` field in Codex stdout responses

## 0.13.2 (2026-05-18)

### Added
- Added Claude Code `PreCompact` and `PostCompact` hook registration, examples, docs, and tests

### Fixed
- Suppressed passive stdout for Codex `SessionStart` and `UserPromptSubmit` while preserving JSON stdout for events like `Stop`

## 0.13.1 (2026-05-18)

### Fixed
- Updated Codex setup to use the current `[features].hooks` flag and remove deprecated `codex_hooks` entries from existing configs

## 0.13.0 (2026-05-17)

### Added
- Added a supported-agent setup matrix to the README covering Cursor, Claude Code, Gemini CLI, GitHub Copilot, OpenCode, and compatible hook runners
- Added dedicated Gemini CLI setup documentation and clarified how Gemini model, tool, and agent lifecycle events map to canonical hook spans
- Added this changelog and release workflow helpers so GitHub releases use curated release notes from `CHANGELOG.md`

### Changed
- Switched release versioning from tag-derived `setuptools-scm` metadata to a checked-in `pyproject.toml` version managed by the release workflow
- Refreshed pip/pipx, source-checkout, config, and log path docs so package installs and copied-source installs are easier to distinguish
- Updated the pinned README install example and package metadata for the `0.13.0` release

### Fixed
- Included the OpenCode TypeScript example in package data and source distributions so documented examples are present in built artifacts
