import {
  clone,
  type DescField,
  type DescMessage,
  getOption,
  hasOption,
  type Message,
  ScalarType,
  toBinary,
} from "@bufbuild/protobuf";
import { type ReflectMessage, reflect } from "@bufbuild/protobuf/reflect";
import { element_key, guard } from "@/models/widgets";

// A user's judgements in a widget payload (docs/design/document-widgets.md): a field the `guard`
// option marks is a user's judgement on the other fields of its message, every other field the
// agent's. The rule an agent's change keeps is themis/widgets/ownership.py's, applied where the
// agent's pushes pass. What the browser checks is the other direction, a user's own edit:
// `userOnlyViolations` holds it to changing guards alone. `checkSchema` refuses a schema neither
// reading can use, as the Python one does; both are held to the schemas in
// ownership-cases.test-support.json.

/** A payload schema whose judgements cannot be read; the message names the field and the rule. */
export class OwnershipSchemaError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "OwnershipSchemaError";
  }
}

export function isGuard(field: DescField): boolean {
  return hasOption(field, guard);
}

function isKey(field: DescField): boolean {
  return getOption(field, element_key);
}

function messageOf(field: DescField): DescMessage | undefined {
  if (field.fieldKind === "message") return field.message;
  if (field.fieldKind === "list" && field.listKind === "message")
    return field.message;
  if (field.fieldKind === "map" && field.mapKind === "message")
    return field.message;
  return undefined;
}

/** Whether `desc`, or any message beneath it, declares a guard. */
export function holdsGuards(
  desc: DescMessage,
  seen: ReadonlySet<string> = new Set(),
): boolean {
  if (seen.has(desc.typeName)) return false;
  const inner = new Set([...seen, desc.typeName]);
  return desc.fields.some((field) => {
    if (isGuard(field)) return true;
    const child = messageOf(field);
    return child !== undefined && holdsGuards(child, inner);
  });
}

/** The message beneath `field` the rule descends into: a message field, not itself a guard, whose
 *  message holds a guard. */
function descended(field: DescField): DescMessage | undefined {
  if (isGuard(field)) return undefined;
  const child = messageOf(field);
  return child !== undefined && holdsGuards(child) ? child : undefined;
}

// What an element may be matched by, as themis/widgets/ownership.py admits: text, integers, truth.
const KEY_SCALARS = new Set<ScalarType>([
  ScalarType.STRING,
  ScalarType.BOOL,
  ScalarType.INT32,
  ScalarType.INT64,
  ScalarType.UINT32,
  ScalarType.UINT64,
  ScalarType.SINT32,
  ScalarType.SINT64,
  ScalarType.FIXED32,
  ScalarType.FIXED64,
  ScalarType.SFIXED32,
  ScalarType.SFIXED64,
]);

/** Raise `OwnershipSchemaError` unless `desc` and every message beneath it can be read: no guard
 *  that is a map, shares a oneof with a field that is not a guard, or ignores a name that is not a
 *  field of its message, is its element key or a guard, or is listed twice; no map whose values
 *  hold guards; every list whose elements hold them keyed by exactly one singular string,
 *  integer or bool field outside any oneof, and not a guard; and no field beneath `desc` of a
 *  closed enum. */
export function checkSchema(desc: DescMessage): void {
  checkGuards(desc);
  checkEnums(desc);
}

function checkGuards(
  desc: DescMessage,
  seen: ReadonlySet<string> = new Set(),
): void {
  if (seen.has(desc.typeName)) return;
  const inner = new Set([...seen, desc.typeName]);
  for (const field of desc.fields) {
    const name = `${desc.typeName}.${field.name}`;
    if (isGuard(field)) {
      checkGuard(desc, field, name);
      continue;
    }
    const element = descended(field);
    if (element === undefined) continue;
    if (field.fieldKind === "map") {
      throw new OwnershipSchemaError(
        `${name} is a map whose values hold guards, which the rule does not read`,
      );
    }
    if (field.fieldKind === "list") checkKey(name, element);
    checkGuards(element, inner);
  }
}

function checkEnums(
  desc: DescMessage,
  seen: ReadonlySet<string> = new Set(),
): void {
  if (seen.has(desc.typeName)) return;
  const inner = new Set([...seen, desc.typeName]);
  for (const field of desc.fields) {
    const enumType =
      field.fieldKind === "enum" ||
      (field.fieldKind === "list" && field.listKind === "enum") ||
      (field.fieldKind === "map" && field.mapKind === "enum")
        ? field.enum
        : undefined;
    if (enumType !== undefined && !enumType.open) {
      throw new OwnershipSchemaError(
        `${desc.typeName}.${field.name} is of the closed enum ${enumType.typeName}, which keeps a value a build does not know apart from its field; a payload's enums have to be open`,
      );
    }
    const message =
      field.fieldKind === "message" ||
      (field.fieldKind === "list" && field.listKind === "message") ||
      (field.fieldKind === "map" && field.mapKind === "message")
        ? field.message
        : undefined;
    if (message !== undefined) checkEnums(message, inner);
  }
}

