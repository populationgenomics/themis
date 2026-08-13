# Design: proto as the schema IDL and serialization posture

**Related:** [`../plans/literature-cache.md`](../plans/literature-cache.md) (the motivating at-rest models),
[`services.md`](services.md) (the service pattern built on the RPC protos),
[`litcache-manifest.md`](litcache-manifest.md) (the litcache manifest structural model).

## Purpose

One source of truth for data shapes, authored as **hand-written `.proto`** under `schema/proto/`. The `.proto` is the
single schema on every side: the protobuf runtime + generated stubs (Python) and protobuf-es (the web tier) serialize it
as **binary proto** for at-rest artifacts and inter-service / BFF↔service gRPC, and as **JSON via the protobuf-es
codec** at the browser↔BFF seam. `buf` drives the toolchain — `buf lint` enforces the discipline, `buf breaking` gates
compatibility, `buf export` feeds the generators — and codegen itself is local (`grpcio-tools` protoc for Python,
`protoc-gen-es` for TypeScript).

## Serialization posture

Serialized data falls into three buckets by *who owns the schema* and *who consumes it*:

| Bucket                                                                        | Format                                       | Compat gate    | Owner    |
| ----------------------------------------------------------------------------- | -------------------------------------------- | -------------- | -------- |
| Authored, internal machine-to-machine (at-rest artifacts + inter-service RPC) | **binary proto**                             | `buf breaking` | us       |
| Authored, browser-facing (browser↔BFF)                                        | **JSON**, over the Connect protocol          | `buf breaking` | us       |
| Externally-defined, ingested (raw upstream payloads we cache)                 | JSON, our model a documented **subset/view** | read-side only | upstream |

- **Bucket 1 — binary proto.** At-rest artifacts and inter-service RPC. Binary, not a name-keyed text projection
  (proto-JSON, pbtxt), for the read-modify-write property below. `buf breaking` gates it.

- **Bucket 2 — browser↔BFF is JSON, over the Connect protocol.** The seam is an authored service
  (`themis/workbench/rpc/workbench.proto`), served by the BFF's Connect handler and called by a generated client. The
  wire stays `application/json`: a Connect unary call is `POST /api/rpc/{package}.{Service}/{Method}` whose body is the
  bare proto3-JSON message, so it is readable in the Network tab and reachable with `curl` — the property that keeps the
  surface consumable by code we don't control (e.g. WebMCP). A method declaring `idempotency_level = NO_SIDE_EFFECTS`
  also answers a `GET` carrying the message in the query; a read may still decline the option, and one does
  ([`frontend-framework.md`](frontend-framework.md) §Data fetching). The handler rejects a field the schema does not
  declare (connect-es tolerates one by default): a misspelled `version` must not quietly read the current document.

  Connect is not a second serialization: it is the *envelope* around the same protobuf-es types the browser and the BFF
  already shared. What it removes is the hand-written part of that seam — per-endpoint fetch wrappers, a bespoke error
  shape, and path/query parsing that arrived as unvalidated strings. Errors are Connect codes; request validation is a
  protovalidate interceptor on the router rather than a call each handler remembers to make. The message shapes were
  already under `buf breaking`; what the service adds is the *method set* — names, request/reply pairing, idempotency —
  which was previously spelled only in route directories and gated by nothing.

  The BFF's handler mounts at one App Router catch-all. Connect-ES's packaged adapters target Node `req`/`res` hosts
  (`connect-next` is Pages-Router-only), but an App Router handler is already passed a fetch `Request`, which is what
  Connect's universal layer speaks; the adapter is `createFetchHandler` from `@connectrpc/connect/protocol` plus a path
  lookup. Those are public, semver-covered exports, and the fetch server path carries the project's own conformance
  coverage.

- **Bucket 3 — external JSON, tolerant subset.** Raw upstream payloads we cache (Crossref, Unpaywall): stored as the
  upstream's JSON, modelled only for the fields we read, tolerant of extras. Never round-tripped through a lossy typed
  write. See External data.

