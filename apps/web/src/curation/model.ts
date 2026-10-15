import type { Assessment } from "@/gen/themis/curation/models/curation_pb";
import type { ModeOfInheritance } from "@/gen/themis/svcv4/models/svcv4_pb";

// The curation module's own domain. Deliberately shares nothing with the workbench's models: a
// curation is not an Analysis, and the two have no reason to move together (curation-surface.md).

/** What a caller may do. Managers additionally administer curators, variants and assignments. */
export type Role = "manager" | "curator";

export interface Person {
  email: string;
  role: Role;
  addedBy: string;
  addedAt: Date;
}

/** A MONDO term id as a registration states it, the form `curation.variants` checks. */
export const MONDO_ID = /^MONDO:[0-9]{7}$/;

/** One VBC against one MDE: the variant under analysis and the entity it is classified against,
 *  whose disease term and mode of inheritance the manager states. Both curators of the variant
 *  answer against this entity, and a curator never restates it. One registration per variant and
 *  entity: the same variant against the same disease under another mode is a registration of its
 *  own. */
export interface Variant {
  id: string;
  gene: string;
  transcript: string;
  hgvsC: string;
  clingenAlleleId: string;
  diseaseLabel: string;
  /** Matches `MONDO_ID`. */
  mondoId: string;
  /** Never UNSPECIFIED. */
  modeOfInheritance: ModeOfInheritance;
  createdBy: string;
  createdAt: Date;
}

export type NewVariant = Omit<Variant, "id" | "createdBy" | "createdAt">;

/** One curator's worksheet on one variant. `workflowsVersion` pins the transcription it is answered
 *  against, so a later correction cannot silently change what a stored answer meant. */
export interface Worksheet {
  id: string;
  variantId: string;
  curatorEmail: string;
  workflowsVersion: string;
  assignedBy: string;
  assignedAt: Date;
}

/** One act of submitting, owning the complete set of assessments it committed. */
export interface Submission {
  id: string;
  worksheetId: string;
  submittedAt: Date;
  note: string;
}

/** One workflow's capture, keyed by the workflow it answers. */
export interface Entry {
  workflowId: string;
  assessment: Assessment;
}

/** A worksheet as its own curator sees it: the variant, the working drafts, and whether it has been
 *  submitted before. */
export interface WorksheetDetail {
  worksheet: Worksheet;
  variant: Variant;
  drafts: Entry[];
  submissions: Submission[];
}

/** A worksheet as a manager sees it in the progress view. `submittedAt` is absent while the curator
 *  is still working. */
export interface WorksheetProgress {
  worksheet: Worksheet;
  draftCount: number;
  latestSubmission?: Submission;
}
