import { describe, expect, test } from "bun:test";
import {
  addDragSession,
  dropIntent,
  encodeTabDrag,
  insertionIndex,
  parseTabDrag,
  removeDragSession,
  resolveDrop,
  resolveSession,
} from "./tab-dnd";

describe("parseTabDrag — validate the payload shape at drop", () => {
  test("a well-formed tab payload round-trips", () => {
    const raw = encodeTabDrag({ tabId: "paper:x", paneId: "pane-1" });
    expect(parseTabDrag(raw)).toEqual({ tabId: "paper:x", paneId: "pane-1" });
  });

  test("an absent payload (foreign drag) is ignored", () => {
    expect(parseTabDrag("")).toBeNull();
    expect(parseTabDrag(null)).toBeNull();
  });

  test("malformed JSON is ignored, not thrown", () => {
    expect(parseTabDrag("{not json")).toBeNull();
  });

  test("a JSON object missing the tab fields is ignored", () => {
    expect(parseTabDrag(JSON.stringify({ url: "https://x" }))).toBeNull();
    expect(parseTabDrag(JSON.stringify({ tabId: 3, paneId: "p" }))).toBeNull();
  });
});

describe("insertionIndex — the slot for a pointer over a vertical tab list", () => {
  const mids = [10, 30, 50];

  test("above the first midpoint inserts at the top", () => {
    expect(insertionIndex(mids, 5)).toBe(0);
  });

  test("between two midpoints inserts between them", () => {
    expect(insertionIndex(mids, 20)).toBe(1);
    expect(insertionIndex(mids, 40)).toBe(2);
  });

  test("below the last midpoint inserts at the end", () => {
    expect(insertionIndex(mids, 99)).toBe(3);
  });

  test("an empty list is always slot 0", () => {
    expect(insertionIndex([], 42)).toBe(0);
  });
});

describe("dropIntent — resolve a validated drop", () => {
  const here = { paneId: "pane-a", index: 1 };

  test("a payload for no tab of this window is ignored", () => {
    expect(dropIntent("paper:x", "pane-a", 0, null)).toEqual({ type: "none" });
  });

  test("a different target pane is a move that carries the pointer's slot", () => {
    expect(dropIntent("paper:x", "pane-b", 0, here)).toEqual({
      type: "move",
      tabId: "paper:x",
      toPaneId: "pane-b",
      toIndex: 0,
    });
    // Dropped into the middle of the sibling strip, the move lands at that slot, not appended.
    expect(dropIntent("paper:x", "pane-b", 2, here)).toEqual({
      type: "move",
      tabId: "paper:x",
      toPaneId: "pane-b",
      toIndex: 2,
    });
  });

  test("dropping in its own slot (before or right after itself) is a no-op", () => {
    expect(dropIntent("paper:x", "pane-a", 1, here)).toEqual({ type: "none" });
    expect(dropIntent("paper:x", "pane-a", 2, here)).toEqual({ type: "none" });
  });

  test("a same-pane drop before its current slot reorders to that index", () => {
    expect(dropIntent("paper:x", "pane-a", 0, here)).toEqual({
      type: "reorder",
      tabId: "paper:x",
      toIndex: 0,
    });
  });

  test("a same-pane drop past its current slot accounts for its own removal", () => {
    // Tab at index 1 of [A, X, B, C] dropped after C (insertion slot 4) → final index 3.
    expect(dropIntent("paper:x", "pane-a", 4, here)).toEqual({
      type: "reorder",
      tabId: "paper:x",
      toIndex: 3,
    });
  });
});

describe("live-session store — cross-window drags a window learns of", () => {
  test("a session added on drag-session resolves while live", () => {
    const sessions = addDragSession({}, "s1", "paper:x", "win-2");
    expect(resolveSession(sessions, "s1")).toEqual({
      tabId: "paper:x",
      sourceWinId: "win-2",
    });
  });

  test("a session is gone after drag-end", () => {
    const live = addDragSession({}, "s1", "paper:x", "win-2");
    expect(resolveSession(removeDragSession(live, "s1"), "s1")).toBeNull();
  });

  test("an unknown or absent id resolves to null", () => {
    const live = addDragSession({}, "s1", "paper:x", "win-2");
    expect(resolveSession(live, "s2")).toBeNull();
    expect(resolveSession(live, null)).toBeNull();
    expect(resolveSession({}, "s1")).toBeNull();
  });

  test("removing one session leaves the others live", () => {
    let sessions = addDragSession({}, "s1", "paper:x", "win-2");
    sessions = addDragSession(sessions, "s2", "paper:y", "win-3");
    sessions = removeDragSession(sessions, "s1");
    expect(resolveSession(sessions, "s1")).toBeNull();
    expect(resolveSession(sessions, "s2")).toEqual({
      tabId: "paper:y",
      sourceWinId: "win-3",
    });
  });
});

describe("resolveDrop — within-window first, then a live cross-window session", () => {
  const session = { tabId: "paper:x", sourceWinId: "win-2" };

  test("a within-window action wins even when a session is also present", () => {
    expect(
      resolveDrop({
        within: { type: "reorder", tabId: "paper:x", toIndex: 2 },
        session,
        destWinId: "win-1",
        destPaneId: "pane-a",
      }),
    ).toEqual({ type: "reorder", tabId: "paper:x", toIndex: 2 });
  });

  test("no within-window match + a session from another window is a cross-window move", () => {
    expect(
      resolveDrop({
        within: { type: "none" },
        session,
        destWinId: "win-1",
        destPaneId: "pane-a",
      }),
    ).toEqual({
      type: "cross-window-move",
      tabId: "paper:x",
      toPaneId: "pane-a",
    });
  });

  test("a same-window session is not treated as a cross-window move", () => {
    expect(
      resolveDrop({
        within: { type: "none" },
        session: { tabId: "paper:x", sourceWinId: "win-1" },
        destWinId: "win-1",
        destPaneId: "pane-a",
      }),
    ).toEqual({ type: "none" });
  });

  test("a foreign drop (no session, no within-window match) is ignored", () => {
    expect(
      resolveDrop({
        within: { type: "none" },
        session: null,
        destWinId: "win-1",
        destPaneId: "pane-a",
      }),
    ).toEqual({ type: "none" });
  });
});
