import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  test,
} from "bun:test";
import type { ReactElement } from "react";
import { click, findAll, install, MiniElement } from "../minidom.test-support";
import { documentVersions } from "./working-document";

const commit = (c: string) => c.repeat(40);
// As the copy lists them: newest first.
const VERSIONS = documentVersions([
  { commit: commit("c"), timestamp: 1_727_163_300 },
  { commit: commit("b"), timestamp: 1_727_163_200 },
  { commit: commit("a"), timestamp: 1_727_163_100 },
]);

let dom: ReturnType<typeof install>;
let React: typeof import("react");
let createRoot: typeof import("react-dom/client").createRoot;
let VersionDropdown: typeof import("./version-dropdown").VersionDropdown;
const unmounts: (() => void)[] = [];

beforeAll(async () => {
  dom = install();
  React = await import("react");
  ({ createRoot } = await import("react-dom/client"));
  ({ VersionDropdown } = await import("./version-dropdown"));
});

afterEach(() => {
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

function triggerOf(container: MiniElement): MiniElement {
  const [trigger] = findAll(
    container,
    (node) =>
      node instanceof MiniElement &&
      node.tagName === "BUTTON" &&
      node.getAttribute("aria-label") === "Select document version",
  );
  if (!(trigger instanceof MiniElement)) throw new Error("no version trigger");
  return trigger;
}

function rowsOf(container: MiniElement): MiniElement[] {
  return findAll(
    container,
    (node) =>
      node instanceof MiniElement &&
      node.getAttribute("role") === "menuitemradio",
  ) as MiniElement[];
}

describe("VersionDropdown", () => {
  test("names a listed commit on the trigger by its version", async () => {
    const container = await mount(
      <VersionDropdown
        versions={VERSIONS}
        selected={commit("a")}
        onSelect={() => {}}
      />,
    );
    expect(triggerOf(container).textContent).toBe("v1");
  });

  test("names a commit the list does not hold by its abbreviated id", async () => {
    const container = await mount(
      <VersionDropdown
        versions={VERSIONS}
        selected={commit("d")}
        onSelect={() => {}}
      />,
    );
    expect(triggerOf(container).textContent).toBe("ddddddd");
  });

  test("lists every recorded tip newest first, numbered from the oldest, with only the shown commit checked", async () => {
    const container = await mount(
      <VersionDropdown
        versions={VERSIONS}
        selected={commit("b")}
        onSelect={() => {}}
      />,
    );
    const trigger = triggerOf(container);
    expect(trigger.disabled).toBe(false);
    await React.act(async () => click(container, trigger));

    const rows = rowsOf(container);
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toMatch(/^v3.+2024/);
    expect(rows[1].textContent).toMatch(/^v2.+2024/);
    expect(rows[2].textContent).toMatch(/^v1.+2024/);
    expect(rows.map((r) => r.getAttribute("aria-checked"))).toEqual([
      "false",
      "true",
      "false",
    ]);
  });

  test.each([
    ["the tip", 0, null],
    ["v2", 1, commit("b")],
    ["v1", 2, commit("a")],
  ] as const)(
    "selecting %s reports its target",
    async (_label, index, expected) => {
      const chosen: (string | null)[] = [];
      const container = await mount(
        <VersionDropdown
          versions={VERSIONS}
          selected={commit("c")}
          onSelect={(c) => chosen.push(c)}
        />,
      );
      await React.act(async () => click(container, triggerOf(container)));
      const row = rowsOf(container)[index];
      expect(row.disabled).toBe(false);
      await React.act(async () => click(container, row));
      expect(chosen).toEqual([expected]);
    },
  );

  test("a single version offers one checked follow-the-branch row", async () => {
    const chosen: (string | null)[] = [];
    const container = await mount(
      <VersionDropdown
        versions={VERSIONS.slice(0, 1)}
        selected={commit("c")}
        onSelect={(c) => chosen.push(c)}
      />,
    );
    await React.act(async () => click(container, triggerOf(container)));

    const rows = rowsOf(container);
    expect(rows).toHaveLength(1);
    expect(rows[0].getAttribute("aria-checked")).toBe("true");
    expect(rows[0].disabled).toBe(false);
    await React.act(async () => click(container, rows[0]));
    expect(chosen).toEqual([null]);
  });
});
