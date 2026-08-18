import { describe, expect, test } from "bun:test";
import type Anthropic from "@anthropic-ai/sdk";
import type {
  BetaManagedAgentsSessionEvent,
  EventSendParams,
} from "@anthropic-ai/sdk/resources/beta/sessions/events";
import { timestampDate } from "@bufbuild/protobuf/wkt";
import {
  type ConversationEvent,
  type SubAgent,
  SubAgentStatus,
} from "@/models/workbench";
import { ResourceNotFoundError, SessionBusyError } from "../../errors";
import {
  AnthropicClient,
  foldEvents,
  foldThreadEvents,
  MANAGED_AGENTS_BETA,
} from "./client";

// The projection is the production path (the fixture bypasses it), so its
// load-bearing behaviours are pinned here: agent/user messages fold to
// assistant/user narration; the custom `shell` tool (agent.custom_tool_use →
// user.custom_tool_result) folds to a row labelled by its model-stated `intent`; a
// prebuilt tool (agent.tool_use) is labelled by its target field; and a call with
// no result event yet stays awaiting.

// Synthetic beta events, cast to the SDK union — the fold only reads the fields each
// `type` carries, so a partial object typed here is enough. Every event the events
// API returns is stamped with `processed_at`, so the helper supplies one.
function ev(e: Record<string, unknown>): BetaManagedAgentsSessionEvent {
  return {
    processed_at: "2024-01-01T00:00:00Z",
    ...e,
  } as unknown as BetaManagedAgentsSessionEvent;
}

const text = (t: string) => [{ type: "text", text: t }];

describe("foldEvents", () => {
  test("projects narration and paired tool calls onto the oneof stream", () => {
    const raw = [
      ev({ type: "session.status_running" }),
      ev({ type: "user.message", id: "u1", content: text("kickoff") }),
      ev({
        type: "agent.message",
        id: "m1",
        content: text("narrating **bold**"),
      }),
      // Custom shell tool: labelled by its model-stated intent, paired by
      // custom_tool_use_id.
      ev({
        type: "agent.custom_tool_use",
        id: "t1",
        name: "shell",
        input: { command: "ls /workspace", intent: "list the workspace" },
      }),
      ev({
        type: "user.custom_tool_result",
        id: "r1",
        custom_tool_use_id: "t1",
        content: text("doc.md"),
        is_error: false,
      }),
      // Prebuilt tool: labelled by its target field, paired by tool_use_id.
      ev({
        type: "agent.tool_use",
        id: "t2",
        name: "read",
        input: { file_path: "/workspace/doc.md" },
      }),
      ev({
        type: "user.tool_result",
        id: "r2",
        tool_use_id: "t2",
        content: text("contents"),
        is_error: false,
      }),
      // A tool with no result event yet → result stays absent (awaiting).
      ev({
        type: "agent.tool_use",
        id: "t3",
        name: "write",
        input: { file_path: "/workspace/out.md" },
      }),
      ev({ type: "session.status_idle", stop_reason: { type: "end_turn" } }),
    ];

    const { events } = foldEvents(raw);

    expect(events.map((e) => e.kind.case)).toEqual([
      "user",
      "assistant",
      "tool",
      "tool",
      "tool",
    ]);
    // Every projected event carries its ordering key.
    expect(events.every((e) => e.occurredAt !== undefined)).toBe(true);

    const [user, assistant, shell, read, write] = events;
    expect(user.kind.case === "user" && user.kind.value.text).toBe("kickoff");
    expect(
      assistant.kind.case === "assistant" && assistant.kind.value.text,
    ).toBe("narrating **bold**");

    if (shell.kind.case !== "tool") throw new Error("expected a tool call");
    expect(shell.kind.value).toMatchObject({
      name: "shell",
      intent: "list the workspace",
      command: "ls /workspace",
    });
    expect(shell.kind.value.result).toMatchObject({
      output: "doc.md",
      isError: false,
    });

    if (read.kind.case !== "tool") throw new Error("expected a tool call");
    expect(read.kind.value.intent).toBe("/workspace/doc.md");
    expect(read.kind.value.result?.output).toBe("contents");

    if (write.kind.case !== "tool") throw new Error("expected a tool call");
    expect(write.kind.value.result).toBeUndefined();
  });

  test("an agent turn with only tool calls emits no narration", () => {
    const { events } = foldEvents([
      ev({ type: "agent.message", id: "m", content: [] }),
      ev({
        type: "agent.tool_use",
        id: "t",
        name: "read",
        input: { file_path: "/x" },
      }),
    ]);
    expect(events.map((e) => e.kind.case)).toEqual(["tool"]);
  });
});

