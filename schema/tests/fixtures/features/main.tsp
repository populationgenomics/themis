// Feature-coverage corpus entry point. One file per construct in the reliable
// subset (docs/design/typespec.md "Authoring rules"); main.tsp imports them all
// so a single `tsp compile` emits one bundle exercising every feature. Doubles
// as the new-domain layout template (main.tsp + cohesion-split files +
// common.tsp for shared types) and the CI smoke test: each feature round-trips
// to JSON Schema + Pydantic + Zod (see schema/tests/test_generated_*).
import "./string_enum.tsp";
import "./optional.tsp";
import "./optional_default.tsp";
import "./literal.tsp";
import "./named_union.tsp";
import "./nested.tsp";
import "./array.tsp";
import "./scalar_formats.tsp";
