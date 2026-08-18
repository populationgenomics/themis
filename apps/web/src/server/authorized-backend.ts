import type {
  Analysis,
  AnalysisInputs,
  DocumentResponse,
  PollResponse,
  Project,
  ThreadResponse,
} from "@/models/workbench";
import { isUndecodableAnalysisError, ResourceNotFoundError } from "./errors";
import type { AnalysisDataPlane, ProjectMembership } from "./ports";

// The authorization chokepoint (docs/design/workspace-model.md Authorization;
// docs/design/security.md). Wraps the raw data plane and admits every access only for
// the bound user: create and list name a Project the user must belong to, and a point
// access must clear the analysis's Project-membership check. `userContext` is the sole
// constructor, so a route never reaches the data plane unscoped.

export class AuthorizedBackend {
  constructor(
    private readonly data: AnalysisDataPlane,
    private readonly membership: ProjectMembership,
    private readonly userEmail: string,
  ) {}

  async listProjects(): Promise<Project[]> {
    return this.membership.projectsOf(this.userEmail);
  }

  async createAnalysis(input: {
    inputs: AnalysisInputs;
    projectId: string;
  }): Promise<Analysis> {
    await this.requireMemberOf(input.projectId);
    return this.data.createAnalysis({
      inputs: input.inputs,
      projectId: input.projectId,
      userEmail: this.userEmail,
    });
  }

  async listAnalyses(projectId: string): Promise<Analysis[]> {
    await this.requireMemberOf(projectId);
    return this.data.listAnalysesIn([projectId]);
  }

  /** Every Analysis the user can reach, newest first — what the Projects page counts
   *  and dates each Project by. One read over the whole membership, not one per
   *  Project. */
  async listAllAnalyses(): Promise<Analysis[]> {
    const projects = await this.membership.projectsOf(this.userEmail);
    return this.data.listAnalysesIn(projects.map((p) => p.id));
  }

  async getAnalysis(analysisId: string): Promise<Analysis> {
    return this.authorizedAnalysis(analysisId);
  }

  async pollEvents(analysisId: string): Promise<PollResponse> {
    return this.data.pollEvents(await this.authorizedAnalysis(analysisId));
  }

  /** One spawned thread's own stream. A point access like the poll's; the thread is
   *  looked up in the resolved row's session (the `ThreadRequest` proto comment). */
  async getThread(
    analysisId: string,
    threadId: string,
  ): Promise<ThreadResponse> {
    return this.data.getThread(
      await this.authorizedAnalysis(analysisId),
      threadId,
    );
  }

  /** Append a curator turn to a running Analysis. Authorized exactly as a point read
   *  is: a non-member is answered not-found, and the turn never reaches the run. */
  async steerAnalysis(analysisId: string, text: string): Promise<void> {
    return this.data.steerAnalysis(
      await this.authorizedAnalysis(analysisId),
      text,
    );
  }

  /** Halt a running Analysis's current step, authorized as a point read is. */
  async interruptAnalysis(analysisId: string): Promise<void> {
    return this.data.interruptAnalysis(
      await this.authorizedAnalysis(analysisId),
    );
  }

  async getDocument(
    analysisId: string,
    version?: number,
  ): Promise<DocumentResponse> {
    await this.authorizedAnalysis(analysisId);
    return this.data.getDocument(analysisId, version);
  }

  /** Authorize a point access and hand back the row it authorized against: the
   *  analysis's Project must be one the user belongs to. A non-member is answered
   *  with not-found, never a distinguishable 403 — a caller must not learn an
   *  analysis outside their Projects exists. The row is handed on rather than
   *  re-read: a poll tick every 2.5s would otherwise query it twice. */
  private async authorizedAnalysis(analysisId: string): Promise<Analysis> {
    let analysis: Analysis;
    try {
      analysis = await this.data.getAnalysis(analysisId);
    } catch (e) {
      // An unreadable payload still authorizes: the error carries the row's Project, so a non-member
      // gets the same not-found as an unknown id and cannot tell the row exists. A member sees the
      // fault, which is theirs to know about.
      if (isUndecodableAnalysisError(e)) {
        if (!(await this.membership.isMember(this.userEmail, e.projectId))) {
          throw new ResourceNotFoundError(`analysis not found: ${analysisId}`);
        }
      }
      throw e;
    }
    if (!(await this.membership.isMember(this.userEmail, analysis.projectId))) {
      throw new ResourceNotFoundError(`analysis not found: ${analysisId}`);
    }
    return analysis;
  }

  /** Authorize access to a named Project: the caller must belong to it. A non-member
   *  Project is answered not-found for the same existence-hiding reason — a caller
   *  must not learn a Project outside their membership exists. */
  private async requireMemberOf(projectId: string): Promise<void> {
    if (!(await this.membership.isMember(this.userEmail, projectId))) {
      throw new ResourceNotFoundError(`project not found: ${projectId}`);
    }
  }
}
