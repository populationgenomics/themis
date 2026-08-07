import { describe, expect, test } from "bun:test";
import {
  hydrationFromLayout,
  type PersistedLayout,
  parseEdge,
  parseLabelMode,
  parseLayout,
  parseRatio,
  serializeLabelMode,
  serializeLayout,
} from "./use-workspace-model";
import type { Tab, Win } from "./workspace-model";
import { WORKING_DOC_TAB_ID } from "./workspace-model";

// The controller's persistence is otherwise DOM/localStorage-bound (covered by screenshots + the F6c/d
// tests); here the pure parse/serialize helpers are unit-tested for round-trip and for rejecting a
// stale or hand-edited entry rather than corrupting reducer state.

describe("conversation-edge persistence", () => {
  test("every edge round-trips (it is stored verbatim)", () => {
    for (const edge of ["left", "right", "top", "bottom"] as const) {
      expect(parseEdge(edge)).toBe(edge);
    }
  });

  test("a value that is not an edge is rejected", () => {
    expect(parseEdge("sideways")).toBeNull();
    expect(parseEdge("")).toBeNull();
    expect(parseEdge(null)).toBeNull();
  });
});

describe("orientation-specific ratio persistence", () => {
  test("an in-range ratio round-trips through String()", () => {
    for (const r of [0.2, 0.33, 0.5, 0.75]) {
      expect(parseRatio(String(r))).toBe(r);
    }
  });

  test("a ratio at or past the bounds, or malformed, is rejected", () => {
    for (const raw of ["0", "1", "-0.2", "1.5", "NaN", "", null]) {
      expect(parseRatio(raw)).toBeNull();
    }
  });
});

describe("strip label-mode persistence", () => {
  test("both modes round-trip", () => {
    expect(parseLabelMode(serializeLabelMode(true))).toBe(true);
    expect(parseLabelMode(serializeLabelMode(false))).toBe(false);
  });

  test("an unrecognized token is rejected", () => {
    expect(parseLabelMode("maybe")).toBeNull();
    expect(parseLabelMode(null)).toBeNull();
  });
});

const WORKING_DOC: Tab = {
  id: WORKING_DOC_TAB_ID,
  kind: "working-doc",
  pinned: true,
  payload: {},
};

function paperTab(docId: string): Tab {
  return {
    id: `paper:${docId}`,
    kind: "paper",
    pinned: false,
    payload: {
      docId,
      title: `Paper ${docId}`,
      hasMarkdown: true,
      hasPdf: false,
      representation: "MARKDOWN",
    },
  };
}

describe("layout serialize → parse round-trip", () => {
  // Working doc in pane 'a', two papers in pane 'b' (the active pane), one paper in a child window, plus
  // a closed-tab descriptor — the whole durable layout the reload consolidates.
  const main: Win = {
    id: "main",
    panes: [
      { id: "pane-0", tabs: [WORKING_DOC], activeTabId: WORKING_DOC_TAB_ID },
      {
        id: "pane-1",
        tabs: [paperTab("x"), paperTab("y")],
        activeTabId: "paper:y",
      },
    ],
    splitRatio: 0.4,
    activePaneId: "pane-1",
  };
  const child: Win = {
    id: "win-child",
    panes: [{ id: "pane-9", tabs: [paperTab("z")], activeTabId: "paper:z" }],
    splitRatio: 0.5,
    activePaneId: "pane-9",
  };
  const closedStack = [
    { id: "paper:gone", kind: "paper", payload: { docId: "gone" } },
  ];

  test("a tab persists as {kind, args}, not its fetched payload", () => {
    const parsed = parseLayout(
      serializeLayout([main, child], "main", closedStack),
    );
    if (!parsed) throw new Error("round-trip returned null");
    // Working doc keeps its slot with null args (reconstructed, never re-fetched); the paper keeps only
    // its re-open args, not the title/representation the reload re-fetches.
    expect(parsed.panes[0].tabs).toEqual([{ kind: "working-doc", args: null }]);
    expect(parsed.panes[1].tabs).toEqual([
      { kind: "paper", args: { docId: "x" } },
      { kind: "paper", args: { docId: "y" } },
    ]);
  });

  test("split state, active pane, and the child-origin + closed sets round-trip", () => {
    const parsed = parseLayout(
      serializeLayout([main, child], "main", closedStack),
    );
    if (!parsed) throw new Error("round-trip returned null");
    expect(parsed.activePaneSide).toBe("b");
    expect(parsed.panes[1].activeTabId).toBe("paper:y");
    expect(parsed.splitRatio).toBe(0.4);
    // The child window's paper survives as a child-origin descriptor (no persisted pane).
    expect(parsed.childTabs).toEqual([{ kind: "paper", args: { docId: "z" } }]);
    expect(parsed.closedStack).toEqual(closedStack);
  });
});

