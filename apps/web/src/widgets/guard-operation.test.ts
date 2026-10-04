import { describe, expect, test } from "bun:test";
import { create, fromBinary, toBinary } from "@bufbuild/protobuf";
import { BinaryWriter, WireType } from "@bufbuild/protobuf/wire";
import { AnySchema, anyPack } from "@bufbuild/protobuf/wkt";
import {
  Checklist_ItemSchema,
  ChecklistSchema,
  Svcv4ClassificationSchema,
} from "@/models/widgets";
import { fbn1Classification } from "@/widgets/svcv4-classification-fixture";
import { writePayload } from "./asset";
import {
  addressName,
  type GuardAddress,
  guardIn,
  setGuardFile,
} from "./guard-operation";

// A user's judgement applied to the bytes of a real asset: the guard its address names changes,
// nothing else does, and fields this build does not know survive the decode and the encode, at
// every level they appear. An address reaches a guard of the root, of a singular message, and of
// an element of a keyed list, and a guard holds a tick or a user's text.

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
  return setGuardFile(asset, mode, {
    path: tick.path,
    address: {
      steps: [{ field: "items", key: tick.itemId }],
      guard: "checked",
    },
    value: tick.checked,
  });
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
    ).toThrow("has no items[bs1].checked");
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

describe("a judgement on an SVCv4 classification", () => {
  const CLASSIFICATION = "assets/svcv4.binpb";
  const drawn = writePayload(Svcv4ClassificationSchema, fbn1Classification());

  function judge(address: GuardAddress, value: boolean | string) {
    const edit = setGuardFile(drawn, "100644", {
      path: CLASSIFICATION,
      address,
      value,
    });
    if (edit === undefined) throw new Error("the judgement wrote nothing");
    return {
      message: edit.message,
      payload: fromBinary(
        Svcv4ClassificationSchema,
        fromBinary(AnySchema, edit.files[0].bytes).value,
      ),
    };
  }

  test("ticks the whole record, the routing, or one code, and nothing beside it", () => {
    const root = judge({ steps: [], guard: "reviewed" }, true);
    expect(root.payload.reviewed).toBe(true);
    expect(root.payload.routing?.reviewed).toBe(false);
    expect(root.message).toBe(`Tick reviewed in ${CLASSIFICATION}`);

    const routing = judge(
      { steps: [{ field: "routing" }], guard: "reviewed" },
      true,
    );
    expect(routing.payload.routing?.reviewed).toBe(true);
    expect(routing.payload.reviewed).toBe(false);

    const code = judge(
      { steps: [{ field: "codes", key: "CLN_DNV" }], guard: "reviewed" },
      true,
    );
    expect(
      code.payload.codes
        .filter((each) => each.reviewed)
        .map((each) => each.code),
    ).toEqual(["CLN_DNV"]);
  });

  test("writes a note, and clearing one is a change of its own", () => {
    const address: GuardAddress = {
      steps: [{ field: "codes", key: "CLN_DNV" }],
      guard: "note",
    };
    const noted = judge(address, "The clinic letter confirms parentage.");
    const code = noted.payload.codes.find((each) => each.code === "CLN_DNV");
    expect(code?.note).toBe("The clinic letter confirms parentage.");
    expect(code?.reviewed).toBe(false);
    expect(noted.message).toBe(
      `Write codes[CLN_DNV].note in ${CLASSIFICATION}`,
    );
    const again = writePayload(Svcv4ClassificationSchema, noted.payload);
    expect(
      setGuardFile(again, "100644", {
        path: CLASSIFICATION,
        address,
        value: "",
      })?.message,
    ).toBe(`Clear codes[CLN_DNV].note in ${CLASSIFICATION}`);
  });

  test("names each address as the state and the commit read it", () => {
    expect(addressName({ steps: [], guard: "note" })).toBe("note");
    expect(
      addressName({
        steps: [{ field: "codes", key: "POP_FRQ" }],
        guard: "reviewed",
      }),
    ).toBe("codes[POP_FRQ].reviewed");
  });

  test("fails, never guesses, on an address the schema has no guard at", () => {
    const refused: [GuardAddress, boolean | string, string][] = [
      [{ steps: [], guard: "classification" }, true, "no plain scalar guard"],
      [
        { steps: [{ field: "routing", key: "x" }], guard: "note" },
        "",
        "singular",
      ],
      [
        { steps: [{ field: "codes" }], guard: "note" },
        "",
        "names an element's key",
      ],
      [
        { steps: [{ field: "framework" }], guard: "note" },
        "",
        "no plain scalar guard",
      ],
      [
        { steps: [{ field: "codes", key: "XYZ_ABC" }], guard: "note" },
        "",
        "has no codes[XYZ_ABC].note",
      ],
      [
        { steps: [], guard: "reviewed" },
        "yes",
        "holds a boolean, not a string",
      ],
      [{ steps: [], guard: "note" }, true, "holds a string, not a boolean"],
    ];
    for (const [address, value, reason] of refused) {
      expect(() =>
        setGuardFile(drawn, "100644", {
          path: CLASSIFICATION,
          address,
          value,
        }),
      ).toThrow(reason);
    }
  });
});

describe("a judgement on an absent singular message", () => {
  test("finds no guard, and writes none", () => {
    const payload = fbn1Classification();
    payload.routing = undefined;
    const address: GuardAddress = {
      steps: [{ field: "routing" }],
      guard: "note",
    };
    expect(
      guardIn(Svcv4ClassificationSchema, payload, address),
    ).toBeUndefined();
  });
});
