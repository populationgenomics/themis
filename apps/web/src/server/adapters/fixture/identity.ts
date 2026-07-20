import type { UserIdentity } from "../../identity";

/** The dev user every local request is attributed to (no IAP offline). */
export const DEV_USER_EMAIL = "user@localhost";

/** The offline identity: no IAP, so every request is the seed dev user. Ignores
 *  headers — only selected for `THEMIS_BACKEND=fixture`, never the real backend. */
export class DevUserIdentity implements UserIdentity {
  async assertedEmail(): Promise<string> {
    return DEV_USER_EMAIL;
  }
}
