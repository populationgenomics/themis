import "@typespec/protobuf";

using TypeSpec.Protobuf;

/**
 * The `auth` service: session-token resolution (docs/plans/self-hosted-sandbox.md §7). The store
 * forwards a sandbox's per-session token; the auth service — the sole reader of the token store —
 * resolves it to the Project + Analysis it grants access to. `regen` compiles this to the committed
 * proto (the contract) and the generated themis/rpc/auth stubs each consumer imports — the server
 * subclasses the servicer base, a caller uses the stub.
 */
@package({ name: "themis.rpc.auth" })
namespace Auth;

/**
 * The token forwarded to be resolved: a high-entropy per-session secret the auth service hashes
 * before lookup.
 */
model ResolveTokenRequest {
  @field(1) session_token: string;
}

/**
 * The binding a resolved token grants — the Themis Project and the Analysis whose workspace the
 * caller may read and write. Project first (a Project nests Analyses); `project_id` is the Themis
 * Project id (reserve `gcp_project_id` for a Google Cloud project id if one is ever needed).
 */
model SessionContext {
  @field(1) project_id: string;
  @field(2) analysis_id: string;
}

/** The auth service: resolve a session token to the Project + Analysis binding it grants. */
@TypeSpec.Protobuf.service
interface Auth {
  /**
   * Resolve a session token to its binding. An invalid, revoked, or expired token is a
   * PERMISSION_DENIED the server adds — a transport-level rejection, not a modelled body this slice.
   */
  resolveSession(...ResolveTokenRequest): SessionContext;
}
