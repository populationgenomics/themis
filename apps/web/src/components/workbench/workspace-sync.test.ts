import { describe, expect, test } from "bun:test";
import { Representation } from "@/models/literature";
import type { WorkspaceModelController } from "./use-workspace-model";
import {
  INITIAL_WORKSPACE_STATE,
  type Source,
  type Tab,
  type Win,
  WORKING_DOC_TAB_ID,
} from "./workspace-model";
import {
  applyWorkspaceCommand,
  buildSnapshot,
  documentFetchKey,
  mirrorWindowActions,
  mirrorWorkspace,
  type WorkspaceCommand,
  type WorkspaceSnapshot,
  windowDestinations,
  windowLabel,
} from "./workspace-sync";

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

const WORKING_TAB: Tab = {
  id: WORKING_DOC_TAB_ID,
  kind: "working-doc",
  pinned: true,
  payload: {},
};

function pinnedWorkingTab(analysisId: string, version: number): Tab {
  return { ...WORKING_TAB, payload: { pin: { analysisId, version } } };
}

function win(id: string, tabs: Tab[], activeTabId: string | null): Win {
  return {
    id,
    panes: [{ id: `${id}-p0`, tabs, activeTabId }],
    splitRatio: 0.5,
    activePaneId: `${id}-p0`,
  };
}

function snapshotWith(windows: Win[]): WorkspaceSnapshot {
  return {
    windows,
    mainId: "main",
    conversation: { edge: "left", ratioH: 0.33, ratioV: 0.33 },
    labels: {},
    highlights: {},
    openPapers: [],
    workingDocument: { version: 4, analysisId: "analysis-1" },
  };
}

describe("buildSnapshot", () => {
  test("carries structure + the working-doc version + analysisId, never a body", () => {
    const state = {
      ...INITIAL_WORKSPACE_STATE,
      highlights: { "paper:x": "a quote" },
      openPapers: ["paper:x"],
    };
    const snapshot = buildSnapshot(state, {
      version: 7,
      analysisId: "analysis-42",
    });
    expect(snapshot.windows).toBe(state.windows);
    expect(snapshot.highlights).toEqual({ "paper:x": "a quote" });
    expect(snapshot.openPapers).toEqual(["paper:x"]);
    expect(snapshot.workingDocument).toEqual({
      version: 7,
      analysisId: "analysis-42",
    });
    // The refetch signal is version + analysisId only; the markdown body must not ride the channel. Pin
    // that on the signal itself — a whole-snapshot substring check would instead trip on an unrelated
    // `hasMarkdown` field the moment a paper tab enters the fixture, testing the fixture, not the rule.
    expect(snapshot.workingDocument).not.toHaveProperty("markdown");
  });

  test("round-trips through structured serialization unchanged", () => {
    const snapshot = snapshotWith([
      win("main", [WORKING_TAB], WORKING_DOC_TAB_ID),
    ]);
    expect(structuredClone(snapshot)).toEqual(snapshot);
  });
});

describe("documentFetchKey", () => {
  test("keys the working-doc fetch on {analysisId, version}", () => {
    const snapshot = snapshotWith([
      win("main", [WORKING_TAB], WORKING_DOC_TAB_ID),
    ]);
    expect(documentFetchKey(snapshot)).toEqual({
      analysisId: "analysis-1",
      version: 4,
    });
  });

  test("is all-null when no document has been produced", () => {
    const snapshot = { ...snapshotWith([]), workingDocument: null };
    expect(documentFetchKey(snapshot)).toEqual({
      analysisId: null,
      version: null,
    });
  });

  test("a version bump re-keys the fetch", () => {
    const base = snapshotWith([]);
    const bumped = {
      ...base,
      workingDocument: { version: 5, analysisId: "analysis-1" },
    };
    expect(documentFetchKey(base).version).not.toBe(
      documentFetchKey(bumped).version,
    );
    expect(documentFetchKey(bumped)).toEqual({
      analysisId: "analysis-1",
      version: 5,
    });
  });

  test("a pin for the signal's analysis selects that version", () => {
    const snapshot = snapshotWith([
      win("main", [pinnedWorkingTab("analysis-1", 2)], WORKING_DOC_TAB_ID),
    ]);
    expect(documentFetchKey(snapshot)).toEqual({
      analysisId: "analysis-1",
      version: 2,
    });
  });

  test("a pin naming another analysis is ignored — the latest wins", () => {
    const snapshot = snapshotWith([
      win("main", [pinnedWorkingTab("analysis-9", 2)], WORKING_DOC_TAB_ID),
    ]);
    expect(documentFetchKey(snapshot)).toEqual({
      analysisId: "analysis-1",
      version: 4,
    });
  });

  test("the pin follows the working-doc tab into a child window", () => {
    const snapshot = snapshotWith([
      win("main", [], null),
      win("child", [pinnedWorkingTab("analysis-1", 1)], WORKING_DOC_TAB_ID),
    ]);
    expect(documentFetchKey(snapshot)).toEqual({
      analysisId: "analysis-1",
      version: 1,
    });
  });
});

