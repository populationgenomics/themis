import type {
  Analysis,
  DocumentResponse,
  PollResponse,
  Project,
} from "@/models/workbench";

// The single port the routes depend on: the authenticated user's view of the
// analysis surface, selected by `THEMIS_BACKEND` (`./adapters`). A user belongs to
// many Projects, so `createAnalysis` and `listAnalyses` name the Project they act in;
// `pollEvents` / `getDocument` are analysis-keyed. The fixture is a single-Project
// stand-in and enforces no membership; per-Project authorization — verifying the
// caller belongs to the named Project — is defined in workspace-model.md
// §Authorization. One port, not a Store/Client split: that split maps two distinct
// real backends (Cloud SQL + Anthropic), whereas the route surface here is one
// cohesive analysis-session lifecycle. The real adapter composes SQL / Anthropic /
// KMS / GCS behind these methods; the routes stay unaware of that topology.
//
// The methods return protobuf-es view-model messages (constructed with `create`);
// the routes serialize them with `toJson` and never reshape the payload.

export interface AnalysisBackend {
  /** The Projects the caller belongs to — the app-bar's Project selector. */
  listProjects(): Promise<Project[]>;

  /** Create an analysis in `projectId` and kick off its agent session: mint the id +
   *  session, seed the run, return the new row. */
  createAnalysis(input: {
    prompt: string;
    projectId: string;
  }): Promise<Analysis>;

  /** A Project's analyses, newest first — the session switcher's source. */
  listAnalyses(projectId: string): Promise<Analysis[]>;

  /** One liveness tick: the FULL projected event list and the working-document
   *  version signal. */
  pollEvents(analysisId: string): Promise<PollResponse>;

  /** The current working document as a produced|not-produced result, or a named
   *  historical `version`. */
  getDocument(analysisId: string, version?: number): Promise<DocumentResponse>;
}
