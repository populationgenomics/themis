import { describe, expect, test } from "bun:test";
import { createHmac } from "node:crypto";
import {
  type BearerDeriver,
  crc32c,
  encodeBearer,
  hashBearer,
  MemoizedBearerDeriver,
} from "./derive";

// Byte-for-byte parity with `themis/clients/auth/derive.py`: the dispatcher
// re-derives the same bearer at spawn and auth resolves it, so the TS and
// Python derivations MUST agree. Both apply HMAC-SHA256 then base64url-unpadded
// (`_encode` in `derive.py`).
const SECRET = Buffer.from("SECRET", "utf8");
const SESSION_ID = "SID";
const PYTHON_REFERENCE_BEARER = "WnyhjKs_-Wc9f4pZW-H7kYKeS31QgIkgM59Au7AWXaQ";
const PYTHON_REFERENCE_HASH =
  "a4ed8c4eb074a2f303d30353926170ce17a79a61a79b5e1663cbbdb5d19f9839";

function referenceMac(): Buffer {
  return createHmac("sha256", SECRET).update(SESSION_ID, "utf8").digest();
}

describe("session-token bearer derivation parity with derive.py", () => {
  test("base64url-unpadded HMAC-SHA256 byte-matches the Python reference", () => {
    expect(encodeBearer(referenceMac())).toBe(PYTHON_REFERENCE_BEARER);
  });

  test("encodeBearer emits unpadded base64url (no '=', '+', or '/')", () => {
    // Standard base64 encodes 0xfb 0xff 0x03 0x04 as "+/8DBA=="; unpadded base64url is "-_8DBA".
    expect(encodeBearer(Uint8Array.of(0xfb, 0xff, 0x03, 0x04))).toBe("-_8DBA");
  });

  test("hashBearer is the lowercase-hex sha256 of the bearer", () => {
    expect(hashBearer(PYTHON_REFERENCE_BEARER)).toBe(PYTHON_REFERENCE_HASH);
  });

  // CRC32C's published check value, which `google_crc32c.value(b'123456789')`
  // also returns. Pins the Castagnoli polynomial — IEEE CRC-32 yields 0xcbf43926
  // for this input, and KMS would reject that as a mismatched request CRC. The
  // value exceeds 2^31, so matching it also proves the unsigned conversion.
  test("crc32c matches the check value KMS and google_crc32c agree on", () => {
    expect(crc32c(Buffer.from("123456789", "utf8"))).toBe(0xe3069283);
  });
});

/** A deriver that counts its calls, fails while `failing` is set, and never settles while
 *  `stalling` is set. */
class CountingDeriver implements BearerDeriver {
  calls = 0;
  failing = false;
  stalling = false;

  async deriveBearer(sessionId: string): Promise<string> {
    this.calls += 1;
    if (this.stalling) return new Promise(() => {});
    if (this.failing) throw new Error("KMS unavailable");
    return `bearer-${sessionId}`;
  }
}

describe("MemoizedBearerDeriver", () => {
  test("derives each session's bearer once", async () => {
    const inner = new CountingDeriver();
    const deriver = new MemoizedBearerDeriver(inner);
    expect(await deriver.deriveBearer("s1")).toBe("bearer-s1");
    expect(await deriver.deriveBearer("s1")).toBe("bearer-s1");
    expect(inner.calls).toBe(1);
  });

  test("a stalled derivation holds up no later call", async () => {
    const inner = new CountingDeriver();
    const deriver = new MemoizedBearerDeriver(inner);
    inner.stalling = true;
    void deriver.deriveBearer("s1");
    inner.stalling = false;
    expect(await deriver.deriveBearer("s1")).toBe("bearer-s1");
    expect(inner.calls).toBe(2);
  });

  test("keeps sessions apart", async () => {
    const deriver = new MemoizedBearerDeriver(new CountingDeriver());
    expect(await deriver.deriveBearer("s1")).toBe("bearer-s1");
    expect(await deriver.deriveBearer("s2")).toBe("bearer-s2");
  });

  test("a failed derivation is not kept: the next call retries", async () => {
    const inner = new CountingDeriver();
    const deriver = new MemoizedBearerDeriver(inner);
    inner.failing = true;
    await expect(deriver.deriveBearer("s1")).rejects.toThrow("KMS unavailable");
    inner.failing = false;
    expect(await deriver.deriveBearer("s1")).toBe("bearer-s1");
    expect(inner.calls).toBe(2);
  });
});
