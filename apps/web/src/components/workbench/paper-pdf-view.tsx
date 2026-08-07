"use client";

import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import { api } from "@/lib/api";
import { Representation } from "@/models/literature";
import { useSpacePan } from "./use-space-pan";
import { WarningChip } from "./warning-chip";

// The PDF representation of a paper, rendered with pdf.js via react-pdf. Loaded only in the browser
// (dynamic import, ssr:false in the pane) because pdf.js touches DOM APIs at import. Text and
// annotation layers are off: pages are display-only, and a citation highlight is drawn as absolutely
// positioned overlay rects (page-fraction coordinates from Locate) over the target page — not via
// pdf.js's own text layer.

// The worker ships with pdfjs-dist; `new URL(..., import.meta.url)` lets the bundler emit it as an
// asset and hand back its URL.
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

interface PdfRegion {
  page: number;
  rects: { x: number; y: number; width: number; height: number }[];
}

export default function PaperPdfView({
  url,
  docId,
  quote,
}: {
  url: string;
  docId: string;
  quote: string | null;
}): React.ReactElement {
  const containerRef = useRef<HTMLDivElement>(null);
  const highlightedPageRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number | null>(null);
  const [pageCount, setPageCount] = useState(0);
  const [region, setRegion] = useState<PdfRegion | null>(null);
  const [unlocated, setUnlocated] = useState<string | null>(null);

  useSpacePan(containerRef);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => {
      const measured = entries[0]?.contentRect.width;
      if (measured) setWidth(Math.floor(measured));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!quote) {
      setRegion(null);
      setUnlocated(null);
      return;
    }
    let cancelled = false;
    // Clear the prior region and chip up front: a rejected or slow-in-flight locate must not leave the
    // previous quote's rects painted (or scroll to its page), nor its "not located" chip standing,
    // under the new quote.
    setRegion(null);
    setUnlocated(null);
    api
      .locate(docId, quote, Representation.PDF)
      .then((result) => {
        if (cancelled) return;
        if (result.result.case === "region") {
          setRegion(result.result.value as PdfRegion);
          setUnlocated(null);
        } else {
          setRegion(null);
          setUnlocated(quote);
        }
      })
      .catch(() => {
        if (!cancelled) setUnlocated(quote);
      });
    return () => {
      cancelled = true;
    };
  }, [docId, quote]);

  // Scroll the highlighted page into view once it and the region are present. The region usually
  // resolves before the pages mount (a small gRPC call vs a multi-MB fetch + pdf.js parse), so the ref
  // is null on the region's first pass and the scroll must retry once the pages exist.
  // biome-ignore lint/correctness/useExhaustiveDependencies: pageCount is a re-run trigger — the pages mount after the region resolves; the body reads only the ref, but the effect must re-run when they appear.
  useEffect(() => {
    if (region && highlightedPageRef.current) {
      highlightedPageRef.current.scrollIntoView({ block: "center" });
    }
  }, [region, pageCount]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {unlocated && <WarningChip quote={unlocated} />}
      <div
        ref={containerRef}
        className="tscroll flex-1 overflow-auto px-[24px] py-[20px]"
      >
        <Document
          file={url}
          onLoadSuccess={({ numPages }) => setPageCount(numPages)}
          loading={<PdfNotice text="Loading PDF…" />}
          error={<PdfNotice text="Could not load the PDF." />}
          className="flex flex-col items-center gap-[16px]"
        >
          {Array.from({ length: pageCount }, (_, index) => {
            const highlighted = region?.page === index;
            return (
              <div
                key={`page-${index + 1}`}
                ref={highlighted ? highlightedPageRef : undefined}
                className="relative shadow-[0_1px_4px_rgba(0,0,0,0.12)]"
              >
                <Page
                  pageNumber={index + 1}
                  width={width ?? undefined}
                  renderTextLayer={false}
                  renderAnnotationLayer={false}
                />
                {highlighted &&
                  region.rects.map((rect) => (
                    <div
                      key={`rect-${rect.x}-${rect.y}-${rect.width}-${rect.height}`}
                      className="pointer-events-none absolute rounded-[2px] bg-[rgb(250_204_21_/_0.4)] mix-blend-multiply"
                      style={{
                        left: `${rect.x * 100}%`,
                        top: `${rect.y * 100}%`,
                        width: `${rect.width * 100}%`,
                        height: `${rect.height * 100}%`,
                      }}
                    />
                  ))}
              </div>
            );
          })}
        </Document>
      </div>
    </div>
  );
}

function PdfNotice({ text }: { text: string }): React.ReactElement {
  return (
    <div className="flex flex-1 items-center justify-center text-[13px] text-ink-faintest">
      {text}
    </div>
  );
}
