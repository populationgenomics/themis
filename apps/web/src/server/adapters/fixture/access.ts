import { create } from "@bufbuild/protobuf";
import {
  type GetAccessResponse,
  GetAccessResponseSchema,
  type ProjectGrant,
  ProjectGrantSchema,
  Role,
} from "@/gen/themis/rpc/auth_pb";
import type { AccessPort } from "../../ports";
import { DEV_USER_EMAIL } from "./identity";

// Two, so the offline path exercises a caller who has to choose between Projects
// rather than one who lands on their only one.
export const FIXTURE_PROJECT = "fixture-cohort";
export const SECOND_FIXTURE_PROJECT = "second-fixture-cohort";

/** The fixture Project's two curators besides the dev user, whom the curation fixture assigns. */
export const FIXTURE_CURATORS = [
  "sam.okafor@example.org",
  "rin.tanaka@example.org",
] as const;

/** The dev user's grants in the fixture's seeded ACL: both roles on the first Project, so the
 *  offline curation surface has a manager who also curates, and the curator role alone on the
 *  second, so it has a Project where the same person only curates. */
export const FIXTURE_GRANTS: readonly ProjectGrant[] = [
  create(ProjectGrantSchema, {
    id: FIXTURE_PROJECT,
    roles: [Role.MANAGER, Role.CURATOR],
  }),
  create(ProjectGrantSchema, {
    id: SECOND_FIXTURE_PROJECT,
    roles: [Role.CURATOR],
  }),
];

const GRANTS_BY_EMAIL: ReadonlyMap<string, readonly ProjectGrant[]> = new Map([
  [DEV_USER_EMAIL, FIXTURE_GRANTS],
  ...FIXTURE_CURATORS.map(
    (email) =>
      [
        email,
        [
          create(ProjectGrantSchema, {
            id: FIXTURE_PROJECT,
            roles: [Role.CURATOR],
          }),
        ],
      ] as const,
  ),
]);

/** Offline access, as the auth service would answer from the fixture's seeded ACL. Anyone the
 *  ACL does not list is granted no Project, so is not admitted: default-deny, not a landing
 *  default. */
export class FixtureAccess implements AccessPort {
  async getAccess(email: string): Promise<GetAccessResponse> {
    return create(GetAccessResponseSchema, {
      projects: [...(GRANTS_BY_EMAIL.get(email) ?? [])],
    });
  }
}