describe("parseLayout rejects a malformed or absent entry", () => {
  test.each([
    ["absent", null],
    ["not JSON", "{"],
    ["not an object", "42"],
    ["no panes", JSON.stringify({ activePaneSide: "a", childTabs: [] })],
    ["empty panes", JSON.stringify({ panes: [], activePaneSide: "a" })],
    [
      "three panes",
      JSON.stringify({
        panes: [{ tabs: [] }, { tabs: [] }, { tabs: [] }],
        activePaneSide: "a",
        childTabs: [],
        closedStack: [],
      }),
    ],
    [
      "bad activePaneSide",
      JSON.stringify({
        panes: [{ tabs: [], activeTabId: null }],
        activePaneSide: "middle",
        childTabs: [],
        closedStack: [],
      }),
    ],
    [
      "closed descriptor with no payload (reopen would dereference it)",
      JSON.stringify({
        panes: [{ tabs: [], activeTabId: null }],
        activePaneSide: "a",
        childTabs: [],
        closedStack: [{ id: "paper:x", kind: "paper" }],
      }),
    ],
  ])("%s → null (fall back to default)", (_label, raw) => {
    expect(parseLayout(raw as string | null)).toBeNull();
  });
});

describe("hydrationFromLayout — consolidate to one main window", () => {
  // Supplementary re-fetch packs its args into a payload with no network, so the full merge is testable
  // as a pure function; a paper re-fetch would need the BFF (manual/screenshot-covered).
  const layout: PersistedLayout = {
    panes: [
      { tabs: [{ kind: "working-doc", args: null }], activeTabId: null },
      {
        tabs: [{ kind: "supplementary", args: { docId: "s1", name: "a.csv" } }],
        activeTabId: "supp:s1:a.csv",
      },
    ],
    activePaneSide: "b",
    splitRatio: 0.4,
    childTabs: [
      { kind: "supplementary", args: { docId: "s2", name: "b.csv" } },
    ],
    closedStack: [],
  };

  test("child-origin tabs append into the active pane after the restored tabs", async () => {
    const h = await hydrationFromLayout(layout);
    expect(h.panes[0].tabs.map((t) => t.id)).toEqual([WORKING_DOC_TAB_ID]);
    // Restored s1 stays first; the child-origin s2 follows it in the active pane 'b'.
    expect(h.panes[1].tabs.map((t) => t.id)).toEqual([
      "supp:s1:a.csv",
      "supp:s2:b.csv",
    ]);
    expect(h.panes[1].activeTabId).toBe("supp:s1:a.csv");
    expect(h.activePaneSide).toBe("b");
    expect(h.splitRatio).toBe(0.4);
  });

  test("a stale/unknown descriptor is dropped, not fatal", async () => {
    const h = await hydrationFromLayout({
      ...layout,
      childTabs: [{ kind: "no-such-kind", args: {} }],
    });
    const ids = h.panes.flatMap((p) => p.tabs.map((t) => t.id));
    expect(ids).toEqual([WORKING_DOC_TAB_ID, "supp:s1:a.csv"]);
  });

  test("a closed-stack entry whose kind the registry lacks is dropped", async () => {
    const h = await hydrationFromLayout({
      ...layout,
      closedStack: [
        {
          id: "supp:s3:c.csv",
          kind: "supplementary",
          payload: { docId: "s3" },
        },
        { id: "gone:1", kind: "no-such-kind", payload: {} },
      ],
    });
    // Otherwise "Reopen closed tab" could resurrect a since-removed kind.
    expect(h.closedStack.map((d) => d.kind)).toEqual(["supplementary"]);
  });

  test("a tab id repeated across panes is kept once (a duplicate crashes the reducer)", async () => {
    const h = await hydrationFromLayout({
      ...layout,
      panes: [
        { tabs: [{ kind: "working-doc", args: null }], activeTabId: null },
        { tabs: [{ kind: "working-doc", args: null }], activeTabId: null },
      ],
      childTabs: [],
    });
    const ids = h.panes.flatMap((p) => p.tabs.map((t) => t.id));
    expect(ids.filter((id) => id === WORKING_DOC_TAB_ID)).toHaveLength(1);
  });

  test("the working document is reconstructed when no descriptor carries it", async () => {
    const h = await hydrationFromLayout({
      panes: [
        {
          tabs: [
            { kind: "supplementary", args: { docId: "s1", name: "a.csv" } },
          ],
          activeTabId: "supp:s1:a.csv",
        },
      ],
      activePaneSide: "a",
      splitRatio: 0.5,
      childTabs: [],
      closedStack: [],
    });
    const ids = h.panes.flatMap((p) => p.tabs.map((t) => t.id));
    expect(ids).toContain(WORKING_DOC_TAB_ID);
  });
});
