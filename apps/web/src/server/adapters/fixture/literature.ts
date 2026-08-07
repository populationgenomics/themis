import { create } from "@bufbuild/protobuf";
import {
  FileRole,
  type LocateResponse,
  LocateResponseSchema,
  type PaperInfo,
  PaperInfoSchema,
  Representation,
} from "@/models/literature";
import { ResourceNotFoundError } from "../../errors";
import type {
  ContentObject,
  ContentPort,
  ContentSelector,
  LiteraturePort,
} from "../../ports";
import { objectKey } from "./content";

// The offline literature corpus: a couple of seeded papers with real bytes (markdown, a figure, a
// minimal PDF, a supplementary file), so the whole pane — tabs, representation toggle, inline
// figures, PDF rendering, supplementary download — runs with no Python service and no GCS.
// `serveContent` resolves a selector to a `ContentObject` in the fixture store (`FIXTURE_BUCKET`) and
// hands it to the injected `ContentPort`, exactly as the live adapter resolves a `gs://` object — the
// port shape is identical, so no pane code changes between offline and live.

const enc = new TextEncoder();

// A source-XML-derived paper: prefers markdown, also has a PDF, a figure, and a supplementary file.
export const DOC_XML = "11111111-1111-4111-8111-111111111111";
// A scan-only paper: its only rendering is a lossy OCR, so it prefers the PDF.
export const DOC_OCR = "22222222-2222-4222-8222-222222222222";

const XML_MARKDOWN = `# Regulatory variation in the ACME locus

A source-XML-derived rendering. The tab strip lists this paper; the markdown ↔ PDF toggle
switches representation, and a citation reveal highlights a quote within it.

![Figure 1: locus schematic](figure1.png)

The figure above is served from the paper's associated files, resolved to the files route.
`;

// A phrase present verbatim in the markdown above, seeded so a `:quote` citation into this paper
// locates in both representations (offsets in markdown; a page region in the PDF).
export const XML_QUOTE = "The tab strip lists this paper";

const OCR_MARKDOWN_ABSENT = null;

// A minimal single-page PDF with visible text. No xref table — pdf.js reconstructs it. Enough for
// the render pipeline and a bbox overlay to have a page to sit on.
const MINIMAL_PDF = `%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>
endobj
4 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
5 0 obj
<< /Length 60 >>
stream
BT /F1 16 Tf 30 120 Td (Fixture paper - PDF representation) Tj ET
endstream
endobj
trailer
<< /Root 1 0 R /Size 6 >>
%%EOF
`;

// A 1x1 transparent PNG — enough to prove figure resolution and inline rendering.
const FIGURE_PNG = new Uint8Array(
  Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC",
    "base64",
  ),
);

const SUPP_CSV = "gene,effect\nACME,increased expression\n";

interface PdfRegionSeed {
  page: number;
  rects: [number, number, number, number][]; // x, y, width, height — page fractions, top-left
}

interface SeededFile {
  bytes: Uint8Array;
  mediaType: string;
}

interface FixturePaper {
  info: PaperInfo;
  markdown: string | null;
  pdf: string | null;
  files: Map<string, SeededFile>;
  // Seeded quote → PDF region. Markdown locations are computed from the source at locate time.
  pdfRegions: Map<string, PdfRegionSeed>;
}

/** Code-point offsets of the first occurrence of `quote` in `markdown`, or null if absent. TextOffsets
 *  is code points (literature.proto), not UTF-16 units — the client converts back to UTF-16 to build the
 *  Range, so an astral character before the quote must not shift it. This adapter is the reference the
 *  pane's conversion is developed against, so it honours the contract even where the seeded corpus is
 *  all-BMP today. */
export function markdownOffsets(
  markdown: string,
  quote: string,
): { start: number; end: number } | null {
  const unitStart = markdown.indexOf(quote);
  if (unitStart < 0) return null;
  const start = [...markdown.slice(0, unitStart)].length;
  return { start, end: start + [...quote].length };
}

