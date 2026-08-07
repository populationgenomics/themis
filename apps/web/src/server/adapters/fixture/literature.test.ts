import { describe, expect, test } from "bun:test";
import { ResourceNotFoundError } from "@/server/errors";
import { createContent } from "./content";
import {
  DOC_XML,
  FixtureLiterature,
  markdownOffsets,
  seedContentStore,
} from "./literature";

function newLiterature(): FixtureLiterature {
  return new FixtureLiterature(createContent(seedContentStore()));
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

// TextOffsets is code points, not UTF-16 units (literature.proto). The seeded corpus is all-BMP, so the
// two only diverge once an astral character precedes the quote — exactly the case a UTF-16 `indexOf`
// would get wrong, and the one the pane's conversion is developed against this adapter for.
describe("markdownOffsets", () => {
  test("reports code-point offsets, unaffected by a preceding astral character", () => {
    const quote = "the finding";
    // "𝛽" (U+1D6FD) is one code point but two UTF-16 units.
    const markdown = `𝛽 introduces ${quote} here`;
    const offsets = markdownOffsets(markdown, quote);
    expect(offsets).not.toBeNull();
    // Code points: "𝛽 introduces " is 13; a UTF-16 indexOf would report 14 (the surrogate pair).
    expect(offsets?.start).toBe(13);
    expect(offsets?.end).toBe(13 + quote.length);
    expect(markdown.indexOf(quote)).toBe(14); // guards that the two genuinely disagree here
  });

  test("a quote containing an astral character counts it as one code point", () => {
    const quote = "β-value 𝛽";
    const offsets = markdownOffsets(`prefix ${quote} suffix`, quote);
    expect(offsets?.start).toBe(7);
    expect(offsets?.end).toBe(7 + [...quote].length); // 9 code points, not 10 UTF-16 units
  });

  test("an absent quote is null", () => {
    expect(markdownOffsets("nothing to see", "missing")).toBeNull();
  });
});
