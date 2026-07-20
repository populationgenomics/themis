import { create } from "@bufbuild/protobuf";
import { type Project, ProjectSchema } from "@/models/workbench";
import type { ProjectMembership } from "../../ports";
import { DEV_USER_EMAIL } from "./identity";

const FIXTURE_PROJECT = { id: "proj_fixture", name: "Fixture Project" };

// Seeded user↔Project membership for the offline path: the dev user belongs to the
// single seeded Project. An unrecognized user belongs to nothing — default-deny, not
// a landing default.
const MEMBERSHIP = new Map<string, readonly { id: string; name: string }[]>([
  [DEV_USER_EMAIL, [FIXTURE_PROJECT]],
]);

/** In-memory membership for the fixture path. */
export class FixtureMembership implements ProjectMembership {
  async isMember(userEmail: string, projectId: string): Promise<boolean> {
    return (MEMBERSHIP.get(userEmail) ?? []).some((p) => p.id === projectId);
  }

  async projectsOf(userEmail: string): Promise<Project[]> {
    return (MEMBERSHIP.get(userEmail) ?? []).map((p) =>
      create(ProjectSchema, p),
    );
  }
}
