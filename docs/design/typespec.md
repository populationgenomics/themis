# Design: TypeSpec as the schema IDL

**Related:** [`../plans/literature-cache.md`](../plans/literature-cache.md) (the motivating models);
[`literature-evidence-layer.md`](literature-evidence-layer.md) §2 (stored artifacts).

## Purpose

One source of truth for data shapes, authored in **TypeSpec** (`.tsp`), generating typed models in every target we use:
**proto** for authored machine-to-machine data — durable at-rest artifacts and inter-service RPC (→ Python stubs,
optionally Pydantic sourced from proto) — and **Zod** for the browser-facing view model. Replaces per-language model
definitions kept in sync by hand. The serialization posture — which data is proto, which is JSON — is
[ADR 0003](../adr/0003-serialization-posture.md).

TypeSpec over hand-authored `.proto`: terser and readable; the compiler errors when a construct an enabled emitter
cannot represent, so it *enforces* the translatable subset rather than leaving it to review; one source emits both
targets without re-modelling.

## Usage

Two authored targets: **proto** (→ committed `.proto` → `protoc`/grpcio-tools Python stubs; RPC uses this already, e.g.
`schema/proto/themis/rpc/auth.proto`, #91) and **Zod** (emitted direct from the `.tsp`), with the freshness (S0.4) and
`buf breaking` compat (S0.6) CI gates in place. The at-rest litcache domain is being re-cut from the retired closed-JSON
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
- Cross-field constraints that don't survive codegen live in a thin hand-written layer over the generated models (see
  Authoring rules); declared-field constraints move to `protovalidate`.

### Using a schema from TypeScript

- Import the generated Zod schemas from the frontend `src/models/<domain>/` (built in S0.3). Zod is the **browser-facing
  view model** (BFF↔frontend), never a durable at-rest artifact.

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
1. **Authored, browser-facing → JSON + Zod.** The BFF↔frontend view model. Zod types gate it; no separate compat tool.
1. **Externally-defined, ingested → JSON, modelled as a subset view.** Raw upstream payloads we cache (Crossref,
   Unpaywall). We store the upstream's JSON as-is and model only the fields we read, tolerant of extras; we never
   round-trip it through a lossy typed write (ADR 0003, "External data").

Content-addressed blobs (`sources/`, `renderings/`, `supplementary/`) are opaque bytes, outside all three.

## Authoring and layout

