import type { Interceptor } from "@connectrpc/connect";
import { GoogleAuth, type IdTokenClient } from "google-auth-library";

/** Add `Authorization: Bearer <id-token>` minted for `audience` (the Cloud Run service URL), the web
 *  tier's own identity on every call to a backend service. `getRequestHeaders` returns the token
 *  through google-auth's own cache — refetched from the metadata server only near expiry, not on
 *  every RPC. */
export function idTokenInterceptor(audience: string): Interceptor {
  const auth = new GoogleAuth();
  let client: Promise<IdTokenClient> | undefined;
  return (next) => async (request) => {
    if (client === undefined) {
      client = auth.getIdTokenClient(audience).catch((error: unknown) => {
        // A rejected promise must not stay cached, or a transient cold-start failure poisons every
        // later RPC for the process's life; drop it so the next request retries the token mint.
        client = undefined;
        throw error;
      });
    }
    const headers = await (await client).getRequestHeaders();
    const authorization = headers.get("authorization");
    if (authorization === null) {
      throw new Error(`no ID token minted for audience ${audience}`);
    }
    request.header.set("authorization", authorization);
    return next(request);
  };
}
