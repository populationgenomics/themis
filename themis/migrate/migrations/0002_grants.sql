-- 0002_grants.sql -- table privileges for session_context.
-- infra/themis_infra/sql.py attaches each service SA as a DB-user LOGIN -- the
-- table-level rights are here. ${AUTH_DB_USER} is the auth SA IAM DB-user login (the
-- SA email minus the .gserviceaccount.com suffix, matching sql.py), substituted by
-- the runner from THEMIS_MIGRATE_SUBSTITUTIONS.
GRANT SELECT ON session_context TO "${AUTH_DB_USER}";
