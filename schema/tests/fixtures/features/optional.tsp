// Optional field (no default): the `?` drops the field from JSON Schema
// `required`; Pydantic `str | None = None`; Zod `.optional()`.
model OptionalHolder {
  required_field: string;
  optional_field?: string;
}
