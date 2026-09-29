import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import path from "node:path";
import { create, fromBinary, type MessageInitShape } from "@bufbuild/protobuf";
import { AnySchema } from "@bufbuild/protobuf/wkt";
import {
  CHANGED_SINCE_SHOWN,
  NO_TICKS,
  reduceTicks,
  type TickEvent,
  tickQueue,
} from "@/components/widgets/checklist-ticks";
import type { WidgetEdit } from "@/components/widgets/edit";
import { type Checklist, ChecklistSchema } from "@/models/widgets";
import { FixtureWorkspace } from "@/server/adapters/fixture/workspace";
import {
  AGENT,
  type SeedFiles,
} from "@/server/adapters/fixture/workspace-seed";
import { COLLABORATIVE_BRANCH, WorkspaceCopy } from "@/workspace-copy/copy";
import {
  type FixtureRemote,
  fixtureRemote,
} from "@/workspace-copy/fixture-remote.test-support";
import {
  directoryStorage,
  scratchDir,
} from "@/workspace-copy/git.test-support";
import { hydrate } from "@/workspace-copy/hydrate";
import { type EditOutcome, publishEdit } from "@/workspace-copy/publish";
import { writePayload } from "./asset";
import { setCheckedFile } from "./checklist-operation";

// A curator's tick through the write path, against the offline store: when the agent pushed between
// the page load and the click to another file, the tick lands on the agent's commit, so both
// changes land; when the agent's commit changed the checklist itself, the tick is not applied, since
// it would land on a checklist the curator never saw.

const ANALYSIS = "an_c4ec";
const AUTHORED = new Date(1_727_163_000_000);
const OPEN = { admit: async () => {} };
const CURATOR = { name: "curator@example.org", email: "curator@example.org" };
const DOCUMENT = "working_document.md";
const ASSET = "assets/curator-checks.binpb";

type Items = NonNullable<MessageInitShape<typeof ChecklistSchema>["items"]>;

function asset(items: Items): Uint8Array {
  return writePayload(ChecklistSchema, create(ChecklistSchema, { items }));
}

const FIRST: Items = [
  { id: "ps3", label: "Functional studies reviewed" },
  { id: "pm2", label: "Absent from controls" },
];

let scratch: ReturnType<typeof scratchDir>;
let copy: WorkspaceCopy;
let remote: FixtureRemote;

beforeEach(async () => {
  scratch = scratchDir("checklist");
  copy = await WorkspaceCopy.open(
    directoryStorage(path.join(scratch.dir, "copy.git")),
  );
  const store = new FixtureWorkspace();
  store.seedCommits(
    ANALYSIS,
    [
      {
        files: {
          [DOCUMENT]: `# Doc\n\n::embed[${ASSET}]\n`,
          [ASSET]: asset(FIRST),
        },
        author: AGENT,
        message: "write the checklist",
      },
    ],
    AUTHORED,
  );
  remote = fixtureRemote(store, ANALYSIS);
});

afterEach(() => scratch.remove());

async function tipBeforeTheAgentPushes(files: SeedFiles): Promise<string> {
  const state = await hydrate(copy, remote, OPEN);
  const base = state.refs.get(COLLABORATIVE_BRANCH);
  if (base === undefined) throw new Error("no tip");
  remote.store.agentPublishes(
    ANALYSIS,
    COLLABORATIVE_BRANCH,
    files,
    new Date(AUTHORED.getTime() + 60_000),
  );
  return base;
}

/** How a widget publishes its edit: against the store's tip, as the worker does. */
async function publishWidgetEdit(edit: WidgetEdit): Promise<EditOutcome> {
  return publishEdit(
    copy,
    remote,
    () => hydrate(copy, remote, OPEN),
    await hydrate(copy, remote, OPEN),
    {
      base: edit.base,
      curator: CURATOR,
      message: edit.message,
      files: new Map(edit.files.map((file) => [file.path, file.bytes])),
    },
  );
}

function copyAncestry(ancestor: string, descendant: string): Promise<boolean> {
  return copy.isAncestor(ancestor, descendant);
}

async function drawnAt(base: string) {
  const drawn = await copy.readFile(base, ASSET);
  if (drawn === undefined) throw new Error(`no ${ASSET} at ${base}`);
  return drawn;
}

/** A tick on `itemId` as the checklist was drawn at `base`, published against the store's tip. */
async function tick(base: string, itemId: string) {
  const drawn = await drawnAt(base);
  const edit = setCheckedFile(drawn.bytes, drawn.mode, {
    path: ASSET,
    itemId,
    checked: true,
  });
  if (edit === undefined) throw new Error("the tick changes nothing");
  return publishWidgetEdit({ base, ...edit });
}

async function tipChecklist(): Promise<Checklist> {
  const { document } = await remote.readRefDoc();
  const tip = document?.refs[COLLABORATIVE_BRANCH]?.target;
  if (tip?.case !== "oid") throw new Error("no tip");
  await hydrate(copy, remote, OPEN);
  return checklistAt(tip.value);
}

