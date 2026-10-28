# Literature cache manifest and paper model

**Related:** [`literature-evidence-layer.md`](literature-evidence-layer.md) (the evidence service that reads this
store), [`evidence-fulltext.md`](evidence-fulltext.md) (how open-access fetch and PDF transcription populate sources and
renderings), [`supplied-papers.md`](supplied-papers.md) (how user-supplied PDFs enter a paper's directory),
[`document-pane.md`](document-pane.md) (how the workbench reveals papers and highlights quotes), [`proto.md`](proto.md)
(protobuf-at-rest and upstream-JSON storage rules).

## Overview

The literature cache stores every paper under a Google Cloud Storage (GCS) prefix `papers/{doc_id}/`, where `doc_id` is
a random UUID4 minted when the paper is first ingested. A single paper can be known by multiple external identifiers
(such as a DOI, a PubMed ID, and a PubMed Central ID) that are discovered at different times, and its full text can
arrive from multiple upstream sources (such as a publisher PDF and a PubMed Central JATS XML file) that are revised or
re-converted independently. If the cache keyed directories by an external identifier or overwrote a single markdown file
in place, late-arriving identifier links would force directory renames, and re-running a markdown converter would
invalidate character offsets in existing citations.

Instead, each paper directory is committed by an atomic
[`Manifest`](../../schema/proto/themis/litcache/models/litcache.proto) (`manifest.pb`) alongside a re-derivable
bibliographic record (`metadata.pb`). Within `manifest.pb`, captured primary files (PDFs and XMLs) are modeled as
append-only lineages of content-addressed revisions (`Manifest.sources`), and derived artifacts such as markdown
conversions are stored as content-addressed entries in `Manifest.records` keyed by the SHA-256 hash of their output
bytes. A pointer map (`Manifest.refs`) names the record that readers should use for each role (such as
`refs["markdown"]`), while older renderings remain addressable by hash so existing quote caches never break. Beside the
manifest, `metadata.pb` stores a [`SourcedMetadata`](../../schema/proto/themis/litcache/models/litcache.proto) message
that pairs the normalized seven-field [`Metadata`](../../schema/proto/themis/litcache/models/litcache.proto) view
consumers read with the complete upstream records (`MetadataSource`) from PubMed, Crossref, and OpenAlex, allowing
normalized fields to be refilled from stored upstream records without re-querying external APIs.

## Background

Themis ingests biomedical literature from a pre-existing seed corpus of PDFs and Docling conversions, from live
open-access fetches across PubMed Central (PMC) and Unpaywall, and from user-uploaded PDFs. Across these paths, four
properties of scientific publishing shape the storage model:

1. **Identifiers arrive piecemeal and sometimes collide.** A preprint is first ingested under a bioRxiv DOI, later
   acquires a journal DOI and a PMID upon publication, and later still receives a PMCID when deposited in PMC. Two
   workers can ingest different identifiers for the same work before any upstream index links them, or an upstream
   record can erroneously attach a review's DOI to another paper.
1. **Primary files and conversions evolve on separate clocks.** A paper may start with a scanned PDF transcribed by an
   OCR model and later gain a publisher JATS XML converted deterministically by `litdown`, or the same PDF may be
   re-transcribed when a vision model improves.
1. **Access and licensing terms attach to the specific file, not the work.** A paper whose publisher PDF is restricted
   to authenticated institutional readers often has an author manuscript in PMC under `CC-BY` that permits external
   sharing.
1. **No single bibliographic index is complete on its own.** PubMed curates Medical Subject Headings (MeSH) and NLM
   publication types for biomedical articles and books, Crossref registers DOIs for non-Medline journal articles and
   preprints, and OpenAlex disambiguates author profiles and ORCIDs across works much more completely than the per-paper
   deposits in PubMed or Crossref.

The protobuf schema for both `manifest.pb` and `metadata.pb` lives in
[`schema/proto/themis/litcache/models/litcache.proto`](../../schema/proto/themis/litcache/models/litcache.proto).

## Design

### Identity and equivalence

The obvious way to name a paper's directory in GCS is by an external identifier such as its DOI or PMID. That breaks as
soon as a paper is ingested by its PMID before its DOI is known, or when a preprint without a PMID is ingested by its
DOI and later indexed by PubMed: any external identifier chosen as the directory key either is absent on some papers or
changes when richer metadata arrives.

To keep directory paths stable, [`themis/litcache/crosswalk.py`](../../themis/litcache/crosswalk.py) assigns each paper
a random UUID4 (`doc_id`) when it is first minted. Before writing to GCS, an ingestion worker classifies all known
identifiers for the paper into normalized `{scheme}:{value}` keys via
[`themis/litcache/identity.py`](../../themis/litcache/identity.py) and claims them in a single PostgreSQL transaction
against `litcache.crosswalk (external_id PRIMARY KEY, doc_id)`. Identifier schemes are ordered by precedence:

```
doi > pmcid > pmid > pii > binhash
```

The highest-precedence key present is recorded on the manifest as `claim_key` (for example,
`doi:10.1038/s41586-020-2649-2`), and the public bibliographic identifiers (`doi`, `pmid`, `pmcid`, and Bookshelf
accession `bookid`) are stored in `Manifest.external_ids`.

When `crosswalk.mint` checks the incoming identifier set against existing rows, three cases arise:

```
Case 1: No existing rows match
  incoming {doi:10.1038/x, pmid:123} ──► mints fresh doc_id "a1b2..."

Case 2: All matching rows point to one doc_id
  incoming {doi:10.1038/x, pmcid:PMC99}
  existing  doi:10.1038/x ──► "a1b2..."  ──► adopts "a1b2...", inserts pmcid:PMC99

Case 3: Matching rows point to two existing doc_ids (late-binding link)
  incoming {doi:10.1101/pre, pmid:456}
  existing  doi:10.1101/pre ──► "b4c5..."
            pmid:456        ──► "f8e9..." ──► adopts canonical min("b4c5...", "f8e9...") = "b4c5...",
                                              records Equivalence edges on both manifests
```

In the third case, two previously separate `doc_id`s are discovered to be the same work after both directories already
exist in GCS. Merging and deleting one of the GCS directories in place would break any analysis or citation holding the
retired `doc_id` and race with concurrent conversions writing to that prefix. Instead, both directories remain in place,
and each manifest's `Equivalence` message records symmetric `edges` to the linked `doc_id`s along with the equivalence
class's `canonical_doc_id` (defined deterministically as the lexicographically lowest `doc_id` in the connected
component).

The PostgreSQL crosswalk table acts only as the concurrency lock during minting, while the GCS manifests remain the
system of record. [`themis/litcache/rebuild.py`](../../themis/litcache/rebuild.py) can drop and reconstruct
`litcache.crosswalk` in a single transaction at any time by scanning `papers/*/manifest.pb`, inverting
`Manifest.external_ids`, and verifying that every manifest's `Equivalence` edges and `canonical_doc_id` agree.
Equivalence classes only grow through ingestion; splitting a falsely merged class (for example, after an upstream record
attaches the wrong DOI) is a manual operator repair with no automated path.

### Path layout

Every object for a paper lives under `papers/{doc_id}/` in the cache bucket, written by
[`themis/litcache/writer.py`](../../themis/litcache/writer.py):

