// Resource isolation by Fetch Metadata. The browser states where a request came from
// (`Sec-Fetch-Site`) and what for (`Sec-Fetch-Mode`, `Sec-Fetch-Dest`) in headers no page can set,
// which is what tells a request the app's own pages made from one another site caused the browser
// to send — identity is ambient here (IAP mints the assertion from the caller's cookie), so the
// two arrive equally authenticated.
//
// The rule is web.dev's resource-isolation policy: accept a request from the app's own origin, one
// the user initiated (a typed URL, a bookmark), or one carrying no Fetch Metadata (a non-browser
// client — curl, the fixture driver); of the rest, accept only a top-level GET navigation, which is
// how a link to a page is followed from anywhere; reject everything else, `same-site` included.

const SITE = "sec-fetch-site";
const MODE = "sec-fetch-mode";
const DEST = "sec-fetch-dest";

/** Thrown for a request another site caused the browser to send. An access check, not a data
 *  fault: the request may be well-formed, and the caller may well hold the role. */
export class CrossSiteRequestError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CrossSiteRequestError";
  }
}

export function isCrossSiteRequestError(
  error: unknown,
): error is CrossSiteRequestError {
  return error instanceof Error && error.name === "CrossSiteRequestError";
}

/** Reject a request another site caused the browser to send. Per request, whatever the method: a
 *  cross-site GET is rejected too, unless it is a top-level navigation. */
export function enforceResourceIsolation(request: Request): void {
  const site = request.headers.get(SITE);
  if (site === null || site === "same-origin" || site === "none") return;
  const mode = request.headers.get(MODE);
  const dest = request.headers.get(DEST);
  // A link to a page, followed from anywhere. Every other navigation destination — iframe, frame,
  // object, embed — renders the response inside the other site's page.
  if (request.method === "GET" && mode === "navigate" && dest === "document") {
    return;
  }
  throw new CrossSiteRequestError(
    `cross-site request rejected: ${request.method} with Sec-Fetch-Site ${site}` +
      (mode === null ? "" : `, Sec-Fetch-Mode ${mode}`),
  );
}
