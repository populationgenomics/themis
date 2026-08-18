"use client";

import type { ReactNode } from "react";
import type { Citation } from "@/components/workbench/markdown";
import { Markdown } from "@/components/workbench/markdown";
import { ToolCallRow } from "@/components/workbench/tool-call-row";
import type { ConversationEvent, SubAgent } from "@/models/workbench";

// One line of a projected stream — the coordinator's, or a spawned thread's body
// (docs/design/conversation-view.md). The event is a proto oneof, so `kind.case` selects
// the variant and `kind.value` is narrowed to it.

export function StreamItem({
  event,
  onCitation,
  card,
}: {
  event: ConversationEvent;
  onCitation?: (citation: Citation) => void;
  /** How to draw a sub-agent card. A thread body has none to draw (one level of
   *  delegation), so it raises there. */
  card: (value: SubAgent) => ReactNode;
}) {
  switch (event.kind.case) {
    case "user":
      return (
        <div className="flex justify-end">
          <UserBubble text={event.kind.value.text} />
        </div>
      );
    case "tool":
      return <ToolCallRow call={event.kind.value} />;
    case "assistant":
      return <Markdown text={event.kind.value.text} onCitation={onCitation} />;
    case "subAgent":
      return card(event.kind.value);
    case undefined:
      throw new Error(`conversation event ${event.id} has no kind`);
    default:
      // `noImplicitReturns` is off, so a variant added to the oneof and left out of the
      // switch above draws nothing at all; the assignment makes that a type error too.
      event.kind satisfies never;
      throw new Error(`conversation event ${event.id} has an unhandled kind`);
  }
}

/** The curator's turn: right-aligned markdown; the newlines they typed survive as breaks. */
export function UserBubble({
  text,
  pending,
}: {
  text: string;
  pending?: boolean;
}) {
  return (
    <div
      className={`max-w-[80%] rounded-[14px_14px_4px_14px] border border-user-bubble-border bg-user-bubble-bg px-[15px] py-[11px]${
        pending ? " border-dashed opacity-60" : ""
      }`}
    >
      <Markdown text={text} breaks />
    </div>
  );
}
