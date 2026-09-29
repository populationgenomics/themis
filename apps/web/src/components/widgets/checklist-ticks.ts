import type { Checklist_Item } from "@/models/widgets";
import {
  itemIn,
  judgedItem,
  setCheckedFile,
} from "@/widgets/checklist-operation";
import type { FileAtCommit } from "@/workspace-copy/copy";
import type { EditOutcome } from "@/workspace-copy/publish";
import { outcomeUnknown, type WidgetEdit, workspaceDamage } from "./edit";
import type { WidgetState } from "./widget-state";

// What a checklist shows over its asset while a curator's ticks are published: the state machine of
// the ticks, apart from the component, so every path through it can be driven directly.
//
// A tick is a record of what the curator did: the item as they judged it, the state they gave it,
// where they made it and, once it lands, the commit it landed as. What an item shows over a version
// is derived from that record, the version's own item and where the version stands in the history
// relative to the record's commit, which the copy answers:
//
// - while the tick saves, it shows over the version it was made on and every one after it;
// - once landed, it shows as saved over the version it was made on and every one after it up to the
//   landing, none of which carries it; over the landing and every version after it the file is the
//   record, marked saved while it still holds the tick, and the record goes once one does not;
// - over any other version, the file shows.
//
// A tick shows only over an item that still says what the curator judged. A refusal puts the item
// back and says why; a publish whose outcome nobody knows puts it back too, and asks the curator to
// redo the change if the next version lacks it; a damaged repository puts it back and says whether
// the change may have been saved. Each puts back the landed tick it replaced, if any, which still
// decides the box while the notice, the item's newest status, stands alone beside it. A tick made
// again on an item before the last one's outcome replaces it, and the outcome of the one replaced
// is dropped.

/** A curator's tick on one item: what it asked for, and how far it has got. */
export interface Tick {
  /** Which of its queue's ticks this is: an outcome belongs to the tick it was published for, and
   *  one reported for a tick a later click on the item replaced is dropped. */
  seq: number;
  checked: boolean;
  /** What the tick judges: the item as the curator saw it when they made it (`judgedItem`). */
  judged: Uint8Array;
  /** The commit the widget was drawn at when the curator made the tick. */
  drawnAt: string;
  state: "saving" | "saved";
  /** The commit the edit landed as, or the tip that already carried it. */
  landedAs?: string;
  /** The landed tick on the item this one replaced, which shows again if this one does not land. */
  replaced?: Tick;
}

export interface Ticks {
  ticks: ReadonlyMap<string, Tick>;
  /** Why an item's last tick was not saved, or may not have been, by item id. */
  failures: ReadonlyMap<string, string>;
  /** Whether a commit is another or an ancestor of it, as the copy answered, by `ancestryKey`. */
  ancestry: ReadonlyMap<string, boolean>;
  /** Each ancestry question the copy failed to answer, until it answers it: why, and whether it is
   *  due to be asked again. */
  unanswered: ReadonlyMap<string, { reason: string; due: boolean }>;
}

/** The tick an event is about: the item, and the tick's `seq` in its queue. */
interface TickRef {
  itemId: string;
  seq: number;
}

export type TickEvent =
  | (TickRef & {
      kind: "ticked";
      checked: boolean;
      drawnAt: string;
      judged: Uint8Array;
    })
  | (TickRef & { kind: "landed"; commit: string })
  | (TickRef & { kind: "refused"; reason: string })
  | (TickRef & { kind: "unconfirmed" })
  /** The repository is damaged; `outcomeUnknown` when a send of the tick went unanswered first. */
  | (TickRef & { kind: "damaged"; outcomeUnknown: boolean })
  /** A version at or after the tick's landing no longer holds it: the file is the record now. */
  | (TickRef & { kind: "superseded" })
  | {
      kind: "related";
      ancestor: string;
      descendant: string;
      isAncestor: boolean;
    }
  | {
      kind: "unrelatable";
      ancestor: string;
      descendant: string;
      reason: string;
    }
  /** A question the copy failed to answer is due to be asked again. */
  | { kind: "due"; ancestor: string; descendant: string };

export const NO_TICKS: Ticks = {
  ticks: new Map(),
  failures: new Map(),
  ancestry: new Map(),
  unanswered: new Map(),
};

/** Why a tick on a checklist the agent changed since it was shown was not applied. */
export const CHANGED_SINCE_SHOWN =
  "the item changed since it was shown; check it again and redo the change";

