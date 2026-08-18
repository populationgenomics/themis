import { describe, expect, test } from "bun:test";
import { Representation } from "@/models/literature";
import {
  computeTarget,
  INITIAL_WORKSPACE_STATE,
  labelKey,
  pinnedDocumentVersion,
  readDocumentPin,
  workspaceModelReducer as reduce,
  type Source,
  type Tab,
  WORKING_DOC_TAB_ID,
  type WorkspaceState,
} from "./workspace-model";

function paperTab(docId: string): Tab {
  return {
    id: `paper:${docId}`,
    kind: "paper",
    pinned: false,
    payload: {
      docId,
      title: `Paper ${docId}`,
      hasMarkdown: true,
      hasPdf: true,
      representation: Representation.MARKDOWN,
    },
  };
}

const CONVERSATION: Source = { kind: "conversation" };

function mainWin(state: WorkspaceState) {
  const win = state.windows.find((w) => w.id === state.mainId);
  if (!win) throw new Error("no main window");
  return win;
}

function paneIds(state: WorkspaceState, winId: string): string[][] {
  const win = state.windows.find((w) => w.id === winId);
  if (!win) throw new Error(`no window ${winId}`);
  return win.panes.map((p) => p.tabs.map((t) => t.id));
}

/** Reveal a paper from the conversation into main (case 1: working doc's pane, unsplit). */
function openInMain(state: WorkspaceState, tab: Tab): WorkspaceState {
  return reduce(state, { type: "openTab", src: CONVERSATION, tab });
}

describe("initial state", () => {
  test("one main window, one pane, working document the sole pinned tab", () => {
    const s = INITIAL_WORKSPACE_STATE;
    expect(s.windows).toHaveLength(1);
    expect(s.windows[0].id).toBe(s.mainId);
    expect(paneIds(s, s.mainId)).toEqual([[WORKING_DOC_TAB_ID]]);
    expect(mainWin(s).panes[0].tabs[0].pinned).toBe(true);
    expect(s.conversation.edge).toBe("left");
  });
});

describe("computeTarget — reveal placement", () => {
  test("document-sourced reveal targets the sibling pane (split when single)", () => {
    const src: Source = {
      kind: "document",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      paneId: mainWin(INITIAL_WORKSPACE_STATE).panes[0].id,
    };
    const target = computeTarget(INITIAL_WORKSPACE_STATE, src, "paper:x");
    expect(target.op).toBe("open");
    // A single-pane source signals a split: the sibling pane does not exist yet.
    expect(target.paneId).toBeNull();
  });

  test("document-sourced reveal splits the source window into two panes", () => {
    const src: Source = {
      kind: "document",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      paneId: mainWin(INITIAL_WORKSPACE_STATE).panes[0].id,
    };
    const next = reduce(INITIAL_WORKSPACE_STATE, {
      type: "openTab",
      src,
      tab: paperTab("x"),
    });
    // Source (working doc) stays in side 'a'; the paper lands in the new side 'b'.
    expect(paneIds(next, next.mainId)).toEqual([
      [WORKING_DOC_TAB_ID],
      ["paper:x"],
    ]);
    const second = mainWin(next).panes[1];
    if (!second) throw new Error("expected two panes");
    expect(mainWin(next).activePaneId).toBe(second.id);
  });

  test("conversation case 1: unsplit main → the working document's pane", () => {
    const next = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    // Tabbed into the working doc's pane, not split beside it.
    expect(paneIds(next, next.mainId)).toEqual([
      [WORKING_DOC_TAB_ID, "paper:x"],
    ]);
  });

  test("conversation case 1: a zero-tab second pane still counts as unsplit", () => {
    // Move the working doc to a child, leaving main a single zero-tab pane, then bring it back so main
    // is a genuine single pane holding only the working doc — the honest unsplit case is case 1 above;
    // here assert the reveal never treats a lone pane as split.
    const src: Source = {
      kind: "document",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      paneId: mainWin(INITIAL_WORKSPACE_STATE).panes[0].id,
    };
    const split = reduce(INITIAL_WORKSPACE_STATE, {
      type: "openTab",
      src,
      tab: paperTab("x"),
    });
    // Close the split-out paper: main collapses back to a single pane holding the working doc.
    const collapsed = reduce(split, { type: "closeTab", tabId: "paper:x" });
    expect(collapsed.windows[0].panes).toHaveLength(1);
    const revealed = openInMain(collapsed, paperTab("y"));
    expect(paneIds(revealed, revealed.mainId)).toEqual([
      [WORKING_DOC_TAB_ID, "paper:y"],
    ]);
  });

  test("conversation case 2: split main → the pane that is not the working document's", () => {
    const src: Source = {
      kind: "document",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      paneId: mainWin(INITIAL_WORKSPACE_STATE).panes[0].id,
    };
    const split = reduce(INITIAL_WORKSPACE_STATE, {
      type: "openTab",
      src,
      tab: paperTab("x"),
    });
    // Working doc in side 'a', paper:x in side 'b'. A conversation reveal lands in side 'b'.
    const revealed = openInMain(split, paperTab("y"));
    expect(paneIds(revealed, revealed.mainId)).toEqual([
      [WORKING_DOC_TAB_ID],
      ["paper:x", "paper:y"],
    ]);
  });

  test("conversation case 3: working doc in a child → main's active pane", () => {
    const inChild = reduce(INITIAL_WORKSPACE_STATE, {
      type: "moveTabToWindow",
      tabId: WORKING_DOC_TAB_ID,
      toWinId: null,
    });
    // Main is now a single zero-tab pane; the reveal lands there.
    expect(mainWin(inChild).panes[0].tabs).toHaveLength(0);
    const revealed = openInMain(inChild, paperTab("y"));
    expect(paneIds(revealed, revealed.mainId)).toEqual([["paper:y"]]);
  });
});

