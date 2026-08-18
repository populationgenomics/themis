import Anthropic from "@anthropic-ai/sdk";
import { oidcFederationProvider } from "@anthropic-ai/sdk/lib/credentials/oidc-federation";
import type {
  BetaManagedAgentsSessionEvent,
  EventSendParams,
} from "@anthropic-ai/sdk/resources/beta/sessions/events";
import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import { GoogleAuth } from "google-auth-library";
import {
  type ConversationEvent,
  ConversationEventSchema,
  SubAgentStatus,
} from "@/models/workbench";
import { ResourceNotFoundError, SessionBusyError } from "../../errors";
import { projectToolCall } from "../../tool-projection";
import type { AnthropicConfig } from "./config";

// The Managed Agents control/data plane over WIF Path B. Creates the agent session,
// kicks it off with the user's prompt as a `user.message`, and pages the
// session's event log — folding the raw beta events into the projected conversation
// stream: agent message → assistant, user message → user, a tool call
// (prebuilt `agent.tool_use` or the custom `shell` tool's `agent.custom_tool_use`)
// → a tool event paired with its later result, and each spawned thread → one
// sub-agent card. Stateless: `listEvents` re-pages the whole log each poll (the
// events API has no replay-since cursor); the client re-projects and the caller
// replaces by id. `listThreadEvents` pages one spawned thread's own log the same way
// (docs/design/conversation-view.md).

export const MANAGED_AGENTS_BETA = "managed-agents-2026-04-01";

/** The send body for one `user.message` turn. Seeding a new session and steering a
 *  running one are the same event, so both go through here — the blank check
 *  included, which keeps an empty turn off the wire on either path. A blank turn is a
 *  broken caller invariant, so it raises rather than borrowing the not-found that
 *  hides existence. */
function userMessageSend(text: string): EventSendParams {
  if (text.trim() === "") {
    throw new Error("refusing a blank user.message turn");
  }
  return {
    events: [{ type: "user.message", content: [{ type: "text", text }] }],
    betas: [MANAGED_AGENTS_BETA],
  };
}

// WIF Path B: the SDK exchanges a Google-signed OIDC ID token (minted for the
// runtime SA the federation rule pins) for a short-lived Anthropic token via the
// RFC 7523 jwt-bearer grant. google-auth requests the token with `format=full`, so
// it carries the `email` claim the rule matches.
const ANTHROPIC_AUDIENCE = "https://api.anthropic.com";

/** Build the Anthropic client on the federation credential provider. `apiKey` /
 *  `authToken` are pinned null so a stray `ANTHROPIC_*` env can never outrank WIF
 *  (the SDK wraps `credentials` in its own refreshing token cache). */
function buildClient(config: AnthropicConfig): Anthropic {
  const auth = new GoogleAuth();
  const credentials = oidcFederationProvider({
    // `fetchIdToken` bypasses the client's own token cache: the federation
    // provider calls this on each Anthropic-token refresh and needs a fresh
    // assertion.
    identityTokenProvider: async () => {
      const idTokenClient = await auth.getIdTokenClient(ANTHROPIC_AUDIENCE);
      return idTokenClient.idTokenProvider.fetchIdToken(ANTHROPIC_AUDIENCE);
    },
    federationRuleId: config.federationRuleId,
    organizationId: config.organizationId,
    serviceAccountId: config.serviceAccountId,
    workspaceId: config.workspaceId,
    baseURL: ANTHROPIC_AUDIENCE,
    fetch: globalThis.fetch,
  });
  return new Anthropic({ apiKey: null, authToken: null, credentials });
}

/** The event log projected to the conversation stream. */
export interface SessionEvents {
  events: ConversationEvent[];
}

export class AnthropicClient {
  private client?: Anthropic;

  constructor(private readonly config: AnthropicConfig) {}

  /** The API surface every call goes through; overridable, so a subclass can stand in
   *  for the network. */
  protected beta(): Anthropic["beta"] {
    if (!this.client) {
      this.client = buildClient(this.config);
    }
    return this.client.beta;
  }

  /** Create the agent session and seed it with the user prompt. Returns the
   *  Anthropic-minted session id (the HMAC input for the bearer). The body is built
   *  before the session is minted, so a blank prompt leaves no empty session behind. */
  async createSession(prompt: string): Promise<string> {
    const body = userMessageSend(prompt);
    const beta = this.beta();
    const session = await beta.sessions.create({
      agent: this.config.agentId,
      environment_id: this.config.environmentId,
      betas: [MANAGED_AGENTS_BETA],
    });
    await beta.sessions.events.send(session.id, body);
    return session.id;
  }

