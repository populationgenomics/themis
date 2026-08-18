import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { timestampDate } from "@bufbuild/protobuf/wkt";
import {
  type Analysis,
  AnalysisInputsSchema,
  AnalysisSchema,
  type ConversationEvent,
  type DiffLine,
  DiffLineKind,
  SubAgentStatus,
  type ToolCall,
  ToolLanguage,
  ToolLanguageSchema,
} from "@/models/workbench";
import {
  afterPoll,
  awaitingToolResult,
  documentMarkdown,
  FINAL_DOC_VERSION,
  initialRunState,
  interrupted,
  type RunState,
  SCRIPTED_STAGES,
  steered,
  threadTimeline,
  timelineAt,
} from "./timeline";

// The offline run is a pure function of its state, and a curator turn splices stages
// into it. The properties below are what let the client reconcile by id across a poll
// sequence that a turn has grown mid-session, and what keeps the run the one place every
// rendering path is exercised offline.

const ANALYSIS: Analysis = create(AnalysisSchema, {
  id: "an_1",
  sessionId: "sess_1",
  projectId: "proj_fixture",
  inputs: create(AnalysisInputsSchema, {
    scenario: { case: "freeForm", value: { prompt: "do the thing" } },
  }),
});

/** The state after `n` polls of an untouched run. */
function afterPolls(n: number): RunState {
  let state = initialRunState();
  for (let i = 0; i < n; i += 1) state = afterPoll(state);
  return state;
}

const ids = (events: readonly ConversationEvent[]) => events.map((e) => e.id);

const instants = (events: readonly ConversationEvent[]) =>
  events.map((e) =>
    e.occurredAt ? timestampDate(e.occurredAt).toISOString() : null,
  );

const toolCall = (event: ConversationEvent): ToolCall | null =>
  event.kind.case === "tool" ? event.kind.value : null;

const toolCalls = (events: readonly ConversationEvent[]): ToolCall[] =>
  events.flatMap((event) => toolCall(event) ?? []);

/** The two sides of a replacement, read back off the kinded lines the run ships. */
function replacementSides(diff: readonly DiffLine[]): {
  before: string;
  after: string;
} {
  const side = (kind: DiffLineKind) =>
    diff
      .filter(
        (line) => line.kind === kind || line.kind === DiffLineKind.CONTEXT,
      )
      .map((line) => line.text)
      .join("\n");
  return {
    before: side(DiffLineKind.REMOVED),
    after: side(DiffLineKind.ADDED),
  };
}

