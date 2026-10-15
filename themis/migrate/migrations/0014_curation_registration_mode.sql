-- 0014_curation_registration_mode.sql -- the registration fixes the whole entity a variant is
-- classified against: a manager registers a disease term and a mode of inheritance, and a disease the
-- gene causes under two modes is two registrations (curation-surface.md). `mode_of_inheritance`
-- mirrors themis.svcv4.models.ModeOfInheritance, its member names without the prefix.
--
-- The backfill reads the mode each variant was registered under from `curation.variants_v1`, the copy
-- 0012 took before dropping the column. The autosomal and semidominant modes carry over unchanged. The
-- retired vocabulary had one X-linked member, so an X-linked row takes its kind from its gene, for the
-- genes registered under it: F8 and RS1 are X-linked recessive, MECP2 X-linked dominant. A row the
-- mapping leaves without a mode stops the migration, and so does one with no snapshot row, since
-- nothing records the mode it was registered under; so does a row whose disease term is not a MONDO
-- id, and so do two rows registering one variant against one entity, which the constraints below
-- would refuse without naming them.
ALTER TABLE curation.variants ADD COLUMN mode_of_inheritance TEXT CHECK (mode_of_inheritance IN (
    'AUTOSOMAL_DOMINANT', 'AUTOSOMAL_RECESSIVE', 'SEMIDOMINANT', 'X_LINKED_DOMINANT', 'X_LINKED_RECESSIVE'));

UPDATE curation.variants AS v SET mode_of_inheritance = CASE
        WHEN s.inheritance IN ('AUTOSOMAL_DOMINANT', 'AUTOSOMAL_RECESSIVE', 'SEMIDOMINANT') THEN s.inheritance
        WHEN s.inheritance = 'X_LINKED' AND v.gene IN ('F8', 'RS1') THEN 'X_LINKED_RECESSIVE'
        WHEN s.inheritance = 'X_LINKED' AND v.gene = 'MECP2' THEN 'X_LINKED_DOMINANT'
    END
    FROM curation.variants_v1 AS s
    WHERE s.id = v.id;

-- Names every row the constraints below would refuse without naming it. The body is a single-quoted
-- string because the migration runner's statement splitter models single quotes, not dollar quoting.
DO '
DECLARE
    unmapped TEXT;
    untermed TEXT;
    duplicated TEXT;
BEGIN
    SELECT string_agg(id || '' ('' || gene || '')'', '', '' ORDER BY id) INTO unmapped
        FROM curation.variants WHERE mode_of_inheritance IS NULL;
    SELECT string_agg(id || '' ('' || gene || '', mondo_id '' || quote_literal(mondo_id) || '')'', '', '' ORDER BY id)
        INTO untermed
        FROM curation.variants WHERE mondo_id !~ ''^MONDO:[0-9]{7}$'';
    SELECT string_agg(registration, ''; '' ORDER BY registration) INTO duplicated FROM (
        SELECT string_agg(id, '', '' ORDER BY id) || '' ('' || transcript || '' '' || hgvs_c || '', '' || mondo_id || '', ''
                || mode_of_inheritance || '')'' AS registration
            FROM curation.variants WHERE mode_of_inheritance IS NOT NULL
            GROUP BY transcript, hgvs_c, mondo_id, mode_of_inheritance HAVING count(*) > 1) AS duplicates;
    IF unmapped IS NOT NULL OR untermed IS NOT NULL OR duplicated IS NOT NULL THEN
        RAISE EXCEPTION ''curation.variants rows 0014 cannot carry over: no mode of inheritance for [%]; no MONDO id for [%]; registered more than once: [%]'',
            coalesce(unmapped, ''''), coalesce(untermed, ''''), coalesce(duplicated, '''');
    END IF;
END';

ALTER TABLE curation.variants ALTER COLUMN mode_of_inheritance SET NOT NULL;

-- The disease term is required and is a MONDO id: it is part of the uniqueness key below, where two
-- spellings of one term would register the same entity twice.
ALTER TABLE curation.variants ALTER COLUMN mondo_id DROP DEFAULT;

ALTER TABLE curation.variants ADD CONSTRAINT variants_mondo_id_check CHECK (mondo_id ~ '^MONDO:[0-9]{7}$');

-- One registration per variant and entity. Two curators of one entity share a registration and its
-- worksheets; the same variant against the same disease under another mode is a registration of its
-- own.
ALTER TABLE curation.variants ADD CONSTRAINT variants_registration_key
    UNIQUE (transcript, hgvs_c, mondo_id, mode_of_inheritance);
