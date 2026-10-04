import { describe, expect, test } from "bun:test";
import { create, toBinary } from "@bufbuild/protobuf";
import { AnySchema, anyPack } from "@bufbuild/protobuf/wkt";
import {
  type Checklist_Item,
  Checklist_ItemSchema,
  ChecklistSchema,
  Svcv4ClassificationSchema,
} from "@/models/widgets";
import { writePayload } from "@/widgets/asset";
import {
  AddressError,
  addressName,
  type GuardAddress,
  judgementIn,
  judgementInAsset,
} from "@/widgets/guard-operation";
import { fbn1Classification } from "@/widgets/svcv4-classification-fixture";
import {
  CopyRequestError,
  CopyWorkerLostError,
  PublishUnconfirmedError,
} from "@/workspace-copy/client";
import {
  PUBLISH_DAMAGED_AFTER_UNKNOWN,
  WORKSPACE_DAMAGED,
} from "@/workspace-copy/protocol";
import type { EditOutcome } from "@/workspace-copy/publish";
import { PublishOutcomeUnknownError } from "@/workspace-copy/publish";
import type { WidgetEdit } from "./edit";
import { outcomeUnknown } from "./edit";
import {
  ALREADY_SO,
  type Ancestry,
  DAMAGED_NOTICE,
  DAMAGED_UNCONFIRMED_NOTICE,
  type GuardInFile,
  guardView,
  type JudgementEvent,
  type Judgements,
  judgementQueue,
  judgementStore,
  NO_JUDGEMENTS,
  reduceJudgements,
  UNCONFIRMED_NOTICE,
  unansweredReason,
} from "./judgements";

/** Where item `id`'s tick sits in a checklist. */
function checkedOf(id: string): GuardAddress {
  return { steps: [{ field: "items", key: id }], guard: "checked" };
}

const PS3_KEY = addressName(checkedOf("ps3"));

/** An item's tick as a checklist holding the item holds it. */
function inFile(item: Checklist_Item): GuardInFile {
  const checklist = create(ChecklistSchema, { items: [item] });
  const found = judgementIn(ChecklistSchema, checklist, checkedOf(item.id));
  if (found === undefined) throw new Error(`no item ${item.id}`);
  return { key: addressName(checkedOf(item.id)), ...found };
}

// Every path a tick takes, from the click to the asset that records it: what the item shows over
// the asset drawn at each commit along the way, and what it says when the tick did not land.

// OLD ← BASE ← AGENT ← LANDED ← LATER, and SIDE on a line of its own from OLD.
const OLD = "0dd0".padEnd(40, "0");
const BASE = "c41e".padEnd(40, "0");
const AGENT = "d8f0".padEnd(40, "0");
const LANDED = "9e27".padEnd(40, "0");
const LATER = "a1b2".padEnd(40, "0");
const SIDE = "5e1d".padEnd(40, "0");
const PARENTS: Record<string, string[]> = {
  [OLD]: [],
  [BASE]: [OLD],
  [AGENT]: [BASE],
  [LANDED]: [AGENT],
  [LATER]: [LANDED],
  [SIDE]: [OLD],
};

function reaches(ancestor: string, descendant: string): boolean {
  if (ancestor === descendant) return true;
  const parents = PARENTS[descendant];
  if (parents === undefined) throw new Error(`no commit ${descendant}`);
  return parents.some((parent) => reaches(ancestor, parent));
}

const ancestry: Ancestry = async (ancestor, descendant) =>
  reaches(ancestor, descendant);

function run(...events: JudgementEvent[]): Judgements {
  return events.reduce(reduceJudgements, NO_JUDGEMENTS);
}

/** What `item` shows at `drawnAt` once the copy has answered every question the view asks. */
function answered(state: Judgements, item: Checklist_Item, drawnAt: string) {
  let at = state;
  for (;;) {
    const view = guardView(at, inFile(item), drawnAt);
    if (view.asks.length === 0) return view;
    at = view.asks.reduce(
      (next, [ancestor, descendant]) =>
        reduceJudgements(next, {
          kind: "related",
          ancestor,
          descendant,
          isAncestor: reaches(ancestor, descendant),
        }),
      at,
    );
  }
}

