import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import { POST as assignCurator } from "@/app/api/projects/[projectId]/curation/variants/[variantId]/assign/route";
import {
  type ProjectGrant,
  ProjectGrantSchema,
  Role,
} from "@/gen/themis/rpc/auth_pb";
import { ModeOfInheritance } from "@/gen/themis/svcv4/models/svcv4_pb";
import {
  FIXTURE_GRANTS,
  FIXTURE_PROJECT,
  FixtureAccess,
} from "@/server/adapters/fixture/access";
import type { Gate } from "@/server/admission";
import { isResourceNotFoundError } from "@/server/errors";
import { buildUserIdentity, DEV_USER_EMAIL } from "@/server/identity";
import { FixtureCurationStore } from "./fixture";
import { FixtureVariantResolver } from "./fixture-resolver";
import {
  type CurationBoundary,
  curationContext,
  toCurationErrorResponse,
} from "./http";
import type { VariantResolver } from "./resolver";
import type { CurationStore } from "./store";

// The boundary with its seams supplied, so the order of its checks is observable: a request another
// site caused is rejected before anyone asks who the caller is.

type Admission = CurationBoundary["member"];

/** Fails the test if the boundary consults it. */
const UNREACHED: Admission = async () => {
  throw new Error("admission was consulted");
};

/** The dev user, a curator of the fixture Project and of nothing else. */
const DEV_USER: Admission = async () => ({
  email: DEV_USER_EMAIL,
  roles: {
    rolesIn: (projectId) =>
      new Set(projectId === FIXTURE_PROJECT ? [Role.CURATOR] : []),
  },
});

function boundary(member: Admission): CurationBoundary {
  return {
    member,
    isCurator: async () => true,
    store: new FixtureCurationStore(),
    resolver: new FixtureVariantResolver(),
  };
}

const URL = `https://themis.example/api/projects/${FIXTURE_PROJECT}/curation/variants`;

function post(headers: Record<string, string>): Request {
  return new Request(URL, {
    method: "POST",
    headers: { "content-type": "text/plain", ...headers },
    body: '{"email":"lured@example.org"}',
  });
}

const thrownBy = (promise: Promise<unknown>): Promise<unknown> =>
  promise.then(
    () => {
      throw new Error("resolved");
    },
    (error: unknown) => error,
  );

describe("the curation boundary", () => {
  test("a same-origin request resolves the caller", async () => {
    const access = await curationContext(
      post({ "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors" }),
      FIXTURE_PROJECT,
      boundary(DEV_USER),
    );
    expect(access.email).toBe(DEV_USER_EMAIL);
  });

  test("a Project the caller holds no role on is not found", async () => {
    const error = await thrownBy(
      curationContext(post({}), "another-project", boundary(DEV_USER)),
    );
    expect(isResourceNotFoundError(error)).toBe(true);
  });

  test("the rejection reaches the caller as forbidden, naming the cross-site request", async () => {
    const error = await thrownBy(
      curationContext(
        post({ "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate" }),
        FIXTURE_PROJECT,
        boundary(UNREACHED),
      ),
    );
    const res = toCurationErrorResponse(error);
    expect(res.status).toBe(403);
    const body = (await res.json()) as {
      error: { code: string; message: string };
    };
    expect(body.error.code).toBe("forbidden");
    expect(body.error.message).toContain("cross-site request rejected");
  });
});

