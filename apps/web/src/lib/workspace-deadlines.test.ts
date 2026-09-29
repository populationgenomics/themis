import { describe, expect, test } from "bun:test";
import { RELAY_DEADLINES_MS, SHEAF_DEADLINES_MS } from "./workspace-deadlines";

describe("the workspace calls' deadlines", () => {
  test("the browser waits longer than the BFF on every call it relays", () => {
    for (const call of Object.keys(
      SHEAF_DEADLINES_MS,
    ) as (keyof typeof SHEAF_DEADLINES_MS)[]) {
      expect(RELAY_DEADLINES_MS[call]).toBeGreaterThan(
        SHEAF_DEADLINES_MS[call],
      );
    }
  });
});
