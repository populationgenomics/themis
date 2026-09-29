import type {
  CopyMethod,
  CopyRequest,
  CopyResults,
  RequestEnvelope,
  ResponseEnvelope,
  SerializedError,
} from "./protocol";

// A window's handle on the SharedWorker that owns every copy of a workspace repository. Every window
// of the workbench — the main one and its mirrors — reaches the same worker, so they read one copy
// and exactly one process writes it.

/** A failure the worker reported, with the class name it had there. */
export class CopyRequestError extends Error {
  readonly refusal: string | undefined;

  constructor(error: SerializedError) {
    super(error.message);
    this.name = error.name;
    this.refusal = error.refusal;
  }
}

/** The SharedWorker stopped answering, so every request in flight was failed. The next request
 *  starts the worker again. */
export class CopyWorkerLostError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CopyWorkerLostError";
  }
}

/** A publish whose worker was lost before it answered, so the edit is unconfirmed: it may or may
 *  not have landed, and nothing recovers it. The widget redraws from the tip — the next Poll shows
 *  the tip moving if it did land — and tells the curator their last change may not have been saved
 *  and to redo it if it is missing. */
export class PublishUnconfirmedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PublishUnconfirmedError";
  }
}

/** Consecutive heartbeat ticks a ping may go unanswered before the worker counts as dead. Counted
 *  in ticks, not wall-clock time, so a hidden tab whose timers the browser throttles to one a
 *  minute does not give up on a live worker. */
export const MISSED_PINGS = 6;
/** How often a window with requests in flight pings the worker. */
const HEARTBEAT_MS = 5_000;

/** Calls `tick` at intervals until the returned function stops it. */
export type Heartbeat = (tick: () => void) => () => void;

const intervalHeartbeat: Heartbeat = (tick) => {
  const interval = setInterval(tick, HEARTBEAT_MS);
  return () => clearInterval(interval);
};

/** A port to the worker, and a way to hear that its script failed. */
export interface WorkerPort {
  port: MessagePort;
  onFailure(listener: () => void): void;
}

type Pending = {
  method: CopyMethod;
  resolve: (value: never) => void;
  reject: (error: Error) => void;
};

class CopyConnection {
  private readonly pending = new Map<number, Pending>();
  private nextId = 1;
  private stopHeartbeat: (() => void) | undefined;
  private outstandingPing: number | undefined;
  private missedPings = 0;
  private lost = false;

  constructor(
    private readonly port: MessagePort,
    private readonly heartbeat: Heartbeat,
    private readonly onLost: () => void,
  ) {
    port.onmessage = (event: MessageEvent<ResponseEnvelope>) =>
      this.receive(event.data);
    port.start();
  }

  request<M extends CopyMethod>(
    request: Extract<CopyRequest, { method: M }>,
  ): Promise<CopyResults[M]> {
    if (this.lost)
      throw new Error("unreachable: a lost connection is replaced");
    const id = this.nextId;
    this.nextId += 1;
    return new Promise<CopyResults[M]>((resolve, reject) => {
      this.pending.set(id, {
        method: request.method,
        resolve: resolve as (value: never) => void,
        reject,
      });
      this.port.postMessage({ id, request } satisfies RequestEnvelope);
      if (request.method !== "ping") this.watch();
    });
  }

  /** Fail every request in flight: the worker is gone, so none will be answered. */
  abandon(reason: string): void {
    if (this.lost) return;
    this.lost = true;
    this.stopHeartbeat?.();
    this.stopHeartbeat = undefined;
    for (const waiting of this.pending.values()) {
      waiting.reject(
        waiting.method === "publish"
          ? new PublishUnconfirmedError(
              `${reason}; the edit may not have been saved`,
            )
          : new CopyWorkerLostError(reason),
      );
    }
    this.pending.clear();
    this.port.close();
    this.onLost();
  }

  private watch(): void {
    if (this.stopHeartbeat !== undefined) return;
    this.missedPings = 0;
    this.stopHeartbeat = this.heartbeat(() => this.tick());
  }

