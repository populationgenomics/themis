"use client";

import type { Svcv4Classification } from "@/models/widgets";
import {
  JudgementFailure,
  NoteControl,
  ReviewedTick,
} from "../judgement-controls";
import type { RowGuards } from "./code-row";
import {
  classLabel,
  consequenceLabel,
  gateLevelLabel,
  inheritanceLabel,
  lettered,
  routeLabel,
  samePoints,
  signedPoints,
  type Tone,
} from "./model";

// The two sections of the classification around its codes: the routing, which says what was
// classified against what and along which paths, with the curator's tick and note on it; and the
// alternatives, the tallies the library computed under other values of the judgement inputs.

export function SectionHeading({
  children,
  aside,
}: {
  children: React.ReactNode;
  aside?: React.ReactNode;
}): React.ReactElement {
  return (
    <div className="flex flex-wrap items-center gap-x-[10px] gap-y-[6px] border-t border-line-soft bg-surface-warm-panel px-[14px] py-[6px]">
      <h4 className="m-0 flex-1 text-[12.5px] font-semibold text-ink-label">
        {children}
      </h4>
      {aside}
    </div>
  );
}

export function RoutingSection({
  payload,
  guards,
}: {
  payload: Svcv4Classification;
  guards: RowGuards;
}): React.ReactElement | null {
  const routing = payload.routing;
  if (routing === undefined) return null;
  const { variant, entity } = routing;
  const note = guards.note.view.value;
  return (
    <section data-section="routing">
      <SectionHeading
        aside={
          <>
            <ReviewedTick guard={guards.reviewed} label="the routing" />
            {note === "" && (
              <NoteControl guard={guards.note} label="the routing" />
            )}
          </>
        }
      >
        Routing
      </SectionHeading>
      <dl className="m-0 grid grid-cols-[88px_minmax(0,1fr)] gap-x-[12px] gap-y-[5px] px-[14px] py-[9px] text-[13.5px] leading-[1.5]">
        <dt className="text-[12px] text-ink-faint">Variant</dt>
        <dd className="m-0 min-w-0">
          <span className="font-mono text-[12.5px] text-ink-primary">
            {variant?.transcriptHgvs}
          </span>
          {variant?.proteinHgvs && (
            <span className="font-mono text-[12.5px] text-ink-muted">
              {" "}
              {variant.proteinHgvs}
            </span>
          )}
          <div className="font-mono text-[12px] text-ink-muted">
            {[
              variant?.geneSymbol,
              variant?.hgncId,
              variant?.genomicHgvs,
              variant?.caid,
            ]
              .filter((part) => part)
              .join(" · ")}
          </div>
        </dd>
        <dt className="text-[12px] text-ink-faint">Entity</dt>
        <dd className="m-0 min-w-0">
          {entity?.disease}{" "}
          <span className="font-mono text-[12.5px] text-ink-muted">
            {entity?.mondoId}
          </span>
          <div className="text-ink-muted">
            {entity &&
              [inheritanceLabel(entity.inheritance), entity.mechanism].join(
                " · ",
              )}
          </div>
          {entity && (
            <div className="text-ink-muted">
              {entity.validitySource}: {entity.validityClassification}, gate{" "}
              {gateLevelLabel(entity.gateLevel)}
              {payload.tally?.gateCapped && (
                <span className="text-amber-uncertainty-heading">
                  {" "}
                  · the gate capped the class
                </span>
              )}
            </div>
          )}
        </dd>
        <dt className="text-[12px] text-ink-faint">Paths</dt>
        <dd className="m-0 min-w-0">
          <div className="text-ink-muted">
            {consequenceLabel(routing.consequence)}
          </div>
          <ul className="m-0 flex list-none flex-col gap-[1px] p-0">
            {payload.paths.map((path) => (
              <li key={path.family}>
                <span className="font-mono text-[12.5px] text-ink-label">
                  {routeLabel(payload, path.family)}
                </span>{" "}
                <span className="font-mono text-[12.5px]">
                  {signedPoints(path.total)}
                </span>{" "}
                <span className="text-ink-muted">
                  {path.selected ? "counted" : "not counted"}
                  {[
                    path.mechanism && `mechanism ${path.mechanism}`,
                    path.exonRelevance &&
                      `exon relevance ${path.exonRelevance}`,
                    !samePoints(path.multiplier, "1") &&
                      `×${Number(path.multiplier)}`,
                  ]
                    .filter((part) => part)
                    .map((part) => ` · ${part}`)
                    .join("")}
                </span>
              </li>
            ))}
          </ul>
        </dd>
        <dt className="text-[12px] text-ink-faint">Why</dt>
        <dd className="m-0 min-w-0">{routing.rationale}</dd>
      </dl>
      {(note !== "" || guards.reviewed.failure || guards.note.failure) && (
        <div className="flex flex-col gap-[4px] px-[14px] pb-[9px]">
          <JudgementFailure failure={guards.reviewed.failure} />
          <JudgementFailure failure={guards.note.failure} />
          {note !== "" && (
            <NoteControl guard={guards.note} label="the routing" />
          )}
        </div>
      )}
    </section>
  );
}

