import {
  create,
  fromBinary,
  type MessageInitShape,
  toBinary,
} from "@bufbuild/protobuf";
import {
  AssessmentSchema,
  type FieldValue,
} from "@/gen/themis/curation/models/curation_pb";
import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";
import {
  AssessmentStatus,
  Classification,
  ModeOfInheritance,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import {
  FIXTURE_CURATORS,
  FIXTURE_PROJECT,
} from "@/server/adapters/fixture/access";
import { ClientInputError, ResourceNotFoundError } from "@/server/errors";
import { DEV_USER_EMAIL } from "@/server/identity";
import type {
  Entry,
  NewVariant,
  Submission,
  Variant,
  Worksheet,
} from "./model";
import { ROUTING_ID, VERDICT_ID } from "./sections";
import { type CurationStore, duplicateRegistration } from "./store";
import { WORKFLOWS_VERSION } from "./version";

// The offline store: the same semantics as `sql.ts`, in memory. Assessments round-trip through the
// proto encoding here too, so an encoding fault shows up offline rather than only against Cloud SQL.
//
// Seeded in the fixture Project, whose seeded ACL makes the dev user its manager and a curator
// too (the arrangement the blindness rule exists for) beside two other curators, so the surface has
// something to render without a database. Both curators of the second variant have submitted, so
// the comparison page has two answers to read against each other.

const [SECOND_CURATOR, THIRD_CURATOR] = FIXTURE_CURATORS;

interface StoredSubmission extends Submission {
  entries: Map<string, Uint8Array>;
}

export class FixtureCurationStore implements CurationStore {
  private readonly variants = new Map<string, Variant>();
  private readonly worksheets = new Map<string, Worksheet>();
  private readonly draftRows = new Map<string, Map<string, Uint8Array>>();
  private readonly submissionRows = new Map<string, StoredSubmission[]>();
  private counter = 0;

  constructor() {
    this.seed();
  }

  private id(prefix: string): string {
    this.counter += 1;
    return `${prefix}_${this.counter.toString().padStart(4, "0")}`;
  }

  private seed(): void {
    const at = new Date("2026-08-01T09:00:00Z");
    const seeded: NewVariant[] = [
      {
        gene: "MYH7",
        transcript: "NM_000257.4",
        hgvsC: "c.1988G>A",
        clingenAlleleId: "CA011552",
        diseaseLabel: "hypertrophic cardiomyopathy",
        mondoId: "MONDO:0005045",
        modeOfInheritance: ModeOfInheritance.AUTOSOMAL_DOMINANT,
        clinicalContext:
          "Proband, 14, referred after exertional syncope. Echocardiogram shows asymmetric septal hypertrophy of 21 mm with no outflow obstruction at rest. Father died suddenly at 38; no autopsy. A 48-gene cardiomyopathy panel found no other variant of interest. Relatives not yet tested.",
      },
      {
        gene: "CFTR",
        transcript: "NM_000492.4",
        hgvsC: "c.1521_1523del",
        clingenAlleleId: "CA118639",
        diseaseLabel: "cystic fibrosis",
        mondoId: "MONDO:0009061",
        modeOfInheritance: ModeOfInheritance.AUTOSOMAL_RECESSIVE,
        clinicalContext:
          "Infant, 4 months, with meconium ileus at birth and recurrent chest infections. Sweat chloride 98 mmol/L on two occasions. CFTR full-gene sequencing with MLPA found the variant homozygous; each parent carries one copy.",
      },
    ];
    for (const variant of seeded) {
      const stored = this.insertVariant(
        FIXTURE_PROJECT,
        variant,
        DEV_USER_EMAIL,
        at,
      );
      // The dev user curates the first variant as well as managing, so the offline surface
      // exercises the manager-who-is-also-assigned case rather than only the clean one.
      const curators =
        stored.gene === "MYH7"
          ? [DEV_USER_EMAIL, SECOND_CURATOR]
          : [SECOND_CURATOR, THIRD_CURATOR];
      for (const curator of curators) {
        const worksheet = this.insertWorksheet(
          stored.id,
          curator,
          DEV_USER_EMAIL,
          at,
        );
        if (stored.gene !== "CFTR") continue;
        const answers = SUBMITTED_ANSWERS.get(curator);
        if (answers === undefined) {
          throw new Error(`the fixture seeds no answers for ${curator}`);
        }
        this.insertSubmission(worksheet.id, answers, at);
      }
    }
  }

  /** A worksheet whose drafts were submitted as they stand, as `submit` leaves one. */
  private insertSubmission(
    worksheetId: string,
    answers: Entry[],
    submittedAt: Date,
  ): void {
    const entries = new Map(
      answers.map(({ workflowId, assessment }) => [
        workflowId,
        toBinary(AssessmentSchema, assessment),
      ]),
    );
    this.draftRows.set(worksheetId, new Map(entries));
    this.submissionRows.set(worksheetId, [
      { id: this.id("sub"), worksheetId, submittedAt, note: "", entries },
    ]);
  }

  private insertVariant(
    projectId: string,
    variant: NewVariant,
    createdBy: string,
    createdAt: Date,
  ): Variant {
    const registered = [...this.variants.values()].some(
      (v) =>
        v.projectId === projectId &&
        v.transcript === variant.transcript &&
        v.hgvsC === variant.hgvsC &&
        v.mondoId === variant.mondoId &&
        v.modeOfInheritance === variant.modeOfInheritance,
    );
    if (registered) throw duplicateRegistration(variant);
    const stored: Variant = {
      ...variant,
      id: this.id("var"),
      projectId,
      createdBy,
      createdAt,
    };
    this.variants.set(stored.id, stored);
    return stored;
  }

  private insertWorksheet(
    variantId: string,
    curatorEmail: string,
    assignedBy: string,
    assignedAt: Date,
  ): Worksheet {
    const worksheet: Worksheet = {
      id: this.id("ws"),
      variantId,
      curatorEmail,
      workflowsVersion: WORKFLOWS_VERSION,
      assignedBy,
      assignedAt,
    };
    this.worksheets.set(worksheet.id, worksheet);
    return worksheet;
  }

  async createVariant(
    projectId: string,
    variant: NewVariant,
    createdBy: string,
  ): Promise<Variant> {
    return this.insertVariant(projectId, variant, createdBy, new Date());
  }

  async listVariants(projectId: string): Promise<Variant[]> {
    return [...this.variants.values()]
      .filter((v) => v.projectId === projectId)
      .sort((a, b) => b.createdAt.getTime() - a.createdAt.getTime());
  }

  async getVariant(id: string): Promise<Variant | undefined> {
    return this.variants.get(id);
  }

  async assign(
    variantId: string,
    curatorEmail: string,
    workflowsVersion: string,
    assignedBy: string,
  ): Promise<Worksheet> {
    const clash = [...this.worksheets.values()].some(
      (w) => w.variantId === variantId && w.curatorEmail === curatorEmail,
    );
    if (clash) {
      throw new ClientInputError(
        `${curatorEmail} is already assigned to this variant`,
      );
    }
    const worksheet = this.insertWorksheet(
      variantId,
      curatorEmail,
      assignedBy,
      new Date(),
    );
    return { ...worksheet, workflowsVersion };
  }

  async withdraw(worksheetId: string): Promise<void> {
    if ((this.submissionRows.get(worksheetId) ?? []).length > 0) {
      throw new ClientInputError(
        `worksheet ${worksheetId} has been submitted and cannot be withdrawn`,
      );
    }
    this.worksheets.delete(worksheetId);
    this.draftRows.delete(worksheetId);
  }

  async getWorksheet(id: string): Promise<Worksheet | undefined> {
    return this.worksheets.get(id);
  }

  async worksheetsOfCurator(email: string): Promise<Worksheet[]> {
    return [...this.worksheets.values()]
      .filter((w) => w.curatorEmail === email)
      .sort((a, b) => b.assignedAt.getTime() - a.assignedAt.getTime());
  }

  async worksheetsOfVariant(variantId: string): Promise<Worksheet[]> {
    return [...this.worksheets.values()]
      .filter((w) => w.variantId === variantId)
      .sort((a, b) => a.curatorEmail.localeCompare(b.curatorEmail));
  }

  async drafts(worksheetId: string): Promise<Entry[]> {
    const rows = this.draftRows.get(worksheetId);
    if (rows === undefined) return [];
    return [...rows.entries()]
      .map(([workflowId, bytes]) => ({
        workflowId,
        assessment: fromBinary(AssessmentSchema, bytes),
      }))
      .sort((a, b) => a.workflowId.localeCompare(b.workflowId));
  }

  async putDraft(worksheetId: string, entry: Entry): Promise<void> {
    if (!this.worksheets.has(worksheetId)) {
      throw new ResourceNotFoundError(`no such worksheet: ${worksheetId}`);
    }
    let rows = this.draftRows.get(worksheetId);
    if (rows === undefined) {
      rows = new Map();
      this.draftRows.set(worksheetId, rows);
    }
    rows.set(entry.workflowId, toBinary(AssessmentSchema, entry.assessment));
  }

  async submissions(worksheetId: string): Promise<Submission[]> {
    return (this.submissionRows.get(worksheetId) ?? []).map(
      ({ entries: _entries, ...submission }) => submission,
    );
  }

  async submit(
    worksheetId: string,
    note: string,
    workflowIds: readonly string[],
  ): Promise<Submission> {
    if (!this.worksheets.has(worksheetId)) {
      throw new ResourceNotFoundError(`no such worksheet: ${worksheetId}`);
    }
    const drafts = new Map(
      [...(this.draftRows.get(worksheetId) ?? [])].filter(([workflowId]) =>
        workflowIds.includes(workflowId),
      ),
    );
    if (drafts.size === 0) {
      throw new ClientInputError(
        "nothing to submit: the worksheet holds no answers",
      );
    }
    const submission: StoredSubmission = {
      id: this.id("sub"),
      worksheetId,
      submittedAt: new Date(),
      note,
      entries: drafts,
    };
    const existing = this.submissionRows.get(worksheetId) ?? [];
    this.submissionRows.set(worksheetId, [...existing, submission]);
    const { entries: _entries, ...plain } = submission;
    return plain;
  }

  async assessments(submissionId: string): Promise<Entry[]> {
    for (const submissions of this.submissionRows.values()) {
      const found = submissions.find((s) => s.id === submissionId);
      if (found === undefined) continue;
      return [...found.entries.entries()]
        .map(([workflowId, bytes]) => ({
          workflowId,
          assessment: fromBinary(AssessmentSchema, bytes),
        }))
        .sort((a, b) => a.workflowId.localeCompare(b.workflowId));
    }
    throw new ResourceNotFoundError(`no such submission: ${submissionId}`);
  }
}

// The two seeded submissions on the CFTR variant. Each field is what the worksheet would have stored:
// the control's field id, the cell's framework name, the label the curator read, and the value —
// the chosen cell's id for a selection, the typed text for a count or a frequency.

function field(
  fieldId: string,
  cellId: string,
  label: string,
  value: string,
): FieldValue {
  return {
    $typeName: "themis.curation.models.curation.FieldValue",
    fieldId,
    cellId,
    label,
    value,
  };
}

function scoredWorkflow(
  workflowId: string,
  fields: FieldValue[],
  rationale: string,
): Entry {
  return {
    workflowId,
    assessment: create(AssessmentSchema, {
      kind: {
        case: "workflow",
        value: { status: AssessmentStatus.SCORED, fields, rationale },
      },
    }),
  };
}

/** A workflow the curator marked as having nothing to assess. `leftover` is what a status change
 *  leaves stored: the worksheet keeps the fields filled before it. */
function noDataWorkflow(
  workflowId: string,
  statusReason: string,
  leftover: FieldValue[],
): Entry {
  return {
    workflowId,
    assessment: create(AssessmentSchema, {
      kind: {
        case: "workflow",
        value: {
          status: AssessmentStatus.NO_DATA,
          fields: leftover,
          statusReason,
        },
      },
    }),
  };
}

function section(
  workflowId: string,
  kind: MessageInitShape<typeof AssessmentSchema>["kind"],
): Entry {
  return { workflowId, assessment: create(AssessmentSchema, { kind }) };
}

const DAFT = (value: string) =>
  field(
    "pop_frq.daft",
    "POP_FRQ.daft",
    "Disease Allele Frequency Threshold",
    value,
  );
const FAF = (value: string) =>
  field(
    "pop_frq.faf",
    "POP_FRQ.faf",
    "Variant GrpMax Filtering Allele Frequency",
    value,
  );
const BELOW_1_5X = field(
  "pop_frq.bin",
  "POP_FRQ.bin.lt_1_5x",
  "Frequency of VBC <(1.5x of DAFT)",
  "pop_frq.bin.lt_1_5x",
);
const DAMAGING_INDEL = field(
  "cds_prd",
  "CDS_PRD.not_repetitive.damaging",
  "InDel is NOT located in a repetitive region (>5 repetitive units) — In silico inframe predictive tool suggests a damaging impact",
  "cds_prd.not_repetitive.damaging",
);
const HOMOZYGOUS_PROBANDS = (count: string) =>
  field(
    "CLN_AFF.arxl.consistent_full_lt_0_0001.homozygous",
    "CLN_AFF.arxl.consistent_full_lt_0_0001.homozygous",
    "Phenotype is CONSISTENT for MDE AND All relevant genes for disorder tested AND Non-genetic etiology is unlikely AND No additional variant of interest AND Likelihood of observing two heterozygous variants is <0.0001 — Homozygous VBC",
    count,
  );

const SUBMITTED_ANSWERS: ReadonlyMap<string, Entry[]> = new Map([
  [
    SECOND_CURATOR,
    [
      section(ROUTING_ID, {
        case: "routing",
        value: {
          consequenceClass: Consequence.INFRAME_INDEL,
          rationale: "A three-base deletion removing Phe508; the frame holds.",
        },
      }),
      scoredWorkflow(
        "pop_frq",
        [BELOW_1_5X, DAFT("0.0153"), FAF("0.00984")],
        "Common in European-ancestry groups, but below the threshold the disease's prevalence allows.",
      ),
      scoredWorkflow(
        "cds_prd",
        [DAMAGING_INDEL],
        "Outside any repeat; the in-frame predictors agree on a damaging effect.",
      ),
      scoredWorkflow(
        "cds_fxn",
        [
          field(
            "cds_fxn",
            "CDS_FXN.assay_consistent_with_controls",
            "There are functional data for the VBC AND Functional assay is consistent with mechanism for VBC AND P & B controls are used",
            "cds_fxn.yes",
          ),
        ],
        "Chloride-transport assays in primary airway cells, with wild-type and known-benign controls, show near-absent function.",
      ),
      scoredWorkflow(
        "cln_aff_arxl",
        [HOMOZYGOUS_PROBANDS("3")],
        "Three unrelated homozygous probands in the cited cohort, each with a classic presentation.",
      ),
      section(VERDICT_ID, {
        case: "verdict",
        value: {
          classification: Classification.PATHOGENIC,
          rationale:
            "The functional data alone carry it past likely pathogenic; the probands agree.",
          classDeterminative: ["cds_fxn", "cln_aff_arxl"],
        },
      }),
    ],
  ],
  [
    THIRD_CURATOR,
    [
      section(ROUTING_ID, {
        case: "routing",
        value: {
          consequenceClass: Consequence.INFRAME_INDEL,
          rationale: "In-frame deletion of one codon.",
        },
      }),
      scoredWorkflow(
        "pop_frq",
        [BELOW_1_5X, DAFT("0.0153"), FAF("0.0104")],
        "Below the threshold, if not by much.",
      ),
      noDataWorkflow(
        "cds_prd",
        "The in-frame predictors I can run were trained with this deletion as a positive, so none of them is independent evidence.",
        [DAMAGING_INDEL],
      ),
      scoredWorkflow(
        "cds_fxn",
        [field("cds_fxn", "CDS_FXN.no", "No", "cds_fxn.no")],
        "The assays I found had no benign controls, so they do not meet the row.",
      ),
      scoredWorkflow(
        "cln_aff_arxl",
        [HOMOZYGOUS_PROBANDS("2")],
        "Two of the three probands share a surname and a clinic; I counted them once.",
      ),
      section(VERDICT_ID, {
        case: "verdict",
        value: {
          classification: Classification.LIKELY_PATHOGENIC,
          rationale:
            "Without a controlled assay the probands hold it at likely pathogenic.",
          classDeterminative: ["cln_aff_arxl", "pop_frq"],
        },
      }),
    ],
  ],
]);
