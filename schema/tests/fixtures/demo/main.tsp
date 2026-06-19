import "./widget.tsp";

// Domain entry point: imports the domain's files so a single
// `tsp compile main.tsp` sees the whole domain. References Widget from
// widget.tsp — a second cross-file ref.
model Catalogue {
  widgets: Widget[];
}