describe("assigning a curator through the assign route", () => {
  interface Globals {
    __themisGate?: Gate;
    __themisCuration?: { store?: CurationStore; resolver?: VariantResolver };
  }
  const globals = globalThis as typeof globalThis & Globals;
  let savedBackend: string | undefined;
  let savedGate: Gate | undefined;
  let savedCuration: Globals["__themisCuration"];

  beforeEach(() => {
    savedBackend = process.env.THEMIS_BACKEND;
    savedGate = globals.__themisGate;
    savedCuration = globals.__themisCuration;
  });

  afterEach(() => {
    if (savedBackend === undefined) delete process.env.THEMIS_BACKEND;
    else process.env.THEMIS_BACKEND = savedBackend;
    globals.__themisGate = savedGate;
    globals.__themisCuration = savedCuration;
  });

  async function setupAssignRoute() {
    process.env.THEMIS_BACKEND = "fixture";
    const grants = new Map<string, readonly ProjectGrant[]>([
      [DEV_USER_EMAIL, FIXTURE_GRANTS],
      [
        "elsewhere@example.org",
        [
          create(ProjectGrantSchema, {
            id: "other-project",
            roles: [Role.CURATOR],
          }),
        ],
      ],
      [
        "manager@example.org",
        [
          create(ProjectGrantSchema, {
            id: FIXTURE_PROJECT,
            roles: [Role.MANAGER],
          }),
        ],
      ],
      [
        "curator@example.org",
        [
          create(ProjectGrantSchema, {
            id: FIXTURE_PROJECT,
            roles: [Role.MANAGER, Role.CURATOR],
          }),
        ],
      ],
    ]);
    const store = new FixtureCurationStore();
    globals.__themisGate = {
      identity: buildUserIdentity({ THEMIS_BACKEND: "fixture" }),
      access: new FixtureAccess(grants),
    };
    globals.__themisCuration = {
      store,
      resolver: new FixtureVariantResolver(),
    };
    const variant = await store.createVariant(
      FIXTURE_PROJECT,
      {
        gene: "FBN1",
        transcript: "NM_000138.5",
        hgvsC: "c.7003C>T",
        clingenAlleleId: "",
        diseaseLabel: "Marfan syndrome",
        mondoId: "MONDO:0007947",
        modeOfInheritance: ModeOfInheritance.AUTOSOMAL_DOMINANT,
        clinicalContext: "Tall stature and ectopia lentis.",
      },
      DEV_USER_EMAIL,
    );
    const assign = (curatorEmail: string) =>
      assignCurator(
        new Request(
          `https://themis.example/api/projects/${FIXTURE_PROJECT}/curation/variants/${variant.id}/assign`,
          {
            method: "POST",
            headers: {
              "content-type": "application/json",
              "sec-fetch-site": "same-origin",
              "sec-fetch-mode": "cors",
            },
            body: JSON.stringify({ curatorEmail }),
          },
        ),
        {
          params: Promise.resolve({
            projectId: FIXTURE_PROJECT,
            variantId: variant.id,
          }),
        },
      );
    return { store, variant, assign };
  }

  test("mints a worksheet when the assignee holds the curator role on the Project", async () => {
    const { store, assign } = await setupAssignRoute();
    const res = await assign("curator@example.org");
    expect(res.status).toBe(200);
    const { id } = (await res.json()) as { id: string };
    const worksheet = await store.getWorksheet(id);
    expect(worksheet?.curatorEmail).toBe("curator@example.org");
  });

  test.each([
    ["a curator of another Project", "elsewhere@example.org"],
    ["a manager who does not curate", "manager@example.org"],
    ["someone the ACL does not list", "mallory@example.org"],
  ])("refuses %s (%s) with 400", async (_, email) => {
    const { store, variant, assign } = await setupAssignRoute();
    const res = await assign(email);
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error: { code: string } };
    expect(body.error.code).toBe("invalid_argument");
    expect(await store.worksheetsOfVariant(variant.id)).toEqual([]);
  });
});

describe("an auth service that cannot answer", () => {
  test("reaches the caller as unavailable, without its own wording", async () => {
    const res = toCurationErrorResponse(
      new ConnectError("the ACL is stale: detail", Code.Unavailable),
    );
    expect(res.status).toBe(503);
    const body = (await res.json()) as {
      error: { code: string; message: string };
    };
    expect(body.error.code).toBe("unavailable");
    expect(body.error.message).not.toContain("detail");
  });
});
