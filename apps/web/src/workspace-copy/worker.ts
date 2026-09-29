import "./buffer-global";
import { createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { Workbench } from "@/models/workbench";
import { indexedDbLedger, lightningStorage } from "./browser-storage";
import { connectRemote, DEADLINES } from "./connect-remote";
import { webLocks } from "./locks";
import {
  parseRequest,
  type RequestEnvelope,
  type ResponseEnvelope,
  type SerializedError,
} from "./protocol";
import { PublishRefusedError } from "./remote";
import { workerCopyService } from "./worker-service";

// The SharedWorker that owns every copy of a workspace repository this browser holds. Each window
// connects a port and sends requests; each is answered once, on the port it came from. Its script is
// a bundler chunk under /_next/static/chunks/, which the proxy serves under the worker policy.

interface ConnectEvent {
  ports: readonly MessagePort[];
}

const origin = self.location.origin;
const client = createClient(
  Workbench,
  createConnectTransport({
    baseUrl: new URL("/api/rpc", origin).href,
    defaultTimeoutMs: Math.max(...Object.values(DEADLINES.rpcMs)),
  }),
);
const service = workerCopyService({
  storage: lightningStorage(),
  remote: (analysisId) => connectRemote(client, analysisId, origin, DEADLINES),
  ledger: indexedDbLedger(),
  locks: webLocks(navigator.locks),
});

// Every request but a ping waits on the reconciliation, so none opens a copy it is deleting. One
// that fails is tried again by the next request rather than failing every request after it.
let reconciled: Promise<void> | undefined;
function ready(): Promise<void> {
  reconciled ??= service.reconcile().catch((error: unknown) => {
    reconciled = undefined;
    throw error;
  });
  return reconciled;
}

function serialize(error: unknown): SerializedError {
  if (error instanceof PublishRefusedError) {
    return { name: error.name, message: error.message, refusal: error.refusal };
  }
  if (error instanceof Error)
    return { name: error.name, message: error.message };
  return { name: "Error", message: String(error) };
}

async function answer(port: MessagePort, message: unknown): Promise<void> {
  let envelope: RequestEnvelope;
  try {
    envelope = parseRequest(message);
  } catch (error) {
    const id = (message as { id?: unknown } | null)?.id;
    console.error("workspace copy refused a malformed request", error);
    if (typeof id === "number") {
      port.postMessage({
        id,
        ok: false,
        error: serialize(error),
      } satisfies ResponseEnvelope);
    }
    return;
  }
  let response: ResponseEnvelope;
  try {
    const request = envelope.request;
    if (request.method !== "ping") await ready();
    const value = await service.handle(request);
    response = { id: envelope.id, ok: true, value };
  } catch (error) {
    console.error(
      "workspace copy request failed",
      envelope.request.method,
      error,
    );
    response = { id: envelope.id, ok: false, error: serialize(error) };
  }
  port.postMessage(response);
}

(self as unknown as { onconnect: (event: ConnectEvent) => void }).onconnect = (
  event,
) => {
  const [port] = event.ports;
  port.onmessage = (message: MessageEvent<unknown>) => {
    void answer(port, message.data);
  };
  port.start();
};
