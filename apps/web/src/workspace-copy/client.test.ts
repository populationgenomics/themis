import { describe, expect, test } from "bun:test";
import {
  CopyWorkerLostError,
  createCopyClient,
  type Heartbeat,
  MISSED_PINGS,
  PublishUnconfirmedError,
} from "./client";
import type { RequestEnvelope, ResponseEnvelope } from "./protocol";

// The window's side of the port, against a stand-in worker on a MessageChannel and a heartbeat the
// test ticks by hand, as a throttled tab's timers would fire: rarely, but in order.

const TIP = "9e27".padEnd(40, "0");

interface FakeWorker {
  /** Requests the worker has received, in order. */
  received: RequestEnvelope[];
  answerPings: boolean;
  send(message: ResponseEnvelope): void;
}

function harness() {
  const workers: FakeWorker[] = [];
  let tick: (() => void) | undefined;
  const heartbeat: Heartbeat = (next) => {
    tick = next;
    return () => {
      tick = undefined;
    };
  };
  const client = createCopyClient(() => {
    const channel = new MessageChannel();
    const worker: FakeWorker = {
      received: [],
      answerPings: true,
      send: (message) => channel.port2.postMessage(message),
    };
    channel.port2.onmessage = (event: MessageEvent<RequestEnvelope>) => {
      worker.received.push(event.data);
      if (event.data.request.method === "ping" && worker.answerPings) {
        worker.send({ id: event.data.id, ok: true, value: undefined });
      }
    };
    workers.push(worker);
    return { port: channel.port1, onFailure: () => {} };
  }, heartbeat);
  const settle = () => Bun.sleep(5);
  return {
    client,
    workers,
    settle,
    async beat(times = 1) {
      for (let i = 0; i < times; i += 1) {
        if (tick === undefined) throw new Error("no heartbeat is running");
        tick();
        await settle();
      }
    },
    beating: () => tick !== undefined,
  };
}

describe("the window's connection to the copy's worker", () => {
  test("answers a request by its id", async () => {
    const h = harness();
    const history = h.client.history("an_1", TIP);
    await h.settle();
    const [request] = h.workers[0].received;
    h.workers[0].send({ id: request.id, ok: true, value: [] });
    expect(await history).toEqual([]);
  });

  test("keeps a slow worker that answers its pings, however seldom the heartbeat fires", async () => {
    const h = harness();
    const sync = h.client.sync("an_1", TIP);
    await h.beat(MISSED_PINGS * 3);
    const request = h.workers[0].received.find(
      (r) => r.request.method === "sync",
    );
    if (request === undefined) throw new Error("no sync sent");
    h.workers[0].send({
      id: request.id,
      ok: true,
      value: undefined,
    });
    expect(await sync).toBeUndefined();
    await h.beat();
    expect(h.beating()).toBe(false);
  });

  test("gives up on a worker that leaves its pings unanswered, then starts a new one", async () => {
    const h = harness();
    const sync = h.client.sync("an_1", TIP).catch((error: unknown) => error);
    await h.settle();
    h.workers[0].answerPings = false;
    // One tick sends the ping; the worker counts as dead once that many more pass unanswered.
    await h.beat(MISSED_PINGS);
    expect(h.beating()).toBe(true);
    await h.beat();
    expect(await sync).toBeInstanceOf(CopyWorkerLostError);
    const again = h.client.history("an_1", TIP);
    await h.settle();
    expect(h.workers).toHaveLength(2);
    const [request] = h.workers[1].received;
    h.workers[1].send({ id: request.id, ok: true, value: [] });
    expect(await again).toEqual([]);
  });

  test("a publish cut off with its worker is unconfirmed, not a retryable failure", async () => {
    const h = harness();
    const publish = h.client
      .publish({
        analysisId: "an_1",
        base: TIP,
        curatorEmail: "curator@example.org",
        message: "Set the state",
        files: [{ path: "state.txt", bytes: new Uint8Array([120]) }],
      })
      .catch((error: unknown) => error);
    await h.settle();
    h.workers[0].answerPings = false;
    await h.beat(MISSED_PINGS + 1);
    expect(await publish).toBeInstanceOf(PublishUnconfirmedError);
  });
});
