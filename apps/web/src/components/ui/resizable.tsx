"use client";

import type { Orientation } from "react-resizable-panels";
import { Separator } from "react-resizable-panels";
import { cn } from "@/lib/utils";

export type {
  GroupImperativeHandle,
  Layout,
  LayoutChangedMeta,
  Orientation,
} from "react-resizable-panels";
export { Group as PaneGroup, Panel, useGroupRef } from "react-resizable-panels";

// The draggable divider between two panes. The Separator itself is a wide, transparent grab target
// (so a pointer press lands on it rather than the adjacent pane's content) with a resize cursor; the
// 1px visible line is a centred child that lights up on hover and while dragging.
export function PaneHandle({
  orientation,
  className,
  ariaLabel,
}: {
  orientation: Orientation;
  className?: string;
  /** Names the divider for assistive tech; the Separator is arrow-key resizable. */
  ariaLabel?: string;
}) {
  // A "horizontal" group lays its panels side by side, so the divider is a vertical line.
  const sideBySide = orientation === "horizontal";
  return (
    <Separator
      aria-label={ariaLabel}
      className={cn(
        "group relative z-10 flex shrink-0 items-stretch justify-center bg-transparent",
        sideBySide
          ? "w-[11px] cursor-col-resize"
          : "h-[11px] flex-col cursor-row-resize",
        className,
      )}
    >
      <div
        className={cn(
          "bg-line-primary transition-colors group-hover:bg-primary group-data-[separator=active]:bg-primary",
          sideBySide ? "w-px" : "h-px",
        )}
      />
    </Separator>
  );
}
