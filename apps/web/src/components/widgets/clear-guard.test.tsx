import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  test,
} from "bun:test";
import { QueryClient } from "@tanstack/react-query";
import { click, findAll, install, MiniElement } from "../minidom.test-support";
import {
  type ClearCopyGuard,
  ClearCopyGuardContext,
  clearCopy,
} from "../workbench/clear-copy";
import { judgementStore } from "./judgements";
import { useWidgetStates, WidgetStatesProvider } from "./widget-state";

// A clear of an Analysis's copy discards the changes its widgets have not saved yet, so it asks
// first while there are any: how many, in the curator's words, and it goes ahead only on their say.

const COMMIT = "c0".padEnd(40, "0");

let dom: ReturnType<typeof install>;
let React: typeof import("react");
let createRoot: typeof import("react-dom/client").createRoot;
let UnsavedChangesGuard: typeof import("./clear-guard").UnsavedChangesGuard;
const unmounts: (() => void)[] = [];

beforeAll(async () => {
  dom = install();
  React = await import("react");
  ({ createRoot } = await import("react-dom/client"));
  ({ UnsavedChangesGuard } = await import("./clear-guard"));
});

afterEach(() => {
  while (unmounts.length > 0) unmounts.pop()?.();
});

afterAll(() => {
  dom.uninstall();
});

function GuardProbe({
  onMount,
}: {
  onMount: (
    states: ReturnType<typeof useWidgetStates>,
    guard: ClearCopyGuard | null,
  ) => void;
}): null {
  const states = useWidgetStates();
  const guard = React.useContext(ClearCopyGuardContext);
  React.useEffect(() => {
    onMount(states, guard);
  }, [onMount, states, guard]);
  return null;
}

/** Mounts `UnsavedChangesGuard` over widget states holding unsaved ticks by analysis id. */
async function mountGuard(ticksByAnalysis: Record<string, string[]>) {
  let capturedGuard: ClearCopyGuard | null = null;
  let capturedStates: ReturnType<typeof useWidgetStates> | null = null;

  const container = dom.document.createElement("div");
  dom.document.body.appendChild(container);
  const root = createRoot(container as unknown as Element);
  unmounts.push(() => {
    React.act(() => root.unmount());
    dom.document.body.removeChild(container);
  });
  await React.act(async () => {
    root.render(
      <WidgetStatesProvider>
        <UnsavedChangesGuard>
          <GuardProbe
            onMount={(states, guard) => {
              capturedStates = states;
              capturedGuard = guard;
            }}
          />
        </UnsavedChangesGuard>
      </WidgetStatesProvider>,
    );
  });

  if (capturedStates === null) throw new Error("states not mounted");
  const states = capturedStates as ReturnType<typeof useWidgetStates>;
  await React.act(async () => {
    for (const [analysisId, items] of Object.entries(ticksByAnalysis)) {
      const store = states.entry(
        analysisId,
        "assets/c.binpb",
        "checklist",
        () => judgementStore(async () => true),
      );
      for (const [index, itemId] of items.entries()) {
        store.dispatch({
          kind: "made",
          key: `items[${itemId}].checked`,
          seq: index + 1,
          value: true,
          drawnAt: COMMIT,
          judged: new Uint8Array(),
        });
      }
    }
  });

  const resets: string[] = [];
  const client = new QueryClient();
  client.setQueryData(["workspace-document", "an_1", COMMIT], {
    commit: COMMIT,
    value: "# Doc\n",
  });

  return {
    container,
    resets,
    states,
    cachedDocument: () =>
      client.getQueryData(["workspace-document", "an_1", COMMIT]),
    dialogs: () =>
      findAll(
        container,
        (node) => node instanceof MiniElement && node.tagName === "DIALOG",
      ) as MiniElement[],
    startClear: () => {
      if (capturedGuard === null) throw new Error("guard not mounted");
      return clearCopy(
        "an_1",
        {
          guard: capturedGuard,
          reset: async (analysisId) => {
            resets.push(analysisId);
          },
          announce: () => {},
          queryClient: client,
        },
        () => {},
      );
    },
    pressButton: async (label: string) => {
      const [button] = findAll(
        container,
        (node) =>
          node instanceof MiniElement &&
          node.tagName === "BUTTON" &&
          node.textContent === label,
      );
      if (!(button instanceof MiniElement)) {
        throw new Error(`no "${label}" button drawn`);
      }
      expect(button.disabled).toBe(false);
      await React.act(async () => click(container, button));
    },
  };
}

describe("UnsavedChangesGuard", () => {
  test("asks with the count, and cancelled leaves the ticks and the copy as they were", async () => {
    const h = await mountGuard({ an_1: ["ps3", "pm2"] });
    let done: Promise<void> | undefined;
    await React.act(async () => {
      done = h.startClear();
    });

    const [dialog] = h.dialogs();
    expect(dialog?.open).toBe(true);
    expect(dialog?.textContent).toContain("2 unsaved changes");
    expect(dialog?.textContent).toContain("discards them");
    expect(dialog?.textContent).toContain(
      "Everything already saved stays as it is.",
    );

    await h.pressButton("Cancel");
    await done;

    expect(h.dialogs()).toEqual([]);
    expect(h.resets).toEqual([]);
    expect(h.cachedDocument()).toBeDefined();
    expect(h.states.unsaved("an_1")).toBe(2);
  });

  test("goes ahead once the curator chooses to discard a single unsaved change", async () => {
    const h = await mountGuard({ an_1: ["ps3"] });
    let done: Promise<void> | undefined;
    await React.act(async () => {
      done = h.startClear();
    });

    const [dialog] = h.dialogs();
    expect(dialog?.open).toBe(true);
    expect(dialog?.textContent).toContain("1 unsaved change");
    expect(dialog?.textContent).not.toContain("1 unsaved changes");
    expect(dialog?.textContent).toContain("discards it");
    expect(dialog?.textContent).toContain(
      "Everything already saved stays as it is.",
    );

    await h.pressButton("Discard and reload");
    await done;

    expect(h.dialogs()).toEqual([]);
    expect(h.resets).toEqual(["an_1"]);
    expect(h.cachedDocument()).toBeUndefined();
  });

  test("with nothing unsaved asks nothing, and goes ahead", async () => {
    const h = await mountGuard({ an_1: [] });
    await React.act(async () => {
      await h.startClear();
    });

    expect(h.dialogs()).toEqual([]);
    expect(h.resets).toEqual(["an_1"]);
    expect(h.cachedDocument()).toBeUndefined();
  });

  test("counts only its own Analysis's changes", async () => {
    const h = await mountGuard({ an_2: ["ps3"] });
    await React.act(async () => {
      await h.startClear();
    });

    expect(h.dialogs()).toEqual([]);
    expect(h.resets).toEqual(["an_1"]);
    expect(h.states.unsaved("an_2")).toBe(1);
  });
});
