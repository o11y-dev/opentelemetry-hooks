// OpenTelemetry Hook — OpenCode plugin example
//
// Copy this file to one of:
//   ~/.config/opencode/plugins/otel-hook.ts   (global)
//   .opencode/plugins/otel-hook.ts             (project)
//
// Or use the setup script:
//   bash setup.sh --opencode           # project-level
//   bash setup.sh --opencode --global  # global
//
// Requires otel-hook in PATH:
//   pip install opentelemetry-hooks
//
// Configure your OTLP endpoint in otel_config.json (see otel_config.example.json).

import type { Plugin } from "@opencode-ai/plugin"

export const OtelHookPlugin: Plugin = async ({ $, directory }) => {
  try {
    await $`which otel-hook`.quiet()
  } catch {
    console.warn("[otel-hook] otel-hook not found in PATH — plugin disabled")
    console.warn("[otel-hook] Install with: pip install opentelemetry-hooks")
    return {}
  }

  async function invoke(payload: Record<string, unknown>): Promise<void> {
    const json = JSON.stringify(payload)
    try {
      await $`IDE_OTEL_IDE_NAME=opencode otel-hook`
        .stdin(json)
        .quiet()
        .nothrow()
    } catch {
      // Hook errors are non-fatal — never block agent execution
    }
  }

  return {
    // ── Session lifecycle ──────────────────────────────────────────────────
    event: async ({ event }) => {
      if (event.type === "session.created") {
        const info = (event.properties as { info?: { id?: string } } | undefined)?.info
        await invoke({
          hook_event_name: "SessionStart",
          source_app: "OpenCode",
          session_id: info?.id,
          cwd: directory,
        })
      } else if (event.type === "session.deleted") {
        const info = (event.properties as { info?: { id?: string } } | undefined)?.info
        await invoke({
          hook_event_name: "SessionEnd",
          source_app: "OpenCode",
          session_id: info?.id,
        })
      }
    },

    // ── Tool call lifecycle ────────────────────────────────────────────────
    "tool.execute.before": async (input, output) => {
      await invoke({
        hook_event_name: "PreToolUse",
        source_app: "OpenCode",
        session_id: input?.sessionID,
        tool_name: input?.tool,
        tool_id: input?.callID,
        tool_input: (output as Record<string, unknown> | undefined)?.args,
      })
    },

    "tool.execute.after": async (input, output) => {
      await invoke({
        hook_event_name: "PostToolUse",
        source_app: "OpenCode",
        session_id: input?.sessionID,
        tool_name: input?.tool,
        tool_id: input?.callID,
        tool_input: (input as Record<string, unknown> | undefined)?.args,
        tool_output: (output as Record<string, unknown> | undefined)?.output,
      })
    },
  }
}

export default OtelHookPlugin
