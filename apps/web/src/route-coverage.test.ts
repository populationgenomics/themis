import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as net from "node:net";
import * as path from "node:path";

// Every route the production build serves is refused to a caller with no IAP assertion, but for the
// perimeter's one exemption and Next's build assets. Walks the build's own manifests —
// app routes, their RSC and segment prefetch variants, server actions — and requests each from the
// standalone server the image runs, configured for the live backend with every dependency
// unreachable, so a refusal can only come from the perimeter. Reads the last build, as
// `rendering.test.ts` does: `bun run build` writes it, and CI builds before it tests.

const STANDALONE = path.join(import.meta.dir, "..", ".next", "standalone");
const BUILD = path.join(STANDALONE, ".next");
const PUBLIC = path.join(import.meta.dir, "..", "public");
const STATIC = path.join(import.meta.dir, "..", ".next", "static");

const EXEMPT = ["/api/healthz"];

const built = fs.existsSync(path.join(BUILD, "BUILD_ID"));
if (!built) {
  // Locally this is a build that has not run yet. In CI it means the build step stopped preceding
  // the test step, and skipping would retire the check silently.
  const absent = `${BUILD} holds no build; run \`bun run build\` first`;
  if (process.env.CI) throw new Error(absent);
  console.warn(`route-coverage.test.ts: ${absent}`);
}

function manifest<T>(relative: string): T {
  const file = path.join(BUILD, relative);
  if (!fs.existsSync(file)) {
    throw new Error(`${file} is missing: run \`bun run build\` first`);
  }
  return JSON.parse(fs.readFileSync(file, "utf8")) as T;
}

interface RoutesManifest {
  staticRoutes: { page: string }[];
  dynamicRoutes: { page: string }[];
  rsc: {
    header: string;
    prefetchHeader: string;
    suffix: string;
    prefetchSuffix?: string;
    prefetchSegmentHeader?: string;
    prefetchSegmentSuffix?: string;
    prefetchSegmentDirSuffix?: string;
  };
}

interface ServerReferenceManifest {
  node: Record<string, { workers: Record<string, unknown> }>;
  edge: Record<string, { workers: Record<string, unknown> }>;
}

interface Case {
  readonly name: string;
  readonly path: string;
  readonly init: RequestInit;
}

const PAGE_LOAD = {
  "sec-fetch-mode": "navigate",
  "sec-fetch-dest": "document",
  "sec-fetch-site": "none",
  accept: "text/html",
};
const SCRIPT = { "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin" };

function rscPath(route: string, suffix: string): string {
  return route === "/" ? `/index${suffix}` : `${route}${suffix}`;
}

/** A concrete path for a route pattern: each dynamic segment filled with a placeholder. */
function concrete(route: string): string {
  return route
    .replace(/\[\[\.\.\.[^\]]+\]\]/g, "a")
    .replace(/\[\.\.\.[^\]]+\]/g, "a/b")
    .replace(/\[[^\]]+\]/g, "x");
}

/** Every way a page or route can be asked for: as a page load, by a script with each method, and as
 *  the RSC payload and prefetches the client router fetches. */
function routeCases(route: string, rsc: RoutesManifest["rsc"]): Case[] {
  const p = concrete(route);
  const cases: Case[] = [
    {
      name: "page load",
      path: p,
      init: { headers: PAGE_LOAD },
    },
    {
      name: "HEAD page load",
      path: p,
      init: { method: "HEAD", headers: PAGE_LOAD },
    },
    { name: "GET, no metadata", path: p, init: {} },
    {
      name: "script POST",
      path: p,
      init: { method: "POST", headers: SCRIPT, body: "{}" },
    },
    {
      name: "script PUT",
      path: p,
      init: { method: "PUT", headers: SCRIPT, body: "{}" },
    },
    {
      name: "script DELETE",
      path: p,
      init: { method: "DELETE", headers: SCRIPT },
    },
    {
      name: "RSC request",
      path: p,
      init: { headers: { ...SCRIPT, [rsc.header]: "1" } },
    },
    {
      name: "RSC prefetch",
      path: p,
      init: {
        headers: { ...SCRIPT, [rsc.header]: "1", [rsc.prefetchHeader]: "1" },
      },
    },
    {
      name: "RSC suffix",
      path: rscPath(p, rsc.suffix),
      init: { headers: SCRIPT },
    },
  ];
  if (rsc.prefetchSuffix !== undefined) {
    cases.push({
      name: "RSC prefetch suffix",
      path: rscPath(p, rsc.prefetchSuffix),
      init: { headers: SCRIPT },
    });
  }
  if (
    rsc.prefetchSegmentDirSuffix !== undefined &&
    rsc.prefetchSegmentSuffix !== undefined
  ) {
    cases.push({
      name: "segment prefetch",
      path: `${rscPath(p, rsc.prefetchSegmentDirSuffix)}/_tree${rsc.prefetchSegmentSuffix}`,
      init: {
        headers: {
          ...SCRIPT,
          [rsc.header]: "1",
          [rsc.prefetchHeader]: "1",
          ...(rsc.prefetchSegmentHeader
            ? { [rsc.prefetchSegmentHeader]: "/_tree" }
            : {}),
        },
      },
    });
  }
  return cases;
}

