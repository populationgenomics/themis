import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import {
  GetAccessResponseSchema,
  ProjectGrantSchema,
  Role,
} from "@/gen/themis/rpc/auth_pb";
import { admit, type Gate } from "./admission";
import { isNotAdmittedError, UnauthenticatedError } from "./errors";
import { GrantedMembership } from "./membership";

// Admission is authentication followed by one access answer, and the membership a request is scoped
// to is built from that answer alone.

const grant = (id: string, roles: Role[] = [Role.CURATOR]) =>
  create(ProjectGrantSchema, { id, roles });

function gate(answer: { projects?: ReturnType<typeof grant>[] }): Gate & {
  asked: string[];
} {
  const asked: string[] = [];
  return {
    asked,
    identity: {
      async authenticate() {
        return { email: "john.doe@example.org" };
      },
    },
    access: {
      async getAccess(email) {
        asked.push(email);
        return create(GetAccessResponseSchema, answer);
      },
    },
  };
}

describe("admission", () => {
  test("an admitted caller comes with the Projects and roles the answer granted", async () => {
    const g = gate({ projects: [grant("asd", [Role.MANAGER])] });
    const { caller, membership } = await admit(new Headers(), g);
    expect(caller.email).toBe("john.doe@example.org");
    expect(membership.projects().map((p) => p.id)).toEqual(["asd"]);
    expect([...membership.rolesIn("asd")]).toEqual([Role.MANAGER]);
    expect(g.asked).toEqual(["john.doe@example.org"]);
  });

  test("a caller granted no Project is not admitted", async () => {
    const error = await admit(new Headers(), gate({})).catch(
      (thrown: unknown) => thrown,
    );
    expect(isNotAdmittedError(error)).toBe(true);
  });

  test("an answer granting a Project with no role is a fault, not an admission", async () => {
    await expect(
      admit(new Headers(), gate({ projects: [grant("asd", [])] })),
    ).rejects.toThrow("at least one");
  });

  test("an unauthenticated request is refused before access is asked", async () => {
    const g = gate({ projects: [grant("asd")] });
    const refusing: Gate = {
      identity: {
        async authenticate() {
          throw new UnauthenticatedError("no credential");
        },
      },
      access: g.access,
    };
    await expect(admit(new Headers(), refusing)).rejects.toThrow(
      "no credential",
    );
    expect(g.asked).toEqual([]);
  });
});

describe("the granted membership", () => {
  const membership = new GrantedMembership([grant("b"), grant("a")]);

  test("admits the granted Projects and no other", () => {
    expect(membership.isMember("a")).toBe(true);
    expect(membership.isMember("b")).toBe(true);
    expect(membership.isMember("c")).toBe(false);
  });

  test("lists them by id", () => {
    expect(membership.projects().map((p) => p.id)).toEqual(["a", "b"]);
  });

  test("holds each Project's roles, and none for a Project it does not hold", () => {
    const both = new GrantedMembership([
      grant("a", [Role.MANAGER, Role.CURATOR]),
      grant("b"),
    ]);
    expect(both.hasRole("a", Role.MANAGER)).toBe(true);
    expect(both.hasRole("a", Role.CURATOR)).toBe(true);
    expect(both.hasRole("b", Role.MANAGER)).toBe(false);
    expect(both.rolesIn("c").size).toBe(0);
  });

  test.each([
    ["no role", []],
    ["an unspecified role", [Role.UNSPECIFIED]],
    ["a repeated role", [Role.CURATOR, Role.CURATOR]],
  ])("refuses a grant carrying %s", (_, roles) => {
    expect(() => new GrantedMembership([grant("a", roles)])).toThrow(
      "at least one",
    );
  });

  test("refuses an answer that grants a Project twice", () => {
    expect(() => new GrantedMembership([grant("a"), grant("a")])).toThrow(
      "twice",
    );
  });
});