Content-addressed blobs (`sources/`, `renderings/`, `supplementary/`) are opaque bytes, outside all three.

## Usage

### Authoring a schema

- Edit the `.proto` under `schema/proto/<package-path>/`. The file's path under `schema/proto/` is its Python package
  path (e.g. `schema/proto/themis/rpc/auth.proto` → `themis.rpc.auth_pb2`).
- Follow the authoring rules below; `buf lint` (a pre-commit hook) enforces the enum discipline and structural sanity.
- Regenerate the committed stubs with `uv run --group codegen python -m tools.schema.regen` (needs `buf` on PATH and
  `apps/web` deps installed). Generated code is **committed and never hand-edited**; a `.proto` change committed without
  regenerating fails CI (`schema-freshness`).

### Using a schema from Python

- Import the generated stubs (`themis/rpc/<domain>_pb2`, `themis/litcache/models/litcache_pb2`); parse/serialize the
  binary directly.
- Declared-field constraints are **protovalidate** options on the proto; enforce them at the read/write boundary with
  `protovalidate.validate(msg)` (raises `protovalidate.ValidationError`).

### Using a schema from TypeScript

- Import the committed protobuf-es stubs under `apps/web/src/gen/` (`@bufbuild/protobuf`) — the generated message types
  (a tagged union for each `oneof`) and, for a service, its descriptor. Connect-ES v2 needs no second code generator:
  `protoc-gen-es` emits the `GenService` descriptor that `createClient` and `router.service` both take, so
  `buf.gen.yaml` is unchanged by adding a service.
- The browser↔BFF seam is that descriptor on both ends: the BFF registers the implementation on its router, the browser
  calls the generated client, and neither restates the wire contract. The BFF additionally speaks binary proto/gRPC to
  the internal services.

Consumers import committed generated code from their own tree; nothing depends on `buf` at build or run time — the
generated-code-is-committed policy buys that.

## Layout

```
schema/proto/                     # hand-authored .proto — the source of truth
  themis/rpc/                     # internal gRPC service contracts (auth, store, hello)
  themis/workbench/models/        # the browser↔BFF view model + its request/reply envelopes
  themis/workbench/rpc/           # the browser-facing Connect service (Workbench)
  themis/litcache/models/         # at-rest domain contracts (the manifest)
buf.yaml                          # module, lint rules, buf breaking config, deps
buf.lock                          # pinned buf deps (protovalidate)
apps/web/buf.gen.yaml             # protobuf-es (local protoc-gen-es plugin)
```

Generated (committed, never hand-edited): `themis/<pkg>/*_pb2.py` + `.pyi` (+ `*_pb2_grpc.py` for services),
`buf/validate/validate_pb2.py` (the protovalidate dep stub), `apps/web/src/gen/**/*_pb.ts`.

## Code generation

`tools/schema/regen.py` generates locally — no remote plugins:

| Stage           | Tool                                     | Output                                                        |
| --------------- | ---------------------------------------- | ------------------------------------------------------------- |
| Python messages | `grpcio-tools` protoc (`--python/--pyi`) | `themis/**/*_pb2.py` + `.pyi`; `buf/validate/validate_pb2.py` |
| gRPC stubs      | `grpcio-tools` protoc (`--grpc_python`)  | `themis/rpc/*_pb2_grpc.py` (service protos only)              |
| protobuf-es     | local `protoc-gen-es` via `buf generate` | `apps/web/src/gen/**/*_pb.ts`                                 |

`buf export` first materializes the protos + the `buf.lock`-pinned `buf/validate` dep into a temp tree (a cached module
fetch, not a remote-plugin call); `grpcio-tools`' protoc runs over it, its bundled protoc pinning the generated-code
version to the protobuf 6.x runtime. gRPC is scoped to `themis/rpc/` (a data proto declares no service). The
`buf/validate` stub is emitted because the `protovalidate` wheels ship no Python stub; well-known types
(`google.protobuf.*`) resolve from `grpcio-tools`' bundled includes and stay runtime-provided. protobuf-es uses the
app's own `@bufbuild/protoc-gen-es` — no BSR.