describe("computeTarget — already-open runs before placement", () => {
  test("a paper already open never yields op 'open'", () => {
    const opened = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const src: Source = {
      kind: "document",
      winId: opened.mainId,
      paneId: mainWin(opened).panes[0].id,
    };
    const target = computeTarget(opened, src, "paper:x");
    expect(target.op).not.toBe("open");
  });

  test("already open in the same window → move to the computed target", () => {
    // Working doc + paper:x share the single main pane; a document reveal of paper:x from that pane
    // moves it to the sibling (splitting), never duplicating.
    const opened = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const src: Source = {
      kind: "document",
      winId: opened.mainId,
      paneId: mainWin(opened).panes[0].id,
    };
    expect(computeTarget(opened, src, "paper:x").op).toBe("move");
    const moved = reduce(opened, { type: "openTab", src, tab: paperTab("x") });
    expect(paneIds(moved, moved.mainId)).toEqual([
      [WORKING_DOC_TAB_ID],
      ["paper:x"],
    ]);
  });

  test("re-revealing a paper already in the target pane activates it, no reorder", () => {
    // pane0 = [working doc, paper:x, paper:y]; a conversation reveal of paper:x targets this same pane.
    let s = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    s = openInMain(s, paperTab("y"));
    expect(paneIds(s, s.mainId)).toEqual([
      [WORKING_DOC_TAB_ID, "paper:x", "paper:y"],
    ]);
    const revealed = openInMain(s, paperTab("x"));
    // Order is preserved — paper:x is surfaced in place, not stripped and re-appended after paper:y.
    expect(paneIds(revealed, revealed.mainId)).toEqual([
      [WORKING_DOC_TAB_ID, "paper:x", "paper:y"],
    ]);
    expect(mainWin(revealed).panes[0].activeTabId).toBe("paper:x");
  });
});

describe("surfacing across windows", () => {
  test("a paper open in another window surfaces that window", () => {
    // Open a paper in main, then move it to a new child window.
    const opened = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const inChild = reduce(opened, {
      type: "moveTabToWindow",
      tabId: "paper:x",
      toWinId: null,
    });
    const childId = inChild.windows.find((w) => w.id !== inChild.mainId)?.id;
    expect(childId).toBeDefined();
    const src: Source = {
      kind: "document",
      winId: inChild.mainId,
      paneId: mainWin(inChild).panes[0].id,
    };
    const target = computeTarget(inChild, src, "paper:x");
    expect(target.op).toBe("surface");
    expect(target.winId).toBe(childId as string);
  });
});

