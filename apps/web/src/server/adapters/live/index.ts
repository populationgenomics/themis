import { loadSqlConfig } from "../../pg";
import type { AccessPort, AnalysisPorts } from "../../ports";
import { createAccess as buildAccess } from "./access";
import { AnthropicClient } from "./client";
import {
  loadAnthropicConfig,
  loadAuthServiceConfig,
  loadKmsConfig,
  loadSheafConfig,
} from "./config";
import { DataPlane } from "./data-plane";
import { KmsSessionTokenDeriver, MemoizedBearerDeriver } from "./derive";
import { Sql } from "./sql";
import { createWorkspace } from "./workspace";

// The `THEMIS_BACKEND=live` composition: the raw `AnalysisDataPlane` over the
// self-hosted data plane — Anthropic session control, KMS-derived bearer, Cloud SQL
// persistence — and the workspace repository over the sheaf
// service, which names each Analysis by a bearer the same deriver derives. Authorization is the AuthorizedBackend
// decorator's job; this layer trusts the (user, project) its caller resolved.

// One SQL pool + Cloud SQL connector. Memoized on `globalThis` so Next's dev HMR does not leak
// a fresh pool per reload.
function sharedSql(): Sql {
  const holder = globalThis as typeof globalThis & {
    __themisLiveSql?: Sql;
  };
  if (!holder.__themisLiveSql)
    holder.__themisLiveSql = new Sql(loadSqlConfig());
  return holder.__themisLiveSql;
}

export function createAnalysisPorts(): AnalysisPorts {
  const deriver = new MemoizedBearerDeriver(
    new KmsSessionTokenDeriver(loadKmsConfig()),
  );
  return {
    dataPlane: new DataPlane(
      new AnthropicClient(loadAnthropicConfig()),
      deriver,
      sharedSql(),
    ),
    workspace: createWorkspace(loadSheafConfig(), deriver),
    documents: null,
  };
}

/** The auth service's access answers (`THEMIS_AUTH_URL`). */
export function createAccess(
  env: Record<string, string | undefined> = process.env,
): AccessPort {
  return buildAccess(loadAuthServiceConfig(env));
}

export { createContent } from "./content";
export { createIdentity } from "./identity";
export { createLiterature } from "./literature";