interface Walk {
  routePatterns: string[];
  guarded: string[];
  cases: Case[];
}

/** The build's routes, and every request the walk makes of them. */
function walk(): Walk {
  const appPaths = manifest<Record<string, string>>(
    "app-path-routes-manifest.json",
  );
  const routes = manifest<RoutesManifest>("routes-manifest.json");
  const actions = manifest<ServerReferenceManifest>(
    "server/server-reference-manifest.json",
  );

  const routePatterns = [
    ...new Set([
      ...Object.values(appPaths),
      ...routes.staticRoutes.map((route) => route.page),
      ...routes.dynamicRoutes.map((route) => route.page),
    ]),
  ].sort();
  const guarded = routePatterns.filter((route) => !EXEMPT.includes(route));

  // Each server action is posted to the route of the page whose worker holds it.
  const actionRoutes = [actions.node, actions.edge]
    .flatMap((byId) => Object.entries(byId))
    .flatMap(([id, { workers }]) =>
      Object.keys(workers).map((worker): [string, string] => {
        const route = appPaths[`/${worker.replace(/^app\//, "")}`];
        if (route === undefined) {
          throw new Error(
            `server action ${id} is held by ${worker}, which serves no route`,
          );
        }
        return [id, concrete(route)];
      }),
    );

  // Paths no route manifest lists that the server answers all the same, and spellings of the
  // exemptions that are not the exemptions.
  const unlisted = [
    ...fs.readdirSync(PUBLIC).map((name) => `/${name}`),
    "/_next/image?url=%2Fthemis-logo.png&w=64&q=75",
    "/_next/staticx",
    "/_next/data/build/index.json",
    "/favicon.ico",
    "/no-such-route",
    "/API/healthz",
    "/api%2Fhealthz",
    "/api/healthz/x",
    "/auth/login",
    "/auth/callback",
  ];

  const cases: Case[] = [
    ...guarded.flatMap((route) => routeCases(route, routes.rsc)),
    ...actionRoutes.map(
      ([id, route]): Case => ({
        name: `server action ${id}`,
        path: route,
        init: {
          method: "POST",
          headers: {
            ...SCRIPT,
            "next-action": id,
            "content-type": "text/plain;charset=UTF-8",
          },
          body: "[]",
        },
      }),
    ),
    ...unlisted.flatMap((p): Case[] => [
      {
        name: "page load",
        path: p,
        init: { headers: PAGE_LOAD },
      },
      { name: "GET, no metadata", path: p, init: {} },
    ]),
  ];
  return { routePatterns, guarded, cases };
}

const { routePatterns, guarded, cases }: Walk = built
  ? walk()
  : { routePatterns: [], guarded: [], cases: [] };

async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => {
        if (address === null || typeof address === "string") {
          reject(new Error("no port"));
        } else {
          resolve(address.port);
        }
      });
    });
  });
}

/** The live backend's config, with every dependency at an address that refuses connections: a
 *  request the perimeter let through would fail at a dependency, never be served. */
const UNREACHABLE = "127.0.0.1:9";
const LIVE_ENV = {
  NODE_ENV: "production",
  NEXT_TELEMETRY_DISABLED: "1",
  THEMIS_BACKEND: "live",
  THEMIS_TRACE_SAMPLE_RATIO: "0",
  THEMIS_PROJECT_NUMBER: "123456789",
  THEMIS_REGION: "australia-southeast1",
  THEMIS_WEB_SERVICE_NAME: "themis-web",
  THEMIS_AUTH_URL: `https://${UNREACHABLE}`,
  THEMIS_FULLTEXT_BUCKET: "route-coverage-corpus",
  THEMIS_SHEAF_BUCKET: "route-coverage-sheaf",
};

let origin = "";
let server: ReturnType<typeof Bun.spawn> | undefined;

