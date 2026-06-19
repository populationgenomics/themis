# Design: TypeSpec as the schema IDL

**Related:** [`../plans/literature-cache.md`](../plans/literature-cache.md) (the
motivating models); [`literature-evidence-layer.md`](literature-evidence-layer.md) §2
(stored artifacts).

## Purpose

One source of truth for data shapes, authored in **TypeSpec** (`.tsp`), generating
typed models in every language we use (Python/Pydantic, TypeScript/Zod) plus the JSON
Schema that validates durable artifacts. Replaces per-language model definitions kept
in sync by hand.

TypeSpec over hand-authored JSON Schema: terser and readable; the compiler errors when
a construct an enabled emitter cannot represent, so it *enforces* the translatable
subset rather than leaving it to review; one source extends to OpenAPI/protobuf later
without re-modelling.

## At-rest vs on-the-wire

Two classes of serialized data. Both evolve **additively only** — breaking changes are
ruled out (see Schema evolution) — and differ only in their content model.

- **At-rest (durable).** litcache GCS artifacts — `manifest.json`, the write-once
  `knowledge_units.jsonl`, crosswalk objects. **JSON by design** (not proto): readable,
  diffable, and validated by the same generated JSON Schema the compat gate diffs.
  Schemas are **closed** content models (`additionalProperties:false`): the current
  schema reads every artifact ever written, additive changes stay compatible, and an
  unknown field fails loud as drift.
- **On-the-wire (ephemeral).** RPC messages between themis components. Components do not
  roll out atomically, so several schema generations are in flight during any rolling
  deploy; additive-only evolution plus tolerant readers keeps that skew safe. Schemas
  are **open** content models so a reader ignores fields a newer producer adds.
  Transport undecided.

## Authoring and layout

```
schema/                # .tsp sources — the primary artifact
  tspconfig.yaml
  litcache/
    main.tsp           # entry point: imports the domain's files
    manifest.tsp
    knowledge_unit.tsp
    common.tsp         # types shared across the domain
  <other-domains>/...
```

- **Split files by cohesion, not by a fixed rule.** A domain is a directory of `.tsp`
  files with `main.tsp` as the entry point that imports them. Co-locate a type with its
  small, local nested types; break out large or independently-meaningful substructures
  (and domain-shared types) into their own files. Avoid both extremes — a single
  monolithic file, and a rigid one-file-per-type split (a single top-level type can be
  huge, e.g. a PubMed citation, and must itself span files).
- Each type is **current** and evolved in place — **additively only** (CI-enforced, see
  Schema evolution). There are no per-version snapshots and no breaking-change path.
- Wire-only shapes follow the same rules — current source only.

## Code generation

`.tsp` is the single source of truth; every target is emitted from it directly. JSON
Schema is one such target, not a hub the others pass through. `<domain>` in the output
paths is the `schema/<domain>/` directory: compiling its `main.tsp` entry point emits one
bundled schema for the whole domain (everything `main.tsp` imports), not one file per type.

| Target | Emitter / tool | Output | Consumer |
|---|---|---|---|
| JSON Schema | `@typespec/json-schema` | `jsonschema/<domain>.schema.json` | at-rest validation; source for Pydantic; compat-gate baseline |
| Pydantic v2 | `datamodel-code-generator` (from JSON Schema) | `<pkg>/models/<domain>/*.py` | Python backend |
| Zod | direct tsp→Zod emitter (`typespec-zod`) | frontend `src/models/<domain>/*.ts` | TS frontend (wire messages) |

JSON Schema has two independent jobs: it is the **committed, language-neutral at-rest
validation artifact**, and it is the **codegen source for Pydantic** — no mature direct
tsp→Pydantic emitter exists, and the schema is needed for at-rest validation regardless.
Validation of durable bytes therefore has two tsp-derived routes — the JSON Schema and
the Pydantic models — neither of which involves Zod.

