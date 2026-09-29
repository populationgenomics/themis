import { createHash } from "node:crypto";
import { create, toBinary } from "@bufbuild/protobuf";
import { timestampFromMs } from "@bufbuild/protobuf/wkt";
import {
  type PublishIntent,
  PublishIntentSchema,
  type PublishResponse,
  PublishResponseSchema,
  type RefDoc,
  RefDocSchema,
  type RefDocSnapshot,
  RefDocSnapshotSchema,
  type RefTarget,
  RefTargetSchema,
  type SignPackUrlsResponse,
  SignPackUrlsResponseSchema,
} from "@/models/sheaf";
import type { Analysis } from "@/models/workbench";
import {
  ResourceNotFoundError,
  WorkspacePackNotListedError,
  WorkspacePublishError,
} from "../../errors";
import type { WorkspaceRepository } from "../../ports";
import { COLLABORATIVE_BRANCH } from "../../workspace";
import { agentHistory, REFLOG_REF, type SeedPublish } from "./workspace-seed";

// The offline workspace repository: sheaf's storage protocol over an in-memory store, one repository
// per Analysis. It decides a publish as the sheaf service does (themis/services/sheaf/servicer.py,
// themis/sheaf/store.py) from the document alone — the refs it names, the reflog ref's presence, the
// generation it was built against — and holds no git objects, so the checks that need them stay the
// writer's, as they are against the service. Pack URLs point at the BFF's own pack route, which
// serves the stored bytes, so the browser runs the same code offline as against the bucket.

/** The service's ceilings, as the deployment sets them (infra/themis_infra/sheaf.py). */
export const FIXTURE_LIMITS = {
  maxPublishBytes: 256 * 1024 * 1024,
  maxRefs: 10_000,
  maxDocumentBytes: 2 * 1024 * 1024,
} as const;

/** How long a pack URL is said to live. The fixture's route checks membership, not the expiry. */
const PACK_URL_TTL_MS = 15 * 60 * 1000;

