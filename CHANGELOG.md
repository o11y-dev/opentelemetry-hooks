# Changelog

## 0.13.6 (unreleased)

### Added
- Added explicit cross-agent MCP server/tool attributes for encoded Codex and Claude tool names and Cursor's `mcp_server_name` payloads.
- Added `telemetry.distro.name` and `telemetry.distro.version` hook provenance without changing agent service identity or version fields.

### Changed
- Encapsulated bounded tool deduplication and provider-specific invocation matching in a session-aware MCP correlator.
- Made session creation, callback deduplication, generic/dedicated Cursor MCP correlation, and pending-generation ownership session-scoped and atomic across hook processes.
- Made `Stop` flush only the current generation while preserving the session, and made `SessionEnd` or stale finalization flush every session-owned batch exactly once before cleanup.

### Fixed
- Prevented duplicate Cursor callbacks and dedicated MCP events from creating duplicate logical tool spans, while reusing the stable generic `tool_use_id` for correlated evidence.
- Correlated unambiguous Codex `PermissionRequest` callbacks with their open tool invocation and preserved Claude failure IDs and status.
- Prevented stale-session cleanup from deleting pending Codex batches before their generation and session spans can be exported.

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
