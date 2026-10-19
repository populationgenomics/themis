import { describe, expect, test } from "bun:test";
import { createHash, createHmac } from "node:crypto";
import {
  type BearerDeriver,
  crc32c,
  encodeBearer,
  hashBearer,
  MemoizedBearerDeriver,
} from "./derive";

// Byte-for-byte parity with `themis/clients/auth/derive.py`: the dispatcher
// re-derives the same bearer at spawn and auth resolves it, so the TS and
// Python derivations MUST agree. The KMS deriver and Python's `fixture_deriver`
// share the same primitive — HMAC-SHA256 then base64url-unpadded (`_encode`) — so
// proving the encoding of an HMAC over a fixed (secret, session_id) matches the
// Python output proves the encoding both derivers rely on.
//
// Reference computed once (do not regenerate casually — a change means the
// encodings diverged):
//   uv run python -c "import asyncio; from themis.clients.auth import derive; \
//     print(asyncio.run(derive.fixture_deriver(b'SECRET')('SID')))"
const SECRET = Buffer.from("SECRET", "utf8");
const SESSION_ID = "SID";
const PYTHON_REFERENCE_BEARER = "WnyhjKs_-Wc9f4pZW-H7kYKeS31QgIkgM59Au7AWXaQ";

function referenceMac(): Buffer {
  return createHmac("sha256", SECRET).update(SESSION_ID, "utf8").digest();
}

describe("session-token bearer derivation parity with derive.py", () => {
  test("base64url-unpadded HMAC-SHA256 byte-matches the Python reference", () => {
    expect(encodeBearer(referenceMac())).toBe(PYTHON_REFERENCE_BEARER);
  });

  test("encodeBearer emits unpadded base64url (no '=', '+', or '/')", () => {
    expect(encodeBearer(referenceMac())).not.toMatch(/[=+/]/);
  });

  test("hashBearer is the lowercase-hex sha256 of the bearer", () => {
    const expected = createHash("sha256")
      .update(PYTHON_REFERENCE_BEARER, "utf8")
      .digest("hex");
    expect(hashBearer(PYTHON_REFERENCE_BEARER)).toBe(expected);
    expect(hashBearer(PYTHON_REFERENCE_BEARER)).toMatch(/^[0-9a-f]{64}$/);
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
