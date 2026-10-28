import { afterEach, describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import {
  GetAccessResponseSchema,
  ProjectGrantSchema,
  Role,
} from "@/gen/themis/rpc/auth_pb";
import type { Gate } from "./admission";
import { literatureContext, memberContext, userContext } from "./context";
import { isNotAdmittedError, isUnauthenticatedError } from "./errors";

// The data seam admits each request itself, from the process gate, whatever the perimeter decided:
// a handler reached around the perimeter still meets the same check.

const holder = globalThis as typeof globalThis & { __themisGate?: Gate };
const previous = holder.__themisGate;

afterEach(() => {
  holder.__themisGate = previous;
});

function installGate(admitted: boolean): void {
  holder.__themisGate = {
    identity: {
      async authenticate(headers) {
        if (!headers.has("x-goog-iap-jwt-assertion")) {
          throw Object.assign(new Error("missing x-goog-iap-jwt-assertion"), {
            name: "UnauthenticatedError",
          });
        }
        return { email: "curator@example.org" };
      },
    },
    access: {
      async getAccess() {
        return create(GetAccessResponseSchema, {
          projects: admitted
            ? [create(ProjectGrantSchema, { id: "asd", roles: [Role.CURATOR] })]
            : [],
        });
      },
    },
  };
}

const ASSERTED = new Headers({ "x-goog-iap-jwt-assertion": "an-assertion" });

const seams: [string, (headers: Headers) => Promise<unknown>][] = [
  ["userContext", userContext],
  ["memberContext", memberContext],
  [
    "literatureContext",
    (headers) =>
      literatureContext(new Request("https://t.example/", { headers })),
  ],
];

describe("the data seam", () => {
  test.each(seams)(
    "%s refuses a request with no assertion",
    async (_, seam) => {
      installGate(true);
      const error = await seam(new Headers()).catch((e: unknown) => e);
      expect(isUnauthenticatedError(error)).toBe(true);
    },
  );

  test.each(seams)(
    "%s refuses an authenticated caller the ACL does not admit",
    async (_, seam) => {
      installGate(false);
      const error = await seam(ASSERTED).catch((e: unknown) => e);
      expect(isNotAdmittedError(error)).toBe(true);
    },
  );

  test("memberContext answers an admitted caller with the Projects their access answer granted", async () => {
    installGate(true);
    const { userEmail, membership } = await memberContext(ASSERTED);
    expect(userEmail).toBe("curator@example.org");
    expect(membership.projects().map((p) => p.id)).toEqual(["asd"]);
  });
});