const PS3 = create(Checklist_ItemSchema, { id: "ps3", label: "one" });
const PS3_TICKED = create(Checklist_ItemSchema, {
  id: "ps3",
  label: "one",
  checked: true,
});
const PS3_REWORDED = create(Checklist_ItemSchema, { id: "ps3", label: "none" });
const PM2 = create(Checklist_ItemSchema, { id: "pm2", label: "two" });

const TICKED: Extract<JudgementEvent, { kind: "made" }> = {
  kind: "made",
  key: PS3_KEY,
  seq: 1,
  value: true,
  drawnAt: BASE,
  judged: inFile(PS3).judged,
};
const LANDED_AT: JudgementEvent = {
  kind: "landed",
  key: PS3_KEY,
  seq: 1,
  commit: LANDED,
};

describe("a tick being saved", () => {
  test("shows over the commit it was made on and every one after it", () => {
    const saving = run(TICKED);
    for (const at of [BASE, AGENT, LATER]) {
      expect(answered(saving, PS3, at)).toMatchObject({
        value: true,
        marker: "saving",
      });
    }
  });

  test("shows over no commit before it, or on another line", () => {
    const saving = run(TICKED);
    for (const at of [OLD, SIDE]) {
      expect(answered(saving, PS3, at)).toMatchObject({
        value: false,
        marker: undefined,
      });
    }
  });

  test("shows over no item the agent reworded since", () => {
    expect(answered(run(TICKED), PS3_REWORDED, AGENT)).toMatchObject({
      value: false,
      marker: undefined,
    });
  });

  test("shows, and takes no new tick, while the copy has not said where the commit stands", () => {
    const view = guardView(run(TICKED), inFile(PS3), AGENT);
    expect(view).toMatchObject({
      value: true,
      marker: "saving",
      undecided: true,
    });
    expect(view.asks).toEqual([[BASE, AGENT]]);
  });

  test("on one item leaves every other item to its asset", () => {
    expect(answered(run(TICKED), PM2, BASE)).toMatchObject({
      value: false,
      marker: undefined,
    });
  });
});

