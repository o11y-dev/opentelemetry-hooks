# Managed deployment templates

Follow [the deployment guide](../../docs/managed-deployment.md) before assignment.

| Artifact | Purpose |
|---|---|
| `deploy-jamf.sh` | Root-run macOS offline install, detection, remediation, rollback and unregister |
| `deploy-intune.ps1` | Administrator/SYSTEM Windows equivalent |
| `runtime-constraints.txt` | Dependency constraints for target-specific hash-locked bundles |
| `opentelemetry-hook.mobileconfig` / `.reg` | Shared fleet endpoint/auth and metadata-only policy |
| `claude-shared.mobileconfig` / `.reg` | Claude-only policy importing native managed telemetry settings |

Profiles are alternatives. Substitute credentials through the management system;
the repository contains placeholders only. Registry templates do not set ACLs:
use Intune/GPO to permit administrator/SYSTEM writes and required hook-user reads.
Protect Claude managed-settings files similarly. A credential read by a user
process is accessible to that user; these files are not a secret vault.

The scripts stage code and registrations. They do not import policies, rotate
secrets or delete user queues. Uninstall retains versioned runtimes and managed
policy for explicit administrator cleanup.

For Claude choose one registration source: script-generated machine hooks or
server-managed hooks. The complete Claude example provides guarded cross-platform
commands; machine registration generates the same passive event list natively.