/** Why a tick built on a version where the item already has the state asked for was not applied. */
export const ALREADY_SO =
  "the version the change would be built on already shows the item this way; check it again on the version shown";

export const UNCONFIRMED_NOTICE =
  "This change may not have been saved. If the box does not show it once the document updates, redo it.";

export const DAMAGED_NOTICE =
  "The workspace is damaged, so this change was not saved.";

export const DAMAGED_UNCONFIRMED_NOTICE =
  "The workspace is damaged, and this change may not have been saved.";

/** Answers whether `ancestor` is `descendant` or reachable from it, in the copy of the widget's
 *  Analysis. */
export type Ancestry = (
  ancestor: string,
  descendant: string,
) => Promise<boolean>;

/** A tick a widget hands its queue: the item and state, and the asset as drawn at `drawnAt`. */
export interface QueuedTick {
  asset: FileAtCommit;
  path: string;
  drawnAt: string;
  itemId: string;
  checked: boolean;
}

interface Landing {
  commit: string;
  /** The asset as the widget wrote it there. */
  asset: FileAtCommit;
}

/** Make the edit `tick` asks for of `from`'s asset and publish it as built on `from`'s commit,
 *  reporting how it ended to `dispatch`. Returns where it landed, `unknown` when nobody knows
 *  whether it did, and undefined when it did not. */
async function publishTick(
  tick: QueuedTick,
  seq: number,
  from: Landing,
  publish: (edit: WidgetEdit) => Promise<EditOutcome>,
  dispatch: (event: TickEvent) => void,
): Promise<Landing | "unknown" | undefined> {
  const { path, itemId, checked } = tick;
  const ref = { itemId, seq };
  let made: ReturnType<typeof setCheckedFile>;
  try {
    made = setCheckedFile(from.asset.bytes, from.asset.mode, {
      path,
      itemId,
      checked,
    });
  } catch (error) {
    dispatch({ kind: "refused", ...ref, reason: reasonOf(error) });
    return undefined;
  }
  if (made === undefined) {
    dispatch({ kind: "refused", ...ref, reason: ALREADY_SO });
    return undefined;
  }
  try {
    const outcome = await publish({ base: from.commit, ...made });
    if (outcome.kind === "fileChanged") {
      dispatch({ kind: "refused", ...ref, reason: CHANGED_SINCE_SHOWN });
      return undefined;
    }
    dispatch({ kind: "landed", ...ref, commit: outcome.commit });
    const [file] = made.files;
    return {
      commit: outcome.commit,
      asset: { bytes: file.bytes, mode: from.asset.mode },
    };
  } catch (error) {
    if (outcomeUnknown(error)) {
      dispatch({ kind: "unconfirmed", ...ref });
      return "unknown";
    }
    const damage = workspaceDamage(error);
    if (damage !== undefined) {
      const unknown = damage === "damagedAfterUnknown";
      dispatch({ kind: "damaged", ...ref, outcomeUnknown: unknown });
      return unknown ? "unknown" : undefined;
    }
    dispatch({ kind: "refused", ...ref, reason: reasonOf(error) });
    return undefined;
  }
}

/** What a tick drawn at `tick.drawnAt` is built on: the queue's last landing, from the bytes it wrote
 *  there, when the drawn commit is that landing or before it and the item there says what the curator
 *  judged, since the asset there holds the queue's own ticks the drawn one does not show yet; else the
 *  drawn commit as drawn. The copy refuses either if the asset changed between it and the tip. */
async function baseFor(
  tick: QueuedTick,
  judged: Uint8Array,
  latest: Landing | undefined,
  isAncestor: Ancestry,
): Promise<Landing> {
  const drawn = { commit: tick.drawnAt, asset: tick.asset };
  if (latest === undefined || !(await isAncestor(drawn.commit, latest.commit)))
    return drawn;
  const there = itemIn(latest.asset.bytes, tick.itemId);
  return there !== undefined && sameBytes(judgedItem(there), judged)
    ? latest
    : drawn;
}

/** A widget's ticks: each shows as saving at once, and they publish one at a time, each built on
 *  `baseFor`'s choice. */
