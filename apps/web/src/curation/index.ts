import { selectedBackend } from "@/server/backend";
import { loadSqlConfig } from "@/server/pg";
import { FixtureCurationStore } from "./fixture";
import { FixtureVariantResolver } from "./fixture-resolver";
import { RegistryVariantResolver } from "./registry-resolver";
import type { VariantResolver } from "./resolver";
import { SqlCurationStore } from "./sql";
import type { CurationStore } from "./store";

// The module's composition root: which store and which allele resolver, memoized process-wide.
//
// The fixture store holds its state in memory, so a POST that saves a draft and the GET that
// follows must reach the same instance — memoizing here is what makes the offline surface behave
// like the deployed one rather than resetting between requests.

type EnvLike = Record<string, string | undefined>;

interface StoreSingletons {
  store?: CurationStore;
  resolver?: VariantResolver;
}

function singletons(): StoreSingletons {
  const holder = globalThis as typeof globalThis & {
    __themisCuration?: StoreSingletons;
  };
  if (!holder.__themisCuration) holder.__themisCuration = {};
  return holder.__themisCuration;
}

/** A FRESH store for the selected backend. Tests use this; routes use `curationStore`. */
export function buildCurationStore(env: EnvLike = process.env): CurationStore {
  return selectedBackend(env) === "live"
    ? new SqlCurationStore(loadSqlConfig(env))
    : new FixtureCurationStore();
}

/** The process-wide store (memoized across requests and HMR reloads). */
export function curationStore(env: EnvLike = process.env): CurationStore {
  const s = singletons();
  if (!s.store) s.store = buildCurationStore(env);
  return s.store;
}

/** A FRESH allele resolver for the selected backend. Tests use this; routes use `variantResolver`. */
export function buildVariantResolver(
  env: EnvLike = process.env,
): VariantResolver {
  return selectedBackend(env) === "live"
    ? new RegistryVariantResolver()
    : new FixtureVariantResolver();
}

/** The process-wide allele resolver. Memoized for the same reason the store is, though it holds no
 *  state: one place decides which backend the module is running against. */
export function variantResolver(env: EnvLike = process.env): VariantResolver {
  const s = singletons();
  if (!s.resolver) s.resolver = buildVariantResolver(env);
  return s.resolver;
}
