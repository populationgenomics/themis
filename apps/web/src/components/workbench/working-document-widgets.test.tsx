import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  test,
} from "bun:test";
import { create, toBinary } from "@bufbuild/protobuf";
import { AnySchema, anyPack, StructSchema } from "@bufbuild/protobuf/wkt";
import type { ReactElement } from "react";
import { ChecklistSchema } from "@/models/widgets";
import {
  PublishUnconfirmedError,
  workspaceCopy,
} from "@/workspace-copy/client";
import type { EditOutcome } from "@/workspace-copy/publish";
import {
  click,
  findAll,
  install,
  type MiniElement,
} from "../minidom.test-support";
import { CHANGED_SINCE_SHOWN, UNCONFIRMED_NOTICE } from "../widgets/judgements";
import type { RegisteredWidget } from "../widgets/registry";
import { WidgetStatesProvider } from "../widgets/widget-state";
import type { WorkingDocumentSignal } from "./workspace-sync";

// The working document's widgets mounted in a DOM and drawn again at other commits, as the tab draws
// them when the tip moves: what a tick shows, and whether it publishes at all, is decided by the
// component tree, which a static render cannot reach.

const PATH = "assets/c.binpb";
const C0 = "c0".padEnd(40, "0");
const C1 = "c1".padEnd(40, "0");
const C2 = "c2".padEnd(40, "0");
/** The agent's commit on C0, which leaves the checklist as it is. */
const CA = "ca".padEnd(40, "0");
const OTHER = "assets/other.binpb";
const MARKDOWN = `# Doc\n\n::embed[${PATH}]\n`;
/** The document with another embed above the checklist's, which moves the checklist's element. */
const INSERTED = `# Doc\n\n::embed[${OTHER}]\n\n::embed[${PATH}]\n`;
const OTHER_ASSET = toBinary(
  AnySchema,
  anyPack(
    ChecklistSchema,
    create(ChecklistSchema, { items: [{ id: "z", label: "another" }] }),
  ),
);
const ASSET = toBinary(
  AnySchema,
  anyPack(
    ChecklistSchema,
    create(ChecklistSchema, { items: [{ id: "a", label: "PM2 applies" }] }),
  ),
);
const TICKED_ASSET = toBinary(
  AnySchema,
  anyPack(
    ChecklistSchema,
    create(ChecklistSchema, {
      items: [{ id: "a", label: "PM2 applies", checked: true }],
    }),
  ),
);

let dom: ReturnType<typeof install>;
let React: typeof import("react");
let createRoot: typeof import("react-dom/client").createRoot;
let QueryClient: typeof import("@tanstack/react-query").QueryClient;
let QueryClientProvider: typeof import("@tanstack/react-query").QueryClientProvider;
let WorkingDocumentView: typeof import("./tab-views").WorkingDocumentView;
let documentState: typeof import("./working-document").documentState;
let REGISTRY: typeof import("./content-kinds").REGISTRY;
const realPublish = workspaceCopy.publish;
const realSync = workspaceCopy.sync;
const realIsAncestor = workspaceCopy.isAncestor;

beforeAll(async () => {
  dom = install();
  React = await import("react");
  ({ createRoot } = await import("react-dom/client"));
  ({ QueryClient, QueryClientProvider } = await import(
    "@tanstack/react-query"
  ));
  ({ WorkingDocumentView } = await import("./tab-views"));
  ({ documentState } = await import("./working-document"));
  ({ REGISTRY } = await import("./content-kinds"));
});

afterEach(() => {
  workspaceCopy.publish = realPublish;
  workspaceCopy.sync = realSync;
  workspaceCopy.isAncestor = realIsAncestor;
});

afterAll(() => {
  dom.uninstall();
});

/** A mounted working document whose asset reads come from `files`, by commit and then path (the
 *  checklist's own path when a commit maps to bytes alone), and whose copy answers ancestry from
 *  `parents`, each commit's parents; by default each commit in `files` is the next one's parent.
 *  With `header`, the tab's header accessory is drawn above it, following the tip. */
