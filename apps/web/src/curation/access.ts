import { AssessmentStatus } from "@/gen/themis/curation/models/curation_pb";
import { ClientInputError, ResourceNotFoundError } from "@/server/errors";
import type {
  Entry,
  NewVariant,
  Person,
  Role,
  Submission,
  Variant,
  Worksheet,
  WorksheetDetail,
  WorksheetProgress,
} from "./model";
import type { ResolvedAllele, VariantResolver } from "./resolver";
import type { CurationStore } from "./store";

// The one place a caller's verified email becomes permission to read or write something. Every
// route goes through an instance of this; none holds a bare `CurationStore`.
//
// Two rules carry the blindness the concordance measurement depends on:
//
//   - a curator sees their own worksheets and no one else's answers, submitted or not;
//   - a manager who is themselves assigned to a variant does not see that variant's other answers.
//     Without the second, blindness is defeated by role, which is the likeliest way to lose it in a
//     small team where the manager also curates.
//
// An unknown worksheet and one belonging to someone else are the same not-found, never a
// distinguishable forbidden.

/** Thrown for a verified caller who holds no curation role, or a curator reaching a manager-only
 *  operation. Distinct from not-found: the surface's existence is not a secret, and telling someone
 *  they need access is more useful than a lie. */
export class CurationAccessError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CurationAccessError";
  }
}

export function isCurationAccessError(
  error: unknown,
): error is CurationAccessError {
  return error instanceof Error && error.name === "CurationAccessError";
}

/** Resolve the caller's role and hand back their scoped access, or refuse. */
export async function accessFor(
  store: CurationStore,
  email: string,
  resolver: VariantResolver,
): Promise<CurationAccess> {
  const role = await store.roleOf(email);
  if (role === undefined) {
    throw new CurationAccessError(
      `${email} has no curation role; a manager must grant one`,
    );
  }
  return new CurationAccess(store, email, role, resolver);
}

/** One of the caller's own worksheets, with the two facts a status is read from. */
export interface MyWorksheet {
  worksheet: Worksheet;
  variant: Variant;
  draftCount: number;
  latestSubmission?: Submission;
}

export class CurationAccess {
  constructor(
    private readonly store: CurationStore,
    readonly email: string,
    readonly role: Role,
    private readonly resolver: VariantResolver,
  ) {}

  get isManager(): boolean {
    return this.role === "manager";
  }

  private requireManager(operation: string): void {
    if (!this.isManager) {
      throw new CurationAccessError(`${operation} is a manager operation`);
    }
  }

  /** The caller's own worksheets, newest assignment first, each with its variant and how far it has
   *  got. Their own progress only — the counts say nothing about anybody else's worksheet on the same
   *  variant, which stays as invisible here as everywhere else. */
  async myWorksheets(): Promise<MyWorksheet[]> {
    const worksheets = await this.store.worksheetsOfCurator(this.email);
    const out: MyWorksheet[] = [];
    for (const worksheet of worksheets) {
      const variant = await this.store.getVariant(worksheet.variantId);
      if (variant === undefined) {
        throw new Error(
          `worksheet ${worksheet.id} references missing variant ${worksheet.variantId}`,
        );
      }
      const submissions = await this.store.submissions(worksheet.id);
      out.push({
        worksheet,
        variant,
        draftCount: (await this.store.drafts(worksheet.id)).length,
        latestSubmission: submissions.at(-1),
      });
    }
    return out;
  }

  /** One of the caller's OWN worksheets, with its drafts. Another curator's worksheet is not-found
   *  here whatever the caller's role: this is the editing surface, and nobody edits another's. */
  async myWorksheet(worksheetId: string): Promise<WorksheetDetail> {
    const worksheet = await this.ownWorksheet(worksheetId);
    const variant = await this.store.getVariant(worksheet.variantId);
    if (variant === undefined) {
      throw new Error(
        `worksheet ${worksheet.id} references missing variant ${worksheet.variantId}`,
      );
    }
    return {
      worksheet,
      variant,
      drafts: await this.store.drafts(worksheetId),
      submissions: await this.store.submissions(worksheetId),
    };
  }

  async saveDraft(worksheetId: string, entry: Entry): Promise<void> {
    await this.ownWorksheet(worksheetId);
    await this.store.putDraft(worksheetId, entry);
  }

  /** Commit every draft as one submission.
   *
   *  Refuses a scored workflow carrying no rationale. That row is the reference's entire value — a
   *  selection with no reasoning is a number, and a number is what the reference exists not to be —
   *  and the refusal has to be here rather than in the UI, because the store copies drafts inside
   *  the database without decoding them and would commit whatever is there. */
  async submit(worksheetId: string, note: string): Promise<Submission> {
    await this.ownWorksheet(worksheetId);
    const unreasoned: string[] = [];
    for (const entry of await this.store.drafts(worksheetId)) {
      const workflow =
        entry.assessment.kind.case === "workflow"
          ? entry.assessment.kind.value
          : undefined;
      if (
        workflow?.status === AssessmentStatus.SCORED &&
        workflow.rationale.trim() === ""
      ) {
        unreasoned.push(entry.workflowId);
      }
    }
    if (unreasoned.length > 0) {
      throw new ClientInputError(
        `${unreasoned.length} scored workflow${unreasoned.length === 1 ? "" : "s"} ` +
          `carr${unreasoned.length === 1 ? "ies" : "y"} no rationale: ${unreasoned.join(", ")}. ` +
          "A selection without reasoning cannot be compared to anyone else's.",
      );
    }
    return this.store.submit(worksheetId, note);
  }

