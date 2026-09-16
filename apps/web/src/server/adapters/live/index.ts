import { loadSqlConfig } from "../../pg";
import type { AnalysisDataPlane, ProjectMembership } from "../../ports";
import { AnthropicClient } from "./client";
import { loadAnthropicConfig, loadGcsConfig, loadKmsConfig } from "./config";
import { DataPlane } from "./data-plane";
import { KmsSessionTokenDeriver } from "./derive";
import { Gcs } from "./gcs";
import { Membership } from "./membership";
import { Sql } from "./sql";

// The `THEMIS_BACKEND=live` composition: the raw `AnalysisDataPlane` over the
// self-hosted data plane — Anthropic session control, KMS-derived bearer, Cloud SQL
// persistence, GCS-direct working documents. Authorization is the AuthorizedBackend
// decorator's job; this layer trusts the (user, project) its caller resolved.

// One SQL pool + Cloud SQL connector, shared by the backend and membership (both
// query the same instance). Memoized on `globalThis` so Next's dev HMR does not leak
// a fresh pool per reload; `context.ts` builds the backend and membership from
// separate factories, so without this each would open its own.
function sharedSql(): Sql {
  const holder = globalThis as typeof globalThis & {
    __themisLiveSql?: Sql;
  };
  if (!holder.__themisLiveSql)
    holder.__themisLiveSql = new Sql(loadSqlConfig());
  return holder.__themisLiveSql;
}

export function createDataPlane(): AnalysisDataPlane {
  return new DataPlane(
    new AnthropicClient(loadAnthropicConfig()),
    new KmsSessionTokenDeriver(loadKmsConfig()),
    sharedSql(),
    new Gcs(loadGcsConfig()),
  );
}

export function createMembership(): ProjectMembership {
  return new Membership(sharedSql());
}

export { createContent } from "./content";
export { createIdentity } from "./identity";
export { createLiterature } from "./literature";