describe("tabs: split, move, close", () => {
  test("a pinned tab never closes", () => {
    const next = reduce(INITIAL_WORKSPACE_STATE, {
      type: "closeTab",
      tabId: WORKING_DOC_TAB_ID,
    });
    expect(next).toBe(INITIAL_WORKSPACE_STATE);
  });

  test("closing the last closable tab collapses a two-pane area to one", () => {
    const src: Source = {
      kind: "document",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      paneId: mainWin(INITIAL_WORKSPACE_STATE).panes[0].id,
    };
    const split = reduce(INITIAL_WORKSPACE_STATE, {
      type: "openTab",
      src,
      tab: paperTab("x"),
    });
    expect(mainWin(split).panes).toHaveLength(2);
    const collapsed = reduce(split, { type: "closeTab", tabId: "paper:x" });
    expect(mainWin(collapsed).panes).toHaveLength(1);
    expect(paneIds(collapsed, collapsed.mainId)).toEqual([
      [WORKING_DOC_TAB_ID],
    ]);
  });

  test("a pane holding only a pinned tab does not collapse when its sibling's tab closes", () => {
    // Split: working doc in 'a', paper in 'b'. Closing the paper leaves 'a' (pinned) as the sole pane.
    const src: Source = {
      kind: "document",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      paneId: mainWin(INITIAL_WORKSPACE_STATE).panes[0].id,
    };
    const split = reduce(INITIAL_WORKSPACE_STATE, {
      type: "openTab",
      src,
      tab: paperTab("x"),
    });
    const collapsed = reduce(split, { type: "closeTab", tabId: "paper:x" });
    expect(paneIds(collapsed, collapsed.mainId)).toEqual([
      [WORKING_DOC_TAB_ID],
    ]);
    expect(mainWin(collapsed).panes[0].tabs[0].pinned).toBe(true);
  });

  test("closing the active tab refocuses a neighbour", () => {
    const a = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const b = openInMain(a, paperTab("y"));
    expect(mainWin(b).panes[0].activeTabId).toBe("paper:y");
    const closed = reduce(b, { type: "closeTab", tabId: "paper:y" });
    expect(mainWin(closed).panes[0].activeTabId).toBe("paper:x");
  });

  test("an emptied main stays a single zero-tab pane, never removed", () => {
    const inChild = reduce(INITIAL_WORKSPACE_STATE, {
      type: "moveTabToWindow",
      tabId: WORKING_DOC_TAB_ID,
      toWinId: null,
    });
    expect(inChild.windows.some((w) => w.id === inChild.mainId)).toBe(true);
    expect(mainWin(inChild).panes).toHaveLength(1);
    expect(mainWin(inChild).panes[0].tabs).toHaveLength(0);
  });

  test("moving a tab out of a child window closes the empty child", () => {
    const opened = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const inChild = reduce(opened, {
      type: "moveTabToWindow",
      tabId: "paper:x",
      toWinId: null,
    });
    expect(inChild.windows).toHaveLength(2);
    const back = reduce(inChild, {
      type: "moveTabToWindow",
      tabId: "paper:x",
      toWinId: inChild.mainId,
    });
    expect(back.windows).toHaveLength(1);
    expect(paneIds(back, back.mainId)).toEqual([
      [WORKING_DOC_TAB_ID, "paper:x"],
    ]);
  });

  test("a split action moves its tab into a new sibling pane", () => {
    const opened = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const split = reduce(opened, { type: "split", tabId: "paper:x" });
    expect(paneIds(split, split.mainId)).toEqual([
      [WORKING_DOC_TAB_ID],
      ["paper:x"],
    ]);
  });

  test("splitting a two-pane area is an idempotent no-op", () => {
    const src: Source = {
      kind: "document",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      paneId: mainWin(INITIAL_WORKSPACE_STATE).panes[0].id,
    };
    const split = reduce(INITIAL_WORKSPACE_STATE, {
      type: "openTab",
      src,
      tab: paperTab("x"),
    });
    const again = reduce(split, { type: "split", tabId: "paper:x" });
    expect(again).toBe(split);
  });
});

describe("reorderTab within a pane", () => {
  test("moves a tab to the target slot in its pane", () => {
    const a = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const b = openInMain(a, paperTab("y"));
    expect(paneIds(b, b.mainId)).toEqual([
      [WORKING_DOC_TAB_ID, "paper:x", "paper:y"],
    ]);
    const reordered = reduce(b, {
      type: "reorderTab",
      tabId: "paper:y",
      toIndex: 0,
    });
    expect(paneIds(reordered, reordered.mainId)).toEqual([
      ["paper:y", WORKING_DOC_TAB_ID, "paper:x"],
    ]);
  });

  test("a same-position reorder is an identity no-op", () => {
    const a = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const again = reduce(a, {
      type: "reorderTab",
      tabId: "paper:x",
      toIndex: 1,
    });
    expect(again).toBe(a);
  });

  test("a target index past the end clamps to the last slot", () => {
    const a = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const b = openInMain(a, paperTab("y"));
    const reordered = reduce(b, {
      type: "reorderTab",
      tabId: WORKING_DOC_TAB_ID,
      toIndex: 99,
    });
    expect(paneIds(reordered, reordered.mainId)).toEqual([
      ["paper:x", "paper:y", WORKING_DOC_TAB_ID],
    ]);
  });

  test("reordering an unknown tab raises", () => {
    expect(() =>
      reduce(INITIAL_WORKSPACE_STATE, {
        type: "reorderTab",
        tabId: "paper:absent",
        toIndex: 0,
      }),
    ).toThrow();
  });
});

