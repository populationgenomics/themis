import type { DescField, DescMessage, Message } from "@bufbuild/protobuf";
import { getOption } from "@bufbuild/protobuf";
import { type ReflectMessage, reflect } from "@bufbuild/protobuf/reflect";
import { element_key } from "@/models/widgets";
import type { EditFile } from "@/workspace-copy/protocol";
import {
  AssetError,
  assetPathProblem,
  payloadTypeName,
  readAny,
  readPayload,
  regularFileProblem,
  writePayload,
} from "./asset";
import { isGuard, judgedContent } from "./ownership";
import { payloadSchema } from "./payloads";

// A user setting one guard of a widget payload, as the edit a window publishes
// (docs/design/workbench-workspace.md, "A curator's edit is rebased onto the tip, file by file"):
// the asset as the widget was drawn from it, with that guard set and every field this build does
// not know kept. Every widget sets its guards through this, whatever its payload type: a guard is
// named by where it sits in the payload, which the schema's element keys make stable across the
// agent's rewrites. The copy lands the edit on a moved tip only while the asset there is the one the
// user saw.

/** One step from a message down to the element holding a guard: a singular message field, or an
 *  element of a list of messages, named by the value of its element key. */
export interface GuardStep {
  field: string;
  /** The element key's value, as text, where `field` is a list; absent for a singular field. */
  key?: string;
}

/** Where a guard sits: the steps from the payload's root to the element holding it, none for a
 *  guard of the root, and the guard's field name there. */
export interface GuardAddress {
  steps: readonly GuardStep[];
  guard: string;
}

/** A guard's value: a tick, or a user's text. */
export type GuardValue = boolean | string;

/** How an address reads in a commit message and a state key, e.g. `codes[POP_FRQ].reviewed`. */
export function addressName(address: GuardAddress): string {
  const steps = address.steps.map((step) =>
    step.key === undefined ? step.field : `${step.field}[${step.key}]`,
  );
  return [...steps, address.guard].join(".");
}

/** An address that cannot name a guard of the schema it is read against: a defect in the widget
 *  that made it, never something the payload's contents cause. */
export class AddressError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AddressError";
  }
}

/** The guard `address` names in `payload`, of type `schema`: the element holding it, its schema,
 *  the guard's field, its value, and whether the element was matched by its key. Undefined when the
 *  payload has no such element. Raises `AddressError` when the address does not fit the schema: a
 *  step naming no message field, a key on a singular field or none on a list, or a guard naming no
 *  plain tick or text guard of the element. A guard with presence, a proto3 `optional` or a message,
 *  is refused, since a tick or text cannot say it is unset. Raises `AssetError` when two elements of
 *  a list share the key a step names, which the payload's rules and the agent's hook both refuse. */
export function guardIn(
  schema: DescMessage,
  payload: Message,
  address: GuardAddress,
):
  | {
      element: Message;
      elementSchema: DescMessage;
      field: DescField;
      value: GuardValue;
      keyed: boolean;
    }
  | undefined {
  let holder: ReflectMessage = reflect(schema, payload);
  for (const step of address.steps) {
    const field = holder.desc.fields.find((each) => each.name === step.field);
    if (field === undefined) {
      throw new AddressError(
        `${addressName(address)}: ${holder.desc.typeName} has no field ${step.field}`,
      );
    }
    const next = stepInto(holder, field, step, address);
    if (next === null) return undefined;
    holder = next;
  }
  const field = holder.desc.fields.find((each) => each.name === address.guard);
  if (
    field === undefined ||
    !isGuard(field) ||
    field.fieldKind !== "scalar" ||
    field.proto.proto3Optional
  ) {
    throw new AddressError(
      `${addressName(address)}: ${holder.desc.typeName} has no plain scalar guard ${address.guard}`,
    );
  }
  const value = holder.get(field);
  if (typeof value !== "boolean" && typeof value !== "string") {
    throw new AddressError(
      `${addressName(address)}: the guard ${address.guard} is neither a tick nor text`,
    );
  }
  const last = address.steps[address.steps.length - 1];
  return {
    element: holder.message,
    elementSchema: holder.desc,
    field,
    value,
    keyed: last?.key !== undefined,
  };
}

