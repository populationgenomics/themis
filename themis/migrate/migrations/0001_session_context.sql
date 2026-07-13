-- 0001_session_context.sql -- the session-token resolution table (docs/plans/migrate-runner.md).
-- The auth service is the sole reader: it hashes an incoming session token and returns
-- the bound Project and Analysis. token_hash is the SHA-256 hex of the bearer, never
-- the plaintext. project_id and analysis_id are opaque text -- foreign keys to the
-- project and analysis tables land with the BFF-owned session-plane schema.
CREATE TABLE session_context (
    token_hash  text PRIMARY KEY,
    project_id  text NOT NULL,
    analysis_id text NOT NULL
);
