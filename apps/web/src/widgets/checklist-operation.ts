import {
  type Checklist_Item,
  Checklist_ItemSchema,
  ChecklistSchema,
} from "@/models/widgets";
import type { EditFile } from "@/workspace-copy/protocol";
import {
  AssetError,
  assetPathProblem,
  readAny,
  readPayload,
  regularFileProblem,
  writePayload,
} from "./asset";
import { judgedContent } from "./ownership";

// A curator ticking or unticking one checklist item, as the edit a window publishes
// (docs/design/workbench-workspace.md, "A curator's edit is rebased onto the tip, file by file"): the
// asset as the checklist was drawn from it, with that item set and every field this build does not
// know kept. The copy lands it on a moved tip only while the asset there is the one the curator saw.

export interface SetChecked {
  /** The asset's path in the repository. */
  path: string;
  itemId: string;
  checked: boolean;
}

/** What a tick on `item` judges: the item as its bytes carry it, without its tick. */
export function judgedItem(item: Checklist_Item): Uint8Array {
  return judgedContent(
    Checklist_ItemSchema,
    item,
    Checklist_ItemSchema.field.checked,
  );
}

/** The item `itemId` of the checklist in `asset`, or undefined when the asset holds no checklist
 *  with that item. */
export function itemIn(
  asset: Uint8Array,
  itemId: string,
): Checklist_Item | undefined {
  try {
    return readPayload(readAny(asset), ChecklistSchema).items.find(
      (each) => each.id === itemId,
    );
  } catch (error) {
    if (error instanceof AssetError) return undefined;
    throw error;
  }
}

/** The message of the commit a tick writes, naming the asset and the item. */
function setCheckedMessage({ path, itemId, checked }: SetChecked): string {
  return `${checked ? "Check" : "Uncheck"} ${itemId} in ${path}`;
}

/** The edit `tick` makes of `asset`, the checklist's bytes as it was drawn from a tree entry of mode
 *  `mode`: the commit message and the asset's new bytes. Undefined when the item already has that
 *  state, so there is nothing to publish. Raises `AssetError` when the path is not one an asset may
 *  be at, the entry is not a regular file, the bytes are not a checklist that passes its rules, or
 *  the checklist has no such item. */
export function setCheckedFile(
  asset: Uint8Array,
  mode: string,
  tick: SetChecked,
): { message: string; files: EditFile[] } | undefined {
  const problem = assetPathProblem(tick.path);
  if (problem !== null) throw new AssetError(`${tick.path}: ${problem}`);
  const notRegular = regularFileProblem(mode);
  if (notRegular !== null) throw new AssetError(`${tick.path}: ${notRegular}`);
  const wrapped = readAny(asset);
  const checklist = readPayload(wrapped, ChecklistSchema);
  const item = checklist.items.find((each) => each.id === tick.itemId);
  if (item === undefined) {
    throw new AssetError(
      `the checklist at ${tick.path} has no item ${tick.itemId}`,
    );
  }
  if (item.checked === tick.checked) return undefined;
  item.checked = tick.checked;
  return {
    message: setCheckedMessage(tick),
    files: [
      { path: tick.path, bytes: writePayload(ChecklistSchema, checklist) },
    ],
  };
}
