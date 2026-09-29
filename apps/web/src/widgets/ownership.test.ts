import { describe, expect, test } from "bun:test";
import {
  create,
  createFileRegistry,
  createRegistry,
  type DescMessage,
  fromJson,
  getOption,
  type JsonValue,
} from "@bufbuild/protobuf";
import { nestedTypes } from "@bufbuild/protobuf/reflect";
import {
  FileDescriptorProtoSchema,
  file_google_protobuf_descriptor,
} from "@bufbuild/protobuf/wkt";
import {
  ChecklistSchema,
  file_themis_widgets_models_widget,
  widget,
} from "@/models/widgets";
import {
  checkSchema,
  judgedContent,
  OwnershipSchemaError,
  userOnlyViolations,
} from "./ownership";
import cases from "./ownership-cases.test-support.json";
import { PAYLOAD_FILES } from "./payloads";

// The schema check, held to the schemas themis/widgets/ownership.py's is held to, and to every
// payload this build knows; and the check a user's own edit passes. The rule an agent's change
// keeps is the Python one's, held to the same file's cases. themis/widgets/tests/case_files.py
// writes the case file.

type SchemaCase = (typeof cases.schemas)[number];

function holderOf(each: SchemaCase): DescMessage {
  const file = fromJson(
    FileDescriptorProtoSchema,
    each.file as unknown as JsonValue,
    { registry: createRegistry(file_themis_widgets_models_widget) },
  );
  const registry = createFileRegistry(file, (name) =>
    name === "themis/widgets/models/widget.proto"
      ? file_themis_widgets_models_widget
      : name === "google/protobuf/descriptor.proto"
        ? file_google_protobuf_descriptor
        : undefined,
  );
  const holder = registry.getMessage(each.type);
  if (holder === undefined) throw new Error(`no ${each.type} in ${each.name}`);
  return holder;
}

describe("the ownership schema check", () => {
  for (const each of cases.schemas) {
    test(`reads ${each.name} as the guest's does`, () => {
      const check = () => checkSchema(holderOf(each));
      if (each.error === null) check();
      else expect(check).toThrow(each.error);
    });
  }

  test("reads every payload this build knows", () => {
    const payloads = PAYLOAD_FILES.flatMap((file) =>
      [...nestedTypes(file)].filter(
        (type): type is DescMessage =>
          type.kind === "message" && getOption(type, widget),
      ),
    );
    expect(payloads.length).toBeGreaterThan(0);
    for (const payload of payloads) checkSchema(payload);
  });
});

describe("a user's edit", () => {
  const base = fromJson(ChecklistSchema, {
    items: [
      { id: "a", label: "one" },
      { id: "b", label: "two", checked: true },
    ],
  });

  test("changes nothing but guards", () => {
    const next = fromJson(ChecklistSchema, {
      items: [
        { id: "a", label: "one", checked: true },
        { id: "b", label: "two" },
      ],
    });
    expect(userOnlyViolations(ChecklistSchema, base, next)).toEqual([]);
  });

  test("that changes a label, drops an item or reorders them is refused", () => {
    for (const items of [
      [
        { id: "a", label: "changed" },
        { id: "b", label: "two", checked: true },
      ],
      [{ id: "a", label: "one" }],
      [
        { id: "b", label: "two", checked: true },
        { id: "a", label: "one" },
      ],
    ]) {
      const next = create(ChecklistSchema, { items });
      expect(userOnlyViolations(ChecklistSchema, base, next)).toHaveLength(1);
    }
  });
});

describe("what a guard judges", () => {
  const registry = createFileRegistry(
    fromJson(FileDescriptorProtoSchema, cases.schema as unknown as JsonValue, {
      registry: createRegistry(file_themis_widgets_models_widget),
    }),
    (name) =>
      name === "themis/widgets/models/widget.proto"
        ? file_themis_widgets_models_widget
        : name === "google/protobuf/descriptor.proto"
          ? file_google_protobuf_descriptor
          : undefined,
  );
  const schema = (name: string): DescMessage => {
    const found = registry.getMessage(`themis.widgets.cases.${name}`);
    if (found === undefined) throw new Error(`no ${name}`);
    return found;
  };
  const Tree = schema("Tree");
  const Node = schema("Node");
  const approved = Tree.fields.find((field) => field.name === "approved");
  const accepted = Node.fields.find((field) => field.name === "accepted");
  if (approved === undefined || accepted === undefined)
    throw new Error("no guard");
  const judged = (desc: DescMessage, judge: typeof approved, json: JsonValue) =>
    Buffer.from(judgedContent(desc, fromJson(desc, json), judge)).toString(
      "hex",
    );

  test("leaves out the fields it ignores and every guard, and keeps the rest", () => {
    const base = judged(Tree, approved, { title: "a", note: { text: "x" } });
    expect(judged(Tree, approved, { title: "a", note: { text: "y" } })).toBe(
      base,
    );
    expect(
      judged(Tree, approved, {
        title: "a",
        approved: true,
        note: { ack: "k" },
      }),
    ).toBe(judged(Tree, approved, { title: "a" }));
    expect(
      judged(Tree, approved, { title: "b", note: { text: "x" } }),
    ).not.toBe(base);
  });

  test("leaves out the key an element is matched by", () => {
    expect(judged(Node, accepted, { id: "a", claim: "c" })).toBe(
      judged(Node, accepted, { id: "b", claim: "c" }),
    );
    expect(judged(Node, accepted, { id: "a", claim: "c" })).not.toBe(
      judged(Node, accepted, { id: "a", claim: "d" }),
    );
  });

  test("is read only of a schema the rule reads", () => {
    const schemaCase = cases.schemas.find(
      (each) => each.name === "a guard ignoring a name its message lacks",
    );
    if (schemaCase === undefined) throw new Error("no such schema case");
    const holder = holderOf(schemaCase);
    const element = holder.fields[0];
    const elementSchema =
      element.fieldKind === "list" && element.listKind === "message"
        ? element.message
        : undefined;
    if (elementSchema === undefined) throw new Error("no element list");
    const done = elementSchema.fields.find((field) => field.name === "done");
    if (done === undefined) throw new Error("no guard");
    expect(() =>
      judgedContent(elementSchema, fromJson(elementSchema, {}), done),
    ).toThrow(OwnershipSchemaError);
  });

  test("is asked of a guard of the message alone", () => {
    const title = Tree.fields.find((field) => field.name === "title");
    if (title === undefined) throw new Error("no title");
    expect(() => judgedContent(Tree, fromJson(Tree, {}), title)).toThrow(
      "not a guard",
    );
  });
});
