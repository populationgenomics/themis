-- 0006_projects.sql -- the Project registry the web BFF reads for display names. The
-- web SA reads it only; project rows, like memberships, are administered out of band.
-- The foreign keys anchor the session plane's project_id columns to a registered
-- Project. ${WEB_DB_USER} is the web SA IAM DB-user login (matching sql.py).
CREATE TABLE projects (
    id   text PRIMARY KEY,
    name text NOT NULL
);
ALTER TABLE project_members ADD FOREIGN KEY (project_id) REFERENCES projects (id);
ALTER TABLE analyses ADD FOREIGN KEY (project_id) REFERENCES projects (id);
ALTER TABLE session_context ADD FOREIGN KEY (project_id) REFERENCES projects (id);
GRANT SELECT ON projects TO "${WEB_DB_USER}";