Remote-plugin codegen is deliberately avoided: `buf generate`'s remote plugins hit the BSR anonymous rate limit, and
`protoc_builtin` embeds a protoc whose generated-code version (7.x) outruns the protobuf runtime the dependency tree
pins (6.x, capped by `apache-beam` and `grpcio-health-checking`). `grpcio-tools`' protoc tracks that runtime.

Policy:

- **Generated code is committed**; CI gates on freshness (`regenerate && git diff --exit-code`). The toolchain isn't
  needed at install/runtime, generated code is reviewable in PRs, and the public mirror stays self-contained.
- **Generated code is never hand-edited.** Cross-boundary behaviour (protovalidate calls) lives in the hand-written
  layer that imports the generated stubs.

## Authoring rules

Enforced by `buf lint` (`BASIC` + `ENUM_VALUE_PREFIX` + `ENUM_ZERO_VALUE_SUFFIX` + `PROTOVALIDATE`; the
package/directory rules are excepted — see `buf.yaml`):

- **Proto-canonical enums.** `UPPER_SNAKE` values, each prefixed with the enum name, and a `*_UNSPECIFIED = 0` sentinel
  (proto3 requires a zero member; it is never a valid domain value — fail loud if it reaches persisted data). Only a
  genuinely-external arbitrary string (e.g. a raw licence URL) is a `string` field. The declared name is not a wire
  concern — enums are integer on the wire, and the single codec maps int↔name on each side.
- **Sum types are a `oneof`** over variant messages, so a cross-field "X iff Y" invariant is structural. Mark the field
  `[(buf.validate.field).required = true]` and the oneof `option (buf.validate.oneof).required = true` so an absent or
  empty variant is rejected.
- **Declared-field constraints are protovalidate options** — `repeated.min_items`, `string.min_len`, message-level `cel`
  for cross-field rules — enforced by `protovalidate.validate` at the boundary.
- **Document with leading `//` comments** on messages, fields, enums, and rpcs; the `.proto` is the source of truth and
  carries the domain documentation. (`protoc` carries a comment into an rpc's generated stub docstring but not into a
  message/field stub — see Documentation flow.)
- **Explicit field numbers**; evolution adds fields and retires them against a reservation (see Schema evolution).

## Read-modify-write and integrity

Binary proto at rest exists for one property: **an older reader round-trips a newer writer's fields untouched.** Unknown
fields (keyed by number + wire-type) are retained in the message's unknown-field set and re-serialized — a name-keyed
text projection (proto-JSON, pbtxt) cannot do this. So a read-modify-write can't silently drop a field a newer component
added. The only artifact modified in place is the litcache manifest (path-addressed by uuid; blobs are immutable). Safe
RMW requires, in order:

1. **Preserve unknowns.** RMW goes through the binary proto message, whose unknown-field set survives
   parse→modify→serialize. Never RMW through a lossy typed projection (proto→JSON→proto drops unknowns).
1. **Fail loud on the write path as a backstop** ("open on read, closed on write"): if write-back cannot account for
   content it didn't model, raise rather than drop — the fail-loud stance belongs on the modify-and-persist path, not
   every read.
1. **Atomic write-back.** GCS `ifGenerationMatch` precondition so a concurrent RMW can't clobber (lost-update is the
   other corruption vector).

Residual, unfixable generically: **semantic coupling** — preservation keeps an unknown field's bytes, not the artifact's
consistency if a field the writer *did* change is derived-from or invariant-with the preserved one. Mitigated by keeping
additive fields independent, and by (2).

## External data (bucket 3)

