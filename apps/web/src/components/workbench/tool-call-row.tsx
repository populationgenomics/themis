"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import { IdentifierTag } from "@/components/identifier-tag";
import { stripScheme } from "@/lib/format";
import { cn } from "@/lib/utils";
import { type ToolCall, ToolLanguage } from "@/models/workbench";
import { DiffBlock } from "./diff-block";
import { ToolText } from "./tool-text";

// A tool-call row: an amber-tan left rail, a teal tool tag, and a one-line intent label.
// Expands to the untruncated input — highlighted text, or an edit's replacement as a
// diff — and the paired result. `result` is absent until the call's result event arrives.
export function ToolCallRow({ call }: { call: ToolCall }) {
  const [expanded, setExpanded] = useState(false);
  const Chevron = expanded ? ChevronDown : ChevronRight;
  const running = call.result === undefined;
  return (
    <div className="border-l-2 border-subagent-border pl-[13px]">
      {/* A div, not a button, so the intent text stays drag-selectable; a plain click
          toggles, but a click that ends a text selection does not. */}
      {/* biome-ignore lint/a11y/useSemanticElements: a button suppresses text selection */}
      <div
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        aria-busy={running}
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
        {running && (
          <span className="ml-auto flex shrink-0 items-center">
            <span
              className="size-[5px] animate-pulse-dot rounded-full bg-status-running-dot"
              aria-hidden
            />
            <span className="sr-only">awaiting result</span>
          </span>
        )}
        {call.result?.isError && (
          <span className="ml-auto shrink-0 rounded-badge bg-error-bg px-[6px] py-[1px] font-mono text-[9.5px] uppercase tracking-[0.06em] text-error-text">
            error
          </span>
        )}
      </div>

      {expanded && <ToolCallBody call={call} />}
    </div>
  );
}

/** What expanding a row reveals: the call's one body, and the result it is paired with. */
export function ToolCallBody({ call }: { call: ToolCall }) {
  return (
    <div className="mt-[8px] flex flex-col gap-[8px] pb-[2px]">
      <Labelled label="input">
        {call.diff.length > 0 ? (
          <DiffBlock lines={call.diff} />
        ) : (
          <ToolBlock text={call.command} language={call.language} />
        )}
      </Labelled>
      {call.result === undefined ? (
        <span className="font-mono text-[11px] text-ink-faintest">
          awaiting result…
        </span>
      ) : (
        <Labelled
          label={call.result.isError ? "result (error)" : "result"}
          isError={call.result.isError}
        >
          <ToolBlock
            text={call.result.output}
            language={ToolLanguage.UNSPECIFIED}
            isError={call.result.isError}
          />
        </Labelled>
      )}
    </div>
  );
}

function Labelled({
  label,
  isError = false,
  children,
}: {
  label: string;
  isError?: boolean;
  children: React.ReactNode;
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
      {children}
    </div>
  );
}

function ToolBlock({
  text,
  language,
  isError = false,
}: {
  text: string;
  language: ToolLanguage;
  isError?: boolean;
}) {
  return (
    <pre
      className={cn(
        "tscroll max-h-[240px] select-text overflow-auto whitespace-pre-wrap break-words rounded-button border px-[11px] py-[8px] font-mono text-[11.5px] leading-[1.5]",
        isError
          ? "border-error-border bg-error-bg text-error-text"
          : "border-line-softest bg-surface-inset text-ink-label",
      )}
    >
      <ToolText text={text} language={language} />
    </pre>
  );
}
