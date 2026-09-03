# Design: litcache manifest — per-source revisions, content-addressed renderings, the quote-reference model

**Status:** current **Related:** [`proto.md`](proto.md) (serialization + the litcache proto),
[`literature-evidence-layer.md`](literature-evidence-layer.md) (the full-text store and the interface that serves it),
[`../plans/literature-cache.md`](../plans/literature-cache.md) (the S0 build plan).

## Overview

The litcache manifest's structural model: a paper's primary vs supplementary files as per-source lineages with
append-only revisions, content-addressed renderings, and the quote-durable cite the KU layer anchors against. Serialized
as a binary proto (`manifest.pb`); the JSON shown below is a *rendering*, not the at-rest artifact — `Access` is a proto
`oneof` over the four access variants (access-iff-`publisher` structural), the residual constraints protovalidate
options (see [`proto.md`](proto.md)).

Beside the manifest, `metadata.pb` holds the paper's bibliographic record inside a typed envelope, `PaperMetadata`, with
one field per index a paper can be resolved from — PubMed, Crossref, OpenAlex — each holding that index's record whole
and in the index's own schema; PubMed's field says which of its two record kinds, a journal article or a Bookshelf book
or chapter, the bytes are, and a summary in one shape is derived on read, never stored
([The bibliographic record](#the-bibliographic-record-metadatapb)).

## Background

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

Hand-authored proto in `schema/proto/themis/litcache/models/litcache.proto` (`Source`, `Revision`, `Rendering`, `Access`
and the enums; `Manifest`, `AssociatedFile`) — the source of truth ([`proto.md`](proto.md)); shown here to keep the
structural model legible. Enums are proto-canonical (`UPPER_SNAKE`, name-prefixed, `*_UNSPECIFIED = 0` sentinel that is
never a valid domain value); declared-field invariants are protovalidate options.

```proto
// The lineage parts.
enum LicenceBasis {
  LICENCE_BASIS_UNSPECIFIED = 0;
  LICENCE_BASIS_ARTIFACT = 1;  // read from the fetched bytes (JATS <license>, Elsevier metadata)
  LICENCE_BASIS_ASSERTED = 2;  // bytes carry no licence, so an access authority supplies it (e.g.
                               // Unpaywall's OA determination), or we assert the work's resolved
                               // terms onto a retained seed pdf that carries none
}

enum SourceKind {
  SOURCE_KIND_UNSPECIFIED = 0;
  SOURCE_KIND_PMC_OA_S3 = 1; SOURCE_KIND_EUROPE_PMC = 2; SOURCE_KIND_ELSEVIER_OA = 3;
  SOURCE_KIND_BIORXIV = 4; SOURCE_KIND_UPLOAD = 5; SOURCE_KIND_SEED = 6;
  SOURCE_KIND_EUROPE_PMC_BOOKSHELF = 7;
}
// Scraped html is converted to xml upstream and enters as media_type xml under a distinct
// handle (e.g. "scraped-html") — so html is not a media type here.
enum SourceFormat { SOURCE_FORMAT_UNSPECIFIED = 0; SOURCE_FORMAT_XML = 1; SOURCE_FORMAT_PDF = 2; }
// llm-ocr (vision-model OCR of the pdf) is the preferred pdf route; docling is the legacy
// fallback, retained so its existing renderings still resolve.
enum Converter {
  CONVERTER_UNSPECIFIED = 0; CONVERTER_LITDOWN = 1; CONVERTER_DOCLING = 2; CONVERTER_LLM_OCR = 3;
}

// Access disposition of a lineage. A oneof, so publisher exists iff the variant is `licensed` —
// access-iff-publisher holds structurally; protovalidate requires exactly one variant.
message FreeToRead {}
message Licensed { string publisher = 1 [(buf.validate.field).string.min_len = 1]; }
message InstitutionCaptured {}
message UnknownAccess {}
message Access {
  oneof kind {
    option (buf.validate.oneof).required = true;
    FreeToRead free_to_read = 1;
    Licensed licensed = 2;
    InstitutionCaptured institution_captured = 3;
    UnknownAccess unknown = 4;
  }
}

// One fetched byte-set of a lineage. Blob at sources/{handle}/{hex}.{ext}; current = latest captured_at.
message Revision {
  string hash = 1;                 // sha256 hex digest of the raw bytes
  optional string origin_url = 2;  // external provenance; omitted for seed/upload
  SourceKind kind = 3;
  google.protobuf.Timestamp captured_at = 4;  // recency signal — NOT array order
  optional bool has_text_layer = 5;  // pdf only: recoverable text layer (pypdfium2 glyphs) — enables quote→bbox
}

// A primary-artifact lineage. `handle` is stable identity across updates.
message Source {
  string handle = 1;               // lineage identity, an open id namespace: "pdf" | "jats-xml" | "scraped-html"
  SourceFormat media_type = 2;
  string licence = 3;              // raw, as litfetch returned it (not an SPDX id)
  LicenceBasis licence_basis = 4;
  Access access = 5 [(buf.validate.field).required = true];
  repeated Revision revisions = 6 [(buf.validate.field).repeated.min_items = 1];  // append-only, by captured_at
}

// Markdown derived from one revision via one route. Blob at renderings/{hex}.md.
message Rendering {
  option (buf.validate.message).cel = {  // model set iff converter is CONVERTER_LLM_OCR
    id: "rendering.model_iff_llm_ocr"
    expression: "(this.converter == 3) == (this.model != '')"
  };
  string from_source = 1;          // Source.handle (open id namespace, not an enum)
  string from_revision = 2;        // the Revision.hash it rendered
  Converter converter = 3;
  string converter_version = 4;    // the converter tool/harness version
  optional string model = 5;       // free-text LLM id, e.g. "claude-opus-4-8"; set iff converter is llm_ocr
  google.protobuf.Timestamp created_at = 6;
}
```

```proto
// The manifest: sources + a content-addressed renderings map.
enum AssociatedFileRole {
  ASSOCIATED_FILE_ROLE_UNSPECIFIED = 0;
  ASSOCIATED_FILE_ROLE_FIGURE = 1;
  ASSOCIATED_FILE_ROLE_SUPPLEMENTARY = 2;
}
message AssociatedFile {
  AssociatedFileRole role = 1;
  string name = 2;
  optional string source_url = 3;
  optional string path = 4;        // supplementary/{hex}.{ext}; absent until fetched
}
message Manifest {
  string doc_id = 1;               // uuid4, == directory name
  ExternalIds external_ids = 2;
  string claim_key = 3;
  Equivalence equivalence = 4;
  Retraction retraction = 5;
  repeated Source sources = 6;             // primary artifacts
  map<string, Rendering> renderings = 7;   // key = markdown content hash (sha256 hex)
  repeated AssociatedFile files = 8;       // supplementary registry (lazy fetch)
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
    "biorxiv": null,
    "bookid": null
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

Two records at the paper's root; everything else content-addressed, flat per artifact:

```
manifest.pb
metadata.pb
sources/pdf/<rev-hex>.pdf
sources/jats-xml/<rev-hex>.xml
renderings/<md-hex>.md
supplementary/<hex>.jpg
```

A second revision appends `sources/jats-xml/<new-rev-hex>.xml`; a re-render appends `renderings/<new-md-hex>.md`. Old
blobs persist, so old cites keep resolving.

`metadata.pb` sits beside the manifest and is the one artifact that may be regenerated after the commit: the manifest
holds no hash of it, and its bytes derive from the paper's identifiers through the resolver ladder rather than from
anything in the directory, so re-deriving it refreshes the bibliographic record while every source and rendering stays.
Which changes that refresh serves, and which need the corpus rebuilt, is decided with the record
([The bibliographic record](#the-bibliographic-record-metadatapb)).

## The bibliographic record: `metadata.pb`

Beside `manifest.pb`, at the paper's root rather than under a content hash, sits `metadata.pb`: the paper's bibliography
— title, authors, journal, abstract, the identifiers its index lists. The manifest says what we hold of a paper and
under what terms; `metadata.pb` says what the paper is. It is write-once and regenerated wholesale from its source,
never edited in place ([`proto.md`](proto.md), bucket 1). This section decides what the record is, and how a reader
tells.

**The record is kept whole, in the schema its index publishes.** A paper's metadata is stored as the record its index
published, in that index's own schema, rather than as a subset of its fields chosen at write time. A subset carries only
what its author foresaw needing, and what it dropped is gone until someone re-fetches; the whole record costs kilobytes
and answers questions nobody has asked yet. The same test rules out storing one index's record mapped into another's
schema: a Crossref work squeezed into `PubmedArticle` keeps only the fields PubMed happens to have a slot for, and a
reader can no longer tell which index answered ([`literature-evidence-layer.md`](literature-evidence-layer.md), a
discovery rpc is a query against one named source). So each index has a schema of its own here. PubMed's is NLM's,
generated from its DTD ([`proto.md`](proto.md), Generated upstream schemas); Crossref's and OpenAlex's are mirrors of
the JSON each publishes, hand-authored and loaded strictly ([`proto.md`](proto.md), Mirrored upstream schemas). The same
reasoning makes the literature interface answer a PMID with PubMed's record whole, so a run's triage read and the
store's copy are the same record kind through the same converter — not the same bytes, since the store's copy is taken
at ingest and the run's at query time, and indexes revise records.

**One field per index.** `metadata.pb` is a typed envelope, `PaperMetadata`
([`litcache.proto`](../../schema/proto/themis/litcache/models/litcache.proto)): one field per index a paper can be
resolved from, each holding that index's record whole, and a protovalidate constraint that at least one is set. The
fields are independent because the indexes are: a paper PubMed indexes may also have an OpenAlex record, and a preprint
PubMed does not index has a Crossref or OpenAlex record and no PubMed one. Which record a reader prefers when several
are present — PubMed's title over Crossref's, say — is a policy of the one function that derives the summary, below, not
a slot in the envelope. The envelope records what each index states and nothing about their precedence, so a new index
is a new field and changes no reader of the others.

**PubMed's record has two kinds, and its field says which.** Most PMIDs name a journal article, but a PMID can also name
a book NCBI hosts on its Bookshelf, or a chapter of one — a GeneReviews chapter, the expert-written summary of a gene or
condition that a variant analysis cites routinely. PubMed's schema gives the two kinds different records,
`PubmedArticle` and `PubmedBookArticle`, that share no top-level shape: a journal record hangs off a citation with a
journal, a book record off a document with a book. Protobuf's wire format carries field numbers, not a type, so bytes
read as the wrong message may decode without error into a record that means nothing, and a store that can hold either
has to write down which one it holds. The envelope's `pubmed` field is therefore a message of its own, `PubmedRecord`,
whose `oneof` has an arm per kind and requires exactly one set — the structural move `Access` makes above. The
exclusivity is PubMed's own rule, a PMID names one record, stated in the type; it is why the two kinds are arms of one
field where other indexes are fields of their own.

**A mirror is loaded strictly, so a lagging mirror fails the paper rather than thinning the record.** Crossref and
OpenAlex publish JSON, and neither publishes a schema a generator consumes the way `pubmed-proto` consumes NLM's DTD:
each publishes an OpenAPI description of its HTTP API, and each description lags and mis-states the records the API
serves — OpenAlex's by a dozen top-level keys, Crossref's by keys and shapes it never lists. Each mirror is therefore
hand-authored against the live records, and a hand-authored schema can lag the upstream. A lagging schema that dropped
the keys it lacked would lose data at write time for good, so the loader refuses a record carrying a key the mirror
lacks: the paper is dead-lettered as schema drift naming the field, and the fix is the field. Nothing lossy is ever
written; the cost is that ingestion of papers resolved through that index pauses until the mirror catches up, which for
OpenAlex is as often as it adds fields. Two upstream shapes have no proto equivalent — an array of arrays, an object
whose values are arrays — and the loader wraps them into messages before the parse; it also drops the null array
elements proto3-JSON cannot hold, except inside a positional array such as a date's parts, where a null between stated
parts fails the parse rather than shifting them. A round trip over live records, parse then serialise then compare, is
what checks that the wrapping and the mirror lose nothing.

**The summary is derived, never stored.** Consumers want a bibliography in one shape — a title, a year, an author list —
without switching on which index answered. That shape is computed from the envelope by one function beside the proto,
and is not a field of it. A stored projection goes stale with every change to the rule that derives it: a chapter with
no title of its own falls back to its book's, say, and the next such rule would leave every stored summary wrong until a
corpus rewrite, at which point the store's rule for derived artifacts would want a summariser version on it, as a
rendering carries its converter's. Derived on read, the same change is a deploy. Adjudicating between coexisting records
lands in the same function.

**An envelope with no record is corruption.** The at-least-one constraint is enforced where the record crosses a
boundary: the writer validates the envelope before it writes, and a reader that parses one fails rather than reading it
as a paper without metadata. A paper no index has a record for is not ingested at all — the resolve ladder dead-letters
it — so an empty envelope never means "metadata not yet resolved"; it means the bytes are wrong.

**A book record carries an identifier of its own.** Beside the PMID, Bookshelf names a chapter by an accession (`NBK…`),
and the accession is how the chapter's text is addressed: Europe PMC serves a chapter's BITS XML under its accession at
the `bookXML` endpoint — a different endpoint and corpus from the JATS article XML behind the plain Europe PMC source
kind. The lineage it produces records a distinct source kind for two reasons: the producer maps the upstream that served
a body onto a `SourceKind` and refuses a body from an upstream it has no kind for as a permanent anomaly, so a new
upstream needs a member; and the member records which corpus the bytes came from. The accession joins the manifest's
external ids (`ExternalIds.bookid`) on the footing every external id there has: an id in the manifest is one the
crosswalk claimed — that is what keeps the crosswalk rebuildable from the manifests alone — so the accession is minted
like the others rather than merely recorded.

**What is deliberately not here: a migration, or a dual-read window.** A change confined to the record is met by a
metadata refresh; a change to the conversions needs a full rebuild; neither is a migration. Every `metadata.pb` in the
store is re-derivable from its source — PubMed's XML, Crossref's or OpenAlex's JSON — through the ladder that resolved
it at ingest, so a change to the record is met by re-deriving the records, not by rewriting stored bytes or teaching
readers a second decoding. Both are operator runs
([`reingest-literature-seed-corpus.md`](../runbooks/reingest-literature-seed-corpus.md)), and which one a change needs
turns on what else has to change:

- A *metadata refresh* re-resolves every paper's bibliographic record through that ladder — efetch for a PMID,
  litfetch's DOI resolution and then OpenAlex for the rest — and overwrites `metadata.pb`. Manifests, conversions and
  `doc_id`s stay untouched: the record derives from nothing in the paper's directory and nothing there derives from it
  ([Path layout](#path-layout)), so it is the one artifact a committed paper can have re-derived. It is the path for a
  change confined to the record — the envelope, say — and the only route to an own-index record for a paper first
  resolved through Crossref or OpenAlex: the raw record was never kept, so no rewrite of the stored bytes could produce
  it.
- A *full rebuild* — the crosswalk table truncated, the `papers/` prefix cleared, the seed re-ingested on Dataflow — is
  the path when the conversions themselves must change: ingestion skips a paper whose manifest exists, and nothing
  regenerates a committed paper's sources or renderings in place.

Neither leaves the readers a fallback to carry. What either leaves is a window — a destructive change to a stored
artifact, allowed on the condition [`migrations.md`](migrations.md#how-it-runs) puts on a destructive migration: the
environment holds no data worth keeping and no users to fail, and the doc names what breaks and until when. The window
opens when the envelope reader deploys, since a pre-envelope blob may decode as an envelope without error and nothing
can tell one from a valid envelope, and the refresh closes it. A read-side fallback is the cost of rewriting a corpus
that has users; paying it here would leave a second decode path in every reader for a state that ceases to exist the
moment the refresh completes.

## Reference / anchor types

Concrete shapes for the cite model. These live in KU records and a derived cache, not `Manifest`, and are **provisional
-- the KU layer is deferred**: `ref_id`, the `status` values, and the span type are not yet frozen.

```proto
message SourceAnchor {
  string paper_id = 1;     // doc_id
  string document_id = 2;  // a rendering hash
  string quote = 3;        // verbatim, against the rendering's bytes
  bool exact = 4;          // false once only fuzzy realignment held
}
message SharedAnchor {     // exported form — quote stripped
  string paper_id = 1;
  string document_id = 2;
  string ref_id = 3;
}
message ResolvedSpan { int32 start = 1; int32 end = 2; }
message OffsetsCacheEntry {
  string ref_id = 1;
  string rendering = 2;    // rendering hash the spans were computed against
  repeated ResolvedSpan spans = 3;
  string status = 4;       // "exact" | "fuzzy" | "unlocatable" — a proto-canonical enum once frozen
}
```

## Implementation state

Shipped: the proto schema (`schema/proto/themis/litcache/models/litcache.proto`) + generated stubs, and the `Access`
boundary validator. The cache writer, the readers, and the KU layer that anchors against the quote-reference model are
staged in the build plan ([`../plans/literature-cache.md`](../plans/literature-cache.md)).

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
- **A Bookshelf accession at the door.** Whether `MaybeIngestPapers` accepts `bookid:` beside `doi:`, `pmid:` and
  `pmcid:` is the door's question, held in
  [`literature-evidence-layer.md`](literature-evidence-layer.md#open-questions).
- **Cross-work quote fallback for entitlement.** A KU anchored to a work an unentitled reader can't see (a licensed
  paper) could be surfaced against an *equivalent ingested work* they can -- e.g. a preprint linked by an equivalence
  edge -- by realigning the same verbatim quote into that work's rendering. The machinery already exists (equivalence
  edges + quote realignment); whether to expose it, and how entitlement composes across a class, is unspecified.
