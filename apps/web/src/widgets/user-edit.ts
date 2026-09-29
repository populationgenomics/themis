import { toBinary } from "@bufbuild/protobuf";
import type { FileAtCommit } from "@/workspace-copy/copy";
import {
  AssetError,
  payloadTypeName,
  readAny,
  readPayload,
  regularFileProblem,
} from "./asset";
import { userOnlyViolations } from "./ownership";
import { payloadSchema } from "./payloads";

// The write path's check on a user's edit: each file an edit replaces has to be a widget asset that
// exists at the edit's base, as a regular file, and the new bytes the same type of payload, differing
// from it in guards alone, the fields that are a user's judgements (docs/design/document-widgets.md).
// The SharedWorker runs it on every edit it publishes, so a defect in a widget cannot write a field
// that is the agent's.

function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((byte, index) => byte === b[index]);
}

/** Raise `AssetError` unless `after`, written at `path` over `before`, the file there at the edit's
 *  base, is a user's edit of a widget asset, in the one encoding this build writes of what it
 *  carries: the bytes a publish stores are the ones every reader reads alike. */
export function checkUserEdit(
  path: string,
  before: FileAtCommit | undefined,
  after: Uint8Array,
): void {
  if (before === undefined) {
    throw new AssetError(
      `${path}: a user's edit changes an existing widget asset, and there is none`,
    );
  }
  const notRegular = regularFileProblem(before.mode);
  if (notRegular !== null) throw new AssetError(`${path}: ${notRegular}`);
  const base = readAny(before.bytes);
  const next = readAny(after);
  const typeName = payloadTypeName(base);
  const schema = payloadSchema(typeName);
  if (schema === undefined) {
    throw new AssetError(
      `${path}: ${typeName} is not a widget payload this build knows`,
    );
  }
  if (payloadTypeName(next) !== typeName) {
    throw new AssetError(
      `${path}: a user's edit keeps the asset's type, ${typeName}`,
    );
  }
  const edited = readPayload(next, schema);
  if (!sameBytes(toBinary(schema, edited), next.value)) {
    throw new AssetError(
      `${path}: a user's edit is written in the encoding this build writes, and this one is not`,
    );
  }
  const problems = userOnlyViolations(
    schema,
    readPayload(base, schema),
    edited,
  );
  if (problems.length > 0) {
    throw new AssetError(`${path}: ${problems.join("; ")}`);
  }
}
