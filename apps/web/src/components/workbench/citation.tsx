import type { ReactNode } from "react";

// A citation the agent embeds in narration or the working document: `:paper[id]` points at a
// paper; `:quote[id, text]` points at a locatable quote within one. Clicking reveals it in the
// document pane (opening the paper tab, then — for a quote — highlighting it).
export type Citation =
  | { kind: "paper"; docId: string }
  | { kind: "quote"; docId: string; quote: string };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function CitationMark({
  citation,
  onCitation,
  children,
}: {
  citation: Citation;
  onCitation: (citation: Citation) => void;
  children: ReactNode;
}) {
  if (!UUID.test(citation.docId)) {
    return (
      <span
        title={`Unresolved citation: ${citation.docId || "missing id"}`}
        className="rounded-[3px] bg-error-bg px-[3px] text-[13px] text-error-text line-through"
      >
        {children}
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={() => onCitation(citation)}
      className="rounded-[3px] px-[2px] text-left text-[14px] text-primary underline decoration-dotted underline-offset-2 hover:bg-surface-inset"
    >
      {children}
    </button>
  );
}
