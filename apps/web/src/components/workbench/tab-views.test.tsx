import { describe, expect, test } from "bun:test";
import type { ReactElement, ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { CopyClearing } from "./clear-copy";
import { REGISTRY, type RenderContext } from "./content-kinds";
import { WorkingDocumentView } from "./tab-views";
import type { WorkingDocumentState } from "./working-document";

// What a curator sees of the working document while the Poll cannot read the workspace: what the
// tab showed stays, and a notice says it may be out of date.

const NOTICE = "The workspace can&#x27;t be reached right now";
const SHOWN: WorkingDocumentState = {
  kind: "shown",
  markdown: "# Variant summary\n\nPS3 applies.\n",
};
const noop = () => {};

const IDLE: CopyClearing = { state: { kind: "idle" }, clear: noop };

function render(
  document: WorkingDocumentState,
  unavailable: boolean,
  clearing: CopyClearing = IDLE,
): string {
  return renderToStaticMarkup(
    <WorkingDocumentView
      document={document}
      unavailable={unavailable}
      clearing={clearing}
      onCitation={noop}
    />,
  );
}

describe("the working document while the workspace is unavailable", () => {
  test("keeps the rendered document and shows the notice above it", () => {
    const html = render(SHOWN, true);
    expect(html).toContain("Variant summary");
    expect(html).toContain("PS3 applies.");
    expect(html).toContain(NOTICE);
    expect(html.indexOf(NOTICE)).toBeLessThan(html.indexOf("Variant summary"));
  });

  test("keeps a state that says there is no document, under the same notice", () => {
    const html = render({ kind: "noRepository" }, true);
    expect(html).toContain("no workspace repository yet");
    expect(html).toContain(NOTICE);
  });

  test("with nothing read yet says only that the workspace can't be reached", () => {
    const html = render({ kind: "unavailable" }, true);
    expect(html.split(NOTICE)).toHaveLength(2);
    expect(html).not.toContain("out of date");
  });

  test("shows no notice over a damaged workspace, which the notice would not describe", () => {
    const html = render({ kind: "damaged" }, true);
    expect(html).toContain("This workspace is damaged");
    expect(html).not.toContain(NOTICE);
  });

  test("shows no notice once the workspace is readable again", () => {
    const html = render(SHOWN, false);
    expect(html).toContain("Variant summary");
    expect(html).not.toContain(NOTICE);
  });
});

const CLEAR = "Clear cache and reload";
const CACHE_FAILED =
  "This workspace couldn&#x27;t be loaded from your browser&#x27;s cache. Clearing the cache and reloading usually fixes this.";
const NOTHING_LOST =
  "Only this browser&#x27;s cached copy is cleared. Nothing saved is lost.";

/** The first element under `node`, expanding function components as React would, whose own type
 *  is `type`: enough of a render to reach a handler without a DOM. */
function findElement(node: ReactNode, type: string): ReactElement | undefined {
  if (node === null || typeof node !== "object") return undefined;
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElement(child, type);
      if (found !== undefined) return found;
    }
    return undefined;
  }
  const element = node as ReactElement<{ children?: ReactNode }>;
  if (element.type === type) return element;
  if (typeof element.type === "function") {
    const component = element.type as (props: unknown) => ReactNode;
    return findElement(component(element.props), type);
  }
  return findElement(element.props.children, type);
}

describe("clearing the browser's copy from the working document", () => {
  test("is offered beside a failed read of the copy, saying nothing saved is lost", () => {
    const html = render({ kind: "copyFailed" }, false);
    expect(html).toContain(CACHE_FAILED);
    expect(html).toContain(CLEAR);
    expect(html).toContain(NOTHING_LOST);
  });

  test("is not offered over a damaged workspace, which clearing leaves damaged", () => {
    expect(render({ kind: "damaged" }, false)).not.toContain(CLEAR);
  });

  test("is not offered beside a Poll that never answered, which the copy has no part in", () => {
    expect(render({ kind: "failed" }, false)).not.toContain(CLEAR);
  });

  test("shows it is busy while it runs, and says why when it fails, offering it again", () => {
    expect(
      render(SHOWN, false, { state: { kind: "clearing" }, clear: noop }),
    ).toContain("Clearing the cache and reloading…");
    const failed = render(SHOWN, false, {
      state: { kind: "failed", message: "the worker stopped answering" },
      clear: noop,
    });
    expect(failed).toContain(
      "The cache couldn&#x27;t be cleared: the worker stopped answering",
    );
    expect(failed).toContain(CLEAR);
  });

  test("clears the copy when its button is pressed", () => {
    let cleared = 0;
    const clearing: CopyClearing = {
      state: { kind: "idle" },
      clear: () => {
        cleared += 1;
      },
    };
    const button = findElement(
      WorkingDocumentView({
        document: { kind: "copyFailed" },
        unavailable: false,
        clearing,
        onCitation: noop,
      }),
      "button",
    ) as ReactElement<{ onClick: () => void }> | undefined;
    if (button === undefined) throw new Error("no button");
    button.props.onClick();
    expect(cleared).toBe(1);
  });
});

describe("the working document's pane menu", () => {
  const ctx = (
    workingDocument: WorkingDocumentState,
    clearCopy: CopyClearing | null = IDLE,
  ): RenderContext => ({
    events: [],
    workingDocument,
    documentSignal: {
      analysisId: "an_1",
      tip: { kind: "commit", commit: "9e27".padEnd(40, "0") },
      pollFailed: false,
      unavailable: false,
      damaged: workingDocument.kind === "damaged",
    },
    documentVersions: null,
    clearCopy,
    highlight: undefined,
    onCitation: noop,
    patch: noop,
  });
  const items = (context: RenderContext) =>
    REGISTRY["working-doc"].menuItems?.({}, context) ?? [];

  test("offers to clear the cache, saying everything saved stays", () => {
    for (const state of [SHOWN, { kind: "copyFailed" } as const]) {
      const [item] = items(ctx(state));
      expect(item?.label).toBe(CLEAR);
      expect(item?.description).toBe(
        "Clears this workspace from your browser's cache and loads it fresh. Everything saved stays as it is.",
      );
    }
  });

  test("does not offer it over a damaged workspace, or while a clear runs", () => {
    expect(items(ctx({ kind: "damaged" }))).toEqual([]);
    expect(
      items(ctx(SHOWN, { state: { kind: "clearing" }, clear: noop })),
    ).toEqual([]);
  });

  test("clears the copy when its item is chosen", () => {
    let cleared = 0;
    const [item] = items(
      ctx(SHOWN, {
        state: { kind: "idle" },
        clear: () => {
          cleared += 1;
        },
      }),
    );
    item?.onSelect();
    expect(cleared).toBe(1);
  });
});
