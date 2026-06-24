// Scalar formats: the built-in TypeSpec scalars in the reliable subset, each
// projecting a JSON Schema numeric type (with bounds) or a `format`. Verifies
// the format/type survives to Pydantic and Zod.
//
// int64 is deliberately excluded: TypeSpec encodes it as a JSON string (JS
// number precision), so JSON Schema/Pydantic see `string` while typespec-zod
// emits `z.bigint()` — an inconsistent cross-target shape, hence out of the
// reliable subset.
model ScalarHolder {
  count: int32;
  ratio: float64;
  when: utcDateTime;
  day: plainDate;
  link: url;
}
