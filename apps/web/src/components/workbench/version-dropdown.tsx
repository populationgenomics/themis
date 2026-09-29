"use client";

import { ChevronDown } from "lucide-react";
import { DropdownMenu, type MenuItem } from "@/components/ui/dropdown-menu";
import { absoluteTime } from "@/lib/format";
import type { DocumentVersion } from "./working-document";

// The working-document version picker: one row per tip the reflog recorded for the branch, newest
// first, each timed by the publish that made it the tip.

/** The picker's rows, newest first. `commit` is what selecting the row means: null for the tip
 *  (follow the branch), the commit itself for an older version (pin it). */
export function versionMenuItems(
  versions: readonly DocumentVersion[],
  selected: string,
): {
  key: string;
  label: string;
  time: string;
  selected: boolean;
  commit: string | null;
}[] {
  return versions.map((version, index) => ({
    key: version.commit,
    label: `v${version.number}`,
    time: absoluteTime(
      new Date(version.timestamp * 1000).toISOString(),
      "reader",
    ),
    selected: version.commit === selected,
    commit: index === 0 ? null : version.commit,
  }));
}

/** The trigger's label for the commit shown: its version number, or its abbreviated id while the
 *  picker's versions do not name it. */
export function versionLabel(
  versions: readonly DocumentVersion[],
  selected: string,
): string {
  const version = versions.find((v) => v.commit === selected);
  return version === undefined ? selected.slice(0, 7) : `v${version.number}`;
}

export function VersionDropdown({
  versions,
  selected,
  onSelect,
}: {
  /** Newest first. */
  versions: readonly DocumentVersion[];
  /** The commit currently shown. */
  selected: string;
  /** Receives the selected row's meaning — see `versionMenuItems`. */
  onSelect: (commit: string | null) => void;
}): React.ReactElement {
  const items: MenuItem[] = versionMenuItems(versions, selected).map(
    (item) => ({
      key: item.key,
      label: (
        <span className="flex items-baseline gap-[10px]">
          <span>{item.label}</span>
          <span className="text-ink-faintest">{item.time}</span>
        </span>
      ),
      selected: item.selected,
      onSelect: () => onSelect(item.commit),
    }),
  );

  return (
    <DropdownMenu
      ariaLabel="Select document version"
      align="end"
      triggerClassName="flex h-[26px] items-center gap-[8px] rounded-button border border-line-primary bg-white px-[10px] font-mono text-[11.5px] text-ink-label transition-colors hover:bg-surface-warm-panel"
      menuClassName="tscroll max-h-[320px] overflow-auto"
      items={items}
    >
      {versionLabel(versions, selected)}
      <ChevronDown className="size-[10px] text-ink-faintest" aria-hidden />
    </DropdownMenu>
  );
}
