# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Gemini CLI support with auto-detect and setup
- Robustness improvements for hook reliability

### Fixed
- Python 3.9 compatibility for shlex command parsing
- Matcher events and auto-detect edge cases

## [0.9.0] - 2026-03-23

### Added
- IDE identity declaration configuration

### Fixed
- Resolve absolute path for `otel-hook` in setup.sh

## [0.8.0] - 2026-03-22

### Added
- Claude Code and OpenCode hooks plugin support
- `--reinstall` flag for setup.sh

### Fixed
- Deduplicate span processor registration in file and console exporters

## [0.7.0] - 2026-03-19

### Added
- GenAI semantic conventions (`gen_ai.client.*` namespace, v1.37+)
- Prefer global `otel-hook` command by default

### Changed
- README trimmed and reorganized around GenAI conventions

## [0.6.0] - 2026-03-16

### Added
- GitHub-friendly README badges (release, CI, OpenTelemetry)
- CLI-style runner label normalization for broader IDE coverage

## [0.5.0] - 2026-03-16

### Added
- Claude Code and Antigravity hook compatibility
- `otel-hook` CLI command entry point
- GenAI v1.37 attribute support
- Release README version updater and merged branch cleanup
- Antigravity workflow example

### Changed
- Default to local script; add `OTEL_HOOK_USE_GLOBAL` opt-in for global command
- Recommend pipx for installation; document pip PATH and PEP 668 pitfalls

### Fixed
- Use writable hook home directory when installed as a package
- Install example JSON templates in wheels
- Python 3.9 compatibility (replace 3.10+ union type syntax)

## [0.4.0] - 2026-03-03

### Added
- Local-files-only export mode (skip OTLP when no endpoint configured)
- Jaeger + local file export backend examples
- Automated semantic release from conventional commits
- Weekly agentic code improvement workflow

### Changed
- README improved for Reddit and AI discoverability

## [0.3.0] - 2026-02-26

### Added
- `_FileSpanExporter` OTel-native file exporter (replaces manual file writes)
- Local span persistence as JSONL for agent analysis
- Local trace saving flag in hook response (opt-in)
- Expanded `otel_config.example.json` with all env vars by category

### Changed
- Renamed local trace saving semantics to "local spans"

### Fixed
- Upgrade pip in CI workflows to resolve outdated dependency

## [0.2.0] - 2026-02-12

### Added
- MDM support for macOS (plist) and Windows (registry) configuration

## [0.1.0] - 2026-02-12

### Added
- Initial OpenTelemetry hook implementation
- Cursor IDE hook support with session-level trace hierarchy
- Cross-process trace context via file-based state management
- CI/CD workflow with Python 3.9 and 3.12 matrix
- Semantic versioning and release workflow
- Stale session flushing (emit `ide.session` root span for unclosed sessions)
- Copilot contributor instructions

[Unreleased]: https://github.com/o11y-dev/opentelemetry-hooks/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/o11y-dev/opentelemetry-hooks/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/o11y-dev/opentelemetry-hooks/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/o11y-dev/opentelemetry-hooks/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/o11y-dev/opentelemetry-hooks/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/o11y-dev/opentelemetry-hooks/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/o11y-dev/opentelemetry-hooks/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/o11y-dev/opentelemetry-hooks/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/o11y-dev/opentelemetry-hooks/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/o11y-dev/opentelemetry-hooks/releases/tag/v0.1.0
