import { describe, expect, test } from "bun:test";
import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { WorkspaceTipSchema } from "@/models/workbench";
import {
  documentState,
  followTip,
  nextFollowed,
  polledTip,
  signalFrom,
} from "./working-document";
import type { ReadTip, WorkingDocumentSignal } from "./workspace-sync";

// The working-document tab must not collapse its states into one: an Analysis with no repository
// yet, a repository whose tip has no document yet, a workspace too large to open, a damaged one, a
// Poll that has not answered, and one that could not read the workspace each say something
// different, decided from the tip and the copy alone.

const TIP = "9e27".padEnd(40, "0");
const LATER = "a1b2".padEnd(40, "0");
const AT_TIP: ReadTip = { kind: "commit", commit: TIP };
const READ = (data: string | null | undefined, error: Error | null = null) => ({
  isError: error !== null,
  error,
  data,
});
const named = (name: string) => Object.assign(new Error(name), { name });
const signal = (
  tip: ReadTip | null,
  unavailable = false,
  damaged = false,
): WorkingDocumentSignal => ({
  analysisId: "an_1",
  tip,
  pollFailed: false,
  unavailable,
  damaged,
});

describe("the working-document state", () => {
  test("is loading until the Poll answers", () => {
    expect(documentState(null, READ(undefined))).toEqual({ kind: "loading" });
  });

  test("with no commit on the branch, is an Analysis with no workspace repository yet", () => {
    expect(
      documentState(signal({ kind: "noCommit" }), READ(undefined)),
    ).toEqual({ kind: "noRepository" });
  });

  test("at a tip with no working document, is a document not written yet", () => {
    expect(documentState(signal(AT_TIP), READ(null))).toEqual({
      kind: "absent",
    });
  });

  test("at a tip, shows the document, or why it could not be read", () => {
    expect(documentState(signal(AT_TIP), READ("# Doc\n"))).toEqual({
      kind: "shown",
      markdown: "# Doc\n",
    });
    expect(documentState(signal(AT_TIP), READ(undefined))).toEqual({
      kind: "loading",
    });
    expect(
      documentState(
        signal(AT_TIP),
        READ(undefined, named("CopyTooLargeError")),
      ),
    ).toEqual({ kind: "tooLarge" });
    expect(
      documentState(signal(AT_TIP), READ(undefined, named("HydrationError"))),
    ).toEqual({ kind: "copyFailed" });
  });

  test("a copy that finds the repository damaged says so, not that a read failed", () => {
    expect(
      documentState(
        signal(AT_TIP),
        READ(undefined, named("WorkspaceDamagedError")),
      ),
    ).toEqual({ kind: "damaged" });
  });

  test("after a Poll that never answered, is a failed read, in every window", () => {
    expect(
      documentState(
        {
          analysisId: "an_1",
          tip: null,
          pollFailed: true,
          unavailable: false,
          damaged: false,
        },
        READ(undefined),
      ),
    ).toEqual({ kind: "failed" });
  });

  test("an unavailable workspace keeps what the tip read before shows", () => {
    expect(documentState(signal(AT_TIP, true), READ("# Doc\n"))).toEqual({
      kind: "shown",
      markdown: "# Doc\n",
    });
    expect(
      documentState(signal({ kind: "noCommit" }, true), READ(undefined)),
    ).toEqual({ kind: "noRepository" });
  });

  test("an unavailable workspace no tick has read is unavailable, not an absent repository", () => {
    expect(documentState(signal(null, true), READ(undefined))).toEqual({
      kind: "unavailable",
    });
  });

  test("a tick that finds the ref document damaged shows the workspace as damaged, not what the tip showed", () => {
    expect(documentState(signal(AT_TIP, false, true), READ("# Doc\n"))).toEqual(
      {
        kind: "damaged",
      },
    );
    expect(documentState(signal(null, false, true), READ(undefined))).toEqual({
      kind: "damaged",
    });
  });

  test("a signal with no tip that is neither failed nor unavailable is refused", () => {
    expect(() => documentState(signal(null), READ(undefined))).toThrow();
  });
});

