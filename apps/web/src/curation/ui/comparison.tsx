"use client";

import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import type { ReactNode } from "react";
import {
  type Assessment,
  AssessmentSchema,
  type FieldValue,
  type RoutingAssessment,
  type VerdictAssessment,
  type WorkflowAssessment,
} from "@/gen/themis/curation/models/curation_pb";
import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";
import {
  AssessmentStatus,
  Classification,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import { consequenceLabel } from "../consequence";
import { ROUTING_ID, VERDICT_ID } from "../sections";
import { WORKFLOWS_VERSION } from "../version";
import { ALL_WORKFLOWS, workflowById } from "../workflows/registry";
import type { WorkflowDef } from "../workflows/types";
import { ClinicalContextPanel } from "./clinical-context";
import { classificationLabel } from "./worksheet";

// The sections of the comparison page (app/curation/compare). A client module because the workflow
// registry is one: its workflows carry their bodies, which a server component cannot import.

/** One curator's submission as the page hands it over. */
export interface CuratorAnswers {
  curator: string;
  workflowsVersion: string;
  /** Proto-JSON of each submitted assessment, by workflow id. */
  entries: Record<string, JsonValue>;
}

/** One curator's submission, split into the worksheet's two sections and its workflows. */
export interface Submitted {
  curator: string;
  workflowsVersion: string;
  routing?: RoutingAssessment;
  verdict?: VerdictAssessment;
  workflows: Map<string, WorkflowAssessment>;
}

const SECTION_KINDS: ReadonlyMap<string, Assessment["kind"]["case"]> = new Map([
  [ROUTING_ID, "routing"],
  [VERDICT_ID, "verdict"],
]);

/** Decodes a submission. Refuses one holding an assessment of the wrong kind under an id: the
 *  worksheet stores each section under its own id and every workflow as a workflow, so a mismatch
 *  is a fault in what was stored, not an answer to draw. */
export function decodeSubmitted(answers: CuratorAnswers): Submitted {
  const submitted: Submitted = {
    curator: answers.curator,
    workflowsVersion: answers.workflowsVersion,
    workflows: new Map(),
  };
  for (const [id, json] of Object.entries(answers.entries)) {
    const { kind } = fromJson(AssessmentSchema, json);
    const expected = SECTION_KINDS.get(id) ?? "workflow";
    if (kind.case !== expected) {
      throw new Error(
        `${answers.curator}'s submission holds ${kind.case ?? "no assessment"} under ${id}, where a ${expected} assessment belongs`,
      );
    }
    switch (kind.case) {
      case "routing":
        submitted.routing = kind.value;
        break;
      case "verdict":
        submitted.verdict = kind.value;
        break;
      case "workflow":
        submitted.workflows.set(id, kind.value);
        break;
    }
  }
  return submitted;
}

/** The workflows any curator answered, in registry order; ids the registry no longer holds follow,
 *  in id order. */
export function workflowOrder(submitted: Submitted[]): string[] {
  const answered = new Set(submitted.flatMap((s) => [...s.workflows.keys()]));
  const known = ALL_WORKFLOWS.filter((w) => answered.has(w.id)).map(
    (w) => w.id,
  );
  const retired = [...answered]
    .filter((id) => workflowById(id) === undefined)
    .sort();
  return [...known, ...retired];
}

/** A section's tag: whether the curators agree, and in what words. */
export interface Agreement {
  agreed: boolean;
  label: string;
}

/** Whether every curator stated an answer and all stated the same one: two unstated answers are
 *  not a shared one. */
export function agreed<T>(stated: (T | undefined)[], unstated: T): boolean {
  return (
    stated.every((value) => value !== undefined && value !== unstated) &&
    new Set(stated).size === 1
  );
}

/** What to print after a field's label, or undefined where the label already says it.
 *
 *  A count or a typed number (POP_FRQ's DAFT and FAF) is stored under its own row's or input's id,
 *  and its value is what was typed. A selection is stored under its table's field id, never a row's,
 *  and its value is the chosen row's id — which the label, that row's own, already names. Only the
 *  registry knows which ids are rows, and only for its own transcription, so a worksheet pinned to
 *  another version, or a workflow the registry no longer holds, prints every value. */
export function printedValue(
  field: FieldValue,
  workflow: WorkflowDef | undefined,
  workflowsVersion: string,
): string | undefined {
  if (field.value === "") return undefined;
  if (workflow === undefined || workflowsVersion !== WORKFLOWS_VERSION) {
    return field.value;
  }
  const typed = [...workflow.cells, ...(workflow.inputs ?? [])].some(
    (cell) => cell.id === field.fieldId,
  );
  return typed ? field.value : undefined;
}

/** Whether the curators gave one workflow the same answer: an answer from every curator, the same
 *  status, and for a scored workflow the same stored fields — the same cells first, then the same
 *  field ids and values in them. A workflow some curator left unanswered is never agreement. Only a
 *  scored workflow's fields are its answer: a status changed after filling them leaves them stored,
 *  and they are not what the curator decided.
 *
 *  Read off the stored fields, never off `printedValue`: which values print depends on the version
 *  a worksheet pins, and the same row chosen under two versions is still the same answer. */
export function workflowAgreement(
  answers: (WorkflowAssessment | undefined)[],
): Agreement {
  const given = answers.filter(
    (a): a is WorkflowAssessment =>
      a !== undefined && a.status !== AssessmentStatus.UNSPECIFIED,
  );
  if (given.length < answers.length) {
    return { agreed: false, label: "not answered by every curator" };
  }
  if (new Set(given.map((a) => a.status)).size > 1) {
    return { agreed: false, label: "different status" };
  }
  if (given[0].status !== AssessmentStatus.SCORED) {
    return { agreed: true, label: "same answer" };
  }
  const stored = (key: (field: FieldValue) => string) =>
    new Set(given.map((a) => a.fields.map(key).sort().join("\n"))).size;
  if (stored((f) => f.cellId) > 1) {
    return { agreed: false, label: "different cells" };
  }
  if (stored((f) => [f.fieldId, f.cellId, f.value].join("\t")) > 1) {
    return { agreed: false, label: "different values" };
  }
  return { agreed: true, label: "same answer" };
}

/** Each item of a list that may name one item twice, keyed so a repeat cannot collide with the
 *  first. */
export function keyedByOccurrence(
  items: string[],
): { item: string; key: string }[] {
  const seen = new Map<string, number>();
  return items.map((item) => {
    const count = (seen.get(item) ?? 0) + 1;
    seen.set(item, count);
    return { item, key: `${item}#${count}` };
  });
}

export function Comparison({
  clinicalContext,
  answers,
}: {
  /** The registration's, which every curator answered from. */
  clinicalContext: string;
  answers: CuratorAnswers[];
}) {
  const submitted = answers.map(decodeSubmitted);
  // Answers given against different transcriptions say so beside each curator's name: the reader
  // has to know a row's wording may have moved between them.
  const versioned = new Set(submitted.map((s) => s.workflowsVersion)).size > 1;
  return (
    <div className="space-y-4">
      <ClinicalContextPanel
        value={clinicalContext}
        heading={<SectionTitle>Clinical context</SectionTitle>}
        note="stated by the registration"
      />
      <RoutingComparison submitted={submitted} versioned={versioned} />
      {workflowOrder(submitted).map((workflowId) => (
        <WorkflowComparison
          key={workflowId}
          workflowId={workflowId}
          submitted={submitted}
          versioned={versioned}
        />
      ))}
      <VerdictComparison submitted={submitted} versioned={versioned} />
    </div>
  );
}

interface SectionProps {
  submitted: Submitted[];
  /** Whether the curators' worksheets pin different transcriptions. */
  versioned: boolean;
}

/** One section: a heading, the tag saying whether the curators agree where agreement is read, and
 *  the curators side by side. */
function ComparisonSection({
  heading,
  agreement,
  children,
}: {
  heading: ReactNode;
  agreement?: Agreement;
  children: ReactNode;
}) {
  const diverged = agreement !== undefined && !agreement.agreed;
  return (
    <section
      className={`rounded-md border bg-white p-5 ${diverged ? "border-amber-uncertainty-border" : "border-line-primary"}`}
    >
      <div className="mb-3 flex items-baseline justify-between gap-3 border-line-row border-b pb-2">
        <span>{heading}</span>
        {agreement ? (
          <span
            className={`framework-voice text-[12.5px] ${diverged ? "text-amber-uncertainty-heading" : "text-ink-faint"}`}
          >
            {agreement.label}
          </span>
        ) : null}
      </div>
      <div className="grid gap-5 md:grid-cols-2">{children}</div>
    </section>
  );
}

function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <span className="framework-voice text-[14px] text-ink-primary">
      {children}
    </span>
  );
}

