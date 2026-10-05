import { describe, expect, spyOn, test } from "bun:test";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { equals } from "@bufbuild/protobuf";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { drawAsset } from "@/components/widgets/draw";
import { WIDGETS } from "@/components/widgets/registry";
import type { WidgetRevision } from "@/components/widgets/revision";
import { WidgetStatesProvider } from "@/components/widgets/widget-state";
import { Svcv4ClassificationSchema } from "@/models/widgets";
import { fbn1Classification } from "@/widgets/svcv4-classification-fixture";
import { parseTextproto } from "@/widgets/textproto";
import { WORKING_DOCUMENT_PATH } from "@/workspace-copy/service";
import {
  exampleDocument,
  examplePath,
  listExamples,
  readExample,
} from "./widget-examples";

// The widget browser's examples: every committed one reads as a payload that passes its rules and
// draws with its widget, and one that does not is reported rather than dropped.

const ROOT = path.join(import.meta.dir, "..", "widgets", "examples");
const COMMIT = "c41e".padEnd(40, "0");
const REVISION: WidgetRevision = {
  analysisId: "an_1",
  tip: COMMIT,
  commit: COMMIT,
  curatorEmail: null,
  pinned: false,
};
const CHECKLIST_HEADER = "# proto-message: themis.widgets.models.Checklist\n";

function drawn(asset: Uint8Array): string {
  const result = drawAsset({ bytes: asset, mode: "100644" }, WIDGETS);
  if (result.kind !== "drawn") throw new Error(result.reason);
  return renderToStaticMarkup(
    <QueryClientProvider client={new QueryClient()}>
      <WidgetStatesProvider>
        {result.draw({
          path: "assets/example.binpb",
          drawnAt: COMMIT,
          revision: REVISION,
          current: true,
          onCitation: () => {},
        })}
      </WidgetStatesProvider>
    </QueryClientProvider>,
  );
}

describe("the committed examples", () => {
  const examples = listExamples(ROOT);

  test("are found, at least one per widget the browser starts with", () => {
    const types = new Set(examples.map((example) => example.typeName));
    expect(types).toContain("themis.widgets.models.Checklist");
    expect(types).toContain("themis.widgets.models.Svcv4Classification");
  });

  test.each(
    examples.map((example) => [`${example.group}/${example.name}`, example]),
  )("%s reads, passes its rules and draws", (_id, example) => {
    expect(example.error).toBeUndefined();
    if (example.asset === undefined) throw new Error("no asset");
    expect(drawn(example.asset).length).toBeGreaterThan(0);
    expect(example.description).not.toBe("");
  });
});

test("the FBN1 example is the widget tests' fixture, read back exactly", () => {
  // Printed by protobuf's reference text-format printer from `fbn1Classification()`, so reading it
  // back with this parser must give the same message.
  const text = readFileSync(
    path.join(ROOT, "svcv4-classification", "fbn1-likely-pathogenic.txtpb"),
    "utf8",
  );
  expect(
    equals(
      Svcv4ClassificationSchema,
      parseTextproto(Svcv4ClassificationSchema, text),
      fbn1Classification(),
    ),
  ).toBe(true);
});