Zod does **not** pass through JSON Schema. The frontend validates wire messages, never
durable artifacts, so Zod need not share the at-rest schema; emitting it straight from
`.tsp` keeps it as faithful to the source as any other target and avoids
`json-schema-to-zod`, which doesn't resolve `$ref`s to `#/$defs/…` — the named,
reused subschemas TypeSpec emits for every model (not inline-nested objects, which it
handles) — silently emitting `z.any()` for them and otherwise forcing a dereference
pass. The trade is leaning on a
community tsp→Zod emitter instead of the more widely used JSON-Schema converter — a
maturity bet, not an architectural compromise, since the emitter reads the same `.tsp`.

The JSON Schema passes through one transform before `datamodel-code-generator`:

- **Normalize** — the emitter's `bundleId` produces one 2020-12 file with all types
  under `$defs`, but inter-type refs are `$id`-relative (`"$ref": "AccessKind.json"`),
  which `datamodel-code-generator` reads as a file path and can't resolve (TypeSpec
  Discussion #4084). Normalize rewrites them to `#/$defs/…` and drops the per-`$def`
  `$id`. The committed JSON Schema is this normalized single-file form. Required only as
  a workaround for #4084; droppable if the emitter fixes it. Worked through, verified
  end to end, in the appendix.

Policy:

- **Generated code is committed**; CI gates on freshness (`regenerate &&
  git diff --exit-code`). The toolchain isn't needed at install/runtime, generated
  code is reviewable in PRs, and the public mirror stays self-contained.
- **Generated code is never hand-edited.** Behaviour (cross-field validation) lives in a
  thin hand-written layer that imports the generated models.

## Authoring rules

The subset that round-trips cleanly to the kept targets (JSON Schema, Pydantic, Zod);
the compiler enforces most, these cover the rest:

- **snake_case identifiers**, so emitted JSON property names match the wire format
  directly (no per-field rename).
- **A field with a default must be optional:** `flagged?: boolean = false`. A
  required field with a default keeps the default out of the generated model.
- **Express a cross-field "X iff Y" constraint as a named union** of per-variant
  models, each carrying a `const` discriminant — not `@discriminator` model
  inheritance. The named union enforces the constraint in every target; inheritance
  does not survive codegen (appendix).
- **Backtick reserved identifiers** (e.g. `` `unknown` ``).
- **Avoid:** heterogeneous/tuple arrays, deep recursion, `patternProperties`, and
  `anyOf`/`oneOf` of unrelated shapes.

Constraints that can't be expressed structurally are **not** generated — they live in
the hand-written layer over the generated models.

## Worked example — litcache `Version`

The `access_publisher`-iff-`licensed` rule has two modelling options. Both shown with
their verbatim emitted JSON Schema, Pydantic, and Zod — the pattern reviewers will see
in PRs.

### (a) Flat — rule enforced in the hand-written layer

```tsp
enum AccessKind {
  free_to_read: "free-to-read", licensed: "licensed",
  institution_captured: "institution-captured", `unknown`: "unknown",
}
model Version {
  version: string;
  source_hash: string;
  licence: Licence;
  access: AccessKind;
  access_publisher?: string;     // iff access == licensed — NOT enforced by the schema
  renderings: Rendering[];
}
model Manifest { doc_id: string; versions: Version[]; }
```

```jsonc
// JSON Schema — Version
{ "type": "object",
  "properties": {
    "version": {"type":"string"}, "source_hash": {"type":"string"},
    "licence": {"$ref":"#/$defs/Licence"}, "access": {"$ref":"#/$defs/AccessKind"},
    "access_publisher": {"type":"string"},
    "renderings": {"type":"array","items":{"$ref":"#/$defs/Rendering"}} },
  "required": ["version","source_hash","licence","access","renderings"] }
```

```python
# Pydantic
class AccessKind(StrEnum):
    free_to_read = 'free-to-read'
    licensed = 'licensed'
    institution_captured = 'institution-captured'
    unknown = 'unknown'

class Version(BaseModel):
    version: str
    source_hash: str
    licence: Licence
    access: AccessKind
    access_publisher: str | None = None   # free-floating; invariant in a hand-written validator
    renderings: list[Rendering]

class Manifest(BaseModel):
    doc_id: str
    versions: list[Version]
```