// The coordinator's own thread is the root; a spawned thread is a card on its stream and
// a body of its own. The session-scope listing IS the coordinator's thread listing, so a
// sub-agent's narration is not on it at all and its tool calls are only cross-posted
// there — which is what the properties below pin.

const ROOT = "sthr_root";
const THREAD = "sthr_child";

const cards = (events: readonly ConversationEvent[]): SubAgent[] =>
  events.flatMap((event) =>
    event.kind.case === "subAgent" ? event.kind.value : [],
  );

const kinds = (events: readonly ConversationEvent[]) =>
  events.map((event) => event.kind.case);

describe("a fan-out of spawned threads", () => {
  test("the root thread carries status events and still never becomes a card", () => {
    // The root's own status events are on this listing and name it, so folding a card
    // per thread id seen would draw the coordinator as its own sub-agent.
    const { events } = foldEvents([
      ev({ type: "session.thread_status_running", session_thread_id: ROOT }),
      ev({ type: "session.thread_created", session_thread_id: THREAD }),
      ev({ type: "session.thread_status_running", session_thread_id: THREAD }),
      ev({
        type: "session.thread_status_idle",
        session_thread_id: ROOT,
        stop_reason: { type: "end_turn" },
      }),
    ]);
    expect(cards(events).map((card) => card.threadId)).toEqual([THREAD]);
  });

  test("a status arriving before the thread's creation still folds to one card", () => {
    const { events } = foldEvents([
      ev({ type: "session.thread_status_running", session_thread_id: THREAD }),
      ev({ type: "session.thread_created", session_thread_id: THREAD }),
      ev({
        type: "session.thread_status_terminated",
        session_thread_id: THREAD,
      }),
    ]);
    expect(cards(events)).toHaveLength(1);
    // The card carries where the thread ended, not where it was first seen.
    expect(cards(events)[0].status).toBe(SubAgentStatus.DONE);
  });

  test("a retrying thread is still running, and one never heard from is too", () => {
    const status = (raw: BetaManagedAgentsSessionEvent[]) =>
      cards(foldEvents(raw).events)[0]?.status;
    expect(
      status([
        ev({ type: "session.thread_created", session_thread_id: THREAD }),
        ev({
          type: "session.thread_status_rescheduled",
          session_thread_id: THREAD,
        }),
      ]),
    ).toBe(SubAgentStatus.RUNNING);
    // Never the zero value: zero is what an unknown status name decodes to on a stale
    // client, so the projection always names a state.
    expect(
      status([
        ev({ type: "session.thread_created", session_thread_id: THREAD }),
      ]),
    ).toBe(SubAgentStatus.RUNNING);
  });

  test("every status the projection emits names a state — zero never leaves it", () => {
    // The client degrades a zero status rather than throwing (a stale tab decodes an
    // unknown status name to zero), so this is where a projection that stopped setting
    // the field fails.
    const upstream = [
      "session.thread_status_running",
      "session.thread_status_rescheduled",
      "session.thread_status_idle",
      "session.thread_status_terminated",
    ];
    for (const type of upstream) {
      const projected = cards(
        foldEvents([
          ev({ type: "session.thread_created", session_thread_id: THREAD }),
          ev({ type, session_thread_id: THREAD }),
        ]).events,
      );
      expect(projected).toHaveLength(1);
      expect(projected[0].status).not.toBe(SubAgentStatus.UNSPECIFIED);
    }
  });

  test("the card states the first instruction sent and the last reply returned", () => {
    // A thread the coordinator addressed without a `thread_created` is spawned all the
    // same; neither the instruction nor the reply is a turn of the coordinator's stream.
    const { events } = foldEvents([
      ev({
        type: "agent.thread_message_sent",
        id: "s1",
        to_session_thread_id: THREAD,
        content: text("gather the functional evidence"),
      }),
      ev({
        type: "agent.thread_message_received",
        id: "r1",
        from_session_thread_id: THREAD,
        content: text("a first pass"),
      }),
      ev({
        type: "agent.thread_message_sent",
        id: "s2",
        to_session_thread_id: THREAD,
        content: text("now check the frequency"),
      }),
      ev({
        type: "agent.thread_message_received",
        id: "r2",
        from_session_thread_id: THREAD,
        content: text("three alleles in gnomAD v4"),
      }),
    ]);
    expect(cards(events)).toEqual([
      expect.objectContaining({
        prompt: "gather the functional evidence",
        summary: "three alleles in gnomAD v4",
      }),
    ]);
    expect(kinds(events)).toEqual(["subAgent"]);
  });

  test("a sub-agent's tool call is its own thread's, not the coordinator's", () => {
    // Both halves together: the same call, cross-posted onto the session scope and
    // native to the thread scope, must be absent from one and present in the other.
    const call = {
      type: "agent.custom_tool_use",
      id: "t_sub",
      name: "shell",
      input: { command: "ls /corpus", intent: "list the corpus" },
    };
    const { events } = foldEvents([
      ev({ type: "session.thread_created", session_thread_id: THREAD }),
      ev({
        type: "agent.tool_use",
        id: "t_coordinator",
        name: "read",
        input: { file_path: "/workspace/document.md" },
      }),
      ev({ ...call, session_thread_id: THREAD }),
      // The result is cross-posted too, as its own record under its own event id.
      ev({
        type: "user.custom_tool_result",
        id: "r_sub_crossposted",
        custom_tool_use_id: "t_sub",
        content: text("corpus.md"),
      }),
    ]);
    expect(
      events.flatMap((event) => (event.kind.case === "tool" ? event.id : [])),
    ).toEqual(["t_coordinator"]);

    const body = foldThreadEvents([
      ev({
        type: "agent.thread_message_received",
        id: "m_in",
        from_session_thread_id: ROOT,
        content: text("list what the corpus holds"),
      }),
      ev(call),
      ev({
        type: "user.custom_tool_result",
        id: "r_sub_own",
        custom_tool_use_id: "t_sub",
        content: text("corpus.md"),
      }),
    ]);
    const tool = body.find((event) => event.kind.case === "tool");
    expect(tool?.id).toBe("t_sub");
    expect(tool?.kind.case === "tool" && tool.kind.value.result?.output).toBe(
      "corpus.md",
    );
  });

  test("at thread scope a stray cross-post marker does not vanish the row", () => {
    // The SDK promises `session_thread_id` empty on a thread's own events; if one
    // carries it anyway, the call is still this listing's own work and renders.
    const body = foldThreadEvents([
      ev({
        type: "agent.thread_message_received",
        id: "m_in",
        from_session_thread_id: ROOT,
        content: text("gather the evidence"),
      }),
      ev({
        type: "agent.custom_tool_use",
        id: "t_marked",
        name: "shell",
        input: { command: "ls /corpus", intent: "list the corpus" },
        session_thread_id: THREAD,
      }),
    ]);
    expect(kinds(body)).toEqual(["user", "tool"]);
  });

  test("a thread body opens on its instruction and carries no card", () => {
    const body = foldThreadEvents([
      ev({
        type: "agent.thread_message_received",
        id: "m_in",
        from_session_thread_id: ROOT,
        content: text("gather the evidence"),
      }),
      ev({ type: "session.thread_created", session_thread_id: "sthr_deeper" }),
      ev({ type: "session.thread_status_running", session_thread_id: THREAD }),
      ev({ type: "agent.message", id: "m1", content: text("reading") }),
      // The sender's own echo of narration it already emitted.
      ev({
        type: "agent.thread_message_sent",
        id: "m_out",
        to_session_thread_id: ROOT,
        content: text("reading"),
      }),
    ]);
    expect(kinds(body)).toEqual(["user", "assistant"]);
    expect(body[0].kind.case === "user" && body[0].kind.value.text).toBe(
      "gather the evidence",
    );
  });
});

