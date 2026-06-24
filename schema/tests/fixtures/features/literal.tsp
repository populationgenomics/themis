// Literal type: a single constant value pinned on a field. Emits JSON Schema
// `const`; Pydantic `Literal['widget']`; Zod `z.literal("widget")`.
model LiteralHolder {
  kind: "widget";
}
