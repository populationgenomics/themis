import { describe, expect, test } from "bun:test";
import { create, equals } from "@bufbuild/protobuf";
import {
  DoubleValueSchema,
  FieldDescriptorProto_Label,
  FieldDescriptorProtoSchema,
  FileDescriptorProtoSchema,
  FloatValueSchema,
  Int32ValueSchema,
  StructSchema,
} from "@bufbuild/protobuf/wkt";
import {
  AssessmentStatus,
  type Confidence,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import { ChecklistSchema, Svcv4ClassificationSchema } from "@/models/widgets";
import { parseTextproto, TextprotoError } from "./textproto";

// The text format as the widget examples are written in it: every construct a payload needs reads
// into the message its descriptor describes, and anything else fails naming the line and column.

describe("the text format", () => {
  test("reads nested and repeated messages, in braces or angle brackets, with comments", () => {
    const read = parseTextproto(
      ChecklistSchema,
      `# a checklist
items {
  id: "a"  # trailing comment
  label: "First"
  checked: true
}
items < id: 'b', label: "Second"; >`,
    );
    expect(
      equals(
        ChecklistSchema,
        read,
        create(ChecklistSchema, {
          items: [
            { id: "a", label: "First", checked: true },
            { id: "b", label: "Second" },
          ],
        }),
      ),
    ).toBe(true);
  });

  test("reads the list syntax for a repeated field", () => {
    const read = parseTextproto(
      Svcv4ClassificationSchema,
      `open_values { assumption: "x" codes: ["CLN_AFF", "CLN_DNV"] }`,
    );
    expect(read.openValues[0].codes).toEqual(["CLN_AFF", "CLN_DNV"]);
  });

  test("reads enums by name or number, and a well-known type as a plain message", () => {
    const read = parseTextproto(
      Svcv4ClassificationSchema,
      `codes {
  code: "POP_FRQ"
  status: ASSESSMENT_STATUS_SCORED
  confidence: 2
  evidence { retrieval { retrieved_at { seconds: 1790677205 nanos: 868006000 } } }
}`,
    );
    const [code] = read.codes;
    expect(code.status).toBe(AssessmentStatus.SCORED);
    expect(code.confidence).toBe(2 as Confidence);
    const source = code.evidence[0].source;
    if (source.case !== "retrieval") throw new Error("no retrieval");
    expect(source.value.retrievedAt?.seconds).toBe(BigInt(1790677205));
    expect(source.value.retrievedAt?.nanos).toBe(868006000);
  });

  test("unescapes strings, joins adjacent ones, and reads them as UTF-8", () => {
    const read = parseTextproto(
      ChecklistSchema,
      String.raw`items { id: "a" label: "tab\there \"quoted\" \x41\101é " 'and ' "more" }`,
    );
    expect(read.items[0].label).toBe('tab\there "quoted" AAé and more');
  });

  test("reads a map as key and value entries", () => {
    const read = parseTextproto(
      StructSchema,
      `fields { key: "a" value { string_value: "x" } }
fields { key: "b" value { number_value: 1.5 } }`,
    );
    expect(Object.keys(read.fields).sort()).toEqual(["a", "b"]);
    expect(read.fields.b.kind).toEqual({ case: "numberValue", value: 1.5 });
  });

  test.each([
    ["an unknown field", `items { nope: 1 }`, "1:9", "has no field nope"],
    [
      "an unknown enum value",
      `codes { status: SCORED }`,
      "1:17",
      "has no value SCORED",
    ],
    ["a field set twice", `items { id: "a" id: "b" }`, "1:17", "set twice"],
    ["an unterminated string", `items { id: "a }`, "1:", "unterminated string"],
    ["a scalar without its colon", `items { id "a" }`, "1:9", 'needs a ":"'],
    [
      "an unknown escape",
      String.raw`items { id: "\q" }`,
      "1:",
      "unknown escape",
    ],
    ["a missing closing brace", `items { id: "a"`, "1:16", 'expected "}"'],
    [
      "the expanded Any form",
      `[type.googleapis.com/x] {}`,
      "1:1",
      "extensions and the expanded Any form",
    ],
  ])("refuses %s, naming where", (_what, text, where, why) => {
    const schema = text.startsWith("codes")
      ? Svcv4ClassificationSchema
      : ChecklistSchema;
    let caught: unknown;
    try {
      parseTextproto(schema, text);
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(TextprotoError);
    expect((caught as Error).message.startsWith(where)).toBe(true);
    expect((caught as Error).message).toContain(why);
  });

  test("refuses an integer out of its field's range", () => {
    expect(() =>
      parseTextproto(
        Svcv4ClassificationSchema,
        `codes { cells { cell_id: "x" count: 4294967296 } }`,
      ),
    ).toThrow("out of range");
  });

  // Each case below is how protobuf's reference parser reads the text, or a text it refuses.
  test.each([
    ["a leading zero as octal", "value: 010", 8],
    ["a signed hex integer", "value: -0x10", -16],
    ["a decimal integer", "value: 10", 10],
  ])("reads %s", (_what, text, value) => {
    expect(parseTextproto(Int32ValueSchema, text).value).toBe(value);
  });

  test("reads a float's forms and its infinities", () => {
    expect(parseTextproto(FloatValueSchema, "value: 1f").value).toBe(1);
    // Past float's largest value as written, and read as it, as the wire would store it.
    expect(parseTextproto(FloatValueSchema, "value: 3.4028235e38").value).toBe(
      Math.fround(3.4028235e38),
    );
    expect(parseTextproto(DoubleValueSchema, "value: .5e1").value).toBe(5);
    expect(parseTextproto(FloatValueSchema, "value: -inf").value).toBe(
      Number.NEGATIVE_INFINITY,
    );
    expect(parseTextproto(FloatValueSchema, "value: nan").value).toBeNaN();
  });

  test("reads the list syntax for scalars after a colon, and for messages without one", () => {
    expect(
      parseTextproto(FileDescriptorProtoSchema, `dependency: ["a", "b"]`)
        .dependency,
    ).toEqual(["a", "b"]);
    expect(
      parseTextproto(FileDescriptorProtoSchema, `message_type [{ name: "A" }]`)
        .messageType[0].name,
    ).toBe("A");
  });

  test("reads a closed enum's known number", () => {
    expect(parseTextproto(FieldDescriptorProtoSchema, "label: 3").label).toBe(
      FieldDescriptorProto_Label.REPEATED,
    );
  });

  test.each([
    [
      "a leading zero that is not octal",
      Int32ValueSchema,
      "value: 08",
      "1:8",
      "expected an integer",
    ],
    [
      "a hex float",
      FloatValueSchema,
      "value: -0x10",
      "1:8",
      "expected a decimal number",
    ],
    [
      "an octal float",
      DoubleValueSchema,
      "value: 010",
      "1:8",
      "expected a decimal number",
    ],
    [
      "a float that overflows float's range",
      FloatValueSchema,
      "value: 1e40",
      "1:8",
      "overflows a float",
    ],
    [
      "a leading plus, as the C++ parser does",
      Int32ValueSchema,
      "value: +5",
      "1:8",
      'unexpected "+"',
    ],
    [
      "an enum number past int32",
      Svcv4ClassificationSchema,
      "codes { confidence: 3000000000 }",
      "1:21",
      "out of range for an enum",
    ],
    [
      "an enum number with a leading zero",
      Svcv4ClassificationSchema,
      "codes { confidence: 010 }",
      "1:21",
      "expected a themis.svcv4.models.Confidence value",
    ],
    [
      "a closed enum's unknown number",
      FieldDescriptorProtoSchema,
      "label: 99",
      "1:8",
      "has no value numbered 99",
    ],
    [
      "a map entry's key set twice",
      StructSchema,
      `fields { key: "a" key: "b" value { number_value: 1 } }`,
      "1:19",
      "sets key twice",
    ],
    [
      "a scalar list without its colon",
      FileDescriptorProtoSchema,
      `dependency ["a", "b"]`,
      "1:1",
      'needs a ":"',
    ],
  ])("refuses %s, as the reference does", (_what, schema, text, where, why) => {
    let caught: unknown;
    try {
      parseTextproto(schema, text);
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(TextprotoError);
    expect((caught as Error).message.startsWith(`${where}:`)).toBe(true);
    expect((caught as Error).message).toContain(why);
  });
});
