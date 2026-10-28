import { type Client, createClient } from "@connectrpc/connect";
import { createGrpcTransport } from "@connectrpc/connect-node";
import { Auth, type GetAccessResponse } from "@/gen/themis/rpc/auth_pb";
import type { AccessPort } from "../../ports";
import { tracingInterceptor } from "../../tracing/connect";
import type { AuthServiceConfig } from "./config";
import { idTokenInterceptor } from "./id-token";

// What a person may reach, as the auth service reads it from the ACL. The web tier calls as
// itself, with its own ID token and no claim: it authenticated the person, and relays nobody's.

/** How long one GetAccess may take. It sits on every request, twice, so a slow answer is a fault. */
export const GET_ACCESS_DEADLINE_MS = 5_000;

export class LiveAccess implements AccessPort {
  constructor(
    private readonly client: Client<typeof Auth>,
    private readonly deadlineMs: number = GET_ACCESS_DEADLINE_MS,
  ) {}

  async getAccess(email: string): Promise<GetAccessResponse> {
    return this.client.getAccess({ email }, { timeoutMs: this.deadlineMs });
  }
}

export function createAccess(config: AuthServiceConfig): AccessPort {
  const transport = createGrpcTransport({
    baseUrl: config.authUrl,
    interceptors: [tracingInterceptor, idTokenInterceptor(config.authUrl)],
  });
  return new LiveAccess(createClient(Auth, transport));
}
