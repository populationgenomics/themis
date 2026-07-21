import { describe, expect, test } from "bun:test";
import type { BetaManagedAgentsSessionEvent } from "@anthropic-ai/sdk/resources/beta/sessions/events";
import { foldEvents } from "./client";

// The projection is the production path (the fixture bypasses it), so its
// load-bearing behaviours are pinned here: agent/user messages fold to
// assistant/user narration; the custom `shell` tool (agent.custom_tool_use →
// user.custom_tool_result) folds to a row labelled by its model-stated `intent`; a
// prebuilt tool (agent.tool_use) is labelled by its target field; and a call with
// no result event yet stays awaiting.

// Synthetic beta events, cast to the SDK union — the fold only reads the fields each
// `type` carries, so a partial object typed here is enough. Every event the events
// API returns is stamped with `processed_at`, so the helper supplies one.
function ev(e: Record<string, unknown>): BetaManagedAgentsSessionEvent {
  return {
    processed_at: "2024-01-01T00:00:00Z",
    ...e,
  } as unknown as BetaManagedAgentsSessionEvent;
}

const text = (t: string) => [{ type: "text", text: t }];

describe("foldEvents", () => {
  test("projects narration and paired tool calls onto the oneof stream", () => {
    const raw = [
      ev({ type: "session.status_running" }),
      ev({ type: "user.message", id: "u1", content: text("kickoff") }),
      ev({
        type: "agent.message",
        id: "m1",
        content: text("narrating **bold**"),
      }),
      // Custom shell tool: labelled by its model-stated intent, paired by
      // custom_tool_use_id.
      ev({
        type: "agent.custom_tool_use",
        id: "t1",
        name: "shell",
        input: { command: "ls /workspace", intent: "list the workspace" },
      }),
      ev({
        type: "user.custom_tool_result",
        id: "r1",
        custom_tool_use_id: "t1",
        content: text("doc.md"),
        is_error: false,
      }),
      // Prebuilt tool: labelled by its target field, paired by tool_use_id.
      ev({
        type: "agent.tool_use",
        id: "t2",
        name: "read",
        input: { file_path: "/workspace/doc.md" },
      }),
      ev({
        type: "user.tool_result",
        id: "r2",
        tool_use_id: "t2",
        content: text("contents"),
        is_error: false,
      }),
      // A tool with no result event yet → result stays absent (awaiting).
      ev({
        type: "agent.tool_use",
        id: "t3",
        name: "write",
        input: { file_path: "/workspace/out.md" },
      }),
      ev({ type: "session.status_idle", stop_reason: { type: "end_turn" } }),
    ];

    const { events } = foldEvents(raw);

    expect(events.map((e) => e.kind.case)).toEqual([
      "user",
      "assistant",
      "tool",
      "tool",
      "tool",
    ]);
    // Every projected event carries its ordering key.
    expect(events.every((e) => e.occurredAt !== undefined)).toBe(true);

    const [user, assistant, shell, read, write] = events;
    expect(user.kind.case === "user" && user.kind.value.text).toBe("kickoff");
    expect(
      assistant.kind.case === "assistant" && assistant.kind.value.text,
    ).toBe("narrating **bold**");

    if (shell.kind.case !== "tool") throw new Error("expected a tool call");
    expect(shell.kind.value).toMatchObject({
      name: "shell",
      intent: "list the workspace",
      command: "ls /workspace",
    });
    expect(shell.kind.value.result).toMatchObject({
      output: "doc.md",
      isError: false,
    });

    if (read.kind.case !== "tool") throw new Error("expected a tool call");
    expect(read.kind.value.intent).toBe("/workspace/doc.md");
    expect(read.kind.value.result?.output).toBe("contents");

    if (write.kind.case !== "tool") throw new Error("expected a tool call");
    expect(write.kind.value.result).toBeUndefined();
  });

  test("an agent turn with only tool calls emits no narration", () => {
    const { events } = foldEvents([
      ev({ type: "agent.message", id: "m", content: [] }),
      ev({
        type: "agent.tool_use",
        id: "t",
        name: "read",
        input: { file_path: "/x" },
      }),
    ]);
    expect(events.map((e) => e.kind.case)).toEqual(["tool"]);
  });
});
