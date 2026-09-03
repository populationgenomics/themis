"use client";

import { create, fromJson, toJson } from "@bufbuild/protobuf";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  type Assessment,
  AssessmentSchema,
  AssessmentStatus,
  type CaseAssessment,
  CaseAssessmentSchema,
  type RoutingAssessment,
  type VerdictAssessment,
  VerdictAssessmentSchema,
  type WorkflowAssessment,
  WorkflowAssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";
import {
  Consequence,
  Inheritance,
} from "@/gen/themis/evidence/models/evidence_pb";
import { Classification } from "@/gen/themis/svcv4/models/svcv4_pb";
import { POP_FRQ_ID } from "../workflows/pop";
import { barredWorkflowIds, barringBin } from "../workflows/rarity-gate";
import {
  ALL_WORKFLOWS,
  CASE_ID,
  groupsFor,
  ROUTING_ID,
  VERDICT_ID,
  workflowsFor,
} from "../workflows/registry";
import type { Routing, WorkflowDef } from "../workflows/types";
import { Autosave, type SaveState } from "./autosave";
import {
  cellLabel,
  FrameworkNote,
  JudgementFields,
  StatusPicker,
  WorkflowCard,
} from "./primitives";

// The worksheet screen. Every section's draft — a workflow's, the routing, the case, the verdict —
// reaches the server through one `Autosave`: after a short idle and on leaving a field, and never
// for a payload matching what was last stored, or refocusing a field it did not change would write
// a row saying so.

const IDLE_MS = 2000;
const RETRY_MS = 5000;

export interface WorksheetInit {
  worksheetId: string;
  variant: {
    gene: string;
    transcript: string;
    hgvsC: string;
    diseaseLabel: string;
    mondoId: string;
  };
  workflowsVersion: string;
  submissionCount: number;
  /** Proto-JSON of each stored draft, by workflow id. */
  drafts: Record<string, unknown>;
}

function emptyWorkflow(): WorkflowAssessment {
  return create(WorkflowAssessmentSchema, {});
}

function wrap(workflow: WorkflowAssessment): Assessment {
  return create(AssessmentSchema, {
    kind: { case: "workflow", value: workflow },
  });
}

async function putDraft(
  worksheetId: string,
  sectionId: string,
  payload: string,
): Promise<void> {
  const res = await fetch(`/api/curation/worksheets/${worksheetId}/draft`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      workflowId: sectionId,
      assessment: JSON.parse(payload),
    }),
  });
  if (!res.ok) throw new Error(`save failed: ${res.status}`);
}

/** Decode the stored drafts once. The server sends proto3-JSON, where an enum is its NAME, so this
 *  has to go through `fromJson`: handing the JSON shape to `create` throws during hydration and
 *  leaves the whole worksheet inert. */
function decodeDrafts(drafts: Record<string, unknown>): {
  answers: Record<string, WorkflowAssessment>;
  routing?: RoutingAssessment;
  verdict?: VerdictAssessment;
  caseContext?: CaseAssessment;
  payloads: Record<string, string>;
} {
  const answers: Record<string, WorkflowAssessment> = {};
  const payloads: Record<string, string> = {};
  let routing: RoutingAssessment | undefined;
  let verdict: VerdictAssessment | undefined;
  let caseContext: CaseAssessment | undefined;
  for (const [id, json] of Object.entries(drafts)) {
    const decoded = fromJson(AssessmentSchema, json as never);
    // What the server already holds, so an untouched workflow is not rewritten on its first blur.
    payloads[id] = JSON.stringify(toJson(AssessmentSchema, decoded));
    if (decoded.kind.case === "workflow") answers[id] = decoded.kind.value;
    else if (decoded.kind.case === "routing") routing = decoded.kind.value;
    else if (decoded.kind.case === "verdict") verdict = decoded.kind.value;
    else if (decoded.kind.case === "caseContext")
      caseContext = decoded.kind.value;
    else {
      // The server refuses to render a worksheet holding one of these, so reaching here means the
      // two disagree. Report it and carry the rest: a throw here would stop the whole worksheet
      // responding with nothing on screen to say why.
      console.error(`curation: draft ${id} carries no assessment; skipped`);
    }
  }
  return { answers, routing, verdict, caseContext, payloads };
}

