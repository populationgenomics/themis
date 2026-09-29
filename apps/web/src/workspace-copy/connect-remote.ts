import { type Client, Code, ConnectError } from "@connectrpc/connect";
import {
  RELAY_DEADLINES_MS,
  type WorkspaceCall,
} from "@/lib/workspace-deadlines";
import type { Workbench } from "@/models/workbench";
import {
  DownloadError,
  PublishFaultError,
  type PublishRefusal,
  PublishRefusedError,
  type Remote,
  StalePackListError,
  WorkspaceDamagedError,
} from "./remote";

// The copy's remote in the SharedWorker: the Workbench's workspace-repository rpcs over Connect,
// same-origin so the browser carries the IAP cookie, and pack downloads with `fetch`.

/** The codes `PublishWorkspace` passes through as refusals, each as the design names its response. */
const REFUSALS: ReadonlyMap<Code, PublishRefusal> = new Map([
  [Code.Aborted, "raceLost"],
  [Code.FailedPrecondition, "branchMoved"],
  [Code.ResourceExhausted, "overCeiling"],
  [Code.InvalidArgument, "malformed"],
]);

/** Codes after which a publish's outcome is unknown: the response was lost on the way (a network
 *  failure reaches Connect as `Unknown`), or the BFF masked a fault of the service as `Internal`.
 *  Sending the identical intent again is safe, since the service answers one that landed with
 *  success. */
const FAULTS: ReadonlySet<Code> = new Set([
  Code.Unknown,
  Code.Internal,
  Code.Unavailable,
  Code.DeadlineExceeded,
]);

/** A relayed call's failure as the copy's error: `DataLoss`, which every relayed call can answer,
 *  is a damaged repository; anything else is the error as it came. */
export function relayFailure(error: unknown): Error {
  const connect = ConnectError.from(error);
  return connect.code === Code.DataLoss
    ? new WorkspaceDamagedError(connect.rawMessage)
    : connect;
}

/** A publish's failure as the copy's error: a refusal, a fault, a damaged repository, or the error
 *  as it came. */
export function publishFailure(error: unknown): Error {
  const connect = ConnectError.from(error);
  const refusal = REFUSALS.get(connect.code);
  if (refusal !== undefined)
    return new PublishRefusedError(refusal, connect.rawMessage);
  if (FAULTS.has(connect.code)) {
    return new PublishFaultError(
      `the publish's outcome is unknown: ${connect.message}`,
      {
        cause: error,
      },
    );
  }
  return relayFailure(connect);
}

/** How long each call may take. Every call a hydration or publish makes runs under the copy's
 *  exclusive lock, so one that never answers would hold the lock for as long as the worker lives. */
export interface Deadlines {
  /** Each relayed rpc, longer than the BFF's own deadline on the call it relays. */
  rpcMs: Readonly<Record<WorkspaceCall, number>>;
  /** How long a download may go without a byte arriving before it counts as failed. A whole pack
   *  may take longer than this; a stalled one may not. */
  stallMs: number;
}

export const DEADLINES: Deadlines = {
  rpcMs: RELAY_DEADLINES_MS,
  stallMs: 30_000,
};

export function connectRemote(
  client: Client<typeof Workbench>,
  analysisId: string,
  origin: string,
  deadlines: Deadlines,
): Remote {
  return {
    async readRefDoc() {
      try {
        return await client.readWorkspaceRefDoc(
          { analysisId },
          { timeoutMs: deadlines.rpcMs.readRefDoc },
        );
      } catch (error) {
        throw relayFailure(error);
      }
    },
    async signPackUrls(packIds) {
      try {
        return (
          await client.signWorkspacePackUrls(
            { analysisId, packIds: [...packIds] },
            { timeoutMs: deadlines.rpcMs.signPackUrls },
          )
        ).packs;
      } catch (error) {
        const connect = ConnectError.from(error);
        if (connect.code === Code.FailedPrecondition) {
          throw new StalePackListError(connect.rawMessage);
        }
        throw relayFailure(connect);
      }
    },
    // A signed URL is a bearer capability: fetched once, never logged, and never followed.
    download: (url, size) =>
      download(new URL(url, origin), size, deadlines.stallMs),
    async publish(intent, pack) {
      try {
        const response = await client.publishWorkspace(
          { analysisId, intent, packBytes: [pack] },
          { timeoutMs: deadlines.rpcMs.publish },
        );
        return response.generation;
      } catch (error) {
        throw publishFailure(error);
      }
    },
  };
}

/** A pack's bytes, aborted as a failed download once `stallMs` pass with none arriving. Any failure
 *  — a refusal, a reset partway through the body, a stall — is a `DownloadError`, which hydration
 *  answers by signing the URL afresh. */
async function download(
  url: URL,
  size: number,
  stallMs: number,
): Promise<Uint8Array> {
  const controller = new AbortController();
  let watchdog: ReturnType<typeof setTimeout> | undefined;
  const progressed = () => {
    clearTimeout(watchdog);
    watchdog = setTimeout(() => controller.abort(), stallMs);
  };
  progressed();
  try {
    const response = await fetch(url, {
      cache: "no-store",
      redirect: "error",
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new DownloadError(
        `a pack download was answered ${response.status}`,
      );
    }
    // Written into one buffer of the declared size as it arrives, so a pack is held once, not
    // once in chunks and again joined.
    const bytes = new Uint8Array(size);
    let at = 0;
    if (response.body !== null) {
      const reader = response.body.getReader();
      for (;;) {
        progressed();
        const { done, value } = await reader.read();
        if (done) break;
        if (at + value.length > size) {
          controller.abort();
          throw new DownloadError(
            `a pack download ran past the ${size} bytes its signing declared`,
          );
        }
        bytes.set(value, at);
        at += value.length;
      }
    }
    if (at !== size) {
      throw new DownloadError(
        `a pack download ended at ${at} of the ${size} bytes its signing declared`,
      );
    }
    return bytes;
  } catch (error) {
    if (error instanceof DownloadError) throw error;
    const stalled = controller.signal.aborted;
    throw new DownloadError(
      stalled
        ? `a pack download stalled for ${stallMs / 1000} s`
        : "a pack download failed",
      { cause: error },
    );
  } finally {
    clearTimeout(watchdog);
  }
}
