-- 0005_project_members.sql -- the user-to-Project membership the web BFF authorizes
-- reads against (AuthorizedBackend). The web SA reads it only; rows are administered
-- out of band, and no membership row means no access.
-- ${WEB_DB_USER} is the web SA IAM DB-user login (the SA email minus the
-- .gserviceaccount.com suffix, matching sql.py), substituted by the runner from
-- THEMIS_MIGRATE_SUBSTITUTIONS.
CREATE TABLE project_members (
    project_id text NOT NULL,
    user_email text NOT NULL,
    PRIMARY KEY (project_id, user_email)
);
GRANT SELECT ON project_members TO "${WEB_DB_USER}";