function mount(
  files: Record<string, Uint8Array | Record<string, Uint8Array>>,
  parents: Record<string, string[]> = inLine(Object.keys(files)),
  { header = false }: { header?: boolean } = {},
) {
  const reaches = (ancestor: string, descendant: string): boolean => {
    if (ancestor === descendant) return true;
    const above = parents[descendant];
    if (above === undefined) throw new Error(`the copy holds no ${descendant}`);
    return above.some((parent) => reaches(ancestor, parent));
  };
  workspaceCopy.isAncestor = async (_analysisId, ancestor, descendant) =>
    reaches(ancestor, descendant);
  const client = new QueryClient();
  for (const [commit, entry] of Object.entries(files)) {
    const byPath = entry instanceof Uint8Array ? { [PATH]: entry } : entry;
    for (const [path, bytes] of Object.entries(byPath)) {
      client.setQueryData(["workspace-file", "an_1", commit, path], {
        commit,
        value: { bytes, mode: "100644" },
      });
    }
  }
  const container = dom.document.createElement("div");
  dom.document.body.appendChild(container);
  const root = createRoot(container as unknown as Element);
  const view = (
    shown: {
      commit: string;
      tip: string;
      markdown: string;
      pinned: boolean;
      damaged: boolean;
    } | null,
  ): ReactElement => {
    if (shown === null) {
      return (
        <QueryClientProvider client={client}>
          <WidgetStatesProvider>
            <p>another tab</p>
          </WidgetStatesProvider>
        </QueryClientProvider>
      );
    }
    const signal: WorkingDocumentSignal = {
      analysisId: "an_1",
      tip: { kind: "commit", commit: shown.tip },
      pollFailed: false,
      unavailable: false,
      damaged: shown.damaged,
    };
    // The state the pane draws, decided from the signal as the tab decides it.
    const document = documentState(signal, {
      isError: false,
      error: null,
      data: {
        commit: shown.commit,
        value: shown.markdown,
        pinned: shown.pinned,
      },
    });
    return (
      <QueryClientProvider client={client}>
        <WidgetStatesProvider>
          {header &&
            REGISTRY["working-doc"].headerAccessory?.(
              { pin: null },
              {
                events: [],
                workingDocument: document,
                documentSignal: signal,
                documentVersions: [
                  { commit: shown.tip, number: 1, timestamp: 0 },
                ],
                clearCopy: null,
                curatorEmail: "curator@example.org",
                highlight: undefined,
                onCitation: () => {},
                patch: () => {},
              },
            )}
          <WorkingDocumentView
            document={document}
            unavailable={false}
            clearing={null}
            signal={signal}
            curatorEmail="curator@example.org"
            onCitation={() => {}}
          />
        </WidgetStatesProvider>
      </QueryClientProvider>
    );
  };
  const settle = async () => {
    for (let i = 0; i < 3; i += 1) {
      await React.act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 10));
      });
    }
  };
  return {
    container,
    render: async (
      commit: string,
      tip = commit,
      markdown = MARKDOWN,
      pinned = false,
      damaged = false,
    ) => {
      await React.act(async () =>
        root.render(view({ commit, tip, markdown, pinned, damaged })),
      );
      await settle();
    },
    away: async () => {
      await React.act(async () => root.render(view(null)));
      await settle();
    },
    tick: async (index = 0) => {
      const box = checkboxes()[index];
      await React.act(async () => click(container, box));
      await settle();
    },
    checkboxes,
    unmount: () => React.act(() => root.unmount()),
  };
  function checkboxes(): MiniElement[] {
    return findAll(
      container,
      (node) => (node as MiniElement).tagName === "INPUT",
    ) as MiniElement[];
  }
}

function inLine(commits: readonly string[]): Record<string, string[]> {
  return Object.fromEntries(
    commits.map((commit, index) => [
      commit,
      index === 0 ? [] : [commits[index - 1]],
    ]),
  );
}

function checklistBytes(
  ...items: { id: string; label: string; checked?: boolean }[]
): Uint8Array {
  return toBinary(
    AnySchema,
    anyPack(ChecklistSchema, create(ChecklistSchema, { items })),
  );
}