async function checklistAt(commit: string): Promise<Checklist> {
  const file = await copy.readFile(commit, ASSET);
  if (file === undefined) throw new Error(`no ${ASSET} at ${commit}`);
  return fromBinary(ChecklistSchema, fromBinary(AnySchema, file.bytes).value);
}

describe("a curator's tick", () => {
  test("drawn before the agent's push to another file lands on the agent's commit, keeping its change", async () => {
    const base = await tipBeforeTheAgentPushes({
      [DOCUMENT]: `# Doc, revised\n\n::embed[${ASSET}]\n`,
    });
    const outcome = await tick(base, "ps3");
    if (outcome.kind !== "landed")
      throw new Error(`not landed: ${outcome.kind}`);
    const commit = await copy.readCommit(outcome.commit);
    expect(commit.parents).not.toContain(base);
    const document = await copy.readFile(outcome.commit, DOCUMENT);
    expect(new TextDecoder().decode(document?.bytes)).toContain("revised");
    const landed = await checklistAt(outcome.commit);
    expect(landed.items.map((item) => [item.id, item.checked])).toEqual([
      ["ps3", true],
      ["pm2", false],
    ]);
  });

  test("on a checklist the agent's push changed is not applied, and lands nothing", async () => {
    const base = await tipBeforeTheAgentPushes({
      [ASSET]: asset([
        FIRST[0],
        { id: "pm2", label: "Absent from gnomAD v4 controls" },
      ]),
    });
    const outcome = await tick(base, "ps3");
    expect(outcome).toMatchObject({ kind: "fileChanged", path: ASSET });
    expect((await tipChecklist()).items.map((item) => item.checked)).toEqual([
      false,
      false,
    ]);
  });
});

