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
        // Close the trace even when the session ends in error.
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