/** A controller that records the calls `applyWorkspaceCommand` makes, without a reducer or network. */
function recordingController(): {
  controller: WorkspaceModelController;
  calls: Array<[string, unknown[]]>;
} {
  const calls: Array<[string, unknown[]]> = [];
  const rec =
    (name: string) =>
    (...args: unknown[]) => {
      calls.push([name, args]);
    };
  const controller = {
    state: INITIAL_WORKSPACE_STATE,
    activateTab: rec("activateTab"),
    activatePane: rec("activatePane"),
    setSplitRatio: rec("setSplitRatio"),
    split: rec("split"),
    reorderTab: rec("reorderTab"),
    moveTabToPane: rec("moveTabToPane"),
    swapPanes: rec("swapPanes"),
    moveTabToWindow: rec("moveTabToWindow"),
    moveTabToNewWindow: rec("moveTabToNewWindow"),
    closeWindow: rec("closeWindow"),
    closeTab: rec("closeTab"),
    openTab: async (...args: unknown[]) => {
      calls.push(["openTab", args]);
    },
    setConversationEdge: rec("setConversationEdge"),
    setLabel: rec("setLabel"),
    setHighlight: rec("setHighlight"),
    patchTab: rec("patchTab"),
    reopenClosed: rec("reopenClosed"),
    consolidate: rec("consolidate"),
    readOuterRatio: () => null,
    writeOuterRatio: rec("writeOuterRatio"),
  } as unknown as WorkspaceModelController;
  return { controller, calls };
}

describe("applyWorkspaceCommand", () => {
  const cases: Array<[WorkspaceCommand, string, unknown[]]> = [
    [{ type: "activateTab", tabId: "t" }, "activateTab", ["t"]],
    [
      { type: "activatePane", winId: "w", paneId: "p" },
      "activatePane",
      ["w", "p"],
    ],
    [{ type: "split", tabId: "t" }, "split", ["t"]],
    [{ type: "reorderTab", tabId: "t", toIndex: 2 }, "reorderTab", ["t", 2]],
    [
      { type: "moveTabToPane", tabId: "t", toPaneId: "p", toIndex: 1 },
      "moveTabToPane",
      ["t", "p", 1],
    ],
    [
      { type: "moveTabToWindow", tabId: "t", toWinId: "w2" },
      "moveTabToWindow",
      ["t", "w2"],
    ],
    [{ type: "closeTab", tabId: "t" }, "closeTab", ["t"]],
    [
      { type: "setLabel", winId: "w", side: "b", value: true },
      "setLabel",
      ["w", "b", true],
    ],
    [
      { type: "setHighlight", tabId: "t", quote: "q" },
      "setHighlight",
      ["t", "q"],
    ],
    [
      { type: "patchTab", tabId: "t", payload: { representation: 1 } },
      "patchTab",
      ["t", { representation: 1 }],
    ],
    [
      { type: "setSplitRatio", winId: "w", ratio: 0.4 },
      "setSplitRatio",
      ["w", 0.4],
    ],
    [{ type: "swapPanes", winId: "w" }, "swapPanes", ["w"]],
  ];

  test.each(cases)(
    "%o dispatches to the matching controller method",
    (command, method, args) => {
      const { controller, calls } = recordingController();
      applyWorkspaceCommand(controller, command);
      expect(calls).toEqual([[method, args]]);
    },
  );

  test("moveTabToWindow with a null target is left to the orchestrator (no dispatch)", () => {
    const { controller, calls } = recordingController();
    applyWorkspaceCommand(controller, {
      type: "moveTabToWindow",
      tabId: "t",
      toWinId: null,
    });
    expect(calls).toEqual([]);
  });

  test("an openTab intent runs the registry opener with the derived id", () => {
    const { controller, calls } = recordingController();
    const src: Source = { kind: "document", winId: "main", paneId: "p0" };
    applyWorkspaceCommand(controller, {
      type: "openTab",
      kind: "paper",
      args: { docId: "doc-9" },
      src,
    });
    expect(calls).toHaveLength(1);
    const [method, args] = calls[0];
    expect(method).toBe("openTab");
    expect(args[0]).toEqual(src);
    expect(args[1]).toBe("paper:doc-9");
  });
});

