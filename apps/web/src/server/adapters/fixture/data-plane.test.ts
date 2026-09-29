import { describe, expect, test } from "bun:test";
import path from "node:path";
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
import { gitText, runGit, scratchDir } from "@/workspace-copy/git.test-support";
import { ResourceNotFoundError } from "../../errors";
import { COLLABORATIVE_BRANCH } from "../../workspace";
import { FixtureDataPlane } from "./data-plane";
import { FIXTURE_PROJECT, SECOND_FIXTURE_PROJECT } from "./membership";
import {
  documentMarkdown,
  FINAL_DOC_VERSION,
  SCRIPTED_STAGES,
} from "./timeline";
import { FixtureWorkspace } from "./workspace";
import { WORKING_DOCUMENT_PATH } from "./workspace-seed";

// The offline run ships the cards a curator can expand, so every one of them has to
// resolve; expanding one must not move the run it belongs to, since `GetThread` is a
// read and the fixture implements it like any other backend; and the states a fan-out
// passes through in one tick have to be reachable without racing the poll.

const cardsOf = (events: readonly ConversationEvent[]): SubAgent[] =>
  events.flatMap((event) =>
    event.kind.case === "subAgent" ? event.kind.value : [],
  );

/** A request that never goes away: these reads are not about cancellation. */
const NEVER = new AbortController().signal;

/** The collaborative branch's tip in `run`'s repository, or undefined before its first commit. */
async function tipOf(
  workspace: FixtureWorkspace,
  run: Analysis,
): Promise<string | undefined> {
  const snapshot = await workspace.readRefDoc(run, NEVER);
  const target = snapshot.document?.refs[COLLABORATIVE_BRANCH]?.target;
  return target?.case === "oid" ? target.value : undefined;
}

/** The working document at `commit` in `run`'s repository, read by `git` from the packs its ref
 *  document lists, as the browser's copy would read it. */
async function documentAt(
  workspace: FixtureWorkspace,
  run: Analysis,
  commit: string,
): Promise<string> {
  const scratch = scratchDir("fixture-document");
  try {
    const gitDir = path.join(scratch.dir, "repo.git");
    runGit(gitDir, ["init", "--bare", "--quiet", gitDir]);
    const { document } = await workspace.readRefDoc(run, NEVER);
    if (document === undefined) throw new Error(`${run.id} has no repository`);
    for (const packId of document.packs) {
      const pack = await workspace.servePack(run, packId);
      runGit(
        gitDir,
        ["index-pack", "--stdin"],
        new Uint8Array(await pack.arrayBuffer()),
      );
    }
    return gitText(gitDir, ["show", `${commit}:${WORKING_DOCUMENT_PATH}`]);
  } finally {
    scratch.remove();
  }
}

/** Whether `run`'s branch holds the script's final document at its tip. */
async function atFinalDocument(
  workspace: FixtureWorkspace,
  run: Analysis,
): Promise<boolean> {
  const tip = await tipOf(workspace, run);
  if (tip === undefined) return false;
  return (
    (await documentAt(workspace, run, tip)) ===
    documentMarkdown(run, FINAL_DOC_VERSION).trimEnd()
  );
}

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
    const data = new FixtureDataPlane(new FixtureWorkspace());
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
    const data = new FixtureDataPlane(new FixtureWorkspace());
    const [run] = await everyRun(data);
    await expect(data.getThread(run, "sthr_invented")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
  });

  test("reading a body advances nothing", async () => {
    const data = new FixtureDataPlane(new FixtureWorkspace());
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
    const data = new FixtureDataPlane(new FixtureWorkspace());
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
    const workspace = new FixtureWorkspace();
    const data = new FixtureDataPlane(workspace);
    const asJson = (response: PollResponse) =>
      JSON.stringify(toJson(PollResponseSchema, response));
    let released = 0;
    for (const run of await everyRun(data)) {
      const first = await data.pollEvents(run);
      const second = await data.pollEvents(run);
      // Held ⇔ polls change nothing — content, not length: a stage may only re-emit
      // ids — short of the final document. A finished run stalls too, but with the
      // corrected revision out.
      const held =
        asJson(first) === asJson(second) &&
        !(await atFinalDocument(workspace, run));
      if (!held) continue;
      released += 1;
      await data.steerAnalysis(run, "Say more about the frequency.");
      for (let tick = 0; tick < 2 * SCRIPTED_STAGES; tick += 1) {
        await data.pollEvents(run);
      }
      const resumed = await data.pollEvents(run);
      expect(resumed.events.length).toBeGreaterThan(second.events.length);
      expect(await atFinalDocument(workspace, run)).toBe(true);
    }
    expect(released).toBeGreaterThan(0);
  });
});

describe("the fixture agent's workspace pushes", () => {
  test("each document version the reveal releases is pushed as the branch's next tip, with its own body", async () => {
    const workspace = new FixtureWorkspace();
    const data = new FixtureDataPlane(workspace);
    const run = await data.createAnalysis({
      projectId: FIXTURE_PROJECT,
      inputs: create(AnalysisInputsSchema, {
        scenario: { case: "freeForm", value: { prompt: "Summarise PS3." } },
      }),
      userEmail: "user@localhost",
    });
    const tips: string[] = [];
    for (let tick = 0; tick < SCRIPTED_STAGES + 2; tick += 1) {
      await data.pollEvents(run);
      const tip = await tipOf(workspace, run);
      if (tip !== undefined && tips.at(-1) !== tip) tips.push(tip);
    }
    const bodies = await Promise.all(
      tips.map((tip) => documentAt(workspace, run, tip)),
    );
    expect(bodies).toEqual(
      Array.from({ length: FINAL_DOC_VERSION }, (_, i) =>
        documentMarkdown(run, i + 1).trimEnd(),
      ),
    );
  });
});