describe("a tick that landed", () => {
  test("shows as saved over the commit it was made on and every one after it up to its landing", () => {
    const saved = run(TICKED, LANDED_AT);
    for (const at of [BASE, AGENT]) {
      expect(answered(saved, PS3, at)).toMatchObject({
        value: true,
        marker: "saved",
      });
    }
  });

  test("gives way to the file over its landing and after, marked saved while the file holds it", () => {
    const saved = run(TICKED, LANDED_AT);
    for (const at of [LANDED, LATER]) {
      expect(answered(saved, PS3_TICKED, at)).toEqual({
        value: true,
        marker: "saved",
        undecided: false,
        asks: [],
      });
    }
  });

  test("goes once a commit after its landing no longer holds it", () => {
    const saved = run(TICKED, LANDED_AT);
    for (const item of [PS3, PS3_REWORDED]) {
      const view = answered(saved, item, LATER);
      expect(view).toMatchObject({ value: false, marker: undefined });
      expect(view.superseded).toEqual({ key: PS3_KEY, seq: 1 });
    }
    const gone = reduceJudgements(saved, {
      kind: "superseded",
      key: PS3_KEY,
      seq: 1,
    });
    expect(gone.judgements.size).toBe(0);
  });

  test("shows over the commit it was made on without asking the copy", () => {
    expect(guardView(run(TICKED, LANDED_AT), inFile(PS3), BASE)).toEqual({
      value: true,
      marker: "saved",
      undecided: false,
      asks: [],
    });
  });

  test("shows over no commit before the one it was made on", () => {
    expect(answered(run(TICKED, LANDED_AT), PS3, OLD)).toMatchObject({
      value: false,
      marker: undefined,
    });
  });

  test("is put back where the tick that replaced it was refused, or its outcome is unknown, and no longer says it saved", () => {
    const untick: JudgementEvent = {
      ...TICKED,
      seq: 2,
      value: false,
      drawnAt: AGENT,
    };
    for (const outcome of [
      { kind: "refused", key: PS3_KEY, seq: 2, reason: "changed" },
      { kind: "unconfirmed", key: PS3_KEY, seq: 2 },
    ] as const) {
      const state = run(TICKED, LANDED_AT, untick, outcome);
      for (const [item, at] of [
        [PS3, AGENT],
        [PS3_TICKED, LATER],
      ] as const) {
        expect(answered(state, item, at)).toMatchObject({
          value: true,
          marker: undefined,
        });
      }
      expect(state.failures.has(PS3_KEY)).toBe(true);
    }
  });

  test("still saving beside a notice on its item is a fault", () => {
    const saving = run(TICKED);
    const faulty = {
      ...saving,
      failures: new Map([[PS3_KEY, "Not saved: changed"]]),
    };
    expect(() => guardView(faulty, inFile(PS3), BASE)).toThrow(
      "while a judgement on it is saving",
    );
  });

  test("that followed a refused one shows only as saved", () => {
    const refusal: JudgementEvent = {
      kind: "refused",
      key: PS3_KEY,
      seq: 1,
      reason: "changed",
    };
    expect(run(TICKED, refusal).failures.has(PS3_KEY)).toBe(true);
    const landed = run(
      TICKED,
      refusal,
      { ...TICKED, seq: 2 },
      {
        kind: "landed",
        key: PS3_KEY,
        seq: 2,
        commit: LANDED,
      },
    );
    expect(landed.failures.has(PS3_KEY)).toBe(false);
    expect(answered(landed, PS3, AGENT)).toMatchObject({
      value: true,
      marker: "saved",
    });
  });

  test("while the copy has not placed the commit, takes ticks where the tick and the file agree", () => {
    const saved = run(TICKED, LANDED_AT);
    expect(guardView(saved, inFile(PS3_TICKED), LATER)).toMatchObject({
      value: true,
      undecided: false,
      // Whether the commit is past the landing first: the one answer that settles most draws.
      asks: [[LANDED, LATER]],
    });
    expect(guardView(saved, inFile(PS3), AGENT)).toMatchObject({
      value: true,
      undecided: true,
    });
  });

  test("shows over no commit on another line", () => {
    const view = answered(run(TICKED, LANDED_AT), PS3, SIDE);
    expect(view).toMatchObject({ value: false, marker: undefined });
    expect(view.superseded).toBeUndefined();
  });
});

describe("a tick that did not land", () => {
  test("puts the item back and says why when refused, until it is ticked again", () => {
    const refused = run(TICKED, {
      kind: "refused",
      key: PS3_KEY,
      seq: 1,
      reason: "the checklist no longer has the item ps3",
    });
    expect(answered(refused, PS3, BASE).value).toBe(false);
    expect(refused.failures.get("items[ps3].checked")).toBe(
      "Not saved: the checklist no longer has the item ps3",
    );
    const again = reduceJudgements(refused, { ...TICKED, seq: 2 });
    expect(again.failures.has(PS3_KEY)).toBe(false);
  });

  test("puts the item back and asks for it to be redone if missing, when its outcome is unknown", () => {
    const state = run(TICKED, { kind: "unconfirmed", key: PS3_KEY, seq: 1 });
    expect(answered(state, PS3, BASE).value).toBe(false);
    expect(state.failures.get("items[ps3].checked")).toBe(UNCONFIRMED_NOTICE);
  });

  test("made again before the last one's outcome replaces it, whichever outcome comes first", () => {
    const again: JudgementEvent = {
      ...TICKED,
      seq: 2,
      drawnAt: AGENT,
      judged: inFile(PS3_REWORDED).judged,
    };
    const replaced = run(TICKED, again);
    const stillSaving = reduceJudgements(replaced, {
      kind: "refused",
      key: PS3_KEY,
      seq: 1,
      reason: "the item changed",
    });
    expect(answered(stillSaving, PS3_REWORDED, AGENT).marker).toBe("saving");
    expect(stillSaving.failures.size).toBe(0);
    const landedFirst = reduceJudgements(replaced, LANDED_AT);
    expect(answered(landedFirst, PS3_REWORDED, AGENT).marker).toBe("saving");
  });
});

