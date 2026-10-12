import { expect, test } from "bun:test"
import { OtelHookPlugin } from "../plugin/opencode"

test("native event contracts preserve sessions without closing recoverable errors", async () => {
  const payloads: Record<string, unknown>[] = []
  const shell = () => ({
    stdin(json: string) { payloads.push(JSON.parse(json)); return this },
    quiet() { return this },
    nothrow() { return Promise.resolve() },
  })
  const plugin = await OtelHookPlugin({ $: shell, directory: "/fixture" } as never)
  for (const event of [
    { type: "session.created", properties: { info: { id: "s1" } } },
    { type: "message.updated", properties: { info: { id: "m1", role: "user", sessionID: "s1" } } },
    { type: "session.compacted", properties: { sessionID: "s1" } },
    { type: "permission.asked", properties: { sessionID: "s1", id: "p1", permission: "bash", tool: { callID: "t1" } } },
    { type: "session.error", properties: { sessionID: "s1", error: { name: "APIError" } } },
    { type: "session.idle", properties: { sessionID: "s1" } },
    { type: "session.deleted", properties: { info: { id: "s1" } } },
  ]) await plugin.event!({ event } as never)
  expect(payloads.map(p => p.hook_event_name)).toEqual([
    "SessionStart", "UserPromptSubmit", "PostCompact", "PermissionRequest", "ErrorOccurred", "Stop", "SessionEnd",
  ])
  expect(payloads.every(p => p.session_id === "s1")).toBe(true)
  expect(payloads[1].event_id).toBe("m1")
  expect(payloads[3].tool_use_id).toBe("t1")
  expect(payloads[4].error).toEqual({ name: "APIError" })
})

test("telemetry failures do not reject native callbacks", async () => {
  let calls = 0
  const shell = () => {
    if (calls++ > 0) throw new Error("fixture transport failure")
    return { quiet() { return Promise.resolve() } }
  }
  const plugin = await OtelHookPlugin({ $: shell, directory: "/fixture" } as never)
  await expect(plugin.event!({ event: { type: "session.compacted", properties: { sessionID: "s1" } } } as never)).resolves.toBeUndefined()
})

test("permission replies retain request identity without forwarding content or deciding permissions", async () => {
  const payloads: Record<string, unknown>[] = []
  const shell = () => ({
    stdin(json: string) { payloads.push(JSON.parse(json)); return this },
    quiet() { return this },
    nothrow() { return Promise.resolve() },
  })
  const plugin = await OtelHookPlugin({ $: shell, directory: "/fixture" } as never)
  for (const reply of ["once", "always", "reject"]) {
    const result = await plugin.event!({ event: { type: "permission.replied", properties: {
      sessionID: "s1", requestID: "p1", reply, message: "PRIVATE_FIXTURE",
    } } } as never)
    expect(result).toBeUndefined()
  }
  await plugin.event!({ event: { type: "permission.replied", properties: {
    sessionID: "s1", permissionID: "legacy", response: "once",
  } } } as never)
  expect(payloads.map(p => p.permission_response)).toEqual(["once", "always", "reject", "once"])
  expect(payloads.map(p => p.permission_request_id)).toEqual(["p1", "p1", "p1", "legacy"])
  expect(payloads.every(p => p.hook_event_name === "PermissionReplied" && p.session_id === "s1")).toBe(true)
  expect(JSON.stringify(payloads)).not.toContain("PRIVATE_FIXTURE")
})