describe("the scripted run", () => {
  test("reveals more of itself on every poll, then settles", () => {
    // Derived from the script's own length, so adding a stage does not edit this test.
    const counts = Array.from(
      { length: SCRIPTED_STAGES + 2 },
      (_, n) => timelineAt(ANALYSIS, afterPolls(n)).events.length,
    );
    expect(counts[0]).toBe(0);
    for (let n = 1; n <= SCRIPTED_STAGES; n += 1) {
      // A stage may resolve a call an earlier one left in flight rather than adding an
      // event, so the count never falls and the run as a whole grows.
      expect(counts[n]).toBeGreaterThanOrEqual(counts[n - 1]);
    }
    expect(counts[SCRIPTED_STAGES]).toBeGreaterThan(counts[0]);
    // Past the end, the reveal is fixed.
    expect(counts[SCRIPTED_STAGES + 1]).toBe(counts[SCRIPTED_STAGES]);
  });

  test("reveals its document versions in order: none, the draft, the revision", () => {
    const versions = Array.from(
      { length: SCRIPTED_STAGES + 1 },
      (_, n) => timelineAt(ANALYSIS, afterPolls(n)).documentVersion,
    );
    expect(versions[0]).toBe(0);
    for (let n = 1; n <= SCRIPTED_STAGES; n += 1) {
      expect(versions[n]).toBeGreaterThanOrEqual(versions[n - 1]);
    }
    // The draft is a version of its own, revealed before the run ends, so the picker
    // has history to show while the run still works.
    expect(versions.slice(0, -1)).toContain(1);
    expect(versions[SCRIPTED_STAGES]).toBe(FINAL_DOC_VERSION);
    expect(FINAL_DOC_VERSION).toBeGreaterThan(1);
  });

  test("serves non-empty, pairwise-distinct markdown for every produced version", () => {
    const bodies = Array.from({ length: FINAL_DOC_VERSION }, (_, i) =>
      documentMarkdown(ANALYSIS, i + 1),
    );
    for (const body of bodies) {
      expect(body.length).toBeGreaterThan(0);
    }
    expect(new Set(bodies).size).toBe(bodies.length);
  });

  test.each([0, -1, 1.5, FINAL_DOC_VERSION + 1])(
    "version %p is one the run never produces",
    (version) => {
      expect(() => documentMarkdown(ANALYSIS, version)).toThrow(
        "no such document version",
      );
    },
  );

  test("every event carries a distinct id and an ordering instant", () => {
    const { events } = timelineAt(ANALYSIS, afterPolls(SCRIPTED_STAGES));
    expect(new Set(ids(events)).size).toBe(events.length);
    expect(instants(events).every((instant) => instant !== null)).toBe(true);
  });

  test("serves the document its own edit produced", () => {
    // The conversation shows the replacement and the pane serves the result; read one
    // off the other, so a fixture that drifted would show an edit the document lacks.
    const calls = toolCalls(
      timelineAt(ANALYSIS, afterPolls(SCRIPTED_STAGES)).events,
    );
    const edit = calls.find((call) => call.diff.length > 0);
    if (edit === undefined) throw new Error("the run makes no edit");
    const { before, after } = replacementSides(edit.diff);
    expect(before).not.toBe(after);

    const drafted = calls.find((call) => call.command.includes(before));
    if (drafted === undefined) {
      throw new Error("no write drafted the text the edit replaces");
    }
    const served = documentMarkdown(ANALYSIS, FINAL_DOC_VERSION);
    // The drafted text is itself the first version the pane can pin...
    expect(drafted.command).toBe(documentMarkdown(ANALYSIS, 1));
    expect(drafted.command).not.toBe(served);
    // ...and the edit applied to it is the final one.
    expect(drafted.command.split(before).join(after)).toBe(served);
  });

  test("resolves a call in place across a reveal — same id, same position", () => {
    const ticks = Array.from(
      { length: SCRIPTED_STAGES + 1 },
      (_, n) => timelineAt(ANALYSIS, afterPolls(n)).events,
    );
    // A reveal only extends the stream, which is what makes "the same position" mean
    // anything: nothing already shown moves.
    for (let n = 0; n < SCRIPTED_STAGES; n += 1) {
      expect(ids(ticks[n + 1]).slice(0, ticks[n].length)).toEqual(
        ids(ticks[n]),
      );
    }
    // ...so a call left in flight comes back carrying its result rather than as a second
    // event, which is the replace-by-id reconciliation the client does.
    const resolvedInPlace = ticks.slice(0, -1).some((events, n) =>
      events.some((event, index) => {
        const call = toolCall(event);
        const later = ticks[n + 1][index];
        return (
          call !== null &&
          call.result === undefined &&
          later.id === event.id &&
          toolCall(later)?.result !== undefined
        );
      }),
    );
    expect(resolvedInPlace).toBe(true);
  });

  test("leaves no call unresolved once it ends", () => {
    const calls = toolCalls(
      timelineAt(ANALYSIS, afterPolls(SCRIPTED_STAGES)).events,
    );
    expect(calls.length).toBeGreaterThan(0);
    expect(
      calls
        .filter((call) => call.result === undefined)
        .map((call) => call.intent),
    ).toEqual([]);
  });

  test("exercises every way a tool call renders", () => {
    // The run is the whole of the offline surface, so a path no call takes is a path
    // neither a capture nor a manual pass ever shows.
    const calls = toolCalls(
      timelineAt(ANALYSIS, afterPolls(SCRIPTED_STAGES)).events,
    );
    const covers = (predicate: (call: ToolCall) => boolean) =>
      calls.some(predicate);
    expect({
      replacement: covers((call) => call.diff.length > 0),
      failure: covers((call) => call.result?.isError === true),
    }).toEqual({ replacement: true, failure: true });

    // Read off the generated descriptor: a language added to the proto is a rendering the
    // run does not show until some step of it writes one, or it is named here as one no
    // step ever will. UNSPECIFIED is covered by the heredoc the run splices.
    const unwritten = new Set<number>([ToolLanguage.TYPESCRIPT]);
    const unshown = ToolLanguageSchema.values
      .filter((value) => !unwritten.has(value.number))
      .filter(
        (value) =>
          !covers(
            (call) => call.command !== "" && call.language === value.number,
          ),
      );
    expect(unshown.map((value) => value.name)).toEqual([]);
  });
});