describe("the projected stream's order", () => {
  const at = (id: string, second: number) =>
    ev({
      type: "agent.message",
      id,
      content: text(id),
      processed_at: `2024-01-01T00:00:0${second}Z`,
    });

  /** A client-originated event as it can arrive: with no `processed_at` at all. */
  const unstampedUser = (id: string) =>
    ({
      type: "user.message",
      id,
      content: text(id),
    }) as unknown as BetaManagedAgentsSessionEvent;

  test("is its stamps, with position breaking a tie and an absent stamp inheriting", () => {
    // The thread-scope listing takes no `order` parameter at all, so the projection
    // orders rather than trusting the page order; the key has to be total for that.
    const unstamped = unstampedUser("unstamped");
    const { events } = foldEvents([
      at("late", 3),
      at("early", 1),
      at("tie-first", 2),
      at("tie-second", 2),
      unstamped,
    ]);

    expect(events.map((event) => event.id)).toEqual([
      "early",
      "tie-first",
      "tie-second",
      "unstamped",
      "late",
    ]);
    // The inherited stamp is on the wire, not only in the sort: another consumer
    // ordering the same proto3-JSON by `occurred_at` reaches the same stream.
    const stamps = events.map((event) =>
      event.occurredAt ? timestampDate(event.occurredAt).getTime() : null,
    );
    expect(stamps).toEqual([...stamps].sort((a, b) => (a ?? 0) - (b ?? 0)));
    expect(stamps[3]).toBe(stamps[2]);
  });

  test("a leading unstamped run inherits the first stamp behind it, not the epoch", () => {
    // The kickoff `user.message` can head a live listing unstamped; inheriting the
    // last-stamp-seen alone would start at zero and put 1970 on the wire.
    const { events } = foldEvents([unstampedUser("kickoff"), at("opening", 5)]);
    expect(events.map((event) => event.id)).toEqual(["kickoff", "opening"]);
    const stamps = events.map((event) =>
      event.occurredAt ? timestampDate(event.occurredAt).getTime() : null,
    );
    expect(stamps[0]).toBe(stamps[1]);
    expect(stamps[0]).not.toBeNull();
    expect(stamps[0]).not.toBe(0);
  });

  test("places a card where the thread's first event landed, not where it ended", () => {
    const { events } = foldEvents([
      ev({
        type: "agent.message",
        id: "before",
        content: text("delegating"),
        processed_at: "2024-01-01T00:00:01Z",
      }),
      ev({
        type: "session.thread_created",
        session_thread_id: THREAD,
        processed_at: "2024-01-01T00:00:02Z",
      }),
      ev({
        type: "agent.message",
        id: "after",
        content: text("carrying on"),
        processed_at: "2024-01-01T00:00:03Z",
      }),
      ev({
        type: "session.thread_status_idle",
        session_thread_id: THREAD,
        stop_reason: { type: "end_turn" },
        processed_at: "2024-01-01T00:00:04Z",
      }),
    ]);
    expect(events.map((event) => event.id)).toEqual([
      "before",
      THREAD,
      "after",
    ]);
  });
});