const TONE_TEXT: Record<Tone, string> = {
  pathogenic: "text-svcv4-pathogenic-text",
  benign: "text-svcv4-benign-text",
  neutral: "text-ink-label",
};

export function toneText(tone: Tone): string {
  return TONE_TEXT[tone];
}

// The pinned block's height in a wide pane, for a jump made before it is measured.
const PINNED_NOMINAL_PX = 150;

export function AlternativesSection({
  payload,
  id,
  pinnedHeight,
}: {
  payload: Svcv4Classification;
  /** The section's id, which the ruler's caption links to. */
  id: string;
  /** The height of the block pinned above the codes, which a jump here has to clear; undefined
   *  until it is measured. */
  pinnedHeight: number | undefined;
}): React.ReactElement | null {
  const rows = lettered(payload);
  if (rows.length === 0) return null;
  return (
    <section
      id={id}
      data-section="alternatives"
      style={{ scrollMarginTop: (pinnedHeight ?? PINNED_NOMINAL_PX) + 8 }}
    >
      <SectionHeading>Other values of the judgement inputs</SectionHeading>
      <table className="w-full border-collapse text-[13.5px]">
        <thead>
          <tr className="text-left text-[12px] text-ink-faint">
            <th className="w-[44px] py-[5px] pr-[8px] pl-[14px] font-normal">
              <span className="sr-only">Letter</span>
            </th>
            <th className="py-[5px] font-normal">If</th>
            <th className="py-[5px] font-normal">Moves</th>
            <th className="py-[5px] pr-[14px] text-right font-normal">Tally</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ alternative, kind, mark }) => {
            const tally = alternative.tally;
            const label = tally ? classLabel(tally.finalClass) : undefined;
            const crosses = tally?.finalClass !== payload.tally?.finalClass;
            return (
              <tr
                key={mark}
                className="border-t border-line-row align-baseline"
              >
                <td className="py-[5px] pr-[8px] pl-[14px] font-mono text-[11.5px] text-ink-muted">
                  {mark}
                </td>
                <td className="py-[5px] pr-[10px]">
                  {alternative.assumption}
                  {kind === "open" && (
                    <span className="ml-[6px] text-[12px] text-amber-uncertainty-heading">
                      open
                    </span>
                  )}
                </td>
                <td className="py-[5px] pr-[10px] font-mono text-[12px] text-ink-muted">
                  {alternative.codes.join(" ")}
                </td>
                <td className="py-[5px] pr-[14px] text-right whitespace-nowrap">
                  <span className="font-mono text-[13px]">
                    {tally ? signedPoints(tally.total) : ""}
                  </span>{" "}
                  <span
                    className={`${label ? toneText(label.tone) : ""} ${crosses ? "font-semibold" : ""}`}
                  >
                    {label?.short}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
