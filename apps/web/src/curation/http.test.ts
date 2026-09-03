import { describe, expect, test } from "bun:test";
import { isCrossSiteRequestError } from "@/server/fetch-metadata";
import { DEV_USER_EMAIL, type UserIdentity } from "@/server/identity";
import { FixtureCurationStore } from "./fixture";
import { FixtureVariantResolver } from "./fixture-resolver";
import {
  type CurationBoundary,
  curationContext,
  toCurationErrorResponse,
} from "./http";

// The boundary with its seams supplied, so the order of its checks is observable: a request another
// site caused is rejected before anyone asks who the caller is.

/** Fails the test if the boundary consults it. */
const UNREACHED: UserIdentity = {
  async assertedEmail() {
    throw new Error("identity was consulted");
  },
};

const DEV_USER: UserIdentity = {
  async assertedEmail() {
    return DEV_USER_EMAIL;
  },
};

function boundary(identity: UserIdentity): CurationBoundary {
  return {
    identity,
    store: new FixtureCurationStore(),
    resolver: new FixtureVariantResolver(),
  };
}

const URL = "https://themis.example/api/curation/people";

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
        boundary(UNREACHED),
      ),
    );
    expect(isCrossSiteRequestError(error)).toBe(true);
  });

  test("a same-origin request resolves the caller", async () => {
    const access = await curationContext(
      post({ "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors" }),
      boundary(DEV_USER),
    );
    expect(access.email).toBe(DEV_USER_EMAIL);
  });

  test("a request carrying no Fetch Metadata resolves the caller", async () => {
    const access = await curationContext(post({}), boundary(DEV_USER));
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
      boundary(DEV_USER),
    );
    expect(access.email).toBe(DEV_USER_EMAIL);
  });

  test("the rejection reaches the caller as forbidden, naming the cross-site request", async () => {
    const error = await thrownBy(
      curationContext(
        post({ "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate" }),
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
