-- 0019_curation_projects.sql -- binds curation to Projects and retires its own role table: a
-- registration belongs to one Project, and who manages or curates a Project comes from the site's ACL
-- through the auth service (access-control.md). Worksheets, drafts, submissions and assessments
-- inherit the Project through their foreign keys onto `curation.variants`.
--
-- Every existing registration was made in dev's one curation round, so all of them move to Project
-- `validation`. The registration key widens to include the Project: the same variant against the same
-- entity may be registered once in each Project.
--
-- Destructive, and run only where that costs nothing real (migrations.md): between `pulumi up` rolling
-- the web tier and this step, the new web revision reads and writes a column that does not exist yet,
-- so every curation page and route fails until the migration lands. The previous revision is gone by
-- then, so nothing reads `curation.roles` after it is dropped.
ALTER TABLE curation.variants ADD COLUMN project_id TEXT;

UPDATE curation.variants SET project_id = 'validation';

ALTER TABLE curation.variants ALTER COLUMN project_id SET NOT NULL;

ALTER TABLE curation.variants ADD CONSTRAINT variants_project_id_check CHECK (project_id ~ '^[a-z0-9][a-z0-9-]*$');

ALTER TABLE curation.variants DROP CONSTRAINT variants_registration_key;

ALTER TABLE curation.variants ADD CONSTRAINT variants_registration_key
    UNIQUE (project_id, transcript, hgvs_c, mondo_id, mode_of_inheritance);

DROP TABLE curation.roles;
