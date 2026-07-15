# ADR 0004: JSON as canonical interchange — enum and sum-type disciplines

**Status:** Proposed. **Extends** [`0003-serialization-posture.md`](0003-serialization-posture.md) (does not supersede
it): at-rest storage stays binary proto. Resolves two open items 0003 left — the bucket-2 "deferred (protobuf-es vs
Zod)" target, and the "proto and Zod no longer share a reliable subset" consequence.

## Context

ADR 0003 put authored data on binary proto at rest, left the browser target deferred, and flagged that once JSON Schema
is gone proto and a Zod emitter share no reliable subset (proto needs integer enums, emits no `oneof`), so a type
shipped both at-rest and browser-side needs **dual, divergent definitions**. That divergence is not intrinsic — it comes
from letting proto and the view model make *different* modeling choices for the same shape. Constrain both to one set of
choices and the divergence collapses.

The goal: the **canonical JSON projection of any authored type is byte-identical everywhere it appears** — proto3-JSON,
the BFF↔frontend view model, a dump-tool rendering, a JSON value crossing any boundary. JSON becomes a lingua franca: a
JSON value is unambiguous and losslessly interpretable at every seam, and the emitted Zod is a faithful validator of it.

## Decision

Two authoring disciplines, plus the Zod bucket-2 resolution.

### 1. Canonical-string enums

A proto enum's declared value name **is** the canonical JSON string, verbatim: snake_case identifiers (`llm_ocr`,
`free_to_read`), never proto-style `UPPER_SNAKE`/per-value prefixing, never kebab-case. proto3-JSON emits the declared
name. The `*_unspecified = 0` member proto3 requires is a **reserved sentinel, never a valid domain value** — enforced
out of persisted/read data (fail loud), so it never appears in valid JSON and the view-model enum omits it.

### 2. Flat sum types

No `oneof` in authored proto. A sum type is a **flat message** — a `string` discriminant + the variant fields as
`optional` — with its "X iff Y" invariant enforced by **one shared, fail-loud validator per type, wired at the writer
and the reader**. Tagged unions, if wanted, exist only in a downstream projection, not in the authored shape.
(Generalizes the existing `Access` model.)

### 3. Bucket 2 → Zod, generated from TypeSpec via a canonicalizer

The browser view model is **Zod, emitted from the same `.tsp`** (resolving 0003's deferral). `typespec-zod`
(pre-release, `0.0.0-68`) is **not proto-aware** — it emits an integer proto enum as `z.enum([0, 1, 2])` (the values,
not the names) and a well-known type as an opaque `{ _extern: z.never() }`, neither of which validates the canonical
proto3-JSON. So a post-processor, `tools.schema.zod_canonicalize`, rewrites the emission against the committed `.proto`:
integer enums → name-string enums (sentinel dropped), well-knowns → their JSON validator (`Timestamp` →
`z.iso.datetime()`), then `zod_reorder` orders it. Under disciplines 1–2 the result is flat and canonical-string — it
*is* the lingua-franca JSON shape — so the frontend validates the canonical JSON directly and branches on the
discriminant. No BFF flat↔tagged mapping, no hand-authored second schema. protobuf-es remains a possible future
optimization, not a current need.

### 4. At-rest stays binary proto

Storage is unchanged from 0003: durable artifacts are binary proto, for the read-modify-write unknown-field retention
0003 turns on. JSON is the canonical **interchange and inspection** projection, **not** a storage format — a name-keyed
JSON projection still drops unknown fields on RMW, so the manifest and friends stay binary.

## Conformance

Because `zod_canonicalize` sources every enum name and well-known type **from the committed proto**, the emitted Zod is
conformant to proto3-JSON **by construction** — there is no independent Zod vocabulary that could drift. The freshness
gate (regenerate → `git diff --exit-code`) is therefore the enforcement: a `.tsp` change that alters an enum or a
well-known re-emits both proto and Zod, and an un-regenerated commit fails CI. A separate name-set comparison gate would
be redundant. `zod_canonicalize` fails loud on an enum with no proto counterpart or an unmapped well-known, so a new
construct is a deliberate extension of the canonicalizer, not a silent mis-projection.

## Why this over the alternatives

- **vs. ditch TypeSpec, hand-author proto + Zod.** Considered. Handwriting is low-cost, but one source + the freshness
  gate makes drift impossible; handwriting relies on discipline. TypeSpec stays the single IDL and repo norm.
- **vs. protobuf-es (browser consumes proto directly).** Avoids a second schema language, but Zod is the idiomatic FE
  validator and the canonicalizer makes it correct. Keep protobuf-es in reserve.
- **vs. 0003 as-is (drop Zod, bucket-2 deferred).** Leaves the FE with no committed contract and reintroduces the
  divergence whenever bucket 2 lands. Deciding now, with the divergence canonicalized away, is cheaper than deferring.

## Consequences

- **Supersedes these 0003 consequences:** the "dual definitions" cost (now one source, canonicalized identical); the
  protobuf-es-vs-Zod deferral (Zod chosen); "proto and Zod share no reliable subset" (`zod_canonicalize` *is* the
  reconciliation).
- **Correction to 0003 / `typespec.md`:** declared-field and cross-field invariants (access-iff-publisher, `@minItems`)
  are **code-level** validators, not `protovalidate` — `@typespec/protobuf` emits no protovalidate option. Discipline
  2's per-type boundary validator is where they live. (0003 and the current `typespec.md` say `protovalidate`; corrected
  in the same change.)
- **Cost — a real trade:** flat sum types make illegal states *representable* in the proto/Zod types; the guarantee
  moves from structural to a runtime fail-loud validator. Only real if that validator is the single canonical one, wired
  at both boundaries, and tested. FE enum exhaustiveness is a lint/validator concern, not structural.
- **The FE speaks canonical JSON, flat.** It branches on discriminant strings rather than pattern-matching a tagged
  union — the ergonomic concession that buys the lingua-franca property.
- **`typespec-zod` immaturity is contained** to `zod_canonicalize` + `zod_reorder` — two audited, unit-tested passes
  over its output — rather than spread across every consumer.

## Realization (in the ADR-0003 stack)

- **#121** (remove chuckd, buf sole gate): unaffected.
- **#122** (converge regen to proto): retains the Zod emitter and adds `zod_canonicalize`; the corpus emits proto + Zod.
- **#123** (prune toolchain + docs): prunes only `@typespec/json-schema` + `datamodel-code-generator`; keeps
  `typespec-zod`/`zod`/`typescript`/`tsconfig`/`smoke:zod`. Docs add disciplines 1–2, reference this ADR, and apply the
  protovalidate→code-level correction. This ADR lands here.
- **litcache Phase-1:** at-rest domain, no Zod emit (`zod_out=None`); adopt the flat-sum boundary validator for
  `Access`; enum values are already canonical snake_case.
