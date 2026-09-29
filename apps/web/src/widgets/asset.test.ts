import { expect, test } from "bun:test";
import {
  create,
  createFileRegistry,
  createRegistry,
  fromJson,
  type JsonValue,
} from "@bufbuild/protobuf";
import {
  AnySchema,
  FileDescriptorProtoSchema,
  file_google_protobuf_descriptor,
} from "@bufbuild/protobuf/wkt";
import { file_themis_widgets_models_widget } from "@/models/widgets";
import { AssetError, payloadTypeName, readAny, readPayload } from "./asset";
import cases from "./asset-cases.test-support.json";
import ownershipCases from "./ownership-cases.test-support.json";
import { payloadSchema } from "./payloads";

// The wrapper cases themis/widgets/asset.py is held to: this build draws exactly the files that
// case says draw, so a file the push hook's parser reads as no asset, or as another type, never
// draws here. A case may wrap a type of the ownership cases' schema, which both sides read beside
// their payloads. themis/widgets/tests/case_files.py writes the case file.

const CASES_TYPES = createFileRegistry(
  fromJson(
    FileDescriptorProtoSchema,
    ownershipCases.schema as unknown as JsonValue,
    { registry: createRegistry(file_themis_widgets_models_widget) },
  ),
  (name) =>
    name === "themis/widgets/models/widget.proto"
      ? file_themis_widgets_models_widget
      : name === "google/protobuf/descriptor.proto"
        ? file_google_protobuf_descriptor
        : undefined,
);

/** Whether the browser draws `bytes`: its Any reads, names a payload this build knows or one of the
 *  ownership cases' types, and the payload reads as that type and passes its rules. */
function draws(bytes: Uint8Array): boolean {
  try {
    const wrapped = readAny(bytes);
    const name = payloadTypeName(wrapped);
    const schema = payloadSchema(name) ?? CASES_TYPES.getMessage(name);
    if (schema === undefined) return false;
    readPayload(wrapped, schema);
    return true;
  } catch (error) {
    if (error instanceof AssetError) return false;
    throw error;
  }
}

for (const each of cases.cases) {
  test(`draws ${each.name} as the guest's linter does`, () => {
    expect(draws(Uint8Array.from(Buffer.from(each.bytes, "base64")))).toBe(
      each.draws,
    );
  });
}

// A payload whose map values and group bodies hold fields of their own: the wire types are checked
// beneath both, as upb, which keeps a mistyped field as one it does not know, would read them.
const NESTED = fromJson(FileDescriptorProtoSchema, {
  name: "themis/widgets/cases/nested.proto",
  package: "themis.widgets.cases.nested",
  syntax: "proto2",
  messageType: [
    {
      name: "Holder",
      field: [
        {
          name: "by_name",
          number: 1,
          label: "LABEL_REPEATED",
          type: "TYPE_MESSAGE",
          typeName: ".themis.widgets.cases.nested.Holder.ByNameEntry",
        },
        {
          name: "part",
          number: 2,
          label: "LABEL_OPTIONAL",
          type: "TYPE_GROUP",
          typeName: ".themis.widgets.cases.nested.Holder.Part",
        },
      ],
      nestedType: [
        {
          name: "ByNameEntry",
          field: [
            {
              name: "key",
              number: 1,
              label: "LABEL_OPTIONAL",
              type: "TYPE_STRING",
            },
            {
              name: "value",
              number: 2,
              label: "LABEL_OPTIONAL",
              type: "TYPE_MESSAGE",
              typeName: ".themis.widgets.cases.nested.Sub",
            },
          ],
          options: { mapEntry: true },
        },
        {
          name: "Part",
          field: [
            {
              name: "done",
              number: 3,
              label: "LABEL_OPTIONAL",
              type: "TYPE_BOOL",
            },
          ],
        },
      ],
    },
    {
      name: "Sub",
      field: [
        { name: "done", number: 1, label: "LABEL_OPTIONAL", type: "TYPE_BOOL" },
      ],
    },
  ],
} as unknown as JsonValue);
const HOLDER = (() => {
  const holder = createFileRegistry(NESTED, () => undefined).getMessage(
    "themis.widgets.cases.nested.Holder",
  );
  if (holder === undefined) throw new Error("no Holder");
  return holder;
})();

function holderAny(value: number[]) {
  return create(AnySchema, {
    typeUrl: `type.googleapis.com/${HOLDER.typeName}`,
    value: Uint8Array.from(value),
  });
}

/** One map entry of `by_name`: key "k", and `value`'s bytes as its value's. */
function entry(value: number[]): number[] {
  const body = [0x0a, 0x01, 0x6b, 0x12, value.length, ...value];
  return [0x0a, body.length, ...body];
}

for (const [name, value, problem] of [
  ["a map value's field in its own wire type", entry([0x08, 0x01]), null],
  [
    "a map value's field in another",
    entry([0x0a, 0x00]),
    "Sub carries its field done",
  ],
  [
    "a map entry holding a third field",
    // protobuf-es reads the third field's value as a second key's tag, where upb reads it as the value.
    [0x0a, 0x07, 0x0a, 0x01, 0x6b, 0x18, 0x0a, 0x01, 0x6b],
    "holding a field other than its key and value",
  ],
  ["a group's field in its own wire type", [0x13, 0x18, 0x01, 0x14], null],
  [
    "a group's field in another",
    [0x13, 0x1a, 0x00, 0x14],
    "Part carries its field done",
  ],
] as const) {
  test(`reads ${name}${problem === null ? "" : ", and refuses it"}`, () => {
    const read = () => readPayload(holderAny([...value]), HOLDER);
    if (problem === null) read();
    else expect(read).toThrow(problem);
  });
}
