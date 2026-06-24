// Named union of per-variant models, each pinning a `const` discriminant — the
// "X iff Y" pattern that enforces a cross-field rule structurally in every
// target (docs/design/typespec.md worked example (b)): the licensed variant
// requires `publisher` (the free variant has none). Emits a JSON Schema
// anyOf; Pydantic a RootModel union; Zod z.union.
model FreeToRead {
  access: "free-to-read";
}
model Licensed {
  access: "licensed";
  publisher: string;
}
union Access {
  FreeToRead,
  Licensed,
}
model AccessHolder {
  access: Access;
}
