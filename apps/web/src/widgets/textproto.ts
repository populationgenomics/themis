import {
  create,
  type DescEnum,
  type DescField,
  type DescMessage,
  type MessageShape,
  ScalarType,
} from "@bufbuild/protobuf";
import {
  isFieldError,
  type ReflectList,
  type ReflectMap,
  type ReflectMessage,
  reflect,
} from "@bufbuild/protobuf/reflect";

// Protobuf's text format, read into a message by its descriptor: what the widget examples are
// written in, so an example reads and diffs as text. Covers what a widget payload needs, and fails
// with a line and column on anything else: scalars, enums by name or number, nested and repeated
// messages (`{}` or `<>`), list syntax (`[a, b]`), maps as `key`/`value` entries, strings in either
// quote with the C escapes and adjacent-string concatenation, and `#` comments. Well-known types
// are ordinary messages here, as the text format writes them (`retrieved_at { seconds: 1 }`).
// Extensions and the expanded `Any` form (`[type.googleapis.com/...] {}`) are refused. Where the
// reference parser reads a text differently from how it looks, as `010` (octal, 8), this reads it
// the same way or refuses it.

/** A text a message cannot be read from; the message names the line, the column and why. */
export class TextprotoError extends Error {
  override name = "TextprotoError";
}

type Token =
  | { kind: "name"; text: string; line: number; column: number }
  | { kind: "number"; text: string; line: number; column: number }
  | { kind: "string"; bytes: Uint8Array; line: number; column: number }
  | { kind: "punct"; text: string; line: number; column: number }
  | { kind: "end"; line: number; column: number };

// `/` only so an expanded Any's type URL tokenizes and is refused by name, not as a stray character.
const PUNCTUATION = new Set(["{", "}", "<", ">", "[", "]", ":", ",", ";", "/"]);

const SIMPLE_ESCAPES: Readonly<Record<string, number>> = {
  a: 0x07,
  b: 0x08,
  f: 0x0c,
  n: 0x0a,
  r: 0x0d,
  t: 0x09,
  v: 0x0b,
  "\\": 0x5c,
  "'": 0x27,
  '"': 0x22,
  "?": 0x3f,
};