describe("mirrorWorkspace", () => {
  const snapshot = snapshotWith([
    win("main", [WORKING_TAB], WORKING_DOC_TAB_ID),
  ]);

  function mirror() {
    const sent: WorkspaceCommand[] = [];
    const workspace = mirrorWorkspace(snapshot, "main", (c) => sent.push(c));
    return { workspace, sent };
  }

  test("exposes the snapshot's structure and signals as reducer state", () => {
    const { workspace } = mirror();
    expect(workspace.state.windows).toBe(snapshot.windows);
    expect(workspace.state.mainId).toBe("main");
  });

  test("mutators post the matching commands", () => {
    const { workspace, sent } = mirror();
    workspace.activateTab("t");
    workspace.split("t");
    workspace.moveTabToPane("t", "p", 1);
    workspace.moveTabToWindow("t", "w2");
    workspace.moveTabToNewWindow("t", "ignored-win", "ignored-pane");
    workspace.closeTab("t");
    workspace.setHighlight("t", "q");
    workspace.patchTab("t", { representation: 1 });
    expect(sent).toEqual([
      { type: "activateTab", tabId: "t" },
      { type: "split", tabId: "t" },
      { type: "moveTabToPane", tabId: "t", toPaneId: "p", toIndex: 1 },
      { type: "moveTabToWindow", tabId: "t", toWinId: "w2" },
      { type: "moveTabToWindow", tabId: "t", toWinId: null },
      { type: "closeTab", tabId: "t" },
      { type: "setHighlight", tabId: "t", quote: "q" },
      { type: "patchTab", tabId: "t", payload: { representation: 1 } },
    ]);
  });

  test("openTab posts the intent as a command, never running the opener", async () => {
    const { workspace, sent } = mirror();
    let ran = false;
    const src: Source = { kind: "document", winId: "main", paneId: "p0" };
    await workspace.openTab(
      src,
      "paper:doc-9",
      async () => {
        ran = true;
        return paperTab("doc-9");
      },
      { intent: { kind: "paper", args: { docId: "doc-9" } } },
    );
    expect(ran).toBe(false);
    expect(sent).toEqual([
      { type: "openTab", kind: "paper", args: { docId: "doc-9" }, src },
    ]);
  });

  test("openTab without an intent fails loud", async () => {
    const { workspace } = mirror();
    const src: Source = { kind: "document", winId: "main", paneId: "p0" };
    await expect(
      workspace.openTab(src, "paper:doc-9", async () => paperTab("doc-9")),
    ).rejects.toThrow("intent");
  });

  test("main-only actions fail loud in a mirror", () => {
    const { workspace } = mirror();
    expect(() => workspace.setConversationEdge("top")).toThrow("main-only");
    expect(() => workspace.consolidate()).toThrow("main-only");
    expect(() => workspace.reopenClosed()).toThrow("main-only");
  });
});

describe("windowDestinations / windowLabel", () => {
  test("names each window by its active tab, excluding the current one", () => {
    const windows = [
      win("main", [WORKING_TAB], WORKING_DOC_TAB_ID),
      win("win-1", [paperTab("d1")], "paper:d1"),
    ];
    expect(windowDestinations(windows, "main")).toEqual([
      { winId: "win-1", label: "Paper d1" },
    ]);
    expect(windowLabel(windows[0])).toBe("Working document");
  });

  test("mirrorWindowActions posts move commands and lists destinations", () => {
    const snapshot = snapshotWith([
      win("main", [WORKING_TAB], WORKING_DOC_TAB_ID),
      win("win-1", [paperTab("d1")], "paper:d1"),
    ]);
    const sent: WorkspaceCommand[] = [];
    const actions = mirrorWindowActions(snapshot, (c) => sent.push(c));
    expect(actions.destinations("win-1")).toEqual([
      { winId: "main", label: "Working document" },
    ]);
    actions.moveToWindow("paper:d1", "main");
    actions.moveToNewWindow("paper:d1");
    expect(sent).toEqual([
      { type: "moveTabToWindow", tabId: "paper:d1", toWinId: "main" },
      { type: "moveTabToWindow", tabId: "paper:d1", toWinId: null },
    ]);
  });
});
