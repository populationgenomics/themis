// How far a worksheet, and a variant, have got.
//
// Derived, never stored. The lifecycle needs no state column — submitted is a submissions row
// existing, in progress is a draft existing — so a status here is a reading of those two facts and
// cannot drift from them (curation-surface.md §Storage).
//
// One module for both screens: a curator's list and the manager's view showed the same two numbers in
// two different vocabularies, so they could disagree about what "in progress" meant.

/** One curator's own worksheet. */
export type WorksheetStatus = "pending" | "in_progress" | "submitted";

/** One variant, over every curator assigned to it.
 *
 *  `part_submitted` is its own state rather than folded into either neighbour: a variant exists to
 *  carry two blind answers, so one submitted answer is neither work in progress nor a finished
 *  variant, and it is the state a manager most needs to see — it is the one where somebody is waited
 *  on. */
export type VariantStatus =
  | "unassigned"
  | "pending"
  | "in_progress"
  | "part_submitted"
  | "complete";

/** What a worksheet has: how many workflows carry a draft, and whether it has been submitted. */
export interface WorksheetFacts {
  draftCount: number;
  submittedAt: string | null;
}

export interface VariantProgress {
  status: VariantStatus;
  /** Assigned curators who have submitted, and how many are assigned — so `part_submitted` can say
   *  which fraction rather than only that it is a fraction. */
  submitted: number;
  assigned: number;
}

export function worksheetStatus(facts: WorksheetFacts): WorksheetStatus {
  if (facts.submittedAt !== null) return "submitted";
  return facts.draftCount > 0 ? "in_progress" : "pending";
}

/** The variant's own state, over its worksheets.
 *
 *  `complete` means every assigned curator has submitted — not "somebody has". A variant reading
 *  complete while a second curator has not started is the one state the concordance measurement
 *  cannot use, so it is the reading the tag must not permit.
 */
export function variantProgress(rows: WorksheetFacts[]): VariantProgress {
  const statuses = rows.map(worksheetStatus);
  const submitted = statuses.filter((s) => s === "submitted").length;
  const progress = { submitted, assigned: rows.length };
  if (rows.length === 0) return { ...progress, status: "unassigned" };
  if (submitted === rows.length) return { ...progress, status: "complete" };
  if (submitted > 0) return { ...progress, status: "part_submitted" };
  const started = statuses.some((s) => s === "in_progress");
  return { ...progress, status: started ? "in_progress" : "pending" };
}

/** Least done first, which is the order a reader scanning for what needs doing wants. */
export const VARIANT_STATUS_ORDER: VariantStatus[] = [
  "unassigned",
  "pending",
  "in_progress",
  "part_submitted",
  "complete",
];

export const WORKSHEET_STATUS_ORDER: WorksheetStatus[] = [
  "pending",
  "in_progress",
  "submitted",
];

export const VARIANT_STATUS_LABELS: Record<VariantStatus, string> = {
  unassigned: "Unassigned",
  pending: "Pending",
  in_progress: "In progress",
  part_submitted: "Part submitted",
  complete: "Complete",
};

export const WORKSHEET_STATUS_LABELS: Record<WorksheetStatus, string> = {
  pending: "Pending",
  in_progress: "In progress",
  submitted: "Submitted",
};
