import type {
  LocateResponse,
  PaperInfo,
  Representation,
} from "@/models/literature";
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

/** A stored content object and how to serve it. Backend-neutral: the object lives in a GCS bucket
 *  (live) or the fixture's in-memory store (fixture). `mediaType` / `downloadName` come from the
 *  resolving surface and drive the egress typing the `ContentPort` applies. */
export interface ContentObject {
  bucket: string;
  object: string;
  mediaType: string;
  downloadName?: string;
}

/** Serves a stored object to the browser. The live implementation signs a short-lived V4 read URL and
 *  answers a `302` (the bytes then flow browser↔GCS, never through the BFF); the fixture streams the
 *  seeded bytes with the egress headers. One reusable primitive across content surfaces (the
 *  literature corpus, per-tenant working documents): each surface resolves its own objects — bounding
 *  which bucket it trusts — and hands them here. */
export interface ContentPort {
  serve(object: ContentObject): Promise<Response>;
}

/** Which object of a paper to fetch. Mirrors the literature proto's ResolveContent selector. */
export type ContentSelector =
  | { kind: "markdown" }
  | { kind: "pdf" }
  | { kind: "file"; name: string };

/** The literature read surface the pane needs. Unlike the data plane it is NOT
 *  Project-scoped: a paper is a shared-corpus resource, IAP-gated at the route, so routes reach it
 *  directly (no `AuthorizedBackend`). The fixture serves a seeded corpus offline; the live adapter
 *  calls the evidence gRPC service and resolves each object through an injected `ContentPort`. */
export interface LiteraturePort {
  /** The paper's representations, default representation, and files. Raises
   *  `ResourceNotFoundError` for an unknown doc_id. */
  describePaper(docId: string): Promise<PaperInfo>;

  /** Serve the selected object: resolve it to a `ContentObject` and hand it to the `ContentPort` (a
   *  signed-URL `302` live, the seeded bytes offline). Raises `ResourceNotFoundError` for an unknown
   *  doc_id or an object the paper lacks. */
  serveContent(docId: string, selector: ContentSelector): Promise<Response>;

  /** Locate a citation's quote within a representation — code-point offsets for markdown, a page
   *  region for a PDF, or a `not_located` result when the quote is absent (a first-class outcome
   *  the pane shows as a warning chip). Raises `ResourceNotFoundError` for an unknown doc_id. */
  locate(
    docId: string,
    quote: string,
    representation: Representation,
  ): Promise<LocateResponse>;
}
