# Pinned OpenTelemetry GenAI contract

The six JSON schemas and LICENSE are unmodified files from
`open-telemetry/semantic-conventions-genai` commit
`9ffc7e00c639be7712e544e3e54331e885dccf84`, under `model/gen-ai/` and the
repository root respectively. They are distributed under Apache-2.0.

`gen-ai-9ffc7e0.json` extracts attribute names and value types from that revision's
`docs/registry/attributes/gen-ai.md` for offline conformance checks.
Tests validate emitted attributes and structured content against these pinned
upstream definitions without fetching schemas during test execution.
