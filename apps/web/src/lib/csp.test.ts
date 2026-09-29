import { describe, expect, test } from "bun:test";
import { getScriptNonceFromHeader } from "next/dist/server/app-render/get-script-nonce-from-header";
import * as csp from "./csp";

// The buckets alone: the policy needs no service URL, so a missing one must not fail a policy.
const LIVE = {
  THEMIS_BACKEND: "live",
  THEMIS_FULLTEXT_BUCKET: "cpg-themis-dev-fulltext",
};

const nonced = (options?: Partial<csp.PolicyOptions>) =>
  csp.policy(csp.mintNonce(), {
    development: false,
    contentSources: [],
    sheafSources: [],
    ...options,
  });

/** The sources named in one directive of a policy. */
const directive = (policy: string, name: string): string[] => {
  const found = policy
    .split(";")
    .map((part) => part.trim())
    .find((part) => part === name || part.startsWith(`${name} `));
  if (found === undefined) throw new Error(`no ${name} in ${policy}`);
  return found.split(/\s+/).slice(1);
};

describe("the nonce", () => {
  test("is unguessable, never repeats, and survives Next's parser", () => {
    const minted = Array.from({ length: 64 }, () => csp.mintNonce());
    expect(new Set(minted).size).toBe(minted.length);
    for (const nonce of minted) {
      // A nonce the parser Next reads it with cannot accept is dropped, and the page renders with
      // no script the policy admits.
      expect(getScriptNonceFromHeader(`script-src 'nonce-${nonce}'`)).toBe(
        nonce,
      );
      // 128 bits, base64. Shorter would be worth guessing.
      expect(nonce.length).toBeGreaterThanOrEqual(22);
    }
  });
});

describe("script sources", () => {
  test("no inline script is admitted", () => {
    const sources = directive(nonced(), "script-src");
    expect(sources).not.toContain("'unsafe-inline'");
    expect(sources).not.toContain("'unsafe-eval'");
    expect(sources).toContain("'strict-dynamic'");
    expect(sources.some((source) => source.startsWith("'nonce-"))).toBe(true);
  });

  test("the dev server's concessions do not reach production", () => {
    expect(directive(nonced({ development: true }), "script-src")).toContain(
      "'unsafe-eval'",
    );
    expect(directive(nonced(), "script-src")).not.toContain("'unsafe-eval'");
  });

  test("script-src is the first script-src* directive", () => {
    // Next selects the directive it takes the nonce from by prefix, so a `script-src-elem` or
    // `script-src-attr` placed ahead of `script-src` leaves every page unsigned.
    const names = nonced()
      .split(";")
      .map((part) => part.trim().split(/\s+/)[0])
      .filter((name) => name?.startsWith("script-src"));
    expect(names[0]).toBe("script-src");
  });
});

describe.each([true, false])("with development=%p", (development) => {
  test("no directive carries both a nonce and 'unsafe-inline'", () => {
    // CSP ignores 'unsafe-inline' in a directive that also names a nonce.
    const policy = nonced({ development });
    for (const name of ["script-src", "style-src", "style-src-attr"]) {
      const sources = directive(policy, name);
      if (sources.some((source) => source.startsWith("'nonce-"))) {
        expect(sources).not.toContain("'unsafe-inline'");
      }
    }
  });
});

describe("paper content sources", () => {
  test("the fixture backend admits nothing off-origin", () => {
    // It streams the bytes itself rather than redirecting, so there is nothing to admit.
    const sources = csp.contentSources({ THEMIS_BACKEND: "fixture" });
    expect(sources).toEqual([]);
    const policy = nonced({ contentSources: sources });
    for (const name of ["img-src", "connect-src"]) {
      expect(
        directive(policy, name).filter((source) => !source.startsWith("'")),
      ).toEqual([]);
    }
  });

  test("the live backend admits one bucket's path, not the shared host", () => {
    // The bare host is every GCS tenant's; script in the page could reach a bucket of its own.
    const sources = csp.contentSources(LIVE);
    expect(sources).toEqual([
      `https://storage.googleapis.com/${LIVE.THEMIS_FULLTEXT_BUCKET}/`,
    ]);
    for (const name of ["img-src", "connect-src"]) {
      expect(directive(nonced({ contentSources: sources }), name)).toEqual(
        expect.arrayContaining(sources),
      );
    }
  });

  test("a live backend missing the bucket refuses to build a policy", () => {
    // Rather than silently emitting a policy that blocks every paper's bytes.
    expect(() =>
      csp.contentSources({ ...LIVE, THEMIS_FULLTEXT_BUCKET: undefined }),
    ).toThrow("THEMIS_FULLTEXT_BUCKET");
  });

  test("an unnamed backend refuses to build a policy", () => {
    expect(() => csp.contentSources({})).toThrow("THEMIS_BACKEND");
  });
});

describe("pack download sources", () => {
  const SHEAF_LIVE = {
    ...LIVE,
    THEMIS_SHEAF_BUCKET: "cpg-themis-dev-sheaf-repositories",
  };
  const bare = (policy: string, name: string) =>
    directive(policy, name).filter((source) => !source.startsWith("'"));

  test("the fixture backend admits nothing off-origin", () => {
    // Its pack URLs name the BFF's own pack route.
    expect(csp.sheafSources({ THEMIS_BACKEND: "fixture" })).toEqual([]);
  });

  test("the live backend admits sheaf's bucket's path to the worker alone", () => {
    const sources = csp.sheafSources(SHEAF_LIVE);
    expect(sources).toEqual([
      `https://storage.googleapis.com/${SHEAF_LIVE.THEMIS_SHEAF_BUCKET}/`,
    ]);
    const options = {
      development: false,
      contentSources: [],
      sheafSources: sources,
    };
    expect(bare(csp.workerPolicy(options), "connect-src")).toEqual(sources);
    // The page never fetches a pack; the worker holding the copy does.
    expect(bare(csp.policy(csp.mintNonce(), options), "connect-src")).toEqual(
      [],
    );
  });

  test("a live backend missing the bucket refuses to build a policy", () => {
    expect(() =>
      csp.sheafSources({
        ...SHEAF_LIVE,
        THEMIS_SHEAF_BUCKET: undefined,
      }),
    ).toThrow("THEMIS_SHEAF_BUCKET");
  });

  test("a worker reaches everything the page does", () => {
    // A bundled worker is page code off its thread; a narrower policy would break a fetch moved
    // there.
    const options = {
      development: false,
      contentSources: csp.contentSources(SHEAF_LIVE),
      sheafSources: csp.sheafSources(SHEAF_LIVE),
    };
    const page = directive(csp.policy(csp.mintNonce(), options), "connect-src");
    expect(directive(csp.workerPolicy(options), "connect-src")).toEqual(
      expect.arrayContaining(page),
    );
  });

  test("a worker loads script from the app's own origin, unsigned", () => {
    // `importScripts` carries no nonce, and 'strict-dynamic' would drop 'self'.
    const sources = directive(
      csp.workerPolicy({
        development: false,
        contentSources: [],
        sheafSources: [],
      }),
      "script-src",
    );
    expect(sources).toEqual(["'self'"]);
  });
});

describe("the framing refusal", () => {
  test("names no origin", () => {
    expect(directive(nonced(), "frame-ancestors")).toEqual(["'none'"]);
  });
});
