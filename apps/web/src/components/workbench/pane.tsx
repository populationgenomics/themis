"use client";

import { MoreHorizontal, X } from "lucide-react";
import { useRef, useState } from "react";
import {
  ContextMenu,
  type ContextMenuItem,
} from "@/components/ui/context-menu";
import { DropdownMenu, type MenuItem } from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import {
  type ContentKind,
  REGISTRY,
  type RenderContext,
} from "./content-kinds";
import type { Citation } from "./markdown";
import {
  dropIntent,
  encodeTabDrag,
  insertionIndex,
  parseTabDrag,
  resolveDrop,
  TAB_DND_TYPE,
} from "./tab-dnd";
import { Notice } from "./tab-views";
import type { WorkspaceModelController } from "./use-workspace-model";
import { useWorkspaceData } from "./workspace-context";
import {
  labelKey,
  type Pane as PaneModel,
  type PaneSide,
  type Tab,
  type Win,
} from "./workspace-model";
import type { CrossWindowDrag, WindowActions } from "./workspace-sync";

// A pane: a tab strip + a content area + a header, holding any document content kind (working
// document, paper, supplementary). Content-agnostic — icon, label, body, and header accessory come
// from the content-kind registry keyed on each tab's `kind`. The header collapses its actions (label
// mode, split, move-to-pane) into a single `⋯` overflow menu. A pane knows its window and pane ids so
// a citation clicked inside it reveals beside itself (`{kind:'document', winId, paneId}`). Two of
// these sit in a split `TabArea`; the strip docks to the pane's outer edge (side 'a' left, 'b' right)
// so the two strips frame the tab area.

const kindOf = (tab: Tab): ContentKind<unknown, unknown> => REGISTRY[tab.kind];
const labelOf = (tab: Tab): string => kindOf(tab).label(tab.payload);

/** The window's live tab-drag, shared across its strips: `active` is true while any tab of this window
 *  is being dragged (either pane), gating the positional drop marker. Cleared on `dragend`, which also
 *  retires any stale marker. A cross-window OS drag started in another window leaves this false here. */
export interface StripDrag {
  active: boolean;
  begin: () => void;
  end: () => void;
}

export function Pane({
  win,
  pane,
  side,
  controller,
  windowActions,
  crossWindowDrag,
  onCitation,
  drag,
}: {
  win: Win;
  pane: PaneModel;
  /** The pane's stable side within its window; the strip label mode keys on it, never the pane id. */
  side: PaneSide;
  controller: WorkspaceModelController;
  /** Move-to-window menu actions (open a new child, move to an existing window). */
  windowActions: WindowActions;
  /** Cross-window drag: announce this pane's drags, resolve a drag from another window at drop. */
  crossWindowDrag: CrossWindowDrag;
  /** Reveal a citation, tagged with this pane's ids so the reducer places the paper beside it. */
  onCitation: (winId: string, paneId: string, citation: Citation) => void;
  /** The window's shared tab-drag state (which pane a drag started in). */
  drag: StripDrag;
}): React.ReactElement {
  const data = useWorkspaceData();
  const activeTab = pane.tabs.find((t) => t.id === pane.activeTabId) ?? null;
  const ctx: RenderContext | null = activeTab
    ? {
        events: data.events,
        workingDocument: data.workingDocument,
        highlight: controller.state.highlights[activeTab.id],
        onCitation: (citation) => onCitation(win.id, pane.id, citation),
        patch: (payload) => controller.patchTab(activeTab.id, payload),
      }
    : null;

  const strip = (
    <TabStrip
      win={win}
      pane={pane}
      side={side}
      controller={controller}
      windowActions={windowActions}
      crossWindowDrag={crossWindowDrag}
      drag={drag}
    />
  );
  return (
    // Activate on click (bubble), not mousedown-capture: a mousedown-capture handler re-renders the pane
    // between mousedown and click and swallows a citation click inside the content. The click bubbles up
    // after the citation's own handler has run, so the reveal is untouched. Pointer convenience only —
    // the keyboard path is focusing a tab, which activates its pane (`activateTab`).
    // biome-ignore lint/a11y/noStaticElementInteractions: pane-focus affordance; keyboard activates via a tab
    // biome-ignore lint/a11y/useKeyWithClickEvents: keyboard activates the pane by focusing a tab, not this container
    <div
      onClick={() => controller.activatePane(win.id, pane.id)}
      className="flex h-full w-full flex-col bg-surface-doc-pane"
    >
      <PaneHeader
        win={win}
        pane={pane}
        side={side}
        controller={controller}
        windowActions={windowActions}
        activeTab={activeTab}
        ctx={ctx}
      />
      <div className="flex min-h-0 flex-1">
        {side === "a" && strip}
        {/* Keyed on the tab id: two tabs of the same kind render the same component type here, so
            without a key React reconciles them as one instance and its state (page count, located
            region, width) bleeds across the switch. */}
        <div
          key={activeTab?.id ?? "empty"}
          className="flex min-w-0 flex-1 flex-col"
        >
          {activeTab && ctx ? (
            kindOf(activeTab).render(activeTab.payload, ctx)
          ) : (
            <Notice text="This pane is empty." />
          )}
        </div>
        {side === "b" && strip}
      </div>
    </div>
  );
}

