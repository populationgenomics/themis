import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import {
  type ConversationEvent,
  ConversationEventSchema,
  SubAgentStatus,
} from "@/models/workbench";
import { ConversationPane } from "./conversation-pane";
import type { PendingTurn } from "./pending-turns";

// Clicks, scrolling and the composer's own keyboard handling are DOM-bound and covered
// by the captures; here the structural contract is tested — where a turn the run has
// not carried yet sits, and how it is distinguished from one it has.

function settled(id: string, text: string): ConversationEvent {
  return create(ConversationEventSchema, {
    id,
    kind: { case: "user", value: { text } },
  });
}

function pendingTurn(
  localId: string,
  text: string,
  status: PendingTurn["status"] = "sent",
): PendingTurn {
  return { localId, text, baseline: 0, status };
}

function card(threadId: string, prompt: string): ConversationEvent {
  return create(ConversationEventSchema, {
    id: threadId,
    kind: {
      case: "subAgent",
      value: { threadId, prompt, status: SubAgentStatus.RUNNING },
    },
  });
}

function render(events: ConversationEvent[], pending: PendingTurn[]): string {
  // A card reads its body through a query, and the pane is where one is mounted; it is
  // never fetched here, since no card starts expanded.
  return renderToStaticMarkup(
    <QueryClientProvider client={new QueryClient()}>
      <ConversationPane
        analysisId="an_1"
        events={events}
        pending={pending}
        onCitation={() => {}}
        composer={<div data-testid="composer" />}
      />
    </QueryClientProvider>,
  );
}

/** The gap the stream lays its items out with, and the gap a fan-out's siblings sit at
 *  — null when nothing was grouped. The fan-out group is found by its `data-fanout`
 *  marker, so the assertion does not pin the group's whole class string. */
function spacing(markup: string): { stream: number; siblings: number | null } {
  const stream = /tscroll[^"]*gap-\[(\d+)px\]/.exec(markup);
  if (!stream) throw new Error("the stream draws no gap");
  const siblings = /data-fanout[^>]*gap-\[(\d+)px\]/.exec(markup);
  return {
    stream: Number(stream[1]),
    siblings: siblings ? Number(siblings[1]) : null,
  };
}

describe("the conversation pane", () => {
  test("a turn the run has not carried yet renders after every settled event", () => {
    const markup = render(
      [settled("ev1", "the kickoff")],
      [pendingTurn("p0", "the curator's answer")],
    );
    expect(markup.indexOf("the curator&#x27;s answer")).toBeGreaterThan(
      markup.indexOf("the kickoff"),
    );
  });

  test("the composer sits below the stream", () => {
    const markup = render([settled("ev1", "the kickoff")], []);
    expect(markup.indexOf("composer")).toBeGreaterThan(
      markup.indexOf("the kickoff"),
    );
  });

  test("a pending turn is marked as such, and a settled one is not", () => {
    const markup = render(
      [settled("ev1", "settled")],
      [pendingTurn("p0", "pending")],
    );
    // One busy region, around the pending turn only.
    expect(markup.match(/aria-busy="true"/g)).toHaveLength(1);
    expect(markup).toContain("Waiting for the run");
  });

  test("a turn still in flight says so, distinctly from one the RPC accepted", () => {
    const inFlight = render([], [pendingTurn("p0", "x", "sending")]);
    expect(inFlight).toContain("Sending…");
    expect(inFlight).not.toContain("Waiting for the run");
  });

  test("threads of one fan-out sit tighter than unrelated neighbours", () => {
    // A coordinator that delegated to several threads at once did one thing, and the
    // siblings have to read as that rather than as several unrelated steps.
    const fanOut = spacing(
      render(
        [
          settled("ev1", "the kickoff"),
          card("sthr_a", "a"),
          card("sthr_b", "b"),
        ],
        [],
      ),
    );
    expect(fanOut.siblings).not.toBeNull();
    expect(fanOut.siblings ?? 0).toBeLessThan(fanOut.stream);

    // A card with no sibling is not grouped, so it keeps the stream's own spacing.
    expect(
      spacing(render([settled("ev1", "x"), card("sthr_a", "a")], [])).siblings,
    ).toBeNull();
  });

  test("a pending turn and a settled one are the same bubble", () => {
    // They must not drift: the pending one is what the curator reads back as their own
    // turn, and it is replaced in place by the settled one a moment later. Rendering
    // the same text both ways differs only by the pending marking.
    const asSettled = render([settled("ev1", "line one\nline two")], []);
    const asPending = render([], [pendingTurn("p0", "line one\nline two")]);
    const bubble = /class="([^"]*rounded-\[14px[^"]*)"/;
    const settledClasses = asSettled.match(bubble)?.[1];
    const pendingClasses = asPending.match(bubble)?.[1];
    expect(settledClasses).toBeDefined();
    expect(pendingClasses).toBeDefined();
    expect(pendingClasses).toContain(settledClasses ?? "");
  });
});
