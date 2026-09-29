import { create, type MessageInitShape } from "@bufbuild/protobuf";
import {
  type CallOptions,
  type Client,
  Code,
  ConnectError,
  createClient,
  encodeBinaryHeader,
} from "@connectrpc/connect";
import { createGrpcTransport } from "@connectrpc/connect-node";
import { CallerClaimSchema } from "@/gen/themis/rpc/auth_pb";
import { CallingAs } from "@/gen/themis/rpc/sandbox_options_pb";
import {
  SHEAF_DEADLINES_MS,
  type SheafDeadlines,
} from "@/lib/workspace-deadlines";
import {
  type PublishIntent,
  type PublishRequestSchema,
  type PublishResponse,
  type RefDocSnapshot,
  Sheaf,
  type SignPackUrlsResponse,
} from "@/models/sheaf";
import type { Analysis } from "@/models/workbench";
import {
  ResourceNotFoundError,
  WorkspaceDamagedError,
  WorkspacePackNotListedError,
  WorkspacePublishError,
  type WorkspacePublishFailure,
} from "../../errors";
import type { WorkspaceRepository } from "../../ports";
import type { SheafConfig } from "./config";
import { idTokenInterceptor } from "./id-token";

// The live workspace repository: the sheaf service over gRPC, called as the web tier. Each call
// presents the web tier's own ID token and names the Analysis through a self claim carrying a session
// bearer derived from the analysis row (docs/design/rpc-authorization.md), so the service opens that
// Analysis's repository and no other.

/** The metadata key the claim rides on (`themis/clients/auth/claim.py`, `CLAIM_METADATA`). */
export const CLAIM_METADATA = "x-themis-claim-bin";

/** Under gRPC's 4 MiB default per-message limit, with margin, as the sandbox worker chunks. */
export const PUBLISH_CHUNK_BYTES = 1 << 20;

/** Derives the session bearer an Analysis's session id is MAC-signed to (`derive.ts`). */
export interface BearerDeriver {
  deriveBearer(sessionId: string): Promise<string>;
}

/** The sheaf codes a publish passes to the browser as themselves, by what each means. */
const PUBLISH_FAILURES: ReadonlyMap<Code, WorkspacePublishFailure> = new Map([
  [Code.Aborted, "raceLost"],
  [Code.FailedPrecondition, "branchMoved"],
  [Code.ResourceExhausted, "overCeiling"],
  [Code.InvalidArgument, "malformed"],
]);

type PublishRequestInit = MessageInitShape<typeof PublishRequestSchema>;

/** The Publish stream for one intent: the intent, then each pack's bytes in order, in chunks under
 *  the message limit. A generator, so a chunk is cut only as the transport draws it and a stream the
 *  service answers early stops being drawn. */
export async function* publishStream(
  intent: PublishIntent,
  packs: readonly Uint8Array[],
): AsyncGenerator<PublishRequestInit> {
  yield { message: { case: "intent", value: intent } };
  for (const [pack, bytes] of packs.entries()) {
    for (let start = 0; start < bytes.length; start += PUBLISH_CHUNK_BYTES) {
      yield {
        message: {
          case: "chunk",
          value: {
            pack,
            content: bytes.subarray(start, start + PUBLISH_CHUNK_BYTES),
          },
        },
      };
    }
  }
}

export class LiveWorkspace implements WorkspaceRepository {
  constructor(
    private readonly client: Client<typeof Sheaf>,
    private readonly deriver: BearerDeriver,
    private readonly deadlines: SheafDeadlines,
  ) {}

  async readRefDoc(
    analysis: Analysis,
    signal: AbortSignal,
  ): Promise<RefDocSnapshot> {
    const options = await this.callingFor(
      analysis,
      this.deadlines.readRefDoc,
      signal,
    );
    try {
      return await this.client.readRefDoc({}, options);
    } catch (error) {
      throw damageTyped(analysis, error);
    }
  }

