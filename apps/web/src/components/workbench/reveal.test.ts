import { describe, expect, test } from "bun:test";
import { revealCitation } from "./reveal";
import type { Source, Tab } from "./workspace-model";

const DOC = "11111111-1111-4111-8111-111111111111";
const FROM_DOC: Source = { kind: "document", winId: "main", paneId: "pane-0" };

/** A controller stub that records the reveal's two side effects — the open and the highlight — without
 *  a reducer or the network. `openTab` never runs the create-thunk, so no paper fetch is attempted. */
function stub(
  openTab: (
    src: Source,
    id: string,
    create: () => Promise<Tab>,
  ) => Promise<void>,
) {
  const opens: Array<{ src: Source; id: string; hasCreate: boolean }> = [];
  const highlights: Array<[string, string]> = [];
  return {
    controller: {
      openTab: (src: Source, id: string, create: () => Promise<Tab>) => {
        opens.push({ src, id, hasCreate: typeof create === "function" });
        return openTab(src, id, create);
      },
      setHighlight: (tabId: string, quote: string) =>
        highlights.push([tabId, quote]),
    },
    opens,
    highlights,
  };
}

describe("revealCitation", () => {
  test("opens the paper beside the source and forwards a create-thunk", async () => {
    const { controller, opens } = stub(async () => {});
    await revealCitation(controller, FROM_DOC, { kind: "paper", docId: DOC });
    expect(opens).toHaveLength(1);
    expect(opens[0].id).toBe(`paper:${DOC}`);
    expect(opens[0].src).toEqual(FROM_DOC);
    // The controller, not the reveal, decides whether to run the fetch — so the thunk reaches it.
    expect(opens[0].hasCreate).toBe(true);
  });

  test("a quote citation highlights only after the open resolves", async () => {
    let resolved = false;
    const { controller, highlights } = stub(async () => {
      resolved = true;
    });
    await revealCitation(
      controller,
      { kind: "conversation" },
      {
        kind: "quote",
        docId: DOC,
        quote: "a cited passage",
      },
    );
    expect(resolved).toBe(true);
    expect(highlights).toEqual([[`paper:${DOC}`, "a cited passage"]]);
  });

  test("a plain paper citation clears the highlight (authoritative, not additive)", async () => {
    const { controller, highlights } = stub(async () => {});
    await revealCitation(controller, FROM_DOC, { kind: "paper", docId: DOC });
    // Surfacing a paper sets the empty highlight (`""`, the clear signal both views honour), so an
    // earlier `:quote`'s highlight and warning chip do not survive a later bare `:paper` reveal.
    expect(highlights).toEqual([[`paper:${DOC}`, ""]]);
  });

  test("a failed open leaves no orphan highlight", async () => {
    const { controller, highlights } = stub(async () => {
      throw new Error("paper fetch failed");
    });
    await revealCitation(controller, FROM_DOC, {
      kind: "quote",
      docId: DOC,
      quote: "a cited passage",
    });
    expect(highlights).toHaveLength(0);
  });
});