const SHEAF_NAMESPACE = "refs/sheaf/";
const BRANCH_PREFIX = "refs/heads/";
const OBJECT_ID = /^[0-9a-f]{40}$/;
const ZERO_OID = "0".repeat(40);
const PACK_ID = /^[0-9a-f]{64}$/;
const ILLEGAL_IN_REF = /\.\.|[:?[\\^~ \t\n\v\f\r*]|@\{/;
const MAX_COMPONENT_BYTES = 255 - ".lock".length;

/** The route a fixture pack URL names; `app/api/workspaces/[analysisId]/packs/[packId]`. */
export function packPath(analysisId: string, packId: string): string {
  return `/api/workspaces/${encodeURIComponent(analysisId)}/packs/${packId}`;
}

interface Repository {
  document: RefDoc;
  generation: bigint;
  packs: Map<string, Uint8Array>;
}

export class FixtureWorkspace implements WorkspaceRepository {
  private readonly repositories = new Map<string, Repository>();
  /** Agent histories not yet built: `git` runs for an Analysis on its repository's first read. */
  private readonly unbuilt = new Map<string, () => SeedPublish[]>();
  private lastGeneration = BigInt(0);

  constructor(private readonly nowMs: () => number = Date.now) {}

  /** Give `analysisId` the repository an agent that wrote `documents` in turn would have left: one
   *  publish per version, the last the branch tip. Built by `git` on the repository's first read. */
  seedAgentHistory(
    analysisId: string,
    documents: readonly string[],
    authoredAt: Date,
  ): void {
    this.seedHistory(analysisId, () => agentHistory(documents, authoredAt));
  }

  /** Give `analysisId` the repository the publishes `build` returns leave, applied in order on the
   *  repository's first read. The repository appears whole or not at all: a build that fails, or a
   *  publish of it the store refuses, fails every read. */
  seedHistory(analysisId: string, build: () => SeedPublish[]): void {
    if (this.repositories.has(analysisId) || this.unbuilt.has(analysisId)) {
      throw new Error(`workspace repository already seeded: ${analysisId}`);
    }
    this.unbuilt.set(analysisId, build);
  }

  async readRefDoc(
    analysis: Analysis,
    _signal: AbortSignal,
  ): Promise<RefDocSnapshot> {
    const repository = this.repository(analysis.id);
    if (repository === undefined) return create(RefDocSnapshotSchema, {});
    return create(RefDocSnapshotSchema, {
      document: repository.document,
      generation: repository.generation,
    });
  }

  async signPackUrls(
    analysis: Analysis,
    packIds: readonly string[],
    _signal: AbortSignal,
  ): Promise<SignPackUrlsResponse> {
    const repository = this.repository(analysis.id);
    const expireTime = timestampFromMs(this.nowMs() + PACK_URL_TTL_MS);
    return create(SignPackUrlsResponseSchema, {
      packs: packIds.map((packId) => {
        const bytes = repository?.document.packs.includes(packId)
          ? repository.packs.get(packId)
          : undefined;
        if (bytes === undefined) {
          throw new WorkspacePackNotListedError(
            `${analysis.id}: the ref document lists no pack ${packId}`,
          );
        }
        return {
          packId,
          url: packPath(analysis.id, packId),
          size: BigInt(bytes.length),
          expireTime,
        };
      }),
    });
  }

  async publish(
    analysis: Analysis,
    intent: PublishIntent,
    packs: readonly Uint8Array[],
  ): Promise<PublishResponse> {
    return this.apply(analysis.id, intent, packs);
  }

  async servePack(analysis: Analysis, packId: string): Promise<Response> {
    const repository = this.repository(analysis.id);
    const bytes = repository?.document.packs.includes(packId)
      ? repository.packs.get(packId)
      : undefined;
    if (bytes === undefined) {
      throw new ResourceNotFoundError(`${analysis.id}: no pack ${packId}`);
    }
    return new Response(new Uint8Array(bytes), {
      headers: {
        "content-type": "application/x-git-packed-objects",
        "content-length": String(bytes.length),
        "x-content-type-options": "nosniff",
        "cache-control": "private, no-store",
      },
    });
  }

  private repository(analysisId: string): Repository | undefined {
    const build = this.unbuilt.get(analysisId);
    if (build !== undefined) {
      // Built whole before anything is stored, so a build that fails fails every read, rather than
      // leaving nothing or half a history looking like what the agent published.
      let repository: Repository | undefined;
      for (const publish of build()) {
        repository = publishOnto(
          repository,
          seedIntent(repository?.generation ?? BigInt(0), publish),
          [publish.pack],
          () => this.nextGeneration(),
        ).repository;
      }
      if (repository === undefined) {
        throw new Error(`an agent history for ${analysisId} published nothing`);
      }
      this.repositories.set(analysisId, repository);
      this.unbuilt.delete(analysisId);
    }
    return this.repositories.get(analysisId);
  }

  private apply(
    analysisId: string,
    intent: PublishIntent,
    packs: readonly Uint8Array[],
  ): PublishResponse {
    const { repository, response } = publishOnto(
      this.repository(analysisId),
      intent,
      packs,
      () => this.nextGeneration(),
    );
    if (repository !== undefined) this.repositories.set(analysisId, repository);
    return response;
  }

  private nextGeneration(): bigint {
    this.lastGeneration += BigInt(1);
    return this.lastGeneration;
  }
}

/** One publish onto `current`, decided as the service decides it: the repository it leaves, which is
 *  `current` itself when the publish had already landed, and the answer. Synchronous, so a caller
 *  that stores the result before yielding makes the compare-and-swap atomic. */
function publishOnto(
  current: Repository | undefined,
  intent: PublishIntent,
  packs: readonly Uint8Array[],
  nextGeneration: () => bigint,
): { repository: Repository | undefined; response: PublishResponse } {
  validateIntent(intent);
  const generation = current?.generation ?? BigInt(0);
  if (generation !== intent.baseGeneration) {
    return { repository: current, response: settle(current, intent) };
  }
  const document = plan(current?.document, intent);
  const stored = receivePacks(intent, packs);
  const repository: Repository = {
    document,
    generation: nextGeneration(),
    packs: new Map([...(current?.packs ?? []), ...stored]),
  };
  return {
    repository,
    response: create(PublishResponseSchema, {
      generation: repository.generation,
    }),
  };
}

function seedIntent(
  baseGeneration: bigint,
  publish: SeedPublish,
): PublishIntent {
  return create(PublishIntentSchema, {
    baseGeneration,
    refUpdates: publish.refUpdates,
    packs: [
      { size: BigInt(publish.pack.length), packId: sha256(publish.pack) },
    ],
  });
}

function malformed(message: string): WorkspacePublishError {
  return new WorkspacePublishError("malformed", message);
}

function overCeiling(message: string): WorkspacePublishError {
  return new WorkspacePublishError("overCeiling", message);
}

/** The refs `intent` moves outside sheaf's own namespace, in name order. */
function movedRefs(intent: PublishIntent): string[] {
  return Object.keys(intent.refUpdates)
    .filter((ref) => !ref.startsWith(SHEAF_NAMESPACE))
    .sort();
}

/** What the service refuses from the intent alone, before it reads the document. */
function validateIntent(intent: PublishIntent): void {
  if (intent.head !== undefined) validateTarget(intent.head);
  for (const [ref, update] of Object.entries(intent.refUpdates)) {
    validateRefName(ref);
    if (update.new === undefined) {
      throw malformed(`${ref}: a publish never deletes a ref`);
    }
    for (const oid of [update.old, update.new]) {
      if (oid !== undefined) validateObjectId(oid);
    }
  }
  if (movedRefs(intent).length === 0) {
    throw malformed(`the intent moves no ref outside ${SHEAF_NAMESPACE}`);
  }
  if (!(REFLOG_REF in intent.refUpdates)) {
    throw malformed(`the intent moves refs without advancing ${REFLOG_REF}`);
  }
  const seen = new Set<string>();
  let declared = BigInt(0);
  for (const [index, pack] of intent.packs.entries()) {
    if (!PACK_ID.test(pack.packId)) {
      throw malformed(`pack ${index}: ${pack.packId} is not a pack id`);
    }
    if (pack.size === BigInt(0)) {
      throw malformed(`pack ${index} declares no bytes; a pack has bytes`);
    }
    if (seen.has(pack.packId)) {
      throw malformed(`pack ${index} is declared twice`);
    }
    seen.add(pack.packId);
    declared += pack.size;
  }
  if (declared > BigInt(FIXTURE_LIMITS.maxPublishBytes)) {
    throw overCeiling(
      `the declared packs total ${declared} bytes; the ceiling is ${FIXTURE_LIMITS.maxPublishBytes}`,
    );
  }
}

/** A publish built against a generation the document has left: landed, a lost race, or a ref moved
 *  under it — over the refs it moves outside sheaf's namespace, in that order. */
function settle(
  current: Repository | undefined,
  intent: PublishIntent,
): PublishResponse {
  const refs = current?.document.refs ?? {};
  const holds = (ref: string) => {
    const target = refs[ref]?.target;
    return target?.case === "oid" ? target.value : undefined;
  };
  const moved = movedRefs(intent);
  if (moved.every((ref) => holds(ref) === intent.refUpdates[ref].new)) {
    return create(PublishResponseSchema, {
      generation: current?.generation ?? BigInt(0),
    });
  }
  if (moved.every((ref) => holds(ref) === intent.refUpdates[ref].old)) {
    throw new WorkspacePublishError(
      "raceLost",
      `the document is at generation ${current?.generation ?? BigInt(0)}, not the base; ${moved.join(", ")} unchanged: rebuild against it`,
    );
  }
  throw new WorkspacePublishError(
    "branchMoved",
    `${moved.filter((ref) => holds(ref) !== intent.refUpdates[ref].old).join(", ")} moved under this publish: not a fast-forward`,
  );
}

/** The document the publish leaves, from the document it was built against. */
function plan(base: RefDoc | undefined, intent: PublishIntent): RefDoc {
  const refs: Record<string, RefTarget> = { ...(base?.refs ?? {}) };
  for (const [ref, update] of Object.entries(intent.refUpdates)) {
    const target = refs[ref]?.target;
    const actual = target?.case === "oid" ? target.value : undefined;
    if (target !== undefined && target.case !== "oid") {
      throw malformed(`${ref} is symbolic; a publish moves only direct refs`);
    }
    if (actual !== update.old) {
      throw malformed(
        `${ref} holds ${actual ?? "nothing"}, not the ${update.old ?? "absence"} the intent read`,
      );
    }
    if (update.new === undefined) {
      throw malformed(`${ref}: a publish never deletes a ref`);
    }
    refs[ref] = create(RefTargetSchema, {
      target: { case: "oid", value: update.new },
    });
  }
  validateRefSet(Object.keys(refs));
  if (Object.keys(refs).length > FIXTURE_LIMITS.maxRefs) {
    throw overCeiling(
      `the publish would leave ${Object.keys(refs).length} refs; the ceiling is ${FIXTURE_LIMITS.maxRefs}`,
    );
  }
  const head = intent.head ?? carryHead(base?.head, refs) ?? headFor(refs);
  const packs = [
    ...new Set([...(base?.packs ?? []), ...intent.packs.map((p) => p.packId)]),
  ].sort();
  const document = create(RefDocSchema, { refs, packs, head });
  const size = toBinary(RefDocSchema, document).length;
  if (size > FIXTURE_LIMITS.maxDocumentBytes) {
    throw overCeiling(
      `the publish would leave a ${size}-byte ref document; the ceiling is ${FIXTURE_LIMITS.maxDocumentBytes}`,
    );
  }
  return document;
}

/** Each declared pack's bytes, checked against its declared size and hash. */
function receivePacks(
  intent: PublishIntent,
  packs: readonly Uint8Array[],
): Map<string, Uint8Array> {
  if (packs.length !== intent.packs.length) {
    throw malformed(
      `the intent declares ${intent.packs.length} packs and ${packs.length} arrived`,
    );
  }
  const stored = new Map<string, Uint8Array>();
  for (const [index, descriptor] of intent.packs.entries()) {
    const bytes = packs[index];
    if (BigInt(bytes.length) !== descriptor.size) {
      throw malformed(
        `pack ${index} delivered ${bytes.length} of the ${descriptor.size} bytes declared`,
      );
    }
    const digest = sha256(bytes);
    if (digest !== descriptor.packId) {
      throw malformed(
        `pack ${index} hashes to ${digest}, not the declared ${descriptor.packId}`,
      );
    }
    stored.set(digest, bytes);
  }
  return stored;
}

/** HEAD carried over, unless it names a branch that does not exist and a branch now does. */
function carryHead(
  head: RefTarget | undefined,
  refs: Record<string, RefTarget>,
): RefTarget | undefined {
  const dangling = head?.target.case === "ref" && !(head.target.value in refs);
  if (dangling && Object.keys(refs).some((r) => r.startsWith(BRANCH_PREFIX))) {
    return undefined;
  }
  return head;
}

/** `refs/heads/main` if it exists, else the first branch in name order, else an unborn main. */
function headFor(refs: Record<string, RefTarget>): RefTarget {
  const branches = Object.keys(refs)
    .filter((ref) => ref.startsWith(BRANCH_PREFIX))
    .sort();
  const ref =
    branches.length === 0 || branches.includes(COLLABORATIVE_BRANCH)
      ? COLLABORATIVE_BRANCH
      : branches[0];
  return create(RefTargetSchema, { target: { case: "ref", value: ref } });
}

function validateTarget(target: RefTarget): void {
  switch (target.target.case) {
    case "oid":
      validateObjectId(target.target.value);
      return;
    case "ref":
      validateRefName(target.target.value);
      return;
    default:
      throw malformed("HEAD names neither an object nor a ref");
  }
}

function validateObjectId(oid: string): void {
  if (!OBJECT_ID.test(oid) || oid === ZERO_OID) {
    throw malformed(`${JSON.stringify(oid)} is not an object id`);
  }
}

/** The subset of `git check-ref-format` sheaf stores (themis/sheaf/refdoc.py). */
function validateRefName(ref: string): void {
  const invalid = (why: string) => malformed(`${JSON.stringify(ref)} ${why}`);
  if (!ref.startsWith("refs/")) throw invalid("is not under refs/");
  if (ILLEGAL_IN_REF.test(ref) || hasControlCharacter(ref)) {
    throw invalid("is not a valid git ref name");
  }
  if (ref.endsWith(".")) throw invalid("may not end with a dot");
  for (const part of ref.split("/")) {
    if (part === "") throw invalid("has an empty path component");
    if (part.startsWith(".") || part.endsWith(".lock")) {
      throw invalid(`has the component ${JSON.stringify(part)} git refuses`);
    }
    if (Buffer.byteLength(part) > MAX_COMPONENT_BYTES) {
      throw invalid("has a component too long for a lock file");
    }
  }
}

function hasControlCharacter(value: string): boolean {
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) return true;
  }
  return false;
}

/** Git stores refs as files under directories, so no ref may be a directory of another. */
function validateRefSet(refs: readonly string[]): void {
  const names = new Set(refs);
  for (const name of names) {
    const parts = name.split("/");
    for (let depth = 1; depth < parts.length; depth += 1) {
      const parent = parts.slice(0, depth).join("/");
      if (names.has(parent)) {
        throw malformed(`${name} and ${parent} cannot both exist`);
      }
    }
  }
}

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}
