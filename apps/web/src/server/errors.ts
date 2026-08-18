// Typed errors the backend/BFF raise; the RPC error interceptor (server/rpc/interceptors.ts)
// maps each to its Connect code. A plain Error matches none and surfaces as `internal`,
// its message replaced so no internal detail reaches the client.
//
// Catch sites discriminate through the guards below — `name`, never `instanceof`: the
// backend is memoized on `globalThis` (server/context.ts) and its instances cross Next's
// page/route module graphs, so an error thrown in one graph reaches a catch holding the
// other graph's class object, and `instanceof` is false there. The constructors pin
// `name` as the graph-independent discriminant.

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

/** Thrown when the run's session refuses a curator turn because the agent is mid-step —
 *  a tool call in flight, so the session accepts only tool-result-shaped events. A
 *  recurring runtime state the curator resolves (wait, or interrupt), not a broken
 *  invariant: maps to Connect `FailedPrecondition`, never a masked internal error. */
export class SessionBusyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SessionBusyError";
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

/** Thrown when an analysis row's `inputs` payload cannot be decoded. Carries the row's Project so
 *  the point-access check can still authorize against it: without that, an undecodable row would
 *  answer a non-member differently from an unknown id and become an existence oracle. Not a
 *  `ResourceNotFoundError` — for a member the row is a genuine fault and must surface as one. */
export class UndecodableAnalysisError extends Error {
  readonly projectId: string;

  constructor(
    analysisId: string,
    projectId: string,
    options?: { cause?: unknown },
  ) {
    super(`analysis inputs do not decode: ${analysisId}`, options);
    this.name = "UndecodableAnalysisError";
    this.projectId = projectId;
  }
}

function named(error: unknown, name: string): error is Error {
  return error instanceof Error && error.name === name;
}

export function isResourceNotFoundError(
  error: unknown,
): error is ResourceNotFoundError {
  return named(error, "ResourceNotFoundError");
}

export function isUnauthenticatedError(
  error: unknown,
): error is UnauthenticatedError {
  return named(error, "UnauthenticatedError");
}

export function isSessionBusyError(error: unknown): error is SessionBusyError {
  return named(error, "SessionBusyError");
}

export function isClientInputError(error: unknown): error is ClientInputError {
  return named(error, "ClientInputError");
}

export function isUndecodableAnalysisError(
  error: unknown,
): error is UndecodableAnalysisError {
  return named(error, "UndecodableAnalysisError");
}