/** The client with its one API seam replaced: records what would be sent, and can be
 *  told to fail either call. */
class StubbedClient extends AnthropicClient {
  readonly sends: { sessionId: string; params: EventSendParams }[] = [];
  readonly creates: unknown[] = [];

  constructor(
    private readonly failures: {
      send?: unknown;
      list?: unknown;
    } = {},
  ) {
    super({
      federationRuleId: "rule",
      organizationId: "org",
      serviceAccountId: "sa",
      workspaceId: "ws",
      agentId: "agent",
      environmentId: "env",
    });
  }

  protected override beta(): Anthropic["beta"] {
    const stub = {
      sessions: {
        create: async (params: unknown) => {
          this.creates.push(params);
          return { id: "sesn_created" };
        },
        events: {
          send: async (sessionId: string, params: EventSendParams) => {
            if (this.failures.send) throw this.failures.send;
            this.sends.push({ sessionId, params });
          },
          list: () => {
            if (this.failures.list) throw this.failures.list;
            return [];
          },
        },
      },
    };
    return stub as unknown as Anthropic["beta"];
  }
}

const notFound = () =>
  Object.assign(new Error("upstream said 404"), { status: 404 });

describe("sending a user turn", () => {
  test("the curator's text reaches the named session as a user.message", async () => {
    const client = new StubbedClient();
    await client.sendUserMessage(
      "sesn_1",
      "Treat the exon as clinically relevant.",
    );

    expect(client.sends).toHaveLength(1);
    const [send] = client.sends;
    expect(send.sessionId).toBe("sesn_1");
    expect(send.params.events).toEqual([
      {
        type: "user.message",
        content: [
          { type: "text", text: "Treat the exon as clinically relevant." },
        ],
      },
    ]);
    // The header gates the whole Managed Agents surface and is optional in the SDK's
    // params type, so dropping it compiles and 400s every send.
    expect(send.params.betas).toContain(MANAGED_AGENTS_BETA);
  });

  test("a blank turn is refused before the API is reached", async () => {
    const client = new StubbedClient();
    const error = await client
      .sendUserMessage("sesn_1", "   \n  ")
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(Error);
    // A blank turn is a broken caller invariant, not a reference that resolves to
    // nothing: it must not borrow the not-found that hides existence.
    expect(error).not.toBeInstanceOf(ResourceNotFoundError);
    expect(client.sends).toEqual([]);
  });

  test("a failed send propagates unchanged rather than resolving to not-found", async () => {
    // The analysis was resolved and membership cleared before the send, so a refusing
    // session is our database and the session store disagreeing — a broken invariant
    // (→ 500, logged), never a not-found the curator caused. The 404 branch logs
    // nothing, so remapping here would lose the one trace a dropped turn leaves.
    const failure = notFound();
    const client = new StubbedClient({ send: failure });
    await expect(client.sendUserMessage("sesn_1", "Most")).rejects.toBe(
      failure,
    );
  });

  test("a busy refusal surfaces typed — a state to act on, not a masked failure", async () => {
    // The API's refusal while a tool result is pending; its only discriminator is the
    // message text, mirrored here as the SDK carries it (`400 {json body}`).
    const refusal = Object.assign(
      new Error(
        '400 {"type":"error","error":{"type":"invalid_request_error","message":"Invalid user.message event at events[0]: waiting on responses to events [sevt_x]; only `user.tool_confirmation`, `user.custom_tool_result`, `user.tool_result`, or `user.interrupt` may be sent (a `system.message` may trail a tool result)"}}',
      ),
      { status: 400 },
    );
    const client = new StubbedClient({ send: refusal });
    await expect(
      client.sendUserMessage("sesn_1", "Most"),
    ).rejects.toBeInstanceOf(SessionBusyError);
  });

  test("a different 400 propagates unchanged rather than reading as busy", async () => {
    const failure = Object.assign(new Error("400 malformed request"), {
      status: 400,
    });
    const client = new StubbedClient({ send: failure });
    await expect(client.sendUserMessage("sesn_1", "Most")).rejects.toBe(
      failure,
    );
  });

  test("an unknown session on the poll is still a typed not-found", async () => {
    // The counterpart to the case above: on a read a 404 can mean a stale client's
    // unknown session, so the remap stays. The two directions differ deliberately.
    const client = new StubbedClient({ list: notFound() });
    await expect(client.listEvents("sesn_gone")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
  });

  test("the halt reaches the named session as a user.interrupt", async () => {
    const client = new StubbedClient();
    await client.sendInterrupt("sesn_1");

    expect(client.sends).toHaveLength(1);
    const [send] = client.sends;
    expect(send.sessionId).toBe("sesn_1");
    expect(send.params.events).toEqual([{ type: "user.interrupt" }]);
    expect(send.params.betas).toContain(MANAGED_AGENTS_BETA);
  });

  test("a blank kickoff mints no session", async () => {
    // The body is built before the session, so a refusal leaves nothing behind.
    const client = new StubbedClient();
    await expect(client.createSession("  ")).rejects.toBeInstanceOf(Error);
    expect(client.creates).toEqual([]);
    expect(client.sends).toEqual([]);
  });
});
