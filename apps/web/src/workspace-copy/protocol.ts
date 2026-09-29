import type { RecordedTip } from "./copy";
import { requireObjectId } from "./git-objects";
import type { EditOutcome } from "./publish";

// The messages between a window and the SharedWorker that owns every copy. A window asks; the worker
// answers each request once, by id, on the port it came from.

/** The name a publish whose outcome is unknown fails under, which crosses the port with its error. */
export const PUBLISH_OUTCOME_UNKNOWN = "PublishOutcomeUnknownError";

/** The name a request fails under when the repository is damaged, whatever the request was: a
 *  window shows it as such and does not retry it. */
export const WORKSPACE_DAMAGED = "WorkspaceDamagedError";

/** A file a curator's edit replaces, with the new bytes the widget made from the file at the
 *  edit's base. */
export interface EditFile {
  path: string;
  bytes: Uint8Array;
}

export type CopyRequest =
  /** Answered at once, whatever the worker is busy with: how a window tells a live worker from a
   *  dead one. */
  | { method: "ping" }
  /** Bring the copy up to date unless its branch is already at `tip`, and check it holds `tip`. */
  | { method: "sync"; analysisId: string; tip: string }
  | { method: "readDocument"; analysisId: string; commit: string }
  | { method: "readFile"; analysisId: string; commit: string; path: string }
  | { method: "history"; analysisId: string; tip: string }
  /** Delete the copy whole once nothing holds its lock; the next read hydrates it from nothing. */
  | { method: "reset"; analysisId: string }
  | {
      method: "isAncestor";
      analysisId: string;
      ancestor: string;
      descendant: string;
    }
  | {
      method: "publish";
      analysisId: string;
      base: string;
      /** The email the BFF verified for the window's page; the commit's author and committer. */
      curatorEmail: string;
      /** The commit's message. */
      message: string;
      /** Each file the edit replaces, once. */
      files: readonly EditFile[];
    };

/** What each request resolves to. */
export interface CopyResults {
  ping: undefined;
  sync: undefined;
  /** The working document's markdown at the commit, or null when the commit has none. */
  readDocument: string | null;
  /** The file's bytes at the commit, or null when it has no such file. */
  readFile: Uint8Array | null;
  /** Each tip the reflog recorded for the collaborative branch, newest first. */
  history: RecordedTip[];
  reset: undefined;
  /** Whether `ancestor` is `descendant` or reachable from it. */
  isAncestor: boolean;
  /** How the edit ended. A `fileChanged` edit was not applied: the widget redraws at the commit it
   *  names, and the curator redoes the edit there. */
  publish: EditOutcome;
}

export type CopyMethod = CopyRequest["method"];

export interface RequestEnvelope {
  id: number;
  request: CopyRequest;
}

/** A failure as it crosses the port: its class name, so the window can tell refusals apart, and
 *  its message. */
export interface SerializedError {
  name: string;
  message: string;
  /** For a refused publish, which refusal. */
  refusal?: string;
}

export type ResponseEnvelope =
  | { id: number; ok: true; value: CopyResults[CopyMethod] }
  | { id: number; ok: false; error: SerializedError };

/** A window's message as the worker receives it, checked: a port carries whatever was posted to it,
 *  so nothing in a request is trusted before this. Raises on anything malformed. */
export function parseRequest(message: unknown): RequestEnvelope {
  const envelope = record(message, "a request envelope");
  const id = envelope.id;
  if (typeof id !== "number" || !Number.isSafeInteger(id)) {
    throw new Error(`a request's id is not an integer: ${JSON.stringify(id)}`);
  }
  const request = record(envelope.request, "a request");
  return { id, request: parseBody(request) };
}

function parseBody(request: Record<string, unknown>): CopyRequest {
  const method = request.method;
  if (method === "ping") return { method };
  const analysisId = analysis(request.analysisId);
  switch (method) {
    case "sync":
      return { method, analysisId, tip: commit(request.tip, "tip") };
    case "readDocument":
      return { method, analysisId, commit: commit(request.commit, "commit") };
    case "readFile": {
      if (typeof request.path !== "string" || request.path === "") {
        throw new Error("a readFile request names no path");
      }
      return {
        method,
        analysisId,
        commit: commit(request.commit, "commit"),
        path: request.path,
      };
    }
    case "history":
      return { method, analysisId, tip: commit(request.tip, "tip") };
    case "reset":
      return { method, analysisId };
    case "isAncestor":
      return {
        method,
        analysisId,
        ancestor: commit(request.ancestor, "ancestor"),
        descendant: commit(request.descendant, "descendant"),
      };
    case "publish": {
      if (typeof request.curatorEmail !== "string") {
        throw new Error("a publish request names no curator");
      }
      if (typeof request.message !== "string") {
        throw new Error("a publish request carries no commit message");
      }
      return {
        method,
        analysisId,
        base: commit(request.base, "base"),
        curatorEmail: request.curatorEmail,
        message: request.message,
        files: editFiles(request.files),
      };
    }
    default:
      throw new Error(
        `not a request the copy answers: ${JSON.stringify(method)}`,
      );
  }
}

/** The files of a publish request: at least one, each a path and its bytes, no path twice. */
function editFiles(value: unknown): EditFile[] {
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error("a publish request replaces no file");
  }
  const seen = new Set<string>();
  const files: EditFile[] = [];
  // for-of visits a sparse array's holes, which map would skip unchecked.
  for (const entry of value as unknown[]) {
    const file = record(entry, "an edited file");
    if (typeof file.path !== "string" || file.path === "") {
      throw new Error("an edited file names no path");
    }
    if (!(file.bytes instanceof Uint8Array)) {
      throw new Error(`the edited file ${file.path} carries no bytes`);
    }
    if (seen.has(file.path)) {
      throw new Error(`a publish request replaces ${file.path} twice`);
    }
    seen.add(file.path);
    files.push({ path: file.path, bytes: file.bytes });
  }
  return files;
}

const ANALYSIS_ID = /^[A-Za-z0-9_-]{1,128}$/;

function analysis(value: unknown): string {
  if (typeof value !== "string" || !ANALYSIS_ID.test(value)) {
    throw new Error(`not an Analysis id: ${JSON.stringify(value)}`);
  }
  return value;
}

function commit(value: unknown, what: string): string {
  if (typeof value !== "string")
    throw new Error(`a request's ${what} is not a commit id`);
  return requireObjectId(value, `a request's ${what}`);
}

function record(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${what} is not an object`);
  }
  return value as Record<string, unknown>;
}
