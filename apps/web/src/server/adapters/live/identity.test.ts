import { describe, expect, setSystemTime, test } from "bun:test";
import * as crypto from "node:crypto";
import { type gaxios, OAuth2Client } from "google-auth-library";
import { UnauthenticatedError } from "../../errors";
import { loadIapConfig } from "./config";
import { freshSeconds, IapVerifier } from "./identity";

// The verifier's real signature, issuer and audience checks, against a key the test
// holds in place of IAP's published ones.

const KID = "test-key";
const CONFIG = {
  projectNumber: "123456789",
  region: "australia-southeast1",
  serviceName: "themis-web",
};
const SERVICE_AUDIENCE =
  "/projects/123456789/locations/australia-southeast1/services/themis-web";

const keys = crypto.generateKeyPairSync("ec", { namedCurve: "P-256" });

class LocalKeyClient extends OAuth2Client {
  override async getIapPublicKeys() {
    const pem = keys.publicKey.export({ type: "spki", format: "pem" });
    return { pubkeys: { [KID]: pem.toString() } };
  }
}

const base64url = (value: string | Buffer) =>
  Buffer.from(value).toString("base64url");

/** An ES256 JWT shaped like IAP's assertion, signed with the test's key. */
function assertion(claims: Record<string, unknown>): string {
  const now = Math.floor(Date.now() / 1000);
  const header = base64url(JSON.stringify({ alg: "ES256", kid: KID }));
  const payload = base64url(
    JSON.stringify({
      iss: "https://cloud.google.com/iap",
      aud: SERVICE_AUDIENCE,
      email: "curator@example.org",
      sub: "accounts.google.com:1",
      iat: now,
      exp: now + 600,
      ...claims,
    }),
  );
  const signature = crypto.sign("sha256", Buffer.from(`${header}.${payload}`), {
    key: keys.privateKey,
    dsaEncoding: "ieee-p1363",
  });
  return `${header}.${payload}.${base64url(signature)}`;
}

const verifier = new IapVerifier(CONFIG, new LocalKeyClient());

const carrying = (jwt: string) =>
  new Headers({ "x-goog-iap-jwt-assertion": jwt });

describe("IapVerifier", () => {
  test("admits an assertion for the Cloud Run service and returns its email", async () => {
    expect(await verifier.assertedEmail(carrying(assertion({})))).toBe(
      "curator@example.org",
    );
  });

  test.each([
    [
      "a load-balancer backend service's audience",
      { aud: "/projects/123456789/global/backendServices/1" },
    ],
    [
      "another service's audience",
      {
        aud: "/projects/123456789/locations/australia-southeast1/services/themis-sheaf",
      },
    ],
    ["another issuer", { iss: "https://accounts.google.com" }],
    [
      "an expiry in the past",
      {
        iat: Math.floor(Date.now() / 1000) - 7200,
        exp: Math.floor(Date.now() / 1000) - 3600,
      },
    ],
    ["no email claim", { email: undefined }],
  ])("refuses an assertion with %s", async (_, claims) => {
    await expect(
      verifier.assertedEmail(carrying(assertion(claims))),
    ).rejects.toBeInstanceOf(UnauthenticatedError);
  });

  test("refuses an assertion signed by another key", async () => {
    const [header, payload] = assertion({}).split(".");
    const other = crypto.generateKeyPairSync("ec", { namedCurve: "P-256" });
    const signature = crypto.sign(
      "sha256",
      Buffer.from(`${header}.${payload}`),
      { key: other.privateKey, dsaEncoding: "ieee-p1363" },
    );
    const forged = `${header}.${payload}.${base64url(signature)}`;
    await expect(
      verifier.assertedEmail(carrying(forged)),
    ).rejects.toBeInstanceOf(UnauthenticatedError);
  });

  test("propagates a key-fetch failure as an outage, not a 401", async () => {
    class UnreachableKeys extends OAuth2Client {
      override async getIapPublicKeys(): Promise<never> {
        throw new Error("gstatic unreachable");
      }
    }
    const outage = new IapVerifier(CONFIG, new UnreachableKeys());
    const refusal = outage.assertedEmail(carrying(assertion({})));
    await expect(refusal).rejects.toThrow("gstatic unreachable");
    await expect(refusal).rejects.not.toBeInstanceOf(UnauthenticatedError);
  });

  test("refuses a request with no assertion", async () => {
    await expect(verifier.assertedEmail(new Headers())).rejects.toBeInstanceOf(
      UnauthenticatedError,
    );
  });
});

/** A key source that counts its fetches and answers each with the test's key under `headers`, or
 *  fails the fetches `failing` names (1-based). */
class CountingKeyClient extends OAuth2Client {
  fetches = 0;

  constructor(
    private readonly headers?: Record<string, string>,
    private readonly failing: ReadonlySet<number> = new Set(),
  ) {
    super();
  }

