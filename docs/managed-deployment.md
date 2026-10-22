# Managed workstation deployment

Distribute the hook runtime through Jamf on macOS and Intune on Windows. Package
staging, hook registration, managed policy and backend delivery are separate
checks. Start with metadata-only capture and logs disabled. Actual managed-device
canaries remain required before fleet rollout.

## Prerequisites and scope

- Install Python 3.12+ at an approved absolute path. Build a separate wheelhouse
  for the target OS, architecture and Python ABI.
- Run Jamf deployment as root, and Intune deployment as SYSTEM/Administrator.
  Executable code is machine-owned; callbacks run as developers with per-user
  writable state. No background service is installed.
- Machine registration supports Claude Code and Cursor. Other harnesses retain
  `otel-hook setup --agent NAME` in the target user's context or project scope.
  Validate the harness version's managed-settings support in the pilot.
- `setup --global` means the current user across projects, not all workstation
  users. Root/SYSTEM setup configures that account. Machine settings apply to
  existing and future users when the harness loads them.
- Claude server-managed settings can require an organization plan, approval and
  a full restart. The cross-platform shell example requires Git Bash on Windows;
  generated machine registration uses native PowerShell execution for Claude.

## Build and approve an offline bundle

Build the approved PR/release wheel in CI, then resolve the target bundle:

```bash
python -m build
python scripts/build_mdm_bundle.py \
  --wheel dist/opentelemetry_hooks-0.15.0-py3-none-any.whl \
  --constraints examples/mdm/runtime-constraints.txt \
  --platform macosx_11_0_arm64 --python-version 3.12 \
  --output bundle-macos-arm64
```

Use `win_amd64` for Windows x64 or the appropriate pip platform tag for the target.
The builder needs pip and package-index access; endpoint installation is offline.
It writes `wheels/` and `requirements.lock`, with exact versions and SHA-256 hashes
for every wheel. Missing target wheels stop resolution. Test the bundle on its
native OS/architecture before approval. Constraints record a reviewed dependency
set; they do not prove that every platform has compatible binaries.

Deliver the lock, wheels and scripts through a trusted management channel. Keep
the lock with the release record: replacing both a wheel and its approved lock
defeats hash verification. Distinguish different builds of PR #99 by wheel hash,
even when the package version remains `0.15.0`.

## Deliver policy and credentials

Choose one policy model from [the templates](../examples/mdm/README.md):

1. For multiple harnesses, deploy `opentelemetry-hook.mobileconfig` through Jamf or
   `opentelemetry-hook.reg` through Intune/GPO. Substitute endpoint, protocol and
   credentials through the management system. Native harness telemetry has its
   own settings if enabled.
2. For Claude shared configuration, deploy `claude-shared.mobileconfig` or
   `claude-shared.reg` and configure Claude's native telemetry using the `env`
   block in [the managed example](../examples/claude-managed-settings.example.json).
   Limit registration to Claude using `O11Y_DEPLOY_AGENTS=claude` or `-Agents claude`.
   Remove stale exporter values from the old hook policy. Configure credentials
   once in Claude's settings; hook processes read the file even when `OTEL_*`
   variables are stripped. Use either the scripts' complete machine registration
   or server-managed hooks, never both for an event. With script registration,
   distribute only the example's `env` block through the admin console.

Claude shared lookup checks endpoint-managed settings, then the server-managed
cache, then user settings, taking the first nonempty value for each allowed
telemetry key. Enforced export excludes the user-settings fallback. Explicit
administrator hook values override imported defaults. Invalid settings disable
hook export while preserving passive responses.

macOS reads `/Library/Managed Preferences/dev.o11y.opentelemetry-hook.plist`, then
user managed preferences only if the machine file is absent. Windows reads
`SOFTWARE\Policies\OpenTelemetryHook` in HKCU then HKLM; machine values win.
Registry `capture`, `logs`, `claude` and `enforce_export` values contain JSON.

