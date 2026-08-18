import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { TabArea } from "./tab-area";
import type { WorkspaceModelController } from "./use-workspace-model";
import { WorkspaceDataProvider } from "./workspace-context";
import {
  INITIAL_WORKSPACE_STATE,
  type Tab,
  type Win,
  type WorkspaceState,
} from "./workspace-model";
import type { CrossWindowDrag, WindowActions } from "./workspace-sync";

// Clicks, drag-resize and menu actions are DOM-bound (screenshots + F6c/d); here TabArea's structural
// contract is tested: a one-pane window renders one pane and no divider, a two-pane window renders two
// panes and one divider. Supplementary tabs render without a query, so a plain static render suffices.

function suppTab(id: string): Tab {
  return {
    id: `supp:${id}`,
    kind: "supplementary",
    pinned: false,
    payload: { docId: id, name: `${id}.csv`, mediaType: "text/csv" },
  };
}

function stateWith(win: Win): WorkspaceState {
  return { ...INITIAL_WORKSPACE_STATE, windows: [win] };
}

const noop = () => {};
function controllerFor(state: WorkspaceState): WorkspaceModelController {
  return {
    state,
    activateTab: noop,
    activatePane: noop,
    setSplitRatio: noop,
    split: noop,
    reorderTab: noop,
    moveTabToPane: noop,
    swapPanes: noop,
    moveTabToWindow: noop,
    moveTabToNewWindow: noop,
    closeWindow: noop,
    closeTab: noop,
    openTab: async () => {},
    setConversationEdge: noop,
    setLabel: noop,
    setHighlight: noop,
    patchTab: noop,
    reopenClosed: noop,
    consolidate: noop,
    readOuterRatio: async () => null,
    writeOuterRatio: noop,
  };
}

const windowActions: WindowActions = {
  canOpenWindow: true,
  destinations: () => [],
  moveToWindow: noop,
  moveToNewWindow: noop,
  moveTabsToNewWindow: noop,
};

const crossWindowDrag: CrossWindowDrag = {
  begin: noop,
  end: noop,
  resolve: () => null,
};

function renderArea(win: Win): string {
  return renderToStaticMarkup(
    <WorkspaceDataProvider
      value={{
        events: [],
        workingDocument: null,
        documentSignal: null,
        documentError: false,
      }}
    >
      <TabArea
        win={win}
        controller={controllerFor(stateWith(win))}
        windowActions={windowActions}
        crossWindowDrag={crossWindowDrag}
        onCitation={noop}
      />
    </WorkspaceDataProvider>,
  );
}

function count(haystack: string, needle: string): number {
  return haystack.split(needle).length - 1;
}

describe("TabArea", () => {
  test("a one-pane window renders one pane and no divider", () => {
    const win: Win = {
      id: "main",
      panes: [{ id: "pane-0", tabs: [suppTab("x")], activeTabId: "supp:x" }],
      splitRatio: 0.5,
      activePaneId: "pane-0",
    };
    const html = renderArea(win);
    expect(count(html, 'aria-label="Pane actions"')).toBe(1);
    expect(html).not.toContain('role="separator"');
  });

  test("a two-pane window renders two panes and one divider", () => {
    const win: Win = {
      id: "main",
      panes: [
        { id: "pane-0", tabs: [suppTab("x")], activeTabId: "supp:x" },
        { id: "pane-1", tabs: [suppTab("y")], activeTabId: "supp:y" },
      ],
      splitRatio: 0.5,
      activePaneId: "pane-0",
    };
    const html = renderArea(win);
    expect(count(html, 'aria-label="Pane actions"')).toBe(2);
    expect(count(html, 'role="separator"')).toBe(1);
  });
});