describe("the run's fan-out", () => {
  const cards = (events: readonly ConversationEvent[]) =>
    events.flatMap((event) =>
      event.kind.case === "subAgent" ? event.kind.value : [],
    );

  test("delegates to more than one thread, and every one of them returns", () => {
    // A fan-out of one would leave sibling spacing and a partial return unreachable
    // offline, and a thread left running past the ending would contradict the close.
    const finished = cards(
      timelineAt(ANALYSIS, afterPolls(SCRIPTED_STAGES)).events,
    );
    expect(finished.length).toBeGreaterThan(1);
    expect(finished.filter((card) => (card.summary ?? "") === "")).toEqual([]);
    // Both settled states are shown: a thread that yielded, and one that terminated.
    expect(new Set(finished.map((card) => card.status)).size).toBeGreaterThan(
      1,
    );
  });

  test("opens each body on the instruction its own card states", () => {
    // The card is what a curator read before expanding; a body that opened on some
    // other text would make the fan-out and the thread two accounts of one delegation.
    const state = afterPolls(SCRIPTED_STAGES);
    for (const card of cards(timelineAt(ANALYSIS, state).events)) {
      const body = threadTimeline(ANALYSIS, state, card.threadId);
      if (body === null) throw new Error(`no body for ${card.threadId}`);
      if (card.prompt === undefined) {
        throw new Error(`card ${card.threadId} states no prompt`);
      }
      const [first] = body;
      expect(first.kind.case === "user" && first.kind.value.text).toBe(
        card.prompt,
      );
      // A body is flat: one level of delegation is all the runtime allows.
      expect(cards(body)).toEqual([]);
    }
  });

  test("grows a body only as the thread it belongs to advances", () => {
    const threadId = cards(
      timelineAt(ANALYSIS, afterPolls(SCRIPTED_STAGES)).events,
    )[0].threadId;
    const lengths = Array.from(
      { length: SCRIPTED_STAGES + 1 },
      (_, n) => threadTimeline(ANALYSIS, afterPolls(n), threadId)?.length ?? 0,
    );
    for (let n = 1; n < lengths.length; n += 1) {
      expect(lengths[n]).toBeGreaterThanOrEqual(lengths[n - 1]);
    }
    expect(lengths[lengths.length - 1]).toBeGreaterThan(0);
  });

  test("has no body for a thread it never spawned", () => {
    expect(
      threadTimeline(ANALYSIS, afterPolls(SCRIPTED_STAGES), "sthr_invented"),
    ).toBeNull();
  });

  test("never shows a card without a named status", () => {
    // The client degrades a zero status rather than throwing, so the run must never
    // produce one.
    for (let n = 0; n <= SCRIPTED_STAGES; n += 1) {
      for (const card of cards(timelineAt(ANALYSIS, afterPolls(n)).events)) {
        expect(card.status).not.toBe(SubAgentStatus.UNSPECIFIED);
      }
    }
  });
});

