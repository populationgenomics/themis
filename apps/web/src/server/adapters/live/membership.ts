import { create } from "@bufbuild/protobuf";
import { type Project, ProjectSchema } from "@/models/workbench";
import type { ProjectMembership } from "../../ports";
import type { Sql } from "./sql";

/** The user↔Project mapping — reads the `project_members` table, joined to the
 *  `projects` registry for display names. Empty ⇒ the user reaches nothing
 *  (default-deny); a deployment is closed until memberships are seeded. */
export class Membership implements ProjectMembership {
  constructor(private readonly sql: Sql) {}

  isMember(userEmail: string, projectId: string): Promise<boolean> {
    return this.sql.isMember(userEmail, projectId);
  }

  async projectsOf(userEmail: string): Promise<Project[]> {
    const rows = await this.sql.projectsOf(userEmail);
    return rows.map((row) => create(ProjectSchema, row));
  }
}
