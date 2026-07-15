import "@typespec/protobuf";
import "./source.tsp";

using TypeSpec.Protobuf;

// The per-paper cache record — identity, provenance, cache-control. The manifest write is
// the ingestion commit point; it is the system of record (the crosswalk mint table is a
// rebuildable index over these). See docs/adr/0002-manifest-renderings-and-reference-model.md
// and docs/plans/literature-cache.md "Schemas".
namespace Litcache;

model ExternalIds {
  @field(1) doi?: string;
  @field(2) pmid?: string;
  @field(3) pmcid?: string;
  @field(4) arxiv?: string;
  @field(5) biorxiv?: string;
}

model Equivalence {
  @field(1) edges: string[]; // other doc_ids of the same work
  @field(2) canonical_doc_id: string; // lowest uuid in the class
}

model Retraction {
  @field(1) retracted?: boolean = false;
  @field(2) source?: string;
  @field(3) date?: string;
}

enum AssociatedFileRole {
  associated_file_role_unspecified: 0,
  figure: 1,
  supplementary: 2,
}

model AssociatedFile {
  @field(1) role: AssociatedFileRole;
  @field(2) name: string;
  @field(3) source_url?: string;
  @field(4) path?: string; // content-addressed bytes' location (supplementary/{hex}.{ext}); absent until fetched
}

model Manifest {
  @field(1) doc_id: string; // uuid4, == directory name
  @field(2) external_ids: ExternalIds;
  @field(3) claim_key: string; // precedence-primary external id, e.g. "doi:10.…"
  @field(4) equivalence: Equivalence;
  @field(5) retraction: Retraction;
  @field(6) sources: Source[]; // primary-artifact lineages (pdf, jats-xml, …)
  @field(7) renderings: Map<string, Rendering>; // key = the markdown's content hash (sha256 hex)
  @field(8) files: AssociatedFile[]; // every KNOWN supplementary file, even un-fetched (lazy fetch)
}
