import { create } from "@bufbuild/protobuf";
import { type Project, ProjectSchema } from "@/models/workbench";
import type { ProjectMembership } from "../../ports";
import { DEV_USER_EMAIL } from "./identity";

// Two, so the offline path exercises a caller who has to choose between Projects
// rather than one who lands on their only one. Ordered by name, as the SQL
// membership returns them.
const FIXTURE_PROJECTS = [
  { id: "proj_fixture", name: "Fixture Project" },
  { id: "proj_fixture_second", name: "Second Fixture Project" },
];

// Seeded user↔Project membership for the offline path: the dev user belongs to the
// seeded Projects. An unrecognized user belongs to nothing — default-deny, not a
// landing default.
const MEMBERSHIP = new Map<string, readonly { id: string; name: string }[]>([
  [DEV_USER_EMAIL, FIXTURE_PROJECTS],
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
