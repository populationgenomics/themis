"use client";

import { useEffect, useRef } from "react";
import type { Citation } from "@/components/workbench/markdown";
import type { ConversationEvent } from "@/models/workbench";
import type { PendingTurn } from "./pending-turns";
import { StreamItem, UserBubble } from "./stream-item";
import { SubAgentCard } from "./sub-agent-card";

// The conversation region's interior (docs/design/conversation-view.md): the projected
// event stream, then the turns this browser has sent and the run has not carried back
// yet, then the composer that sends them.
export function ConversationPane({
  analysisId,
  events,
  pending,
  onCitation,
  composer,
}: {
  analysisId: string;
  events: ConversationEvent[];
  pending: readonly PendingTurn[];
  onCitation: (citation: Citation) => void;
  composer: React.ReactNode;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const shown = useRef(0);

  // Follows the curator's own turn only: a bubble appended below the fold is
  // indistinguishable from a turn that was not taken.
  useEffect(() => {
    if (pending.length > shown.current) {
      const el = scroller.current;
      if (el) el.scrollTop = el.scrollHeight;
    }
    shown.current = pending.length;
  }, [pending.length]);

  return (
    <div className="flex h-full min-w-0 flex-col">
      <div
        ref={scroller}
        className="tscroll flex min-h-0 flex-1 flex-col gap-[22px] overflow-auto px-[26px] pt-[22px] pb-[26px]"
      >
        {fanOuts(events).map((run) => {
          const items = run.map((event) => (
            <ConversationItem
              key={event.id}
              analysisId={analysisId}
              event={event}
              onCitation={onCitation}
            />
          ));
          // Siblings of one fan-out sit tighter (docs/design/conversation-view.md).
          return run.length > 1 ? (
            <div
              key={run[0].id}
              data-fanout
              className="flex flex-col gap-[9px]"
            >
              {items}
            </div>
          ) : (
            items
          );
        })}
        {pending.map((turn) => (
          <PendingBubble key={turn.localId} turn={turn} />
        ))}
      </div>
      {composer}
    </div>
  );
}

/** The stream cut into runs of adjacent sub-agent cards; every other event is a run of
 *  its own. */
function fanOuts(events: readonly ConversationEvent[]): ConversationEvent[][] {
  const runs: ConversationEvent[][] = [];
  for (const event of events) {
    const last = runs[runs.length - 1];
    if (event.kind.case === "subAgent" && last?.[0].kind.case === "subAgent") {
      last.push(event);
    } else {
      runs.push([event]);
    }
  }
  return runs;
}

function ConversationItem({
  analysisId,
  event,
  onCitation,
}: {
  analysisId: string;
  event: ConversationEvent;
  onCitation: (citation: Citation) => void;
}) {
  return (
    <StreamItem
      event={event}
      onCitation={onCitation}
      card={(value) => (
        <SubAgentCard
          analysisId={analysisId}
          card={value}
          onCitation={onCitation}
        />
      )}
    />
  );
}

/** A turn sent and not yet carried back by the poll. */
function PendingBubble({ turn }: { turn: PendingTurn }) {
  return (
    <div className="flex flex-col items-end gap-[4px]" aria-busy="true">
      <UserBubble text={turn.text} pending />
      <span className="text-[11px] text-ink-faintest">
        {turn.status === "sending" ? "Sending…" : "Waiting for the run"}
      </span>
    </div>
  );
}
