import { TriangleAlert } from "lucide-react";

// A persistent notice that the current citation's quote could not be located in the shown
// representation, showing the quote so a curator can find it by eye (docs/design/document-pane.md,
// Citations). Occupies the tab's highlight slot until the next successful reveal.
export function WarningChip({ quote }: { quote: string }): React.ReactElement {
  return (
    <div className="flex shrink-0 items-start gap-[8px] border-b border-amber-quote-border bg-amber-quote-bg px-[20px] py-[9px] text-[12.5px] text-amber-quote-text">
      <TriangleAlert className="mt-[1px] size-[14px] shrink-0" aria-hidden />
      <span>
        Couldn't locate this quote in the current view:{" "}
        <span className="italic">“{quote}”</span>
      </span>
    </div>
  );
}