`enforce_export: true` requires a managed generic endpoint and `grpc` or
`http/protobuf` protocol, explicit or imported through Claude shared mode.
Inherited/local `OTEL_EXPORTER_OTLP*` settings, including alternate per-signal
endpoints and headers, are cleared before applying trusted values. Environment
overrides and skip flags cannot bypass enforced export or managed structured
capture/log restrictions. Unmanaged installations retain environment overrides.
See [configuration](configuration.md#managed-policy).

Use MDM/GPO protection for policy: administrators write; only required identities
read credentials. Never put secrets in hook commands, installer arguments,
repository examples or diagnostic output. The scripts accept no credential
parameters. Windows runtime ACLs grant SYSTEM/Admin full control and Users
read/execute; credential policy ACLs are a separate management responsibility.
A user who runs the hook can read its credentials. These controls govern this
runtime, not arbitrary user programs or edits to a user-readable managed cache.
Use a collector/broker when endpoints must not hold long-lived backend credentials.

## Install and register

Stage the approved bundle locally in an administrator-controlled directory.
Run on macOS as root:

```bash
O11Y_DEPLOY_PYTHON=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
  /bin/bash deploy-jamf.sh install /staged/bundle/wheels /staged/bundle/requirements.lock 0.15.0
```

Run on Windows as Administrator/SYSTEM:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy-intune.ps1 `
  -Action install -Wheelhouse C:\Staged\bundle\wheels `
  -LockFile C:\Staged\bundle\requirements.lock -Version 0.15.0 `
  -Python 'C:\Program Files\Python312\python.exe'
```

Both default to Claude and Cursor. Restrict assignments to supported harnesses.
Installation uses `--no-index --require-hashes`, checks package version and SDK
imports, and activates only a complete versioned runtime. Repeated install or
remediate repairs owned registrations without duplicates. Invalid registration
JSON is rejected rather than overwritten. Incomplete deployment exits nonzero.

Stable launchers are `/Library/Application Support/OpenTelemetryHook/bin/otel-hook`
and `%ProgramData%\OpenTelemetryHook\bin\otel-hook.ps1`. They locate versioned
runtimes without relying on GUI PATH. If the active runtime is missing, launchers
return `{}` and success. Package state stays in each developer's data directory;
do not direct all users to a shared writable `IDE_OTEL_HOOK_HOME`.

Registration merges marked entries into Claude's machine `managed-settings.json`
or Cursor's machine `hooks.json`, preserving other settings and hooks. Commands
carry `--managed`. Available marked machine registrations suppress this
package's overlapping legacy user/project callbacks per event; managed user registrations use
`--registration-scope user` to suppress older overlaps. Other vendors' hooks and
native telemetry remain separate. Avoid two independent deliveries of the same
managed command. Review obsolete user/project registrations before uninstall,
when they may resume. Equal callback content is not invocation identity.

For user-context registration of Claude/Cursor, run as the intended user:

```bash
otel-hook managed register --scope user --agent cursor --executable /absolute/path/to/otel-hook
```

This refuses a duplicate user registration while machine registration is active.
Other harnesses continue to use normal setup. For new users, either machine
settings apply or the organization must schedule user-context setup at onboarding.

## Detection and remediation

Jamf detection: `deploy-jamf.sh detect '' '' 0.15.0`. Intune detection:
`deploy-intune.ps1 -Action detect -Version 0.15.0`. Capture exit status; zero means
the expected version, dependency metadata and registrations were found. Use
action `remediate` with the installation arguments to repair. The SDK-independent
underlying command is:

```bash
otel-hook managed status --scope machine --agent cursor \
  --executable /absolute/path/to/otel-hook --expected-version 0.15.0
```

The report identifies scope, path, version and issues. A sanitized abbreviated
example is:

```json
{"status":"installed","scope":"machine","issues":[],"version":"0.15.0","backend_delivery":"unverified"}
```

As the developer, run `otel-hook --claude doctor --agent claude --json` to inspect
configuration sources, effective capture policy, local state and recent delivery
health. A successful installation or local span is not collector acceptance.

In an actual GUI-launched session, submit a prompt, invoke tools, run concurrent
subagents and end the session. Verify backend parent IDs, timestamps, package
version and capture policy. Check collector rejects, queue/quarantine growth,
latency, memory and bytes against the approved baseline. Record OS, architecture,
harness/package versions, enabled native telemetry and observed backend delivery.

## Rotation and lifecycle

Rotate credentials through the same policy source. The next hook process rereads
settings; restart native harness telemetry to update long-lived exporters. Verify
delivery with the new key before revoking the old one. Record no secret values.

Upgrade by staging an approved bundle and rerunning install. Retain the prior
complete runtime for `rollback`, which restores it; restore the corresponding
managed policy separately and verify after restart. An unchanged install must
not overwrite the prior-version rollback reference. See [upgrades](upgrades.md).

`uninstall` removes marked registration before the launcher/active reference.
It retains versioned runtimes, credentials/policy, per-user state and quarantine.
Remove server-managed hook assignments as well. Administrators can then remove
the managed credential/policy assignment and retained runtime directory after
checking references. Decide explicitly whether to retain/delete user queues;
never move records into another capture policy. Exported data is not retracted.

## Troubleshooting

| Symptom | Check and action |
|---|---|
| Installed but user hooks absent | Confirm scope; root/SYSTEM `setup --global` targets that profile. Inspect machine settings and restart. |
| Stale version or dependencies | Use managed status, inspect staging failures and redeploy the matching approved bundle. |
| Works only in terminal | Check absolute launcher path, GUI context, Python architecture and permissions; avoid shell startup/PATH assumptions. |
| Claude shared export missing | Inspect managed files/cache and shared mode. User settings alone cannot satisfy enforced export. |
| Wrong effective policy | Inspect doctor configuration sources. HKLM beats HKCU; ordinary overrides differ from enforced export. |
| Duplicate callbacks | Review server-managed, machine, user/project registrations and other integrations. Assign one owner per callback. |
| Collector failures | Check endpoint/protocol/auth, sanitized health and collector rejects; preserve policy-local queue/quarantine. |
| Interrupted stage or invalid JSON | Rerun approved remediation. Restore a valid configuration backup without discarding unrelated settings. |
