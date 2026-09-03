"use client";

import type { ReactNode } from "react";
import {
  AssessmentStatus,
  Confidence,
  type FieldValue,
  type WorkflowAssessment,
} from "@/gen/themis/curation/models/curation_pb";

// The worksheet's presentational vocabulary. Two rules run through all of it:
//
//   - no points, no totals, no running score, and no option is styled as preferable to another;
//   - transcribed framework wording is `framework-voice`, what the curator writes is
//     `curator-voice` (curation.css).

/** One cell of a transcribed workflow: a decision-tree row, or a control that answers one. `cell`
 *  is the framework's own name for it, and what a round joins a run's stated decision against. */
export interface Cell {
  id: string;
  cell: string;
  /** The description the calculator's rowspan carries over this row and its neighbours; consecutive
   *  cells carrying the same one are one section. */
  group?: string;
  label: string;
  /** A qualifier the calculator prints beside the label, in the row's own columns — a zygosity, a
   *  phase condition. Not a description carried over several rows: that is `group`. */
  detail?: string;
  /** Where the framework defines the row by a ratio rather than by a description, the ratio: the
   *  smallest FAF/DAFT multiple (inclusive) the row covers, and whether it bars the rarity-gated
   *  codes. Only POP_FRQ's frequency rows carry it. */
  ratio?: { minMultiple: number; barsRarityGatedCodes: boolean };
}

/** What the calculator prints for one cell, read left to right: the description its section carries,
 *  the row, the row's own qualifier. What the stored answer and the cell inventory both record —
 *  a row's own text ("First LP Variant") names a position in a count, not a criterion. */
export function cellLabel(cell: Cell): string {
  return [cell.group, cell.label, cell.detail]
    .filter((part): part is string => part !== undefined)
    .join(" — ");
}

/** A run of consecutive cells under one section description, or a run under none. */
export interface Section {
  group?: string;
  cells: Cell[];
}

/** The rows of one table split into the sections the calculator's rowspans draw: maximal runs of
 *  cells sharing a `group`.
 *
 *  Refuses a table whose sections cannot be drawn — a blank description, or one resuming after
 *  another has intervened, which would print the same heading twice with nothing to say which rows
 *  each covers. Refused here rather than over a workflow's cells, because only the array a table is
 *  handed says what is consecutive on screen. */
export function sections(cells: Cell[]): Section[] {
  const found: Section[] = [];
  const closed = new Set<string>();
  for (const cell of cells) {
    if (cell.group !== undefined && cell.group.trim() === "") {
      throw new Error(
        `the curation cell ${cell.cell} carries a blank section description`,
      );
    }
    const open = found.at(-1);
    if (open !== undefined && open.group === cell.group) {
      open.cells.push(cell);
      continue;
    }
    if (cell.group !== undefined && closed.has(cell.group)) {
      throw new Error(
        `the curation cell ${cell.cell} resumes a section another has closed`,
      );
    }
    if (open?.group !== undefined) closed.add(open.group);
    found.push({ group: cell.group, cells: [cell] });
  }
  return found;
}

/** A `FieldValue` for one cell, carrying the label the curator actually read. */
export function fieldValue(cell: Cell, value: string): FieldValue {
  return {
    $typeName: "themis.curation.models.curation.FieldValue",
    fieldId: cell.id,
    cellId: cell.cell,
    label: cellLabel(cell),
    value,
  };
}

export function readField(
  assessment: WorkflowAssessment,
  fieldId: string,
): string {
  return assessment.fields.find((f) => f.fieldId === fieldId)?.value ?? "";
}

export function withField(
  assessment: WorkflowAssessment,
  cell: Cell,
  value: string,
): WorkflowAssessment {
  const next = assessment.fields.filter((f) => f.fieldId !== cell.id);
  if (value !== "") next.push(fieldValue(cell, value));
  next.sort((a, b) => a.fieldId.localeCompare(b.fieldId));
  return { ...assessment, fields: next };
}

