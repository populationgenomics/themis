"use client";

import { PanelBottom, PanelLeft, PanelRight, PanelTop } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Edge } from "./workspace-model";

const EDGES: ReadonlyArray<{
  value: Edge;
  label: string;
  Icon: typeof PanelLeft;
}> = [
  { value: "left", label: "Dock conversation left", Icon: PanelLeft },
  { value: "right", label: "Dock conversation right", Icon: PanelRight },
  { value: "top", label: "Dock conversation top", Icon: PanelTop },
  { value: "bottom", label: "Dock conversation bottom", Icon: PanelBottom },
];

// Which edge the conversation region docks to. Lives on the Analysis page alone: it configures the
// conversation region, and no other page has one.
export function ConversationDock({
  edge,
  onEdge,
}: {
  edge: Edge;
  onEdge: (edge: Edge) => void;
}) {
  return (
    <div className="flex items-center gap-[2px] rounded-field border border-line-primary p-[2px]">
      {EDGES.map(({ value, label, Icon }) => {
        const active = edge === value;
        return (
          <button
            key={value}
            type="button"
            aria-pressed={active}
            aria-label={label}
            title={label}
            onClick={() => onEdge(value)}
            className={cn(
              "flex size-[26px] items-center justify-center rounded-[5px]",
              active
                ? "bg-primary text-primary-foreground"
                : "text-ink-muted hover:bg-surface-idle hover:text-ink-primary",
            )}
          >
            <Icon className="size-[14px]" aria-hidden />
          </button>
        );
      })}
    </div>
  );
}
