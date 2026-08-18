import { cn } from "@/lib/utils";
import { type DiffLine, DiffLineKind } from "@/models/workbench";

// The two sides of an `edit`, line by line. A div rather than a pre: each line needs a
// block-level row so its wash reaches the block's edges.

interface LineStyle {
  row: string;
  gutter: string;
  sign: string;
}

/** A kind this build predates draws with no sign and no wash. */
const UNSIGNED: LineStyle = { row: "", gutter: "", sign: " " };

// Zero has no entry on purpose: the client's JSON parse decodes an unknown enum *name*
// to 0, so a stale tab across a deploy is handed zero, not the new number.
const STYLES: Readonly<Partial<Record<DiffLineKind, LineStyle>>> = {
  [DiffLineKind.CONTEXT]: {
    row: "text-ink-neutral",
    gutter: "text-ink-ghost",
    sign: " ",
  },
  [DiffLineKind.REMOVED]: {
    row: "bg-diff-removed-bg text-diff-removed-fg",
    gutter: "text-diff-removed-gutter",
    sign: "-",
  },
  [DiffLineKind.ADDED]: {
    row: "bg-diff-added-bg text-diff-added-fg",
    gutter: "text-diff-added-gutter",
    sign: "+",
  },
};

export function DiffBlock({ lines }: { lines: readonly DiffLine[] }) {
  return (
    <div className="tscroll max-h-[240px] select-text overflow-auto rounded-button border border-line-softest bg-surface-inset px-[11px] py-[8px] font-mono text-[11.5px] leading-[1.5]">
      {lines.map((line, index) => (
        <DiffRow
          // A diff line has no id: its position in the replacement is what it is. The
          // text is folded in because a bare index is refused as a key.
          key={`${index}-${line.text}`}
          line={line}
        />
      ))}
    </div>
  );
}

function DiffRow({ line }: { line: DiffLine }) {
  const style = STYLES[line.kind] ?? UNSIGNED;
  return (
    <div className={cn("-mx-[11px] flex px-[11px]", style.row)}>
      <span className={cn("w-[11px] shrink-0 select-none", style.gutter)}>
        {style.sign}
      </span>
      <span className="min-w-0 whitespace-pre-wrap break-words">
        {/* A blank line still needs a row tall enough to carry its wash. */}
        {line.text === "" ? " " : line.text}
      </span>
    </div>
  );
}
