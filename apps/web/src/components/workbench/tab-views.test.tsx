import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  test,
} from "bun:test";
import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { workspaceCopy } from "@/workspace-copy/client";
import { click, findAll, install, MiniElement } from "../minidom.test-support";
import type { CopyClearing } from "./clear-copy";
import { WorkingDocumentView } from "./tab-views";
import type { WorkspaceModelController } from "./use-workspace-model";
import type { WorkingDocumentState } from "./working-document";
import { INITIAL_WORKSPACE_STATE } from "./workspace-model";
import type {
  CrossWindowDrag,
  WindowActions,
  WorkingDocumentSignal,
} from "./workspace-sync";

// What a curator sees of the working document while the Poll cannot read the workspace: what the
// tab showed stays, and a notice says it may be out of date.

const NOTICE = "The workspace can&#x27;t be reached right now";
const TIP = "9e27".padEnd(40, "0");
const SHOWN: WorkingDocumentState = {
  kind: "shown",
  markdown: "# Variant summary\n\nPS3 applies.\n",
  commit: TIP,
  pinned: false,
};
const noop = () => {};

const IDLE: CopyClearing = { state: { kind: "idle" }, clear: noop };

let dom: ReturnType<typeof install>;
let React: typeof import("react");
let createRoot: typeof import("react-dom/client").createRoot;
let QueryClient: typeof import("@tanstack/react-query").QueryClient;
let QueryClientProvider: typeof import("@tanstack/react-query").QueryClientProvider;
let Pane: typeof import("./pane").Pane;
let WorkspaceDataProvider: typeof import("./workspace-context").WorkspaceDataProvider;
const realReset = workspaceCopy.reset;
const unmounts: (() => void)[] = [];

beforeAll(async () => {
  dom = install();
  React = await import("react");
  ({ createRoot } = await import("react-dom/client"));
  ({ QueryClient, QueryClientProvider } = await import(
    "@tanstack/react-query"
  ));
  ({ Pane } = await import("./pane"));
  ({ WorkspaceDataProvider } = await import("./workspace-context"));
});

afterEach(() => {
  workspaceCopy.reset = realReset;
  while (unmounts.length > 0) unmounts.pop()?.();
});

afterAll(() => {
  dom.uninstall();
});

async function mount(element: ReactElement): Promise<MiniElement> {
  const container = dom.document.createElement("div");
  dom.document.body.appendChild(container);
  const root = createRoot(container as unknown as Element);
  unmounts.push(() => {
    React.act(() => root.unmount());
    dom.document.body.removeChild(container);
  });
  await React.act(async () => root.render(element));
  return container;
}

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
      signal={{
        analysisId: "an_1",
        tip: { kind: "commit", commit: TIP },
        pollFailed: false,
        unavailable,
        damaged: false,
      }}
      curatorEmail="curator@example.org"
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

  test("clears the copy when its button is pressed", async () => {
    let cleared = 0;
    const clearing: CopyClearing = {
      state: { kind: "idle" },
      clear: () => {
        cleared += 1;
      },
    };
    const container = await mount(
      <WorkingDocumentView
        document={{ kind: "copyFailed" }}
        unavailable={false}
        clearing={clearing}
        signal={null}
        curatorEmail="curator@example.org"
        onCitation={noop}
      />,
    );
    const [button] = findAll(
      container,
      (node) =>
        node instanceof MiniElement &&
        node.tagName === "BUTTON" &&
        node.textContent === CLEAR,
    );
    if (!(button instanceof MiniElement)) throw new Error("no clear button");
    expect(button.disabled).toBe(false);
    await React.act(async () => click(container, button));
    expect(cleared).toBe(1);
  });
});