```
papers/{doc_id}/
  manifest.pb                          # atomic commit point (protobuf Manifest)
  metadata.pb                          # bibliographic record (protobuf SourcedMetadata)
  sources/
    xml/{sha256}.xml                   # raw JATS XML bytes, content-addressed
    pdf/{sha256}.pdf                   # raw PDF bytes, content-addressed
    pdf-2/{sha256}.pdf                 # second PDF lineage when captured under different terms
  renderings/
    {sha256}.md                        # rendered markdown, keyed by its own SHA-256
    {sha256}.docling.json              # optional structured Docling output (CONVERTER_DOCLING)
  figures/
    {sha256}.{ext}                     # extracted or fetched figure images
  supplementary/
    {sha256}.{ext}                     # lazily fetched supplementary files
```

Writing a paper directory takes multiple GCS uploads, and GCS has no multi-object transactions. To prevent readers from
observing a half-written paper, `writer.write_paper` writes all content-addressed blobs (`sources/`, `renderings/`,
`figures/`, and `supplementary/`) first, validates the assembled `Manifest` against its protovalidate rules, and uploads
`manifest.pb` last with `if_generation_match=0` (create-only). A paper exists in the cache if and only if its
`manifest.pb` exists. If a worker crashes before uploading `manifest.pb`, the unreferenced content-addressed blobs are
harmless and a subsequent run re-uploads identical bytes to the same paths idempotently before committing the manifest.

Subsequent updates to an existing paper—such as adding a newly fetched open-access source (`add_source`), recording a
completed markdown rendering (`add_rendering`), or attaching associated files (`add_files`)—go through `writer.commit`.
`commit` reads `manifest.pb` at its current GCS generation, applies the mutation in memory, validates the resulting
`Manifest`, and writes it back with `if_generation_match=generation`, retrying from a fresh read if a concurrent writer
advanced the generation first.

If two ingestion workers race past the initial existence check for the same `doc_id`, the second writer's
`if_generation_match=0` upload fails with `PreconditionFailed` and falls back to `_join_committed`. Before merging its
blobs into the winner's manifest, `_join_committed` checks `_claims_only`: every identifier in the joining worker's
`ExternalIds` must already be present in the committed manifest's `ExternalIds`. When a misassigned upstream identifier
routes a different paper to an existing `doc_id`, the joining worker carries an identifier of its own that the winner's
manifest does not list; `_join_committed` then leaves the winner's manifest and `metadata.pb` untouched rather than
grafting another paper's full text or metadata onto the committed directory.

`metadata.pb` is uploaded immediately after `manifest.pb` commits. Keeping `metadata.pb` in a separate object from
`manifest.pb` means that refreshing upstream bibliographic records or refilling `Metadata` fields
([`themis/litcache/refresh.py`](../../themis/litcache/refresh.py)) never touches `manifest.pb` or contends with
conversion workers updating `Manifest.records`.

[`themis/litcache/writer.py`](../../themis/litcache/writer.py) is the single write gate for every `papers/{doc_id}/`
directory. No pipeline stage, conversion worker, or CLI script uploads `manifest.pb` or content-addressed blobs
directly; routing every mutation through `writer.py` guarantees that content-addressed blobs land before the manifest
references them, `buf validate` (protovalidate) checks pass before any write, and optimistic concurrency
(`if_generation_match`) serializes concurrent writers on the same paper.

Bulk ingestion also writes a per-run log outside `papers/` under `runs/{run_id}/`. When a paper fails a stage during a
Dataflow run, the pipeline classifies the failure into a three-tier dead-letter taxonomy under
`runs/{run_id}/anomalies/{stage}/{shard}.jsonl`:

- **`transient`:** Upstream rate limits, network timeouts, or 5xx responses that a subsequent retry run can resolve.
- **`permanent`:** Unresolvable identifiers (`MetadataUnresolvedError`), upstream structural precondition failures
  (`RecordPreconditionError`), ambiguous OpenAlex works (`AmbiguousWorkError`), or unrecognized upstream source kinds
  that require data repair or upstream fixes before retrying.
- **`unhandled`:** Unexpected exceptions or schema drift (`SchemaDriftError`) indicating a bug or upstream contract
  change that must be fixed in code.

Full-text conversion deliberately runs outside the Dataflow ingestion job. Fetching metadata and open-access archives is
cheap, embarrassingly parallel network I/O suited to Dataflow workers, whereas PDF transcription (`CONVERTER_DOCLING`
and `CONVERTER_LLM_OCR`) is slow, GPU- or model-API-rate-limited work that
[`themis/litcache/produce.py`](../../themis/litcache/produce.py) runs selectively via the conversion queue.

### Captured files are lineages of revisions

If the cache modeled updates by versioning the whole paper (`v1`, `v2`), fetching a corrected JATS XML from PMC would
appear to supersede an unchanged publisher PDF, and re-running a markdown converter would look like a new version of the
paper itself. Instead, `Manifest.sources` models each primary file stream as an independent `Source` lineage identified
by a short `handle` (such as `xml` or `pdf`) with an append-only `revisions` list:

```
Source (handle = "xml", media_type = SOURCE_FORMAT_XML, licence = "CC-BY-4.0", access = FreeToRead{})
  └── revisions: Revision (hash = "8f3a...", kind = SOURCE_KIND_PMC_OA_S3, captured_at = 2026-02-01)
  └── revisions: Revision (hash = "c41e...", kind = SOURCE_KIND_EUROPE_PMC, captured_at = 2026-03-15)  ◄── current (latest captured_at)

Source (handle = "pdf", media_type = SOURCE_FORMAT_PDF, licence = "", access = Licensed{publisher = "Elsevier"})
  └── revisions: Revision (hash = "19b0...", kind = SOURCE_KIND_SEED, has_text_layer = true)           ◄── current (latest captured_at)
```

`revisions` within a lineage is an append-only list; a lineage's current revision is the one captured most recently,
judged by its `captured_at` timestamp (`latest captured_at`) and never by its position in the list. Each `Revision`
records the SHA-256 `hash` of its raw bytes (which determines its path `sources/{handle}/{hash}.{ext}`), its provenance
`kind` (`SOURCE_KIND_PMC_OA_S3`, `SOURCE_KIND_EUROPE_PMC`, `SOURCE_KIND_ELSEVIER_OA`, `SOURCE_KIND_BIORXIV`,
`SOURCE_KIND_UPLOAD`, `SOURCE_KIND_SEED`, `SOURCE_KIND_EUROPE_PMC_BOOKSHELF`, or `SOURCE_KIND_UNPAYWALL`), its
`captured_at` timestamp, an optional `origin_url`, and, for PDFs, an optional `has_text_layer` boolean indicating
whether positioned characters can be extracted directly without OCR.

#### Recorded licence

Licensing and access terms (`licence`, `licence_basis`, and the `Access.kind` oneof: `free_to_read` (`FreeToRead`, an
empty message since the licence string and its basis sit on the enclosing `Source`), `licensed` (`Licensed` carrying
`publisher`), `institution_captured` (`InstitutionCaptured`), or `unknown` (`UnknownAccess`)) live on `Source` rather
than on `Manifest` or `Revision`. Placing them on `Manifest` would fail when a paper has both a restricted publisher PDF
(`licensed`) and an open-access author manuscript in PMC (`free_to_read`); placing them on `Revision` would duplicate
terms that remain constant across routine updates of the same stream. A new revision joins an existing `Source` lineage
only when its `licence`, `licence_basis`, and `Access` match that lineage's terms (`writer.same_terms`). If a second PDF
for the same paper arrives under different terms—for instance, a `CC-BY` author manuscript PDF alongside a restricted
publisher PDF—`writer._lineage_handle` allocates a numbered lineage handle (`pdf-2`) so each file's redistribution
boundary stays exact.

