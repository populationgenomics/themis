import { describe, expect, test } from "bun:test";
import { WORKSPACE_DAMAGED } from "@/workspace-copy/protocol";
import { retryCopy } from "./queries";

// A read of the browser's copy is tried again only after a failure another try can clear.

const named = (name: string) => Object.assign(new Error(name), { name });

describe("a failed read of the copy", () => {
  test("after a relay call failed, is tried again", () => {
    expect(retryCopy(0, named("ConnectError"))).toBe(true);
  });

  test("of a damaged repository is never tried again", () => {
    expect(retryCopy(0, named(WORKSPACE_DAMAGED))).toBe(false);
  });
});
