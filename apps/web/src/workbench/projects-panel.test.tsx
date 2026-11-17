import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import { renderToStaticMarkup } from "react-dom/server";
import {
  GetAccessResponseSchema,
  ProjectGrantSchema,
  Role,
} from "@/gen/themis/rpc/auth_pb";
import { type Analysis, AnalysisSchema } from "@/models/workbench";
import type { Gate } from "@/server/admission";
import type { AnalysisPorts } from "@/server/ports";
import { ProjectsPanel } from "./projects-panel";

// What each Project card shows on the landing page. The failure these guard against is silent: a
// Project that holds Analyses rendering as though it held none, or dating itself by epoch 0.

const holder = globalThis as typeof globalThis & {
  __themisGate?: Gate;
  __themisComposition?: { analysisPorts?: AnalysisPorts };
};

let savedBackend: string | undefined;
let hadGate = false;
let savedGate: Gate | undefined;
let hadComposition = false;
let savedComposition: { analysisPorts?: AnalysisPorts } | undefined;

beforeEach(() => {
  savedBackend = process.env.THEMIS_BACKEND;
  hadGate = "__themisGate" in holder;
  savedGate = holder.__themisGate;
  hadComposition = "__themisComposition" in holder;
  savedComposition = holder.__themisComposition;
});

afterEach(() => {
  if (savedBackend === undefined) delete process.env.THEMIS_BACKEND;
  else process.env.THEMIS_BACKEND = savedBackend;
  if (hadGate) holder.__themisGate = savedGate;
  else delete holder.__themisGate;
  if (hadComposition) holder.__themisComposition = savedComposition;
  else delete holder.__themisComposition;
});

const unused = (method: string): never => {
  throw new Error(`unexpected call to ${method}`);
};

function installSeam(
  projectIds: readonly string[],
  analyses: readonly Analysis[],
): void {
  holder.__themisGate = {
    identity: {
      async authenticate() {
        return { email: "curator@example.org" };
      },
    },
    access: {
      async getAccess() {
        return create(GetAccessResponseSchema, {
          projects: projectIds.map((id) =>
            create(ProjectGrantSchema, { id, roles: [Role.CURATOR] }),
          ),
        });
      },
    },
  };
  holder.__themisComposition = {
    analysisPorts: {
      dataPlane: {
        async listAnalysesIn(scope) {
          const allowed = new Set(scope);
          return analyses.filter((a) => allowed.has(a.projectId));
        },
        createAnalysis: () => unused("createAnalysis"),
        getAnalysis: () => unused("getAnalysis"),
        pollEvents: () => unused("pollEvents"),
        steerAnalysis: () => unused("steerAnalysis"),
        interruptAnalysis: () => unused("interruptAnalysis"),
        getThread: () => unused("getThread"),
      },
      workspace: {
        readRefDoc: () => unused("readRefDoc"),
        signPackUrls: () => unused("signPackUrls"),
        publish: () => unused("publish"),
        servePack: () => unused("servePack"),
      },
      documents: null,
    },
  };
}

function analysis(id: string, projectId: string, iso?: string): Analysis {
  return create(AnalysisSchema, {
    id,
    projectId,
    ...(iso ? { createdAt: timestampFromDate(new Date(iso)) } : {}),
  });
}

function cardFor(html: string, projectId: string): string {
  const marker = `href="/project/${projectId}"`;
  const start = html.indexOf(marker);
  if (start < 0) throw new Error(`no card linking to ${projectId}`);
  const end = html.indexOf("</a>", start);
  return html.slice(start, end);
}

async function renderPanel(): Promise<string> {
  return renderToStaticMarkup(
    await ProjectsPanel({ requestHeaders: new Headers() }),
  );
}

describe("ProjectsPanel", () => {
  test("renders the offline fixture backend's seeded Projects through userContext", async () => {
    process.env.THEMIS_BACKEND = "fixture";
    delete holder.__themisGate;
    delete holder.__themisComposition;

    const html = await renderPanel();
    const cards = html.split('href="/project/').slice(1);
    expect(cards.length).toBeGreaterThanOrEqual(2);
    for (const card of cards) {
      const body = card.slice(0, card.indexOf("</a>"));
      expect(body).not.toContain(">0 analyses<");
      expect(body).toContain("<time");
    }
  });

  test("counts and dates each Project from its own Analyses", async () => {
    installSeam(
      ["proj_a", "proj_b"],
      [
        analysis("an_1", "proj_a", "2026-08-01T00:00:00Z"),
        analysis("an_2", "proj_a", "2026-08-03T00:00:00Z"),
        analysis("an_3", "proj_b", "2026-08-09T00:00:00Z"),
      ],
    );
    const html = await renderPanel();

    const cardA = cardFor(html, "proj_a");
    expect(cardA).toContain("2 analyses");
    expect(cardA).toContain('dateTime="2026-08-03T00:00:00.000Z"');
    expect(cardA).not.toContain("2026-08-01");

    const cardB = cardFor(html, "proj_b");
    expect(cardB).toContain("1 analysis");
    expect(cardB).not.toContain("1 analyses");
    expect(cardB).toContain('dateTime="2026-08-09T00:00:00.000Z"');
  });

  test("shows no date on a Project with no Analyses, rather than epoch 0", async () => {
    installSeam(
      ["proj_a", "proj_b"],
      [analysis("an_1", "proj_a", "2026-08-01T00:00:00Z")],
    );
    const html = await renderPanel();
    const emptyCard = cardFor(html, "proj_b");
    expect(emptyCard).toContain("0 analyses");
    expect(emptyCard).not.toContain("latest");
    expect(emptyCard).not.toContain("<time");
  });

  test("raises when an Analysis has no timestamp rather than dating its Project by epoch 0", async () => {
    installSeam(["proj_a"], [analysis("an_broken", "proj_a")]);
    await expect(renderPanel()).rejects.toThrow("analysis has no created_at");
  });
});