describe("fail-loud preconditions", () => {
  test("moving an unknown tab raises", () => {
    expect(() =>
      reduce(INITIAL_WORKSPACE_STATE, {
        type: "moveTabToPane",
        tabId: "paper:absent",
        toPaneId: mainWin(INITIAL_WORKSPACE_STATE).panes[0].id,
      }),
    ).toThrow();
  });

  test("activating an unknown pane/window is a no-op, not a fault", () => {
    // A click can bubble to a pane's activation handler after an action removed it; that must not throw.
    expect(
      reduce(INITIAL_WORKSPACE_STATE, {
        type: "activatePane",
        winId: "win-absent",
        paneId: "pane-absent",
      }),
    ).toBe(INITIAL_WORKSPACE_STATE);
    expect(
      reduce(INITIAL_WORKSPACE_STATE, {
        type: "activatePane",
        winId: INITIAL_WORKSPACE_STATE.mainId,
        paneId: "pane-absent",
      }),
    ).toBe(INITIAL_WORKSPACE_STATE);
  });
});

describe("moveTabToPane / activateTab success paths", () => {
  // pane0 = [working doc, paper:a]; a document-sourced reveal splits paper:b into pane1 (now active).
  function twoPanes(): WorkspaceState {
    const s = openInMain(INITIAL_WORKSPACE_STATE, paperTab("a"));
    const p0 = mainWin(s).panes[0].id;
    return reduce(s, {
      type: "openTab",
      src: { kind: "document", winId: s.mainId, paneId: p0 },
      tab: paperTab("b"),
    });
  }

  test("moving the sibling pane's only tab lands it once and collapses the emptied pane", () => {
    const s = twoPanes();
    const p0 = mainWin(s).panes[0].id;
    const moved = reduce(s, {
      type: "moveTabToPane",
      tabId: "paper:b",
      toPaneId: p0,
    });
    expect(paneIds(moved, moved.mainId)).toEqual([
      [WORKING_DOC_TAB_ID, "paper:a", "paper:b"],
    ]);
  });

  test("moving a tab out of a still-populated pane keeps both panes", () => {
    const s = twoPanes();
    const p1 = mainWin(s).activePaneId; // the split made the new pane active
    const moved = reduce(s, {
      type: "moveTabToPane",
      tabId: "paper:a",
      toPaneId: p1,
    });
    expect(paneIds(moved, moved.mainId)).toEqual([
      [WORKING_DOC_TAB_ID],
      ["paper:b", "paper:a"],
    ]);
  });

  test("moveTabToPane honours toIndex", () => {
    const s = twoPanes();
    const p0 = mainWin(s).panes[0].id;
    const moved = reduce(s, {
      type: "moveTabToPane",
      tabId: "paper:b",
      toPaneId: p0,
      toIndex: 1,
    });
    expect(paneIds(moved, moved.mainId)).toEqual([
      [WORKING_DOC_TAB_ID, "paper:b", "paper:a"],
    ]);
  });

  test("activateTab retargets the window's active pane and the pane's active tab", () => {
    const s = twoPanes();
    const p0 = mainWin(s).panes[0].id;
    expect(mainWin(s).activePaneId).not.toBe(p0); // pane1 (paper:b) is active after the split
    const act = reduce(s, { type: "activateTab", tabId: "paper:a" });
    expect(mainWin(act).activePaneId).toBe(p0);
    const pane0 = mainWin(act).panes.find((p) => p.id === p0);
    expect(pane0?.activeTabId).toBe("paper:a");
  });

  test("activating an absent tab is a no-op, not a fault", () => {
    expect(
      reduce(INITIAL_WORKSPACE_STATE, {
        type: "activateTab",
        tabId: "paper:absent",
      }),
    ).toBe(INITIAL_WORKSPACE_STATE);
  });

  test("activating the already-active tab returns the same state (no re-render)", () => {
    const s = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x")); // paper:x is now active in pane0
    expect(reduce(s, { type: "activateTab", tabId: "paper:x" })).toBe(s);
  });

  test("focusing a pane whose tab is already active changes only activePaneId", () => {
    const s = twoPanes(); // pane0 = [wd, paper:a] (paper:a active); pane1 = [paper:b] is the active pane
    const before0 = mainWin(s).panes[0];
    expect(before0.activeTabId).toBe("paper:a");
    expect(mainWin(s).activePaneId).not.toBe(before0.id);
    const act = reduce(s, { type: "activateTab", tabId: "paper:a" });
    expect(mainWin(act).activePaneId).toBe(before0.id);
    // pane0 already showed paper:a, so only the window's active pane moved — pane0 keeps its identity.
    expect(mainWin(act).panes[0]).toBe(before0);
  });
});