  override async getIapPublicKeys(): Promise<{
    pubkeys: Record<string, string>;
    res?: gaxios.GaxiosResponse<void>;
  }> {
    this.fetches += 1;
    if (this.failing.has(this.fetches)) throw new Error("gstatic unreachable");
    const pem = keys.publicKey.export({ type: "spki", format: "pem" });
    const res =
      this.headers &&
      (Object.assign(new Response(null, { headers: this.headers }), {
        data: undefined,
      }) as gaxios.GaxiosResponse<void>);
    return { pubkeys: { [KID]: pem.toString() }, res };
  }
}

describe("IapVerifier's keys", () => {
  test("are reused while the response keeps them fresh, and fetched once it does not", async () => {
    const start = Date.now();
    const client = new CountingKeyClient({
      "cache-control": "public, max-age=3000",
      age: "1000",
    });
    const cached = new IapVerifier(CONFIG, client);
    try {
      await cached.assertedEmail(carrying(assertion({})));
      setSystemTime(start + 1_999_000);
      await cached.assertedEmail(carrying(assertion({})));
      expect(client.fetches).toBe(1);
      setSystemTime(start + 2_001_000);
      await cached.assertedEmail(carrying(assertion({})));
      expect(client.fetches).toBe(2);
    } finally {
      setSystemTime();
    }
  });

  test("are not reused when the response holds no keys", async () => {
    class EmptyKeyClient extends CountingKeyClient {
      override async getIapPublicKeys() {
        const fetched = await super.getIapPublicKeys();
        return { ...fetched, pubkeys: {} };
      }
    }
    const client = new EmptyKeyClient({ "cache-control": "max-age=3000" });
    const verifier = new IapVerifier(CONFIG, client);
    await expect(verifier.warm()).rejects.toThrow("not a non-empty map");
    await expect(verifier.warm()).rejects.toThrow("not a non-empty map");
    expect(client.fetches).toBe(2);
  });

  test.each([
    ["states no max-age", { "cache-control": "no-cache" }],
    [
      "has spent its max-age in a cache",
      { "cache-control": "max-age=60", age: "60" },
    ],
    ["carries no headers", undefined],
  ])("are fetched anew when the response %s", async (_, headers) => {
    const client = new CountingKeyClient(headers);
    const uncached = new IapVerifier(CONFIG, client);
    await uncached.assertedEmail(carrying(assertion({})));
    await uncached.assertedEmail(carrying(assertion({})));
    expect(client.fetches).toBe(2);
  });

  test("are fetched once for concurrent requests", async () => {
    const client = new CountingKeyClient();
    const verifier = new IapVerifier(CONFIG, client);
    await Promise.all([
      verifier.assertedEmail(carrying(assertion({}))),
      verifier.assertedEmail(carrying(assertion({}))),
    ]);
    expect(client.fetches).toBe(1);
  });

  test("are fetched again after a failed fetch", async () => {
    const client = new CountingKeyClient(
      { "cache-control": "max-age=3000" },
      new Set([1]),
    );
    const verifier = new IapVerifier(CONFIG, client);
    await expect(verifier.warm()).rejects.toThrow("gstatic unreachable");
    expect(await verifier.assertedEmail(carrying(assertion({})))).toBe(
      "curator@example.org",
    );
    expect(client.fetches).toBe(2);
  });

  test("warmed, serve the first request without a fetch", async () => {
    const client = new CountingKeyClient({ "cache-control": "max-age=3000" });
    const verifier = new IapVerifier(CONFIG, client);
    await verifier.warm();
    await verifier.assertedEmail(carrying(assertion({})));
    expect(client.fetches).toBe(1);
  });
});

describe("freshSeconds", () => {
  test.each([
    [{ "cache-control": "public, max-age=3000" }, 3000],
    [{ "cache-control": "public, max-age=3000", age: "132" }, 2868],
    [{ "cache-control": "max-age=60, must-revalidate", age: "90" }, 0],
    [{ "cache-control": "s-maxage=60" }, undefined],
    [{ "cache-control": "no-store" }, undefined],
    [{}, undefined],
    [{ "cache-control": "max-age=60", age: "soon" }, undefined],
  ])("reads %j as %p", (headers, expected) => {
    expect(freshSeconds(new Headers(headers))).toBe(expected);
  });
});

describe("loadIapConfig", () => {
  const env = {
    THEMIS_PROJECT_NUMBER: "123456789",
    THEMIS_REGION: "australia-southeast1",
    THEMIS_WEB_SERVICE_NAME: "themis-web",
  };

  test("reads the audience inputs", () => {
    expect(loadIapConfig(env)).toEqual(CONFIG);
  });

  test.each(Object.keys(env))("fails loud when %s is unset", (name) => {
    expect(() => loadIapConfig({ ...env, [name]: undefined })).toThrow(name);
  });
});
