-- 0010_litcache_crosswalk_read_grant.sql -- the evidence service's read rights on the crosswalk.
-- 0003 creates litcache.crosswalk owned by the migrator and 0007 grants the ingestion SA its mint
-- rights (SELECT, INSERT). The evidence read service resolves an external id to a doc_id
-- (MaybeIngestPapers) and never claims one, so it gets SELECT alone: an INSERT here would mint a
-- doc_id naming no manifest. ${EVIDENCE_DB_USER} is the evidence SA IAM DB-user login (the SA email
-- minus the .gserviceaccount.com suffix, matching sql.py), from THEMIS_MIGRATE_SUBSTITUTIONS.
GRANT USAGE ON SCHEMA litcache TO "${EVIDENCE_DB_USER}";
GRANT SELECT ON litcache.crosswalk TO "${EVIDENCE_DB_USER}";