/** The message a step leads to; null where the payload holds no such element. */
function stepInto(
  holder: ReflectMessage,
  field: DescField,
  step: GuardStep,
  address: GuardAddress,
): ReflectMessage | null {
  if (field.fieldKind === "message") {
    if (step.key !== undefined) {
      throw new AddressError(
        `${addressName(address)}: ${field.name} is a singular field, and a step into it names no key`,
      );
    }
    return holder.isSet(field) ? (holder.get(field) as ReflectMessage) : null;
  }
  if (field.fieldKind === "list" && field.listKind === "message") {
    if (step.key === undefined) {
      throw new AddressError(
        `${addressName(address)}: ${field.name} is a list, and a step into it names an element's key`,
      );
    }
    const key = field.message.fields.find((each) =>
      getOption(each, element_key),
    );
    if (key === undefined) {
      throw new AddressError(
        `${addressName(address)}: ${field.message.typeName} marks no element key`,
      );
    }
    const matches = [...(holder.get(field) as Iterable<ReflectMessage>)].filter(
      (element) => String(element.get(key)) === step.key,
    );
    if (matches.length > 1) {
      throw new AssetError(
        `${addressName(address)}: ${matches.length} elements of ${field.name} share the key ${step.key}`,
      );
    }
    return matches[0] ?? null;
  }
  throw new AddressError(
    `${addressName(address)}: ${field.name} is not a message field`,
  );
}

/** What the guard `address` names in `payload` judges, and its value: the element's protected
 *  content, as `judgedContent` encodes it. Undefined when the payload has no such element. */
export function judgementIn(
  schema: DescMessage,
  payload: Message,
  address: GuardAddress,
): { judged: Uint8Array; value: GuardValue } | undefined {
  const found = guardIn(schema, payload, address);
  if (found === undefined) return undefined;
  return {
    judged: judgedContent(found.elementSchema, found.element, found.field, {
      keyed: found.keyed,
    }),
    value: found.value,
  };
}

/** The guard `address` names in the asset `bytes`, read and validated as the payload type it names;
 *  undefined when the asset is no payload this build knows, fails its rules, or has no such
 *  element. An address that does not fit the payload's schema raises, as `guardIn` does. */
export function judgementInAsset(
  bytes: Uint8Array,
  address: GuardAddress,
): { judged: Uint8Array; value: GuardValue } | undefined {
  try {
    const wrapped = readAny(bytes);
    const schema = payloadSchema(payloadTypeName(wrapped));
    if (schema === undefined) return undefined;
    return judgementIn(schema, readPayload(wrapped, schema), address);
  } catch (error) {
    if (error instanceof AssetError) return undefined;
    throw error;
  }
}

/** A user's change to one guard of the asset at `path`. */
export interface SetGuard {
  /** The asset's path in the repository. */
  path: string;
  address: GuardAddress;
  value: GuardValue;
}

/** The message of the commit a guard change writes, naming the asset and the guard. */
function setGuardMessage({ path, address, value }: SetGuard): string {
  const name = addressName(address);
  if (typeof value === "boolean") {
    return `${value ? "Tick" : "Untick"} ${name} in ${path}`;
  }
  return value === "" ? `Clear ${name} in ${path}` : `Write ${name} in ${path}`;
}

/** The edit `change` makes of `asset`, a widget payload's bytes as it was drawn from a tree entry of
 *  mode `mode`: the commit message and the asset's new bytes. Undefined when the guard already has
 *  that value, so there is nothing to publish. Raises `AssetError` when the path is not one an asset
 *  may be at, the entry is not a regular file, the bytes are no payload this build knows that
 *  passes its rules, the payload has no element at the address, or the value is not of the guard's
 *  type. */
export function setGuardFile(
  asset: Uint8Array,
  mode: string,
  change: SetGuard,
): { message: string; files: EditFile[] } | undefined {
  const problem = assetPathProblem(change.path);
  if (problem !== null) throw new AssetError(`${change.path}: ${problem}`);
  const notRegular = regularFileProblem(mode);
  if (notRegular !== null)
    throw new AssetError(`${change.path}: ${notRegular}`);
  const wrapped = readAny(asset);
  const typeName = payloadTypeName(wrapped);
  const schema = payloadSchema(typeName);
  if (schema === undefined) {
    throw new AssetError(
      `${change.path}: ${typeName} is not a widget payload this build knows`,
    );
  }
  const payload = readPayload(wrapped, schema);
  const found = guardIn(schema, payload, change.address);
  if (found === undefined) {
    throw new AssetError(
      `the ${typeName} at ${change.path} has no ${addressName(change.address)}`,
    );
  }
  if (typeof found.value !== typeof change.value) {
    throw new AssetError(
      `${addressName(change.address)} holds a ${typeof found.value}, not a ${typeof change.value}`,
    );
  }
  if (found.value === change.value) return undefined;
  reflect(found.elementSchema, found.element).set(found.field, change.value);
  return {
    message: setGuardMessage(change),
    files: [{ path: change.path, bytes: writePayload(schema, payload) }],
  };
}
