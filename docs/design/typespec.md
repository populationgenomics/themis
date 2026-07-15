# Design: TypeSpec as the schema IDL

**Related:** [`../plans/literature-cache.md`](../plans/literature-cache.md) (the motivating models);
[`literature-evidence-layer.md`](literature-evidence-layer.md) §2 (stored artifacts).

## Purpose

One source of truth for data shapes, authored in **TypeSpec** (`.tsp`). Two targets: **proto** for authored
machine-to-machine data — durable at-rest artifacts and inter-service RPC (→ Python stubs, optionally Pydantic sourced
from proto) — and **Zod** for the browser-facing view model (bucket 2), canonicalized to the same proto3-JSON shape so
JSON is a lingua franca across every seam. Replaces per-language model definitions kept in sync by hand. The
serialization posture — which data is proto, which is JSON — is [ADR 0003](../adr/0003-serialization-posture.md); the
canonical-JSON disciplines (enum strings, flat sum types) are [ADR 0004](../adr/0004-json-canonical-interchange.md).

TypeSpec over hand-authored `.proto`: terser and readable; the compiler errors when a construct an enabled emitter
cannot represent, so it *enforces* the translatable subset rather than leaving it to review; one source emits both
targets without re-modelling.

## Usage

The primary target is **proto** (→ committed `.proto` → `protoc`/grpcio-tools Python stubs; RPC uses this already, e.g.
`schema/proto/themis/rpc/auth.proto`, #91), with the freshness (S0.4) and `buf breaking` compat (S0.6) CI gates in
place. The browser-facing bucket 2 emits **Zod**, canonicalized against the committed proto so it validates the same
proto3-JSON (ADR 0004; see Code generation). The at-rest litcache domain is being re-cut from the retired closed-JSON
rails onto this proto path (ADR 0003; see Staged adoption).

### Authoring a schema

- Edit `.tsp` under `schema/<domain>/`; `main.tsp` is the entry point that imports the domain's files. File-splitting is
  covered in Authoring and layout, the translatable subset in Authoring rules.
- The feature corpus lives under `schema/tests/fixtures/<domain>/`, not the repo `tests/` tree: TypeSpec resolves
  emitter packages by walking up from each `.tsp` file to a `node_modules`, so every source must sit at or below the
  `schema/` Bun toolchain.
- Regenerate the committed artifacts with `uv run python -m tools.schema.regen` (one-time toolchain install:
  `bun install` in `schema/`). Generated code is **committed and never hand-edited**; a `.tsp` change committed without
  regenerating fails CI (S0.4).

### Using a schema from Python

- Import the generated proto stubs (`<pkg>/models/<domain>/…_pb2`); parse/serialize the `.pb` bytes directly. If a
  domain wants Pydantic ergonomics, source the models **from proto**, not from a reintroduced JSON Schema (ADR 0003).
- Cross-field constraints and declared-field constraints don't survive codegen (the emitter emits no `protovalidate`);
  both live in a thin hand-written validator over the generated models, wired at the boundary (see Authoring rules).

### Using a schema from TypeScript

- Import the committed Zod (`schema/tests/zod/…` today; a real view model when one lands). It validates the canonical
  proto3-JSON — name-string enums, flat sum types — so a value the BFF speaks parses on both sides (ADR 0004).

Consumers import committed generated code from their own tree; nothing depends on `schema/` (the authoring toolchain) at
build or run time — the property the generated-code-is-committed policy (Code generation) buys.

## Three serialization buckets

Serialized data falls into three buckets by *who owns the schema* and *who consumes it* (ADR 0003). All authored data
evolves **additively only** — breaking changes are ruled out (see Schema evolution).

1. **Authored, internal machine-to-machine → binary proto**, gated by `buf breaking`. Both durable litcache artifacts
   (`manifest.pb`, the write-once per-paper `knowledge_units.pb`, `metadata.pb`) and inter-service RPC over gRPC (see
   Wire and RPC). Proto's open readers ignore fields a newer producer adds — the skew tolerance a rolling deploy needs —
   and, decisively, binary proto **retains unknown fields** through a parse→modify→serialize round trip, so
   read-modify-write can't silently drop a field an older component didn't model. That immunity is why at-rest is
   *binary* proto, not a name-keyed text projection (proto-JSON, pbtxt).
1. **Authored, browser-facing → Zod over canonical JSON.** The BFF↔frontend view model, emitted as Zod from the same
   `.tsp` and canonicalized to the proto3-JSON shape (ADR 0004), so the JSON the BFF projects validates identically on
   both sides. The frontend has no real models yet — the feature corpus is the only Zod today — but the target is
   settled (protobuf-es stays a future option; see Open questions).
1. **Externally-defined, ingested → JSON, modelled as a subset view.** Raw upstream payloads we cache (Crossref,
   Unpaywall). We store the upstream's JSON as-is and model only the fields we read, tolerant of extras; we never
   round-trip it through a lossy typed write (ADR 0003, "External data").

Content-addressed blobs (`sources/`, `renderings/`, `supplementary/`) are opaque bytes, outside all three.

**Placement.** An at-rest domain is a plain library under the one `themis/` namespace — `themis.<domain>` (e.g.
`themis.litcache`), authored as `.tsp` under `schema/<domain>/`, with its committed `.proto` and generated stubs
produced per [Code generation](#code-generation). It has no wire surface, so it is *not* a `themis.services` /
`themis.rpc` / `themis.clients` member. Staying under `themis/` rather than a top-level package follows the
single-namespace convention ([`../repo-structure.md`](../repo-structure.md)); a top-level name would pay off only for a
cache shared *outside* themis, which is speculative — revisit if a real out-of-themis consumer appears.

## Authoring and layout

```
schema/                # .tsp sources + the Bun toolchain (authoring only)
  package.json         # pins @typespec/compiler + emitters; lockfile alongside
  tsconfig.json        # the Zod tsc smoke config (bun run smoke:zod)
  node_modules/        # installed from the lockfile; gitignored
  litcache/            # a real domain
    main.tsp           # entry point: imports the domain's files
    manifest.tsp
    knowledge_unit.tsp
    common.tsp         # types shared across the domain
  <other-domains>/...
  tests/               # the feature corpus + its test, kept under
    fixtures/          #   schema/ so node_modules stays an ancestor (see Usage)
      <domain>/main.tsp
    proto/             # the corpus's committed generated .proto (+ Python stubs)
    zod/               # the corpus's committed generated Zod (.ts)
```

Real domains sit directly under `schema/`; the synthetic feature corpus sits under `schema/tests/`. Both are below the
toolchain, which is what the emitter resolver needs.

- **Split files by cohesion, not by a fixed rule.** A domain is a directory of `.tsp` files with `main.tsp` as the entry
  point that imports them. Co-locate a type with its small, local nested types; break out large or
  independently-meaningful substructures (and domain-shared types) into their own files. Avoid both extremes — a single
  monolithic file, and a rigid one-file-per-type split (a single top-level type can be huge, e.g. a PubMed citation, and
  must itself span files).
- Each type is **current** and evolved in place — **additively only** (CI-enforced, see Schema evolution). There are no
  per-version snapshots and no breaking-change path.
- Wire-only shapes follow the same rules — current source only.

## Code generation

`.tsp` is the single source of truth. Proto is emitted from it **directly**; the Python stubs are a two-step path
(standard `protoc` over the committed `.proto`), not a JSON-Schema hub they pass through. `<domain>` in the output paths
is the `schema/<domain>/` directory: compiling its `main.tsp` entry point emits one artifact for the whole domain
(everything `main.tsp` imports), not one file per type.

| Target         | Emitter / tool                          | Output                           | Consumer                                        |
| -------------- | --------------------------------------- | -------------------------------- | ----------------------------------------------- |
| proto          | `@typespec/protobuf`                    | `schema/proto/<domain>/*.proto`  | at-rest artifacts + RPC; `buf breaking` gate    |
| Python stubs   | `protoc` / grpcio-tools (from `.proto`) | `<pkg>/models/<domain>/*_pb2.py` | Python backend (optionally Pydantic-from-proto) |
| Zod (bucket 2) | `typespec-zod` → `zod_canonicalize`     | `<domain>/*.ts`                  | browser view model; validates canonical JSON    |

Proto is the authored-data format for bucket 1 (at-rest + RPC); the committed `.proto` is both the compat-gate baseline
and the `protoc` source for the Python stubs. Declared-field constraints don't survive codegen (the emitter emits no
`protovalidate`); they live in the hand-written validator layer. If a domain wants Pydantic ergonomics, source the
models **from proto** (a proto→pydantic generator, or `betterproto`), never by reintroducing JSON Schema as a hub (ADR
0003).

Zod (bucket 2) is emitted from the same `.tsp` by `typespec-zod`, then repaired by `tools.schema.zod_canonicalize`:
`typespec-zod` is not proto-aware (it emits integer enums as `z.enum([0,1,2])` and well-known types as
`{ _extern: z.never() }`), so the pass rewrites those against the committed proto to the canonical proto3-JSON shape
(name-string enums, `z.iso.datetime()`), and `zod_reorder` orders the declarations. Sourcing names/types from the proto
makes the Zod conformant to proto3-JSON **by construction** (ADR 0004).

Policy:

- **Generated code is committed**; CI gates on freshness (`regenerate && git diff --exit-code`). The toolchain isn't
  needed at install/runtime, generated code is reviewable in PRs, and the public mirror stays self-contained.
- **Generated code is never hand-edited.** Behaviour (cross-field validation) lives in a thin hand-written layer that
  imports the generated models.

## Authoring rules

The subset that round-trips cleanly to the emitted target (proto → Python stubs); the compiler enforces most, these
cover the rest:

- **snake_case identifiers**, so emitted property names match the proto/JSON field names directly (no per-field rename).
- **A field with a default must be optional:** `flagged?: boolean = false`. A required field with a default keeps the
  default out of the generated model.
- **Enum values are snake_case identifiers** (no hyphens), and that name *is* the canonical JSON string: proto enums are
  integer on the wire but proto3-JSON keys them by name, and `zod_canonicalize` emits the same name (ADR 0004). Reserve
  a `*_unspecified = 0` sentinel (proto3 requires a zero member); it is never a valid domain value. Only
  genuinely-external arbitrary strings (e.g. a raw licence URL) are `string` fields.
- **A cross-field "X iff Y" constraint** cannot be structural at rest: `@typespec/protobuf` emits no `oneof` (unions are
  rejected). Model the discriminant flat (a `string` field + the conditional field) and enforce the invariant with a
  code-level validator, fail-loud at the boundary (worked example) — the emitter emits no `protovalidate`.
- **Backtick reserved identifiers** (e.g. `` `unknown` ``).
- **Document with `/** */` doc-comments (or `@doc`), never `//`.** The compiler discards `//` line comments, so they
  reach no emitter, no generated docstring, and no skill text; only doc-comments propagate (see
  [Documentation flow](#documentation-flow)).
- **Avoid:** heterogeneous/tuple arrays, deep recursion, `anyOf`/`oneOf` of unrelated shapes, and `int64` (TypeSpec
  encodes it as a JSON string for JS number precision, so the proto-JSON/Python view sees `string`).

The reliable subset is pinned concretely by the feature-coverage corpus under `schema/tests/fixtures/features/` — one
`.tsp` per construct, each verified to round-trip to the emitted target (S0.5).

Constraints that can't be expressed structurally are **not** generated — they live in the hand-written layer over the
generated models.

## Worked example — the `access`-iff-`publisher` rule

`Access` is at-rest (bucket 1), so the target is **proto** — and `@typespec/protobuf` **cannot emit a `oneof`**: a
union-typed field is rejected outright and there is no `oneof` decorator (verified against 0.83.0 and the 0.84 dev
line). So a cross-field "X iff Y" invariant cannot hold *structurally* in proto. The at-rest model is **flat** — a
`string` discriminant plus the conditional field — with the invariant enforced by a code-level validator:

```tsp
model Access {
  @field(1) access: string;      // "free-to-read" | "licensed" | "institution-captured" | "unknown"
  @field(2) publisher?: string;  // present iff access == "licensed"
}
```

```proto
message Access {
  string access = 1;
  optional string publisher = 2;
}
```

Nothing structural stops `access = "licensed"` with no `publisher`; the invariant is a **code-level validator** — one
shared, fail-loud function per type, wired at the writer and the reader (ADR 0004) — checking:

```text
(access == "licensed") iff publisher is present
```

Giving up the `oneof` costs nothing durable: on the wire a set `oneof` member encodes identically to a standalone field,
so `oneof` is codegen sugar (mutual-exclusion + `WhichOneof`), not a wire/at-rest distinction. The emitted **Zod** view
model (bucket 2) is flat too — it validates the same canonical JSON and branches on the `access` string — rather than a
discriminated union, so the wire and browser shapes stay identical (ADR 0004). The trade is that the flat model makes an
illegal state (`licensed` with no `publisher`) *representable*; the boundary validator is what rules it out.

(The only way to a proto `oneof` is `Extern` to a hand-authored `.proto` — see "Escape hatch" in the appendix — not
worth it for a rule the validator covers.)

## Schema evolution

**Breaking changes are ruled out.** A schema evolves in place, **additively only** — add a field (with a fresh `@field`
number), add enum members. Anything that could invalidate existing data or an older reader (removing, renaming, or
renumbering a field, repurposing a number, narrowing a value set) is not allowed. So there is **no schema version, no
migration, and no version dispatch**: a reader parses every artifact ever written, and proto's unknown-field retention
means an older reader round-trips a newer writer's fields untouched (ADR 0003). A field is never removed, only
deprecated in place (kept, ignored); a retired number is fenced with `reserved` so it's never reused.

A genuinely necessary breaking change — removing a field once every artifact has been migrated off it — is an
out-of-band, manual one-off: deliberately merge a PR with a red `schema-compat`, as a reviewed decision, not via any
in-tool flag. The gate is a **sign, not a cop**: advisory, never a merge-blocking required check, so a red is a
conscious human call that keeps breaking changes loud rather than routine. Unlike the retired JSON path, proto carries
**reserved-field-id bookkeeping** — the retired number goes in a `reserved` statement (fields are positional), so a
lingering artifact that still carries it stays readable rather than misparsing.

- **CI compat gate** (`buf breaking`, `schema-compat.yml`, S0.6). Each change to a committed `.proto` is diffed against
  its baseline with **`buf breaking`** and **fails (red) on any incompatible delta — no in-tool override**; it catches
  field-number reuse/removal, renumbering, and type changes. `buf breaking` is the **sole** authored-data gate, covering
  both at-rest and RPC proto (the chuckd / JSON-Schema gate is retired, ADR 0003).
  - **Baseline** is the `.proto` on the PR base branch (`HEAD^` on a push to main) — the stand-in for "last released
    version", there being no release/tag process; the released line is `main` under additive-only evolution.
- **Golden fixtures.** A corpus of historical artifacts under `tests/fixtures/` that the current schema must still parse
  — a regression test that additive-only really held.

The cost is that schemas only grow — deprecated fields and reserved numbers linger — in exchange for never migrating
data, versioning an artifact, or dispatching on version.

## Wire and RPC

The wire transport is **gRPC** (HTTP/2, binary protobuf). RPC shapes are authored in TypeSpec and emitted to `.proto`
with `@typespec/protobuf` — the same emitter and `buf breaking` gate as at-rest proto (see Three serialization buckets);
[`services.md`](services.md) is the service pattern built on it (the servicer base, the `themis.rpc.<domain>` stubs, the
deploy). Components don't roll out atomically, so a rolling deploy always has several message generations in flight;
additive-only evolution plus proto's tolerant readers keeps that skew safe.

- **Forward tolerance:** a proto reader ignores fields a newer producer adds — no lockstep required.
- **Additive only:** add a field with a fresh `@field` number; never renumber, remove, or repurpose one (retire with
  `reserved`). The same additive rule as at-rest (see Schema evolution).
- **No in-payload version:** the message type identifies the shape on the wire, so there is no `schema_version` field.

The proto authoring constraints apply to **all** proto, at-rest and wire alike: integer enums with a forced `0` member
and identifier-only names — so enum values are snake_case identifiers (the canonical JSON string, ADR 0004), and only
genuinely-external arbitrary strings (e.g. a raw licence URL) are `string` fields, membership validated in code; no
`const` and no `oneof`, so a discriminant is a plain `string` field with its invariant in a code-level validator; an
explicit `@field(n)` on every property (appendix).

## Documentation flow

Documentation authored on the `.tsp` is what makes the generated code and the agent's skill doc legible. A doc-comment
(`/** */` or `@doc`) is documentation the emitters carry; a `//` line comment is dropped by the compiler and reaches
nothing. Where a doc-comment lands depends on the target:

| Doc-comment on…                    | Reaches                                     | Via                                                  |
| ---------------------------------- | ------------------------------------------- | ---------------------------------------------------- |
| an rpc / service                   | the gRPC Stub/Servicer **method** docstring | `@typespec/protobuf` → `protoc` `grpc_python` plugin |
| a message / field (at-rest or RPC) | **nothing** on the generated Python         | `protoc` carries no comment into `_pb2.py` / `.pyi`  |

Everything is proto now (ADR 0003), so this holds for at-rest artifacts as much as RPC: the path documents *operations*
but not *message shapes*, and an agent introspecting a message type or its fields at runtime sees no descriptions, even
with perfect doc-comments. (When an rpc has no doc-comment, its generated stub carries the tell-tale
`"Missing associated documentation comment in .proto file."` — the placeholder that proves the comment path exists.)
Verify that `@typespec/protobuf` emits doc-comments as `.proto` leading comments; the plugin surfaces them only if they
reach the `.proto`.

**Generate the agent/skill doc from the contract, not from runtime `__doc__`.** The proto `FileDescriptorProto`
(compiled with `--include_source_info`) and the `.tsp` itself each retain *every* doc-comment; a skill-text generator
that reads the contract is complete regardless of what survives into `_pb2`. Depending on Python docstrings instead
would silently drop every message-field description. Attaching docstrings to the generated message types (a documented
facade, or reading the descriptor's source info onto `__doc__`) is a deferred nice-to-have, not the route to
LLM-legibility. Either way the prerequisite is the same: the documentation must be a doc-comment at the source
([Authoring rules](#authoring-rules)).

## Tooling

- Bun toolchain in CI for `tsp compile` (the `@typespec/protobuf` emitter); `protoc` / grpcio-tools (a Python dev dep,
  `codegen` uv group) compile the emitted `.proto` to Python stubs. JS deps pinned via the committed `schema/` Bun
  lockfile.
- **Regen is a `tools/` Python orchestrator run as `uv run python -m tools.<name>`** — the repo has no task runner and
  this stays in the uv/Python ecosystem. It runs `tsp compile` (emitting `.proto`) → `protoc` the `.proto` to Python
  stubs. CI runs it and checks for no diff.
- **The compat gate is a CI workflow** (`schema-compat.yml`) running `buf breaking` over each committed `.proto` against
  its base-branch baseline (see Schema evolution). It shells out to a pinned `buf` and fails hard on any incompatible
  delta; the pure logic is unit-tested. `buf breaking` is the sole authored-data gate (the chuckd job is retired, ADR
  0003).

## Staged adoption

**No further litcache work begins until Stage 0 is merged and green** — settling the toolchain before any schema we care
about exists is the point of this ordering.

0. **Settle the toolchain and the reliable feature subset — no litcache.** Stand up `schema/` (Bun deps,
   `tsconfig.json`), the `@typespec/protobuf` emitter, the `protoc` Python-stub step, the `tools/` regen orchestrator,
   and the CI gates (freshness + `buf breaking`). The driver is a **feature-coverage corpus of schemas in a `tests/`
   directory** — one per feature (enum, optional, optional-with-default, nested, array, scalar formats) — that defines
   the reliable subset and doubles as the CI smoke test and new-domain template. Output: every convention locked, gate
   settled, zero domain modelling.
1. **litcache schema** — author the real models (the `litcache/` type files) on the settled rails. The at-rest half is
   being re-cut from closed JSON to proto per ADR 0003. Unblocks the litcache build.
1. Add the hand-written load/dump facade (proto messages ↔ storage, with the RMW discipline of ADR 0003) + golden
   fixtures (a corpus of historical artifacts the current schema must still parse).
1. Bring further domains under TypeSpec as they appear.
1. RPC uses the same `@typespec/protobuf` emitter (gRPC); services build on it (see [`services.md`](services.md)).

## Open questions

- Generated-code review burden on the public mirror (diff volume/noise).
- **Bucket-2 view model is Zod, canonicalized** (ADR 0004) — `typespec-zod` → `zod_canonicalize` → `zod_reorder`, one
  source, conformant to proto3-JSON by construction. protobuf-es (the browser consuming proto directly) stays a future
  option if the FE would rather not carry Zod, but is not needed. Open sub-questions: `typespec-zod` is pre-release
  (`0.0.0-68`), so watch for emitter changes that widen what `zod_canonicalize` must repair; and the first *real* view
  model (vs the corpus) will exercise whether a curated projection wants to diverge from the stored shape.
- **proto→pydantic generator maturity** — verify before sourcing the backend's Pydantic from proto; `betterproto` is the
  fallback (ADR 0003).

## Appendix: emitter behaviour

Validated end to end (TypeSpec 1.13 → `@typespec/protobuf` → `.proto` → `protoc`/grpcio-tools → Python stubs). Records
the why behind the authoring rules and the proto constraints.

**Cross-target feature coverage:**

| Feature (as authored)  | Proto                               | Python (proto stub)     | Zod (canonicalized)           |
| ---------------------- | ----------------------------------- | ----------------------- | ----------------------------- |
| enum (identifier-safe) | integer enum, forced `0` member     | `int` / enum            | `z.enum([names])`             |
| optional `T?`          | `optional`                          | presence via `HasField` | `.optional()`                 |
| optional + default     | (proto3 has no field defaults)      | —                       | `.optional().default(v)`      |
| `int32`                | `int32`                             | `int`                   | `z.number().int()` (ranged)   |
| array                  | `repeated`                          | repeated field          | `z.array(...)`                |
| nested model           | `message`                           | nested message class    | nested schema ref             |
| `Timestamp`            | `google.protobuf.Timestamp`         | `Timestamp`             | `z.iso.datetime()`            |
| named union            | unsupported — flat + code validator | —                       | flat (no discriminated union) |
| field numbers          | required (`@field(n)`)              | —                       | —                             |

**Why a field with a default must be optional.** A required field with a default keeps the default out of the generated
model; making it optional preserves it. (proto3 scalars carry implicit zero-defaults regardless; the rule matters for
field presence.)

**Why sum types are flat, not named unions.** A named union of `const`-tagged variants would emit a proto `oneof`
(rejected) and, on the Zod side, a discriminated union — a shape that diverges from the flat proto and breaks the
identical-JSON property (ADR 0004). So both targets model the discriminant flat (a `string` field + conditional fields)
and the "X iff Y" invariant is a code-level validator, not the shape (worked example). `@discriminator` inheritance is
also rejected: it types the use site as the bare base (`access: Access`), losing the constraint anyway.

**Escape hatch (`Extern`).** `model X is Extern<"path.proto", "pkg.X">` makes the proto emitter emit an `import` +
verbatim reference instead of converting the model — the mechanism behind `WellKnown.Timestamp`/`Empty`. It is the only
route to a hand-authored proto construct the emitter can't produce (a `oneof`, `google.protobuf.Timestamp`). Use it for
well-known types; for a cross-field invariant prefer flat + a code-level validator — `Extern` splits the source of truth
(a hand-authored `.proto` island kept in sync by hand) for a `oneof` that is only codegen sugar (identical on the wire).

**Proto authoring constraints.** Proto enums are integer with a forced `0` member and identifier-only names; enum values
are snake_case identifiers and that name is the canonical JSON string (ADR 0004). Only a genuinely-external arbitrary
string (e.g. a raw licence URL) is a `string` field (membership validated in code). Proto has no `const` and the emitter
no `oneof`, so a discriminant is a plain `string` field and its cross-field invariant lives in a code-level validator,
not the shape. Every message property needs an explicit `@field(n)`; a paramless op maps to `google.protobuf.Empty`;
streaming is `@stream`. These apply to all authored proto — at-rest and wire alike (ADR 0003).

**`@typespec/versioning`.** Not needed here: there are no schema versions — each domain has a single current `.tsp`
evolved additively (see Schema evolution).