### Derived artifacts are content-addressed records

Markdown renderings are only the first kind of derived artifact in the extraction chain: next come **variant sets**
(extracted from a rendering and a supplementary spreadsheet), **pedigrees** (built from variant sets), and **case
reports** (written from a pedigree and a rendering—depending on two artifacts of different kinds). Putting a dedicated
map on the manifest for each kind (`renderings`, `variant_sets`, `pedigrees`, and so on) breaks at the second level:
artifacts depend on each other across kinds, and a reader following a case report back to its text would have to learn
every kind's scheme. Ranking candidates at read time also breaks at the second level: which rendering a variant set was
extracted from is part of the variant set, so the choice must be made *before* extraction runs and stored in `refs`
(`markdown` for the primary rendering, `markdown/table-s2.docx` for a supplementary rendering, `variants` for the
current variant set).

Instead, all derived artifacts are stored in `Manifest.records`, a map from the SHA-256 hex digest of the artifact's
output bytes to a `Record` message. Each `Record` states when it was produced (`created_at`), which inputs it was
derived from (`deps`), and how it was constructed (`construction`, currently `RenderingConstruction` with `converter`,
`converter_version`, and optional `model`). Every hash listed in `Record.deps` must exist either as a `Revision.hash` in
`Manifest.sources` or as another key in `Manifest.records`, a constraint enforced on every write by the protovalidate
CEL rule `manifest.deps_resolve`.

Keying `Manifest.records` by the SHA-256 of the output bytes rather than by
`(revision_hash, converter, converter_version)` solves two problems at once:

1. **Idempotent re-conversion and stable quote caches.** Re-running a converter after a patch bump often produces
   byte-identical markdown for most papers. Because the output hash is unchanged, `renderings/{hash}.md` is not
   duplicated, [`records.put_record`](../../themis/litcache/records.py) updates the existing record's metadata in place,
   and every cached quote span keyed to `(doc_id, rendering_hash)` remains valid.
1. **Uniform provenance DAG.** Because both source revisions and derived records are addressed by SHA-256 hashes,
   `Record.deps` can point to source revisions, other derived records, or both without changing schema shape:

```
sources["xml"].revisions[0] (hash: "8f3a...") ◄── deps ── records["d49b..."] (CONVERTER_LITDOWN, v0.3.1)
sources["pdf"].revisions[0] (hash: "19b0...") ◄── deps ── records["71c2..."] (CONVERTER_LLM_OCR, model: "claude-...")
```

The trade-off of keying by output bytes is that two runs producing identical bytes share one `Record` entry, which
retains only the `deps` and `construction` of the run that finished most recently—so a rendering re-made by a newer
converter version that outputs the same markdown records the newer version and loses the older (which is harmless to
readers because a quote cites bytes, and identical bytes are the same text).

When a paper is converted by `CONVERTER_DOCLING`, the writer also stores `{hash}.docling.json` beside `{hash}.md` under
`renderings/` so the structured layout tree is preserved alongside the markdown text.

### A ref names the record a reader should use

A paper directory can hold several markdown renderings at once—for example, a legacy `CONVERTER_DOCLING` rendering from
the seed corpus, a `CONVERTER_LLM_OCR` transcription of its PDF, and a `CONVERTER_LITDOWN` rendering of its PMC XML. If
every reader scanned `Manifest.records` and applied its own preference order, two services on different builds could
disagree on which markdown text represents the paper.

Instead, `Manifest.refs` maps a logical role name to the hash of the winning record in `Manifest.records` (validated by
the CEL rule `manifest.refs_resolve`). Readers of a paper's full text always follow `refs["markdown"]` via
[`records.markdown`](../../themis/litcache/records.py). Converter preference lives in code over `Record` and `Source`
rather than as a stored priority field on the manifest, though `refs` move only when a writer or backfill recomputes
them: a change to the ranking reaches existing manifests only when a writer or backfill runs, whereas a read-time
ranking would have applied on deploy. Whenever a writer adds a rendering through `records.put_record`, `put_record`
compares the new record against the record currently named by `refs["markdown"]` using a total order:

1. **Converter fidelity:** `CONVERTER_LITDOWN` (structured XML input) outranks `CONVERTER_LLM_OCR` (vision-model PDF
   transcription), which outranks `CONVERTER_DOCLING` (heuristic PDF extraction). Any unrecognized `Converter` enum
   value ranks below all known converters rather than raising an error, so a worker running an older build during a
   rolling deployment can still commit its own work when it reads a manifest written by a newer build.
1. **Recency:** Between two records from the same converter tier, the one with the later `created_at` timestamp wins.
1. **Deterministic tie-breaker:** If timestamps are identical, the lexicographically smaller SHA-256 hash key wins.

`put_record` only ever moves `refs["markdown"]` to the record being added, never to a third record, and leaves the ref
unchanged if it already points to the same output hash. When a higher-ranked rendering arrives, `refs["markdown"]`
advances atomically in the manifest commit, while the superseded rendering stays in `Manifest.records` and `renderings/`
so pinned references can still read it by hash.

Across multi-level extraction chains, a stored ref can point at a record whose input has since been superseded—for
example, `refs["variants"]` points at `v1` (extracted from rendering `m1`) after `refs["markdown"]` has moved on to
`m2`. `v1` remains valid and its quotes still resolve because `m1` is kept in `Manifest.records`, and staleness is
visible directly from the graph: **a record is current when each of its `deps` is the record its kind's ref names**.
Non-current records form the extraction lane's work queue, read off the store rather than tracked in a separate table; a
reader needing a consistent chain follows `deps` down from the record it starts at, rather than reading current refs at
each level.

### A change to the manifest's shape pauses the writers instead of carrying both forms

Because `manifest.pb` is stored as binary protobuf in GCS, evolving its schema by carrying both old and new fields
across a multi-release transition (dual-writing and dual-reading) would leave permanent `reserved` scars in
`litcache.proto` and transitional branching across every reader and writer ([`proto.md`](proto.md)). Three internal
processes write `manifest.pb`: the batch ingestion pipeline, the full-text conversion worker draining a Cloud Tasks
queue, and the evidence service's on-demand ingest (`MaybeIngestPapers`). The first two can be paused; on-demand ingest
cannot be paused because it runs whenever a caller asks for a paper the store does not yet hold.

Because on-demand ingest continues running during a deployment, a breaking change to `Manifest` uses two rewrite passes
across four steps:

1. **Pause the pausable writers** (the conversion queue and bulk ingestion); on-demand ingest continues writing
   manifests in the old form until the new code deploys.
1. **Run the first one-off rewrite** over every stored `papers/*/manifest.pb`, writing the new form while leaving the
   old field in place so existing readers keep working.
1. **Deploy the updated readers and writers** that use only the new form, then **run the rewrite a second time** to
   catch any manifest that on-demand ingest wrote during the rollout, and verify that none remain in the old form.