/** Stand in for the SharedWorker's publish, answering each call when the test says. */
function deferredPublish() {
  const bases: string[] = [];
  const answers: ((outcome: EditOutcome) => void)[] = [];
  workspaceCopy.publish = (request) => {
    bases.push(request.base);
    return new Promise((resolve) => answers.push(resolve));
  };
  const answer = (index: number, outcome: EditOutcome) =>
    React.act(async () => answers[index](outcome));
  return { bases, answer };
}

/** Stand in for the SharedWorker's publish, recording each call. */
function publishing(outcome: EditOutcome): unknown[] {
  const calls: unknown[] = [];
  workspaceCopy.publish = async (request) => {
    calls.push(request);
    return outcome;
  };
  return calls;
}

describe("a checklist in the working document", () => {
  test("keeps a tick's notice when the tip moves and it is drawn at the new commit", async () => {
    const calls = publishing({ kind: "fileChanged", commit: C1, path: PATH });
    const doc = mount({ [C0]: ASSET, [C1]: ASSET });
    await doc.render(C0);
    const [box] = doc.checkboxes();
    await doc.tick();
    expect(calls).toHaveLength(1);
    expect(doc.container.textContent).toContain(
      `Not saved: ${CHANGED_SINCE_SHOWN}`,
    );

    await doc.render(C1);

    expect(doc.checkboxes()[0]).toBe(box);
    expect(doc.container.textContent).toContain(
      `Not saved: ${CHANGED_SINCE_SHOWN}`,
    );
    await doc.unmount();
  });

  test("drawn at a commit behind the tip, publishes nothing", async () => {
    const calls = publishing({
      kind: "landed",
      commit: C1,
      generation: BigInt(1),
    });
    const doc = mount({ [C0]: ASSET });
    await doc.render(C0, C1);
    await doc.tick();
    expect(calls).toHaveLength(0);
    await doc.unmount();
  });

  test("drawn from the previous commit's asset while the tip's loads, publishes nothing", async () => {
    const calls = publishing({
      kind: "landed",
      commit: C1,
      generation: BigInt(1),
    });
    workspaceCopy.sync = () => new Promise(() => {});
    const doc = mount({ [C0]: ASSET });
    await doc.render(C0);
    await doc.render(C1);
    expect(doc.checkboxes()).toHaveLength(1);
    await doc.tick();
    expect(calls).toHaveLength(0);
    await doc.unmount();
  });

  test("shows a tick as saving, and checked, at once", async () => {
    workspaceCopy.publish = () => new Promise(() => {});
    const doc = mount({ [C0]: ASSET });
    await doc.render(C0);
    await doc.tick();
    expect(doc.checkboxes()[0].checked).toBe(true);
    expect(doc.checkboxes()[0].hasAttribute("disabled")).toBe(true);
    expect(doc.container.textContent).toContain("saving…");
    await doc.unmount();
  });

  test("keeps a tick's notice when an embed is inserted above it", async () => {
    publishing({ kind: "fileChanged", commit: C1, path: PATH });
    const doc = mount({
      [C0]: ASSET,
      [C1]: { [PATH]: ASSET, [OTHER]: OTHER_ASSET },
    });
    await doc.render(C0);
    const [box] = doc.checkboxes();
    await doc.tick();

    await doc.render(C1, C1, INSERTED);

    expect(doc.checkboxes()).toHaveLength(2);
    expect(doc.checkboxes()).not.toContain(box);
    expect(doc.container.textContent).toContain(
      `Not saved: ${CHANGED_SINCE_SHOWN}`,
    );
    await doc.unmount();
  });

  test("keeps a publishing tick through an embed inserted above it, and shows it saved when it lands", async () => {
    let answer: (outcome: EditOutcome) => void = () => {};
    workspaceCopy.publish = () =>
      new Promise((resolve) => {
        answer = resolve;
      });
    const doc = mount({
      [C0]: ASSET,
      [C1]: { [PATH]: ASSET, [OTHER]: OTHER_ASSET },
      [C2]: { [PATH]: TICKED_ASSET, [OTHER]: OTHER_ASSET },
    });
    await doc.render(C0);
    await doc.tick();

    await doc.render(C1, C1, INSERTED);
    expect(doc.container.textContent).toContain("saving…");
    await React.act(async () =>
      answer({ kind: "landed", commit: C2, generation: BigInt(2) }),
    );
    await doc.render(C1, C1, INSERTED);

    const box = doc.checkboxes()[1];
    expect(box.checked).toBe(true);
    expect(box.hasAttribute("disabled")).toBe(false);
    expect(doc.container.textContent).toContain("saved");
    expect(
      findAll(
        doc.container,
        (node) => (node as MiniElement).getAttribute?.("role") === "alert",
      ),
    ).toEqual([]);
    await doc.unmount();
  });

  test("keeps an unconfirmed tick's notice through a switch to another tab and back", async () => {
    workspaceCopy.publish = async () => {
      throw new PublishUnconfirmedError("the worker was lost");
    };
    const doc = mount({ [C0]: ASSET });
    await doc.render(C0);
    await doc.tick();
    expect(doc.container.textContent).toContain(UNCONFIRMED_NOTICE);

    await doc.away();
    expect(doc.container.textContent).not.toContain(UNCONFIRMED_NOTICE);
    await doc.render(C0);

    expect(doc.container.textContent).toContain(UNCONFIRMED_NOTICE);
    await doc.unmount();
  });

  test("shows no tick over a pinned version, saving or saved, and takes none there", async () => {
    let answer: (outcome: EditOutcome) => void = () => {};
    workspaceCopy.publish = () =>
      new Promise((resolve) => {
        answer = resolve;
      });
    const doc = mount({ [C0]: ASSET, [C1]: TICKED_ASSET });
    await doc.render(C0);
    await doc.tick();

    await doc.render(C0, C0, MARKDOWN, true);
    expect(doc.checkboxes()[0].checked).toBe(false);
    expect(doc.checkboxes()[0].hasAttribute("disabled")).toBe(true);
    expect(doc.container.textContent).not.toContain("saving");
    await React.act(async () =>
      answer({ kind: "landed", commit: C1, generation: BigInt(2) }),
    );
    await doc.render(C0, C1, MARKDOWN, true);
    expect(doc.checkboxes()[0].checked).toBe(false);
    expect(doc.container.textContent).not.toContain("saved");

    // The same version, followed from the tip rather than pinned, shows the tick it lacks.
    await doc.render(C0, C1);
    expect(doc.checkboxes()[0].checked).toBe(true);
    await doc.unmount();
  });

  test("takes no tick while the pane shows the workspace damaged, and claims no save there", async () => {
    const doc = mount({ [C0]: ASSET }, undefined, { header: true });
    await doc.render(C0);
    expect(doc.checkboxes()).toHaveLength(1);
    expect(doc.container.textContent).toContain("Saved");

    await doc.render(C0, C0, MARKDOWN, false, true);
    expect(doc.checkboxes()).toHaveLength(0);
    expect(doc.container.textContent).toContain("This workspace is damaged");
    expect(doc.container.textContent).not.toContain("Saved");

    // A tick that reads the ref document again draws the checklist again.
    await doc.render(C0);
    expect(doc.checkboxes()).toHaveLength(1);
    await doc.unmount();
  });

  test("shows a tick in every embed of its asset, and publishes it once", async () => {
    const published: unknown[] = [];
    workspaceCopy.publish = (edit) => {
      published.push(edit);
      return new Promise(() => {});
    };
    const doc = mount({ [C0]: ASSET });
    await doc.render(C0, C0, `${MARKDOWN}\n::embed[${PATH}]\n`);
    await doc.tick(0);

    expect(doc.checkboxes().map((box) => box.checked)).toEqual([true, true]);
    expect(doc.checkboxes()[1].hasAttribute("disabled")).toBe(true);
    expect(published).toHaveLength(1);
    await doc.unmount();
  });
});

