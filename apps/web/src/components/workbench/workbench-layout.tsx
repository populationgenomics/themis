"use client";

import type { ReactNode, Ref } from "react";
import {
  type GroupImperativeHandle,
  type Layout,
  type LayoutChangedMeta,
  type Orientation,
  PaneGroup,
  PaneHandle,
  Panel,
} from "@/components/ui/resizable";
import type { Edge } from "./workspace-model";

// The outer split: the conversation region beside the tab area. Its direction flips with the dock edge
// (left/right → side by side, top/bottom → stacked), and the conversation sits on the correct side
// (left/top first, right/bottom second). The ratio is applied imperatively by the caller per
// orientation, so a flip never carries a width % into a height %.

export const CONVERSATION_PANEL_ID = "conversation";
export const TAB_AREA_PANEL_ID = "tab-area";

export function edgeToOrientation(edge: Edge): Orientation {
  return edge === "left" || edge === "right" ? "horizontal" : "vertical";
}

/** True when the conversation renders as the first panel (docked left or top). */
export function conversationFirst(edge: Edge): boolean {
  return edge === "left" || edge === "top";
}

export function ConversationSplit({
  edge,
  conversation,
  tabArea,
  groupRef,
  onLayoutChanged,
  defaultConversationRatio,
}: {
  edge: Edge;
  conversation: ReactNode;
  tabArea: ReactNode;
  groupRef: Ref<GroupImperativeHandle | null>;
  onLayoutChanged: (layout: Layout, meta: LayoutChangedMeta) => void;
  /** The conversation's fraction of the split at first paint (before the persisted ratio loads). */
  defaultConversationRatio: number;
}): React.ReactElement {
  const orientation = edgeToOrientation(edge);
  const first = conversationFirst(edge);
  // Keyed so an edge flip (which swaps their sibling order) reconciles by identity, not position:
  // otherwise React hands the conversation panel the tab area's props (id, defaultSize) rather than
  // moving the instance, mismatching the library's id-keyed layout state against the panel it draws.
  const conversationPanel = (
    <Panel
      key={CONVERSATION_PANEL_ID}
      id={CONVERSATION_PANEL_ID}
      minSize="20%"
      defaultSize={`${defaultConversationRatio * 100}%`}
      className="min-h-0 min-w-0"
    >
      {conversation}
    </Panel>
  );
  const tabAreaPanel = (
    <Panel
      key={TAB_AREA_PANEL_ID}
      id={TAB_AREA_PANEL_ID}
      minSize="20%"
      className="min-h-0 min-w-0"
    >
      {tabArea}
    </Panel>
  );
  return (
    <PaneGroup
      id="workbench-outer"
      orientation={orientation}
      groupRef={groupRef}
      onLayoutChanged={onLayoutChanged}
      className="min-h-0 flex-1"
    >
      {first ? conversationPanel : tabAreaPanel}
      <PaneHandle
        orientation={orientation}
        ariaLabel="Resize conversation and documents"
      />
      {first ? tabAreaPanel : conversationPanel}
    </PaneGroup>
  );
}
