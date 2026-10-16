-- 0017_curation_case_retired.sql -- retires the worksheet's case section and requires the clinical
-- context `0016_curation_clinical_context` added (curation-surface.md): the section's rows, stored
-- under the workflow id `case` in both tiers, are deleted.
--
-- Every registration without a clinical context, which is every one made before this deploy rolled
-- the web tier, has it written by hand before this runs. One still without it stops the migration,
-- and so does a submission whose only row is a case row, which the deletion would leave as a
-- submission of nothing. Both are named, and each is repaired by hand before the deploy is retried.

-- The body is a single-quoted string because the migration runner's statement splitter models
-- single quotes, not dollar quoting.
DO '
DECLARE
    uncontexted TEXT;
    emptied TEXT;
BEGIN
    SELECT string_agg(id || '' ('' || transcript || '':'' || hgvs_c || '', '' || mondo_id || '', ''
            || mode_of_inheritance || '')'', '', '' ORDER BY id)
        INTO uncontexted
        FROM curation.variants WHERE clinical_context IS NULL;
    SELECT string_agg(s.id, '', '' ORDER BY s.id) INTO emptied
        FROM curation.submissions AS s
        WHERE EXISTS (SELECT 1 FROM curation.assessments AS a
                WHERE a.submission_id = s.id AND a.workflow_id = ''case'')
            AND NOT EXISTS (SELECT 1 FROM curation.assessments AS a
                WHERE a.submission_id = s.id AND a.workflow_id <> ''case'');
    IF uncontexted IS NOT NULL OR emptied IS NOT NULL THEN
        RAISE EXCEPTION ''0017 cannot retire the case section: no clinical context for [%]; submissions holding nothing but a case row: [%]'',
            coalesce(uncontexted, ''''), coalesce(emptied, '''');
    END IF;
END';

DELETE FROM curation.drafts WHERE workflow_id = 'case';

DELETE FROM curation.assessments WHERE workflow_id = 'case';

ALTER TABLE curation.variants ALTER COLUMN clinical_context SET NOT NULL;