export function Worksheet({ init }: { init: WorksheetInit }) {
  const [decoded] = useState(() => decodeDrafts(init.drafts));
  const [answers, setAnswers] = useState<Record<string, WorkflowAssessment>>(
    decoded.answers,
  );
  // Restored from the saved routing draft, not re-derived. Re-deriving reset the consequence class
  // on every revisit, which dropped every predicted-effect workflow off the screen while their
  // stored answers stayed in the drafts and went into the next submission unseen.
  const [routing, setRouting] = useState<Routing>({
    inheritance: decoded.routing?.inheritance ?? Inheritance.UNSPECIFIED,
    consequenceClass:
      decoded.routing?.consequenceClass ?? Consequence.UNSPECIFIED,
  });
  const [caseContext, setCaseContext] = useState<CaseAssessment>(
    () => decoded.caseContext ?? create(CaseAssessmentSchema, {}),
  );
  const [verdict, setVerdict] = useState<VerdictAssessment>(
    () => decoded.verdict ?? create(VerdictAssessmentSchema, {}),
  );
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [autosave] = useState(
    () =>
      new Autosave({
        stored: decoded.payloads,
        write: (sectionId, payload) =>
          putDraft(init.worksheetId, sectionId, payload),
        onState: setSaveState,
        idleMs: IDLE_MS,
        retryMs: RETRY_MS,
      }),
  );
  useEffect(() => () => autosave.dispose(), [autosave]);

  // The answers as they stand after the last change. Handlers and the gate effect run before React
  // re-renders, so reading `answers` there would see the record as it was BEFORE the change.
  const latest = useRef(decoded.answers);
  // What status the rarity gate displaced, per workflow, so lifting the gate gives it back. Held for
  // the life of the page rather than stored: a reload re-derives the gate from the frequency, and the
  // one case it cannot recover — gate applied, page reloaded, frequency then corrected — leaves an
  // unlocked picker on a workflow the curator can see is marked not applicable.
  const overridden = useRef(new Map<string, AssessmentStatus>());

  const save = useCallback(
    (workflowId: string, workflow: WorkflowAssessment) =>
      autosave.commit(workflowId, wrap(workflow)),
    [autosave],
  );

  const update = useCallback(
    (workflowId: string, next: WorkflowAssessment) => {
      latest.current = { ...latest.current, [workflowId]: next };
      setAnswers(latest.current);
      autosave.schedule(workflowId, wrap(next));
    },
    [autosave],
  );

  const groups = useMemo(() => groupsFor(routing), [routing]);
  const visible = useMemo(() => groups.flatMap((g) => g.workflows), [groups]);

  // SVCv4's rarity gate. A frequency common enough against the disease threshold bars the clinical
  // and locus codes, so their status is the framework's rather than the curator's. Only the status
  // moves; every field already captured stays.
  const popFrq = answers[POP_FRQ_ID];
  const barringRow = barringBin(popFrq);
  // Which workflows the frequency bars, over EVERY workflow rather than the ones on screen. Routing
  // decides what renders, never what the gate has decided: a barred workflow that leaves the screen is
  // still barred, and reading the gate off `visible` made a change of inheritance restore its status
  // — writing `scored` back, off-screen, under a frequency that bars it, into the next submission.
  const barred = barredWorkflowIds(ALL_WORKFLOWS, popFrq);
  useEffect(() => {
    const displaced = overridden.current;
    const nowBarred = barredWorkflowIds(ALL_WORKFLOWS, popFrq);
    for (const id of nowBarred) {
      // Only a workflow the curator has already answered. Manufacturing a draft for an untouched one
      // would put a row the curator never wrote into the submission, and make the "nothing answered
      // yet" submit guard read as answered.
      const current = latest.current[id];
      if (!current || current.status === AssessmentStatus.NOT_APPLICABLE) {
        continue;
      }
      displaced.set(id, current.status);
      const next = { ...current, status: AssessmentStatus.NOT_APPLICABLE };
      update(id, next);
      void save(id, next);
    }
    // A corrected frequency has to give back the status it displaced. Left standing, a forced
    // `not applicable` is a wrong answer that reads as a deliberate one, and the curator has no
    // signal that nine of them need revisiting.
    for (const [id, status] of [...displaced]) {
      if (nowBarred.has(id)) continue;
      displaced.delete(id);
      const current = latest.current[id];
      if (!current || current.status !== AssessmentStatus.NOT_APPLICABLE)
        continue;
      const next = { ...current, status };
      update(id, next);
      void save(id, next);
    }
  }, [popFrq, update, save]);

  return (
    <div className="mx-auto flex max-w-6xl gap-8 px-6 py-8">
      <Ledger workflows={visible} answers={answers} />
      <div className="min-w-0 flex-1 space-y-5">
        <Header init={init} saveState={saveState} />
        <RoutingCard
          routing={routing}
          onChange={(next) => {
            setRouting(next);
            void autosave.commit(
              ROUTING_ID,
              create(AssessmentSchema, {
                kind: {
                  case: "routing",
                  value: {
                    inheritance: next.inheritance,
                    consequenceClass: next.consequenceClass,
                    entity: init.variant.diseaseLabel,
                    mondoId: init.variant.mondoId,
                    rationale: "",
                  },
                },
              }),
            );
          }}
        />
        <CaseCard
          value={caseContext}
          onChange={(next) => {
            setCaseContext(next);
            autosave.schedule(
              CASE_ID,
              create(AssessmentSchema, {
                kind: { case: "caseContext", value: next },
              }),
            );
          }}
          onBlur={() => autosave.flush(CASE_ID)}
        />
        <RoutingNotes routing={routing} />
        {groups.map((group) => (
          <section key={group.key} className="space-y-5">
            <h2 className="field-eyebrow pt-3 text-ink-faint">{group.title}</h2>
            {group.workflows.map((workflow) => {
              const assessment = answers[workflow.id] ?? emptyWorkflow();
              return (
                <WorkflowCard
                  key={workflow.id}
                  anchor={workflow.id}
                  code={workflow.code}
                  title={workflow.title}
                  applicability={workflow.applicability}
                  status={assessment.status}
                >
                  <div className="mb-3">
                    <StatusPicker
                      value={assessment.status}
                      disabled={barred.has(workflow.id)}
                      onChange={(status) => {
                        const next = { ...assessment, status };
                        update(workflow.id, next);
                        void save(workflow.id, next);
                      }}
                    />
                  </div>
                  {barred.has(workflow.id) && barringRow ? (
                    <FrameworkNote>
                      Barred by the population frequency: POP_FRQ is assessed as
                      “{cellLabel(barringRow)}”.
                    </FrameworkNote>
                  ) : null}
                  {assessment.status === AssessmentStatus.SCORED ? (
                    <workflow.Body
                      assessment={assessment}
                      siblings={answers}
                      onChange={(next) => update(workflow.id, next)}
                      onBlur={() => autosave.flush(workflow.id)}
                    />
                  ) : null}
                  <JudgementFields
                    assessment={assessment}
                    cells={workflow.cells}
                    onChange={(next) => update(workflow.id, next)}
                    onBlur={() => autosave.flush(workflow.id)}
                  />
                </WorkflowCard>
              );
            })}
          </section>
        ))}
        <VerdictCard
          verdict={verdict}
          scored={visible.filter(
            (w) => answers[w.id]?.status === AssessmentStatus.SCORED,
          )}
          onChange={(next) => {
            setVerdict(next);
            autosave.schedule(
              VERDICT_ID,
              create(AssessmentSchema, {
                kind: { case: "verdict", value: next },
              }),
            );
          }}
          onBlur={() => autosave.flush(VERDICT_ID)}
        />
        <Submit
          worksheetId={init.worksheetId}
          autosave={autosave}
          saveState={saveState}
          answered={Object.keys(answers).length}
        />
      </div>
    </div>
  );
}

