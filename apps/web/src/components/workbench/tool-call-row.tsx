"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import { IdentifierTag } from "@/components/identifier-tag";
import { stripScheme } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { ToolCall } from "@/models/workbench";

// A tool-call row (design-spec §2.4.1): an amber-tan left rail, a teal tool tag, and
// a one-line intent label. Expands to the full untruncated command and the paired
// result (both scroll rather than clip), so nothing is hidden behind a dead
// truncation. `result` is absent until the call's result event arrives.
export function ToolCallRow({ call }: { call: ToolCall }) {
  const [expanded, setExpanded] = useState(false);
  const Chevron = expanded ? ChevronDown : ChevronRight;
  return (
    <div className="border-l-2 border-subagent-border pl-[13px]">
      {/* A div, not a button, so the intent text stays drag-selectable; a plain click
          toggles, but a click that ends a text selection does not. */}
      {/* biome-ignore lint/a11y/useSemanticElements: a button suppresses text selection */}
      <div
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={() => {
          if (window.getSelection()?.toString()) return;
          setExpanded((e) => !e);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded((x) => !x);
          }
        }}
        className="flex w-full cursor-pointer select-text items-center gap-[9px] text-left"
      >
        <Chevron
          className="size-[12px] shrink-0 text-ink-faintest"
          aria-hidden
        />
        <IdentifierTag className="shrink-0 rounded-tag px-[6px] py-[1.5px] text-[10.5px] font-semibold">
          {call.name}
        </IdentifierTag>
        <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-ink-body">
          {stripScheme(call.intent)}
        </span>
        {call.result?.isError && (
          <span className="ml-auto shrink-0 rounded-badge bg-error-bg px-[6px] py-[1px] font-mono text-[9.5px] uppercase tracking-[0.06em] text-error-text">
            error
          </span>
        )}
      </div>

      {expanded && (
        <div className="mt-[8px] flex flex-col gap-[8px] pb-[2px]">
          <ToolBlock label="command" text={call.command} />
          {call.result === undefined ? (
            <span className="font-mono text-[11px] text-ink-faintest">
              awaiting result…
            </span>
          ) : (
            <ToolBlock
              label={call.result.isError ? "result (error)" : "result"}
              text={call.result.output}
              isError={call.result.isError}
            />
          )}
        </div>
      )}
    </div>
  );
}

function ToolBlock({
  label,
  text,
  isError = false,
}: {
  label: string;
  text: string;
  isError?: boolean;
}) {
  return (
    <div>
      <div
        className={cn(
          "mb-[3px] font-mono text-[9.5px] uppercase tracking-[0.07em]",
          isError ? "text-error-text" : "text-ink-faintest",
        )}
      >
        {label}
      </div>
      <pre
        className={cn(
          "tscroll max-h-[240px] select-text overflow-auto whitespace-pre-wrap break-words rounded-button border px-[11px] py-[8px] font-mono text-[11.5px] leading-[1.5]",
          isError
            ? "border-error-border bg-error-bg text-error-text"
            : "border-line-softest bg-surface-inset text-ink-label",
        )}
      >
        {text}
      </pre>
    </div>
  );
}
