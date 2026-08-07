"use client";

import { useState } from "react";
import { PaneGroup, PaneHandle, Panel } from "@/components/ui/resizable";
import type { Citation } from "./markdown";
import { Pane, type StripDrag } from "./pane";
import type { WorkspaceModelController } from "./use-workspace-model";
import type { Win } from "./workspace-model";
import type { CrossWindowDrag, WindowActions } from "./workspace-sync";

// A window's tab area: one `Pane`, or two side-by-side in a nested left/right `PaneGroup` with a
// draggable divider. At most two panes (design non-goal: no recursive grids). The divider ratio is the
// window's `splitRatio`; the two panes take stable sides 'a' (left) and 'b' (right).

export function TabArea({
  win,
  controller,
  windowActions,
  crossWindowDrag,
  onCitation,
}: {
  win: Win;
  controller: WorkspaceModelController;
  /** Move-to-window menu actions (open a new child, move to an existing window). */
  windowActions: WindowActions;
  /** Cross-window drag: announce this window's drags, resolve a drag from another window at drop. */
  crossWindowDrag: CrossWindowDrag;
  onCitation: (winId: string, paneId: string, citation: Citation) => void;
}): React.ReactElement {
  const [dragActive, setDragActive] = useState(false);
  const drag: StripDrag = {
    active: dragActive,
    begin: () => setDragActive(true),
    end: () => setDragActive(false),
  };
  if (win.panes.length === 1) {
    return (
      <div className="flex min-h-0 min-w-0 flex-1">
        <Pane
          win={win}
          pane={win.panes[0]}
          side="a"
          controller={controller}
          windowActions={windowActions}
          crossWindowDrag={crossWindowDrag}
          onCitation={onCitation}
          drag={drag}
        />
      </div>
    );
  }
  const [a, b] = win.panes;
  return (
    <PaneGroup
      id={`${win.id}-tabarea`}
      orientation="horizontal"
      defaultLayout={{
        [a.id]: win.splitRatio * 100,
        [b.id]: (1 - win.splitRatio) * 100,
      }}
      onLayoutChanged={(layout, meta) => {
        // Write back only a user drag/keyboard resize (not a programmatic relayout): pane 'a's fraction
        // is the window's splitRatio — persisted for main, broadcast to mirrors.
        if (!meta.isUserInteraction) return;
        const aPct = layout[a.id];
        if (typeof aPct === "number")
          controller.setSplitRatio(win.id, aPct / 100);
      }}
      className="min-h-0 min-w-0 flex-1"
    >
      <Panel id={a.id} minSize="20%" className="min-h-0 min-w-0">
        <Pane
          win={win}
          pane={a}
          side="a"
          controller={controller}
          windowActions={windowActions}
          crossWindowDrag={crossWindowDrag}
          onCitation={onCitation}
          drag={drag}
        />
      </Panel>
      <PaneHandle
        orientation="horizontal"
        ariaLabel="Resize the two document panes"
      />
      <Panel id={b.id} minSize="20%" className="min-h-0 min-w-0">
        <Pane
          win={win}
          pane={b}
          side="b"
          controller={controller}
          windowActions={windowActions}
          crossWindowDrag={crossWindowDrag}
          onCitation={onCitation}
          drag={drag}
        />
      </Panel>
    </PaneGroup>
  );
}
