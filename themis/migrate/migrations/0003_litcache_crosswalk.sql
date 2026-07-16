-- 0003_litcache_crosswalk.sql -- the litcache external-id -> doc_id mint table.
-- external_id is a paper's external identifier (DOI, PMID, PMCID, …) as a `scheme:value`
-- key; doc_id is the uuid4 naming that paper's cache directory.
-- A derived index, rebuildable from the manifests (themis.litcache.rebuild), but the live
-- table is the concurrency mint lock (themis.litcache.crosswalk). doc_id is TEXT, not uuid:
-- it is a string everywhere the schema reaches (the manifest field, the proto wire, the
-- paper directory name — proto has no uuid type), so TEXT keeps one representation across
-- the seam. Table grants land with the litcache DB-user (a later migration, as the BFF
-- write grant does in 0002_grants).
CREATE SCHEMA IF NOT EXISTS litcache;

CREATE TABLE IF NOT EXISTS litcache.crosswalk (
    external_id TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL
);

-- Reverse lookup (doc_id -> its external ids) for callers holding a doc_id: the same
-- fact is in the manifest, but the indexed table answers it without a GCS read.
CREATE INDEX IF NOT EXISTS crosswalk_doc_id_idx ON litcache.crosswalk (doc_id);
