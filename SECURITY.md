# Security reporting

Hook payloads can contain prompts, source code, file paths, tool output and
credentials. Do not include real session payloads, secrets or private repository
content in a public issue, discussion or pull request.

## Report a sensitive finding

Check the repository's [Security tab](https://github.com/o11y-dev/opentelemetry-hooks/security)
for a **Report a vulnerability** option. If available, use it for private disclosure.

If private reporting is unavailable, open a
[GitHub issue](https://github.com/o11y-dev/opentelemetry-hooks/issues/new)
requesting a private reporting channel. Include only a general description such
as "Request for private security contact". Wait for an agreed private channel
before sharing vulnerability details or reproduction material. Do not describe
an exploitable issue publicly to obtain a contact.

Once a private channel is established, provide:

- Affected package, Python and harness versions, and operating system.
- Installation method and whether managed configuration is involved.
- Expected and observed behavior, with synthetic reproduction steps.
- Potential impact and any relevant sanitized diagnostics.

Coordinate public disclosure with the maintainers. No guaranteed acknowledgment,
fix deadline or long-term support window is published. Include the exact affected
version; do not assume older releases receive security backports.

## Deployment responsibilities

Use a tested, pinned version and review configuration before upgrades. Keep
exporter credentials out of repositories, configure TLS and destination access
controls, and apply organizational retention and endpoint-management policies.

Metadata-only capture is the default, but metadata can still identify sessions
and tools. Full capture is not a redaction feature. Span extraction limits do
not bound full JSON logs, and masking canonical text does not mask the original
log body. See [configuration](docs/configuration.md) for capture, exclusions and
managed-policy precedence.