```ts
// Zod
export const VersionSchema = z.object({
  version: z.string(),
  source_hash: z.string(),
  licence: z.enum(["CC-BY-4.0","publisher-proprietary","unknown"]),
  access: z.enum(["free-to-read","licensed","institution-captured","unknown"]),
  access_publisher: z.string().optional(),
  renderings: z.array(z.object({ converter: z.string(), converter_version: z.string() })),
})
```

`access_publisher` is free-floating; nothing stops `access=licensed` with no publisher.
That invariant is a hand-written `@model_validator`.

### (b) Named union — rule enforced structurally

```tsp
model FreeToRead          { access: "free-to-read"; }
model Licensed            { access: "licensed"; publisher: string; }
model InstitutionCaptured { access: "institution-captured"; }
model UnknownAccess       { access: "unknown"; }
union Access { FreeToRead, Licensed, InstitutionCaptured, UnknownAccess }
model VersionDU { version: string; source_hash: string; access: Access; }
```

```jsonc
// JSON Schema — Access is an anyOf; the licensed variant requires publisher
"Access":   { "anyOf": [ {"$ref":"#/$defs/FreeToRead"}, {"$ref":"#/$defs/Licensed"},
                         {"$ref":"#/$defs/InstitutionCaptured"}, {"$ref":"#/$defs/UnknownAccess"} ] }
"Licensed": { "type":"object",
              "properties": {"access":{"type":"string","const":"licensed"},"publisher":{"type":"string"}},
              "required": ["access","publisher"] }
```

```python
# Pydantic
class Licensed(BaseModel):
    access: Literal['licensed']
    publisher: str                        # required exactly when access == licensed

class Access(RootModel[FreeToRead | Licensed | InstitutionCaptured | UnknownAccess]):
    root: FreeToRead | Licensed | InstitutionCaptured | UnknownAccess

class VersionDU(BaseModel):
    version: str
    source_hash: str
    access: Access                         # read the variant via version.access.root
```

```ts
// Zod
export const VersionDUSchema = z.object({
  version: z.string(),
  source_hash: z.string(),
  access: z.union([
    z.object({ access: z.literal("free-to-read") }),
    z.object({ access: z.literal("licensed"), publisher: z.string() }),
    z.object({ access: z.literal("institution-captured") }),
    z.object({ access: z.literal("unknown") }),
  ]),
})
```

The invariant holds in all three targets with no hand-written validator
(`access=licensed` without `publisher` is rejected). Cost: in Python the field is a
`RootModel`, read via `version.access.root`. Use (b) where the guarantee must hold in
every language; (a) where a simpler schema is worth a hand-written check.

## Schema evolution

**Breaking changes are ruled out.** A schema evolves in place, **additively only** — add
optional fields, add enum members. Anything that could invalidate existing data or an
older reader (removing or renaming a field, narrowing a value set, tightening a pattern)
is not allowed. So there is **no schema version, no migration, and no version dispatch**:
the current schema reads every artifact ever written, and a reader validates against it
directly. A field is never removed, only deprecated in place (kept optional, ignored).

If a change ever genuinely cannot be expressed additively, that is an out-of-band,
manual, one-off — explicitly outside this system, not a supported schema operation.

