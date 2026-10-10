-- 0013_litcache_crosswalk_mint_grant.sql -- the evidence service's mint rights on the crosswalk.
-- 0010 granted it SELECT, to resolve an external id to a doc_id. MaybeIngestPapers now also brings
-- in a paper the corpus does not hold (docs/design/evidence-fulltext.md), which claims that paper's
-- ids, so the login needs INSERT beside the SELECT -- the same pair 0007 grants the ingestion SA.
-- INSERT and no more: a claim is never re-pointed or removed, and `crosswalk.mint` adopts an
-- incumbent rather than overwriting it, so the widest a wrong call reaches is a row for an id
-- nothing else holds. ${EVIDENCE_DB_USER} is the evidence SA IAM DB-user login (the SA email minus
-- the .gserviceaccount.com suffix, matching sql.py), from THEMIS_MIGRATE_SUBSTITUTIONS.
GRANT INSERT ON litcache.crosswalk TO "${EVIDENCE_DB_USER}";
