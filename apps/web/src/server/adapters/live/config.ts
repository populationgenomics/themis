// Env-driven configuration for the live (self-hosted data-plane) adapter. Every
// value is required and validated up front; a missing one is a fail-closed
// misconfiguration, never a silent default. The names mirror the env infra sets on
// the web Cloud Run service (infra/themis_infra/web.py) — keep them in lockstep.

type EnvLike = Record<string, string | undefined>;

/** Managed Agents control/data-plane inputs. The four `ANTHROPIC_*` federation
 *  ids drive WIF Path B; `agentId` / `environmentId` are the control-plane
 *  resources a session references. */
export interface AnthropicConfig {
  federationRuleId: string;
  organizationId: string;
  serviceAccountId: string;
  workspaceId: string;
  agentId: string;
  environmentId: string;
}

/** Cloud SQL connector inputs: the instance the connector dials, the database,
 *  and the IAM DB-user login to authenticate as (no password — the connector
 *  supplies the IAM credential). */
export interface SqlConfig {
  connectionName: string;
  database: string;
  iamUser: string;
}

/** The Cloud KMS MAC key version the bearer is derived through. Pinned to a
 *  single `.../cryptoKeyVersions/<n>`: a different version derives different
 *  bearers and would strand every live session. */
export interface KmsConfig {
  sessionTokenKeyVersion: string;
}

/** The bucket holding `<analysis_id>/versions/<n>` working-document snapshots. */
export interface GcsConfig {
  workingDocumentBucket: string;
}

/** IAP JWT audience inputs. The `aud` an IAP assertion carries is the backend
 *  service resource fronted by the load balancer, NOT the Cloud Run service. */
export interface IapConfig {
  projectNumber: string;
  backendServiceId: string;
}

function required(env: EnvLike, name: string): string {
  const value = env[name];
  if (value === undefined || value === "") {
    throw new Error(`${name} is not set — cannot build the live adapter`);
  }
  return value;
}

/** Read + validate the IAP audience inputs. Identity verification runs on every
 *  request (create/poll/document), independent of the Anthropic/KMS/GCS
 *  wiring a data-plane method touches. */
export function loadIapConfig(env: EnvLike = process.env): IapConfig {
  return {
    projectNumber: required(env, "THEMIS_PROJECT_NUMBER"),
    backendServiceId: required(env, "THEMIS_IAP_BACKEND_SERVICE_ID"),
  };
}

/** Read + validate the Cloud SQL connection inputs. Membership authorization
 *  queries SQL on every request, independent of the Anthropic/KMS/GCS wiring
 *  a data-plane method touches. */
export function loadSqlConfig(env: EnvLike = process.env): SqlConfig {
  return {
    connectionName: required(env, "THEMIS_SQL_CONNECTION_NAME"),
    database: required(env, "THEMIS_SQL_DATABASE"),
    iamUser: required(env, "THEMIS_SQL_IAM_USER"),
  };
}

/** Read + validate the Managed Agents inputs. Rejects a set `ANTHROPIC_API_KEY` /
 *  `ANTHROPIC_AUTH_TOKEN` — neither is read, and neither may be present: the SDK
 *  suppresses its env read for an explicit `profile` but not for `credentials`, so
 *  only the pinned `apiKey`/`authToken` nulls in `client.ts` keep a static
 *  credential from outranking WIF. This is the backstop for those. */
export function loadAnthropicConfig(
  env: EnvLike = process.env,
): AnthropicConfig {
  if (env.ANTHROPIC_API_KEY || env.ANTHROPIC_AUTH_TOKEN) {
    throw new Error(
      "ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN must be unset for WIF Path B " +
        "(a static credential outranks federation and silently wins)",
    );
  }
  return {
    federationRuleId: required(env, "ANTHROPIC_FEDERATION_RULE_ID"),
    organizationId: required(env, "ANTHROPIC_ORGANIZATION_ID"),
    serviceAccountId: required(env, "ANTHROPIC_SERVICE_ACCOUNT_ID"),
    workspaceId: required(env, "ANTHROPIC_WORKSPACE_ID"),
    agentId: required(env, "THEMIS_ANTHROPIC_AGENT_ID"),
    environmentId: required(env, "ANTHROPIC_ENVIRONMENT_ID"),
  };
}

/** Read + validate the KMS MAC key version the session bearer derives through. */
export function loadKmsConfig(env: EnvLike = process.env): KmsConfig {
  return {
    sessionTokenKeyVersion: required(env, "THEMIS_SESSION_TOKEN_KEY_VERSION"),
  };
}

/** Read + validate the working-document bucket. */
export function loadGcsConfig(env: EnvLike = process.env): GcsConfig {
  return {
    workingDocumentBucket: required(
      env,
      "THEMIS_STORE_WORKING_DOCUMENT_BUCKET",
    ),
  };
}