  async signPackUrls(
    analysis: Analysis,
    packIds: readonly string[],
    signal: AbortSignal,
  ): Promise<SignPackUrlsResponse> {
    const options = await this.callingFor(
      analysis,
      this.deadlines.signPackUrls,
      signal,
    );
    try {
      return await this.client.signPackUrls({ packIds: [...packIds] }, options);
    } catch (error) {
      if (error instanceof ConnectError && error.code === Code.NotFound) {
        throw new WorkspacePackNotListedError(
          `${analysis.id}: ${error.rawMessage}`,
        );
      }
      throw damageTyped(analysis, error);
    }
  }

  async publish(
    analysis: Analysis,
    intent: PublishIntent,
    packs: readonly Uint8Array[],
  ): Promise<PublishResponse> {
    const options = await this.callingFor(analysis, this.deadlines.publish);
    try {
      return await this.client.publish(publishStream(intent, packs), options);
    } catch (error) {
      if (error instanceof ConnectError) {
        const failure = PUBLISH_FAILURES.get(error.code);
        if (failure !== undefined) {
          throw new WorkspacePublishError(failure, error.rawMessage);
        }
      }
      throw damageTyped(analysis, error);
    }
  }

  async servePack(analysis: Analysis, packId: string): Promise<Response> {
    throw new ResourceNotFoundError(
      `${analysis.id}: pack ${packId} is served from the bucket, by the URL the sheaf service signed`,
    );
  }

  /** The call options naming `analysis` to the service: a self claim carrying its session bearer,
   *  and what is left of the call's `timeoutMs` once the bearer is derived, so the derivation and
   *  the rpc share one deadline and one request. */
  private async callingFor(
    analysis: Analysis,
    timeoutMs: number,
    signal?: AbortSignal,
  ): Promise<CallOptions> {
    const deadline = Date.now() + timeoutMs;
    const sessionToken = await beforeDeadline(
      this.deriver.deriveBearer(analysis.sessionId),
      deadline,
      signal,
    );
    const claim = create(CallerClaimSchema, {
      callingAs: CallingAs.SELF,
      sessionToken,
    });
    return {
      headers: {
        [CLAIM_METADATA]: encodeBinaryHeader(claim, CallerClaimSchema),
      },
      signal,
      timeoutMs: remainingMs(deadline),
    };
  }
}

/** The damage the service's `DataLoss` reports about `analysis`'s repository; any other failure as
 *  it was raised. */
function damageTyped(analysis: Analysis, error: unknown): unknown {
  return error instanceof ConnectError && error.code === Code.DataLoss
    ? new WorkspaceDamagedError(`${analysis.id}: ${error.rawMessage}`)
    : error;
}

/** The milliseconds left before `deadline`, raising `DeadlineExceeded` when none are. */
function remainingMs(deadline: number): number {
  const left = deadline - Date.now();
  if (left <= 0) {
    throw new ConnectError("the call's deadline passed", Code.DeadlineExceeded);
  }
  return left;
}

/** `work`, or `DeadlineExceeded` at `deadline`, or `Canceled` when `signal` aborts — the codes the
 *  rpc itself answers with for either. The work is not stopped, only no longer waited on. */
async function beforeDeadline<T>(
  work: Promise<T>,
  deadline: number,
  signal: AbortSignal | undefined,
): Promise<T> {
  if (signal?.aborted) {
    throw new ConnectError("the request was cancelled", Code.Canceled);
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  let onAbort: (() => void) | undefined;
  const stop = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () =>
        reject(
          new ConnectError("the call's deadline passed", Code.DeadlineExceeded),
        ),
      remainingMs(deadline),
    );
    onAbort = () =>
      reject(new ConnectError("the request was cancelled", Code.Canceled));
    signal?.addEventListener("abort", onAbort, { once: true });
  });
  try {
    return await Promise.race([work, stop]);
  } finally {
    clearTimeout(timer);
    if (onAbort !== undefined) signal?.removeEventListener("abort", onAbort);
  }
}

/** The live workspace repository over the sheaf service `config` names. */
export function createWorkspace(
  config: SheafConfig,
  deriver: BearerDeriver,
): WorkspaceRepository {
  const transport = createGrpcTransport({
    baseUrl: config.sheafUrl,
    interceptors: [idTokenInterceptor(config.sheafUrl)],
  });
  return new LiveWorkspace(
    createClient(Sheaf, transport),
    deriver,
    SHEAF_DEADLINES_MS,
  );
}
