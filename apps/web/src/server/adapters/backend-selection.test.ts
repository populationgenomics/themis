import { describe, expect, test } from "bun:test";
import { selectedBackend } from "./index";

// Selecting a backend is deliberate. The fixture's identity resolver attributes
// every request to the seed dev user without verifying an assertion, so an absent
// or unrecognised `THEMIS_BACKEND` must fail rather than resolve to it — a deploy
// that lost the variable fails closed instead of authenticating everyone.

describe("selectedBackend", () => {
  test("resolves the two named backends", () => {
    expect(selectedBackend({ THEMIS_BACKEND: "fixture" })).toBe("fixture");
    expect(selectedBackend({ THEMIS_BACKEND: "real" })).toBe("real");
  });

  test("an absent value is a misconfiguration, not the fixture", () => {
    expect(() => selectedBackend({})).toThrow(/THEMIS_BACKEND/);
    expect(() => selectedBackend({ THEMIS_BACKEND: undefined })).toThrow(
      /THEMIS_BACKEND/,
    );
    expect(() => selectedBackend({ THEMIS_BACKEND: "" })).toThrow(
      /THEMIS_BACKEND/,
    );
  });

  test("an unrecognised value fails loud", () => {
    expect(() => selectedBackend({ THEMIS_BACKEND: "REAL" })).toThrow(
      /THEMIS_BACKEND/,
    );
    expect(() => selectedBackend({ THEMIS_BACKEND: "prod" })).toThrow(
      /THEMIS_BACKEND/,
    );
  });
});
