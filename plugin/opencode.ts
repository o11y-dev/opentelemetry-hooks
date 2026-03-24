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
  // The payload carries source_app: "OpenCode", and the hook also prefers
  // parent-process discovery, so no IDE override env var is needed here.
  // Hook errors are always non-fatal — never block agent execution.
  async function invoke(payload: Record<string, unknown>): Promise<void> {
    const json = JSON.stringify(payload)
    try {
      await $`otel-hook`
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
      type Props = { info?: { id?: string }; message?: { role?: string; sessionID?: string; parts?: Array<{ type: string; text?: string }> } }
      const props = event.properties as Props | undefined

      if (event.type === "session.created") {
        await invoke({
          hook_event_name: "SessionStart",
          source_app: "OpenCode",
          session_id: props?.info?.id,
          cwd: directory,
        })
      } else if (event.type === "session.deleted") {
        await invoke({
          hook_event_name: "SessionEnd",
          source_app: "OpenCode",
          session_id: props?.info?.id,
        })
      } else if (event.type === "session.error") {
        // Emit SessionEnd with status=error so the trace is closed on failure.
        await invoke({
          hook_event_name: "SessionEnd",
          source_app: "OpenCode",
          session_id: props?.info?.id,
          status: "error",
        })
      } else if (event.type === "session.idle") {
        // Agent finished responding — equivalent to the Stop event in other IDEs.
        await invoke({
          hook_event_name: "Stop",
          source_app: "OpenCode",
          session_id: props?.info?.id,
          status: "idle",
        })
      } else if (event.type === "message.updated") {
        // Capture user prompts only (not assistant responses).
        const msg = props?.message
        if (msg?.role === "user") {
          const textPart = msg.parts?.find((p) => p.type === "text")
          await invoke({
            hook_event_name: "UserPromptSubmit",
            source_app: "OpenCode",
            session_id: msg.sessionID,
            prompt: textPart?.text,
          })
        }
      } else if (event.type === "file.edited") {
        // Published by write / edit / apply_patch tools after every file modification.
        // properties contains exactly: { file: string } — no session_id available.
        const filePath = (event.properties as { file?: string } | undefined)?.file
        await invoke({
          hook_event_name: "AfterFileEdit",
          source_app: "OpenCode",
          file_path: filePath,
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
      const outObj = output as Record<string, unknown> | undefined
      // For the bash tool, metadata.exit carries the numeric exit code.
      // Non-zero exit → PostToolUseFailure; zero / absent → PostToolUse.
      const meta = outObj?.metadata as Record<string, unknown> | undefined
      const exitCode = typeof meta?.exit === "number" ? (meta.exit as number) : undefined
      const failed = exitCode !== undefined && exitCode !== 0

      await invoke({
        hook_event_name: failed ? "PostToolUseFailure" : "PostToolUse",
        source_app: "OpenCode",
        session_id: input?.sessionID,
        tool_name: input?.tool,
        tool_id: input?.callID,
        tool_input: (input as Record<string, unknown> | undefined)?.args,
        tool_output: outObj?.output,
        ...(failed ? { exit_code: exitCode, error: `exit ${exitCode}` } : {}),
      })
    },
  }
}

export default OtelHookPlugin
