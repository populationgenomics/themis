"use client";

import { MoreHorizontal } from "lucide-react";
import { useEffect, useState } from "react";
import { DropdownMenu, type MenuItem } from "@/components/ui/dropdown-menu";
import { Tooltip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

// The ids an Analysis is looked up by outside the workbench — its own, and that of the platform
// session its agent runs in — each copied to the clipboard from a menu beside the Analysis's title.

/** How long a copy stays confirmed beside the menu. A failure stays until the next copy. */
const CONFIRMATION_MS = 2000;

/** How the last copy ended. `name` reads mid-sentence: "the session ID". */
type CopyOutcome =
  | { kind: "copied"; name: string }
  | { kind: "failed"; name: string; message: string };

export function AnalysisIdentifiersMenu({
  analysisId,
  sessionId,
}: {
  analysisId: string;
  /** The platform session the agent runs in; null for a run the platform holds no session of. */
  sessionId: string | null;
}): React.ReactElement {
  const [outcome, setOutcome] = useState<CopyOutcome | null>(null);

  useEffect(() => {
    if (outcome?.kind !== "copied") return;
    const timer = setTimeout(() => setOutcome(null), CONFIRMATION_MS);
    return () => clearTimeout(timer);
  }, [outcome]);

  async function copy(name: string, value: string): Promise<void> {
    // Emptied first, so a copy that ends as the last one did still changes what the status holds.
    setOutcome(null);
    try {
      await navigator.clipboard.writeText(value);
      setOutcome({ kind: "copied", name });
    } catch (error) {
      console.error(`copying the ${name} failed`, error);
      setOutcome({
        kind: "failed",
        name,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }

  const identifiers = [{ name: "Analysis ID", value: analysisId }];
  if (sessionId !== null) {
    identifiers.push({ name: "session ID", value: sessionId });
  }
  const items: MenuItem[] = identifiers.map(({ name, value }) => ({
    key: name,
    label: (
      <span className="flex flex-col gap-[1px]">
        <span>Copy {name}</span>
        <span className="font-mono text-[11.5px] text-ink-faintest">
          {value}
        </span>
      </span>
    ),
    onSelect: () => void copy(name, value),
  }));

  return (
    <div className="flex shrink-0 items-center gap-[8px]">
      <DropdownMenu
        ariaLabel="Analysis identifiers"
        triggerClassName="flex size-[28px] items-center justify-center rounded-field text-ink-faint hover:bg-surface-idle hover:text-ink-primary"
        items={items}
      >
        <MoreHorizontal className="size-[16px]" aria-hidden />
      </DropdownMenu>
      <Tooltip content={outcome?.kind === "failed" ? outcome.message : ""}>
        {/* Mounted while empty: a live region announces a change to what it holds, not its arrival. */}
        <span
          role="status"
          className={cn(
            "whitespace-nowrap text-[11.5px]",
            outcome?.kind === "failed" ? "text-destructive" : "text-ink-faint",
          )}
        >
          {outcome === null
            ? ""
            : outcome.kind === "copied"
              ? `Copied the ${outcome.name}`
              : `Couldn't copy the ${outcome.name}`}
        </span>
      </Tooltip>
    </div>
  );
}
