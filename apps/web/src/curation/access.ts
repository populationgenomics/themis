import { Role } from "@/gen/themis/rpc/auth_pb";
import { AssessmentStatus } from "@/gen/themis/svcv4/models/svcv4_pb";
import { asciiLowercase, isServiceAccountEmail } from "@/server/email";
import { ClientInputError, ResourceNotFoundError } from "@/server/errors";
import type {
  Entry,
  NewVariant,
  Submission,
  Variant,
  Worksheet,
  WorksheetDetail,
  WorksheetProgress,
} from "./model";
import type { ResolvedAllele, VariantResolver } from "./resolver";
import { ROUTING_ID, VERDICT_ID } from "./sections";
import type { CurationStore } from "./store";

// The one place a caller's verified email and their roles in a Project become permission to read or
// write something. Every route goes through an instance of this; none holds a bare `CurationStore`.
// An instance is bound to one Project: a registration in another Project, and every worksheet on
// one, is not-found here, exactly as a worksheet of someone else's is.
//
// The roles are the ACL's, through the access answer of this request: a manager manages the
// Project's registrations, assigns its curators and reads the comparisons; a curator works the
// worksheets assigned to them. One person may hold both.
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

/** Thrown for a member of the Project reaching an operation their roles there do not allow: a
 *  curator managing, a manager working a worksheet without the curator role. Distinct from
 *  not-found: the operation's existence is not a secret, and telling someone they need the role is
 *  more useful than a lie. */
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

/** The caller's roles in each Project, as this request's access answer granted them. */
export interface ProjectRoles {
  rolesIn(projectId: string): ReadonlySet<Role>;
}

/** Whether `email` holds the curator role on `projectId`, asked of the ACL for someone other
 *  than the caller. */
export type CuratorCheck = (
  email: string,
  projectId: string,
) => Promise<boolean>;

/** The caller's access to the curation of `projectId`. A Project the caller holds no role on is
 *  not-found, as the workbench answers it. */
export function accessFor(
  store: CurationStore,
  caller: { email: string; roles: ProjectRoles },
  projectId: string,
  resolver: VariantResolver,
  isCurator: CuratorCheck,
): CurationAccess {
  const roles = caller.roles.rolesIn(projectId);
  if (roles.size === 0) {
    throw new ResourceNotFoundError(`no such project: ${projectId}`);
  }
  return new CurationAccess(
    store,
    caller.email,
    projectId,
    roles,
    resolver,
    isCurator,
  );
}

