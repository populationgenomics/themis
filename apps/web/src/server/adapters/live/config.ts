// Env-driven configuration for the live (self-hosted data-plane) adapter. Every
// value is required where it is read, and validated there; a missing one is a
// fail-closed misconfiguration, never a silent default. Each loader reads only what its
// consumer uses, so a variable fails the paths that need it and no other. The names are
// the env `WebService` sets on the web Cloud Run service (infra/themis_infra/web.py)
// — keep them in lockstep.

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

/** The Cloud KMS MAC key version the bearer is derived through. Pinned to a
 *  single `.../cryptoKeyVersions/<n>`: a different version derives different
 *  bearers and would strand every live session. */
export interface KmsConfig {
  sessionTokenKeyVersion: string;
}

/** The evidence gRPC service the BFF resolves papers through. `evidenceUrl` is both
 *  the transport base URL and the audience the ID-token interceptor mints for
 *  (Cloud Run IAM authenticates an ID token whose `aud` is the service URL). */
export interface EvidenceConfig {
  evidenceUrl: string;
  /** The single corpus bucket the BFF will read. The evidence service names the object; the BFF holds
   *  the authz boundary and refuses any object outside this bucket — so a service bug can't turn a
   *  paper-content route into a read of another bucket the web SA holds (e.g. per-tenant working docs). */
  corpusBucket: string;
}

/** The sheaf service the BFF relays the browser's workspace-repository calls to. `sheafUrl` is both
 *  the transport base URL and the audience the ID-token interceptor mints for. */
export interface SheafConfig {
  sheafUrl: string;
}

/** IAP JWT audience inputs. IAP is enabled on the Cloud Run service itself, so
 *  the `aud` its assertion carries names that service:
 *  `/projects/<projectNumber>/locations/<region>/services/<serviceName>`. */
export interface IapConfig {
  projectNumber: string;
  region: string;
  serviceName: string;
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
    region: required(env, "THEMIS_REGION"),
    serviceName: required(env, "THEMIS_WEB_SERVICE_NAME"),
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

/** Read + validate the evidence service URL. Content reveal + resolution run
 *  through it on the pane's requests, independent of the data-plane wiring. */
export function loadEvidenceConfig(env: EnvLike = process.env): EvidenceConfig {
  return {
    evidenceUrl: required(env, "THEMIS_EVIDENCE_URL"),
    corpusBucket: loadCorpusBucket(env),
  };
}

/** Read + validate the sheaf service URL. */
export function loadSheafConfig(env: EnvLike = process.env): SheafConfig {
  return { sheafUrl: required(env, "THEMIS_SHEAF_URL") };
}

/** The corpus bucket the paper-content routes redirect into, which the page's content security
 *  policy admits (lib/csp.ts). */
export function loadCorpusBucket(env: EnvLike = process.env): string {
  return required(env, "THEMIS_FULLTEXT_BUCKET");
}

/** Sheaf's bucket: where the sheaf service keeps every workspace repository, and so where the pack
 *  URLs it signs point. The BFF holds no role on it and names no object in it; only the content security
 *  policy reads it (lib/csp.ts). */
export function loadSheafBucket(env: EnvLike = process.env): string {
  return required(env, "THEMIS_SHEAF_BUCKET");
}
