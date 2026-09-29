"use client";

import { useEffect, useSyncExternalStore } from "react";
import { type Citation, CitationMark } from "@/components/workbench/citation";
import type { Checklist, Checklist_Item } from "@/models/widgets";
import { workspaceCopy } from "@/workspace-copy/client";
import {
  type ItemView,
  itemView,
  NO_TICKS,
  type Tick,
  tickStore,
  unansweredReason,
} from "./checklist-ticks";
import { useWidgetEdit } from "./edit";
import type { WidgetProps } from "./registry";
import { useWidgetStates } from "./widget-state";

// A checklist the agent asks a curator to work through: each item's label as plain text, the paper
// or passage it rests on as the citation mark prose uses, and whether a curator has ticked it, with
// what a tick being published shows over it (checklist-ticks.ts), kept for the window's life beside
// every other component drawing the same asset (widget-state.tsx).

export function ChecklistWidget(
  props: WidgetProps<Checklist>,
): React.ReactElement {
  const { payload, asset, path, drawnAt, onCitation, revision } = props;
  const edit = useWidgetEdit(props);
  const { analysisId } = revision;
  const store = useWidgetStates().entry(
    analysisId,
    path,
    "checklist-ticks",
    () =>
      tickStore((ancestor, descendant) =>
        workspaceCopy.isAncestor(analysisId, ancestor, descendant),
      ),
  );
  const state = useSyncExternalStore(store.subscribe, store.state, store.state);
  // A pinned version is not the tip's line, and a tick shows over none of its items.
  const shown = revision.pinned ? NO_TICKS : state;
  const views = payload.items.map((item) => itemView(shown, item, drawnAt));
  // Each render asks what its views are missing; both are no-ops once answered.
  useEffect(() => {
    for (const view of views) {
      for (const [ancestor, descendant] of view.asks)
        store.relate(ancestor, descendant);
      if (view.superseded !== undefined)
        store.dispatch({ kind: "superseded", ...view.superseded });
    }
  });

  const tick = (item: Checklist_Item, checked: boolean) => {
    if (edit === null) return Promise.resolve();
    return store.queue(
      { asset, path, drawnAt, itemId: item.id, checked },
      edit,
      store.dispatch,
    );
  };

  return (
    <ul className="divide-y divide-line-row rounded-[8px] border border-line-soft bg-surface-doc-pane">
      {payload.items.map((item, index) => {
        const view = views[index];
        return (
          <ChecklistRow
            key={item.id}
            item={item}
            view={view}
            failure={
              shown.failures.get(item.id) ?? unansweredReason(shown, view)
            }
            editable={edit !== null && !view.undecided}
            onTick={(checked) => void tick(item, checked)}
            onCitation={onCitation}
          />
        );
      })}
    </ul>
  );
}

function ChecklistRow({
  item,
  view,
  failure,
  editable,
  onTick,
  onCitation,
}: {
  item: Checklist_Item;
  view: ItemView;
  failure: string | undefined;
  editable: boolean;
  onTick: (checked: boolean) => void;
  onCitation: (citation: Citation) => void;
}): React.ReactElement {
  const citation = item.citation;
  return (
    <li className="flex items-start gap-[10px] px-[13px] py-[9px]">
      <input
        type="checkbox"
        checked={view.checked}
        disabled={!editable || view.marker === "saving"}
        onChange={(event) => onTick(event.target.checked)}
        aria-label={item.label}
        className="mt-[4px] size-[14px] shrink-0 accent-primary disabled:cursor-default"
      />
      <div className="min-w-0 flex-1 text-[14px] leading-[1.55] text-ink-body">
        <div>{item.label}</div>
        {citation !== undefined && (
          <div className="mt-[2px] text-[13px]">
            <CitationMark
              citation={
                citation.quote === ""
                  ? { kind: "paper", docId: citation.docId }
                  : {
                      kind: "quote",
                      docId: citation.docId,
                      quote: citation.quote,
                    }
              }
              onCitation={onCitation}
            >
              {citation.quote === "" ? "source" : `“${citation.quote}”`}
            </CitationMark>
          </div>
        )}
        {failure !== undefined && (
          <div role="alert" className="mt-[3px] text-[12.5px] text-error-text">
            {failure}
          </div>
        )}
      </div>
      {view.marker !== undefined && <TickState state={view.marker} />}
    </li>
  );
}

const TICK_STATE: Record<Tick["state"], { text: string; className: string }> = {
  saving: { text: "saving…", className: "text-ink-faint" },
  saved: { text: "saved", className: "text-status-done-fg" },
};

function TickState({ state }: { state: Tick["state"] }): React.ReactElement {
  const { text, className } = TICK_STATE[state];
  return (
    <span className={`mt-[3px] shrink-0 text-[11.5px] ${className}`}>
      {text}
    </span>
  );
}
