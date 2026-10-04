"use client";

import type { DescMessage, MessageShape } from "@bufbuild/protobuf";
import { useEffect, useMemo, useSyncExternalStore } from "react";
import {
  addressName,
  type GuardAddress,
  type GuardValue,
  judgementIn,
} from "@/widgets/guard-operation";
import { workspaceCopy } from "@/workspace-copy/client";
import { useWidgetEdit } from "./edit";
import {
  type GuardView,
  guardView,
  judgementStore,
  NO_JUDGEMENTS,
  unansweredReason,
} from "./judgements";
import type { WidgetProps } from "./registry";
import { useWidgetStates, type WidgetState } from "./widget-state";

// A widget's guards as its component draws them: what each shows over the asset drawn, with the
// curator's judgements being published laid over it (judgements.ts), how to make one, and the note
// a curator is still writing. Both stores live for the window's life beside every other component
// drawing the same asset (widget-state.tsx), so neither a judgement nor a draft is lost when the
// component is thrown away and drawn again, or a control moves as its value changes.

/** A note the curator is writing and has not saved: the text, and what the guard judged when they
 *  started it, so a save can tell whether it would land beside content they did not write it
 *  about. */
export interface Draft {
  text: string;
  judged: Uint8Array;
}

/** One guard as a widget draws it. */
export interface GuardState {
  view: GuardView;
  /** What the guard judges in the asset drawn. */
  judged: Uint8Array;
  /** Why the guard's last judgement was not saved, or may not have been, or why the widget cannot
   *  tell what the version drawn holds. */
  failure: string | undefined;
  /** Whether the curator can make a judgement on it now. */
  editable: boolean;
  /** Make the judgement `value`. Returns whether it was taken: a guard that is not editable takes
   *  none. */
  set(value: GuardValue): boolean;
  /** The note being written on this guard, if any. */
  draft: Draft | undefined;
  /** Start, change or drop the draft. */
  setDraft(draft: Draft | undefined): void;
}

/** The notes being written on one asset's guards, by guard. None of them is a change the copy
 *  holds, so none counts as unsaved to the window. */
class DraftStore implements WidgetState {
  private drafts = new Map<string, Draft>();
  private readonly listeners = new Set<() => void>();

  state = (): ReadonlyMap<string, Draft> => this.drafts;

  set(key: string, draft: Draft | undefined): void {
    const next = new Map(this.drafts);
    if (draft === undefined) next.delete(key);
    else next.set(key, draft);
    this.drafts = next;
    for (const listener of this.listeners) listener();
  }

  unsaved = (): number => 0;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
}

/** The guards at `addresses` in the payload a widget draws, each as `GuardState`, by
 *  `addressName`. Raises where the payload holds no guard at an address, which is a defect in the
 *  widget asking. */
export function useJudgements<Desc extends DescMessage>(
  props: WidgetProps<MessageShape<Desc>>,
  schema: Desc,
  addresses: readonly GuardAddress[],
): ReadonlyMap<string, GuardState> {
  const { payload, asset, path, drawnAt, revision } = props;
  const edit = useWidgetEdit(props);
  const { analysisId } = revision;
  const states = useWidgetStates();
  const store = states.entry(analysisId, path, "judgements", () =>
    judgementStore((ancestor, descendant) =>
      workspaceCopy.isAncestor(analysisId, ancestor, descendant),
    ),
  );
  const drafts = states.entry(
    analysisId,
    path,
    "note-drafts",
    () => new DraftStore(),
  );
  const state = useSyncExternalStore(store.subscribe, store.state, store.state);
  const written = useSyncExternalStore(
    drafts.subscribe,
    drafts.state,
    drafts.state,
  );
  const keys = addresses.map(addressName).join("\n");
  // What each guard judges depends on the payload alone; encoding it is the costly part.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `keys` stands for `addresses`, which a caller rebuilds on every render
  const found = useMemo(
    () =>
      addresses.map((address) => {
        const key = addressName(address);
        const guard = judgementIn(schema, payload, address);
        if (guard === undefined) {
          throw new Error(`the ${schema.typeName} drawn has no ${key}`);
        }
        return { address, key, ...guard };
      }),
    [schema, payload, keys],
  );
  // A pinned version is not the tip's line, and a judgement shows over none of its guards.
  const shown = revision.pinned ? NO_JUDGEMENTS : state;
  const guards = new Map<string, GuardState>();
  const views: GuardView[] = [];
  for (const { address, key, judged, value } of found) {
    const view = guardView(shown, { key, judged, value }, drawnAt);
    views.push(view);
    const editable =
      edit !== null && !view.undecided && view.marker !== "saving";
    guards.set(key, {
      view,
      judged,
      failure: shown.failures.get(key) ?? unansweredReason(shown, view),
      editable,
      set: (next) => {
        if (!editable || edit === null) return false;
        void store.queue(
          { asset, path, drawnAt, address, value: next },
          edit,
          store.dispatch,
        );
        return true;
      },
      draft: written.get(key),
      setDraft: (draft) => drafts.set(key, draft),
    });
  }
  // Each render asks what its views are missing; both are no-ops once answered.
  useEffect(() => {
    for (const view of views) {
      for (const [ancestor, descendant] of view.asks)
        store.relate(ancestor, descendant);
      if (view.superseded !== undefined)
        store.dispatch({ kind: "superseded", ...view.superseded });
    }
  });
  return guards;
}
