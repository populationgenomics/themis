import { timestampMs } from "@bufbuild/protobuf/wkt";
import type { RefDoc, SignedPack } from "@/models/sheaf";
import type { DocumentState, WorkspaceCopy } from "./copy";
import { sha256Hex } from "./pack";
import { DownloadError, type Remote, StalePackListError } from "./remote";

// Bringing a copy up to date, as the sandbox worker's mirror does (docs/design/workbench-workspace.md,
// "The browser keeps its own copy of the repository"): read the ref document, sign URLs for the packs
// the copy lacks, download and verify and index each, then flush and set the refs to exactly what the
// document says, and delete the packs it no longer lists. Refs move only at the end, so a hydration
// cut short leaves the packs it indexed and the next one asks only for the rest.

/** The most pack ids one signing request may name. */
export const SIGN_BATCH = 256;
/** How often a hydration reads the ref document again after finding its pack list stale. */
const MAX_DOCUMENT_READS = 4;
/** How often one pack is signed and downloaded before the hydration fails. */
const MAX_DOWNLOAD_ATTEMPTS = 3;
/** A URL that expires sooner than this is signed afresh before its download starts. */
const EXPIRY_MARGIN_MS = 60_000;

/** The document state a copy was brought to; what a publish is built against. */
export type Hydrated = DocumentState;

/** Decides whether a copy may grow to a size, before any byte of the growth is downloaded. */
export interface Admission {
  /** Raises when a copy of `bytes` is refused; may make room first, by evicting other copies. */
  admit(bytes: number): Promise<void>;
}

/** The ref document kept changing under a hydration, or its packs could not be downloaded. */
export class HydrationError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "HydrationError";
  }
}

/** Bring `copy` to the repository's current ref document. Raises `WorkspaceDamagedError` from the
 *  first read or signing that finds the repository damaged, with nothing read or signed again. On
 *  any failure the copy's cache is replaced, since an index read during the failed attempt may be
 *  one that attempt left bad. */
export async function hydrate(
  copy: WorkspaceCopy,
  remote: Remote,
  admission: Admission,
  now: () => number = Date.now,
): Promise<Hydrated> {
  try {
    for (let read = 1; read <= MAX_DOCUMENT_READS; read += 1) {
      const snapshot = await remote.readRefDoc();
      const document = snapshot.document;
      if (document === undefined) {
        if (snapshot.generation !== BigInt(0)) {
          throw new HydrationError(
            `a repository with no document at generation ${snapshot.generation}`,
          );
        }
        await copy.setRefs(new Map(), snapshot.generation);
        await copy.prunePacks(new Set());
        return { generation: snapshot.generation, refs: new Map() };
      }
      const refs = documentRefs(document);
      try {
        await fetchMissing(copy, remote, admission, document.packs, now);
      } catch (error) {
        if (error instanceof StalePackListError) continue;
        throw error;
      }
      await copy.setRefs(refs, snapshot.generation);
      // After the refs, so no ref ever names an object only a deleted pack held.
      await copy.prunePacks(new Set(document.packs));
      return { generation: snapshot.generation, refs };
    }
    throw new HydrationError(
      `the ref document's pack list was stale on ${MAX_DOCUMENT_READS} reads in a row`,
    );
  } catch (error) {
    copy.resetCache();
    throw error;
  }
}

/** The refs a document names, each a commit id. Raises on a symbolic ref, which no writer of a
 *  sheaf repository publishes. */
export function documentRefs(document: RefDoc): Map<string, string> {
  const refs = new Map<string, string>();
  for (const [name, target] of Object.entries(document.refs)) {
    if (target.target.case !== "oid") {
      throw new HydrationError(`${name} is not a commit in the ref document`);
    }
    refs.set(name, target.target.value);
  }
  return refs;
}

async function fetchMissing(
  copy: WorkspaceCopy,
  remote: Remote,
  admission: Admission,
  listed: readonly string[],
  now: () => number,
): Promise<void> {
  const have = await copy.indexedPacks();
  const missing = listed.filter((id) => !have.has(id));
  if (missing.length === 0) return;
  // A compaction's new pack is indexed beside the packs it replaced; the cache is dropped first so
  // those are not held in memory through the indexing peak.
  if (await copy.holdsUnlisted(new Set(listed))) copy.resetCache();
  const signed = new Map<string, SignedPack>();
  for (const pack of await signAll(remote, missing))
    signed.set(pack.packId, pack);
  let incoming = 0;
  for (const pack of signed.values()) incoming += Number(pack.size);
  // Measured by the packs the document lists: packs it no longer lists are deleted once the refs
  // move, so they do not count against the copy.
  await admission.admit((await copy.packBytes(listed)) + incoming);
  for (const packId of missing) {
    const first = signed.get(packId);
    if (first === undefined)
      throw new HydrationError(`no URL was signed for pack ${packId}`);
    await copy.addPack(packId, await download(remote, first, now));
  }
}

async function signAll(
  remote: Remote,
  packIds: readonly string[],
): Promise<SignedPack[]> {
  const signed: SignedPack[] = [];
  for (let at = 0; at < packIds.length; at += SIGN_BATCH) {
    const batch = packIds.slice(at, at + SIGN_BATCH);
    const packs = await remote.signPackUrls(batch);
    if (
      packs.length !== batch.length ||
      packs.some((pack, i) => pack.packId !== batch[i])
    ) {
      throw new HydrationError(
        "the signed URLs do not answer the packs asked for, in order",
      );
    }
    signed.push(...packs);
  }
  return signed;
}

/** One pack's bytes, verified against its id. A URL about to expire, and every URL after a failed
 *  attempt, is signed afresh: an expired URL's refusal can reach a browser as an opaque network
 *  error, so the same URL is never tried twice. */
async function download(
  remote: Remote,
  first: SignedPack,
  now: () => number,
): Promise<Uint8Array> {
  let pack = first;
  let lastFailure: unknown;
  for (let attempt = 1; attempt <= MAX_DOWNLOAD_ATTEMPTS; attempt += 1) {
    if (attempt > 1 || expiresSoon(pack, now)) {
      [pack] = await signAll(remote, [first.packId]);
    }
    try {
      const bytes = await remote.download(pack.url, Number(pack.size));
      const digest = await sha256Hex(bytes);
      if (digest !== pack.packId) {
        throw new DownloadError(
          `pack ${pack.packId} downloaded as bytes hashing to ${digest}`,
        );
      }
      return bytes;
    } catch (error) {
      if (!(error instanceof DownloadError)) throw error;
      lastFailure = error;
    }
  }
  throw new HydrationError(
    `pack ${first.packId} failed to download ${MAX_DOWNLOAD_ATTEMPTS} times`,
    { cause: lastFailure },
  );
}

function expiresSoon(pack: SignedPack, now: () => number): boolean {
  if (pack.expireTime === undefined) {
    throw new HydrationError(`pack ${pack.packId} was signed with no expiry`);
  }
  return timestampMs(pack.expireTime) - now() < EXPIRY_MARGIN_MS;
}
