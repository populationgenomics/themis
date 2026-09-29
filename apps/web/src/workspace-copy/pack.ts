import { bytesHex } from "./git-objects";

// The two headers hydration reads: how many objects a pack declares, and how many its index holds.

const PACK_MAGIC = [0x50, 0x41, 0x43, 0x4b]; // "PACK"
const INDEX_MAGIC = [0xff, 0x74, 0x4f, 0x63]; // "\377tOc", version 2 and later

/** Lowercase hex SHA-256 of `bytes`: the id sheaf names a pack by. */
export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as BufferSource);
  return bytesHex(new Uint8Array(digest));
}

/** The object count a pack's header declares (bytes 8–11, big-endian). */
export function packObjectCount(pack: Uint8Array): number {
  if (pack.length < 12 || !PACK_MAGIC.every((byte, i) => pack[i] === byte)) {
    throw new Error("not a git pack: the header is missing");
  }
  const version = readUint32(pack, 4);
  if (version !== 2 && version !== 3) {
    throw new Error(
      `a git pack of version ${version}, which git does not write`,
    );
  }
  return readUint32(pack, 8);
}

/** The object count a version-2 pack index holds: the last entry of its fan-out table. */
export function indexObjectCount(index: Uint8Array): number {
  if (
    index.length < 8 + 256 * 4 ||
    !INDEX_MAGIC.every((byte, i) => index[i] === byte)
  ) {
    throw new Error("not a version-2 git pack index");
  }
  if (readUint32(index, 4) !== 2) {
    throw new Error(`a git pack index of version ${readUint32(index, 4)}`);
  }
  return readUint32(index, 8 + 255 * 4);
}

function readUint32(bytes: Uint8Array, at: number): number {
  return new DataView(bytes.buffer, bytes.byteOffset + at, 4).getUint32(0);
}
