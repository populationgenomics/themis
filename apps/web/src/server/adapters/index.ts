import type { AnalysisBackend } from "../ports";
import { createFixtureBackend } from "./fixture";

export type Backend = "fixture" | "real";

// A narrow env shape so callers/tests need not supply a full ProcessEnv.
type EnvLike = Record<string, string | undefined>;

/** Which backend to build. `fixture` is the default (dev/demo/offline); `real` is
 *  opt-in via `THEMIS_BACKEND=real`. Any other value fails loud. */
export function selectedBackend(env: EnvLike = process.env): Backend {
  const raw = env.THEMIS_BACKEND;
  if (raw === "real") return "real";
  if (raw !== undefined && raw !== "" && raw !== "fixture") {
    throw new Error(
      `invalid THEMIS_BACKEND: ${JSON.stringify(raw)} (expected "fixture" | "real")`,
    );
  }
  return "fixture";
}

/** Build a FRESH backend. Each call news up an independent instance — the right
 *  thing for tests; route handlers use the memoized `getBackend` instead. `real`
 *  fails loud: its adapter is not wired. */
export function createBackend(env: EnvLike = process.env): AnalysisBackend {
  if (selectedBackend(env) === "real") {
    throw new Error(
      "THEMIS_BACKEND=real: the real backend adapter is not wired yet",
    );
  }
  return createFixtureBackend();
}

// ---------------------------------------------------------------------------
// Runtime composition root — the process-wide backend singleton. A stateful backend
// must persist across requests (a POST that creates an analysis and the polls that
// follow share one instance); the instance is cached on `globalThis` so Next's dev
// HMR (which re-evaluates modules) does not rebuild it between reloads.
// ---------------------------------------------------------------------------

interface Composition {
  backend?: AnalysisBackend;
}

function composition(): Composition {
  const holder = globalThis as typeof globalThis & {
    __themisComposition?: Composition;
  };
  if (!holder.__themisComposition) {
    holder.__themisComposition = {};
  }
  return holder.__themisComposition;
}

/** The process-wide backend for the runtime path (memoized across requests and
 *  HMR reloads). */
export function getBackend(env: EnvLike = process.env): AnalysisBackend {
  const c = composition();
  if (!c.backend) c.backend = createBackend(env);
  return c.backend;
}