export function withoutField(
  assessment: WorkflowAssessment,
  fieldId: string,
): WorkflowAssessment {
  return {
    ...assessment,
    fields: assessment.fields.filter((f) => f.fieldId !== fieldId),
  };
}

/** A note the calculator prints under a workflow, verbatim. Indigo is the app's "the system is
 *  speaking" colour, which is what a framework condition is. */
export function FrameworkNote({ children }: { children: ReactNode }) {
  return (
    <p className="framework-voice mt-3 border-agent-border border-l-2 bg-agent-tint/50 py-1.5 pl-3 text-[13px] text-ink-muted leading-relaxed">
      {children}
    </p>
  );
}

/** The three answers a workflow can carry. `no data` and `not applicable` are findings, not
 *  omissions, so they sit beside `scored` rather than being the absence of it. */
export function StatusPicker({
  value,
  onChange,
  disabled,
}: {
  value: AssessmentStatus;
  onChange: (next: AssessmentStatus) => void;
  /** Set where a framework precondition has already decided the status, so it is not the curator's
   *  to change — the rarity gate. */
  disabled?: boolean;
}) {
  const options: [AssessmentStatus, string, string][] = [
    [
      AssessmentStatus.SCORED,
      "Scored",
      "The workflow applies and I have assessed it",
    ],
    [
      AssessmentStatus.NOT_APPLICABLE,
      "Not applicable",
      "A framework precondition bars this code",
    ],
    [
      AssessmentStatus.NO_DATA,
      "No data",
      "It applies, but there is nothing to assess it on",
    ],
  ];
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map(([status, label, title]) => (
        <button
          key={status}
          type="button"
          title={title}
          disabled={disabled}
          aria-pressed={value === status}
          onClick={() => onChange(status)}
          className={`framework-voice rounded-sm border px-2.5 py-1 text-[13px] transition-colors disabled:cursor-not-allowed ${
            value === status
              ? "border-primary bg-primary text-primary-foreground"
              : "border-line-input bg-white text-ink-muted enabled:hover:border-ink-ghost disabled:opacity-40"
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

/** One section of a table: the description on a line of its own, the rows it covers indented under
 *  it. A run with no description is drawn flush, so the indent is what ties a row to a heading. */
function SectionBlock({
  group,
  children,
}: {
  group?: string;
  children: ReactNode;
}) {
  return (
    <div>
      {group !== undefined ? (
        <p className="framework-voice py-2.5 pr-2 font-medium text-[13.5px] text-ink-body leading-snug">
          {group}
        </p>
      ) : null}
      <div
        className={
          group !== undefined
            ? "divide-y divide-line-row pl-4"
            : "divide-y divide-line-row"
        }
      >
        {children}
      </div>
    </div>
  );
}

/** A radio group over decision-tree cells. */
export function ChoiceRows({
  name,
  cells,
  value,
  onChange,
  onBlur,
}: {
  name: string;
  cells: Cell[];
  value: string;
  onChange: (cell: Cell) => void;
  onBlur?: () => void;
}) {
  return (
    <div className="divide-y divide-line-row border-line-row border-t">
      {sections(cells).map((section) => (
        <SectionBlock key={section.cells[0].id} group={section.group}>
          {section.cells.map((cell) => (
            <label
              key={cell.id}
              className="flex cursor-pointer items-start gap-3 py-2.5 pr-2 hover:bg-surface-warm-panel"
            >
              <input
                type="radio"
                name={name}
                checked={value === cell.id}
                onChange={() => onChange(cell)}
                onBlur={onBlur}
                // The wrapping label names the row alone, so two branches of one table reach a
                // screen reader as the same choice; the section's description is what separates them.
                aria-label={cellLabel(cell)}
                className="mt-1 accent-ink-primary"
              />
              <span className="framework-voice text-[13.5px] text-ink-body leading-snug">
                {cell.label}
                {cell.detail ? (
                  <span className="mt-0.5 block text-ink-muted">
                    {cell.detail}
                  </span>
                ) : null}
              </span>
            </label>
          ))}
        </SectionBlock>
      ))}
    </div>
  );
}

/** Decision-tree rows whose selection follows from a value the curator typed rather than from a
 *  click. The same rows as `ChoiceRows`, shown disabled: a curator who disagrees with the row corrects
 *  the number that chose it. */
export function DerivedRows({
  cells,
  selected,
  note,
}: {
  cells: Cell[];
  selected: string | null;
  /** Why this row, or why none — the arithmetic stated so it can be checked rather than trusted. */
  note: ReactNode;
}) {
  return (
    <div>
      <p className="framework-voice mt-2 mb-1 text-[12.5px] text-ink-muted">
        {note}
      </p>
      <div className="divide-y divide-line-row border-line-row border-t">
        {sections(cells).map((section) => (
          <SectionBlock key={section.cells[0].id} group={section.group}>
            {section.cells.map((cell) => (
              <div
                key={cell.id}
                className={`flex items-start gap-3 py-2.5 pr-2 ${
                  selected === cell.id ? "bg-surface-warm-panel" : ""
                }`}
              >
                <input
                  type="radio"
                  disabled
                  readOnly
                  checked={selected === cell.id}
                  aria-label={cellLabel(cell)}
                  className="mt-1 accent-ink-primary"
                />
                <span
                  className={`framework-voice text-[13.5px] leading-snug ${
                    selected === cell.id ? "text-ink-body" : "text-ink-muted"
                  }`}
                >
                  {cell.label}
                  {cell.detail ? (
                    <span className="mt-0.5 block text-ink-muted">
                      {cell.detail}
                    </span>
                  ) : null}
                </span>
              </div>
            ))}
          </SectionBlock>
        ))}
      </div>
    </div>
  );
}

/** A criteria table where each row takes a count of applicable individuals — the shape most of the
 *  clinical and locus workflows use. The calculator's points column is dropped; the row label is
 *  what carries the meaning. */
export function CountRows({
  cells,
  assessment,
  onChange,
  countLabel,
  onBlur,
}: {
  cells: Cell[];
  assessment: WorkflowAssessment;
  onChange: (next: WorkflowAssessment) => void;
  countLabel: string;
  onBlur?: () => void;
}) {
  return (
    <div className="border-line-row border-t">
      <div className="flex items-center justify-end gap-2 py-1.5">
        <span className="field-eyebrow text-ink-faint">{countLabel}</span>
      </div>
      <div className="divide-y divide-line-row border-line-row border-t">
        {sections(cells).map((section) => (
          <SectionBlock key={section.cells[0].id} group={section.group}>
            {section.cells.map((cell) => (
              <div key={cell.id} className="flex items-start gap-4 py-2.5">
                <span className="framework-voice flex-1 text-[13.5px] text-ink-body leading-snug">
                  {cell.label}
                  {cell.detail ? (
                    <span className="mt-0.5 block text-ink-muted">
                      {cell.detail}
                    </span>
                  ) : null}
                </span>
                <input
                  type="text"
                  inputMode="numeric"
                  aria-label={`${countLabel}: ${cellLabel(cell)}`}
                  value={readField(assessment, cell.id)}
                  onChange={(e) =>
                    onChange(withField(assessment, cell, e.target.value))
                  }
                  onBlur={onBlur}
                  className="w-16 shrink-0 rounded-sm border border-line-input bg-white px-2 py-1 text-right font-mono text-[13px] text-ink-body focus:border-ink-ghost focus:outline-none"
                />
              </div>
            ))}
          </SectionBlock>
        ))}
      </div>
    </div>
  );
}

/** A free numeric or short-text input the calculator prints with a placeholder. No `inputMode`: the
 *  frequencies typed here are routinely scientific (`1.18e-05`), which both `numeric` and `decimal`
 *  keypads exclude. */
export function ValueField({
  cell,
  assessment,
  onChange,
  placeholder,
  onBlur,
}: {
  cell: Cell;
  assessment: WorkflowAssessment;
  onChange: (next: WorkflowAssessment) => void;
  placeholder: string;
  onBlur?: () => void;
}) {
  return (
    <label className="flex flex-wrap items-center gap-3 py-2">
      <span className="framework-voice min-w-64 flex-1 text-[13.5px] text-ink-body">
        {cell.label}
      </span>
      <input
        type="text"
        value={readField(assessment, cell.id)}
        placeholder={placeholder}
        onChange={(e) => onChange(withField(assessment, cell, e.target.value))}
        onBlur={onBlur}
        className="framework-voice w-72 rounded-sm border border-line-input bg-white px-2.5 py-1.5 font-mono text-[13px] text-ink-body placeholder:font-sans placeholder:text-ink-faintest focus:border-ink-ghost focus:outline-none"
      />
    </label>
  );
}

function Eyebrow({ label, hint }: { label: string; hint: string }) {
  return (
    <div className="mb-1.5 flex items-baseline gap-2">
      <span className="field-eyebrow text-ink-label">{label}</span>
      <span className="framework-voice text-[12px] text-ink-faint">{hint}</span>
    </div>
  );
}

function Prose({
  value,
  onChange,
  onBlur,
  placeholder,
  rows,
  label,
}: {
  value: string;
  onChange: (next: string) => void;
  onBlur: () => void;
  placeholder: string;
  rows: number;
  label: string;
}) {
  return (
    <textarea
      aria-label={label}
      value={value}
      rows={rows}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      onBlur={onBlur}
      className="curator-voice w-full resize-y rounded-sm border border-line-input bg-white px-3 py-2 text-ink-body focus:border-ink-ghost focus:outline-none"
    />
  );
}

/** What is captured beside the selection. Each field earns its place by a comparison the evaluation
 *  round makes; the hints say so in the curator's terms rather than the round's. */
export function JudgementFields({
  assessment,
  cells,
  onChange,
  onBlur,
}: {
  assessment: WorkflowAssessment;
  cells: Cell[];
  onChange: (next: WorkflowAssessment) => void;
  onBlur: () => void;
}) {
  const scored = assessment.status === AssessmentStatus.SCORED;
  // Nothing is asked of an untouched workflow. Rendering the capture fields before a status is
  // chosen turns an unanswered workflow into an unfilled form, which reads as a demand rather than
  // as the "not reached yet" it is — and buries the answered ones under it.
  if (assessment.status === AssessmentStatus.UNSPECIFIED) return null;
  const confidences: [Confidence, string][] = [
    [Confidence.SETTLED, "Settled"],
    [Confidence.LEANING, "Leaning"],
    [Confidence.OPEN, "Genuinely open"],
  ];
  return (
    <div className="mt-4 space-y-4 border-line-row border-t pt-4">
      {!scored && (
        <div>
          <Eyebrow label="Why not" hint="what rules this workflow out here" />
          <Prose
            label="Why this workflow was not scored"
            value={assessment.statusReason}
            rows={2}
            placeholder="The precondition that bars it, or what evidence is missing."
            onChange={(statusReason) =>
              onChange({ ...assessment, statusReason })
            }
            onBlur={onBlur}
          />
        </div>
      )}
      <div>
        <Eyebrow
          label="Evidence"
          hint="what this rests on — PMIDs, databases, values"
        />
        <Prose
          label="Evidence"
          value={assessment.evidence}
          rows={3}
          placeholder="PMID 12345678, table 2. gnomAD v4.1 grpmax FAF 0.00012. ClinVar SCV000123, 2 stars."
          onChange={(evidence) => onChange({ ...assessment, evidence })}
          onBlur={onBlur}
        />
      </div>
      <div>
        <Eyebrow label="Rationale" hint="why this, given that evidence" />
        <Prose
          label="Rationale"
          value={assessment.rationale}
          rows={4}
          placeholder="Your reasoning, in enough detail that someone could disagree with a step of it."
          onChange={(rationale) => onChange({ ...assessment, rationale })}
          onBlur={onBlur}
        />
      </div>
      {scored && cells.length > 1 && (
        <div>
          <Eyebrow
            label="Nearest call not taken"
            hint="and what ruled it out"
          />
          <select
            aria-label="Nearest call not taken"
            value={assessment.nearestAlternative?.fieldId ?? ""}
            onChange={(e) => {
              const cell = cells.find((c) => c.id === e.target.value);
              onChange({
                ...assessment,
                nearestAlternative: cell ? fieldValue(cell, "") : undefined,
              });
            }}
            className="framework-voice mb-2 w-full rounded-sm border border-line-input bg-white px-2.5 py-1.5 text-[13px] text-ink-body focus:border-ink-ghost focus:outline-none"
          >
            <option value="">No close alternative</option>
            {cells.map((cell) => (
              <option key={cell.id} value={cell.id}>
                {cellLabel(cell)}
              </option>
            ))}
          </select>
          {assessment.nearestAlternative && (
            <Prose
              label="What ruled out the nearest alternative"
              value={assessment.nearestAlternativeReason}
              rows={2}
              placeholder="The clause that separates the two — often one sentence of a supplement."
              onChange={(nearestAlternativeReason) =>
                onChange({ ...assessment, nearestAlternativeReason })
              }
              onBlur={onBlur}
            />
          )}
        </div>
      )}
      <div>
        <Eyebrow
          label="How open"
          hint="so a disagreement inside your doubt is not read as an error"
        />
        <div className="flex flex-wrap items-center gap-1.5">
          {confidences.map(([confidence, label]) => (
            <button
              key={confidence}
              type="button"
              aria-pressed={assessment.confidence === confidence}
              onClick={() => {
                onChange({ ...assessment, confidence });
                onBlur();
              }}
              className={`framework-voice rounded-sm border px-2.5 py-1 text-[13px] transition-colors ${
                assessment.confidence === confidence
                  ? "border-ink-label bg-surface-inset text-ink-primary"
                  : "border-line-input bg-white text-ink-muted hover:border-ink-ghost"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        {assessment.confidence === Confidence.OPEN && (
          <div className="mt-2">
            <Prose
              label="What would settle it"
              value={assessment.confidenceNote}
              rows={2}
              placeholder="What would settle it."
              onChange={(confidenceNote) =>
                onChange({ ...assessment, confidenceNote })
              }
              onBlur={onBlur}
            />
          </div>
        )}
      </div>
    </div>
  );
}

/** One workflow: its code component, the calculator's title, the controls, and the capture. */
export function WorkflowCard({
  anchor,
  code,
  title,
  applicability,
  status,
  children,
}: {
  /** The workflow's own id, which is what the ledger links to. Not the code: several workflows carry
   *  one code — the AD and AR/X-linked branches of an observation, segregation's five tables — so a
   *  code-derived anchor is duplicated in the document and every link to it lands on the first. */
  anchor: string;
  code: string;
  title: string;
  applicability?: string;
  status: AssessmentStatus;
  children: ReactNode;
}) {
  const mark =
    status === AssessmentStatus.SCORED
      ? "bg-curation-recorded"
      : status === AssessmentStatus.UNSPECIFIED
        ? "bg-curation-untouched"
        : "bg-curation-declined";
  return (
    <section
      id={`workflow-${anchor}`}
      className="scroll-mt-24 rounded-md border border-line-primary bg-white p-5 shadow-[0_1px_2px_rgba(28,27,26,0.04)]"
    >
      <header className="mb-4 border-line-row border-b pb-3">
        <div className="flex items-center gap-2">
          <span className={`size-1.5 rounded-full ${mark}`} aria-hidden />
          <span className="font-mono text-[12px] text-ink-faint tracking-wide">
            {code}
          </span>
        </div>
        <h2 className="framework-voice mt-1 font-medium text-[17px] text-ink-primary leading-tight">
          {title}
        </h2>
        {applicability ? (
          <p className="framework-voice mt-1 text-[13px] text-ink-muted">
            {applicability}
          </p>
        ) : null}
      </header>
      {children}
    </section>
  );
}