/** Move-to-window items for a tab: a new window (main only — a mirror can't open one, see
 *  `WindowActions.canOpenWindow`) plus each existing window named by its active/pinned tab (design
 *  §Accessibility — windows have no user title). Shared by the tab context menu and the pane overflow
 *  menu; the shapes coincide. */
function moveToWindowItems(
  tab: Tab,
  win: Win,
  windowActions: WindowActions,
): { key: string; label: string; onSelect: () => void }[] {
  const items: { key: string; label: string; onSelect: () => void }[] = [];
  if (windowActions.canOpenWindow)
    items.push({
      key: "move-new-window",
      label: "Move to new window",
      onSelect: () => windowActions.moveToNewWindow(tab.id),
    });
  for (const dest of windowActions.destinations(win.id))
    items.push({
      key: `move-window-${dest.winId}`,
      label: `Move to ${dest.label}`,
      onSelect: () => windowActions.moveToWindow(tab.id, dest.winId),
    });
  return items;
}

/** Move-to-window is offered when the move leaves something behind in the source window. The main
 *  window always keeps its conversation region, so it always qualifies (even for the sole working
 *  document); a child window is a tab area only, so moving its one tab would leave it empty — pointless
 *  churn — and it qualifies only while it holds more than the tab being moved. */
function canMoveTabToWindow(win: Win, mainId: string): boolean {
  if (win.id === mainId) return true;
  return win.panes.reduce((n, p) => n + p.tabs.length, 0) > 1;
}

/** Moving a whole pane out leaves something behind when the window is main (the conversation) or the
 *  window has a second pane. */
function canMovePaneToWindow(win: Win, mainId: string): boolean {
  return win.id === mainId || win.panes.length > 1;
}

/** Move-to-window items for a whole PANE — all its tabs into one new window, or each into an existing
 *  one. The pane overflow menu reparents the pane; the per-tab context menu moves a single tab. */
function paneMoveItems(
  pane: PaneModel,
  win: Win,
  windowActions: WindowActions,
): MenuItem[] {
  const tabIds = pane.tabs.map((t) => t.id);
  const items: MenuItem[] = [];
  if (windowActions.canOpenWindow)
    items.push({
      key: "move-pane-new-window",
      label: "Move to new window",
      onSelect: () => windowActions.moveTabsToNewWindow(tabIds),
    });
  for (const dest of windowActions.destinations(win.id))
    items.push({
      key: `move-pane-window-${dest.winId}`,
      label: `Move to ${dest.label}`,
      onSelect: () => {
        for (const id of tabIds) windowActions.moveToWindow(id, dest.winId);
      },
    });
  return items;
}

/** The header actions, collapsed into one overflow menu: strip label mode, and — depending on the
 *  split state — split the active tab into a new pane or move it to the other pane, plus move-to-window. */
function paneMenuItems(
  win: Win,
  pane: PaneModel,
  side: PaneSide,
  controller: WorkspaceModelController,
  windowActions: WindowActions,
  activeTab: Tab | null,
): MenuItem[] {
  const labels =
    controller.state.labels[labelKey(win.id, side)] ?? side === "b";
  const items: MenuItem[] = [
    {
      key: "labels",
      label: labels ? "Show tab icons only" : "Show tab titles",
      onSelect: () => controller.setLabel(win.id, side, !labels),
    },
  ];
  // Reopen the last closed tab — the accessible path to `reopenClosed` (a keyboard shortcut would clash
  // with the browser's own Cmd/Ctrl+Shift+T). Absent when the stack is empty, and in a mirror window,
  // whose snapshot carries no `closedStack` (a main-only action).
  if (controller.state.closedStack.length > 0)
    items.push({
      key: "reopen",
      label: "Reopen closed tab",
      onSelect: () => controller.reopenClosed(),
    });
  if (!activeTab) return items;
  if (win.panes.length === 2) {
    const other = win.panes.find((p) => p.id !== pane.id);
    if (other)
      items.push({
        key: "move",
        label: "Move to other pane",
        onSelect: () => controller.moveTabToPane(activeTab.id, other.id),
      });
    items.push({
      key: "swap",
      label: "Swap panes",
      onSelect: () => controller.swapPanes(win.id),
    });
  } else if (pane.tabs.length > 1) {
    items.push({
      key: "split",
      label: "Split pane",
      onSelect: () => controller.split(activeTab.id),
    });
  }
  if (canMovePaneToWindow(win, controller.state.mainId))
    items.push(...paneMoveItems(pane, win, windowActions));
  return items;
}

