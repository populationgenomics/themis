"use client";

import { type Citation, CitationMark } from "@/components/workbench/citation";
import {
  type Checklist,
  type Checklist_Item,
  ChecklistSchema,
} from "@/models/widgets";
import { addressName, type GuardAddress } from "@/widgets/guard-operation";
import { JudgementFailure, JudgementMarker } from "./judgement-controls";
import type { WidgetProps } from "./registry";
import { type GuardState, useJudgements } from "./use-judgements";

// A checklist the agent asks a curator to work through: each item's label as plain text, the paper
// or passage it rests on as the citation mark prose uses, and whether a curator has ticked it, with
// what a tick being published shows over it (use-judgements.ts).

export function ChecklistWidget(
  props: WidgetProps<Checklist>,
): React.ReactElement {
  const { payload, onCitation } = props;
  const guards = useJudgements(
    props,
    ChecklistSchema,
    payload.items.map(checkedOf),
  );
  return (
    <ul className="divide-y divide-line-row rounded-[8px] border border-line-soft bg-surface-doc-pane">
      {payload.items.map((item) => {
        const guard = guards.get(addressName(checkedOf(item)));
        if (guard === undefined) {
          throw new Error(`the checklist's item ${item.id} has no tick`);
        }
        return (
          <ChecklistRow
            key={item.id}
            item={item}
            guard={guard}
            onCitation={onCitation}
          />
        );
      })}
    </ul>
  );
}

function ChecklistRow({
  item,
  guard,
  onCitation,
}: {
  item: Checklist_Item;
  guard: GuardState;
  onCitation: (citation: Citation) => void;
}): React.ReactElement {
  const { view, failure, editable } = guard;
  const citation = item.citation;
  return (
    <li className="flex items-start gap-[10px] px-[13px] py-[9px]">
      <input
        type="checkbox"
        checked={view.value === true}
        disabled={!editable}
        onChange={(event) => guard.set(event.target.checked)}
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
          <div className="mt-[3px]">
            <JudgementFailure failure={failure} />
          </div>
        )}
      </div>
      {view.marker !== undefined && (
        <div className="mt-[3px] shrink-0">
          <JudgementMarker state={view.marker} />
        </div>
      )}
    </li>
  );
}

/** Where an item's tick sits in the checklist. */
function checkedOf(item: Checklist_Item): GuardAddress {
  return { steps: [{ field: "items", key: item.id }], guard: "checked" };
}
