import { describe, expect, test } from "bun:test";
import { loadEvidenceConfig } from "./config";
import { corpusObject } from "./literature";

// The live adapter's gRPC + GCS signing is exercised end-to-end against the deployed service. What is
// offline-checkable here is the corpus-bucket pin — the confused-deputy control on the evidence
// service's resolution — extracted as the pure `corpusObject`. The serving (signing, egress, 302) is
// `signedRedirect`'s, covered in content.test.ts.
describe("corpusObject", () => {
  const CORPUS = "cpg-themis-dev-fulltext";

  test("parses the gs:// location into a bucket/object content ref", () => {
    const obj = corpusObject(
      {
        gcsUri: `gs://${CORPUS}/papers/x/paper.pdf`,
        mediaType: "application/pdf",
      },
      CORPUS,
    );
    expect(obj.bucket).toBe(CORPUS);
    expect(obj.object).toBe("papers/x/paper.pdf");
    expect(obj.mediaType).toBe("application/pdf");
  });

  test("carries the download name through to the ref", () => {
    const obj = corpusObject(
      { gcsUri: `gs://${CORPUS}/f/deadbeef`, mediaType: "text/csv" },
      CORPUS,
      "supp.csv",
    );
    expect(obj.downloadName).toBe("supp.csv");
  });

  test("refuses an object outside the corpus bucket (a service bug must not reach another bucket)", () => {
    expect(() =>
      corpusObject(
        { gcsUri: "gs://tenant-working-docs/secret", mediaType: "text/plain" },
        CORPUS,
      ),
    ).toThrow("outside the corpus bucket");
  });

  test("rejects a non-gs:// content URI", () => {
    expect(() =>
      corpusObject(
        { gcsUri: "https://evil/x", mediaType: "text/plain" },
        CORPUS,
      ),
    ).toThrow("non-gs://");
  });
});

// Config still fails closed on a missing service URL / bucket rather than building a transport with
// no target.
describe("loadEvidenceConfig", () => {
  test("reads the evidence service URL and the corpus bucket", () => {
    expect(
      loadEvidenceConfig({
        THEMIS_EVIDENCE_URL: "https://evidence.example",
        THEMIS_FULLTEXT_BUCKET: "cpg-themis-dev-fulltext",
      }),
    ).toEqual({
      evidenceUrl: "https://evidence.example",
      corpusBucket: "cpg-themis-dev-fulltext",
    });
  });

  test("fails loud when the URL is unset", () => {
    expect(() => loadEvidenceConfig({ THEMIS_FULLTEXT_BUCKET: "b" })).toThrow(
      "THEMIS_EVIDENCE_URL",
    );
  });

  test("fails loud when the corpus bucket is unset", () => {
    expect(() =>
      loadEvidenceConfig({ THEMIS_EVIDENCE_URL: "https://evidence.example" }),
    ).toThrow("THEMIS_FULLTEXT_BUCKET");
  });
});