1. **Resume the paused writers**, retire the old field (`reserved`), and delete the migration tool.

### Associated files: figures and supplementary

Papers carry two kinds of associated blobs alongside their main text: figures (`ASSOCIATED_FILE_ROLE_FIGURE`) and
supplementary materials (`ASSOCIATED_FILE_ROLE_SUPPLEMENTARY`). Rather than maintaining separate protobuf messages with
identical fields, `Manifest.files` stores both as
[`AssociatedFile`](../../schema/proto/themis/litcache/models/litcache.proto) entries distinguished by `role`. Each entry
records its `role`, the original filename (`name`, such as `"Fig1.jpg"` or `"mmc1.xlsx"`), an optional `source_url`,
and—once its bytes are stored in GCS—its relative `path` (`figures/{hash}.{ext}` or `supplementary/{hash}.{ext}`),
`hash`, and `captured_at`.

Figures and supplementary files differ in how they are produced:

- **Figures are extracted during conversion and stored eagerly.** Figure images are small, bounded in format, and
  written alongside the markdown so they render inline when a curator opens the paper in the workbench.
- **Supplementary files are listed from the package and can be registered before their bytes are stored.**
  `AssociatedFile.path` is optional so a manifest can catalogue a supplement's filename and `source_url` before its blob
  is fetched. Today, the conversion worker's free `supplements` step
  ([`themis/litcache/produce.py`](../../themis/litcache/produce.py)) extracts supplementary files from the PMC OA
  archive eagerly because the archive is already downloaded for the XML step, while `ResolveContent` in the literature
  service ([`themis/services/evidence/literature/servicer.py`](../../themis/services/evidence/literature/servicer.py))
  returns `NOT_FOUND` for any entry whose `path` is still unset. `AssociatedFile` omits a `caption` field because in a
  survey of 1,680 PMC XML papers in our corpus (see [Appendix](#appendix)), 98.5% of supplementary files were explicitly
  referenced in the body text (with captions or surrounding descriptions already present in the markdown rendering), so
  storing a separate caption on `Manifest.files` would duplicate text the rendering already carries.

### Retraction and correction

When a publisher retracts or issues an expression of concern for a paper, the original PDF and XML files usually remain
byte-identical upstream while a separate notice DOI is registered in PubMed (`RetractionIn` / `RetractedPublication`) or
Crossref (`update-to`). Modeling retraction as a new `Source` revision would miss retractions where the full-text bytes
never change. Instead, `Manifest.retraction` is a top-level
[`Retraction`](../../schema/proto/themis/litcache/models/litcache.proto) field (`status`, `evidence_source`,
`detected_at`, and `notice_doi`) that metadata refresh passes can update in place without touching `Manifest.sources` or
`Manifest.records`.

### The bibliographic record: metadata.pb

#### SourcedMetadata: consumer Metadata beside MetadataSource

Consumers of the literature cache—such as search indexing, citation formatting, and the workbench document pane—need
seven bibliographic fields: `title`, `authors`, `year`, `container_title`, `abstract`, `mesh_headings`, and
`publication_types`. If `metadata.pb` stored only those seven normalized fields, fixing a parser bug or adding a field
later would require re-fetching every paper from PubMed, Crossref, and OpenAlex over rate-limited network APIs.
Conversely, if consumers read the raw upstream records directly, every caller would have to implement three index
parsers and handle upstream schema quirks.

[`themis/litcache/sourced_metadata.py`](../../themis/litcache/sourced_metadata.py) resolves this tension by storing a
[`SourcedMetadata`](../../schema/proto/themis/litcache/models/litcache.proto) message in `metadata.pb` with two required
halves:

```
metadata.pb (SourcedMetadata)
├── metadata (Metadata)               ◄── read by all consumers via sourced_metadata.read_metadata()
│     title, authors[], year, container_title,
│     abstract, mesh_headings[], publication_types[]
└── source (MetadataSource)           ◄── read only by metadata_fields.fill()
      ├── pubmed (PubmedRecord: article | book_article)   ─┐ at most one resolving record
      ├── crossref_json (compact JSON text)               ─┘ (or openalex_json alone)
      └── openalex_json (compact JSON text, alone or beside pubmed / crossref_json)
```

Consumers call `sourced_metadata.read_metadata(data)`, which validates the message and returns `Metadata` alone so no
caller outside `themis/litcache/` can couple to `MetadataSource`. When the extraction rules in
[`themis/litcache/metadata_fields.py`](../../themis/litcache/metadata_fields.py) change, a refill pass reads each stored
`metadata.pb` via `sourced_metadata.parse_to_refill` (which validates `MetadataSource` alone so records that violate a
newly tightened `Metadata` rule can still be loaded), recomputes `Metadata = metadata_fields.fill(source)`, and writes
`metadata.pb` back without making any external HTTP calls.

Every committed paper must have a valid `SourcedMetadata`. If no upstream index resolves a paper's identifiers
(`MetadataUnresolvedError`), the ingestion pipeline records the paper as unresolved rather than inventing an empty or
placeholder `metadata.pb`.

#### How each index record is stored in MetadataSource

Each index's record in `MetadataSource` is stored whole in the representation that matches its upstream contract, never
translated into another index's schema:

- **PubMed (`MetadataSource.pubmed`):** Stored as a typed
  [`PubmedRecord`](../../schema/proto/themis/litcache/models/litcache.proto) oneof holding either a
  `pubmed_proto.pubmed.PubmedArticle` (`article`) or a `pubmed_proto.pubmed.PubmedBookArticle` (`book_article`). NCBI's
  efetch XML follows a stable DTD, and [`themis/litcache/efetch.py`](../../themis/litcache/efetch.py) converts each XML
  element directly into its `pubmed_proto` message while harvesting cross-identifiers (`doi`, `pmcid`, and for Bookshelf
  records the single canonical `NBK…` accession `bookid`). If a PubMed record fails a structural precondition—such as a
  book record lacking a canonical `NBK` accession or listing conflicting DOIs—`efetch` raises `RecordPreconditionError`
  for that paper alone rather than falling back to Crossref and disguising the broken PubMed record.
- **Crossref (`MetadataSource.crossref_json`) and OpenAlex (`MetadataSource.openalex_json`):** Stored as compact ASCII
  JSON strings (`ensure_ascii=True`, no NaN/Infinity tokens) via
  [`themis/litcache/upstream_record.py`](../../themis/litcache/upstream_record.py). Unlike NLM's versioned DTD, Crossref
  and OpenAlex are unversioned REST APIs that add keys and change types on unannounced schedules (for example, OpenAlex
  added top-level `has_content` booleans to every work without a version bump). Decoding those responses into
  hand-written protobuf mirror schemas caused protobuf's JSON parser to reject whole batches whenever an unread field
  appeared or changed type ([`proto.md`](proto.md)). Holding the upstream JSON object verbatim as text preserves every
  field the index returned, while `upstream_record.JsonObject` validates only the specific keys Themis reads and raises
  `SchemaDriftError` naming the exact dotted path (such as
  `publication_year is "2024", where an integer or null is read`) if a read field changes shape.

The CEL rule `metadata_source.indexes` on `MetadataSource` enforces that at least one index record is present and that
`pubmed` and `crossref_json` are never both set on the same paper.

#### The resolving index's record, and OpenAlex's beside it

[`themis/litcache/resolve.py`](../../themis/litcache/resolve.py) resolves a paper's identifiers down a priority ladder
so the most curated bibliographic record becomes the resolving record:

```
Single-paper (resolve_metadata):
  PMID present? ──yes──► PubMed efetch ─────────────────────────┐
       │ no / miss                                              ├──► Look up companion OpenAlex work
  DOI present?  ──yes──► Crossref (/works/{doi}) ───────────────┘    (by PMID, then DOI) and pair in
                                                                     MetadataSource.openalex_json
Batch (resolve_batch):
  PMIDs ───────────────► Batched PubMed efetch ─────────────────┐
  DOIs  ───────────────► litfetch batch resolver + OpenAlex ────┼──► Batched companion OpenAlex lookup
                         (routes discovered PMIDs to efetch;    │    for all PubMed-resolved papers
                          keeps OpenAlex work alone if no PMID) ┘
```

In both paths, PubMed wins whenever a PMID is known or discovered (provided PubMed's record does not state a different
DOI from the one being resolved). For papers outside PubMed, `resolve_metadata` resolves the DOI through Crossref
([`themis/litcache/crossref.py`](../../themis/litcache/crossref.py)), whereas `resolve_batch` uses OpenAlex
([`themis/litcache/openalex.py`](../../themis/litcache/openalex.py)) because Crossref has no batch lookup endpoint that
avoids per-DOI rate limits.

Once the resolving index (PubMed or Crossref) is settled, `resolve` performs a second lookup to fetch OpenAlex's work
for the same paper—querying OpenAlex first by PMID and, on a miss, by DOI—and stores that JSON work in
`MetadataSource.openalex_json` alongside the resolving record. Pairing OpenAlex beside PubMed or Crossref is driven by a
sharp difference in how upstream indexes curate author lists versus other metadata fields (see [Appendix](#appendix) for
the full corpus comparisons):

- **OpenAlex disambiguates authors and attaches ORCIDs across works.** PubMed and Crossref only record an ORCID when the
  publisher deposited it on that specific article. OpenAlex groups publications into disambiguated author profiles
  (`A5…`) and propagates an ORCID linked on a recent paper onto the author's whole career
  (`authorships[].author.orcid`), yielding 50–63% ORCID coverage in our seed sample compared with 21% in PubMed.
  Validated across 100 dual-covered works, 99 had identical author counts, 96 had identical family names in order (the
  remaining 3 differed only by script or spelling), and all 45 PubMed ORCIDs agreed with OpenAlex. Two trade-offs are
  accepted:
  1. OpenAlex profile-merge errors occasionally attach an ORCID to a 1983 or 1996 paper written before ORCID launched in
     2012; this is acceptable because patient deduplication uses author overlap only as a hint for a human reviewer.
  1. Taking `authors` from `openalex_json` even on PubMed-resolved records couples PubMed ingest to OpenAlex's
     `authorships` schema whenever an OpenAlex companion record is fetched.
- **PubMed and Crossref are more faithful on every other field.** In PubMed, `major_topic` (`MajorTopicYN="Y"`) often
  attaches to a specific MeSH qualifier (`Q…`) rather than the heading's descriptor (`D…`); verified across 80
  dual-covered works, 169 of 269 major headings in PubMed were major only via a qualifier, and OpenAlex marked none of
  them because OpenAlex's `is_major_topic` mirrors only the descriptor's `MajorTopicYN`. Similarly, OpenAlex
  reconstructs abstracts from an inverted word-position index (`abstract_inverted_index`), which strips section labels
  and can scramble punctuation when words share positions.

Accordingly, `metadata_fields.read` takes `title`, `year`, `container_title`, `abstract`, `mesh_headings`, and
`publication_types` from the resolving index's record (`pubmed` or `crossref_json`, or `openalex_json` when OpenAlex is
the sole record), and takes the entire `authors` list from `openalex_json` whenever `openalex_json` is present and lists
at least one author. The author list is taken as a whole from one index rather than merged position-by-position across
indexes, because consortium entries and group authors cause author counts and positions to diverge between PubMed and
OpenAlex.

When [`openalex.find`](../../themis/litcache/openalex.py) looks up a paper's work in OpenAlex, four outcomes are
possible:

1. **One matching work:** Stored in `openalex_json`. Because OpenAlex's list endpoint (`/works?filter=…`) truncates
   `authorships` at 100 entries, any work returned with 100 authorships is re-fetched individually from `/works/{id}` so
   the stored JSON record is complete. If a DOI matches an OpenAlex work only through `locations[].doi` rather than the
   work's primary `doi` (which happens when OpenAlex merges a bioRxiv preprint into its published journal work),
   `Answer.by_location` marks the match so OpenAlex supplies its record and authors without overwriting the preprint's
   identifiers or publisher with the journal version's.
1. **No matching work:** The resolving record (`pubmed` or `crossref_json`) is stored alone, and `authors` is filled
   from it.
1. **Multiple matching works (`AmbiguousWorkError`):** OpenAlex occasionally holds duplicate works for the same DOI or
   PMID. If multiple works match a DOI and exactly one of them carries a PMID, `openalex.read_results` selects that work
   (the others are unlinked repository copies). Otherwise, choosing arbitrarily could attach another work's author list
   to the paper, so the resolver raises `AmbiguousWorkError` and holds the paper back for review.
1. **Transport or HTTP error:** Propagates so the caller retries rather than silently dropping OpenAlex's record.

#### Filling Metadata from MetadataSource

[`themis/litcache/metadata_fields.py`](../../themis/litcache/metadata_fields.py) fills `Metadata` from `MetadataSource`
under strict normalization rules enforced by `sourced_metadata.check_metadata`:

- **Plain text with scientific notation intact (`title`, `container_title`, `abstract`, `authors[].name`):** Every text
  field is normalized to single-spaced plain UTF-8 text. Crossref and OpenAlex leak inline HTML, JATS, and MathML markup
  (`<i>`, `<scp>`, `<mml:math>`, etc.) and entities in titles, abstracts, and journal names. Rather than stripping
  `<[^<>]+>` (which corrupts scientific notation like `p < 0.05; n > 10` or `c.1A>G`), `_strip_inline_markup` matches
  only known markup and MathML element names (`_MARKUP_ELEMENTS`, `_MATHML_ELEMENTS`, plus `jats:`/`mml:`/`ns\d+:`
  prefixes) via `_escape_text`, converts `<br>` to space, drops `<mml:annotation>` and `<mml:annotation-xml>` subtrees,
  decodes HTML entities closed by `;` once (`html.unescape`), and runs `_strip_inline_markup` a second time for
  double-escaped tags (`&lt;i&gt;`). PubMed (`PubmedArticle` and `PubmedBookArticle`) already converted inline markup to
  Markdown at ingest; `metadata_fields.py` calls `xml_converter.markdown_to_plain_text` to strip formatting while
  unescaping `\*` and `\_` back to literal characters (such as `p.R391*`). Structured abstract section labels become
  `Label: text`, and a leading `"Abstract:"` heading is stripped.
- **Authors and ORCIDs (`authors`):** Empty names, invalid PubMed author entries (`ValidYN="N"`), and editor lists
  (`TYPE_EDITORS`) are omitted. ORCID normalization strips the `https://orcid.org/` prefix, uppercases a trailing `x`,
  and verifies both the 19-character pattern `\d{4}-\d{4}-\d{4}-\d{3}[\dX]` and the ISO 7064 mod 11-2 check digit; a
  syntactically malformed or bad-checksum string becomes `""` (treating it as upstream user-entry noise), whereas a
  non-string JSON value raises `SchemaDriftError`.
- **Publication year (`year`):** Validated by the protovalidate pattern `^([0-9]{4})?$` (either four ASCII digits
  `1000`–`9999` or `""` when not stated). Holding `year` as a string follows `EuropePmcRecord.year` and avoids a proto
  presence bit (`""` means "not stated"). PubMed's sentinel year `1` (`_UNPARSED_YEAR = 1`, emitted when `MedlineDate`
  failed to parse) is normalized to `""`. For PubMed book chapters (such as *GeneReviews* entries updated independently
  of their parent book), the chapter's `ContributionDate` takes precedence over the book's `PubDate`.
- **MeSH headings (`mesh_headings`):** Each `MeshTerm.ui` is validated by the protovalidate pattern
  `^[DQ]([0-9]{6}|[0-9]{9})$` (a `D` or `Q` prefix followed by either 6 or 9 digits, such as `D012345` or `D000123456`),
  while the `D` vs. `Q` prefix distinction on `MeshHeading` is enforced by two CEL rules: `mesh_heading.descriptor_kind`
  (`this.ui == '' || this.ui.startsWith('D')` on `descriptor`) and `mesh_heading.qualifier_kind`
  (`this.qualifiers.all(q, q.ui.startsWith('Q'))` on `qualifiers`). PubMed journal articles copy `MeshHeadingList`
  directly. OpenAlex flattens each `MeshHeading` into one row per qualifier (`descriptor_ui`, `descriptor_name`,
  `qualifier_ui`, `qualifier_name`, `is_major_topic`), but its `is_major_topic` boolean mirrors only the descriptor's
  `MajorTopicYN` in PubMed; therefore `_openalex_mesh_headings` groups rows by `descriptor_ui` in first-seen order,
  marks the descriptor `major_topic = True` if any of its rows has `is_major_topic`, and always sets qualifier
  `major_topic = False` rather than fabricating which qualifier carried the mark. Note that empty `mesh_headings` or
  `publication_types` is **not** a negative signal for downstream triage: 78 of 295 PubMed seed records had no MeSH
  headings because they were not yet indexed for MEDLINE, yet 23 of those 72 non-MEDLINE records were tagged
  `Case Reports`, and Crossref and OpenAlex have no equivalent to `Case Reports`.
- **Publication types (`publication_types`):** Held as deduplicated NLM English strings (`"Journal Article"`,
  `"Case Reports"`, `"Review"`, etc.) rather than NLM UIs, trading sensitivity to rare NLM string renames (such as
  `Retraction of Publication` to `Retraction Notice`) for human-readable self-description. OpenAlex and Crossref have
  separate closed mapping tables in `metadata_fields.py`: `_OPENALEX_PUBLICATION_TYPES` maps `'article'` →
  `"Journal Article"`, `'review'` → `"Review"`, `'preprint'` → `"Preprint"`, `'letter'` → `"Letter"`, `'erratum'` →
  `"Published Erratum"`, and `'retraction'` → `"Retraction Notice"`; `_CROSSREF_PUBLICATION_TYPES` (keyed by
  `(type, subtype)` where `None` matches any subtype) maps `('journal-article', None)` → `"Journal Article"` and
  `('posted-content', 'preprint')` → `"Preprint"` (so a Crossref letter or editorial arrives as `journal-article` and
  maps to `"Journal Article"`). Any other string (such as `"book-chapter"` or `"dataset"`) is unmapped and produces
  `[]`, whereas a `type` or `subtype` value of the wrong JSON type raises `SchemaDriftError`.

#### Bookshelf chapter identity (`NBK…`)

NCBI Bookshelf chapters—most notably *GeneReviews*—carry a PMID and an `NBK…` book accession (`bookid`), and are served
by Europe PMC as BITS XML at `bookXML` keyed by that `NBK…` accession. Three rules keep Bookshelf chapters first-class
in the store:

1. **Bibliographic parsing:** `efetch.py` parses `<PubmedBookArticle>` into `PubmedRecord.book_article`
   (`pubmed_proto.pubmed.PubmedBookArticle`) and extracts the chapter's canonical `NBK…` accession into
   `ExternalIds.bookid`.
1. **Distinct upstream `SourceKind`:** Full-text BITS XML fetched from Europe PMC's Bookshelf endpoint is recorded with
   `SOURCE_KIND_EUROPE_PMC_BOOKSHELF` (distinct from `SOURCE_KIND_EUROPE_PMC` for journal JATS XML). Because
   `SourceKind` identifies which upstream service the bytes came from, the conversion producer can select the right XML
   parser and refuse unrecognized upstreams as permanent anomalies.
1. **Crosswalk registration:** `ExternalIds.bookid` is claimed in `litcache.crosswalk` (`bookid:{NBK…}`) alongside
   `pmid`, `pmcid`, and `doi` so that `rebuild.py` can reconstruct the entire crosswalk from the GCS manifests alone.

### Operator maintenance paths and the move off the mirrors

Because `metadata.pb` is a self-contained sidecar derived from `Manifest.external_ids`, operators have three distinct
maintenance paths (`reingest-literature-seed-corpus.md`) ordered from cheapest to most expensive:

1. **Fill of the metadata fields:** Recomputes `SourcedMetadata.metadata` in place from the stored `MetadataSource`
   bytes (`pubmed`, `openalex_json`, `crossref_json`) via `metadata_fields.fill_sourced_metadata` (`refresh.py`) in
   minutes without touching external APIs.
1. **Metadata refresh (`--refresh-metadata`):** [`themis/litcache/refresh.py`](../../themis/litcache/refresh.py) does
   not delete `metadata.pb` itself—it resolves and writes `metadata.pb` only for committed papers whose `metadata.pb` is
   absent—so bringing stored records up to a change starts with the operator deleting `metadata.pb` (and any legacy
   `pubmed.pb` / `europe_pmc.pb` files) before running the refresh to re-resolve each paper via the bulk ladder (batched
   PubMed `efetch` for records with a PMID; `litfetch` DOI + OpenAlex for the rest). Note the trade-off: an on-demand
   paper originally resolved through Crossref loses Crossref's record (`MetadataSource.crossref_json`) when its
   `metadata.pb` is deleted and refreshed in bulk, receiving OpenAlex's (`MetadataSource.openalex_json`) instead.
1. **Full rebuild:** Wipes the store and re-runs conversions when `manifest.pb` or conversion outputs themselves must
   change. Unlike `metadata.pb`, `manifest.pb` and conversion sidecars (`{hash}.docling.json`, `QuoteIndex`) hold
   expensive LLM and Docling outputs, so schema changes to them use additive proto changes and expand/contract backfills
   rather than wiping and re-computing.

#### The move off the mirrors

When replacing `PaperMetadata` and the hand-written `CrossrefWork` and `OpenAlexWork` mirror protos with
`SourcedMetadata` (`crossref_json` and `openalex_json`), we retired the old messages and mirror `.proto` files outright.
Normally, [`proto.md`](proto.md) (§Retiring an RPC or a message) **forbids** deleting a retired message under the
`models` trees because bytes already written at rest could still need it, and the `backward-compatible` CI check
enforces that rule with no in-tool override. Deleting `PaperMetadata` and the two mirror files in one step failed the
required `backward-compatible` check and required an admin merge bypass (`review-policy.md`), paired with deleting every
`metadata.pb` before the deploy, deploying, deleting every `metadata.pb` a **second time** (because unpausable on-demand
ingest `MaybeIngestPapers` may have written old-shape records during the rollout), and running the metadata refresh.

Those deletions were permissible only because no production environment held this store yet; the sole environment that
did (`dev`) met the condition `migrations.md` puts on a destructive change (it held no data worth keeping and had no
users to fail). Once a production environment holds the store, that condition no longer holds: from then on,
`proto.md`'s rule binds `metadata.pb` just as it binds every other at-rest contract—a retired message stays declared,
and a destructive change no longer goes in one step. Changing what a field means in place is never open: protobuf bytes
carry field numbers and wire types rather than names or meanings, so a record written before the change would decode
silently into the new meaning.

### Quote-reference model

Analyses and conversation turns cite papers using markdown directives `:paper[doc_id]` and
`:quote[doc_id, verbatim quote]` ([`document-pane.md`](document-pane.md)). If a citation stored character offsets
(`start`, `end`) as its primary anchor, any re-conversion of the paper—or even toggling the document pane between
markdown and PDF—would shift offsets and silently highlight the wrong text.

Instead, the verbatim quote string is the durable anchor, and character offsets or PDF bounding boxes are resolved on
demand by [`anchorite`](../../themis/services/evidence/literature/litcache.py) against a specific rendering:

```
Authoring / storage (durable):
  :quote[a1b2..., the variant was observed in 14 of 18 probands]

Resolution via evidence service Locate / Validate (per representation):
  ├── REPRESENTATION_MARKDOWN ──► reads Manifest.refs["markdown"] ("d49b...")
  │                               runs anchorite.locate_quote_span(markdown, quote)
  │                               ──► TextOffsets { start: 14208, end: 14253 }
  └── REPRESENTATION_PDF      ──► reads current PDF revision ("19b0...")
                                  ──► PdfRegion { page: 4, rects: [...] }
```

The manifest side is settled: whenever a rendering is addressed directly (as in the offsets cache and licence-safe
shared anchors below), it is named by its record's key in `Manifest.records` (`rendering_hash`, the SHA-256 of the
markdown bytes), while the primary citation anchor (`SourceAnchor`) names only the paper (`doc_id`) and the verbatim
`quote` so it survives converter upgrades across renderings. The anchor and the offsets cache shapes below are
provisional:

