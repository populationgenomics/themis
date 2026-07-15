import "@typespec/protobuf";

using TypeSpec.Protobuf;

namespace Features;

// Optional-with-default: a field carrying a default must be optional (authoring rule).
// proto3 has no field defaults (emits `optional bool`); Zod keeps `.default(false)`.
model DefaultHolder {
  @field(1) flagged?: boolean = false;
}