describe("a tick the copy rebased", () => {
  const landedAs = (commit: string, generation: number): EditOutcome => ({
    kind: "landed",
    commit,
    generation: BigInt(generation),
  });

  test("shows as saved over the agent's commit it rebased over, drawn before the answer, and is taken back from its landing", async () => {
    const { bases, answer } = deferredPublish();
    const doc = mount({ [C0]: ASSET, [CA]: ASSET, [C1]: TICKED_ASSET });
    await doc.render(C0);
    await doc.tick();
    await doc.render(CA);
    expect(doc.container.textContent).toContain("saving…");
    await answer(0, landedAs(C1, 1));
    await doc.render(CA);
    expect(doc.checkboxes()[0].checked).toBe(true);
    expect(doc.container.textContent).toContain("saved");

    await doc.tick();

    expect(bases).toEqual([C0, C1]);
    expect(doc.checkboxes()[0].checked).toBe(false);
    expect(doc.container.textContent).toContain("saving…");
    await doc.unmount();
  });

  test("shows as saved over the agent's commit it rebased over, drawn after the answer", async () => {
    const { bases, answer } = deferredPublish();
    const doc = mount({ [C0]: ASSET, [CA]: ASSET, [C1]: TICKED_ASSET });
    await doc.render(C0);
    await doc.tick();
    await answer(0, landedAs(C1, 1));
    await doc.render(CA);
    expect(doc.checkboxes()[0].checked).toBe(true);
    expect(doc.container.textContent).toContain("saved");

    await doc.tick();

    expect(bases).toEqual([C0, C1]);
    expect(doc.container.textContent).not.toContain("Not saved");
    await doc.unmount();
  });

  test("of two, shows the second over the first's landing drawn after both answered", async () => {
    const { answer } = deferredPublish();
    const doc = mount({
      [C0]: checklistBytes(
        { id: "a", label: "one" },
        { id: "b", label: "two" },
      ),
      [C1]: checklistBytes(
        { id: "a", label: "one", checked: true },
        { id: "b", label: "two" },
      ),
      [C2]: checklistBytes(
        { id: "a", label: "one", checked: true },
        { id: "b", label: "two", checked: true },
      ),
    });
    await doc.render(C0);
    await doc.tick(0);
    await doc.tick(1);
    await answer(0, landedAs(C1, 1));
    await new Promise((resolve) => setTimeout(resolve, 20));
    await answer(1, landedAs(C2, 2));

    await doc.render(C1);

    expect(doc.checkboxes().map((box) => box.checked)).toEqual([true, true]);
    await doc.unmount();
  });

  test("goes once a commit after its landing lost it, the agent having reworded the item", async () => {
    const { answer } = deferredPublish();
    const reworded = checklistBytes({ id: "a", label: "PM2 applies, strong" });
    const doc = mount({ [C0]: ASSET, [C1]: TICKED_ASSET, [C2]: reworded });
    await doc.render(C0);
    await doc.tick();
    await answer(0, landedAs(C1, 1));
    await doc.render(C1);
    expect(doc.container.textContent).toContain("saved");

    await doc.render(C2);

    expect(doc.checkboxes()[0].checked).toBe(false);
    expect(doc.container.textContent).not.toContain("saved");
    await doc.render(C1);
    expect(doc.container.textContent).not.toContain("saved");
    await doc.unmount();
  });
});