- **Primary anchor (`SourceAnchor`):** Carries `doc_id`, the verbatim `quote`, and an optional `section_hint`. Because
  the quote is plain text from the paper, it survives converter upgrades (`CONVERTER_DOCLING` to `CONVERTER_LITDOWN`)
  and works across both markdown and PDF representations.
- **Derived offset cache (`OffsetsCacheEntry`):** Resolved spans (`start`, `end`) for a markdown rendering are keyed by
  `(doc_id, rendering_hash, sha256(normalised_quote))`. When `Manifest.refs["markdown"]` advances to a new
  `rendering_hash`, cached offsets for the old hash simply miss without serving stale coordinates.
- **Licence-safe export (`SharedAnchor`):** Quoting verbatim text from a restricted (`licensed` or
  `institution_captured`) source is safe inside an internal workspace, but exporting an analysis outside the licence
  boundary cannot redistribute non-`CC-BY` prose. Stripping the verbatim `quote` and exporting
  `(doc_id, rendering_hash, converter_version, spans)` lets a recipient with legitimate access to the same paper
  reconstruct the highlight using the deterministic converter without the exported file carrying copyrighted text.

#### Reference and anchor types

Concrete shapes for the cite model. These live in knowledge-unit records and a derived cache, not in the manifest, and
are provisional: `ref_id`, the status values, and the span type are not yet frozen.

