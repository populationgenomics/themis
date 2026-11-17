import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { GET as getFile } from "@/app/api/papers/[id]/files/[name]/route";
import { GET as getMarkdown } from "@/app/api/papers/[id]/markdown/route";
import { GET as getPdf } from "@/app/api/papers/[id]/pdf/route";
import { GetAccessResponseSchema } from "@/gen/themis/rpc/auth_pb";
import { buildGate, type Gate } from "@/server/admission";
import { UnauthenticatedError } from "@/server/errors";
import type { LiteraturePort } from "@/server/ports";

// The paper-content routes and the `run` boundary they share: happy-path bytes and egress headers
// from the literature port, and compact `{error:{code,message}}` responses when admission or
// content resolution throws — with internal detail masked on a 500.

const DOC_XML = "11111111-1111-4111-8111-111111111111";
const DOC_OCR = "22222222-2222-4222-8222-222222222222";

interface Globals {
  __themisGate?: Gate;
  __themisComposition?: {
    analysisPorts?: unknown;
    literature?: LiteraturePort;
  };
}

const globals = globalThis as typeof globalThis & Globals;
let savedBackend: string | undefined;
let savedGate: Gate | undefined;
let hadComposition = false;
let savedLiterature: LiteraturePort | undefined;

function setLiterature(literature: LiteraturePort | undefined): void {
  globals.__themisComposition = {
    ...globals.__themisComposition,
    literature,
  };
}

beforeEach(() => {
  savedBackend = process.env.THEMIS_BACKEND;
  savedGate = globals.__themisGate;
  hadComposition = globals.__themisComposition !== undefined;
  savedLiterature = globals.__themisComposition?.literature;
  process.env.THEMIS_BACKEND = "fixture";
  globals.__themisGate = buildGate({ THEMIS_BACKEND: "fixture" });
  setLiterature(undefined);
});

afterEach(() => {
  if (savedBackend === undefined) delete process.env.THEMIS_BACKEND;
  else process.env.THEMIS_BACKEND = savedBackend;
  globals.__themisGate = savedGate;
  if (!hadComposition) {
    globals.__themisComposition = undefined;
  } else {
    setLiterature(savedLiterature);
  }
});

const get = (path: string) => new Request(`https://themis.example${path}`);

describe("the paper-content routes", () => {
  test("GET /api/papers/[id]/markdown serves the paper's markdown", async () => {
    const res = await getMarkdown(get(`/api/papers/${DOC_XML}/markdown`), {
      params: Promise.resolve({ id: DOC_XML }),
    });
    expect(res.status).toBe(200);
    expect(await res.text()).toContain(
      "# Regulatory variation in the ACME locus",
    );
  });

  test("GET /api/papers/[id]/pdf serves the paper's PDF", async () => {
    const res = await getPdf(get(`/api/papers/${DOC_XML}/pdf`), {
      params: Promise.resolve({ id: DOC_XML }),
    });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("application/pdf");
  });

  test("GET /api/papers/[id]/files/[name] serves an associated file", async () => {
    const res = await getFile(get(`/api/papers/${DOC_XML}/files/supp.csv`), {
      params: Promise.resolve({ id: DOC_XML, name: "supp.csv" }),
    });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-disposition")).toBe(
      "attachment; filename*=UTF-8''supp.csv",
    );
    expect(await res.text()).toContain("ACME,increased expression");
  });

  test("an absent representation answers 404 with a generic message", async () => {
    const res = await getMarkdown(get(`/api/papers/${DOC_OCR}/markdown`), {
      params: Promise.resolve({ id: DOC_OCR }),
    });
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({
      error: { code: "not_found", message: "resource not found" },
    });
  });

  test("an unauthenticated caller answers 401", async () => {
    const base = buildGate({ THEMIS_BACKEND: "fixture" });
    globals.__themisGate = {
      identity: {
        async authenticate() {
          throw new UnauthenticatedError("no IAP assertion");
        },
      },
      access: base.access,
    };
    const res = await getPdf(get(`/api/papers/${DOC_XML}/pdf`), {
      params: Promise.resolve({ id: DOC_XML }),
    });
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({
      error: { code: "unauthenticated", message: "unauthenticated" },
    });
  });

  test("a caller the deployment does not admit answers 403", async () => {
    const base = buildGate({ THEMIS_BACKEND: "fixture" });
    globals.__themisGate = {
      identity: base.identity,
      access: {
        async getAccess() {
          return create(GetAccessResponseSchema, { projects: [] });
        },
      },
    };
    const res = await getFile(get(`/api/papers/${DOC_XML}/files/figure1.png`), {
      params: Promise.resolve({ id: DOC_XML, name: "figure1.png" }),
    });
    expect(res.status).toBe(403);
    expect(await res.json()).toEqual({
      error: { code: "permission_denied", message: "not admitted" },
    });
  });

  test("an unexpected error answers a 500 that does not leak the thrown message", async () => {
    setLiterature({
      async describePaper() {
        throw new Error("unreached");
      },
      async locate() {
        throw new Error("unreached");
      },
      async serveContent() {
        throw new Error("internal secret detail");
      },
    });
    const wasError = console.error;
    console.error = () => {};
    let res: Response;
    try {
      res = await getPdf(get(`/api/papers/${DOC_XML}/pdf`), {
        params: Promise.resolve({ id: DOC_XML }),
      });
    } finally {
      console.error = wasError;
    }
    expect(res.status).toBe(500);
    const body = await res.json();
    expect(body).toEqual({
      error: { code: "internal", message: "internal server error" },
    });
    expect(JSON.stringify(body)).not.toContain("secret");
  });

  test.each([
    ["ResourceNotFoundError", 404, "not_found", "resource not found"],
    ["UnauthenticatedError", 401, "unauthenticated", "unauthenticated"],
    ["NotAdmittedError", 403, "permission_denied", "not admitted"],
  ] as const)(
    "a foreign-graph %s still maps to its envelope",
    async (name, status, code, message) => {
      // The ports are memoized on `globalThis` and cross Next's module graphs, so a
      // route can catch an instance whose class object is another graph's —
      // `instanceof` is false there; the name is the discriminant.
      setLiterature({
        async describePaper() {
          throw new Error("unreached");
        },
        async locate() {
          throw new Error("unreached");
        },
        async serveContent() {
          throw Object.assign(new Error("detail"), { name });
        },
      });
      const res = await getMarkdown(get(`/api/papers/${DOC_XML}/markdown`), {
        params: Promise.resolve({ id: DOC_XML }),
      });
      expect(res.status).toBe(status);
      expect(await res.json()).toEqual({ error: { code, message } });
    },
  );
});
