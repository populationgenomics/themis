import { describe, expect, test } from "bun:test";
import type { ContentObject } from "@/server/ports";
import { createContent, type ObjectSigner } from "./content";

// LiveContent's GCS signing is exercised end-to-end against the deployed service (no fake verifies a
// V4 signature, and keyless signBlob has no local emulator). Offline tests drive `ContentPort.serve`
// through an injected signer and clock to check the 302 redirect, cache window, and signed-URL
// disposition overrides.
describe("LiveContent.serve", () => {
  const NOW = 1_000_000;

  function recordingContent() {
    const calls: Array<Parameters<ObjectSigner>> = [];
    const sign: ObjectSigner = async (bucket, object, opts) => {
      calls.push([bucket, object, opts]);
      return `https://storage.googleapis.com/${bucket}/${object}?sig=v4`;
    };
    return { content: createContent({ sign, now: () => NOW }), calls };
  }

  function object(mediaType: string, downloadName?: string): ContentObject {
    return {
      bucket: "example-corpus",
      object: "papers/x/obj",
      mediaType,
      downloadName,
    };
  }

  test("302s to the signed URL, cacheable only within — not up to — the signature lifetime", async () => {
    const { content, calls } = recordingContent();
    const res = await content.serve(object("application/pdf"));
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe(
      "https://storage.googleapis.com/example-corpus/papers/x/obj?sig=v4",
    );
    const [bucket, name, opts] = calls[0];
    expect(bucket).toBe("example-corpus");
    expect(name).toBe("papers/x/obj");
    expect(opts.responseType).toBe("application/pdf");
    // The cache window must be strictly inside the signature lifetime, or a redirect reused at the
    // edge of its window points at an already-expired signature (a 403 a hard refresh can't clear).
    const maxAge = Number(
      /max-age=(\d+)/.exec(res.headers.get("cache-control") ?? "")?.[1],
    );
    expect(maxAge * 1000).toBeLessThan(opts.expiresMs - NOW);
  });

  test("off-allowlist media is forced to an attachment download via the signed URL", async () => {
    const { content, calls } = recordingContent();
    await content.serve(object("image/svg+xml"));
    expect(calls[0][2].responseDisposition).toBe("attachment");
  });

  test("a download name is carried into the signed URL's disposition", async () => {
    const { content, calls } = recordingContent();
    await content.serve(object("text/csv", "supplement.csv"));
    // The object key is content-addressed; without this the browser saves the download as `obj`.
    expect(calls[0][2].responseDisposition).toBe(
      "attachment; filename*=UTF-8''supplement.csv",
    );
  });

  test("allowlisted media is served inline (no forced disposition)", async () => {
    const { content, calls } = recordingContent();
    await content.serve(object("image/png"));
    expect(calls[0][2].responseDisposition).toBeUndefined();
  });
});