const CORPUS: Map<string, FixturePaper> = new Map([
  [
    DOC_XML,
    {
      info: create(PaperInfoSchema, {
        docId: DOC_XML,
        title: "Regulatory variation in the ACME locus",
        hasMarkdown: true,
        markdownFromXml: true,
        hasPdf: true,
        defaultRepresentation: Representation.MARKDOWN,
        files: [
          {
            name: "figure1.png",
            role: FileRole.FIGURE,
            mediaType: "image/png",
          },
          {
            name: "supp.csv",
            role: FileRole.SUPPLEMENTARY,
            mediaType: "text/csv",
          },
        ],
      }),
      markdown: XML_MARKDOWN,
      pdf: MINIMAL_PDF,
      files: new Map<string, SeededFile>([
        ["figure1.png", { bytes: FIGURE_PNG, mediaType: "image/png" }],
        ["supp.csv", { bytes: enc.encode(SUPP_CSV), mediaType: "text/csv" }],
      ]),
      pdfRegions: new Map<string, PdfRegionSeed>([
        [XML_QUOTE, { page: 0, rects: [[0.09, 0.38, 0.75, 0.09]] }],
      ]),
    },
  ],
  [
    DOC_OCR,
    {
      info: create(PaperInfoSchema, {
        docId: DOC_OCR,
        title: "A scanned paper (OCR only)",
        hasMarkdown: false,
        markdownFromXml: false,
        hasPdf: true,
        defaultRepresentation: Representation.PDF,
        files: [],
      }),
      markdown: OCR_MARKDOWN_ABSENT,
      pdf: MINIMAL_PDF,
      files: new Map(),
      pdfRegions: new Map(),
    },
  ],
]);

// The fixture's stand-in bucket. Synthetic: no such GCS bucket exists — it only keys the offline
// store, mirroring the `bucket/object` a live resolution yields.
const FIXTURE_BUCKET = "fixture-corpus";

const markdownObject = (docId: string): string => `${docId}/markdown.md`;
const pdfObject = (docId: string): string => `${docId}/paper.pdf`;
const fileObject = (docId: string, name: string): string =>
  `${docId}/files/${name}`;

/** The seeded content store the fixture `ContentPort` serves from — every rendering and file byte in
 *  `CORPUS`, keyed by the same `{FIXTURE_BUCKET}/object` path `resolveObject` produces. */
export function seedContentStore(): Map<string, Uint8Array> {
  const store = new Map<string, Uint8Array>();
  for (const [docId, paper] of CORPUS) {
    if (paper.markdown !== null) {
      store.set(
        objectKey(FIXTURE_BUCKET, markdownObject(docId)),
        enc.encode(paper.markdown),
      );
    }
    if (paper.pdf !== null) {
      store.set(
        objectKey(FIXTURE_BUCKET, pdfObject(docId)),
        enc.encode(paper.pdf),
      );
    }
    for (const [name, file] of paper.files) {
      store.set(objectKey(FIXTURE_BUCKET, fileObject(docId, name)), file.bytes);
    }
  }
  return store;
}

export class FixtureLiterature implements LiteraturePort {
  constructor(private readonly content: ContentPort) {}

  async describePaper(docId: string): Promise<PaperInfo> {
    return this.paper(docId).info;
  }

  async serveContent(
    docId: string,
    selector: ContentSelector,
  ): Promise<Response> {
    return this.content.serve(this.resolveObject(docId, selector));
  }

  private resolveObject(
    docId: string,
    selector: ContentSelector,
  ): ContentObject {
    const paper = this.paper(docId);
    switch (selector.kind) {
      case "markdown":
        if (paper.markdown === null) {
          throw new ResourceNotFoundError(`${docId} has no markdown rendering`);
        }
        return {
          bucket: FIXTURE_BUCKET,
          object: markdownObject(docId),
          mediaType: "text/markdown",
        };
      case "pdf":
        if (paper.pdf === null) {
          throw new ResourceNotFoundError(`${docId} has no PDF`);
        }
        return {
          bucket: FIXTURE_BUCKET,
          object: pdfObject(docId),
          mediaType: "application/pdf",
        };
      case "file": {
        const file = paper.files.get(selector.name);
        if (file === undefined) {
          throw new ResourceNotFoundError(
            `${docId} has no file ${selector.name}`,
          );
        }
        return {
          bucket: FIXTURE_BUCKET,
          object: fileObject(docId, selector.name),
          mediaType: file.mediaType,
          downloadName: selector.name,
        };
      }
    }
  }

  async locate(
    docId: string,
    quote: string,
    representation: Representation,
  ): Promise<LocateResponse> {
    const paper = this.paper(docId);
    if (representation === Representation.MARKDOWN && paper.markdown !== null) {
      const offsets = markdownOffsets(paper.markdown, quote);
      if (offsets)
        return create(LocateResponseSchema, {
          result: { case: "offsets", value: offsets },
        });
    }
    if (representation === Representation.PDF) {
      const region = paper.pdfRegions.get(quote);
      if (region !== undefined) {
        return create(LocateResponseSchema, {
          result: {
            case: "region",
            value: {
              page: region.page,
              rects: region.rects.map(([x, y, width, height]) => ({
                x,
                y,
                width,
                height,
              })),
            },
          },
        });
      }
    }
    return create(LocateResponseSchema, {
      result: { case: "notLocated", value: {} },
    });
  }

  private paper(docId: string): FixturePaper {
    const paper = CORPUS.get(docId);
    if (paper === undefined) {
      throw new ResourceNotFoundError(`unknown paper ${docId}`);
    }
    return paper;
  }
}