/** A workflow by the registry's code and title. */
function WorkflowName({ id }: { id: string }) {
  const workflow = workflowById(id);
  // A retired id is printed as stored: that is the data, not a stand-in for a name.
  if (workflow === undefined) {
    return <span className="font-mono text-[12px] text-ink-faint">{id}</span>;
  }
  return (
    <>
      <span className="font-mono text-[12px] text-ink-faint">
        {workflow.code}
      </span>
      <span className="framework-voice ml-2 text-[14px] text-ink-primary">
        {workflow.title}
      </span>
    </>
  );
}

/** The curator a column is, with the transcription their worksheet pins where the curators'
 *  differ, and anything the section adds. */
function CuratorHeading({
  submitted,
  versioned,
  detail,
}: {
  submitted: Submitted;
  versioned: boolean;
  detail?: string;
}) {
  return (
    <p className="framework-voice mb-1 text-[12.5px] text-ink-faint">
      {[
        submitted.curator,
        versioned ? submitted.workflowsVersion : undefined,
        detail,
      ]
        .filter((part) => part !== undefined)
        .join(" · ")}
    </p>
  );
}

function NothingRecorded() {
  return (
    <p className="framework-voice text-[13px] text-ink-faintest">
      nothing recorded
    </p>
  );
}

function CuratorProse({ children }: { children: string }) {
  return (
    <p className="curator-voice border-line-row border-l-2 pl-3 text-[15px] text-ink-body">
      {children}
    </p>
  );
}

