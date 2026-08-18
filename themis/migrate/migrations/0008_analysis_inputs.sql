-- 0008_analysis_inputs.sql -- an Analysis is created from typed scenario inputs, not a free-form
-- prompt. `inputs` is a serialized themis.workbench.models.workbench.AnalysisInputs: the oneof case
-- is the scenario, and the BFF renders both the agent's kickoff text and every UI label from it.
-- Binary, not JSONB: the proto is the schema, and no query reads inside the payload.
--
-- Pre-scenario rows go rather than being back-filled into `free_form{prompt}`, which would be
-- lossless: nothing outside dev has run against this table, and the alternative is hand-encoding an
-- AnalysisInputs payload here. session_context references analyses, so it clears first.
DELETE FROM session_context;
DELETE FROM analyses;
ALTER TABLE analyses DROP COLUMN prompt;
ALTER TABLE analyses ADD COLUMN inputs bytea NOT NULL;
