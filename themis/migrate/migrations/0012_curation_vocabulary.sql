-- 0012_curation_vocabulary.sql -- snapshots both assessment tiers and the variant registry, and
-- drops the variant's inheritance seed, for the curation contract's move onto the shared framework
-- vocabularies (curation-surface.md). Those enums number their members differently, so every
-- assessment already written decodes to the wrong member; SQL cannot reach inside a serialized
-- proto, so the rewrite is a separate step -- `themis.curation.vocabulary_backfill`, run after this
-- migration (docs/runbooks/curation-vocabulary-deploy.md). It also makes `curation.drafts.updated_at`
-- the row's last-write time for every write from here on, which is what that rewrite's closed-window
-- check takes it for; on any later migration's snapshot the check holds by schema. The snapshot taken
-- below predates the trigger, so this run's check rests on the deployed writer naming the column on
-- every upsert, which the surface's writer does.
--
-- The `_v1` copies are what that rewrite is verified against and, if it goes wrong, restored from;
-- this database has no down-migrations, so undoing anything here is a further forward migration
-- reading a copy. `curation.variants_v1` is the only copy of the dropped column, and what such a
-- migration would put back into it. A later migration drops all three.
CREATE TABLE curation.assessments_v1 AS SELECT * FROM curation.assessments;

CREATE TABLE curation.drafts_v1 AS SELECT * FROM curation.drafts;

-- Before the drop, or the copy does not hold the column it is taken for.
CREATE TABLE curation.variants_v1 AS SELECT * FROM curation.variants;

-- The manager's registration-time seed of the curator's routing, which the curator's own
-- RoutingAssessment is now the sole record of. The column's CHECK goes with it, and every read of
-- curation.variants by a revision predating this one fails until that revision is replaced.
ALTER TABLE curation.variants DROP COLUMN inheritance;

-- `DEFAULT now()` fires on INSERT only, so an upsert that leaves `updated_at` out keeps the
-- first-insert time, and a draft saved after the surface was closed reads as saved before. The
-- trigger stamps every UPDATE instead, except inside a transaction that has run
-- `SET LOCAL curation.backfill_in_progress = 'on'`: the rewrite's write-back changes bytes, not what
-- the curator last saved, and must not restamp every draft. It guards the snapshot of any later
-- migration, not `drafts_v1` above, every row of which was written before it existed. The body is a
-- single-quoted string because the migration runner's statement splitter models single quotes, not
-- dollar quoting.
CREATE FUNCTION curation.stamp_updated_at() RETURNS trigger LANGUAGE plpgsql AS '
BEGIN
    IF current_setting(''curation.backfill_in_progress'', true) = ''on'' THEN
        RETURN NEW;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END';

COMMENT ON FUNCTION curation.stamp_updated_at() IS
    'Sets NEW.updated_at to now() on UPDATE, unless the transaction has run SET LOCAL curation.backfill_in_progress = ''on'' (themis.curation.vocabulary_backfill, whose write-back is not a write of the curator''s).';

CREATE TRIGGER drafts_stamp_updated_at BEFORE UPDATE ON curation.drafts
    FOR EACH ROW EXECUTE FUNCTION curation.stamp_updated_at();
