import { loadEvidenceConfig } from "@/server/adapters/live/config";
import { selectedBackend } from "@/server/backend";

// The Content Security Policy the request perimeter declares on each response it returns, and the
// per-request nonce it is built around. Defense in depth behind the render path's own sanitization:
// a curator's session is ambient, so script the render path admits acts as them.
//
// Scripts are admitted by nonce plus 'strict-dynamic' and by nothing else: Next stamps the nonce on
// the framework bootstrap, and every chunk that bootstrap goes on to load — including a client
// navigation's, whose payload carries some other request's nonce — is admitted by 'strict-dynamic'.
// The nonce exists only during a request, which is what src/app/layout.tsx forces.

type EnvLike = Record<string, string | undefined>;

/** A GCS bucket name, as the policy may interpolate it. Narrower than GCS itself allows; wide
 *  enough for every name it mints, and it excludes the space and `;` that would end a directive. */
const BUCKET_NAME = /^[a-z0-9][a-z0-9._-]*$/;

/** The policy's inputs beyond the nonce. */
export interface PolicyOptions {
  /** A `next dev` server, which needs two concessions production does not: React `eval`s to rebuild
   *  server error stacks in the browser, and Fast Refresh injects stylesheets it cannot nonce. */
  development: boolean;
  /** Where a paper's bytes reach the browser from, as CSP source expressions. */
  contentSources: readonly string[];
}

/**
 * The sources the paper-content routes send the browser to for a paper's bytes.
 *
 * The live backend answers them with a 302 into the corpus bucket and the fixture streams the bytes
 * itself, so offline there is nothing off-origin to admit. Scoped to the bucket's path rather than
 * the bare host, which is shared with every other GCS tenant: a browser matches the path on a direct
 * request and ignores it once a request has been redirected, so this admits whatever signed URL the
 * 302 lands on while refusing script that aims a fetch at a bucket of its own.
 */
export function contentSources(env: EnvLike = process.env): string[] {
  if (selectedBackend(env) !== "live") return [];
  const { corpusBucket } = loadEvidenceConfig(env);
  if (!BUCKET_NAME.test(corpusBucket)) {
    throw new Error(
      `THEMIS_FULLTEXT_BUCKET is not a bucket name: ${corpusBucket}`,
    );
  }
  // Path style, as `getSignedUrl` mints it; signing with `virtualHostedStyle` would move the bucket
  // into the host and stop matching this.
  return [`https://storage.googleapis.com/${corpusBucket}/`];
}

/** 16 bytes from the CSPRNG, base64. */
export function mintNonce(): string {
  return btoa(
    String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))),
  );
}

/** The policy for one request, as a header value. */
export function policy(nonce: string, options: PolicyOptions): string {
  const content = options.contentSources.map((source) => ` ${source}`).join("");
  return [
    "default-src 'self'",
    // First of the `script-src*` directives: Next picks the one it reads the nonce from by prefix,
    // so a `script-src-elem` or `script-src-attr` ahead of this would leave every page unsigned.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${options.development ? " 'unsafe-eval'" : ""}`,
    options.development
      ? "style-src 'self' 'unsafe-inline'"
      : `style-src 'self' 'nonce-${nonce}'`,
    // Server-rendered `style` attributes, which `style-src` does not govern and no nonce applies to
    // (next/font emits `style="color:transparent"`). React's client writes styles through CSSOM,
    // which CSP does not govern at all.
    "style-src-attr 'unsafe-inline'",
    `img-src 'self'${content}`,
    `connect-src 'self'${content}`,
    "font-src 'self'",
    // pdf.js parses off the main thread; the bundler emits its worker same-origin, so the blob-URL
    // fallback it keeps for a cross-origin worker is never taken.
    "worker-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "upgrade-insecure-requests",
  ].join("; ");
}
