import { create } from "@bufbuild/protobuf";
import type { PublishIntent, SignedPack } from "@/models/sheaf";
import { type Analysis, AnalysisSchema } from "@/models/workbench";
import {
  FixtureWorkspace,
  packPath,
} from "@/server/adapters/fixture/workspace";
import {
  isWorkspacePackNotListedError,
  isWorkspacePublishError,
} from "@/server/errors";
import {
  DownloadError,
  PublishFaultError,
  type PublishRefusal,
  PublishRefusedError,
  type Remote,
  StalePackListError,
} from "./remote";

// A copy's remote over the offline sheaf store, which decides a publish as the service does. Each
// call is counted, and a test can make the next downloads or a publish's response go missing.

const REFUSALS: Record<string, PublishRefusal> = {
  raceLost: "raceLost",
  branchMoved: "branchMoved",
  overCeiling: "overCeiling",
  malformed: "malformed",
};

/** The BFF request a store call rides on, which never goes away here: no browser leaves. */
const NEVER = new AbortController().signal;

export interface FixtureRemote extends Remote {
  analysis: Analysis;
  store: FixtureWorkspace;
  calls: {
    readRefDoc: number;
    signed: string[];
    downloads: string[];
    publishes: number;
  };
  /** Fail the next `count` downloads. */
  failDownloads(count: number): void;
  /** Store the next publish and then lose its response. */
  loseNextResponse(): void;
  /** Run `hook` before the next publish reaches the store: another writer racing this one. */
  beforeNextPublish(hook: () => Promise<void>): void;
  /** Corrupt the bytes the next download returns. */
  corruptNextDownload(): void;
}

export function fixtureRemote(
  store: FixtureWorkspace,
  analysisId: string,
): FixtureRemote {
  const analysis = create(AnalysisSchema, {
    id: analysisId,
    sessionId: `sess_${analysisId}`,
  });
  const calls = {
    readRefDoc: 0,
    signed: [] as string[],
    downloads: [] as string[],
    publishes: 0,
  };
  let failingDownloads = 0;
  let corrupt = false;
  let loseResponse = false;
  let hook: (() => Promise<void>) | undefined;
  return {
    analysis,
    store,
    calls,
    failDownloads: (count) => {
      failingDownloads = count;
    },
    loseNextResponse: () => {
      loseResponse = true;
    },
    beforeNextPublish: (next) => {
      hook = next;
    },
    corruptNextDownload: () => {
      corrupt = true;
    },
    async readRefDoc() {
      calls.readRefDoc += 1;
      return store.readRefDoc(analysis, NEVER);
    },
    async signPackUrls(packIds: readonly string[]): Promise<SignedPack[]> {
      calls.signed.push(...packIds);
      try {
        return (await store.signPackUrls(analysis, packIds, NEVER)).packs;
      } catch (error) {
        if (isWorkspacePackNotListedError(error))
          throw new StalePackListError(error.message);
        throw error;
      }
    },
    async download(url: string, size: number): Promise<Uint8Array> {
      calls.downloads.push(url);
      if (failingDownloads > 0) {
        failingDownloads -= 1;
        throw new DownloadError(`download of ${url} failed`);
      }
      const packId = url.split("/").at(-1);
      if (packId === undefined || url !== packPath(analysis.id, packId)) {
        throw new Error(`not a fixture pack URL: ${url}`);
      }
      const bytes = new Uint8Array(
        await (await store.servePack(analysis, packId)).arrayBuffer(),
      );
      if (corrupt) {
        corrupt = false;
        bytes[bytes.length - 1] ^= 0xff;
      }
      if (bytes.length !== size) {
        throw new DownloadError(
          `${url} delivered ${bytes.length} of ${size} bytes`,
        );
      }
      return bytes;
    },
    async publish(intent: PublishIntent, pack: Uint8Array): Promise<bigint> {
      calls.publishes += 1;
      if (hook !== undefined) {
        const run = hook;
        hook = undefined;
        await run();
      }
      try {
        const response = await store.publish(analysis, intent, [pack]);
        if (loseResponse) {
          loseResponse = false;
          throw new PublishFaultError("the response was lost");
        }
        return response.generation;
      } catch (error) {
        if (isWorkspacePublishError(error)) {
          throw new PublishRefusedError(REFUSALS[error.failure], error.message);
        }
        throw error;
      }
    },
  };
}

/** One pack's bytes, signed and downloaded as a copy would. */
export async function downloadPack(
  remote: Remote,
  packId: string,
): Promise<Uint8Array> {
  const [signed] = await remote.signPackUrls([packId]);
  return remote.download(signed.url, Number(signed.size));
}

export { FixtureWorkspace };
