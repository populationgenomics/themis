import Anthropic from "@anthropic-ai/sdk";
import { oidcFederationProvider } from "@anthropic-ai/sdk/lib/credentials/oidc-federation";
import type { BetaManagedAgentsSessionEvent } from "@anthropic-ai/sdk/resources/beta/sessions/events";
import { create } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import { GoogleAuth } from "google-auth-library";
import {
  type ConversationEvent,
  ConversationEventSchema,
} from "@/models/workbench";
import { ResourceNotFoundError } from "../../errors";
import { deriveToolLabel, toolCommand } from "../../tool-label";
import type { AnthropicConfig } from "./config";

// The Managed Agents control/data plane over WIF Path B. Creates the agent session,
// kicks it off with the user's prompt as a `user.message`, and pages the
// session's event log — folding the raw beta events into the projected conversation
// stream: agent message → assistant, user message → user, a tool call
// (prebuilt `agent.tool_use` or the custom `shell` tool's `agent.custom_tool_use`)
// → a tool event paired with its later result. Stateless: `listEvents` re-pages the
// whole log each poll (the events API has no replay-since cursor); the client
// re-projects and the caller replaces by id.

const MANAGED_AGENTS_BETA = "managed-agents-2026-04-01";

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

  private beta(): Anthropic["beta"] {
    if (!this.client) {
      this.client = buildClient(this.config);
    }
    return this.client.beta;
  }

  /** Create the agent session and seed it with the user prompt. Returns the
   *  Anthropic-minted session id (the HMAC input for the bearer). */
  async createSession(prompt: string): Promise<string> {
    const beta = this.beta();
    const session = await beta.sessions.create({
      agent: this.config.agentId,
      environment_id: this.config.environmentId,
      betas: [MANAGED_AGENTS_BETA],
    });
    await beta.sessions.events.send(session.id, {
      events: [
        { type: "user.message", content: [{ type: "text", text: prompt }] },
      ],
      betas: [MANAGED_AGENTS_BETA],
    });
    return session.id;
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
}

/** The pure projection of a paged session log — exported for the projection test;
 *  `listEvents` wraps it around the SDK paging. */
export function foldEvents(
  raw: readonly BetaManagedAgentsSessionEvent[],
): SessionEvents {
  return { events: mapEvents(raw) };
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
 *  originating tool-call event's `id`, so the pairing is uniform downstream. */
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

/** An event's RFC 3339 `processed_at` as a proto Timestamp — the ordering key on the
 *  projected stream. Absent on the event types that carry no timestamp; the client
 *  falls back to stream order there. */
function eventTime(processedAt: string | null | undefined) {
  return processedAt ? timestampFromDate(new Date(processedAt)) : undefined;
}

function mapEvents(
  raw: readonly BetaManagedAgentsSessionEvent[],
): ConversationEvent[] {
  const results = collectToolResults(raw);
  const out: ConversationEvent[] = [];
  for (const event of raw) {
    switch (event.type) {
      case "agent.message": {
        const text = joinText(event.content);
        // A turn with no text (an agent turn that only makes tool calls) is not a
        // narration.
        if (text === "") break;
        out.push(
          create(ConversationEventSchema, {
            id: event.id,
            occurredAt: eventTime(event.processed_at),
            kind: { case: "assistant", value: { text } },
          }),
        );
        break;
      }
      case "user.message": {
        const text = joinText(event.content);
        if (text === "") break;
        out.push(
          create(ConversationEventSchema, {
            id: event.id,
            occurredAt: eventTime(event.processed_at),
            kind: { case: "user", value: { text } },
          }),
        );
        break;
      }
      // Both shapes carry the same name + input; the custom `shell` tool arrives as
      // custom_tool_use. `result` is absent until the paired result event lands.
      case "agent.tool_use":
      case "agent.custom_tool_use":
        out.push(
          create(ConversationEventSchema, {
            id: event.id,
            occurredAt: eventTime(event.processed_at),
            kind: {
              case: "tool",
              value: {
                name: event.name,
                intent: deriveToolLabel(event.name, event.input),
                command: toolCommand(event.input),
                result: results.get(event.id),
              },
            },
          }),
        );
        break;
      default:
        // thinking, spans, thread lifecycle, mcp tool calls, etc. have no
        // conversation display slot.
        break;
    }
  }
  return out;
}