describe("a checklist's store", () => {
  const settle = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

  test("asks the copy each question once, and shows the answer", async () => {
    const asked: string[] = [];
    const store = judgementStore(async (ancestor, descendant) => {
      asked.push(`${ancestor} ${descendant}`);
      return reaches(ancestor, descendant);
    });
    store.dispatch(TICKED);
    const [[ancestor, descendant]] = guardView(
      store.state(),
      inFile(PS3),
      AGENT,
    ).asks;
    store.relate(ancestor, descendant);
    store.relate(ancestor, descendant);
    await settle();
    store.relate(ancestor, descendant);
    expect(asked).toEqual([`${BASE} ${AGENT}`]);
    expect(guardView(store.state(), inFile(PS3), AGENT)).toMatchObject({
      marker: "saving",
      asks: [],
    });
  });

  test("says why a question failed on the item that asked it, and asks again once it is due", async () => {
    let fail = true;
    let asked = 0;
    const store = judgementStore(async (ancestor, descendant) => {
      asked += 1;
      if (fail) throw new Error("the copy does not hold it");
      return reaches(ancestor, descendant);
    }, 10);
    store.dispatch(TICKED);
    store.relate(BASE, AGENT);
    await settle();
    const view = guardView(store.state(), inFile(PS3), AGENT);
    expect(unansweredReason(store.state(), view)).toContain(
      "the copy does not hold it",
    );
    expect(
      unansweredReason(
        store.state(),
        guardView(store.state(), inFile(PM2), AGENT),
      ),
    ).toBeUndefined();
    store.relate(BASE, AGENT);
    expect(asked).toBe(1);

    fail = false;
    await settle(20);
    store.relate(BASE, AGENT);
    await settle();
    expect(asked).toBe(2);
    const answeredView = guardView(store.state(), inFile(PS3), AGENT);
    expect(unansweredReason(store.state(), answeredView)).toBeUndefined();
    expect(answeredView.marker).toBe("saving");
  });

  test("waits longer before asking again after each failure", async () => {
    let asked = 0;
    const store = judgementStore(async () => {
      asked += 1;
      throw new Error("the copy does not hold it");
    }, 50);
    store.dispatch(TICKED);
    store.relate(BASE, AGENT);
    await settle(75);
    // Due after 50 ms: asked again, and due again 100 ms later.
    store.relate(BASE, AGENT);
    await settle(75);
    store.relate(BASE, AGENT);
    expect(asked).toBe(2);
    await settle(50);
    store.relate(BASE, AGENT);
    expect(asked).toBe(3);
  });
});

describe("a publish's failure", () => {
  test("is of unknown outcome only when its worker was lost with it in flight, or its last publish went unanswered", () => {
    expect(outcomeUnknown(new PublishUnconfirmedError("lost"))).toBe(true);
    expect(
      outcomeUnknown(
        new CopyRequestError({
          name: new PublishOutcomeUnknownError("unanswered").name,
          message: "unanswered",
        }),
      ),
    ).toBe(true);
    expect(outcomeUnknown(new CopyWorkerLostError("lost"))).toBe(false);
    expect(
      outcomeUnknown(
        new CopyRequestError({ name: "PublishRefusedError", message: "no" }),
      ),
    ).toBe(false);
  });
});

