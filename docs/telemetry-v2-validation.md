# Telemetry schema v2 validation

Baseline: PR #99 at `73a8590d377d41793b0279a646a5d6f5357ccf45`.
Validation environment: Linux, Python 3.12.14, OTel SDK 1.45.1.

## Reproducible checks

```bash
python -m pytest tests/test_telemetry_v2.py -q
python -m pytest tests -q
ruff check .
python -m build
bun test tests/opencode.test.ts
```

The schema-v2 tests inspect real SDK spans and decoded trace/log OTLP protobuf.
They verify branch-local boundaries, native log correlation/types/severity,
original nanosecond timestamps, UNSET ordinary status, matching tool scopes,
asynchronous subagents, original UTF-8 facts, shared preview budgets, protected
mapping validation, oversized-body omission before persistence, exporter-init
failure responses, replay identity, retry quarantine and timeout closure.
Existing tests additionally cover provider parsing, MCP correlation, capture
modes and aliases, SDK-free CLI boundaries, process-separated lifecycle replay,
policy isolation, native Windows registration and installed-command execution.
Windows CI now includes the v2 acceptance tests against the installed wheel.

The managed sandbox injects empty agent/project marker directories above test
folders. Setup autodetection tests can therefore discover `/tmp` as a project.
This reproduces on the unchanged baseline. Local setup validation used a temporary
pytest plugin outside the repository to add explicit project roots to setup test
folders. Three shell tests asserting that **no** agent markers exist anywhere in
the parent tree remain incompatible with the sandbox; they are unchanged and run
normally in GitHub Actions. No production root-discovery behavior was changed to
accommodate this environment.

## Synthetic before/after comparison

Run against separate baseline and current checkouts using the same interpreter:

```bash
python scripts/benchmark_telemetry.py /path/to/baseline /path/to/current
```

[Recorded measurements](telemetry-v2-benchmark.json) compare three six-callback
sessions for each profile/revision (18 CLI processes per cell). Each session
contains a prompt, a tool invocation, Unicode payloads and explicit termination.
The runner uses the real SDK and protobuf encoders, with collector-free exporter
stubs. CLI latency includes process startup; memory is peak per-process Linux RSS;
bytes count serialized OTLP requests, including resource/scope/envelope overhead.

This is a small synthetic sample in a shared development environment, not a
latency target or a vendor-harness performance claim. Added provenance and payload
facts increase exported bytes. Results do not measure network/collector overhead,
large/concurrent session memory, Windows latency or production backend limits.

## Live coverage and rollout limits

No live Cursor, Claude, Codex, Gemini, Copilot, Windsurf, OpenCode or Antigravity
vendor session was run for this change. Provider fixtures and the OpenCode Bun
contract suite are replay evidence. Antigravity still uses a manual compatibility
runner. Per-record collector rejections are not exposed by the SDK's export enum;
collector-side rejection metrics remain necessary. Span/log delivery is
independent and replay can duplicate accepted records.

Before rollout, perform the [schema-v2 canary procedure](upgrades.md#schema-version-2-rollout)
on the actual harness/OS/backend and compare hook latency, queue/quarantine growth,
memory and exported bytes. Canary execution and deployment are operational rollout
steps, not claimed by these local checks.
