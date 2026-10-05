import { describe, expect, test } from "bun:test";
import { create, type MessageInitShape, toBinary } from "@bufbuild/protobuf";
import { AnySchema, anyPack, TimestampSchema } from "@bufbuild/protobuf/wkt";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { Markdown } from "@/components/workbench/markdown";
import { type Checklist, ChecklistSchema } from "@/models/widgets";
import { drawAsset } from "./draw";
import { placeholder, WIDGETS } from "./registry";
import type { WidgetRevision } from "./revision";
import { WidgetStatesProvider } from "./widget-state";

// The renderer's side of drawability: every way an `::embed` can fail to draw is a placeholder naming
// the path and the reason, and the rest of the document still renders.

const COMMIT = "c41e".padEnd(40, "0");
const REVISION: WidgetRevision = {
  analysisId: "an_1",
  tip: COMMIT,
  commit: COMMIT,
  curatorEmail: null,
  pinned: false,
};
const DOC_ID = "11111111-1111-4111-8111-111111111111";

function checklist(
  items: NonNullable<MessageInitShape<typeof ChecklistSchema>["items"]>,
): Checklist {
  return create(ChecklistSchema, { items });
}

function asset(payload: Checklist): Uint8Array {
  return toBinary(AnySchema, anyPack(ChecklistSchema, payload));
}

const VALID = checklist([
  { id: "ps3", label: "Functional <b>studies</b> reviewed", checked: true },
  {
    id: "pm2",
    label: "Absent from controls",
    citation: { docId: DOC_ID, quote: "not observed in 250,000 alleles" },
  },
]);

function reason(bytes: Uint8Array | null, mode = "100644"): string {
  const drawn = drawAsset(bytes === null ? null : { bytes, mode }, WIDGETS);
  if (drawn.kind !== "placeholder") throw new Error("the asset drew");
  return drawn.reason;
}

describe("an asset", () => {
  test("that is absent draws a placeholder", () => {
    expect(reason(null)).toContain("no file at this path");
  });

  test("that is not a regular file draws a placeholder, whatever its bytes", () => {
    expect(reason(asset(VALID), "120000")).toContain("mode 120000");
  });

  test("that is not a serialized Any draws a placeholder", () => {
    expect(reason(new TextEncoder().encode("# not an asset\n"))).toContain(
      "not a serialized google.protobuf.Any",
    );
  });

  test("of a type no component draws draws a placeholder", () => {
    const other = toBinary(
      AnySchema,
      anyPack(TimestampSchema, create(TimestampSchema, { seconds: BigInt(1) })),
    );
    expect(reason(other)).toContain(
      "google.protobuf.Timestamp, which is not a widget type this build draws",
    );
  });

  test("of a type registered with a placeholder draws as a type no component draws", () => {
    const file = { bytes: asset(VALID), mode: "100644" };
    const unregistered = drawAsset(file, new Map());
    const drawn = drawAsset(
      file,
      new Map([[ChecklistSchema.typeName, placeholder(ChecklistSchema)]]),
    );
    expect(unregistered.kind).toBe("placeholder");
    expect(drawn).toEqual(unregistered);
  });

  test("of a type registered with a placeholder still has its payload checked", () => {
    const duplicated = asset(
      checklist([
        { id: "a", label: "one" },
        { id: "a", label: "two" },
      ]),
    );
    const drawn = drawAsset(
      { bytes: duplicated, mode: "100644" },
      new Map([[ChecklistSchema.typeName, placeholder(ChecklistSchema)]]),
    );
    if (drawn.kind !== "placeholder") throw new Error("the asset drew");
    expect(drawn.reason).toContain("every item needs an id");
  });

  test("whose payload does not parse draws a placeholder", () => {
    const wrapped = anyPack(ChecklistSchema, VALID);
    wrapped.value = new Uint8Array([0x0a, 0xff]);
    expect(reason(toBinary(AnySchema, wrapped))).toContain(
      "does not parse as themis.widgets.models.Checklist",
    );
  });

  test("whose payload fails its rules draws a placeholder", () => {
    const duplicated = checklist([
      { id: "a", label: "one" },
      { id: "a", label: "two" },
    ]);
    expect(reason(asset(duplicated))).toContain("every item needs an id");
    expect(reason(asset(checklist([])))).toContain("fails its rules");
  });

  test("that is valid draws its component, labels as text", () => {
    const drawn = drawAsset({ bytes: asset(VALID), mode: "100644" }, WIDGETS);
    if (drawn.kind !== "drawn") throw new Error(drawn.reason);
    const html = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <WidgetStatesProvider>
          {drawn.draw({
            path: "assets/c.binpb",
            drawnAt: COMMIT,
            revision: REVISION,
            current: true,
            onCitation: () => {},
          })}
        </WidgetStatesProvider>
      </QueryClientProvider>,
    );
    expect(html).toContain("Functional &lt;b&gt;studies&lt;/b&gt; reviewed");
    expect(html).not.toContain("<b>");
    expect(html).toContain("not observed in 250,000 alleles");
  });
});

function renderDocument(
  markdown: string,
  files: Record<string, Uint8Array | null>,
): string {
  const client = new QueryClient();
  for (const [path, value] of Object.entries(files)) {
    client.setQueryData(["workspace-file", "an_1", COMMIT, path], {
      commit: COMMIT,
      value: value === null ? null : { bytes: value, mode: "100644" },
    });
  }
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <WidgetStatesProvider>
        <Markdown text={markdown} onCitation={() => {}} revision={REVISION} />
      </WidgetStatesProvider>
    </QueryClientProvider>,
  );
}

describe("the working document", () => {
  test("draws an embedded asset where the directive stands", () => {
    const html = renderDocument(
      "# Doc\n\nBefore.\n\n::embed[assets/c.binpb]\n\nAfter.",
      { "assets/c.binpb": asset(VALID) },
    );
    expect(html).toContain("Absent from controls");
    expect(html.indexOf("Before.")).toBeLessThan(
      html.indexOf("Absent from controls"),
    );
    expect(html.indexOf("Absent from controls")).toBeLessThan(
      html.indexOf("After."),
    );
    expect(html).not.toContain("::embed");
  });

  test("draws a placeholder naming the path for each embed it cannot draw, and the rest", () => {
    const html = renderDocument(
      [
        "::embed[assets/gone.binpb]",
        "::embed[../outside.binpb]",
        "::embed[a*b*.binpb]",
        "::embed[assets/c.binpb]{x=1}",
        "::embed[]",
        "- ::embed[assets/c.binpb]",
        "::embed[www.]",
        "Still here.",
      ].join("\n\n"),
      { "assets/gone.binpb": null },
    );
    expect(html).toContain("assets/gone.binpb");
    expect(html).toContain("no file at this path");
    expect(html).toContain("../outside.binpb");
    expect(html).toContain("a*b*.binpb");
    expect(html).toContain("a character outside");
    expect(html).toContain("written exactly as ::embed[&lt;path&gt;]");
    expect(html).toContain("names no path");
    expect(html).toContain("top level of the document");
    expect(html).toContain("www.");
    expect(html).toContain("Still here.");
  });

  test("reads nothing for an embed on a surface that draws no widgets", () => {
    const html = renderToStaticMarkup(
      <Markdown text="::embed[assets/c.binpb]" onCitation={() => {}} />,
    );
    expect(html).toContain("::embed[assets/c.binpb]");
  });
});
