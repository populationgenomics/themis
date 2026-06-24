// Nested model reference: a model field typed as another named model — emits a
// $ref to the named subschema (not an inline object), the shape typespec-zod
// must resolve and json-schema-to-zod could not (design doc, Code generation).
model Inner {
  value: string;
}
model Outer {
  inner: Inner;
}
