import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import {
  type Analysis,
  AnalysisInputsSchema,
  AnalysisSchema,
  type DocumentResponse,
  DocumentResponseSchema,
  type PollResponse,
  PollResponseSchema,
  type Project,
  ProjectSchema,
  type ThreadResponse,
  ThreadResponseSchema,
} from "@/models/workbench";
import { AuthorizedBackend } from "./authorized-backend";
import { ResourceNotFoundError, UndecodableAnalysisError } from "./errors";
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
  readonly steers: { analysisId: string; text: string }[] = [];
  readonly interrupts: string[] = [];
  readonly threads: { analysisId: string; threadId: string }[] = [];
  /** How many times the authorizing row was read — the chokepoint hands it on rather
   *  than making each access re-read it. */
  rowReads = 0;
  listScope: readonly string[] = [];
  constructor(private readonly projectByAnalysis: Record<string, string>) {}

  async createAnalysis(input: CreateAnalysisInput): Promise<Analysis> {
    this.creates.push(input);
    return create(AnalysisSchema, {
      id: "an_new",
      projectId: input.projectId,
      inputs: input.inputs,
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
  async getThread(
    analysis: Analysis,
    threadId: string,
  ): Promise<ThreadResponse> {
    this.threads.push({ analysisId: analysis.id, threadId });
    return create(ThreadResponseSchema, {});
  }
  async steerAnalysis(analysis: Analysis, text: string): Promise<void> {
    this.steers.push({ analysisId: analysis.id, text });
  }
  async interruptAnalysis(analysis: Analysis): Promise<void> {
    this.interrupts.push(analysis.id);
  }
  async getDocument(): Promise<DocumentResponse> {
    return create(DocumentResponseSchema, {});
  }
  async getAnalysis(analysisId: string): Promise<Analysis> {
    this.rowReads += 1;
    const projectId = this.projectByAnalysis[analysisId];
    if (projectId === undefined) {
      throw new ResourceNotFoundError(`analysis not found: ${analysisId}`);
    }
    // A row whose stored payload does not decode, as the SQL adapter reports one.
    if (analysisId.startsWith("an_corrupt")) {
      throw new UndecodableAnalysisError(analysisId, projectId);
    }
    return create(AnalysisSchema, { id: analysisId, projectId });
  }
}

const INPUTS = create(AnalysisInputsSchema, {
  scenario: {
    case: "variantClassification",
    value: {
      transcript: "NM_001382309.1",
      hgvsC: "c.332del",
      clinicalContext: "de novo, developmental delay",
    },
  },
});

// The user is a member of proj_a but not proj_b. an_mine ∈ proj_a; an_theirs ∈ proj_b.
const USER = "user@example.org";
function backend(extra: Record<string, string> = {}): {
  authz: AuthorizedBackend;
  data: FakeDataPlane;
} {
  const data = new FakeDataPlane({
    an_mine: "proj_a",
    an_theirs: "proj_b",
    ...extra,
  });
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

  test("an unreadable row tells a non-member exactly what an unknown id does", async () => {
    // The decode runs before the membership check, so without the Project riding on the failure a
    // corrupt row in someone else's Project would answer differently from an absent one — an
    // existence oracle for any id a caller cares to guess.
    const { authz } = backend({ an_corrupt_theirs: "proj_b" });
    const unknown = await authz
      .getDocument("an_absent")
      .catch((e: unknown) => e);
    const corrupt = await authz
      .getDocument("an_corrupt_theirs")
      .catch((e: unknown) => e);
    expect(corrupt).toBeInstanceOf(ResourceNotFoundError);
    expect((corrupt as Error).constructor).toBe((unknown as Error).constructor);
    expect((corrupt as Error).message).not.toContain("proj_b");
  });

  test("a member sees an unreadable row as the fault it is", async () => {
    const { authz } = backend({ an_corrupt_mine: "proj_a" });
    await expect(authz.getDocument("an_corrupt_mine")).rejects.toBeInstanceOf(
      UndecodableAnalysisError,
    );
  });

  test("an unknown analysis is not-found, same as a non-member", async () => {
    const { authz } = backend();
    await expect(authz.getDocument("an_absent")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
  });

  test("reading one analysis is authorized on the same terms", async () => {
    // The Analysis page resolves its subject through this, so a row outside the caller's Projects
    // must not reach it — and must not be distinguishable from one that does not exist.
    const { authz } = backend();
    await expect(authz.getAnalysis("an_mine")).resolves.toMatchObject({
      id: "an_mine",
    });
    await expect(authz.getAnalysis("an_theirs")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
    await expect(authz.getAnalysis("an_absent")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
  });
});

describe("AuthorizedBackend steer", () => {
  test("a member's turn reaches the run", async () => {
    const { authz, data } = backend();
    await authz.steerAnalysis(
      "an_mine",
      "Treat the exon as clinically relevant.",
    );
    expect(data.steers).toEqual([
      { analysisId: "an_mine", text: "Treat the exon as clinically relevant." },
    ]);
  });

  test("a non-member's turn is not-found and never reaches the run", async () => {
    // A turn is a write into someone else's session, so the refusal has to land before the data
    // plane — and it must not distinguish a Project the caller cannot reach from one that is not
    // there at all.
    const { authz, data } = backend();
    const error = await authz
      .steerAnalysis("an_theirs", "Most")
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ResourceNotFoundError);
    expect((error as Error).message).not.toContain("proj_b");
    await expect(
      authz.steerAnalysis("an_absent", "Most"),
    ).rejects.toBeInstanceOf(ResourceNotFoundError);
    expect(data.steers).toEqual([]);
  });

  test("a member's interrupt reaches the run", async () => {
    const { authz, data } = backend();
    await authz.interruptAnalysis("an_mine");
    expect(data.interrupts).toEqual(["an_mine"]);
  });

  test("a non-member's interrupt is not-found and never reaches the run", async () => {
    // An interrupt is a write into someone else's session on the same terms a turn is.
    const { authz, data } = backend();
    await expect(authz.interruptAnalysis("an_theirs")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
    await expect(authz.interruptAnalysis("an_absent")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
    expect(data.interrupts).toEqual([]);
  });
});

describe("AuthorizedBackend thread read", () => {
  test("a member reads a thread of their own run, against the row this resolved", async () => {
    // The session the thread is looked up in comes from the row, so the data plane is
    // handed the row and never an id the caller could have named a session with.
    const { authz, data } = backend();
    await authz.getThread("an_mine", "sthr_1");
    expect(data.threads).toEqual([
      { analysisId: "an_mine", threadId: "sthr_1" },
    ]);
    // One read of the row per access: the chokepoint hands it on rather than making
    // the data plane fetch it again.
    expect(data.rowReads).toBe(1);
  });

  test("a non-member's thread read is not-found and never reaches the data plane", async () => {
    const { authz, data } = backend();
    const error = await authz
      .getThread("an_theirs", "sthr_1")
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ResourceNotFoundError);
    expect((error as Error).message).not.toContain("proj_b");
    await expect(authz.getThread("an_absent", "sthr_1")).rejects.toBeInstanceOf(
      ResourceNotFoundError,
    );
    expect(data.threads).toEqual([]);
  });
});

describe("AuthorizedBackend listing", () => {
  test("lists the named Project's analyses for a member", async () => {
    const { authz, data } = backend();
    const analyses = await authz.listAnalyses("proj_a");
    expect(data.listScope).toEqual(["proj_a"]);
    expect(analyses.map((a) => a.id)).toEqual(["an_mine"]);
  });

  test("listing across Projects is scoped to the membership", async () => {
    // The Projects page counts from this one read, so its scope is the whole access boundary: an
    // Analysis in a Project the caller does not belong to must not be counted anywhere.
    const { authz, data } = backend();
    const analyses = await authz.listAllAnalyses();
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
      inputs: INPUTS,
      projectId: "proj_a",
    });
    expect(analysis.projectId).toBe("proj_a");
    expect(data.creates).toEqual([
      { inputs: INPUTS, projectId: "proj_a", userEmail: USER },
    ]);
  });

  test("creating in a non-member Project is not-found and creates nothing", async () => {
    const { authz, data } = backend();
    await expect(
      authz.createAnalysis({ inputs: INPUTS, projectId: "proj_b" }),
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
