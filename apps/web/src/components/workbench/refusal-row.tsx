import type { Refusal } from "@/models/workbench";

// A refused turn, drawn where the turn stopped; the explanation is plain text
// (docs/design/conversation-view.md, "A refused turn is a line of its own").

export function RefusalRow({ refusal }: { refusal: Refusal }) {
  return (
    <div
      role="note"
      aria-label="refused"
      className="rounded-card border border-error-border bg-error-bg px-[13px] py-[10px]"
    >
      <div className="flex items-center gap-[9px]">
        <span className="shrink-0 rounded-badge border border-error-border px-[6px] py-[1px] font-mono text-[9.5px] font-semibold uppercase tracking-[0.06em] text-error-text">
          refused
        </span>
        <span className="min-w-0 truncate font-mono text-[11.5px] text-error-text">
          {refusal.category ?? "no policy category given"}
        </span>
      </div>
      <p className="mt-[7px] text-[12.5px] leading-[1.55] text-ink-body">
        {refusal.explanation ?? (
          <span className="italic text-ink-faintest">no explanation given</span>
        )}
      </p>
    </div>
  );
}
