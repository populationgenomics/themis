import { describe, expect, test } from "bun:test";
import type { ContentObject } from "@/server/ports";
import { type ObjectSigner, signedRedirect } from "./content";

// LiveContent's GCS signing is exercised end-to-end against the deployed service (no fake verifies a
// V4 signature, and keyless signBlob has no local emulator). What is offline-checkable is
// `signedRedirect`'s decisions, through an injected signer: the egress disposition and the cache
// window. The real signature stays deploy-verified.
describe("signedRedirect", () => {
  const NOW = 1_000_000;

  function recordingSigner(): {
    sign: ObjectSigner;
    calls: Array<Parameters<ObjectSigner>>;
  } {
    const calls: Array<Parameters<ObjectSigner>> = [];
    const sign: ObjectSigner = async (bucket, object, opts) => {
      calls.push([bucket, object, opts]);
      return `https://signed/${object}`;
    };
    return { sign, calls };
  }

  function object(mediaType: string, downloadName?: string): ContentObject {
    return {
      bucket: "corpus",
      object: "papers/x/obj",
      mediaType,
      downloadName,
    };
  }

  test("302s to the signed URL, cacheable only within — not up to — the signature lifetime", async () => {
    const { sign, calls } = recordingSigner();
    const res = await signedRedirect(object("application/pdf"), sign, NOW);
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe("https://signed/papers/x/obj");
    const [, name, opts] = calls[0];
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
    const { sign, calls } = recordingSigner();
    await signedRedirect(object("image/svg+xml"), sign, NOW);
    expect(calls[0][2].responseDisposition).toBe("attachment");
  });

  test("a download name is carried into the signed URL's disposition", async () => {
    const { sign, calls } = recordingSigner();
    await signedRedirect(object("text/csv", "supplement.csv"), sign, NOW);
    // The object key is content-addressed; without this the browser saves the download as `obj`.
    expect(calls[0][2].responseDisposition).toBe(
      "attachment; filename*=UTF-8''supplement.csv",
    );
  });

  test("allowlisted media is served inline (no forced disposition)", async () => {
    const { sign, calls } = recordingSigner();
    await signedRedirect(object("image/png"), sign, NOW);
    expect(calls[0][2].responseDisposition).toBeUndefined();
  });
});