function Header({
  init,
  saveState,
}: {
  init: WorksheetInit;
  saveState: SaveState;
}) {
  const { variant } = init;
  return (
    <header className="sticky top-0 z-10 -mx-2 mb-2 rounded-md border border-line-primary bg-white/95 px-4 py-3 backdrop-blur">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <Link
            href="/curation"
            className="framework-voice mb-1 inline-flex items-center gap-1 text-[12.5px] text-ink-faint hover:text-ink-body"
          >
            <span aria-hidden>&larr;</span> All worksheets
          </Link>
          <p className="font-mono text-[13px] text-ink-primary">
            {variant.transcript}:{variant.hgvsC}
          </p>
          <p className="framework-voice mt-0.5 text-[13px] text-ink-muted">
            {variant.gene} · {variant.diseaseLabel}
            {variant.mondoId ? ` · ${variant.mondoId}` : ""}
          </p>
        </div>
        <SaveIndicator state={saveState} />
      </div>
    </header>
  );
}

/** Three states, and the failed one is loud: a save that did not land must not look like one that
 *  did, or a curator closes the tab on work that is gone. */
function SaveIndicator({ state }: { state: SaveState }) {
  if (state === "failed") {
    return (
      <span className="framework-voice rounded-sm border border-destructive/40 bg-destructive/10 px-2 py-1 text-[12.5px] text-destructive">
        Not saved — your work is still on screen. Retrying.
      </span>
    );
  }
  const label =
    state === "saving"
      ? "Saving…"
      : state === "saved"
        ? "Saved"
        : "No changes yet";
  return (
    <span className="framework-voice text-[12.5px] text-ink-faint">
      {label}
    </span>
  );
}