  private tick(): void {
    const waiting = [...this.pending.values()].some((p) => p.method !== "ping");
    if (!waiting) {
      this.stopHeartbeat?.();
      this.stopHeartbeat = undefined;
      return;
    }
    if (this.outstandingPing !== undefined) {
      this.missedPings += 1;
      if (this.missedPings >= MISSED_PINGS) {
        this.abandon(
          `the workspace copy's SharedWorker left ${MISSED_PINGS} pings in a row unanswered`,
        );
      }
      return;
    }
    const ping = this.nextId;
    this.outstandingPing = ping;
    this.request({ method: "ping" }).then(
      () => {
        if (this.outstandingPing === ping) this.outstandingPing = undefined;
        this.missedPings = 0;
      },
      () => {},
    );
  }

  private receive(message: ResponseEnvelope): void {
    const waiting = this.pending.get(message.id);
    if (waiting === undefined) {
      throw new Error(
        `the workspace copy answered a request never sent: ${message.id}`,
      );
    }
    this.pending.delete(message.id);
    if (message.ok) waiting.resolve(message.value as never);
    else waiting.reject(new CopyRequestError(message.error));
  }
}

/** The copy's reads and its write, as a window calls them, over ports `open` starts. */
export function createCopyClient(
  open: () => WorkerPort,
  heartbeat: Heartbeat = intervalHeartbeat,
) {
  let connection: CopyConnection | undefined;
  const connect = (): CopyConnection => {
    if (connection === undefined) {
      const { port, onFailure } = open();
      const opened: CopyConnection = new CopyConnection(port, heartbeat, () => {
        if (connection === opened) connection = undefined;
      });
      onFailure(() =>
        opened.abandon("the workspace copy's SharedWorker failed"),
      );
      connection = opened;
    }
    return connection;
  };
  return {
    /** Bring `analysisId`'s copy up to date unless its branch is at `tip` or past it already, and
     *  clear a commit a lost worker left pending. */
    sync: (analysisId: string, tip: string) =>
      connect().request({ method: "sync", analysisId, tip }),
    readDocument: (analysisId: string, commit: string) =>
      connect().request({ method: "readDocument", analysisId, commit }),
    readFile: (analysisId: string, commit: string, path: string) =>
      connect().request({ method: "readFile", analysisId, commit, path }),
    /** The versions the picker lists, read once the copy holds `tip`. */
    history: (analysisId: string, tip: string) =>
      connect().request({ method: "history", analysisId, tip }),
    /** Delete `analysisId`'s copy whole once nothing holds its lock, discarding a commit waiting
     *  on its publish; the next read hydrates it from nothing. */
    reset: (analysisId: string) =>
      connect().request({ method: "reset", analysisId }),
    /** Whether `ancestor` is `descendant` or reachable from it, for commits the copy holds. */
    isAncestor: (analysisId: string, ancestor: string, descendant: string) =>
      connect().request({
        method: "isAncestor",
        analysisId,
        ancestor,
        descendant,
      }),
    /** Publish an edit as the curator whose email the BFF verified for this page. Resolves to how
     *  it ended, `fileChanged` when a file it replaces changed under the curator and it was not
     *  applied. Rejects with `PublishUnconfirmedError` if the worker is lost first. */
    publish: (
      request: Omit<Extract<CopyRequest, { method: "publish" }>, "method">,
    ) => connect().request({ method: "publish", ...request }),
  };
}

function openSharedWorker(): WorkerPort {
  if (typeof SharedWorker === "undefined") {
    throw new Error(
      "this browser has no SharedWorker, which the workspace copy needs",
    );
  }
  const worker = new SharedWorker(new URL("./worker.ts", import.meta.url), {
    name: "themis-workspace-copy",
  });
  return {
    port: worker.port,
    // A worker whose script fails to load or run answers nothing.
    onFailure: (listener) => {
      worker.onerror = listener;
    },
  };
}

/** The window's one client, opening the worker on first use and again after it is lost. */
export const workspaceCopy = createCopyClient(openSharedWorker);