```protobuf
message SourceAnchor {
  string doc_id = 1;
  string quote = 2;
  string section_hint = 3;
}

message SharedAnchor {
  string doc_id = 1;
  string rendering_hash = 2;
  string converter_version = 3;
  repeated ResolvedSpan spans = 4;
}

message ResolvedSpan {
  uint32 start = 1;
  uint32 end = 2;
}

message OffsetsCacheEntry {
  string doc_id = 1;
  string rendering_hash = 2;
  string quote_hash = 3;
  repeated ResolvedSpan spans = 4;
}
```

## Alternatives considered

- **A derived artifact stored beside the paper under a name computed from its input, outside the manifest** (for
  example, naming a variant set extracted from rendering `m1` under prompt v3 from `m1` and `3`). Lost at the first
  level holding two candidates: the next step's input is a choice rather than a computation, a name computed from one
  input cannot record which candidate was chosen, and nothing lists what exists short of listing the bucket.
- **Refs in an object of their own beside the manifest.** Lost because recording an artifact and moving the ref that
  names it could no longer happen in one write (so a ref could briefly name a record the manifest does not hold), nor
  could a single `buf validate` check confirm that every ref resolves.
- **A record keyed by its bytes, its deps, and its construction together.** Lost because no reader would answer
  differently: a quote cites bytes, and two routes to identical text yield the same text.
