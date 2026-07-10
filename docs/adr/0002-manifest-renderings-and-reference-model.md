# ADR 0002: Per-source revisions, content-addressed renderings, the quote-reference model

**Status:** Accepted; superseded in part by [`0003-serialization-posture.md`](0003-serialization-posture.md). Defines
the manifest's source/rendering model and the quote-durable cite model that the KU layer (deferred) anchors against.
Realized in `schema/litcache/source.tsp` + `schema/litcache/manifest.tsp` and the writer. **The structural model here
stands; only its serialization moved:** the manifest is a binary proto message (`manifest.pb`), not closed JSON, and
`Access` is modelled flat (a `string access` field + optional `publisher`, the iff-invariant enforced by
`protovalidate`) rather than a structural named union — `@typespec/protobuf` emits no `oneof` (ADR 0003). The JSON below
is a *rendering* of the binary form, not the at-rest artifact.

## Context

The manifest must capture, as simply as the domain allows:

1. **Which files are the primary paper artifact** (pdf, jats-xml, or an xml conversion of scraped html) vs which are
   **supplementary** (figures, tables, data files).
1. **Primary artifacts may be updated** under stable identifiers -- a PMC reissue (`PMC2992036.1` -> `.2`), a publisher
   erratum -- without the DOI/PMID/PMCID changing.
1. **Artifacts are rendered into markdown** via various routes; the **source + route determine the fidelity** of the
   markdown -- a faithful reproduction (xml→litdown) or approximate (pdf→ocr).
1. **Primary artifacts refer to secondary ones** -- jats-xml links figure images by href.
1. The system **persists verbatim quotes against markdown**. A quote (a) **may reference an older revision** of a file
   and (b) **must be upgradeable** to the current revision.
1. **Licensing recorded at the level it actually exists** -- per source lineage (a PMC-OA `jats-xml` and a publisher
   `pdf` of the same paper routinely carry different terms; terms are stable across re-fetches of one file).

The update axis is **per file**, not per paper: a corrected figure or a reissued xml changes one artifact. Licence, too,
is **per file lineage**, not per paper. The model puts both on the file lineage.

## Decision

The manifest carries `sources` (primary-artifact lineages) and a content-addressed `renderings` map. No paper-wide
snapshot axis.

1. **`Source` is a primary-artifact lineage**, keyed by a stable `handle` (`"pdf"`, `"jats-xml"`, `"scraped-html"` --
   lineage identity, distinct from `media_type` since two lineages can share a media type). It carries `licence` /
   `licence_basis` / `access` and an **append-only `revisions[]`**.
1. **`Revision` is one fetched byte-set** of that lineage: `{hash, origin_url?, kind, captured_at, has_text_layer?}`.
   **Current = latest `captured_at`** -- the recency signal is the timestamp, never array order.
1. **`renderings` is a content-addressed map** keyed by the markdown's content hash (a bare sha256 hex digest). Value =
   `{from_source (handle), from_revision (hash), converter, converter_version, created_at}`. The key is identity +
   integrity at once; the blob lives at `renderings/{hex}.md` (no `path` field). Re-rendering appends a
   `renderings[new_hash]` entry; old hashes still resolve old cites (req 5a); dedup is free; resolution is O(1).
1. **Fidelity is a read-path policy, not a stored field** (req 3). A rendering records only the raw facts: its
   `from_source` (hence `media_type`) and `converter`. Whether a route is high-fidelity (xml→litdown -- safe to quote,
   and worth showing a reader in place of the pdf) or approximate (pdf→ocr) is a preference over
   `(media_type, converter)` applied at read time, curator-overridable -- not a `quality_tag` baked into the manifest,
   which would only relabel `media_type`. Nor a provenance/verification axis: every quote is human-checked regardless,
   so a per-quote trust flag carries no signal.
1. **No stored default rendering.** The canonical rendering is *derived* at read time: the highest-fidelity route (the
   `(media_type, converter)` preference above) on the latest revision of that source. A stored hash would conflate the
   stable **route** choice with **revision** recency and go stale on every re-render. The manifest records facts (what
   renderings exist, from what); the read tool applies the preference policy. Cite resolution never consults a default
   -- an anchor's `document_id` is the exact rendering hash served, recorded in the KU record.
1. **`files` (supplementary) is a list** doubling as the lazy-fetch registry of known-but-unfetched files (which have
   **no hash yet**, so `path` absent until fetched). Primary→secondary links (req 4) resolve by href→name at render
   time; a `references` index is materialised only if the read path needs it -- deferred.
