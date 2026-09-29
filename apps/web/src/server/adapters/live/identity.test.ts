import { describe, expect, test } from "bun:test";
import * as crypto from "node:crypto";
import { OAuth2Client } from "google-auth-library";
import { UnauthenticatedError } from "../../errors";
import { loadIapConfig } from "./config";
import { IapVerifier } from "./identity";

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
