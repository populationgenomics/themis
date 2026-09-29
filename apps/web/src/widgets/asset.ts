import {
  type DescField,
  type DescMessage,
  fromBinary,
  type MessageShape,
  ScalarType,
  toBinary,
} from "@bufbuild/protobuf";
import { BinaryReader, WireType } from "@bufbuild/protobuf/wire";
import { type Any, AnySchema, anyPack } from "@bufbuild/protobuf/wkt";
import { createValidator } from "@bufbuild/protovalidate";

// The asset an `::embed[<path>]` names (docs/design/document-widgets.md), as themis/widgets/asset.py
// defines it for the guest: a path over a character set on which markdown's reading and the raw
// label agree, and bytes that are a serialized google.protobuf.Any over a payload that parses as its
// type and passes that type's protovalidate rules. Nothing here is React, so the SharedWorker applies
// a widget's operation through the same reads the renderer draws with.
//
// The Any is read only in the one encoding every parser reads alike, the type URL, then the value,
// and no other field: protobuf-es reads some bytes upb refuses, and a file the push hook's parser
// refuses is one it compares as no asset at all, so drawing it would draw a version the hook never
// checked. The payload is read only where each field it carries has the wire type its schema gives
// it, for the same reason: protobuf-es reads a known field whatever its wire type, where upb keeps
// one in another wire type as a field it does not know. asset-cases.test-support.json holds this
// reading and themis/widgets/asset.py's to the same cases.
//
// Every decode of a payload keeps its unknown fields and every encode writes them back: an asset
// from a newer agent SDK carries fields this build does not know, and dropping them would delete
// them on a curator's click.

/** An asset, or the path naming it, that the document cannot draw; the message says why. */
export class AssetError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AssetError";
  }
}

const PATH_CHARACTERS = /^[A-Za-z0-9._/-]+$/;

/** Why `path` is not one an `::embed` may name, or null: relative, every segment a real one, over
 *  A–Z, a–z, 0–9, `-`, `_`, `.` and `/`. */
export function assetPathProblem(path: string): string | null {
  if (path === "") return "the directive names no path";
  if (!PATH_CHARACTERS.test(path)) {
    return 'the path has a character outside A-Z, a-z, 0-9, "-", "_", "." and "/"';
  }
  if (path.startsWith("/")) return "the path is absolute";
  for (const segment of path.split("/")) {
    if (segment === "" || segment === "." || segment === "..") {
      return 'the path has an empty, "." or ".." segment';
    }
    if (segment.toLowerCase() === ".git") return "the path is inside .git";
  }
  return null;
}

/** The modes of a regular file in a git tree. A symbolic link (120000) holds the path it points at,
 *  not an asset. */
const REGULAR_FILE_MODES = new Set(["100644", "100755"]);

/** Why an entry of tree mode `mode` is not an asset, or null for a regular file. */
export function regularFileProblem(mode: string): string | null {
  return REGULAR_FILE_MODES.has(mode)
    ? null
    : `the path names a tree entry of mode ${mode}, not a regular file`;
}

/** The asset's `Any`. Raises `AssetError` when the bytes are not one naming a type, in the
 *  encoding `writePayload` writes. */
export function readAny(bytes: Uint8Array): Any {
  let wrapped: Any;
  try {
    wrapped = fromBinary(AnySchema, bytes, { readUnknownFields: true });
  } catch (error) {
    throw new AssetError(
      `the file is not a serialized google.protobuf.Any (${String(error)})`,
    );
  }
  if (wrapped.typeUrl === "") {
    throw new AssetError(
      "the file is not a serialized google.protobuf.Any naming a type",
    );
  }
  const canonical = toBinary(AnySchema, wrapped, { writeUnknownFields: false });
  if (!sameBytes(canonical, bytes)) {
    throw new AssetError(
      "the file is a google.protobuf.Any in an encoding other than the one its writer uses: the type URL, then the value, and no other field",
    );
  }
  return wrapped;
}

function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((byte, index) => byte === b[index]);
}

/** The message type an asset's `Any` names: its type URL's tail. */
export function payloadTypeName(wrapped: Any): string {
  return wrapped.typeUrl.slice(wrapped.typeUrl.lastIndexOf("/") + 1);
}

const validator = createValidator();

/** The payload `wrapped` carries, parsed as `schema` and validated. Raises `AssetError` when the
 *  `Any` names another type, the bytes do not parse as `schema`, or the payload fails its rules. */
export function readPayload<Desc extends DescMessage>(
  wrapped: Any,
  schema: Desc,
): MessageShape<Desc> {
  const name = payloadTypeName(wrapped);
  if (name !== schema.typeName) {
    throw new AssetError(`the asset holds ${name}, not ${schema.typeName}`);
  }
  let payload: MessageShape<Desc>;
  let problem: string | null;
  try {
    payload = fromBinary(schema, wrapped.value, { readUnknownFields: true });
    problem = wireTypeProblem(schema, wrapped.value);
  } catch (error) {
    throw new AssetError(
      `the payload does not parse as ${name} (${String(error)})`,
    );
  }
  if (problem !== null) throw new AssetError(problem);
  requireValid(schema, payload);
  return payload;
}

const VARINT_SCALARS = new Set<ScalarType>([
  ScalarType.INT32,
  ScalarType.INT64,
  ScalarType.UINT32,
  ScalarType.UINT64,
  ScalarType.SINT32,
  ScalarType.SINT64,
  ScalarType.BOOL,
]);
const I64_SCALARS = new Set<ScalarType>([
  ScalarType.FIXED64,
  ScalarType.SFIXED64,
  ScalarType.DOUBLE,
]);