- **Using an external identifier (DOI or PMID) as the GCS directory name.** Rejected because no single identifier scheme
  covers every paper (preprints lack PMIDs; older clinical reports and Bookshelf chapters often lack DOIs), and
  discovering a higher-precedence identifier later would require moving every object under `papers/{id}/` and updating
  all references.
- **Including the converter in `Source` identity.** Rejected because a `Source` represents a captured upstream file (an
  XML or PDF lineage), whereas a converter produces a derived `Record` in `Manifest.records`. Keeping converters out of
  `Source` lets multiple converters run against the same captured revision without duplicating source blobs.
- **Overwriting a single `paper.md` rendering in place.** Rejected because overwriting the markdown file on converter
  upgrades would invalidate cached quote offsets and make it impossible to reproduce the exact text an earlier analysis
  cited. Content-addressing renderings in `Manifest.records` and advancing `Manifest.refs["markdown"]` gives readers a
  single current rendering while keeping prior renderings addressable by hash.
- **Separate protobuf messages for figures and supplementary files.** Rejected because both share the exact same
  attributes (`role`, `name`, `source_url`, `path`, `hash`, `captured_at`) and differ only in `AssociatedFileRole` and
  how their bytes are populated.
- **Eagerly downloading all supplementary files into the cache alongside the figures.** Rejected as a universal rule
  because supplementary archives from non-OA sources can be hundreds of megabytes or require a separate fetch before the
  manifest can list them. Instead, `path` is optional on `AssociatedFile` so a manifest entry can record `name` and
  `source_url` before its blob is written ([`themis/litcache/writer.py`](../../themis/litcache/writer.py)), while the
  conversion worker's `supplements` step populates PMC OA supplements eagerly because the source archive is already
  downloaded.
