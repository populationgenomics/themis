import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  test,
} from "bun:test";
import { create, type MessageInitShape, toJson } from "@bufbuild/protobuf";
import {
  click,
  findAll,
  install,
  MiniElement,
} from "@/components/minidom.test-support";
import {
  AssessmentSchema,
  type WorkflowAssessment,
  WorkflowAssessmentSchema,
} from "@/gen/themis/curation/models/curation_pb";
import { Consequence } from "@/gen/themis/evidence/models/evidence_pb";
import { Role } from "@/gen/themis/rpc/auth_pb";
import {
  AssessmentStatus,
  ModeOfInheritance,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import { curationApiPath } from "@/lib/routes";
import { FIXTURE_PROJECT } from "@/server/adapters/fixture/access";
import { DEV_USER_EMAIL } from "@/server/identity";
import { CurationAccess } from "./access";
import { FixtureCurationStore } from "./fixture";
import { FixtureVariantResolver } from "./fixture-resolver";
import { ROUTING_ID } from "./sections";
import type { WorksheetInit } from "./ui/worksheet";

// A submission takes what the curator can see. A draft for a workflow the routing has taken off the
// screen (a consequence class changed, or a mode registered more narrowly than the curator routed
// on) stays a draft rather than going into the reference unseen.

const WORKSHEET = "ws_0002"; // the dev user's MYH7 worksheet in the fixture seed

let dom: ReturnType<typeof install>;
let React: typeof import("react");
let createRoot: typeof import("react-dom/client").createRoot;
let Worksheet: typeof import("./ui/worksheet").Worksheet;
const realFetch = globalThis.fetch;
const unmounts: (() => void)[] = [];

beforeAll(async () => {
  dom = install();
  React = await import("react");
  ({ createRoot } = await import("react-dom/client"));
  ({ Worksheet } = await import("./ui/worksheet"));
});

afterEach(() => {
  globalThis.fetch = realFetch;
  while (unmounts.length > 0) unmounts.pop()?.();
});

afterAll(() => {
  dom.uninstall();
});

function scored(rationale: string): WorkflowAssessment {
  return create(WorkflowAssessmentSchema, {
    status: AssessmentStatus.SCORED,
    rationale,
  });
}

function draftJson(kind: MessageInitShape<typeof AssessmentSchema>["kind"]) {
  return toJson(AssessmentSchema, create(AssessmentSchema, { kind }));
}

function worksheetInit(
  modeOfInheritance: ModeOfInheritance,
  drafts: Record<string, unknown>,
): WorksheetInit {
  return {
    projectId: FIXTURE_PROJECT,
    worksheetId: WORKSHEET,
    workflowsVersion: "v",
    submissionCount: 0,
    drafts,
    variant: {
      gene: "MYH7",
      transcript: "NM_000257.4",
      hgvsC: "c.1988G>A",
      diseaseLabel: "hypertrophic cardiomyopathy",
      mondoId: "MONDO:0005045",
      modeOfInheritance,
      clinicalContext: "Proband with septal hypertrophy.",
    },
  };
}

async function mountWorksheet(init: WorksheetInit): Promise<MiniElement> {
  const container = dom.document.createElement("div");
  dom.document.body.appendChild(container);
  const root = createRoot(container as unknown as Element);
  unmounts.push(() => {
    React.act(() => root.unmount());
    dom.document.body.removeChild(container);
  });
  await React.act(async () => {
    root.render(React.createElement(Worksheet, { init }));
  });
  return container;
}

function submitButton(container: MiniElement): MiniElement {
  const [button] = findAll(
    container,
    (node) =>
      node instanceof MiniElement &&
      node.tagName === "BUTTON" &&
      node.textContent === "Submit",
  );
  if (!(button instanceof MiniElement)) throw new Error("no Submit button");
  return button;
}

/** The dev user as the fixture's ACL grants them: manager and curator of the fixture Project. */
function devUserAccess(store: FixtureCurationStore): CurationAccess {
  return new CurationAccess(
    store,
    DEV_USER_EMAIL,
    FIXTURE_PROJECT,
    new Set([Role.MANAGER, Role.CURATOR]),
    new FixtureVariantResolver(),
    async () => true,
  );
}

async function seeded() {
  const store = new FixtureCurationStore();
  const access = devUserAccess(store);
  const put = (workflowId: string, workflow: WorkflowAssessment) =>
    access.saveDraft(WORKSHEET, {
      workflowId,
      assessment: create(AssessmentSchema, {
        kind: { case: "workflow", value: workflow },
      }),
    });
  await put("cln_aff_ad", scored("shown"));
  // Hidden under autosomal dominant, and missing the rationale submitting would demand of it.
  await put("cln_aff_arxl", scored(""));
  await access.saveDraft(WORKSHEET, {
    workflowId: ROUTING_ID,
    assessment: create(AssessmentSchema, {
      kind: { case: "routing", value: { rationale: "why" } },
    }),
  });
  return { store, access };
}

describe("a submission", () => {
  test("takes the shown workflows and the sections, and leaves a hidden draft a draft", async () => {
    const { store, access } = await seeded();
    const submission = await access.submit(WORKSHEET, "", ["cln_aff_ad"]);
    const committed = (await store.assessments(submission.id)).map(
      (e) => e.workflowId,
    );
    expect(committed).toEqual(["cln_aff_ad", ROUTING_ID]);
    expect((await store.drafts(WORKSHEET)).map((e) => e.workflowId)).toContain(
      "cln_aff_arxl",
    );
  });

  test("still refuses a shown scored workflow without a rationale", async () => {
    const { access } = await seeded();
    await expect(
      access.submit(WORKSHEET, "", ["cln_aff_ad", "cln_aff_arxl"]),
    ).rejects.toThrow(/cln_aff_arxl/);
  });
});

describe("the hidden answers the submit card names", () => {
  test("names the answered workflows the routing leaves off the screen, and none of the shown", async () => {
    const container = await mountWorksheet(
      worksheetInit(ModeOfInheritance.X_LINKED_DOMINANT, {
        [ROUTING_ID]: draftJson({
          case: "routing",
          value: { consequenceClass: Consequence.MISSENSE },
        }),
        cln_aff_ad: draftJson({ case: "workflow", value: scored("a") }),
        cln_aff_arxl: draftJson({ case: "workflow", value: scored("b") }),
      }),
    );
    expect(container.textContent).toContain(
      "One saved answer is for a workflow the routing no longer shows (CLN_AFF cln_aff_arxl). It stays saved and is not submitted.",
    );
    expect(container.textContent).not.toContain("cln_aff_ad");
  });

  test("names multiple hidden answers in sorted order", async () => {
    const container = await mountWorksheet(
      worksheetInit(ModeOfInheritance.X_LINKED_DOMINANT, {
        [ROUTING_ID]: draftJson({
          case: "routing",
          value: { consequenceClass: Consequence.MISSENSE },
        }),
        cln_aff_arxl: draftJson({ case: "workflow", value: scored("b") }),
        cds_fxn: draftJson({ case: "workflow", value: scored("c") }),
      }),
    );
    expect(container.textContent).toContain(
      "2 saved answers are for workflows the routing no longer shows (CDS_FXN cds_fxn, CLN_AFF cln_aff_arxl). They stay saved and are not submitted.",
    );
  });
});

describe("the submit button", () => {
  test("stays disabled when the worksheet holds no draft, matching the server's refusal", async () => {
    const store = new FixtureCurationStore();
    const access = devUserAccess(store);
    let called = false;
    globalThis.fetch = (async () => {
      called = true;
      return new Response("{}", { status: 200 });
    }) as unknown as typeof fetch;

    const empty = await mountWorksheet(
      worksheetInit(ModeOfInheritance.AUTOSOMAL_DOMINANT, {}),
    );
    expect(submitButton(empty).disabled).toBe(true);
    await React.act(async () => click(empty, submitButton(empty)));
    expect(called).toBe(false);
    await expect(access.submit(WORKSHEET, "", [])).rejects.toThrow(
      /nothing to submit/,
    );
  });

  test("stays disabled when the worksheet holds only an off-screen workflow draft", async () => {
    const hiddenOnly = await mountWorksheet(
      worksheetInit(ModeOfInheritance.AUTOSOMAL_DOMINANT, {
        cln_aff_arxl: draftJson({ case: "workflow", value: scored("hidden") }),
      }),
    );
    expect(submitButton(hiddenOnly).disabled).toBe(true);
  });

  test("enables and submits when the worksheet holds only a routing draft", async () => {
    const store = new FixtureCurationStore();
    const access = devUserAccess(store);
    const submissions: string[] = [];
    globalThis.fetch = (async (url: RequestInfo | URL, init?: RequestInit) => {
      expect(String(url)).toBe(
        curationApiPath(FIXTURE_PROJECT, "worksheets", WORKSHEET, "submit"),
      );
      expect(init?.method).toBe("POST");
      const body = JSON.parse(String(init?.body)) as {
        note: string;
        workflowIds: string[];
      };
      const created = await access.submit(
        WORKSHEET,
        body.note,
        body.workflowIds,
      );
      submissions.push(created.id);
      return new Response("{}", { status: 200 });
    }) as unknown as typeof fetch;

    await access.saveDraft(WORKSHEET, {
      workflowId: ROUTING_ID,
      assessment: create(AssessmentSchema, {
        kind: { case: "routing", value: { rationale: "why" } },
      }),
    });
    const drafts = Object.fromEntries(
      (await store.drafts(WORKSHEET)).map((entry) => [
        entry.workflowId,
        toJson(AssessmentSchema, entry.assessment),
      ]),
    );
    const withRouting = await mountWorksheet(
      worksheetInit(ModeOfInheritance.AUTOSOMAL_DOMINANT, drafts),
    );
    const button = submitButton(withRouting);
    expect(button.disabled).toBe(false);
    await React.act(async () => click(withRouting, button));
    expect(submissions).toHaveLength(1);
    expect(withRouting.textContent).toContain("Submitted.");
    const committed = await store.assessments(submissions[0]);
    expect(committed.map((entry) => entry.workflowId)).toEqual([ROUTING_ID]);
  });
});
