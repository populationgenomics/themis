-- 0009_litcache_crosswalk_case_fold.sql -- fold the case-insensitive crosswalk keys.
-- litcache.crosswalk matches byte-exact, so a paper stored under one spelling of a DOI was
-- unreachable by another; themis.litcache.crosswalk.normalise_key folds every write, and these rows
-- predate it. `COLLATE "C"` restricts lower()/upper() to ASCII, which is what normalise_key does:
-- the default collation also maps U+0130, to a spelling the code cannot produce.
-- Each DELETE keeps the lowest spelling of a (folded key, doc_id) group, so one paper reached under
-- several spellings collapses instead of colliding on the primary key. A folded key spanning two
-- doc_ids is a duplicate-paper fault and aborts the migration; to list those before re-running:
--   SELECT folded, array_agg(DISTINCT doc_id) FROM (
--     SELECT CASE WHEN external_id LIKE 'doi:%' THEN lower(external_id COLLATE "C")
--                 ELSE 'pmcid:' || upper(substring(external_id FROM 7) COLLATE "C") END AS folded,
--            doc_id FROM litcache.crosswalk
--     WHERE external_id LIKE 'doi:%' OR external_id LIKE 'pmcid:%') g
--   GROUP BY folded HAVING count(DISTINCT doc_id) > 1;
DELETE FROM litcache.crosswalk a USING litcache.crosswalk b
WHERE a.external_id LIKE 'doi:%' AND b.external_id LIKE 'doi:%' AND a.doc_id = b.doc_id
  AND lower(a.external_id COLLATE "C") = lower(b.external_id COLLATE "C")
  AND a.external_id > b.external_id COLLATE "C";

UPDATE litcache.crosswalk SET external_id = lower(external_id COLLATE "C")
WHERE external_id LIKE 'doi:%' AND external_id <> lower(external_id COLLATE "C");

DELETE FROM litcache.crosswalk a USING litcache.crosswalk b
WHERE a.external_id LIKE 'pmcid:%' AND b.external_id LIKE 'pmcid:%' AND a.doc_id = b.doc_id
  AND upper(a.external_id COLLATE "C") = upper(b.external_id COLLATE "C")
  AND a.external_id > b.external_id COLLATE "C";

UPDATE litcache.crosswalk SET external_id = 'pmcid:' || upper(substring(external_id FROM 7) COLLATE "C")
WHERE external_id LIKE 'pmcid:%'
  AND external_id <> 'pmcid:' || upper(substring(external_id FROM 7) COLLATE "C");
