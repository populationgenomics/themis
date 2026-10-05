"use client";

import { timestampDate } from "@bufbuild/protobuf/wkt";
import { useId } from "react";
import { Tooltip } from "@/components/ui/tooltip";
import { type Citation, CitationMark } from "@/components/workbench/citation";
import {
  AssessmentStatus,
  Confidence,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import type {
  Svcv4Classification,
  Svcv4Classification_Code,
  Svcv4Classification_Evidence,
} from "@/models/widgets";
import {
  JudgementFailure,
  NoteControl,
  ReviewedTick,
} from "../judgement-controls";
import type { GuardState } from "../use-judgements";
import {
  classLabel,
  confidenceLabel,
  lettered,
  pointsOf,
  samePoints,
  signedPoints,
  statusLabel,
} from "./model";

// One evidence code as a row of the classification: collapsed, the code, its title, its share of
// the balance on the ruler's scale, its points and how open the call is; expanded, the cell it was
// priced from, where the value came from, the evidence and the reasoning. The curator's tick and
// note sit on the row itself, and a note shows whether the row is open or not.

export interface RowGuards {
  reviewed: GuardState;
  note: GuardState;
}

export function CodeRow({
  payload,
  code,
  counted,
  guards,
  reviewReason,
  expanded,
  onToggle,
  highlighted,
  onHighlight,
  scale,
  onCitation,
}: {
  payload: Svcv4Classification;
  code: Svcv4Classification_Code;
  /** Whether the tally counts the code's points: false on a path the max-path rule did not pick. */
  counted: boolean;
  guards: RowGuards;
  /** Why a curator should look at the code; undefined where nothing asks them to. */
  reviewReason: string | undefined;
  expanded: boolean;
  onToggle: () => void;
  highlighted: boolean;
  onHighlight: (code: string | null) => void;
  /** The largest magnitude on the ruler, which the row's own bar is drawn against. */
  scale: number;
  onCitation: (citation: Citation) => void;
}): React.ReactElement {
  const details = useId();
  const scored = code.status === AssessmentStatus.SCORED;
  const note = guards.note.view.value;
  return (
    <li
      className={`border-t border-line-row first:border-t-0 transition-colors motion-reduce:transition-none ${highlighted ? "bg-surface-warm-panel" : ""}`}
      onMouseEnter={() => onHighlight(code.code)}
      onMouseLeave={() => onHighlight(null)}
    >
      <div className="flex flex-wrap items-center gap-x-[10px] gap-y-[4px] px-[14px] py-[7px]">
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={expanded ? details : undefined}
          onClick={onToggle}
          onFocus={() => onHighlight(code.code)}
          onBlur={() => onHighlight(null)}
          className="flex min-w-[min(100%,10rem)] flex-1 items-baseline gap-[10px] text-left focus-visible:outline-2 focus-visible:outline-ring"
        >
          <span
            aria-hidden
            className={`w-[10px] shrink-0 text-[10px] text-ink-faint transition-transform motion-reduce:transition-none ${expanded ? "rotate-90" : ""}`}
          >
            ▸
          </span>
          <span className="w-[66px] shrink-0 font-mono text-[12.5px] font-medium text-ink-primary">
            {code.code}
          </span>
          <span className="min-w-0 flex-1 text-[13.5px] leading-[1.4] text-ink-body">
            {code.title}
          </span>
        </button>
        <span className="hidden w-[56px] shrink-0 @[640px]:block">
          {scored && (
            <RowBar points={code.points} counted={counted} scale={scale} />
          )}
        </span>
        <span className="w-[64px] shrink-0 text-right @[560px]:w-[92px]">
          <Points code={code} counted={counted} />
        </span>
        <span className="shrink-0 @[560px]:w-[84px]">
          <Openness code={code} reviewReason={reviewReason} />
        </span>
        <ReviewedTick guard={guards.reviewed} label={code.code} compact />
        {note === "" && (
          <NoteControl guard={guards.note} label={code.code} compact />
        )}
      </div>
      {(note !== "" || guards.reviewed.failure || guards.note.failure) && (
        <div className="flex flex-col gap-[4px] px-[14px] pb-[8px] pl-[34px]">
          <JudgementFailure failure={guards.reviewed.failure} />
          <JudgementFailure failure={guards.note.failure} />
          {note !== "" && <NoteControl guard={guards.note} label={code.code} />}
        </div>
      )}
      {expanded && (
        <div id={details} className="px-[14px] pb-[12px] pl-[34px]">
          <Details
            payload={payload}
            code={code}
            counted={counted}
            onCitation={onCitation}
          />
        </div>
      )}
    </li>
  );
}

/** The row's share of the balance, drawn from a centre zero on the ruler's scale. */
function RowBar({
  points,
  counted,
  scale,
}: {
  points: string;
  counted: boolean;
  scale: number;
}): React.ReactElement {
  const value = points === "" ? 0 : pointsOf(points);
  const half = 28;
  const length = scale === 0 ? 0 : (Math.abs(value) / scale) * half;
  const fill = !counted
    ? "bg-ink-ghost"
    : value < 0
      ? "bg-svcv4-benign"
      : "bg-svcv4-pathogenic";
  return (
    <span aria-hidden className="relative block h-[8px] w-[56px]">
      <span className="absolute inset-y-[-2px] left-1/2 w-px bg-line-input" />
      {length > 0 && (
        <span
          className={`absolute inset-y-0 rounded-[2px] ${fill}`}
          style={
            value < 0
              ? { right: `${half}px`, width: `${length}px` }
              : { left: `${half}px`, width: `${length}px` }
          }
        />
      )}
    </span>
  );
}

function Points({
  code,
  counted,
}: {
  code: Svcv4Classification_Code;
  counted: boolean;
}): React.ReactElement {
  if (code.status !== AssessmentStatus.SCORED) {
    return (
      <span className="text-[12.5px] text-ink-faint">
        {statusLabel(code.status)}
      </span>
    );
  }
  const tone =
    !counted || pointsOf(code.points) === 0
      ? "text-ink-muted"
      : pointsOf(code.points) < 0
        ? "text-svcv4-benign-text"
        : "text-svcv4-pathogenic-text";
  return (
    <span className={`font-mono text-[13px] font-medium ${tone}`}>
      {signedPoints(code.points)}
      {!counted && (
        <span className="ml-[4px] hidden font-sans text-[11.5px] font-normal text-ink-faint @[560px]:inline">
          not counted
        </span>
      )}
    </span>
  );
}

function Openness({
  code,
  reviewReason,
}: {
  code: Svcv4Classification_Code;
  reviewReason: string | undefined;
}): React.ReactElement {
  const label = confidenceLabel(code.confidence);
  const uncertain =
    code.confidence === Confidence.LEANING ||
    code.confidence === Confidence.OPEN;
  const hover = [
    label === "" ? "" : `Confidence: ${label}`,
    reviewReason === undefined ? "" : `Needs review: ${reviewReason}`,
  ].filter((part) => part !== "");
  return (
    // The dot names its reason and the label is always read, so the panel repeats nothing for a
    // screen reader.
    <Tooltip
      describes={false}
      content={
        hover.length === 0
          ? ""
          : hover.map((part) => <div key={part}>{part}</div>)
      }
    >
      {/* At least 24px wide, so the confidence is reachable where only an empty box shows it. */}
      <span
        className={`-my-[4px] inline-flex min-w-[24px] items-center gap-[5px] py-[4px] text-[12px] ${uncertain ? "text-amber-uncertainty-heading" : "text-ink-faint"}`}
      >
        {reviewReason !== undefined && (
          <span
            role="img"
            aria-label={`needs review: ${reviewReason}`}
            className="size-[7px] shrink-0 rounded-full bg-amber-uncertainty-icon"
          />
        )}
        <span className="sr-only @[560px]:not-sr-only">{label}</span>
      </span>
    </Tooltip>
  );
}

function Details({
  payload,
  code,
  counted,
  onCitation,
}: {
  payload: Svcv4Classification;
  code: Svcv4Classification_Code;
  counted: boolean;
  onCitation: (citation: Citation) => void;
}): React.ReactElement {
  const scored = code.status === AssessmentStatus.SCORED;
  const moves = lettered(payload).filter(({ alternative }) =>
    alternative.codes.includes(code.code),
  );
  return (
    <dl className="grid grid-cols-[88px_minmax(0,1fr)] gap-x-[12px] gap-y-[7px] text-[13.5px] leading-[1.5]">
      {!scored && (
        <Entry term={statusLabel(code.status)}>{code.statusReason}</Entry>
      )}
      {scored && (
        <Entry term="Points">
          <span className="font-mono text-[13px]">
            {signedPoints(code.points)}
          </span>
          {!samePoints(code.rawPoints, code.points) && (
            <span className="text-ink-muted">
              {" "}
              from{" "}
              <span className="font-mono text-[13px]">
                {signedPoints(code.rawPoints)}
              </span>
              {code.adjustment !== "" && `, ${code.adjustment}`}
            </span>
          )}
          {samePoints(code.rawPoints, code.points) &&
            code.adjustment !== "" && (
              <span className="text-ink-muted"> ({code.adjustment})</span>
            )}
          {!counted && (
            <span className="text-ink-muted">
              {" "}
              · on the path the tally does not count
            </span>
          )}
        </Entry>
      )}
      {code.decision !== "" && <Entry term="Cell">{code.decision}</Entry>}
      {code.cells.length > 0 && (
        <Entry term={code.cells.length === 1 ? "Cell" : "Cells"}>
          <ul className="flex flex-col gap-[2px]">
            {code.cells.map((cell) => (
              <li key={cell.cellId}>
                <span className="font-mono text-[12.5px] text-ink-label">
                  {cell.count} × {cell.cellId}
                </span>{" "}
                <span className="text-ink-muted">{cell.description}</span>{" "}
                <span className="font-mono text-[12.5px] text-ink-muted">
                  {cell.pointsEach === ""
                    ? "not valued"
                    : `${signedPoints(cell.pointsEach)} each`}
                </span>
              </li>
            ))}
          </ul>
        </Entry>
      )}
      {code.basis !== "" && (
        <Entry term="Read from">
          <span className="font-mono text-[12.5px] text-ink-label">
            {code.basis}
          </span>
        </Entry>
      )}
      {code.evidence.length > 0 && (
        <Entry term="Evidence">
          <ul className="flex flex-col gap-[6px]">
            {code.evidence.map((item, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: evidence items carry no id, and the list is drawn as written
              <EvidenceItem key={index} item={item} onCitation={onCitation} />
            ))}
          </ul>
        </Entry>
      )}
      {code.rationale !== "" && <Entry term="Why">{code.rationale}</Entry>}
      {code.nearestAlternative !== undefined && (
        <Entry term="Not chosen">
          {code.nearestAlternative.cellId !== "" && (
            <span className="font-mono text-[12.5px] text-ink-label">
              {code.nearestAlternative.cellId}{" "}
            </span>
          )}
          {code.nearestAlternative.description}
          <div className="text-ink-muted">
            Ruled out: {code.nearestAlternative.reason}
          </div>
        </Entry>
      )}
      {confidenceLabel(code.confidence) !== "" && (
        <Entry term="Confidence">
          <span className="font-medium">
            {confidenceLabel(code.confidence)}
          </span>
          {code.confidenceNote !== "" && (
            <div className="text-ink-muted">{code.confidenceNote}</div>
          )}
        </Entry>
      )}
      {moves.length > 0 && (
        <Entry term="What moves it">
          <ul className="flex flex-col gap-[2px]">
            {moves.map(({ alternative, mark }) => (
              <li key={mark}>
                <span className="font-mono text-[12px] text-ink-muted">
                  {mark}
                </span>{" "}
                {alternative.assumption}:{" "}
                <span className="font-mono text-[13px]">
                  {alternative.tally
                    ? signedPoints(alternative.tally.total)
                    : ""}
                </span>{" "}
                {alternative.tally
                  ? classLabel(alternative.tally.finalClass).label
                  : ""}
              </li>
            ))}
          </ul>
        </Entry>
      )}
      {code.releases.length > 0 && (
        <Entry term="Releases">
          <span className="font-mono text-[12px] text-ink-muted">
            {code.releases
              .map((release) => `${release.source} ${release.datasetVersion}`)
              .join(" · ")}
          </span>
        </Entry>
      )}
    </dl>
  );
}

function Entry({
  term,
  children,
}: {
  term: string;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <>
      <dt className="pt-[1px] text-[12px] text-ink-faint first-letter:uppercase">
        {term}
      </dt>
      <dd className="m-0 min-w-0 whitespace-pre-wrap text-ink-body">
        {children}
      </dd>
    </>
  );
}

const SOURCE_TAG =
  "shrink-0 rounded-tag border px-[5px] py-[1px] font-mono text-[10.5px] leading-[1.4]";

function EvidenceItem({
  item,
  onCitation,
}: {
  item: Svcv4Classification_Evidence;
  onCitation: (citation: Citation) => void;
}): React.ReactElement {
  const source = item.source;
  return (
    <li className="flex items-start gap-[8px]">
      {source.case === "retrieval" && (
        <span
          className={`${SOURCE_TAG} border-line-primary bg-surface-inset text-ink-label`}
        >
          service
        </span>
      )}
      {source.case === "citation" && (
        <span
          className={`${SOURCE_TAG} border-agent-border bg-agent-tint text-agent-fg`}
        >
          paper
        </span>
      )}
      {source.case === "caseText" && (
        <span
          className={`${SOURCE_TAG} border-user-bubble-border bg-user-bubble-bg text-ink-label`}
        >
          case
        </span>
      )}
      {source.case === "webResult" && (
        <span
          className={`${SOURCE_TAG} border-amber-uncertainty-border bg-amber-uncertainty-bg text-amber-uncertainty-heading`}
        >
          web · unverified
        </span>
      )}
      {source.case === undefined && (
        <span className={`${SOURCE_TAG} border-line-primary text-ink-faint`}>
          unknown
        </span>
      )}
      <div className="min-w-0 flex-1">
        <div>{item.statement}</div>
        {source.case === "retrieval" && (
          <div className="font-mono text-[12px] text-ink-muted">
            {source.value.source} · {source.value.datasetVersions.join(", ")}
            {source.value.retrievedAt !== undefined &&
              ` · read ${timestampDate(source.value.retrievedAt).toISOString().slice(0, 10)}`}
          </div>
        )}
        {source.case === "citation" && (
          <div className="text-[13px]">
            <CitationMark
              citation={
                source.value.quote === ""
                  ? { kind: "paper", docId: source.value.docId }
                  : {
                      kind: "quote",
                      docId: source.value.docId,
                      quote: source.value.quote,
                    }
              }
              onCitation={onCitation}
            >
              {source.value.quote === "" ? "source" : `“${source.value.quote}”`}
            </CitationMark>
          </div>
        )}
        {source.case === "caseText" && (
          <div className="text-[13px] text-ink-muted">
            “{source.value.quote}”
          </div>
        )}
        {source.case === "webResult" && (
          <div className="break-all font-mono text-[12px] text-ink-muted">
            {source.value.title !== "" && (
              <span className="font-sans">{source.value.title} · </span>
            )}
            {source.value.url}
          </div>
        )}
      </div>
    </li>
  );
}