- **CI compat gate.** Each change to a committed `jsonschema/<domain>.schema.json` is
  checked against the last released version with **`chuckd`** (Confluent's JSON Schema
  compatibility rules) in **`BACKWARD`** mode, and **fails hard on any incompatible
  delta — there is no override.** One tool covers both structural breaks *and*
  value-domain narrowing (enum-member removal, pattern/range tightening — the narrowing a
  string-projecting check like proto misses). `BACKWARD`, not `FULL`: `FULL` forbids
  adding a field at all, so it's unusable; the wire's forward tolerance comes from
  lenient readers.
  - **Content model sets the verdict on adding a field** (validated against `chuckd`,
    appendix): **at-rest closed** (`additionalProperties:false`) — add-optional is clean,
    removal and narrowing fail, and an unknown field fails loud as drift; **wire open** —
    a reader tolerates added fields, so `chuckd`'s `PROPERTY_ADDED_TO_OPEN_CONTENT_MODEL`
    on an open schema is downgraded to a warning ("a lenient reader tolerates an added
    field" is an invariant, not a judgment). Removal and narrowing trip *different*
    findings and still fail.
- **Golden fixtures.** A corpus of historical artifacts under `tests/fixtures/` that the
  current schema must still validate — a regression test that additive-only really held.

The cost is that schemas only grow — deprecated fields linger — in exchange for never
migrating data, versioning an artifact, or dispatching on version.

## Wire and RPC

Components don't roll out atomically, so a rolling deploy always has several schema
generations in flight. Additive-only evolution plus tolerant readers makes that skew
safe; strict lockstep isn't a real property and isn't required.

- **Forward tolerance:** wire models are open, so an older consumer ignores fields a
  newer producer adds.
- **Additive only:** new fields are optional; never remove, repurpose, or narrow a field
  (the same rule as at-rest — see Schema evolution).
- **Enforce in CI** with the same `chuckd` `BACKWARD` gate; on an open wire schema a
  field addition is downgraded to a warning (see Schema evolution).
- **No in-payload version:** the transport carries message-type identity (a schema id in
  the Confluent wire format, the message type in gRPC/proto, route + content type in
  REST), so there is no `schema_version` on the wire.

**Transport is undecided.** Shapes are defined once in TypeSpec regardless:

- **REST/JSON:** add `@typespec/http` + `@typespec/openapi3` → OpenAPI → clients.
- **gRPC:** add `@typespec/protobuf` → `.proto` + stubs.

**Protobuf is deferred, not excluded.** No durable data uses proto (at-rest is JSON)
and no transport is chosen, so proto is not a target today. If gRPC is adopted, add the
protobuf emitter **scoped to the specific wire models**, authored to proto's
constraints (string fields for hyphenated/dotted vocabularies; explicit `@field`
numbers; literals dropped — appendix). The durable/shared schema never targets proto,
so it keeps the richer enums and literals the kept targets use; adding proto later is
local to the wire models.

## Tooling

- Node toolchain in CI for `tsp compile` (the `@typespec/json-schema` and `typespec-zod`
  emitters); a Python dev dep (`codegen` uv group) on `datamodel-code-generator`. The
  compat gate (see Schema evolution) runs `chuckd` as a pinned CI container (it is a JVM tool,
  shipped only as a Docker image). Node deps pinned via a `schema/` lockfile.
- **Regen is a `tools/` Python orchestrator run as `uv run python -m tools.<name>`** —
  the repo has no task runner and this stays in the uv/Python ecosystem. It runs
  `tsp compile` (emitting JSON Schema and Zod) → normalize the JSON Schema → Pydantic.
  CI runs it and checks for no diff.
- **The compat gate is a separate CI step**: diff each committed
  `jsonschema/<domain>.schema.json` against its last released version through `chuckd`,
  fail hard on any incompatible delta (see Schema evolution).

## Staged adoption

**No further litcache work begins until Stage 0 is merged and green** — settling the
toolchain before any schema we care about exists is the point of this ordering.

0. **Settle the toolchain and the reliable feature subset — no litcache.** Stand up
   `schema/` (npm deps, `tspconfig.yaml`), the JSON Schema normalize pass, the Pydantic
   generator and the direct Zod emitter (Zod generated and `tsc`-smoke-tested, no consumer yet),
   the `tools/` regen orchestrator, and the CI gates (freshness + compat gate). The
   driver is a **feature-coverage corpus of schemas in a `tests/` directory** — one per
   feature (string enum, optional, optional-with-default, literal, named union, nested,
   array, scalar formats) — that defines the reliable subset and doubles as the CI
   smoke test and new-domain template. The **compat-tool experiment is done** (a
   before/after change matrix run against `chuckd`): `chuckd` `BACKWARD` covers
   structural rules and value-domain narrowing, closed content models make add-optional
   clean, `FULL` is unusable (see Schema evolution, appendix). Output: every convention locked,
   gate settled, zero domain modelling.
1. **litcache schema** — author the real models (the `litcache/` type files) on the
   settled rails. Unblocks the litcache S0 build.
2. Add the hand-written load/dump facade + golden fixtures (a corpus of historical
   artifacts the current schema must still validate).
3. Bring further domains under TypeSpec as they appear.
4. Add a wire emitter (proto or OpenAPI) once the RPC transport is decided.

## Open questions

- gRPC vs REST/JSON for RPC — defers the wire-emitter choice.
- Generated-code review burden on the public mirror (diff volume/noise).
- `typespec-zod` maturity — a community emitter, not first-party. Stage 0 must confirm
  it covers the feature corpus; fallback is JSON Schema + `json-schema-to-zod` with the
  dereference pass.

## Appendix: emitter behaviour

Validated end to end (TypeSpec 1.13 → `@typespec/json-schema` → JSON Schema →
`datamodel-code-generator` → Pydantic v2; `typespec-zod` → Zod direct from `.tsp`;
`@typespec/protobuf` → proto). Records the why behind the authoring rules and the proto
deferral.

**Cross-target feature coverage:**

| Feature (as authored) | JSON Schema | Pydantic | Zod | Proto |
|---|---|---|---|---|
| string enum | `enum:[…]` | `StrEnum` | `z.enum([…])` | string field (proto enums are integer) |
| literal (`access: "licensed"`) | `const: "licensed"` | `Literal['licensed']` | `z.literal("licensed")` | not representable |
| optional `T?` | omit from `required` | `T \| None = None` | `.optional()` | `optional` |
| optional + default | `default` + not required | `= False` | default | (no field defaults) |
| `int32` | `integer` + min/max | `conint(ge,le)` | `.int().gte().lte()` | `int32` |
| array | `array`/`items` | `list[…]` | `z.array(…)` | `repeated` |
| nested model | `$ref` | nested class | `z.object({…})` | `message` |
| named union | `anyOf` of `const`-tagged variants | `RootModel[A\|B…]` | `z.union([…])` | `oneof` |
| field numbers | — | — | — | required (`@field(n)`) |

**Pydantic specifics.** Enums → `StrEnum`; scalars `url`→`AnyUrl`,
`utcDateTime`→`AwareDatetime`, `plainDate`→`date` (import auto-aliased around a field
named `date`). Content model follows the class: **wire** schemas are open (no
`additionalProperties:false`), so a reader ignores unknown fields — forward-tolerant
reads under skew; **at-rest** schemas are closed, so unknown fields fail loud as drift.

**Why a field with a default must be optional.** `flagged: boolean = false` emits
`default:false` but keeps the field in `required`, so codegen drops the default. Making
it optional removes it from `required`, and the default survives.

**Why named union, not `@discriminator` inheritance.** Model-inheritance
`@discriminator` emits subtypes with `allOf:[$ref base]` but types the use site as the
bare base — no `anyOf`, no discriminator — so codegen produces `access: Access` (base)
and the constraint is lost. A named union emits the `anyOf` that codegen turns into an
enforcing union. (A true tagged union with `Field(discriminator=…)` would need the
`discriminator` keyword via `@discriminated`; the `anyOf`+`const` form already
enforces.)

**Why Zod is emitted direct from `.tsp`, not via `json-schema-to-zod`.** That converter
does not resolve `$ref`s to `#/$defs/…` — the named, reused subschemas TypeSpec emits
for each model (inline-nested objects are fine) — and silently emits `z.any()` for them
(its default recursion depth is also 0). Routing Zod through JSON Schema would therefore
need a dereference pass to inline every ref before conversion. A direct tsp→Zod emitter
(`typespec-zod`) reads the source models and sidesteps the converter entirely — no
dereference pass. Zod is frontend-wire-only, so it gains nothing from sharing the
at-rest JSON Schema.

**Protobuf constraints (for the deferred wire-emitter path).** Proto enums are integer
with a forced `0` member and identifier-only names, so a string vocabulary with hyphens
or dots (SPDX licences, access kinds) cannot be a proto enum — model it as a `string`
field (set-validation then lives in code, which lockstep/compatible-evolution does not
need on the wire). Proto has no `const`; a literal discriminant (e.g. a union tag)
becomes a plain field validated in code. Every message property needs an explicit
`@field(n)`. None of these block
proto; they are why proto is kept off the shared/durable schema and added scoped to
wire models only.

**`@typespec/versioning`.** With the json-schema emitter it does not produce per-version
schemas — it merges all `@added`/`@removed` annotations into one shape. Per-version
emission is an OpenAPI-emitter feature. Not needed here: there are no schema versions —
each domain has a single current `.tsp` evolved additively.

**Compat-gate experiment (chuckd).** Change matrix run through `chuckd` on draft-07
schemas, both content models:

| change | open, BACKWARD | closed, BACKWARD |
|---|---|---|
| add-optional | break (`PROPERTY_ADDED_TO_OPEN_CONTENT_MODEL`) | **OK** |
| add-required | break | break |
| remove-optional | OK | break |
| remove-required | OK | break |
| rename-field | break | break |
| enum-add | OK | OK |
| enum-remove | break (`COMBINED_TYPE_SUBSCHEMAS_CHANGED`) | break |
| `const` bump | break | break |
| pattern-tighten | break (`PATTERN_ADDED`) | break |

Findings: (1) `chuckd` `BACKWARD` catches value-domain narrowing (enum-remove,
pattern-tighten, `const`) — the proto/`jsonsubschema`-structural blind spot — so one
tool covers both halves and `jsonsubschema` is not needed. (2) On open models it flags
`add-optional` exactly as `jsonsubschema` does; the lever is the content model, not the
tool. (3) Closed models give clean additive semantics (add-optional OK, removal/narrowing
break) — hence at-rest closed. (4) `FULL` fails every field addition regardless of
content model, so the gate runs `BACKWARD`. Exit code is the count of incompatibilities
(0 = compatible). chuckd is a JVM tool, Docker-only (no PyPI).

**Bundle → normalize → Pydantic (verified end to end).** A multi-file domain (four
`.tsp` files, cross-file refs, an enum, an optional field) compiled with
`@typespec/json-schema` (`emitAllModels: true`, `bundleId: litcache.schema.json`,
`file-type: json`), TypeSpec 1.13.0.

1. **Output is one bundled file**, 2020-12, all types under `$defs` — and **standards
   valid** (`check-jsonschema --check-metaschema` passes). Confirms a domain emits one
   schema file, not one per type.
2. **But the emitter's refs are `$id`-relative**, and each `$def` keeps its own `$id`:

   ```jsonc
   "$defs": {
     "AccessKind": { "$id": "AccessKind.json", "type": "string", "enum": [...] },
     "Version": { "$id": "Version.json", "properties": {
       "access": { "$ref": "AccessKind.json" } } }   // <- not #/$defs/AccessKind
   }
   ```
3. **`datamodel-code-generator` can't consume that** — it reads `"$ref": "AccessKind.json"`
   as a file path: `$ref file not found: …/AccessKind.json`, no output. (TypeSpec
   Discussion #4084.)
4. **Normalize** — strip each `$def`'s `$id`/`$schema`, rewrite `"$ref": "X.json"` →
   `"#/$defs/X"`. Still metaschema-valid, and `datamodel-code-generator` then emits
   correct Pydantic v2: `AccessKind` enum, `access_publisher: str | None = None`,
   cross-file refs resolved to `list[Version]` / `list[KnowledgeUnit]`.

So the bundled form is standard but the JSON-Schema toolchain needs the normalize pass;
it's a #4084 workaround, not intrinsic. (Minor: the bundle root has no top-level type,
so datamodel-codegen also emits a spurious `Model(RootModel[Any])` — harmless; pin a
root type or drop it.)