1. **Everything fetched/derived is content-addressed**, path derived from handle/hash/media-type: sources at
   `sources/{handle}/{hex}.{ext}`, renderings at `renderings/{hex}.md`, supplementary at `supplementary/{hex}.{ext}`.

### Licence lives on the source lineage

`licence` (raw, as litfetch returned it -- not an SPDX id), `licence_basis`, and `access` sit on `Source`: they describe
where a source came from and under what terms -- constant across its revisions, varying between the pdf and the xml of
one paper. `licence_basis` records **where the licence string came from**, not how trustworthy it is: `artifact` = read
out of the fetched bytes (a JATS `<license>` element, Elsevier metadata); `asserted` = the bytes carry no licence, so an
access authority supplies it (Unpaywall's OA determination for the work) or we assert the work's resolved terms onto a
retained seed pdf that embeds none. litfetch returns both the raw string and the basis; litcache stores them verbatim.
Supplementary `files` carry no licence for now (not served as standalone evidence); add it if that changes.

## Quote-reference model

The cite model the KU layer (deferred) anchors against. The manifest side (rendering hashes) is settled; the anchor and
offsets-cache shapes below are provisional (see [Reference / anchor types](#reference--anchor-types)).

- **Durable anchor** (write-once, in the KU record): `{paper_id, document_id, quote, exact}`, where `document_id` is a
  **rendering hash** (you quote text, which is a rendering). No offsets -- they go stale on re-render; the verbatim
  `quote` is the durable boundary-side anchor. The quote is not immortal -- if the source text itself changes it may no
  longer appear -- but it degrades *detectably* (`exact -> fuzzy -> unlocatable`), where offsets would silently
  misalign; and a re-alignment that still finds the text keeps the anchor usable.
- **Exported quote-stripped** as `{paper_id, document_id, ref_id}` (the quote stays in the KU record; stripping keeps
  shares free of copyrightable text).
- **Resolved-offsets cache** (derived, recomputable, mutable): `(ref, rendering) -> spans`, with
  `status: exact | fuzzy | unlocatable`. Any `start`/offset hint lives **only here**, never in the durable anchor.
  Re-alignment moves the status either way -- a `fuzzy` hit can recover to `exact` against a better rendering, caught
  before it degrades to `unlocatable`.
- **Resolution walk:** `document_id` (rendering hash) -> `renderings[hash]` -> `from_source` (handle) + `from_revision`
  -> `Source.revisions[hash]` bytes -> re-align the quote (bbox via the pdf character layer iff `has_text_layer`; else
  map into the xml/text).
- **Upgrade across revisions (req 5b):** quote -> rendering -> `(from_source, converter)`; look up that source's
  **latest** revision; if newer than `from_revision`, re-render the same route over the latest revision and realign the
  verbatim quote (exact -> fuzzy -> unlocatable). `unlocatable` is the explicit "source changed under me" signal.
- **Two mint paths, one model:** the KU extractor aligns its quote offline. Agent cite-back: the read tool serves
  markdown **bundled with** its rendering hash; on the agent's quote it **verifies verbatim** against the served bytes
  (rejecting hallucinated quotes), mints `{paper, document, quote}`, and seeds the offsets cache for free (rendering
  known and current).

## Schema

The TypeSpec, realized in `schema/litcache/source.tsp` (`Source`, `Revision`, `Rendering`, `Access`, the enums) and
`schema/litcache/manifest.tsp` (`Manifest`, `AssociatedFile`).

```tsp
// source.tsp: a primary-artifact lineage with append-only revisions.
enum LicenceBasis {
  artifact: "artifact"; // read from the fetched bytes (JATS <license>, Elsevier metadata)
  asserted: "asserted"; // bytes carry no licence, so an access authority supplies it
                        // (e.g. Unpaywall's OA determination), or we assert the work's
                        // resolved terms onto a retained seed pdf that carries none
}

model FreeToRead { access: "free-to-read"; }
model Licensed { access: "licensed"; publisher: string; }
model InstitutionCaptured { access: "institution-captured"; }
model UnknownAccess { access: "unknown"; }
union Access { FreeToRead, Licensed, InstitutionCaptured, UnknownAccess }

enum SourceKind {
  pmc_oa_s3: "pmc_oa_s3"; europe_pmc: "europe_pmc"; elsevier_oa: "elsevier_oa";
  biorxiv: "biorxiv"; upload: "upload"; seed: "seed";
}
// Scraped html is converted to xml upstream and enters as media_type xml under a
// distinct handle (e.g. "scraped-html") — so html is not a media type here.
enum SourceFormat { xml: "xml"; pdf: "pdf"; }
// llm-ocr (vision-model OCR of the pdf) is the preferred pdf route; docling is the
// legacy fallback, retained so its existing renderings still resolve.
enum Converter { litdown: "litdown"; docling: "docling"; llm_ocr: "llm-ocr"; }

// One fetched byte-set of a lineage. Blob at sources/{handle}/{hex}.{ext}.
model Revision {
  hash: string;              // sha256 hex digest of the raw bytes
  origin_url?: string;       // external provenance; omitted for seed/upload
  kind: SourceKind;          // provenance of THIS fetch
  captured_at: utcDateTime;  // recency signal — NOT array order
  has_text_layer?: boolean; // pdf only: the pdf carries a recoverable text layer
                             // (pypdfium2 extracts positioned glyphs) — enables quote→bbox
}

// A primary-artifact lineage. The handle is stable identity across updates.
model Source {
  handle: string;            // lineage identity — an open id namespace, NOT an enum:
                             // e.g. "pdf", "jats-xml", "scraped-html"
  media_type: SourceFormat;
  licence: string;           // raw, as litfetch returned it (not an SPDX id)
  licence_basis: LicenceBasis;
  access: Access;            // named union — publisher required iff licensed
  revisions: Revision[];     // append-only, ordered by captured_at; last = current
}

// Markdown derived from one revision via one route. Blob at renderings/{hex}.md.
model Rendering {
  from_source: string;       // Source.handle (open id namespace, not an enum)
  from_revision: string;     // the Revision.hash it rendered
  converter: Converter;
  converter_version: string; // the converter tool/harness version
  model?: string;            // free-text LLM id, e.g. "claude-opus-4-8";
                             // required iff converter == llm-ocr (writer-enforced)
  created_at: utcDateTime;
}
```

```tsp
// manifest.tsp: sources + a content-addressed renderings map.
enum AssociatedFileRole { figure: "figure"; supplementary: "supplementary"; }
model AssociatedFile {
  role: AssociatedFileRole;
  name: string;
  source_url?: string;
  path?: string;             // supplementary/{hex}.{ext}; absent until fetched
}
model Manifest {
  doc_id: string;            // uuid4, == directory name
  external_ids: ExternalIds;
  claim_key: string;
  equivalence: Equivalence;
  retraction: Retraction;
  sources: Source[];                 // primary artifacts
  renderings: Record<Rendering>;     // key = markdown content hash (sha256 hex)
  files: AssociatedFile[];           // supplementary registry (lazy fetch)
}
```

**Bare sha256 hex everywhere.** The `hash` fields, the `renderings` map keys, and the on-disk filename stems are all the
same bare sha256 hex digest -- no prefix, no transform between manifest and path. sha256 is the fixed content-address
algorithm, defined once in `litcache/hashing.py`; a future migration would version the manifest and mark the algorithm
on both the fields and the filenames together.

## Example manifest

An extraction under this model. The pdf is a seed (no embedded licence, so `licence_basis: asserted`); the xml carries a
JATS `<license>` (`artifact`) -- illustrating per-source `licence_basis` variation. Single revision each. The second
rendering (llm-ocr of the pdf, carrying `model`) is **fabricated** to show the shape and the preference order in play.

```json
{
  "doc_id": "bed7486a-69e9-4a5a-b4f3-a4de08341ab0",
  "external_ids": {
    "doi": "10.1186/1471-2156-11-102",
    "pmid": "21070663",
    "pmcid": null,
    "arxiv": null,
    "biorxiv": null
  },
  "claim_key": "doi:10.1186/1471-2156-11-102",
  "equivalence": { "edges": [], "canonical_doc_id": "bed7486a-69e9-4a5a-b4f3-a4de08341ab0" },
  "retraction": { "retracted": false, "source": null, "date": null },
  "sources": [
    {
      "handle": "pdf",
      "media_type": "pdf",
      "licence": "http://creativecommons.org/licenses/by/2.0",
      "licence_basis": "asserted",
      "access": { "access": "free-to-read" },
      "revisions": [
        {
          "hash": "be1f931f0cc02dcd505851469627ddc80bdfa25773eb98548d94f35e45344891",
          "origin_url": null,
          "kind": "seed",
          "captured_at": "2026-06-29T06:01:23.412321Z",
          "has_text_layer": null
        }
      ]
    },
    {
      "handle": "jats-xml",
      "media_type": "xml",
      "licence": "http://creativecommons.org/licenses/by/2.0",
      "licence_basis": "artifact",
      "access": { "access": "free-to-read" },
      "revisions": [
        {
          "hash": "647ab726d771e3cb112145093a6211be9d2e548ac7b0ece469af7625f0f1ea1a",
          "origin_url": "https://pmc-oa-opendata.s3.amazonaws.com/PMC2992036.1/PMC2992036.1.xml",
          "kind": "pmc_oa_s3",
          "captured_at": "2026-06-29T06:01:23.412321Z",
          "has_text_layer": null
        }
      ]
    }
  ],
  "renderings": {
    "dd4306e549f89ed2b95b25f3eb6ee2f6fc813a90ac55392c2cab2ff1ae0724a7": {
      "from_source": "jats-xml",
      "from_revision": "647ab726d771e3cb112145093a6211be9d2e548ac7b0ece469af7625f0f1ea1a",
      "converter": "litdown",
      "converter_version": "0.3.0",
      "created_at": "2026-06-29T06:01:23.412321Z"
    },
    "1111111111111111111111111111111111111111111111111111111111111111": {
      "from_source": "pdf",
      "from_revision": "be1f931f0cc02dcd505851469627ddc80bdfa25773eb98548d94f35e45344891",
      "converter": "llm-ocr",
      "converter_version": "1.0.0",
      "model": "claude-opus-4-8",
      "created_at": "2026-06-29T06:01:23.412321Z"
    }
  },
  "files": [
    {
      "role": "supplementary",
      "name": "1471-2156-11-102-1.jpg",
      "source_url": "https://pmc-oa-opendata.s3.amazonaws.com/PMC2992036.1/1471-2156-11-102-1.jpg",
      "path": "supplementary/a156b981e30d5234b7a8320f62ccddd21816b9dfacafc5634d75ccb272559877.jpg"
    }
  ]
}
```

(`files` truncated to one of seven; the rest are identical in shape.)

## Path layout

Content-addressed throughout, flat per artifact:

```
sources/pdf/<rev-hex>.pdf
sources/jats-xml/<rev-hex>.xml
renderings/<md-hex>.md
supplementary/<hex>.jpg
```

A second revision appends `sources/jats-xml/<new-rev-hex>.xml`; a re-render appends `renderings/<new-md-hex>.md`. Old
blobs persist, so old cites keep resolving.

## Reference / anchor types

Concrete shapes for the cite model. These live in KU records and a derived cache, not `Manifest`, and are **provisional
-- the KU layer is deferred**: `ref_id`, the `status` values, and the span type are not yet frozen.

```tsp
model SourceAnchor {
  paper_id: string;     // doc_id
  document_id: string;  // a rendering hash
  quote: string;        // verbatim, against the rendering's bytes
  exact: boolean;       // false once only fuzzy realignment held
}
model SharedAnchor {    // exported form — quote stripped
  paper_id: string;
  document_id: string;
  ref_id: string;
}
model ResolvedSpan { start: int32; end: int32; }
model OffsetsCacheEntry {
  ref_id: string;
  rendering: string;    // rendering hash the spans were computed against
  spans: ResolvedSpan[];
  status: "exact" | "fuzzy" | "unlocatable";
}
```

## Open questions

- **Supplementary updates & licence.** `files` has no revision history and no licence. A corrected figure or a CC0 data
  file with its own terms would need both. Deferred until a case lands.
- **Materialising primary->secondary refs (req 4).** Left to render-time href matching; a `references` index on the
  rendering/revision is additive if the read path needs it.
- **Renderings nesting.** Kept flat (top-level map, `from_source`/`from_revision` fields) rather than nested under their
  `Source`/`Revision`. Structural nesting is more coherent but deepens the tree and hampers enumeration.
- **Fidelity preference over `(media_type, converter)`.** The canonical rendering (and the markdown-vs-pdf display
  choice) ranks: xml over pdf; among pdf routes, **llm-ocr over docling**. Where the order lives (read-path config,
  curator-overridable) and how model identity factors in (prefer a newer `model`?) are unspecified.
- **`model` as a conditional-required field.** `model?` is optional in the schema but required iff
  `converter == llm-ocr`; the writer enforces it. Expressing the invariant structurally would mean splitting `Rendering`
  into a converter-discriminated union (cf. `Access`), which duplicates four common fields across variants for one
  conditional field -- not worth it. Revisit if more converter-specific fields appear.
- **Cross-work quote fallback for entitlement.** A KU anchored to a work an unentitled reader can't see (a licensed
  paper) could be surfaced against an *equivalent ingested work* they can -- e.g. a preprint linked by an equivalence
  edge -- by realigning the same verbatim quote into that work's rendering. The machinery already exists (equivalence
  edges + quote realignment); whether to expose it, and how entitlement composes across a class, is unspecified.