/** Split `text` into tokens, the byte content of each string already unescaped. */
function tokenize(text: string): Token[] {
  const tokens: Token[] = [];
  const encoder = new TextEncoder();
  let at = 0;
  let line = 1;
  let lineStart = 0;
  const fail = (why: string): never => {
    throw new TextprotoError(`${line}:${at - lineStart + 1}: ${why}`);
  };
  while (at < text.length) {
    const char = text[at];
    if (char === "\n") {
      at += 1;
      line += 1;
      lineStart = at;
      continue;
    }
    if (char === " " || char === "\t" || char === "\r") {
      at += 1;
      continue;
    }
    if (char === "#") {
      while (at < text.length && text[at] !== "\n") at += 1;
      continue;
    }
    const column = at - lineStart + 1;
    if (PUNCTUATION.has(char)) {
      tokens.push({ kind: "punct", text: char, line, column });
      at += 1;
      continue;
    }
    if (char === '"' || char === "'") {
      const bytes: number[] = [];
      at += 1;
      while (text[at] !== char) {
        if (at >= text.length || text[at] === "\n") fail("unterminated string");
        if (text[at] !== "\\") {
          const point = text.codePointAt(at) ?? 0;
          const piece = String.fromCodePoint(point);
          bytes.push(...encoder.encode(piece));
          at += piece.length;
          continue;
        }
        const next = text[at + 1];
        if (next !== undefined && next in SIMPLE_ESCAPES) {
          bytes.push(SIMPLE_ESCAPES[next]);
          at += 2;
        } else if (next !== undefined && /[0-7]/.test(next)) {
          const digits = /^[0-7]{1,3}/.exec(text.slice(at + 1))?.[0] ?? "";
          const value = Number.parseInt(digits, 8);
          if (value > 0xff) fail(`octal escape \\${digits} is past a byte`);
          bytes.push(value);
          at += 1 + digits.length;
        } else if (next === "x" || next === "X") {
          const digits = /^[0-9A-Fa-f]{1,2}/.exec(text.slice(at + 2))?.[0];
          if (digits === undefined) fail("\\x needs a hex digit");
          bytes.push(Number.parseInt(digits as string, 16));
          at += 2 + (digits as string).length;
        } else if (next === "u" || next === "U") {
          const width = next === "u" ? 4 : 8;
          const digits = text.slice(at + 2, at + 2 + width);
          if (!new RegExp(`^[0-9A-Fa-f]{${width}}$`).test(digits)) {
            fail(`\\${next} needs ${width} hex digits`);
          }
          const point = Number.parseInt(digits, 16);
          if (point > 0x10ffff || (point >= 0xd800 && point <= 0xdfff)) {
            fail(`\\${next}${digits} is not a Unicode scalar value`);
          }
          bytes.push(...encoder.encode(String.fromCodePoint(point)));
          at += 2 + width;
        } else {
          fail(`unknown escape \\${next ?? ""}`);
        }
      }
      at += 1;
      tokens.push({
        kind: "string",
        bytes: Uint8Array.from(bytes),
        line,
        column,
      });
      continue;
    }
    const word = /^[A-Za-z_][A-Za-z0-9_.]*/.exec(text.slice(at))?.[0];
    if (word !== undefined) {
      tokens.push({ kind: "name", text: word, line, column });
      at += word.length;
      continue;
    }
    const number =
      /^-?(0[xX][0-9A-Fa-f]+|(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?[fF]?)/.exec(
        text.slice(at),
      )?.[0];
    if (number !== undefined && /\d/.test(number)) {
      tokens.push({ kind: "number", text: number, line, column });
      at += number.length;
      continue;
    }
    if (char === "-" && /^-[A-Za-z]/.test(text.slice(at))) {
      // `-inf` and `-infinity`: a sign before a name.
      const name = /^-[A-Za-z]+/.exec(text.slice(at))?.[0] ?? "";
      tokens.push({ kind: "name", text: name, line, column });
      at += name.length;
      continue;
    }
    fail(`unexpected ${JSON.stringify(char)}`);
  }
  tokens.push({ kind: "end", line, column: at - lineStart + 1 });
  return tokens;
}

class Reader {
  private at = 0;
  constructor(private readonly tokens: Token[]) {}

  peek(): Token {
    return this.tokens[this.at];
  }

  next(): Token {
    const token = this.tokens[this.at];
    if (token.kind !== "end") this.at += 1;
    return token;
  }

  fail(token: Token, why: string): never {
    throw new TextprotoError(`${token.line}:${token.column}: ${why}`);
  }

  isPunct(text: string): boolean {
    const token = this.peek();
    return token.kind === "punct" && token.text === text;
  }

  take(text: string): boolean {
    if (!this.isPunct(text)) return false;
    this.next();
    return true;
  }

  expect(text: string): void {
    const token = this.next();
    if (token.kind !== "punct" || token.text !== text) {
      this.fail(token, `expected ${JSON.stringify(text)}`);
    }
  }
}

/** Read `text` as a `schema` message in the text format. Raises `TextprotoError` naming the line
 *  and column of the first thing it cannot read. The result is not validated against the payload's
 *  rules; that is the caller's to do, as for any payload. */
export function parseTextproto<Desc extends DescMessage>(
  schema: Desc,
  text: string,
): MessageShape<Desc> {
  const reader = new Reader(tokenize(text));
  const message = create(schema);
  readFields(reader, reflect(schema, message), undefined);
  const rest = reader.peek();
  if (rest.kind !== "end") reader.fail(rest, "expected a field name");
  return message;
}

/** Read fields into `target` until `close` (or the end of the text, for the outermost message). */
function readFields(
  reader: Reader,
  target: ReflectMessage,
  close: string | undefined,
): void {
  const seen = new Set<string>();
  for (;;) {
    const token = reader.peek();
    if (close !== undefined && reader.take(close)) return;
    if (token.kind === "end") {
      if (close !== undefined)
        reader.fail(token, `expected ${JSON.stringify(close)}`);
      return;
    }
    if (token.kind === "punct" && token.text === "[") {
      reader.fail(
        token,
        "extensions and the expanded Any form are not read here",
      );
    }
    if (token.kind !== "name") reader.fail(token, "expected a field name");
    reader.next();
    const field = target.desc.fields.find((each) => each.name === token.text);
    if (field === undefined) {
      reader.fail(token, `${target.desc.typeName} has no field ${token.text}`);
    }
    readField(reader, target, field, token, seen);
    if (!reader.take(",")) reader.take(";");
  }
}

function readField(
  reader: Reader,
  target: ReflectMessage,
  field: DescField,
  at: Token,
  seen: Set<string>,
): void {
  const colon = reader.take(":");
  if (field.fieldKind === "list" || field.fieldKind === "map") {
    const items = reader.isPunct("[")
      ? readListItems(reader, field, colon, at)
      : [{ at: reader.peek(), value: readOne(reader, field, colon, at) }];
    for (const item of items) {
      checked(reader, item.at, () => addItem(target, field, item.value));
    }
    return;
  }
  if (seen.has(field.name)) reader.fail(at, `${field.name} is set twice`);
  if (field.oneof !== undefined) {
    for (const member of field.oneof.fields) {
      if (member !== field && seen.has(member.name)) {
        reader.fail(
          at,
          `${field.name} and ${member.name} are both set in one oneof`,
        );
      }
    }
  }
  seen.add(field.name);
  const valueAt = reader.peek();
  const value = readOne(reader, field, colon, at);
  checked(reader, valueAt, () => target.set(field, value));
}

/** Run a write into a message, reporting a value the message refuses at the value's token. The
 *  reader checks ranges itself; this catches what it does not know to. */
function checked(reader: Reader, at: Token, write: () => void): void {
  try {
    write();
  } catch (error) {
    if (isFieldError(error)) reader.fail(at, error.message);
    throw error;
  }
}

/** The items of a `[a, b]` list, each with the token it starts at. A list of scalars or enums needs
 *  the colon, as a single one does; a list of messages or map entries may leave it out. */
function readListItems(
  reader: Reader,
  field: DescField,
  colon: boolean,
  at: Token,
): { at: Token; value: unknown }[] {
  const kind = elementKind(field).kind;
  if (!colon && kind !== "message" && kind !== "map") {
    reader.fail(at, `${field.name} needs a ":" before its value`);
  }
  reader.expect("[");
  const items: { at: Token; value: unknown }[] = [];
  if (reader.take("]")) return items;
  for (;;) {
    const itemAt = reader.peek();
    items.push({ at: itemAt, value: readOne(reader, field, true, itemAt) });
    if (reader.take("]")) return items;
    reader.expect(",");
  }
}

/** One value of `field`: a scalar or enum after a colon, or a message in braces. */
function readOne(
  reader: Reader,
  field: DescField,
  colon: boolean,
  at: Token,
): unknown {
  const kind = elementKind(field);
  if (kind.kind === "message") {
    const close = reader.take("{") ? "}" : reader.take("<") ? ">" : undefined;
    if (close === undefined)
      reader.fail(reader.peek(), "expected a message in braces");
    const inner = reflect(kind.message);
    readFields(reader, inner, close);
    return inner;
  }
  if (kind.kind === "map") {
    const close = reader.take("{") ? "}" : reader.take("<") ? ">" : undefined;
    if (close === undefined)
      reader.fail(reader.peek(), "expected a map entry in braces");
    return readMapEntry(reader, field, close);
  }
  if (!colon) reader.fail(at, `${field.name} needs a ":" before its value`);
  const token = reader.next();
  if (kind.kind === "enum") return enumValue(reader, kind.enum, token);
  return scalarValue(reader, kind.scalar, token);
}

type ElementKind =
  | { kind: "message"; message: DescMessage }
  | { kind: "enum"; enum: DescEnum }
  | { kind: "scalar"; scalar: ScalarType }
  | { kind: "map" };

function elementKind(field: DescField): ElementKind {
  switch (field.fieldKind) {
    case "message":
      return { kind: "message", message: field.message };
    case "enum":
      return { kind: "enum", enum: field.enum };
    case "scalar":
      return { kind: "scalar", scalar: field.scalar };
    case "list":
      if (field.listKind === "message")
        return { kind: "message", message: field.message };
      if (field.listKind === "enum") return { kind: "enum", enum: field.enum };
      return { kind: "scalar", scalar: field.scalar };
    case "map":
      return { kind: "map" };
  }
}

/** A map entry's key and value, as `key: ... value: ...` inside braces already opened. */
function readMapEntry(
  reader: Reader,
  field: DescField,
  close: string,
): { key: unknown; value: unknown } {
  if (field.fieldKind !== "map") throw new Error("not a map field");
  let key: unknown;
  let value: unknown;
  const seen = new Set<string>();
  while (!reader.take(close)) {
    const token = reader.next();
    if (
      token.kind !== "name" ||
      (token.text !== "key" && token.text !== "value")
    ) {
      reader.fail(token, 'a map entry holds only "key" and "value"');
    }
    if (seen.has(token.text)) {
      reader.fail(token, `a map entry sets ${token.text} twice`);
    }
    seen.add(token.text);
    const colon = reader.take(":");
    if (token.text === "key") {
      if (!colon) reader.fail(token, 'key needs a ":"');
      key = scalarValue(reader, field.mapKey, reader.next());
    } else if (field.mapKind === "message") {
      const inner = reflect(field.message);
      const open = reader.take("{") ? "}" : reader.take("<") ? ">" : undefined;
      if (open === undefined)
        reader.fail(reader.peek(), "expected a message in braces");
      readFields(reader, inner, open);
      value = inner;
    } else {
      if (!colon) reader.fail(token, 'value needs a ":"');
      const next = reader.next();
      value =
        field.mapKind === "enum"
          ? enumValue(reader, field.enum, next)
          : scalarValue(reader, field.scalar, next);
    }
    if (!reader.take(",")) reader.take(";");
  }
  if (key === undefined) reader.fail(reader.peek(), "a map entry needs a key");
  return { key, value };
}

function addItem(
  target: ReflectMessage,
  field: DescField,
  item: unknown,
): void {
  if (field.fieldKind === "list") {
    (target.get(field) as ReflectList).add(item);
    return;
  }
  const { key, value } = item as { key: unknown; value: unknown };
  const map = target.get(field) as ReflectMap;
  map.set(key, value ?? defaultMapValue(field));
}

/** A map entry's value where the text gives none: the field's zero, as the wire reads a missing
 *  value. */
function defaultMapValue(field: DescField): unknown {
  if (field.fieldKind !== "map") throw new Error("not a map field");
  if (field.mapKind === "message") return reflect(field.message);
  if (field.mapKind === "enum") return 0;
  switch (field.scalar) {
    case ScalarType.STRING:
      return "";
    case ScalarType.BYTES:
      return new Uint8Array();
    case ScalarType.BOOL:
      return false;
    default:
      return INT64.has(field.scalar) || UINT64.has(field.scalar)
        ? BigInt(0)
        : 0;
  }
}

function enumValue(reader: Reader, desc: DescEnum, token: Token): number {
  if (token.kind === "name") {
    const value = desc.values.find((each) => each.name === token.text);
    if (value === undefined)
      reader.fail(token, `${desc.typeName} has no value ${token.text}`);
    return value.number;
  }
  // Decimal only: a decimal number reads alike in both reference parsers, which disagree on a
  // leading zero (C++ reads 011 as octal 9, Python refuses it). Hex, which both read, is refused too.
  if (token.kind !== "number" || !/^-?(0|[1-9]\d*)$/.test(token.text)) {
    return reader.fail(token, `expected a ${desc.typeName} value`);
  }
  const number = BigInt(token.text);
  if (number < INT32_MIN || number > INT32_MAX) {
    reader.fail(token, `${token.text} is out of range for an enum`);
  }
  if (
    !desc.open &&
    !desc.values.some((each) => each.number === Number(number))
  ) {
    reader.fail(token, `${desc.typeName} has no value numbered ${token.text}`);
  }
  return Number(number);
}

const INT32 = new Set([
  ScalarType.INT32,
  ScalarType.SINT32,
  ScalarType.SFIXED32,
]);
const UINT32 = new Set([ScalarType.UINT32, ScalarType.FIXED32]);
const INT64 = new Set([
  ScalarType.INT64,
  ScalarType.SINT64,
  ScalarType.SFIXED64,
]);
const UINT64 = new Set([ScalarType.UINT64, ScalarType.FIXED64]);
// BigInt calls, not literals: the build targets a version before BigInt literals.
const ZERO = BigInt(0);
const ONE = BigInt(1);
const TWO = BigInt(2);
const INT32_MIN = -(TWO ** BigInt(31));
const INT32_MAX = TWO ** BigInt(31) - ONE;

function scalarValue(
  reader: Reader,
  scalar: ScalarType,
  first: Token,
): unknown {
  if (scalar === ScalarType.STRING || scalar === ScalarType.BYTES) {
    if (first.kind !== "string")
      return reader.fail(first, "expected a quoted string");
    const pieces = [first.bytes];
    for (
      let next = reader.peek();
      next.kind === "string";
      next = reader.peek()
    ) {
      reader.next();
      pieces.push(next.bytes);
    }
    const bytes = concat(pieces);
    if (scalar === ScalarType.BYTES) return bytes;
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      return reader.fail(first, "a string field's bytes are not UTF-8");
    }
  }
  if (scalar === ScalarType.BOOL) {
    const text =
      first.kind === "name" || first.kind === "number" ? first.text : "";
    if (["true", "True", "t", "1"].includes(text)) return true;
    if (["false", "False", "f", "0"].includes(text)) return false;
    return reader.fail(first, "expected true or false");
  }
  if (scalar === ScalarType.DOUBLE || scalar === ScalarType.FLOAT) {
    if (first.kind === "name") {
      const name = first.text.toLowerCase();
      if (name === "inf" || name === "infinity")
        return Number.POSITIVE_INFINITY;
      if (name === "-inf" || name === "-infinity")
        return Number.NEGATIVE_INFINITY;
      if (name === "nan") return Number.NaN;
    }
    // Decimal only, with no leading zero: the reference parsers refuse a hex or octal float.
    if (
      first.kind !== "number" ||
      !/^-?(0|[1-9]\d*|(0|[1-9]\d*)?\.\d*)([eE][-+]?\d+)?[fF]?$/.test(
        first.text,
      )
    ) {
      return reader.fail(first, "expected a decimal number");
    }
    const value = Number(first.text.replace(/[fF]$/, ""));
    if (scalar === ScalarType.DOUBLE) return value;
    // The reference parsers read a float past float's range as infinity; that is refused here.
    const single = Math.fround(value);
    if (Number.isFinite(value) && !Number.isFinite(single)) {
      reader.fail(first, `${first.text} overflows a float`);
    }
    return single;
  }
  const value = integerValue(reader, first);
  const within = (low: bigint, high: bigint) => {
    if (value < low || value > high)
      reader.fail(first, `${value} is out of range`);
  };
  if (INT32.has(scalar)) {
    within(INT32_MIN, INT32_MAX);
    return Number(value);
  }
  if (UINT32.has(scalar)) {
    within(ZERO, TWO ** BigInt(32) - ONE);
    return Number(value);
  }
  if (INT64.has(scalar)) {
    within(-(TWO ** BigInt(63)), TWO ** BigInt(63) - ONE);
    return value;
  }
  if (UINT64.has(scalar)) {
    within(ZERO, TWO ** BigInt(64) - ONE);
    return value;
  }
  return reader.fail(first, "unsupported scalar type");
}

/** An integer token's value, read as the reference parser reads it: `0x` hex, a leading `0` octal,
 *  and decimal otherwise. `08` is neither and is refused. */
function integerValue(reader: Reader, token: Token): bigint {
  const match =
    token.kind === "number"
      ? /^(-?)(0[xX][0-9A-Fa-f]+|0[0-7]*|[1-9]\d*)$/.exec(token.text)
      : null;
  if (match === null) return reader.fail(token, "expected an integer");
  const [, sign, digits] = match;
  const magnitude =
    digits.length > 1 && digits[0] === "0" && !/[xX]/.test(digits[1])
      ? BigInt(`0o${digits.slice(1)}`)
      : BigInt(digits);
  return sign === "-" ? -magnitude : magnitude;
}

function concat(pieces: readonly Uint8Array[]): Uint8Array {
  const out = new Uint8Array(
    pieces.reduce((sum, piece) => sum + piece.length, 0),
  );
  let at = 0;
  for (const piece of pieces) {
    out.set(piece, at);
    at += piece.length;
  }
  return out;
}
