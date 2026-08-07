import type { UserIdentity } from "../../identity";
import type {
  AnalysisDataPlane,
  ContentPort,
  LiteraturePort,
  ProjectMembership,
} from "../../ports";
import { createContent as buildContent } from "./content";
import { FixtureDataPlane } from "./data-plane";
import { DevUserIdentity } from "./identity";
import { FixtureLiterature, seedContentStore } from "./literature";
import { FixtureMembership } from "./membership";

/** A FRESH in-memory data plane. The runtime composition root (`../index.ts`)
 *  memoizes one instance so a POST that creates an analysis and the following
 *  polls share the same in-memory state. */
export function createDataPlane(): AnalysisDataPlane {
  return new FixtureDataPlane();
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