describe("a row's status", () => {
  /** The row's marker and notice, read off their elements rather than the row's words. */
  const status = (doc: ReturnType<typeof mount>) => ({
    saved: findAll(
      doc.container,
      (node) =>
        (node as MiniElement).tagName === "SPAN" &&
        node.textContent === "saved",
    ).length,
    notices: findAll(
      doc.container,
      (node) => (node as MiniElement).getAttribute?.("role") === "alert",
    ).map((node) => node.textContent),
  });

  test("after a landed tick, is only the notice of a later tick refused", async () => {
    const { answer } = deferredPublish();
    const doc = mount({ [C0]: ASSET, [C1]: TICKED_ASSET });
    await doc.render(C0);
    await doc.tick();
    await answer(0, { kind: "landed", commit: C1, generation: BigInt(1) });
    await doc.render(C1);
    expect(status(doc)).toEqual({ saved: 1, notices: [] });

    await doc.tick();
    await answer(1, { kind: "fileChanged", commit: C2, path: PATH });
    await doc.render(C1);

    expect(status(doc)).toEqual({
      saved: 0,
      notices: [`Not saved: ${CHANGED_SINCE_SHOWN}`],
    });
    expect(doc.checkboxes()[0].checked).toBe(true);
    await doc.unmount();
  });

  test("after a refused tick, is only saved once a later one lands", async () => {
    const { answer } = deferredPublish();
    const doc = mount({ [C0]: ASSET, [C1]: TICKED_ASSET });
    await doc.render(C0);
    await doc.tick();
    await answer(0, { kind: "fileChanged", commit: C0, path: PATH });
    await doc.render(C0);
    expect(status(doc)).toEqual({
      saved: 0,
      notices: [`Not saved: ${CHANGED_SINCE_SHOWN}`],
    });

    await doc.tick();
    await answer(1, { kind: "landed", commit: C1, generation: BigInt(1) });
    await doc.render(C0);

    expect(status(doc)).toEqual({ saved: 1, notices: [] });
    expect(doc.checkboxes()[0].checked).toBe(true);
    await doc.unmount();
  });
});

