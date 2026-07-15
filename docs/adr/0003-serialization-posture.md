# ADR 0003: Serialization posture — binary proto for authored data, JSON for external

**Status:** Accepted. Supersedes the at-rest half of [`../design/typespec.md`](../design/typespec.md) ("At-rest vs
on-the-wire", "Code generation", "Schema evolution", "Wire and RPC") and the closed-model / JSON-at-rest assumption in
[`0002-manifest-renderings-and-reference-model.md`](0002-manifest-renderings-and-reference-model.md). The structural
model ADR 0002 defines (sources, revisions, content-addressed renderings, the quote-durable cite) is unchanged; only its
serialization moves.

## Context

Durable litcache artifacts (`manifest.json`, `knowledge_units.jsonl`) are **closed JSON** (JSON Schema from TypeSpec,
sealed, `chuckd`-gated); inter-service RPC is already **binary proto** (`schema/proto/themis/rpc/auth.proto`, #91,
`buf breaking`-gated). This ADR moves the at-rest artifacts to binary proto too, joining the format the services already
speak — it does not change RPC or the browser-facing path (bucket 2, deferred).

The driver is a corruption risk closed JSON does not cover and binary proto eliminates by construction (below).

## Decision

Serialized data falls into three buckets by *who owns the schema* and *who consumes it*:

| Bucket                                                                        | Format                                       | Compat gate    | Owner of the shape |
| ----------------------------------------------------------------------------- | -------------------------------------------- | -------------- | ------------------ |
| Authored, internal machine-to-machine (at-rest artifacts + inter-service RPC) | **binary proto**                             | `buf breaking` | us                 |
| Authored, browser-facing (BFF↔frontend)                                       | **deferred** (protobuf-es vs Zod)            | —              | us (view model)    |
| Externally-defined, ingested (raw upstream payloads we cache)                 | JSON, our model a documented **subset/view** | read-side only | the upstream       |

TypeSpec stays the single IDL: it authors proto (bucket 1) today. The bucket-2 browser target is **deferred and not
committed** (protobuf-es vs Zod, see Consequences). Content-addressed blobs (`sources/`, `renderings/`,
`supplementary/`) are opaque bytes, unaffected.

## Why

- **Closed buys little under our constraints.** Given additive-only evolution, typed writers, and content-addressed
  blobs, an undeclared field is inert (ignored) or prevented at construction. Closed's residual value — catching a
  typo'd optional field on a hand-built write path, an out-of-band manifest edit, an accidental additive-rule violation
  — is narrow, and each has a cheaper cover (typed construction, review, `buf breaking`). Google's long use of proto as
  an at-rest format is the existence proof that open durable data is fine at scale.
- **The decisive risk is read-modify-write through a lossy model.** A component reads an artifact into a model that
  strips fields it doesn't know, then writes the stripped view back, dropping a field a newer writer added. **Binary
  proto is immune by construction**: unknown fields (keyed by number + wire-type) are retained in the message's
  unknown-field set and re-serialized. This is why at-rest is *binary* proto, not proto-JSON or pbtxt — both text
  projections are name-keyed and cannot round-trip an unknown field.
- **External data we don't own** may carry fields our model omits and may change shape without our say. We keep it as
  the upstream's JSON, model an explicit subset for the fields we read, and never round-trip it through a lossy typed
  write. Forcing external payloads into an authored proto would either lose data or couple our schema to theirs.

## Read-modify-write and integrity

The only artifact modified in place is the manifest (path-addressed by uuid; blobs are immutable). Safe RMW requires, in
order:

1. **Preserve unknowns.** RMW goes through the binary proto message, whose unknown-field set survives
   parse→modify→serialize. Never RMW through a lossy typed projection.
1. **Fail loud on the write path as a backstop** ("open on read, closed on write"): if write-back cannot account for
   content it didn't model, raise rather than drop. This is where the repo's fail-loud stance belongs — the
   modify-and-persist path, not every read.
1. **Atomic write-back.** GCS `ifGenerationMatch` precondition so a concurrent RMW can't clobber (lost-update is the
   other corruption vector).

Residual, unfixable generically: **semantic coupling** — preservation keeps an unknown field's bytes, not the artifact's
internal consistency if a field the writer *did* change is derived-from or invariant-with the preserved one. Mitigated
by keeping additive fields independent, and by (2).

## External data (bucket 3)

For a cached upstream payload (a raw Crossref or Unpaywall response): store the upstream's JSON as-is; model only the
fields we read, as a **subset view, not a closed contract**. Reads are tolerant (extra upstream fields ignored). We do
not RMW external JSON; if we must annotate it, we write a *separate* authored artifact rather than mutating the upstream
blob.

The bucket-1-vs-3 axis for external data is **re-derivability**, not "did we author a schema over it": a write-once
projection over a retained/re-fetchable authoritative source is bucket 1 (regenerate wholesale, never RMW); a cached
per-request response we keep as received and cannot re-derive is bucket 3 (preserve the raw bytes, tolerant subset
read).

## Artifact classifications

- **`manifest.json` → `manifest.pb`.** Binary proto. The one RMW'd artifact; the RMW discipline above applies. `Access`
  is modelled **flat** — a `string access` field + optional `publisher` — with the access-iff-publisher invariant
  enforced by `protovalidate`, not structurally: `@typespec/protobuf` emits no `oneof` (union-typed fields are rejected;
  no `oneof` decorator exists), and a `oneof` would be wire-identical to flat fields anyway, so nothing durable is lost.