describe("a curator turn joining the run", () => {
  test("is released at once, and the agent takes it up on the next tick", () => {
    const mid = afterPolls(1);
    const steeredState = steered(mid, "Treat the exon as clinically relevant.");

    const turn = timelineAt(ANALYSIS, steeredState).events;
    const before = timelineAt(ANALYSIS, mid).events;
    expect(turn.length).toBe(before.length + 1);
    const last = turn[turn.length - 1];
    expect(last.kind.case).toBe("user");
    expect(last.kind.case === "user" && last.kind.value.text).toBe(
      "Treat the exon as clinically relevant.",
    );

    // The reply is the next stage, not the same one.
    const next = timelineAt(ANALYSIS, afterPoll(steeredState)).events;
    expect(next.length).toBeGreaterThan(turn.length);
    expect(next.slice(turn.length).map((e) => e.kind.case)).toContain(
      "assistant",
    );
  });

  test("stays where it was made — the rest of the script still follows it", () => {
    const steeredState = steered(afterPolls(1), "Answer: Most");
    let state = steeredState;
    for (let i = 0; i < 8; i += 1) state = afterPoll(state);

    const finished = timelineAt(ANALYSIS, state).events;
    const scripted = timelineAt(ANALYSIS, afterPolls(SCRIPTED_STAGES)).events;
    // Nothing the script would have emitted is lost.
    for (const id of ids(scripted)) expect(ids(finished)).toContain(id);
    // ...and the turn sits inside it, not appended past the ending.
    const turnIndex = ids(finished).indexOf("ev-steer-1");
    expect(turnIndex).toBeGreaterThan(0);
    expect(turnIndex).toBeLessThan(finished.length - 1);
  });

  test("never moves an event a curator has already been shown", () => {
    // Ids are the client's reconciliation key and the instants are the ordering key, so
    // a splice that renumbered either would make the poll re-render settled turns.
    const mid = afterPolls(2);
    const before = timelineAt(ANALYSIS, mid).events;
    const after = timelineAt(ANALYSIS, steered(mid, "Answer: Most")).events;

    expect(ids(after).slice(0, before.length)).toEqual(ids(before));
    expect(instants(after).slice(0, before.length)).toEqual(instants(before));
  });

  test("two turns made between polls are answered in the order they were made", () => {
    // Not just the turns: the replies too. A splice that landed the second turn inside
    // the first turn's block would have the agent answer the newer direction first.
    const first = steered(afterPolls(1), "first");
    const second = steered(first, "second");
    let state = second;
    for (let i = 0; i < 8; i += 1) state = afterPoll(state);

    const shown = ids(timelineAt(ANALYSIS, state).events);
    const order = [
      "ev-steer-1",
      "ev-steer-1-ack",
      "ev-steer-2",
      "ev-steer-2-ack",
    ].map((id) => shown.indexOf(id));
    expect(order).not.toContain(-1);
    expect(order).toEqual([...order].sort((a, b) => a - b));
  });

  test("never precedes the kickoff, even sent before the first poll", () => {
    // A curator cannot answer a run that has not started, so a turn taken at reveal
    // zero still lands behind the kickoff rather than opening the conversation.
    const shown = ids(
      timelineAt(ANALYSIS, steered(initialRunState(), "Answer: Most")).events,
    );
    expect(shown.indexOf("ev-kickoff")).toBe(0);
    expect(shown.indexOf("ev-steer-1")).toBeGreaterThan(0);
  });

  test("does not clear a document the run already produced", () => {
    const finished = afterPolls(SCRIPTED_STAGES);
    const produced = timelineAt(ANALYSIS, finished).documentVersion;
    expect(produced).toBeGreaterThan(0);
    expect(
      timelineAt(ANALYSIS, steered(finished, "One more thing")).documentVersion,
    ).toBe(produced);
  });
});