export function tickQueue(
  isAncestor: Ancestry,
): (
  tick: QueuedTick,
  publish: (edit: WidgetEdit) => Promise<EditOutcome>,
  dispatch: (event: TickEvent) => void,
) => Promise<void> {
  let chain: Promise<void> = Promise.resolve();
  let latest: Landing | undefined;
  let ticks = 0;
  return (tick, publish, dispatch) => {
    const item = itemIn(tick.asset.bytes, tick.itemId);
    if (item === undefined) {
      throw new Error(
        `the checklist drawn at ${tick.drawnAt} has no item ${tick.itemId} to tick`,
      );
    }
    ticks += 1;
    const seq = ticks;
    const judged = judgedItem(item);
    dispatch({
      kind: "ticked",
      itemId: tick.itemId,
      seq,
      checked: tick.checked,
      drawnAt: tick.drawnAt,
      judged,
    });
    const run = chain.then(async () => {
      let from: Landing;
      try {
        from = await baseFor(tick, judged, latest, isAncestor);
      } catch (error) {
        dispatch({
          kind: "refused",
          itemId: tick.itemId,
          seq,
          reason: reasonOf(error),
        });
        return;
      }
      const landed = await publishTick(tick, seq, from, publish, dispatch);
      // A publish nobody knows the end of may have moved the asset past the last landing.
      if (landed === "unknown") latest = undefined;
      else if (landed !== undefined) latest = landed;
    });
    chain = run.catch(() => {});
    return run;
  };
}

function reasonOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function reduceTicks(state: Ticks, event: TickEvent): Ticks {
  switch (event.kind) {
    case "ticked": {
      const before = state.ticks.get(event.itemId);
      const replaced =
        before?.state === "saved"
          ? { ...before, replaced: undefined }
          : before?.replaced;
      return {
        ...state,
        ticks: withEntry(state.ticks, event.itemId, {
          seq: event.seq,
          checked: event.checked,
          judged: event.judged,
          drawnAt: event.drawnAt,
          state: "saving",
          replaced,
        }),
        failures: withoutEntry(state.failures, event.itemId),
      };
    }
    case "landed": {
      const tick = state.ticks.get(event.itemId);
      if (tick?.seq !== event.seq || tick.state !== "saving") return state;
      return {
        ...state,
        ticks: withEntry(state.ticks, event.itemId, {
          ...tick,
          state: "saved",
          landedAs: event.commit,
          replaced: undefined,
        }),
      };
    }
    case "refused":
      if (!isSaving(state, event)) return state;
      return {
        ...state,
        ticks: restored(state.ticks, event.itemId),
        failures: withEntry(
          state.failures,
          event.itemId,
          `Not saved: ${event.reason}`,
        ),
      };
    case "unconfirmed":
      if (!isSaving(state, event)) return state;
      return {
        ...state,
        ticks: restored(state.ticks, event.itemId),
        failures: withEntry(state.failures, event.itemId, UNCONFIRMED_NOTICE),
      };
    case "damaged":
      if (!isSaving(state, event)) return state;
      return {
        ...state,
        ticks: restored(state.ticks, event.itemId),
        failures: withEntry(
          state.failures,
          event.itemId,
          event.outcomeUnknown ? DAMAGED_UNCONFIRMED_NOTICE : DAMAGED_NOTICE,
        ),
      };
    case "superseded":
      if (state.ticks.get(event.itemId)?.seq !== event.seq) return state;
      return { ...state, ticks: withoutEntry(state.ticks, event.itemId) };
    case "related": {
      const key = ancestryKey(event.ancestor, event.descendant);
      return {
        ...state,
        ancestry: withEntry(state.ancestry, key, event.isAncestor),
        unanswered: withoutEntry(state.unanswered, key),
      };
    }
    case "unrelatable":
      return {
        ...state,
        unanswered: withEntry(
          state.unanswered,
          ancestryKey(event.ancestor, event.descendant),
          { reason: event.reason, due: false },
        ),
      };
    case "due": {
      const key = ancestryKey(event.ancestor, event.descendant);
      const failed = state.unanswered.get(key);
      if (failed === undefined) return state;
      return {
        ...state,
        unanswered: withEntry(state.unanswered, key, { ...failed, due: true }),
      };
    }
  }
}

/** `ticks` without the item's tick, and with the landed tick it replaced back, if any. */
function restored(
  ticks: ReadonlyMap<string, Tick>,
  itemId: string,
): ReadonlyMap<string, Tick> {
  const replaced = ticks.get(itemId)?.replaced;
  return replaced === undefined
    ? withoutEntry(ticks, itemId)
    : withEntry(ticks, itemId, replaced);
}

/** Why the item cannot show where the version drawn stands against its tick, where that decides
 *  what it shows and the copy failed to answer one of the questions `view` asks. */
