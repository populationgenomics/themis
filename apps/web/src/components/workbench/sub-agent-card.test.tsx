import { describe, expect, test } from "bun:test";
import { create, fromJson, type MessageInitShape } from "@bufbuild/protobuf";
import { renderToStaticMarkup } from "react-dom/server";
import {
  ConversationEventSchema,
  SubAgentSchema,
  SubAgentStatus,
  SubAgentStatusSchema,
} from "@/models/workbench";
import { SubAgentBody, SubAgentHeader } from "./sub-agent-card";

// What a card states before its body is fetched, and what the body refuses to draw.
// Expanding is DOM-bound and covered by the captures; both components here are pure, so
// neither needs a query client.

function header(
  init: MessageInitShape<typeof SubAgentSchema>,
  expanded = false,
): string {
  return renderToStaticMarkup(
    <SubAgentHeader
      card={create(SubAgentSchema, init)}
      expanded={expanded}
      onToggle={() => {}}
    />,
  );
}

const RETURNED = {
  threadId: "sthr_literature",
  status: SubAgentStatus.IDLE,
  prompt: "Search the corpus for functional evidence on this variant.",
  summary: "Two papers report an assay; transactivation is reduced.",
};

describe("a collapsed sub-agent card", () => {
  test("identifies the thread by what it was asked, and states what came back", () => {
    const markup = header(RETURNED);
    expect(markup).toContain("sub-agent");
    expect(markup).toContain("Search the corpus for functional evidence");
    expect(markup).toContain("Two papers report an assay");
    expect(markup).toContain(">idle<");
  });

  test("represents a prompt that has not arrived rather than drawing a blank line", () => {
    // A thread is created before the coordinator's instruction lands, so the gap is a
    // state of the card, not a missing value to render as nothing.
    const markup = header({
      ...RETURNED,
      prompt: undefined,
      summary: undefined,
    });
    expect(markup).toContain("no instruction yet");
    expect(markup).not.toContain('title=""');
  });

  test("distinguishes an instruction that folded empty from one not yet arrived", () => {
    // `prompt` is `optional string` exactly so these differ: undefined means the
    // coordinator has not instructed the thread; "" means an instruction landed whose
    // message carried no text block. An empty summary draws no line at all.
    const markup = header({ ...RETURNED, prompt: "", summary: "" });
    expect(markup).toContain("instruction carried no text");
    expect(markup).not.toContain("no instruction yet");
    expect(markup).not.toMatch(/<p[\s>]/);
  });

  test("keeps the summary up when expanded", () => {
    // The body carries the reply only when the thread narrated it as an
    // `agent.message` — observed, not promised — so a summary hidden on expand could
    // be shown nowhere.
    expect(header(RETURNED, true)).toContain("Two papers report an assay");
  });

  test("renders the summary as markdown", () => {
    const markup = header({
      ...RETURNED,
      summary: "the assay shows **reduced** activity",
    });
    expect(markup).toContain("<strong");
    expect(markup).not.toContain("**");
  });

  test("clamps the summary to a preview only while collapsed", () => {
    expect(header(RETURNED)).toContain("line-clamp-3");
    expect(header(RETURNED, true)).not.toContain("line-clamp-3");
  });

  test("draws a status this build predates neutrally rather than throwing", () => {
    // A tab polling on its old bundle through a deploy is handed whatever the deployed
    // BFF projects, so a status added upstream reaches this component before a pill for
    // it does.
    const ahead = (Math.max(
      ...SubAgentStatusSchema.values.map((value) => value.number),
    ) + 1) as SubAgentStatus;
    const markup = header({ ...RETURNED, status: ahead });
    expect(markup).toContain(">unknown<");
    expect(markup).toContain("Two papers report an assay");
  });

  test("an unknown status name on the wire decodes to zero, which draws neutrally", () => {
    // The BFF serializes a known status as its *name*, and the client parses with
    // `ignoreUnknownFields: true`, under which an unknown name decodes to 0 — not to
    // the unknown number. So a stale tab across a deploy that added a status is handed
    // zero, and zero must degrade like a status this build predates. That the
    // projection never emits zero is pinned in the live client's tests.
    const card = fromJson(
      SubAgentSchema,
      { threadId: "sthr_ahead", status: "SUB_AGENT_STATUS_PAUSED" },
      { ignoreUnknownFields: true },
    );
    expect(card.status).toBe(SubAgentStatus.UNSPECIFIED);
    expect(header(card)).toContain(">unknown<");
  });
});

describe("a sub-agent thread's body", () => {
  test("renders the thread's own stream, opening on the instruction", () => {
    const markup = renderToStaticMarkup(
      <SubAgentBody
        events={[
          create(ConversationEventSchema, {
            id: "m_in",
            kind: { case: "user", value: { text: "gather the evidence" } },
          }),
          create(ConversationEventSchema, {
            id: "t1",
            kind: {
              case: "tool",
              value: { name: "grep", intent: "the corpus" },
            },
          }),
        ]}
      />,
    );
    expect(markup.indexOf("gather the evidence")).toBeLessThan(
      markup.indexOf("the corpus"),
    );
  });

  test("stands in for a thread that has not started", () => {
    expect(renderToStaticMarkup(<SubAgentBody events={[]} />)).toContain(
      "the thread has not started",
    );
  });

  test("refuses a card nested inside it", () => {
    // A body that drew a card would open a fetch inside a fetch instead of failing
    // loudly.
    expect(() =>
      renderToStaticMarkup(
        <SubAgentBody
          events={[
            create(ConversationEventSchema, {
              id: "sthr_nested",
              kind: { case: "subAgent", value: RETURNED },
            }),
          ]}
        />,
      ),
    ).toThrow();
  });
});
