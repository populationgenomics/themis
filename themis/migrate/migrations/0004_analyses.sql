-- 0004_analyses.sql -- the BFF analyses table and its session-plane write grants.
-- The web BFF owns the session plane: it creates one analyses row per session and writes
-- the resolved session_context. id is the BFF-generated analysis_id -- also the GCS
-- working-document key prefix and session_context.analysis_id. ${WEB_DB_USER} is the web
-- SA IAM DB-user login (the SA email minus the .gserviceaccount.com suffix, matching
-- sql.py), substituted by the runner from THEMIS_MIGRATE_SUBSTITUTIONS.
CREATE TABLE analyses (
    id          text PRIMARY KEY,
    session_id  text NOT NULL UNIQUE,
    project_id  text NOT NULL,
    prompt      text NOT NULL,
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE session_context ADD FOREIGN KEY (analysis_id) REFERENCES analyses (id);
GRANT SELECT, INSERT ON analyses TO "${WEB_DB_USER}";
GRANT INSERT, DELETE ON session_context TO "${WEB_DB_USER}";
