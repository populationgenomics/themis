import type { Interceptor } from "@connectrpc/connect";
import { Code, ConnectError } from "@connectrpc/connect";
import { userContext } from "@/server/context";
import {
  isClientInputError,
  isResourceNotFoundError,
  isSessionBusyError,
  isUnauthenticatedError,
} from "@/server/errors";
import { setUserContext } from "./context";

// The two layers every RPC passes through, applied to the router rather than to each
// method.

/** Verify the request's caller and bind it to the call. */
export function identity(): Interceptor {
  return (next) => async (req) => {
    setUserContext(req.contextValues, await userContext(req.header));
    return next(req);
  };
}

/** The message every masked failure carries. Shared with the boundary in ./handler, which
 *  masks again for failures raised outside this chain. */
export const INTERNAL_MESSAGE = "internal server error";

/** Map a thrown error to its Connect code. Internal detail never reaches the client: an
 *  unrecognized failure is logged server-side and answered with a generic message, and a
 *  not-found never says which resource, so a caller cannot probe for existence.
 *
 *  Everything this wraps is the server's own state, validation excepted — it sits outside
 *  (./handler), so a rejection describing the caller's own input reaches them as raised. A
 *  `ConnectError` arriving from within is a service this BFF called, `InvalidArgument`
 *  included: a message the BFF itself built wrong is an internal fault, not the caller's. */
export function errors(): Interceptor {
  return (next) => async (req) => {
    try {
      return await next(req);
    } catch (error) {
      throw toConnectError(error);
    }
  };
}

function toConnectError(error: unknown): ConnectError {
  if (isResourceNotFoundError(error)) {
    return new ConnectError("resource not found", Code.NotFound);
  }
  if (isUnauthenticatedError(error)) {
    return new ConnectError("unauthenticated", Code.Unauthenticated);
  }
  if (isSessionBusyError(error)) {
    // A fixed phrase, not the thrown message: the upstream refusal names internal
    // event ids, and the composer shows this text to the curator.
    return new ConnectError(
      "the agent is still working on its current step",
      Code.FailedPrecondition,
    );
  }
  if (isClientInputError(error)) {
    // The caller's own malformed request. Its message names the offending field and is theirs to
    // read — this is not internal state, so it is not masked.
    return new ConnectError(error.message, Code.InvalidArgument);
  }
  console.error("unhandled rpc error", error);
  return new ConnectError(INTERNAL_MESSAGE, Code.Internal);
}