describe("the tip a window follows", () => {
  const polled = (init: MessageInitShape<typeof WorkspaceTipSchema>) =>
    polledTip(create(WorkspaceTipSchema, init));

  test("is each tip a tick reads", () => {
    expect(
      followTip(AT_TIP, polled({ state: { case: "commit", value: LATER } })),
    ).toEqual({ kind: "commit", commit: LATER });
    expect(
      followTip(AT_TIP, polled({ state: { case: "noCommit", value: {} } })),
    ).toEqual({ kind: "noCommit" });
  });

  test("stays at the last tip read across ticks that cannot read one", () => {
    const unavailable = polled({ state: { case: "unavailable", value: {} } });
    expect(followTip(AT_TIP, unavailable)).toBe(AT_TIP);
    expect(followTip(null, unavailable)).toBeNull();
  });

  test("keeps its identity while the tick agrees with it", () => {
    expect(
      followTip(AT_TIP, polled({ state: { case: "commit", value: TIP } })),
    ).toBe(AT_TIP);
  });

  test("reads a damaged ref document as its own state", () => {
    expect(polled({ state: { case: "damaged", value: {} } })).toEqual({
      kind: "damaged",
    });
  });

  test("a Poll answered with no tip state is refused", () => {
    expect(() => polled({})).toThrow();
    expect(() => polledTip(undefined)).toThrow();
  });
});

describe("what a window follows across ticks", () => {
  const A = "an_a";
  const B = "an_b";
  const UNAVAILABLE = { kind: "unavailable" } as const;

  test("drops another Analysis's tip: B's first tick unavailable carries none of A's commit", () => {
    const onA = nextFollowed(null, A, AT_TIP);
    expect(onA).toEqual({ analysisId: A, tip: AT_TIP });
    expect(nextFollowed(onA, B, UNAVAILABLE)).toBeNull();
    expect(nextFollowed(onA, B, undefined)).toBeNull();
  });

  test("keeps the same tip, by identity, across an unavailable tick and back at the same commit", () => {
    const first = nextFollowed(null, A, AT_TIP);
    const unavailable = nextFollowed(first, A, UNAVAILABLE);
    expect(unavailable).toBe(first);
    const again = nextFollowed(unavailable, A, {
      kind: "commit",
      commit: TIP,
    });
    expect(again).toBe(first);
  });

  test("keeps the tip across a damaged tick, and moves on the next one read", () => {
    const first = nextFollowed(null, A, AT_TIP);
    expect(nextFollowed(first, A, { kind: "damaged" })).toBe(first);
    expect(nextFollowed(first, A, { kind: "commit", commit: LATER })).toEqual({
      analysisId: A,
      tip: { kind: "commit", commit: LATER },
    });
  });
});

describe("the signal from the Poll", () => {
  const ANSWERED = { isLoadingError: false, isRefetchError: false };

  test("is null until the Poll answers, and a failed read when its first answer failed", () => {
    expect(signalFrom("an_1", null, undefined, ANSWERED)).toBeNull();
    expect(
      signalFrom("an_1", null, undefined, {
        isLoadingError: true,
        isRefetchError: false,
      }),
    ).toMatchObject({ pollFailed: true });
  });

  test("says the workspace may be out of date when the Poll fails after answering", () => {
    const failing = signalFrom("an_1", AT_TIP, "commit", {
      isLoadingError: false,
      isRefetchError: true,
    });
    expect(failing).toMatchObject({ tip: AT_TIP, unavailable: true });
    if (failing === null) throw new Error("no signal");
    expect(documentState(failing, READ("# Doc\n"))).toEqual({
      kind: "shown",
      markdown: "# Doc\n",
    });
  });

  test("marks each tick's state: unavailable, damaged, or read", () => {
    expect(signalFrom("an_1", AT_TIP, "unavailable", ANSWERED)).toMatchObject({
      unavailable: true,
      damaged: false,
    });
    expect(signalFrom("an_1", AT_TIP, "damaged", ANSWERED)).toMatchObject({
      unavailable: false,
      damaged: true,
    });
    expect(signalFrom("an_1", AT_TIP, "commit", ANSWERED)).toMatchObject({
      unavailable: false,
      damaged: false,
    });
  });
});