For a cached upstream payload (a raw Crossref or Unpaywall response): store the upstream's JSON as-is; model only the
fields we read, as a **subset view, not a closed contract**. Reads are tolerant (extra upstream fields ignored). We do
not RMW external JSON; if we must annotate it, we write a *separate* authored artifact rather than mutating the upstream
blob.

The bucket-1-vs-3 axis is **re-derivability**, not "did we author a schema over it": a write-once projection over a
retained/re-fetchable authoritative source is bucket 1 (regenerate wholesale, never RMW — e.g. `metadata.pb` from the
re-fetchable PubMed XML); a cached per-request response we keep as received and cannot re-derive is bucket 3 (preserve
the raw bytes, tolerant subset read).

## Schema evolution

**Breaking changes are ruled out.** A proto evolves in place: add a field with a fresh number, add enum members.
Renaming, renumbering, or repurposing a field is not allowed. **Retiring one is** — delete it and reserve **both its
number and its name**, which is what keeps every reader correct: the number for binary readers, the name for the browser
seam, which is proto3-JSON and keys on it (Serialization posture, bucket 2). A retired field is gone from the contract
rather than lingering as one that must never be set; nothing new may claim what it held. Reserving governs *reuse*; the
*transition window* is a separate precondition — a field may only be retired once nothing in flight still sets it. The
binary legs tolerate the skew, but the browser seam rejects a JSON key the schema no longer declares
(`ignoreUnknownFields: false`), so a browser-facing field is retired in two changes: stop sending it and deploy, then
delete it. So there is no schema version, no migration, no version dispatch: a reader parses every artifact ever
written, and binary proto's unknown-field retention means an older reader round-trips a newer writer's fields untouched.

- **CI compat gate** (`buf breaking`, `schema-compat.yml`). Each committed `.proto` is diffed against its base-branch
  baseline under the `FILE` category — minus `FIELD_NO_DELETE`, plus the two rules that admit a deletion only when the
  number and the name are reserved — and **fails on any incompatible delta, with no in-tool override**. Pre-release
  contracts (no persisted data, no deployed consumer) are excluded until they stabilize (`tools/schema/buf_compat.py`).
- **Golden fixtures.** A corpus of historical artifacts the current schema must still parse — the regression proof that
  evolution stayed compatible.

## Wire and RPC

The internal wire transport is gRPC (HTTP/2, binary protobuf). RPC shapes are authored in the same
`schema/proto/themis/rpc/` protos and gated by the same `buf breaking`; [`services.md`](services.md) is the service
pattern (the servicer base, the `themis.rpc.<domain>` stubs, the deploy). Rolling deploys keep several message
generations in flight; adding a field plus proto's tolerant readers keeps that skew safe on this leg, and a retirement
is sequenced to stay safe on the browser one (Schema evolution). The BFF↔services leg is this same gRPC/proto; the
browser↔BFF leg is the Connect protocol as JSON (Serialization posture, bucket 2).

The two legs differ because a browser cannot speak gRPC: it has no access to HTTP/2 trailers, which gRPC uses to carry
final status. That is why the browser-facing service lives outside `themis/rpc/` — under
`schema/proto/themis/workbench/rpc/`, its own package, so the surface a curator's browser reaches never shares a
namespace with the ones only the sandbox and the internal services may. It is the one service proto with no Python
implementation; the gRPC-stub pass selects on *declares a service*, so it emits a `_pb2_grpc.py` nothing imports. That
is the same generate-uniformly, consume-selectively posture the TypeScript side already has (`apps/web/src/gen/` carries
stubs for the internal protos the browser never calls), and it is preferred to teaching the generator an exception.

## Protos in Cloud SQL columns

*Forward-looking* — no at-rest proto sits in Cloud SQL yet (durable artifacts are GCS blobs). When authored proto data
does land in a column, three shapes, in order of preference:

