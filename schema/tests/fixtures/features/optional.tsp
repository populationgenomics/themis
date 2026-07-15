import "@typespec/protobuf";

using TypeSpec.Protobuf;

namespace Features;

// Optional field (no default): `?` emits `optional` in proto (explicit presence);
// Zod `.optional()`.
model OptionalHolder {
  @field(1) required_field: string;
  @field(2) optional_field?: string;
}
