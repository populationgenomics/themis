import { create } from "@bufbuild/protobuf";
import {
  type Client,
  Code,
  ConnectError,
  createClient,
  type Interceptor,
} from "@connectrpc/connect";
import { createGrpcTransport } from "@connectrpc/connect-node";
import { GoogleAuth, type IdTokenClient } from "google-auth-library";
import {
  FileSelectorSchema,
  Literature,
  type LocateResponse,
  LocateResponseSchema,
  MarkdownSelectorSchema,
  type PaperInfo,
  PdfSelectorSchema,
  Representation,
  type ResolveContentRequest,
} from "@/models/literature";
import { ResourceNotFoundError } from "../../errors";
import type {
  ContentObject,
  ContentPort,
  ContentSelector,
  LiteraturePort,
} from "../../ports";
import { loadEvidenceConfig } from "./config";
import { createContent } from "./content";

// The live literature adapter — the BFF's first gRPC client. A Connect gRPC transport over the
// protobuf-es literature stub, authenticated by an ID-token interceptor (audience = the service URL,
// as Cloud Run IAM requires). `describePaper`/`locate` return the RPC responses directly (the
// literature proto IS the BFF↔frontend view model). `serveContent` resolves the object's `gs://` URI
// through the service, pins it to the corpus bucket, and hands it to the injected `ContentPort` to
// serve.

class LiveLiterature implements LiteraturePort {
  constructor(
    private readonly client: Client<typeof Literature>,
    private readonly content: ContentPort,
    private readonly corpusBucket: string,
  ) {}

  async describePaper(docId: string): Promise<PaperInfo> {
    try {
      return await this.client.describePaper({ docId });
    } catch (error) {
      throw mapNotFound(error, `unknown paper ${docId}`);
    }
  }

  async serveContent(
    docId: string,
    selector: ContentSelector,
  ): Promise<Response> {
    return this.content.serve(await this.resolveObject(docId, selector));
  }

  private async resolveObject(
    docId: string,
    selector: ContentSelector,
  ): Promise<ContentObject> {
    let location: { gcsUri: string; mediaType: string };
    try {
      location = await this.client.resolveContent({
        docId,
        selector: encodeSelector(selector),
      });
    } catch (error) {
      throw mapNotFound(error, `${docId}: no such content`);
    }
    const downloadName = selector.kind === "file" ? selector.name : undefined;
    return corpusObject(location, this.corpusBucket, downloadName);
  }

  async locate(
    docId: string,
    quote: string,
    representation: Representation,
  ): Promise<LocateResponse> {
    try {
      return await this.client.locate({ docId, quote, representation });
    } catch (error) {
      // The pane has no "unavailable" highlight state — only located / not-located — so "the paper
      // lacks this representation" (FAILED_PRECONDITION) surfaces as the not-located warning for
      // either representation, matching the fixture adapter, rather than erroring the reveal.
      // UNIMPLEMENTED is only a legitimate outcome for PDF, whose server-side location is not wired
      // yet; for markdown it means version skew or a servicer bug, and must surface, not be masked.
      if (
        error instanceof ConnectError &&
        (error.code === Code.FailedPrecondition ||
          (error.code === Code.Unimplemented &&
            representation === Representation.PDF))
      ) {
        return create(LocateResponseSchema, {
          result: { case: "notLocated", value: {} },
        });
      }
      throw mapNotFound(error, `unknown paper ${docId}`);
    }
  }
}

/** The resolved `gs://` location as a `ContentObject`, pinned to the corpus bucket: an evidence-service
 *  bug must not name an object in another bucket the web SA can read (the per-tenant working-document
 *  bucket), so a resolution outside `corpusBucket` is refused before it reaches the signer. Pure, so
 *  the pin is testable without gRPC. */
export function corpusObject(
  location: { gcsUri: string; mediaType: string },
  corpusBucket: string,
  downloadName?: string,
): ContentObject {
  const { bucket, object } = parseGcsUri(location.gcsUri);
  if (bucket !== corpusBucket) {
    throw new Error(
      `evidence service named an object outside the corpus bucket: ${location.gcsUri}`,
    );
  }
  return { bucket, object, mediaType: location.mediaType, downloadName };
}

/** Add `Authorization: Bearer <id-token>` minted for `audience` (the Cloud Run service URL).
 *  `getRequestHeaders` returns the token through google-auth's own cache — refetched from the
 *  metadata server only near expiry, not on every RPC. */
function idTokenInterceptor(audience: string): Interceptor {
  const auth = new GoogleAuth();
  let client: Promise<IdTokenClient> | undefined;
  return (next) => async (request) => {
    if (client === undefined) {
      client = auth.getIdTokenClient(audience).catch((error: unknown) => {
        // A rejected promise must not stay cached, or a transient cold-start failure poisons every
        // later RPC for the process's life; drop it so the next request retries the token mint.
        client = undefined;
        throw error;
      });
    }
    const headers = await (await client).getRequestHeaders();
    const authorization = headers.get("authorization");
    if (authorization === null) {
      throw new Error(`no ID token minted for audience ${audience}`);
    }
    request.header.set("authorization", authorization);
    return next(request);
  };
}

/** The ResolveContent selector oneof for a port `ContentSelector`. */
function encodeSelector(
  selector: ContentSelector,
): ResolveContentRequest["selector"] {
  switch (selector.kind) {
    case "markdown":
      return { case: "markdown", value: create(MarkdownSelectorSchema, {}) };
    case "pdf":
      return { case: "pdf", value: create(PdfSelectorSchema, {}) };
    case "file":
      return {
        case: "file",
        value: create(FileSelectorSchema, { name: selector.name }),
      };
  }
}

/** A NOT_FOUND gRPC status is the port's `ResourceNotFoundError`; anything else propagates. */
function mapNotFound(error: unknown, message: string): never {
  if (error instanceof ConnectError && error.code === Code.NotFound) {
    throw new ResourceNotFoundError(message);
  }
  throw error;
}

function parseGcsUri(uri: string): { bucket: string; object: string } {
  const match = /^gs:\/\/([^/]+)\/(.+)$/.exec(uri);
  if (match === null) {
    throw new Error(
      `evidence service returned a non-gs:// content URI: ${uri}`,
    );
  }
  return { bucket: match[1], object: match[2] };
}

export function createLiterature(
  content: ContentPort = createContent(),
): LiteraturePort {
  const config = loadEvidenceConfig();
  const transport = createGrpcTransport({
    baseUrl: config.evidenceUrl,
    interceptors: [idTokenInterceptor(config.evidenceUrl)],
  });
  return new LiveLiterature(
    createClient(Literature, transport),
    content,
    config.corpusBucket,
  );
}