function PaneHeader({
  win,
  pane,
  side,
  controller,
  windowActions,
  activeTab,
  ctx,
}: {
  win: Win;
  pane: PaneModel;
  side: PaneSide;
  controller: WorkspaceModelController;
  windowActions: WindowActions;
  activeTab: Tab | null;
  ctx: RenderContext | null;
}): React.ReactElement {
  return (
    <div className="flex h-[42px] shrink-0 items-center justify-between gap-[12px] border-b border-line-soft py-[6px] pr-[10px] pl-[20px]">
      <span className="truncate text-[13.5px] font-semibold text-ink-primary">
        {activeTab ? labelOf(activeTab) : "Empty pane"}
      </span>
      <div className="flex shrink-0 items-center gap-[8px]">
        {activeTab &&
          ctx &&
          kindOf(activeTab).headerAccessory?.(activeTab.payload, ctx)}
        <DropdownMenu
          items={paneMenuItems(
            win,
            pane,
            side,
            controller,
            windowActions,
            activeTab,
          )}
          ariaLabel="Pane actions"
          align="end"
          triggerClassName="flex size-[28px] items-center justify-center rounded-[6px] text-ink-faintest hover:bg-surface-inset hover:text-ink-primary"
        >
          <MoreHorizontal className="size-[16px]" aria-hidden />
        </DropdownMenu>
      </div>
    </div>
  );
}

/** The dragged tab's live location within this window, or null when its id belongs to no tab here —
 *  the validate-at-drop check that ignores a foreign or cross-window payload. */
function locateInWindow(
  win: Win,
  tabId: string,
): { paneId: string; index: number } | null {
  for (const p of win.panes) {
    const index = p.tabs.findIndex((t) => t.id === tabId);
    if (index !== -1) return { paneId: p.id, index };
  }
  return null;
}

/** The insertion slot for a drop at `clientY`, from the measured midpoints of this strip's tabs. */
function insertAtFor(strip: HTMLElement, clientY: number): number {
  const els = strip.querySelectorAll<HTMLElement>("[data-tab-id]");
  const midpoints = Array.from(els, (el) => {
    const r = el.getBoundingClientRect();
    return r.top + r.height / 2;
  });
  return insertionIndex(midpoints, clientY);
}

