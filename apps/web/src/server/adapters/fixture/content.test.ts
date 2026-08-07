import { describe, expect, test } from "bun:test";
import { ResourceNotFoundError } from "@/server/errors";
import { createContent, objectKey } from "./content";

const BYTES = new Uint8Array([1, 2, 3]);

function serve(mediaType: string, downloadName?: string): Promise<Response> {
  const store = new Map([[objectKey("b", "o"), BYTES]]);
  return createContent(store).serve({
    bucket: "b",
    object: "o",
    mediaType,
    downloadName,
  });
}

// The egress invariant, pinned as a property rather than today's allowlist: a media type off the
// inline set is forced to a download; one on it is not; every response is nosniff. A future addition
// to INLINE_MEDIA_TYPES must not need this edited; dropping the attachment fallback must fail it.
describe("FixtureContent egress headers", () => {
  test.each(["image/png", "image/jpeg", "application/pdf", "text/markdown"])(
    "%s (allowlisted) renders inline — no attachment",
    async (mediaType) => {
      const res = await serve(mediaType);
      expect(res.headers.get("content-disposition")).toBeNull();
      expect(res.headers.get("x-content-type-options")).toBe("nosniff");
    },
  );

  test.each(["image/svg+xml", "text/html", "application/octet-stream"])(
    "%s (off the allowlist) is forced to a download",
    async (mediaType) => {
      const res = await serve(mediaType);
      expect(res.headers.get("content-disposition")).toBe("attachment");
      expect(res.headers.get("x-content-type-options")).toBe("nosniff");
    },
  );

  test("a forced download names the saved file, escaping the name it carries", async () => {
    const res = await serve("application/octet-stream", 'sup "1".csv');
    expect(res.headers.get("content-disposition")).toBe(
      "attachment; filename*=UTF-8''sup%20%221%22.csv",
    );
  });

  test("a malformed media type throws (the Headers constructor rejects a CR/LF)", async () => {
    await expect(serve("text/plain\r\nx: y")).rejects.toThrow();
  });
});

describe("FixtureContent.serve", () => {
  // A cached stream would be drained after the first read — a blank figure on the second request, with
  // the suite still green. Each read must mint a fresh one-shot body over the stored bytes.
  test("hands out a fresh full body on each read of the same object", async () => {
    const port = createContent(new Map([[objectKey("b", "o"), BYTES]]));
    const ref = { bucket: "b", object: "o", mediaType: "image/png" };
    const first = new Uint8Array(await (await port.serve(ref)).arrayBuffer());
    const second = new Uint8Array(await (await port.serve(ref)).arrayBuffer());
    expect(first).toEqual(BYTES);
    expect(second).toEqual(BYTES);
  });

  test("an object absent from the store is a ResourceNotFoundError", async () => {
    const port = createContent(new Map());
    await expect(
      port.serve({ bucket: "b", object: "missing", mediaType: "image/png" }),
    ).rejects.toBeInstanceOf(ResourceNotFoundError);
  });
});