describe("an example that does not draw", () => {
  test("is reported when it names no type", () => {
    const read = readExample(
      "checklist",
      "bare",
      'items { id: "a" label: "x" }',
    );
    expect(read.asset).toBeUndefined();
    expect(read.error).toContain("proto-message");
  });

  test("is reported when it names a type no widget draws", () => {
    const read = readExample(
      "x",
      "y",
      "# proto-message: google.protobuf.Timestamp\nseconds: 1\n",
    );
    expect(read.error).toContain("not a widget payload type");
  });

  test("is reported with the line where it does not parse", () => {
    const read = readExample(
      "checklist",
      "broken",
      `${CHECKLIST_HEADER}items {\n  nope: 1\n}\n`,
    );
    expect(read.asset).toBeUndefined();
    expect(read.error).toStartWith("line 3:3:");
  });

  test("that parses and fails its rules is still written, so its embed says why", () => {
    const read = readExample(
      "checklist",
      "duplicate-ids",
      `${CHECKLIST_HEADER}items { id: "a" label: "x" }\nitems { id: "a" label: "y" }\n`,
    );
    expect(read.error).toBeUndefined();
    if (read.asset === undefined) throw new Error("no asset");
    const result = drawAsset({ bytes: read.asset, mode: "100644" }, WIDGETS);
    if (result.kind !== "placeholder") throw new Error("drawn");
    expect(result.reason).toContain("every item needs an id of its own");
  });

  test("is reported when its file name makes no asset path", () => {
    const read = readExample(
      "checklist",
      "has space",
      `${CHECKLIST_HEADER}items { id: "a" label: "x" }\n`,
    );
    expect(read.asset).toBeUndefined();
    expect(read.error).toContain("makes no asset path");
  });
});

describe("listing examples", () => {
  test("reads only <group>/<name>.txtpb, and reports a bad file on its own card", () => {
    const root = mkdtempSync(path.join(tmpdir(), "widget-examples-"));
    mkdirSync(path.join(root, "checklist", "nested"), { recursive: true });
    mkdirSync(path.join(root, "svcv4"));
    const write = (file: string, text: string) =>
      writeFileSync(path.join(root, file), text);
    write("top.txtpb", CHECKLIST_HEADER);
    write("checklist/nested/deep.txtpb", CHECKLIST_HEADER);
    write("checklist/notes.md", "not an example");
    write("checklist/good.txtpb", `${CHECKLIST_HEADER}items { id: "a" }\n`);
    write("checklist/typo.txtpb", `${CHECKLIST_HEADER}items { lable: "a" }\n`);
    write(
      "svcv4/enum-past-int32.txtpb",
      "# proto-message: themis.widgets.models.Svcv4Classification\ncodes { confidence: 3000000000 }\n",
    );
    mkdirSync(path.join(root, "checklist", "a-directory.txtpb"));
    // The unreadable file is logged in full for its author; captured here, not printed.
    const logged = spyOn(console, "error").mockImplementation(() => {});
    let listed: ReturnType<typeof listExamples>;
    let calls: unknown[][];
    try {
      listed = listExamples(root);
    } finally {
      calls = [...logged.mock.calls];
      logged.mockRestore();
      rmSync(root, { recursive: true });
    }
    expect(calls.map(([first]) => first)).toEqual([
      "widget example checklist/a-directory:",
    ]);
    expect(listed.map((each) => `${each.group}/${each.name}`)).toEqual([
      "checklist/a-directory",
      "checklist/good",
      "checklist/typo",
      "svcv4/enum-past-int32",
    ]);
    const [directory, good, typo, enumPast] = listed;
    expect(directory.error).toContain("the browser failed to read it");
    expect(good.error).toBeUndefined();
    expect(typo.error).toContain("has no field lable");
    expect(enumPast.error).toContain("out of range for an enum");
  });
});

describe("the document examples open in", () => {
  test("embeds each example that reads, and says why for one that does not", () => {
    const good = readExample(
      "checklist",
      "good",
      `${CHECKLIST_HEADER}#\n# One item.\n\nitems { id: "a" label: "x" }\n`,
    );
    const bad = readExample("checklist", "bad", `${CHECKLIST_HEADER}items {`);
    const files = exampleDocument("Widget examples: checklist", [good, bad]);
    const markdown = files[WORKING_DOCUMENT_PATH];
    if (typeof markdown !== "string") throw new Error("no document");
    expect(markdown).toContain(`::embed[${examplePath("checklist", "good")}]`);
    expect(markdown).toContain("One item.");
    expect(markdown).toContain("does not read as a payload");
    expect(files[examplePath("checklist", "good")]).toBe(
      good.asset as Uint8Array,
    );
    expect(files[examplePath("checklist", "bad")]).toBeUndefined();
  });
});
