import type { PublishIntent, RefDocSnapshot, SignedPack } from "@/models/sheaf";
import { WORKSPACE_DAMAGED } from "./protocol";

// What the copy needs from the outside, for one Analysis: the three Workbench rpcs that relay the
// sheaf service, and a download of a signed URL. The SharedWorker implements it over Connect and
// `fetch`; tests implement it over the fixture store.

/** Each relayed call raises `WorkspaceDamagedError` when the service reports the repository
 *  damaged. */
export interface Remote {
  readRefDoc(): Promise<RefDocSnapshot>;
  /** Signed URLs for at most 256 packs the current document lists, in the order asked. Raises
   *  `StalePackListError` when the document no longer lists one of them. */
  signPackUrls(packIds: readonly string[]): Promise<SignedPack[]>;
  /** The `size` bytes behind a signed URL, as its signing declared them. Raises `DownloadError` on
   *  any failure, a body of any other length included. */
  download(url: string, size: number): Promise<Uint8Array>;
  /** Publish one commit's pack. Raises `PublishRefusedError` for the four refusals the relay passes
   *  through, `PublishFaultError` when the outcome is unknown (the response was lost, or the service
   *  faulted), and anything else as it came. */
  publish(intent: PublishIntent, pack: Uint8Array): Promise<bigint>;
}

/** The repository is damaged: its stored document does not parse, or a pack it lists is not held.
 *  No call clears it, so nothing that meets it is tried again. */
export class WorkspaceDamagedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = WORKSPACE_DAMAGED;
  }
}

/** The pack list a signing request named is stale: read the ref document again. */
export class StalePackListError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StalePackListError";
  }
}

export class DownloadError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "DownloadError";
  }
}

/** The four refusals of a publish the browser acts on, each as the design names its response. */
export type PublishRefusal =
  | "raceLost"
  | "branchMoved"
  | "overCeiling"
  | "malformed";

export class PublishRefusedError extends Error {
  constructor(
    readonly refusal: PublishRefusal,
    message: string,
  ) {
    super(message);
    this.name = "PublishRefusedError";
  }
}

/** A publish whose outcome is unknown: sending the identical intent again is safe, since the service
 *  answers a publish that already landed with success. */
export class PublishFaultError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "PublishFaultError";
  }
}
