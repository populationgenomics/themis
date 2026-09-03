import { describe, expect, test } from "bun:test";
import {
  enforceResourceIsolation,
  isCrossSiteRequestError,
} from "./fetch-metadata";

// The rule, header combination by header combination. What matters is which side of the line each
// falls on: a same-origin or user-initiated request and a non-browser client pass; a request another
// site caused is rejected whatever its method, with the one exception of following a link to a page.

type Metadata = Partial<
  Record<"sec-fetch-site" | "sec-fetch-mode" | "sec-fetch-dest", string>
>;

function request(method: string, metadata: Metadata): Request {
  return new Request("https://themis.example/api/curation/people", {
    method,
    headers: metadata,
  });
}

const rejected = (method: string, metadata: Metadata): boolean => {
  try {
    enforceResourceIsolation(request(method, metadata));
    return false;
  } catch (error) {
    if (!isCrossSiteRequestError(error)) throw error;
    return true;
  }
};

describe("resource isolation", () => {
  test("a request carrying no Fetch Metadata passes: a non-browser client", () => {
    expect(rejected("POST", {})).toBe(false);
    expect(rejected("GET", {})).toBe(false);
  });

  test("a same-origin request passes, whatever the method", () => {
    for (const method of ["GET", "POST", "PUT"]) {
      expect(
        rejected(method, {
          "sec-fetch-site": "same-origin",
          "sec-fetch-mode": "cors",
        }),
      ).toBe(false);
    }
  });

  test("a user-initiated request passes: a typed URL, a bookmark", () => {
    expect(
      rejected("GET", {
        "sec-fetch-site": "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
      }),
    ).toBe(false);
  });

  test("a cross-site form post is rejected: the navigation exemption is GET-only", () => {
    // The shape a `<form method="POST" enctype="text/plain">` on another site produces — a simple
    // request, no preflight, arriving as the curator.
    expect(
      rejected("POST", {
        "sec-fetch-site": "cross-site",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
      }),
    ).toBe(true);
  });

  test("a cross-site subresource or fetch is rejected, GET included", () => {
    for (const [mode, dest] of [
      ["cors", "empty"],
      ["no-cors", "image"],
      ["no-cors", "script"],
    ]) {
      expect(
        rejected("GET", {
          "sec-fetch-site": "cross-site",
          "sec-fetch-mode": mode,
          "sec-fetch-dest": dest,
        }),
      ).toBe(true);
    }
  });

  test("a same-site request is treated as another site's", () => {
    expect(
      rejected("POST", {
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
      }),
    ).toBe(true);
  });

  test("a cross-site top-level navigation passes: a link to a page, followed", () => {
    expect(
      rejected("GET", {
        "sec-fetch-site": "cross-site",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
      }),
    ).toBe(false);
  });

  test("a navigation into a frame, <object> or <embed> is rejected: it renders inside the other page", () => {
    for (const dest of ["iframe", "frame", "object", "embed"]) {
      expect(
        rejected("GET", {
          "sec-fetch-site": "cross-site",
          "sec-fetch-mode": "navigate",
          "sec-fetch-dest": dest,
        }),
      ).toBe(true);
    }
  });

  test("the rejection names the request it rejected", () => {
    expect(() =>
      enforceResourceIsolation(
        request("POST", {
          "sec-fetch-site": "cross-site",
          "sec-fetch-mode": "navigate",
        }),
      ),
    ).toThrow(/cross-site request rejected: POST .*cross-site.*navigate/);
  });
});
