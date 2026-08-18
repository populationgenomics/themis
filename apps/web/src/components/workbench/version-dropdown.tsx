"use client";

import { ChevronDown } from "lucide-react";
import { DropdownMenu, type MenuItem } from "@/components/ui/dropdown-menu";

// The working-document version picker: one row per saved version, latest first.

/** The picker's rows, latest first. `version` is what selecting the row means: null for the latest
 *  (follow the current document), the version itself for an older one (pin it). */
export function versionMenuItems(
  latest: number,
  selected: number,
): { key: string; label: string; selected: boolean; version: number | null }[] {
  return Array.from({ length: latest }, (_, i) => latest - i).map((v) => ({
    key: `v${v}`,
    label: `v${v}`,
    selected: v === selected,
    version: v === latest ? null : v,
  }));
}

export function VersionDropdown({
  latest,
  selected,
  onSelect,
}: {
  latest: number;
  /** The version currently shown (defaults to `latest`). */
  selected: number;
  /** Receives the selected row's meaning — see `versionMenuItems`. */
  onSelect: (version: number | null) => void;
}): React.ReactElement {
  const items: MenuItem[] = versionMenuItems(latest, selected).map((item) => ({
    key: item.key,
    label: item.label,
    selected: item.selected,
    onSelect: () => onSelect(item.version),
  }));

  return (
    <DropdownMenu
      ariaLabel="Select document version"
      align="end"
      triggerClassName="flex h-[26px] items-center gap-[8px] rounded-button border border-line-primary bg-white px-[10px] font-mono text-[11.5px] text-ink-label transition-colors hover:bg-surface-warm-panel"
      menuClassName="tscroll max-h-[320px] overflow-auto"
      items={items}
    >
      v{selected}
      <ChevronDown className="size-[10px] text-ink-faintest" aria-hidden />
    </DropdownMenu>
  );
}