export function unansweredReason(
  state: Ticks,
  view: ItemView,
): string | undefined {
  if (!view.undecided) return undefined;
  for (const [ancestor, descendant] of view.asks) {
    const failed = state.unanswered.get(ancestryKey(ancestor, descendant));
    if (failed !== undefined) {
      return `Could not tell whether this version holds the tick: ${failed.reason}`;
    }
  }
  return undefined;
}

/** How many ticks in `state` are still waiting on their outcome: one at most per item, since a tick
 *  made again on an item replaces the one before. */
function savingCount(state: Ticks): number {
  let count = 0;
  for (const tick of state.ticks.values()) {
    if (tick.state === "saving") count += 1;
  }
  return count;
}

/** Whether `ref` is the item's tick still waiting on its outcome, not one a later click replaced. */
function isSaving(state: Ticks, ref: TickRef): boolean {
  const tick = state.ticks.get(ref.itemId);
  return tick?.seq === ref.seq && tick.state === "saving";
}

export function ancestryKey(ancestor: string, descendant: string): string {
  return `${ancestor} ${descendant}`;
}

/** What one item shows over a version. */
export interface ItemView {
  checked: boolean;
  /** The marker beside it: a tick saving, or one saved, unless a notice says why the item's last
   *  tick was not saved or may not have been. */
  marker?: Tick["state"];
  /** Whether the item waits on the copy to say where the version stands against its tick, and the
   *  tick and the file disagree: it shows the tick meanwhile, and takes no new one. */
  undecided: boolean;
  /** The ancestry answers the view needs and does not have yet, as pairs of commits. */
  asks: [ancestor: string, descendant: string][];
  /** A tick the version shows the file has moved past, which the view no longer needs. */
  superseded?: TickRef;
}

/** What `item`, as the asset read at `drawnAt` holds it, shows under `state`'s tick on it. */
export function itemView(
  state: Ticks,
  item: Checklist_Item,
  drawnAt: string,
): ItemView {
  const file: ItemView = {
    checked: item.checked,
    marker: undefined,
    undecided: false,
    asks: [],
  };
  const tick = state.ticks.get(item.id);
  if (tick === undefined) return file;
  const shown = {
    file,
    overlay: {
      checked: tick.checked,
      marker: tick.state,
      undecided: false,
      asks: [],
    },
    judgesThis: sameBytes(tick.judged, judgedItem(item)),
  };
  const view =
    tick.landedAs === undefined
      ? savingView(state, tick, drawnAt, shown)
      : landedView(
          state,
          { ...tick, landedAs: tick.landedAs },
          item,
          drawnAt,
          shown,
        );
  if (!state.failures.has(item.id)) return view;
  // A notice is the item's newest status, since a tick clears it: the landed tick a refused or
  // unconfirmed one put back still decides the box, and no longer says it saved.
  if (view.marker === "saving") {
    throw new Error(
      `the item ${item.id} has a notice while a tick on it is saving`,
    );
  }
  return { ...view, marker: undefined };
}

interface Shown {
  /** The item as the file holds it. */
  file: ItemView;
  /** The item as the tick asks for it. */
  overlay: ItemView;
  /** Whether the item still says what the tick judged. */
  judgesThis: boolean;
}

/** A tick being saved shows over the commit it was made on and every one after it. */
function savingView(
  state: Ticks,
  tick: Tick,
  drawnAt: string,
  shown: Shown,
): ItemView {
  if (!shown.judgesThis) return shown.file;
  const after = atOrAfter(state, tick.drawnAt, drawnAt);
  if (after === undefined) return pending(shown, [[tick.drawnAt, drawnAt]]);
  return after ? shown.overlay : shown.file;
}

/** A tick that landed shows as saved over the commit it was made on and every one after it up to
 *  the landing; over the landing and after it the file shows, marked saved while it holds the tick,
 *  and the tick goes once it does not. The landing is after the commit the tick was made on, which
 *  settles both of them without asking; elsewhere each question waits on the one before it, the one
 *  that settles most draws first. */
