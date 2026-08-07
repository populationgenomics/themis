// Typed errors the backend/BFF raise; the RPC error interceptor (server/rpc/interceptors.ts)
// maps each to its Connect code by type. A plain Error matches none and surfaces as
// `internal`, its message replaced so no internal detail reaches the client.

/** Thrown for an unknown analysis or document version — a caller reference that
 *  resolves to nothing. Not for invariant breaks: those stay plain Errors. */
export class ResourceNotFoundError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ResourceNotFoundError";
  }
}

/** Thrown when a request that must be authenticated carries no verifiable
 *  identity — a missing or invalid IAP assertion. A forged or absent token is never
 *  trusted. */
export class UnauthenticatedError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "UnauthenticatedError";
  }
}

/** Thrown when the caller's own request is malformed — a blank required field, an unspecified enum.
 *  Maps to Connect `InvalidArgument`, and unlike the masked internal errors its message (a field-level
 *  description, never internal state) reaches the caller, since the fault is theirs to fix. */
export class ClientInputError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ClientInputError";
  }
}
