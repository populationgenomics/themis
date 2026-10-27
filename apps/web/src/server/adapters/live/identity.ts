import { OAuth2Client } from "google-auth-library";
import { UnauthenticatedError } from "../../errors";
import type { UserIdentity } from "../../identity";
import { type IapConfig, loadIapConfig } from "./config";

// App-layer IAP verification. The plaintext `x-goog-authenticated-user-email` (and
// `x-serverless-authorization`) headers are forgeable by anything that reaches the
// container and MUST NOT be trusted. The only unforgeable identity is the signed IAP
// assertion — a per-request JWT IAP mints. We verify its signature, issuer, and
// audience against IAP's published keys and read the user email (and sub) from the
// VERIFIED claims. Fail CLOSED: a request that cannot be verified is rejected, never
// treated as anonymous.

const IAP_ASSERTION_HEADER = "x-goog-iap-jwt-assertion";
const IAP_ISSUER = "https://cloud.google.com/iap";
// Every request waits on a key fetch in flight, so one that stalls must not hold them for long.
const KEY_FETCH_TIMEOUT_MS = 5_000;

/** Verifies the signed IAP assertion and returns the email from its verified
 *  claims. Every failure mode — absent, bad signature, wrong issuer/audience,
 *  expired, or no email claim — throws `UnauthenticatedError` (→ 401). */
export class IapVerifier implements UserIdentity {
  // IAP is enabled on the Cloud Run service, so the audience names the service:
  // `/projects/<n>/locations/<region>/services/<name>`.
  private readonly audience: string;
  private keys?: { pubkeys: PublicKeys; freshUntil: number };
  private keysInFlight?: Promise<PublicKeys>;

  /** `client` fetches IAP's published keys and checks the signature; a test
   *  passes one whose key source it controls. */
  constructor(
    config: IapConfig,
    private readonly client: OAuth2Client = new OAuth2Client({
      transporterOptions: { timeout: KEY_FETCH_TIMEOUT_MS },
    }),
  ) {
    this.audience = `/projects/${config.projectNumber}/locations/${config.region}/services/${config.serviceName}`;
  }

  async assertedEmail(headers: Headers): Promise<string> {
    const assertion = headers.get(IAP_ASSERTION_HEADER);
    if (!assertion) {
      throw new UnauthenticatedError(`missing ${IAP_ASSERTION_HEADER}`);
    }
    // Outside the try: a key-fetch failure is an outage, not an unverifiable
    // caller, and propagates as a 500 rather than a misleading 401.
    const pubkeys = await this.publicKeys();
    let email: unknown;
    try {
      const ticket = await this.client.verifySignedJwtWithCertsAsync(
        assertion,
        pubkeys,
        this.audience,
        [IAP_ISSUER],
      );
      email = ticket.getPayload()?.email;
    } catch (error) {
      throw new UnauthenticatedError("IAP assertion failed verification", {
        cause: error,
      });
    }
    if (typeof email !== "string" || email === "") {
      throw new UnauthenticatedError(
        "verified IAP assertion carries no email claim",
      );
    }
    return email;
  }

  /** Fetch IAP's published keys ahead of the first request that needs them. */
  async warm(): Promise<void> {
    await this.publicKeys();
  }

  /** IAP's published keys, reused for as long as the response's own headers keep them fresh; the
   *  client fetches them anew on every call. Concurrent callers share one fetch. */
  private publicKeys(): Promise<PublicKeys> {
    if (this.keys && Date.now() < this.keys.freshUntil) {
      return Promise.resolve(this.keys.pubkeys);
    }
    this.keysInFlight ??= this.fetchPublicKeys().finally(() => {
      this.keysInFlight = undefined;
    });
    return this.keysInFlight;
  }

  private async fetchPublicKeys(): Promise<PublicKeys> {
    const fetchedAt = Date.now();
    const { pubkeys, res } = await this.client.getIapPublicKeys();
    if (!isKeySet(pubkeys)) {
      throw new Error(
        "IAP's published keys are not a non-empty map of PEM strings",
      );
    }
    const lifetime = freshSeconds(res?.headers);
    this.keys =
      lifetime === undefined
        ? undefined
        : { pubkeys, freshUntil: fetchedAt + lifetime * 1000 };
    return pubkeys;
  }
}

/** How long a response may be reused, by its headers: `max-age` less the `Age` a cache in front of
 *  the origin already spent. `undefined`, so not reused, when `Cache-Control` states no `max-age` or
 *  `Age` is malformed. */
export function freshSeconds(headers: Headers | undefined): number | undefined {
  const maxAge = headers
    ?.get("cache-control")
    ?.match(/(?:^|,)\s*max-age=(\d+)\s*(?:,|$)/)?.[1];
  if (maxAge === undefined) return undefined;
  const age = headers?.get("age") ?? "0";
  if (!/^\d+$/.test(age)) return undefined;
  return Math.max(0, Number(maxAge) - Number(age));
}

/** IAP's published keys by key id, as `getIapPublicKeys` returns them (the library does not export
 *  the type). */
type PublicKeys = Record<string, string>;

function isKeySet(value: unknown): value is PublicKeys {
  return (
    typeof value === "object" &&
    value !== null &&
    Object.keys(value).length > 0 &&
    Object.values(value).every((key) => typeof key === "string")
  );
}

/** Build the IAP verifier from env (`THEMIS_PROJECT_NUMBER`, `THEMIS_REGION`,
 *  `THEMIS_WEB_SERVICE_NAME`: the audience inputs). Fails
 *  loud on a missing input — a fail-closed misconfiguration, not a reason to skip
 *  verification. */
export function createIdentity(
  env: Record<string, string | undefined> = process.env,
): IapVerifier {
  return new IapVerifier(loadIapConfig(env));
}
