import { Storage } from "@google-cloud/storage";
import { contentDisposition } from "@/server/content-egress";
import type { ContentObject, ContentPort } from "../../ports";

// The live content port: serve a stored GCS object by signing a short-lived V4 read URL and answering
// a 302, so the bytes flow browser↔GCS and never pass through the BFF. Generic over the object — it
// signs whatever bucket/object it is handed (bounded by the web SA's IAM), so a second content surface
// (per-tenant working documents) reuses it; the trust boundary on *which* bucket a resolution may name
// is the resolving adapter's (`literature.ts` pins the corpus bucket).

/** A signed read URL for one GCS object, valid until `expiresMs`. `responseType`/`responseDisposition`
 *  override what GCS serves the object as, carrying the egress typing onto the direct download. */
export type ObjectSigner = (
  bucket: string,
  object: string,
  opts: {
    expiresMs: number;
    responseType: string;
    responseDisposition?: string;
  },
) => Promise<string>;

/** The signed-URL lifetime — the bearer-capability window (a leaked URL is live this long), chosen
 *  for that, not freshness (the corpus is immutable). Minting is one IAM signBlob RPC, and the cached
 *  302 re-mints at most once per window per browser, so the window can be short. */
const CONTENT_URL_TTL_SECONDS = 15 * 60;

/** The 302's `max-age` — strictly less than the signature lifetime. The browser's cache clock starts
 *  at response receipt (mint + latency), not at mint, and can skew further, so a redirect cached at
 *  the edge of its window must still point at a live signature; the margin absorbs that gap. */
const CONTENT_URL_CACHE_SECONDS = CONTENT_URL_TTL_SECONDS - 60;

/** Sign a read URL for `object` and build the `302` to it, forcing off-allowlist media to an
 *  attachment download through the signed URL's response override. Pure over an injected `sign` +
 *  `nowMs`, so the egress and cache-window decisions are testable without GCS. */
export async function signedRedirect(
  object: ContentObject,
  sign: ObjectSigner,
  nowMs: number,
): Promise<Response> {
  const url = await sign(object.bucket, object.object, {
    expiresMs: nowMs + CONTENT_URL_TTL_SECONDS * 1000,
    responseType: object.mediaType,
    responseDisposition: contentDisposition(
      object.mediaType,
      object.downloadName,
    ),
  });
  return new Response(null, {
    status: 302,
    headers: {
      location: url,
      "cache-control": `private, max-age=${CONTENT_URL_CACHE_SECONDS}`,
    },
  });
}

class LiveContent implements ContentPort {
  private storage?: Storage;

  serve(object: ContentObject): Promise<Response> {
    return signedRedirect(
      object,
      (bucket, name, opts) => this.gcsSign(bucket, name, opts),
      Date.now(),
    );
  }

  /** A keyless V4 read URL via the Storage client (the web SA's `signBlob`, no stored key). Not
   *  exercised offline — the fake supports no signing — so it stays a thin wrapper over `getSignedUrl`;
   *  `signedRedirect`'s decisions are what the tests cover, through an injected signer. */
  private async gcsSign(
    bucket: string,
    object: string,
    opts: {
      expiresMs: number;
      responseType: string;
      responseDisposition?: string;
    },
  ): Promise<string> {
    const [url] = await this.storageClient()
      .bucket(bucket)
      .file(object)
      .getSignedUrl({
        version: "v4",
        action: "read",
        expires: opts.expiresMs,
        responseType: opts.responseType,
        ...(opts.responseDisposition
          ? { responseDisposition: opts.responseDisposition }
          : {}),
      });
    return url;
  }

  private storageClient(): Storage {
    if (!this.storage) {
      this.storage = new Storage();
    }
    return this.storage;
  }
}

export function createContent(): ContentPort {
  return new LiveContent();
}
