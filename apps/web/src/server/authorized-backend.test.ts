import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import {
  type Analysis,
  AnalysisSchema,
  type DocumentResponse,
  DocumentResponseSchema,
  type PollResponse,
  PollResponseSchema,
  type Project,
  ProjectSchema,
} from "@/models/workbench";
import { AuthorizedBackend } from "./authorized-backend";
import { ResourceNotFoundError } from "./errors";
import type {
  AnalysisDataPlane,
  CreateAnalysisInput,
  ProjectMembership,
} from "./ports";

class FakeMembership implements ProjectMembership {
  constructor(private readonly byUser: Record<string, string[]>) {}
  async isMember(userEmail: string, projectId: string): Promise<boolean> {
    return (this.byUser[userEmail] ?? []).includes(projectId);
  }
  async projectsOf(userEmail: string): Promise<Project[]> {
    return (this.byUser[userEmail] ?? []).map((id) =>
      create(ProjectSchema, { id, name: id }),
    );
  }
}

class FakeDataPlane implements AnalysisDataPlane {
  readonly creates: CreateAnalysisInput[] = [];
  listScope: readonly string[] = [];
  constructor(private readonly projectByAnalysis: Record<string, string>) {}

  async createAnalysis(input: CreateAnalysisInput): Promise<Analysis> {
    this.creates.push(input);
    return create(AnalysisSchema, {
      id: "an_new",
      projectId: input.projectId,
      prompt: input.prompt,
    });
  }
  async listAnalysesIn(projectIds: readonly string[]): Promise<Analysis[]> {
    this.listScope = projectIds;
    return Object.entries(this.projectByAnalysis)
      .filter(([, projectId]) => projectIds.includes(projectId))
      .map(([id, projectId]) => create(AnalysisSchema, { id, projectId }));
  }
  async pollEvents(): Promise<PollResponse> {
    return create(PollResponseSchema, {});
  }
  async getDocument(): Promise<DocumentResponse> {
    return create(DocumentResponseSchema, {});
  }
  async projectOfAnalysis(analysisId: string): Promise<string> {
    const projectId = this.projectByAnalysis[analysisId];
    if (projectId === undefined) {
      throw new ResourceNotFoundError(`analysis not found: ${analysisId}`);
    }
    return projectId;
  }
}

// The user is a member of proj_a but not proj_b. an_mine ∈ proj_a; an_theirs ∈ proj_b.
const USER = "user@example.org";
function backend(): { authz: AuthorizedBackend; data: FakeDataPlane } {
  const data = new FakeDataPlane({ an_mine: "proj_a", an_theirs: "proj_b" });
  const membership = new FakeMembership({ [USER]: ["proj_a"] });
  return { authz: new AuthorizedBackend(data, membership, USER), data };
}

describe("AuthorizedBackend point access", () => {
  test("a member reaches an analysis in their Project", async () => {
    const { authz } = backend();
    await expect(authz.getDocument("an_mine")).resolves.toBeDefined();
    await expect(authz.pollEvents("an_mine")).resolves.toBeDefined();
  });

  test("a non-member gets not-found, not a distinguishable forbidden", async () => {
    const { authz } = backend();
    const error = await authz.getDocument("an_theirs").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ResourceNotFoundError);
    // The refusal must not reveal which Project the analysis is in.
    expect((error as Error).message).not.toContain("proj_b");
    await expect(authz.pollEvents("an_theirs")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
  });

  test("an unknown analysis is not-found, same as a non-member", async () => {
    const { authz } = backend();
    await expect(authz.getDocument("an_absent")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
  });
});

describe("AuthorizedBackend listing", () => {
  test("lists the named Project's analyses for a member", async () => {
    const { authz, data } = backend();
    const analyses = await authz.listAnalyses("proj_a");
    expect(data.listScope).toEqual(["proj_a"]);
    expect(analyses.map((a) => a.id)).toEqual(["an_mine"]);
  });

  test("listing a non-member Project is not-found and never reaches the data", async () => {
    const { authz, data } = backend();
    await expect(authz.listAnalyses("proj_b")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
    expect(data.listScope).toEqual([]);
  });
});

describe("AuthorizedBackend create", () => {
  test("creates in the named Project and records the user", async () => {
    const { authz, data } = backend();
    const analysis = await authz.createAnalysis({
      prompt: "classify",
      projectId: "proj_a",
    });
    expect(analysis.projectId).toBe("proj_a");
    expect(data.creates).toEqual([
      { prompt: "classify", projectId: "proj_a", userEmail: USER },
    ]);
  });

  test("creating in a non-member Project is not-found and creates nothing", async () => {
    const { authz, data } = backend();
    await expect(
      authz.createAnalysis({ prompt: "x", projectId: "proj_b" }),
    ).rejects.toBeInstanceOf(ResourceNotFoundError);
    expect(data.creates).toEqual([]);
  });
});

describe("AuthorizedBackend projects", () => {
  test("lists the Projects the user belongs to", async () => {
    const { authz } = backend();
    const projects = await authz.listProjects();
    expect(projects.map((p) => p.id)).toEqual(["proj_a"]);
  });
});
