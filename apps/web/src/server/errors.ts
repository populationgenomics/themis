// Typed errors the backend/BFF raise; the route boundary (app/api/_lib/http.ts) maps
// each to its HTTP status by type. A plain Error matches none and surfaces as 500.

/** Thrown for an unknown analysis or document version — a caller reference that
 *  resolves to nothing. Not for invariant breaks: those stay plain Errors (→ 500). */
export class ResourceNotFoundError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ResourceNotFoundError";
  }
}

/** Thrown when a request that must be authenticated carries no verifiable
 *  identity — a missing or invalid IAP assertion. The BFF boundary maps it to 401
 *  (app/api/_lib/http.ts); a forged or absent token is never trusted. */
export class UnauthenticatedError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "UnauthenticatedError";
  }
}
