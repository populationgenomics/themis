import Anthropic from "@anthropic-ai/sdk";
import { AnthropicClient } from "./client";
import type { AnthropicConfig } from "./config";

export const TEST_ANTHROPIC_CONFIG: AnthropicConfig = {
  federationRuleId: "rule_test",
  organizationId: "org_test",
  serviceAccountId: "sa_test",
  workspaceId: "ws_test",
  agentId: "agent_test",
  environmentId: "env_test",
};

export interface RecordedRequest {
  readonly method: string;
  readonly url: URL;
  readonly headers: Headers;
  readonly body: unknown;
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export function anthropicErrorResponse(
  status: number,
  type: string,
  message: string,
): Response {
  return jsonResponse({ type: "error", error: { type, message } }, status);
}

type AnthropicFetch = NonNullable<
  ConstructorParameters<typeof Anthropic>[0]
>["fetch"];

/** Build an `AnthropicClient` backed by the real `@anthropic-ai/sdk` over an in-process `fetch`
 *  handler (no network, no retries, isolated from ambient `ANTHROPIC_*` env vars). */
export function anthropicClientOverFetch(
  handler: (req: RecordedRequest) => Response | Promise<Response>,
  config: AnthropicConfig = TEST_ANTHROPIC_CONFIG,
): { client: AnthropicClient; requests: RecordedRequest[] } {
  const requests: RecordedRequest[] = [];
  const fetchImpl: AnthropicFetch = async (input, init) => {
    const request = new Request(input, init);
    const rawBody = await request.text();
    const recorded: RecordedRequest = {
      method: request.method,
      url: new URL(request.url),
      headers: request.headers,
      body: rawBody === "" ? null : (JSON.parse(rawBody) as unknown),
    };
    requests.push(recorded);
    return handler(recorded);
  };
  const sdk = new Anthropic({
    apiKey: null,
    authToken: null,
    webhookKey: null,
    credentials: async () => ({
      token: "federated-access-token",
      expiresAt: Math.floor(Date.now() / 1000) + 3600,
    }),
    baseURL: "https://api.anthropic.com",
    maxRetries: 0,
    fetch: fetchImpl,
  });
  return { client: new AnthropicClient(config, sdk), requests };
}