describe("a tick in the widget", () => {
  test("on a checklist the agent changed since it was drawn asks for it again, and publishes nothing", async () => {
    const base = await tipBeforeTheAgentPushes({
      [ASSET]: asset([
        FIRST[0],
        { id: "pm2", label: "Absent from gnomAD v4 controls" },
      ]),
    });
    const before = await remote.readRefDoc();
    let state = NO_TICKS;
    await tickQueue(copyAncestry)(
      {
        asset: await drawnAt(base),
        path: ASSET,
        drawnAt: base,
        itemId: "ps3",
        checked: true,
      },
      publishWidgetEdit,
      (event) => {
        state = reduceTicks(state, event);
      },
    );
    expect(state.failures.get("ps3")).toBe(`Not saved: ${CHANGED_SINCE_SHOWN}`);
    expect(state.ticks.size).toBe(0);
    expect((await remote.readRefDoc()).generation).toBe(before.generation);
    expect((await tipChecklist()).items.map((item) => item.checked)).toEqual([
      false,
      false,
    ]);
  });

  test("made twice, on two items, before the widget redraws, lands both", async () => {
    const state = await hydrate(copy, remote, OPEN);
    const base = state.refs.get(COLLABORATIVE_BRANCH);
    if (base === undefined) throw new Error("no tip");
    const asset = await drawnAt(base);
    let ticks = NO_TICKS;
    const dispatch = (event: TickEvent) => {
      ticks = reduceTicks(ticks, event);
    };
    const queue = tickQueue(copyAncestry);
    const tick = (itemId: string) =>
      queue(
        { asset, path: ASSET, drawnAt: base, itemId, checked: true },
        publishWidgetEdit,
        dispatch,
      );
    await Promise.all([tick("ps3"), tick("pm2")]);
    expect(ticks.failures.size).toBe(0);
    expect((await tipChecklist()).items.map((item) => item.checked)).toEqual([
      true,
      true,
    ]);
  });

  test("made on the commit its own earlier tick landed as, while a later one publishes, lands too", async () => {
    const state = await hydrate(copy, remote, OPEN);
    const base = state.refs.get(COLLABORATIVE_BRANCH);
    if (base === undefined) throw new Error("no tip");
    let ticks = NO_TICKS;
    const landedAs = new Map<string, string>();
    const dispatch = (event: TickEvent) => {
      ticks = reduceTicks(ticks, event);
      if (event.kind === "landed") landedAs.set(event.itemId, event.commit);
    };
    const queue = tickQueue(copyAncestry);
    const asset = await drawnAt(base);
    const first = queue(
      { asset, path: ASSET, drawnAt: base, itemId: "ps3", checked: true },
      publishWidgetEdit,
      dispatch,
    );
    const second = queue(
      { asset, path: ASSET, drawnAt: base, itemId: "pm2", checked: true },
      publishWidgetEdit,
      dispatch,
    );
    await first;
    const redrawn = landedAs.get("ps3");
    if (redrawn === undefined) throw new Error("the first tick did not land");
    const third = queue(
      {
        asset: await drawnAt(redrawn),
        path: ASSET,
        drawnAt: redrawn,
        itemId: "pm2",
        checked: false,
      },
      publishWidgetEdit,
      dispatch,
    );
    await Promise.all([second, third]);
    expect(ticks.failures.size).toBe(0);
    expect((await tipChecklist()).items.map((item) => item.checked)).toEqual([
      true,
      false,
    ]);
  });

  test("queued behind one that lands, on an item the agent changed in between, is not applied", async () => {
    const state = await hydrate(copy, remote, OPEN);
    const base = state.refs.get(COLLABORATIVE_BRANCH);
    if (base === undefined) throw new Error("no tip");
    const drawn = await drawnAt(base);
    let ticks = NO_TICKS;
    const dispatch = (event: TickEvent) => {
      ticks = reduceTicks(ticks, event);
    };
    let publishes = 0;
    const publish = async (edit: WidgetEdit) => {
      const outcome = await publishWidgetEdit(edit);
      publishes += 1;
      if (publishes === 1) {
        remote.store.agentPublishes(
          ANALYSIS,
          COLLABORATIVE_BRANCH,
          {
            [ASSET]: asset([
              { ...FIRST[0], checked: true },
              { id: "pm2", label: "Present in controls" },
            ]),
          },
          new Date(AUTHORED.getTime() + 60_000),
        );
      }
      return outcome;
    };
    const queue = tickQueue(copyAncestry);
    const tick = (itemId: string) =>
      queue(
        { asset: drawn, path: ASSET, drawnAt: base, itemId, checked: true },
        publish,
        dispatch,
      );
    await Promise.all([tick("ps3"), tick("pm2")]);
    expect(ticks.failures.get("pm2")).toBe(`Not saved: ${CHANGED_SINCE_SHOWN}`);
    expect(
      (await tipChecklist()).items.map((item) => [item.label, item.checked]),
    ).toEqual([
      ["Functional studies reviewed", true],
      ["Present in controls", false],
    ]);
  });

  test("made on an agent's commit after its own tick landed is built on that commit as drawn", async () => {
    const state = await hydrate(copy, remote, OPEN);
    const base = state.refs.get(COLLABORATIVE_BRANCH);
    if (base === undefined) throw new Error("no tip");
    let ticks = NO_TICKS;
    const dispatch = (event: TickEvent) => {
      ticks = reduceTicks(ticks, event);
    };
    const queue = tickQueue(copyAncestry);
    await queue(
      {
        asset: await drawnAt(base),
        path: ASSET,
        drawnAt: base,
        itemId: "ps3",
        checked: true,
      },
      publishWidgetEdit,
      dispatch,
    );
    remote.store.agentPublishes(
      ANALYSIS,
      COLLABORATIVE_BRANCH,
      {
        [ASSET]: asset([
          { ...FIRST[0], checked: true },
          { id: "pm2", label: "Present in controls" },
        ]),
      },
      new Date(AUTHORED.getTime() + 60_000),
    );
    const agents = (await hydrate(copy, remote, OPEN)).refs.get(
      COLLABORATIVE_BRANCH,
    );
    if (agents === undefined) throw new Error("no tip");
    await queue(
      {
        asset: await drawnAt(agents),
        path: ASSET,
        drawnAt: agents,
        itemId: "pm2",
        checked: true,
      },
      publishWidgetEdit,
      dispatch,
    );
    expect(ticks.failures.size).toBe(0);
    expect(
      (await tipChecklist()).items.map((item) => [item.label, item.checked]),
    ).toEqual([
      ["Functional studies reviewed", true],
      ["Present in controls", true],
    ]);
  });

  test("drawn at the agent's commit its own tick was rebased over, is built on that tick's landing and lands", async () => {
    const base = await tipBeforeTheAgentPushes({
      [DOCUMENT]: `# Doc, revised\n\n::embed[${ASSET}]\n`,
    });
    const agents = (await hydrate(copy, remote, OPEN)).refs.get(
      COLLABORATIVE_BRANCH,
    );
    if (agents === undefined) throw new Error("no tip");
    let ticks = NO_TICKS;
    const dispatch = (event: TickEvent) => {
      ticks = reduceTicks(ticks, event);
    };
    const queue = tickQueue(copyAncestry);
    const tick = { path: ASSET, itemId: "ps3" };
    await queue(
      { ...tick, asset: await drawnAt(base), drawnAt: base, checked: true },
      publishWidgetEdit,
      dispatch,
    );
    // The agent's commit, drawn once the tick landed on it, does not hold the tick; the row shows it
    // saved, and the curator takes it back there.
    await queue(
      {
        ...tick,
        asset: await drawnAt(agents),
        drawnAt: agents,
        checked: false,
      },
      publishWidgetEdit,
      dispatch,
    );
    expect(ticks.failures.size).toBe(0);
    expect((await tipChecklist()).items.map((item) => item.checked)).toEqual([
      false,
      false,
    ]);
    const landed = ticks.ticks.get("ps3")?.landedAs;
    if (landed === undefined) throw new Error("the untick did not land");
    expect(await copy.isAncestor(agents, landed)).toBe(true);
  });
});
