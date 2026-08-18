import { describe, expect, test } from "bun:test";
import { create, toJson } from "@bufbuild/protobuf";
import {
  type Analysis,
  AnalysisInputsSchema,
  type ConversationEvent,
  type PollResponse,
  PollResponseSchema,
  type SubAgent,
  SubAgentStatus,
} from "@/models/workbench";
import { ResourceNotFoundError } from "../../errors";
import { FixtureDataPlane } from "./data-plane";
import { FIXTURE_PROJECT, SECOND_FIXTURE_PROJECT } from "./membership";
import { FINAL_DOC_VERSION, SCRIPTED_STAGES } from "./timeline";

// The offline run ships the cards a curator can expand, so every one of them has to
// resolve; expanding one must not move the run it belongs to, since `GetThread` is a
// read and the fixture implements it like any other backend; and the states a fan-out
// passes through in one tick have to be reachable without racing the poll.

const cardsOf = (events: readonly ConversationEvent[]): SubAgent[] =>
  events.flatMap((event) =>
    event.kind.case === "subAgent" ? event.kind.value : [],
  );

/** Every analysis the fixture seeds. */
function everyRun(data: FixtureDataPlane): Promise<Analysis[]> {
  return data.listAnalysesIn([FIXTURE_PROJECT, SECOND_FIXTURE_PROJECT]);
}

/** The cards a run shows after `ticks` polls. */
async function pollTo(
  data: FixtureDataPlane,
  run: Analysis,
  ticks: number,
): Promise<SubAgent[]> {
  let events: ConversationEvent[] = [];
  for (let tick = 0; tick < ticks; tick += 1) {
    events = (await data.pollEvents(run)).events;
  }
  return cardsOf(events);
}

