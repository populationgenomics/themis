import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { PaperInfoSchema, Representation } from "@/models/literature";
import { ResourceNotFoundError } from "@/server/errors";
import { createContent } from "./content";
import {
  DOC_XML,
  FixtureLiterature,
  type FixturePaper,
  seedContentStore,
} from "./literature";

function newLiterature(): FixtureLiterature {
  return new FixtureLiterature(createContent(seedContentStore()));
}

function literatureWithMarkdown(
  docId: string,
  markdown: string,
): FixtureLiterature {
  const paper: FixturePaper = {
    info: create(PaperInfoSchema, {
      docId,
      title: "Synthetic fixture paper",
      hasMarkdown: true,
      markdownFromXml: true,
      hasPdf: false,
      defaultRepresentation: Representation.MARKDOWN,
    }),
    markdown,
    pdf: null,
    files: new Map(),
    pdfRegions: new Map(),
  };
  const corpus = new Map([[docId, paper]]);
  return new FixtureLiterature(createContent(seedContentStore(corpus)), corpus);
}

// The two adapters share the port shape: serveContent resolves the selector and the ContentPort
// serves it. Offline that is a 200 byte body (the fixture never signs) — a 302 here would mean the
// fixture grew a signing path it can't have. (Stream freshness is the ContentPort's job — see
// content.test.ts.)
describe("FixtureLiterature.serveContent", () => {
  test("serves a resolved file's bytes with a 200, not a redirect", async () => {
    const res = await newLiterature().serveContent(DOC_XML, {
      kind: "file",
      name: "figure1.png",
    });
    expect(res.status).toBe(200);
    expect((await res.arrayBuffer()).byteLength).toBeGreaterThan(0);
  });

  test("an absent file is a ResourceNotFoundError (the routes' documented 404)", async () => {
    await expect(
      newLiterature().serveContent(DOC_XML, { kind: "file", name: "nope.png" }),
    ).rejects.toBeInstanceOf(ResourceNotFoundError);
  });
});

// TextOffsets is code points, not UTF-16 units (literature.proto). An astral character precedes or
// sits inside the quote where a UTF-16 `indexOf` diverges from code-point indexing.
describe("FixtureLiterature.locate", () => {
  test("reports code-point offsets, unaffected by a preceding astral character", async () => {
    // "𝛽" (U+1D6FD) is one code point ("𝛽 introduces " = 13) but two UTF-16 units (indexOf = 14).
    const markdown = "𝛽 introduces the finding here";
    expect(markdown.indexOf("the finding")).toBe(14);
    const located = await literatureWithMarkdown("doc-astral", markdown).locate(
      "doc-astral",
      "the finding",
      Representation.MARKDOWN,
    );
    expect(located.result).toMatchObject({
      case: "offsets",
      value: { start: 13, end: 24 },
    });
  });

  test("a quote containing an astral character counts it as one code point", async () => {
    // "prefix " is 7 code points; "β-value 𝛽" is 9 code points (10 UTF-16 units), so end is 16.
    const located = await literatureWithMarkdown(
      "doc-astral-quote",
      "prefix β-value 𝛽 suffix",
    ).locate("doc-astral-quote", "β-value 𝛽", Representation.MARKDOWN);
    expect(located.result).toMatchObject({
      case: "offsets",
      value: { start: 7, end: 16 },
    });
  });

  test("an absent quote is not located", async () => {
    const located = await literatureWithMarkdown(
      "doc-plain",
      "nothing to see",
    ).locate("doc-plain", "missing", Representation.MARKDOWN);
    expect(located.result.case).toBe("notLocated");
  });
});
