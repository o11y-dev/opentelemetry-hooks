---
description: Run the OpenTelemetry hook with an explicit Antigravity IDE label.
---

1. Replace `{{SCRIPT_PATH}}` with your hook command, for example `otel-hook`.
2. Invoke `env IDE_OTEL_IDE_NAME=antigravity {{SCRIPT_PATH}}`. // turbo
3. If your workflow passes JSON on stdin, the hook will normalize common camelCase fields such as `sessionId`, `toolName`, `toolInput`, and `hookEventType`.