describe("the fixture's spawned threads", () => {
  test("every card the run shows resolves to a body", async () => {
    // A card whose thread id did not resolve would 404 the moment a curator expanded
    // it, and nothing short of expanding it would show that.
    const data = new FixtureDataPlane();
    const runs = await everyRun(data);
    expect(runs.length).toBeGreaterThan(0);
    let seen = 0;
    for (const run of runs) {
      // Walk the whole reveal: a card exists in states no single tick shows.
      for (let tick = 0; tick < 12; tick += 1) {
        const { events } = await data.pollEvents(run);
        for (const card of cardsOf(events)) {
          seen += 1;
          await expect(
            data.getThread(run, card.threadId),
          ).resolves.toBeDefined();
        }
      }
    }
    expect(seen).toBeGreaterThan(0);
  });

  test("a thread the run never spawned is not-found", async () => {
    const data = new FixtureDataPlane();
    const [run] = await everyRun(data);
    await expect(data.getThread(run, "sthr_invented")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
  });

  test("reading a body advances nothing", async () => {
    const data = new FixtureDataPlane();
    const runs = await everyRun(data);
    const run = runs[runs.length - 1];
    const cards = await pollTo(data, run, 6);
    const card = cards.find((c) => c.prompt !== undefined);
    if (card === undefined) throw new Error("the run instructs no thread");

    const ids = async () =>
      (await data.getThread(run, card.threadId)).events.map((e) => e.id);
    const before = await ids();
    expect(before.length).toBeGreaterThan(0);
    for (let read = 0; read < 3; read += 1)
      await data.getThread(run, card.threadId);
    expect(await ids()).toEqual(before);
  });

  test("holds a fan-out at a state the script would pass through in one tick", async () => {
    const data = new FixtureDataPlane();
    const runs = await everyRun(data);
    // Read twice, several ticks apart: a run that advanced would have left the state.
    const held = await Promise.all(
      runs.map(async (run) => ({
        early: await pollTo(data, run, 4),
        late: await pollTo(data, run, 4),
      })),
    );
    const holds = (predicate: (cards: SubAgent[]) => boolean) =>
      held.some(({ early, late }) => predicate(early) && predicate(late));

    expect({
      // A thread created before its instruction landed — the card with nothing to name
      // it by.
      spawnedWithoutPrompt: holds(
        (cards) =>
          cards.length > 1 && cards.every((c) => c.prompt === undefined),
      ),
      // One sibling returned while the other still runs.
      partiallyReturned: holds(
        (cards) =>
          cards.some((c) => c.status === SubAgentStatus.RUNNING) &&
          cards.some((c) => c.status !== SubAgentStatus.RUNNING),
      ),
    }).toEqual({ spawnedWithoutPrompt: true, partiallyReturned: true });
  });

  test("a curator turn releases a held run, which then runs to its ending", async () => {
    // The hold is a display seed, not a frozen analysis: a run spoken to resumes. The
    // spliced turn alone grows the stream, so growth on the next poll proves nothing —
    // the run has to keep releasing its own stages, all the way to the document.
    const data = new FixtureDataPlane();
    const asJson = (response: PollResponse) =>
      JSON.stringify(toJson(PollResponseSchema, response));
    let released = 0;
    for (const run of await everyRun(data)) {
      const first = await data.pollEvents(run);
      const second = await data.pollEvents(run);
      // Held ⇔ polls change nothing — content, not length: a stage may only re-emit
      // ids — short of the final document version. A finished run stalls too, but
      // with the corrected revision out.
      const held =
        asJson(first) === asJson(second) &&
        second.workingDocumentVersion !== FINAL_DOC_VERSION;
      if (!held) continue;
      released += 1;
      await data.steerAnalysis(run, "Say more about the frequency.");
      for (let tick = 0; tick < 2 * SCRIPTED_STAGES; tick += 1) {
        await data.pollEvents(run);
      }
      const resumed = await data.pollEvents(run);
      expect(resumed.events.length).toBeGreaterThan(second.events.length);
      expect(resumed.workingDocumentVersion).toBe(FINAL_DOC_VERSION);
    }
    expect(released).toBeGreaterThan(0);
  });
});

describe("the fixture's document versions", () => {
  const inputs = () =>
    create(AnalysisInputsSchema, {
      scenario: { case: "freeForm", value: { prompt: "run the fixture" } },
    });

  async function freshRun(): Promise<{
    data: FixtureDataPlane;
    run: Analysis;
  }> {
    const data = new FixtureDataPlane();
    const run = await data.createAnalysis({
      inputs: inputs(),
      projectId: FIXTURE_PROJECT,
      userEmail: "curator@example.org",
    });
    return { data, run };
  }

  /** Poll until the reveal reaches `version`, bounded by the script's length. */
  async function pollToVersion(
    data: FixtureDataPlane,
    run: Analysis,
    version: number,
  ): Promise<void> {
    for (let tick = 0; tick < SCRIPTED_STAGES; tick += 1) {
      const poll = await data.pollEvents(run);
      if ((poll.workingDocumentVersion ?? 0) >= version) return;
    }
    throw new Error(`the script never revealed version ${version}`);
  }

  test("before any reveal: unversioned is not-produced; a named version is not-found", async () => {
    const { data, run } = await freshRun();
    expect((await data.getDocument(run.id)).document).toBeUndefined();
    await expect(data.getDocument(run.id, 1)).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
  });

  test("every revealed version stays fetchable, each with its own body", async () => {
    const { data, run } = await freshRun();
    await pollToVersion(data, run, FINAL_DOC_VERSION);
    expect((await data.getDocument(run.id)).document?.version).toBe(
      FINAL_DOC_VERSION,
    );
    const bodies = new Set<string>();
    for (let version = 1; version <= FINAL_DOC_VERSION; version += 1) {
      const res = await data.getDocument(run.id, version);
      expect(res.document?.version).toBe(version);
      expect(res.document?.markdown).toBeTruthy();
      bodies.add(res.document?.markdown ?? "");
    }
    expect(bodies.size).toBe(FINAL_DOC_VERSION);
  });

  test("mid-run, the revealed draft serves while the revision is still not-found", async () => {
    const { data, run } = await freshRun();
    await pollToVersion(data, run, 1);
    expect((await data.getDocument(run.id, 1)).document?.version).toBe(1);
    expect((await data.getDocument(run.id)).document?.version).toBe(1);
    await expect(
      data.getDocument(run.id, FINAL_DOC_VERSION),
    ).rejects.toBeInstanceOf(ResourceNotFoundError);
  });

  test.each([0, -1])("version %i is not-found", async (version) => {
    const { data, run } = await freshRun();
    await pollToVersion(data, run, FINAL_DOC_VERSION);
    await expect(data.getDocument(run.id, version)).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
  });

  test("a version beyond the reveal is not-found", async () => {
    const { data, run } = await freshRun();
    await pollToVersion(data, run, FINAL_DOC_VERSION);
    await expect(
      data.getDocument(run.id, FINAL_DOC_VERSION + 1),
    ).rejects.toBeInstanceOf(ResourceNotFoundError);
  });
});
