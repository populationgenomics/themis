import { afterEach, expect, spyOn, test } from "bun:test";
import { warmLiveBackend } from "./warm";

const logged = spyOn(console, "error").mockImplementation(() => {});

afterEach(() => {
  logged.mockClear();
});

test("a warm start whose every step fails resolves, and logs each failure", async () => {
  // No backend, IAP or Cloud SQL inputs: both steps throw before reaching any network.
  await expect(warmLiveBackend({})).resolves.toBeUndefined();
  const failures = logged.mock.calls.map(([message]) => String(message));
  expect(failures).toEqual([
    expect.stringContaining("IAP public keys failed"),
    expect.stringContaining("Cloud SQL connection failed"),
  ]);
});