function TabStrip({
  win,
  pane,
  side,
  controller,
  windowActions,
  crossWindowDrag,
  drag,
}: {
  win: Win;
  pane: PaneModel;
  side: PaneSide;
  controller: WorkspaceModelController;
  windowActions: WindowActions;
  crossWindowDrag: CrossWindowDrag;
  drag: StripDrag;
}): React.ReactElement {
  const labels =
    controller.state.labels[labelKey(win.id, side)] ?? side === "b";
  const other =
    win.panes.length === 2 ? win.panes.find((p) => p.id !== pane.id) : null;
  const stripRef = useRef<HTMLDivElement>(null);
  // The insertion slot to mark while a tab is dragged over this strip; null when none is.
  const [dropAt, setDropAt] = useState<number | null>(null);
  // A within-window drag (same or other pane) drops at the pointer's slot, so show the positional line
  // at `dropAt` for both. It gates on an active drag, so a marker left by a cancelled drag clears when
  // `drag.active` resets on `dragend`. A cross-window OS drag is not active here, so no line shows
  // during it — but the drop still lands at the pointer's slot (see `onDrop`).
  const draggingTab = drag.active;

  const onDrop = (e: React.DragEvent): void => {
    setDropAt(null);
    e.preventDefault();
    if (!stripRef.current) return;
    // Discriminate at drop, never dragover (values unreadable then). The within-window path parses the
    // custom type (stripped on a cross-window OS drag, so null there); the cross-window path reads the
    // opaque session id from `text/plain` and correlates it to a live remote drag.
    const insertAt = insertAtFor(stripRef.current, e.clientY);
    const payload = parseTabDrag(e.dataTransfer.getData(TAB_DND_TYPE));
    const within = payload
      ? dropIntent(
          payload.tabId,
          pane.id,
          insertAt,
          locateInWindow(win, payload.tabId),
        )
      : { type: "none" as const };
    const session = crossWindowDrag.resolve(
      e.dataTransfer.getData("text/plain") || null,
    );
    const resolution = resolveDrop({
      within,
      session,
      destWinId: win.id,
      destPaneId: pane.id,
    });
    if (resolution.type === "reorder")
      controller.reorderTab(resolution.tabId, resolution.toIndex);
    else if (resolution.type === "move")
      controller.moveTabToPane(
        resolution.tabId,
        resolution.toPaneId,
        resolution.toIndex,
      );
    else if (resolution.type === "cross-window-move")
      // Honour the pointer's slot here too: the cross-window resolution carries no index (it correlates
      // a remote session, not a local drag), but the OS drop fires on this strip with a real clientY,
      // so `insertAt` places the arriving tab where it was dropped rather than always at the end.
      controller.moveTabToPane(resolution.tabId, resolution.toPaneId, insertAt);
  };

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: a drop target; drag has no keyboard equivalent — the per-tab context menu is the keyboard/touch path for these moves.
    <div
      ref={stripRef}
      onDragOver={(e) => {
        // Accept any drag so `drop` fires (payload values are unreadable until then); the shape is
        // validated at drop. The insertion marker shows only for our own drags — the one type readable
        // during `dragover`.
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        if (!e.dataTransfer.types.includes(TAB_DND_TYPE)) return;
        if (stripRef.current)
          setDropAt(insertAtFor(stripRef.current, e.clientY));
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null))
          setDropAt(null);
      }}
      onDrop={onDrop}
      className={cn(
        "flex shrink-0 flex-col gap-[4px] bg-surface-inset py-[8px]",
        side === "b"
          ? "border-l border-line-soft"
          : "border-r border-line-soft",
        labels ? "w-[196px]" : "w-[46px] items-center",
      )}
    >
      <div
        className={cn(
          "flex flex-col gap-[3px]",
          labels ? "w-full px-[6px]" : "items-center",
        )}
      >
        {pane.tabs.flatMap((tab, i) => {
          const nodes: React.ReactNode[] = [];
          if (draggingTab && dropAt === i)
            nodes.push(<DropLine key={`drop-${tab.id}`} labels={labels} />);
          nodes.push(
            <TabButton
              key={tab.id}
              tab={tab}
              winId={win.id}
              paneId={pane.id}
              active={tab.id === pane.activeTabId}
              labels={labels}
              crossWindowDrag={crossWindowDrag}
              onSelect={() => controller.activateTab(tab.id)}
              onClose={() => controller.closeTab(tab.id)}
              onDragBegin={drag.begin}
              onDragEnd={drag.end}
              items={tabMenuItems(
                tab,
                win,
                pane,
                other,
                controller,
                windowActions,
              )}
            />,
          );
          return nodes;
        })}
        {draggingTab && dropAt === pane.tabs.length && (
          <DropLine key="drop-end" labels={labels} />
        )}
      </div>
    </div>
  );
}

/** The insertion marker painted between tabs while a tab is dragged over the strip. */
function DropLine({ labels }: { labels: boolean }): React.ReactElement {
  return (
    <div
      aria-hidden
      className={cn(
        "h-[2px] rounded-full bg-[var(--color-primary)]",
        labels ? "w-full" : "w-[24px]",
      )}
    />
  );
}

/** A tab's context-menu actions — the keyboard/touch/pointer path for split, move, and close. Split
 *  offers to separate the tab into a new pane only from a single-pane window holding more than it;
 *  move-to-other-pane replaces it once the window is split; move-to-window is always present (a new
 *  window, plus each existing one); close is present for any closable tab. */
function tabMenuItems(
  tab: Tab,
  win: Win,
  pane: PaneModel,
  other: PaneModel | null | undefined,
  controller: WorkspaceModelController,
  windowActions: WindowActions,
): ContextMenuItem[] {
  const items: ContextMenuItem[] = [];
  if (other)
    items.push({
      key: "move",
      label: "Move to other pane",
      onSelect: () => controller.moveTabToPane(tab.id, other.id),
    });
  else if (pane.tabs.length > 1)
    items.push({
      key: "split",
      label: "Split pane",
      onSelect: () => controller.split(tab.id),
    });
  if (canMoveTabToWindow(win, controller.state.mainId))
    items.push(...moveToWindowItems(tab, win, windowActions));
  if (!tab.pinned)
    items.push({
      key: "close",
      label: "Close",
      onSelect: () => controller.closeTab(tab.id),
    });
  return items;
}

