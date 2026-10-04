import { describe, expect, test } from "bun:test";
import { QueryClient } from "@tanstack/react-query";
import type { ReactElement, ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { clearCopy } from "../workbench/clear-copy";
import {
  UnsavedChangesBody,
  unsavedChangesGuard,
  unsavedChangesQuestion,
} from "./clear-guard";
import { judgementStore } from "./judgements";
import { WidgetStates } from "./widget-state";

// A clear of an Analysis's copy discards the changes its widgets have not saved yet, so it asks
// first while there are any: how many, in the curator's words, and it goes ahead only on their say.

const COMMIT = "c0".padEnd(40, "0");

/** Widget states holding one checklist of `an_1` with a tick saving on each of `items`. */
function withTicks(...items: string[]) {
  const states = new WidgetStates();
  const store = states.entry("an_1", "assets/c.binpb", "checklist", () =>
    judgementStore(async () => true),
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
  return { states, store };
}

/** Clear `an_1`'s copy through the widget guard, the curator answering `answer`. */
async function clearWith(states: WidgetStates, answer: boolean) {
  const asked: number[] = [];
  const resets: string[] = [];
  const client = new QueryClient();
  client.setQueryData(["workspace-document", "an_1", COMMIT], {
    commit: COMMIT,
    value: "# Doc\n",
  });
  await clearCopy(
    "an_1",
    {
      guard: unsavedChangesGuard(states, async (unsaved) => {
        asked.push(unsaved);
        return answer;
      }),
      reset: async (analysisId) => {
        resets.push(analysisId);
      },
      announce: () => {},
      queryClient: client,
    },
    () => {},
  );
  const read = client.getQueryData(["workspace-document", "an_1", COMMIT]);
  return { asked, resets, read };
}

describe("the question before a clear", () => {
  test("names how many changes it discards, and that the saved stay", () => {
    expect(unsavedChangesQuestion(1)).toBe(
      "You have 1 unsaved change. Clearing the cache discards it. Everything already saved stays as it is.",
    );
    expect(unsavedChangesQuestion(3)).toBe(
      "You have 3 unsaved changes. Clearing the cache discards them. Everything already saved stays as it is.",
    );
    expect(() => unsavedChangesQuestion(0)).toThrow();
  });

  test("offers to discard and reload, or to cancel", () => {
    const html = renderToStaticMarkup(
      <UnsavedChangesBody question={{ unsaved: 2, answer: () => {} }} />,
    );
    expect(html).toContain("You have 2 unsaved changes.");
    expect(html).toContain("Discard and reload");
    expect(html).toContain("Cancel");
  });

  test("answers as the button pressed", () => {
    const answers: boolean[] = [];
    const body = UnsavedChangesBody({
      question: { unsaved: 1, answer: (discard) => answers.push(discard) },
    });
    for (const label of ["Cancel", "Discard and reload"]) {
      const button = findButton(body, label);
      if (button === undefined) throw new Error(`no ${label} button`);
      button.props.onClick();
    }
    expect(answers).toEqual([false, true]);
  });
});

describe("a clear of a copy whose widgets hold unsaved changes", () => {
  test("asks with their count, and cancelled leaves the ticks and the copy as they were", async () => {
    const { states, store } = withTicks("ps3", "pm2");
    const { asked, resets, read } = await clearWith(states, false);
    expect(asked).toEqual([2]);
    expect(resets).toEqual([]);
    expect(read).toBeDefined();
    expect(store.unsaved()).toBe(2);
  });

  test("goes ahead once the curator chooses to discard them", async () => {
    const { states } = withTicks("ps3");
    const { asked, resets, read } = await clearWith(states, true);
    expect(asked).toEqual([1]);
    expect(resets).toEqual(["an_1"]);
    expect(read).toBeUndefined();
  });

  test("with nothing unsaved asks nothing, and goes ahead", async () => {
    const { states } = withTicks();
    const { asked, resets } = await clearWith(states, false);
    expect(asked).toEqual([]);
    expect(resets).toEqual(["an_1"]);
  });

  test("counts only its own Analysis's changes", async () => {
    const { states } = withTicks("ps3");
    expect(states.unsaved("an_2")).toBe(0);
  });
});

function findButton(
  node: ReactNode,
  label: string,
): ReactElement<{ onClick: () => void }> | undefined {
  if (node === null || typeof node !== "object") return undefined;
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findButton(child, label);
      if (found !== undefined) return found;
    }
    return undefined;
  }
  const element = node as ReactElement<{
    children?: ReactNode;
    onClick: () => void;
  }>;
  if (element.type === "button" && element.props.children === label)
    return element;
  return findButton(element.props.children, label);
}
