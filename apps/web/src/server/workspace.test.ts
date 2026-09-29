import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { RefDocSnapshotSchema } from "@/models/sheaf";
import { branchTip, COLLABORATIVE_BRANCH } from "./workspace";

const TIP = "9e270000000000000000000000000000000000bb";

describe("branchTip", () => {
  test("is absent while the branch has no commit", () => {
    const absentRepository = create(RefDocSnapshotSchema, {});
    const noBranch = create(RefDocSnapshotSchema, {
      document: {
        refs: { "refs/sheaf/reflog": { target: { case: "oid", value: TIP } } },
        head: { target: { case: "ref", value: COLLABORATIVE_BRANCH } },
      },
      generation: BigInt(1),
    });
    expect(branchTip(absentRepository)).toBeUndefined();
    expect(branchTip(noBranch)).toBeUndefined();
  });

  test("is the commit the branch names", () => {
    const snapshot = create(RefDocSnapshotSchema, {
      document: {
        refs: {
          [COLLABORATIVE_BRANCH]: { target: { case: "oid", value: TIP } },
        },
      },
      generation: BigInt(1),
    });
    expect(branchTip(snapshot)).toBe(TIP);
  });

  test("refuses a branch that names another ref", () => {
    // No push creates one, so the document is not one a writer of the repository made.
    const snapshot = create(RefDocSnapshotSchema, {
      document: {
        refs: {
          [COLLABORATIVE_BRANCH]: {
            target: { case: "ref", value: "refs/heads/other" },
          },
        },
      },
      generation: BigInt(1),
    });
    expect(() => branchTip(snapshot)).toThrow(COLLABORATIVE_BRANCH);
  });
});
