import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import {
  Code,
  ConnectError,
  createClient,
  createRouterTransport,
} from "@connectrpc/connect";
import {
  ContentLocationSchema,
  Literature,
  type ResolveContentRequest,
} from "@/models/literature";
import { ResourceNotFoundError } from "@/server/errors";
import type { ContentObject, ContentPort } from "@/server/ports";
import { loadEvidenceConfig } from "./config";
import { LiveLiterature } from "./literature";

// `LiveLiterature.serveContent` driven over an in-memory Connect router: selector encoding,
// `NotFound` mapping, and the confused-deputy pin keeping resolved `gs://` URIs inside the corpus
// bucket before `ContentPort.serve` signs anything.
describe("LiveLiterature.serveContent", () => {
  const CORPUS = "example-corpus";

  function unreachedContent(): ContentPort {
    return {
      async serve() {
        throw new Error("content port was reached");
      },
    };
  }

  function recordingContent() {
    const served: ContentObject[] = [];
    const content: ContentPort = {
      async serve(object) {
        served.push(object);
        return new Response(null, {
          status: 302,
          headers: {
            location: `https://storage.googleapis.com/${object.bucket}/${object.object}?sig=v4`,
          },
        });
      },
    };
    return { content, served };
  }

  function adapter(
    resolveContent: (req: ResolveContentRequest) => {
      gcsUri: string;
      mediaType: string;
    },
    content: ContentPort,
  ): LiveLiterature {
    const transport = createRouterTransport(({ service }) => {
      service(Literature, {
        resolveContent: (req) =>
          create(ContentLocationSchema, resolveContent(req)),
      });
    });
    return new LiveLiterature(
      createClient(Literature, transport),
      content,
      CORPUS,
    );
  }

  test("resolves the gs:// location over RPC and hands the corpus object to ContentPort", async () => {
    const { content, served } = recordingContent();
    let seenSelectorCase: string | undefined;
    const literature = adapter((req) => {
      seenSelectorCase = req.selector.case;
      return {
        gcsUri: `gs://${CORPUS}/papers/x/paper.pdf`,
        mediaType: "application/pdf",
      };
    }, content);

    const res = await literature.serveContent("doc-1", { kind: "pdf" });

    expect(seenSelectorCase).toBe("pdf");
    expect(res.status).toBe(302);
    expect(served).toEqual([
      {
        bucket: CORPUS,
        object: "papers/x/paper.pdf",
        mediaType: "application/pdf",
        downloadName: undefined,
      },
    ]);
  });

  test("encodes a file selector and carries its download name to ContentPort", async () => {
    const { content, served } = recordingContent();
    let requestedName: string | undefined;
    const literature = adapter((req) => {
      if (req.selector.case === "file") {
        requestedName = req.selector.value.name;
      }
      return { gcsUri: `gs://${CORPUS}/f/deadbeef`, mediaType: "text/csv" };
    }, content);

    await literature.serveContent("doc-1", { kind: "file", name: "supp.csv" });

    expect(requestedName).toBe("supp.csv");
    expect(served).toEqual([
      {
        bucket: CORPUS,
        object: "f/deadbeef",
        mediaType: "text/csv",
        downloadName: "supp.csv",
      },
    ]);
  });

  test("refuses an object outside the corpus bucket before reaching ContentPort", async () => {
    const literature = adapter(
      () => ({
        gcsUri: "gs://tenant-working-docs/secret",
        mediaType: "text/plain",
      }),
      unreachedContent(),
    );

    await expect(
      literature.serveContent("doc-1", { kind: "markdown" }),
    ).rejects.toThrow("outside the corpus bucket");
  });

  test("rejects a non-gs:// content URI before reaching ContentPort", async () => {
    const literature = adapter(
      () => ({ gcsUri: "https://evil.example/x", mediaType: "text/plain" }),
      unreachedContent(),
    );

    await expect(
      literature.serveContent("doc-1", { kind: "markdown" }),
    ).rejects.toThrow("non-gs://");
  });

  test("maps NOT_FOUND from ResolveContent to ResourceNotFoundError", async () => {
    const literature = adapter(() => {
      throw new ConnectError("no markdown", Code.NotFound);
    }, unreachedContent());

    await expect(
      literature.serveContent("doc-1", { kind: "markdown" }),
    ).rejects.toBeInstanceOf(ResourceNotFoundError);
  });
});

// Config still fails closed on a missing service URL / bucket rather than building a transport with
// no target.
describe("loadEvidenceConfig", () => {
  test("reads the evidence service URL and the corpus bucket", () => {
    expect(
      loadEvidenceConfig({
        THEMIS_EVIDENCE_URL: "https://evidence.example",
        THEMIS_FULLTEXT_BUCKET: "example-corpus",
      }),
    ).toEqual({
      evidenceUrl: "https://evidence.example",
      corpusBucket: "example-corpus",
    });
  });

  test("fails loud when the URL is unset", () => {
    expect(() => loadEvidenceConfig({ THEMIS_FULLTEXT_BUCKET: "b" })).toThrow(
      "THEMIS_EVIDENCE_URL",
    );
  });

  test("fails loud when the corpus bucket is unset", () => {
    expect(() =>
      loadEvidenceConfig({ THEMIS_EVIDENCE_URL: "https://evidence.example" }),
    ).toThrow("THEMIS_FULLTEXT_BUCKET");
  });
});
