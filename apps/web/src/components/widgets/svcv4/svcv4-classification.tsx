"use client";

import { useId, useRef, useState } from "react";
import { Tooltip } from "@/components/ui/tooltip";
import {
  type Svcv4Classification,
  Svcv4ClassificationSchema,
} from "@/models/widgets";
import { addressName, type GuardAddress } from "@/widgets/guard-operation";
import {
  JudgementFailure,
  NoteControl,
  ReviewedTick,
} from "../judgement-controls";
import type { WidgetProps } from "../registry";
import { type GuardState, useJudgements } from "../use-judgements";
import { CodeRow, type RowGuards } from "./code-row";
import {
  classLabel,
  codeGroups,
  counted,
  decisiveMarks,
  domain,
  inheritanceLabel,
  reviewReason,
  signedPoints,
} from "./model";
import { Ruler } from "./ruler";
import {
  AlternativesSection,
  RoutingSection,
  SectionHeading,
  toneText,
} from "./sections";
import { useBox } from "./use-box";

// One SVCv4 classification, as the working document draws it (docs/design/svcv4-classification-
// widget.md): the class and total, the points ruler pinned while the curator scrolls, the routing,
// every code the paths taken admit in the payload's order, and the tallies under other values of
// the judgement inputs. A curator ticks and notes the whole record, the routing and each code.

const ROOT: readonly GuardAddress[] = [
  { steps: [], guard: "reviewed" },
  { steps: [], guard: "note" },
];
const ROUTING: readonly GuardAddress[] = [
  { steps: [{ field: "routing" }], guard: "reviewed" },
  { steps: [{ field: "routing" }], guard: "note" },
];

function codeAddress(code: string, guard: "reviewed" | "note"): GuardAddress {
  return { steps: [{ field: "codes", key: code }], guard };
}

function pair(
  guards: ReadonlyMap<string, GuardState>,
  [reviewed, note]: readonly GuardAddress[],
): RowGuards {
  const tick = guards.get(addressName(reviewed));
  const text = guards.get(addressName(note));
  if (tick === undefined || text === undefined) {
    throw new Error(`no guards drawn at ${addressName(reviewed)}`);
  }
  return { reviewed: tick, note: text };
}

