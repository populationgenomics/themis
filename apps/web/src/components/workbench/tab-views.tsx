"use client";

import { useQuery } from "@tanstack/react-query";
import { Download, TriangleAlert } from "lucide-react";
import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState } from "react";
import { RenderBoundary } from "@/components/render-boundary";
import type { WidgetRevision } from "@/components/widgets/revision";
import { api, paperContent } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Representation } from "@/models/literature";
import { COPY_CEILING_BYTES } from "@/workspace-copy/residency";
import type { CopyClearing } from "./clear-copy";
import { applyQuoteHighlight, clearQuoteHighlight } from "./highlight";
import { type Citation, corpusFigureResolver, Markdown } from "./markdown";
import { WarningChip } from "./warning-chip";
import type { WorkingDocumentState } from "./working-document";
import type { WorkingDocumentSignal } from "./workspace-sync";

// The content views for a tab, keyed on primitives (doc id, quote, name) rather than any tab-union
// type, so both the F4 document pane and the F5 group render them unchanged. A paper's markdown/PDF
// choice and highlight are resolved server-side (`Locate`); the client only applies the result.

const DOCUMENT_PATH = "/workspace/working_document.md";

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

/** The working document in the state the window reads it in. While `unavailable`, the latest Poll
 *  could not read the workspace, or the Poll has failed since: whatever the state shows stays, under
 *  a notice that it may be out of date. */
export function WorkingDocumentView({
  document,
  unavailable,
  clearing,
  signal,
  curatorEmail,
  onCitation,
}: {
  document: WorkingDocumentState;
  unavailable: boolean;
  /** Clearing the browser's copy of the Analysis; null until the Poll names one. */
  clearing: CopyClearing | null;
  /** The branch's tip the document was read against. */
  signal: WorkingDocumentSignal | null;
  curatorEmail: string;
  onCitation: (citation: Citation) => void;
}): React.ReactElement {
  if (clearing?.state.kind === "clearing")
    return <Notice text="Clearing the cache and reloading…" />;
  if (clearing?.state.kind === "failed")
    return (
      <ClearCopyControl
        clearing={clearing}
        text={`The cache couldn't be cleared: ${clearing.state.message}`}
      />
    );
  if (document.kind === "copyFailed") {
    if (clearing === null)
      throw new Error("a failed read of a copy names no Analysis to clear");
    return (
      <ClearCopyControl
        clearing={clearing}
        text="This workspace couldn't be loaded from your browser's cache. Clearing the cache and reloading usually fixes this."
      />
    );
  }
  const body = (
    <WorkingDocumentBody
      document={document}
      signal={signal}
      curatorEmail={curatorEmail}
      onCitation={onCitation}
    />
  );
  // Neither of these shows anything the notice could call out of date.
  if (
    !unavailable ||
    document.kind === "unavailable" ||
    document.kind === "damaged"
  )
    return body;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <output className="flex shrink-0 items-start gap-[8px] border-b border-amber-quote-border bg-amber-quote-bg px-[20px] py-[9px] text-[12.5px] text-amber-quote-text">
        <TriangleAlert className="mt-[1px] size-[14px] shrink-0" aria-hidden />
        <span>
          The workspace can't be reached right now. What you see may be out of
          date.
        </span>
      </output>
      {body}
    </div>
  );
}

/** Why the copy needs clearing, and the button that clears it. */
function ClearCopyControl({
  clearing,
  text,
}: {
  clearing: CopyClearing;
  text: string;
}): React.ReactElement {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-[12px] px-[28px] text-center">
      <span className="max-w-[420px] text-[13px] text-ink-faintest">
        {text}
      </span>
      <button
        type="button"
        onClick={clearing.clear}
        className="rounded-field border border-line-soft px-[10px] py-[4px] text-[12px] font-medium text-ink-faint hover:text-ink-primary"
      >
        Clear cache and reload
      </button>
      <span className="text-[11.5px] text-ink-faintest">
        Only this browser's cached copy is cleared. Nothing saved is lost.
      </span>
    </div>
  );
}

function WorkingDocumentBody({
  document,
  signal,
  curatorEmail,
  onCitation,
}: {
  document: WorkingDocumentState;
  /** The branch's tip the document was read against. */
  signal: WorkingDocumentSignal | null;
  curatorEmail: string;
  onCitation: (citation: Citation) => void;
}): React.ReactElement {
  switch (document.kind) {
    case "failed":
      return <Notice text="Couldn't load this document version." />;
    case "copyFailed":
      throw new Error("a failed read of the copy is drawn with its control");
    case "loading":
      return <Notice text="Loading the workspace…" />;
    case "unavailable":
      return (
        <Notice text="The workspace can't be reached right now, so the working document can't be shown yet." />
      );
    case "damaged":
      return (
        <Notice text="This workspace is damaged, so its working document can't be shown." />
      );
    case "absent":
      return (
        <Notice text="The agent has not written the working document yet." />
      );
    case "noRepository":
      return (
        <Notice text="This Analysis has no workspace repository yet. The agent creates it with its first commit; an Analysis started before workspaces were repositories has none." />
      );
    case "tooLarge":
      return (
        <Notice
          text={`This workspace is too large to open in the browser: one workspace may take up to ${COPY_CEILING_BYTES / (1024 * 1024)} MiB.`}
        />
      );
    case "shown":
      return (
        <ShownDocument
          markdown={document.markdown}
          commit={document.commit}
          pinned={document.pinned}
          signal={signal}
          curatorEmail={curatorEmail}
          onCitation={onCitation}
        />
      );
  }
}

function ShownDocument({
  markdown,
  commit,
  pinned,
  signal,
  curatorEmail,
  onCitation,
}: {
  markdown: string;
  commit: string;
  pinned: boolean;
  signal: WorkingDocumentSignal | null;
  curatorEmail: string;
  onCitation: (citation: Citation) => void;
}): React.ReactElement {
  if (signal?.tip?.kind !== "commit") {
    throw new Error(
      `a working document is shown at ${commit} with no branch tip`,
    );
  }
  const { analysisId } = signal;
  const tip = signal.tip.commit;
  // Memoized on its fields: the renderer rebuilds every widget when the revision's identity changes,
  // and the window re-renders on each Poll.
  const revision = useMemo<WidgetRevision>(
    () => ({
      analysisId,
      tip,
      commit,
      // Only the tip is edited: a curator's change to an earlier version would land on a tip they
      // are not looking at.
      curatorEmail: commit === tip && !pinned ? curatorEmail : null,
      pinned,
    }),
    [analysisId, tip, commit, curatorEmail, pinned],
  );
  return (
    <div className="tscroll flex-1 overflow-auto px-[28px] pt-[24px] pb-[30px]">
      <div className="mb-[16px] font-mono text-[12px] text-ink-faint">
        {DOCUMENT_PATH}
      </div>
      <RenderBoundary
        resetKey={commit}
        fallback={(error) => (
          <p className="text-[13px] text-ink-faint">
            This version of the document could not be drawn: {error.message}
          </p>
        )}
      >
        <Markdown text={markdown} onCitation={onCitation} revision={revision} />
      </RenderBoundary>
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
