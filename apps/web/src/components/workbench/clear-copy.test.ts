import { describe, expect, test } from "bun:test";
import { QueryClient } from "@tanstack/react-query";
import { type ClearCopyState, clearCopy } from "./clear-copy";

// A curator's clear of an Analysis's copy: the copy is reset, the other windows are told, and every
// read of that Analysis this window holds is read again; a clear the guard declines does nothing.

const COMMIT = "9e27".padEnd(40, "0");

function seeded(): QueryClient {
  const client = new QueryClient();
  for (const analysisId of ["an_1", "an_2"]) {
    client.setQueryData(["workspace-document", analysisId, COMMIT], {
      commit: COMMIT,
      value: "# Doc\n",
    });
    client.setQueryData(["workspace-history", analysisId, COMMIT], []);
  }
  return client;
}

function run(
  client: QueryClient,
  deps: {
    guard?: (analysisId: string) => Promise<boolean>;
    reset?: (analysisId: string) => Promise<unknown>;
  } = {},
) {
  const resets: string[] = [];
  const announced: string[] = [];
  const states: ClearCopyState[] = [];
  const done = clearCopy(
    "an_1",
    {
      guard: deps.guard ?? null,
      reset:
        deps.reset ??
        (async (analysisId) => {
          resets.push(analysisId);
        }),
      announce: (analysisId) => announced.push(analysisId),
      queryClient: client,
    },
    (state) => states.push(state),
  );
  return { done, resets, announced, states };
}

describe("clearing a copy", () => {
  test("resets it, tells the other windows, and reads this Analysis again, and no other", async () => {
    const client = seeded();
    const { done, resets, announced, states } = run(client);
    await done;
    expect(resets).toEqual(["an_1"]);
    expect(announced).toEqual(["an_1"]);
    expect(states).toEqual([{ kind: "clearing" }, { kind: "idle" }]);
    expect(
      client.getQueryData(["workspace-document", "an_1", COMMIT]),
    ).toBeUndefined();
    expect(
      client.getQueryData(["workspace-history", "an_1", COMMIT]),
    ).toBeUndefined();
    expect(
      client.getQueryData(["workspace-document", "an_2", COMMIT]),
    ).toBeDefined();
  });

  test("the guard declined does nothing at all", async () => {
    const client = seeded();
    const { done, resets, announced, states } = run(client, {
      guard: async () => false,
    });
    await done;
    expect(resets).toEqual([]);
    expect(announced).toEqual([]);
    expect(states).toEqual([]);
    expect(
      client.getQueryData(["workspace-document", "an_1", COMMIT]),
    ).toBeDefined();
  });

  test("that fails says why, and leaves what the window reads as it was", async () => {
    const client = seeded();
    const { done, announced, states } = run(client, {
      reset: async () => {
        throw new Error("the worker stopped answering");
      },
    });
    await done;
    expect(states).toEqual([
      { kind: "clearing" },
      { kind: "failed", message: "the worker stopped answering" },
    ]);
    expect(announced).toEqual([]);
    expect(
      client.getQueryData(["workspace-document", "an_1", COMMIT]),
    ).toBeDefined();
  });
});
