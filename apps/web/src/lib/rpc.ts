import type { DescMethod } from "@bufbuild/protobuf";
import { Code, ConnectError, createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { Workbench } from "@/models/workbench";

// The browser's Workbench client, built from the same service descriptor the BFF's handler serves.
// Two callers, not the components directly: the TanStack Query hooks in lib/queries.ts (the poll and
// the calls it drives), and the paper read seam in lib/api.ts (describePaper / locate, which are
// one-shot and not cache-keyed). The workspace-repository methods have a client of their own, in the
// SharedWorker that holds the browser's copy (workspace-copy/worker.ts). Whether the api.ts seam should itself route through
// lib/queries.ts hooks is an open shape question (see lib/api.ts) — until it's settled, both reach
// this client.

const transport = createConnectTransport({
  // Same-origin, so the browser carries the IAP cookie.
  baseUrl: "/api/rpc",
});

export const workbench = createClient(Workbench, transport);

/** True when a call to `method` failed because the agent is mid-step: the composer words this
 *  itself, pointing at the stop control beside it. Only a turn is refused that way, as Connect
 *  `FailedPrecondition`; the same code from another method means something else, such as a publish
 *  whose branch moved, so the code alone does not say the agent is busy. */
export function isAgentBusy(method: DescMethod, error: unknown): boolean {
  return (
    method === Workbench.method.steer &&
    error instanceof ConnectError &&
    error.code === Code.FailedPrecondition
  );
}

/** The message to show a curator for a failed call. `ConnectError.message` is prefixed
 *  with its code (`[invalid_argument] …`), which is for logs, not for a person. */
export function errorMessage(error: unknown): string {
  if (error instanceof ConnectError) {
    return error.rawMessage;
  }
  return error instanceof Error ? error.message : String(error);
}