  private async ownWorksheet(worksheetId: string): Promise<Worksheet> {
    const worksheet = await this.store.getWorksheet(worksheetId);
    if (worksheet === undefined || worksheet.curatorEmail !== this.email) {
      throw new ResourceNotFoundError(`no such worksheet: ${worksheetId}`);
    }
    return worksheet;
  }

  // --- manager operations ---

  async listPeople(): Promise<Person[]> {
    this.requireManager("listing curators");
    return this.store.listPeople();
  }

  async addCurator(email: string): Promise<Person> {
    this.requireManager("adding a curator");
    return this.store.addPerson(email, "curator", this.email);
  }

  async removePerson(email: string): Promise<void> {
    this.requireManager("removing a curator");
    if (email === this.email) {
      throw new CurationAccessError(
        "a manager cannot remove their own role; ask another manager",
      );
    }
    await this.store.removePerson(email);
  }

  async listVariants(): Promise<Variant[]> {
    this.requireManager("listing variants");
    return this.store.listVariants();
  }

  async createVariant(variant: NewVariant): Promise<Variant> {
    this.requireManager("creating a variant");
    return this.store.createVariant(variant, this.email);
  }

  /** The identity the ClinGen Allele Registry holds for an allele id. Manager-only for the reason
   *  `createVariant` is: it is a step of registering a variant, which is a manager's work. */
  async resolveAllele(clingenAlleleId: string): Promise<ResolvedAllele> {
    this.requireManager("resolving a ClinGen allele id");
    return this.resolver.resolve(clingenAlleleId);
  }

  async assign(
    variantId: string,
    curatorEmail: string,
    workflowsVersion: string,
  ): Promise<Worksheet> {
    this.requireManager("assigning a curator");
    if ((await this.store.getVariant(variantId)) === undefined) {
      throw new ResourceNotFoundError(`no such variant: ${variantId}`);
    }
    if ((await this.store.roleOf(curatorEmail)) === undefined) {
      throw new CurationAccessError(
        `${curatorEmail} has no curation role; add them before assigning`,
      );
    }
    return this.store.assign(
      variantId,
      curatorEmail,
      workflowsVersion,
      this.email,
    );
  }

  async withdraw(worksheetId: string): Promise<void> {
    this.requireManager("withdrawing an assignment");
    const worksheet = await this.store.getWorksheet(worksheetId);
    if (worksheet === undefined) {
      throw new ResourceNotFoundError(`no such worksheet: ${worksheetId}`);
    }
    await this.store.withdraw(worksheetId);
  }

  /** Per-curator progress on one variant. Counts and submission times only — no answers — so it is
   *  readable by a manager who curates the variant themselves. */
  async progress(variantId: string): Promise<WorksheetProgress[]> {
    this.requireManager("reading progress");
    const worksheets = await this.store.worksheetsOfVariant(variantId);
    const out: WorksheetProgress[] = [];
    for (const worksheet of worksheets) {
      const submissions = await this.store.submissions(worksheet.id);
      const latest = submissions.at(-1);
      out.push({
        worksheet,
        draftCount: (await this.store.drafts(worksheet.id)).length,
        // The note is the curator's own prose, dropped here rather than trusted to every caller to
        // project away: this view is readable by a manager who curates the variant themselves.
        latestSubmission:
          latest === undefined ? undefined : { ...latest, note: "" },
      });
    }
    return out;
  }

  /** Every curator's submitted answers on one variant, for reading divergence.
   *
   *  Refused where the caller is themselves assigned to the variant, whatever their role, and
   *  refused until at least two curators have submitted — one submitted answer is not a comparison,
   *  and serving it would let a manager read a colleague's reasoning under the name of one. */
  async comparison(
    variantId: string,
  ): Promise<
    { worksheet: Worksheet; submission: Submission; entries: Entry[] }[]
  > {
    this.requireManager("comparing answers");
    const worksheets = await this.store.worksheetsOfVariant(variantId);
    if (worksheets.some((w) => w.curatorEmail === this.email)) {
      throw new CurationAccessError(
        `you are assigned to this variant, so its other answers stay blind to you`,
      );
    }
    const out: {
      worksheet: Worksheet;
      submission: Submission;
      entries: Entry[];
    }[] = [];
    for (const worksheet of worksheets) {
      const latest = (await this.store.submissions(worksheet.id)).at(-1);
      if (latest === undefined) continue;
      out.push({
        worksheet,
        submission: latest,
        entries: await this.store.assessments(latest.id),
      });
    }
    if (out.length < 2) {
      throw new CurationAccessError(
        `${out.length} of ${worksheets.length} curators have submitted; a comparison needs two`,
      );
    }
    return out;
  }
}