- **`metadata.json` → `metadata.pb`** (bucket 1). `pubmed_proto` (in the `pubmedifier` repo, shipped as a wheel; not
  governed here) is a CPG-authored, deterministic projection of the PubMed DTD via `xml_converter.py` — it intentionally
  drops fields we don't model, so it is *not* a complete DTD model. It qualifies for bucket 1 not on completeness but
  on: **write-once / overwrite-from-fresh** (converted from upstream XML, never RMW'd — no lossy-round-trip vector);
  **deterministic transform over a re-derivable source** (same XML → same proto; the authoritative PubMed XML,
  re-fetchable from NCBI, stays the system of record, so a dropped field is recovered by changing the transform and
  re-running); and **we own and gate the shape** with `buf breaking`. Converter drift behavior: `_consume` raises on a
  required-tag mismatch (structural drift fails loud); trailing/optional unmodeled elements are silently skipped
  (acceptable given re-derivability). Crossref/Unpaywall responses, by contrast, are bucket 3 — cached per-request
  payloads with no owned transform.
- **`knowledge_units.jsonl` → a single `repeated KnowledgeUnit` message per paper** (`knowledge_units.pb`). Per-`{uuid}`
  and small (dozens of units), write-once (a re-extraction overwrites wholesale). Units are consumed as a set and
  single-KU lookup is by `id` through the derived entities map, never by positional index; there is no incremental
  append. So a length-delimited stream buys streaming/append we don't need (and Python protobuf has no first-class
  delimited I/O), **bagz** is the corpus-scale counterpart (offset-indexed random access — the vm_search projection
  layer, one level up) and overkill per-doc, and per-record `.pb` scatters tiny blobs and breaks the
  one-artifact-alongside-manifest layout. `entities.jsonl` (derived, per-doc, rebuildable) takes the same shape.
- **Drop JSON Schema entirely** (and `tools/schema/normalize.py`, the #4084 ref-rewrite). Bucket 1 is proto + `buf`;
  bucket 2 is the browser view model (target deferred); bucket 3, as built, never stores raw external JSON — external
  responses are mapped to canonical proto at ingest (`crossref.py` → `PubmedArticle`), and the drift that matters (a
  *read* field changing type or vanishing) is already caught at the ingest boundary by fail-loud mapping. A JSON-Schema
  pass would be redundant (the mapper walks every field anyway) and can't express the semantic mapping. If a genuine
  store-raw-external-JSON artifact is ever introduced *and* read in multiple places, add validation matched to the
  reader then (the browser view model if TS, a small TypeSpec-authored `jsonschema` check for Python).
- **Constraints move to `protovalidate`.** The `@minItems`, pattern, and range constraints JSON Schema carried become
  protovalidate options; string-valued enums stay (proto enums, JSON-name encoding), membership enforced by
  protovalidate if strictness is wanted.
- **Readability affordance: an on-demand Python dump CLI** over the generated `_pb2` modules (GCS-aware via ADC,
  dispatches on artifact type, emits text_format or `MessageToJson`), with a **normalized mode for stable diffs** — sort
  map keys (unordered in proto) and pin field order/whitespace, but **preserve repeated-field order** (semantically
  significant). Preferred over bare `protoc --decode` because the descriptors are embedded in the generated modules (no
  `-I`/import-closure) and `pubmed_proto` is already a dependency, so `metadata.pb` works without vendoring its schema.
  `protoc --decode` is the schema-in-hand fallback, `protoc --decode_raw` the zero-dependency peek. No committed JSON
  rendering per `.pb` (a second, derived, drift-prone artifact).

## Realization

Three PRs, dependency-ordered, each independently reviewable and mirror-safe:

1. This ADR + the `typespec.md` rewrite (the decision on record).
1. Schema-tooling setup: `regen.py` converges the at-rest domains onto the existing proto path (`@typespec/protobuf` →
   committed `.proto` → `protoc`/grpcio-tools stubs); remove `chuckd` (`tools/schema/chuckd_compat.py`, the
   `schema-compat.yml` chuckd job, the pinned Docker image), the `at_rest`/seal role, JSON Schema, and `normalize.py`;
   `buf breaking` becomes the sole compat gate.
1. Re-cut the in-flight litcache stack onto proto: the `.tsp` become proto-oriented (`@field` numbers, `Access` flat +
   protovalidate); `themis/litcache/models/` is generated from proto (or proto-sourced Pydantic); the writer/readers
   operate on proto messages; storage reads/writes `.pb`; the RMW discipline above; the dump CLI.

## Consequences

- JSON's diffability is given up for at-rest, recovered on demand by the dump CLI, not by a stored rendering.
- Pydantic stays optional: if the backend wants its ergonomics, source it **from proto** (a proto→pydantic generator, or
  `betterproto`), never by reintroducing JSON Schema as a hub. Generator maturity is verified before betting on it;
  `betterproto` is the fallback.
- A type that is both at-rest (proto) and browser-shipped needs **dual definitions** (proto + browser), a genuine cost
  of proto-at-rest. Proto and Zod no longer share a reliable subset once JSON Schema is gone: `@typespec/protobuf` needs
  integer enums and emits no `oneof`/literal, while a Zod emitter needs string enums.
- The browser could instead **consume proto directly via protobuf-es** (buf's TS runtime), collapsing the dual
  definition — the likely bucket-2 direction, undecided; the frontend has no models yet.
- **Prefer identifier-safe enum values** (snake_case): proto enums are integer with identifier-only names. Hyphenated
  vocabularies were our own choice, not a requirement; only genuinely-external arbitrary strings (e.g. a raw licence
  URL) are `string` fields.
- The feature-coverage corpus (`schema/tests/fixtures/features/`) retires its JSON-Schema arm; it verifies proto
  round-trip (bucket 2 deferred).
