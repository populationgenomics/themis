# Design: proto as the schema IDL and serialization posture

**Related:** [`../plans/literature-cache.md`](../plans/literature-cache.md) (the motivating at-rest models),
[`services.md`](services.md) (the internal gRPC service pattern built on the RPC protos),
[`litcache-manifest.md`](litcache-manifest.md) (the literature cache manifest and bibliographic record),
[`evidence-fulltext.md`](evidence-fulltext.md) (the literature cache worker and job log),
[`migrations.md`](migrations.md) (how schema and database transitions run),
[`analysis-scenarios.md`](analysis-scenarios.md) and [`curation-surface.md`](curation-surface.md) (inline binary protos
in Cloud SQL).

## Overview

Data in Themis crosses three boundaries: internal Python services and durable storage, the browser-to-BFF seam in the
web tier, and cached responses from external upstreams. Maintaining separate schema languages across Python and
TypeScript forces hand-written translation layers and cross-language JSON conventions that drift over time.

Every authored data shape and RPC contract in Themis is written once as a hand-authored `.proto` file under
[`schema/proto/`](../../schema/proto). That `.proto` file serves as the single schema on every side of the system.
Internal services and at-rest artifacts serialize messages as binary protobuf so that an older reader round-trips fields
added by a newer writer without dropping them. The browser and the Next.js backend-for-frontend (BFF) exchange those
same messages as JSON over the Connect protocol, using the `protobuf-es` codec on both ends so neither side writes REST
route boilerplate or separate validation schemas. Raw JSON payloads cached from external upstreams are stored as text
and read through a narrow subset view rather than round-tripped through a lossy typed write.

`buf` drives the schema toolchain: `buf lint` checks structure and naming, `buf breaking` blocks incompatible changes in
CI, and `buf export` feeds local code generators (`grpcio-tools` for Python and `protoc-gen-es` for TypeScript). The
generated stubs are committed to the repository so that no consumer depends on `buf` or remote plugins at build or run
time.

## Background

Themis splits its runtime across two languages and two storage tiers. The internal data-plane services and sandbox
workers run in Python and communicate over gRPC ([`services.md`](services.md)). The web tier runs in Next.js, where a
TypeScript BFF serves the React browser client and calls the internal Python services over gRPC
([`frontend-framework.md`](frontend-framework.md)). Durable state lives in Cloud Storage objects (such as paper
manifests, bibliographic records, and workspace repositories) and in Cloud SQL tables.

Protocol Buffers (`proto3`) separates a message's schema from its wire encoding. In binary protobuf, each field on the
wire is keyed by its numeric tag and wire type rather than its field name. When a binary parser encounters a field tag
it does not recognize from its compiled schema, it retains the raw bytes in the message's unknown-field set and writes
them back out when the message is serialized again. In proto3-JSON, by contrast, fields are keyed by their declared
names, and a parser that decodes JSON into a typed message and re-serializes it cannot retain fields it does not know.

The repository organizes all hand-authored schemas and copies of upstream schemas under
[`schema/proto/`](../../schema/proto), configured by [`buf.yaml`](../../buf.yaml) and
[`apps/web/buf.gen.yaml`](../../apps/web/buf.gen.yaml):

```
schema/proto/                     # hand-authored .proto — the source of truth
  themis/rpc/                     # internal gRPC service contracts (auth, hello, sheaf, the evidence interfaces)
  themis/evidence/models/         # the value types and routing vocabularies the interfaces share
  themis/svcv4/models/            # the framework's own output vocabulary (the class ladder)
  themis/workbench/models/        # the browser↔BFF view model + its request/reply envelopes
  themis/workbench/rpc/           # the browser-facing Connect service (Workbench)
  themis/litcache/models/         # at-rest domain contracts (manifest, bibliographic record, job log, worker request)
  themis/curation/models/         # what a curator records on one worksheet
  themis/sheaf/models/            # at-rest: the sheaf ref document
  clinvar_proto/                  # copy of ClinVar's published schema
  pubmed_proto/                   # copy of PubMed's published schema
buf.yaml                          # module, lint rules, buf breaking config, deps
buf.lock                          # pinned buf deps (protovalidate)
apps/web/buf.gen.yaml             # protobuf-es (local protoc-gen-es plugin)
```

A `.proto` file's path under `schema/proto/` matches its Python import path, so `schema/proto/themis/rpc/auth.proto`
generates `themis.rpc.auth_pb2`. Python consumers import those generated modules directly and enforce field constraints
at read and write boundaries with `protovalidate.validate(msg)`. TypeScript consumers import committed `protobuf-es`
stubs from `apps/web/src/gen/`, which provide message types (with tagged unions for each `oneof`) and `GenService`
descriptors for Connect services.

