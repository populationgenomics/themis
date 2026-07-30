import type { ConnectRouter, ConnectRouterOptions } from "@connectrpc/connect";
import { createConnectRouter } from "@connectrpc/connect";
import type { UniversalHandler } from "@connectrpc/connect/protocol";
import { createFetchHandler } from "@connectrpc/connect/protocol";

// Serves a Connect router from App Router route handlers.
// See docs/design/proto.md (Serialization posture, bucket 2).

/** Serves one request against the method at `path` — `/{package}.{Service}/{Method}`,
 *  which the caller extracts from its own routing. */
export type FetchRouter = (request: Request, path: string) => Promise<Response>;

export interface FetchRouterOptions extends ConnectRouterOptions {
  /** Registers the service implementations on the router. */
  routes: (router: ConnectRouter) => void;
}

/** Build a fetch handler over the registered routes. An unknown path answers 404, which
 *  a Connect client surfaces as `unimplemented`. */
export function createFetchRouter(options: FetchRouterOptions): FetchRouter {
  const router = createConnectRouter(options);
  options.routes(router);
  const handlers = new Map(
    router.handlers.map((handler: UniversalHandler) => [
      handler.requestPath,
      createFetchHandler(handler),
    ]),
  );
  return async (request, path) => {
    const handler = handlers.get(path);
    if (handler === undefined) {
      return new Response("not found", { status: 404 });
    }
    return handler(request);
  };
}
