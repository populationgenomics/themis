import type { UserIdentity } from "../../identity";
import type {
  AnalysisPorts,
  ContentPort,
  LiteraturePort,
  ProjectMembership,
} from "../../ports";
import { createContent as buildContent } from "./content";
import { FixtureDataPlane } from "./data-plane";
import { DevUserIdentity } from "./identity";
import { FixtureLiterature, seedContentStore } from "./literature";
import { FIXTURE_PROJECT, FixtureMembership } from "./membership";
import { FixtureWorkspace } from "./workspace";

/** A FRESH in-memory data plane, the workspace repositories its scripted runs wrote, and document
 *  seeding into the fixture's Project. The runtime composition root (`../index.ts`) memoizes one
 *  set so a POST that creates an analysis and the following polls share the same in-memory state. */
export function createAnalysisPorts(): AnalysisPorts {
  const workspace = new FixtureWorkspace();
  const dataPlane = new FixtureDataPlane(workspace);
  return {
    dataPlane,
    workspace,
    documents: {
      projectId: FIXTURE_PROJECT,
      seedDocument: (projectId, prompt, files) =>
        dataPlane.seedDocument(projectId, prompt, files),
    },
  };
}

/** The seeded fixture membership. */
export function createMembership(): ProjectMembership {
  return new FixtureMembership();
}

/** The offline content port: serves the seeded corpus bytes. */
export function createContent(): ContentPort {
  return buildContent(seedContentStore());
}

/** The seeded offline literature corpus. */
export function createLiterature(
  content: ContentPort = createContent(),
): LiteraturePort {
  return new FixtureLiterature(content);
}

/** The offline identity: every request is the seed dev user, no assertion. */
export function createIdentity(): UserIdentity {
  return new DevUserIdentity();
}
