"use client";

import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import dynamic from "next/dynamic";
import { useEffect, useRef, useState } from "react";
import { api, paperContent } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Representation } from "@/models/literature";
import type { WorkingDocument } from "@/models/workbench";
import { applyQuoteHighlight, clearQuoteHighlight } from "./highlight";
import { type Citation, corpusFigureResolver, Markdown } from "./markdown";
import { WarningChip } from "./warning-chip";

// The content views for a tab, keyed on primitives (doc id, quote, name) rather than any tab-union
// type, so both the F4 document pane and the F5 group render them unchanged. A paper's markdown/PDF
// choice and highlight are resolved server-side (`Locate`); the client only applies the result.

const DOCUMENT_PATH = "/workspace/document.md";

// pdf.js touches DOM APIs at import, so the PDF view loads only in the browser.
export const PaperPdfView = dynamic(() => import("./paper-pdf-view"), {
  ssr: false,
  loading: () => <Notice text="Loading viewer…" />,
});

export function Notice({ text }: { text: string }): React.ReactElement {
  return (
    <div className="flex flex-1 items-center justify-center px-[28px] text-center text-[13px] text-ink-faintest">
      {text}
    </div>
  );
}

export function RepresentationToggle({
  representation,
  onChange,
}: {
  representation: Representation;
  onChange: (representation: Representation) => void;
}): React.ReactElement {
  return (
    <div className="flex shrink-0 items-center gap-[2px] rounded-field border border-line-soft p-[2px]">
      {[
        { rep: Representation.MARKDOWN, label: "Markdown" },
        { rep: Representation.PDF, label: "PDF" },
      ].map(({ rep, label }) => (
        <button
          key={label}
          type="button"
          onClick={() => onChange(rep)}
          className={cn(
            "rounded-[5px] px-[9px] py-[3px] text-[11.5px] font-medium",
            representation === rep
              ? "bg-primary text-primary-foreground"
              : "text-ink-faint hover:text-ink-primary",
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export function WorkingDocumentView({
  document,
  onCitation,
}: {
  document: WorkingDocument | null;
  onCitation: (citation: Citation) => void;
}): React.ReactElement {
  if (document === null) {
    return (
      <Notice text="The agent has not written the working document yet." />
    );
  }
  return (
    <div className="tscroll flex-1 overflow-auto px-[28px] pt-[24px] pb-[30px]">
      <div className="mb-[16px] font-mono text-[12px] text-ink-faint">
        {DOCUMENT_PATH}
      </div>
      <Markdown text={document.markdown} onCitation={onCitation} />
    </div>
  );
}

export function PaperMarkdownView({
  docId,
  quote,
}: {
  docId: string;
  quote: string | null;
}): React.ReactElement {
  const containerRef = useRef<HTMLDivElement>(null);
  const [unlocated, setUnlocated] = useState<string | null>(null);
  const markdown = useQuery({
    queryKey: ["paper-markdown", docId],
    queryFn: () => api.getPaperMarkdownText(docId),
  });
  const ready = markdown.isSuccess;

  useEffect(() => {
    const container = containerRef.current;
    if (!ready || !container) return;
    if (!quote) {
      clearQuoteHighlight(docId);
      setUnlocated(null);
      return;
    }
    let cancelled = false;
    // Clear the prior chip up front: a slow or rejected locate must not leave the previous quote's
    // "not located" warning standing over the new one. (The highlight itself is cleared by cleanup.)
    setUnlocated(null);
    api
      .locate(docId, quote, Representation.MARKDOWN)
      .then((result) => {
        if (cancelled) return;
        const located = result.result.case === "offsets";
        if (located && applyQuoteHighlight(container, quote, docId)) {
          setUnlocated(null);
        } else {
          clearQuoteHighlight(docId);
          setUnlocated(quote);
        }
      })
      .catch(() => {
        if (!cancelled) setUnlocated(quote);
      });
    return () => {
      cancelled = true;
      clearQuoteHighlight(docId);
    };
  }, [docId, quote, ready]);

  if (markdown.isPending) return <Notice text="Loading…" />;
  if (markdown.isError) {
    return (
      <Notice text={`Could not load the paper: ${markdown.error.message}`} />
    );
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {unlocated && <WarningChip quote={unlocated} />}
      <div
        ref={containerRef}
        className="tscroll flex-1 overflow-auto px-[28px] pt-[24px] pb-[30px]"
      >
        <Markdown
          text={markdown.data}
          resolveImage={corpusFigureResolver((name) =>
            paperContent.file(docId, name),
          )}
        />
      </div>
    </div>
  );
}

export function SupplementaryView({
  docId,
  name,
}: {
  docId: string;
  name: string;
}): React.ReactElement {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-[12px] px-[28px] text-center">
      <p className="text-[13px] text-ink-faint">
        Supplementary files open externally; in-app rendering is not built yet.
      </p>
      <a
        href={paperContent.file(docId, name)}
        download={name}
        className="flex items-center gap-[7px] rounded-field bg-primary px-[16px] py-[8px] text-[13px] font-semibold text-primary-foreground"
      >
        <Download className="size-[15px]" aria-hidden />
        Download {name}
      </a>
    </div>
  );
}