function checkGuard(desc: DescMessage, field: DescField, name: string): void {
  if (field.fieldKind === "map") {
    throw new OwnershipSchemaError(
      `${name} is a map, and a map may not be a guard`,
    );
  }
  // protobuf-es leaves the synthetic oneof of a proto3 `optional` field off `oneof`, which holds the
  // guard alone.
  const oneof = field.oneof;
  if (oneof !== undefined && !oneof.fields.every(isGuard)) {
    throw new OwnershipSchemaError(
      `${name} is a guard sharing the oneof ${oneof.name} with a field that is not one`,
    );
  }
  const ignores = getOption(field, guard).ignores;
  for (const ignored of ignores) {
    const target = desc.fields.find((each) => each.name === ignored);
    if (target === undefined) {
      throw new OwnershipSchemaError(
        `${name} ignores ${ignored}, which is not a field of ${desc.typeName}`,
      );
    }
    if (isKey(target)) {
      throw new OwnershipSchemaError(
        `${name} ignores ${ignored}, the element key, which no guard judges anyway`,
      );
    }
    if (isGuard(target)) {
      throw new OwnershipSchemaError(
        `${name} ignores ${ignored}, a guard, which no guard judges anyway`,
      );
    }
    if (ignores.filter((each) => each === ignored).length > 1) {
      throw new OwnershipSchemaError(
        `${name} ignores ${ignored} more than once`,
      );
    }
  }
}

function checkKey(list: string, element: DescMessage): void {
  const keys = element.fields.filter(isKey);
  if (keys.length !== 1) {
    throw new OwnershipSchemaError(
      `${list} lists ${element.typeName} elements holding guards, so ${element.typeName} has to mark exactly one field element_key; it marks ${keys.length}`,
    );
  }
  const [key] = keys;
  if (
    key.fieldKind !== "scalar" ||
    !KEY_SCALARS.has(key.scalar) ||
    isGuard(key) ||
    key.oneof !== undefined ||
    // protobuf-es leaves the synthetic oneof of a proto3 `optional` field off `oneof`.
    key.proto.proto3Optional
  ) {
    throw new OwnershipSchemaError(
      `${element.typeName}.${key.name} is a key, and has to be a singular string, integer or bool field outside any oneof, and not a guard`,
    );
  }
}

/** What a user's edit from `base` to `next` changes beyond guards: every other field, every key,
 *  every element and every unknown field has to be as it was, compared by their wire encoding.
 *  Empty when the edit changes guards alone. */
export function userOnlyViolations(
  schema: DescMessage,
  base: Message,
  next: Message,
): string[] {
  checkSchema(schema);
  const before = toBinary(schema, withoutGuards(schema, base));
  const after = toBinary(schema, withoutGuards(schema, next));
  return sameBytes(before, after)
    ? []
    : [`the edit changes ${schema.typeName} beyond a user's judgements`];
}

/** What the guard `judge` of `message` judges: the encoding of every field of the message but the
 *  fields `judge` ignores and, where `keyed`, the key the message was matched by as an element of a
 *  list, with every guard at any depth at its default and every list whose elements are matched by
 *  key in key order, since a list's order is not part of what is judged. The root of a payload and
 *  a singular message are not `keyed`: they judge a key field like any other. Raises
 *  `OwnershipSchemaError` on a schema `checkSchema` refuses. */
export function judgedContent(
  schema: DescMessage,
  message: Message,
  judge: DescField,
  { keyed }: { keyed: boolean },
): Uint8Array {
  if (!isGuard(judge) || !schema.fields.includes(judge)) {
    throw new Error(`${judge.name} is not a guard of ${schema.typeName}`);
  }
  checkSchema(schema);
  const judged = clone(schema, message);
  strip(reflect(schema, judged), { sortKeyed: true });
  const holder = reflect(schema, judged);
  const ignores = new Set(getOption(judge, guard).ignores);
  for (const field of schema.fields) {
    if ((keyed && isKey(field)) || ignores.has(field.name)) holder.clear(field);
  }
  return toBinary(schema, judged);
}

function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((byte, index) => byte === b[index]);
}

function withoutGuards(schema: DescMessage, message: Message): Message {
  const copy = clone(schema, message);
  strip(reflect(schema, copy), { sortKeyed: false });
  return copy;
}

/** Clear every guard beneath `holder`, and with `sortKeyed` put every list whose elements hold
 *  guards in key order, as themis/widgets/ownership.py's `_strip` does before it compares. A user's
 *  edit is compared unsorted: the browser writes the order it read. */
function strip(
  holder: ReflectMessage,
  { sortKeyed }: { sortKeyed: boolean },
): void {
  for (const field of holder.desc.fields) {
    if (isGuard(field)) {
      holder.clear(field);
      continue;
    }
    const element = descended(field);
    if (element === undefined) continue;
    if (field.fieldKind === "list") {
      for (const item of holder.get(field) as Iterable<ReflectMessage>)
        strip(item, { sortKeyed });
      if (sortKeyed) sortByKey(holder.message, field, element);
    } else if (field.fieldKind === "message" && holder.isSet(field)) {
      strip(holder.get(field) as ReflectMessage, { sortKeyed });
    }
  }
}

function sortByKey(
  message: Message,
  list: DescField,
  element: DescMessage,
): void {
  const key = element.fields.find(isKey);
  if (key === undefined) {
    throw new OwnershipSchemaError(
      `${element.typeName} holds guards in a list and marks no element key`,
    );
  }
  const items = (message as unknown as Record<string, Message[]>)[
    list.localName
  ];
  const value = (item: Message) =>
    (item as unknown as Record<string, string | number | bigint | boolean>)[
      key.localName
    ];
  items.sort((a, b) =>
    value(a) < value(b) ? -1 : value(a) > value(b) ? 1 : 0,
  );
}