export function Svcv4ClassificationWidget(
  props: WidgetProps<Svcv4Classification>,
): React.ReactElement {
  const { payload, onCitation } = props;
  const codeGuards = payload.codes.map((code) => [
    codeAddress(code.code, "reviewed"),
    codeAddress(code.code, "note"),
  ]);
  const guards = useJudgements(props, Svcv4ClassificationSchema, [
    ...ROOT,
    ...ROUTING,
    ...codeGuards.flat(),
  ]);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [onlyNeedingReview, setOnlyNeedingReview] = useState(false);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const alternativesId = useId();
  const pinned = useRef<HTMLDivElement>(null);
  const pinnedBox = useBox(pinned);

  const record = pair(guards, ROOT);
  const decisive = decisiveMarks(payload);
  const rowGuards = new Map(
    payload.codes.map((code, index) => [
      code.code,
      pair(guards, codeGuards[index]),
    ]),
  );
  const reviewedCodes = payload.codes.filter(
    (code) => rowGuards.get(code.code)?.reviewed.view.value === true,
  ).length;
  const needing = new Map<string, string>();
  for (const code of payload.codes) {
    const reason = reviewReason(
      code,
      decisive,
      rowGuards.get(code.code)?.reviewed.view.value === true,
    );
    if (reason !== undefined) needing.set(code.code, reason);
  }
  const [low, high] = domain(payload);
  const scale = Math.max(Math.abs(low), Math.abs(high));
  const reported = classLabel(payload.classification);
  // With an input open, every surviving value's tally is reported, the one the codes sum to first.
  const reportedTallies = [
    payload.tally,
    ...payload.openValues.map((value) => value.tally),
  ].filter((tally) => tally !== undefined);
  const variant = payload.routing?.variant;
  const entity = payload.routing?.entity;
  const toggle = (code: string) =>
    setExpanded((open) => {
      const next = new Set(open);
      if (!next.delete(code)) next.add(code);
      return next;
    });

  return (
    <section
      data-widget="svcv4-classification"
      aria-label={`SVCv4 classification of ${variant?.transcriptHgvs ?? "the variant"}`}
      className="@container my-[16px] rounded-card border border-line-primary bg-white text-ink-body"
    >
      <header data-section="summary" className="px-[14px] pt-[11px] pb-[9px]">
        <div className="flex flex-wrap items-center gap-x-[10px] gap-y-[4px]">
          <span className="font-mono text-[11px] tracking-[0.02em] text-ink-faint">
            {payload.framework?.name ?? "SVCv4"} classification
          </span>
          {/* The footer states the usage in full, so the panel repeats it for no reader. */}
          <Tooltip content={payload.framework?.usage} describes={false}>
            <span className="rounded-tag border border-amber-uncertainty-border bg-amber-uncertainty-bg px-[5px] text-[11px] text-amber-uncertainty-heading">
              draft framework, evaluation only
            </span>
          </Tooltip>
          <span className="flex-1" />
          <ReviewedTick
            guard={record.reviewed}
            label="the whole classification"
          />
          {record.note.view.value === "" && (
            <NoteControl guard={record.note} label="the whole classification" />
          )}
        </div>
        <div className="mt-[4px] flex flex-wrap items-baseline gap-x-[12px]">
          <h3
            className={`m-0 text-[20px] font-semibold leading-[1.25] ${toneText(reported.tone)}`}
          >
            {reported.label}
          </h3>
          {reportedTallies.length === 1 && (
            <span className="font-mono text-[18px] font-medium text-ink-primary">
              {signedPoints(reportedTallies[0].total)}
            </span>
          )}
          {reportedTallies.length > 1 && (
            <span className="text-[14px] text-ink-muted">
              {reportedTallies.map((tally, index) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: the tallies are the reported values in order
                <span key={index}>
                  {index > 0 && " or "}
                  <span className="font-mono text-[16px] font-medium text-ink-primary">
                    {signedPoints(tally.total)}
                  </span>{" "}
                  <span className={toneText(classLabel(tally.finalClass).tone)}>
                    {classLabel(tally.finalClass).short}
                  </span>
                </span>
              ))}
            </span>
          )}
        </div>
        <div className="mt-[2px] text-[13px] text-ink-muted">
          <span className="font-mono text-[12.5px] text-ink-label">
            {variant?.transcriptHgvs}
          </span>
          {[
            variant?.geneSymbol,
            entity?.disease,
            entity && inheritanceLabel(entity.inheritance),
          ]
            .filter((part) => part)
            .map((part) => ` · ${part}`)
            .join("")}
        </div>
        <div className="mt-[6px] flex flex-col gap-[4px]">
          <JudgementFailure failure={record.reviewed.failure} />
          <JudgementFailure failure={record.note.failure} />
          {record.note.view.value !== "" && (
            <NoteControl guard={record.note} label="the whole classification" />
          )}
        </div>
      </header>
      <div
        ref={pinned}
        className="sticky top-0 z-10 border-t border-line-soft bg-white/95 px-[14px] pt-[9px] pb-[7px] backdrop-blur-[2px]"
      >
        <Ruler
          payload={payload}
          highlighted={highlighted}
          onHighlight={setHighlighted}
          alternativesId={alternativesId}
        />
        <div className="mt-[6px] flex flex-wrap items-center gap-x-[14px] gap-y-[4px] text-[12.5px] text-ink-muted">
          <span>
            {reviewedCodes} of {payload.codes.length} codes reviewed
            {needing.size > 0 && (
              <>
                {" · "}
                <span className="text-amber-uncertainty-heading">
                  {needing.size} need review
                </span>
              </>
            )}
          </span>
          <span className="flex-1" />
          <label className="inline-flex cursor-pointer items-center gap-[6px]">
            <input
              type="checkbox"
              checked={onlyNeedingReview}
              onChange={(event) => setOnlyNeedingReview(event.target.checked)}
              className="size-[13px] accent-primary"
            />
            Show only codes needing review
          </label>
        </div>
      </div>
      <RoutingSection payload={payload} guards={pair(guards, ROUTING)} />
      <div data-section="codes">
        {codeGroups(payload).map((group) => {
          // A row the curator is working on stays while they do: its tick saving or saved, its note written.
          const shown = group.codes.filter((code) => {
            if (!onlyNeedingReview || needing.has(code.code)) return true;
            const guard = rowGuards.get(code.code);
            return (
              guard !== undefined &&
              (guard.reviewed.view.marker !== undefined ||
                guard.note.view.marker !== undefined ||
                guard.note.draft !== undefined)
            );
          });
          if (shown.length === 0) return null;
          return (
            <section key={group.heading}>
              <SectionHeading
                aside={
                  group.path && (
                    <span className="text-[12px] text-ink-muted">
                      <span className="font-mono">
                        {signedPoints(group.path.total)}
                      </span>{" "}
                      {group.path.selected ? "counted" : "not counted"}
                    </span>
                  )
                }
              >
                {group.path
                  ? `Variant effect · ${group.heading}`
                  : group.heading}
              </SectionHeading>
              <ul className="m-0 list-none p-0">
                {shown.map((code) => {
                  const guard = rowGuards.get(code.code);
                  if (guard === undefined)
                    throw new Error(`no guards for ${code.code}`);
                  return (
                    <CodeRow
                      key={code.code}
                      payload={payload}
                      code={code}
                      counted={counted(payload, code)}
                      guards={guard}
                      reviewReason={needing.get(code.code)}
                      expanded={expanded.has(code.code)}
                      onToggle={() => toggle(code.code)}
                      highlighted={highlighted === code.code}
                      onHighlight={setHighlighted}
                      scale={scale}
                      onCitation={onCitation}
                    />
                  );
                })}
              </ul>
            </section>
          );
        })}
      </div>
      {onlyNeedingReview && needing.size === 0 && (
        <p className="m-0 border-t border-line-soft px-[14px] py-[10px] text-[13px] text-ink-muted">
          No code needs review: every uncertain or class-deciding code is
          ticked.
        </p>
      )}
      <AlternativesSection
        payload={payload}
        id={alternativesId}
        pinnedHeight={pinnedBox?.height}
      />
      <footer className="rounded-b-card border-t border-line-soft bg-surface-warm-panel px-[14px] py-[8px] text-[12px] leading-[1.5] text-ink-muted">
        {payload.framework?.usage}
        {payload.framework && (
          <span className="font-mono text-[11px] text-ink-faint">
            {" "}
            {payload.framework.status} · citations at{" "}
            {payload.framework.citationsRepository}@
            {payload.framework.citationsRevision.slice(0, 10)}
          </span>
        )}
      </footer>
    </section>
  );
}
