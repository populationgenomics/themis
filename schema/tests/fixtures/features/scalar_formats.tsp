import "@typespec/protobuf";

using TypeSpec.Protobuf;

namespace Features;

// Scalar formats in the reliable subset and their proto mappings: int32→int32,
// float64→double, url→string, utcDateTime carried as WellKnown.Timestamp
// (→ google.protobuf.Timestamp, auto-imported).
//
// Excluded: int64 (TypeSpec encodes it as a JSON string for JS precision — an
// inconsistent cross-target shape); plainDate and bare utcDateTime have no proto
// scalar.
model ScalarHolder {
  @field(1) count: int32;
  @field(2) ratio: float64;
  @field(3) when: WellKnown.Timestamp;
  @field(4) link: url;
}
