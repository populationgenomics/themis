-- 0007_litcache_crosswalk_grant.sql -- the ingestion SA's table rights on the crosswalk.
-- 0003 creates litcache.crosswalk owned by the migrator; the Dataflow ingestion worker SA
-- mints doc_ids there (themis.litcache.crosswalk: SELECT existing external_ids, INSERT new).
-- It has the Cloud SQL connect grant (infra/themis_infra/ingest.py) but no table rights until
-- here. ${INGEST_DB_USER} is the ingest SA IAM DB-user login (the SA email minus the
-- .gserviceaccount.com suffix, matching sql.py), substituted from THEMIS_MIGRATE_SUBSTITUTIONS.
GRANT USAGE ON SCHEMA litcache TO "${INGEST_DB_USER}";
GRANT SELECT, INSERT ON litcache.crosswalk TO "${INGEST_DB_USER}";
