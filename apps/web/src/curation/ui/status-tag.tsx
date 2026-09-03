"use client";

import {
  VARIANT_STATUS_LABELS,
  type VariantProgress,
  WORKSHEET_STATUS_LABELS,
  type WorksheetStatus,
} from "../status";

// How far something has got, as a mark on a list.
//
// Quiet by design, like the rest of the surface: a dot and a word, no fill and no badge. A list of
// worksheets should not read as a scoreboard — "pending" is a fact about a queue, not a reproach.
//
// `curation-recorded` is reused for a submitted worksheet and a complete variant because it already
// means "answered" on the worksheet's own ledger. The other marks stay neutral rather than borrowing
// the ledger's remaining two, which mean something else there (declined, and not yet reached).

const DOT = "mr-1.5 inline-block size-1.5 rounded-full align-middle";

/** A dot and a word, as one ordinary inline box.
 *
 *  Not `inline-flex`: a flex container's baseline is synthesised from its first item, which here is an
 *  empty dot, so the whole tag sat off the baseline of any text beside it — visibly, against the
 *  `N assigned` next to it on a variant card. Keeping the dot inline inside the text means the tag's
 *  baseline is the text's own. */
function Tag({ dot, children }: { dot: string; children: string }) {
  return (
    <span className="framework-voice whitespace-nowrap text-[12.5px] text-ink-muted">
      <span className={`${DOT} ${dot}`} aria-hidden />
      {children}
    </span>
  );
}

export function WorksheetStatusTag({ status }: { status: WorksheetStatus }) {
  const dot = {
    pending: "bg-curation-untouched",
    in_progress: "bg-ink-ghost",
    submitted: "bg-curation-recorded",
  }[status];
  return <Tag dot={dot}>{WORKSHEET_STATUS_LABELS[status]}</Tag>;
}

export function VariantStatusTag({ progress }: { progress: VariantProgress }) {
  const dot = {
    unassigned: "bg-transparent ring-1 ring-line-input",
    pending: "bg-curation-untouched",
    in_progress: "bg-ink-ghost",
    part_submitted: "bg-ink-ghost",
    complete: "bg-curation-recorded",
  }[progress.status];
  // The fraction, where the state is a fraction. `Part submitted` alone leaves a manager counting
  // rows to find out who is being waited on.
  const label =
    progress.status === "part_submitted"
      ? `${VARIANT_STATUS_LABELS.part_submitted} · ${progress.submitted} of ${progress.assigned}`
      : VARIANT_STATUS_LABELS[progress.status];
  return <Tag dot={dot}>{label}</Tag>;
}

/** Filter chips over a fixed set of states, each carrying how many carry it. A state nothing is in is
 *  shown and disabled rather than hidden, so the set of states a reader can filter by does not change
 *  under them as work moves. */
export function StatusFilter<S extends string>({
  order,
  labels,
  counts,
  selected,
  onSelect,
}: {
  order: S[];
  labels: Record<S, string>;
  counts: Record<S, number>;
  /** null is "everything". */
  selected: S | null;
  onSelect: (next: S | null) => void;
}) {
  const total = order.reduce((sum, status) => sum + (counts[status] ?? 0), 0);
  const chip = (active: boolean, empty: boolean) =>
    `framework-voice rounded-sm border px-2.5 py-1 text-[12.5px] transition-colors ${
      active
        ? "border-primary bg-primary text-primary-foreground"
        : `border-line-input bg-white text-ink-muted ${
            empty ? "opacity-40" : "hover:border-ink-ghost"
          }`
    }`;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <button
        type="button"
        aria-pressed={selected === null}
        onClick={() => onSelect(null)}
        className={chip(selected === null, false)}
      >
        All {total}
      </button>
      {order.map((status) => (
        <button
          key={status}
          type="button"
          aria-pressed={selected === status}
          disabled={(counts[status] ?? 0) === 0 && selected !== status}
          onClick={() => onSelect(status)}
          className={chip(selected === status, (counts[status] ?? 0) === 0)}
        >
          {labels[status]} {counts[status] ?? 0}
        </button>
      ))}
    </div>
  );
}

export type SortBy = "recent" | "status";

export function SortControl({
  value,
  onChange,
  recentLabel,
}: {
  value: SortBy;
  onChange: (next: SortBy) => void;
  recentLabel: string;
}) {
  return (
    <label className="framework-voice flex items-center gap-2 whitespace-nowrap text-[12.5px] text-ink-faint">
      Sort
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as SortBy)}
        className="framework-voice rounded-sm border border-line-input bg-white px-2 py-1 text-[12.5px] text-ink-body"
      >
        <option value="recent">{recentLabel}</option>
        <option value="status">By status</option>
      </select>
    </label>
  );
}