const controller: WorkspaceModelController = {
  state: INITIAL_WORKSPACE_STATE,
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

async function mountWorkingDocPane(workingDocument: WorkingDocumentState) {
  const win = INITIAL_WORKSPACE_STATE.windows[0];
  const signal: WorkingDocumentSignal = {
    analysisId: "an_1",
    tip: { kind: "commit", commit: TIP },
    pollFailed: false,
    unavailable: false,
    damaged: workingDocument.kind === "damaged",
  };
  const container = await mount(
    <QueryClientProvider client={new QueryClient()}>
      <WorkspaceDataProvider
        value={{
          events: [],
          workingDocument,
          documentSignal: signal,
          documentVersions: null,
          curatorEmail: "curator@example.org",
        }}
      >
        <Pane
          win={win}
          pane={win.panes[0]}
          side="a"
          controller={controller}
          windowActions={windowActions}
          crossWindowDrag={crossWindowDrag}
          onCitation={noop}
          drag={{ active: false, begin: noop, end: noop }}
        />
      </WorkspaceDataProvider>
    </QueryClientProvider>,
  );

  const openMenu = async (): Promise<MiniElement> => {
    const [trigger] = findAll(
      container,
      (node) =>
        node instanceof MiniElement &&
        node.tagName === "BUTTON" &&
        node.getAttribute("aria-label") === "Pane actions",
    );
    if (!(trigger instanceof MiniElement)) throw new Error("no pane menu");
    expect(trigger.disabled).toBe(false);
    await React.act(async () => click(container, trigger));
    const [menu] = findAll(
      container,
      (node) =>
        node instanceof MiniElement && node.getAttribute("role") === "menu",
    );
    if (!(menu instanceof MiniElement)) throw new Error("menu did not open");
    return menu;
  };

  return { container, openMenu };
}

describe("the working document's pane menu", () => {
  test.each([
    ["the document is shown", SHOWN],
    ["a read of the copy failed", { kind: "copyFailed" } as const],
  ])(
    "offers to clear the cache when %s, saying everything saved stays",
    async (_, state) => {
      const { openMenu } = await mountWorkingDocPane(state);
      const menu = await openMenu();
      const [item] = findAll(
        menu,
        (node) =>
          node instanceof MiniElement &&
          node.getAttribute("role") === "menuitem" &&
          node.textContent.includes(CLEAR),
      );
      expect(item?.textContent).toBe(
        `${CLEAR}Clears this workspace from your browser's cache and loads it fresh. Everything saved stays as it is.`,
      );
    },
  );

  test("does not offer to clear the cache over a damaged workspace", async () => {
    const { openMenu } = await mountWorkingDocPane({ kind: "damaged" });
    const menu = await openMenu();
    expect(menu.textContent).toContain("Show tab titles");
    expect(menu.textContent).not.toContain(CLEAR);
  });

  test("clears the copy when its item is chosen, and hides the item while the clear runs", async () => {
    const resets: string[] = [];
    let finishReset: (() => void) | undefined;
    workspaceCopy.reset = (analysisId) => {
      resets.push(analysisId);
      return new Promise<undefined>((resolve) => {
        finishReset = () => resolve(undefined);
      });
    };

    const { container, openMenu } = await mountWorkingDocPane(SHOWN);
    const menu = await openMenu();
    const [item] = findAll(
      menu,
      (node) =>
        node instanceof MiniElement &&
        node.getAttribute("role") === "menuitem" &&
        node.textContent.includes(CLEAR),
    );
    if (!(item instanceof MiniElement)) throw new Error("no clear menu item");
    expect(item.disabled).toBe(false);
    await React.act(async () => click(container, item));

    expect(resets).toEqual(["an_1"]);
    expect(container.textContent).toContain(
      "Clearing the cache and reloading…",
    );

    const runningMenu = await openMenu();
    expect(runningMenu.textContent).toContain("Show tab titles");
    expect(runningMenu.textContent).not.toContain(CLEAR);

    await React.act(async () => {
      finishReset?.();
    });
    expect(container.textContent).not.toContain("Clearing the cache");
    expect(container.textContent).not.toContain("couldn't be cleared");
    expect(container.textContent).toContain("Variant summary");
  });
});
