import { describe, expect, test } from "bun:test";
import { createRef } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { GroupImperativeHandle } from "@/components/ui/resizable";
import { REGISTRY } from "./content-kinds";
import { ConversationSplit } from "./workbench-layout";
import type { Edge } from "./workspace-model";

// Interactive drag-resize and the ratio-apply effect are DOM-bound; here the outer split's
// rendered panel order and orientation across dock edges are tested.

function renderSplit(edge: Edge): string {
  return renderToStaticMarkup(
    <ConversationSplit
      edge={edge}
      groupRef={createRef<GroupImperativeHandle | null>()}
      onLayoutChanged={() => {}}
      defaultConversationRatio={0.33}
      conversation={<div>CONVERSATION_REGION</div>}
      tabArea={<div>TAB_AREA</div>}
    />,
  );
}

describe("outer split direction and conversation side", () => {
  test.each([
    ["left", "row", true],
    ["right", "row", false],
    ["top", "column", true],
    ["bottom", "column", false],
  ] as const)(
    "edge %s → flex-direction %s, conversation first=%p",
    (edge, direction, first) => {
      const html = renderSplit(edge);
      expect(html).toContain(`flex-direction:${direction}`);
      const conv = html.indexOf("CONVERSATION_REGION");
      const tabs = html.indexOf("TAB_AREA");
      expect(conv).toBeGreaterThanOrEqual(0);
      expect(tabs).toBeGreaterThanOrEqual(0);
      expect(conv < tabs).toBe(first);
    },
  );

  test("the divider is a labelled separator", () => {
    const html = renderSplit("left");
    expect(html).toContain('role="separator"');
    expect(html).toContain('aria-label="Resize conversation and documents"');
  });
});

describe("the conversation is a region, not a tab", () => {
  test("there is no conversation content kind, so nothing can place it in a tab", () => {
    // The load-bearing invariant: a tab is one of the registry's kinds (working-doc / paper /
    // supplementary), and the conversation is not among them, so no pane can render or hold it as a
    // tab. (The old check for `role="tab"`/`"tablist"` tested nothing — the tab strip emits
    // `aria-current` buttons, not those roles, so it never appeared in the tree either way.)
    expect(Object.keys(REGISTRY)).not.toContain("conversation");
  });
});
