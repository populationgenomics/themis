import { afterEach, describe, expect, mock, spyOn, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { api } from "@/lib/api";
import { PaperInfoSchema, Representation } from "@/models/literature";
import { REGISTRY } from "./content-kinds";

// `api` is a shared module singleton and bun runs the whole suite in one registry, so a spy that
// outlives this file would leak into whatever runs next; restore after each test.
afterEach(() => {
  mock.restore();
});

interface PaperPayload {
  docId: string;
  title: string;
  hasMarkdown: boolean;
  hasPdf: boolean;
  representation: Representation;
  error?: boolean;
}

function mockPaper(
  overrides: Partial<{
    title: string;
    hasMarkdown: boolean;
    hasPdf: boolean;
    defaultRepresentation: Representation;
  }>,
): void {
  spyOn(api, "getPaper").mockResolvedValue(
    create(PaperInfoSchema, {
      title: "A paper",
      hasMarkdown: true,
      hasPdf: true,
      defaultRepresentation: Representation.MARKDOWN,
      ...overrides,
    }),
  );
}

describe("paper content kind", () => {
  test("id is derived deterministically from the doc id", () => {
    expect(REGISTRY.paper.id({ docId: "doc-1" })).toBe("paper:doc-1");
  });

  test("open builds a closable paper tab whose id matches id(args)", async () => {
    mockPaper({ title: "Structural variants" });
    const args = { docId: "doc-1" };
    const tab = await REGISTRY.paper.open?.(args);
    expect(tab?.id).toBe(REGISTRY.paper.id(args));
    expect(tab?.kind).toBe("paper");
    expect(tab?.pinned).toBe(false);
    expect((tab?.payload as PaperPayload).title).toBe("Structural variants");
  });

  test("open defaults to PDF only when the paper's default representation is PDF", async () => {
    mockPaper({ defaultRepresentation: Representation.PDF });
    const tab = await REGISTRY.paper.open?.({ docId: "doc-1" });
    expect((tab?.payload as PaperPayload).representation).toBe(
      Representation.PDF,
    );
  });

  test("open defaults to markdown when the paper's default is not PDF", async () => {
    mockPaper({ defaultRepresentation: Representation.MARKDOWN });
    const tab = await REGISTRY.paper.open?.({ docId: "doc-1" });
    expect((tab?.payload as PaperPayload).representation).toBe(
      Representation.MARKDOWN,
    );
  });

  // The property the clamp establishes: the opened representation is one the paper actually has, even
  // when the default names the absent one.
  test.each([
    [
      "default names PDF but only markdown exists",
      Representation.PDF,
      false,
      true,
      Representation.MARKDOWN,
    ],
    [
      "default names markdown but only PDF exists",
      Representation.MARKDOWN,
      true,
      false,
      Representation.PDF,
    ],
  ])(
    "open clamps to a representation the paper has: %s",
    async (_label, defaultRepresentation, hasPdf, hasMarkdown, expected) => {
      mockPaper({ defaultRepresentation, hasPdf, hasMarkdown });
      const tab = await REGISTRY.paper.open?.({ docId: "doc-1" });
      expect((tab?.payload as PaperPayload).representation).toBe(expected);
    },
  );

  test("open with neither representation opens the tab in its error state", async () => {
    mockPaper({ hasMarkdown: false, hasPdf: false });
    const tab = await REGISTRY.paper.open?.({ docId: "doc-1" });
    expect((tab?.payload as PaperPayload).error).toBe(true);
  });
});

describe("supplementary content kind", () => {
  test("id is derived from the doc id and file name", () => {
    expect(
      REGISTRY.supplementary.id({
        docId: "doc-1",
        name: "table_s1.csv",
        mediaType: "text/csv",
      }),
    ).toBe("supp:doc-1:table_s1.csv");
  });
});
