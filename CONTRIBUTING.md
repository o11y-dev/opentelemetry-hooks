# Contributing to opentelemetry-hooks

## Setup

```bash
git clone https://github.com/o11y-dev/opentelemetry-hooks.git
cd opentelemetry-hooks
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Running tests

```bash
python -m pytest tests/ -v
```

Tests run against Python 3.9 and 3.12 in CI. Make sure your changes work on both.

## How the hook works

```
IDE event (JSON on stdin)
    |
    v
otel_hook.py
    |-- normalizes event (camelCase / PascalCase / IDE-specific payloads)
    |-- detects IDE from process tree + payload heuristics
    |-- maps event to OTel span (gen_ai.client.* semantic conventions)
    |-- manages session trace hierarchy (session -> generation -> events)
    |-- exports via OTLP gRPC/HTTP + optional local file
    |
    v
stdout: {"continue": true}   (never blocks the IDE)
```

State is managed via JSON files in the hook home directory. Lock files prevent concurrent corruption. The hook must never crash or block the IDE.

## Commit convention

This project uses [Conventional Commits](https://www.conventionalcommits.org/). The release workflow auto-detects version bumps from commit prefixes:

- `feat:` - new feature (minor version bump)
- `fix:` - bug fix (patch version bump)
- `docs:` - documentation only
- `test:` - test changes only
- `ci:` - CI/CD changes
- `chore:` - maintenance

Breaking changes: add `BREAKING CHANGE:` in the commit footer (major version bump).

## Adding support for a new IDE

1. **Add IDE detection** in `otel_hook.py`: update `_detect_ide()` with process tree or payload heuristics for the new IDE
2. **Add event mapping** if the IDE uses non-standard event names: update `_normalize_event_name()`
3. **Add a setup path** in `setup.sh`: add a `--<ide-name>` flag and a `setup_<ide>()` function that writes the hook config
4. **Add an example config** in `examples/`: create `<ide>-hooks.example.json`
5. **Add tests**: cover detection, normalization, and setup in `tests/`
6. **Update README.md**: add the IDE to the supported events table and installation section

## Adding a new event type

1. Add the event name to `_normalize_event_name()` in `otel_hook.py`
2. Handle the event in `_process_event()` with appropriate span attributes
3. Add the event to the supported events table in `README.md`
4. Add test coverage in `tests/test_otel_hook.py`

## PR expectations

- One logical change per PR
- Tests pass on Python 3.9 and 3.12
- Conventional commit message on the squash/merge commit
- If you're adding IDE support, include a real example payload in the PR description
- Open an issue first for large changes

## Code style

No formatter is enforced. Match the existing style: type hints where helpful, defensive error handling (the hook must never crash), and clear variable names.
