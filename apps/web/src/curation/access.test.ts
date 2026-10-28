import { describe, expect, test } from "bun:test";
import { Role } from "@/gen/themis/rpc/auth_pb";
import { ModeOfInheritance } from "@/gen/themis/svcv4/models/svcv4_pb";
import {
  FIXTURE_CURATORS,
  FIXTURE_PROJECT,
} from "@/server/adapters/fixture/access";
import { isClientInputError, isResourceNotFoundError } from "@/server/errors";
import { DEV_USER_EMAIL } from "@/server/identity";
import {
  accessFor,
  type CurationAccess,
  isCurationAccessError,
} from "./access";
import { FixtureCurationStore } from "./fixture";
import { FixtureVariantResolver } from "./fixture-resolver";
import type { NewVariant } from "./model";

// What each role may do in a Project, and that a Project's curation reaches nothing of another's.

const OTHER_PROJECT = "acute-care";
const [SAM] = FIXTURE_CURATORS;

const ELSEWHERE: NewVariant = {
  gene: "FBN1",
  transcript: "NM_000138.5",
  hgvsC: "c.7003C>T",
  clingenAlleleId: "",
  diseaseLabel: "Marfan syndrome",
  mondoId: "MONDO:0007947",
  modeOfInheritance: ModeOfInheritance.AUTOSOMAL_DOMINANT,
  clinicalContext: "Tall stature.",
};

/** `email` holding `roles` on the fixture Project and both roles on `OTHER_PROJECT`; only `SAM` is
 *  a curator by the ACL. */
function accessAs(
  store: FixtureCurationStore,
  email: string,
  roles: Role[],
  projectId = FIXTURE_PROJECT,
): CurationAccess {
  const held = new Map<string, ReadonlySet<Role>>([
    [FIXTURE_PROJECT, new Set(roles)],
    [OTHER_PROJECT, new Set([Role.MANAGER, Role.CURATOR])],
  ]);
  return accessFor(
    store,
    { email, roles: { rolesIn: (id) => held.get(id) ?? new Set() } },
    projectId,
    new FixtureVariantResolver(),
    async (candidate, project) =>
      candidate === SAM && project === FIXTURE_PROJECT,
  );
}

const thrownBy = (promise: Promise<unknown>): Promise<unknown> =>
  promise.then(
    () => {
      throw new Error("resolved");
    },
    (error: unknown) => error,
  );

async function seededVariant(store: FixtureCurationStore, gene: string) {
  const variant = (await store.listVariants(FIXTURE_PROJECT)).find(
    (v) => v.gene === gene,
  );
  if (variant === undefined) throw new Error(`the fixture seeds no ${gene}`);
  return variant;
}

describe("curation access in a Project", () => {
  test("is not found for a Project the caller holds no role on", () => {
    const store = new FixtureCurationStore();
    expect(() =>
      accessAs(store, DEV_USER_EMAIL, [Role.CURATOR], "no-such-project"),
    ).toThrow("no such project");
  });

  test("refuses a curator the manager's operations", async () => {
    const curator = accessAs(new FixtureCurationStore(), SAM, [Role.CURATOR]);
    expect(isCurationAccessError(await thrownBy(curator.listVariants()))).toBe(
      true,
    );
  });

  test("gives a manager without the curator role no worksheet to work", async () => {
    const store = new FixtureCurationStore();
    const manager = accessAs(store, DEV_USER_EMAIL, [Role.MANAGER]);
    expect(await manager.myWorksheets()).toEqual([]);
    const [own] = await store.worksheetsOfCurator(DEV_USER_EMAIL);
    expect(
      isCurationAccessError(await thrownBy(manager.myWorksheet(own.id))),
    ).toBe(true);
  });

  test("lists only the Project's registrations and worksheets", async () => {
    const store = new FixtureCurationStore();
    await accessAs(
      store,
      DEV_USER_EMAIL,
      [Role.MANAGER],
      OTHER_PROJECT,
    ).createVariant(ELSEWHERE);
    const here = accessAs(store, DEV_USER_EMAIL, [Role.MANAGER, Role.CURATOR]);
    expect((await here.listVariants()).map((v) => v.gene).sort()).toEqual([
      "CFTR",
      "MYH7",
    ]);
    expect(
      (await here.myWorksheets()).map(({ variant }) => variant.projectId),
    ).toEqual([FIXTURE_PROJECT]);
  });

  test("answers a registration or worksheet of another Project as not found", async () => {
    const store = new FixtureCurationStore();
    const myh7 = await seededVariant(store, "MYH7");
    const [own] = await store.worksheetsOfCurator(DEV_USER_EMAIL);
    const elsewhere = accessAs(
      store,
      DEV_USER_EMAIL,
      [Role.MANAGER, Role.CURATOR],
      OTHER_PROJECT,
    );
    for (const attempt of [
      elsewhere.getVariant(myh7.id),
      elsewhere.progress(myh7.id),
      elsewhere.comparison(myh7.id),
      elsewhere.assign(myh7.id, SAM, "v"),
      elsewhere.myWorksheet(own.id),
    ]) {
      expect(isResourceNotFoundError(await thrownBy(attempt))).toBe(true);
    }
  });

  test("assigns only a curator of the Project, as the ACL lists them", async () => {
    const store = new FixtureCurationStore();
    const cftr = await seededVariant(store, "CFTR");
    const manager = accessAs(store, DEV_USER_EMAIL, [Role.MANAGER]);
    expect(
      isClientInputError(
        await thrownBy(manager.assign(cftr.id, "mallory@example.org", "v")),
      ),
    ).toBe(true);
    const myh7 = await seededVariant(store, "MYH7");
    const samOnMyh7 = (await store.worksheetsOfVariant(myh7.id)).find(
      (w) => w.curatorEmail === SAM,
    );
    if (samOnMyh7 === undefined)
      throw new Error("the fixture assigns SAM to MYH7");
    await store.withdraw(samOnMyh7.id);
    const worksheet = await manager.assign(myh7.id, SAM.toUpperCase(), "v");
    expect(worksheet.curatorEmail).toBe(SAM);
  });

  test("refuses to assign a worksheet to a service account, the operator's included", async () => {
    const store = new FixtureCurationStore();
    const cftr = await seededVariant(store, "CFTR");
    const manager = accessAs(store, DEV_USER_EMAIL, [Role.MANAGER]);
    const operator = "themis-clu@themis-dev.iam.gserviceaccount.com";
    const error = await thrownBy(manager.assign(cftr.id, operator, "v"));
    expect(isClientInputError(error)).toBe(true);
    expect((error as Error).message).toContain("service account");
  });

  test("still keeps a manager assigned to a variant blind to its other answers", async () => {
    const store = new FixtureCurationStore();
    const myh7 = await seededVariant(store, "MYH7");
    const manager = accessAs(store, DEV_USER_EMAIL, [
      Role.MANAGER,
      Role.CURATOR,
    ]);
    expect(
      isCurationAccessError(await thrownBy(manager.comparison(myh7.id))),
    ).toBe(true);
    const cftr = await seededVariant(store, "CFTR");
    expect(await manager.comparison(cftr.id)).toHaveLength(2);
  });
});
