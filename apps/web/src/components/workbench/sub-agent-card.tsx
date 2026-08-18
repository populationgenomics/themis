"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import { type Citation, Markdown } from "@/components/workbench/markdown";
import { useThread } from "@/lib/queries";
import { errorMessage } from "@/lib/rpc";
import { cn } from "@/lib/utils";
import {
  type ConversationEvent,
  type SubAgent,
  SubAgentStatus,
} from "@/models/workbench";
import { StreamItem } from "./stream-item";

// A thread the coordinator spawned, as one collapsible card
// (docs/design/conversation-view.md). Collapsed it states what the thread was asked,
// where it stands, and what it returned; expanded it fetches and renders the thread's
// own stream. The body is a separate component so it draws without a query client.

interface StatusStyle {
  label: string;
  pill: string;
  dot: string;
}

// Zero has no entry on purpose: the client's JSON parse decodes an unknown enum *name*
// to 0, so a stale tab across a deploy is handed zero, not the new number.
const STATUS: Readonly<Partial<Record<SubAgentStatus, StatusStyle>>> = {
  [SubAgentStatus.RUNNING]: {
    label: "running",
    pill: "border-status-running-border bg-status-running-bg text-status-running-fg",
    dot: "animate-pulse-dot bg-status-running-dot",
  },
  [SubAgentStatus.IDLE]: {
    label: "idle",
    pill: "border-status-idle-border bg-status-idle-bg text-status-idle-fg",
    dot: "bg-status-idle-dot",
  },
  [SubAgentStatus.DONE]: {
    label: "done",
    pill: "border-status-done-border bg-status-done-bg text-status-done-fg",
    dot: "bg-status-done-dot",
  },
};

/** A status this build predates names no state it can draw. */
const UNKNOWN: StatusStyle = {
  label: "unknown",
  pill: "border-line-softest bg-surface-idle text-ink-faintest",
  dot: "bg-ink-ghost",
};

/** The card and the body it reveals. */
export function SubAgentCard({
  analysisId,
  card,
  onCitation,
}: {
  analysisId: string;
  card: SubAgent;
  onCitation?: (citation: Citation) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const thread = useThread(analysisId, card.threadId, card.status, expanded);
  return (
    <div className="rounded-card border border-subagent-border bg-surface-warm-panel px-[13px] py-[10px]">
      <SubAgentHeader
        card={card}
        expanded={expanded}
        onToggle={() => setExpanded((e) => !e)}
        onCitation={onCitation}
      />
      {expanded && (
        <div className="mt-[10px] border-t border-line-softest pt-[12px]">
          {thread.data ? (
            <SubAgentBody events={thread.data.events} onCitation={onCitation} />
          ) : (
            <p className="font-mono text-[11px] text-ink-faintest">
              {thread.error
                ? errorMessage(thread.error)
                : "reading the thread…"}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/** What the card states without its body: what the thread was asked, where it stands,
 *  and what it returned. The summary stays up when the card is expanded — the body
 *  carries the reply only when the thread narrated it, which is observed, not
 *  promised. Collapsed, the summary is a clamped preview; expanded, it is the full
 *  reply. */
export function SubAgentHeader({
  card,
  expanded,
  onToggle,
  onCitation,
}: {
  card: SubAgent;
  expanded: boolean;
  onToggle: () => void;
  onCitation?: (citation: Citation) => void;
}) {
  const Chevron = expanded ? ChevronDown : ChevronRight;
  const style = STATUS[card.status] ?? UNKNOWN;
  return (
    <>
      {/* A div, not a button, so the prompt stays drag-selectable; a plain click
          toggles, but a click that ends a text selection does not. */}
      {/* biome-ignore lint/a11y/useSemanticElements: a button suppresses text selection */}
      <div
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={() => {
          if (window.getSelection()?.toString()) return;
          onToggle();
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}
        className="flex w-full cursor-pointer select-text items-center gap-[9px] text-left"
      >
        <Chevron
          className="size-[12px] shrink-0 text-ink-faintest"
          aria-hidden
        />
        <span className="shrink-0 rounded-badge bg-subagent-bg px-[6px] py-[1.5px] font-mono text-[9.5px] font-semibold uppercase tracking-[0.06em] text-subagent-fg">
          sub-agent
        </span>
        <Prompt text={card.prompt} />
        <span
          className={cn(
            "flex shrink-0 items-center gap-[5px] rounded-badge border px-[6px] py-[1px] font-mono text-[9.5px] uppercase tracking-[0.06em]",
            style.pill,
          )}
        >
          <span
            className={cn("size-[5px] rounded-full", style.dot)}
            aria-hidden
          />
          {style.label}
        </span>
      </div>
      {card.summary !== undefined && card.summary !== "" && (
        <div className={cn("mt-[7px] pl-[21px]", !expanded && "line-clamp-3")}>
          <Markdown text={card.summary} onCitation={onCitation} />
        </div>
      )}
    </>
  );
}

/** The instruction, as the card's identifying line. It lands after the thread is
 *  spawned, so the gap before it is a state of its own and is drawn as one — as is an
 *  instruction that landed carrying no text. */
function Prompt({ text }: { text: string | undefined }) {
  if (text === undefined || text === "") {
    return (
      <span className="min-w-0 flex-1 truncate text-[12px] italic text-ink-faintest">
        {text === undefined
          ? "no instruction yet"
          : "instruction carried no text"}
      </span>
    );
  }
  return (
    <span
      className="min-w-0 flex-1 truncate text-[12px] text-ink-body"
      title={text}
    >
      {text}
    </span>
  );
}

/** The thread's own stream. Its first event is the coordinator's instruction, so the
 *  card draws no second copy of it. */
export function SubAgentBody({
  events,
  onCitation,
}: {
  events: readonly ConversationEvent[];
  onCitation?: (citation: Citation) => void;
}) {
  if (events.length === 0) {
    return (
      <p className="text-[12.5px] italic text-ink-faintest">
        the thread has not started
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-[16px]">
      {events.map((event) => (
        <StreamItem
          key={event.id}
          event={event}
          onCitation={onCitation}
          card={() => {
            throw new Error(
              `a thread body carries a sub-agent card: ${event.id}`,
            );
          }}
        />
      ))}
    </div>
  );
}
