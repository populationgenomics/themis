import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import {
  GetAccessResponseSchema,
  ProjectGrantSchema,
  Role,
} from "@/gen/themis/rpc/auth_pb";
import { FIXTURE_PROJECT } from "@/server/adapters/fixture/access";
import { isResourceNotFoundError } from "@/server/errors";
import { isCrossSiteRequestError } from "@/server/fetch-metadata";
import { DEV_USER_EMAIL } from "@/server/identity";
import { FixtureCurationStore } from "./fixture";
import { FixtureVariantResolver } from "./fixture-resolver";
import {
  type CurationBoundary,
  curationContext,
  curatorCheckThrough,
  toCurationErrorResponse,
} from "./http";

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
  test("a cross-site POST is rejected before the caller is resolved", async () => {
    const error = await thrownBy(
      curationContext(
        post({
          "sec-fetch-site": "cross-site",
          "sec-fetch-mode": "navigate",
          "sec-fetch-dest": "document",
        }),
        FIXTURE_PROJECT,
        boundary(UNREACHED),
      ),
    );
    expect(isCrossSiteRequestError(error)).toBe(true);
  });

  test("a cross-site GET that is not a navigation is rejected too", async () => {
    const error = await thrownBy(
      curationContext(
        new Request(URL, {
          headers: { "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors" },
        }),
        FIXTURE_PROJECT,
        boundary(UNREACHED),
      ),
    );
    expect(isCrossSiteRequestError(error)).toBe(true);
  });

  test("a same-origin request resolves the caller", async () => {
    const access = await curationContext(
      post({ "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors" }),
      FIXTURE_PROJECT,
      boundary(DEV_USER),
    );
    expect(access.email).toBe(DEV_USER_EMAIL);
  });

  test("a request carrying no Fetch Metadata resolves the caller", async () => {
    const access = await curationContext(
      post({}),
      FIXTURE_PROJECT,
      boundary(DEV_USER),
    );
    expect(access.email).toBe(DEV_USER_EMAIL);
  });

  test("a page reached by following a link from another site resolves the caller", async () => {
    // The pages hand the boundary a GET carrying the navigation's own headers.
    const access = await curationContext(
      new Request("http://internal/curation", {
        headers: {
          "sec-fetch-site": "cross-site",
          "sec-fetch-mode": "navigate",
          "sec-fetch-dest": "document",
        },
      }),
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

describe("the curator check", () => {
  // As the auth service would answer for each person: roles per Project.
  const answers: Record<string, [string, Role[]][]> = {
    "elsewhere@example.org": [["other-project", [Role.CURATOR]]],
    "manager@example.org": [[FIXTURE_PROJECT, [Role.MANAGER]]],
    "curator@example.org": [[FIXTURE_PROJECT, [Role.MANAGER, Role.CURATOR]]],
  };
  const isCurator = curatorCheckThrough({
    async getAccess(email) {
      return create(GetAccessResponseSchema, {
        projects: (answers[email] ?? []).map(([id, roles]) =>
          create(ProjectGrantSchema, { id, roles }),
        ),
      });
    },
  });

  test.each([
    ["a curator of another Project", "elsewhere@example.org", false],
    ["a manager who does not curate", "manager@example.org", false],
    ["someone the file does not list", "mallory@example.org", false],
    ["a curator of the Project", "curator@example.org", true],
  ])("holds for %s: %s → %p", async (_, email, expected) => {
    expect(await isCurator(email, FIXTURE_PROJECT)).toBe(expected);
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
