import {
  addressName,
  type GuardAddress,
  type GuardValue,
  judgementInAsset,
  setGuardFile,
} from "@/widgets/guard-operation";
import type { FileAtCommit } from "@/workspace-copy/copy";
import type { EditOutcome } from "@/workspace-copy/publish";
import { outcomeUnknown, type WidgetEdit, workspaceDamage } from "./edit";
import type { WidgetState } from "./widget-state";

// What a widget shows over its asset while a curator's judgements are published: the state machine
// of the judgements, apart from the component, so every path through it can be driven directly.
// Every widget's guards go through it, whatever the payload: a judgement is a guard's new value, a
// tick or a note, on the element its address names (guard-operation.ts).
//
// A judgement is a record of what the curator did: the element as they judged it, the value they
// gave the guard, where they made it and, once it lands, the commit it landed as. What a guard shows
// over a version is derived from that record, the version's own element and where the version
// stands in the history relative to the record's commit, which the copy answers:
//
// - while the judgement saves, it shows over the version it was made on and every one after it;
// - once landed, it shows as saved over the version it was made on and every one after it up to the
//   landing, none of which carries it; over the landing and every version after it the file is the
//   record, marked saved while it still holds the judgement, and the record goes once one does not;
// - over any other version, the file shows.
//
// A judgement shows only over an element that still says what the curator judged. A refusal puts
// the guard back and says why; a publish whose outcome nobody knows puts it back too, and asks the
// curator to redo the change if the next version lacks it; a damaged repository puts it back and
// says whether the change may have been saved. Each puts back the landed judgement it replaced, if
// any, which still decides the guard while the notice, the guard's newest status, stands alone
// beside it. A judgement made again on a guard before the last one's outcome replaces it, and the
// outcome of the one replaced is dropped.

/** A curator's judgement on one guard: what it asked for, and how far it has got. */
export interface Judgement {
  /** Which of its queue's judgements this is: an outcome belongs to the judgement it was published
   *  for, and one reported for a judgement a later change to the guard replaced is dropped. */
  seq: number;
  value: GuardValue;
  /** What the judgement judges: the element as the curator saw it when they made it, as
   *  `judgementIn` encodes it. */
  judged: Uint8Array;
  /** The commit the widget was drawn at when the curator made the judgement. */
  drawnAt: string;
  state: "saving" | "saved";
  /** The commit the edit landed as, or the tip that already carried it. */
  landedAs?: string;
  /** The landed judgement on the guard this one replaced, which shows again if this one does not
   *  land. */
  replaced?: Judgement;
}

export interface Judgements {
  /** By guard, as `addressName` names it. */
  judgements: ReadonlyMap<string, Judgement>;
  /** Why a guard's last judgement was not saved, or may not have been, by guard. */
  failures: ReadonlyMap<string, string>;
  /** Whether a commit is another or an ancestor of it, as the copy answered, by `ancestryKey`. */
  ancestry: ReadonlyMap<string, boolean>;
  /** Each ancestry question the copy failed to answer, until it answers it: why, and whether it is
   *  due to be asked again. */
  unanswered: ReadonlyMap<string, { reason: string; due: boolean }>;
}

/** The judgement an event is about: the guard, and the judgement's `seq` in its queue. */
interface JudgementRef {
  key: string;
  seq: number;
}

export type JudgementEvent =
  | (JudgementRef & {
      kind: "made";
      value: GuardValue;
      drawnAt: string;
      judged: Uint8Array;
    })
  | (JudgementRef & { kind: "landed"; commit: string })
  | (JudgementRef & { kind: "refused"; reason: string })
  | (JudgementRef & { kind: "unconfirmed" })
  /** The repository is damaged; `outcomeUnknown` when a send of the judgement went unanswered
   *  first. */
  | (JudgementRef & { kind: "damaged"; outcomeUnknown: boolean })
  /** A version at or after the judgement's landing no longer holds it: the file is the record now. */
  | (JudgementRef & { kind: "superseded" })
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

export const NO_JUDGEMENTS: Judgements = {
  judgements: new Map(),
  failures: new Map(),
  ancestry: new Map(),
  unanswered: new Map(),
};

/** Why a judgement on an element the agent changed since it was shown was not applied. */
export const CHANGED_SINCE_SHOWN =
  "what it judged changed since it was shown; check it again and redo the change";

/** Why a judgement built on a version where the guard already has the value asked for was not
 *  applied. */
export const ALREADY_SO =
  "the version the change would be built on already shows this; check it again on the version shown";

