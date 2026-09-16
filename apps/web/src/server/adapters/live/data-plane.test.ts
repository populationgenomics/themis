import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { isManagedSession } from "@/lib/harness";
import { type Analysis, AnalysisSchema } from "@/models/workbench";
import { isUnmanagedSessionError } from "../../errors";
import type { AnthropicClient } from "./client";
import { DataPlane } from "./data-plane";
import type { KmsSessionTokenDeriver } from "./derive";
import type { Gcs } from "./gcs";
import type { Sql } from "./sql";

// A run driven by another harness has no session here. The poll still has to answer, because the
// working-document version rides on it and the document pane has no other source — but only for
// that case: a platform session whose read fails is an outage, and an outage that rendered as a run
// with nothing to say would be indistinguishable from a healthy one that has not started.

function analysis(sessionId: string): Analysis {
  return create(AnalysisSchema, {
    id: "an_1",
    sessionId,
    projectId: "proj_a",
  });
}

type Calls = { listEvents: number; steers: number; interrupts: number };

function plane(
  calls: Calls,
  listEvents: () => unknown = () => ({ events: [] }),
  document: { version: number; markdown: string } | null = {
    version: 7,
    markdown: "# doc",
  },
) {
  const anthropic = {
    listEvents: async () => {
      calls.listEvents += 1;
      return listEvents();
    },
    sendUserMessage: async () => {
      calls.steers += 1;
    },
    sendInterrupt: async () => {
      calls.interrupts += 1;
    },
  } as unknown as AnthropicClient;
  const gcs = {
    latestWorkingDocument: async () => document,
  } as unknown as Gcs;
  return new DataPlane(
    anthropic,
    {} as unknown as KmsSessionTokenDeriver,
    {} as unknown as Sql,
    gcs,
  );
}

describe("a run another harness drove", () => {
  test("polls to an empty conversation and the document version it has produced", async () => {
    const calls: Calls = { listEvents: 0, steers: 0, interrupts: 0 };

    const response = await plane(calls).pollEvents(analysis("pi_abc"));

    expect(calls.listEvents).toBe(0);
    expect(response.events).toEqual([]);
    // The signal the document pane fetches on; without it the pane never asks for anything.
    expect(response.workingDocumentVersion).toBe(7);
  });

  test("polls to no version at all before it has committed anything", async () => {
    const calls: Calls = { listEvents: 0, steers: 0, interrupts: 0 };

    const response = await plane(calls, undefined, null).pollEvents(
      analysis("pi_abc"),
    );

    expect(calls.listEvents).toBe(0);
    // Unset, not zero: the pane asks for nothing until the run has produced something.
    expect(response.workingDocumentVersion).toBeUndefined();
  });

  test("refuses a steer and an interrupt rather than sending them nowhere", async () => {
    const calls: Calls = { listEvents: 0, steers: 0, interrupts: 0 };
    const plane_ = plane(calls);

    const refusal = async (call: Promise<void>): Promise<unknown> =>
      call.then(
        () => null,
        (error: unknown) => error,
      );

    expect(
      isUnmanagedSessionError(
        await refusal(plane_.steerAnalysis(analysis("pi_abc"), "hello")),
      ),
    ).toBe(true);
    expect(
      isUnmanagedSessionError(
        await refusal(plane_.interruptAnalysis(analysis("pi_abc"))),
      ),
    ).toBe(true);
    expect(calls.steers).toBe(0);
    expect(calls.interrupts).toBe(0);
  });
});

describe("a run the platform holds", () => {
  test("still reads its conversation from the platform", async () => {
    const calls: Calls = { listEvents: 0, steers: 0, interrupts: 0 };

    const response = await plane(calls).pollEvents(analysis("sesn_01xyz"));

    expect(calls.listEvents).toBe(1);
    expect(response.workingDocumentVersion).toBe(7);
  });

  test("surfaces a failed read rather than an empty conversation", async () => {
    const calls: Calls = { listEvents: 0, steers: 0, interrupts: 0 };
    const outage = plane(calls, () => {
      throw new Error("the platform is unreachable");
    });

    await expect(outage.pollEvents(analysis("sesn_01xyz"))).rejects.toThrow(
      "the platform is unreachable",
    );
  });
});

describe("which runtime a session id names", () => {
  test("only a locally-minted prefix reads as unmanaged", () => {
    expect(isManagedSession("pi_abc")).toBe(false);
    expect(isManagedSession("sesn_01xyz")).toBe(true);
    // An id in neither shape is the platform's, so a surprise never silences the conversation.
    expect(isManagedSession("something-else")).toBe(true);
  });
});
