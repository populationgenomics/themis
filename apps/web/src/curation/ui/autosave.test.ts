import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import {
  type Assessment,
  AssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";
import { Autosave, type SaveState } from "./autosave";

// The save path with a server of its own, so what reaches the server — how many writes, in what
// order, carrying which value — and what the indicator is told are both observable.

const IDLE_MS = 20;
const RETRY_MS = 20;
const CASE = "case";
const WORKFLOW = "pop_frq";
const ROUTING = "routing";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function prose(text: string): Assessment {
  return create(AssessmentSchema, {
    kind: { case: "caseContext", value: { other: text } },
  });
}

/** A server that records every write; rejects a section's writes while it is listed as down, and
 *  holds a write for as long as the next queued delay says. */
class Harness {
  readonly writes: { sectionId: string; text: string }[] = [];
  readonly states: SaveState[] = [];
  readonly down = new Set<string>();
  readonly delays: number[] = [];
  readonly autosave: Autosave;

  constructor(stored: Record<string, string> = {}) {
    this.autosave = new Autosave({
      stored,
      write: async (sectionId, payload) => {
        const delay = this.delays.shift();
        if (delay !== undefined) await sleep(delay);
        if (this.down.has(sectionId)) throw new Error("503");
        const decoded = JSON.parse(payload) as {
          caseContext: { other: string };
        };
        this.writes.push({ sectionId, text: decoded.caseContext.other });
      },
      onState: (state) => this.states.push(state),
      idleMs: IDLE_MS,
      retryMs: RETRY_MS,
    });
  }

  texts(sectionId: string): string[] {
    return this.writes
      .filter((write) => write.sectionId === sectionId)
      .map((write) => write.text);
  }

  get lastState(): SaveState | undefined {
    return this.states.at(-1);
  }
}

describe("typing", () => {
  test("several edits inside the idle window yield one write, of the last", async () => {
    const h = new Harness();
    h.autosave.schedule(CASE, prose("a"));
    h.autosave.schedule(CASE, prose("ab"));
    h.autosave.schedule(CASE, prose("abc"));
    await sleep(IDLE_MS * 3);
    expect(h.texts(CASE)).toEqual(["abc"]);
    expect(h.lastState).toBe("saved");
  });

  test("leaving the field writes at once, and the idle timer does not write again", async () => {
    const h = new Harness();
    h.autosave.schedule(CASE, prose("a"));
    h.autosave.flush(CASE);
    await sleep(1);
    expect(h.texts(CASE)).toEqual(["a"]);
    await sleep(IDLE_MS * 3);
    expect(h.texts(CASE)).toEqual(["a"]);
  });

  test("a section the server already holds is not rewritten", async () => {
    // The stored payload in the server's own shape: proto-JSON, as the page hands it over.
    const h = new Harness({
      [CASE]: JSON.stringify({ caseContext: { other: "a" } }),
    });
    await h.autosave.commit(CASE, prose("a"));
    h.autosave.flush(CASE);
    await sleep(1);
    expect(h.writes).toEqual([]);
    expect(h.states).toEqual([]);
  });
});

describe("a write that fails", () => {
  test("keeps 'saved' off screen while another section's write lands, until it lands itself", async () => {
    const h = new Harness();
    h.down.add(WORKFLOW);
    await h.autosave.commit(WORKFLOW, prose("scored"));
    expect(h.lastState).toBe("failed");

    await h.autosave.commit(ROUTING, prose("AD"));
    expect(h.texts(ROUTING)).toEqual(["AD"]);
    expect(h.lastState).toBe("failed");
    expect(h.states).not.toContain("saved");

    h.down.delete(WORKFLOW);
    await sleep(RETRY_MS * 3);
    expect(h.texts(WORKFLOW)).toEqual(["scored"]);
    expect(h.lastState).toBe("saved");
  });

  test("is saved again once the section is reverted to what the server holds", async () => {
    const h = new Harness({
      [CASE]: JSON.stringify({ caseContext: { other: "a" } }),
    });
    h.down.add(CASE);
    await h.autosave.commit(CASE, prose("ab"));
    expect(h.lastState).toBe("failed");
    await h.autosave.commit(CASE, prose("a"));
    expect(h.lastState).toBe("saved");
    // No retry left armed: nothing writes after the revert.
    await sleep(RETRY_MS * 3);
    expect(h.writes).toEqual([]);
  });

  test("is retried with the newest value, never the one that failed", async () => {
    const h = new Harness();
    h.down.add(CASE);
    await h.autosave.commit(CASE, prose("a"));
    h.autosave.schedule(CASE, prose("ab"));
    h.down.delete(CASE);
    await sleep(Math.max(IDLE_MS, RETRY_MS) * 3);
    expect(h.texts(CASE)).toEqual(["ab"]);
    expect(h.lastState).toBe("saved");
  });
});

describe("writes in flight", () => {
  test("to one section land in the order they were made", async () => {
    const h = new Harness();
    h.delays.push(30, 0);
    void h.autosave.commit(CASE, prose("a"));
    void h.autosave.commit(CASE, prose("ab"));
    await sleep(60);
    expect(h.texts(CASE)).toEqual(["a", "ab"]);
  });

  test("read as saving until every one has landed", async () => {
    const h = new Harness();
    h.delays.push(0, 30);
    void h.autosave.commit(WORKFLOW, prose("x"));
    void h.autosave.commit(ROUTING, prose("y"));
    await sleep(10);
    expect(h.texts(WORKFLOW)).toEqual(["x"]);
    expect(h.lastState).toBe("saving");
    await sleep(40);
    expect(h.lastState).toBe("saved");
  });
});

describe("settling", () => {
  test("lands every pending value before it resolves", async () => {
    const h = new Harness();
    h.autosave.schedule(CASE, prose("a"));
    h.autosave.schedule(WORKFLOW, prose("x"));
    await h.autosave.settle();
    expect(h.texts(CASE)).toEqual(["a"]);
    expect(h.texts(WORKFLOW)).toEqual(["x"]);
    expect(h.lastState).toBe("saved");
  });

  test("waits for a write already in flight", async () => {
    const h = new Harness();
    h.delays.push(30);
    void h.autosave.commit(CASE, prose("a"));
    await h.autosave.settle();
    expect(h.texts(CASE)).toEqual(["a"]);
  });

  test("rejects while a section is still failing, naming it, and leaves its retry armed", async () => {
    const h = new Harness();
    h.down.add(CASE);
    h.autosave.schedule(CASE, prose("a"));
    await expect(h.autosave.settle()).rejects.toThrow(/not saved: case/);
    expect(h.lastState).toBe("failed");
    h.down.delete(CASE);
    await sleep(RETRY_MS * 3);
    expect(h.texts(CASE)).toEqual(["a"]);
    expect(h.lastState).toBe("saved");
  });
});

test("disposal stops the timers", async () => {
  const h = new Harness();
  h.autosave.schedule(CASE, prose("a"));
  h.autosave.dispose();
  await sleep(IDLE_MS * 3);
  expect(h.writes).toEqual([]);
});
