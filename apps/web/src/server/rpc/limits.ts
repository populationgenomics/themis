// The size bounds on a request to the RPC surface, apart from the router so next.config.ts can read
// them without loading the server.

/** The largest request body the router reads, decompressed. Sized for the largest message a browser
 *  sends, a `PublishWorkspace` carrying a curator's pack base64-encoded in proto3-JSON: 16 MiB holds
 *  a pack of about 12 MiB. Under Cloud Run's 32 MiB HTTP/1 request limit, so the platform in front
 *  passes an oversized publish on for the router to answer `resource_exhausted`. */
export const READ_MAX_BYTES = 16 * 1024 * 1024;

/** The largest request Cloud Run passes to an HTTP/1 service, which the web service is: nothing
 *  longer reaches the app. */
export const CLOUD_RUN_HTTP1_REQUEST_MAX_BYTES = 32 * 1024 * 1024;

/** How much of a request body Next's proxy buffers: all of any body that can reach the app. The
 *  proxy runs on the RPC mount and truncates a longer body without failing the request, dropping
 *  whole chunks as they arrive, so a limit only a little above the router's can still hand it fewer
 *  bytes than it reads, and a chunked publish over the router's limit would then fail to parse
 *  rather than be answered `resource_exhausted`. */
export const PROXY_BODY_MAX_BYTES = CLOUD_RUN_HTTP1_REQUEST_MAX_BYTES;
