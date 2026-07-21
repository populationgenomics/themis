import { createHash } from "node:crypto";
import { KeyManagementServiceClient } from "@google-cloud/kms";
import { CRC32C } from "@google-cloud/storage";
import type { KmsConfig } from "./config";

// Derive a session's per-session bearer by MAC-signing its id, byte-identical to
// `themis/clients/auth/derive.py` (the dispatcher re-derives the same bearer at
// spawn and the store resolves it, so the two implementations MUST agree):
// `bearer = base64url_unpadded(HMAC_SHA256(key, session_id))` via a Cloud KMS MAC
// key whose material never leaves KMS. We store only `sha256(bearer)`; the
// plaintext bearer is handed to Anthropic and never persisted.

/** Base64url-encode a MAC without padding — the shared encoding both the KMS and
 *  the Python fixture derivers apply (`_encode` in derive.py: urlsafe b64 with
 *  `=` stripped). Node's `base64url` output is already unpadded. */
export function encodeBearer(mac: Uint8Array): string {
  return Buffer.from(mac).toString("base64url");
}

/** The lowercase-hex SHA-256 of the bearer — the value persisted in
 *  `session_context.token_hash`; the plaintext bearer is never stored. */
export function hashBearer(bearer: string): string {
  return createHash("sha256").update(bearer, "utf8").digest("hex");
}

/** CRC32C of a buffer as the unsigned 32-bit integer KMS's `Int64Value` carries.
 *  `CRC32C.valueOf()` is signed; `>>> 0` reinterprets it unsigned, matching
 *  `google_crc32c.value` on the Python side. */
export function crc32c(data: Buffer): number {
  const digest = new CRC32C();
  digest.update(data);
  return digest.valueOf() >>> 0;
}

/** Derives session bearers by MAC-signing each session id through the pinned
 *  Cloud KMS MAC key version. The client is lazy so constructing the deriver
 *  needs no credentials (only `deriveBearer` reaches KMS). */
export class KmsSessionTokenDeriver {
  private client?: KeyManagementServiceClient;

  constructor(private readonly config: KmsConfig) {}

  private kms(): KeyManagementServiceClient {
    if (!this.client) {
      this.client = new KeyManagementServiceClient();
    }
    return this.client;
  }

  async deriveBearer(sessionId: string): Promise<string> {
    const data = Buffer.from(sessionId, "utf8");
    const [response] = await this.kms().macSign({
      name: this.config.sessionTokenKeyVersion,
      data,
      dataCrc32c: { value: crc32c(data) },
    });
    const mac = response.mac;
    if (mac === undefined || mac === null || typeof mac === "string") {
      throw new Error(
        `KMS macSign returned no binary MAC for ${this.config.sessionTokenKeyVersion}`,
      );
    }
    if (!response.verifiedDataCrc32c) {
      throw new Error(
        "KMS did not verify the request-data CRC — corrupted in transit",
      );
    }
    // gax hands an Int64Value back as a number, a string, or a Long depending on
    // codec settings; `toString` normalises all three.
    const macCrc32c = response.macCrc32c?.value;
    if (
      macCrc32c === undefined ||
      macCrc32c === null ||
      Number(macCrc32c.toString()) !== crc32c(Buffer.from(mac))
    ) {
      throw new Error("KMS MAC response CRC mismatch — corrupted in transit");
    }
    return encodeBearer(mac);
  }
}