function Eyebrow({ children }: { children: string }) {
  return <p className="field-eyebrow mb-1 text-ink-label">{children}</p>;
}

/** The two routings: the consequence class each curator stated, and their reasoning, which is also
 *  where a curator argues that another entity is the question to ask. */
function RoutingComparison({ submitted, versioned }: SectionProps) {
  const same = agreed(
    submitted.map((s) => s.routing?.consequenceClass),
    Consequence.UNSPECIFIED,
  );
  return (
    <ComparisonSection
      heading={<SectionTitle>Routing</SectionTitle>}
      agreement={{
        agreed: same,
        label: same
          ? "same consequence class"
          : "not the same consequence class",
      }}
    >
      {submitted.map((s) => (
        <div key={s.curator}>
          <CuratorHeading submitted={s} versioned={versioned} />
          {s.routing ? (
            <>
              <p className="framework-voice mb-2 text-[13px] text-ink-body">
                Consequence class:{" "}
                {consequenceLabel(s.routing.consequenceClass)}
              </p>
              {s.routing.rationale ? (
                <CuratorProse>{s.routing.rationale}</CuratorProse>
              ) : null}
            </>
          ) : (
            <NothingRecorded />
          )}
        </div>
      ))}
    </ComparisonSection>
  );
}

function WorkflowComparison({
  workflowId,
  submitted,
  versioned,
}: SectionProps & { workflowId: string }) {
  const workflow = workflowById(workflowId);
  const answers = submitted.map((s) => ({
    submitted: s,
    assessment: s.workflows.get(workflowId),
  }));
  return (
    <ComparisonSection
      heading={<WorkflowName id={workflowId} />}
      agreement={workflowAgreement(answers.map((a) => a.assessment))}
    >
      {answers.map((answer) => (
        <CuratorAnswer
          key={answer.submitted.curator}
          submitted={answer.submitted}
          versioned={versioned}
          assessment={answer.assessment}
          workflow={workflow}
        />
      ))}
    </ComparisonSection>
  );
}