describe("the run mid-step and halted", () => {
  test("the mid-step window opens with the edit in flight and closes with its result", () => {
    expect(awaitingToolResult(ANALYSIS, afterPolls(6))).toBe(false);
    expect(awaitingToolResult(ANALYSIS, afterPolls(7))).toBe(true);
    expect(awaitingToolResult(ANALYSIS, afterPolls(8))).toBe(false);
  });

  test("an interrupt closes the in-flight edit with an error result and ends the script", () => {
    const halted = interrupted(afterPolls(7));
    const edit = timelineAt(ANALYSIS, halted).events.find(
      (event) => event.id === "ev-edit",
    );
    if (edit?.kind.case !== "tool") throw new Error("expected the edit call");
    expect(edit.kind.value.result?.isError).toBe(true);
    expect(awaitingToolResult(ANALYSIS, halted)).toBe(false);

    // The script past the halt never plays, however long the poll runs on.
    let state = halted;
    for (let i = 0; i < 8; i += 1) state = afterPoll(state);
    const settled = timelineAt(ANALYSIS, state);
    expect(ids(settled.events)).not.toContain("ev-close");
    // The draft the write landed survives the halt; the corrected revision never does.
    expect(settled.documentVersion).toBe(1);
  });

  test("an interrupt between steps halts the script where it is", () => {
    // No call in flight, but the run is not settled: the halt lands at the frontier
    // — the resolved write keeps its real result, and the rest of the script never
    // plays. The one no-op state is a settled run, as the live API treats idle.
    const halted = interrupted(afterPolls(2));
    let state = halted;
    for (let i = 0; i < 8; i += 1) state = afterPoll(state);
    const settled = timelineAt(ANALYSIS, state);
    expect(ids(settled.events)).not.toContain("ev-edit");
    expect(ids(settled.events)).not.toContain("ev-close");
    // The revealed write already produced the draft; the halt keeps it.
    expect(settled.documentVersion).toBe(1);
    const write = settled.events.find((event) => event.id === "ev-write");
    if (write?.kind.case !== "tool") throw new Error("expected the write call");
    expect(write.kind.value.result?.isError).toBeFalsy();
  });

  test("an interrupt on a settled run changes nothing", () => {
    const settled = afterPolls(SCRIPTED_STAGES);
    expect(interrupted(settled)).toEqual(settled);
    expect(
      timelineAt(ANALYSIS, interrupted(settled)).documentVersion,
    ).toBeGreaterThan(0);
  });

  test("an interrupt before the kickoff reveals changes nothing", () => {
    // A run that has not shown its kickoff has nothing to halt, and the kickoff (the
    // curator's own creation input) is not an interrupt's to erase.
    expect(interrupted(initialRunState())).toEqual(initialRunState());
  });

  test("a second halt never resurrects what the first cancelled", () => {
    // The closing error stage is not a script index; a re-halt that recounted it as
    // one would swap the closed call back to its resolved script twin — rewriting
    // events a curator was already shown.
    const halted = interrupted(afterPolls(7));
    const spoken = steered(halted, "Stop meant stop.");
    const before = timelineAt(ANALYSIS, spoken).events;
    const again = interrupted(spoken);
    const after = timelineAt(ANALYSIS, again).events;

    expect(ids(after).slice(0, before.length)).toEqual(ids(before));
    expect(instants(after).slice(0, before.length)).toEqual(instants(before));
    const edit = after.find((event) => event.id === "ev-edit");
    if (edit?.kind.case !== "tool") throw new Error("expected the edit call");
    expect(edit.kind.value.result?.isError).toBe(true);
    expect(ids(after)).not.toContain("ev-close");
  });

  test("a halt does not fabricate the uptake of a pending turn", () => {
    // Live processes a queued message only after the interrupt idles the session, so
    // the agent's uptake plays on a later poll — a stop press must not make the
    // agent visibly do work.
    const spoken = steered(afterPolls(1), "One more thing");
    const halted = interrupted(spoken);
    expect(ids(timelineAt(ANALYSIS, halted).events)).not.toContain(
      "ev-steer-1-ack",
    );
    expect(ids(timelineAt(ANALYSIS, afterPoll(halted)).events)).toContain(
      "ev-steer-1-ack",
    );
  });
});
