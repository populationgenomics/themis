// Types shared across the corpus. Kept in common.tsp to model the layout rule
// (domain-shared types live here) and to exercise the cross-file ref the
// bundler must resolve.

// String enum: snake_case members, hyphenated string values.
enum Colour {
  red: "red",
  green: "green",
  blue: "blue",
}