/** An address as the ACL states it: ASCII letters lowercased, as the auth service matches. */
function asListed(email: string): string {
  return asciiLowercase(email.trim());
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
    readonly projectId: string,
    private readonly roles: ReadonlySet<Role>,
    private readonly resolver: VariantResolver,
    private readonly isCurator: CuratorCheck,
  ) {}

  get isManager(): boolean {
    return this.roles.has(Role.MANAGER);
  }

  get isCuratorHere(): boolean {
    return this.roles.has(Role.CURATOR);
  }

  private requireManager(operation: string): void {
    if (!this.isManager) {
      throw new CurationAccessError(`${operation} is a manager operation`);
    }
  }

  private requireCurator(operation: string): void {
    if (!this.isCuratorHere) {
      throw new CurationAccessError(
        `${operation} needs the curator role on ${this.projectId}`,
      );
    }
  }

  /** The caller's own worksheets in this Project, newest assignment first, each with its variant and
   *  how far it has got. Their own progress only — the counts say nothing about anybody else's
   *  worksheet on the same variant, which stays as invisible here as everywhere else. Empty for a
   *  caller without the curator role, who has no worksheets to work. */
  async myWorksheets(): Promise<MyWorksheet[]> {
    if (!this.isCuratorHere) return [];
    const worksheets = await this.store.worksheetsOfCurator(this.email);
    const out: MyWorksheet[] = [];
    for (const worksheet of worksheets) {
      const variant = await this.store.getVariant(worksheet.variantId);
      if (variant === undefined) {
        throw new Error(
          `worksheet ${worksheet.id} references missing variant ${worksheet.variantId}`,
        );
      }
      if (variant.projectId !== this.projectId) continue;
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
    const { worksheet, variant } = await this.ownWorksheet(worksheetId);
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

  /** Commit the drafts of the workflows the worksheet shows, and of its two sections, as one
   *  submission. `shownWorkflowIds` is what the routing put on the curator's screen: a draft for a
   *  workflow the routing has since hidden stays a draft, because the curator cannot see what it
   *  would submit.
   *
   *  Refuses a scored workflow carrying no rationale. That row is the reference's entire value — a
   *  selection with no reasoning is a number, and a number is what the reference exists not to be —
   *  and the refusal has to be here rather than in the UI, because the store copies drafts inside
   *  the database without decoding them and would commit whatever is there. */
  async submit(
    worksheetId: string,
    note: string,
    shownWorkflowIds: readonly string[],
  ): Promise<Submission> {
    await this.ownWorksheet(worksheetId);
    const submitted = [...shownWorkflowIds, ROUTING_ID, VERDICT_ID];
    const unreasoned: string[] = [];
    for (const entry of await this.store.drafts(worksheetId)) {
      if (!submitted.includes(entry.workflowId)) continue;
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
    return this.store.submit(worksheetId, note, submitted);
  }

  /** The caller's own worksheet in this Project, with its variant. Anybody else's, one in another
   *  Project, and an unknown one are the same not-found. Refused to a caller without the curator
   *  role here: working a worksheet is a curator's operation. */
  private async ownWorksheet(
    worksheetId: string,
  ): Promise<{ worksheet: Worksheet; variant: Variant }> {
    this.requireCurator("working a worksheet");
    const worksheet = await this.store.getWorksheet(worksheetId);
    if (worksheet === undefined || worksheet.curatorEmail !== this.email) {
      throw new ResourceNotFoundError(`no such worksheet: ${worksheetId}`);
    }
    const variant = await this.store.getVariant(worksheet.variantId);
    if (variant === undefined) {
      throw new Error(
        `worksheet ${worksheet.id} references missing variant ${worksheet.variantId}`,
      );
    }
    if (variant.projectId !== this.projectId) {
      throw new ResourceNotFoundError(`no such worksheet: ${worksheetId}`);
    }
    return { worksheet, variant };
  }

  /** A registration of this Project; one in another Project is the same not-found as an unknown id. */
  private async variantHere(variantId: string): Promise<Variant> {
    const variant = await this.store.getVariant(variantId);
    if (variant === undefined || variant.projectId !== this.projectId) {
      throw new ResourceNotFoundError(`no such variant: ${variantId}`);
    }
    return variant;
  }

  // --- manager operations ---

  async listVariants(): Promise<Variant[]> {
    this.requireManager("listing variants");
    return this.store.listVariants(this.projectId);
  }

  /** One registered variant of this Project. Manager-only, as `listVariants` is. */
  async getVariant(variantId: string): Promise<Variant> {
    this.requireManager("reading a variant");
    return this.variantHere(variantId);
  }

  async createVariant(variant: NewVariant): Promise<Variant> {
    this.requireManager("creating a variant");
    return this.store.createVariant(this.projectId, variant, this.email);
  }

  /** The identity the ClinGen Allele Registry holds for an allele id. Manager-only for the reason
   *  `createVariant` is: it is a step of registering a variant, which is a manager's work. */
  async resolveAllele(clingenAlleleId: string): Promise<ResolvedAllele> {
    this.requireManager("resolving a ClinGen allele id");
    return this.resolver.resolve(clingenAlleleId);
  }

  /** Assign `curatorEmail` to a variant of this Project. The address is taken as the ACL lists
   *  it, ASCII letters lowercased, and it must hold the curator role on this Project there: the
   *  site's lists are the only source of who curates. */
  async assign(
    variantId: string,
    curatorEmail: string,
    workflowsVersion: string,
  ): Promise<Worksheet> {
    this.requireManager("assigning a curator");
    await this.variantHere(variantId);
    const curator = asListed(curatorEmail);
    // The operator account holds every role on every Project, and a worksheet is a person's work.
    if (isServiceAccountEmail(curator)) {
      throw new ClientInputError(
        `${curator} is a service account; a worksheet goes to a person`,
      );
    }
    if (!(await this.isCurator(curator, this.projectId))) {
      throw new ClientInputError(
        `${curator} is not a curator of ${this.projectId}; the site's access list names who is`,
      );
    }
    return this.store.assign(variantId, curator, workflowsVersion, this.email);
  }

  async withdraw(worksheetId: string): Promise<void> {
    this.requireManager("withdrawing an assignment");
    const worksheet = await this.store.getWorksheet(worksheetId);
    const variant =
      worksheet === undefined
        ? undefined
        : await this.store.getVariant(worksheet.variantId);
    if (variant?.projectId !== this.projectId) {
      throw new ResourceNotFoundError(`no such worksheet: ${worksheetId}`);
    }
    await this.store.withdraw(worksheetId);
  }

  /** Per-curator progress on one variant. Counts and submission times only — no answers — so it is
   *  readable by a manager who curates the variant themselves. */
  async progress(variantId: string): Promise<WorksheetProgress[]> {
    this.requireManager("reading progress");
    await this.variantHere(variantId);
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
    await this.variantHere(variantId);
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