const STATUSES: ReadonlyMap<AssessmentStatus, string> = new Map([
  [AssessmentStatus.UNSPECIFIED, "not answered"],
  [AssessmentStatus.SCORED, "scored"],
  [AssessmentStatus.NOT_APPLICABLE, "not applicable"],
  [AssessmentStatus.NO_DATA, "no data"],
]);

function statusLabel(assessment: WorkflowAssessment | undefined): string {
  if (assessment === undefined) return "not answered";
  const label = STATUSES.get(assessment.status);
  if (label === undefined) {
    throw new Error(
      `${assessment.status} is not a status the worksheet offers`,
    );
  }
  return label;
}

function CuratorAnswer({
  submitted,
  versioned,
  assessment,
  workflow,
}: {
  submitted: Submitted;
  versioned: boolean;
  assessment?: WorkflowAssessment;
  workflow: WorkflowDef | undefined;
}) {
  const scored = assessment?.status === AssessmentStatus.SCORED;
  return (
    <div>
      <CuratorHeading
        submitted={submitted}
        versioned={versioned}
        detail={statusLabel(assessment)}
      />
      {assessment ? (
        <>
          {scored ? (
            <ul className="mb-2 space-y-0.5">
              {assessment.fields.map((field) => {
                const value = printedValue(
                  field,
                  workflow,
                  submitted.workflowsVersion,
                );
                return (
                  <li
                    key={field.fieldId}
                    className="framework-voice text-[13px] text-ink-body"
                  >
                    {field.label}
                    {value !== undefined ? (
                      <span className="ml-1 font-mono text-ink-muted">
                        {value}
                      </span>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          ) : null}
          {!scored && assessment.statusReason ? (
            <div className="mb-2">
              <Eyebrow>Why not</Eyebrow>
              <CuratorProse>{assessment.statusReason}</CuratorProse>
            </div>
          ) : null}
          {assessment.rationale ? (
            <CuratorProse>{assessment.rationale}</CuratorProse>
          ) : null}
        </>
      ) : (
        <NothingRecorded />
      )}
    </div>
  );
}

/** The two verdicts: the class each curator stated, their reasoning, and the calls they hold
 *  class-determinative — what says whether a divergence above could have moved the class at all. */
function VerdictComparison({ submitted, versioned }: SectionProps) {
  const same = agreed(
    submitted.map((s) => s.verdict?.classification),
    Classification.UNSPECIFIED,
  );
  return (
    <ComparisonSection
      heading={<SectionTitle>Verdict</SectionTitle>}
      agreement={{
        agreed: same,
        label: same ? "same classification" : "different classification",
      }}
    >
      {submitted.map((s) => (
        <div key={s.curator}>
          <CuratorHeading submitted={s} versioned={versioned} />
          {s.verdict ? (
            <>
              <p className="framework-voice mb-2 text-[13px] text-ink-body">
                Classification: {classificationLabel(s.verdict.classification)}
              </p>
              {s.verdict.rationale ? (
                <CuratorProse>{s.verdict.rationale}</CuratorProse>
              ) : null}
              {s.verdict.classDeterminative.length > 0 ? (
                <div className="mt-3">
                  <Eyebrow>Class-determinative</Eyebrow>
                  <ul className="space-y-0.5">
                    {keyedByOccurrence(s.verdict.classDeterminative).map(
                      ({ item, key }) => (
                        <li key={key}>
                          <WorkflowName id={item} />
                        </li>
                      ),
                    )}
                  </ul>
                </div>
              ) : null}
            </>
          ) : (
            <NothingRecorded />
          )}
        </div>
      ))}
    </ComparisonSection>
  );
}
