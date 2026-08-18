import { describe, expect, test } from "bun:test";
import { create, fromJson, type MessageInitShape } from "@bufbuild/protobuf";
import { renderToStaticMarkup } from "react-dom/server";
import {
  DiffLineKind,
  DiffLineKindSchema,
  DiffLineSchema,
  ToolCallSchema,
  ToolLanguage,
} from "@/models/workbench";
import { DiffBlock } from "./diff-block";
import { ToolCallBody, ToolCallRow } from "./tool-call-row";

// What a collapsed row states about a call, what expanding it reveals, and how a
// replacement's two sides are drawn. Toggling between the two is DOM-bound and covered by
// the captures.

function row(init: MessageInitShape<typeof ToolCallSchema>): string {
  return renderToStaticMarkup(
    <ToolCallRow call={create(ToolCallSchema, init)} />,
  );
}

function body(init: MessageInitShape<typeof ToolCallSchema>): string {
  return renderToStaticMarkup(
    <ToolCallBody call={create(ToolCallSchema, init)} />,
  );
}

/** What a row says about a call's state, read off the two independent signals it draws:
 *  `aria-busy` on the row itself, and the badge at its right edge. */
function stateSignals(markup: string): { running: boolean; failed: boolean } {
  return {
    running: markup.includes('aria-busy="true"'),
    failed: markup.includes(">error<"),
  };
}

function diffRows(
  lines: readonly { kind: DiffLineKind; text: string }[],
): { classes: string; sign: string; text: string }[] {
  const markup = renderToStaticMarkup(
    <DiffBlock lines={lines.map((line) => create(DiffLineSchema, line))} />,
  );
  const row =
    /<div class="([^"]*)"><span class="[^"]*">(.)<\/span><span class="[^"]*">([^<]*)<\/span><\/div>/g;
  return [...markup.matchAll(row)].map(([, classes, sign, text]) => ({
    classes,
    sign,
    text,
  }));
}

describe("a collapsed tool-call row", () => {
  test("tags the tool and states what the call is for", () => {
    const markup = row({
      name: "grep",
      intent: "the citation directives",
      command: "grep -rn ':paper' /workspace",
    });
    expect(markup).toMatch(/class="[^"]*bg-teal-tint[^"]*">grep</);
    expect(markup).toContain("the citation directives");
    // Collapsed means the body is not yet drawn — the label is all there is.
    expect(markup).not.toContain("/workspace");
  });

  test("says exactly one thing about the call's state", () => {
    const states = [
      { result: undefined, running: true, failed: false },
      { result: { output: "3 lines" }, running: false, failed: false },
      {
        result: { output: "no such file", isError: true },
        running: false,
        failed: true,
      },
    ];
    expect(
      states.map(({ result }) =>
        stateSignals(row({ name: "read", intent: "/a.md", result })),
      ),
    ).toEqual(states.map(({ running, failed }) => ({ running, failed })));
  });
});

describe("an expanded tool-call row", () => {
  test("draws a replacement as a diff and any other body as text", () => {
    const replacement = body({
      name: "edit",
      intent: "/a.md",
      diff: [{ kind: DiffLineKind.ADDED, text: "the corrected line" }],
      result: { output: "applied 1 edit" },
    });
    expect(replacement).toContain("bg-diff-added-bg");

    const text = body({
      name: "shell",
      intent: "look",
      command: "echo hello",
      language: ToolLanguage.SHELL,
      result: { output: "hello" },
    });
    expect(text).not.toContain("bg-diff-added-bg");
    expect(text).toContain("<span");
  });

  test("never highlights a result, however much it reads like a program", () => {
    const markup = body({
      name: "shell",
      intent: "look",
      result: { output: "echo 'not a program' # but it lexes as one" },
    });
    expect(markup).toContain("not a program");
    // The only spans this component draws are a lexer's.
    expect(markup).not.toContain("<span");
  });

  test("stands in for a result the call has not returned yet", () => {
    expect(body({ name: "read", intent: "/a.md" })).toContain(
      "awaiting result",
    );
  });
});

describe("a replacement drawn line by line", () => {
  const REPLACEMENT = [
    { kind: DiffLineKind.CONTEXT, text: "### Sources" },
    { kind: DiffLineKind.REMOVED, text: "drawn from one paper." },
    { kind: DiffLineKind.ADDED, text: "drawn from two papers." },
    { kind: DiffLineKind.ADDED, text: "the second is a scan." },
  ];

  test("draws one row per line, signed by which side it falls on", () => {
    const drawn = diffRows(REPLACEMENT);
    expect(drawn.map((line) => line.text)).toEqual(
      REPLACEMENT.map((line) => line.text),
    );
    expect(drawn.map((line) => line.sign)).toEqual([" ", "-", "+", "+"]);
  });

  test("washes the two sides of the replacement, and nothing else", () => {
    const washes = diffRows(REPLACEMENT).map(
      ({ classes }) => /bg-diff-(\w+)-bg/.exec(classes)?.[1] ?? null,
    );
    expect(washes).toEqual([null, "removed", "added", "added"]);
  });

  test("draws a kind this build predates unsigned rather than throwing", () => {
    // A tab polling on its old bundle is handed whatever the deployed BFF projects, so a
    // kind added upstream reaches this component before a style for it does.
    const ahead = (Math.max(
      ...DiffLineKindSchema.values.map((value) => value.number),
    ) + 1) as DiffLineKind;
    const drawn = diffRows([{ kind: ahead, text: "a side yet to be named" }]);
    expect(drawn.map(({ sign, text }) => [sign, text])).toEqual([
      [" ", "a side yet to be named"],
    ]);
    expect(drawn[0].classes).not.toMatch(/bg-diff/);
  });

  test("an unknown kind name on the wire decodes to zero, which draws unsigned", () => {
    // The BFF serializes a known kind as its *name*, and the client parses with
    // `ignoreUnknownFields: true`, under which an unknown name decodes to 0 — not to the
    // unknown number. So a stale tab across a deploy that added a kind is handed zero,
    // and zero must degrade like any kind this build predates. The projection's own
    // guarantee — no emitted line carries zero — lives in the tool-diff tests.
    const line = fromJson(
      DiffLineSchema,
      { kind: "DIFF_LINE_KIND_CHANGED_SPAN", text: "which side?" },
      { ignoreUnknownFields: true },
    );
    expect(line.kind).toBe(DiffLineKind.UNSPECIFIED);
    const drawn = diffRows([line]);
    expect(drawn.map(({ sign, text }) => [sign, text])).toEqual([
      [" ", "which side?"],
    ]);
    expect(drawn[0].classes).not.toMatch(/bg-diff/);
  });
});
