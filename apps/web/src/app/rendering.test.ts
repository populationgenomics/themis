import { describe, expect, test } from "bun:test";
import fs from "node:fs";
import path from "node:path";

// Whether the app still renders every page per request, read off the last build. `bun run build`
// writes the manifest, and CI builds before it tests.

const MANIFEST = path.join(
  import.meta.dir,
  "../../.next/prerender-manifest.json",
);

/** Prerendered routes that need no nonce, each for a reason a rebuild cannot change. */
const NONCELESS_BY_NATURE: Record<string, string> = {
  "/icon.svg": "an image, so it carries no script",
  // A `global-error.tsx` declaring `force-dynamic` does not move this one.
  "/_global-error":
    "Next's root error shell, which cannot be opted out of prerendering; reachable only as a typed" +
    " URL, since a root error raised in the browser renders it with the runtime already live and a" +
    " server-side render failure is rendered through the request",
};

const built = fs.existsSync(MANIFEST);
if (!built) {
  // Locally this is a build that has not run yet. In CI it means the build step stopped preceding
  // the test step, and skipping would retire the check silently.
  const absent = `${MANIFEST} is absent; run \`bun run build\` first`;
  if (process.env.CI) throw new Error(absent);
  console.warn(`rendering.test.ts: ${absent}`);
}

describe.skipIf(!built)("every page renders per request", () => {
  test("no page is prerendered without a stated reason", () => {
    // A page rendered without a request carries no nonce, and the policy then admits none of its
    // script — a blank page, with nothing failing server-side to say so (src/app/layout.tsx).
    const manifest = JSON.parse(fs.readFileSync(MANIFEST, "utf8")) as {
      routes: Record<string, unknown>;
      // A dynamic route with a fallback is a prerendered shell too, by another name.
      dynamicRoutes: Record<string, unknown>;
    };
    const prerendered = [
      ...Object.keys(manifest.routes),
      ...Object.keys(manifest.dynamicRoutes),
    ];
    // Rules out a vacuous pass against a manifest that lists nothing at all.
    expect(prerendered).not.toEqual([]);
    const unexplained = prerendered.filter(
      (route) => !(route in NONCELESS_BY_NATURE),
    );
    expect(
      unexplained,
      `add a reason to NONCELESS_BY_NATURE, or make these render per request`,
    ).toEqual([]);
  });
});