1. **GCS pointer, metadata in SQL (default for anything large).** The row stores a pointer (path/generation) to a binary
   `.pb` in GCS plus the few columns needed to query/join; the proto itself stays out of the database. Keeps rows small,
   reuses the RMW discipline above, and avoids putting large opaque blobs in Postgres.
1. **Inline binary proto (`bytea`) for small records.** The message serialized into a `bytea` column, with any field
   that must be **indexed or queried** pulled out into its own standalone column. Preserves the unknown-field round-trip
   property (it is still binary), so RMW-safe. Cost: the pulled-out columns must be kept in sync with the embedded proto
   on every write — a real burden, since Postgres has no native proto awareness. Use only when a record is genuinely
   small and RMW'd.
1. **Proto→JSON (`jsonb`) — read-mostly only.** Enables native Postgres JSON lookups and indexing without pulling fields
   out. But proto3-JSON is name-keyed and **cannot round-trip an unknown field** (same limitation as any text
   projection), so a read-modify-write through the `jsonb` silently drops fields a newer writer added — the exact
   corruption binary proto exists to prevent. Acceptable **only** for data that is written whole and never RMW'd through
   the JSON, or where losing unknown fields on write is genuinely fine.

Default to (1) for large blobs and (2) for small authored records that need RMW-safety; reach for (3) only for
query-heavy, read-mostly data where the unknown-field caveat is understood and accepted.

## Documentation flow

A `//` comment on the `.proto` is the documentation the generators carry:

| Comment on…       | Reaches                                     | Via                                     |
| ----------------- | ------------------------------------------- | --------------------------------------- |
| an rpc / service  | the gRPC Stub/Servicer **method** docstring | `grpc/python` plugin                    |
| a message / field | **nothing** on the generated Python         | `protoc` carries no comment into `_pb2` |

The path documents *operations* but not *message shapes* — an agent introspecting a message type at runtime sees no
descriptions. Generate any agent/skill doc from the contract (the `.proto`, or a `FileDescriptorProto` compiled with
`--include_source_info`), which retains every comment, not from runtime `__doc__`.

## Tooling

- `buf` on PATH for `buf export` / `buf lint` / `buf breaking` (CI installs it via `bufbuild/buf-setup-action`);
  `grpcio-tools` (the `codegen` uv group) for the Python protoc; `apps/web` deps installed for `protoc-gen-es`.
- **Regen is a `tools/` Python orchestrator** (`uv run --group codegen python -m tools.schema.regen`). CI runs it and
  checks for no diff (`schema-freshness.yml`).
- **The compat gate** (`schema-compat.yml`) runs `buf breaking` over each committed `.proto` against its base-branch
  baseline via a pinned `buf` image; the pure logic is unit-tested.
- **`buf lint`** is a pre-commit hook (gated in `lint.yml`).

## Why this shape

- **One codec, so no cross-language JSON disciplines.** An earlier direction kept TypeSpec as the IDL authoring both
  proto and a browser Zod view model, with disciplines (snake_case name-as-JSON-string enums, flat sum types) whose sole
  job was to keep proto and Zod agreeing on one canonical JSON projection. Collapsing to one `.proto` source — with the
  browser↔BFF seam typed by the same protobuf-es JSON codec on both ends, no separate Zod schema — makes that agreement
  automatic, so the disciplines are unnecessary. Hand-authored `.proto` is then simpler and unlocks two things the
  TypeSpec emitter could not express: **protovalidate** options and real **`oneof`**.
- **The seam is a declared service, not a set of routes.** Naming the browser↔BFF methods in the `.proto` is what lets
  the envelope be generated rather than written: adding a method is a proto edit and an implementation, not a new route
  directory plus a fetch wrapper plus an error mapping.
- **Structural over validated.** A `oneof` makes an illegal sum-type state unrepresentable; the residual constraints are
  declarative protovalidate options, buf-lint-checked — not hand-written validators.
- **Binary at rest for RMW safety** (above) — the one property a text projection cannot provide.
