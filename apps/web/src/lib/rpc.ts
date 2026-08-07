import { ConnectError, createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { Workbench } from "@/models/workbench";

// The browser's Workbench client, built from the same service descriptor the BFF's handler serves.
// Two callers, not the components directly: the TanStack Query hooks in lib/queries.ts (the
// version-keyed, cache-shared reads), and the paper read seam in lib/api.ts (describePaper / locate,
// which are one-shot and not cache-keyed). Whether the api.ts seam should itself route through
// lib/queries.ts hooks is an open shape question (see lib/api.ts) — until it's settled, both reach
// this client.

const transport = createConnectTransport({
  // Same-origin, so the browser carries the IAP cookie.
  baseUrl: "/api/rpc",
});

export const workbench = createClient(Workbench, transport);

/** The message to show a curator for a failed call. `ConnectError.message` is prefixed
 *  with its code (`[invalid_argument] …`), which is for logs, not for a person. */
export function errorMessage(error: unknown): string {
  if (error instanceof ConnectError) {
    return error.rawMessage;
  }
  return error instanceof Error ? error.message : String(error);
}
