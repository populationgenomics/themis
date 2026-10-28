-- 0018_projects_retired.sql -- retires the Project registry and its memberships: which Projects exist,
-- their datasets and who is on each now come from the site's ACL, which the auth service reads
-- (access-control.md). An Analysis keeps its project_id as an opaque id, so a Project that leaves the
-- ACL leaves its Analyses stored and unreachable.
--
-- The foreign keys onto projects go first, since Postgres refuses to drop a table another table
-- references; the web SA's SELECT grants go with the tables. The web tier that read both tables is
-- replaced by the same deploy's `pulumi up`, before this runs, so no deployed reader is left behind.
ALTER TABLE analyses DROP CONSTRAINT analyses_project_id_fkey;

ALTER TABLE session_context DROP CONSTRAINT session_context_project_id_fkey;

DROP TABLE project_members;

DROP TABLE projects;
