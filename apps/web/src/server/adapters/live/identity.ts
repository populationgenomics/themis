import { OAuth2Client } from "google-auth-library";
import { UnauthenticatedError } from "../../errors";
import type { UserIdentity } from "../../identity";
import { type IapConfig, loadIapConfig } from "./config";

// App-layer IAP verification. The Cloud Run invoker is not restricted
// to IAP, so the plaintext `x-goog-authenticated-user-email` (and
// `x-serverless-authorization`) headers are forgeable and MUST NOT be trusted. The
// only unforgeable identity is the signed IAP assertion — a per-request JWT IAP
// mints. We verify its signature, issuer, and audience against IAP's published
// keys and read the user email (and sub) from the VERIFIED claims. Fail CLOSED: a
// request that cannot be verified is rejected, never treated as anonymous.

const IAP_ASSERTION_HEADER = "x-goog-iap-jwt-assertion";
const IAP_ISSUER = "https://cloud.google.com/iap";

/** Verifies the signed IAP assertion and returns the email from its verified
 *  claims. Every failure mode — absent, bad signature, wrong issuer/audience,
 *  expired, or no email claim — throws `UnauthenticatedError` (→ 401). */
export class IapVerifier implements UserIdentity {
  private readonly client = new OAuth2Client();
  // The audience is the backend SERVICE resource IAP fronts on the load balancer,
  // not the Cloud Run service id: `/projects/<n>/global/backendServices/<id>`.
  private readonly audience: string;

  constructor(config: IapConfig) {
    this.audience = `/projects/${config.projectNumber}/global/backendServices/${config.backendServiceId}`;
  }

  async assertedEmail(headers: Headers): Promise<string> {
    const assertion = headers.get(IAP_ASSERTION_HEADER);
    if (!assertion) {
      throw new UnauthenticatedError(`missing ${IAP_ASSERTION_HEADER}`);
    }
    // Outside the try: a key-fetch failure is an outage, not an unverifiable
    // caller, and propagates as a 500 rather than a misleading 401.
    const { pubkeys } = await this.client.getIapPublicKeys();
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
}

/** Build the IAP verifier from env (the `THEMIS_IAP_*` audience inputs). Fails
 *  loud on a missing input — a fail-closed misconfiguration, not a reason to skip
 *  verification. */
export function createIdentity(
  env: Record<string, string | undefined> = process.env,
): IapVerifier {
  return new IapVerifier(loadIapConfig(env));
}
