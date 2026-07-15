import "@typespec/protobuf";
import "./common.tsp";

using TypeSpec.Protobuf;

namespace Features;

// Array fields: a scalar array and an enum array — emit `repeated` (proto) / z.array
// (Zod).
model ArrayHolder {
  @field(1) tags: string[];
  @field(2) palette: Colour[];
}
