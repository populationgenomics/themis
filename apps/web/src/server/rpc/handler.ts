import { createValidateInterceptor } from "@connectrpc/validate";
import { Workbench } from "@/models/workbench";
import type { FetchRouter } from "./fetch-router";
import { createFetchRouter } from "./fetch-router";
import { errors, INTERNAL_MESSAGE, identity } from "./interceptors";
import { READ_MAX_BYTES } from "./limits";
import { workbenchService } from "./service";

// The BFF's whole RPC surface, composed once.
// See docs/design/proto.md (Serialization posture, bucket 2).

const router = createFetchRouter({
  // Connect only. The gRPC protocol needs HTTP/2 trailers Next cannot serve, and nothing
  // here speaks gRPC-Web; advertising either would fail obscurely rather than as 404.
  grpc: false,
  grpcWeb: false,
  // connect-es tolerates unknown JSON fields by default. A field the schema does not
  // declare is a caller's mistake — a misspelled `version` would otherwise silently
  // return the current document instead of the named one.
  jsonOptions: { ignoreUnknownFields: false },
  readMaxBytes: READ_MAX_BYTES,
  // Outermost-first. Validation sits outside `errors` so its rejection — the caller's own
  // input, violations and all — is the one failure the mask never sees.
  interceptors: [createValidateInterceptor(), errors(), identity()],
  routes: (route) => {
    route.service(Workbench, workbenchService);
  },
});

const MASKED = JSON.stringify({ code: "internal", message: INTERNAL_MESSAGE });

/** Mask a 500 raised outside the interceptor chain. Interceptors wrap the method call
 *  only; decoding the request, serializing the reply, and parsing a timeout happen in the
 *  protocol handler around it, and Connect answers those with the raw reason.
 *
 *  Connect answers `data_loss` with a 500 too, and passes it as raised. Only the chain mints that
 *  code: the protocol handler around it never does (Connect raises `data_loss` only decoding a
 *  binary header, which a Connect-protocol router does not), and `errors` masks every
 *  `ConnectError` a service this BFF called raises, `data_loss` included. */
export async function maskInternal(response: Response): Promise<Response> {
  if (response.status !== 500) {
    return response;
  }
  const body = await response.text();
  if (connectCode(body) === "data_loss") {
    return new Response(body, { status: 500, headers: response.headers });
  }
  if (!body.includes(INTERNAL_MESSAGE)) {
    console.error("unhandled rpc error outside the interceptor chain", body);
  }
  return new Response(MASKED, {
    status: 500,
    headers: { "content-type": "application/json" },
  });
}

/** Serve `inner` behind the boundary: the 500 mask, and a reply no cache may store. */
export function servingRpc(inner: FetchRouter): FetchRouter {
  return async (request, path) => {
    const response = await maskInternal(await inner(request, path));
    // Every reply is scoped to the caller the identity interceptor bound, and a shared cache
    // keyed on the URL alone would hand one curator's to another: the IAP cookie that
    // authenticates the request is invisible to RFC 9111's Authorization rule.
    response.headers.set("cache-control", "private, no-store");
    return response;
  };
}

export const serveRpc: FetchRouter = servingRpc(router);

/** The Connect error code a 500's body names, or `undefined` for a body that is not a Connect
 *  error, which the mask then replaces. */
function connectCode(body: string): unknown {
  try {
    const parsed: unknown = JSON.parse(body);
    return typeof parsed === "object" && parsed !== null
      ? (parsed as { code?: unknown }).code
      : undefined;
  } catch {
    return undefined;
  }
}