beforeAll(async () => {
  if (!built) return;
  // Where the image puts Next's build assets beside the standalone server (Dockerfile).
  fs.cpSync(STATIC, path.join(BUILD, "static"), { recursive: true });
  const port = await freePort();
  origin = `http://127.0.0.1:${port}`;
  server = Bun.spawn(["bun", "server.js"], {
    cwd: STANDALONE,
    env: {
      PATH: process.env.PATH,
      HOME: process.env.HOME,
      ...LIVE_ENV,
      PORT: String(port),
      HOSTNAME: "127.0.0.1",
    },
    stdout: "ignore",
    stderr: "ignore",
  });
  const deadline = Date.now() + 60_000;
  for (;;) {
    try {
      if ((await fetch(`${origin}/api/healthz`)).ok) return;
    } catch {
      // Not listening yet.
    }
    if (server.exitCode !== null) {
      throw new Error(`the server exited with ${server.exitCode}`);
    }
    if (Date.now() > deadline)
      throw new Error("the server did not start within 60 s");
    await Bun.sleep(250);
  }
}, 70_000);

afterAll(() => {
  server?.kill();
});

const send = (c: Pick<Case, "path" | "init">) =>
  fetch(`${origin}${c.path}`, { ...c.init, redirect: "manual" });

describe.skipIf(!built)("the production build's routes", () => {
  test("the walk found the routes it is meant to cover", () => {
    // Non-empty rules out a vacuous pass; the exemptions must be routes, or they exempt nothing.
    expect(guarded.length).toBeGreaterThan(0);
    for (const route of EXEMPT) expect(routePatterns).toContain(route);
  });

  test.each(cases.map((c) => [`${c.name}: ${c.path}`, c] as const))(
    "%s is refused without an assertion",
    async (_, c) => {
      const response = await send(c);
      // A page load too: only a request that went around IAP arrives without an assertion, so there
      // is no sign-in to send it to.
      expect(response.status).toBe(401);
      if (c.init.method !== "HEAD") {
        expect(await response.json()).toEqual({
          code: "unauthenticated",
          message: "unauthenticated",
        });
      }
    },
  );

  test("the liveness probe answers without a credential", async () => {
    const response = await send({ path: "/api/healthz", init: {} });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
  });

  test("/api/healthz/ is sent to the exemption it spells, not served as one", async () => {
    // Next's own trailing-slash redirect, which runs before the proxy.
    const response = await send({ path: "/api/healthz/", init: {} });
    expect(response.status).toBe(308);
    expect(
      new URL(response.headers.get("location") ?? "", origin).pathname,
    ).toBe("/api/healthz");
  });

  // A malformed or forged assertion is refused before any key is fetched only in the identity's unit
  // tests (adapters/live/identity.test.ts): here the verifier would fetch IAP's published keys first.
  test.each([
    [
      "IAP's plaintext user header",
      {
        "x-goog-authenticated-user-email":
          "accounts.google.com:curator@example.org",
      },
    ],
    [
      "a serverless authorization header",
      { "x-serverless-authorization": "Bearer forged" },
    ],
    ["a bearer token", { authorization: "Bearer forged" }],
    ["an empty assertion", { "x-goog-iap-jwt-assertion": "" }],
  ])("%s without an assertion is refused 401", async (_, credential) => {
    for (const path of [
      "/",
      "/api/rpc/themis.workbench.rpc.Workbench/ListProjects",
    ]) {
      const response = await send({
        path,
        init: { method: "POST", headers: { ...SCRIPT, ...credential } },
      });
      expect(response.status).toBe(401);
    }
  });

  test("a build asset is served without a credential", async () => {
    const chunk = fs
      .readdirSync(path.join(STATIC, "chunks"))
      .find((name) => name.endsWith(".js"));
    if (chunk === undefined) throw new Error("the build has no chunks");
    const response = await send({
      path: `/_next/static/chunks/${chunk}`,
      init: {},
    });
    expect(response.status).toBe(200);
    // The worker policy, which carries no render nonce, unlike a page's.
    const policy = response.headers.get("content-security-policy") ?? "";
    expect(policy).toContain("script-src 'self'");
    expect(policy).not.toContain("'nonce-");
  });

  test.each([
    "/_next/static/chunks/no-such-chunk.js",
    "/_next/static/no-such-dir/no-such-asset.css",
  ])("%s, a build asset that does not exist, is not found", async (path) => {
    const response = await send({ path, init: {} });
    expect(response.status).toBe(404);
  });
});
