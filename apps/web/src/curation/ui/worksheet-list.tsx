"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import {
  WORKSHEET_STATUS_LABELS,
  WORKSHEET_STATUS_ORDER,
  type WorksheetStatus,
  worksheetStatus,
} from "../status";
import {
  type SortBy,
  SortControl,
  StatusFilter,
  WorksheetStatusTag,
} from "./status-tag";

// A curator's own worksheets, with how far each has got.
//
// The status is derived here from the two facts the server sent rather than stored — see `status.ts`.
// Filtering and sorting are local state, not a query: the list is one page of the caller's own
// assignments, so there is nothing to paginate and no round trip to spend.

export interface WorksheetRow {
  worksheetId: string;
  gene: string;
  transcript: string;
  hgvsC: string;
  diseaseLabel: string;
  draftCount: number;
  submittedAt: string | null;
}

export function WorksheetList({ rows }: { rows: WorksheetRow[] }) {
  const [status, setStatus] = useState<WorksheetStatus | null>(null);
  const [sort, setSort] = useState<SortBy>("recent");

  const withStatus = useMemo(
    () => rows.map((row) => ({ row, status: worksheetStatus(row) })),
    [rows],
  );

  const counts = useMemo(() => {
    const out = { pending: 0, in_progress: 0, submitted: 0 };
    for (const { status: s } of withStatus) out[s] += 1;
    return out;
  }, [withStatus]);

  const shown = useMemo(() => {
    const kept = withStatus.filter(
      (entry) => status === null || entry.status === status,
    );
    if (sort === "recent") return kept;
    // `rows` arrives newest-assignment first, so a stable sort by status keeps that as the tiebreak.
    return [...kept].sort(
      (a, b) =>
        WORKSHEET_STATUS_ORDER.indexOf(a.status) -
        WORKSHEET_STATUS_ORDER.indexOf(b.status),
    );
  }, [withStatus, status, sort]);

  if (rows.length === 0) {
    return (
      <p className="framework-voice rounded-md border border-line-primary border-dashed bg-white px-5 py-10 text-center text-[13.5px] text-ink-muted">
        Nothing assigned yet. A manager assigns variants to curate.
      </p>
    );
  }

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <StatusFilter
          order={WORKSHEET_STATUS_ORDER}
          labels={WORKSHEET_STATUS_LABELS}
          counts={counts}
          selected={status}
          onSelect={setStatus}
        />
        <SortControl
          value={sort}
          onChange={setSort}
          recentLabel="Newest assigned"
        />
      </div>
      {shown.length === 0 ? (
        <p className="framework-voice rounded-md border border-line-primary border-dashed bg-white px-5 py-8 text-center text-[13.5px] text-ink-muted">
          None of your worksheets is{" "}
          {WORKSHEET_STATUS_LABELS[status ?? "pending"].toLowerCase()}.
        </p>
      ) : (
        <ul className="space-y-2">
          {shown.map(({ row, status: rowStatus }) => (
            <li key={row.worksheetId}>
              <Link
                href={`/curation/${row.worksheetId}`}
                className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 rounded-md border border-line-primary bg-white px-4 py-3 hover:border-ink-ghost"
              >
                <div className="min-w-0">
                  <p className="font-mono text-[13px] text-ink-primary">
                    {row.transcript}:{row.hgvsC}
                  </p>
                  <p className="framework-voice mt-0.5 text-[13px] text-ink-muted">
                    {row.gene} · {row.diseaseLabel}
                  </p>
                </div>
                <span className="flex items-baseline gap-3">
                  {rowStatus === "in_progress" ? (
                    <span className="framework-voice text-[12.5px] text-ink-faint">
                      {row.draftCount} workflow
                      {row.draftCount === 1 ? "" : "s"}
                    </span>
                  ) : null}
                  {row.submittedAt ? (
                    <span className="framework-voice text-[12.5px] text-ink-faint">
                      {row.submittedAt.slice(0, 10)}
                    </span>
                  ) : null}
                  <WorksheetStatusTag status={rowStatus} />
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
