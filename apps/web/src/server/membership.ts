import { create } from "@bufbuild/protobuf";
import { type ProjectGrant, Role } from "@/gen/themis/rpc/auth_pb";
import { type Project, ProjectSchema } from "@/models/workbench";
import type { ProjectMembership } from "./ports";

const NO_ROLE: ReadonlySet<Role> = new Set();

/** A caller's membership as one access answer granted it. Built per request and never kept, so a
 *  change to the ACL reaches the caller's next request. */
export class GrantedMembership implements ProjectMembership {
  private readonly roles: ReadonlyMap<string, ReadonlySet<Role>>;

  /** Throws for an answer the auth service never gives: a grant with no id or no role, an
   *  unspecified or repeated role, or one Project granted twice. */
  constructor(grants: readonly ProjectGrant[]) {
    const byId = new Map<string, ReadonlySet<Role>>();
    for (const grant of grants) {
      if (grant.id === "") throw new Error("a Project grant has no id");
      if (byId.has(grant.id)) {
        throw new Error(`Project ${grant.id} is granted twice`);
      }
      const roles = new Set(grant.roles);
      if (
        roles.size === 0 ||
        roles.size !== grant.roles.length ||
        roles.has(Role.UNSPECIFIED)
      ) {
        throw new Error(
          `Project ${grant.id} is granted with roles [${grant.roles.join(", ")}]; ` +
            "a grant carries each role at most once, and at least one",
        );
      }
      byId.set(grant.id, roles);
    }
    this.roles = byId;
  }

  isMember(projectId: string): boolean {
    return this.roles.has(projectId);
  }

  /** The caller's roles in `projectId`; empty for a Project they are not on. */
  rolesIn(projectId: string): ReadonlySet<Role> {
    return this.roles.get(projectId) ?? NO_ROLE;
  }

  hasRole(projectId: string, role: Role): boolean {
    return this.rolesIn(projectId).has(role);
  }

  projects(): Project[] {
    return [...this.roles.keys()]
      .sort((a, b) => a.localeCompare(b))
      .map((id) => create(ProjectSchema, { id }));
  }
}
