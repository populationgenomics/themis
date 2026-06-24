// Optional-with-default: a field carrying a default must be optional (authoring
// rule — a default on a required field is dropped from the generated model).
// Emits JSON Schema `default`; Pydantic `bool = False`; Zod `.default(false)`.
model DefaultHolder {
  flagged?: boolean = false;
}
