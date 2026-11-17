import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  test,
} from "bun:test";
import { install } from "../minidom.test-support";
import type { WorkspaceModelController } from "./use-workspace-model";

// The controller persists only the workbench's arrangement in IndexedDB and validates every record
// coming back out of the store before dispatching into the reducer, so a stale or malformed entry
// leaves the reducer's defaults standing.

const kv = new Map<string, unknown>();
const openDbs: { onversionchange: (() => void) | null }[] = [];

function installFakeIndexedDb(): () => void {
  const global = globalThis as Record<string, unknown>;
  const previous = Object.getOwnPropertyDescriptor(global, "indexedDB");
  const fake = {
    open() {
      const db = {
        objectStoreNames: { contains: () => true },
        createObjectStore() {},
        close() {},
        onversionchange: null as (() => void) | null,
        transaction() {
          const tx = {
            oncomplete: null as (() => void) | null,
            onerror: null as (() => void) | null,
            onabort: null as (() => void) | null,
            objectStore() {
              return {
                get(key: string) {
                  const req = {
                    result: kv.get(key),
                    onsuccess: null as (() => void) | null,
                    onerror: null as (() => void) | null,
                  };
                  queueMicrotask(() => req.onsuccess?.());
                  return req;
                },
                put(value: unknown, key: string) {
                  kv.set(key, value);
                  queueMicrotask(() => tx.oncomplete?.());
                },
              };
            },
          };
          return tx;
        },
      };
      openDbs.push(db);
      const req = {
        result: db,
        onupgradeneeded: null as (() => void) | null,
        onsuccess: null as (() => void) | null,
        onerror: null as (() => void) | null,
        onblocked: null as (() => void) | null,
      };
      queueMicrotask(() => req.onsuccess?.());
      return req;
    },
  };
  Object.defineProperty(global, "indexedDB", {
    value: fake,
    configurable: true,
    writable: true,
  });
  return () => {
    for (const db of openDbs.splice(0)) db.onversionchange?.();
    if (previous === undefined) delete global.indexedDB;
    else Object.defineProperty(global, "indexedDB", previous);
  };
}

let dom: ReturnType<typeof install>;
let uninstallIdb: () => void;
let React: typeof import("react");
let createRoot: typeof import("react-dom/client").createRoot;
let useWorkspaceModelController: typeof import("./use-workspace-model").useWorkspaceModelController;
const unmounts: (() => void)[] = [];

beforeAll(async () => {
  dom = install();
  uninstallIdb = installFakeIndexedDb();
  React = await import("react");
  ({ createRoot } = await import("react-dom/client"));
  ({ useWorkspaceModelController } = await import("./use-workspace-model"));
});

afterEach(() => {
  while (unmounts.length > 0) unmounts.pop()?.();
  kv.clear();
});

afterAll(() => {
  uninstallIdb();
  dom.uninstall();
});

function ControllerProbe({
  onController,
}: {
  onController: (controller: WorkspaceModelController) => void;
}): null {
  onController(useWorkspaceModelController());
  return null;
}

async function mountController(): Promise<() => WorkspaceModelController> {
  let latest: WorkspaceModelController | null = null;
  const container = dom.document.createElement("div");
  dom.document.body.appendChild(container);
  const root = createRoot(container as unknown as Element);
  unmounts.push(() => {
    React.act(() => root.unmount());
    dom.document.body.removeChild(container);
  });
  await React.act(async () => {
    root.render(
      React.createElement(ControllerProbe, {
        onController: (controller) => {
          latest = controller;
        },
      }),
    );
  });
  return () => {
    if (latest === null) throw new Error("controller not mounted");
    return latest;
  };
}

// Values a store can hand back that no validator should accept.
const FOREIGN: readonly [string, unknown][] = [
  ["null", null],
  ["undefined", undefined],
  ["an object", {}],
  ["an array", []],
  ["an empty string", ""],
  ["zero", 0],
  ["false", false],
];

describe("conversation-edge persistence", () => {
  test.each(["right", "top", "bottom"] as const)(
    "writes and rehydrates %s edge across mounts",
    async (edge) => {
      const first = await mountController();
      await React.act(async () => {
        first().setConversationEdge(edge);
      });

      const second = await mountController();
      expect(second().state.conversation.edge).toBe(edge);
    },
  );

  test("overwrites a docked edge back to left and rehydrates left", async () => {
    const first = await mountController();
    await React.act(async () => {
      first().setConversationEdge("right");
    });
    await React.act(async () => {
      first().setConversationEdge("left");
    });

    const second = await mountController();
    expect(second().state.conversation.edge).toBe("left");
  });

  test.each([...FOREIGN, ["an unknown string", "sideways"], ["a number", 1]])(
    "ignores %s and keeps the default edge",
    async (_label, raw) => {
      kv.set("workbench:conversationEdge", raw);
      const getController = await mountController();
      expect(getController().state.conversation.edge).toBe("left");
    },
  );
});

describe("ratio persistence", () => {
  test.each([
    [0.2, 0.3, 0.7],
    [0.33, 0.4, 0.6],
    [0.75, 0.65, 0.25],
  ] as const)(
    "writes and rehydrates inner ratio %d and orientation ratios %d / %d",
    async (inner, ratioH, ratioV) => {
      const first = await mountController();
      await React.act(async () => {
        first().setSplitRatio("main", inner);
        first().writeOuterRatio("horizontal", ratioH);
        first().writeOuterRatio("vertical", ratioV);
      });

      const second = await mountController();
      expect(second().state.windows[0].splitRatio).toBe(inner);
      expect(await second().readOuterRatio("horizontal")).toBe(ratioH);
      expect(await second().readOuterRatio("vertical")).toBe(ratioV);
    },
  );

  test.each([
    ...FOREIGN,
    ["the upper bound 1", 1],
    ["a negative fraction", -0.2],
    ["a fraction above 1", 1.5],
    ["NaN", Number.NaN],
    ["Infinity", Number.POSITIVE_INFINITY],
    ["a numeric string", "0.5"],
  ])("rejects %s as a ratio", async (_label, raw) => {
    kv.set("workbench:innerRatio", raw);
    kv.set("workbench:ratioH", raw);
    const getController = await mountController();
    expect(getController().state.windows[0].splitRatio).toBe(0.5);
    expect(await getController().readOuterRatio("horizontal")).toBeNull();
  });
});

describe("strip label-mode persistence", () => {
  test("writes and rehydrates both boolean label modes", async () => {
    const first = await mountController();
    await React.act(async () => {
      first().setLabel("main", "a", true);
      first().setLabel("main", "b", false);
    });

    const second = await mountController();
    expect(second().state.labels).toEqual({
      "main:a": true,
      "main:b": false,
    });
  });

  test.each([
    ["null", null],
    ["undefined", undefined],
    ["an object", {}],
    ["an array", []],
    ["an empty string", ""],
    ["a string", "titles"],
    ["a number", 1],
  ])("ignores %s as a label mode", async (_label, raw) => {
    kv.set("workbench:tabLabels:a", raw);
    const getController = await mountController();
    expect(getController().state.labels).toEqual({});
  });
});
