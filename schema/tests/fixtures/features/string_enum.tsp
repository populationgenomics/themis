import "@typespec/protobuf";
import "./common.tsp";

using TypeSpec.Protobuf;

namespace Features;

// Integer-enum reference (proto enums are integer): emits a proto enum field; Zod
// z.enum. Colour lives in common.tsp (cross-file ref).
model EnumHolder {
  @field(1) colour: Colour;
}