/** The wire type a single value of scalar type `scalar` is written in. */
function scalarWireType(scalar: ScalarType): WireType {
  if (VARINT_SCALARS.has(scalar)) return WireType.Varint;
  if (I64_SCALARS.has(scalar)) return WireType.Bit64;
  if (scalar === ScalarType.STRING || scalar === ScalarType.BYTES) {
    return WireType.LengthDelimited;
  }
  return WireType.Bit32;
}

/** The wire types `field` may be written in. */
function wireTypesOf(field: DescField): WireType[] {
  switch (field.fieldKind) {
    case "scalar":
      return [scalarWireType(field.scalar)];
    case "enum":
      return [WireType.Varint];
    case "message":
      return [
        field.delimitedEncoding
          ? WireType.StartGroup
          : WireType.LengthDelimited,
      ];
    case "list":
      if (field.listKind === "message") {
        return [
          field.delimitedEncoding
            ? WireType.StartGroup
            : WireType.LengthDelimited,
        ];
      }
      if (field.listKind === "enum") {
        return [WireType.Varint, WireType.LengthDelimited];
      }
      return [
        ...new Set([scalarWireType(field.scalar), WireType.LengthDelimited]),
      ];
    case "map":
      return [WireType.LengthDelimited];
  }
}

function wireTypeMismatch(schema: DescMessage, field: DescField): string {
  return `${schema.typeName} carries its field ${field.name} in a wire type its schema does not give it, which parsers read differently`;
}

/** Why `bytes`, read as `schema`, carry a field `schema` declares in a wire type other than its own,
 *  at any depth, or null when they carry none. */
function wireTypeProblem(
  schema: DescMessage,
  bytes: Uint8Array,
): string | null {
  return walkFields(schema, new BinaryReader(bytes), undefined);
}

/** `wireTypeProblem` over the fields `reader` holds from where it is: to its end, or for a group,
 *  to the end tag of `group`. */
function walkFields(
  schema: DescMessage,
  reader: BinaryReader,
  group: number | undefined,
): string | null {
  while (reader.pos < reader.len) {
    const [number, wireType] = reader.tag();
    if (wireType === WireType.EndGroup) {
      if (number === group) return null;
      return `${schema.typeName} holds an end tag for no group it is in`;
    }
    const field = schema.fields.find((each) => each.number === number);
    if (field === undefined) {
      reader.skip(wireType, number);
      continue;
    }
    if (!wireTypesOf(field).includes(wireType)) {
      return wireTypeMismatch(schema, field);
    }
    const message =
      field.fieldKind === "message" ||
      (field.fieldKind === "list" && field.listKind === "message")
        ? field.message
        : undefined;
    let problem: string | null = null;
    if (message !== undefined && wireType === WireType.StartGroup) {
      problem = walkFields(message, reader, number);
    } else if (message !== undefined) {
      problem = wireTypeProblem(message, reader.bytes());
    } else if (field.fieldKind === "map") {
      problem = mapEntryProblem(schema, field, reader.bytes());
    } else {
      reader.skip(wireType, number);
    }
    if (problem !== null) return problem;
  }
  return group === undefined
    ? null
    : `${schema.typeName} holds a group with no end tag`;
}

/** `wireTypeProblem` over one entry of the map `field` of `schema`: a key, a value, and nothing else,
 *  since protobuf-es reads any other field of an entry as part of the one after it. */
function mapEntryProblem(
  schema: DescMessage,
  field: DescField & { fieldKind: "map" },
  bytes: Uint8Array,
): string | null {
  const reader = new BinaryReader(bytes);
  while (reader.pos < reader.len) {
    const [number, wireType] = reader.tag();
    if (number === 1) {
      if (wireType !== scalarWireType(field.mapKey))
        return wireTypeMismatch(schema, field);
      reader.skip(wireType, number);
    } else if (number === 2) {
      const expected =
        field.mapKind === "scalar"
          ? scalarWireType(field.scalar)
          : field.mapKind === "enum"
            ? WireType.Varint
            : WireType.LengthDelimited;
      if (wireType !== expected) return wireTypeMismatch(schema, field);
      if (field.mapKind === "message") {
        const inner = wireTypeProblem(field.message, reader.bytes());
        if (inner !== null) return inner;
      } else {
        reader.skip(wireType, number);
      }
    } else {
      return `${schema.typeName} carries an entry of its map ${field.name} holding a field other than its key and value, which parsers read differently`;
    }
  }
  return null;
}

/** `payload` as asset bytes. Raises `AssetError` when the payload fails its rules. */
export function writePayload<Desc extends DescMessage>(
  schema: Desc,
  payload: MessageShape<Desc>,
): Uint8Array {
  requireValid(schema, payload);
  return toBinary(AnySchema, anyPack(schema, payload));
}

function requireValid<Desc extends DescMessage>(
  schema: Desc,
  payload: MessageShape<Desc>,
): void {
  const result = validator.validate(schema, payload);
  if (result.kind === "invalid") {
    throw new AssetError(
      `the ${schema.typeName} payload fails its rules: ${result.violations.map(String).join("; ")}`,
    );
  }
  if (result.kind === "error") {
    throw new AssetError(
      `the ${schema.typeName} payload's rules could not be checked: ${result.error.message}`,
    );
  }
}