export const UNCONFIRMED_NOTICE =
  "This change may not have been saved. If it does not show once the document updates, redo it.";

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

/** A judgement a widget hands its queue: the guard and its new value, and the asset as drawn at
 *  `drawnAt`. */
export interface QueuedJudgement {
  asset: FileAtCommit;
  path: string;
  drawnAt: string;
  address: GuardAddress;
  value: GuardValue;
}

interface Landing {
  commit: string;
  /** The asset as the widget wrote it there. */
  asset: FileAtCommit;
}

/** Make the edit `judgement` asks for of `from`'s asset and publish it as built on `from`'s commit,
 *  reporting how it ended to `dispatch`. Returns where it landed, `unknown` when nobody knows
 *  whether it did, and undefined when it did not. */
async function publishJudgement(
  judgement: QueuedJudgement,
  seq: number,
  from: Landing,
  publish: (edit: WidgetEdit) => Promise<EditOutcome>,
  dispatch: (event: JudgementEvent) => void,
): Promise<Landing | "unknown" | undefined> {
  const { path, address, value } = judgement;
  const ref = { key: addressName(address), seq };
  let made: ReturnType<typeof setGuardFile>;
  try {
    made = setGuardFile(from.asset.bytes, from.asset.mode, {
      path,
      address,
      value,
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

/** What a judgement drawn at `judgement.drawnAt` is built on: the queue's last landing, from the
 *  bytes it wrote there, when the drawn commit is that landing or before it and the element there
 *  says what the curator judged, since the asset there holds the queue's own judgements the drawn
 *  one does not show yet; else the drawn commit as drawn. The copy refuses either if the asset
 *  changed between it and the tip. */
async function baseFor(
  judgement: QueuedJudgement,
  judged: Uint8Array,
  latest: Landing | undefined,
  isAncestor: Ancestry,
): Promise<Landing> {
  const drawn = { commit: judgement.drawnAt, asset: judgement.asset };
  if (latest === undefined || !(await isAncestor(drawn.commit, latest.commit)))
    return drawn;
  const there = judgementInAsset(latest.asset.bytes, judgement.address);
  return there !== undefined && sameBytes(there.judged, judged)
    ? latest
    : drawn;
}

/** A widget's judgements: each shows as saving at once, and they publish one at a time, each built
 *  on `baseFor`'s choice. Judgements on different guards of one asset share the queue, since each
 *  is built on the asset the one before it wrote. */
export function judgementQueue(
  isAncestor: Ancestry,
): (
  judgement: QueuedJudgement,
  publish: (edit: WidgetEdit) => Promise<EditOutcome>,
  dispatch: (event: JudgementEvent) => void,
) => Promise<void> {
  let chain: Promise<void> = Promise.resolve();
  let latest: Landing | undefined;
  let made = 0;
  return (judgement, publish, dispatch) => {
    const found = judgementInAsset(judgement.asset.bytes, judgement.address);
    const key = addressName(judgement.address);
    if (found === undefined) {
      throw new Error(
        `the asset drawn at ${judgement.drawnAt} has no ${key} to judge`,
      );
    }
    made += 1;
    const seq = made;
    dispatch({
      kind: "made",
      key,
      seq,
      value: judgement.value,
      drawnAt: judgement.drawnAt,
      judged: found.judged,
    });
    const run = chain.then(async () => {
      let from: Landing;
      try {
        from = await baseFor(judgement, found.judged, latest, isAncestor);
      } catch (error) {
        dispatch({ kind: "refused", key, seq, reason: reasonOf(error) });
        return;
      }
      const landed = await publishJudgement(
        judgement,
        seq,
        from,
        publish,
        dispatch,
      );
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

export function reduceJudgements(
  state: Judgements,
  event: JudgementEvent,
): Judgements {
  switch (event.kind) {
    case "made": {
      const before = state.judgements.get(event.key);
      const replaced =
        before?.state === "saved"
          ? { ...before, replaced: undefined }
          : before?.replaced;
      return {
        ...state,
        judgements: withEntry(state.judgements, event.key, {
          seq: event.seq,
          value: event.value,
          judged: event.judged,
          drawnAt: event.drawnAt,
          state: "saving",
          replaced,
        }),
        failures: withoutEntry(state.failures, event.key),
      };
    }
    case "landed": {
      const judgement = state.judgements.get(event.key);
      if (judgement?.seq !== event.seq || judgement.state !== "saving")
        return state;
      return {
        ...state,
        judgements: withEntry(state.judgements, event.key, {
          ...judgement,
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
        judgements: restored(state.judgements, event.key),
        failures: withEntry(
          state.failures,
          event.key,
          `Not saved: ${event.reason}`,
        ),
      };
    case "unconfirmed":
      if (!isSaving(state, event)) return state;
      return {
        ...state,
        judgements: restored(state.judgements, event.key),
        failures: withEntry(state.failures, event.key, UNCONFIRMED_NOTICE),
      };
    case "damaged":
      if (!isSaving(state, event)) return state;
      return {
        ...state,
        judgements: restored(state.judgements, event.key),
        failures: withEntry(
          state.failures,
          event.key,
          event.outcomeUnknown ? DAMAGED_UNCONFIRMED_NOTICE : DAMAGED_NOTICE,
        ),
      };
    case "superseded":
      if (state.judgements.get(event.key)?.seq !== event.seq) return state;
      return {
        ...state,
        judgements: withoutEntry(state.judgements, event.key),
      };
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

/** `judgements` without the guard's judgement, and with the landed judgement it replaced back, if
 *  any. */
function restored(
  judgements: ReadonlyMap<string, Judgement>,
  key: string,
): ReadonlyMap<string, Judgement> {
  const replaced = judgements.get(key)?.replaced;
  return replaced === undefined
    ? withoutEntry(judgements, key)
    : withEntry(judgements, key, replaced);
}

/** Why the guard cannot show where the version drawn stands against its judgement, where that
 *  decides what it shows and the copy failed to answer one of the questions `view` asks. */
export function unansweredReason(
  state: Judgements,
  view: GuardView,
): string | undefined {
  if (!view.undecided) return undefined;
  for (const [ancestor, descendant] of view.asks) {
    const failed = state.unanswered.get(ancestryKey(ancestor, descendant));
    if (failed !== undefined) {
      return `Could not tell whether this version holds the change: ${failed.reason}`;
    }
  }
  return undefined;
}

/** How many judgements in `state` are still waiting on their outcome: one at most per guard, since a
 *  judgement made again on a guard replaces the one before. */
function savingCount(state: Judgements): number {
  let count = 0;
  for (const judgement of state.judgements.values()) {
    if (judgement.state === "saving") count += 1;
  }
  return count;
}

/** Whether `ref` is the guard's judgement still waiting on its outcome, not one a later change
 *  replaced. */
function isSaving(state: Judgements, ref: JudgementRef): boolean {
  const judgement = state.judgements.get(ref.key);
  return judgement?.seq === ref.seq && judgement.state === "saving";
}

export function ancestryKey(ancestor: string, descendant: string): string {
  return `${ancestor} ${descendant}`;
}

/** A guard as the asset read at a version holds it: the guard, its value there, and what it
 *  judges there, as `judgementIn` reads them. */
export interface GuardInFile {
  key: string;
  value: GuardValue;
  judged: Uint8Array;
}

/** What one guard shows over a version. */
export interface GuardView {
  value: GuardValue;
  /** The marker beside it: a judgement saving, or one saved, unless a notice says why the guard's
   *  last judgement was not saved or may not have been. */
  marker?: Judgement["state"];
  /** Whether the guard waits on the copy to say where the version stands against its judgement,
   *  and the judgement and the file disagree: it shows the judgement meanwhile, and takes no new
   *  one. */
  undecided: boolean;
  /** The ancestry answers the view needs and does not have yet, as pairs of commits. */
  asks: [ancestor: string, descendant: string][];
  /** A judgement the version shows the file has moved past, which the view no longer needs. */
  superseded?: JudgementRef;
}

/** What `guard`, as the asset read at `drawnAt` holds it, shows under `state`'s judgement on it. */
export function guardView(
  state: Judgements,
  guard: GuardInFile,
  drawnAt: string,
): GuardView {
  const file: GuardView = {
    value: guard.value,
    marker: undefined,
    undecided: false,
    asks: [],
  };
  const judgement = state.judgements.get(guard.key);
  if (judgement === undefined) return file;
  const shown = {
    file,
    overlay: {
      value: judgement.value,
      marker: judgement.state,
      undecided: false,
      asks: [],
    },
    judgesThis: sameBytes(judgement.judged, guard.judged),
  };
  const view =
    judgement.landedAs === undefined
      ? savingView(state, judgement, drawnAt, shown)
      : landedView(
          state,
          { ...judgement, landedAs: judgement.landedAs },
          guard,
          drawnAt,
          shown,
        );
  if (!state.failures.has(guard.key)) return view;
  // A notice is the guard's newest status, since a judgement clears it: the landed judgement a
  // refused or unconfirmed one put back still decides the guard, and no longer says it saved.
  if (view.marker === "saving") {
    throw new Error(
      `the guard ${guard.key} has a notice while a judgement on it is saving`,
    );
  }
  return { ...view, marker: undefined };
}

interface Shown {
  /** The guard as the file holds it. */
  file: GuardView;
  /** The guard as the judgement asks for it. */
  overlay: GuardView;
  /** Whether the element still says what the judgement judged. */
  judgesThis: boolean;
}

/** A judgement being saved shows over the commit it was made on and every one after it. */
function savingView(
  state: Judgements,
  judgement: Judgement,
  drawnAt: string,
  shown: Shown,
): GuardView {
  if (!shown.judgesThis) return shown.file;
  const after = atOrAfter(state, judgement.drawnAt, drawnAt);
  if (after === undefined)
    return pending(shown, [[judgement.drawnAt, drawnAt]]);
  return after ? shown.overlay : shown.file;
}

/** A judgement that landed shows as saved over the commit it was made on and every one after it up
 *  to the landing; over the landing and after it the file shows, marked saved while it holds the
 *  judgement, and the judgement goes once it does not. The landing is after the commit the
 *  judgement was made on, which settles both of them without asking; elsewhere each question waits
 *  on the one before it, the one that settles most draws first. */
function landedView(
  state: Judgements,
  judgement: Judgement & { landedAs: string },
  guard: GuardInFile,
  drawnAt: string,
  shown: Shown,
): GuardView {
  const { file, overlay, judgesThis } = shown;
  const landing = judgement.landedAs;
  const past =
    drawnAt !== landing && drawnAt === judgement.drawnAt
      ? false
      : atOrAfter(state, landing, drawnAt);
  if (past === undefined) return pending(shown, [[landing, drawnAt]]);
  if (past) {
    return judgesThis && guard.value === judgement.value
      ? { ...file, marker: "saved" }
      : { ...file, superseded: { key: guard.key, seq: judgement.seq } };
  }
  if (!judgesThis) return file;
  if (drawnAt === judgement.drawnAt) return overlay;
  const since = atOrAfter(state, judgement.drawnAt, drawnAt);
  if (since === undefined)
    return pending(shown, [[judgement.drawnAt, drawnAt]]);
  if (!since) return file;
  const before = state.ancestry.get(ancestryKey(drawnAt, landing));
  if (before === undefined) return pending(shown, [[drawnAt, landing]]);
  return before ? overlay : file;
}

/** Whether `commit` is `from` or after it, as far as the copy has answered. */
function atOrAfter(
  state: Judgements,
  from: string,
  commit: string,
): boolean | undefined {
  return commit === from || state.ancestry.get(ancestryKey(from, commit));
}

/** A guard waiting on `asks`: it shows the judgement meanwhile, and takes no new judgement where the
 *  judgement and the file disagree, since which one a change replaces is not settled yet. */
function pending(
  { file, overlay, judgesThis }: Shown,
  asks: [string, string][],
): GuardView {
  if (!judgesThis) return { ...file, asks };
  return { ...overlay, undecided: overlay.value !== file.value, asks };
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

/** One asset's judgements, as every component drawing it shares them: the state they show, the
 *  queue they publish through, the ancestry answers they are shown by, and a subscription for
 *  React's external-store hook. Each judgement on it still saving is one unsaved change. */
export interface JudgementStore extends WidgetState {
  state(): Judgements;
  dispatch(event: JudgementEvent): void;
  queue: ReturnType<typeof judgementQueue>;
  /** Ask the copy whether `ancestor` is `descendant` or before it, unless it answered, is being
   *  asked, or failed to and is not due to be asked again, and record the answer, or why there is
   *  none. A failed question falls due again after a wait that doubles with each failure. */
  relate(ancestor: string, descendant: string): void;
}

export function judgementStore(
  isAncestor: Ancestry,
  askAgainAfterMs = ASK_AGAIN_AFTER_MS,
): JudgementStore {
  let state = NO_JUDGEMENTS;
  const asking = new Set<string>();
  const failures = new Map<string, number>();
  const listeners = new Set<() => void>();
  const dispatch = (event: JudgementEvent) => {
    state = reduceJudgements(state, event);
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
    queue: judgementQueue(isAncestor),
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
