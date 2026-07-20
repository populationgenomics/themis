"use client";

import { type Timestamp, timestampDate } from "@bufbuild/protobuf/wkt";
import { ChevronDown, History } from "lucide-react";
import { DropdownMenu, type MenuItem } from "@/components/ui/dropdown-menu";
import type { Analysis } from "@/models/workbench";

// A compact switcher over the prior analyses: a trigger that opens a dropdown of
// them (prompt, created time). Selecting one hands its id back to the workbench,
// which restores that analysis's poll + document view.
export function AnalysisBrowser({
  analyses,
  currentId,
  onSelect,
}: {
  analyses: Analysis[];
  currentId: string | null;
  onSelect: (id: string) => void;
}) {
  const items: MenuItem[] = analyses.map((analysis) => ({
    key: analysis.id,
    label: <AnalysisLabel analysis={analysis} />,
    selected: analysis.id === currentId,
    onSelect: () => onSelect(analysis.id),
  }));

  return (
    <DropdownMenu
      items={items}
      align="end"
      emptyLabel={
        <div className="px-[14px] py-[16px] text-[12.5px] text-ink-faintest">
          No prior analyses yet.
        </div>
      }
      triggerClassName="flex h-[32px] items-center gap-[7px] rounded-field border border-line-input bg-white px-[11px] text-[12px] font-medium text-ink-muted"
      menuClassName="tscroll mt-[6px] max-h-[320px] w-[340px] overflow-auto rounded-card"
      itemClassName="px-[14px] py-[10px]"
    >
      <History className="size-[13px] text-ink-faint" aria-hidden />
      Analyses
      <span className="font-mono text-[11px] text-ink-faintest">
        {analyses.length}
      </span>
      <ChevronDown className="size-[12px] text-ink-faintest" aria-hidden />
    </DropdownMenu>
  );
}

function AnalysisLabel({ analysis }: { analysis: Analysis }) {
  return (
    <span className="flex flex-col gap-[6px]">
      <span className="truncate text-[13px] font-medium text-ink-body">
        {analysis.prompt}
      </span>
      <span className="font-mono text-[10.5px] text-ink-faintest">
        {analysis.createdAt && formatCreatedAt(analysis.createdAt)}
      </span>
    </span>
  );
}

function formatCreatedAt(createdAt: Timestamp): string {
  return timestampDate(createdAt).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
