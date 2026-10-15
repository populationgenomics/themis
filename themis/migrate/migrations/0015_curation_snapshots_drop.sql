-- 0015_curation_snapshots_drop.sql -- drops the copies `0012_curation_vocabulary` took of both
-- assessment tiers and the variant registry. The one-shot rewrite onto the shared vocabularies read
-- and was verified against them, and `0014_curation_registration_mode` read the registered modes from
-- `curation.variants_v1`; both have run, and nothing else reads a copy. The rewrite's tool,
-- `themis.curation.vocabulary_backfill`, and its runbook are retired with this migration.
DROP TABLE curation.assessments_v1;

DROP TABLE curation.drafts_v1;

DROP TABLE curation.variants_v1;

COMMENT ON FUNCTION curation.stamp_updated_at() IS
    'Sets NEW.updated_at to now() on UPDATE, unless the transaction has run SET LOCAL curation.backfill_in_progress = ''on'', as an operator rewrite of the stored assessments does: changing the bytes is not a write of the curator''s.';