function landedView(
  state: Ticks,
  tick: Tick & { landedAs: string },
  item: Checklist_Item,
  drawnAt: string,
  shown: Shown,
): ItemView {
  const { file, overlay, judgesThis } = shown;
  const landing = tick.landedAs;
  const past =
    drawnAt !== landing && drawnAt === tick.drawnAt
      ? false
      : atOrAfter(state, landing, drawnAt);
  if (past === undefined) return pending(shown, [[landing, drawnAt]]);
  if (past) {
    return judgesThis && item.checked === tick.checked
      ? { ...file, marker: "saved" }
      : { ...file, superseded: { itemId: item.id, seq: tick.seq } };
  }
  if (!judgesThis) return file;
  if (drawnAt === tick.drawnAt) return overlay;
  const since = atOrAfter(state, tick.drawnAt, drawnAt);
  if (since === undefined) return pending(shown, [[tick.drawnAt, drawnAt]]);
  if (!since) return file;
  const before = state.ancestry.get(ancestryKey(drawnAt, landing));
  if (before === undefined) return pending(shown, [[drawnAt, landing]]);
  return before ? overlay : file;
}

/** Whether `commit` is `from` or after it, as far as the copy has answered. */
function atOrAfter(
  state: Ticks,
  from: string,
  commit: string,
): boolean | undefined {
  return commit === from || state.ancestry.get(ancestryKey(from, commit));
}

/** An item waiting on `asks`: it shows the tick meanwhile, and takes no new tick where the tick and
 *  the file disagree, since which one a click toggles is not settled yet. */
function pending(
  { file, overlay, judgesThis }: Shown,
  asks: [string, string][],
): ItemView {
  if (!judgesThis) return { ...file, asks };
  return { ...overlay, undecided: overlay.checked !== file.checked, asks };
}

function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((byte, index) => byte === b[index]);
}

function withEntry<V>(
  map: ReadonlyMap<string, V>,
  key: string,
  value: V,
): ReadonlyMap<string, V> {
  return new Map(map).set(key, value);
}

function withoutEntry<V>(
  map: ReadonlyMap<string, V>,
  key: string,
): ReadonlyMap<string, V> {
  const next = new Map(map);
  next.delete(key);
  return next;
}

/** How long a question the copy failed to answer waits before it is asked again the first time;
 *  each failure after doubles it, up to `ASK_AGAIN_AT_MOST_MS`. */
export const ASK_AGAIN_AFTER_MS = 2000;
export const ASK_AGAIN_AT_MOST_MS = 60_000;

/** One checklist's ticks, as every component drawing its asset shares them: the state they show,
 *  the queue they publish through, the ancestry answers they are shown by, and a subscription for
 *  React's external-store hook. Each tick on it still saving is one unsaved change. */
export interface TickStore extends WidgetState {
  state(): Ticks;
  dispatch(event: TickEvent): void;
  queue: ReturnType<typeof tickQueue>;
  /** Ask the copy whether `ancestor` is `descendant` or before it, unless it answered, is being
   *  asked, or failed to and is not due to be asked again, and record the answer, or why there is
   *  none. A failed question falls due again after a wait that doubles with each failure. */
  relate(ancestor: string, descendant: string): void;
}

export function tickStore(
  isAncestor: Ancestry,
  askAgainAfterMs = ASK_AGAIN_AFTER_MS,
): TickStore {
  let state = NO_TICKS;
  const asking = new Set<string>();
  const failures = new Map<string, number>();
  const listeners = new Set<() => void>();
  const dispatch = (event: TickEvent) => {
    state = reduceTicks(state, event);
    for (const listener of listeners) listener();
  };
  return {
    state: () => state,
    unsaved: () => savingCount(state),
    dispatch,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    queue: tickQueue(isAncestor),
    relate: (ancestor, descendant) => {
      const key = ancestryKey(ancestor, descendant);
      const failed = state.unanswered.get(key);
      if (
        asking.has(key) ||
        state.ancestry.has(key) ||
        (failed !== undefined && !failed.due)
      )
        return;
      asking.add(key);
      isAncestor(ancestor, descendant).then(
        (answer) => {
          asking.delete(key);
          failures.delete(key);
          dispatch({
            kind: "related",
            ancestor,
            descendant,
            isAncestor: answer,
          });
        },
        (error: unknown) => {
          asking.delete(key);
          dispatch({
            kind: "unrelatable",
            ancestor,
            descendant,
            reason: reasonOf(error),
          });
          const attempts = (failures.get(key) ?? 0) + 1;
          failures.set(key, attempts);
          setTimeout(
            () => dispatch({ kind: "due", ancestor, descendant }),
            Math.min(
              askAgainAfterMs * 2 ** (attempts - 1),
              ASK_AGAIN_AT_MOST_MS,
            ),
          );
        },
      );
    },
  };
}
