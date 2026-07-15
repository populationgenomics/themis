import "@typespec/protobuf";

using TypeSpec.Protobuf;

namespace Features;

// Nested model reference: a field typed as another named model — emits a nested
// `message` type (proto) / named object (Zod), not an inline anonymous shape.
model Inner {
  @field(1) value: string;
}
model Outer {
  @field(1) inner: Inner;
}