```
schema/                # .tsp sources + the Bun toolchain (authoring only)
  package.json         # pins @typespec/compiler + emitters; lockfile alongside
  tspconfig.yaml       # shared emitter config
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
    proto/             # the corpus's committed generated .proto (+ zod/)
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

`.tsp` is the single source of truth. Proto and Zod are emitted from it **directly**; the Python stubs are the one
two-step path (standard `protoc` over the committed `.proto`), not a JSON-Schema hub the others pass through. `<domain>`
in the output paths is the `schema/<domain>/` directory: compiling its `main.tsp` entry point emits one artifact for the
whole domain (everything `main.tsp` imports), not one file per type.

| Target       | Emitter / tool                          | Output                              | Consumer                                        |
| ------------ | --------------------------------------- | ----------------------------------- | ----------------------------------------------- |
| proto        | `@typespec/protobuf`                    | `schema/proto/<domain>/*.proto`     | at-rest artifacts + RPC; `buf breaking` gate    |
| Python stubs | `protoc` / grpcio-tools (from `.proto`) | `<pkg>/models/<domain>/*_pb2.py`    | Python backend (optionally Pydantic-from-proto) |
| Zod          | direct tsp→Zod emitter (`typespec-zod`) | frontend `src/models/<domain>/*.ts` | browser view model (BFF↔frontend)               |

Proto is the authored-data format for bucket 1 (at-rest + RPC); the committed `.proto` is both the compat-gate baseline
and the `protoc` source for the Python stubs. Declared-field constraints ride along as `protovalidate` options. If a
domain wants Pydantic ergonomics, source the models **from proto** (a proto→pydantic generator, or `betterproto`), never
by reintroducing JSON Schema as a hub (ADR 0003).

Zod is emitted straight from `.tsp`, not via `json-schema-to-zod`, which doesn't resolve `$ref`s to `#/$defs/…` — the
named, reused subschemas TypeSpec emits for every model (not inline-nested objects, which it handles) — silently
emitting `z.any()` for them and otherwise forcing a dereference pass. The trade is leaning on a community tsp→Zod
emitter — a maturity bet, not an architectural compromise, since the emitter reads the same `.tsp`.

Policy:

- **Generated code is committed**; CI gates on freshness (`regenerate && git diff --exit-code`). The toolchain isn't
  needed at install/runtime, generated code is reviewable in PRs, and the public mirror stays self-contained.
- **Generated code is never hand-edited.** Behaviour (cross-field validation) lives in a thin hand-written layer that
  imports the generated models.

## Authoring rules

The subset that round-trips cleanly to the kept targets (proto → Python stubs, Zod); the compiler enforces most, these
cover the rest:

- **snake_case identifiers**, so emitted property names match the proto/JSON field names directly (no per-field rename).
- **A field with a default must be optional:** `flagged?: boolean = false`. A required field with a default keeps the
  default out of the generated model.
- **A cross-field "X iff Y" constraint** cannot be structural at rest: `@typespec/protobuf` emits no `oneof` (unions are
  rejected). Model the discriminant flat (a `string` field + the conditional field) and enforce the invariant with a
  `protovalidate` rule (worked example). A named union is still the right shape for a Zod **view model** — it emits a
  discriminated union — where the structural guarantee is wanted browser-side.
- **Backtick reserved identifiers** (e.g. `` `unknown` ``).
- **Avoid:** heterogeneous/tuple arrays, deep recursion, `anyOf`/`oneOf` of unrelated shapes, and `int64` (TypeSpec
  encodes it as a JSON string for JS number precision, so the proto-JSON/Python view sees `string` while `typespec-zod`
  emits `z.bigint()` — an inconsistent cross-target shape).

The reliable subset is pinned concretely by the feature-coverage corpus under `schema/tests/fixtures/features/` — one
`.tsp` per construct, each verified to round-trip to the kept targets (S0.5).

Constraints that can't be expressed structurally are **not** generated — they live in the hand-written layer over the
generated models.

## Worked example — the `access`-iff-`publisher` rule

`Access` is at-rest (bucket 1), so the target is **proto** — and `@typespec/protobuf` **cannot emit a `oneof`**: a
union-typed field is rejected outright and there is no `oneof` decorator (verified against 0.83.0 and the 0.84 dev
line). So a cross-field "X iff Y" invariant cannot hold *structurally* in proto. The at-rest model is **flat** — a
`string` discriminant plus the conditional field — with the invariant enforced by `protovalidate`:

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

Nothing structural stops `access = "licensed"` with no `publisher`; the invariant is a `protovalidate` CEL rule on the
message (fail-loud at construction, ADR 0003):

```text
(this.access == "licensed") == has(this.publisher)
```

Giving up the `oneof` costs nothing durable: on the wire a set `oneof` member encodes identically to a standalone field,
so `oneof` is codegen sugar (mutual-exclusion + `WhichOneof`), not a wire/at-rest distinction. A **browser view model**
(bucket 2) that surfaces access *can* keep the structural form — Zod is the target there, and `typespec-zod` does emit a
discriminated union from a named union:

```ts
// Zod (bucket 2) — the structural guarantee holds where Zod is the target
export const AccessSchema = z.discriminatedUnion("access", [
  z.object({ access: z.literal("free-to-read") }),
  z.object({ access: z.literal("licensed"), publisher: z.string() }),
  z.object({ access: z.literal("institution-captured") }),
  z.object({ access: z.literal("unknown") }),
])
```

(The only way to a proto `oneof` is `Extern` to a hand-authored `.proto` — see "Escape hatch" in the appendix — not
worth it for a rule `protovalidate` covers.)

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

The proto authoring constraints now apply to **all** proto, at-rest and wire alike (no longer wire-specific): integer
enums with a forced `0` member and identifier-only names — so hyphenated/dotted vocabularies (SPDX licences, access
kinds) are `string` fields, membership validated in code or `protovalidate`; no `const` and no `oneof`, so a
discriminant is a plain `string` field with its invariant in `protovalidate`; an explicit `@field(n)` on every property
(appendix).

## Tooling

- Bun toolchain in CI for `tsp compile` (the `@typespec/protobuf` and `typespec-zod` emitters); `protoc` / grpcio-tools
  (a Python dev dep, `codegen` uv group) compile the emitted `.proto` to Python stubs. JS deps pinned via the committed
  `schema/` Bun lockfile.
- **Regen is a `tools/` Python orchestrator run as `uv run python -m tools.<name>`** — the repo has no task runner and
  this stays in the uv/Python ecosystem. It runs `tsp compile` (emitting `.proto` and Zod) → `protoc` the `.proto` to
  Python stubs → reorder the Zod (see appendix). CI runs it and checks for no diff.
- **The compat gate is a CI workflow** (`schema-compat.yml`) running `buf breaking` over each committed `.proto` against
  its base-branch baseline (see Schema evolution). It shells out to a pinned `buf` and fails hard on any incompatible
  delta; the pure logic is unit-tested. `buf breaking` is the sole authored-data gate (the chuckd job is retired, ADR
  0003).

## Staged adoption

**No further litcache work begins until Stage 0 is merged and green** — settling the toolchain before any schema we care
about exists is the point of this ordering.

0. **Settle the toolchain and the reliable feature subset — no litcache.** Stand up `schema/` (Bun deps,
   `tspconfig.yaml`), the `@typespec/protobuf` and direct Zod emitters, the `protoc` Python-stub step (Zod generated and
   `tsc`-smoke-tested, no consumer yet), the `tools/` regen orchestrator, and the CI gates (freshness + `buf breaking`).
   The driver is a **feature-coverage corpus of schemas in a `tests/` directory** — one per feature (string enum,
   optional, optional-with-default, literal, named union, nested, array, scalar formats) — that defines the reliable
   subset and doubles as the CI smoke test and new-domain template. Output: every convention locked, gate settled, zero
   domain modelling.
1. **litcache schema** — author the real models (the `litcache/` type files) on the settled rails. The at-rest half is
   being re-cut from closed JSON to proto per ADR 0003. Unblocks the litcache build.
1. Add the hand-written load/dump facade (proto messages ↔ storage, with the RMW discipline of ADR 0003) + golden
   fixtures (a corpus of historical artifacts the current schema must still parse).
1. Bring further domains under TypeSpec as they appear.
1. RPC uses the same `@typespec/protobuf` emitter (gRPC); services build on it (see [`services.md`](services.md)).

## Open questions

- Generated-code review burden on the public mirror (diff volume/noise).
- Whether the pre-release `typespec-zod` emitter (pinned `0.0.0-68`, internal name `efv2-zod-sketch`) holds up as the
  corpus grows: it covers the corpus and emits faithful named schemas but mis-orders declarations across files, so it
  needs the reorder pass (appendix). With JSON Schema retired (ADR 0003) there is no `json-schema-to-zod` fallback; an
  emitter regression is handled by fixing the reorder pass or the emitter itself.
- **proto→pydantic generator maturity** — verify before sourcing the backend's Pydantic from proto; `betterproto` is the
  fallback (ADR 0003).

## Appendix: emitter behaviour

Validated end to end (TypeSpec 1.13 → `@typespec/protobuf` → `.proto` → `protoc`/grpcio-tools → Python stubs;
`typespec-zod` → Zod direct from `.tsp`). Records the why behind the authoring rules and the proto constraints.

**Cross-target feature coverage:**

| Feature (as authored)          | Proto                                     | Python (proto stub)     | Zod                     |
| ------------------------------ | ----------------------------------------- | ----------------------- | ----------------------- |
| string enum (hyphenated)       | `string` field (proto enums integer-only) | `str`                   | `z.enum([…])`           |
| literal (`access: "licensed"`) | not representable                         | —                       | `z.literal("licensed")` |
| optional `T?`                  | `optional`                                | presence via `HasField` | `.optional()`           |
| optional + default             | (proto3 has no field defaults)            | —                       | default                 |
| `int32`                        | `int32`                                   | `int`                   | `.int().gte().lte()`    |
| array                          | `repeated`                                | repeated field          | `z.array(…)`            |
| nested model                   | `message`                                 | nested message class    | `z.object({…})`         |
| named union                    | unsupported — flat + `protovalidate`      | —                       | `z.discriminatedUnion`  |
| field numbers                  | required (`@field(n)`)                    | —                       | —                       |

**Why a field with a default must be optional.** A required field with a default keeps the default out of the generated
model; making it optional preserves it. (proto3 scalars carry implicit zero-defaults regardless; the rule matters for
the Zod view model and for field presence.)

**Named union (Zod only), and why not `@discriminator` inheritance.** For a Zod view model a named union of
`const`-tagged variants emits a discriminated union that enforces the invariant structurally; `@discriminator`
inheritance does not — it types the use site as the bare base, so codegen produces `access: Access` (base) and the
constraint is lost. Proto has no counterpart: `@typespec/protobuf` rejects union-typed fields and exposes no `oneof`
decorator, so an at-rest domain models the discriminant flat and enforces the invariant with `protovalidate` (worked
example).

**Escape hatch (`Extern`).** `model X is Extern<"path.proto", "pkg.X">` makes the proto emitter emit an `import` +
verbatim reference instead of converting the model — the mechanism behind `WellKnown.Timestamp`/`Empty`. It is the only
route to a hand-authored proto construct the emitter can't produce (a `oneof`, `google.protobuf.Timestamp`). Use it for
well-known types; for a cross-field invariant prefer flat + `protovalidate` — `Extern` splits the source of truth (a
hand-authored `.proto` island kept in sync by hand) for a `oneof` that is only codegen sugar (identical on the wire).

**Why Zod is emitted direct from `.tsp`, not via `json-schema-to-zod`.** That converter does not resolve `$ref`s to
`#/$defs/…` — the named, reused subschemas a JSON-Schema route would emit for each model — and silently emits `z.any()`
for them. A direct tsp→Zod emitter (`typespec-zod`) reads the source models and sidesteps the converter entirely.

The emitter's one defect (S0.3) is declaration order: it emits each model as `export const <name> = …` but a schema can
land before another it references, which Zod's eager evaluation rejects at compile time (`TS2448`/`TS2454`, used before
declaration). A deterministic **reorder pass** (`tools/schema/zod_reorder.py`) parses the emission, builds the reference
graph, and re-emits the declarations dependency-first. The reliable subset forbids recursion, so the graph is acyclic; a
cycle (which Zod expresses only via `z.lazy`) fails loud.

**Proto authoring constraints.** Proto enums are integer with a forced `0` member and identifier-only names, so a string
vocabulary with hyphens or dots (SPDX licences, access kinds) cannot be a proto enum — model it as a `string` field
(membership validated in code or `protovalidate`). Proto has no `const` and the emitter no `oneof`, so a discriminant is
a plain `string` field and its cross-field invariant lives in `protovalidate`, not the shape. Every message property
needs an explicit `@field(n)`; a paramless op maps to `google.protobuf.Empty`; streaming is `@stream`. These apply to
all authored proto — at-rest and wire alike (ADR 0003).

**`@typespec/versioning`.** Not needed here: there are no schema versions — each domain has a single current `.tsp`
evolved additively (see Schema evolution).
