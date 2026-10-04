import { describe, expect, test } from "bun:test";
import { create, type MessageInitShape, toBinary } from "@bufbuild/protobuf";
import { BinaryWriter, WireType } from "@bufbuild/protobuf/wire";
import { AnySchema } from "@bufbuild/protobuf/wkt";
import { Checklist_ItemSchema, ChecklistSchema } from "@/models/widgets";
import { readAny, readPayload, writePayload } from "./asset";
import { setGuardFile } from "./guard-operation";
import { checkUserEdit } from "./user-edit";

// The write path's check that a user's edit changes guards alone, whatever widget made it.

const PATH = "assets/checklist.binpb";
type Items = NonNullable<MessageInitShape<typeof ChecklistSchema>["items"]>;

function asset(items: Items): Uint8Array {
  return writePayload(ChecklistSchema, create(ChecklistSchema, { items }));
}

const BASE = asset([
  { id: "a", label: "one" },
  { id: "b", label: "two", checked: true },
]);

const DRAWN = { bytes: BASE, mode: "100644" };

/** Check writing `bytes` over the asset as drawn. */
function writing(bytes: Uint8Array): void {
  checkUserEdit(PATH, DRAWN, bytes);
}

describe("a user's edit", () => {
  test("that ticks and unticks items passes", () => {
    const edit = setGuardFile(BASE, "100644", {
      path: PATH,
      address: { steps: [{ field: "items", key: "a" }], guard: "checked" },
      value: true,
    });
    if (edit === undefined) throw new Error("nothing written");
    const [written] = edit.files;
    const checklist = readPayload(readAny(written.bytes), ChecklistSchema);
    expect(checklist.items.map((item) => item.checked)).toEqual([true, true]);
    writing(written.bytes);
    writing(
      asset([
        { id: "a", label: "one" },
        { id: "b", label: "two" },
      ]),
    );
  });

  test("that changes an agent-written field, or the list, fails", () => {
    for (const items of [
      [
        { id: "a", label: "changed" },
        { id: "b", label: "two", checked: true },
      ],
      [{ id: "b", label: "two", checked: true }],
      [
        { id: "a", label: "one" },
        { id: "b", label: "two", checked: true },
        { id: "c", label: "new" },
      ],
    ]) {
      expect(() => writing(asset(items))).toThrow("beyond a user's judgements");
    }
  });

  test("that writes a file that was no asset, or another type, or over a link, fails", () => {
    expect(() => checkUserEdit("notes.md", undefined, BASE)).toThrow(
      "there is none",
    );
    const otherType = toBinary(
      AnySchema,
      create(AnySchema, {
        typeUrl: "type.googleapis.com/google.protobuf.Empty",
        value: new Uint8Array(),
      }),
    );
    expect(() => writing(otherType)).toThrow("keeps the asset's type");
    expect(() =>
      checkUserEdit(PATH, { bytes: BASE, mode: "120000" }, BASE),
    ).toThrow("mode 120000");
  });

  test("whose bytes two parsers read differently, or not in the encoding this build writes, fails", () => {
    const field = Checklist_ItemSchema.field;
    const wrap = (value: Uint8Array) =>
      toBinary(
        AnySchema,
        create(AnySchema, {
          typeUrl: "type.googleapis.com/themis.widgets.models.Checklist",
          value,
        }),
      );
    const list = (...items: Uint8Array[]) => {
      const writer = new BinaryWriter();
      for (const item of items) {
        writer
          .tag(ChecklistSchema.field.items.number, WireType.LengthDelimited)
          .bytes(item);
      }
      return writer.finish();
    };
    const b = toBinary(
      Checklist_ItemSchema,
      create(Checklist_ItemSchema, { id: "b", label: "two", checked: true }),
    );
    const id = new BinaryWriter()
      .tag(field.id.number, WireType.LengthDelimited)
      .string("a")
      .finish();
    // Item a's tick written with the length-delimited wire type around a repeat of its id: read as
    // ticked here, and by upb as unticked beside a field it does not know.
    const misread = new BinaryWriter()
      .tag(field.checked.number, WireType.LengthDelimited)
      .bytes(id)
      .raw(id)
      .tag(field.label.number, WireType.LengthDelimited)
      .string("one")
      .finish();
    expect(() => writing(wrap(list(misread, b)))).toThrow(
      "in a wire type its schema does not give it",
    );
    // Item a's label before its id, ticked: read alike everywhere, and not the bytes this build writes.
    const reordered = new BinaryWriter()
      .tag(field.checked.number, WireType.Varint)
      .bool(true)
      .tag(field.label.number, WireType.LengthDelimited)
      .string("one")
      .raw(id)
      .finish();
    expect(() => writing(wrap(list(reordered, b)))).toThrow(
      "in the encoding this build writes",
    );
  });
});
