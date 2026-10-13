# Contributing

Small fixes and improvements are welcome. Open an issue before starting a large
change so we can agree on scope.

## Development setup

Clone the repository and create a virtual environment with Python 3.12 or newer.
On macOS/Linux:

```bash
git clone https://github.com/o11y-dev/opentelemetry-hooks.git
cd opentelemetry-hooks
python3.12 -m venv .venv
source .venv/bin/activate
```

On Windows, use `py -3.12 -m venv .venv` and activate it with
`.venv\Scripts\Activate.ps1` in PowerShell. Then install the package and test tools:

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e .
```

The editable install provides the `otel-hook` command used by integration tests.
Edit the source checkout, never an installed copy in another tool's environment.

## Where changes belong

- `otel_hooks/parsers/`: shared `BaseParser`, canonical models, provider registry
  and provider-specific payload rules. Parsing must work without SDK imports,
  state writes or dependency installation.
- `otel_hook.py`: CLI setup, lifecycle handling, session state and telemetry export.
- `otel_hooks/config.py`: structured settings, managed configuration and legacy key translation.
- `otel_hooks/capture.py`: payload filtering and bounded typed attribute extraction.
- `otel_hooks/attributes.py`: canonical field mappings.
- `otel_hooks/bootstrap.py`: source-checkout dependency provisioning.
- `enrichment_connectors.py`: memory enrichment.
- `plugin/opencode.ts`: OpenCode callback bridge.

For provider changes, verify payloads and hook behavior against first-party
documentation and add sanitized fixtures under `tests/fixtures/contracts/`.
Preserve existing event contracts, privacy defaults and provider responses unless
the change explicitly documents a migration. Keep tests focused on behavior and
regressions; avoid unrelated refactoring.

## Validation

Run lint and the Python suite on macOS/Linux:

```bash
python -m ruff check .
python -m pytest tests/ -q
```

On native Windows, run lint and the platform, parser and contract suites:

```powershell
python -m ruff check .
python -m pytest tests/test_platform_support.py tests/test_parsers.py tests/test_contracts.py -q
```

The `setup.sh` tests require a POSIX environment. Windows CI also installs the
built wheel outside the checkout to check packaging and executable paths.

For OpenCode bridge changes, install Bun and run `bun test tests/opencode.test.ts`.
For packaging changes, install `build` and run `python -m build`; verify new modules
and required data files are present in the wheel and source distribution.

Use synthetic payloads and temporary state for manual hook replays. Set
`IDE_OTEL_HOOK_HOME` and `IDE_OTEL_CONFIG` to temporary locations, and clear inherited
exporter credentials before testing. Do not commit real transcripts, tokens,
collector credentials, local configuration or generated state.

## Pull requests

Describe the problem, resulting behavior and validation. Update the README or
relevant docs when setup or behavior changes, and add an unreleased changelog entry
for user-visible changes. Use conventional commit prefixes such as `fix:`, `feat:`
or `docs:`. Keep the PR focused and ensure CI passes.

Contributions are provided under the repository's [MIT license](LICENSE).
