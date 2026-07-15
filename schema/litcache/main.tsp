import "@typespec/protobuf";
import "./source.tsp";
import "./manifest.tsp";

using TypeSpec.Protobuf;

// litcache domain entry point: imports the domain's files so a single `tsp compile` emits
// one proto. Manifest is the at-rest cache record; `metadata.pb` (bibliographic) rides
// separate rails (pubmed-proto) and is not modelled here — see docs/plans/literature-cache.md.
@package({ name: "themis.litcache.models.litcache" })
namespace Litcache;
