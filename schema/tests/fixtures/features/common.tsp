// Types shared across the corpus — the layout rule (domain-shared types live here)
// and the cross-file ref the emitters must resolve.
namespace Features;

// Proto enums are integer with a zero-valued first member and identifier-only names;
// a hyphenated string vocabulary (SPDX licences, access kinds) is a `string` field
// instead, not an enum (Authoring rules).
enum Colour {
  red: 0,
  green: 1,
  blue: 2,
}