  /** Append a curator turn to a live session — the same send that seeds a new one.
   *  A busy refusal — the session is mid-step, accepting only tool-result-shaped
   *  events until the pending call resolves — is a state the curator acts on (wait,
   *  or interrupt), so it surfaces typed. Every other failure propagates: the caller
   *  resolved the analysis before calling, so any other refusal is our database and
   *  the session store disagreeing — a broken invariant (→ 500, logged), not a caller
   *  reference that resolves to nothing. A turn the session never accepted must never
   *  be reported as delivered. */
  async sendUserMessage(sessionId: string, text: string): Promise<void> {
    try {
      await this.beta().sessions.events.send(sessionId, userMessageSend(text));
    } catch (error) {
      if (isBusyRefusal(error)) {
        throw new SessionBusyError(`session ${sessionId} is mid-step`);
      }
      throw error;
    }
  }

  /** Halt the session's current step: one `user.interrupt` event. The API closes any
   *  pending tool call with an error result and idles the session; against an
   *  already-idle session the interrupt is a no-op, so the send is safe to race a
   *  step completing. Every failure propagates, for `sendUserMessage`'s reason. */
  async sendInterrupt(sessionId: string): Promise<void> {
    await this.beta().sessions.events.send(sessionId, {
      events: [{ type: "user.interrupt" }],
      betas: [MANAGED_AGENTS_BETA],
    });
  }

  /** Page the whole session log and project it. An unknown session id → a typed
   *  not-found (→ 404); every other failure propagates (→ 500). */
  async listEvents(sessionId: string): Promise<SessionEvents> {
    const beta = this.beta();
    const raw: BetaManagedAgentsSessionEvent[] = [];
    try {
      for await (const event of beta.sessions.events.list(sessionId, {
        order: "asc",
        betas: [MANAGED_AGENTS_BETA],
      })) {
        raw.push(event);
      }
    } catch (error) {
      if (isNotFoundError(error)) {
        throw new ResourceNotFoundError(`unknown session: ${sessionId}`);
      }
      throw error;
    }
    return foldEvents(raw);
  }

  /** Page one spawned thread's own log and project it — the body a curator's expanded
   *  sub-agent card reveals. Scoping the listing by `sessionId` is the authorization
   *  (the `ThreadRequest` proto comment). */
  async listThreadEvents(
    sessionId: string,
    threadId: string,
  ): Promise<ConversationEvent[]> {
    const raw: BetaManagedAgentsSessionEvent[] = [];
    try {
      for await (const event of this.beta().sessions.threads.events.list(
        threadId,
        { session_id: sessionId, betas: [MANAGED_AGENTS_BETA] },
      )) {
        raw.push(event);
      }
    } catch (error) {
      if (isNotFoundError(error)) {
        throw new ResourceNotFoundError(`unknown thread: ${threadId}`);
      }
      throw error;
    }
    return foldThreadEvents(raw);
  }
}

/** The pure projection of a paged session log — exported for the projection test;
 *  `listEvents` wraps it around the SDK paging. */
export function foldEvents(
  raw: readonly BetaManagedAgentsSessionEvent[],
): SessionEvents {
  return {
    events: project(raw, { kind: "session", threads: scanThreads(raw) }),
  };
}

/** The pure projection of one spawned thread's own paged log. */
export function foldThreadEvents(
  raw: readonly BetaManagedAgentsSessionEvent[],
): ConversationEvent[] {
  return project(raw, { kind: "thread" });
}

/** True when the API refused an event send because the session is awaiting responses
 *  to earlier events — a tool call in flight. The refusal is a plain 400
 *  `invalid_request_error` whose only discriminator is this message text (no
 *  structured code exists), so a reworded upstream message degrades to the unmatched
 *  path (→ 500), never to a misclassification. The phrase is specific to the
 *  pending-tool-events refusal; the budget-cap refusal words itself differently. */
function isBusyRefusal(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  const { status, message } = error as { status?: unknown; message?: unknown };
  return (
    status === 400 &&
    typeof message === "string" &&
    message.includes("waiting on responses to events")
  );
}

/** True when an SDK error is a 404 (the session id is unknown), so the caller can
 *  map it to `ResourceNotFoundError` (→ 404) instead of a masking 500. */
function isNotFoundError(error: unknown): boolean {
  if (error instanceof Anthropic.NotFoundError) return true;
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    (error as { status?: unknown }).status === 404
  );
}

/** Join the text blocks of an event's content into one string. A tool result may
 *  carry no content blocks (an empty result). */
