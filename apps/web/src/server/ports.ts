import type {
  Analysis,
  DocumentResponse,
  PollResponse,
  Project,
} from "@/models/workbench";

// The server's ports — what an adapter implements. `AnalysisDataPlane` is the raw,
// unauthorized persistence layer; `ProjectMembership` is the user↔Project mapping.
// RPC handlers reach neither directly: `AuthorizedBackend` (authorized-backend.ts)
// wraps the pair, bound to the verified user, and `userContext` is its sole
// constructor — so every access is membership-scoped by construction. See
// docs/design/workspace-model.md (Authorization) and docs/design/security.md.
//
// The methods return protobuf-es view-model messages (constructed with `create`), which
// Connect serializes as they are.

export interface CreateAnalysisInput {
  prompt: string;
  // The Project the analysis lands in — named by the caller and membership-verified
  // by `AuthorizedBackend`, not chosen by the data plane.
  projectId: string;
  // The verified caller, recorded as the analysis creator.
  userEmail: string;
}

/** Raw analysis persistence + retrieval, with NO authorization. Only the
 *  composition root and `AuthorizedBackend` hold one; a handler never does. The live
 *  adapter composes SQL / Anthropic / KMS / GCS behind these methods. */
export interface AnalysisDataPlane {
  /** Create the analysis and kick off its agent session: mint the id + session,
   *  seed the run, return the new row. */
  createAnalysis(input: CreateAnalysisInput): Promise<Analysis>;

  /** Analyses in the given Projects, newest first — the session switcher's source.
   *  An empty Project set yields no rows. */
  listAnalysesIn(projectIds: readonly string[]): Promise<Analysis[]>;

  /** One liveness tick: the FULL projected event list and the working-document
   *  version signal. */
  pollEvents(analysisId: string): Promise<PollResponse>;

  /** The current working document as a produced|not-produced result, or a named
   *  historical `version`. */
  getDocument(analysisId: string, version?: number): Promise<DocumentResponse>;

  /** The Project owning an analysis. Raises `ResourceNotFoundError` when the
   *  analysis is unknown — the same not-found a non-member gets, so a caller can
   *  never distinguish "outside my Projects" from "does not exist". */
  projectOfAnalysis(analysisId: string): Promise<string>;
}

/** The user↔Project membership mapping — the access boundary. Seeded offline by
 *  the fixture; read from the `project_members` table by the live adapter. */
export interface ProjectMembership {
  isMember(userEmail: string, projectId: string): Promise<boolean>;

  /** Every Project the user belongs to (id + name). Empty ⇒ the user can reach
   *  nothing (default-deny). */
  projectsOf(userEmail: string): Promise<Project[]>;
}
