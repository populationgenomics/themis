import "@typespec/protobuf";

using TypeSpec.Protobuf;

/**
 * The `store` service: persist and restore a sandbox's /workspace across respawns
 * (docs/plans/self-hosted-sandbox.md §9), authorized per session (the bearer resolves via the
 * auth service, carried as x-themis-session-token metadata). `regen` compiles this to the committed
 * proto and the generated themis/rpc/store stubs each consumer imports — the server subclasses the
 * servicer base, a caller uses the stub.
 *
 * /workspace splits into two co-equal artifacts, both restored on spawn and checkpointed on idle:
 * the working document (the durable deliverable, versioned) and the ephemeral workspace (the rest
 * of the filesystem, an opaque tar archive streamed as bytes chunks).
 */
@package({ name: "themis.rpc.store" })
namespace Store;

/**
 * The working document (GLOSSARY): the Analysis's evolving artifact, a single markdown file this
 * slice. Each put appends an immutable version (server-assigned); get returns the latest.
 */
model PutWorkingDocumentRequest {
  @field(1) markdown: string;
}

/** Carries the version the store appended. */
model PutWorkingDocumentResponse {
  @field(1) version: int32;
}

/** The latest version's whole body, returned on restore. */
model WorkingDocumentSnapshot {
  @field(1) version: int32;
  @field(2) markdown: string;
}

/**
 * One chunk of the ephemeral /workspace tar archive — the streaming unit for both the checkpoint
 * (client-stream in) and the restore (server-stream out).
 */
model WorkspaceChunk {
  @field(1) content: bytes;
}

/** putWorkspace's reply — empty; the checkpoint's outcome is the RPC status. */
model PutWorkspaceResponse {}

/** The store service: the versioned working document and the ephemeral /workspace archive. */
@TypeSpec.Protobuf.service
interface Store {
  /**
   * Append a new immutable version of the working document. Each call mints a new server-assigned
   * version, so the append is not idempotent and must not be auto-retried by a caller.
   */
  putWorkingDocument(...PutWorkingDocumentRequest): PutWorkingDocumentResponse;

  /**
   * The latest working-document version; a NOT_FOUND on the genuine first spawn is the server's,
   * not modelled this slice.
   */
  getWorkingDocument(): WorkingDocumentSnapshot;

  /**
   * Checkpoint the ephemeral /workspace remainder — an opaque tar archive, streamed in and
   * overwrite-on-put.
   */
  @stream(StreamMode.In)
  putWorkspace(...WorkspaceChunk): PutWorkspaceResponse;

  /**
   * Restore the ephemeral /workspace archive, streamed out; a NOT_FOUND when none yet is the
   * server's.
   */
  @stream(StreamMode.Out)
  getWorkspace(): WorkspaceChunk;
}
