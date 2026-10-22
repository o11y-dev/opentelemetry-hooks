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

### Coralogix and current OTel naming audit

The [source-pinned compatibility audit](coralogix-compatibility.md) records the
2026-10-22 comparison. Fifteen additional cases cover OpenAI Responses and Chat
Completions usage under the default policy, cache-write aliases and inclusive
accounting, invalid counts, standard/legacy tool identity and context occupancy.
The schema-v2, capture and runtime suites pass 431 tests. Contract, current-hook
and managed-deployment checks pass 123 tests with one Windows-only skip; two
setup tests use the explicit temporary project roots described below because
of sandbox ancestor markers. The rebuilt installed wheel passes all 35 schema-v2
cases outside the checkout. Ruff, whitespace checks and wheel/sdist builds pass.
No live Coralogix payload or AI Center recognition test was performed.

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

### Managed deployment extension

The MDM extension adds explicit machine/user registration, per-event overlap
suppression, enforced export policy, shared Claude settings, offline bundle
generation and Jamf/Intune lifecycle scripts. The operator procedures and policy
templates are in [managed deployment](managed-deployment.md).

Local Linux validation covers registration upgrade/removal without discarding
unrelated settings, interrupted atomic saves, partial/missing launchers, passive
duplicate callbacks, shared-settings precedence, export override rejection,
registry value types, SDK-independent management commands, profile/JSON templates,
missing-file shell guards, and wheel hash locking. The focused managed/template
suite passes 24 tests with one Windows-native test skipped on Linux.
The broader Linux run, using temporary explicit project roots outside the
repository, passed 702 tests with three platform skips; the three unchanged
shell tests requiring no ancestor agent markers remain incompatible with this
sandbox. Additional current managed tests were run separately after that run.
Ruff, whitespace checks, wheel and source-distribution builds also pass locally.
The rebuilt installed wheel passed 43 managed/template/schema-v2 tests outside
the checkout; the Windows-native check and source-only bundle-builder test were
skipped in that installed-wheel run.

CI includes native Windows PowerShell parsing/launcher checks and macOS policy
tests, plus target-specific wheelhouse resolution and hash-checked offline
installation. These runner checks do not establish Jamf/Intune assignment,
new-user onboarding, actual GUI harness behavior or live collector acceptance.
Record those checks and credential rotation/rollback on managed pilot devices
before broader rollout. No fleet deployment or live backend canary is claimed.

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