function TabButton({
  tab,
  winId,
  paneId,
  active,
  labels,
  crossWindowDrag,
  onSelect,
  onClose,
  onDragBegin,
  onDragEnd,
  items,
}: {
  tab: Tab;
  /** The tab's window, broadcast with the drag session so a drop can tell same- from cross-window. */
  winId: string;
  /** The tab's source pane, written to the drag payload so the drop can resolve within the window. */
  paneId: string;
  active: boolean;
  labels: boolean;
  /** Announce this drag over the workspace channel so another window can accept a cross-window drop. */
  crossWindowDrag: CrossWindowDrag;
  onSelect: () => void;
  onClose: () => void;
  /** Record/clear this drag in the window's shared drag state (drives the drop marker). */
  onDragBegin: () => void;
  onDragEnd: () => void;
  /** The tab's context-menu actions; empty when the tab affords none (a lone pinned tab). */
  items: ContextMenuItem[];
}): React.ReactElement {
  const closable = !tab.pinned;
  const label = labelOf(tab);
  const icon = kindOf(tab).icon;
  const activeClass = active
    ? "bg-surface-doc-pane text-ink-primary shadow-[inset_0_0_0_1px_var(--color-line-soft)]"
    : "text-ink-faint hover:bg-surface-doc-pane hover:text-ink-primary";
  const hasMenu = items.length > 0;

  // The opaque session id for the in-flight drag: minted at dragstart, torn down at dragend. It carries
  // no structured data, so an external drop target that captures `text/plain` gets only a meaningless
  // UUID.
  const sessionRef = useRef<string | null>(null);

  const onDragStart = (e: React.DragEvent): void => {
    const sessionId = crypto.randomUUID();
    sessionRef.current = sessionId;
    e.dataTransfer.setData(
      TAB_DND_TYPE,
      encodeTabDrag({ tabId: tab.id, paneId }),
    );
    e.dataTransfer.setData("text/plain", sessionId);
    e.dataTransfer.effectAllowed = "move";
    crossWindowDrag.begin(sessionId, tab.id, winId);
    onDragBegin();
  };

  const endDrag = (): void => {
    if (sessionRef.current) {
      crossWindowDrag.end(sessionRef.current);
      sessionRef.current = null;
    }
    onDragEnd();
  };

  const body = labels ? (
    <div className="group relative" data-tab-id={tab.id}>
      <button
        type="button"
        draggable
        onDragStart={onDragStart}
        onDragEnd={endDrag}
        onClick={onSelect}
        title={label}
        aria-current={active}
        aria-haspopup={hasMenu ? "menu" : undefined}
        className={cn(
          "flex w-full items-center gap-[8px] rounded-[6px] py-[6px] pr-[24px] pl-[9px] text-left",
          activeClass,
        )}
      >
        {icon}
        <span className="min-w-0 flex-1 truncate text-[12.5px]">{label}</span>
      </button>
      {closable && (
        <button
          type="button"
          onClick={onClose}
          aria-label={`Close ${label}`}
          className="absolute top-1/2 right-[5px] hidden size-[16px] -translate-y-1/2 items-center justify-center rounded-[4px] text-ink-faintest group-hover:flex hover:bg-surface-inset hover:text-ink-primary"
        >
          <X className="size-[11px]" strokeWidth={2.4} aria-hidden />
        </button>
      )}
    </div>
  ) : (
    <div className="group relative" data-tab-id={tab.id}>
      <button
        type="button"
        draggable
        onDragStart={onDragStart}
        onDragEnd={endDrag}
        onClick={onSelect}
        title={label}
        aria-label={label}
        aria-current={active}
        aria-haspopup={hasMenu ? "menu" : undefined}
        className={cn(
          "flex size-[30px] items-center justify-center rounded-[7px]",
          activeClass,
        )}
      >
        {icon}
      </button>
      {closable && (
        <button
          type="button"
          onClick={onClose}
          aria-label={`Close ${label}`}
          className="absolute -top-[2px] -right-[2px] hidden size-[15px] items-center justify-center rounded-full border border-line-soft bg-white text-ink-faint group-hover:flex hover:text-ink-primary"
        >
          <X className="size-[10px]" strokeWidth={2.6} aria-hidden />
        </button>
      )}
    </div>
  );

  if (!hasMenu) return body;
  return (
    <ContextMenu ariaLabel={`Actions for ${label}`} items={items}>
      {body}
    </ContextMenu>
  );
}