describe("labels keyed on window + pane side", () => {
  test("label mode survives split → collapse → resplit", () => {
    const src: Source = {
      kind: "document",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      paneId: mainWin(INITIAL_WORKSPACE_STATE).panes[0].id,
    };
    const mainId = INITIAL_WORKSPACE_STATE.mainId;
    const labelled = reduce(INITIAL_WORKSPACE_STATE, {
      type: "setLabel",
      winId: mainId,
      side: "a",
      value: true,
    });
    const split = reduce(labelled, {
      type: "openTab",
      src,
      tab: paperTab("x"),
    });
    const collapsed = reduce(split, { type: "closeTab", tabId: "paper:x" });
    const resplit = reduce(collapsed, {
      type: "openTab",
      src,
      tab: paperTab("y"),
    });
    // The pane ids changed across the cycle; the side-keyed label did not.
    const resplitSecond = resplit.windows[0].panes[1];
    if (!resplitSecond) throw new Error("expected two panes after resplit");
    expect(resplitSecond.id).not.toBe(split.windows[0].panes[0].id);
    expect(resplit.labels[labelKey(mainId, "a")]).toBe(true);
  });
});

describe("close/reopen stack", () => {
  test("closing pushes a descriptor and dropClosed pops it", () => {
    const opened = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const closed = reduce(opened, { type: "closeTab", tabId: "paper:x" });
    expect(closed.closedStack.map((d) => d.id)).toEqual(["paper:x"]);
    expect(closed.openPapers).not.toContain("paper:x");
    const popped = reduce(closed, { type: "dropClosed" });
    expect(popped.closedStack).toHaveLength(0);
  });

  test("dropClosed with an empty stack is a no-op", () => {
    expect(reduce(INITIAL_WORKSPACE_STATE, { type: "dropClosed" })).toBe(
      INITIAL_WORKSPACE_STATE,
    );
  });

  test("re-placing a closed paper drops its stale descriptor", () => {
    const opened = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const closed = reduce(opened, { type: "closeTab", tabId: "paper:x" });
    expect(closed.closedStack.map((d) => d.id)).toEqual(["paper:x"]);
    // The paper comes back by another route (a fresh reveal into main): the descriptor must not
    // linger, or dropClosed → controller re-open would resurrect a duplicate from a stale snapshot.
    const reopened = openInMain(closed, paperTab("x"));
    expect(reopened.closedStack).toHaveLength(0);
  });

  test("closing drops the tab's highlight", () => {
    const opened = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const highlighted: WorkspaceState = {
      ...opened,
      highlights: { "paper:x": "a cited passage" },
    };
    const closed = reduce(highlighted, { type: "closeTab", tabId: "paper:x" });
    expect(closed.highlights["paper:x"]).toBeUndefined();
  });
});

describe("setSplitRatio", () => {
  function splitState(): WorkspaceState {
    const src: Source = {
      kind: "document",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      paneId: mainWin(INITIAL_WORKSPACE_STATE).panes[0].id,
    };
    return reduce(INITIAL_WORKSPACE_STATE, {
      type: "openTab",
      src,
      tab: paperTab("x"),
    });
  }

  test("updates only the addressed window's ratio", () => {
    const split = splitState();
    const inChild = reduce(split, {
      type: "moveTabToWindow",
      tabId: "paper:x",
      toWinId: null,
    });
    // A second paper split in main gives main two panes too, so both windows can carry a ratio.
    const src: Source = {
      kind: "document",
      winId: inChild.mainId,
      paneId: mainWin(inChild).panes[0].id,
    };
    const twoSplit = reduce(inChild, {
      type: "openTab",
      src,
      tab: paperTab("y"),
    });
    const child = childId(twoSplit);
    const next = reduce(twoSplit, {
      type: "setSplitRatio",
      winId: twoSplit.mainId,
      ratio: 0.7,
    });
    expect(mainWin(next).splitRatio).toBe(0.7);
    expect(next.windows.find((w) => w.id === child)?.splitRatio).toBe(
      twoSplit.windows.find((w) => w.id === child)?.splitRatio,
    );
  });

  test("a single-pane window records the ratio its next split will open at", () => {
    // Both the persisted restore and a drag whose pane later closed land here while main holds one
    // pane; dropping it on the floor is how a stored divider position silently never applies.
    const next = reduce(INITIAL_WORKSPACE_STATE, {
      type: "setSplitRatio",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      ratio: 0.7,
    });
    expect(mainWin(next).splitRatio).toBe(0.7);
  });

  test("an out-of-range ratio falls back to 0.5", () => {
    const next = reduce(splitState(), {
      type: "setSplitRatio",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      ratio: 4,
    });
    expect(mainWin(next).splitRatio).toBe(0.5);
  });

  test("an unknown window raises", () => {
    expect(() =>
      reduce(INITIAL_WORKSPACE_STATE, {
        type: "setSplitRatio",
        winId: "win-absent",
        ratio: 0.5,
      }),
    ).toThrow();
  });
});

