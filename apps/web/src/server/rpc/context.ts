import type { ContextValues, HandlerContext } from "@connectrpc/connect";
import { Code, ConnectError, createContextKey } from "@connectrpc/connect";
import type { UserContext } from "@/server/context";

// The verified caller, carried per call in Connect's context values. `identity` (see
// ./interceptors) is the only writer and runs for every method on the router — the
// data-seam half of the request-auth chokepoint (docs/design/security.md).

// `null` default, not an anonymous context: an unresolved caller must fail the call, never
// silently downgrade it.
const userContextKey = createContextKey<UserContext | null>(null, {
  description: "themis.user-context",
});

/** Bind the verified caller to the call. */
export function setUserContext(values: ContextValues, user: UserContext): void {
  values.set(userContextKey, user);
}

/** The call's verified caller. Raises when the identity interceptor did not run: an
 *  unwired chokepoint is a bug in the composition, never a reason to serve the call. */
export function requireUserContext(ctx: HandlerContext): UserContext {
  const user = ctx.values.get(userContextKey);
  if (user === null) {
    throw new ConnectError(
      "user context missing: the identity interceptor did not run",
      Code.Internal,
    );
  }
  return user;
}
