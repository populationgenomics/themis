import { ConnectError, createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { Workbench } from "@/models/workbench";

// The browser's Workbench client, built from the same service descriptor the BFF's handler
// serves. The TanStack Query hooks in lib/queries.ts are its only callers; no component
// reaches it directly.

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