describe("splitRatio survives the split it sizes", () => {
  test("the ratio a window carries is what its next split opens at", () => {
    // A curator drags the divider, closes the paper (collapsing to one pane), then reveals another.
    // Re-splitting at a hard-coded half would discard the position they set, and the persisted
    // ratio — restored while main is single-pane — could never apply at all.
    const sized = reduce(INITIAL_WORKSPACE_STATE, {
      type: "setSplitRatio",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      ratio: 0.7,
    });
    const src: Source = {
      kind: "document",
      winId: sized.mainId,
      paneId: sized.windows[0].panes[0].id,
    };
    const split = reduce(sized, { type: "openTab", src, tab: paperTab("x") });
    const main = split.windows.find((w) => w.id === split.mainId);
    expect(main?.panes.length).toBe(2);
    expect(main?.splitRatio).toBe(0.7);
  });

  test("consolidating does not discard it", () => {
    // Consolidate rebuilds main as a single pane. Resetting the ratio there loses the drag for the
    // next reveal, and the store still holds the old value, so the two disagree until a reload.
    const sized = reduce(INITIAL_WORKSPACE_STATE, {
      type: "setSplitRatio",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      ratio: 0.7,
    });
    const single = reduce(sized, { type: "consolidate" });
    expect(single.windows[0].splitRatio).toBe(0.7);
    const src: Source = {
      kind: "document",
      winId: single.mainId,
      paneId: single.windows[0].panes[0].id,
    };
    const resplit = reduce(single, {
      type: "openTab",
      src,
      tab: paperTab("x"),
    });
    expect(
      resplit.windows.find((w) => w.id === resplit.mainId)?.splitRatio,
    ).toBe(0.7);
  });
});

describe("consolidate", () => {
  test("pulls every tab into a single main pane and drops child windows", () => {
    const withX = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const xInChild = reduce(withX, {
      type: "moveTabToWindow",
      tabId: "paper:x",
      toWinId: null,
    });
    const withY = openInMain(xInChild, paperTab("y"));
    expect(withY.windows).toHaveLength(2);
    const single = reduce(withY, { type: "consolidate" });
    expect(single.windows).toHaveLength(1);
    expect(single.windows[0].panes).toHaveLength(1);
    const ids = single.windows[0].panes[0].tabs.map((t) => t.id).sort();
    expect(ids).toEqual([WORKING_DOC_TAB_ID, "paper:x", "paper:y"].sort());
  });
});

/** The id of the sole non-main window. */
function childId(state: WorkspaceState): string {
  const child = state.windows.find((w) => w.id !== state.mainId);
  if (!child) throw new Error("no child window");
  return child.id;
}

describe("moveTabToWindow — new window", () => {
  test("mints the window with caller-supplied ids when given", () => {
    const withX = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const moved = reduce(withX, {
      type: "moveTabToWindow",
      tabId: "paper:x",
      toWinId: null,
      newWinId: "win-custom",
      newPaneId: "pane-custom",
    });
    const child = moved.windows.find((w) => w.id === "win-custom");
    expect(child).toBeDefined();
    expect(child?.panes[0].id).toBe("pane-custom");
    expect(child?.panes[0].tabs.map((t) => t.id)).toEqual(["paper:x"]);
  });

  test("raises when a caller-supplied window id is already in use", () => {
    const withX = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const withY = openInMain(withX, paperTab("y"));
    const oneChild = reduce(withY, {
      type: "moveTabToWindow",
      tabId: "paper:x",
      toWinId: null,
      newWinId: "win-custom",
      newPaneId: "pane-custom",
    });
    // Ids address panes/windows on their own, so a duplicate must fail rather than mint a collision.
    expect(() =>
      reduce(oneChild, {
        type: "moveTabToWindow",
        tabId: "paper:y",
        toWinId: null,
        newWinId: "win-custom",
        newPaneId: "pane-other",
      }),
    ).toThrow("window id in use");
  });
});

