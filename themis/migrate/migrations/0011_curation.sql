-- 0011_curation.sql -- the curation worksheet's own schema (curation-surface.md). A schema rather
-- than a table prefix so grants, ownership and the blast radius of a change are statable about
-- `curation` as a whole. ${WEB_DB_USER} is the web SA IAM DB-user login (the SA email minus the
-- .gserviceaccount.com suffix, matching sql.py), substituted from THEMIS_MIGRATE_SUBSTITUTIONS.
CREATE SCHEMA IF NOT EXISTS curation;

-- Who may reach the surface, and as what. Managers are seeded out of band as project_members rows
-- are; managers add curators through the surface. IAP decides who reaches the app at all, so this
-- authorizes an already-authenticated caller and never authenticates one.
CREATE TABLE curation.roles (
    email    TEXT PRIMARY KEY,
    role     TEXT NOT NULL CHECK (role IN ('manager', 'curator')),
    added_by TEXT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One VBC against one MDE -- SVCv4's variant under analysis and its unit of classification. The
-- entity columns sit here rather than on the worksheet: two curators of one variant must be
-- answering the same question, and an entity that could differ per worksheet would make their
-- answers incomparable. Every field is manager-typed; nothing here is resolved.
-- `inheritance` mirrors the Inheritance enum in themis/curation/models/curation.proto. The surface
-- renders the identity as `transcript:hgvs_c`, so neither column may hold a whole `NM_...:c....`
-- term or be blank -- either way the rendering grows a colon that reads as part of the identity.
CREATE TABLE curation.variants (
    id                TEXT PRIMARY KEY,
    gene              TEXT NOT NULL,
    transcript        TEXT NOT NULL CHECK (transcript <> '' AND transcript NOT LIKE '%:%'),
    hgvs_c            TEXT NOT NULL CHECK (hgvs_c <> '' AND hgvs_c NOT LIKE '%:%'),
    clingen_allele_id TEXT NOT NULL DEFAULT '',
    disease_label     TEXT NOT NULL,
    mondo_id          TEXT NOT NULL DEFAULT '',
    inheritance       TEXT NOT NULL CHECK (inheritance IN (
        'AUTOSOMAL_DOMINANT', 'AUTOSOMAL_RECESSIVE', 'SEMIDOMINANT', 'X_LINKED')),
    created_by        TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One curator's worksheet on one variant. `workflows_version` pins the transcription it was filled
-- against, so a later correction cannot silently change what a stored answer meant.
CREATE TABLE curation.worksheets (
    id                TEXT PRIMARY KEY,
    variant_id        TEXT NOT NULL REFERENCES curation.variants (id),
    curator_email     TEXT NOT NULL,
    workflows_version TEXT NOT NULL,
    assigned_by       TEXT NOT NULL,
    assigned_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (variant_id, curator_email)
);

CREATE INDEX worksheets_curator_idx ON curation.worksheets (curator_email);

-- Auto-save scratch, one row per workflow, upserted. The only table in this database carrying an
-- UPDATE grant: a draft is superseded by its own next keystroke, no round reads it, and losing the
-- table would cost unsubmitted typing and nothing any reference rests on. Cascades with its
-- worksheet, since withdrawing an unstarted assignment should not be blocked by scratch.
CREATE TABLE curation.drafts (
    worksheet_id TEXT NOT NULL REFERENCES curation.worksheets (id) ON DELETE CASCADE,
    workflow_id  TEXT NOT NULL,
    assessment   BYTEA NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (worksheet_id, workflow_id)
);

-- One act of submitting. No ON DELETE CASCADE and no UPDATE: a submitted worksheet cannot be
-- withdrawn, so the manager's DELETE on worksheets reaches unsubmitted ones only, and the reference
-- a round may already have read cannot be deleted out from under it.
CREATE TABLE curation.submissions (
    id           TEXT PRIMARY KEY,
    worksheet_id TEXT NOT NULL REFERENCES curation.worksheets (id),
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    note         TEXT NOT NULL DEFAULT ''
);

CREATE INDEX submissions_worksheet_idx ON curation.submissions (worksheet_id, submitted_at DESC);

-- What a submission committed: the complete set of assessments as of that moment, never touched
-- again. A submission owns its set rather than assessments carrying a revision of their own, so a
-- resubmission that changed three workflows of twenty stays distinguishable from a re-affirmation
-- of all twenty. `assessment` is a serialized themis.curation.models.curation.Assessment.
CREATE TABLE curation.assessments (
    submission_id TEXT NOT NULL REFERENCES curation.submissions (id),
    workflow_id   TEXT NOT NULL,
    assessment    BYTEA NOT NULL,
    PRIMARY KEY (submission_id, workflow_id)
);

GRANT USAGE ON SCHEMA curation TO "${WEB_DB_USER}";
GRANT SELECT, INSERT ON curation.roles TO "${WEB_DB_USER}";
GRANT SELECT, INSERT ON curation.variants TO "${WEB_DB_USER}";
GRANT SELECT, INSERT ON curation.worksheets TO "${WEB_DB_USER}";
GRANT SELECT, INSERT ON curation.drafts TO "${WEB_DB_USER}";
GRANT SELECT, INSERT ON curation.submissions TO "${WEB_DB_USER}";
GRANT SELECT, INSERT ON curation.assessments TO "${WEB_DB_USER}";
-- Scratch only; every table that is evidence stays insert-only.
GRANT UPDATE ON curation.drafts TO "${WEB_DB_USER}";
-- A manager withdrawing a role grant or an unsubmitted assignment.
GRANT DELETE ON curation.roles TO "${WEB_DB_USER}";
GRANT DELETE ON curation.worksheets TO "${WEB_DB_USER}";
