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
      type Props = {
        sessionID?: string
        error?: unknown
        info?: { id?: string; role?: string; sessionID?: string }
        permission?: string
        requestID?: string
        reply?: string
        permissionID?: string
        response?: string
        id?: string
        tool?: { callID?: string }
      }
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
        // An error can be recoverable; it does not mean the session was deleted.
        await invoke({
          hook_event_name: "ErrorOccurred",
          source_app: "OpenCode",
          session_id: props?.sessionID,
          error: props?.error,
          status: "error",
        })
      } else if (event.type === "session.idle") {
        // Agent finished responding — equivalent to the Stop event in other IDEs.
        await invoke({
          hook_event_name: "Stop",
          source_app: "OpenCode",
          session_id: props?.sessionID,
          status: "idle",
        })
      } else if (event.type === "session.compacted") {
        await invoke({
          hook_event_name: "PostCompact",
          source_app: "OpenCode",
          session_id: props?.sessionID,
        })
      } else if (event.type === "permission.asked") {
        await invoke({
          hook_event_name: "PermissionRequest",
          source_app: "OpenCode",
          session_id: props?.sessionID,
          event_id: props?.id,
          tool_name: props?.permission,
          tool_use_id: props?.tool?.callID,
        })
      } else if (event.type === "permission.replied") {
        // Observe the completed decision without changing the permission flow.
        const requestID = props?.requestID ?? props?.permissionID
        await invoke({
          hook_event_name: "PermissionReplied",
          source_app: "OpenCode",
          session_id: props?.sessionID,
          event_id: requestID,
          permission_request_id: requestID,
          permission_response: props?.reply ?? props?.response,
        })
      } else if (event.type === "message.updated") {
        // Capture user prompts only (not assistant responses).
        // MessageUpdated supplies metadata in info; text parts are separate events.
        const msg = props?.info
        if (msg?.role === "user") {
          await invoke({
            hook_event_name: "UserPromptSubmit",
            source_app: "OpenCode",
            session_id: msg.sessionID,
            event_id: msg.id,
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
