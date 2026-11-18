import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  jest,
  spyOn,
  test,
} from "bun:test";
import type { ReactElement } from "react";
import { click, findAll, install, MiniElement } from "../minidom.test-support";

const ANALYSIS_ID = "an_test_analysis";
const SESSION_ID = "sesn_test_session";
const REASON = "Write permission denied.";

let dom: ReturnType<typeof install>;
let React: typeof import("react");
let createRoot: typeof import("react-dom/client").createRoot;
let AnalysisIdentifiersMenu: typeof import("./analysis-identifiers-menu").AnalysisIdentifiersMenu;
const unmounts: (() => void)[] = [];

/** What stands in for the browser's clipboard: it records each write, rejects it with `rejection`,
 *  or leaves it unsettled until `hold` resolves. */
let clipboard: {
  written: string[];
  rejection: Error | null;
  hold: Promise<void> | null;
};
let clipboardBefore: PropertyDescriptor | undefined;

beforeAll(async () => {
  dom = install();
  React = await import("react");
  ({ createRoot } = await import("react-dom/client"));
  ({ AnalysisIdentifiersMenu } = await import("./analysis-identifiers-menu"));
  clipboardBefore = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: {
      async writeText(text: string): Promise<void> {
        if (clipboard.hold !== null) await clipboard.hold;
        if (clipboard.rejection !== null) throw clipboard.rejection;
        clipboard.written.push(text);
      },
    },
  });
});

beforeEach(() => {
  clipboard = { written: [], rejection: null, hold: null };
});

afterEach(() => {
  jest.useRealTimers();
  while (unmounts.length > 0) unmounts.pop()?.();
});

afterAll(() => {
  if (clipboardBefore === undefined) {
    delete (navigator as { clipboard?: unknown }).clipboard;
  } else {
    Object.defineProperty(navigator, "clipboard", clipboardBefore);
  }
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

function withAttribute(
  container: MiniElement,
  name: string,
  value: string,
): MiniElement[] {
  return findAll(
    container,
    (node) => node instanceof MiniElement && node.getAttribute(name) === value,
  ) as MiniElement[];
}

function rowsOf(container: MiniElement): MiniElement[] {
  return withAttribute(container, "role", "menuitem");
}

function statusOf(container: MiniElement): string {
  return withAttribute(container, "role", "status")[0].textContent;
}

/** Open the menu and select the row that holds `id`. */
async function copy(container: MiniElement, id: string): Promise<void> {
  const [trigger] = withAttribute(container, "aria-haspopup", "menu");
  await React.act(async () => click(container, trigger));
  const row = rowsOf(container).find((r) => r.textContent.includes(id));
  if (row === undefined) throw new Error(`no row holds ${id}`);
  await React.act(async () => click(container, row));
}

function mountMenu(sessionId: string | null = SESSION_ID) {
  return mount(
    <AnalysisIdentifiersMenu analysisId={ANALYSIS_ID} sessionId={sessionId} />,
  );
}

describe("AnalysisIdentifiersMenu", () => {
  test("shows each identifier whole, so it can be read without copying", async () => {
    const container = await mountMenu();
    const [trigger] = withAttribute(container, "aria-haspopup", "menu");
    await React.act(async () => click(container, trigger));
    expect(rowsOf(container).map((row) => row.textContent)).toEqual([
      expect.stringContaining(ANALYSIS_ID),
      expect.stringContaining(SESSION_ID),
    ]);
  });

  test("offers the Analysis ID alone for a run with no platform session", async () => {
    const container = await mountMenu(null);
    const [trigger] = withAttribute(container, "aria-haspopup", "menu");
    await React.act(async () => click(container, trigger));
    expect(rowsOf(container).map((row) => row.textContent)).toEqual([
      expect.stringContaining(ANALYSIS_ID),
    ]);
  });

  test.each([
    ["the Analysis ID", ANALYSIS_ID],
    ["the session ID", SESSION_ID],
  ])("selecting %s puts it on the clipboard", async (_name, id) => {
    const container = await mountMenu();
    await copy(container, id);
    expect(clipboard.written).toEqual([id]);
  });

  test("confirms a copy beside the menu, naming what was copied", async () => {
    const container = await mountMenu();
    expect(statusOf(container)).toBe("");
    await copy(container, SESSION_ID);
    expect(statusOf(container)).toBe("Copied the session ID");
  });

  test("a confirmation goes away by itself", async () => {
    jest.useFakeTimers();
    const container = await mountMenu();
    await copy(container, SESSION_ID);
    await React.act(async () => jest.advanceTimersByTime(60_000));
    expect(statusOf(container)).toBe("");
  });

  describe("when the browser refuses the copy", () => {
    let logged: ReturnType<typeof spyOn>;

    // The component logs the failure as well; keep it out of the test output.
    beforeEach(() => {
      logged = spyOn(console, "error").mockImplementation(() => {});
      clipboard.rejection = new Error(REASON);
    });

    afterEach(() => {
      logged.mockRestore();
    });

    test("says which copy did not happen", async () => {
      const container = await mountMenu();
      await copy(container, SESSION_ID);
      expect(statusOf(container)).toBe("Couldn't copy the session ID");
    });

    test("carries the browser's reason", async () => {
      const container = await mountMenu();
      await copy(container, SESSION_ID);
      expect(container.textContent).toContain(REASON);
    });

    test("the failure stays until the next copy", async () => {
      jest.useFakeTimers();
      const container = await mountMenu();
      await copy(container, SESSION_ID);
      await React.act(async () => jest.advanceTimersByTime(60_000));
      expect(statusOf(container)).toBe("Couldn't copy the session ID");
    });

    test("the failure is taken down while the next copy is under way", async () => {
      const container = await mountMenu();
      await copy(container, SESSION_ID);
      let release: () => void = () => {};
      clipboard.hold = new Promise((resolve) => {
        release = resolve;
      });
      await copy(container, SESSION_ID);
      expect(statusOf(container)).toBe("");
      await React.act(async () => release());
      expect(statusOf(container)).toBe("Couldn't copy the session ID");
    });

    test("a copy that then succeeds replaces the failure and its reason", async () => {
      const container = await mountMenu();
      await copy(container, SESSION_ID);
      clipboard.rejection = null;
      await copy(container, ANALYSIS_ID);
      expect(statusOf(container)).toBe("Copied the Analysis ID");
      expect(container.textContent).not.toContain(REASON);
    });
  });
});
