import "@typespec/protobuf";

using TypeSpec.Protobuf;

// A Source is a primary-artifact lineage — "the pdf", "the jats-xml", a "scraped-html"
// body converted to xml. The handle is stable identity across updates; revisions are
// append-only (latest captured_at = current). Licence and access live here: they vary
// between lineages (a CC-BY xml vs a restricted pdf) but are stable across a lineage's
// revisions. The converter is not part of the lineage — a Rendering records which
// converter produced it. See docs/adr/0002-manifest-renderings-and-reference-model.md.
namespace Litcache;

// Proto enums are integer with a zero-valued `*_unspecified` sentinel (0 = unset, never a
// real value; the writer always sets a real member — enforced in the hand-written layer).

enum LicenceBasis {
  licence_basis_unspecified: 0,
  artifact: 1, // read from the fetched bytes (JATS <license>, Elsevier metadata)
  asserted: 2, // bytes carry no licence: an access authority supplies it (Unpaywall's
  // OA determination), or the work's resolved terms are asserted onto a seed pdf
}

enum SourceKind {
  source_kind_unspecified: 0,
  pmc_oa_s3: 1,
  europe_pmc: 2,
  elsevier_oa: 3,
  biorxiv: 4,
  upload: 5,
  seed: 6,
}

// Scraped html is converted to xml upstream and enters as media_type xml under a distinct
// handle — html is not a media type here.
enum SourceFormat {
  source_format_unspecified: 0,
  xml: 1,
  pdf: 2,
}

enum Converter {
  converter_unspecified: 0,
  litdown: 1,
  docling: 2,
  llm_ocr: 3, // vision-model OCR of the pdf — preferred pdf route; docling is the legacy fallback
}

// access-iff-publisher is NOT structural: @typespec/protobuf emits no oneof, so Access is
// flat — a `string access` discriminant + optional `publisher`, present iff
// access == "licensed". The invariant is enforced in the hand-written writer layer, not the
// shape (ADR 0003). access values: "free-to-read" | "licensed" | "institution-captured" |
// "unknown" (a string field, so hyphenated values are fine; membership checked in code).
model Access {
  @field(1) access: string;
  @field(2) publisher?: string;
}

// One fetched byte-set of a lineage. Content-addressed at sources/{handle}/{hex}.{ext};
// current = latest captured_at, never array order.
model Revision {
  @field(1) hash: string; // sha256 hex digest of the raw source bytes
  @field(2) origin_url?: string; // external provenance (the OA fetch URL); omitted for seed/upload
  @field(3) kind: SourceKind;
  @field(4) captured_at: WellKnown.Timestamp;
  @field(5) has_text_layer?: boolean; // pdf only: the pdf carries a recoverable text layer (pypdfium2
  // extracts positioned glyphs) — enables quote→bbox; omitted for xml
}

model Source {
  @field(1) handle: string; // lineage identity, stable across updates: "pdf" | "jats-xml" | "scraped-html"
  @field(2) media_type: SourceFormat;
  @field(3) licence: string; // raw, as litfetch returned it (not an SPDX id)
  @field(4) licence_basis: LicenceBasis;
  @field(5) access: Access;
  // append-only, ordered by captured_at; current = latest. Non-empty enforced in the
  // hand-written layer (@typespec/protobuf emits no protovalidate).
  @field(6) revisions: Revision[];
}

// Markdown derived from one revision via one route. Content-addressed at renderings/{hex}.md
// (the map key in Manifest.renderings is that hash). Fidelity is not stored — it is a
// read-path preference over (media_type, converter).
model Rendering {
  @field(1) from_source: string; // Source.handle (an open id namespace, not an enum)
  @field(2) from_revision: string; // the Revision.hash this markdown was produced from
  @field(3) converter: Converter;
  @field(4) converter_version: string; // the converter tool/harness version
  @field(5) `model`?: string; // free-text LLM id (e.g. "claude-opus-4-8"); required iff converter == llm_ocr
  @field(6) created_at: WellKnown.Timestamp;
}