describe("closeWindow — reparent on child close", () => {
  test("pinned tabs reparent into main's active pane, closable tabs drop", () => {
    // Working doc + a paper, then move both into a child window.
    const withX = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const docInChild = reduce(withX, {
      type: "moveTabToWindow",
      tabId: WORKING_DOC_TAB_ID,
      toWinId: null,
    });
    const child = childId(docInChild);
    const bothInChild = reduce(docInChild, {
      type: "moveTabToWindow",
      tabId: "paper:x",
      toWinId: child,
    });
    const highlighted = reduce(bothInChild, {
      type: "setHighlight",
      tabId: "paper:x",
      quote: "a passage",
    });
    const closed = reduce(highlighted, { type: "closeWindow", winId: child });

    expect(closed.windows).toHaveLength(1);
    expect(closed.windows[0].id).toBe(closed.mainId);
    const mainTabs = closed.windows[0].panes.flatMap((p) =>
      p.tabs.map((t) => t.id),
    );
    expect(mainTabs).toContain(WORKING_DOC_TAB_ID);
    expect(mainTabs).not.toContain("paper:x");
    expect(closed.openPapers).not.toContain("paper:x");
    expect(closed.highlights["paper:x"]).toBeUndefined();
    // Closing the window records its closable tabs for reopen, as a one-at-a-time close would.
    expect(closed.closedStack.map((d) => d.id)).toContain("paper:x");
  });

  test("closing main is a no-op (it never self-closes here)", () => {
    const closed = reduce(INITIAL_WORKSPACE_STATE, {
      type: "closeWindow",
      winId: INITIAL_WORKSPACE_STATE.mainId,
    });
    expect(closed).toBe(INITIAL_WORKSPACE_STATE);
  });

  test("drops the closed window's label keys (no per-pop-out growth)", () => {
    const withX = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const xInChild = reduce(withX, {
      type: "moveTabToWindow",
      tabId: "paper:x",
      toWinId: null,
    });
    const child = childId(xInChild);
    const labelled = reduce(xInChild, {
      type: "setLabel",
      winId: child,
      side: "a",
      value: true,
    });
    expect(labelled.labels[labelKey(child, "a")]).toBe(true);
    const closed = reduce(labelled, { type: "closeWindow", winId: child });
    expect(labelled.labels[labelKey(child, "a")]).toBe(true); // untouched original
    expect(closed.labels[labelKey(child, "a")]).toBeUndefined();
  });
});

describe("openTab — forceLocal reveal fallback", () => {
  test("surfaces a cross-window paper by default, moves it locally with forceLocal", () => {
    const withX = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const xInChild = reduce(withX, {
      type: "moveTabToWindow",
      tabId: "paper:x",
      toWinId: null,
    });
    const child = childId(xInChild);
    const mainPane = mainWin(xInChild).panes[0].id;
    const src: Source = {
      kind: "document",
      winId: xInChild.mainId,
      paneId: mainPane,
    };

    // Default: the paper stays in the child window (surface, not yank).
    const surfaced = reduce(xInChild, {
      type: "openTab",
      src,
      tab: paperTab("x"),
    });
    expect(surfaced.windows.find((w) => w.id === child)).toBeDefined();

    // forceLocal: the paper moves into main and the emptied child window is pruned.
    const local = reduce(xInChild, {
      type: "openTab",
      src,
      tab: paperTab("x"),
      forceLocal: true,
    });
    expect(local.windows.find((w) => w.id === child)).toBeUndefined();
    const mainTabs = mainWin(local).panes.flatMap((p) =>
      p.tabs.map((t) => t.id),
    );
    expect(mainTabs).toContain("paper:x");
  });
});

describe("activatePane", () => {
  test("activating the already-active pane returns the same state (no re-render)", () => {
    const s = INITIAL_WORKSPACE_STATE;
    const win = mainWin(s);
    const next = reduce(s, {
      type: "activatePane",
      winId: win.id,
      paneId: win.activePaneId,
    });
    expect(next).toBe(s);
  });
});

