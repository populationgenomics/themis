# Design: Literature-evidence layer

**Status:** design — infra architecture resolved; evidence-trust model (grounding
quality, retraction currency, KU dedup) and several empirical questions still open
(§9). Build pending; not yet ticketed.
**Related:** [`../PRODUCT.md`](../PRODUCT.md) §4 (facts-as-durable-value), §7
(facts-cross-Projects / literature-does-not), §9 (security); the workspace and
sharing rules in [`workspace-model.md`](workspace-model.md); infrastructure in
[`spike-infrastructure.md`](spike-infrastructure.md). Terms in
[`../../GLOSSARY.md`](../../GLOSSARY.md).

## Purpose

The evidence layer that supplies Themis with literature: find papers, read their
full text, answer specific questions, and distil reusable facts. It is the
concrete realisation of PRODUCT.md's "platform-wide, provenance-tracked
substrate of facts extracted from the literature" plus the source-document
access that backs it.

Built on **pubmedifier** ([`populationgenomics/pubmedifier`](https://github.com/populationgenomics/pubmedifier) — existing: a 37M-abstract PubMed search service with
E5-base-v2 embeddings, an ANN index, a MedCPT cross-encoder reranker, a
PMID→full-text-markdown ladder, and per-user publisher credentials). pubmedifier
is extended and partly re-scoped here, not replaced.

**Motivating example.** A variant curator asks whether there is functional evidence
that *KCNJ11* p.Arg201His impairs K_ATP channel function (an ACMG PS3 question). The
layer answers from facts it has already distilled: it grounds the question to the
gene and variant, pulls the knowledge units that bear on it, and judges each as
*supporting* or *refuting* the claim — returning a cited tally (say four supporting
reports and one conflicting) without reading a paper — **the tally itself is always
displayable** (facts aren't copyrightable); only the source-sentence check below is
entitlement-gated. To check the load-bearing report, the curator follows its
citation and the gated reader shows the exact source sentence — but only for a paper
they are entitled to read. Meanwhile the same question runs against the 37M-abstract
corpus to surface papers *not yet ingested*; these are captured and distilled **in
the background of the same conversation** and folded in as they land (subject to
ingestion latency). Everything
below — the cache, source access, knowledge units, the retrieval funnel, and
collections — exists to make that loop cheap, cited, and shareable; §4.1 walks this
example through the components step by step.

## 1. The boundary: two planes

The split between what is shared and what is gated is driven by two forcing
functions only — **copyright** and **compute** — and they act on different axes.
**Copyright** is the *sharing* gate: it bars redistribution of the source **and its
whole-paper derivations** — verbatim text *and* a full transcription/markdown, since
a reworded whole-paper rendering is still a derivative work carrying far more than
isolated facts. **Compute** is *not* a sharing gate: it makes *preprocessing the
37M corpus* (capture + extract) prohibitively expensive at our resources, so the
processed substrate covers a selected subset (§5), not all of PubMed. Everything
else defaults open: extracted **atomic facts** ("knowledge units", §4) are **not
copyrightable**, so the shared fact substrate crosses institutional and Project
lines even when a fact was distilled from a licensed paper one institution can read
and another cannot. The hard-gated artifacts are the **verbatim source text and its
whole-paper derivations** (markdown, transcriptions); only the atomic-fact substrate
crosses freely.

This yields two planes:

- **Shared public plane** — pubmedifier. Discovery (abstract search + rerank),
  the public-OA full-text cache, and corpus-scale classifier scoring for
  collection selection. Low sensitivity; reusable beyond Themis.
- **Themis boundary plane** — behind the egress-controlled boundary
  (`spike-infrastructure.md` §8). Paragraph embeddings, the knowledge-unit
  substrate, the licensed/captured cache tier, the extraction pipeline, and
  question-answering. Reached by the agent only through tightly-typed MCP-tunnel
  tools; the agent never touches the stores directly.

Which plane an artifact physically lives in (the public store vs behind the
boundary) **follows from** its shared-vs-gated class above; it is not an
independent decision.

## 2. Cache

One **logical keyspace** keyed by a canonical paper id (§2.2 — PMID/DOI/PMCID are
alternate keys; not all papers have them). The key is versioned `(doc_id, version)`,
where `doc_id` is the canonical id and `version` identifies one **immutable snapshot
of the paper's text** — a distinct revision of the same work (preprint vs published,
successive PMC article versions, PubMed revisions). Each version is captured and
stored once and never overwritten (§2.1), so a citation to `(doc_id, version)`
always resolves to the exact text it was made against. The stored representation is
**markdown**, the form fed to LLMs.

- **XML→markdown is preferred over PDF→markdown** and treated as a faithful
  representation of the paper; PDF-derived markdown is accepted as a fallback.
  The conversion route is recorded as a quality tag (`xml-faithful` |
  `pdf-derived`), not a separate pipeline.
- Each object carries two **orthogonal** tags, set on every object from the start:
  - a **licence** — the canonical licence identifier (SPDX-style: `CC-BY-4.0`,
    `CC-BY-NC-4.0`, `CC-BY-ND-4.0`, `CC-BY-SA-4.0`, `CC0-1.0`,
    `publisher-proprietary`, `unknown`), sourced from authoritative metadata (PMC
    OA-subset licence field, Crossref `license` URL + `content-version`,
    Unpaywall). The redistribution **policy booleans** the design keys on
    (`redistributable`, `derivatives-allowed`, `commercial-allowed`, `share-alike`)
    are **derived** from this identifier, not stored by hand — so the
    CC-BY-vs-NC-vs-ND distinctions that actually govern reuse are preserved, not
    collapsed into a single "readable" flag.
  - a **provenance/access** tag — how it entered and who may read it
    (`free-to-read` | `licensed:<publisher>` | `institution-captured`), plus the
    **capture entitlement** — the contractual basis a captured copy was obtained
    under and the audience it may be served to (e.g. an institutional subscription
    entitles that institution's members to *read* it, not to redistribute it). It
    governs who the gated read-tool (§3) will resolve source text for. This is
    independent of the licence: "free-to-read" is an access fact, not a
    redistribution right.

  The licence governs the **verbatim source full text** (caching, redistribution,
  reformatting-as-derivative); it does **not** gate the knowledge-unit substrate,
  which shares on the non-copyrightability basis (§4.3).
- **Write-through**: every read path (agent reads, Q&A, extraction) populates
  the cache, so it warms incrementally and markdown is never reconverted.
- A **gated read-tool** mediates access to verbatim full text per the source
  rules (§3); the cache is not read directly.

The OA tier may later be physically separated into the shared plane; until reuse
or scale justifies that migration, a single store behind the boundary suffices.
The per-object licence (and its derived `redistributable` flag) makes the split a
later data migration — filter the redistributable objects out — not a
re-architecture.

### 2.1 Storage layout — one directory per paper (GCS)

Each paper is a **GCS directory** holding all its artifacts: source `xml` and/or
`pdf`, derived `markdown`, extracted `figures/` (images), `supplementary/` files,
the write-once `knowledge_units.jsonl` (Layer 1 model output, §4.2), and the
derived `entities.jsonl` (the entity→KU map — stable entity ids + mention
clustering, rebuildable, §4.2). **Revisions are
additive**: a new version (preprint→published, PMC article version, PubMed
revision) is collected as an **additional `(version)` set of source + `markdown`
artifacts in the same directory** — existing version artifacts are **never
overwritten**, so a knowledge unit that cites a given `(doc_id, version)` markdown
always resolves to the exact, unchanged text it was extracted from (§2.2).
**GCS is the durable source of truth**; everything in Cloud SQL (§2.3) is a
rebuildable projection of these directories.

- **`manifest.json`** describes the directory: this UUID, all known external ids
  (§2.2), any equivalence edges linking it to other UUIDs of the same work + the
  resulting canonical UUID (§2.2), the licence, access, and quality tags (§2),
  capture provenance, a **retraction flag** (§6.1),
  and **the source URL of
  every known associated file — even files not (yet) downloaded** (a figure or
  supplementary archive we know exists but haven't fetched). This makes the
  directory self-describing and supports lazy fetch.
- **`metadata.json`** holds bibliographic metadata. The **schema is `pubmed_pb2`**
  (pubmed-proto) — the 37M corpus is already modelled there, so we reuse it rather
  than re-model — but it is **stored as JSON** (protobuf's canonical JSON
  serialization; a JSONSchema is generable from the proto). So every consumer reads
  plain, inspectable JSON with **no protobuf dependency**, while in-corpus and
  external papers (e.g. bioRxiv, synthesised into the same schema) stay uniform
  downstream.

### 2.2 Identity — canonical id + external-id crosswalk

Not every paper has a PMID or DOI (preprints, supplements), so the cache key is
an internally-minted **UUID**, with a **crosswalk** mapping external ids (PMID,
DOI, PMCID, arXiv/bioRxiv) → UUID. The UUID is the directory name, the Cloud SQL
primary key, and what knowledge units cite.

- **Late binding (4c).** A paper may enter external to the corpus and *become*
  part of it later (a preprint gets a PMID; the corpus ingests it). The crosswalk
  is **mutable**: reconciliation adds the new PMID/corpus-gid to the paper's
  identity. Two cases:
  - **Known same paper at capture** — a revision whose identity resolves when it
    arrives (a PMC article version; a published paper whose record carries the
    preprint DOI) is added to the existing directory as an additive `(version)`
    artifact (§2.1), under one UUID.
  - **Separately cached, linked later** — if a preprint and its published version
    were cached under **separate UUIDs** before the link was known, we **do not
    merge their directories**. The crosswalk records an **equivalence edge** (same
    work); the linked UUIDs form an **equivalence class** with a deterministic
    **canonical UUID** (lowest), reconstructable from crosswalk edges alone. This
    replaces an earlier physical survivor-merge — the only operation that mutated
    the otherwise-additive GCS store — so GCS stays purely additive and rebuild
    stays trivial.

  Raw `DOI → UUID` is therefore **M:N**, but `DOI → canonical-UUID` is a function:
  the canonical representative is the single paper identity that dedup, counting,
  Project joins, and presentation key on. **Knowledge units are never re-pointed**
  — each cites the specific `(doc_id, version)` markdown it was extracted from, and
  resolves through the crosswalk if its UUID was later folded into a class. The
  corpus link is formed *after the fact*, never assumed at capture.
- **Versions carry their own licence; binaries are content-addressed.** Versions in
  a class keep per-object licence/access (§2): a preprint stays
  `CC-BY / free-to-read` and cleanly separable to the shared plane even when the
  version of record is `publisher-proprietary`. Large binary artifacts (figures,
  supplementary data) are stored **content-addressed** — identical data shared
  across versions or papers is stored once and referenced by hash from each
  version's manifest (which still lists human-readable filenames), the licence
  carried on the blob.
- **Cross-version facts reconcile at extraction, not by merge (§4.2):** the prior
  version's KUs seed the new version's extraction, partitioning its output into
  new / lifted / silently-absent facts. Within a class, conflicts resolve by
  **temporal recency** (latest version authoritative) after the **retraction gate**
  (§6.1); *across* classes recency does not apply — independent works disagreeing
  is real evidence conflict (§4.1).

### 2.3 Structured projection — Cloud SQL (rebuildable), not parquet/DuckDB

Extracted columns (title, abstract, authors, year, journal, MeSH, the crosswalk,
knowledge units, grounding overlay) live in **Cloud SQL** (Postgres — themis
already runs it).

The knowledge-unit table is the one worth sizing, since it is the per-fact
substrate. Draft row:

```
knowledge_unit(
  id uuid pk, doc_id uuid, version text,  -- source anchor (§4.2)
  spans jsonb,                                  -- [{start,end}] offsets, no text
  assertion text,                               -- expression-stripped, ~250 B
  type text, epistemic text, confidence real,
  supersedes uuid null, prov jsonb,
  created_at timestamptz)
-- entity/grounding live in sibling tables keyed by id (Layer 2, recomputable)
```

**Napkin math.** Extraction is confined to the cached RD-relevant subset (§5), not
the 37M corpus — order ~100k papers — and the reference build measured ~85 atomic
units/paper, each assertion ~250 B. So the KU *text + structured columns* are
~100k × 85 × ~400 B ≈ **~3.4 GB** — trivial on the always-on instance's disk, and
nowhere near "close to the full text" (one paper's markdown alone is ~40 KB). The
cost that *would* bite is not storage: embedding the KUs is ~8.5M vectors × 768-d ≈
~13 GB at fp16 (**peanuts**). The open question is **value** — does a KU semantic
index add retrieval recall over grounded + paragraph search (§4.1)? Structural +
entity-grounded retrieval is the floor; whether to add the KU index is the §9
question, decided on recall, not on the ~13 GB. (The only real costs would be the
GPU embed pass and keeping the index RAM-resident in pgvector, §2.4.)

- **Why Cloud SQL over a parquet+DuckDB snapshot (4a):** the workload is
  **incremental and transactional** — papers added one at a time, units appended,
  the grounding overlay recomputed, the crosswalk mutated, joined against KUs and
  Project/entity tables. That is OLTP, where Postgres fits; parquet+DuckDB is for
  read-only analytical scans of a *static* dump and is poor at per-row mutation,
  concurrent writes, and joins.
- **Parquet is the bulk-load / rebuild artifact, not the query engine.** The
  PubMed ingestion pipeline (Beam) emits the extracted corpus columns as
  **parquet on GCS**; Cloud SQL is populated by bulk-loading it (`COPY` into a
  staging table → upsert). The same parquet doubles as the corpus-columns rebuild
  source and as a DuckDB-queryable analytical snapshot if ever wanted — so parquet
  earns its place as the columnar transfer/rebuild format *feeding* the OLTP
  store, not as the live query layer.
- **Reingestion upserts; it never replaces.** PubMed re-ingests (annual baseline
  + rolling updates; papers get revised). Two **ownership domains** keep this
  safe: corpus-derived columns (the ingestion regenerates them) vs cache-owned
  tables (UUID crosswalk, knowledge units, grounding overlay, manifests,
  externally-captured papers — *never touched* by reingest), bridged by the
  crosswalk (§2.2). On reingest the corpus-columns table is **upserted keyed by
  corpus id + revision** (staging-table merge / `INSERT … ON CONFLICT`) — never
  dropped-and-reloaded, which would destroy the cache-owned layer and orphan the
  units that cite it. When a reingested revision differs from the one a cached
  extraction was built on, the cached artifacts are **flagged stale** for lazy
  re-extraction (the `(doc_id, version)` field tracks this). Reingestion is also
  the natural **trigger for late-binding reconciliation** (§2.2): newly-ingested
  corpus papers are matched (DOI/title) to any pre-existing external UUID and
  linked or merged.
- **Rebuildable from GCS (4d).** Cloud SQL holds **no primary state**: it is a
  materialised projection rebuilt from **two GCS sources** — the ingestion's
  corpus-columns parquet, and the per-paper cache directories (`manifest.json` +
  `metadata.json` + `knowledge_units.jsonl`), joined via the crosswalk. Lose the
  database, rebuild it from the bucket. The expensive Layer-1 extraction is stored
  as a GCS artifact (not Postgres-primary) so a rebuild never re-extracts; the
  grounding overlay is recomputable anyway (§4.2).
- **Corpus columns — project the proven subset for the full corpus.** Exploding a
  column subset out of the bagz files has already paid off in practice, so that
  subset is projected into Cloud SQL for **all 37M** papers, not just cached ones —
  giving uniform SQL over the whole corpus (joins to KUs, the crosswalk,
  Project/entity tables; relational filtering). This is a **column subset, not the
  full bagz payload**: vm_search keeps the embedding and word/bigram/filter indices
  it already serves; Cloud SQL holds only the columns worth querying relationally.
  The tens-of-GB duplication is the accepted cost of that query surface — disk on
  the always-on instance, not RAM-resident (unlike the pgvector index, §2.4).

### 2.4 Embeddings — split by scale and mutability

Two embedding indices with opposite characteristics, so they live in different
stores. Cloud SQL is fully **managed** Postgres: you choose a machine type
(vCPU/RAM) but get no raw VM, no local NVMe, and **no scale-to-zero** — it runs and
bills 24/7 at the configured size. `pgvector` (0.8) ANN is **RAM-bound**: fast
only while index + vectors sit in the instance's memory; spill to disk and it
degrades.

**Embeddings never egress as raw vectors.** No tool returns vectors; similarity is
computed **server-side** and only results cross (paper mappings, scores). So
"is an embedding shareable?" never arises — the vectors don't move, and the public
discovery surface receives results, not vectors. (It is also not a meaningful
derivative of the source: an embedding captures concepts, not predicates, and is
not invertible to its paragraph — not even to a semantically-equivalent one — so it
carries no redistributable expression regardless.)

**The 37M abstract corpus → vm_search ANN VM (NVMe + Rust HNSW), not pgvector.**
Measured (X1, `pubmedifier/docs/pgvector-bench.md`): head-to-head over the live
~30M-doc E5 corpus, same `m=16`/`ef_construction=200` graph, identical queries,
recall@100 ground-truthed by exact seq-scan.

- **vm_search is ~55× faster at first-pass recall (≤0.89) on identical 32 GB
  hardware** — ~7 ms vs pgvector's ~385 ms; first-pass retrieval feeding a read is
  the operating point, not high-recall exact search.
- **pgvector needs the index RAM-resident to compete.** That corpus's HNSW index
  is **110 GB**; pgvector built it on a 256 GB host (160 GB `maintenance_work_mem`)
  and degrades a consistent **3–4×** forced onto pd-ssd at 32 GB. The 110 GB index
  is the disqualifier: serving it competitively wants a ≥128 GB RAM-resident
  instance, always-on.
- **Neither of pgvector's apparent edges is durable.** Its higher recall ceiling
  (~0.984 vs vm_search's ~0.97) is a **build-heuristic** gap, not a backend
  property — the bench attributes it to pgvector's C build vs FAISS `HNSWFlat`,
  and porting that heuristic to a Rust-readable graph is a ~1–3 day,
  license-permitted follow-up. Conversely pgvector has its **own ceiling**:
  `hnsw.ef_search` is hard-capped at **1000** (0.8.x), bounding candidate depth,
  where vm_search pushes ef to **5120+** for deep pools.

**The ~4M paragraph index → pgvector in Cloud SQL.** This is a *different regime*
and the bench verdict does not transfer. At 4M × 768-d the index is **~6 GB**
(`halfvec`/fp16) — resident in RAM the Cloud SQL instance already pays for, so the
110 GB-can't-fit failure mode that condemns pgvector at 30M never bites. Here
pgvector is the **better** fit, for reasons beyond size:

- **Native incremental `INSERT`** — the paragraph index grows in **small batches**
  as papers enter the write-through cache (§2) — embedding is a batch GPU step
  (§7), but the batches are small and frequent, not one static bulk build.
  pgvector ingests each batch with an ordinary transactional `INSERT`; the
  vm_search batch-mmap build would need base+delta/compaction machinery to match.
  The incremental workload wants the incremental-friendly store.
- **Filtered ANN** — pgvector combines the vector search with SQL predicates
  (collection membership §5, licence tag §2, MeSH) in one query; vm_search would
  need a separate filter-fusion build.
- **Co-located** with the structured projection (§2.3) and crosswalk — no second
  serving component to build, deploy, and keep warm.

## 3. Source access and capture

Verbatim source full text is the gated artifact. Access follows the user's
**institutional licensing** (an axis distinct from the Project boundary;
PRODUCT.md §7) and Project membership; provenance and access are recorded for
every object.

Capture has two routes:

- **Upload** — a researcher adds a paper they already hold. The researcher
  supplied the access; the system ingests, converts to markdown, and records
  provenance.
- **Proven-access fetch** — the system fetches on a researcher's behalf **only
  against proven access**. Per-user publisher credentials (already in
  pubmedifier; e.g. the Elsevier key) are the proof. Open-access is trivially
  proven. With no proof the system does not fetch; it offers the upload route.

Capture runs in a **dedicated worker, not the agent sandbox** (§6).

## 4. Knowledge units and question-answering

**RAG forms facts; knowledge units remember them.** Full-text retrieval-and-read
is the mechanism that answers arbitrary specific questions; knowledge units are
the durable, shared, cited cache of facts already formed. A question consults
the knowledge-unit substrate first, falls back to reading source text, and a
read mints new knowledge units.

### 4.1 Retrieval funnel

A **probe** is a natural-language proposition to **answer or refute** ("the KCNJ11
p.Arg201His variant impairs K_ATP channel function"; "variant X is benign for
condition Y"). "Pertains to" is an **entailment** relation, not similarity: cosine
fires on same-*topic* and cannot separate support from refute ("X causes Y" and "X
does not cause Y" embed alike). So the substrate query is **retrieve broad, then
entail** — not nearest-neighbour.

1. **Knowledge-unit substrate** — answer from facts already formed (cheap, cited,
   cross-Project; often answers outright). Three moves: **(a) understand** — an LLM
   turns the probe into grounded entities/CURIEs where resolvable (gene/variant/
   disease) + a normalised hypothesis + optional relation/ACMG cell (query-side
   grounding reuses the §4.2 linker, same CURIE space as the units); **(b)
   retrieve** (recall, polarity-agnostic) — union of **grounded-entity match** (KUs
   whose entity set intersects the probe's CURIEs; precise, misses ungrounded
   units) and **KU semantic embedding** (paraphrase + ungrounded recall backstop) —
   a refuting unit is topically near, so both surface it and polarity is the next
   step's job; **(c) entail** (precision + polarity) — judge each candidate against
   the hypothesis → {supports, refutes, neutral} + strength. (c) is the step cosine
   cannot do, and it is the same NLI judge the KU-quality scorer runs.
2. **Discovery = ingestion targeting** (not a prefilter for step 1) — the
   pubmedifier abstract/paragraph corpus search + MedCPT rerank (§2.4, §4.4) finds
   papers pertaining to the probe **not yet in the KU DB**, queued for capture +
   extraction (§3); step 1 then re-runs over the fuller substrate. Corpus recall
   closes the KU DB's **coverage gap**; it never caps a fact query by prefiltering
   it.
3. **Read** — a small candidate set is read **whole** (a single paper fits model
   context); a large set (an accumulated Project corpus or a preprocessed
   collection, §5) is reached by **paragraph retrieval** (§4.4). The agent answers,
   cites, and mints knowledge units — which feed step 1 next time.

The steps cooperate rather than strictly fall through. Recall over the literature
is bounded by ingestion (step 2), not by the fact query (step 1).

**Conflict resolution.** When entailed units disagree, two rules apply in order: a
**retraction gate** drops units from retracted versions (§6.1), then — **within an
equivalence class** (the versions of one work, §2.2) — **temporal recency** wins
(version of record supersedes preprint; a correction supersedes the version of
record). *Across* classes there is no recency: two independent works disagreeing is
a genuine evidence conflict, reported in the support/refute tally, not silently
resolved.

#### Worked example — a probe through the funnel

**Probe.** *"Is there functional evidence that the KCNJ11 p.Arg201His variant
impairs K_ATP channel function?"* (a PS3 question during variant curation).

- **Understand.** → entities `HGNC:6257` (KCNJ11), `HGVS:NM_000525.4:c.602G>A`
  (p.Arg201His); hypothesis "the R201H variant reduces K_ATP channel function";
  target cell **PS3** (functional).
- **Retrieve.** grounded-entity match returns units whose entity set includes the
  variant; KU-embedding match adds paraphrases ("Kir6.2 R201H lowers ATP
  sensitivity") and any unit the linker failed to ground → ~12 candidate units
  across 5 papers.
- **Entail.** judge each against the hypothesis → **4 support** (e.g. "the R201H
  mutant channel showed markedly reduced ATP sensitivity in vitro"), **1 refutes**
  (no measurable effect in a different assay), the rest **neutral** (mention with
  no functional measurement).
- **Aggregate.** 4 supporting / 1 refuting, each an independent report with its own
  citation — **not merged** (independent support is evidence weight; the conflict
  is surfaced, not hidden).
- **Verify (gated, §3).** for the load-bearing supporting unit the agent resolves
  `cite:{doc_id, version, spans}` → the gated read-tool slices the span (entitlement
  permitting) so a curator sees the exact sentence.
- **Coverage check (step 2, in parallel).** the same probe runs against the 37M
  abstract corpus; a recent paper matching "KCNJ11 R201H functional" is **not yet
  in the KU DB** → queued for ingestion, after which its units join the substrate
  and the probe is re-answerable with fuller recall.

The agent produced the support/refute tally from facts (step 1), grew coverage
(step 2), and validated against source (step 3) — reading no paper to compute the
tally.

### 4.2 Knowledge-unit structure — two layers

- **Layer 1 — free-form units (immutable, append-only).** Paragraph-level
  extraction in the style of Project Alexandria
  (<https://arxiv.org/abs/2502.19413>): entities, attributes, and relationships
  with stylistic content stripped, which is the basis for non-copyrightability.
  Domain-agnostic; the costly LLM pass. Extracted **append-style** (§4.5), not
  per-paragraph-in-isolation. The jsonl is **write-once — what the model emits**:
  assertions plus raw, unresolved entity-mention strings, with **no stable entity
  ids** (the model doesn't assign them at generation time). Entity identity is
  resolved in a separate pass into an **entity→KU map** stored alongside (§2.1,
  `entities.jsonl`): it mints a stable local id per resolved entity, clusters that
  entity's mentions across units, and indexes which units mention it — so other
  layers reference entities by id while the write-once jsonl is never rewritten
  (rebuild the map with a better resolver, Layer 1 untouched). Unit refinements are
  append-only — a later unit carries a `supersedes` pointer to the unit it refines,
  never mutating it. **Queries resolve
  to the head of the supersedes-chain** (the latest non-superseded unit for an
  id); superseded units are retained for provenance, not returned by default.
  There is no compaction in Layer 1 (the substrate grows monotonically); bounding
  query cost is the dedup/merge work tracked in §9. Source linkage is by
  **character-offset spans into the pinned source markdown** (see *Source
  anchors* below), **never** verbatim text.
- **Layer 2 — grounding overlay (derived, recomputable).** Controlled-vocabulary
  and ACMG-type pointers that *reference* Layer-1 entities/assertions —
  "this entity is an instance of `HGNC:1100`", "this assertion is a PS3-type
  functional claim" — each tagged with the grounding-model version that produced
  it. Grounded identifiers are **`DB:id` CURIEs** (`HGNC:`, `OMIM:`/`MONDO:`,
  `HPO:`, `HGVS:`/`dbSNP:`) — one uniform `prefix:id` key across every ontology the
  layer targets, so resolution, display, and cross-references are prefix-dispatched
  rather than per-source. Recomputing the overlay (improved entity-linker, extended
  ontology) does **not** re-run extraction; Layer 1 is untouched.

Here "grounding" means **ontology-linking** (Layer 2 — resolving an entity/assertion
to a `DB:id` CURIE or ACMG type), *not* text-faithfulness. Extraction is **always**
text-faithful: the floor is transcribing the paper's stated facts, which are
grounded-in-text trivially. Cross-paragraph synthesis — a fact assembled by reading
several passages together — is **in scope**, but carries `epistemic: inferred`
(per PRODUCT.md §6) so a reader can tell a transcribed fact from a synthesised one.
What a unit can lack is an **ontology** grounding (no CURIE resolved yet), and that
is the goal-not-a-gate:

Grounding is a **goal, not a gate**: extraction never drops a fact that fails to
ground. Grounded/typed units are the first-class, queryable, cross-Project
substrate; ungrounded free-form units persist and are upgraded as grounding
improves.

Every unit carries: the assertion (expression stripped); ≥1 anchor entity
(ontology-grounded where possible — HGNC / HGVS / MONDO-or-OMIM / HPO); a
citation pointer (**`(doc_id, version)`** + character-offset spans (see *Source
anchors* below) — PMID/DOI for display; version-pinned so revisions/merges never
re-attribute it (§2.2); never verbatim);
provenance (extractor
model+version, date, source); epistemic status (observed / inferred / assumed,
per PRODUCT.md §6) and confidence; optional ACMG cell(s) the unit bears on.
Typed assertion categories align to ACMG evidence (gene–disease association;
variant observation / proband count + zygosity; functional effect; population
frequency; segregation; *de novo*); a free-form type carries the long tail. One
paper yields many atomic units.

**Worked example.** A paragraph stating *"Gain-of-function mutations in KCNJ11 or
ABCC8 cause neonatal diabetes"* yields a write-once Layer-1 unit — the model's
assertion plus raw, unresolved mention strings, no entity ids:

```jsonc
// knowledge_units.jsonl  (Layer 1 — write-once, exactly as the model emits it)
{ "id": "u17",
  "assertion": "Gain-of-function mutations in KCNJ11 or ABCC8 cause neonatal diabetes",
  "mentions": ["KCNJ11", "ABCC8", "neonatal diabetes"],   // raw strings, unresolved
  "type": "gene-disease-association",
  "epistemic": "observed", "confidence": 0.9,
  "cite": {"doc_id": "9f3a-…", "version": "pmc-v2", "spans": [{"start": 4012, "end": 4083}]},
  "prov": {"extractor": "gemma-4-31b", "date": "2026-06-14"} }
```

A later paragraph adds mechanism and permanence. The refinement is **appended and
points back** — `u17` is never edited:

```jsonc
{ "id": "u41", "supersedes": "u17",
  "assertion": "Activating mutations in the K_ATP-channel subunits KCNJ11 (Kir6.2) and ABCC8 (SUR1) cause permanent neonatal diabetes by suppressing insulin secretion",
  "mentions": ["KCNJ11 (Kir6.2)", "ABCC8 (SUR1)", "permanent neonatal diabetes"],
  "type": "gene-disease-association", "epistemic": "observed", "confidence": 0.95,
  "cite": {"doc_id": "9f3a-…", "version": "pmc-v2", "spans": [{"start": 5566, "end": 5701}]}, "prov": {…} }
```

A query for this fact returns `u41` (the head of the supersedes-chain); `u17` is
retained for provenance, not returned by default.

The **entity→KU map** is built by a separate resolution pass and stored alongside.
It mints the stable entity ids, clusters the raw mentions (here `"KCNJ11"` and
`"KCNJ11 (Kir6.2)"` resolve to one entity), and indexes the units each entity
occurs in — all without touching the write-once jsonl:

```jsonc
// entities.jsonl  (derived, write-alongside, rebuildable) — resolution only
{ "eid": "e30", "canonical": "KCNJ11",
  "mentions": ["KCNJ11", "KCNJ11 (Kir6.2)"], "kus": ["u17", "u41"] }
{ "eid": "e31", "canonical": "ABCC8",
  "mentions": ["ABCC8", "ABCC8 (SUR1)"], "kus": ["u17", "u41"] }
{ "eid": "e32", "canonical": "permanent neonatal diabetes",
  "mentions": ["neonatal diabetes", "permanent neonatal diabetes"], "kus": ["u17", "u41"] }
```

The **Layer-2 grounding overlay** is computed on top of the resolved entities and
only *references* their ids — re-running an improved entity-linker rewrites the map
and overlay and never touches `u17`/`u41`:

```jsonc
// grounding overlay (derived, recomputable) — each entity maps to a DB:id CURIE
{ "unit": "u41", "grounding_model": "themis-linker-v3",
  "entities": [ {"eid": "e30", "curie": "HGNC:6257"},        // KCNJ11
                {"eid": "e31", "curie": "HGNC:59"},          // ABCC8
                {"eid": "e32", "curie": "MONDO:0010890"} ],  // permanent neonatal diabetes mellitus
  "assertion_type": "gene-disease-association" }
```

(A *functional* assertion — e.g. "the R201H KCNJ11 variant reduces ATP sensitivity
of the K_ATP channel" — would carry a variant anchor as a CURIE
(`HGVS:NM_000525.4:c.602G>A`, or `dbSNP:rs104894157`) and a `PS3`-type overlay
instead.) This is what "grounding is an overlay, not a gate" (§4.2) buys:
`u17`/`u41` stay queryable as free-form facts even before any IRI resolves, and
improving the ontology never disturbs the extracted assertions.

#### Source anchors — offsets, never quotes

A unit's `cite` records **`(doc_id, version, spans)`** — the paper id, the exact
markdown version it was extracted from, and one or more **character-offset ranges**
into that version. It stores **no source text**.

- **Producing the offsets.** The extractor emits a verbatim supporting quote
  (models quote far better than they count characters); an alignment step locates
  that quote in the pinned markdown — exact match first, fuzzy/edit-distance on
  near-misses, with an explicit *unlocatable* outcome — and records the resulting
  span(s). The quote is then **discarded**: only the offsets persist. So a shared
  unit is an expression-stripped assertion plus numeric pointers — it reproduces
  nothing copyrightable (§4.3). **Multiple spans** cover a fact assembled from
  non-contiguous sentences (e.g. a methods sentence plus a results sentence).
- **The markdown is the coordinate system, so it is immutable and pinned.** A
  version artifact is never overwritten (§2.1); the **markdown-converter version is
  part of the version's identity** (re-converting mints a new version); a version
  with live units is never garbage-collected. Offsets are codepoint indices over a
  frozen Unicode normal form.
- **Version lift-over — two kinds.** *Mechanical* (same text, new markdown from a
  better converter): slice the original span by its offsets and relocate it in the
  new version — deterministic, the old version is the bridge, the original anchor
  retained. *Semantic* (a published version superseding a preprint, where peer
  review may reword, add, or drop): the prior version's KUs **seed** the new
  version's extraction (the two-stage shape of §4.5), partitioning its output into
  **new** assertions; **lifted** facts — a superseding unit (`supersedes`) anchored
  to the new version, **only if its supporting span actually relocates there** (an
  unconfirmable lift is not lifted); and a **silent residual** — a prior fact the
  new version doesn't carry persists, prior-version-cited (dropped-in-revision is
  indistinguishable from extractor-miss). A contradiction surfaces as an ordinary
  *negative* unit on the new version. Either kind runs on the gated plane (it needs
  the text) and never edits the original unit's assertion.
- **Resolution is gated.** Turning spans back into text means slicing the gated
  markdown, so grounding is visible exactly where the reader is already entitled to
  that paper (§3) — OA to everyone, licensed/captured to entitled readers only. An
  unentitled consumer of the shared substrate gets an assertion verifiable by us
  but not by them; that is intended.

### 4.3 Knowledge-unit substrate shares; sources do not

Knowledge units cross Projects and institutions (non-copyrightable assertion;
citation is offsets, not text — *Source anchors*, §4.2). Resolving a citation to
verbatim source text is gated per §3.

### 4.4 Paragraph embeddings — body-resolution discovery

An embedding index over the **bodies** of cached papers (one vector per
paragraph), built incrementally as papers enter the cache. Its job is **paper-
level discovery** — *find papers about X* — at body resolution, complementing the
corpus-wide abstract-embedding search. In the funnel its role is **§4.1 step 2 —
ingestion targeting**: surfacing papers not yet in the KU DB for capture, *not*
answering fact probes (those go to the KU substrate, §4.1 step 1). (It is also
**not** for within-paper Q&A — single papers are read whole — nor for extraction
support; those rationales were superseded by whole-paper reads and eager append
extraction, §4.5.)

- **Mechanism.** Embed each body paragraph; score a query against a paper's
  paragraphs and **max-pool to a paper score** (relevant if *any* paragraph
  matches). **RRF-fuse** with the abstract-embedding search, then **MedCPT
  rerank** the fused top-N (§4.1). A paper in the full-text subset gets two signals
  (abstract + body); a paper outside it gets abstract-only.
- **Two backends, one id space.** Paragraph search runs on **pgvector** (the
  cached subset; §2.4) and abstract search on the **vm_search ANN VM** (the 37M
  corpus). Fusion is on the **canonical UUID** (§2.2), but UUIDs are needed only
  for the *fused candidate set*, not all 37M: the abstract arm returns corpus
  gids/PMIDs and its **top-N** are resolved gid→UUID through the crosswalk (minting
  a UUID for any corpus paper entering the candidate set — N is hundreds);
  paragraph hits are already UUID-keyed. Cached papers carry both ids and fuse to
  two signals; uncached papers union abstract-only; paragraph-only preprints union
  on their own UUID.
- **Where it wins: body-bound queries.** Recall improves specifically for queries
  whose subject is in the body, not the abstract — a method, reagent, secondary
  finding, organism-as-control, or specific value. Measured: **~42% of
  abstract-retrieval-missed golds were unfindable by abstract embeddings even at
  depth 3000** (X7), their relevant content being body-bound — shapes common in RD
  curation ("papers using an animal model of Y", "a functional assay on variant
  Z"). For abstract-topic queries the gain is small (the abstract already finds
  them); max-pool also lifts false positives, which the MedCPT rerank absorbs.
- **Cheap, scoped to the cached subset, built now.** You can only body-embed
  cached bodies, so paragraph search covers cached collections, not the 37M corpus
  — the cheap win exactly where caching is invested, **deferring the
  37M-full-text embed** (the cold-corpus body-recall lever, a separate major
  build). Vectors are cheap (~4M for a 100k seed, one-time GPU-hours, E5-base) and
  slot into the write-through cache, served from **pgvector** (small, incremental,
  filter-scoped — §2.4). Collections (§5) cache RD papers → body-embedded →
  surface more RD papers → cached in turn, so coverage compounds with use. A
  never-cached body-bound paper stays reachable only by abstract search. Building
  this is **not** a bet against the KU substrate.
- **Open: value relative to the KU substrate.** For discovery, the grounded KU
  substrate (§4.2) is a third signal over the same subset (search KUs/entities →
  papers). Whether paragraph-embedding discovery and KU/entity discovery are
  complementary or one dominates is **unresolved** — resolve empirically (§9).
  Embeddings are built because they are cheap and natural here, not because they
  win that comparison.

### 4.5 Extraction mechanism — measured

These are empirical, from a one-paper 6-config probe + a 20-paper scale-up +
an append/single-para A/B. They override the earlier estimates where they differ.

- **Append, not per-paragraph-in-isolation (X2).** Each paragraph is extracted with
  the *earlier paragraphs as context* (a cached, growing prefix) plus a rolling
  list of recently-extracted units, instructed to resolve references and not
  duplicate. Measured against independent-paragraph extraction (Haiku fixed, 3
  papers), append produced **fewer but better units**: it resolved coreference
  from context (vague subject → named entity), consolidated duplicates, dropped
  metadata noise (DOIs/authors mis-extracted as "facts"), and added inferential
  context the isolated pass lost — an LLM side-by-side rated append more complete
  on every paper. Coreference-resolution-at-extraction is its distinctive value
  and can't be recovered later, which matters because the grounding overlay and
  cross-Project merge *require* resolved entities (you can't ground "the protein").
- **Cost of append ≈ 1.4× independent, not 2×.** The growing paper-so-far prefix
  is a real cache prefix (measured 98k–206k cache-read tokens/paper), holding the
  input premium down; append also emits ~30–40% fewer units. It is **sequential
  per paper** (parallelise across papers, not within).
- **Model tier and reasoning don't move extraction (X3).** Extraction is a transform,
  not a judgement. Over 20 papers, Haiku−reasoning ≈ Haiku+reasoning ≈
  Sonnet+reasoning on answer-coverage (reasoning recovered 0 papers; a bigger
  model recovered 0 of the misses). So extraction runs on the **cheapest tier
  (Haiku) without reasoning**. (Reserve reasoning for the *judgement* steps — the
  gate and answer-step, §4.1 — where it is separately decisive (X6, §7.2). A
  single-paper result suggesting reasoning helped extraction did **not** replicate
  at n=20 — it was variance.)
- **Open-weight can *exceed* Haiku at extraction, not just match it (X4).** Gemma-4-31B
  (self-hosted, vLLM) vs Haiku 4.5 over 17 OA papers, append-style, judged by
  Sonnet: Gemma rated **more accurate on 17/17** papers and **better at coreference
  on 14/17** (Haiku 2, 1 tie); answer-coverage tied (Gemma 16/17 + 1 partial,
  Haiku 17/17). Gemma resolved abbreviations/co-citations more completely
  ("platelet-derived growth factor (PDGF)-AA"; "*P. syringae* pv. tomato DC3000
  hopM1⁻/avrE1⁻ double mutant"), while Haiku committed outright errors Gemma
  avoided — a wrong author affiliation, and *inverting* a paper's claim about
  imaging resolution. Two facts make this **conservative**: the judge is an
  Anthropic sibling of Haiku (self-preference would favour Haiku, yet Gemma won),
  and Haiku extracted *more* units (#h > #g on every paper) but scored *less*
  accurate — its extra volume was verbosity, not coverage. So "tier doesn't move
  extraction" (Haiku↔Sonnet) extends downward to open-weight at 31B, and slightly
  *up* in quality. Caveat: n=17, single judge/paper (not a panel); only the 31B
  size is verified — smaller candidates (§7.1) are untested. This resolves §7.1's
  in-flight quality test in the floor option's favour at the 31B tier.
- **Residual failure mode: figure-bound facts.** The one extraction miss that
  survives every model/reasoning/design choice is a value stated only in a figure
  image (e.g. "lower in X and Y than Z [Fig 1B]"; the text gives a partial
  comparison, the ranking is in the figure). Text extraction can't reach it; the
  fix is **multimodal figure reading**, not a bigger model, better table rendering,
  or XML-vs-PDF (figures are images either way). Tables themselves render fine.
- **Measured cost: ~$0.15–0.23/paper (X9)** (Haiku, append, real OA papers) — real
  papers run larger than §7.1's 40-paragraph assumption, so the §7.1 sweep
  estimate is ~2× low; budget **~$15–23k for a 100k-paper sweep**.
- **KU quality needs a real metric.** It is invisible to answer-coverage (LitQA2
  questions don't test coreference) and is *not* captured by raw embedding-cosine
  dedup (cosine>0.9 fires on same-*entity* units, not true duplicates). Use an
  entity-normalised dedup metric or an LLM judge — build it before trusting a
  quality number.

## 5. Collections

Selection of large paper sets to preprocess (cache + embed + extract). Multiple
first-class, tagged **collections**, not one cut; the agent can scope retrieval
per collection (e.g. per ACMG evidence type or disease area). Membership is
many-to-many with per-route provenance, ingested via several routes whose union
forms a collection:

- an existing **curated set** (human-picked, high trust);
- **MeSH / publication-type** selection (e.g. `Rare Diseases`, `Case Reports`,
  organism check-tags) — free, high precision, a training signal;
- a **supervised classifier on embeddings** (E5 + logistic regression; measured
  AUC 0.926 separating relevant from not on a rare-disease-gene-discovery query —
  X8) for recall across the 37M corpus;
- **citation / semantic expansion** from known relevant papers.

Selection is **recomputable and incremental** (improve the classifier, re-score,
grow the cache), mirroring the grounding overlay. Candidate (classifier-derived)
collections carry lower trust than curated sets and are promoted by curator
validation (mirroring the working-document→Report accept step). The same
classifier family produces category facets (case report, functional study,
animal model) as filterable tags.

## 6. Security, egress, and integrity

The layer adds **no new agent-side egress surface**; it rides themis's existing
model (`spike-infrastructure.md` §8, PRODUCT.md §9):

- The agent reaches sources only through **tightly-typed MCP tools**; the
  self-hosted sandbox has deny-by-default egress and **nothing to exfiltrate
  to**.
- Outbound network calls happen **infra-side, never under agent control**, under
  an egress allowlist:
  - **bulk-available** sources (PubMed/PMC, the OA subset) are **mirrored
    locally** — no live querying;
  - **non-bulk** resolution/metadata services (Crossref, Semantic Scholar,
    Unpaywall, arXiv) are queried **live by the discovery plane / capture
    worker**, not the agent.
- Source full text is **untrusted content**: knowledge units derived from it
  carry provenance and epistemic status — asserted by a source, not ground truth
  (PRODUCT.md §6). No new mechanism is needed beyond the tool surface, the
  sandbox, and provenance.

### 6.1 Retraction and currency

A retracted or withdrawn paper's knowledge units stay in the immutable
append-only substrate (§4.2) — they are **not deleted**. Retraction is handled
as a **paper-level overlay fact, checked at resolution time**, not a mutation of
units:

- **Flag, don't purge.** Retraction/withdrawal is recorded as a flag on the
  paper: in `manifest.json` (GCS source of truth) and its Cloud SQL projection.
  Detection rides the existing non-bulk metadata path (§6) — PubMed retraction
  notices and Crossref retraction metadata, polled by the discovery/capture
  worker, never the agent.
- **Every result resolves to a paper; the flag propagates on use.** Each KU and
  each paragraph-embedding hit resolves to a `(doc_id, version)` (§2.2). The
  gated read-tool and the Q&A path **re-check the retraction flag at resolution
  time** and surface it alongside the result, so a retracted source can never be
  cited as live evidence — decisive for ACMG-evidence use. This makes currency a
  query-time check rather than a substrate-purge, consistent with append-only
  Layer 1 and the recomputable overlay. Retraction is the **gate applied before
  within-class temporal recency** (§4.1, §2.2): a retracted version is suppressed
  regardless of how recent it is — recency orders versions, retraction removes them.
- **Open:** retraction *detection latency and coverage* (PubMed/Crossref lag,
  preprints with no formal retraction channel) — see §9.

## 7. Compute and storage

Seed scale: ~100k open-access papers. Most boundary-plane storage maps onto
infrastructure the Spike already provisions (Cloud SQL Postgres, GCS, Cloud Run,
MCP tunnels); the layer is largely additive tables, buckets, jobs, and tools.

| Asset | Size / cost | Where |
|---|---|---|
| Cache markdown (~100k) | ~4 GB | GCS |
| Paragraph embeddings (~4M × 768-d) | ~6 GB halfvec/fp16 | pgvector in Cloud SQL — small + incremental + filter-scoped (§2.4) |
| Abstract embeddings (~37M × 768-d) | 110 GB HNSW index | vm_search ANN VM (NVMe + Rust HNSW), *not* pgvector (§2.4) |
| Knowledge-unit substrate + overlay | <1 GB (~0.5–2M units) | Cloud SQL (Postgres) |
| Corpus columns (37M, subset) | tens of GB, disk not RAM-resident | Cloud SQL (Postgres) — bulk-loaded from ingestion parquet (§2.3) |
| Paragraph-embedding compute | one-time, hours on one GPU (E5-base) | batch job |
| Knowledge-unit extraction (variable cost) | the dominant cost — see §7.1. **Measured ~$15–23k** for a 100k full sweep (Haiku, append; batched ~half); self-hosted spot ~$6–8k (X4, X5, X9) | LLM API / GPU |
| Classifier scoring (37M) | minutes (LR over existing E5 vectors) | shared plane |
| MedCPT rerank | CPU adequate at shortlist depth (~30 s for a 200-candidate pool); GPU only for deep pools | shared plane |

Storage is negligible. The only material variable cost is **extraction LLM
spend** — modelled in §7.1.

## 7.1 Knowledge-unit extraction cost

Extraction is the one part of the system whose cost is large and worth modelling
before committing to a full-corpus sweep. The cost is:

```
calls × (input_tokens × input_price + output_tokens × output_price)
```

modulated by prompt caching and the Batches API. Four cost drivers, in order of
leverage:

1. **Output tokens × output price — the irreducible floor.** Output cannot be
   cached. At a full sweep this term alone dominates, and **model choice is the
   biggest single lever** (Haiku output is 3× cheaper than Sonnet).
2. **Call count** = papers swept × paragraphs/paper, where *paragraphs/paper* is
   the **extraction call granularity** (one append step per paragraph, §4.5) — not
   a claim that knowledge units are one-per-paragraph (a paragraph yields many
   units, or none). Extraction is an eager whole-paper append sweep — every
   paragraph extracted, no within-paper fraction — so the only lever is *which
   papers* are swept: **collection selection (§5)** confines the sweep to the
   cached RD-relevant subset, not the
   37M corpus. (Paragraph embeddings serve discovery, not extraction targeting —
   §4.4.)
3. **Prompt caching.** The extraction prompt's fixed prefix (schema +
   few-shot examples, ~1.5k tokens) is identical on every call → cache reads at
   ~0.1× input price. Kills the fixed-input cost; does nothing for output.
4. **Batches API.** 50% off everything. Bulk extraction is non-latency-
   sensitive, so this is free money — always batch the sweep.

**Modelled** (superseded by measurement below; kept for the driver logic, not
the numbers). Assume 100k papers × ~40 paragraphs = 4M paragraphs; per call
~2,350 input (350 paragraph + ~500 rolling prior-KU + ~1,500 cacheable
schema/few-shot) + ~400 output; catalogue prices Haiku $1/$5, Sonnet $3/$15 per
1M, cache read ~0.1× input, Batches −50%. The model said Sonnet+cache+batch
~$18k, Haiku+cache+batch ~$6k. The instructive part is *why*: **output sets the
floor** — 4M × 400 output tokens is ~$24k at Sonnet pricing *before* batch, and
caching can't touch output. So model choice (Haiku output 3× cheaper) is the
biggest lever; caching and Batches trim the rest.

**Measured** (X9, the operating number). **~$0.15–0.23/paper on real OA papers**
(Haiku, append). Real papers run larger than the 40-paragraph assumption, so the
modelled sweep was ~2× low; a full 100k sweep is **~$15–23k**. The modelled
per-call also assumed the small schema prefix caches — in practice it's the
*paper-so-far* append prefix that caches (§4.5). **Caveat (§9):** probes likely
ran **synchronously**, not through Batches; if so the batched production sweep is
~half (~$0.08–0.12/paper → **~$8–12k**). Use the batched figure as baseline.

**Floor option — self-hosted open model.** A self-hosted model costs GPU-hours,
not per-token. Both legs are now **measured** (Gemma-4-31B, 4×A100-40GB, §4.5),
not modelled: quality **beats Haiku** (risk retired at the 31B tier), and
graphs-on throughput is **19.7 papers/hr/A100** (eager was ~2 — a config
artifact, not a hardware limit). At that rate it is **cheaper than Haiku-batch
only on spot** (~$6–8k vs ~$10k for 100k); on-demand it is ~1.9× *pricier*
(~$19k). So it earns its place as a **spot-priced, recurring/at-scale**
extractor — still a later optimization than the Spike, but a viable one.

- **Compute, measured (not modelled) (X5).** Gemma-4-31B on 4×A100-40GB, **graphs-on**,
  append extraction at concurrency 17: **19.7 papers/hr/A100** (78.8/hr on the
  box), GPUs at 100% util. So **$/paper = (A100 $/hr) ÷ 19.7**: spot
  (~$1.1–1.5/A100-hr) → **$0.06–0.08/paper → ~$6–8k for 100k** (under
  Haiku-batch); on-demand (~$3.7) → ~$0.19 → ~$19k (over). Break-even with
  Haiku-batch is ~15 papers/hr/A100 on spot, ~37 on-demand — **spot clears it,
  on-demand doesn't.** (Eager mode managed only ~2 papers/hr/A100 → ~$185k; it was
  a workaround for a CUDA-graph-capture crash on a missing `ninja` build dep, not
  a real ceiling. With ninja installed, graphs-on boots clean after a one-time
  ~27 min compile/capture.)
- **Week deadline.** 100k/168h ≈ 595 papers/hr → **~30 A100-40GB** at 19.7/hr/A100,
  i.e. **~3–4 days on spot** — within reach of existing preemptible quota, no new
  quota request needed. Spot preemption ⇒ **checkpoint per paper** (the eval run's
  write-only-at-end design lost an entire run to one tunnel drop — same lesson).
- **Bottleneck is prefill, not decode.** 299 tok/s output at 100% util means the
  GPU is busy re-prefilling the ~8k-token append window each chunk, not
  generating — vLLM prefix-caching holds the *paper-so-far* prefix across
  paragraphs (the API bills those re-reads; self-hosted doesn't), but each new
  chunk still extends it. More GPUs/concurrency won't raise per-GPU throughput
  (already saturated); the levers are **bigger chunks** (fewer append steps → less
  repeated prefill) or **faster-prefill hardware (e.g. H100)**, either of
  which could also reach on-demand parity.
- **Candidates** (current small-open instruct field): Qwen3-14B/32B, Mistral
  Small 3 (24B), Gemma 3 12B — permissive licences, all with vLLM guided-JSON
  decoding for structured output (replacing the API's structured-output
  enforcement).
- **Quality risk — retired at the 31B tier (X4, §4.5).** §4.5's "tier doesn't
  move extraction" was Haiku-vs-**Sonnet** and did not on its own license
  "small-open = Haiku." A direct test now does, *upward*: Gemma-4-31B *beat* Haiku
  on KU-extraction quality under a conservative Haiku-sibling judge — open-weight
  at 31B is a quality **upgrade**, at ~10–30× lower compute cost. Two questions
  remain before committing the corpus: (a) do the **smaller/cheaper candidates**
  (12–24B) hold that quality, or is ~31B the floor? — A/B them on the KU-quality
  metric (§9); (b) re-price compute for whichever size passes (next bullet).
- **Ops cost is real and excluded from the compute figures above** (vLLM serving,
  batching, retries, a GPU box to stand up and babysit). For a *one-time* sweep —
  self-hosted spot ~$6–8k vs Haiku-batch ~$8–12k — that thin margin doesn't repay
  the ops overhead under cost-unconstrained exploration (PRODUCT.md §2/§6); it
  earns its place for **recurring / at-scale** extraction — incremental ingestion
  at full corpus scale, or an extractor-model upgrade that re-sweeps (S3).

## 7.2 Reasoning and the cost matrix

Reasoning (extended/adaptive thinking) materially improves **abstract
judgement** — observed directly in the prepass gate, where a reasoning gate
skipped 0/28 golds versus 10/28 for a snap, no-reasoning gate. The cost catch:
**thinking tokens bill as output**, and output is the dominant, uncacheable term
(§7.1). So reasoning multiplies the expensive half of every judgement call, and
**where it's affordable depends entirely on how many calls there are**.

**Per-1,000 judgement calls** (an abstract-relevance decision: input ≈ 1,500
cacheable prefix + ~500 abstract/question; output ≈ 50 tokens no-reasoning vs
~550 with ~500 thinking tokens; fixed prefix cached at 0.1×, no batch — this is
query-time, latency-sensitive). Thinking volume scales with effort (~200 low →
~1,500+ high), so treat the reasoning column as a mid-effort estimate:

| Model | no reasoning | + reasoning |
|---|---|---|
| Haiku | ~$0.90 | ~$3.40 |
| Sonnet | ~$2.70 | ~$10.20 |
| Opus | ~$4.50 | ~$17.00 |

Reasoning is ~3–4× the per-call cost (output goes ~50 → ~550); the model ladder is
a further ~3× (Haiku→Sonnet) and ~1.7× (Sonnet→Opus).

**Applied at the two scales in this system:**

- **Per-query judgement (the gate, the answer-step) — reasoning is cheap, use
  it.** A walk of 30 candidates is 30 calls/query; even **Opus + reasoning ≈
  $0.51/query** (200 candidates ≈ $3.40/query). At interactive Spike volumes
  this is negligible — so spend reasoning here, where it's *observed* to matter
  and N is bounded.
- **Corpus-sweep judgement (extraction, or any LLM-classification at 100k+
  scale) — reasoning is expensive.** Over the 4M-paragraph sweep (batched, cached
  prefix), adding ~500 thinking tokens/call lifts the floor sharply:

  | Model | sweep, no reasoning | sweep, + reasoning |
  |---|---|---|
  | Haiku | ~$6k | ~$11k |
  | Sonnet | ~$18k | ~$33k |
  | Opus | ~$30k | ~$55k |

  (Targeting to ~25% of paragraphs divides each by ~4.)

**The principle: reasoning pays off for *judgement*, not for *extraction or
classification*.** Put reasoning on the gate and the answer-step — small N,
judgement is load-bearing, and measured to matter (X6, above). Run the bulk
**extraction** sweep *without* reasoning on Haiku — neither reasoning nor tier
moves extraction (X3, §4.5). Likewise keep collection selection (§5) on the
E5+LR classifier, not an LLM-judgement pass — a reasoning LLM over 37M abstracts
would be the most expensive thing in the system. Extraction is a transform; the
gate and answer-step are judgements — spend reasoning only on the latter.

## 8. Staged build

Each stage stands alone and is additive; ordering is a guide, not a critical
path — stages parallelise and reorder to favour fast iteration. Effort is
eng-weeks for a small team.

- **S0 — Cache + capture + whole-read Q&A (~2–4 wk).** Licence-tagged
  write-through cache; upload + proven-access fetch; discovery→whole-read MCP
  tools. Reuses pubmedifier's full-text ladder and per-user credentials. Serves
  the Spike (`variant + condition → ACMG`, public sources) immediately.
- **S1 — Seed + paragraph retrieval (~3–6 wk).** Collection selection v1 (MeSH +
  E5+LR classifier); fill the ~100k OA cache; paragraph embeddings; passage-
  retrieval tool. Answers specific RD questions over a real corpus.
  Parallelisable with S0.
- **S2 — Knowledge-unit substrate (~4–8 wk, widest variance).** Free-form
  extraction (Layer 1) + grounding overlay (Layer 2) + substrate store +
  knowledge-unit-first Q&A path. **Entity-linking/grounding quality is the
  load-bearing unknown, not just fiddly** — HGVS/variant normalisation is
  unsolved (§9), so the upper end of the range is the realistic planning figure;
  the recomputable overlay (§4.2) de-risks shipping with partial grounding rather
  than collapsing the estimate. Yields cheap, cited, cross-Project facts and ACMG
  evidence population.
- **S3 — Scale and share (ongoing).** Grow and validate collections; cross-
  Project knowledge-unit sharing; physical OA-tier split into the shared plane;
  more publishers; MedCPT rerank in discovery; deeper grounding.

## 9. Open questions

- How a user's **institutional affiliation** is established and trusted for
  proven-access fetch (also open in `workspace-model.md`).
- **Grounding/entity-linking quality**, especially HGVS/variant normalisation —
  now load-bearing on **both sides** of the probe path (§4.1): the query's entities
  and the units' entities must resolve to the same CURIEs. The recomputable overlay
  de-risks but does not solve it; like extraction, it needs its own eval.
- **KU semantic embedding** — the probe→KU retrieve step (§4.1 step 1b) wants a
  fact-level semantic index (~13 GB, §2.3) to catch paraphrase and ungrounded units.
  Either pay it or accept the recall ceiling of a grounded-only path; reopens the
  deferral implied in §4.4/§2.3.
- **Entailment-judge calibration** — the support/refute judge (§4.1 step 1c) is the
  KU-quality scorer's NLI judge; its stability and cross-judge agreement gate the
  probe path's precision, so the scorer's robustness suite is the shared instrument.
- **OA redistribution rights** — the licence representation is settled (§2:
  canonical identifier → derived policy booleans). Residuals: confirming the
  source-metadata → identifier mapping (PMC/Crossref/Unpaywall coverage and
  conflicts), and the **NC/commercial determination** — whether a clinical-genomics
  product counts as commercial use under a `CC-BY-NC` licence.
- **Knowledge-unit dedup/merge correctness** across papers — the cross-Project
  signal's value depends on it; needs an entity-normalised metric (raw embedding
  cosine doesn't work — §4.5).
- **Figure-bound facts** — values present only in a figure image are unreachable
  by text extraction (§4.5). Whether to add a **multimodal figure-reading** pass
  (and at what cost/coverage) is open; XML-faithful markdown does not help here.
- **A KU-quality metric** — extraction quality (coreference, dedup, refinement)
  is invisible to answer-coverage; build a judge/entity-normalised metric to
  evaluate extraction-design changes (§4.5).
- **Next empirical study (moderately sized).** Three questions below resolve in
  one study over a few hundred papers, sharing one instrument and one caveat: KU
  quality is **not cheaply measurable empirically** (coreference/dedup/refinement
  are invisible to answer-coverage), so **LLM-as-judge is the quality ceiling** —
  use it, with its self-preference and single-judge limits acknowledged (X4).
  - **Smaller open-weight extractors** — Gemma-4-31B beats Haiku (X4); whether the
    12–24B candidates (§7.1) hold that quality, or ~31B is the floor, is untested.
    Re-price compute for whichever size passes.
  - **KU substrate vs paragraph embeddings for discovery** — over the same
    full-text subset both can answer "papers about X" (KU/entity search vs
    body-resolution paragraph search, §4.4). They likely have **different
    searchability properties**; whether complementary or one dominates is unknown.
    Build embeddings into the study so they are scored for their own weight, not
    assumed.
  - **Collection-selection generalization** — the classifier's AUC 0.926 (X8) is a
    single RD-gene-discovery query; precision/recall across disease areas and ACMG
    evidence types is untested, and selection sets the real extraction-cost lever
    (§5, §7.1).
- **Retraction detection latency and coverage** — the §6.1 flag is only as good as
  the signal feeding it: PubMed/Crossref retraction lag, and preprints with no
  formal retraction channel, bound it.
- **Extraction-cost measurement basis** — confirm whether the X9 cost probes ran
  synchronously or through the Batches API; if synchronous, the batched production
  sweep is ~half the measured figure (§7.1).

## Appendix: Experiment log

Every empirical claim in the body cites a row here by `X#`. The body holds the
*decision and interpretation*; this table holds the *method and raw result*, so a
future reader can weigh each finding without re-deriving it. (`X#` is the
experiment id; `E5` elsewhere is the embedding model, not an experiment.) Dates
backfill where unrecorded.

| # | Date | Question | Setup (n, models, judge) | Result | → |
|---|---|---|---|---|---|
| X1 | — | pgvector vs vm_search for the 37M abstract index | head-to-head over the live ~30M-doc E5 corpus; same `m=16`/`ef_construction=200`; identical queries; recall@100 ground-truthed by exact seq-scan; 32 GB and 256 GB hosts (`pubmedifier/docs/pgvector-bench.md`) | vm_search ~55× faster first-pass (≤0.89 recall): ~7 ms vs ~385 ms at 32 GB. pgvector's 110 GB HNSW degrades 3–4× spilled to pd-ssd. Its higher recall ceiling (0.984 vs 0.97) is a build-heuristic gap, not a backend property; `hnsw.ef_search` capped at 1000 (0.8.x) vs vm_search's 5120+ | §2.4 |
| X2 | — | append vs independent-paragraph extraction | 3 papers, Haiku fixed; append (prior paragraphs + rolling KU list as context) vs per-paragraph isolation; LLM side-by-side judge | append → fewer but better units: resolved coreference (vague subject → named entity), consolidated dups, dropped metadata noise, added inferential context; rated more complete on every paper | §4.5 |
| X3 | — | does model tier or reasoning move extraction? | 20 papers; Haiku−reasoning vs Haiku+reasoning vs Sonnet+reasoning; answer-coverage | no movement — reasoning recovered 0 papers, Sonnet 0 of Haiku's misses. (An earlier 1-paper signal that reasoning helped was variance.) Residual miss across **all** configs: figure-bound facts (value only in a figure image) — needs multimodal, not a bigger model | §4.5, §7.2 |
| X4 | 2026-06-14 | Gemma-4-31B vs Haiku 4.5 extraction quality | 17 OA papers, append; judged by Sonnet (a Haiku sibling → self-preference would favour Haiku) | Gemma more accurate **17/17**, better coreference **14/17** (Haiku 2, 1 tie); answer-coverage tied. Haiku extracted *more* units but scored less accurate (verbosity, not coverage). Caveat: n=17, single judge/paper, only 31B verified | §4.5, §7.1 |
| X5 | — | Gemma-4-31B serving throughput | 4×A100-40GB, vLLM, append, concurrency 17; graphs-on vs eager | graphs-on **19.7 papers/hr/A100** (78.8/box), 100% util, prefill-bound (299 tok/s decode). Eager ~2/hr was a ninja/CUDA-graph-capture artifact, not a ceiling. → spot $0.06–0.08/paper | §7.1 |
| X6 | — | reasoning value at the prepass gate | 28 golds; reasoning gate vs snap (no-reasoning) gate | reasoning gate skipped **0/28** golds vs **10/28** for the snap gate | §7.2 |
| X7 | — | abstract-retrieval body-bound miss rate | golds missed by abstract embeddings, checked to depth 3000 | **~42%** unfindable by abstract embeddings — relevant content body-bound; motivates body (paragraph) embeddings | §4.4 |
| X8 | — | collection classifier separability | E5 embeddings + logistic regression; rare-disease-gene-discovery query | **AUC 0.926** relevant-vs-not | §5 |
| X9 | — | measured extraction cost | Haiku, append, real OA papers (likely synchronous, not Batches — confirm §9) | **~$0.15–0.23/paper** → ~$15–23k for a 100k sweep; ~half if batched. Real papers run larger than the 40-paragraph model assumption, so the modelled sweep was ~2× low | §7.1 |
