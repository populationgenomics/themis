// Feature-coverage corpus entry point. One file per construct in the proto-reliable
// subset (docs/design/typespec.md "Authoring rules"); main.tsp imports them all so a
// single `tsp compile` emits one proto (+ Zod) exercising every construct. Doubles as
// the new-domain layout template (main.tsp + cohesion-split files + common.tsp for
// shared types) and the CI smoke test: each construct round-trips to proto + Zod (see
// schema/tests/test_generated_*).
import "@typespec/protobuf";
import "./string_enum.tsp";
import "./optional.tsp";
import "./optional_default.tsp";
import "./nested.tsp";
import "./array.tsp";
import "./scalar_formats.tsp";

using TypeSpec.Protobuf;

@package({ name: "features" })
namespace Features;
