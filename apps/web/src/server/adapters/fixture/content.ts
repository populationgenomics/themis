import { contentDisposition } from "@/server/content-egress";
import { ResourceNotFoundError } from "../../errors";
import type { ContentObject, ContentPort } from "../../ports";

// The offline content port: the fixture has no bucket to sign against, so it holds the corpus bytes in
// an in-memory store keyed like GCS (`bucket/object`) and streams them with the egress headers. This
// is the sole surviving byte-stream serving path — the live port 302s to GCS and never streams. It
// plays the role GCS plays for the live port, so the resolve→serve split is the same offline and live.

/** The seeded object store: `bucket/object` → bytes, the fixture's stand-in for GCS. */
export type ContentStore = ReadonlyMap<string, Uint8Array>;

export function objectKey(bucket: string, object: string): string {
  return `${bucket}/${object}`;
}

class FixtureContent implements ContentPort {
  constructor(private readonly store: ContentStore) {}

  async serve(object: ContentObject): Promise<Response> {
    const bytes = this.store.get(objectKey(object.bucket, object.object));
    if (bytes === undefined) {
      throw new ResourceNotFoundError(
        `no seeded object gs://${object.bucket}/${object.object}`,
      );
    }
    const headers: Record<string, string> = {
      "content-type": object.mediaType,
      "x-content-type-options": "nosniff",
    };
    const disposition = contentDisposition(
      object.mediaType,
      object.downloadName,
    );
    if (disposition) headers["content-disposition"] = disposition;
    // A fresh one-shot stream per call: the store holds the bytes, so a second read of the same object
    // still gets a full body (a cached stream would be drained after the first).
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes);
        controller.close();
      },
    });
    try {
      return new Response(body, { headers });
    } catch (error) {
      // A malformed mediaType (a CR/LF) makes the Headers constructor throw; cancel the source stream
      // so an already-opened read doesn't leak. cancel() on an errored stream rejects with the stored
      // error — swallow it so `throw error` below is what reaches the route boundary.
      void body.cancel().catch(() => {});
      throw error;
    }
  }
}

export function createContent(store: ContentStore): ContentPort {
  return new FixtureContent(store);
}
