import type {
  PublishIntent,
  PublishResponse,
  RefDocSnapshot,
  SignPackUrlsResponse,
} from "@/models/sheaf";
import type {
  Analysis,
  AnalysisInputs,
  PollResponse,
  Project,
  ThreadResponse,
  WorkspaceTip,
} from "@/models/workbench";
import {
  isUndecodableAnalysisError,
  isWorkspaceDamagedError,
  ResourceNotFoundError,
} from "./errors";
import type {
  AnalysisDataPlane,
  ProjectMembership,
  WorkspaceRepository,
} from "./ports";
import { damagedTip, readTip, unavailableTip } from "./workspace";

// The authorization chokepoint (docs/design/workspace-model.md Authorization;
// docs/design/security.md). Wraps the raw data plane and workspace repository and admits every
// access only for the bound user: create and list name a Project the user must belong to, and a point
// access must clear the analysis's Project-membership check. `userContext` is the sole
// constructor, so a route never reaches the data plane unscoped.

export class AuthorizedBackend {
  constructor(
    private readonly data: AnalysisDataPlane,
    private readonly workspace: WorkspaceRepository,
    private readonly membership: ProjectMembership,
    private readonly userEmail: string,
    private readonly pollTipBudgetMs: number,
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

  /** One liveness tick: the run's stream, and where the workspace repository's collaborative branch
   *  stands. A damaged ref document is reported as damaged, and one the tick cannot read within
   *  `pollTipBudgetMs` as unavailable; either way the events still arrive (the
   *  `PollResponse.workspace_tip` proto comment). */
  async pollEvents(
    analysisId: string,
    signal: AbortSignal,
  ): Promise<PollResponse> {
    const analysis = await this.authorizedAnalysis(analysisId);
    const [response, workspaceTip] = await Promise.all([
      this.data.pollEvents(analysis),
      this.workspaceTip(analysis, signal),
    ]);
    response.workspaceTip = workspaceTip;
    return response;
  }

  /** The Poll's tip. A read that fails is logged, as the fault it is, and answered as damaged when
   *  the service found the document damaged, else as unavailable; running out of the budget is such
   *  a failure. One the caller abandoned propagates, since no answer reaches anyone. */
  private async workspaceTip(
    analysis: Analysis,
    signal: AbortSignal,
  ): Promise<WorkspaceTip> {
    const budget = AbortSignal.any([
      signal,
      AbortSignal.timeout(this.pollTipBudgetMs),
    ]);
    try {
      return readTip(await this.workspace.readRefDoc(analysis, budget));
    } catch (error) {
      if (signal.aborted) throw error;
      if (budget.aborted) {
        console.error(
          `the workspace ref document of ${analysis.id} did not read within ${this.pollTipBudgetMs} ms`,
        );
        return unavailableTip();
      }
      if (isWorkspaceDamagedError(error)) {
        console.error(
          `the workspace ref document of ${analysis.id} is damaged`,
          error,
        );
        return damagedTip();
      }
      console.error(
        `the workspace ref document of ${analysis.id} is unreadable`,
        error,
      );
      return unavailableTip();
    }
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

  /** The workspace repository's ref document, for the browser's copy of it. */
  async readWorkspaceRefDoc(
    analysisId: string,
    signal: AbortSignal,
  ): Promise<RefDocSnapshot> {
    return this.workspace.readRefDoc(
      await this.authorizedAnalysis(analysisId),
      signal,
    );
  }

  async signWorkspacePackUrls(
    analysisId: string,
    packIds: readonly string[],
    signal: AbortSignal,
  ): Promise<SignPackUrlsResponse> {
    return this.workspace.signPackUrls(
      await this.authorizedAnalysis(analysisId),
      packIds,
      signal,
    );
  }

  /** Publish a curator's commit. Authorized as a point read is, so a non-member's bytes never reach
   *  the repository. */
  async publishWorkspace(
    analysisId: string,
    intent: PublishIntent,
    packs: readonly Uint8Array[],
  ): Promise<PublishResponse> {
    return this.workspace.publish(
      await this.authorizedAnalysis(analysisId),
      intent,
      packs,
    );
  }

  /** The bytes behind a pack URL the backend minted to point at the BFF. */
  async serveWorkspacePack(
    analysisId: string,
    packId: string,
  ): Promise<Response> {
    return this.workspace.servePack(
      await this.authorizedAnalysis(analysisId),
      packId,
    );
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