describe("the working document's header", () => {
  test("says saving while a tick's publish is in flight, and saved once it lands", async () => {
    const { answer } = deferredPublish();
    const doc = mount({ [C0]: ASSET, [C1]: TICKED_ASSET }, undefined, {
      header: true,
    });
    await doc.render(C0);
    expect(doc.container.textContent).toContain("Saved");

    await doc.tick();
    expect(doc.container.textContent).toContain("Saving…");
    expect(doc.container.textContent).not.toContain("Saved");

    await answer(0, { kind: "landed", commit: C1, generation: BigInt(1) });
    await doc.render(C1);
    expect(doc.container.textContent).toContain("Saved");
    expect(doc.container.textContent).not.toContain("Saving…");
    await doc.unmount();
  });

  test("says saving until the last of two ticks has an outcome", async () => {
    const { answer } = deferredPublish();
    const doc = mount(
      {
        [C0]: checklistBytes(
          { id: "a", label: "one" },
          { id: "b", label: "two" },
        ),
        [C1]: checklistBytes(
          { id: "a", label: "one", checked: true },
          { id: "b", label: "two" },
        ),
      },
      undefined,
      { header: true },
    );
    await doc.render(C0);
    await doc.tick(0);
    await doc.tick(1);

    await answer(0, { kind: "landed", commit: C1, generation: BigInt(1) });
    await doc.render(C1);
    expect(doc.container.textContent).toContain("Saving…");

    await answer(1, { kind: "fileChanged", commit: C1, path: PATH });
    await doc.render(C1);
    expect(doc.container.textContent).toContain("Saved");
    expect(doc.container.textContent).toContain(
      `Not saved: ${CHANGED_SINCE_SHOWN}`,
    );
    await doc.unmount();
  });
});

describe("a widget in the working document", () => {
  test("draws again at a commit after the one it failed to draw at", async () => {
    const { WIDGETS } = await import("../widgets/registry");
    const widgets = WIDGETS as Map<string, RegisteredWidget>;
    function FailsAtC0({ drawnAt }: { drawnAt: string }): ReactElement {
      if (drawnAt === C0) throw new TypeError("a fault at c0");
      return <p>drawn at c1</p>;
    }
    widgets.set(StructSchema.typeName, {
      schema: StructSchema,
      read: () => (context) => <FailsAtC0 drawnAt={context.drawnAt} />,
    });
    const quiet = console.error;
    console.error = () => {};
    try {
      const struct = toBinary(
        AnySchema,
        anyPack(StructSchema, create(StructSchema)),
      );
      const doc = mount({ [C0]: struct, [C1]: struct });
      await doc.render(C0);
      expect(doc.container.textContent).toContain(
        "the widget failed to draw: a fault at c0",
      );

      await doc.render(C1);

      expect(doc.container.textContent).toContain("drawn at c1");
      await doc.unmount();
    } finally {
      console.error = quiet;
      widgets.delete(StructSchema.typeName);
    }
  });
});