## Design

### Three serialization buckets by schema owner and consumer

Serialized data in Themis falls into three buckets according to who owns the schema and who consumes the bytes.
Content-addressed blobs (`sources/`, `renderings/`, and `supplementary/`) are opaque bytes and sit outside all three
buckets.

| Bucket                                                                        | Format                              | Compatibility gate | Schema owner |
| ----------------------------------------------------------------------------- | ----------------------------------- | ------------------ | ------------ |
| 1. Authored, internal machine-to-machine (at-rest artifacts and internal RPC) | Binary protobuf                     | `buf breaking`     | Themis       |
| 2. Authored, browser-facing (browser↔BFF)                                     | Proto3-JSON over Connect            | `buf breaking`     | Themis       |
| 3. Externally defined, ingested (cached raw upstream payloads)                | Raw JSON read through a subset view | Read-side only     | Upstream     |

```
  Upstream APIs ──(Bucket 3: raw JSON)──► Ingest / Cache
                                                │
                                   (Bucket 1: binary proto)
                                                ▼
  Cloud Storage / SQL ◄──(Bucket 1)──► Internal Python Services
                                                ▲
                                   (Bucket 1: gRPC / binary)
                                                │
                                           Next.js BFF
                                                ▲
                                 (Bucket 2: Connect / proto3-JSON)
                                                │
                                          Browser Client
```

Bucket 1 covers all internal gRPC calls and durable artifacts whose schema we author. It uses binary protobuf rather
than a name-keyed text projection like proto-JSON or `pbtxt` so that read-modify-write operations preserve unknown
fields.

Bucket 2 covers the browser-to-BFF seam, which sends proto3-JSON over the Connect protocol so requests remain
inspectable in the browser Network tab and callable with standard HTTP tools.

Bucket 3 covers raw upstream payloads that we cache as received, such as per-request Unpaywall responses. What separates
Bucket 1 from Bucket 3 is whether the artifact can be re-derived from a retained or re-fetchable source, not whether we
wrote a schema over it. A write-once derivation over an authoritative source that can be queried again belongs to Bucket
1 and is regenerated wholesale rather than modified in place; the index record in `metadata.pb`, re-fetched from PubMed
XML or from Crossref or OpenAlex JSON, works this way. A cached per-request response that we cannot re-derive belongs to
Bucket 3: we keep the raw bytes as received and read only the subset of fields we need.

### Binary protobuf at rest preserves unknown fields across read-modify-write

The obvious way to store structured artifacts in Cloud Storage is as JSON, which is human-readable and easy to inspect
with command-line tools. JSON breaks as soon as an artifact is updated in place during a rolling deploy or across
versions. When a newer writer adds a field to an artifact and an older service instance reads that JSON into its older
schema, modifies another field, and writes the JSON back out, the older reader silently drops the new field because its
schema does not name it.

Binary protobuf prevents this data loss: **an older reader round-trips a newer writer's fields untouched.** When the
older reader parses the binary message, any field tag it does not recognize is kept in the message's unknown-field set
and re-emitted when the modified message is serialized:

```
  Writer (v2 schema)              Reader/Writer (v1 schema)            Storage (after RMW)
┌────────────────────┐          ┌───────────────────────────┐        ┌────────────────────┐
│ 1: doc_id = "7c1e" │ ──read─► │ Known:   1: doc_id="7c1e" │ ─save► │ 1: doc_id = "7c1e" │
│ 2: status = READY  │          │          2: status=DONE   │        │ 2: status = DONE   │
│ 9: new_field = 42  │          │ Unknown: [tag 9, varint]  │        │ 9: new_field = 42  │
└────────────────────┘          └───────────────────────────┘        └────────────────────┘
```

In the literature cache, content-addressed blobs are immutable, and only three path-addressed artifacts under a paper's
UUID are modified in place:

- **The paper's job log**, which the worker only appends to by writing the existing object bytes followed by the binary
  encoding of the new entries, without decoding the existing bytes
  ([`evidence-fulltext.md`](evidence-fulltext.md#the-job-log-records-what-the-worker-has-tried)).
- **The metadata fields of `metadata.pb`**, which are recomputed from the index record stored beside them inside
  `SourcedMetadata` ([`litcache-manifest.md`](litcache-manifest.md#the-bibliographic-record-metadatapb)). The fill
  rewrites `SourcedMetadata` through the binary protobuf message so any unknown field is preserved, and writes the
  object back with a Cloud Storage `ifGenerationMatch` precondition so a concurrent ingest or refresh wins and forces
  the fill to re-read.
- **The `litcache` manifest**, which tracks a paper's sources and renderings.

Safe read-modify-write of the manifest relies on three rules applied in order:

1. **Preserve unknowns through the binary message.** Every read-modify-write parses, mutates, and serializes the binary
   protobuf message directly. Converting to JSON or another lossy projection in the middle drops the unknown-field set.
1. **Fail loud on the write path.** Readers remain open to unknown fields, while the modify-and-persist path fails loud
   if write-back cannot account for content it did not model, raising an error rather than dropping data.
1. **Write back atomically.** Every write-back passes a Cloud Storage `ifGenerationMatch` precondition so concurrent
   writers cannot overwrite each other's updates.

Binary unknown-field preservation has one limitation that no wire format can solve automatically: semantic coupling.
Preserving an unknown field's bytes does not keep the artifact consistent if a field the older writer *did* change is
linked by an invariant to the unknown field. We mitigate this by keeping additive fields independent of existing fields
and by failing loud on the write path when a writer cannot account for the state it is modifying.

### Protos in Cloud SQL columns

Most durable protobuf artifacts live as binary `.pb` objects in Cloud Storage, while two authored messages live directly
in Cloud SQL columns: `AnalysisInputs`, which records the scenario and inputs an Analysis was created from
([`analysis-scenarios.md`](analysis-scenarios.md) §Storage), and the curation worksheet's `Assessment`, which stores one
row per workflow in both the draft and submitted tiers ([`curation-surface.md`](curation-surface.md) §Storage).

When storing protobuf data alongside relational rows, we choose among three storage shapes in order of preference:

1. **Cloud Storage pointer with metadata in SQL (default for large artifacts).** The SQL row stores the object path and
   generation of a binary `.pb` blob in Cloud Storage along with the scalar columns needed for queries and joins. This
   keeps database rows small and reuses the Cloud Storage `ifGenerationMatch` read-modify-write discipline.
1. **Inline binary protobuf (`bytea`) for small records.** The message is serialized into a `bytea` column, and any
   field that SQL queries must filter or index is extracted into a standalone column alongside it. Because the stored
   value is binary protobuf, it preserves unknown fields across read-modify-write. The cost is that every write must
   keep the extracted SQL columns synchronized with the embedded binary payload, because PostgreSQL cannot inspect the
   protobuf bytes directly. A write-once record such as `analyses.inputs` uses `bytea` to get a schema-checked contract
   even though it never performs a read-modify-write.
1. **Proto3-JSON (`jsonb`) for read-mostly data.** Serializing the message to `jsonb` allows native PostgreSQL JSON
   queries and indexes without maintaining duplicate scalar columns. However, because proto3-JSON is keyed by field
   name, any read-modify-write through the `jsonb` column drops unknown fields written by a newer release. This shape is
   acceptable only for query-heavy data that is written whole and never modified in place, or where dropping unknown
   fields on write is acceptable.

### The browser-to-BFF seam is Connect JSON over a POST-only route

The internal transport between the BFF and Python services is gRPC over HTTP/2 using binary protobuf. The browser cannot
speak gRPC directly because browser networking APIs do not expose the HTTP/2 trailers that gRPC uses to carry final
status codes. Meanwhile, hand-writing Next.js REST route handlers for the browser-to-BFF seam requires per-endpoint
`fetch` wrappers, custom error envelopes, and manual parsing of path and query strings without compatibility checks.

Instead, the browser-to-BFF seam is declared as a protobuf service in
[`schema/proto/themis/workbench/rpc/workbench.proto`](../../schema/proto/themis/workbench/rpc/workbench.proto) and
served over the Connect protocol. Connect is not a second serialization format; it is a thin HTTP envelope around the
same `protobuf-es` message types that the browser and BFF already share. A unary call is a standard HTTP request whose
body is the bare proto3-JSON message:

```
POST /api/rpc/themis.workbench.rpc.Workbench/Poll HTTP/1.1
Content-Type: application/json
Cookie: GCP_IAAP_AUTH_TOKEN=...

{"analysisId": "anl_01J..."}
```

Keeping the wire payload as `application/json` makes every request and response readable in the browser's Network tab
and reachable from `curl` or external tooling such as WebMCP. Errors travel as standard Connect error codes, and request
validation runs once as a `protovalidate` interceptor on the router in
[`apps/web/src/server/rpc/handler.ts`](../../apps/web/src/server/rpc/handler.ts) rather than inside each handler method.
Declaring the service in `.proto` puts the method names and request/reply pairings under `buf breaking` alongside the
message shapes.

The BFF mounts the Connect router at a single Next.js App Router catch-all route under
[`apps/web/src/app/api/rpc/`](../../apps/web/src/app/api/rpc). Because Connect-ES's packaged Next.js adapter
(`connect-next`) targets the legacy Pages Router and Node `req`/`res` objects, our route handler adapts the Web
`Request` that the App Router passes directly to `createFetchHandler` from `@connectrpc/connect/protocol`, which is a
public semver-covered export backed by Connect's conformance test suite.

The route and handler enforce four boundary rules:

- **POST-only mount.** The route file exports only `POST`, so any `GET` request receives Next.js's 405 response
  regardless of what a method declares. Because cross-site `GET` navigation can ride the curator's Identity-Aware Proxy
  (IAP) cookie, refusing `GET` keeps every RPC out of reach of cross-site `GET` requests.
- **Content-Type check against cross-site forms.** A cross-site HTML form can send a `POST` without a CORS preflight
  using one of three form content types. The Connect handler requires `application/json`, rejecting form-encoded
  requests before any method runs.
- **Strict unknown-field rejection.** `connect-es` ignores unknown JSON fields by default. Our router sets
  `ignoreUnknownFields: false` so that a caller sending an undeclared or misspelled field (such as a misspelled
  `version` parameter that would otherwise silently read the current document) receives an immediate validation error.
- **No shared caching.** Every response sets `Cache-Control: private, no-store` so no shared proxy or CDN caches a
  curator-scoped reply.

Underneath the route mount, no method in `workbench.proto` declares `idempotency_level = NO_SIDE_EFFECTS`. In Connect,
that option enables `GET` requests carrying the message in the query string, which our browser transport never uses and
our `private, no-store` responses could not cache anyway. Because `buf breaking`'s `RPC_SAME_IDEMPOTENCY_LEVEL` rule
rejects changing `idempotency_level` in either direction, enforcing that rule on `workbench.proto` would permanently
freeze each method's initial setting even though the service's only consumer ships in the same deployment. We therefore
ignore `RPC_SAME_IDEMPOTENCY_LEVEL` for `themis/workbench/rpc/` alone (the gRPC contracts keep
`RPC_SAME_IDEMPOTENCY_LEVEL`, where the level carries retry semantics a deployed client reads), and a handler test in
[`apps/web/src/server/rpc/handler.test.ts`](../../apps/web/src/server/rpc/handler.test.ts) inspects the service
descriptor to assert that every method refuses `GET`. If a future read method needs `GET`, that change updates the proto
option, the route exports, and the handler test together.

The browser-facing service lives under `schema/proto/themis/workbench/rpc/` rather than `schema/proto/themis/rpc/` so
that the surface reachable from a curator's browser never shares a package namespace with internal services reserved for
the sandbox and backend. `workbench.proto` is the only service proto with no Python implementation. The Python gRPC
codegen pass still emits a `_pb2_grpc.py` file for it because the generator selects every proto that declares a
`service` rather than carrying a special-case exclusion list, matching the TypeScript pass that emits `protobuf-es`
stubs for internal protos the browser never calls.

### External JSON and upstream schemas

External data from third-party APIs enters Themis in three ways depending on whether the upstream payload is cached
per-request, stored as a schema-less JSON record inside a binary artifact, or published with a formal upstream schema.

#### Cached upstream JSON (Bucket 3)

For a cached per-request payload such as a raw Unpaywall response, we store the upstream JSON bytes as-is and model only
the fields we read as a subset view rather than a closed contract. Readers ignore extra fields that the upstream adds. A
read fails loud when a field that every record must state is missing, or when a field we read has an unexpected type or
shape; a field that the upstream legitimately omits is read as not stated. We never perform read-modify-write on
external JSON; if we need to attach Themis annotations to an upstream payload, we write a separate authored artifact
alongside it.

#### Upstream JSON held as text inside an authored proto

Some upstreams serve JSON records without publishing an accurate schema that a code generator can turn into a `.proto`.
Crossref and OpenAlex are two such upstreams whose bibliographic records `litcache` stores inside the `SourcedMetadata`
message in `metadata.pb` ([`litcache-manifest.md`](litcache-manifest.md#the-bibliographic-record-metadatapb)).

We store each such record in a `string` field of the authored protobuf message, holding the upstream's JSON response
re-serialized from the parsed value with no keys dropped or renamed. Using `string` rather than `bytes` states that the
payload is UTF-8 text and lets the protobuf runtime verify UTF-8 encoding on parse and serialize. The three obvious
typed alternatives each fail on live upstream data:

- **Hand-authored strict proto.** Defining a protobuf message whose fields mirror the upstream's keys and parsing with
  strict unknown-field checks gives static types, but halts ingestion whenever the upstream adds a new key until we edit
  and deploy the schema. OpenAlex adds keys frequently enough that strict parsing breaks regular ingestion
  ([`litcache-manifest.md`](litcache-manifest.md#the-bibliographic-record-metadatapb)).
- **Proto generated from the upstream's OpenAPI spec.** Generating the message from OpenAPI avoids hand-authoring, but
  upstream OpenAPI descriptions lag behind live payloads; OpenAlex's spec omits roughly a dozen top-level keys present
  in live responses, causing strict parses to fail until hand-patched.
- **`google.protobuf.Struct`.** Storing the JSON in a `Struct` requires no schema maintenance, but `Struct` represents
  every JSON number as an IEEE 754 double, silently losing precision on any integer above $2^{53}$ so a reader no longer
  receives the exact value the upstream served.

Beside the JSON `string` field sits one reader per upstream that extracts only the fields Themis uses. Like Bucket 3
readers, it fails loud when a required field is missing or malformed (dead-lettering the paper at ingest as schema drift
naming the offending field), reads optional absent fields as not stated, and leaves all other keys untouched in the JSON
text. Themis's own metadata fields on `SourcedMetadata` are populated through these same readers, keeping the upstream
field names and shapes in one place per upstream. Upstream vocabulary values (such as a work's raw `type` string) stay
verbatim in the JSON text, while normalized vocabularies that Themis matches on (such as NLM's Publication Type) are
derived when populating Themis's metadata fields.

Although `metadata.pb` embeds this upstream JSON text, `metadata.pb` itself remains a Bucket 1 binary artifact because
its index record is written once and can be re-derived wholesale by re-querying the upstream index, while Themis's
metadata fields beside it are recomputed from that index record.

#### Generated upstream schemas

When an upstream publishes a formal XSD schema and we return its payload as a typed protobuf message rather than a
`Struct`, we generate the `.proto` from the upstream schema instead of transcribing a thousand-element XSD by hand.

That generation runs in a separate first-party repository,
[xsd-former](https://github.com/populationgenomics/xsd-former), which takes the upstream XSD plus flattening and
annotation transforms and publishes a Python wheel containing the `.proto` file, generated Python stubs, an XML
converter, and Pydantic models: `clinvar-proto` for ClinVar VCV records and `pubmed-proto` for PubMed citations. Pinning
the wheel in our dependencies keeps the schema, stubs, and XML converter at a single synchronized version.

Because our own `.proto` files embed those upstream messages (`themis/rpc/clinvar.proto` embeds ClinVar's record, and
`themis/rpc/literature.proto` and `themis/litcache/models/litcache.proto` embed PubMed's records), `buf` needs the
upstream `.proto` files inside our module tree to resolve `import` statements. During code generation,
[`tools/schema/regen.py`](../../tools/schema/regen.py) copies
[`clinvar.proto`](../../schema/proto/clinvar_proto/clinvar.proto) and
[`pubmed.proto`](../../schema/proto/pubmed_proto/pubmed.proto) verbatim from the installed wheels into `schema/proto/`
at the exact relative paths the wheels' own Python stubs register their descriptors under. When `protoc` compiles our
schemas, it derives Python `import` statements from those paths so our generated stubs import the wheel's modules
directly rather than registering a duplicate descriptor. The Python codegen pass emits nothing for the copied files,
while the TypeScript `protobuf-es` pass compiles them like any other `.proto` so the web tier gets TypeScript types. The
CI freshness check catches both hand edits to the copies and wheel version bumps made without running code generation.

Because `xsd-former` assigns field numbers positionally in XSD document order without keeping a ledger of previously
released field numbers, any upstream schema revision that inserts an element in the middle of a sequence shifts every
subsequent field number. No `reserved` statement in the committed copy can prevent that, because every regeneration
rewrites the copy whole from the wheel. `buf breaking` compares the committed upstream copies just like hand-authored
protos and rejects any wheel bump that renumbers fields, so the wheel stays frozen on its current pin until `xsd-former`
carries a persistent numbering ledger that emits `reserved` statements for removed fields (the wheel is first-party, so
building that ledger is ours to do). That refusal protects two live boundaries:

- **ClinVar records in flight across version skew.** ClinVar messages are never persisted at rest, and only travel
  inside RPC responses from the evidence service. However, the sandbox guest that decodes those responses runs with the
  `clinvar-proto` wheel pinned in its container image (the `guest` dependency group in
  [`pyproject.toml`](../../pyproject.toml)), and a running sandbox Job can keep its image for up to four hours while the
  evidence service redeploys beneath it. A renumbered ClinVar message sent to an in-flight guest would decode silently
  into the wrong fields.
- **PubMed records stored at rest.** Every `metadata.pb` in `litcache` stores a `PubmedArticle` or `PubmedBookArticle`
  message on disk ([`litcache-manifest.md`](litcache-manifest.md#the-bibliographic-record-metadatapb)), so renumbering
  `pubmed.proto` would corrupt existing stored records.

### Local code generation and committed stubs

The obvious way to run `buf` code generation is through `buf generate` using remote plugins on the Buf Schema Registry
(BSR) or `protoc_builtin`. Both break in our environment: remote plugins hit BSR anonymous rate limits in CI and local
builds, and `protoc_builtin` embeds a `protoc` version (7.x) whose generated Python code requires a newer protobuf
runtime than our dependency tree allows (capped at 6.x by `apache-beam` and `grpcio-health-checking`).

Instead, [`tools/schema/regen.py`](../../tools/schema/regen.py) runs all code generation locally using the tools
installed in the workspace (`uv run --group codegen python -m tools.schema.regen`):

| Stage           | Tool                                             | Output                                                                               |
| --------------- | ------------------------------------------------ | ------------------------------------------------------------------------------------ |
| Upstream copy   | Pinned `clinvar-proto` and `pubmed-proto` wheels | `schema/proto/clinvar_proto/clinvar.proto`, `schema/proto/pubmed_proto/pubmed.proto` |
| Python messages | `grpcio-tools` `protoc` (`--python` / `--pyi`)   | `themis/**/*_pb2.py` + `.pyi`, `buf/validate/validate_pb2.py`                        |
| gRPC stubs      | `grpcio-tools` `protoc` (`--grpc_python`)        | `*_pb2_grpc.py` (service protos only)                                                |
| gRPC stub types | `mypy-protobuf`'s `protoc-gen-mypy_grpc`         | `*_pb2_grpc.pyi` (service protos only)                                               |
| TypeScript      | Local `protoc-gen-es` via `buf generate`         | `apps/web/src/gen/**/*_pb.ts`                                                        |

`regen.py` first runs `buf export` to materialize our `.proto` files alongside the `buf.lock`-pinned `buf/validate`
dependency into a temporary directory (using a cached module fetch rather than a remote plugin call). It then runs the
`protoc` binary bundled inside `grpcio-tools` over that directory, which keeps the generated Python code matched to the
protobuf 6.x runtime. Because the `protovalidate` Python wheel ships no generated `validate_pb2.py` stub, `regen.py`
emits `buf/validate/validate_pb2.py` into the repository, while standard `google.protobuf.*` well-known types resolve
from `grpcio-tools`'s bundled includes at runtime. On the TypeScript side, `buf generate` runs the local
`@bufbuild/protoc-gen-es` plugin configured in [`apps/web/buf.gen.yaml`](../../apps/web/buf.gen.yaml); Connect-ES v2
uses the `GenService` descriptors that `protoc-gen-es` emits directly, requiring no separate Connect codegen plugin.

All generated files are committed to git and never edited by hand. When proto inputs change, CI
([`.github/workflows/schema-freshness.yml`](../../.github/workflows/schema-freshness.yml)) verifies freshness by running
`python -m tools.schema.regen` and then `git diff --cached --exit-code`. Committing the generated code means application
builds, tests, and the public repository mirror never need `buf` installed at install or run time, and schema changes
show their exact generated diff in pull requests.

When `protoc` compiles `.proto` files into Python, leading `//` comments do not reach every output file equally:

| Comment location   | Reaches in generated Python                | Mechanism                                |
| ------------------ | ------------------------------------------ | ---------------------------------------- |
| `service` or `rpc` | Stub and Servicer method `__doc__` strings | `grpc_python` plugin                     |
| `message` or field | Nothing in `*_pb2.py`                      | `protoc` strips message/field docstrings |

Because Python `_pb2.py` classes carry no field or message descriptions at runtime, any agent or skill documentation
must be generated from the `.proto` source or from a `FileDescriptorProto` compiled with `--include_source_info`, which
retains all leading comments, rather than by introspecting Python `__doc__` attributes.

### Authoring and validation rules

`buf lint` runs as a pre-commit hook (and in CI under `lint.yml`) using the `BASIC`, `ENUM_VALUE_PREFIX`,
`ENUM_ZERO_VALUE_SUFFIX`, and `PROTOVALIDATE` rules configured in [`buf.yaml`](../../buf.yaml) (with
`PACKAGE_DIRECTORY_MATCH` and `DIRECTORY_SAME_PACKAGE` excepted for the co-located `themis.rpc.*` packages and the
copied upstream schemas, and so file paths map cleanly to Python packages). Hand-authored schemas follow six
conventions:

- **Proto-canonical enums.** Enum values use `UPPER_SNAKE` prefixed with the enum's type name, and every enum declares a
  `*_UNSPECIFIED = 0` sentinel at tag zero. Zero is never a valid domain value, and readers fail loud if `0` appears in
  persisted data. Enums travel as integers on the binary wire and map between integer and name in the single
  `protobuf-es` codec on the browser seam. Only genuinely open external strings, such as raw license URLs, use `string`
  fields instead of an enum.
- **Field names that read as what they hold.** A `repeated` field of countable items uses a plural noun (`authors`,
  `articles`), while a mass noun (`evidence`, `provenance`) or a relation name (`located_in`) stays in its natural form.
  Giving a `repeated` field a singular name requires a specific domain reason: `rcv` matches ClinVar's key,
  `class_determinative` matches the label on the curator's worksheet, `sensitivity` names the analysis that its entries
  form together, and `admits_caller` reads as a clause in each `option` line that sets it.
- **Sum types as `oneof` over variant messages.** Modeling a sum type as a `oneof` makes cross-field "X if and only if
  Y" invariants structural rather than runtime-checked. Each variant message field is marked
  `[(buf.validate.field).required = true]` and the `oneof` itself carries `option (buf.validate.oneof).required = true`
  so an unset or empty variant is rejected at validation time. Where nothing runs `protovalidate` over an at-rest
  message because its sole reader validates in Python code (as with the sheaf ref document in
  [`refdoc.proto`](../../schema/proto/themis/sheaf/models/refdoc.proto)), we omit the `protovalidate` options so the
  schema never claims a check that does not run.
- **Declarative field constraints via `protovalidate`.** Field-level and cross-field invariants (`repeated.min_items`,
  `string.min_len`, and message-level `cel` expressions) are declared directly on the `.proto` and enforced at service
  and storage boundaries via `protovalidate.validate(msg)`.
- **Leading `//` comments.** Every service, RPC, message, field, and enum carries leading `//` documentation in the
  `.proto` file.
- **Explicit field numbers and reservations.** Every field has an explicit tag number, and retired fields reserve both
  their number and their name.

### Schema evolution

Schemas evolve in place without version numbers in package names or version-dispatch branches in readers. Every reader
parses every historical artifact ever written, verified against a committed corpus of golden fixtures.

#### Adding and retiring fields

Compatible evolution adds new fields with fresh tag numbers or adds new enum members. Renaming, renumbering, or changing
the type or meaning of an existing field is forbidden.

Retiring a field is permitted provided the change deletes the field and marks **both its field number and its field
name** as `reserved`:

```proto
message CreateAnalysisRequest {
  reserved 2;
  reserved "prompt";
  string project_id = 1 [(buf.validate.field).string.min_len = 1];
}
```

Reserving the tag number protects binary protobuf readers and writers from reusing the slot, while reserving the field
name protects the proto3-JSON browser seam, which keys fields by name. In CI
([`.github/workflows/schema-compat.yml`](../../.github/workflows/schema-compat.yml)),
[`tools/schema/buf_compat.py`](../../tools/schema/buf_compat.py) compares every committed contract from the change that
first commits it (so once merged, a contract evolves additively like any other), running `buf breaking` under the `FILE`
category with `FIELD_NO_DELETE` excepted and `FIELD_NO_DELETE_UNLESS_NAME_RESERVED` and
`FIELD_NO_DELETE_UNLESS_NUMBER_RESERVED` added, and allows no in-tool override comments. Because `protoc` synthesizes a
nested `<FieldName>Entry` message (`option map_entry = true`) for every `map<K, V>` field and `MESSAGE_NO_DELETE` fires
on that synthetic message even when the map field's number and name are reserved, `buf_compat.py` filters
`MESSAGE_NO_DELETE` findings on `map_entry = true` messages so `map<K, V>` fields on model messages can be retired under
the same reservation rule.

Reserving a field's number and name prevents future reuse, while retiring a field safely across a deployment requires
waiting out the transition window until nothing in flight still sets the field. On binary legs, unknown-field retention
absorbs version skew during rolling deploys. On the browser seam, however, the BFF sets `ignoreUnknownFields: false` and
rejects any JSON key that the schema no longer declares. Retiring a browser-facing field therefore takes two changes:
first stop sending the field from the client and deploy, and then delete and reserve the field in the `.proto`.
Collapsing that retirement into a single change is allowed only when the transition skew costs nothing real under the
destructive migration criterion in [`migrations.md`](migrations.md) (no live users affected, and the design doc states
what breaks and until when). Retiring `CreateAnalysisRequest.prompt` in [`analysis-scenarios.md`](analysis-scenarios.md)
§Storage followed that single-step path: any browser tab still running the previous bundle failed to create an Analysis
until reloaded.

#### Changing at-rest contracts

When an at-rest contract needs an incompatible change (such as changing a field's type or meaning), we make an
expand-and-contract transition:

1. Add the new field alongside the old field so readers accept either form.
1. Rewrite every stored artifact to populate the new field.
1. Delete the old field and reserve its number and name.

Different stores handle the rewrite step according to their mutability:

- **Immutable stores**, such as pushed revisions in a sheaf repository, are never rewritten, so their readers keep
  accepting the old field indefinitely.
- **Mutable manifests** in `litcache` pause their writers while stored manifests are rewritten rather than teaching
  readers to accept both forms simultaneously
  ([`litcache-manifest.md`](litcache-manifest.md#a-change-to-the-manifests-shape-pauses-the-writers-instead-of-carrying-both-forms)).
- **Re-derivable records**, such as `litcache`'s `metadata.pb`, can be regenerated by a metadata refresh and may take a
  destructive change in a single step when the failing window costs nothing real
  ([`litcache-manifest.md`](litcache-manifest.md#the-move-off-the-mirrors)).

#### Retiring an RPC or a message

While protobuf provides `reserved` for field numbers and names, it has no syntax to reserve a deleted `rpc` name or a
deleted `message` name. Enabling `buf breaking`'s RPC, message, and file deletion rules on service protos would
therefore block every RPC retirement unconditionally. Instead, [`buf.yaml`](../../buf.yaml) ignores those deletion rules
under the service directories (`schema/proto/themis/rpc/` and `schema/proto/themis/workbench/rpc/`), where request and
reply messages belong to a single RPC and are deleted with it (or when an entire service file is removed).

Under the service trees, static type checkers enforce safety by catching any implementation or caller that outlives a
deleted RPC declaration:

- **Python servicers.** Every servicer method carries `@typing.override`, enforced by pyright's `reportImplicitOverride`
  rule, so an implementation whose RPC was removed from the `.proto` fails type checking.
- **Python callers.** `protoc-gen-mypy_grpc` generates `.pyi` stub files beside every `_pb2_grpc.py` file, so calling a
  removed method on `HelloAsyncStub` or another client stub fails pyright at check time rather than `AttributeError` at
  run time.
- **Browser callers and the BFF.** Both the browser client and the BFF's `ServiceImpl<T>` handler map are typed from the
  generated `GenService` descriptor, so `tsc` catches any surviving caller or handler on `themis/workbench/rpc/`.

Outside the service directories, deleting a message or a `.proto` file remains blocked by `buf breaking` in the models
trees (`themis/litcache/models/`, `themis/sheaf/models/`, `themis/curation/models/`, `themis/workbench/models/`, etc.).
The risk for a model message is bytes already persisted in storage rather than code still calling a method, and static
type checkers go silent as soon as the last code reader is deleted. Keeping the deletion gate on models protects:

- `litcache.proto`, which backs every stored paper's manifest and bibliographic record (until a production environment
  holds `litcache`'s store, a message or file used only by its bibliographic record may be deleted past the gate once
  all stored records using it have been removed; see
  [`litcache-manifest.md`](litcache-manifest.md#the-move-off-the-mirrors)).
- `themis/sheaf/models/`, which backs the ref document whose noncurrent object generations remain readable long after
  they are superseded.
- `themis/workbench/models/`, which is Bucket 2 on the wire but defines `AnalysisInputs` (stored as binary bytes in
  `analyses.inputs`) alongside the Workbench service's request and reply envelopes (such as `CreateAnalysisRequest`,
  `ListProjectsResponse`, and `PollResponse`, which `themis/workbench/rpc/workbench.proto` imports and uses as its
  active RPC input and output types). Because those request and reply messages sit in a models file alongside a
  persisted message, retiring a browser RPC leaves its old request and reply messages in
  `themis/workbench/models/workbench.proto` rather than deleting them; keeping an unused generated type costs almost
  nothing, whereas deleting a persisted message breaks stored rows. **Decision (new policy):** new request and reply
  messages that belong exclusively to an RPC must be declared in `themis/workbench/rpc/workbench.proto` rather than
  `themis/workbench/models/` so they sit under the `rpc/` compat-gate exception and can be deleted when their RPC is
  retired.

Static type checks verify that no code in the current checkout calls a retired RPC, while reviewers still verify the
rolling-deploy transition window to ensure no in-flight container from the previous release is still calling it.

## Alternatives considered

**TypeSpec as the primary IDL with a separate Zod view model.** An earlier iteration authored schemas in TypeSpec and
emitted both `.proto` files for the backend and Zod schemas for the browser view model. Making two different runtimes
and validators agree on a single JSON projection required restrictive authoring rules across every schema: enums had to
serialize as lowercase `snake_case` strings rather than proto enums, and sum types had to be flattened because the
emitter could not express protobuf `oneof` or `protovalidate` annotations cleanly. Using hand-authored `.proto` as the
single source of truth and running the `protobuf-es` JSON codec on both sides of the browser-to-BFF seam makes wire
agreement automatic, eliminates the duplicate Zod schema layer, and lets schemas use native protobuf `oneof` and
declarative `protovalidate` rules directly.