describe("a widget's queue of ticks", () => {
  function checklist(
    ...items: { id: string; label: string; checked?: boolean }[]
  ) {
    return {
      bytes: toBinary(
        AnySchema,
        anyPack(ChecklistSchema, create(ChecklistSchema, { items })),
      ),
      mode: "100644",
    };
  }
  const asset = checklist(
    { id: "ps3", label: "one" },
    { id: "pm2", label: "two", checked: true },
  );

  function recorder() {
    const box = { state: NO_JUDGEMENTS };
    return {
      box,
      dispatch: (event: JudgementEvent) => {
        box.state = reduceJudgements(box.state, event);
      },
    };
  }

  /** A publish answering each edit in turn from `outcomes`, recording what it was asked. */
  function publishing(...outcomes: EditOutcome[]) {
    const edits: WidgetEdit[] = [];
    return {
      edits,
      publish: async (edit: WidgetEdit) => {
        edits.push(edit);
        const outcome = outcomes.shift();
        if (outcome === undefined) throw new Error("an edit nobody expected");
        return outcome;
      },
    };
  }

  test("shows every tick as saving at once, while an earlier one is still publishing", async () => {
    const { box, dispatch } = recorder();
    let publishes = 0;
    const publish = () => {
      publishes += 1;
      return new Promise<EditOutcome>(() => {});
    };
    const queue = judgementQueue(ancestry);
    const tick = { asset, path: "c.binpb", drawnAt: BASE, value: true };
    void queue({ ...tick, address: checkedOf("ps3") }, publish, dispatch);
    void queue(
      { ...tick, address: checkedOf("pm2"), value: false },
      publish,
      dispatch,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(answered(box.state, PS3, BASE)).toMatchObject({
      value: true,
      marker: "saving",
    });
    expect(answered(box.state, PM2, BASE)).toMatchObject({
      value: false,
      marker: "saving",
    });
    expect(publishes).toBe(1);
  });

  test("builds a tick drawn before its own landing on that landing, from the bytes it wrote there", async () => {
    const { box, dispatch } = recorder();
    const { edits, publish } = publishing(
      { kind: "landed", commit: LANDED, generation: BigInt(1) },
      { kind: "landed", commit: LATER, generation: BigInt(2) },
    );
    const queue = judgementQueue(ancestry);
    await queue(
      {
        asset,
        path: "c.binpb",
        drawnAt: BASE,
        address: checkedOf("ps3"),
        value: true,
      },
      publish,
      dispatch,
    );
    // The row at AGENT, before the landing, shows the tick as saved; unticking it is built on LANDED.
    await queue(
      {
        asset,
        path: "c.binpb",
        drawnAt: AGENT,
        address: checkedOf("ps3"),
        value: false,
      },
      publish,
      dispatch,
    );
    expect(edits.map((edit) => edit.base)).toEqual([BASE, LANDED]);
    expect(
      judgementInAsset(edits[1].files[0].bytes, checkedOf("ps3"))?.value,
    ).toBe(false);
    expect(box.state.failures.size).toBe(0);
  });

  test("builds a tick drawn after its own landing, or on another line, on the commit drawn", async () => {
    for (const drawnAt of [LATER, SIDE]) {
      const { dispatch } = recorder();
      const { edits, publish } = publishing(
        { kind: "landed", commit: LANDED, generation: BigInt(1) },
        { kind: "landed", commit: "f".repeat(40), generation: BigInt(2) },
      );
      const queue = judgementQueue(ancestry);
      const tick = {
        asset,
        path: "c.binpb",
        address: checkedOf("ps3"),
        value: true,
      };
      await queue({ ...tick, drawnAt: BASE }, publish, dispatch);
      await queue(
        { ...tick, drawnAt, address: checkedOf("pm2"), value: false },
        publish,
        dispatch,
      );
      expect(edits.map((edit) => edit.base)).toEqual([BASE, drawnAt]);
    }
  });

  test("builds a tick on the commit drawn where its item there says something other than at the landing", async () => {
    const { dispatch } = recorder();
    const { edits, publish } = publishing(
      { kind: "landed", commit: LANDED, generation: BigInt(1) },
      { kind: "landed", commit: LATER, generation: BigInt(2) },
    );
    const queue = judgementQueue(ancestry);
    await queue(
      {
        asset,
        path: "c.binpb",
        drawnAt: AGENT,
        address: checkedOf("ps3"),
        value: true,
      },
      publish,
      dispatch,
    );
    const older = checklist(
      { id: "ps3", label: "earlier" },
      { id: "pm2", label: "two" },
    );
    await queue(
      {
        asset: older,
        path: "c.binpb",
        drawnAt: BASE,
        address: checkedOf("ps3"),
        value: true,
      },
      publish,
      dispatch,
    );
    expect(edits.map((edit) => edit.base)).toEqual([AGENT, BASE]);
  });

  test("reports each tick's outcome on it, not on a later tick on the same item", async () => {
    const { box, dispatch } = recorder();
    const answers: ((outcome: EditOutcome) => void)[] = [];
    const publish = () =>
      new Promise<EditOutcome>((resolve) => {
        answers.push(resolve);
      });
    const queue = judgementQueue(ancestry);
    const tick = {
      asset,
      path: "c.binpb",
      address: checkedOf("ps3"),
      value: true,
    };
    const first = queue({ ...tick, drawnAt: BASE }, publish, dispatch);
    const second = queue({ ...tick, drawnAt: AGENT }, publish, dispatch);
    await new Promise((resolve) => setTimeout(resolve, 0));
    answers[0]({ kind: "fileChanged", commit: AGENT, path: "c.binpb" });
    await first;
    expect(answered(box.state, PS3, AGENT).marker).toBe("saving");
    expect(box.state.failures.size).toBe(0);
    await new Promise((resolve) => setTimeout(resolve, 0));
    answers[1]({ kind: "landed", commit: LANDED, generation: BigInt(2) });
    await second;
    expect(box.state.judgements.get("items[ps3].checked")).toMatchObject({
      state: "saved",
      landedAs: LANDED,
    });
  });

  test("refuses a tick on an item the asset it was drawn from lacks", () => {
    const tick = () =>
      judgementQueue(ancestry)(
        {
          asset,
          path: "c.binpb",
          drawnAt: BASE,
          address: checkedOf("zz9"),
          value: true,
        },
        async () => ({ kind: "unchanged", commit: BASE }),
        () => {},
      );
    expect(tick).toThrow("has no items[zz9].checked to judge");
  });

  test("says why, and publishes nothing, when the item already has the state asked for", async () => {
    const { box, dispatch } = recorder();
    const { edits, publish } = publishing();
    await judgementQueue(ancestry)(
      {
        asset,
        path: "c.binpb",
        drawnAt: BASE,
        address: checkedOf("pm2"),
        value: true,
      },
      publish,
      dispatch,
    );
    expect(edits).toEqual([]);
    expect(box.state.judgements.size).toBe(0);
    expect(box.state.failures.get("items[pm2].checked")).toBe(
      `Not saved: ${ALREADY_SO}`,
    );
  });

  test("builds a tick after one whose outcome is unknown on the commit drawn", async () => {
    const { dispatch } = recorder();
    let calls = 0;
    const bases: string[] = [];
    const publish = async (edit: WidgetEdit): Promise<EditOutcome> => {
      calls += 1;
      bases.push(edit.base);
      if (calls === 2) throw new PublishUnconfirmedError("lost");
      return {
        kind: "landed",
        commit: calls === 1 ? AGENT : LATER,
        generation: BigInt(calls),
      };
    };
    const queue = judgementQueue(ancestry);
    const tick = {
      asset,
      path: "c.binpb",
      address: checkedOf("ps3"),
      value: true,
    };
    await queue({ ...tick, drawnAt: BASE }, publish, dispatch);
    await queue(
      { ...tick, drawnAt: BASE, address: checkedOf("pm2"), value: false },
      publish,
      dispatch,
    );
    // The unknown publish may have moved the asset past AGENT: the third is built on what was drawn.
    await queue(
      { ...tick, drawnAt: BASE, address: checkedOf("pm2"), value: false },
      publish,
      dispatch,
    );
    expect(bases).toEqual([BASE, AGENT, BASE]);
  });

  test("puts a tick back, publishes it once, and says the change was not saved, when the workspace is damaged", async () => {
    const { box, dispatch } = recorder();
    let publishes = 0;
    const publish = async (): Promise<EditOutcome> => {
      publishes += 1;
      throw new CopyRequestError({
        name: WORKSPACE_DAMAGED,
        message: "the stored document does not parse",
      });
    };
    const queue = judgementQueue(ancestry);
    await queue(
      {
        asset,
        path: "c.binpb",
        address: checkedOf("ps3"),
        value: true,
        drawnAt: BASE,
      },
      publish,
      dispatch,
    );
    expect(publishes).toBe(1);
    expect(answered(box.state, PS3, BASE).value).toBe(false);
    expect(box.state.failures.get("items[ps3].checked")).toBe(DAMAGED_NOTICE);
  });

  test("says the change may not have been saved when the workspace was found damaged after a send went unanswered", async () => {
    const { box, dispatch } = recorder();
    let calls = 0;
    const bases: string[] = [];
    const publish = async (edit: WidgetEdit): Promise<EditOutcome> => {
      calls += 1;
      bases.push(edit.base);
      if (calls === 2) {
        throw new CopyRequestError({
          name: PUBLISH_DAMAGED_AFTER_UNKNOWN,
          message: "never answered, and the repository is damaged",
        });
      }
      return {
        kind: "landed",
        commit: calls === 1 ? AGENT : LATER,
        generation: BigInt(calls),
      };
    };
    const queue = judgementQueue(ancestry);
    const tick = {
      asset,
      path: "c.binpb",
      address: checkedOf("ps3"),
      value: true,
    };
    await queue({ ...tick, drawnAt: BASE }, publish, dispatch);
    await queue(
      { ...tick, drawnAt: BASE, address: checkedOf("pm2"), value: false },
      publish,
      dispatch,
    );
    expect(box.state.failures.get("items[pm2].checked")).toBe(
      DAMAGED_UNCONFIRMED_NOTICE,
    );
    // The untick goes back: the box shows the file's item, still ticked.
    const pm2Ticked = create(Checklist_ItemSchema, {
      id: "pm2",
      label: "two",
      checked: true,
    });
    expect(answered(box.state, pm2Ticked, BASE).value).toBe(true);
    // As after any unknown outcome, the asset may have moved past AGENT: the next is built on BASE.
    await queue(
      { ...tick, drawnAt: BASE, address: checkedOf("pm2"), value: false },
      publish,
      dispatch,
    );
    expect(bases).toEqual([BASE, AGENT, BASE]);
  });

  test("says why a tick was not built when the copy cannot place the commit it was drawn at", async () => {
    const { box, dispatch } = recorder();
    const { publish } = publishing({
      kind: "landed",
      commit: LANDED,
      generation: BigInt(1),
    });
    const queue = judgementQueue(async () => {
      throw new Error("the copy does not hold it");
    });
    const tick = {
      asset,
      path: "c.binpb",
      address: checkedOf("ps3"),
      value: true,
    };
    await queue({ ...tick, drawnAt: BASE }, publish, dispatch);
    await queue({ ...tick, drawnAt: AGENT, value: false }, publish, dispatch);
    expect(box.state.failures.get("items[ps3].checked")).toBe(
      "Not saved: the copy does not hold it",
    );
  });
});

describe("judgements on an SVCv4 classification", () => {
  const PATH = "assets/svcv4.binpb";
  const NOTE: GuardAddress = {
    steps: [{ field: "codes", key: "CLN_DNV" }],
    guard: "note",
  };
  const TICK: GuardAddress = {
    steps: [{ field: "codes", key: "CLN_DNV" }],
    guard: "reviewed",
  };
  const ROUTING: GuardAddress = {
    steps: [{ field: "routing" }],
    guard: "reviewed",
  };
  const ROOT: GuardAddress = { steps: [], guard: "reviewed" };

  function asset(judged: Parameters<typeof fbn1Classification>[0] = {}) {
    return {
      bytes: writePayload(
        Svcv4ClassificationSchema,
        fbn1Classification(judged),
      ),
      mode: "100644",
    };
  }

  function inAsset(address: GuardAddress, bytes: Uint8Array) {
    const found = judgementInAsset(bytes, address);
    if (found === undefined) throw new Error(`no ${addressName(address)}`);
    return { key: addressName(address), ...found };
  }

  test("a note shows while it saves, as saved once landed, and goes once a later version drops it", () => {
    const text = "The clinic letter confirms parentage.";
    const drawn = asset();
    const made = run({
      kind: "made",
      key: addressName(NOTE),
      seq: 1,
      value: text,
      drawnAt: BASE,
      judged: inAsset(NOTE, drawn.bytes).judged,
    });
    expect(guardView(made, inAsset(NOTE, drawn.bytes), BASE)).toMatchObject({
      value: text,
      marker: "saving",
    });
    const landed = reduceJudgements(made, {
      kind: "landed",
      key: addressName(NOTE),
      seq: 1,
      commit: LANDED,
    });
    const noted = asset({ codes: { CLN_DNV: { note: text } } });
    expect(answered2(landed, inAsset(NOTE, noted.bytes), LATER)).toMatchObject({
      value: text,
      marker: "saved",
    });
    const cleared = answered2(landed, inAsset(NOTE, drawn.bytes), LATER);
    expect(cleared).toMatchObject({ value: "", marker: undefined });
    expect(cleared.superseded).toEqual({ key: addressName(NOTE), seq: 1 });
  });

  test("a second guard on one element is built on the first's landing", async () => {
    const edits: WidgetEdit[] = [];
    const publish = async (edit: WidgetEdit): Promise<EditOutcome> => {
      edits.push(edit);
      return {
        kind: "landed",
        commit: edits.length === 1 ? AGENT : LANDED,
        generation: BigInt(edits.length),
      };
    };
    const box = { state: NO_JUDGEMENTS };
    const dispatch = (event: JudgementEvent) => {
      box.state = reduceJudgements(box.state, event);
    };
    const queue = judgementQueue(ancestry);
    const drawn = asset();
    await queue(
      { asset: drawn, path: PATH, drawnAt: BASE, address: NOTE, value: "why" },
      publish,
      dispatch,
    );
    await queue(
      { asset: drawn, path: PATH, drawnAt: BASE, address: TICK, value: true },
      publish,
      dispatch,
    );
    expect(edits.map((edit) => edit.base)).toEqual([BASE, AGENT]);
    const written = inAsset(NOTE, edits[1].files[0].bytes);
    expect(written.value).toBe("why");
    expect(inAsset(TICK, edits[1].files[0].bytes).value).toBe(true);
    expect(box.state.failures.size).toBe(0);
  });

  test("a guard of the root and of a singular message shows over its asset like an element's", () => {
    const drawn = asset();
    for (const address of [ROOT, ROUTING]) {
      const state = run({
        kind: "made",
        key: addressName(address),
        seq: 1,
        value: true,
        drawnAt: BASE,
        judged: inAsset(address, drawn.bytes).judged,
      });
      expect(
        guardView(state, inAsset(address, drawn.bytes), BASE),
      ).toMatchObject({
        value: true,
        marker: "saving",
      });
    }
  });

  test("the record's tick judges its codes as a set, whatever their order", () => {
    const shuffled = fbn1Classification();
    shuffled.codes.reverse();
    const reordered = writePayload(Svcv4ClassificationSchema, shuffled);
    expect(inAsset(ROOT, reordered).judged).toEqual(
      inAsset(ROOT, asset().bytes).judged,
    );
    const reworded = fbn1Classification();
    reworded.codes[0].rationale = "another reason";
    expect(
      inAsset(ROOT, writePayload(Svcv4ClassificationSchema, reworded)).judged,
    ).not.toEqual(inAsset(ROOT, asset().bytes).judged);
  });

  test("an address that does not fit the schema is a defect, never a missing element", () => {
    expect(() =>
      judgementInAsset(asset().bytes, {
        steps: [{ field: "codes" }],
        guard: "note",
      }),
    ).toThrow(AddressError);
    expect(
      judgementInAsset(asset().bytes, {
        steps: [{ field: "codes", key: "XYZ_ABC" }],
        guard: "note",
      }),
    ).toBeUndefined();
  });
});

/** What `guard` shows at `drawnAt` once the copy has answered every question the view asks. */
function answered2(state: Judgements, guard: GuardInFile, drawnAt: string) {
  let at = state;
  for (;;) {
    const view = guardView(at, guard, drawnAt);
    if (view.asks.length === 0) return view;
    at = view.asks.reduce(
      (next, [ancestor, descendant]) =>
        reduceJudgements(next, {
          kind: "related",
          ancestor,
          descendant,
          isAncestor: reaches(ancestor, descendant),
        }),
      at,
    );
  }
}
