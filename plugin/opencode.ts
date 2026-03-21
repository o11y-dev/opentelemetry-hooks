import type { Plugin } from "@opencode-ai/plugin"

// OpenTelemetry Hook — OpenCode plugin
//
// Captures AI agent activity (sessions, tool calls) as OpenTelemetry traces
// and forwards them to any OTLP-compatible backend (Jaeger, Grafana, Datadog, …).
//
// Requires: otel-hook in PATH
//   pip install opentelemetry-hooks
//   # or: pipx install opentelemetry-hooks
//
// Setup:
//   bash setup.sh --opencode           # project-level  (.opencode/plugins/)
//   bash setup.sh --opencode --global  # global  (~/.config/opencode/plugins/)
//
// Docs: https://github.com/o11y-dev/opentelemetry-hooks

export const OtelHookPlugin: Plugin = async ({ $, directory }) => {
  try {
    await $`which otel-hook`.quiet()
  } catch {
    console.warn("[otel-hook] otel-hook not found in PATH — plugin disabled")
    console.warn("[otel-hook] Install with: pip install opentelemetry-hooks")
    return {}
  }

  // Pipe a JSON payload to otel-hook on stdin.
  // IDE_OTEL_IDE_NAME is set for belt-and-suspenders detection; the payload
  // also carries source_app: "OpenCode" which triggers auto-detection.
  // Hook errors are always non-fatal — never block agent execution.
  async function invoke(payload: Record<string, unknown>): Promise<void> {
    const json = JSON.stringify(payload)
    try {
      await $`IDE_OTEL_IDE_NAME=opencode otel-hook`
        .stdin(json)
        .quiet()
        .nothrow()
    } catch {
      // pass
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
    //
    // tool.execute.before: input = { tool, sessionID, callID }
    //                      output = { args }   ← mutable; can be modified
    //
    // tool.execute.after:  input  = { tool, sessionID, callID, args }
    //                      output = { title, output, metadata }
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
