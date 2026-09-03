import { fromBinary, toBinary } from "@bufbuild/protobuf";
import { AssessmentSchema } from "@/gen/themis/curation/models/curation_pb";
import { ClientInputError, ResourceNotFoundError } from "@/server/errors";
import { DEV_USER_EMAIL } from "@/server/identity";
import type {
  Entry,
  NewVariant,
  Person,
  Role,
  Submission,
  Variant,
  Worksheet,
} from "./model";
import type { CurationStore } from "./store";
import { WORKFLOWS_VERSION } from "./version";

// The offline store: the same semantics as `sql.ts`, in memory. Assessments round-trip through the
// proto encoding here too, so an encoding fault shows up offline rather than only against Cloud SQL.
//
// Seeded with one manager (the dev user, who is also assigned a worksheet — the arrangement the
// blindness rule exists for) and two curators, so the surface has something to render without a
// database.

const SECOND_CURATOR = "sam.okafor@example.org";
const THIRD_CURATOR = "rin.tanaka@example.org";

interface StoredSubmission extends Submission {
  entries: Map<string, Uint8Array>;
}

export class FixtureCurationStore implements CurationStore {
  private readonly people = new Map<string, Person>();
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
    for (const [email, role] of [
      [DEV_USER_EMAIL, "manager"],
      [SECOND_CURATOR, "curator"],
      [THIRD_CURATOR, "curator"],
    ] as const) {
      this.people.set(email, {
        email,
        role,
        addedBy: DEV_USER_EMAIL,
        addedAt: at,
      });
    }
    const seeded: NewVariant[] = [
      {
        gene: "MYH7",
        transcript: "NM_000257.4",
        hgvsC: "c.1988G>A",
        clingenAlleleId: "CA011552",
        diseaseLabel: "hypertrophic cardiomyopathy",
        mondoId: "MONDO:0005045",
      },
      {
        gene: "CFTR",
        transcript: "NM_000492.4",
        hgvsC: "c.1521_1523del",
        clingenAlleleId: "CA118639",
        diseaseLabel: "cystic fibrosis",
        mondoId: "MONDO:0009061",
      },
    ];
    for (const variant of seeded) {
      const stored = this.insertVariant(variant, DEV_USER_EMAIL, at);
      // The dev user curates the first variant as well as managing, so the offline surface
      // exercises the manager-who-is-also-assigned case rather than only the clean one.
      const curators =
        stored.gene === "MYH7"
          ? [DEV_USER_EMAIL, SECOND_CURATOR]
          : [SECOND_CURATOR, THIRD_CURATOR];
      for (const curator of curators) {
        this.insertWorksheet(stored.id, curator, DEV_USER_EMAIL, at);
      }
    }
  }

  private insertVariant(
    variant: NewVariant,
    createdBy: string,
    createdAt: Date,
  ): Variant {
    const stored: Variant = {
      ...variant,
      id: this.id("var"),
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

  async roleOf(email: string): Promise<Role | undefined> {
    return this.people.get(email)?.role;
  }

  async listPeople(): Promise<Person[]> {
    return [...this.people.values()].sort((a, b) =>
      a.email.localeCompare(b.email),
    );
  }

  async addPerson(email: string, role: Role, addedBy: string): Promise<Person> {
    const existing = this.people.get(email);
    if (existing !== undefined) return existing;
    const person: Person = { email, role, addedBy, addedAt: new Date() };
    this.people.set(email, person);
    return person;
  }

  async removePerson(email: string): Promise<void> {
    this.people.delete(email);
  }

  async createVariant(
    variant: NewVariant,
    createdBy: string,
  ): Promise<Variant> {
    return this.insertVariant(variant, createdBy, new Date());
  }

  async listVariants(): Promise<Variant[]> {
    return [...this.variants.values()].sort(
      (a, b) => b.createdAt.getTime() - a.createdAt.getTime(),
    );
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

  async submit(worksheetId: string, note: string): Promise<Submission> {
    if (!this.worksheets.has(worksheetId)) {
      throw new ResourceNotFoundError(`no such worksheet: ${worksheetId}`);
    }
    const drafts = this.draftRows.get(worksheetId);
    if (drafts === undefined || drafts.size === 0) {
      throw new ClientInputError(
        "nothing to submit: the worksheet holds no answers",
      );
    }
    const submission: StoredSubmission = {
      id: this.id("sub"),
      worksheetId,
      submittedAt: new Date(),
      note,
      entries: new Map(drafts),
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