- **Hand-maintained protobuf mirror schemas for Crossref and OpenAlex.** Rejected after production experience showed
  that unannounced upstream additions (such as OpenAlex's `has_content`) or type variations on unread fields broke
  protobuf JSON parsing across batches. Storing `crossref_json` and `openalex_json` as validated JSON text keeps the
  full upstream record while checking only the fields Themis reads.

## Open questions

- **Multi-writer equivalence merges.** When `crosswalk.mint` detects that a newly ingested paper bridges two existing
  `doc_id`s, writing symmetric `Equivalence` edges requires updating multiple manifests (`papers/{id_1}/manifest.pb` and
  `papers/{id_2}/manifest.pb`). The exact retry and reconciliation flow when one manifest update succeeds and the second
  fails midway is not yet wired into `writer.py`.
- **Cross-source text equivalence.** When a paper has both an XML-derived rendering and a PDF-derived rendering, we do
  not yet check whether the two represent the same underlying version of the manuscript (for example, an accepted author
  manuscript in PMC versus the copy-edited publisher proof PDF).
- **Licence string normalization.** `Source.licence` currently stores the raw licence string returned by `litfetch`
  (such as a Creative Commons URL or `"cc-by"`). Mapping these strings to canonical SPDX identifiers is deferred until
  automated export filtering consumes `Source.licence`.
- **Garbage collection of unreferenced blobs and superseded records.** A worker crash before the initial `manifest.pb`
  commit, or a superseded record no longer referenced by any `refs` entry or pinned citation, can leave unreferenced
  blobs under `papers/{doc_id}/`. Because blob sizes are modest, periodic mark-and-sweep against `manifest.pb` and
  active citations is deferred.
- **On-demand supplementary file fetch and transcription.** While `Manifest.files` supports unpopulated `AssociatedFile`
  entries (`path` unset) that the writer ([`themis/litcache/writer.py`](../../themis/litcache/writer.py)) can populate
  later, `ResolveContent` currently returns `NOT_FOUND` rather than fetching and transcribing deferred supplementary
  files on demand.
- **Automated retraction polling cadence.** `Manifest.retraction` defines how retraction notices are recorded on a
  paper, but the scheduled refresh cadence for polling PubMed and Crossref retraction updates across the committed
  corpus is not yet fixed.
- **Model ordering within the same converter.** `records.put_record` ranks converters by tier (`CONVERTER_LITDOWN` >
  `CONVERTER_LLM_OCR` > `CONVERTER_DOCLING`) and then by `created_at`, leaving open whether a newer `model` on the same
  converter should outrank an older one regardless of when each rendering was produced, and where that model ranking
  should live.
- **Converter-specific fields on a rendering.** `RenderingConstruction` currently carries `model` as a single optional
  field used only by `CONVERTER_LLM_OCR`. If further converter-specific fields appear, they can either arrive as a
  per-converter oneof beside the common fields or absorb the common fields via an expand/contract migration.
- **Falling back to an equivalent work for entitlement.** When a knowledge unit or citation is anchored to a restricted
  work (such as a publisher PDF) that a reader is not entitled to view, `Manifest.equivalences` could allow falling back
  to an open-access equivalent (such as a preprint or author manuscript) by re-aligning the verbatim quote against the
  equivalent work's rendering.
- **Per-file licence on supplementary materials.** `AssociatedFile` currently carries no licence field of its own and is
  governed by the enclosing paper's `Source` terms; recording a distinct licence on a supplementary file (for example, a
  CC0 dataset attached to a CC-BY or restricted article) would require an optional licence field on `AssociatedFile`.
- **Alerting on on-demand metadata schema drift.** Whereas batch ingestion writes `SchemaDriftError` failures to
  `runs/{run_id}/anomalies/`, on-demand ingest (`MaybeIngestPapers`) logs schema drift only to the evidence service's
  log; wiring a log-based metric and alert to the environment's operational alerts channel remains open.

## Appendix

### Corpus survey: supplementary file references and captions

To decide whether supplementary file captions belong in `Manifest.files` and whether supplementary blobs should be
fetched eagerly or lazily, we surveyed 1,680 PMC JATS XML papers in the seed corpus:

| Metric                                                                                                     | Value                                                                                 |
| ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Papers with at least one `<supplementary-material>` element                                                | 75% of the 1,680 papers                                                               |
| Mean supplementary files per paper (across all papers / across papers with any)                            | 3.5 / 4.6                                                                             |
| Supplementary files explicitly cross-referenced in body prose (`<xref ref-type="supplementary-material">`) | 98.5%                                                                                 |
| Mean prose mentions per supplementary file                                                                 | 2.6                                                                                   |
| Supplementary elements carrying a `<caption>` or `<label>` in the XML                                      | 86% (mean ~90 chars per file, ~320 chars / ~80 tokens per paper)                      |
| Remaining 14% without an XML `<caption>`                                                                   | 94% of these are still described by surrounding body sentences at their `<xref>` site |

### Corpus survey: PubMed, Crossref, and OpenAlex metadata coverage

To decide how `metadata_fields.fill` should combine records when PubMed or Crossref resolves a paper and OpenAlex's work
sits beside it in `MetadataSource`, we compared upstream records across three samples from the seed corpus:

1. **Author names and ORCID coverage (100 dual-covered works in PubMed and OpenAlex):**
   - **ORCIDs:** OpenAlex attached a valid ORCID (`authorships[].author.orcid`) to **50–63%** of authors in our seed
     samples, compared with **21%** in PubMed (`Identifier[@Source="ORCID"]`). All **45** PubMed ORCIDs in the 100-work
     sample agreed with OpenAlex; the coverage gain comes from OpenAlex grouping publications into disambiguated author
     profiles (`A5…`) and propagating an ORCID linked on a recent paper across the author's career.
   - **Author counts and ordering:** Across the 100 dual-covered works, **99** had identical author counts and **96**
     had identical family names in order (the remaining **3** differed only by script or spelling). Where consortium or
     group authors (`CollectiveName` in PubMed) appear, taking the author list as a whole from one index avoids
     positional mismatches.
1. **MeSH major-topic fidelity (80 dual-covered works with MeSH headings in both PubMed and OpenAlex):**
   - Across the 80 dual-covered works, **169 of 269** major headings in PubMed were major only via a qualifier
     (`QualifierName[@MajorTopicYN="Y"]` while the parent descriptor had `MajorTopicYN="N"`), and OpenAlex marked
     **none** of them because OpenAlex's `is_major_topic` mirrors only the descriptor's `MajorTopicYN`. Reading
     `mesh_headings` from PubMed whenever a PubMed record is present preserves NLM's exact descriptor and qualifier
     flags, while `_openalex_mesh_headings` sets `major_topic = False` on qualifiers when reconstructing headings from
     OpenAlex alone.
1. **PubMed records without MeSH headings (295 PubMed-resolved works in the seed corpus):**
   - **78 of 295** PubMed seed records had no `MeshHeadingList` because they were not yet indexed for MEDLINE, yet
     **23** of those 72 non-MEDLINE records were tagged `Case Reports` in `PublicationTypeList` (which Crossref and
     OpenAlex do not have an equivalent for). Consequently, empty `mesh_headings` or `publication_types` must not be
     treated as a negative signal during triage. When OpenAlex resolves a paper on its own, grouping OpenAlex's `mesh`
     rows into `Metadata.mesh_headings` recovers descriptor and qualifier UIs (`D…` and `Q…`) with descriptor-level
     `major_topic` flags.
