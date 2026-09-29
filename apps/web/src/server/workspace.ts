import { create } from "@bufbuild/protobuf";
import type { RefDocSnapshot } from "@/models/sheaf";
import { type WorkspaceTip, WorkspaceTipSchema } from "@/models/workbench";

// What the workbench reads off a workspace repository's ref document on every Poll.

/** The branch the working document lives on and a curator's edit lands on. Which branch is the
 *  collaborative one is open (docs/design/workbench-workspace.md, Open questions). */
export const COLLABORATIVE_BRANCH = "refs/heads/main";

/** The collaborative branch's tip commit, or `undefined` while it has none — before the repository
 *  exists, or while nothing has been published to the branch. Raises for a branch that names another
 *  ref: a push cannot create one, so the document is not one a writer of this repository made. */
export function branchTip(snapshot: RefDocSnapshot): string | undefined {
  const target = snapshot.document?.refs[COLLABORATIVE_BRANCH];
  if (target === undefined) return undefined;
  if (target.target.case !== "oid") {
    throw new Error(
      `${COLLABORATIVE_BRANCH} is not a commit in the ref document (${target.target.case ?? "unset"})`,
    );
  }
  return target.target.value;
}

/** The Poll's `workspace_tip` for a ref document it read: the branch's tip commit, or no commit.
 *  Raises as `branchTip` does. */
export function readTip(snapshot: RefDocSnapshot): WorkspaceTip {
  const commit = branchTip(snapshot);
  return create(WorkspaceTipSchema, {
    state:
      commit === undefined
        ? { case: "noCommit", value: {} }
        : { case: "commit", value: commit },
  });
}

/** The Poll's `workspace_tip` for a ref document it could not read. */
export function unavailableTip(): WorkspaceTip {
  return create(WorkspaceTipSchema, {
    state: { case: "unavailable", value: {} },
  });
}

/** The Poll's `workspace_tip` for a ref document the service found damaged. */
export function damagedTip(): WorkspaceTip {
  return create(WorkspaceTipSchema, {
    state: { case: "damaged", value: {} },
  });
}