describe("swapPanes", () => {
  test("swaps the two panes' contents while keeping the slots (ids/order)", () => {
    const src: Source = {
      kind: "document",
      winId: INITIAL_WORKSPACE_STATE.mainId,
      paneId: mainWin(INITIAL_WORKSPACE_STATE).panes[0].id,
    };
    const split = reduce(INITIAL_WORKSPACE_STATE, {
      type: "openTab",
      src,
      tab: paperTab("x"),
    });
    expect(paneIds(split, split.mainId)).toEqual([
      [WORKING_DOC_TAB_ID],
      ["paper:x"],
    ]);
    const [idA, idB] = mainWin(split).panes.map((p) => p.id);
    const swapped = reduce(split, { type: "swapPanes", winId: split.mainId });
    // Contents traded sides; the slot ids (and thus their widths) stayed put.
    expect(paneIds(swapped, swapped.mainId)).toEqual([
      ["paper:x"],
      [WORKING_DOC_TAB_ID],
    ]);
    expect(mainWin(swapped).panes.map((p) => p.id)).toEqual([idA, idB]);
    // Swapping twice restores the original.
    const back = reduce(swapped, { type: "swapPanes", winId: swapped.mainId });
    expect(paneIds(back, back.mainId)).toEqual(paneIds(split, split.mainId));
  });

  test("swapping a single-pane window is a no-op", () => {
    expect(
      reduce(INITIAL_WORKSPACE_STATE, {
        type: "swapPanes",
        winId: INITIAL_WORKSPACE_STATE.mainId,
      }),
    ).toBe(INITIAL_WORKSPACE_STATE);
  });
});

describe("patchTab safety (failed-reveal rollback)", () => {
  test("patching an absent tab is a no-op — no throw, nothing on the reopen stack", () => {
    // The reveal patches a failed placeholder to an error state; if the user already closed the
    // loading tab, that patch reaches an absent id. It must not throw (as closeTab would) or record
    // anything for reopen.
    const s = INITIAL_WORKSPACE_STATE;
    const next = reduce(s, {
      type: "patchTab",
      tabId: "paper:gone",
      payload: { loading: false, error: true },
    });
    expect(next.closedStack).toHaveLength(0);
    expect(paneIds(next, next.mainId)).toEqual(paneIds(s, s.mainId));
  });

  test("patching a present tab merges the error flag without touching the reopen stack", () => {
    const opened = openInMain(INITIAL_WORKSPACE_STATE, paperTab("x"));
    const errored = reduce(opened, {
      type: "patchTab",
      tabId: "paper:x",
      payload: { error: true },
    });
    const tab = errored.windows
      .flatMap((w) => w.panes)
      .flatMap((p) => p.tabs)
      .find((t) => t.id === "paper:x");
    expect((tab?.payload as { error?: boolean }).error).toBe(true);
    expect(errored.closedStack).toHaveLength(0);
  });
});

describe("readDocumentPin", () => {
  test("a valid pin round-trips", () => {
    expect(
      readDocumentPin({ pin: { analysisId: "an-1", version: 3 } }),
    ).toEqual({ analysisId: "an-1", version: 3 });
  });

  test.each([
    ["an absent payload", undefined],
    ["a null payload", null],
    ["a non-object payload", "pin"],
    ["a payload without a pin", {}],
    ["a null pin (follow latest)", { pin: null }],
    ["a non-object pin", { pin: 3 }],
    ["a pin missing its analysisId", { pin: { version: 3 } }],
    ["a pin with an empty analysisId", { pin: { analysisId: "", version: 3 } }],
    ["a pin missing its version", { pin: { analysisId: "an-1" } }],
    [
      "a pin with a non-integer version",
      { pin: { analysisId: "an-1", version: 1.5 } },
    ],
    ["a pin with a zero version", { pin: { analysisId: "an-1", version: 0 } }],
  ])("%s reads as null", (_name, payload) => {
    expect(readDocumentPin(payload)).toBeNull();
  });
});

describe("pinnedDocumentVersion", () => {
  const payload = { pin: { analysisId: "an-1", version: 2 } };

  test("a pin for the given analysis selects its version", () => {
    expect(pinnedDocumentVersion(payload, "an-1")).toBe(2);
  });

  test.each([
    ["a pin naming another analysis", payload, "an-2"],
    ["no analysis open", payload, null],
    ["an unpinned payload", {}, "an-1"],
  ])("%s follows the latest (null)", (_name, p, analysisId) => {
    expect(pinnedDocumentVersion(p, analysisId)).toBeNull();
  });
});
