import type {
  Entry,
  NewVariant,
  Person,
  Role,
  Submission,
  Variant,
  Worksheet,
} from "./model";

// The storage port. Two implementations: Cloud SQL (`sql.ts`) and an in-memory fixture
// (`fixture.ts`), selected by `THEMIS_BACKEND`.
//
// Unscoped by design — every method here trusts its caller. `access.ts` is the only thing that
// constructs one for a request, and it is what decides who may see what; a route holding a raw
// store would bypass the blindness rules the reference depends on.

export interface CurationStore {
  /** The caller's role, or undefined for an email with no row — which is how a verified IAP user
   *  who was never granted access is refused. */
  roleOf(email: string): Promise<Role | undefined>;

  listPeople(): Promise<Person[]>;
  addPerson(email: string, role: Role, addedBy: string): Promise<Person>;
  removePerson(email: string): Promise<void>;

  createVariant(variant: NewVariant, createdBy: string): Promise<Variant>;
  listVariants(): Promise<Variant[]>;
  getVariant(id: string): Promise<Variant | undefined>;

  /** Assign a curator, minting their worksheet. Rejects a second assignment of the same curator to
   *  the same variant. */
  assign(
    variantId: string,
    curatorEmail: string,
    workflowsVersion: string,
    assignedBy: string,
  ): Promise<Worksheet>;

  /** Withdraw an assignment. Refused once the worksheet carries a submission — a reference a round
   *  may already have read cannot be deleted out from under it. */
  withdraw(worksheetId: string): Promise<void>;

  getWorksheet(id: string): Promise<Worksheet | undefined>;
  worksheetsOfCurator(email: string): Promise<Worksheet[]>;
  worksheetsOfVariant(variantId: string): Promise<Worksheet[]>;

  /** The working drafts, one per workflow answered so far. */
  drafts(worksheetId: string): Promise<Entry[]>;

  /** Upsert one workflow's draft. */
  putDraft(worksheetId: string, entry: Entry): Promise<void>;

  /** Every submission on the worksheet, oldest first, so the last is the current one. */
  submissions(worksheetId: string): Promise<Submission[]>;

  /** Copy every draft into a new submission's assessments, in one transaction. A partially
   *  committed submission would be a reference nobody could tell was partial. Rejects a worksheet
   *  with no drafts. */
  submit(worksheetId: string, note: string): Promise<Submission>;

  /** What a submission committed. */
  assessments(submissionId: string): Promise<Entry[]>;
}
