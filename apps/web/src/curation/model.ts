import type { Assessment } from "@/gen/themis/curation/models/curation_pb";

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

/** One VBC against one MDE — the variant under analysis and the entity it is classified against.
 *  Every field is manager-typed; the surface resolves nothing.
 *
 *  The mode of inheritance is not here. It is half of what an MDE is, but the registration precedes
 *  every curator, and a mode filled in for them is a judgement they cannot be told to have made —
 *  each worksheet's `RoutingAssessment` is the whole record of it. */
export interface Variant {
  id: string;
  gene: string;
  transcript: string;
  hgvsC: string;
  clingenAlleleId: string;
  diseaseLabel: string;
  mondoId: string;
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
