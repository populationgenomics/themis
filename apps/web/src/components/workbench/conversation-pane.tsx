import { Markdown } from "@/components/workbench/markdown";
import { ToolCallRow } from "@/components/workbench/tool-call-row";
import type { ConversationEvent } from "@/models/workbench";

// The left conversation pane (design-spec §2.4.1). Renders the projected event
// stream — assistant turns (left, plain markdown), user steers (right, cream
// bubble), and tool calls (the expandable tool-call row). Speaker is conveyed by
// alignment + styling only; no avatars, no per-turn labels. The event is a proto
// oneof, so `kind.case` selects the variant and `kind.value` is narrowed to it.
export function ConversationPane({ events }: { events: ConversationEvent[] }) {
  return (
    <div className="flex min-w-0 flex-1 flex-col border-r border-line-primary">
      <div className="tscroll flex flex-1 flex-col gap-[22px] overflow-auto px-[26px] pt-[22px] pb-[26px]">
        {events.map((event) => (
          <ConversationItem key={event.id} event={event} />
        ))}
      </div>
    </div>
  );
}

function ConversationItem({ event }: { event: ConversationEvent }) {
  switch (event.kind.case) {
    case "user":
      return (
        <div className="flex justify-end">
          <div className="max-w-[80%] rounded-[14px_14px_4px_14px] border border-user-bubble-border bg-user-bubble-bg px-[15px] py-[11px] text-[14px] leading-[1.6] text-ink-body">
            {event.kind.value.text}
          </div>
        </div>
      );
    case "tool":
      return <ToolCallRow call={event.kind.value} />;
    case "assistant":
      return <Markdown text={event.kind.value.text} />;
    case undefined:
      throw new Error(`conversation event ${event.id} has no kind`);
  }
}
