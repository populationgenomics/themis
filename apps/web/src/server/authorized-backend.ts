import type {
  Analysis,
  DocumentResponse,
  PollResponse,
  Project,
} from "@/models/workbench";
import { ResourceNotFoundError } from "./errors";
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
    prompt: string;
    projectId: string;
  }): Promise<Analysis> {
    await this.requireMemberOf(input.projectId);
    return this.data.createAnalysis({
      prompt: input.prompt,
      projectId: input.projectId,
      userEmail: this.userEmail,
    });
  }

  async listAnalyses(projectId: string): Promise<Analysis[]> {
    await this.requireMemberOf(projectId);
    return this.data.listAnalysesIn([projectId]);
  }

  async pollEvents(analysisId: string): Promise<PollResponse> {
    await this.requireMember(analysisId);
    return this.data.pollEvents(analysisId);
  }

  async getDocument(
    analysisId: string,
    version?: number,
  ): Promise<DocumentResponse> {
    await this.requireMember(analysisId);
    return this.data.getDocument(analysisId, version);
  }

  /** Authorize a point access: the analysis's Project must be one the user belongs
   *  to. A non-member is answered with not-found, never a distinguishable 403 — a
   *  caller must not learn an analysis outside their Projects exists. */
  private async requireMember(analysisId: string): Promise<void> {
    const projectId = await this.data.projectOfAnalysis(analysisId);
    if (!(await this.membership.isMember(this.userEmail, projectId))) {
      throw new ResourceNotFoundError(`analysis not found: ${analysisId}`);
    }
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