function joinText(
  content: ReadonlyArray<{ type: string; text?: string | null }> | undefined,
): string {
  return (content ?? [])
    .filter((block) => block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("");
}

interface ToolResultInit {
  output: string;
  isError: boolean;
}

/** Pair each tool result to its call, keyed for O(1) lookup as the stream is
 *  folded. A prebuilt tool result (`user.tool_result` / `agent.tool_result`) keys
 *  on `tool_use_id`; a custom tool result (`user.custom_tool_result`, the `shell`
 *  tool the worker answers) keys on `custom_tool_use_id`. Both id spaces match the
 *  originating tool-call event's `id`, so the pairing is uniform downstream.
 *
 *  Pairing is within one listing: a sub-agent's result is cross-posted to the
 *  session scope as a second record with its own event id, so a call and the result
 *  a card's body shows it are read from the same log, never merged across two. */
function collectToolResults(
  raw: readonly BetaManagedAgentsSessionEvent[],
): Map<string, ToolResultInit> {
  const results = new Map<string, ToolResultInit>();
  for (const event of raw) {
    if (
      event.type === "user.tool_result" ||
      event.type === "agent.tool_result"
    ) {
      results.set(event.tool_use_id, {
        output: joinText(event.content),
        isError: event.is_error === true,
      });
    } else if (event.type === "user.custom_tool_result") {
      results.set(event.custom_tool_use_id, {
        output: joinText(event.content),
        isError: event.is_error === true,
      });
    }
  }
  return results;
}

/** Milliseconds for an event's RFC 3339 `processed_at`, or null where it carries none
 *  or one that does not parse. */
function stampMs(processedAt: string | null | undefined): number | null {
  if (!processedAt) return null;
  const ms = Date.parse(processedAt);
  return Number.isNaN(ms) ? null : ms;
}

/** What a card states about one spawned thread, folded over the whole log before the
 *  stream is projected — so the card is complete where its first event placed it,
 *  whichever of its events comes first. */
interface ThreadFold {
  status: SubAgentStatus;
  /** Absent until the coordinator's first message lands — absent, not empty, because a
   *  message with no text block folds to an empty string. */
  prompt: string | undefined;
  /** Absent until the thread returns a message, on the same terms. */
  summary: string | undefined;
}

/** The display state a thread's status event puts its card in. A rescheduled thread is
 *  retrying a transient failure, so it is still running. */
const THREAD_STATUS: Readonly<Record<string, SubAgentStatus>> = {
  "session.thread_status_running": SubAgentStatus.RUNNING,
  "session.thread_status_rescheduled": SubAgentStatus.RUNNING,
  "session.thread_status_idle": SubAgentStatus.IDLE,
  "session.thread_status_terminated": SubAgentStatus.DONE,
};

/** The threads the coordinator spawned, each with its latest status, the first
 *  instruction sent to it and the last reply returned from it. A thread is spawned iff
 *  it was created or addressed: the root thread is neither — it carries status events
 *  of its own and receives replies, but nothing creates it and nothing sends to it — so
 *  it never becomes a card. */
function scanThreads(
  raw: readonly BetaManagedAgentsSessionEvent[],
): Map<string, ThreadFold> {
  const threads = new Map<string, ThreadFold>();
  const spawn = (id: string) => {
    // A spawned thread that has reported no status yet is running; the zero value is
    // never projected.
    if (!threads.has(id)) {
      threads.set(id, {
        status: SubAgentStatus.RUNNING,
        prompt: undefined,
        summary: undefined,
      });
    }
  };
  for (const event of raw) {
    if (event.type === "session.thread_created") spawn(event.session_thread_id);
    else if (event.type === "agent.thread_message_sent")
      spawn(event.to_session_thread_id);
  }
  for (const event of raw) {
    switch (event.type) {
      case "agent.thread_message_sent": {
        const fold = threads.get(event.to_session_thread_id);
        if (fold && fold.prompt === undefined) {
          fold.prompt = joinText(event.content);
        }
        break;
      }
      case "agent.thread_message_received": {
        const fold = threads.get(event.from_session_thread_id);
        if (fold) fold.summary = joinText(event.content);
        break;
      }
      case "session.thread_status_running":
      case "session.thread_status_rescheduled":
      case "session.thread_status_idle":
      case "session.thread_status_terminated": {
        const fold = threads.get(event.session_thread_id);
        if (fold) fold.status = THREAD_STATUS[event.type];
        break;
      }
      default:
        break;
    }
  }
  return threads;
}

/** Which log is being projected. A session-scope listing is the coordinator's own
 *  thread listing, with each spawned thread's status events and tool uses cross-posted
 *  onto it; a thread-scope listing is one spawned thread's own. */
type Scope =
  | { kind: "session"; threads: ReadonlyMap<string, ThreadFold> }
  | { kind: "thread" };

/** The variant an event is projected to — the init shape of the oneof, less the
 *  already-built-message form the init type also admits. */
type KindInit = Extract<
  MessageInitShape<typeof ConversationEventSchema>,
  { $typeName?: undefined }
>["kind"];

/** A projected event with the total, stable key the stream is ordered by: the stamp it
 *  was given, and the position it was folded at, which breaks a tie between equal
 *  stamps. */
interface Ordered {
  id: string;
  kind: KindInit;
  /** Null while no stamp has been seen; a leading unstamped run is filled in after the
   *  fold, from the first stamp behind it. */
  ms: number | null;
  position: number;
}

function project(
  raw: readonly BetaManagedAgentsSessionEvent[],
  scope: Scope,
): ConversationEvent[] {
  const results = collectToolResults(raw);
  const out: Ordered[] = [];
  const carded = new Set<string>();
  // An event upstream left unstamped inherits the last stamp seen, so the key stays
  // total and it sorts where it was folded.
  let ms: number | null = null;

  const push = (id: string, kind: KindInit) => {
    out.push({ id, kind, ms, position: out.length });
  };

  /** Place a spawned thread's card where its first event landed, once. */
  const placeCard = (threadId: string) => {
    if (scope.kind !== "session" || carded.has(threadId)) return;
    const fold = scope.threads.get(threadId);
    if (!fold) return;
    carded.add(threadId);
    push(threadId, { case: "subAgent", value: { threadId, ...fold } });
  };

  for (const event of raw) {
    ms = stampMs(event.processed_at) ?? ms;
    switch (event.type) {
      case "agent.message": {
        const text = joinText(event.content);
        // A turn with no text (an agent turn that only makes tool calls) is not a
        // narration.
        if (text === "") break;
        push(event.id, { case: "assistant", value: { text } });
        break;
      }
      case "user.message": {
        const text = joinText(event.content);
        if (text === "") break;
        push(event.id, { case: "user", value: { text } });
        break;
      }
      // The instruction a thread was given: its own user turn on its stream. On the
      // coordinator's it is a reply arriving from a sub-agent, which the card already
      // states as its summary.
      case "agent.thread_message_received": {
        if (scope.kind === "session") {
          placeCard(event.from_session_thread_id);
          break;
        }
        const text = joinText(event.content);
        if (text === "") break;
        push(event.id, { case: "user", value: { text } });
        break;
      }
      // At session scope, the coordinator's outbound instruction — the card states it
      // as its prompt; at thread scope, the sender's own echo of its reply. Never a row.
      case "agent.thread_message_sent":
        placeCard(event.to_session_thread_id);
        break;
      case "session.thread_created":
        placeCard(event.session_thread_id);
        break;
      case "session.thread_status_running":
      case "session.thread_status_rescheduled":
      case "session.thread_status_idle":
      case "session.thread_status_terminated":
        placeCard(event.session_thread_id);
        break;
      // Both shapes carry the same name + input; the custom `shell` tool arrives as
      // custom_tool_use. `result` is absent until the paired result event lands.
      case "agent.tool_use":
      case "agent.custom_tool_use": {
        // Set ⇒ cross-posted from a sub-agent's thread (the SDK documents the field, and
        // promises it empty at thread scope): card material at session scope, a row of
        // the thread's own stream anywhere else.
        if (scope.kind === "session" && event.session_thread_id) {
          placeCard(event.session_thread_id);
          break;
        }
        push(event.id, {
          case: "tool",
          value: {
            ...projectToolCall(event.name, event.input),
            result: results.get(event.id),
          },
        });
        break;
      }
      default:
        // thinking, spans, mcp tool calls, etc. have no conversation display slot.
        break;
    }
  }
  // A leading unstamped run inherits *forward* from the first stamp behind it: epoch
  // zero on the wire would be a fabricated instant, not an ordering key.
  const firstStamp = out.find((entry) => entry.ms !== null)?.ms ?? null;
  for (const entry of out) {
    if (entry.ms !== null) break;
    entry.ms = firstStamp;
  }
  return out
    .sort((a, b) => (a.ms ?? 0) - (b.ms ?? 0) || a.position - b.position)
    .map((entry) =>
      create(ConversationEventSchema, {
        id: entry.id,
        // Unset only when the whole listing is unstamped: there is no instant to state.
        occurredAt:
          entry.ms === null ? undefined : timestampFromDate(new Date(entry.ms)),
        kind: entry.kind,
      }),
    );
}