/** A ledger, not a progress bar: "no data" is an answer, and a bar would read it as a gap and press
 *  the curator to fill it. */
function Ledger({
  workflows,
  answers,
}: {
  workflows: WorkflowDef[];
  answers: Record<string, WorkflowAssessment>;
}) {
  return (
    <nav className="sticky top-8 hidden h-fit w-52 shrink-0 lg:block">
      <p className="field-eyebrow mb-2 text-ink-faint">Recorded</p>
      <ul className="space-y-0.5">
        {workflows.map((workflow) => {
          const status =
            answers[workflow.id]?.status ?? AssessmentStatus.UNSPECIFIED;
          const mark =
            status === AssessmentStatus.SCORED
              ? "bg-curation-recorded"
              : status === AssessmentStatus.UNSPECIFIED
                ? "bg-curation-untouched"
                : "bg-curation-declined";
          return (
            <li key={workflow.id}>
              <a
                href={`#workflow-${workflow.id}`}
                className="flex items-center gap-2 rounded-sm px-1.5 py-1 hover:bg-surface-warm-panel"
              >
                <span className={`size-1.5 shrink-0 rounded-full ${mark}`} />
                <span className="truncate font-mono text-[11.5px] text-ink-muted">
                  {workflow.code}
                </span>
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

/** Every mode the contract names, the autosomal ones first. Y-linked, mitochondrial and undetermined
 *  are here even though the calculator's splits have no branch for them: a curator has to be able to
 *  state the mode whatever it is, and an empty routed set is the framework's answer to one it does
 *  not route. */
export const INHERITANCES: [Inheritance, string][] = [
  [Inheritance.AUTOSOMAL_DOMINANT, "Autosomal dominant"],
  [Inheritance.AUTOSOMAL_RECESSIVE, "Autosomal recessive"],
  [Inheritance.SEMIDOMINANT, "Semi-dominant"],
  [Inheritance.X_LINKED, "X-linked"],
  [Inheritance.Y_LINKED, "Y-linked"],
  [Inheritance.MITOCHONDRIAL, "Mitochondrial"],
  [Inheritance.UNDETERMINED, "Undetermined"],
];

/** Every class the contract names, in the order the calculator groups them. Non-coding is here even
 *  though no predicted-effect workflow is transcribed for it: a curator whose variant is non-coding
 *  has to be able to say so, and an empty routed set is the framework's answer to that. */
export const CONSEQUENCES: [Consequence, string][] = [
  [Consequence.MISSENSE, "Missense"],
  [Consequence.NONSENSE, "Nonsense"],
  [Consequence.FRAMESHIFT, "Frameshift"],
  [Consequence.INFRAME_INDEL, "In-frame indel"],
  [Consequence.CANONICAL_SPLICE, "Canonical splice"],
  [Consequence.INTRONIC, "Intronic"],
  [Consequence.SYNONYMOUS, "Synonymous"],
  [Consequence.NON_CODING, "Non-coding"],
  [Consequence.EXON_DELETION, "Exon deletion"],
  [Consequence.EXON_DUPLICATION, "Exon duplication"],
  [Consequence.START_LOST, "Start lost"],
  [Consequence.STOP_LOST, "Stop lost"],
];

/** The routing is a judgement of the curator's own, and it decides which workflows apply. Recorded
 *  as its own section rather than inferred from the variant row. */
export function RoutingCard({
  routing,
  onChange,
}: {
  routing: Routing;
  onChange: (next: Routing) => void;
}) {
  return (
    <section className="rounded-md border border-line-primary bg-white p-5">
      <h2 className="framework-voice font-medium text-[17px] text-ink-primary">
        Routing
      </h2>
      <p className="framework-voice mt-1 mb-4 text-[13px] text-ink-muted">
        Which workflows apply follows from these. State them before you start.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="block">
          <span className="field-eyebrow text-ink-label">
            Mode of inheritance
          </span>
          <select
            value={routing.inheritance}
            onChange={(e) =>
              onChange({ ...routing, inheritance: Number(e.target.value) })
            }
            className="framework-voice mt-1 w-full rounded-sm border border-line-input bg-white px-2.5 py-1.5 text-[13.5px] text-ink-body"
          >
            <option value={Inheritance.UNSPECIFIED}>Select…</option>
            {INHERITANCES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="field-eyebrow text-ink-label">
            Consequence class
          </span>
          <select
            value={routing.consequenceClass}
            onChange={(e) =>
              onChange({ ...routing, consequenceClass: Number(e.target.value) })
            }
            className="framework-voice mt-1 w-full rounded-sm border border-line-input bg-white px-2.5 py-1.5 text-[13.5px] text-ink-body"
          >
            <option value={Consequence.UNSPECIFIED}>Select…</option>
            {CONSEQUENCES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
    </section>
  );
}

/** What the routing leaves off the page, said out loud. Each axis is either still unanswered — both
 *  pickers open unset, and nothing seeds them — or answered with a member the transcription has no
 *  workflow for. Either way the absence is stated, rather than left looking like a page that failed
 *  to render.
 *
 *  Derived by asking the registry what the answer adds, not from a list of members: a class or mode
 *  the framework starts routing stops being named here without anyone remembering to edit this. */
function RoutingNotes({ routing }: { routing: Routing }) {
  const routed = workflowsFor(routing).length;
  const adds = (unanswered: Partial<Routing>) =>
    routed > workflowsFor({ ...routing, ...unanswered }).length;
  const notes: string[] = [];
  if (routing.inheritance === Inheritance.UNSPECIFIED) {
    notes.push(
      "State the mode of inheritance above to see the unaffected, affected, homozygous and segregation workflows it selects between.",
    );
  } else if (!adds({ inheritance: Inheritance.UNSPECIFIED })) {
    notes.push(
      "No workflow is routed by this mode of inheritance: the framework's observation workflows split on autosomal dominant, autosomal recessive, semi-dominant and X-linked only.",
    );
  }
  if (routing.consequenceClass === Consequence.UNSPECIFIED) {
    notes.push(
      "Name the consequence class above to see the predicted-effect workflows.",
    );
  } else if (!adds({ consequenceClass: Consequence.UNSPECIFIED })) {
    notes.push(
      "No predicted-effect workflow is transcribed for this consequence class.",
    );
  }
  if (notes.length === 0) return null;
  return (
    <div className="framework-voice space-y-2 rounded-md border border-line-primary border-dashed bg-surface-warm-panel px-5 py-8 text-center text-[13.5px] text-ink-muted">
      {notes.map((note) => (
        <p key={note}>{note}</p>
      ))}
    </div>
  );
}

// The message's own prose fields, named explicitly: `keyof CaseAssessment` also picks up
// protobuf-es's `$typeName` and `$unknown`.
type CaseField =
  | "probandNarrative"
  | "testingPerformed"
  | "coObservedVariant"
  | "segregation"
  | "assays"
  | "other";

const CASE_FIELDS: [CaseField, string, string][] = [
  [
    "probandNarrative",
    "Presentation",
    "the phenotype, in the referring clinician's words",
  ],
  ["testingPerformed", "Testing performed", "what was sequenced and analysed"],
  [
    "coObservedVariant",
    "Co-observed variant",
    "identity, phase, how the phase was established, its own classification",
  ],
  ["segregation", "Segregation", "who carries it, who is affected"],
  ["assays", "Assays", "functional and RNA data, controls, calibration"],
  ["other", "Anything else", "whatever the slots above do not cover"],
];

/** The facts that bear on several workflows at once — the comp-het partner, the testing breadth,
 *  the assay — recorded once here instead of retyped into each workflow's evidence.
 *
 *  Collapsed by default: a curator reaches for it when the case needs it, and an empty six-field
 *  form at the top of the page would read as six more things demanded before they can start. */
function CaseCard({
  value,
  onChange,
  onBlur,
}: {
  value: CaseAssessment;
  onChange: (next: CaseAssessment) => void;
  /** Commit the current draft now — bound to a field losing focus. */
  onBlur: () => void;
}) {
  const filled = CASE_FIELDS.filter(([key]) => value[key] !== "").length;
  const [open, setOpen] = useState(filled > 0);
  return (
    <section className="rounded-md border border-line-primary bg-white p-5">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-baseline justify-between gap-3 text-left"
      >
        <span>
          <span className="framework-voice block font-medium text-[17px] text-ink-primary">
            The case
          </span>
          <span className="framework-voice mt-1 block text-[13px] text-ink-muted">
            Facts more than one workflow rests on. Recorded here so a reader can
            tell a different conclusion from different information.
          </span>
        </span>
        <span className="framework-voice shrink-0 text-[12.5px] text-ink-faint">
          {filled > 0 ? `${filled} recorded` : "none recorded"} ·{" "}
          {open ? "hide" : "show"}
        </span>
      </button>
      {open && (
        <div className="mt-4 space-y-4 border-line-row border-t pt-4">
          {CASE_FIELDS.map(([key, label, hint]) => (
            <div key={key}>
              <div className="mb-1.5 flex items-baseline gap-2">
                <span className="field-eyebrow text-ink-label">{label}</span>
                <span className="framework-voice text-[12px] text-ink-faint">
                  {hint}
                </span>
              </div>
              <textarea
                aria-label={label}
                value={value[key]}
                rows={2}
                onChange={(e) => onChange({ ...value, [key]: e.target.value })}
                onBlur={onBlur}
                className="curator-voice w-full resize-y rounded-sm border border-line-input bg-white px-3 py-2 text-ink-body focus:border-ink-ghost focus:outline-none"
              />
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

/** The ordered ladder, then the results that are not points on it: no class established, and the two
 *  SM18 substitutes for a class where gene-disease validity falls below Limited. A curator who
 *  reached one of those has no ladder answer to give, so the picker has to carry it. */
export const CLASSIFICATIONS: [Classification, string][] = [
  [Classification.PATHOGENIC, "Pathogenic"],
  [Classification.LIKELY_PATHOGENIC, "Likely pathogenic"],
  [Classification.VUS, "Uncertain significance"],
  [Classification.LIKELY_BENIGN, "Likely benign"],
  [Classification.BENIGN, "Benign"],
  [Classification.NOT_ESTABLISHED, "No class established"],
  [
    Classification.VARIANT_IN_GENE_OF_UNCERTAIN_SIGNIFICANCE,
    "Variant in gene of uncertain significance",
  ],
  [Classification.DO_NOT_REPORT, "Do not report"],
];

/** The classification and its reasoning. Stated, never derived: the worksheet collects no points,
 *  and `themis.svcv4` owns the arithmetic that would turn a code set into a band.
 *
 *  The class-determinative list is what says whether a divergence could have moved the answer at
 *  all, so a round can tell a disagreement that mattered from one that did not. */
export function VerdictCard({
  verdict,
  scored,
  onChange,
  onBlur,
}: {
  verdict: VerdictAssessment;
  scored: WorkflowDef[];
  onChange: (next: VerdictAssessment) => void;
  /** Commit the current draft now — bound to the reasoning losing focus. */
  onBlur: () => void;
}) {
  const determinative = new Set(verdict.classDeterminative);
  return (
    <section className="rounded-md border border-line-primary bg-white p-5">
      <h2 className="framework-voice font-medium text-[17px] text-ink-primary">
        Verdict
      </h2>
      <p className="framework-voice mt-1 mb-4 text-[13px] text-ink-muted">
        Your classification, and what holds you at it rather than the class
        above or below.
      </p>
      <div className="mb-4 flex flex-wrap gap-1.5">
        {CLASSIFICATIONS.map(([value, label]) => (
          <button
            key={value}
            type="button"
            aria-pressed={verdict.classification === value}
            onClick={() => onChange({ ...verdict, classification: value })}
            className={`framework-voice rounded-sm border px-2.5 py-1 text-[13px] transition-colors ${
              verdict.classification === value
                ? "border-primary bg-primary text-primary-foreground"
                : "border-line-input bg-white text-ink-muted hover:border-ink-ghost"
            }`}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="mb-1.5 flex items-baseline gap-2">
        <span className="field-eyebrow text-ink-label">Reasoning</span>
        <span className="framework-voice text-[12px] text-ink-faint">
          what holds you at this class rather than the one above
        </span>
      </div>
      <textarea
        aria-label="Verdict reasoning"
        value={verdict.rationale}
        rows={4}
        placeholder="Your reading of the evidence as a whole, not a restatement of the codes."
        onChange={(e) => onChange({ ...verdict, rationale: e.target.value })}
        onBlur={onBlur}
        className="curator-voice w-full resize-y rounded-sm border border-line-input bg-white px-3 py-2 text-ink-body focus:border-ink-ghost focus:outline-none"
      />
      {scored.length > 0 && (
        <div className="mt-4">
          <div className="mb-1.5 flex items-baseline gap-2">
            <span className="field-eyebrow text-ink-label">
              Class-determinative
            </span>
            <span className="framework-voice text-[12px] text-ink-faint">
              the calls that would change the class if they were wrong
            </span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {scored.map((workflow) => (
              <button
                key={workflow.id}
                type="button"
                aria-pressed={determinative.has(workflow.id)}
                onClick={() => {
                  const next = new Set(determinative);
                  if (next.has(workflow.id)) next.delete(workflow.id);
                  else next.add(workflow.id);
                  onChange({ ...verdict, classDeterminative: [...next] });
                }}
                className={`rounded-sm border px-2 py-1 font-mono text-[12px] transition-colors ${
                  determinative.has(workflow.id)
                    ? "border-ink-label bg-surface-inset text-ink-primary"
                    : "border-line-input bg-white text-ink-faint hover:border-ink-ghost"
                }`}
              >
                {workflow.code}
              </button>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

/** The server copies the drafts it holds, so every pending write has to land first: the click that
 *  submits also blurs the field being typed in, and a PUT dispatched on the same click as the POST
 *  can be applied after it. A section still failing bars submitting — the submission would quietly
 *  leave it out. */
function Submit({
  worksheetId,
  autosave,
  saveState,
  answered,
}: {
  worksheetId: string;
  autosave: Autosave;
  saveState: SaveState;
  answered: number;
}) {
  const [state, setState] = useState<
    "idle" | "saving" | "sending" | "done" | "error"
  >("idle");
  const [message, setMessage] = useState("");
  const busy = state === "saving" || state === "sending";
  return (
    <section className="rounded-md border border-line-primary bg-surface-warm-panel p-5">
      <h2 className="framework-voice font-medium text-[15px] text-ink-primary">
        Submit this worksheet
      </h2>
      <p className="framework-voice mt-1 mb-3 text-[13px] text-ink-muted">
        Submitting records everything you have answered as one set. You can keep
        working afterwards and submit again.
      </p>
      <button
        type="button"
        disabled={answered === 0 || busy || saveState === "failed"}
        onClick={async () => {
          setState("saving");
          setMessage("");
          try {
            await autosave.settle();
          } catch {
            setState("error");
            setMessage(
              "Not every section is saved yet, so nothing was submitted. Try again once the header reads Saved.",
            );
            return;
          }
          setState("sending");
          try {
            const res = await fetch(
              `/api/curation/worksheets/${worksheetId}/submit`,
              {
                method: "POST",
                headers: { "content-type": "application/json" },
                body: JSON.stringify({ note: "" }),
              },
            );
            if (res.ok) {
              setState("done");
              setMessage("Submitted.");
              return;
            }
            const body = (await res.json().catch(() => null)) as {
              error?: { message?: string };
            } | null;
            setState("error");
            setMessage(body?.error?.message ?? "Submission failed.");
          } catch {
            setState("error");
            setMessage("Could not reach the server. Try again.");
          }
        }}
        className="framework-voice rounded-sm bg-primary px-3.5 py-1.5 text-[13.5px] text-primary-foreground disabled:opacity-40"
      >
        {state === "saving"
          ? "Saving…"
          : state === "sending"
            ? "Submitting…"
            : "Submit"}
      </button>
      {message ? (
        <span
          className={`framework-voice ml-3 text-[13px] ${state === "error" ? "text-destructive" : "text-ink-muted"}`}
        >
          {message}
        </span>
      ) : null}
    </section>
  );
}
