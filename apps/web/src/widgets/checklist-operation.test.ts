import { describe, expect, test } from "bun:test";
import { create, fromBinary, toBinary } from "@bufbuild/protobuf";
import { BinaryWriter, WireType } from "@bufbuild/protobuf/wire";
import { AnySchema, anyPack } from "@bufbuild/protobuf/wkt";
import { Checklist_ItemSchema, ChecklistSchema } from "@/models/widgets";
import { setCheckedFile } from "./checklist-operation";

// A tick applied to the bytes of a real asset: the item it names changes, nothing else does, and
// fields this build does not know survive the decode and the encode, at every level they appear.

const PATH = "assets/checklist.binpb";
const TYPE_URL = "type.googleapis.com/themis.widgets.models.Checklist";
/** Field numbers no build of the schema declares, standing in for a newer agent SDK's. */
const NEWER_PAYLOAD_FIELD = 15;
const NEWER_ITEM_FIELD = 9;
const ITEMS = ChecklistSchema.field.items.number;
const ITEM = Checklist_ItemSchema.field;

/** An item as a newer schema would write it: the known fields, plus one this build does not know. */
function newerItem(id: string, label: string, checked: boolean): Uint8Array {
  const writer = new BinaryWriter();
  if (checked) writer.tag(ITEM.checked.number, WireType.Varint).bool(true);
  return writer
    .tag(ITEM.id.number, WireType.LengthDelimited)
    .string(id)
    .tag(ITEM.label.number, WireType.LengthDelimited)
    .string(label)
    .tag(NEWER_ITEM_FIELD, WireType.LengthDelimited)
    .string("kept")
    .finish();
}

function newerAsset(): Uint8Array {
  const payload = new BinaryWriter()
    .tag(ITEMS, WireType.LengthDelimited)
    .bytes(newerItem("ps3", "Functional studies reviewed", false))
    .tag(ITEMS, WireType.LengthDelimited)
    .bytes(newerItem("pm2", "Absent from controls", true))
    .tag(NEWER_PAYLOAD_FIELD, WireType.Varint)
    .int32(42)
    .finish();
  return toBinary(
    AnySchema,
    create(AnySchema, { typeUrl: TYPE_URL, value: payload }),
  );
}

function apply(
  tick: { path: string; itemId: string; checked: boolean },
  asset: Uint8Array,
  mode = "100644",
) {
  return setCheckedFile(asset, mode, tick);
}

function unknownFieldNumbers(message: {
  $unknown?: { no: number }[];
}): number[] {
  return (message.$unknown ?? []).map((field) => field.no);
}

describe("ticking a checklist item", () => {
  test("sets that item and keeps every field the build does not know", () => {
    const edit = apply(
      { path: PATH, itemId: "ps3", checked: true },
      newerAsset(),
    );
    if (edit === undefined) throw new Error("the tick wrote nothing");
    expect(edit.files.map((file) => file.path)).toEqual([PATH]);
    const wrapped = fromBinary(AnySchema, edit.files[0].bytes);
    expect(wrapped.typeUrl).toBe(TYPE_URL);
    const checklist = fromBinary(ChecklistSchema, wrapped.value);
    expect(checklist.items.map((item) => [item.id, item.checked])).toEqual([
      ["ps3", true],
      ["pm2", true],
    ]);
    expect(unknownFieldNumbers(checklist)).toEqual([NEWER_PAYLOAD_FIELD]);
    for (const item of checklist.items) {
      expect(unknownFieldNumbers(item)).toEqual([NEWER_ITEM_FIELD]);
    }
  });

  test("publishes nothing when the item already has the state", () => {
    expect(
      apply({ path: PATH, itemId: "pm2", checked: true }, newerAsset()),
    ).toBeUndefined();
  });

  test("fails, never guesses, when the item is not in the asset", () => {
    expect(() =>
      apply({ path: PATH, itemId: "bs1", checked: true }, newerAsset()),
    ).toThrow("has no item bs1");
  });

  test("fails when the asset is no valid checklist, or its path none an asset may be at", () => {
    const emptied = toBinary(
      AnySchema,
      anyPack(ChecklistSchema, create(ChecklistSchema, {})),
    );
    expect(() =>
      apply({ path: PATH, itemId: "ps3", checked: true }, emptied),
    ).toThrow("fails its rules");
    expect(() =>
      apply(
        { path: "../outside.binpb", itemId: "ps3", checked: true },
        newerAsset(),
      ),
    ).toThrow("segment");
  });

  test("fails on a tree entry that is not a regular file, whatever its bytes", () => {
    expect(() =>
      apply(
        { path: PATH, itemId: "ps3", checked: true },
        newerAsset(),
        "120000",
      ),
    ).toThrow("mode 120000");
  });
});
