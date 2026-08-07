// Corpus content is third-party, so its media type decides whether a browser may render it inline.
// Anything off this allowlist — notably a publisher-supplied `image/svg+xml` or `text/html` — is
// served as a download (`Content-Disposition: attachment`), so it cannot execute as a document. The
// fixture byte path sets the header directly; the live path bakes the same decision into the signed
// URL's `response-content-disposition` override.

const INLINE_MEDIA_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "application/pdf",
  "text/markdown",
]);

/** Whether `mediaType` may be served inline (its parameters, e.g. `; charset=`, are ignored). */
export function isInlineMediaType(mediaType: string): boolean {
  return INLINE_MEDIA_TYPES.has(mediaType.split(";")[0].trim().toLowerCase());
}

/** The `Content-Disposition` for corpus content, or `undefined` when the media type renders inline.
 *  Off-allowlist media is forced to `attachment` so it cannot execute as a document from this origin;
 *  a `filename` names the saved file, else the browser derives it from the URL's last segment — the
 *  opaque object key on the signed-URL path, not the file's name. The name is carried in RFC 5987
 *  `filename*` form so any byte in a third-party file name is encoded unambiguously and cannot break
 *  out of the header. */
export function contentDisposition(
  mediaType: string,
  filename?: string,
): string | undefined {
  if (isInlineMediaType(mediaType)) return undefined;
  if (filename === undefined) return "attachment";
  return `attachment; filename*=UTF-8''${encodeRfc5987(filename)}`;
}

/** Percent-encode a string as an RFC 5987 `ext-value`: `encodeURIComponent` leaves `!'()*` untouched,
 *  which the grammar's `attr-char` set forbids, so escape those too. */
function encodeRfc5987(value: string): string {
  return encodeURIComponent(value).replace(
    /['()*!]/g,
    (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}
