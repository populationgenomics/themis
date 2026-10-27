import { IapVerifier } from "./adapters/live/identity";
import { getUserIdentity } from "./identity";
import { getPool, loadSqlConfig } from "./pg";

// What a live server's first request would otherwise wait on, started when the server starts: IAP's
// verification keys, and the Cloud SQL connector with the pool's first connection. Both are
// network round trips the server can overlap with loading its modules.

type EnvLike = Record<string, string | undefined>;

/** Fetch IAP's keys into the process's identity, and open the pool's first connection. Never
 *  rejects: a step that fails is logged and left to the first request that needs it, which retries
 *  it.
 *
 *  The process's identity is resolved before the first `await`, so a caller that does not await
 *  this has still built it by the time the call returns. */
export function warmLiveBackend(env: EnvLike): Promise<void> {
  const identity = step("IAP public keys", () => {
    const resolved = getUserIdentity(env);
    if (!(resolved instanceof IapVerifier)) {
      throw new Error("the live backend's identity is not the IAP verifier");
    }
    return resolved.warm();
  });
  const pool = step("Cloud SQL connection", async () => {
    const client = await (await getPool(loadSqlConfig(env))).connect();
    client.release();
  });
  return Promise.all([identity, pool]).then(() => undefined);
}

async function step(name: string, run: () => Promise<void>): Promise<void> {
  const started = performance.now();
  try {
    await run();
    console.log(
      `warm start: ${name} ready in ${Math.round(performance.now() - started)} ms`,
    );
  } catch (error) {
    console.error(
      `warm start: ${name} failed; the first request retries it`,
      error,
    );
  }
}
