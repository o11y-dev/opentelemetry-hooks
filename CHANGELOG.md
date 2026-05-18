# Changelog

## 0.13.1 (unreleased)

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
