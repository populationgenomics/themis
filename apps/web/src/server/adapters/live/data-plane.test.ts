import { describe, expect, test } from "bun:test";
import Anthropic from "@anthropic-ai/sdk";
import { create } from "@bufbuild/protobuf";
import { type Analysis, AnalysisSchema } from "@/models/workbench";
import { ResourceNotFoundError, UnmanagedSessionError } from "../../errors";
import { createPool } from "../../pg";
import {
  anthropicClientOverFetch,
  anthropicErrorResponse,
  jsonResponse,
  type RecordedRequest,
} from "./anthropic.test-support";
import { DataPlane } from "./data-plane";
import type { BearerDeriver } from "./derive";
import { Sql } from "./sql";

// A run driven by another harness has no session here. The poll still has to answer, because the
// backend adds the workspace tip to its response and the document pane has no other source; but
// only for that case: a platform session whose read fails is an outage, and an outage that rendered
// as a run with nothing to say would be indistinguishable from a healthy one that has not started.

function analysis(sessionId: string): Analysis {
  return create(AnalysisSchema, {
    id: "an_1",
    sessionId,
    projectId: "proj_a",
  });
}

const UNUSED_DERIVER: BearerDeriver = {
  async deriveBearer(sessionId: string): Promise<string> {
    throw new Error(`unexpected deriveBearer(${sessionId})`);
  },
};

function unusedSql(): Sql {
  return new Sql(
    createPool({
      stream: () => {
        throw new Error("unexpected SQL connection");
      },
    }),
  );
}

function plane(
  handler: (req: RecordedRequest) => Response | Promise<Response>,
) {
  const { client, requests } = anthropicClientOverFetch(handler);
  return {
    dataPlane: new DataPlane(client, UNUSED_DERIVER, unusedSql()),
    requests,
  };
}

describe("a run another harness drove", () => {
  test("polls to an empty conversation without asking the platform", async () => {
    const { dataPlane, requests } = plane(() => {
      throw new Error("unexpected platform request");
    });

    const response = await dataPlane.pollEvents(analysis("pi_abc"));

    expect(requests).toEqual([]);
    expect(response.events).toEqual([]);
  });

  test.each([
    [
      "a thread read",
      (dataPlane: DataPlane) =>
        dataPlane.getThread(analysis("pi_abc"), "sthr_1"),
    ],
    [
      "a steer",
      (dataPlane: DataPlane) =>
        dataPlane.steerAnalysis(analysis("pi_abc"), "hello"),
    ],
    [
      "an interrupt",
      (dataPlane: DataPlane) => dataPlane.interruptAnalysis(analysis("pi_abc")),
    ],
  ] as const)(
    "refuses %s rather than sending it nowhere",
    async (_label, act) => {
      const { dataPlane, requests } = plane(() => {
        throw new Error("unexpected platform request");
      });

      await expect(act(dataPlane)).rejects.toBeInstanceOf(
        UnmanagedSessionError,
      );
      expect(requests).toEqual([]);
    },
  );
});

describe("a run the platform holds", () => {
  test("still reads its conversation from the platform", async () => {
    const { dataPlane } = plane((req) => {
      if (
        req.method === "GET" &&
        req.url.pathname === "/v1/sessions/sesn_01xyz/events"
      ) {
        return jsonResponse({
          data: [
            {
              type: "agent.message",
              id: "ev_1",
              processed_at: "2024-01-01T00:00:00Z",
              content: [{ type: "text", text: "Reading the locus." }],
            },
          ],
          next_page: null,
        });
      }
      return anthropicErrorResponse(404, "not_found_error", "unknown session");
    });

    const response = await dataPlane.pollEvents(analysis("sesn_01xyz"));

    expect(response.events).toHaveLength(1);
    expect(response.events[0]?.kind).toEqual({
      case: "assistant",
      value: expect.objectContaining({ text: "Reading the locus." }),
    });
  });

  test("surfaces a failed read rather than an empty conversation", async () => {
    const { dataPlane } = plane(() =>
      anthropicErrorResponse(500, "api_error", "the platform is unreachable"),
    );

    await expect(
      dataPlane.pollEvents(analysis("sesn_01xyz")),
    ).rejects.toBeInstanceOf(Anthropic.InternalServerError);
  });

  test("routes an unrecognised session prefix to the platform so it fails loud rather than silently returning empty", async () => {
    const { dataPlane, requests } = plane(() =>
      anthropicErrorResponse(404, "not_found_error", "unknown session id"),
    );

    await expect(
      dataPlane.pollEvents(analysis("unknown_123")),
    ).rejects.toBeInstanceOf(ResourceNotFoundError);
    expect(requests).toHaveLength(1);
    expect(requests[0]?.url.pathname).toBe("/v1/sessions/unknown_123/events");
  });
});
