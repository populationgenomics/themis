import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import type { ReactElement } from "react";
import { findAll, install, MiniElement } from "../minidom.test-support";

// A tooltip's text reaches a screen reader without the panel ever opening: a reading cursor moves
// through the page without hovering or focusing what it reads.

let dom: ReturnType<typeof install>;
let React: typeof import("react");
let createRoot: typeof import("react-dom/client").createRoot;
let Tooltip: typeof import("./tooltip").Tooltip;

beforeAll(async () => {
  dom = install();
  React = await import("react");
  ({ createRoot } = await import("react-dom/client"));
  ({ Tooltip } = await import("./tooltip"));
});

afterAll(() => {
  dom.uninstall();
});

async function mount(element: ReactElement): Promise<MiniElement> {
  const container = dom.document.createElement("div");
  dom.document.body.appendChild(container);
  const root = createRoot(container as unknown as Element);
  await React.act(async () => root.render(element));
  return container;
}

function byTag(root: MiniElement, tag: string): MiniElement {
  const [found] = findAll(
    root,
    (node) => node instanceof MiniElement && node.tagName === tag,
  );
  if (!(found instanceof MiniElement)) throw new Error(`no ${tag} drawn`);
  return found;
}

/** The text of the elements `element`'s `aria-describedby` names. */
function description(root: MiniElement, element: MiniElement): string {
  const ids = (element.getAttribute("aria-describedby") ?? "")
    .split(/\s+/)
    .filter((token) => token !== "");
  return ids
    .map((id) => {
      const [named] = findAll(
        root,
        (node) => node instanceof MiniElement && node.getAttribute("id") === id,
      );
      if (named === undefined) throw new Error(`nothing has the id ${id}`);
      return named.textContent;
    })
    .join(" ");
}

describe("Tooltip", () => {
  test("describes a target that takes focus before it is hovered or focused", async () => {
    const root = await mount(
      <Tooltip content="Mark as reviewed">
        <button type="button">Reviewed</button>
      </Tooltip>,
    );
    expect(description(root, byTag(root, "BUTTON"))).toBe("Mark as reviewed");
  });

  test("keeps a description the target already has", async () => {
    const root = await mount(
      <>
        <span id="own">Pinned</span>
        <Tooltip content="Mark as reviewed">
          <button type="button" aria-describedby="own">
            Reviewed
          </button>
        </Tooltip>
      </>,
    );
    expect(description(root, byTag(root, "BUTTON"))).toBe(
      "Pinned Mark as reviewed",
    );
  });

  test("carries the text beside a target that takes no focus", async () => {
    const root = await mount(
      <Tooltip content="Population frequency">
        <span>POP_FRQ</span>
      </Tooltip>,
    );
    expect(root.textContent).toBe("POP_FRQ (Population frequency)");
    expect(byTag(root, "SPAN").getAttribute("aria-describedby")).toBeNull();
  });

  test("adds nothing for text that repeats the target's name", async () => {
    const root = await mount(
      <Tooltip content="Reviewed" describes={false}>
        <button type="button">Reviewed</button>
      </Tooltip>,
    );
    expect(byTag(root, "BUTTON").getAttribute("aria-describedby")).toBeNull();
    expect(root.textContent).toBe("Reviewed");
  });
});
