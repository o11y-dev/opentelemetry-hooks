# Upgrade and rollback

This capture pipeline changes defaults and consumer contracts. Before upgrading:

1. Record the package version and installer, and back up `otel_config.json`,
   managed policy and hook registrations. Review the [changelog](../CHANGELOG.md).
2. Replace retired content flags with an explicit `capture.mode` and path policy.
   Omitting the mode selects `metadata_only`; hook logs default off. Use
   `logs.enabled` to enable the log signal. Existing runtime aliases remain
   supported; old per-content and per-event log switches do not restore capture.
3. Update consumers that rely on separate shell/MCP/tool/conversation logs or old
   flattened fields. Validate their interpretation against the
   [telemetry contract](telemetry-reference.md), not just successful export.
4. Install the approved version, rerun setup and verify real sessions in the pilot.

For rollback, stop active pilot sessions, reinstall the previously tested version
with the package manager that owns `otel-hook`, restore its configuration and
managed policy, then rerun setup in the original scope and verify delivery.
If Reflect owns the hook environment, roll back through Reflect's package manager;
do not install a competing standalone copy. Unregister hooks to pause collection
if needed; use `--no-global --cwd /path/to/repo` for project registrations.

Rollback does not retract exported data. New capture policies isolate pending
state, and older versions may not understand it. Preserve state for investigation
and verify which batches replay before resuming collection. See
[configuration and state behavior](configuration.md).

